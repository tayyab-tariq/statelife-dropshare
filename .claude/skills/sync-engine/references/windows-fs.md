# Windows file-system rules

Loaded by the `sync-engine` skill for any code touching the local disk. OS calls live in `client/platform/windows/`.

**Development happens on macOS**, where none of the behaviours below can be reproduced (APFS is
case-insensitive like NTFS, but locking, long paths, reserved names, the Recycle Bin and
`ReadDirectoryChangesW` all differ). Cover them with `@pytest.mark.windows_only` tests and a
manual check list for the Windows machine. Also note macOS may hand you NFD-normalized names; always
normalize to NFC before comparing or sending to the server.
Requirements: NFR-COMPAT-02/03/04, FR-SYNC-01/06/07/09, FR-SHARE-05.

## Names and paths

- Validate every incoming server name with `dropshare_shared.naming` before creating it locally. Names that are illegal on Windows (reserved device names, `< > : " / \ | ? *`, trailing dot or space) go to "Can't sync" with a reason; they don't block the rest of the sync.
- NTFS is case-insensitive but case-preserving: compare normalized names; handle case-only renames as a two-step rename via a temporary name.
- Long paths: use absolute paths and the `\\?\` prefix (via a platform helper) for file operations when a path may exceed 260 characters (NFR-COMPAT-03). Don't rely on the user having enabled the long-path registry setting.
- Unicode: normalize to NFC when comparing; keep the on-disk form for display.

## Sync root

- Must be a local fixed NTFS volume: reject network drives, removable drives, the root of a drive, system folders, and folders inside another sync client's folder (FR-SYNC-01).
- Hidden temp folder `<sync root>\.dropshare-tmp\` (hidden attribute set) for partial downloads and fallback "deleted" items; ignored by the scanner and watcher.
- If the sync root disappears (deleted, drive missing), stop syncing immediately and show an error — never interpret it as "user deleted everything".

## Locked and changing files

- Office and other apps hold exclusive locks; antivirus scanners hold short-lived handles. Treat `PermissionError`/sharing violations as **retry later** with backoff (e.g. 2 s → 30 s cap), shown as "waiting", not as errors.
- Ignore Office lock/temp files (`~$*`, `*.tmp`) and editor swap files (FR-SYNC-07).
- A file is stable when size and mtime are unchanged for 2 s and it can be opened for reading.

## Deleting and attributes

- User files are only ever removed via `RecycleBin.send(path)` (platform helper using `SHFileOperationW` with `FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_SILENT | FOF_NOERRORUI`). See `algorithm.md` §8 for the fallback.
- Viewer mounts: set `FILE_ATTRIBUTE_READONLY` on files; clear it before applying remote updates and set it again after.
- Never follow junctions/symlinks out of the sync root; skip them and list them in "Can't sync".

## File identity

- `os.stat(path).st_ino` gives the NTFS file ID, stable across renames and moves on the same volume. Store it in the index for move detection; it changes when a file is copied or replaced by some editors (save-as-new-file), so fall back to size + hash.

## Watcher caveats

- `ReadDirectoryChangesW` (used by watchdog) can overflow and drop events under heavy load. Treat events as hints and schedule a scan of the affected directory on overflow, plus the periodic full scan.
- Events can arrive out of order and in bursts (save via temp-file-and-rename); debounce and coalesce by path before reconciling.
