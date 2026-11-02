# Reverse proxy (NFR-SEC-01, NFR-SEC-06)

The DropShare server is never exposed directly. A reverse proxy on Linux ends TLS, sends HSTS,
and rate-limits, then forwards to the app (requirements §12, production topology). Who runs
the proxy is still open (OQ-8); this page and [`nginx.conf.example`](nginx.conf.example) are the
reference for whoever does. If another proxy is used, it must do the same things listed here.

## What the proxy does

| Concern | Setting | Why |
|---|---|---|
| TLS 1.2+ only | `ssl_protocols TLSv1.2 TLSv1.3` | NFR-SEC-01 |
| HSTS | `max-age=31536000`, no `includeSubDomains` | NFR-SEC-01. Other hosts on the organization's domain aren't ours to pin. The app sends the same header as a fallback; the proxy drops the app's copy so there is exactly one. |
| No plain HTTP | no port 80 listener (or redirect only) | NFR-SEC-01. The client refuses `http://` except for localhost (OQ-12). |
| Who is "one caller" | an IPv4 address, or an IPv6 /64 | Anyone with IPv6 gets a whole /64 and could use a fresh address per request. The config groups addresses by /64 (see the comment on `$dropshare_client_key`); a /64 can end up as at most two keys, never more. Keep the separate `listen 443` and `listen [::]:443` (nginx's default `ipv6only=on`): with one dual-stack socket every IPv4 caller would show as `::ffff:a.b.c.d` and share a single key. |
| Global rate limit | 100 requests/s per caller, burst 500 | NFR-SEC-06. A flood guard: an initial sync makes about 3 requests per small file and ran at 80-250 files/s from one client (`docs/perf-results.md`). |
| Sign-in limit | `POST /api/v1/auth/login` and `/api/v1/me/password`: 20/min per caller, burst 10 | NFR-SEC-06, on top of the per-account lockout (FR-AUTH-06). Token refresh isn't in this zone: refresh tokens can't be guessed, and every device refreshes about every 15 minutes. |
| 429 answer | JSON error envelope, `RATE_LIMITED`, `Retry-After: 10`, `X-Request-ID` | The client shows it like any API error and retries sync work later. A refused refresh keeps the user signed in: the proxy refuses before the server sees the token. The request ID matches the proxy's log line (log `$request_id` in the access-log format). |
| Client address | `X-Forwarded-For` **replaced** with `$remote_addr` | The audit log records the real caller (FR-AUDIT-02); anything a caller sent is discarded. |
| Scheme | `X-Forwarded-Proto $scheme` | Tells the app the request came over HTTPS (for its HSTS fallback). |
| Uploads | `client_max_body_size 16m`, request buffering off | Chunks are 8 MiB (`UPLOAD_CHUNK_SIZE_BYTES`). Raise this if the chunk size is raised. |
| Long-poll | `proxy_read_timeout 90s` | The server holds `/changes/wait` up to 55 s; the client waits 75 s. |

## Office NAT

Users in the office usually share one public address, so they share the per-address limits.
The global limit is set well above one office's normal use. If it still bites, add the office
egress range to the `geo $dropshare_global_exempt` block: that exempts it from the global limit
only. Sign-in stays limited (and account lockout applies to everyone).

## App settings that must match

- `TRUSTED_PROXIES`: the proxy's address(es) as the app sees them, e.g. `["10.0.0.5"]`. The app
  believes `X-Forwarded-For` and `X-Forwarded-Proto` only from these. Unset, the audit log shows
  the proxy's address for every request.
- Run uvicorn with `--no-proxy-headers`, so `TRUSTED_PROXIES` is the one place proxies are
  trusted. (By default uvicorn rewrites the client address from `X-Forwarded-For` for
  127.0.0.1, which works but splits the rule across two places.)
- Bind the app to an internal address only (`--host 127.0.0.1` when the proxy is on the same
  host). Nothing may reach it except the proxy.

## Requirements

`http2 on;` needs nginx 1.25.1 or later; on older versions use `listen 443 ssl http2;` instead.
The example was checked with `nginx -t` and run on nginx 1.27 against a stub upstream (one HSTS
header, a spoofed `X-Forwarded-For` replaced, TLS 1.1 refused, the 12th sign-in in a burst
answered 429 with the JSON body and a request ID, and the /64 grouping on sample IPv6
addresses). It has not yet been run in front of the real app on staging, and the global limit
hasn't been driven past 100 requests/s.

## Check on staging

Replace `dropshare.example.com` with the real name.

```bash
# HSTS once, TLS 1.2+
curl -sI https://dropshare.example.com/health | grep -ci strict-transport-security   # 1
curl -s --tls-max 1.1 https://dropshare.example.com/health                           # fails

# Sign-in limit: the 12th quick attempt is refused with the JSON envelope
for i in $(seq 1 12); do curl -s -o /dev/null -w "%{http_code} " -X POST \
  -H 'Content-Type: application/json' -d '{}' https://dropshare.example.com/api/v1/auth/login; done
# 400 x11 then 429

# Audit records the real address: sign in from a known machine, then
uv run --package dropshare-server dropshare-server audit export --output /tmp/a.csv --action auth.login
# the source IP column is that machine's address, not the proxy's
```

Also run an initial sync from one Windows client (NFR-PERF-05) and confirm the proxy log shows
no `limiting requests` lines for it.
