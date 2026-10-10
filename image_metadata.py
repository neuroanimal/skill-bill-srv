"""
Reading EXIF metadata from JPEG and HEIC/HEIF photos without external libraries.

Why a custom parser: the server has neither exiftool nor Pillow, and installing a
dependency merely to extract a date and coordinates is disproportionate. EXIF is a
TIFF structure, and reading it comes down to walking the IFD directories.

Supported containers:
  JPEG  — APP1 block with the "Exif\\0\\0" signature, followed by plain TIFF
  HEIC  — ISOBMFF (the same box format as MP4): the 'meta' box holds 'iinf' with a
          list of items and 'iloc' with their positions; we look for an 'Exif' item

Returned fields (present only when read): dates, GPS, camera, lens, exposure
parameters. The file is read in fragments — never pulled into memory.
"""

# Parsing binary formats inherently needs many branches and arguments describing
# the position in the file, endianness and entry type. Splitting this into smaller
# functions would hurt readability, because it would scatter the logic of a single
# pass over the structure across several places.
# pylint: disable=too-many-arguments,too-many-positional-arguments
# pylint: disable=too-many-branches,too-many-locals

import struct

# --- The TIFF/EXIF tags we care about ---

# Main directory (IFD0) — device information
TAG_MAKE = 0x010F
TAG_MODEL = 0x0110
TAG_ORIENTATION = 0x0112
TAG_DATETIME = 0x0132
TAG_EXIF_IFD = 0x8769
TAG_GPS_IFD = 0x8825

# EXIF sub-directory — photo parameters
TAG_EXPOSURE_TIME = 0x829A
TAG_FNUMBER = 0x829D
TAG_ISO = 0x8827
TAG_DATETIME_ORIGINAL = 0x9003
TAG_DATETIME_DIGITIZED = 0x9004
TAG_FOCAL_LENGTH = 0x920A
TAG_LENS_MODEL = 0xA434

# GPS sub-directory
GPS_LAT_REF = 1
GPS_LAT = 2
GPS_LON_REF = 3
GPS_LON = 4
GPS_ALT_REF = 5
GPS_ALT = 6

# Sizes of TIFF data types in bytes, indexed by type number
TYPE_SIZES = {1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 6: 1, 7: 1, 8: 2, 9: 4, 10: 8, 11: 4, 12: 8}


def _read_value(data, tiff_start, endian, type_id, count, value_offset):
    """
    Reads the value of a single IFD entry.

    Entries up to 4 bytes sit directly in the value field; longer ones keep an
    offset there, relative to the start of the TIFF structure — hence `tiff_start`.
    """
    size = TYPE_SIZES.get(type_id, 1) * count

    if size <= 4:
        raw = value_offset[:size]
    else:
        offset = struct.unpack(endian + 'I', value_offset)[0]
        start = tiff_start + offset
        if start + size > len(data):
            return None
        raw = data[start:start + size]

    if type_id == 2:  # ASCII
        return raw.split(b'\x00')[0].decode('utf-8', errors='replace')

    if type_id in (3, 8):  # SHORT
        vals = struct.unpack(endian + ('h' if type_id == 8 else 'H') * count, raw[:2 * count])
        return vals[0] if count == 1 else list(vals)

    if type_id in (4, 9):  # LONG
        vals = struct.unpack(endian + ('i' if type_id == 9 else 'I') * count, raw[:4 * count])
        return vals[0] if count == 1 else list(vals)

    if type_id in (5, 10):  # RATIONAL — para licznik/mianownik
        fmt = endian + ('ii' if type_id == 10 else 'II') * count
        nums = struct.unpack(fmt, raw[:8 * count])
        pairs = [(nums[i], nums[i + 1]) for i in range(0, len(nums), 2)]
        result = [(n / d if d else 0.0) for n, d in pairs]
        return result[0] if count == 1 else result

    return raw


def _parse_ifd(data, tiff_start, ifd_offset, endian):
    """Returns a dict {tag: value} for a single IFD directory."""
    pos = tiff_start + ifd_offset
    if pos + 2 > len(data):
        return {}

    count = struct.unpack(endian + 'H', data[pos:pos + 2])[0]
    entries = {}
    pos += 2

    for _ in range(count):
        if pos + 12 > len(data):
            break
        tag, type_id, num = struct.unpack(endian + 'HHI', data[pos:pos + 8])
        value = _read_value(data, tiff_start, endian, type_id, num, data[pos + 8:pos + 12])
        if value is not None:
            entries[tag] = value
        pos += 12

    return entries


def _dms_to_degrees(dms, ref):
    """
    Converts degrees/minutes/seconds into decimal degrees.

    The sign follows the hemisphere: 'S' and 'W' mean negative values.
    """
    if not isinstance(dms, list) or len(dms) < 3:
        return None
    degrees = dms[0] + dms[1] / 60.0 + dms[2] / 3600.0
    if str(ref).upper() in ('S', 'W'):
        degrees = -degrees
    return round(degrees, 7)


def _parse_tiff(data, tiff_start):
    """Parses the TIFF structure from the given position and assembles the result."""
    if tiff_start + 8 > len(data):
        return {}

    byte_order = data[tiff_start:tiff_start + 2]
    if byte_order == b'II':
        endian = '<'
    elif byte_order == b'MM':
        endian = '>'
    else:
        return {}

    ifd0_offset = struct.unpack(endian + 'I', data[tiff_start + 4:tiff_start + 8])[0]
    ifd0 = _parse_ifd(data, tiff_start, ifd0_offset, endian)

    result = {}
    for tag, key in ((TAG_MAKE, 'make'), (TAG_MODEL, 'model'),
                     (TAG_ORIENTATION, 'orientation'), (TAG_DATETIME, 'datetime')):
        if tag in ifd0:
            result[key] = ifd0[tag]

    # EXIF sub-directory — photo parameters and more precise dates
    if TAG_EXIF_IFD in ifd0:
        exif = _parse_ifd(data, tiff_start, ifd0[TAG_EXIF_IFD], endian)
        for tag, key in ((TAG_DATETIME_ORIGINAL, 'datetime_original'),
                         (TAG_DATETIME_DIGITIZED, 'datetime_digitized'),
                         (TAG_LENS_MODEL, 'lens_model'),
                         (TAG_FOCAL_LENGTH, 'focal_length'),
                         (TAG_FNUMBER, 'f_number'),
                         (TAG_ISO, 'iso'),
                         (TAG_EXPOSURE_TIME, 'exposure_time')):
            if tag in exif:
                result[key] = exif[tag]

    # GPS sub-directory
    if TAG_GPS_IFD in ifd0:
        gps = _parse_ifd(data, tiff_start, ifd0[TAG_GPS_IFD], endian)
        lat = _dms_to_degrees(gps.get(GPS_LAT), gps.get(GPS_LAT_REF, 'N'))
        lon = _dms_to_degrees(gps.get(GPS_LON), gps.get(GPS_LON_REF, 'E'))
        if lat is not None and lon is not None:
            result['latitude'] = lat
            result['longitude'] = lon
        if GPS_ALT in gps:
            altitude = gps[GPS_ALT]
            # GPSAltitudeRef == 1 means an altitude below sea level
            if gps.get(GPS_ALT_REF) == 1:
                altitude = -altitude
            result['altitude'] = altitude

    return result


def _find_jpeg_exif(fh):
    """
    Szuka bloku APP1 z EXIF w pliku JPEG.

    Returns (buffer, tiff_start_position) or (None, 0). We only walk the block
    headers, so the image data is never read.
    """
    fh.seek(0)
    if fh.read(2) != b'\xff\xd8':
        return None, 0

    while True:
        marker = fh.read(2)
        if len(marker) < 2 or marker[0] != 0xFF:
            return None, 0

        # SOS (0xDA) starts the image data — there will be no EXIF beyond it
        if marker[1] == 0xDA:
            return None, 0

        length_bytes = fh.read(2)
        if len(length_bytes) < 2:
            return None, 0
        length = struct.unpack('>H', length_bytes)[0] - 2

        if marker[1] == 0xE1:  # APP1
            payload = fh.read(length)
            if payload.startswith(b'Exif\x00\x00'):
                # The TIFF structure starts after the 6-byte signature
                return payload, 6
            continue

        fh.seek(length, 1)


def _iter_boxes(fh, start, end):
    """Iterates over ISOBMFF boxes in the range [start, end)."""
    pos = start
    while pos + 8 <= end:
        fh.seek(pos)
        header = fh.read(8)
        if len(header) < 8:
            return
        size = struct.unpack('>I', header[:4])[0]
        name = header[4:8].decode('latin-1', errors='replace')
        data_start = pos + 8

        if size == 0:
            size = end - pos
        elif size == 1:
            size = struct.unpack('>Q', fh.read(8))[0]
            data_start += 8

        if size < 8:
            return

        yield name, data_start, min(pos + size, end)
        pos += size


def _heic_exif_item_id(fh, start, end):
    """Finds the item ID of an 'Exif' item inside the 'iinf' box."""
    fh.seek(start)
    version = fh.read(4)[0]
    count = struct.unpack('>H' if version == 0 else '>I',
                          fh.read(2 if version == 0 else 4))[0]

    pos = fh.tell()
    for _ in range(count):
        if pos + 8 > end:
            return None
        fh.seek(pos)
        header = fh.read(8)
        size = struct.unpack('>I', header[:4])[0]
        if header[4:8] != b'infe' or size < 12:
            pos += max(size, 8)
            continue

        body = fh.read(min(size - 8, 64))
        infe_version = body[0]
        if infe_version >= 2:
            # infe v2/v3: item_ID, protection, item_type (4 znaki)
            item_id = struct.unpack('>H', body[4:6])[0] if infe_version == 2 \
                else struct.unpack('>I', body[4:8])[0]
            type_at = 8 if infe_version == 2 else 10
            if body[type_at:type_at + 4] == b'Exif':
                return item_id
        pos += size

    return None


def _heic_item_offset(fh, start, end, want_id):
    """Reads the position of the item with the given ID from the 'iloc' box."""
    fh.seek(start)
    header = fh.read(4)
    version = header[0]

    packed = fh.read(2)
    if len(packed) < 2:
        return None
    offset_size = packed[0] >> 4
    length_size = packed[0] & 0x0F
    base_size = packed[1] >> 4

    count_raw = fh.read(2 if version < 2 else 4)
    count = struct.unpack('>H' if version < 2 else '>I', count_raw)[0]

    def read_int(size):
        if size == 0:
            return 0
        raw = fh.read(size)
        return int.from_bytes(raw, 'big') if raw else 0

    for _ in range(count):
        if fh.tell() >= end:
            return None
        item_id = struct.unpack('>H' if version < 2 else '>I',
                                fh.read(2 if version < 2 else 4))[0]
        if version in (1, 2):
            fh.read(2)  # construction_method — we only support a file offset
        fh.read(2)      # data_reference_index
        base_offset = read_int(base_size)
        extent_count = struct.unpack('>H', fh.read(2))[0]

        for _extent in range(extent_count):
            if version in (1, 2):
                read_int(0)  # extent_index unused at our sizes
            extent_offset = read_int(offset_size)
            extent_length = read_int(length_size)
            if item_id == want_id:
                return base_offset + extent_offset, extent_length

    return None


def _find_heic_exif(fh, total):
    """Returns (buffer, tiff_start_position) for a HEIC file, or (None, 0)."""
    meta_range = None
    for name, data_start, box_end in _iter_boxes(fh, 0, total):
        if name == 'meta':
            # 'meta' is a full box — it has 4 version bytes before its children
            meta_range = (data_start + 4, box_end)
            break

    if not meta_range:
        return None, 0

    item_id = None
    location = None
    for name, data_start, box_end in _iter_boxes(fh, *meta_range):
        if name == 'iinf':
            item_id = _heic_exif_item_id(fh, data_start, box_end)
        elif name == 'iloc' and item_id is not None:
            location = _heic_item_offset(fh, data_start, box_end, item_id)

    if not location:
        return None, 0

    offset, length = location
    fh.seek(offset)
    payload = fh.read(min(length, 128 * 1024))
    if len(payload) < 4:
        return None, 0

    # The Exif item payload starts with a 4-byte offset to the TIFF header
    tiff_offset = struct.unpack('>I', payload[:4])[0] + 4
    return payload, tiff_offset


def extract_image_metadata(path):
    """
    Reads EXIF from a JPEG or HEIC/HEIF photo.

    Returns a dict with dates, coordinates and camera data. When EXIF is absent it
    returns a diagnostic note rather than raising — missing metadata is a normal
    case (screenshots, for instance).
    """
    try:
        with open(path, 'rb') as fh:
            fh.seek(0, 2)
            total = fh.tell()

            fh.seek(0)
            head = fh.read(12)

            if head[:2] == b'\xff\xd8':
                buffer, tiff_start = _find_jpeg_exif(fh)
                container = 'jpeg'
            elif head[4:8] == b'ftyp':
                buffer, tiff_start = _find_heic_exif(fh, total)
                container = 'heic'
            else:
                return {'skipped': 'unrecognised image format'}

            if buffer is None:
                return {'container': container, 'skipped': 'no EXIF block'}

            result = _parse_tiff(buffer, tiff_start)
            result['container'] = container
            return result
    except (OSError, struct.error, ValueError, IndexError) as exc:
        # Corrupted or incomplete EXIF must not bring the transfer down — the file
        # is already on disk and the metadata is only an addition.
        return {'error': f'EXIF read error: {exc}'}


if __name__ == '__main__':
    import json
    import sys

    if len(sys.argv) < 2:
        print('Usage: python3 image_metadata.py <file.jpg|file.heic>')
        sys.exit(1)
    print(json.dumps(extract_image_metadata(sys.argv[1]), indent=2, ensure_ascii=False))
