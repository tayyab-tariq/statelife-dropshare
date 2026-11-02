# Releasing the desktop client

FR-UPD-01 … 05, OQ-48, OQ-49. Builds and publishing both run on Windows: the client build machine,
and the Windows server where the admin CLI runs.

Until **OQ-6** (the organization's code-signing certificate) is settled, builds are signed with a
self-signed **test** certificate, trusted only on the test machines. Real distribution to users
also needs **OQ-8** (the production server address, which is built into the installer).

## 1. Test certificate (once, on the build machine)

```powershell
$cert = New-SelfSignedCertificate -Type CodeSigningCert -Subject "CN=StateLife Test" `
    -CertStoreLocation Cert:\CurrentUser\My -NotAfter (Get-Date).AddYears(2)
$cert.Thumbprint   # for -CertThumbprint
Export-Certificate -Cert $cert -FilePath StateLife-Test.cer
```

On **every machine that checks the signature** (the build machine, the Windows server, and each
test client), trust the public certificate for the current user (no key is copied):

```powershell
Import-Certificate -FilePath StateLife-Test.cer -CertStoreLocation Cert:\CurrentUser\Root
Import-Certificate -FilePath StateLife-Test.cer -CertStoreLocation Cert:\CurrentUser\TrustedPublisher
```

The name after `CN=` is the **publisher**: the server's `RELEASE_PUBLISHER` and the build's
`-Publisher` must be exactly that ("StateLife Test"). The certificate itself is **pinned** by its
SHA-256, which the build prints: set it on the server as `RELEASE_CERT_SHA256=["<sha256>"]`.
Another certificate with the same name is refused by the server and by clients.

**Renewing the certificate:** build with the new one and `-AlsoTrust <old SHA-256>`, and list both
on the server. Once every client runs a build that pins the new certificate, drop the old one.

## 2. Build

Set the version in `client/pyproject.toml` (higher than any release on the channel), then:

```powershell
pwsh scripts/build_client.ps1 -ServerUrl https://<server> -Publisher "StateLife Test" `
    -CertThumbprint <thumbprint> -TimestampUrl http://timestamp.digicert.com
```

It prints the installer path, its SHA-256 (also written beside it as `.sha256`) and the signing
certificate's SHA-256. It stops if the signature doesn't verify, names another publisher or
certificate, has no timestamp (without one, every installer stops verifying once the certificate
expires), or if the installer's own version isn't the one in `client/pyproject.toml`.
`-Unsigned` builds are for trying the installer locally; they can't be published and never
install updates.

## 3. Publish (on the server)

Copy the installer to the server, then:

```powershell
uv run --package dropshare-server dropshare-server releases publish --version 0.3.0 `
    --channel beta --min-supported 0.2.0 --installer DropShare-0.3.0-setup.exe `
    --sha256 <sha256> --notes-file notes.txt
uv run --package dropshare-server dropshare-server releases list
```

- **Beta first**: put testers on beta with `users set-channel <username> beta`, publish there,
  and once it is good publish the same installer again as `--channel stable`.
- **`--min-supported`** on a stable release forces older clients to update before they sync
  (`426 UPGRADE_REQUIRED`). Raise it only when older clients would break; it takes effect within a
  minute.
- **A bad release**: `releases withdraw <version> --channel <channel>`. Clients stop being offered
  it, and the previous release is offered again. A client that already downloaded it checks with
  the server before installing (and at its next check) and drops it; only one that is offline
  then still installs it, or installs it at the next start without having checked since.
- Always upgrade the server before publishing a client that needs it (OQ-29).
- **The first release with the updater (from 0.2.0):** installed 0.2.0 clients have no updater,
  so they can't update themselves. Install the new version on every machine by hand (the
  installer upgrades in place, keeping sync state), and keep `--min-supported 0.2.0` until that
  is done; raising it sooner leaves those clients stopped with no way to update.
- A client that is both signed out and too old can't fetch its update (signing in needs a
  supported version, OQ-49): reinstall it by hand.

## 4. What users see

Clients check at sign-in and every 6 hours, download in the background, verify the SHA-256 and the
signature, and offer **Restart and update** or **Update on next restart** (Settings › About shows
the state). Sync stops at a safe point before the installer runs, and the app starts again after.
