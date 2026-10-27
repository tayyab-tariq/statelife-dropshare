# Desktop client architecture

Loaded by the `desktop-feature` skill and the `ui-reviewer` agent.
Requirements: §6.10, §6.11, NFR-PERF-03, NFR-COMPAT-04, NFR-MAINT-03.

## Contents
1. Layers
2. Threading model
3. Worker helper
4. API client
5. Tokens and credentials
6. Paths, config, and logging
7. Platform layer
8. App startup and lifetime

## 1. Layers

```
ui/            widgets, dialogs, Qt item models        → imports controllers, shared models, Qt
controllers/   QObjects: slots for actions, signals    → imports services, sync engine adapter, worker helper
services/      api client, auth, transfers, updater    → imports shared, httpx, platform
sync/          sync engine (no Qt imports)             → imports services, store, platform
store/         local SQLite index (SQLAlchemy)         → used by sync/ only
platform/      OS-specific code behind a protocol      → used by services/sync/app wiring
```

Rules:
- `ui/` never imports `services/`, `sync/`, `store/`, or `httpx`. If a widget needs data, a controller provides it via a signal.
- `sync/` and `services/` don't import Qt (except a thin adapter in `controllers/`), so they're testable with plain pytest and portable to a future CLI or macOS build.
- Dependencies are wired once in `app.py` and passed into constructors. No module-level singletons or global state.

## 2. Threading model

The GUI thread only handles events and paints. Everything that can take more than ~16 ms runs elsewhere:

| Work | Where |
|---|---|
| One-off API calls (load folder, share, admin actions) | `QThreadPool` via the worker helper |
| Sync engine (scan, reconcile, transfers) | Its own Python threads, owned by the engine; Qt-free |
| File watching | watchdog's observer thread, feeding the engine's queue |
| Long-poll for remote changes | A dedicated engine thread |
| Update download and verification | Worker helper |

- Results return to the GUI thread **only via signals**. Emitting a signal from any thread is safe; Qt queues delivery to the receiver's thread. Never call widget methods from another thread.
- Long-running work accepts a `threading.Event` (or similar) for cancellation; closing a window cancels its pending tasks.
- On quit: stop accepting new work, ask the sync engine to stop at a safe point (journal flushed), wait with a timeout, then exit.
- `time.sleep` is forbidden in `ui/` and `controllers/`. Use `QTimer` for delays.

## 3. Worker helper

One small helper in `controllers/worker.py`; don't reinvent it per feature.

```python
class _Signals(QObject):
    succeeded = Signal(object)
    failed = Signal(object)   # the exception

class Task(QRunnable, Generic[T]):
    def __init__(self, fn: Callable[[], T]) -> None:
        super().__init__()
        self.fn = fn
        self.signals = _Signals()

    def run(self) -> None:
        try:
            result = self.fn()
        except Exception as exc:  # reported to the UI, also logged by the controller
            self.signals.failed.emit(exc)
        else:
            self.signals.succeeded.emit(result)
```

Controllers keep a reference to running tasks' signal objects until they finish, map exceptions
to user-facing messages (section 4), and emit typed, feature-specific signals
(`folder_loaded(FolderPage)`, `load_failed(str)`).

The sync engine reports progress through a callback interface; `controllers/sync_controller.py`
implements it by emitting Qt signals (`status_changed`, `activity_added`, `conflict_detected`).

## 4. API client

`services/api_client.py` wraps a single `httpx.Client` (thread-safe for concurrent requests):

- `base_url` from config; timeouts from settings (connect ~10 s, read ~60 s; longer for long-poll and transfers). TLS verification always on (`verify=True`); never disable it.
- Default headers: `X-Client-Version`, `User-Agent: DropShare/<version> (Windows)`, `Authorization: Bearer <access>`.
- Parses every response into the `shared/` envelope models; returns `data` typed by the endpoint function.
- Raises a small exception hierarchy that controllers and the engine branch on:
  - `ApiError(status, code: ErrorCode, message, details, request_id)` for envelope errors
  - `OfflineError` for connection failures/timeouts (engine switches to Offline state and backs off)
  - `UpgradeRequiredError` for 426 (stop sync, offer the update)
  - `SessionExpiredError` when refresh fails (go to the sign-in screen). A refresh that was sent but got no answer also ends the session: the token may already be rotated, and re-sending it would revoke the whole family (OQ-16). Only a request that never connected keeps the token (`OfflineError`).
- Token refresh: on `401 UNAUTHENTICATED`, refresh under a lock so concurrent requests trigger **one** refresh, then retry the original request once.
- Logs method, path, status, duration, and `X-Request-ID`; never headers, tokens, or bodies.

User-facing messages map from `ErrorCode` in one place (`controllers/messages.py`), not scattered through widgets.

## 5. Tokens and credentials

- The refresh token lives only in Windows Credential Manager via `keyring` (behind the platform protocol). The access token lives only in memory.
- Never write tokens to config, logs, diagnostics exports, or crash reports. A logging filter redacts anything that looks like a bearer token.
- Sign-out: call `/auth/logout`, delete the credential, stop the engine, and return to the sign-in window (FR-AUTH-04).

## 6. Paths, config, and logging

- App data: `%LOCALAPPDATA%\StateLife\DropShare\` (from the platform layer): `config.json`, `index.db`, `logs\`.
- Config is a Pydantic model (`ClientConfig`) persisted as JSON with an atomic write (temp file + `os.replace`). It stores the server URL override, sync root path, notification and autostart preferences — never secrets.
- Logging: `logging` with a rotating file handler (e.g. 5 × 10 MB) plus the token-redaction filter. Install `sys.excepthook` and `threading.excepthook` to log unhandled exceptions and show a non-blocking error notice.
- "Export diagnostics" zips logs and a sanitized config (FR-CLIENT-06).

## 7. Platform layer

`platform/base.py` defines a `Protocol` per capability; `platform/windows/` implements them; a
factory picks the implementation by `sys.platform`.

Development happens on macOS, so `platform/macos/` provides **development-grade** implementations
that let the client run on a Mac: Keychain via `keyring`, move to `~/.Trash` for the Recycle Bin,
`st_ino` for file IDs, `open`/`open -R` for the shell, and no-ops for autostart, the link scheme,
and signature verification (the updater is disabled on macOS). It is not shipped (BL-08 is the real
macOS client). Never let macOS behaviour stand in for verifying Windows behaviour (NFR-COMPAT-07).

Capabilities:

- `CredentialStore` — get/set/delete the refresh token (keyring).
- `Autostart` — enable/disable start at sign-in (HKCU `Run` key via `winreg`).
- `RecycleBin` — move a path to the Recycle Bin (`SHFileOperationW` with `FOF_ALLOWUNDO` via `ctypes`).
- `FileIdentity` — stable file ID (`os.stat().st_ino` on NTFS) for move detection.
- `FileAttributes` — set read-only / hidden attributes.
- `Shell` — open a folder in Explorer, reveal a file (`explorer /select,`).
- `SignatureVerifier` — verify an installer's Authenticode signature and publisher (see `packaging.md`).
- `Paths` — app-data dir, default sync root, long-path handling.

Nothing outside `platform/` imports `winreg`, `ctypes.windll`, or calls Windows-only APIs. Each
capability also has a fake for tests. Tests that exercise real Windows APIs are marked
`@pytest.mark.windows_only` and run on the Windows machine or a Windows CI runner.

Looks also differ: Qt uses the native style on each OS, Windows commonly runs at 125–150% scaling,
and fonts differ. Final visual checks happen on Windows.

## 8. App startup and lifetime

1. Enforce a single instance (`QLocalServer`/`QLocalSocket` named per Windows user); a second launch signals the first to show its window and exits (FR-CLIENT-07). If the second launch was started by a `statelife-dropshare://open?node=<uuid>` link, it forwards the link over the socket and the first instance opens that item (FR-SHARE-07).
   - Parse links with `urllib.parse`; accept only scheme `statelife-dropshare`, action `open`, and a valid UUID. Ignore anything else and never pass link contents to a shell or file path.
2. Configure logging, load config, build the API client, services, store, sync engine, controllers.
3. Create the tray icon first; show the main window unless started with `--background` (autostart).
4. If there's no valid session, show sign-in; after sign-in on first run, show the sync-folder chooser (FR-SYNC-01).
5. Start the sync engine. Check for updates (FR-UPD-03).
6. Closing the main window hides it to the tray; **Quit** from the tray shuts down as in section 2.
