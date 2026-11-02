# StateLife DropShare — Performance results

Numbers recorded by the scripts in `scripts/perf/` (OQ-40), newest at the bottom. Each entry
says where it was measured. Runs on the development Mac use a local server and PostgreSQL and a
smaller scale; the numbers that count for go-live come from the Windows machine or staging over
a real network, at full scale.

| Requirement | Script | Full-scale command |
|---|---|---|
| NFR-PERF-01 metadata p95 < 300 ms, 100 users | `metadata_load.py` | `--users 100 --seconds 300` |
| NFR-PERF-02 small file on another device < 10 s (p95) | `propagation.py` | `--files 100` |
| NFR-PERF-04 ≥ 80% of bandwidth for large transfers | `throughput.py` | `--mb 4096`, over a real network |
| NFR-PERF-05 initial sync survives restarts | `initial_sync.py` | `--files 50000 --gb 50` |

Run each as `uv run python scripts/perf/<script> --server <url> …` against a development or
staging server (they create `perf-…` test accounts there).

**Note on the first NFR-PERF-04 run below (2026-10-30):** on localhost the "available
bandwidth" is a memory copy (2.9 GB/s), so the ratio measures the client's own hashing and
disk speed, not a network. Both client rates (273 MiB/s up, 1,194 MiB/s down) are above a full
1 Gbit/s link (119 MiB/s). It must be measured again over a real network.

**Note on the first NFR-PERF-01 run below:** its 3 failed calls came from the run's own
settings (4 workers × a 50-connection pool against PostgreSQL's 100 connections), not the
app; the second run uses the default pool. In deployment, workers × (DB_POOL_SIZE +
DB_MAX_OVERFLOW) must stay below the database's connection limit (M14).

## NFR-PERF-04 large transfer bandwidth — 2026-10-30 09:03 UTC

- **Target:** at least 80% of the available bandwidth
- **Result:** upload 71%, download 41% of the raw transfer — ❌ not met
- **Where:** Devs-MacBook-Pro-71.local (macOS-27.0.1-arm64-arm-64bit), Python 3.12.15
- 512 MiB file against http://127.0.0.1:8765
- upload: client 273 MiB/s, raw 387 MiB/s
- download: client 1194 MiB/s, raw 2901 MiB/s
- one file, one chunk at a time (chunks of one file in parallel: only if below target, plan M10 Q2)

## NFR-PERF-01 metadata API — 2026-10-30 09:04 UTC

- **Target:** p95 < 300 ms at 100 concurrent active users
- **Result:** p95 131 ms over 39405 calls (876 calls/s) — ✅ met
- **Where:** Devs-MacBook-Pro-71.local (macOS-27.0.1-arm64-arm-64bit), Python 3.12.15
- 100 users for 45 s against http://127.0.0.1:8765
- `GET /me`: n=7881, p50=84.2, p95=118.8, p99=144.8, mean=85.0 ms
- `GET /namespaces`: n=7881, p50=95.9, p95=131.6, p99=157.7, mean=96.3 ms
- `GET /namespaces/{id}/changes`: n=7881, p50=93.2, p95=127.9, p99=154.7, mean=93.7 ms
- `GET /nodes/{id}/children`: n=7881, p50=100.4, p95=139.5, p99=163.6, mean=100.9 ms
- `GET /trash`: n=7881, p50=96.7, p95=131.3, p99=157.2, mean=96.8 ms

## NFR-PERF-01 metadata API — 2026-10-30 09:06 UTC

- **Target:** p95 < 300 ms at 100 concurrent active users
- **Result:** p95 31 ms over 22670 calls (378 calls/s), 0 failed — ✅ met
- **Where:** Devs-MacBook-Pro-71.local (macOS-27.0.1-arm64-arm-64bit), Python 3.12.15
- 100 users for 60 s against http://127.0.0.1:8765
- `GET /me`: n=4534, p50=16.6, p95=25.3, p99=31.4, mean=15.2 ms
- `GET /namespaces`: n=4534, p50=20.0, p95=30.5, p99=37.3, mean=18.4 ms
- `GET /namespaces/{id}/changes`: n=4534, p50=19.6, p95=29.7, p99=37.3, mean=18.1 ms
- `GET /nodes/{id}/children`: n=4534, p50=22.0, p95=32.9, p99=38.9, mean=20.0 ms
- `GET /trash`: n=4534, p50=21.0, p95=31.2, p99=37.4, mean=18.9 ms

## NFR-PERF-02 change propagation — 2026-10-30 09:19 UTC

- **Target:** a small file visible on another device within 10 s (p95)
- **Result:** p95 4.17 s over 20 files, 0 not seen within 60 s — ✅ met
- **Where:** Devs-MacBook-Pro-71.local (macOS-27.0.1-arm64-arm-64bit), Python 3.12.15
- two engines, one user, against http://127.0.0.1:8765
- seconds: n=20, p50=4.1, p95=4.2, p99=4.2, mean=4.1
- includes the 2 s a new file must stay unchanged before it is uploaded (FR-SYNC-06)

## NFR-PERF-05 initial sync — 2026-10-30 12:46 UTC

- **Target:** an initial sync completes without intervention and survives a restart midway
- **Result:** 200 files / 0.2 GB up in 0.1 min and down in 0.1 min, each killed and restarted once; 0 files wrong or missing — ✅ met
- **Where:** Devs-MacBook-Pro-71.local (macOS-27.0.1-arm64-arm-64bit), Python 3.12.15
- against http://127.0.0.1:8765; full scale is 50,000 files / 50 GB
- upload 32 files/s, 32 MB/s; download 50 files/s, 50 MB/s
- killed (SIGKILL) at 50% of the files moved

## NFR-PERF-05 initial sync — 2026-10-30 12:48 UTC

- **Target:** an initial sync completes without intervention and survives a restart midway
- **Result:** 5000 files / 5.0 GB up in 1.0 min and down in 0.3 min, each killed and restarted once; 0 files wrong or missing — ✅ met
- **Where:** Devs-MacBook-Pro-71.local (macOS-27.0.1-arm64-arm-64bit), Python 3.12.15
- against http://127.0.0.1:8765; full scale is 50,000 files / 50 GB
- upload 82 files/s, 82 MB/s; download 248 files/s, 248 MB/s
- killed (SIGKILL) at 50% of the files moved
