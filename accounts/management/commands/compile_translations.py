"""Compile locale/<lang>/LC_MESSAGES/django.po into the binary django.mo Django reads.

Django's own ``compilemessages`` shells out to GNU gettext's ``msgfmt``, which isn't
installed on Windows dev machines or on the Railway image. The .mo format is small and
fully documented (GNU gettext manual, "The Format of GNU MO Files"), so this command
writes it directly in pure Python. The compiled .mo files are committed to git so the
deployed app never needs to run this.

Usage:  python manage.py compile_translations
"""

import ast
import struct
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError


def parse_po(text):
    """Return {msgid_key: msgstr_bytes} for every translated entry in a .po file.

    Handles the subset of the .po syntax this project uses: ``#`` comments,
    ``msgctxt``, ``msgid``/``msgstr``, plural ``msgid_plural``/``msgstr[n]``, and
    multi-line strings continued on following lines. Untranslated entries (empty
    msgstr) are skipped so Django falls back to the English source text.
    """
    entries = []
    current = {}
    last_key = None

    def finish():
        if 'msgid' in current:
            entries.append(dict(current))
        current.clear()

    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith('#'):
            # A blank line or comment after a complete entry starts a new one.
            if 'msgstr' in current or any(k.startswith('msgstr[') for k in current):
                finish()
            last_key = None
            continue
        if line.startswith('"'):
            if last_key is None:
                raise CommandError(f'line {lineno}: continuation string with no keyword')
            current[last_key] += ast.literal_eval(line)
            continue
        keyword, _, value = line.partition(' ')
        if keyword in ('msgctxt', 'msgid') and ('msgstr' in current or any(k.startswith('msgstr[') for k in current)):
            finish()
        current[keyword] = ast.literal_eval(value.strip())
        last_key = keyword
    finish()

    catalog = {}
    for entry in entries:
        msgid = entry['msgid']
        if 'msgid_plural' in entry:
            forms = [entry[k] for k in sorted(k for k in entry if k.startswith('msgstr['))]
            if not all(forms):
                continue
            key = msgid + '\x00' + entry['msgid_plural']
            value = '\x00'.join(forms)
        else:
            value = entry.get('msgstr', '')
            key = msgid
            if not value:
                continue
        if 'msgctxt' in entry:
            key = entry['msgctxt'] + '\x04' + key
        catalog[key] = value
    return catalog


def build_mo(catalog):
    """Serialize a {msgid: msgstr} dict to GNU .mo bytes (little-endian, no hash table)."""
    keys = sorted(catalog)  # the format requires msgids sorted for binary search
    ids = [k.encode('utf-8') for k in keys]
    strs = [catalog[k].encode('utf-8') for k in keys]

    count = len(keys)
    header_size = 7 * 4
    ids_table = header_size
    strs_table = ids_table + count * 8
    data_start = strs_table + count * 8

    id_offsets, str_offsets, blob = [], [], b''
    for data, offsets in ((ids, id_offsets), (strs, str_offsets)):
        for item in data:
            offsets.append((len(item), data_start + len(blob)))
            blob += item + b'\x00'

    out = struct.pack('<7I', 0x950412DE, 0, count, ids_table, strs_table, 0, data_start)
    for length, offset in id_offsets + str_offsets:
        out += struct.pack('<2I', length, offset)
    return out + blob


class Command(BaseCommand):
    help = 'Compile locale/*/LC_MESSAGES/*.po files to .mo without GNU gettext.'

    def handle(self, *args, **options):
        po_files = [po for base in settings.LOCALE_PATHS for po in Path(base).glob('*/LC_MESSAGES/*.po')]
        if not po_files:
            raise CommandError('No .po files found under LOCALE_PATHS.')
        for po in po_files:
            catalog = parse_po(po.read_text(encoding='utf-8'))
            po.with_suffix('.mo').write_bytes(build_mo(catalog))
            # The '' key is the header entry, not a real translation.
            self.stdout.write(f'{po}: {len(catalog) - ("" in catalog)} translated strings')
