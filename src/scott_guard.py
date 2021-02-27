#!/usr/bin/env python3
"""Keep-to-ENEX converter supporting migration to Evernote, Joplin, etc."""
import argparse
import base64
import datetime
import itertools
import json
import os
import pathlib
import sys
import time
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape


__version__ = '0.0.1'


SHORT_NAME = 'Scott-Guard'
LONG_NAME = 'Keep-to-ENEX Converter'
APP_NAME = f'{SHORT_NAME}: {LONG_NAME}'

CODE_NAME = f'keep-converter.scottguard-{__version__}'

SOURCE_APPLICATION = f'Google Keep ({LONG_NAME})'

MAX_SIZE = 250

TAG_IMPORT_NAME = 'keep-import'


#
# Patch ElementTree to support CDATA
#

class CDATA(str):  # str to smuggle past namespace

    __tag_name__ = 'CDATA'

    def __new__(cls):
        return super().__new__(cls, cls.__tag_name__)

    def __call__(self, text=None):
        element = ET.Element(self)
        element.text = text
        return element

    def wrap(self, text):
        return f"\n<![{self}[{text}]]>\n"

CDATA = CDATA()

serialize_xml_original = ET._serialize['xml']

def serialize_xml(write, elem, *args, **kwargs):
    if elem.tag is CDATA:
        write(CDATA.wrap(elem.text))
    else:
        serialize_xml_original(write, elem, *args, **kwargs)

ET._serialize_xml = ET._serialize['xml'] = serialize_xml


#
# Evernote XML (enex) builder
#

def enex_datetime(stamp):
    return datetime.datetime.fromtimestamp(stamp).strftime('%Y%m%dT%H%M%SZ')


def annotation_content(annotation):
    (title, description, url) = (
        annotation.get(key) for key in ('title', 'description', 'url')
    )

    heading = title and f"## {title}"
    body = description and f"> {description}"
    ref = url and f"[source]({url})"

    return '\n\n'.join(item for item in (heading, body, ref) if item)


def list_item_content(list_item):
    item_text = (' ' + list_item.get('text', '')).rstrip()
    item_checked = 'x' if list_item.get('isChecked', False) else ' '
    return f'- [{item_checked}]{item_text}'


def build_enex(stream, out, note_tags, import_tags, export_date):
    root = ET.Element('en-export', {
        'export-date': enex_datetime(export_date),
        'application': APP_NAME,
        'version': __version__,
    })

    for data in stream:
        note = ET.SubElement(root, 'note')

        title = ET.SubElement(note, 'title')
        title.text = data.get('title')  # attempt 1 of 3

        created = ET.SubElement(note, 'created')
        created.text = enex_datetime(0)
        updated = ET.SubElement(note, 'updated')
        updated.text = enex_datetime(data.get('userEditedTimestampUsec', 0) / 10 ** 6)

        label_names = [label['name'] for label in data.get('labels', ())] if import_tags else ()
        for label_name in itertools.chain(label_names, note_tags):
            tag = ET.SubElement(note, 'tag')
            tag.text = label_name

        list_contents = data.get('listContent', ())
        text_content = data.get('textContent', '').strip()

        if not title.text:
            # title: attempt 2 of 3: snippet from text_content
            (first_line, *_remainder) = text_content.split('\n', 1)
            if len(first_line) > 80:
                (first_split, *_remainder) = first_line[:79].rsplit(' ', 1)
                if len(first_split) > 78:
                    first_split = first_line[:78]

                title.text = first_split + ' …'
            else:
                title.text = first_line

        # NOTE: COMPAT: Smuggle newlines into Joplin via HTML <br> (and escape)
        # TODO: (This is not great. Explore flexibility / alternatives.)
        text_content = escape(text_content).replace('\n', '<br />')  # and see below

        if list_contents:
            # joplin/enex todo lists are markup in the text content so add these there
            list_content_text = '\n\n'.join(map(escape, map(list_item_content, list_contents)))  # NOTE: escape

            if text_content:
                text_content += '\n\n---\n\n' + list_content_text
            else:
                text_content = list_content_text

        # joplin/enex don't appear to support everything that keep annotations do;
        # so, throw these into the text content as well.

        annotations = data.get('annotations', ())

        # (if there's a single annotation this can treated as
        # the "true" topic of the keep note)
        try:
            (annotation,) = annotations
        except ValueError:
            annotation = annotation_title = annotation_url = None
        else:
            annotation_title = annotation.get('title', '')
            annotation_url = annotation.get('url', '')

            # title: attempt 3 of 3: singular annotation title
            if annotation_title and (
                not title.text or (
                    title.text.startswith('http') and
                    ' ' not in title.text.strip(' …')
                )
            ):
                # title is empty or just a URL: try the annotation instead:
                title.text = annotation_title

            if text_content == annotation_url or (
                title.text and text_content == f"{title.text}\n{annotation_url}"
            ):
                # text content is just repetition of information in the
                # annotation: clear it:
                text_content = ''

        content_markdown = '\n\n---\n\n'.join(
            # itertools.chain(
                # ((text_content,) if text_content else ()),        # NOTE: see below
                map(escape, map(annotation_content, annotations)),  # NOTE: escape
            # )
        )

        # NOTE: COMPAT: similar to other <br> issues, Joplin doesn't consistently
        # treat content leading annotations as proper paragraphs. Hence,
        # text_content is removed from above chain and final <br> is ensured here.
        if text_content:
            if content_markdown:
                content_markdown = f'{text_content}<br /><br />---\n\n{content_markdown}'
            else:
                content_markdown = text_content

        # NOTE: COMPAT: fromstring introduced to smuggle in <br>
        #
        # (Might be useful in case "markdown" contains HTML tags;
        # but, this shouldn't be the case, at least not with Keep.)
        #
        # content_root = ET.Element('en-note')
        # content_root.text = content_markdown
        try:
            content_root = ET.fromstring(f'''
                <en-note>
                    {content_markdown}
                </en-note>
            ''')
        except ET.ParseError as exc:
            print(f"content re-parsing error in {data.path.name}:", exc, file=sys.stderr)
            root.remove(note)
            continue

        content_xml = ("""<?xml version="1.0" encoding="UTF-8" standalone="no"?>\n"""
                       """<!DOCTYPE en-note SYSTEM "http://xml.evernote.com/pub/enml2.dtd">\n""" +
                       ET.tostring(content_root, encoding='unicode'))

        content = ET.SubElement(note, 'content')
        content.append(CDATA(content_xml))

        attributes = ET.SubElement(note, 'note-attributes')

        source = ET.SubElement(attributes, 'source')
        source.text = CODE_NAME

        source_application = ET.SubElement(attributes, 'source-application')
        source_application.text = SOURCE_APPLICATION

        if list_contents:
            reminder_order = ET.SubElement(attributes, 'reminder-order')
            reminder_order.text = 'yes'  # trick joplin into treating as todo

            if all(list_item.get('isChecked', False) for list_item in list_contents):
                reminder_done = ET.SubElement(attributes, 'reminder-done-time')
                reminder_done.text = updated.text

        if annotation_url:
            source_url = ET.SubElement(attributes, 'source-url')
            source_url.text = annotation_url

        # add our own metadata for import file
        takeout_file = ET.SubElement(attributes, 'takeout-file')
        takeout_file.text = data.path.name

        for attachment in data.get('attachments', ()):
            attachment_name = attachment.get('filePath')
            attachment_path = attachment_name and data.path.with_name(attachment_name)

            if (
                not attachment_path or
                not attachment_path.is_file() or
                not os.access(attachment_path, os.R_OK)
            ):
                print(
                    "cannot read attachment file at",
                    repr(attachment_name),
                    "from",
                    data.path,
                    file=sys.stderr,
                )
                continue

            resource = ET.SubElement(note, 'resource')

            resource_encoded = base64.b64encode(attachment_path.read_bytes())
            resource_data = ET.SubElement(resource, 'data', encoding='base64')
            resource_data.text = str(resource_encoded, encoding='utf-8')

            if mimetype := attachment.get('mimetype'):
                mime = ET.SubElement(resource, 'mime')
                mime.text = mimetype

    out.write("""<?xml version='1.0' encoding='UTF-8'?>\n""")
    out.write("""<!DOCTYPE en-export SYSTEM "http://xml.evernote.com/pub/evernote-export3.dtd">\n""")

    document = ET.ElementTree(root)

    try:
        document.write(out, encoding='unicode', xml_declaration=False)
    except BrokenPipeError:
        # just piped to head or something
        pass


#
# json -> enex converter
#

def batch(iterable, size):
    iterator = iter(iterable)

    while True:
        chunk = itertools.islice(iterator, size)

        try:
            item = next(chunk)
        except StopIteration:
            return

        yield itertools.chain((item,), chunk)


class KeepNote(dict):

    def __init__(self, data, path):
        super().__init__(data)
        self.path = path


def stream_json(paths):
    for path in paths:
        with path.open() as descriptor:
            data = json.load(descriptor)
            yield KeepNote(data, path)


def filter_stream(stream, only_pinned, ignore_pinned, only_archived, ignore_archive, only_tagged):
    only_tagged = set(only_tagged) if only_tagged else None

    for data in stream:
        if only_pinned and not data.get('isPinned', False):
            continue

        if ignore_pinned and data.get('isPinned', False):
            continue

        if only_archived and not data.get('isArchived', False):
            continue

        if ignore_archive and data.get('isArchived', False):
            continue

        if only_tagged is not None:
            for label in data.get('labels', ()):
                if label['name'] in only_tagged:
                    break
            else:
                continue

        yield data


def convert(paths, dest, only_pinned=False, ignore_pinned=False,
            only_archived=False, ignore_archive=False, only_tagged=None,
            tags=(), import_tags=True, max_size=MAX_SIZE):
    start_time = int(time.time())

    json_stream = stream_json(paths)

    filtered_stream = filter_stream(json_stream, only_pinned, ignore_pinned,
                                    only_archived, ignore_archive, only_tagged)

    for (count, json_chunk) in enumerate(batch(filtered_stream, max_size)):
        try:
            if dest == '-':
                target = sys.stdout
            else:
                dest_path = dest

                if dest_path.is_dir():
                    dest_path /= f'conversion-{start_time}-{count}.enex'

                target = dest_path.open('w')

            build_enex(json_chunk, target, tags, import_tags, start_time)
        finally:
            if target is not sys.stdout:
                target.close()


#
# CLI
#

def recursive_json_path(value):
    path = pathlib.Path(value)

    if path.is_file():
        if path.suffix.lower() != '.json':
            raise argparse.ArgumentTypeError(f"only .json files expected not: '{path}'")

        return (path,)

    if path.is_dir():
        if not any(path.glob('*.json')):
            raise argparse.ArgumentTypeError(f"directory of .json file(s) "
                                             f"expected but has none: '{path}'")

        return path.glob('*.json')

    raise argparse.ArgumentTypeError(f"file or directory of .json file(s) "
                                     f"expected but is neither: '{path}'")


def output_target(value):
    if value == '-':
        return value

    path = pathlib.Path(value)

    if path.is_dir():
        return path

    if path.exists():
        raise argparse.ArgumentTypeError(f"output file already exists: '{path}'")

    if not os.access(path, os.W_OK):
        raise argparse.ArgumentTypeError(f"output path not write-accessible: '{path}'")

    return path


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)

    parser.add_argument('--size', default=MAX_SIZE, metavar='INT', type=int,
                        help=f"maximum number of notes to write to each enex document "
                             f"(default: {MAX_SIZE})")

    parser.add_argument('--tag', default=[TAG_IMPORT_NAME], action='append', dest='tags', metavar='NAME',
                        help=f"tag(s) to apply to imported notes (default: '{TAG_IMPORT_NAME}')")
    parser.add_argument('--no-extra-tag', action='store_false', dest='do_extra_tag',
                        help="do not apply additional tag(s) to imported notes")
    parser.add_argument('--no-import-tag', action='store_false', dest='do_import_tag',
                        help="do not recreate imported notes' tags (from Keep)")
    parser.add_argument('--no-tags', action='store_false', dest='do_tag',
                        help="do not create any tags at all")

    parser.add_argument('--only-tagged', action='append', metavar='NAME',
                        help="only import notes with these tag(s)")

    pinned_group = parser.add_mutually_exclusive_group()
    pinned_group.add_argument('--only-pinned', action='store_true',
                              help="only import pinned notes")
    pinned_group.add_argument('--none-pinned', action='store_true', dest='ignore_pinned',
                              help="do not import pinned notes")

    archive_group = parser.add_mutually_exclusive_group()
    archive_group.add_argument('--none-in-archive', dest='ignore_archive', action='store_true',
                               help="do not import archived notes")
    archive_group.add_argument('--only-in-archive', dest='only_archived', action='store_true',
                               help="only import archived notes")

    parser.add_argument('--out', metavar='PATH', default='-', type=output_target,
                        help="path to an output directory or file (default: stdout)")
    parser.add_argument('path_groups', metavar='PATH', nargs='+', type=recursive_json_path,
                        help="path to a JSON file or a directory of JSON file(s)")

    args = parser.parse_args(argv)

    if args.tags and args.tags != [TAG_IMPORT_NAME] and (not args.do_tag or not args.do_extra_tag):
        parser.error("argument --tag conflicts with --no-extra-tag and with --no-tags")
    if not args.do_tag and (not args.do_import_tag or not args.do_extra_tag):
        parser.error("argument --no-tags implies and conflicts "
                     "with --no-extra-tag and with --no-import-tag")

    paths = itertools.chain.from_iterable(args.path_groups)
    tags = args.tags if (args.do_tag and args.do_extra_tag) else ()
    do_import_tag = args.do_tag and args.do_import_tag

    convert(paths, args.out, args.only_pinned, args.ignore_pinned,
            args.only_archived, args.ignore_archive,
            args.only_tagged, tags, do_import_tag, args.size)


if __name__ == '__main__':
    main()
