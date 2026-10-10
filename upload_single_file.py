#!/usr/bin/env python3
"""
Chunked upload jednego pliku do serwera.
Usage: python upload_single_file.py /path/to/file
"""

import hashlib
import sys
from pathlib import Path

import requests

# NGROK URL
SERVER_BASE = 'https://setting-revisable-pacific.ngrok-free.dev'
CHUNK_SIZE = 1024 * 1024  # 1 MB


def upload_file(filepath: str):  # pylint: disable=too-many-locals,too-many-statements
    """
    Sends a file in chunks with resume support.

    A single linear flow on purpose: manifest, probe, chunk loop, final check.
    Splitting it would scatter one protocol exchange across several functions.
    """

    file_path = Path(filepath)
    if not file_path.exists():
        print(f"Error: file does not exist: {filepath}")
        return

    file_size = file_path.stat().st_size
    file_name = file_path.name

    print(f"Plik: {file_name}")
    print(f"Size: {file_size} bytes ({file_size / 1024 / 1024:.2f} MB)")

    # 1. Send the manifest
    print("\n[1/4] Sending manifest...")
    manifest = {
        'files': [{
            'name': file_name,
            'type': 'video',
            'size': file_size,
            'timestamp': int(file_path.stat().st_mtime),
            'latitude': 50.0647,
            'longitude': 19.9449,
        }]
    }

    resp = requests.post(f'{SERVER_BASE}/manifest', json=manifest, timeout=10)
    if resp.status_code != 200:
        print(f"Manifest error: {resp.status_code}")
        print(resp.text)
        return

    manifest_data = resp.json()
    print(f"Manifest accepted: {len(manifest_data.get('files', []))} files")

    # 2. Take session_id from the manifest (or generate one)
    session_id = f"upload_{file_name}_{hashlib.md5(file_name.encode()).hexdigest()[:8]}"

    # 3. Check whether the file already exists partially (resume)
    print("\n[2/4] Checking the state of an existing upload...")
    probe = requests.get(f'{SERVER_BASE}/probe', params={
        'session_id': session_id,
        'file_name': file_name,
    }, timeout=10)

    probe_data = probe.json()
    print(f"Stan: {probe_data.get('status')}")

    if probe_data.get('status') == 'done':
        print("File already complete!")
        return

    # 4. Chunked upload
    print("\n[3/4] Starting chunked upload...")

    with open(file_path, 'rb') as f:
        chunk_index = 0

        while True:
            chunk = f.read(CHUNK_SIZE)
            if not chunk:
                break

            # Headers
            headers = {
                'X-File-Name': file_name,
                'X-File-Size': str(file_size),
                'X-Chunk-Index': str(chunk_index),
                'X-Session-ID': session_id,
            }

            # Sending the chunk
            resp = requests.post(
                f'{SERVER_BASE}/upload-chunk',
                data=chunk,
                headers=headers,
                timeout=60,
            )

            if resp.status_code != 200:
                print(f"Chunk {chunk_index} error: {resp.status_code}")
                return

            data = resp.json()

            if data.get('status') == 'resume':
                # Resuming from a different chunk
                next_chunk = data.get('next_chunk', 0)
                print(f"Resuming from chunk {next_chunk}")
                chunk_index = next_chunk
                # Jump to the matching position in the file
                f.seek(chunk_index * CHUNK_SIZE)
                continue

            if data.get('status') == 'done':
                print(f"Chunk {chunk_index} sent ({len(chunk)} bytes)")

            chunk_index += 1

            # Report progress every 10 chunks
            if chunk_index % 10 == 0:
                bytes_sent = chunk_index * CHUNK_SIZE
                pct = min(100, bytes_sent / file_size * 100)
                print(f"Progress: {pct:.1f}% ({bytes_sent}/{file_size} bytes)")

    # 5. Check the final status
    print("\n[4/4] Checking final state...")
    final = requests.get(f'{SERVER_BASE}/probe', params={
        'session_id': session_id,
        'file_name': file_name,
    }, timeout=10)

    final_data = final.json()
    print(f"Final state: {final_data.get('status')} - {final_data.get('message')}")
    print("\nUpload completed successfully!")
    print(f"Session ID: {session_id}")


if __name__ == '__main__':
    if len(sys.argv) < 2:
        print("Usage: python upload_single_file.py /path/to/file")
        sys.exit(1)

    upload_file(sys.argv[1])
