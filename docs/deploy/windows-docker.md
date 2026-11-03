# Production on Windows with Docker Desktop (OQ-50)

Interim hosting (OQ-48): the server runs in Docker Desktop on a Windows server at `<server IP>`.
An existing nginx on another machine ends TLS and forwards the DropShare domain to
`http://<server IP>:5009`. Every push to `main` is tested on GitHub's Linux runners and then
deployed by a self-hosted runner on the server (`.github/workflows/deploy.yml`).

| Container | What it does |
|---|---|
| `dropshare-postgres-1` | PostgreSQL 16. Data in the Docker volume `dropshare_dropshare-postgres`. Port 5432 on the server's loopback only. |
| `dropshare-api-1` | Runs the migrations, then the API (4 workers) on port 5009: loopback only until `DROPSHARE_API_BIND` is set (step 5). |
| `dropshare-jobs-1` | Runs the cleanup jobs (NFR-REL-05) at each deploy and then daily. |
| `dropshare-watchdog-1` | Restarts the API if its health check fails. |

Users' files live in `E:\DropShareData`, a normal Windows folder mounted into the containers.

Known gaps, recorded in requirements OQ-50:

- **No backups** (NFR-REL-06). Needed, with a tested restore, before go-live.
- **The app uses the PostgreSQL superuser**, so database grants don't protect the audit log
  (FR-AUDIT-03). Keep `E:\DropShareData` and the host `server\.env` (below) locked down.
- **nginx → app is plain HTTP** across the internal network. Passwords, tokens and files cross it
  unencrypted, so the network between the proxy and `<server IP>` must be trusted.

## Never do this

- **Never use Docker Desktop's "Reset to factory defaults" or "Clean / Purge data".** They delete
  the database volume, and there are no backups yet.
- **Don't change `DROPSHARE_TOKEN_SECRET`** without a reason, such as a leak. It signs the
  15-minute access tokens only: changing it rejects those, and every client fetches a new one
  with its refresh token, so nobody is signed out.
- **Changing `DROPSHARE_POSTGRES_PASSWORD`** in GitHub alone breaks the app: PostgreSQL reads it
  only when the volume is first created. Change it in the database first
  (`ALTER USER <DROPSHARE_POSTGRES_USER> PASSWORD '…'`), then in GitHub.
- **Never empty or move `E:\DropShareData`** while the stack runs.

## One-time server setup

### 1. Docker Desktop

1. Install Docker Desktop with the WSL 2 backend.
2. Settings → General: turn on **Start Docker Desktop when you sign in**.
3. Settings → Software updates: turn **off** automatic updates. An update restarts the engine and
   with it DropShare; update by hand in a maintenance window.
4. Settings → Resources → Advanced: put the **disk image location** on `E:` (the database lives
   there) and raise the virtual disk limit if needed.
5. Docker Desktop runs only while a user is signed in. After a reboot DropShare stays down until
   someone signs in, unless the server signs in automatically.
6. Check, in PowerShell: `docker info --format '{{.OperatingSystem}} / {{.OSType}}'` prints
   `Docker Desktop / linux`.

### 2. Tools and folders

```powershell
winget install Git.Git
winget install Microsoft.PowerShell      # the workflow runs PowerShell 7 (pwsh)
New-Item -ItemType Directory E:\DropShareData
```

The deploy refuses to start if `E:\DropShareData` is missing; Docker never creates it.

### 3. GitHub runner

1. DropShare repo → Settings → Actions → Runners → **New self-hosted runner** → Windows, x64.
   Run the commands it shows in a new folder such as `C:\actions-runner-dropshare`. A runner
   registered for another repository can't run this one's workflows.
2. When `config.cmd` asks for labels, enter `dropshare`. Answer **Y** to run as a service.
3. When it asks for the service account, give the Windows user that runs Docker Desktop
   (`.\<user>` and its password), not the default `NETWORK SERVICE`. That user must be in the
   local `docker-users` group (`net localgroup docker-users`; add with
   `net localgroup docker-users <user> /add`, then sign out and in). Otherwise Docker answers
   "permission denied … docker_engine".

### 4. Locking down GitHub

The runner can control Docker, and through it the whole server: **anyone with write access to
this repository is effectively an administrator of `<server IP>`.** Any workflow file on any
branch can ask for the runner, so these settings are what stand between a branch and production:

1. **Keep the repository private.**
2. **Protect `main`** (Settings → Branches): changes only through a reviewed pull request.
   Already done.
3. **Limit the environment to `main`**: Settings → Environments → `dropshare-production` →
   Deployment branches and tags → **Selected branches and tags** → Add rule → `main`. The
   secrets are then given only to jobs running from `main`, whatever a workflow on another
   branch says. (The workflow also skips the deploy outside `main`, but a branch can change its
   own copy of the workflow, so only this setting protects the secrets.)
4. **Fork pull requests**: Settings → Actions → General → "Approval for running fork pull request
   workflows from contributors" → **Require approval for all external contributors**.
5. Give write access only to people who may administer the server.

### 5. GitHub environment

In the `dropshare-production` environment, add the secrets:

| Name | Value |
|---|---|
| `DROPSHARE_POSTGRES_PASSWORD` | A long random password |
| `DROPSHARE_TOKEN_SECRET` | `[Convert]::ToBase64String([Security.Cryptography.RandomNumberGenerator]::GetBytes(48))` |

Variables (all optional):

| Name | Default | When |
|---|---|---|
| `DROPSHARE_API_BIND` | `127.0.0.1` | `<server IP>` once the firewall rule (step 6) is in place and tested |
| `DROPSHARE_STORAGE_PATH` | `E:\DropShareData` | Only if the folder moves |
| `DROPSHARE_TRUSTED_PROXIES` | `[]` | After step 6, see "Trusting the proxy" |
| `DROPSHARE_RELEASE_PUBLISHER` | empty | When the code-signing certificate exists (OQ-6) |
| `DROPSHARE_RELEASE_CERT_SHA256` | `[]` | Same, as a JSON list: `["<sha256>"]` |
| `DROPSHARE_POSTGRES_USER`, `DROPSHARE_POSTGRES_DB` | `dropshare` | Before the first deploy only |

### 6. Firewall, then open the port

The app speaks plain HTTP on port 5009, so only the proxy may reach it (NFR-SEC-01). Until this
step is done the app listens on the server's loopback only and the proxy can't reach it.

1. Allow the proxy:

   ```powershell
   New-NetFirewallRule -DisplayName "DropShare from proxy" -Direction Inbound -Protocol TCP `
     -LocalPort 5009 -RemoteAddress <proxy internal IP> -Action Allow
   ```

2. Docker Desktop may add its own allow rules for its backend program, which would let anyone
   in. Look with
   `Get-NetFirewallRule -DisplayName '*Docker*' | Format-Table DisplayName, Enabled, Action`
   and disable any inbound allow rule that isn't limited to the proxy.
3. Set the GitHub variable `DROPSHARE_API_BIND` to `<server IP>` and redeploy (Actions → Deploy
   DropShare → Run workflow).
4. Test: from the proxy, `curl http://<server IP>:5009/health` answers; from any other machine,
   `Test-NetConnection <server IP> -Port 5009` fails. If the second one succeeds, fix the
   firewall before going further.

### 7. The proxy owner

Send them `docs/deploy/reverse-proxy.md`. In short: upstream `http://<server IP>:5009`; TLS 1.2+;
one HSTS header; replace `X-Forwarded-For` with `$remote_addr` and set `X-Forwarded-Proto`;
`client_max_body_size 16m` with request buffering off; `proxy_read_timeout 90s`; the rate limits
in `nginx.conf.example`.

## Deploying

Merge a pull request into `main`. The workflow tests, then runs `docker compose up -d --build` on
the server and checks `http://127.0.0.1:5009/health/ready`. To redeploy without a change:
Actions → Deploy DropShare → **Run workflow** (from `main`).

## Trusting the proxy

Only after step 6 is in place and tested. Until `DROPSHARE_TRUSTED_PROXIES` is set, the audit log
records the proxy's address for every request. Sign in once through the domain, then on the
server:

```powershell
docker exec dropshare-postgres-1 psql -U dropshare -d dropshare -c "SELECT occurred_at, action, source_ip FROM audit_events ORDER BY occurred_at DESC LIMIT 5"
```

`source_ip` is the address the app sees the proxy as. Set the variable to it (`["<address>"]`)
and redeploy. Docker Desktop may show its own gateway address instead of the proxy's. Trusting
that address means trusting every connection that reaches port 5009, so then the firewall rule
is the only thing stopping a forged `X-Forwarded-For`: repeat the test in step 6.4 first, and
leave the variable at `[]` if you can't confirm it.

## Day to day

```powershell
docker ps --filter label=com.docker.compose.project=dropshare   # status
docker logs --tail 100 dropshare-api-1                         # API log (JSON lines)
docker logs --tail 50 dropshare-jobs-1                         # last job runs
docker restart dropshare-api-1
```

Admin CLI (FR-ADMIN-01), for everything except publishing releases:

```powershell
docker exec -it dropshare-api-1 dropshare-server --help
docker exec -it dropshare-api-1 dropshare-server users list
```

## Publishing a client release (on the host)

`releases publish` checks the installer's Authenticode signature, so it runs on Windows, not in a
container (OQ-49). One-time: install uv, clone the repo to a folder of its own (not the runner's
work folder), run `uv sync`, and create `server\.env`. Use the same database values as the GitHub
secrets and variables:

```dotenv
DB_DIALECT=postgresql
DB_HOST=127.0.0.1
DB_PORT=5432
DB_NAME=dropshare
DB_USER=dropshare
DB_PASSWORD=<the DROPSHARE_POSTGRES_PASSWORD secret>
# Its own random value, NOT the production DROPSHARE_TOKEN_SECRET: the CLI never issues tokens,
# it only needs 32+ characters. Generate it with the same PowerShell one-liner.
TOKEN_SECRET=<a separate random value>
STORAGE_LOCAL_ROOT=E:\DropShareData
RELEASE_PUBLISHER=<as in GitHub>
RELEASE_CERT_SHA256=["<as in GitHub>"]
```

The file holds the database superuser's password, so only you and administrators may read it:

```powershell
icacls server\.env /inheritance:r /grant:r "${env:USERNAME}:R" "Administrators:F"
```

Keep that clone on the same commit as `main`, then:
`uv run --package dropshare-server dropshare-server releases publish --help`.

## Windows checks after the first deploy

1. The deploy is green and `http://127.0.0.1:5009/health/ready` reports database and storage `ok`.
2. Upload a file of several GB from a client (through the proxy, or on the server against
   `http://127.0.0.1:5009`). It completes, and a new file appears under
   `E:\DropShareData\blobs`. This proves the folder mount handles chunk writes, `fsync` and the
   final rename. If uploads fail with permission or "access denied" errors on rename, look at
   antivirus scanning of `E:\DropShareData` before anything else.
3. After step 6: binding to `<server IP>` works and the port tests pass.
4. Reboot, sign in, and check the stack comes back on its own.
