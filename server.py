"""
Skill Bill Server — receiving files from the iPhone (Expo Go) through ngrok.

Endpoints:
  GET  /ping          — health check
  POST /manifest      — file registration: derives the target directory from metadata (date + GPS)
  GET  /probe         — how many bytes we already have (for resuming an upload)
  POST /upload-chunk  — receives the next fragment of a file (offset-based resume)

The upload is offset-based, not index-based: the client asks /probe for `uploaded`,
then sends a fragment with an X-Offset header. Fragments may arrive in any order;
the server writes each at its stated offset and tracks the received byte ranges,
which keeps resuming free of duplicates and gaps after a dropped connection.
"""

import base64
import json
import os
import re
import shutil
import threading
import time
from datetime import datetime, timezone

import requests
from flask import Flask, jsonify, request

from image_metadata import extract_image_metadata
from mp4_metadata import extract_mp4_metadata
import verify_index

app = Flask(__name__)

BASE_UPLOAD_DIR = '/mnt/c/gs/upload.go'
# pending/ — files mid-transfer (incomplete) + a .target sidecar with the destination path
PENDING_DIR = os.path.join(BASE_UPLOAD_DIR, '_pending')
# Registry of completed transfers: session key -> target path.
#
# Needed because the session sidecar is removed on completion, so /probe cannot tell
# a never-started file from an already-downloaded one. The queue must know this in
# order not to copy from Photos and re-send tens of gigabytes.
COMPLETED_INDEX = os.path.join(PENDING_DIR, '_completed.json')

os.makedirs(BASE_UPLOAD_DIR, exist_ok=True)
os.makedirs(PENDING_DIR, exist_ok=True)

# Reverse-geocoding cache: (lat, lon, language) -> raw address dict.
# Nominatim allows 1 request per second, and many photos from one session share
# almost identical coordinates — without the cache we would be cut off after a few files.
_GEO_CACHE = {}

# Timestamp of the last Nominatim request — used to honour the 1/s rate limit.
_LAST_GEO_CALL = 0.0


def sanitize_segment(name):
    """Clean a name for path use (Windows rejects < > : " | ? * and trailing spaces)."""
    if not name:
        return ''
    cleaned = re.sub(r'[<>:"|?*\\/]', '_', str(name))
    cleaned = cleaned.replace(' ', '_').strip('._')
    return cleaned


# Tiers of the location hierarchy: one directory per tier, the first present key wins.
#
# Splitting into tiers rather than using a flat list of keys solves a real problem:
# Nominatim can return the same unit under two keys in two languages
# (city_district="Old Town" and suburb="Stare Miasto" are the same district of Krakow).
# Comparing strings will not catch that, but since both keys describe the same
# administrative tier, taking a single name from it is enough.
#
# At the same time district and neighbourhood are separate tiers, so the
# Debniki/Zakrzowek case still yields two directory levels.
LOCATION_TIERS = (
    ('country',),
    ('state', 'region'),
    ('county',),
    ('city', 'town', 'village', 'municipality'),
    ('city_district', 'borough', 'suburb'),
    ('neighbourhood', 'quarter', 'hamlet'),
)


def _geocode_raw(lat, lon, lang):
    """
    A single Nominatim request; returns the raw `address` dict.

    The cache is keyed by coordinates rounded to ~100 m AND by language: photos from
    one session have nearly identical coordinates, so without the cache we would send
    dozens of requests about the same place and get cut off.
    """
    cache_key = (round(lat, 3), round(lon, 3), lang)
    if cache_key in _GEO_CACHE:
        return _GEO_CACHE[cache_key]

    # Nominatim allows 1 request per second. With a queue of thousands of files,
    # ignoring that limit would get us banned and the whole place hierarchy would be lost.
    global _LAST_GEO_CALL  # pylint: disable=global-statement
    wait = 1.1 - (time.monotonic() - _LAST_GEO_CALL)
    if wait > 0:
        time.sleep(wait)

    address = {}
    try:
        resp = requests.get(
            'https://nominatim.openstreetmap.org/reverse',
            params={'format': 'json', 'lat': lat, 'lon': lon, 'zoom': 18, 'addressdetails': 1},
            headers={
                'User-Agent': 'skill-bill-uploader/1.0',
                'Accept-Language': lang,
            },
            timeout=8,
        )
        if resp.status_code == 200:
            address = resp.json().get('address', {})
    except (requests.RequestException, ValueError, KeyError):
        address = {}
    finally:
        _LAST_GEO_CALL = time.monotonic()

    _GEO_CACHE[cache_key] = address
    return address


def _address_to_tiers(address):
    """Builds the list of path segments from a raw address dict, one per tier."""
    hierarchy = []
    for tier_keys in LOCATION_TIERS:
        for key in tier_keys:
            val = address.get(key)
            if not val:
                continue
            seg = sanitize_segment(val)
            # The same name can appear on several tiers
            # (e.g. city=Krakow, county=Krakow) — we do not duplicate directories.
            if seg and seg not in hierarchy:
                hierarchy.append(seg)
            break  # tier handled, move on to the next one
    return hierarchy


def get_location_hierarchy(lat, lon, lang='en'):
    """
    Reverse geocoding via Nominatim (OpenStreetMap, free, no key required).
    Returns the hierarchy: country -> region -> county -> city -> district -> neighbourhood.
    With no GPS, or on error, returns an empty hierarchy (the file lands in No_GPS/).
    """
    try:
        lat = float(lat)
        lon = float(lon)
    except (TypeError, ValueError):
        return []

    if lat == 0 and lon == 0:
        return []

    return _address_to_tiers(_geocode_raw(lat, lon, lang))


def describe_location(lat, lon):
    """
    Place description in both languages — to be stored in the metadata file.

    Directories are built from the English variant, but we also keep the Polish one
    in the metadata: local names are sometimes the only recognisable ones (Zakrzowek,
    Bronowice), and when browsing the archive years later every hint counts.

    Returns a dict with keys 'en' and 'pl', each with a hierarchy and the full address.
    """
    try:
        lat = float(lat)
        lon = float(lon)
    except (TypeError, ValueError):
        return {}

    if lat == 0 and lon == 0:
        return {}

    result = {}
    for lang in ('en', 'pl'):
        address = _geocode_raw(lat, lon, lang)
        if not address:
            continue
        result[lang] = {
            'hierarchy': _address_to_tiers(address),
            'address': address,
        }
    return result


def parse_timestamp(meta):
    """
    Extract the creation date from metadata.
    Priority: EXIF 'date' ("2025:06:12 17:35:12") > 'timestamp' (unix, seconds or ms).
    """
    raw_date = meta.get('date')
    if raw_date:
        text = str(raw_date)
        # EXIF: 2025:06:12 17:35:12
        m = re.match(r'(\d{4}):(\d{2}):(\d{2})[ T](\d{2}):(\d{2}):(\d{2})', text)
        if m:
            return datetime(*(int(g) for g in m.groups()))
        # ISO: 2025-06-12T17:35:12
        m = re.match(r'(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2}):(\d{2})', text)
        if m:
            return datetime(*(int(g) for g in m.groups()))

    ts = meta.get('timestamp')
    if ts:
        try:
            ts = float(ts)
            # iOS reports modificationTime in milliseconds — detect by magnitude
            if ts > 1e11:
                ts /= 1000.0
            return datetime.fromtimestamp(ts)
        except (TypeError, ValueError, OSError):
            pass
    return None


def build_target_dir(meta):
    """
    Build the target directory, interleaving date with location:
        <YEAR>/<Country>/<Region>/<City>/<YYYYMMDD>/<District>/<Neighbourhood>/<HHMM>/
    The year and YYYYMMDD slot in between the location levels — as agreed, the
    hierarchy is meant to mix time and place.
    No GPS  -> .../No_GPS/...   No date -> Unknown_Date/
    """
    dt = parse_timestamp(meta)
    geo = get_location_hierarchy(meta.get('latitude'), meta.get('longitude'))

    parts = []

    # Year
    parts.append(str(dt.year) if dt else 'Unknown_Date')

    # Location: first 3 levels (country/region/city)
    if geo:
        parts.extend(geo[:3])
    else:
        parts.append('No_GPS')

    # Full date
    if dt:
        parts.append(dt.strftime('%Y%m%d'))

    # Location: remaining levels (district/neighbourhood)
    if len(geo) > 3:
        parts.extend(geo[3:])

    # Hour and minute — groups files from a single shooting session
    if dt:
        parts.append(dt.strftime('%H%M'))

    return os.path.join(BASE_UPLOAD_DIR, *parts)


def session_paths(session_id):
    """Working file paths for a session: partial data + metadata sidecar."""
    safe = sanitize_segment(session_id) or 'session'
    return (
        os.path.join(PENDING_DIR, safe + '.part'),
        os.path.join(PENDING_DIR, safe + '.json'),
    )


def merge_ranges(ranges):
    """
    Merges overlapping and adjacent byte ranges.

    Ranges are kept as a list of [start, end) pairs in ascending order. Merging after
    every append keeps the list short — with a sequential transfer (even when chunks
    arrive out of order) usually a single range remains.
    """
    if not ranges:
        return []

    ordered = sorted(ranges)
    merged = [list(ordered[0])]
    for start, end in ordered[1:]:
        if start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return [list(pair) for pair in merged]


def contiguous_from_zero(ranges):
    """
    How many bytes we have in an unbroken run from the start of the file.

    This is the number we give the client as its resume point: data beyond the first
    gap is stored, but we cannot confirm it as a complete prefix.
    """
    if not ranges or ranges[0][0] != 0:
        return 0
    return ranges[0][1]


def total_received(ranges):
    """Sum of bytes across all ranges — used to detect completeness."""
    return sum(end - start for start, end in ranges)


# Per-session locks. With parallel chunks several requests update the same state
# file and the same data file at once — without a lock the writes would overwrite
# each other's range list and the transfer would end up with gaps.
_SESSION_LOCKS = {}
_LOCKS_GUARD = threading.Lock()


def get_session_lock(session_id):
    """Returns the lock for a given session, creating it on first use."""
    with _LOCKS_GUARD:
        if session_id not in _SESSION_LOCKS:
            _SESSION_LOCKS[session_id] = threading.Lock()
        return _SESSION_LOCKS[session_id]


def rebuild_completed_index():
    """
    Rebuilds the registry of completed transfers by scanning *.meta.json files.

    The registry is secondary to the data on disk: every downloaded file has a
    sidecar with its session id next to it, so it can be reconstructed without
    losing knowledge of what we already have. Thanks to that, deleting or corrupting
    the registry does not cause tens of gigabytes to be re-sent.
    """
    index = {}
    for root, _dirs, files in os.walk(BASE_UPLOAD_DIR):
        # The working directory holds unfinished sessions — those do not belong here
        if os.path.abspath(root).startswith(os.path.abspath(PENDING_DIR)):
            continue
        for name in files:
            if not name.endswith('.meta.json'):
                continue
            sidecar = os.path.join(root, name)
            try:
                with open(sidecar, encoding='utf-8') as fh:
                    record = json.load(fh)
            except (OSError, ValueError):
                continue

            session_id = record.get('session_id')
            target = record.get('target_file')
            if not session_id or not target or not os.path.exists(target):
                continue

            index[sanitize_segment(session_id)] = {
                'target_file': target,
                'size': (record.get('metadata') or {}).get('size'),
                'completed_at': record.get('completed_at'),
            }

    if index:
        with open(COMPLETED_INDEX, 'w', encoding='utf-8') as fh:
            json.dump(index, fh, indent=2, ensure_ascii=False)
        print(f'[INDEX] Registry rebuilt from sidecars: {len(index)} files')
    return index


def load_completed():
    """Loads the registry of completed transfers, rebuilding it when absent."""
    if not os.path.exists(COMPLETED_INDEX):
        return rebuild_completed_index()
    try:
        with open(COMPLETED_INDEX, encoding='utf-8') as fh:
            return json.load(fh)
    except (OSError, ValueError):
        # A corrupted registry is rebuilt from disk rather than starting empty —
        # otherwise everything would be sent a second time.
        print('[INDEX] Registry corrupted — rebuilding from sidecars')
        return rebuild_completed_index()


def mark_completed(session_id, target_file, size):
    """Appends a completed transfer to the registry."""
    index = load_completed()
    index[sanitize_segment(session_id)] = {
        'target_file': target_file,
        'size': size,
        'completed_at': datetime.now(timezone.utc).isoformat(),
    }
    with open(COMPLETED_INDEX, 'w', encoding='utf-8') as fh:
        json.dump(index, fh, indent=2, ensure_ascii=False)


@app.route('/ping', methods=['GET'])
def ping():
    """Health check — used by the app to display the server status."""
    return jsonify({'status': 'ok', 'message': 'Skill Bill Server is running!'})


@app.route('/log', methods=['POST'])
def log_from_client():
    """
    The app sends logs — we print them in the server console.
    This makes debugging easier without transcribing messages off the phone screen.

    Two formats:
      {"level": ..., "message": ...}   — a single entry
      {"lines": [{"level","message"}]} — a batch collected during a transfer,
                                        while remote logging was muted
    """
    data = request.get_json(silent=True) or {}

    lines = data.get('lines')
    if isinstance(lines, list):
        for entry in lines:
            level = str(entry.get('level', 'info')).upper()
            print(f'[APP {level}] {entry.get("message", "")}')
        return jsonify({'status': 'ok', 'received': len(lines)})

    level = str(data.get('level', 'info')).upper()
    print(f'[APP {level}] {data.get("message", "")}')
    return jsonify({'status': 'ok'})


@app.route('/manifest', methods=['POST'])
def receive_manifest():
    """
    Registers a file before the upload.

    Input:
      {"session_id": "...", "file": {"name": ..., "size": ..., "date": ..., "timestamp": ...,
                                     "latitude": ..., "longitude": ..., ...}}

    Writes a JSON sidecar with the full metadata and the computed target path, and
    returns how many bytes we already have (so the client can resume immediately).
    """
    data = request.get_json(silent=True) or {}
    meta = data.get('file') or {}
    session_id = data.get('session_id') or meta.get('name')

    if not session_id or not meta.get('name'):
        return jsonify({'status': 'error', 'message': 'Wymagane: session_id i file.name'}), 400

    target_dir = build_target_dir(meta)
    # Registration touches only the state sidecar; the data file is created on the
    # first chunk, which is why its path is not needed here.
    _, meta_path = session_paths(session_id)

    # The list of already received ranges MUST survive re-registration.
    # The queue calls /manifest before every send attempt, including on resume —
    # overwriting the sidecar with a clean record would wipe the state and the
    # transfer would start from zero even though the data is already on disk.
    previous_ranges = read_ranges(meta_path)

    record = {
        'session_id': session_id,
        'target_dir': target_dir,
        'target_file': os.path.join(target_dir, sanitize_segment(meta['name'])),
        'registered_at': datetime.now(timezone.utc).isoformat(),
        'metadata': meta,
        'ranges': previous_ranges,
    }
    with open(meta_path, 'w', encoding='utf-8') as fh:
        json.dump(record, fh, indent=2, ensure_ascii=False)

    return jsonify({
        'status': 'ok',
        'session_id': session_id,
        'target_dir': target_dir,
        'uploaded': contiguous_from_zero(previous_ranges),
        'received': total_received(previous_ranges),
        'size': meta.get('size', 0),
    })


def build_bundle():
    """
    Assembles a compact index bundle for offline work on the phone.

    Why offline: verifying well over ten thousand items takes hours, and the ngrok
    tunnel drops regularly (tls: bad record MAC). Querying the server item by item
    would mean every drop ruins a batch. With the bundle on the phone the server is
    needed once — to fetch it.

    The format is deliberately frugal: paths live in an array and both indexes hold
    only positions within that array. Without this the same path would appear twice
    and the bundle grows from 1.9 MB to 4.6 MB.

      paths     — list of paths relative to the library root
      md5       — digest -> position in paths
      name_size — "name|size" (name lowercased) -> position in paths

    The name enters the index in two variants (on-disk and original from the phone),
    because on a collision within a directory we append a suffix and the two diverge.
    """
    verify_index.refresh()
    rows = verify_index.rows()

    paths = []
    md5map = {}
    nsmap = {}
    for row in rows:
        idx = len(paths)
        paths.append(row['path'])
        # The first copy wins: for duplicated contents we point at a single path,
        # because the phone only needs to know the file is somewhere.
        md5map.setdefault(row['md5'], idx)
        # Both name variants, but without repetition: usually the on-disk and the
        # original name are identical, and then there is no point storing the same
        # pair twice.
        both_names = {row.get('disk_name'), row.get('orig_name')}
        for name in sorted(n for n in both_names if n):
            nsmap.setdefault(f'{name.lower()}|{row["size"]}', idx)

    return {
        'paths': paths,
        'md5': md5map,
        'name_size': nsmap,
        'files': len(rows),
        'unique_contents': len(md5map),
        'built_at': datetime.now(timezone.utc).isoformat(),
    }


@app.route('/verify/bundle', methods=['GET'])
def verify_bundle():
    """
    Serves the compact index for offline verification.

    The phone fetches this once and stores it locally; the rest of the run needs
    neither the server nor the network. After downloading new files to disk the index
    must be rebuilt (build_verify_index.py) and the bundle fetched again.
    """
    return jsonify({'status': 'ok', **build_bundle()})


@app.route('/verify/stats', methods=['GET'])
def verify_stats():
    """
    State of the verification index — how many files the server knows about.

    The phone calls this once at startup to show whether the index exists at all
    (build_verify_index.py creates it) and whether it is worth starting verification.
    """
    return jsonify({'status': 'ok', **verify_index.stats()})


@app.route('/verify', methods=['POST'])
def verify():
    """
    Checks a batch of files: does the library hold exactly these materials.

    Input:
      {"items": [{"id": <asset id>, "name": ..., "size": ..., "md5": ...}, ...]}

    Output: a list of results in input order, each with a status of
    verified / size-match / name-mismatch / missing (described in verify_index).

    In batches rather than one by one: the phone has well over ten thousand items
    and every request through the tunnel pays a full round trip. A hundred items in
    one request turns hours of waiting into seconds.

    MD5 is decisive. The 'verified' status means a bit-for-bit match and only it
    entitles one to delete the material from the phone — which is why the status
    names are deliberately literal, so that nobody mistakes a name-and-size
    coincidence for proof of a file's identity.
    """
    data = request.get_json(silent=True) or {}
    items = data.get('items')
    if not isinstance(items, list):
        return jsonify({'status': 'error', 'message': 'Wymagane: items (lista)'}), 400

    results = verify_index.check_many(items)

    # A summary in the response so the app does not have to compute it itself
    # for every batch.
    counts = {}
    for row in results:
        counts[row['status']] = counts.get(row['status'], 0) + 1

    return jsonify({'status': 'ok', 'counts': counts, 'results': results})


@app.route('/have', methods=['GET'])
def have():
    """
    Is the given file already fully downloaded?

    The queue asks this before copying an asset out of Photos — copying a 200 MB
    video only to discover it is already on disk would waste both space and time on
    a phone that is precisely short of space.

    It also checks that the target file physically exists: the registry may have been
    moved along with the directory while the file was deleted by hand.
    """
    session_id = request.args.get('session_id')
    if not session_id:
        return jsonify({'status': 'error', 'message': 'Brak session_id'}), 400

    entry = load_completed().get(sanitize_segment(session_id))
    if not entry:
        return jsonify({'status': 'ok', 'done': False})

    target = entry.get('target_file', '')
    if not os.path.exists(target):
        # Entry present, file absent — treated as not downloaded so it can be retried
        return jsonify({'status': 'ok', 'done': False, 'stale_entry': True})

    return jsonify({
        'status': 'ok',
        'done': True,
        'target_file': target,
        'size': entry.get('size'),
    })


def read_ranges(meta_path):
    """Loads the list of received ranges from the session sidecar."""
    if not os.path.exists(meta_path):
        return []
    try:
        with open(meta_path, encoding='utf-8') as fh:
            return merge_ranges(json.load(fh).get('ranges', []))
    except (OSError, ValueError):
        return []


@app.route('/probe', methods=['GET'])
def probe():
    """
    Session state — the resume point and the list of ranges held.

    `uploaded` is the unbroken run from the start of the file; with parallel chunks
    further ranges may also exist beyond a gap, which is why we return the full list
    and let the client decide what is still missing.
    """
    session_id = request.args.get('session_id')
    if not session_id:
        return jsonify({'status': 'error', 'message': 'Brak session_id'}), 400

    part_path, meta_path = session_paths(session_id)
    ranges = read_ranges(meta_path)

    return jsonify({
        'status': 'ok',
        'session_id': session_id,
        'registered': os.path.exists(meta_path),
        'uploaded': contiguous_from_zero(ranges),
        'received': total_received(ranges),
        'ranges': ranges,
        'part_size': os.path.getsize(part_path) if os.path.exists(part_path) else 0,
    })


@app.route('/upload-chunk', methods=['POST'])
def upload_chunk():  # pylint: disable=too-many-return-statements
    """
    Receives a fragment of a file and writes it at the stated position.

    Headers:
      X-Session-Id     — session identifier (the same as in /manifest)
      X-Offset         — byte position of this fragment within the file
      X-Encoding       — 'base64' when the body is encoded (RN cannot send raw bytes)

    Fragments may arrive in any order — the client sends several at once so that
    network latencies overlap instead of adding up. That is why we write at the
    position from X-Offset rather than appending at the end, and keep a list of
    received ranges. Completeness is checked by the sum of ranges, not by file size.
    """
    session_id = request.headers.get('X-Session-Id')
    offset = request.headers.get('X-Offset', type=int)
    encoding = (request.headers.get('X-Encoding') or '').lower()

    if not session_id or offset is None:
        print(f'[CHUNK] REJECTED — headers missing. All of them: {dict(request.headers)}')
        return jsonify({
            'status': 'error',
            'message': 'Required headers: X-Session-Id, X-Offset',
        }), 400

    part_path, meta_path = session_paths(session_id)

    if not os.path.exists(meta_path):
        return jsonify({
            'status': 'error',
            'message': 'Unknown session — POST /manifest first',
        }), 409

    # Reading the body can fail when the tunnel (ngrok) cuts the connection mid-
    # transfer — Werkzeug then raises, and without this block the client gets an
    # unexplained 400, which is impossible to diagnose from the phone.
    try:
        body = request.get_data()
    except Exception as exc:  # pylint: disable=broad-exception-caught
        # A broad exception on purpose: transport errors arrive under various classes
        # (ClientDisconnected, ConnectionResetError, chunked-decoding errors), and the
        # reaction is always the same — reject the chunk, the client will retry it.
        print(f'[CHUNK] REJECTED — could not read body: {exc!r}')
        return jsonify({'status': 'error', 'message': f'Body not read: {exc}'}), 400

    if request.content_length and len(body) != request.content_length:
        print(f'[CHUNK] REJECTED — incomplete body: {len(body)}/{request.content_length} B')
        return jsonify({
            'status': 'error',
            'message': f'Incomplete body: {len(body)}/{request.content_length}',
        }), 400

    if encoding == 'base64':
        try:
            body = base64.b64decode(body, validate=False)
        except (ValueError, TypeError) as exc:
            print(f'[CHUNK] REJECTED — base64: {exc}, first 60 B: {request.get_data()[:60]!r}')
            return jsonify({'status': 'error', 'message': f'base64 error: {exc}'}), 400

    # Writing at the stated position and updating the state — both steps under one
    # lock, because parallel chunks would otherwise overwrite each other's range list.
    with get_session_lock(sanitize_segment(session_id)):
        with open(meta_path, encoding='utf-8') as fh:
            record = json.load(fh)
        total_size = int(record['metadata'].get('size') or 0)

        # 'r+b' requires an existing file, so on the first chunk we create it with 'wb'.
        # Writing past the current end of file creates a hole (a sparse file) — the
        # filesystem fills it with zeros, and the range list tells us what is real anyway.
        mode = 'r+b' if os.path.exists(part_path) else 'wb'
        with open(part_path, mode) as fh:
            fh.seek(offset)
            fh.write(body)

        ranges = merge_ranges(record.get('ranges', []) + [[offset, offset + len(body)]])
        record['ranges'] = ranges
        with open(meta_path, 'w', encoding='utf-8') as fh:
            json.dump(record, fh, indent=2, ensure_ascii=False)

        uploaded = contiguous_from_zero(ranges)
        received = total_received(ranges)

        # Completeness is computed from the sum of ranges, not the file size: writing
        # past the end grows the size at once even though the middle may still be empty.
        done = 0 < total_size <= received

        if done:
            finalize(record, part_path, meta_path)

    return jsonify({
        'status': 'done' if done else 'ok',
        'session_id': session_id,
        'uploaded': uploaded,
        'received': received,
        'size': total_size,
        'target_file': record['target_file'] if done else None,
    })


def enrich_from_file(path, client_meta):
    """
    Supplements the client's metadata with data read from the file itself.

    The Expo Go app does not always have access to an asset's location, while the
    MP4/MOV container carries that data in its own atoms — we read it here as a
    supplementary source.

    Precedence rule: data from the phone wins when it is sensible. The phone's time
    is local and it determines the directory; the time from the file (mvhd, always
    UTC) is used only when the phone supplied nothing — and then we convert it to the
    server's local time, so as not to mix time zones within the tree.

    Returns (merged_metadata, metadata_from_file).
    """
    # The extension is taken from the ORIGINAL name, not from the path passed in
    # `path`: during the transfer the file is called "<session>.part", so checking its
    # extension would reject every file and metadata reading would never run.
    ext = os.path.splitext(client_meta.get('name', ''))[1].lower()

    if ext in ('.mp4', '.mov', '.m4v'):
        file_meta = extract_mp4_metadata(path)
        date_key = 'created_utc'
        date_is_utc = True
    elif ext in ('.jpg', '.jpeg', '.heic', '.heif', '.tif', '.tiff'):
        file_meta = extract_image_metadata(path)
        # EXIF stores the camera's local time without a zone — we do not convert it
        date_key = 'datetime_original'
        date_is_utc = False
    else:
        return client_meta, {'skipped': f'unsupported extension: {ext or "none"}'}

    merged = dict(client_meta)

    # GPS: taken from the file when the phone supplied nothing usable
    has_client_gps = False
    try:
        has_client_gps = float(client_meta.get('latitude') or 0) != 0 \
            or float(client_meta.get('longitude') or 0) != 0
    except (TypeError, ValueError):
        has_client_gps = False

    if not has_client_gps and 'latitude' in file_meta:
        merged['latitude'] = file_meta['latitude']
        merged['longitude'] = file_meta['longitude']
        print(f'[META] GPS from file: {file_meta["latitude"]}, {file_meta["longitude"]}')

    # Date: only as a fallback source, when the phone supplied nothing.
    # Video (mvhd) keeps the time in UTC and needs converting to the local zone,
    # whereas EXIF stores the camera's local time — converting it would break the hour.
    if not client_meta.get('timestamp') and not client_meta.get('date'):
        raw = file_meta.get(date_key)
        if raw:
            try:
                if date_is_utc:
                    utc_dt = datetime.fromisoformat(raw).replace(tzinfo=timezone.utc)
                    merged['date'] = utc_dt.astimezone().strftime('%Y-%m-%dT%H:%M:%S')
                    print(f'[META] Date from file (mvhd, UTC->local): {merged["date"]}')
                else:
                    # EXIF uses the "2025:06:09 19:33:01" format — build_target_dir reads it
                    merged['date'] = raw
                    print(f'[META] Date from file (EXIF, local): {raw}')
            except ValueError:
                pass

    return merged, file_meta


def finalize(record, part_path, meta_path):
    """
    Moves the complete file from _pending/ into the target directory and writes a
    <name>.meta.json file next to it with the full metadata from the phone.

    The target directory is recomputed here: only with the complete file in hand can
    we read the GPS and date the app did not supply. The path established at
    /manifest is therefore provisional and may change at this point.

    On a name collision we append a suffix _1, _2, ... — we never overwrite.
    """
    merged_meta, file_meta = enrich_from_file(part_path, record['metadata'])

    target_dir = build_target_dir(merged_meta)
    if target_dir != record['target_dir']:
        print(f'[META] Directory changed after analysing the file:\n'
              f'       was: {record["target_dir"]}\n'
              f'       now: {target_dir}')
    record['target_dir'] = target_dir
    record['target_file'] = os.path.join(target_dir, sanitize_segment(merged_meta['name']))
    record['metadata_from_file'] = file_meta
    # Place description in both languages — directories come from the English
    # variant, but we keep the Polish one in the metadata too (Bronowice, Zakrzowek).
    record['location'] = describe_location(
        merged_meta.get('latitude'), merged_meta.get('longitude')
    )

    os.makedirs(target_dir, exist_ok=True)

    target = record['target_file']
    if os.path.exists(target):
        stem, ext = os.path.splitext(target)
        idx = 1
        while os.path.exists(f'{stem}_{idx}{ext}'):
            idx += 1
        target = f'{stem}_{idx}{ext}'
        record['target_file'] = target

    shutil.move(part_path, target)

    # Metadata as a separate file next to the original — the media file itself is untouched.
    record['completed_at'] = datetime.now(timezone.utc).isoformat()
    with open(target + '.meta.json', 'w', encoding='utf-8') as fh:
        json.dump(record, fh, indent=2, ensure_ascii=False)

    # The session sidecar is no longer needed
    if os.path.exists(meta_path):
        os.remove(meta_path)

    # Registry entry — thanks to it the queue will skip this file on the next run
    mark_completed(record['session_id'], target, merged_meta.get('size'))

    print(f'[DONE] {target}')


if __name__ == '__main__':
    print(f'Uploads   -> {BASE_UPLOAD_DIR}')
    print(f'Pending   -> {PENDING_DIR}')
    print('Endpoints -> /ping, /manifest, /probe, /upload-chunk')
    # threaded=True explicitly: the client sends several chunks at once, and a server
    # handling requests one after another would defeat the whole point of parallelism.
    # Concurrent writes to a single file are safe — the per-session lock protects them.
    app.run(host='0.0.0.0', port=9001, debug=True, threaded=True)
