"""
Extracting metadata from MP4/MOV (QuickTime) containers without external tools.

Why: the iPhone app does not always supply GPS or dates — in Expo Go the asset's
location is unavailable. The file, however, carries that data in its own atoms, so
the server can read it itself and correct the target directory accordingly.

Atoms read:
  moov/mvhd            — creation and modification time (seconds since 1904-01-01 UTC)
  moov/udta/(c)xyz     — location (legacy format) in ISO 6709, e.g. "+50.0647+019.9449/"
  moov/meta/keys+ilst  — location (new Apple format,
                         com.apple.quicktime.location.ISO6709), typical for iPhone .mov
  moov/meta/ilst       — remaining tags (make, model) when present

The 'meta' atom comes in two variants — ISO (with a 4-byte version+flags field) and
QuickTime (without it); the parser detects the variant by content, because .mov
files from the iPhone use the QuickTime variant.

The parser deliberately never loads the file into memory — it hops across atom
headers, so it works on multi-gigabyte files.
"""

import re
import struct
from datetime import datetime, timedelta

# QuickTime counts time from 1904-01-01, not from the UNIX epoch
QT_EPOCH = datetime(1904, 1, 1)

# Atoms that are containers — we descend into these recursively.
# 'keys' and 'ilst' are needed for the new Apple location format
# (com.apple.quicktime.location.ISO6709): 'keys' maps index -> key name,
# 'ilst' holds the values under the same index.
CONTAINERS = {'moov', 'udta', 'trak', 'mdia', 'minf', 'stbl', 'meta', 'keys', 'ilst'}

# QuickTime location atom (legacy format): 0xA9 + 'xyz' (copyright sign + xyz)
GEO_ATOM = b'\xa9xyz'

# New Apple location format: a textual key in 'moov/meta/keys' whose value
# (ISO 6709) sits in 'moov/meta/ilst' under the same index.
APPLE_LOC_KEY = b'com.apple.quicktime.location.ISO6709'

# ISO 6709: +DD.DDDD+DDD.DDDD/  (optionally with altitude)
ISO6709 = re.compile(
    rb'([+-]\d+\.?\d*)([+-]\d+\.?\d*)([+-]\d+\.?\d*)?/'
)


def _iter_atoms(fh, start, end):
    """
    Iterates over atoms in the range [start, end), yielding (name, data_offset, end).

    Does not descend recursively — recursion is left to the caller, so it can decide
    which containers are worth entering.
    """
    pos = start
    while pos + 8 <= end:
        fh.seek(pos)
        header = fh.read(8)
        if len(header) < 8:
            return
        size = struct.unpack('>I', header[:4])[0]
        name = header[4:8]
        data_start = pos + 8

        if size == 0:
            # size 0 = the atom extends to the end of the file
            size = end - pos
        elif size == 1:
            # size 1 = rozmiar 64-bitowy w kolejnych 8 bajtach
            size = struct.unpack('>Q', fh.read(8))[0]
            data_start += 8

        if size < 8:
            return

        atom_end = min(pos + size, end)
        yield name.decode('latin-1'), data_start, atom_end
        pos += size


def _parse_mvhd(fh, data_start):
    """Returns (created, modified) as UTC datetimes, or (None, None) on error."""
    fh.seek(data_start)
    version_flags = fh.read(4)
    if len(version_flags) < 4:
        return None, None

    if version_flags[0] == 0:
        raw = fh.read(8)
        if len(raw) < 8:
            return None, None
        created, modified = struct.unpack('>II', raw)
    else:
        raw = fh.read(16)
        if len(raw) < 16:
            return None, None
        created, modified = struct.unpack('>QQ', raw)

    def to_dt(seconds):
        # 0 means "not set"; absurdly large values indicate corrupted data
        if not seconds or seconds > 4_000_000_000:
            return None
        try:
            return QT_EPOCH + timedelta(seconds=seconds)
        except OverflowError:
            return None

    return to_dt(created), to_dt(modified)


def _parse_geo(fh, data_start, atom_end):
    """Zwraca (lat, lon) z atomu (c)xyz albo (None, None)."""
    fh.seek(data_start)
    payload = fh.read(min(atom_end - data_start, 256))
    match = ISO6709.search(payload)
    if not match:
        return None, None
    try:
        return float(match.group(1)), float(match.group(2))
    except (TypeError, ValueError):
        return None, None


def _meta_child_offset(fh, data_start, atom_end):
    """
    Returns the offset of the first child of a 'meta' atom, handling both variants.

    ISO 'meta' (MP4) has a 4-byte version+flags field before the first child;
    QuickTime 'meta' (typical for iPhone .mov) does NOT — the child starts right
    away. Always assuming the ISO variant shifted the entry point 4 bytes to the
    wrong place and the entire content of 'meta' (including the location) was skipped.

    We tell them apart by content: if a valid atom header sits right after the
    'meta' header (a sensible size plus an alphanumeric name such as 'hdlr'/'keys'),
    it is the QuickTime variant — no version field. Otherwise ISO — with +4.
    """
    fh.seek(data_start)
    head = fh.read(8)
    if len(head) >= 8:
        size = struct.unpack('>I', head[:4])[0]
        name = head[4:8]
        # An atom name is normally 4 printable ASCII characters; a leading 0x00
        # (as in ISO version+flags) does not satisfy that.
        looks_like_atom = (
            8 <= size <= (atom_end - data_start)
            and all(0x20 <= b < 0x7f for b in name)
        )
        if looks_like_atom:
            return data_start          # QuickTime: child starts immediately
    return data_start + 4              # ISO: skip version+flags


def _parse_apple_location(fh, meta_start, meta_end):
    """
    Returns (lat, lon) from the new Apple format, or (None, None).

    The new-style location is split across two 'meta' sub-atoms:
      keys : a list of key names, indexed from 1
      ilst : the values, each in a container numbered by the key index
    Instead of fully mapping keys<->ilst it is enough to find, within 'meta', the
    raw ISO 6709 string adjacent to the location key name — the value is stored as
    ISO 6709 text in 'ilst' anyway. So we scan the content of 'meta' for the
    ISO 6709 pattern; restricting the scan to the inside of 'meta' guards against
    hitting a coincidental string in the video data (mdat lies outside 'meta').
    """
    fh.seek(meta_start)
    blob = fh.read(max(0, meta_end - meta_start))
    if APPLE_LOC_KEY not in blob:
        return None, None
    match = ISO6709.search(blob)
    if not match:
        return None, None
    try:
        return float(match.group(1)), float(match.group(2))
    except (TypeError, ValueError):
        return None, None


def extract_mp4_metadata(path):
    """
    Czyta metadane z pliku MP4/MOV.

    Returns a dict with the following keys (present only when read):
        created_utc  — ISO 8601, czas utworzenia z mvhd
        modified_utc — ISO 8601, czas modyfikacji z mvhd
        latitude     — float
        longitude    — float
        atoms        — list of top-level atoms encountered (diagnostics)
    """
    result = {'atoms': []}

    try:
        with open(path, 'rb') as fh:
            fh.seek(0, 2)
            total = fh.tell()

            def scan(start, end, depth=0):
                # The depth limit guards against looping on a corrupted file
                if depth > 6:
                    return
                for name, data_start, atom_end in _iter_atoms(fh, start, end):
                    if depth == 0:
                        result['atoms'].append(name)

                    if name == 'mvhd' and 'created_utc' not in result:
                        created, modified = _parse_mvhd(fh, data_start)
                        if created:
                            result['created_utc'] = created.isoformat()
                        if modified:
                            result['modified_utc'] = modified.isoformat()

                    elif name == GEO_ATOM.decode('latin-1') and 'latitude' not in result:
                        lat, lon = _parse_geo(fh, data_start, atom_end)
                        if lat is not None:
                            result['latitude'] = lat
                            result['longitude'] = lon

                    if name in CONTAINERS:
                        inner = data_start
                        if name == 'meta':
                            # 'meta' comes in two variants: ISO (with a 4-byte
                            # version+flags field) and QuickTime (without it). Picking
                            # the wrong variant broke parsing of 'meta' content on
                            # iPhone .mov files. We determine the variant by content.
                            inner = _meta_child_offset(fh, data_start, atom_end)
                            # The new Apple location format lives inside this 'meta'
                            # (keys+ilst). We try it if the legacy (c)xyz was absent.
                            if 'latitude' not in result:
                                lat, lon = _parse_apple_location(fh, data_start, atom_end)
                                if lat is not None:
                                    result['latitude'] = lat
                                    result['longitude'] = lon
                        scan(inner, atom_end, depth + 1)

            scan(0, total)
    except OSError as exc:
        result['error'] = f'could not read file: {exc}'

    return result


if __name__ == '__main__':
    import json
    import sys

    if len(sys.argv) < 2:
        print('Usage: python3 mp4_metadata.py <file.mp4>')
        sys.exit(1)
    print(json.dumps(extract_mp4_metadata(sys.argv[1]), indent=2, ensure_ascii=False))
