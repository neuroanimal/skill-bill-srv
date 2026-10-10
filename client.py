"""
Skill Bill Client — pulls files FROM a source on the phone and lays them out on disk.

This reverses the direction of server.py. The reason is networking: the iPhone
hotspot isolates clients (AP isolation), so the phone-as-client cannot reach the
computer-as-server over the local network — confirmed with tcpdump (0 packets).
The opposite direction does work: the computer can reach the phone-as-gateway
(172.20.10.1), so the computer must PULL while the phone SERVES. This keeps the
transfer local, with no LTE usage.

The client is shared between two sources (see the Source classes below):
  * ShareServerSource — the external "Share Server" app on the iPhone (port 8888),
    which serves a file list at GET / and files at GET /files/<name>.
  * SkillBillServeSource — our Skill-Bill app in Expo Go, in serving mode
    (to be implemented on the app side); same list()/open() interface.

All file-placement logic (GPS + date -> directory) is reused from server.py
(enrich_from_file, build_target_dir, describe_location) — we do not duplicate it,
so that both directions produce an identical directory structure.
"""

import argparse
import json
import os
import struct
import sys
import time
from datetime import datetime, timezone
from urllib.parse import quote

import requests

# Reuse server logic — a single source of truth for the directory structure.
from server import (
    BASE_UPLOAD_DIR,
    build_target_dir,
    describe_location,
    enrich_from_file,
    mark_completed,
    sanitize_segment,
)

# Default address of the phone serving the file-sharing app.
# Two network setups seen so far:
#   - Apple Personal Hotspot (phone-as-router): phone is the gateway 172.20.10.1
#   - SIM router with its own LAN (192.168.1.0/24): phone is a DHCP client,
#     e.g. 192.168.1.135 — read it from the router's LAN device list, matching
#     the phone's Wi-Fi MAC (iOS: Settings > Wi-Fi > network > Wi-Fi Address).
# Note: iOS "Private Wi-Fi Address" rotates the MAC, so the lease may show as
# NIEZNANA/UNKNOWN until matched by that MAC. Override at runtime with --host.
DEFAULT_PHONE_HOST = '192.168.1.135'

# Default ports for each source.
SHARE_SERVER_PORT = 8888
SKILL_BILL_SERVE_PORT = 9002

# We download in chunks so large videos do not load fully into memory
# and so progress can be shown.
DOWNLOAD_CHUNK = 1024 * 1024  # 1 MB

# Directory for temporary (incomplete) downloads — next to the target tree.
DOWNLOAD_TMP = os.path.join(BASE_UPLOAD_DIR, '_downloading')

# Video/image extensions we know how to structurally validate.
_MP4_EXTS = ('.mp4', '.mov', '.m4v')
_JPEG_EXTS = ('.jpg', '.jpeg')
_HEIF_EXTS = ('.heic', '.heif')
_PNG_EXTS = ('.png',)

# Top-level atoms that a complete MP4/MOV container must contain.
# 'ftyp' identifies the file type, 'moov' holds metadata, 'mdat' holds the media.
_REQUIRED_MP4_ATOMS = ('ftyp', 'moov', 'mdat')

# ISO-BMFF brand codes that mark a HEIF/HEIC still image, found in the 'ftyp' box.
_HEIF_BRANDS = (b'heic', b'heix', b'heif', b'mif1', b'msf1', b'hevc')


class RemoteFile:
    """A single file at the source: original name, size (if known), download URL."""

    # Plain data container — intentionally without methods.
    # pylint: disable=too-few-public-methods

    def __init__(self, name, size, url):
        # Original name (e.g. "IMG_7411.mov") — also used to detect the extension.
        self.name = name
        # Size in bytes, or None when the source does not report it in the listing.
        self.size = size
        # Full URL to download the content.
        self.url = url


class Source:
    """
    Common interface for a file source. A concrete backend implements list().

    This abstraction lets client.py handle both Share Server and the future
    Skill-Bill serving mode without changing the download/placement logic.
    """

    def __init__(self, base_url):
        # Base URL of the source, e.g. "http://172.20.10.1:8888".
        self.base_url = base_url.rstrip('/')

    def list(self):
        """Returns a list of RemoteFile available at the source."""
        raise NotImplementedError

    def health(self):
        """Returns True when the source responds. Defaults to trying list()."""
        try:
            self.list()
            return True
        except (requests.RequestException, ValueError):
            return False


class ShareServerSource(Source):
    """
    Source: the external Share Server app on the iPhone.

    We parse the file list from the home page HTML — Share Server has no JSON API,
    but each file is a link <a href="/files/NAME" download="NAME">. The size is not
    reliably present in the listing, so it stays None and is learned from the
    Content-Length header at download time.
    """

    def list(self):
        """Fetches and parses the file list from the Share Server home page."""
        resp = requests.get(self.base_url + '/', timeout=10)
        resp.raise_for_status()
        html = resp.text

        files = []
        seen = set()
        # Each file row carries a download="original name" attribute — that is
        # more reliable than decoding href, since href is URL-encoded.
        for chunk in html.split('download="')[1:]:
            name = chunk.split('"', 1)[0]
            if not name or name in seen:
                # Share Server shows two variants of the same file (original and
                # "(1)") — deduplicate by the name visible to the user.
                continue
            seen.add(name)
            url = self.base_url + '/files/' + quote(name)
            files.append(RemoteFile(name=name, size=None, url=url))
        return files


class SkillBillServeSource(Source):
    """
    Source: our Skill-Bill app in Expo Go, in serving mode.

    Assumed contract (to be implemented on the app side):
      GET /list  -> JSON: {"files": [{"name": ..., "size": ..., "id": ...}, ...]}
      GET /file/<id or name> -> file content
    Until the app exposes this, the source raises a clear error rather than guessing.
    """

    def list(self):
        """Fetches the file list from the Skill-Bill /list endpoint (serving mode)."""
        resp = requests.get(self.base_url + '/list', timeout=10)
        resp.raise_for_status()
        data = resp.json()
        files = []
        for item in data.get('files', []):
            name = item.get('name')
            if not name:
                continue
            ident = item.get('id', name)
            url = self.base_url + '/file/' + quote(str(ident))
            files.append(RemoteFile(name=name, size=item.get('size'), url=url))
        return files


def _human(size):
    """Returns a human-readable size (e.g. '12.3 MB') or '?' when unknown."""
    if size is None:
        return '?'
    for unit in ('B', 'KB', 'MB', 'GB'):
        if size < 1024 or unit == 'GB':
            return f'{size:.1f} {unit}'
        size /= 1024.0
    return f'{size:.1f} GB'


def _validate_mp4_container(path):
    """
    Structurally validates an MP4/MOV file WITHOUT decoding it (no player needed).

    Walks the top-level atom headers and checks two things:
      * every required atom is present (ftyp, moov, mdat),
      * no atom claims a size that runs past the end of the file (truncation).
    A truncated download — the most common failure over the network — is caught
    here, because the last atom's declared size would exceed the file length.

    Returns (ok, reason). reason is '' on success or a short explanation.
    """
    try:
        total = os.path.getsize(path)
        found = set()
        with open(path, 'rb') as fh:
            pos = 0
            while pos + 8 <= total:
                fh.seek(pos)
                header = fh.read(8)
                if len(header) < 8:
                    break
                size = struct.unpack('>I', header[:4])[0]
                name = header[4:8].decode('latin-1', errors='replace')
                data_start = pos + 8
                if size == 1:
                    # 64-bit extended size in the next 8 bytes.
                    ext = fh.read(8)
                    if len(ext) < 8:
                        return False, 'truncated extended-size header'
                    size = struct.unpack('>Q', ext)[0]
                    data_start += 8
                elif size == 0:
                    # size 0 means the atom runs to end of file — always fits.
                    size = total - pos
                if size < 8:
                    return False, f'invalid atom size at offset {pos}'
                if pos + size > total:
                    # Declared atom extends past the file — truncated download.
                    return False, f"atom '{name}' exceeds file end (truncated)"
                found.add(name)
                pos += size
    except OSError as exc:
        return False, f'read error: {exc}'

    missing = [a for a in _REQUIRED_MP4_ATOMS if a not in found]
    if missing:
        return False, 'missing atoms: ' + ', '.join(missing)
    return True, ''


def _validate_jpeg(path):
    """
    Structurally validates a JPEG without decoding it.

    Checks the SOI marker (FF D8) at the start and the EOI marker (FF D9) at the
    very end. A truncated JPEG — the typical broken-download symptom — is missing
    its trailing EOI, so this catches it cheaply.

    Returns (ok, reason).
    """
    try:
        size = os.path.getsize(path)
        if size < 4:
            return False, 'file too small for JPEG'
        with open(path, 'rb') as fh:
            head = fh.read(2)
            fh.seek(-2, os.SEEK_END)
            tail = fh.read(2)
    except OSError as exc:
        return False, f'read error: {exc}'
    if head != b'\xff\xd8':
        return False, 'missing SOI marker (FFD8)'
    if tail != b'\xff\xd9':
        return False, 'missing EOI marker (FFD9) — truncated'
    return True, ''


def _validate_png(path):
    """
    Structurally validates a PNG without decoding it.

    Checks the 8-byte PNG signature at the start and the IEND chunk at the end
    (a complete PNG always ends with the IEND chunk type followed by its CRC).

    Returns (ok, reason).
    """
    signature = b'\x89PNG\r\n\x1a\n'
    try:
        size = os.path.getsize(path)
        if size < len(signature) + 12:
            return False, 'file too small for PNG'
        with open(path, 'rb') as fh:
            head = fh.read(len(signature))
            # IEND is the last chunk: 4-byte type 'IEND' followed by 4-byte CRC,
            # so the type sits 8 bytes before the end of the file.
            fh.seek(-8, os.SEEK_END)
            tail = fh.read(4)
    except OSError as exc:
        return False, f'read error: {exc}'
    if head != signature:
        return False, 'missing PNG signature'
    if tail != b'IEND':
        return False, 'missing IEND chunk — truncated'
    return True, ''


def _validate_heif(path):  # pylint: disable=too-many-return-statements
    """
    Structurally validates a HEIF/HEIC image (ISO-BMFF) without decoding it.

    HEIC uses the same box structure as MP4/MOV. We reuse the box walk to confirm
    no box runs past the end of file (truncation), and additionally require an
    'ftyp' box whose brand marks a still image (heic/heix/mif1/...). The media data
    lives in 'mdat', so we require that too.

    Returns (ok, reason).
    """
    try:
        total = os.path.getsize(path)
        found = set()
        brand_ok = False
        with open(path, 'rb') as fh:
            pos = 0
            while pos + 8 <= total:
                fh.seek(pos)
                header = fh.read(8)
                if len(header) < 8:
                    break
                box_size = struct.unpack('>I', header[:4])[0]
                name = header[4:8].decode('latin-1', errors='replace')
                data_start = pos + 8
                if box_size == 1:
                    ext = fh.read(8)
                    if len(ext) < 8:
                        return False, 'truncated extended-size header'
                    box_size = struct.unpack('>Q', ext)[0]
                    data_start += 8
                elif box_size == 0:
                    box_size = total - pos
                if box_size < 8:
                    return False, f'invalid box size at offset {pos}'
                if pos + box_size > total:
                    return False, f"box '{name}' exceeds file end (truncated)"
                if name == 'ftyp':
                    # Read the brand list and check for a still-image brand.
                    fh.seek(data_start)
                    brands = fh.read(min(box_size - 8, 256))
                    brand_ok = any(b in brands for b in _HEIF_BRANDS)
                found.add(name)
                pos += box_size
    except OSError as exc:
        return False, f'read error: {exc}'

    if 'ftyp' not in found:
        return False, 'missing ftyp box'
    if not brand_ok:
        return False, 'ftyp has no HEIF/HEIC brand'
    if 'mdat' not in found:
        return False, 'missing mdat box'
    return True, ''


def _auto_verify(path, name, meta_size):
    """
    Runs pre-validation on a placed file without opening a player.

    Two independent checks:
      * size: on-disk size must equal the reported meta size (when meta size known),
      * container: MP4/MOV atoms must be present and not truncated.
    Returns a short status string for the report column: 'OK', 'SIZE MISMATCH',
    'BAD: <reason>', or 'OK (size only)' when the container type is not checkable.
    """
    disk_size = os.path.getsize(path)

    # Size check first — cheapest and catches the common truncated-download case.
    if meta_size is not None and disk_size != meta_size:
        return 'SIZE MISMATCH'

    ext = os.path.splitext(name)[1].lower()
    if ext in _MP4_EXTS:
        ok, reason = _validate_mp4_container(path)
        return 'OK' if ok else f'BAD: {reason}'
    if ext in _JPEG_EXTS:
        ok, reason = _validate_jpeg(path)
        return 'OK' if ok else f'BAD: {reason}'
    if ext in _HEIF_EXTS:
        ok, reason = _validate_heif(path)
        return 'OK' if ok else f'BAD: {reason}'
    if ext in _PNG_EXTS:
        ok, reason = _validate_png(path)
        return 'OK' if ok else f'BAD: {reason}'

    # No structural validator for this type — size passed, so OK.
    return 'OK (size only)'


def _download_to_tmp(remote, log):
    """
    Downloads a file into the temp directory, returns (tmp_path, size) or (None, 0).

    Downloading into a temp file (rather than straight into the tree) is required
    because the target directory is only known after reading metadata from the
    downloaded file.
    """
    os.makedirs(DOWNLOAD_TMP, exist_ok=True)
    tmp_path = os.path.join(DOWNLOAD_TMP, sanitize_segment(remote.name) or 'file')

    try:
        with requests.get(remote.url, stream=True, timeout=60) as resp:
            resp.raise_for_status()
            total = resp.headers.get('Content-Length')
            total = int(total) if total else remote.size
            got = 0
            last = time.time()
            with open(tmp_path, 'wb') as fh:
                for chunk in resp.iter_content(DOWNLOAD_CHUNK):
                    if not chunk:
                        continue
                    fh.write(chunk)
                    got += len(chunk)
                    # Progress every ~2 s so the console is not flooded on big files.
                    now = time.time()
                    if total and now - last > 2:
                        pct = 100.0 * got / total
                        log(f'    {remote.name}: {pct:.0f}% ({_human(got)}/{_human(total)})')
                        last = now
        return tmp_path, got
    except requests.RequestException as exc:
        log(f'    [ERROR] download {remote.name}: {exc}')
        # Remove the incomplete temp file so it does not masquerade as complete.
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        return None, 0


def _normalize_name(name):
    """
    Normalizes a file name to the MediaLibrary convention: extension UPPERCASE,
    stem unchanged.

    Files pulled earlier by server.py (from iOS MediaLibrary) have an uppercase
    extension — 'IMG_2642.MP4', 'IMG_1463.MOV'. Share Server serves them with a
    lowercase extension ('IMG_7257.mov'). To keep the whole on-disk library
    consistent, we save Share Server files under the same convention. We do NOT
    touch the stem — iOS never changes the case of the 'IMG_XXXX' part, so
    normalizing it could only distort unusual names.
    """
    stem, ext = os.path.splitext(name)
    return stem + ext.upper()


def _place_file(tmp_path, remote, log):  # pylint: disable=too-many-locals
    """
    Places a downloaded file into the target tree by GPS + date (reuses server.py).

    Returns (target_path, info) or (None, None) on error. `info` is a dict for the
    batch report: name, disk_size, meta_size, time (capture time), location (coarse,
    EN), verified (pre-validation status). Also writes the .meta.json sidecar the
    same way server.py does, so both directions produce identical artifacts.
    """
    # Minimal "client" metadata: only the name. The rest (GPS, date) comes from the
    # file via enrich_from_file — for Share Server the phone passes nothing else.
    client_meta = {'name': remote.name, 'size': os.path.getsize(tmp_path)}

    merged_meta, file_meta = enrich_from_file(tmp_path, client_meta)

    target_dir = build_target_dir(merged_meta)
    os.makedirs(target_dir, exist_ok=True)

    # Normalize the saved name to the MediaLibrary convention (uppercase extension)
    # so the on-disk library is consistent regardless of source (Share Server vs server).
    out_name = _normalize_name(merged_meta['name'])
    target = os.path.join(target_dir, sanitize_segment(out_name))
    # Name collision in the same directory — append a suffix, as server.py does.
    if os.path.exists(target):
        stem, ext = os.path.splitext(target)
        idx = 1
        while os.path.exists(f'{stem}_{idx}{ext}'):
            idx += 1
        target = f'{stem}_{idx}{ext}'

    os.replace(tmp_path, target)

    # Location in both languages + metadata sidecar — consistent with server.finalize().
    location = describe_location(
        merged_meta.get('latitude'), merged_meta.get('longitude')
    )
    record = {
        'source': 'client',
        'name': remote.name,
        'metadata': client_meta,
        'metadata_from_file': file_meta,
        'target_file': target,
        'location': location,
        'completed_at': datetime.now(timezone.utc).isoformat(),
    }
    with open(target + '.meta.json', 'w', encoding='utf-8') as fh:
        json.dump(record, fh, indent=2, ensure_ascii=False)

    log(f'    -> {target}')

    # Report data: capture time, coarse location, both sizes, verification status.
    # meta_size is what the source/metadata reports; disk_size is what we actually
    # wrote — showing both lets the user spot a truncated download at a glance.
    info = {
        'name': remote.name,
        'disk_size': os.path.getsize(target),
        'meta_size': client_meta.get('size'),
        'time': merged_meta.get('date') or file_meta.get('created_utc') or '?',
        'location': _short_location(location),
        'verified': _auto_verify(target, remote.name, client_meta.get('size')),
    }
    return target, info


def _short_location(location):
    """Returns a coarse place description (city + district) from the EN hierarchy, or 'No_GPS'."""
    hier = (location.get('en') or {}).get('hierarchy') if location else None
    if not hier:
        return 'No_GPS'
    # Take the two most specific tiers, e.g. "Krakow / Bienczyce".
    return ' / '.join(hier[-2:])


def pull(source, log=print, skip_existing=True):
    """
    Main loop: lists the source, downloads and places each new file.

    Returns statistics (dict). skip_existing skips files whose name we already have
    on disk — this lets an interrupted transfer resume without duplicates.
    """
    stats = {'total': 0, 'done': 0, 'skipped': 0, 'failed': 0}
    # Collected info about files handled in this run — for the batch report.
    batch_report = []

    log(f'Source : {source.base_url}')
    files = source.list()
    stats['total'] = len(files)
    log(f'Files at source: {len(files)}')

    completed_index = {}
    if skip_existing:
        # Dedup + data for the 'already have' rows from sidecars (see
        # _downloaded_index) — NOT by target path, which may be suffixed on collision.
        completed_index = _downloaded_index()

    for i, remote in enumerate(files, 1):
        log(f'[{i}/{len(files)}] {remote.name} ({_human(remote.size)})')

        if skip_existing and remote.name.lower() in completed_index:
            log('    (skip — already have)')
            stats['skipped'] += 1
            # Take size/time/location from the existing sidecar on disk, so that
            # size-based verification works for skipped files too.
            row = _sidecar_report_row(remote.name, completed_index[remote.name.lower()])
            batch_report.append(row)
            continue

        tmp_path, size = _download_to_tmp(remote, log)
        if not tmp_path:
            stats['failed'] += 1
            batch_report.append({
                'name': remote.name, 'disk_size': None, 'meta_size': remote.size,
                'time': '', 'location': '', 'verified': '', 'status': 'DOWNLOAD ERROR',
            })
            continue

        target, info = _place_file(tmp_path, remote, log)
        if not target:
            stats['failed'] += 1
            batch_report.append({
                'name': remote.name, 'disk_size': size, 'meta_size': size,
                'time': '', 'location': '', 'verified': '', 'status': 'WRITE ERROR',
            })
            continue

        # Record completion the same way server.py does — shared registry, so pulls
        # and uploads skip each other.
        mark_completed(f'client:{remote.name}', target, size)
        # Update the index so a second occurrence of this name in the same batch
        # (Share Server shows two variants) is already recognized as 'already have'.
        completed_index[remote.name.lower()] = {
            'target_file': target,
            'metadata': {'size': size},
            'location': {},
        }
        stats['done'] += 1
        info['status'] = 'downloaded'
        batch_report.append(info)

    log(
        f'\nDone. Downloaded: {stats["done"]}, skipped: {stats["skipped"]}, '
        f'errors: {stats["failed"]}, at source: {stats["total"]}'
    )
    if batch_report:
        _print_batch_report(batch_report, log)
    return stats


def _sidecar_report_row(name, record):
    """
    Builds a report row for an 'already have' file from its existing sidecar.

    Reads both sizes and re-runs auto-verification against the file on disk, so a
    skipped file is still validated (e.g. detects a previously corrupted copy)
    without any network access or a media player.
    """
    target = record.get('target_file')
    disk_size = None
    verified = ''
    meta_size = (record.get('metadata') or {}).get('size')
    if target and os.path.exists(target):
        disk_size = os.path.getsize(target)
        verified = _auto_verify(target, name, meta_size)

    meta = record.get('metadata') or {}
    file_meta = record.get('metadata_from_file') or {}
    time_val = meta.get('date') or file_meta.get('created_utc') or ''
    location = _short_location(record.get('location') or {})

    return {
        'name': name, 'disk_size': disk_size, 'meta_size': meta_size,
        'time': time_val, 'location': location, 'verified': verified,
        'status': 'already have',
    }


def _print_batch_report(rows, log):
    """
    Prints a table of ALL files in the batch — sorted by name.

    Columns: name (to locate in Photos and on disk), size on disk, meta size,
    capture time, coarse location, auto-verify result, status. The status shows the
    reason: 'downloaded', 'already have', or an error — so it is immediately clear
    why a file was not stored. This is the working list for on-disk verification and
    for deleting files on the phone.
    """
    rows = sorted(rows, key=lambda r: r['name'].lower())

    def width(key, header):
        return max([len(str(r.get(key) or '')) for r in rows] + [len(header)])

    name_w = width('name', 'FILE')
    loc_w = width('location', 'LOCATION')
    status_w = width('status', 'STATUS')
    ver_w = width('verified', 'AUTO-VERIFIED')

    log('\n=== BATCH (sorted by name) ===')
    log(f'{"FILE":<{name_w}} | {"DISK SIZE":>9} | {"META SIZE":>9} | '
        f'{"CAPTURE TIME":<16} | {"LOCATION":<{loc_w}} | '
        f'{"AUTO-VERIFIED":<{ver_w}} | STATUS')
    log(f'{"-" * name_w}-+-{"-" * 9}-+-{"-" * 9}-+-{"-" * 16}-+-'
        f'{"-" * loc_w}-+-{"-" * ver_w}-+-{"-" * status_w}')
    for r in rows:
        # Capture time shortened to "YYYY-MM-DD HH:MM" (drop seconds/timezone).
        tstr = str(r.get('time') or '').replace('T', ' ')[:16]
        disk = _human(r['disk_size']) if r.get('disk_size') is not None else '-'
        meta = _human(r['meta_size']) if r.get('meta_size') is not None else '-'
        loc = r.get('location') or ''
        ver = r.get('verified') or ''
        log(f'{r["name"]:<{name_w}} | {disk:>9} | {meta:>9} | {tstr:<16} | '
            f'{loc:<{loc_w}} | {ver:<{ver_w}} | {r.get("status", "")}')


def watch(source, interval=5, log=print, skip_existing=True):
    """
    Continuous mode: periodically polls the source and pulls each new batch.

    Designed for mass downloading in batches, since Share Server copies shared files
    into its own sandbox (so they will not all fit at once). Flow: you share a batch
    in the app -> watch pulls it -> you clear the share and provide the next batch
    -> watch pulls the next one. The completed registry skips what we already have,
    so it safely tolerates the same file reappearing in the listing.

    Ends via Ctrl+C. `interval` is the poll gap in seconds.
    """
    log(f'Watch mode: polling {source.base_url} every {interval}s. Ctrl+C to stop.')
    grand_total = 0
    try:
        while True:
            try:
                files = source.list()
            except (requests.RequestException, ValueError) as exc:
                # The source may briefly vanish (switching batches in the app) — keep waiting.
                log(f'(source unreachable: {exc} — retrying in {interval}s)')
                time.sleep(interval)
                continue

            # Only pull what we do not yet have — pull() checks this too, but here we
            # additionally silence the empty loop so as not to flood the log.
            pending = _pending_names(files, skip_existing)
            if pending:
                stats = pull(source, log=log, skip_existing=skip_existing)
                grand_total += stats['done']
                if stats['done']:
                    log(f'(new files downloaded since watch start: {grand_total})')
                    # Pause after the batch: while we wait for a key, NOTHING scans
                    # the directory or touches files — so they can be verified and
                    # deleted on the phone without an access conflict with Windows.
                    _wait_for_key(
                        '\nPress any key to continue synchronization '
                        '(Ctrl+C to stop)... '
                    )
                    continue  # go straight to the next cycle, skipping the interval wait
            time.sleep(interval)
    except KeyboardInterrupt:
        log(f'\nWatch stopped. Total downloaded this session: {grand_total}')


def _wait_for_key(prompt):
    """
    Waits for a single key press (without Enter, when possible).

    On a POSIX terminal we switch the tty to raw mode to capture one character.
    When stdin is not a terminal (e.g. a redirected stream) or termios is
    unavailable, we fall back to input() — then Enter is required.
    """
    sys.stdout.write(prompt)
    sys.stdout.flush()
    try:
        import termios  # pylint: disable=import-outside-toplevel
        import tty      # pylint: disable=import-outside-toplevel
        fd = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
        try:
            tty.setraw(fd)
            sys.stdin.read(1)
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)
        sys.stdout.write('\n')
    except (ImportError, OSError):
        # No real terminal — fall back to waiting for Enter.
        try:
            input()
        except EOFError:
            pass


def _pending_names(files, skip_existing):
    """Returns names of files we do not yet have (used to silence empty watch cycles)."""
    if not skip_existing:
        return [f.name for f in files]
    have = _downloaded_index()
    return [f.name for f in files if f.name.lower() not in have]


def _downloaded_index():
    """
    Returns a map: ORIGINAL name (lowercase) -> sidecar record.

    Replaces the earlier set of names. This lets the 'already have' rows show size,
    time and location from the EXISTING .meta.json on disk, without downloading the
    file or querying Share Server (which does not report size in its listing anyway).
    This speeds up verification during mass downloading.

    Client dedup is based on the source name (e.g. "IMG_7257.mov"), NOT on the
    target name — the target may be suffixed on collision ("IMG_7257_1.mov"), so
    comparing by target path would never match and the file would download forever.

    The original name is read from the sidecar from TWO possible places:
      * top-level 'name'   — how client.py writes it (_place_file),
      * 'metadata'.'name'  — how server.py writes it (upload via ngrok).
    Names are kept lowercase, because iOS is inconsistent about extension case
    ('.mov' vs '.MOV') for the same asset.

    We do not use server.load_completed(): that registry is keyed by server sessions
    and rebuilds from a 'session_id' field, which client sidecars do not have.
    """
    index = {}
    for root, _dirs, files in os.walk(BASE_UPLOAD_DIR):
        # Skip temporary/working files — those are not completed downloads.
        if os.path.abspath(root).startswith(os.path.abspath(DOWNLOAD_TMP)):
            continue
        for fname in files:
            if not fname.endswith('.meta.json'):
                continue
            try:
                with open(os.path.join(root, fname), encoding='utf-8') as fh:
                    record = json.load(fh)
            except (OSError, ValueError):
                continue
            # Order: top-level 'name' (client), then metadata.name (server).
            orig = record.get('name') or (record.get('metadata') or {}).get('name')
            if orig:
                index[orig.lower()] = record
    return index


def build_source(kind, host, port):
    """Builds a source object of the given kind ('share' or 'skillbill')."""
    base = f'http://{host}:{port}'
    if kind == 'share':
        return ShareServerSource(base)
    if kind == 'skillbill':
        return SkillBillServeSource(base)
    raise ValueError(f'unknown source kind: {kind}')


def main(argv=None):
    """CLI entry point: parses arguments and runs pull()/watch()."""
    parser = argparse.ArgumentParser(
        description='Pulls files from the phone (Share Server or Skill-Bill serve) '
                    'and lays them out on disk by GPS + date.'
    )
    parser.add_argument(
        '--source', choices=('share', 'skillbill'), default='share',
        help='source kind: share = Share Server (default), skillbill = app in serving mode'
    )
    parser.add_argument(
        '--host', default=DEFAULT_PHONE_HOST,
        help=f'phone address (default {DEFAULT_PHONE_HOST} — SIM-router LAN lease; use 172.20.10.1 for iPhone hotspot)'
    )
    parser.add_argument(
        '--port', type=int, default=None,
        help='source port (default depends on the source kind)'
    )
    parser.add_argument(
        '--no-skip', action='store_true',
        help='download everything again, do not skip files already stored'
    )
    parser.add_argument(
        '--list-only', action='store_true',
        help='only list the files at the source, do not download'
    )
    parser.add_argument(
        '--watch', action='store_true',
        help='continuous mode: keep pulling new batches (for mass downloading in batches)'
    )
    parser.add_argument(
        '--interval', type=int, default=5,
        help='poll gap in --watch mode, in seconds (default 5)'
    )
    args = parser.parse_args(argv)

    port = args.port
    if port is None:
        port = SHARE_SERVER_PORT if args.source == 'share' else SKILL_BILL_SERVE_PORT

    source = build_source(args.source, args.host, port)

    if not source.health():
        print(f'[ERROR] Source unreachable: {source.base_url}', file=sys.stderr)
        print('Check that the serving app is running on the phone and the host/port are correct.',
              file=sys.stderr)
        return 1

    if args.list_only:
        for remote in source.list():
            print(f'{remote.name}\t{_human(remote.size)}')
        return 0

    if args.watch:
        watch(source, interval=args.interval, skip_existing=not args.no_skip)
        return 0

    pull(source, skip_existing=not args.no_skip)
    return 0


if __name__ == '__main__':
    sys.exit(main())
