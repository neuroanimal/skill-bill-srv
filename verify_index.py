"""
Querying the verification index — answers the phone's question
"do you have exactly this file".

The index is built by build_verify_index.py; this module only reads and searches
it. The split is deliberate: building takes minutes and runs once, while querying
must be instant and run inside the server process.

The primary key is MD5, i.e. content. The name is only auxiliary, because in this
library it is unreliable for two reasons: after months iOS reuses the same
IMG_XXXX names, and on a collision within a directory we append a suffix
(_1, _(1)). Comparing by content is immune to both cases.
"""

import json
import os
import threading

# Paths must match build_verify_index.py — the single source of truth is the
# library root, everything else is derived from it.
BASE_UPLOAD_DIR = '/mnt/c/gs/upload.go'
VERIFY_DIR = os.path.join(BASE_UPLOAD_DIR, '_verify')
INDEX_PATH = os.path.join(VERIFY_DIR, 'index.jsonl')

# The index is held in process memory. We reload it when the file's modification
# time changes — this way newly appended files (another builder run) become visible
# without restarting the server, which we must not ask the user to do anyway.
_LOCK = threading.Lock()
_CACHE = {
    'mtime': None,
    'by_md5': {},
    'by_name': {},
    'rows': [],
}


def _load_locked():
    """Loads the index into memory. Called only while holding the lock."""
    by_md5 = {}
    by_name = {}
    entries = []

    if not os.path.exists(INDEX_PATH):
        _CACHE.update({'mtime': None, 'by_md5': {}, 'by_name': {}, 'rows': []})
        return

    for row in parse_index_file(INDEX_PATH):
        if 'md5' not in row:
            continue
        entries.append(row)

        # The same content may sit under several paths (duplicates) — hence the
        # value is a list, not a single entry.
        by_md5.setdefault(row['md5'], []).append(row)

        # We index by both the on-disk name and the original name from the phone;
        # both are needed because they differ on collisions.
        # A set, not a tuple: usually both names are identical, and appending the
        # row twice would then inflate the reported number of copies.
        keys = {n.lower() for n in (row.get('disk_name'), row.get('orig_name')) if n}
        for key in keys:
            by_name.setdefault(key, []).append(row)

    _CACHE.update({
        'mtime': os.path.getmtime(INDEX_PATH),
        'by_md5': by_md5,
        'by_name': by_name,
        'rows': entries,
    })


def _ensure_fresh():
    """
    Ensures the in-memory index matches the file on disk.

    The check costs a single stat call, but the file lives on a Windows drive seen
    through 9p, where such an operation takes a few milliseconds. That is why we
    call this ONCE per batch (check_many) rather than per item — across 12.5k
    queries the difference is a minute of waiting instead of an instant.
    """
    with _LOCK:
        if not os.path.exists(INDEX_PATH):
            if _CACHE['mtime'] is not None or not _CACHE['rows']:
                _load_locked()
            return
        current = os.path.getmtime(INDEX_PATH)
        if _CACHE['mtime'] != current:
            _load_locked()


def parse_index_file(path):
    """
    Reads the index file in JSON Lines format and yields its entries.

    The single place where the way of reading the index is defined — used both by
    this module (querying) and by build_verify_index.py (resuming an interrupted
    run). Without it the format would be described in two files and could drift
    apart.

    Corrupted lines are skipped without aborting: the last line is sometimes
    incomplete when the builder was killed mid-write.
    """
    if not os.path.exists(path):
        return

    with open(path, encoding='utf-8') as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except ValueError:
                continue


def refresh():
    """
    Forces a check whether the in-memory index matches the file on disk.

    Public, because a caller performing many queries in a row (building the
    bundle, checking a batch) should do this once up front rather than per item —
    a single stat call over 9p costs a few milliseconds.
    """
    _ensure_fresh()


def rows():
    """Returns the index entries. The list is shared — callers must not modify it."""
    return _CACHE['rows']


def stats():
    """Index summary — file count, unique contents, how many lack metadata."""
    _ensure_fresh()
    entries = _CACHE['rows']
    return {
        'files': len(entries),
        'unique_contents': len(_CACHE['by_md5']),
        'total_bytes': sum(r.get('size') or 0 for r in entries),
        'without_gps': sum(1 for r in entries if not r.get('has_gps')),
        'without_time': sum(1 for r in entries if not r.get('captured')),
        'index_present': os.path.exists(INDEX_PATH),
    }


def _row_summary(row):
    """Condensed entry for the response — just enough to recognise the file on disk."""
    return {
        'path': row.get('path'),
        'size': row.get('size'),
        'captured': row.get('captured'),
        'place': row.get('place'),
        'has_gps': row.get('has_gps'),
        'has_sidecar': row.get('has_sidecar'),
    }


def check_one(name=None, size=None, md5=None, fresh=True):
    """
    Checks whether the library contains the given file.

    Returns a dict with a `status` field:
      verified      — MD5 matches, i.e. the content is identical bit for bit.
                      The only status under which deleting from the phone is safe.
      size-match    — a file with this name and size exists, but no MD5 was given.
                      Strong, yet not conclusive evidence.
      name-mismatch — a file with this name exists, but of a different size or
                      content. Needs a manual look: either it is a different photo
                      under the same name, or the transfer was corrupted.
      missing       — nothing similar found.

    MD5 is decisive. When the phone could not compute it, we fall back to name and
    size, but the status says so explicitly — so that nobody deletes a photo on the
    strength of a mere name-and-size coincidence without knowing it.

    `fresh=False` skips the index freshness check — used by check_many, which does
    it once for the whole batch.
    """
    if fresh:
        _ensure_fresh()

    if md5:
        matches = _CACHE['by_md5'].get(md5.lower())
        if matches:
            return {
                'status': 'verified',
                'matched_by': 'md5',
                'copies': len(matches),
                'locations': [_row_summary(r) for r in matches],
            }

    candidates = _CACHE['by_name'].get(name.lower()) if name else None
    if candidates:
        if size is not None:
            same_size = [r for r in candidates if r.get('size') == size]
            if same_size:
                # Name and size match. If the phone supplied an MD5 and we did
                # not hit above, the content is DIFFERENT — the file of that name
                # and size on disk is not the same material.
                return {
                    'status': 'name-mismatch' if md5 else 'size-match',
                    'matched_by': 'name+size',
                    'copies': len(same_size),
                    'locations': [_row_summary(r) for r in same_size],
                }
        return {
            'status': 'name-mismatch',
            'matched_by': 'name',
            'copies': len(candidates),
            'locations': [_row_summary(r) for r in candidates],
        }

    return {'status': 'missing', 'matched_by': None, 'copies': 0, 'locations': []}


def check_many(items):
    """
    Checks a batch of files at once.

    A batch rather than individual queries, because the phone has well over ten
    thousand items to check and every request through the tunnel pays a full
    round trip. Each input element is a dict with keys name, size, md5 (any of
    them may be absent); the result preserves input order and adds an `id` field
    when one was given, so the phone can match answers to its own assets.
    """
    results = []
    # Index freshness is checked once for the whole batch — a stat over 9p costs
    # a few milliseconds, so doing it per item would dominate the response time.
    _ensure_fresh()
    for item in items or []:
        verdict = check_one(
            name=item.get('name'),
            size=item.get('size'),
            md5=item.get('md5'),
            fresh=False,
        )
        if item.get('id'):
            verdict['id'] = item['id']
        verdict['name'] = item.get('name')
        results.append(verdict)
    return results
