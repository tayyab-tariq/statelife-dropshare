# Desktop UI rules (PySide6)

Loaded by the `desktop-feature` skill and the `ui-reviewer` agent.
Requirements: §6.3, §6.10, NFR-UX-01..03.

## 1. Look and feel

- Use the platform's native Qt style (on Windows 11 the modern Windows style) and the system palette. Don't ship a custom stylesheet for the whole app; small, targeted styling only.
- Colours: never hard-code hex values in widgets. If a colour is needed beyond the palette (status green/amber/red for the tray or badges), define it once in `ui/theme.py` with light, dark, and high-contrast variants derived from the palette.
- Icons: SVG (or multi-resolution) via `QIcon`; tray icons in every state at 16/20/24/32 px.
- Layouts everywhere (`QVBoxLayout`, `QGridLayout`, `QFormLayout`); no `setGeometry` or fixed pixel sizes except minimum sizes in logical units. Text must not clip at 200% scaling or with larger system fonts.
- Copy follows NFR-UX-03: sentence case, action-specific buttons ("Share folder", "Restore", "Delete permanently"), errors say what happened and what to do. One name per action throughout the flow.

## 2. Screens (v1)

| Screen | Requirements |
|---|---|
| Sign-in | FR-AUTH-01, FR-AUTH-05 (forced change on first sign-in) |
| Sync-folder chooser (first run) | FR-SYNC-01 |
| Main window: navigation (My files, Shared with me, Trash, Activity, Can't sync) + file browser | FR-FILE-*, FR-TRASH-*, FR-CLIENT-03, FR-SYNC-07 |
| Share dialog (members, roles, "Copy link") | FR-SHARE-01..04, FR-SHARE-07 |
| Settings | FR-CLIENT-04, FR-AUTH-07 |
| Tray icon + menu | FR-CLIENT-01 |
| Update prompt | FR-UPD-03/04 |

The Administration section (FR-ADMIN-02..06) is Phase 2; don't build it in v1.

## 3. The four view states

Every view that shows server or index data has loading, error, empty, and populated states (NFR-UX-01). Use one reusable widget, not ad-hoc labels:

```python
class StatefulView(QStackedWidget):
    retry_requested = Signal()
    def show_loading(self, message: str = "Loading…") -> None: ...
    def show_error(self, message: str) -> None: ...      # message + "Try again" button
    def show_empty(self, message: str, action: QAction | None = None) -> None: ...
    def show_content(self) -> None: ...
```

- **Loading:** a lightweight placeholder that keeps the layout (busy indicator + label); appear only after ~300 ms so fast loads don't flicker.
- **Error:** what failed, and a "Try again" button wired to `retry_requested`.
- **Empty:** explain and offer the next action ("This folder is empty. Drag files here or choose Upload.").
- Keep showing previous content while refreshing an already-loaded view; don't flash back to loading.

## 4. Lists and tables

- Use `QAbstractTableModel`/`QAbstractItemModel` + `QTableView`/`QTreeView`, never per-row widgets.
- Paginated folders: implement `canFetchMore`/`fetchMore` in the model, fetching the next page via the controller.
- Update models with fine-grained signals (`dataChanged`, `rowsInserted`) when sync events arrive; avoid `beginResetModel` on every change.
- Sorting/filtering via `QSortFilterProxyModel`. Stable identity by node ID, not row index.
- Long names elide in the middle; full name in the tooltip and accessible description.

## 5. Accessibility (non-negotiable)

- Every interactive widget has `setAccessibleName`; icon-only buttons also get a tooltip. Add `setAccessibleDescription` where the name alone is ambiguous.
- Form labels use buddies and mnemonics (`QLabel("&Password")` + `setBuddy`).
- Logical tab order (`setTabOrder` where the layout order isn't right); visible focus (don't remove focus rects).
- Keyboard equivalents for everything:
  - Standard keys: `Delete` (to trash), `F2` (rename), `Ctrl+Shift+N` (new folder), `Enter` (open), `Backspace`/`Alt+Up` (parent), `F5` (refresh), `Shift+F10`/Menu key (context menu).
  - Drag-and-drop always has a menu alternative ("Move to…", "Upload…").
- Dialogs: `QDialogButtonBox` with a default button and Escape to cancel; destructive actions require confirmation with the item name in the text.
- Never convey state by colour alone: pair status colours with icons and text.
- Respect high-contrast mode and the system's reduced-animation setting (no decorative animation).

## 6. Feedback

- Disable an action's controls while its request is pending; show progress for anything over ~1 s.
- Transfers show per-item progress and an overall summary in the Activity view and tray tooltip.
- Successful actions confirm briefly (status bar or subtle inline message), not with modal dialogs.
- Errors from the server use the mapped message from `controllers/messages.py`.

## 7. Tray and notifications

- Tray states and menu exactly as FR-CLIENT-01; tooltip shows the current state and, while syncing, "Syncing n items".
- Notifications via `QSystemTrayIcon.showMessage`, only for the types in FR-CLIENT-02, each switchable in Settings.
- Coalesce bursts: "12 files were shared with you" instead of 12 notifications; at most one notification per type per ~10 s.

## 8. Testing UI

- pytest-qt `qtbot`; find widgets by accessible name (`findChild` + a helper matching `accessibleName()`), never by private attribute.
- Drive interactions with `qtbot.mouseClick`/`keyClick`; wait with `qtbot.waitSignal`/`waitUntil`.
- Controllers in UI tests are real, services are fakes, so threading and signals are exercised.
