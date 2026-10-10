#!/usr/bin/env python3
"""
Builds a verification index of the downloaded library — the basis for safely
deleting photos from the phone.

The problem it solves: manually comparing a photo in the Photos app against a file
in the directory tree (name, time, location) takes thousands of taps across 12.5k
files. The index lets the phone ask the server "do you have exactly this file" and
get an answer based on content rather than on the name.

Why MD5 and not a more modern digest: it is the only digest the iOS side can
compute on its own in Expo Go — `expo-file-system/legacy`
`getInfoAsync(uri, { md5: true })`. The choice of algorithm is therefore forced by
the other side of the comparison, not by performance. For detecting corruption and
truncation MD5 is entirely sufficient; its cryptographic weakness (the possibility
of deliberately fabricating a collision) is irrelevant here, because nobody is
trying to deceive us.

Why multithreaded: the files live on a Windows drive seen through 9p, where the
bottleneck is operation latency, not throughput. Measured on this data:
1 thread 115 MB/s, 8 threads 275 MB/s, 16 threads 331 MB/s.

SAFETY: the script only READS media files and their sidecars. It writes solely
into the _verify/ subdirectory — it never deletes or overwrites anything.
"""

import argparse
import hashlib
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor

# The index format (reading) is described in verify_index — here we only write it,
# so we borrow the reading side from there instead of duplicating the parsing.
import verify_index

# Root of the downloaded library — the same one server.py and client.py use.
BASE_UPLOAD_DIR = '/mnt/c/gs/upload.go'

# Directory for verification artifacts. A separate subdirectory rather than a file
# in the root, because client.py walks the tree looking for *.meta.json and must not
# be handed anything it could mistake for a sidecar.
VERIFY_DIR = os.path.join(BASE_UPLOAD_DIR, '_verify')

# JSON Lines, not one large JSON: appending line by line survives an interruption
# halfway through — across 48 GB and several minutes of work that is a real risk.
INDEX_PATH = os.path.join(VERIFY_DIR, 'index.jsonl')

# Working directories we do not index: partial and temporary files are not
# complete library files.
SKIP_DIRS = {'_pending', '_tmp', '_verify'}

# Read chunk size. 1 MB is a compromise: large chunks amortise the 9p latency
# while still not pulling huge videos into memory.
READ_CHUNK = 1024 * 1024


def iter_media_files():
    """
    Yields paths of media files in the library.

    Skips sidecars (*.meta.json) and working directories. A generator, not a list —
    with well over ten thousand files there is no reason to hold them all at once.
    """
    for root, dirs, files in os.walk(BASE_UPLOAD_DIR):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for name in files:
            if name.endswith('.meta.json'):
                continue
            yield os.path.join(root, name)


def file_md5(path):
    """Computes a file's MD5, reading it in chunks (huge videos never enter memory)."""
    digest = hashlib.md5()
    with open(path, 'rb') as fh:
        for block in iter(lambda: fh.read(READ_CHUNK), b''):
            digest.update(block)
    return digest.hexdigest()


def read_sidecar(media_path):
    """
    Reads a file's sidecar and extracts what serves verification.

    Returns (original_name, capture_time, place_description, has_gps).
    When the sidecar is missing or corrupted it returns None/False throughout —
    the absence of metadata is itself information, because the user requires the
    presence of the JSON file as one of the conditions for considering a file
    correctly downloaded.
    """
    path = media_path + '.meta.json'
    if not os.path.exists(path):
        return None, None, None, False

    try:
        with open(path, encoding='utf-8') as fh:
            record = json.load(fh)
    except (OSError, ValueError):
        return None, None, None, False

    meta = record.get('metadata') or {}
    from_file = record.get('metadata_from_file') or {}

    # The original name sits in one of two places: at the top level (that is how
    # client.py writes it) or inside 'metadata' (that is how server.py writes it).
    orig_name = record.get('name') or meta.get('name')

    # Capture time: first what the phone supplied, then what we read from the file.
    captured = (
        meta.get('date')
        or from_file.get('datetime_original')
        or from_file.get('created_utc')
    )

    # We count GPS as present when either side supplied non-zero coordinates.
    has_gps = False
    for source in (meta, from_file):
        try:
            if float(source.get('latitude') or 0) or float(source.get('longitude') or 0):
                has_gps = True
                break
        except (TypeError, ValueError):
            continue

    # Place description in the English variant — the same one directories are built from.
    location = record.get('location') or {}
    hierarchy = (location.get('en') or {}).get('hierarchy') or []

    return orig_name, captured, '/'.join(hierarchy), has_gps


def describe(path):
    """
    Describes a single file: size, MD5, data from the sidecar.

    Called from many threads, so it touches no shared state — the whole result
    comes back as a dict and the main thread does the writing to the index.
    """
    try:
        stat = os.stat(path)
        orig_name, captured, place, has_gps = read_sidecar(path)
        return {
            'path': os.path.relpath(path, BASE_UPLOAD_DIR),
            # The name stored on disk may carry a collision suffix (_1), so we keep
            # both: the on-disk one and the original from the phone.
            'disk_name': os.path.basename(path),
            'orig_name': orig_name,
            'size': stat.st_size,
            'mtime': int(stat.st_mtime),
            'md5': file_md5(path),
            'captured': captured,
            'place': place,
            'has_gps': has_gps,
            'has_sidecar': orig_name is not None,
        }
    except OSError as exc:
        return {'path': os.path.relpath(path, BASE_UPLOAD_DIR), 'error': str(exc)}


def load_existing():
    """
    Loads the existing index as a map path -> entry.

    Allows resuming an interrupted run and skipping files that have not changed.
    We read the format through verify_index.parse_index_file so that the way of
    reading the index lives in one place — this script writes it, that module
    reads it when querying.
    """
    entries = {}
    for row in verify_index.parse_index_file(INDEX_PATH):
        if 'path' in row:
            entries[row['path']] = row
    return entries


def build(workers, rebuild):  # pylint: disable=too-many-locals
    """Builds (or tops up) the index. Returns the number of files processed."""
    os.makedirs(VERIFY_DIR, exist_ok=True)

    existing = {} if rebuild else load_existing()
    if rebuild and os.path.exists(INDEX_PATH):
        # We do not delete the old index, only set it aside with a timestamp
        # suffix — in keeping with the rule that nothing in this tree is lost.
        backup = f'{INDEX_PATH}.{int(time.time())}.bak'
        os.replace(INDEX_PATH, backup)
        print(f'[INFO] Previous index kept as {os.path.basename(backup)}')

    print('[INFO] Scanning tree...')
    todo = []
    skipped = 0
    for path in iter_media_files():
        rel = os.path.relpath(path, BASE_UPLOAD_DIR)
        prev = existing.get(rel)
        if prev and 'md5' in prev:
            try:
                stat = os.stat(path)
                # Same size and mtime = same file, no need to hash it again.
                if prev.get('size') == stat.st_size and prev.get('mtime') == int(stat.st_mtime):
                    skipped += 1
                    continue
            except OSError:
                pass
        todo.append(path)

    total_bytes = 0
    for path in todo:
        try:
            total_bytes += os.path.getsize(path)
        except OSError:
            pass

    print(f'[INFO] To hash: {len(todo)} files '
          f'({total_bytes / 1024 / 1024 / 1024:.1f} GB), skipped unchanged: {skipped}')

    if not todo:
        print('[INFO] Index is up to date — nothing to hash.')
        return 0

    started = time.time()
    done = 0
    done_bytes = 0
    errors = 0

    # We append in 'a' mode and flush after every entry: an interrupted run then
    # costs at most one file rather than all the work done so far.
    with open(INDEX_PATH, 'a', encoding='utf-8') as out:
        with ThreadPoolExecutor(workers) as pool:
            for row in pool.map(describe, todo):
                out.write(json.dumps(row, ensure_ascii=False) + '\n')
                out.flush()
                done += 1
                done_bytes += row.get('size') or 0
                if 'error' in row:
                    errors += 1
                    print(f'[ERROR] {row["path"]}: {row["error"]}')
                if done % 500 == 0 or done == len(todo):
                    elapsed = time.time() - started
                    speed = done_bytes / 1024 / 1024 / elapsed if elapsed else 0
                    left = (len(todo) - done) * elapsed / done if done else 0
                    print(f'  {done}/{len(todo)}  {speed:.0f} MB/s  '
                          f'~{left / 60:.1f} min left')

    elapsed = time.time() - started
    print(f'[DONE] {done} files in {elapsed:.0f}s, errors: {errors}')
    print(f'[DONE] Index: {INDEX_PATH}')
    return done


def summary():
    """Prints an index summary — the state of the library in numbers."""
    entries = load_existing()
    if not entries:
        print('[INFO] Index is empty. Run without --summary to build it.')
        return

    rows = [r for r in entries.values() if 'md5' in r]
    total_size = sum(r['size'] for r in rows)

    by_md5 = {}
    for row in rows:
        by_md5.setdefault(row['md5'], []).append(row)

    duplicates = {k: v for k, v in by_md5.items() if len(v) > 1}
    dup_files = sum(len(v) for v in duplicates.values())
    # How much space could be reclaimed by keeping one copy of each content.
    dup_waste = sum(v[0]['size'] * (len(v) - 1) for v in duplicates.values())

    no_sidecar = [r for r in rows if not r.get('has_sidecar')]
    no_gps = [r for r in rows if not r.get('has_gps')]
    no_time = [r for r in rows if not r.get('captured')]

    print(f'files in index:         {len(rows)}')
    print(f'total size:             {total_size / 1024 / 1024 / 1024:.1f} GB')
    print(f'unique contents (MD5):  {len(by_md5)}')
    print(f'duplicated contents:    {len(duplicates)} '
          f'(across {dup_files} files, {dup_waste / 1024 / 1024:.0f} MB redundant)')
    print(f'without sidecar:        {len(no_sidecar)}')
    print(f'without GPS:            {len(no_gps)}')
    print(f'without capture time:   {len(no_time)}')

    if duplicates:
        print('\nlargest duplicated contents:')
        worst = sorted(duplicates.values(), key=lambda v: -v[0]['size'] * (len(v) - 1))
        for group in worst[:5]:
            print(f'  {group[0]["size"] / 1024 / 1024:6.1f} MB x{len(group)}  '
                  f'{group[0]["disk_name"]}')
            for row in group:
                print(f'       {row["path"]}')


def main(argv=None):
    """Entry point: build the index or print a summary."""
    parser = argparse.ArgumentParser(
        description='Builds an MD5 index of the downloaded library (media files read-only).'
    )
    parser.add_argument('--workers', type=int, default=16,
                        help='Number of reader threads (default 16; 9p likes parallelism).')
    parser.add_argument('--rebuild', action='store_true',
                        help='Hash everything from scratch (old index kept as .bak).')
    parser.add_argument('--summary', action='store_true',
                        help='Hash nothing, only show a summary of the existing index.')
    args = parser.parse_args(argv)

    if not os.path.isdir(BASE_UPLOAD_DIR):
        print(f'[ERROR] Library directory missing: {BASE_UPLOAD_DIR}')
        return 1

    if args.summary:
        summary()
        return 0

    build(args.workers, args.rebuild)
    summary()
    return 0


if __name__ == '__main__':
    sys.exit(main())
