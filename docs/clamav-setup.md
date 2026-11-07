# ClamAV setup (malware scanning)

DropShare scans every uploaded file with ClamAV before anyone can see it (FR-SAFE-01). There is
no setting to turn this off (OQ-58): without a scanner, uploads wait. This page covers the
development Mac and the tests. Production runs the same image from `docker-compose.yml` (the
`clamav` and `scanner` services; see `docs/deploy/windows-docker.md`).

## How it fits together

- **`clamav`**: the official `clamav/clamav-debian` image (the LTS line). It runs `clamd`, which
  scans, and `freshclam`, which downloads new signatures several times a day from
  `database.clamav.net`. Its settings are in `deploy/clamav/clamd.conf`.
- **The API** scans files up to `SCAN_INLINE_MAX_BYTES` (64 MiB) within `complete`, and answers
  202 for larger ones or while `clamd` is unreachable.
- **The scan worker** (`dropshare-server jobs run scan-uploads --loop`) scans the rest and keeps
  retrying, with growing pauses, while `clamd` is down.

## On the Mac

The Debian-based image is used because it is built for Apple Silicon too; the Alpine-based
`clamav/clamav` isn't.

```bash
docker run -d --name dropshare-clamav --restart unless-stopped \
  -p 127.0.0.1:3310:3310 \
  -v dropshare-clamav:/var/lib/clamav \
  -v "$PWD/deploy/clamav/clamd.conf:/etc/clamav/clamd.conf:ro" \
  clamav/clamav-debian:1.4.6
```

The first start downloads about 300 MB of signatures and loads them: allow a few minutes. It's
ready once `docker logs dropshare-clamav` shows `socket found, clamd started.` On a first start,
`clamd` may load the signatures bundled in the image while `freshclam` downloads newer ones;
`docker exec dropshare-clamav clamdscan --reload` makes it use the new ones (the deploy does
this every time). Signatures are
kept in the `dropshare-clamav` volume, so later starts are quicker. `clamd` needs about 1.5 GB
of memory, briefly twice that while it reloads new signatures; give Docker Desktop at least 4 GB.

In `server/.env`, `CLAMD_HOST=localhost` and `CLAMD_PORT=3310` (the defaults). Then run the scan
worker beside the API:

```bash
uv run --package dropshare-server dropshare-server jobs run scan-uploads --loop
```

Check: `curl -s http://127.0.0.1:8000/health/ready` shows `"scanner": "ok"` and the signatures'
age in hours.

## Tests

- `uv run pytest server/tests` uses a fake scanner and needs no ClamAV.
- Tests marked `clamav` run against a real `clamd` when `CLAMD_TEST_HOST` is set: the EICAR test
  file, EICAR inside a zip, clean content, fresh signatures, and a full scan of the largest
  allowed file, 2,000 MiB (a few minutes).

```bash
CLAMD_TEST_HOST=127.0.0.1 uv run pytest -m clamav server/tests
```

## Troubleshooting

- **Uploads stay "scanning"**: is the worker running, and can it reach `clamd`? Its log names
  each upload it waits for; after ten tries it logs an error.
- **`/health/ready` says the scanner failed**: `docker logs dropshare-clamav`. While signatures
  load after a start, this is expected.
- **Signatures older than 48 hours** (a warning in the API log): `freshclam` can't reach
  `database.clamav.net`; check outbound HTTPS from the server.
- **A file is refused as `Heuristics.Limits.Exceeded`**: it is larger or more deeply nested than
  `clamd` can scan in full, so it is refused rather than accepted unscanned (OQ-58 (2)).
