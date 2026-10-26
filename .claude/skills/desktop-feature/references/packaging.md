# Packaging, signing, and auto-update (Windows)

Loaded by the `desktop-feature` skill for installer, build, signing, and updater work.
Requirements: FR-UPD-01..05, OQ-6 (certificate).

## Build pipeline

`scripts/build_client.ps1` runs these steps on **Windows only** (the Windows machine or a Windows CI runner; PyInstaller can't cross-compile and Inno Setup is Windows-only). It fails fast with a clear message if started on another OS. Each step fails the build on error:

1. **Version**: read from `client/pyproject.toml`; must match the git tag for release builds.
2. **Freeze** with PyInstaller using the checked-in spec file `installer/dropshare.spec`, **one-folder** mode (fewer antivirus false positives and faster start than one-file). Include Qt plugins needed for Windows, the SVG icon plugin, app resources, and package metadata (`copy_metadata`) for anything that reads `importlib.metadata`. Embed a Windows version resource (product name, version, publisher).
3. **Sign** the main executable (and any other executables we ship) with `signtool sign /fd sha256 /tr <RFC3161 timestamp URL> /td sha256` using the organization certificate. The certificate/key is never in the repo or on developer machines; signing runs in CI or via a cloud/HSM signing service (OQ-6).
4. **Installer** with Inno Setup (`installer/dropshare.iss`):
   - `PrivilegesRequired=lowest`, `DefaultDirName={localappdata}\Programs\StateLife DropShare` (per-user, no admin rights).
   - Register the `statelife-dropshare://` URL scheme under `HKCU\Software\Classes\statelife-dropshare` pointing to the app with `"%1"` (FR-SHARE-07); removed on uninstall.
   - Start-menu shortcut; start-at-sign-in entry in `HKCU\Software\Microsoft\Windows\CurrentVersion\Run` with `--background` (the app's Settings toggle owns it afterwards).
   - Uninstall keeps the sync folder; asks whether to remove `%LOCALAPPDATA%\StateLife\DropShare` app data.
   - Closes the running app before upgrading files (`CloseApplications`), and relaunches it after a silent update.
5. **Sign** the installer.
6. **Checksum**: compute SHA-256 of the signed installer.
7. **Publish** (release builds only): upload installer, version, channel, minimum supported version, notes, and checksum via the server CLI release command (FR-ADMIN-01, FR-UPD-05).

Unsigned builds are for local testing only and are never published (FR-UPD-02).

## Auto-update flow (client)

Implemented in `services/updater.py` (logic) + `controllers/update_controller.py` (UI signals),
with signature checks in `platform/windows/`.

1. Check `GET /client/releases/latest?platform=windows&channel=<channel>` at startup and every 6 h, and immediately on a `426 UPGRADE_REQUIRED`.
2. Compare versions numerically (`X.Y.Z` → tuple of ints). Ignore releases that are not newer.
3. Download the installer in a worker to a temp folder (resumable via `Range`), then verify:
   - SHA-256 equals the manifest checksum, **and**
   - Authenticode signature is valid and the signer matches the configured publisher (certificate subject/thumbprint allowlist in client config shipped with the build). The Windows implementation can use `Get-AuthenticodeSignature` via `powershell -NoProfile`, or `WinVerifyTrust` via `ctypes`.
   - If either check fails: delete the file, log the reason, show a clear error, and do not run it.
4. Offer "Restart and update" or "Later" (mandatory if below the minimum supported version: sync stays stopped until updated).
5. To install: stop the sync engine at a safe point, launch the installer with Inno Setup's silent flags (`/VERYSILENT /SUPPRESSMSGBOXES /NORESTART`), and quit. The installer relaunches the app; sync resumes from the index (state survives updates).
6. Downloads only from the configured server over HTTPS; never follow a URL to another host.

## Versioning

- Semantic versions `MAJOR.MINOR.PATCH`. The server's minimum supported version moves only when an API change breaks older clients.
- The client sends its version in `X-Client-Version` on every request.
- Release notes are plain text, shown in the update prompt.
