# skill-bill-srv

Backend of **Skill Bill**: it moves photos and videos from an iPhone to a computer, files every one
of them into a folder built from its **capture date and GPS location**, and lets the phone check, by
content, whether a file is already safely on disk before it is deleted from the phone.

The phone side is the Expo app [skill-bill](https://github.com/neuroanimal/skill-bill).

## Why it exists

A library of about 12,500 photos and videos had to leave the phone. Every way of doing that fell
short:
- by hand, with no order on disk;
- by cloud, with limits and still no order;
- by comparing names, which is unreliable because iOS reuses `IMG_XXXX` names after a few months.

This project transfers the files over a flaky tunnel or a phone hotspot, places them by time and
place, and proves with an MD5 digest that each copy is complete.

## Two directions of transfer

| Direction | Script | When to use |
|---|---|---|
| phone → computer (push) | `server.py` (Flask) reached through an ngrok tunnel; the app uploads chunks | phone on mobile data, any network |
| computer → phone (pull) | `client.py` pulls from a server app running **on the phone** | phone hotspot: iOS isolates hotspot clients (AP isolation, confirmed with tcpdump), so the phone cannot reach the computer, but the computer can reach the phone at `172.20.10.1`. No mobile data is used |

`client.py` supports two sources with the same interface:
- `share`: the third-party *Share Server* app (port 8888);
- `skillbill`: the Skill Bill app in serving mode.

Both directions reuse the same filing logic from `server.py`, so the folder tree is identical
whichever way a file arrives.

## Folder layout

```
<YEAR>/<Country>/<Region>/<City>/<YYYYMMDD>/<District>/<Neighbourhood>/<HHMM>/<file>
```

Date and place are interleaved on purpose. Missing data gives `No_GPS/` or `Unknown_Date/`.

- **Where the data comes from:** coordinates and dates come from the app or, when Expo Go does not
  provide them, from the file itself. `image_metadata.py` reads EXIF in JPEG and HEIC, and
  `mp4_metadata.py` reads QuickTime atoms in MP4 and MOV, including Apple's
  `com.apple.quicktime.location.ISO6709`. Both parsers are pure Python, read files in fragments
  and work on multi-gigabyte videos.
- **Place names:** they come from OpenStreetMap Nominatim reverse geocoding. The script respects its
  1 request/second limit and caches results by coordinates rounded to about 100 m.
- **Duplicate names:** names are tiered so that two keys describing the same unit
  (`city_district="Old Town"` and `suburb="Stare Miasto"`) yield one folder, not two.

## Resumable, out-of-order upload

- `POST /manifest` registers a file and derives its target folder from metadata.
- `GET /probe` returns how many bytes the server already holds, so an interrupted upload resumes
  where it stopped.
- `POST /upload-chunk` accepts a fragment at the position given in the `X-Offset` header.

Fragments may arrive in any order and in parallel. The server records the received byte ranges,
which keeps a resumed transfer free of gaps and duplicates. The app tunes the chunk size and the
number of chunks in flight to what the tunnel can take.

## Verifying before deleting from the phone

1. `build_verify_index.py` hashes the whole library once and writes an index into `_verify/`. It
   uses multiple threads because the files sit on a Windows drive seen through 9p, where latency,
   not throughput, is the limit. Measured: 1 thread 115 MB/s, 8 threads 275 MB/s, 16 threads
   331 MB/s.
2. The phone downloads the index once (`GET /verify/bundle`) and then checks its photos **offline**.
   MD5 is used because it is the only digest Expo Go can compute itself.
3. `GET /have`, `POST /verify` and `GET /verify/stats` answer single questions and report coverage.

The index builder only reads media files. It writes nothing outside `_verify/` and never deletes
anything.

## Running

```bash
python3 -m venv .venv && .venv/bin/pip install flask requests
.venv/bin/python server.py                              # listens on port 9001
ngrok http 9001                                         # public tunnel for the app

.venv/bin/python client.py --source share --host 172.20.10.1 --watch   # pull over the hotspot
.venv/bin/python client.py --list-only                                 # only list what the phone offers
.venv/bin/python build_verify_index.py --workers 16                    # build the verification index
.venv/bin/python upload_single_file.py /path/to/file                   # test upload of one file
```

The library root is set in `server.py` (`BASE_UPLOAD_DIR`). The tunnel address is set in
`upload_single_file.py` and in the app (`SERVER_BASE`).

## Security

This is a personal tool, and the server is **not** hardened for exposure on the internet:
- there is no authentication;
- `app.run(..., debug=True)` enables the Werkzeug debugger, which allows executing code on the
  computer when it is reachable through a public tunnel.

Run the tunnel only for the duration of a transfer. Before any wider use, add a shared token check
and switch debug mode off.

## Files

| File | Purpose |
|---|---|
| `server.py` | Flask server: manifest, probe, chunked upload, verification endpoints, filing by date and place |
| `client.py` | pull mode: downloads from a server app on the phone, same filing logic |
| `image_metadata.py` | EXIF reader for JPEG and HEIC, without external libraries |
| `mp4_metadata.py` | metadata reader for MP4 and MOV atoms, without external libraries |
| `build_verify_index.py` | one-off MD5 index of the library |
| `verify_index.py` | fast lookups in that index, used by the server |
| `upload_single_file.py` | command-line test upload of one file |
| `hotspot.sh`, `Share_Server_client.sh` | shortcuts for the two pull setups |
