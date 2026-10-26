---
name: desktop-feature
description: Build or change the PySide6 (Qt 6) Windows desktop client of this file-sync project following the ui → controllers → services layering, the off-UI-thread worker model, required loading/error/empty/populated states, accessibility and HiDPI rules, the platform abstraction for Windows-specific code, and the packaging/signing/auto-update pipeline. Use this for ANY client-side work — windows, dialogs, widgets, Qt item models, the tray icon, notifications, settings, sign-in, the file browser, sharing screens and internal links, controllers, API client and token handling, the updater, installer, or PyInstaller builds — even if the change looks small.
---

# Desktop feature

Before writing code, read `references/architecture.md` (layers, threading, API client, tokens,
platform layer). For anything visual, also read `references/ui.md` (widgets, states,
accessibility, HiDPI, tray). For installer, signing, or update work, read `references/packaging.md`.
For anything that touches syncing or files on disk, use the `sync-engine` skill as well.

Check `docs/requirements.md` for the requirement IDs and acceptance criteria you are implementing.

## Workflow for a new feature

1. **Plan.** Propose which widgets/dialogs, which controller, which services, and **which thread** each piece of work runs on. For a new screen, sketch the layout (ASCII is fine) and the four view states. Wait for approval if the plan wasn't already approved.
2. **Service layer** (`services/`): typed functions using the `ApiClient` and models from `shared/`. No Qt imports unless it's a Qt-specific service (updater UI hooks are not services).
3. **Controller** (`controllers/`): a `QObject` exposing slots for UI actions and signals for results (`loaded`, `failed`, `progress`). It runs blocking work through the worker helper and never touches widgets.
4. **UI** (`ui/`): widgets bind to controller signals; Qt item models for lists; four states; accessible names; keyboard shortcuts.
5. **Platform code**, if any, goes behind the `platform/base.py` protocol with a `platform/windows/` implementation.
6. **Tests.** pytest-qt for widget behaviour (find by accessible name), plain pytest for services and controllers with a fake `ApiClient`.
7. **Verify.** `uv run ruff check . && uv run ruff format --check . && uv run mypy . && uv run pytest client/tests` on the Mac. Then give me a short **Windows check** list for the Windows machine: run from source, check the screen at 100%, 150% and 200% scaling, light and dark, keyboard only, plus `uv run pytest -m windows_only client/tests` if Windows-specific code changed.

## Common mistakes to avoid

- Calling `httpx`, hashing, scanning folders, or SQLite from a slot on the GUI thread
- Touching a widget from a worker thread (only emit signals from workers)
- Widgets importing services or the store directly instead of going through a controller
- Populating thousands of `QListWidgetItem`s instead of using a `QAbstractItemModel`
- Fixed pixel sizes and hard-coded colours that break HiDPI, dark mode, and high contrast
- Missing `setAccessibleName` on icon-only buttons; no keyboard path for drag-and-drop actions
- Silent failures: every failed action shows what happened and how to retry
- `os`/`ctypes`/`winreg` calls outside `platform/`
