# Packaging, signing, and auto-update (Windows)

Loaded by the `desktop-feature` skill for installer, build, signing, and updater work.
Requirements: FR-UPD-01..05, OQ-6 (certificate).

## Build pipeline

`scripts/build_client.ps1` runs these steps on **Windows only** (the Windows machine or a Windows CI runner; PyInstaller can't cross-compile and Inno Setup is Windows-only). It fails fast with a clear message if started on another OS. Each step fails the build on error:

1. **Version**: read from `client/pyproject.toml` (`MAJOR.MINOR.PATCH`, OQ-17). Also writes `build_info.json` (the built-in server URL and the expected update publisher, FR-CLIENT-05) and renders `DropShare.ico` from the brand SVG (`scripts/packaging/make_icon.py`).
2. **Freeze** with PyInstaller using the checked-in spec file `installer/dropshare.spec`, **one-folder** mode (fewer antivirus false positives and faster start than one-file). Include Qt plugins needed for Windows, the SVG icon plugin, app resources, and package metadata (`copy_metadata`) for anything that reads `importlib.metadata`. Embed a Windows version resource (product name, version, publisher).
3. **Sign** the main executable (and any other executables we ship) with `signtool sign /fd sha256 /tr <RFC3161 timestamp URL> /td sha256` using the organization certificate. The certificate/key is never in the repo or on developer machines; signing runs in CI or via a cloud/HSM signing service (OQ-6).
4. **Installer** with Inno Setup (`installer/dropshare.iss`):
   - `PrivilegesRequired=lowest`, `DefaultDirName={localappdata}\Programs\StateLife DropShare` (per-user, no admin rights).
   - Register the `statelife-dropshare://` URL scheme under `HKCU\Software\Classes\statelife-dropshare` pointing to the app with `"%1"` (FR-SHARE-07); removed on uninstall.
   - Start-menu shortcut. The start-at-sign-in entry (`HKCU\...\Run`) is the app's own (OQ-44); Setup only removes it on uninstall (OQ-49).
   - Uninstall keeps the sync folder; asks whether to remove local app data, and if so runs `DropShare.exe --remove-app-data`, which deletes only DropShare's own files there and the saved sign-in (`services/app_data.py`).
   - Never closes the app by force (`CloseApplications=no`): Setup waits for the "StateLife DropShare running" mutex (`platform/windows/running.py`) to go, silently for an update, otherwise asking the user to quit it. It relaunches the app after a silent update.
5. **Sign** the installer.
6. **Checksum**: compute SHA-256 of the signed installer.
7. **Publish** (release builds only): upload installer, version, channel, minimum supported version, notes, and checksum via the server CLI release command (FR-ADMIN-01, FR-UPD-05).

Unsigned builds are for local testing only and are never published (FR-UPD-02).

## Auto-update flow (client)

Implemented in `services/updater.py` (logic) + `controllers/update_controller.py` (UI signals),
with signature checks in `platform/windows/`.

1. Check `GET /client/releases/latest?platform=windows-x64` at sign-in and every 6 h, and immediately on a `426 UPGRADE_REQUIRED`. The server picks the channel from the user (OQ-49); release endpoints and token refresh work below the minimum version.
2. Compare versions numerically (`X.Y.Z` → tuple of ints). Ignore releases that are not newer.
3. Download the installer in a worker to a temp folder (resumable via `Range`), then verify:
   - SHA-256 equals the manifest checksum, **and**
   - Authenticode signature is valid, the signer's name equals the publisher in `build_info.json`, its certificate's SHA-256 is one pinned there (`cert_sha256`), and the installer's version resource equals the release version (no downgrade). `WinVerifyTrust` via `ctypes`, in `dropshare_shared/signing.py` (shared with the release CLI, which refuses to publish anything else). An offline revocation check keeps the file and tries again later.
   - If either check fails: delete the file, log the reason, show a clear error, and do not run it.
4. Offer "Restart and update", "Update on next restart" or "Not now" (the dialog opens by itself below the minimum supported version: sync stays stopped until updated).
5. To install now: verify again, remember the update, quit (sync stops at a safe point), then launch the installer with `/VERYSILENT /SUPPRESSMSGBOXES /NORESTART`. On next restart: the remembered update is verified and launched before any window or sync starts, once. The installer relaunches the app; sync resumes from the index (state survives updates).
6. Downloads only from the configured server over HTTPS; never follow a URL to another host.

## Versioning

- Semantic versions `MAJOR.MINOR.PATCH`. The server's minimum supported version moves only when an API change breaks older clients.
- The client sends its version in `X-Client-Version` on every request.
- Release notes are plain text, shown in the update prompt.
