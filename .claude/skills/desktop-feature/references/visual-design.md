# Desktop visual design (PySide6)

Loaded by the `desktop-feature` skill and the `ui-reviewer` agent for any visual work.
Read `ui.md` first: its rules on states, accessibility, and HiDPI still apply and win on conflict.

**Goal:** a clean, modern, branded Windows 11-style app. Simple, not decorative. The test is
"would this look at home next to OneDrive or the Windows 11 Settings app, in State Life colours?"
Restraint beats ornament: one accent colour, lots of whitespace, few borders, no gradients,
no shadows-on-everything, no animation beyond what Qt does natively.

---

## 1. Architecture of the look

| Piece          | Location                                   | Rule                                                                                                                                                                                    |
| -------------- | ------------------------------------------ | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Design tokens  | `ui/theme.py`                              | The only place colours, spacing, radii, and font sizes are defined.                                                                                                                     |
| App stylesheet | `ui/theme.py` → `build_stylesheet(tokens)` | One QSS string generated from tokens and applied once with `app.setStyleSheet(...)`. No per-widget `setStyleSheet` except via object names/properties the global sheet already targets. |
| Base style     | `app.py`                                   | `QApplication.setStyle("Fusion")`.                                                                                                                                                      |
| Icons          | `resources/icons/`                         | One SVG family, tinted at runtime from tokens.                                                                                                                                          |
| Brand assets   | `resources/brand/`                         | Logo files, swapped without code changes (§7).                                                                                                                                          |

**Why Fusion:** the native `windowsvista`/`windows11` and `macos` styles ignore large parts of QSS,
so the same stylesheet renders differently on the Mac and on Windows. Fusion honours QSS fully and
renders identically on both, which matters because development happens on macOS.
The Windows feel comes from the tokens (Segoe UI Variable, radii, spacing), not the native style.

**Theme switching:** build tokens for the current scheme at startup and rebuild the stylesheet when
`QGuiApplication.styleHints().colorSchemeChanged` fires (light ↔ dark). Rebuild cached tinted
icons at the same time.

**High contrast:** when Windows high-contrast mode is on, do **not** apply the custom stylesheet or
brand palette. Use the system palette with the stock style so the user's contrast theme wins.
Detection goes through the platform layer (`platform.is_high_contrast()`; Windows:
`SystemParametersInfoW(SPI_GETHIGHCONTRAST)`; macOS dev: "Increase contrast" or `False`).
This needs a `windows_only` test and a Windows check item.

---

## 2. Colour tokens

### 2.1 Brand palette (source: https://statehealth.com.pk/)

> **PLACEHOLDER VALUES.** The hex codes below are stand-ins until the real values are taken from
> statehealth.com.pk. Replace only this table and `_BRAND` in `ui/theme.py`; everything else is derived.
> Do not "improve" these values on your own; ask me.

| Token                   | Value                     | Use                                                            |
| ----------------------- | ------------------------- | -------------------------------------------------------------- |
| `brand_primary`         | `#0B6E4F` _(placeholder)_ | Primary buttons, selection, active nav item, links, focus ring |
| `brand_primary_hover`   | `#095C42` _(placeholder)_ | Hover on primary                                               |
| `brand_primary_pressed` | `#074A35` _(placeholder)_ | Pressed on primary                                             |
| `brand_secondary`       | `#1F4E79` _(placeholder)_ | Rare accents only: header strip in sign-in, info badges        |
| `brand_on_primary`      | `#FFFFFF`                 | Text/icons on primary fills                                    |

Rules for brand colour:

- The primary colour covers **at most ~10% of any screen**: primary button, selection, active nav, focus.
  Never as a large background (except the sign-in header strip).
- Text on `brand_primary` must meet WCAG AA (4.5:1). If a replaced brand colour fails, darken it for
  fills and keep the original for the logo only. Add a unit test that checks the contrast ratio.
- In dark mode, use a lighter tint of the primary (derive it in `theme.py`, e.g. mix 30% with white)
  so it reads on dark surfaces; keep the same hue.

### 2.2 Neutrals and semantics

| Token            | Light                | Dark                      | Use                                     |
| ---------------- | -------------------- | ------------------------- | --------------------------------------- |
| `bg_window`      | `#F7F8FA`            | `#1C1D1F`                 | Main window background                  |
| `bg_surface`     | `#FFFFFF`            | `#262729`                 | Cards, content area, dialogs, inputs    |
| `bg_sidebar`     | `#F0F2F5`            | `#202123`                 | Navigation sidebar                      |
| `bg_hover`       | `#0000000A`          | `#FFFFFF0F`               | Row/button hover (alpha overlay)        |
| `bg_selected`    | primary at 12% alpha | primary-tint at 20% alpha | Selected rows, active nav               |
| `border`         | `#E1E4E8`            | `#3A3B3E`                 | Hairlines, input borders, card outlines |
| `text_primary`   | `#1B1C1E`            | `#F2F3F5`                 | Body text                               |
| `text_secondary` | `#5F6368`            | `#A8ABB0`                 | Labels, metadata (size, date), hints    |
| `text_disabled`  | `#A0A4A8`            | `#6B6E72`                 | Disabled                                |
| `success`        | `#107C10`            | `#6CCB5F`                 | Synced / up to date                     |
| `warning`        | `#9D5D00`            | `#FCE100`                 | Paused, waiting, needs attention        |
| `danger`         | `#C42B1C`            | `#FF99A4`                 | Errors, can't sync, destructive buttons |
| `info`           | = `brand_secondary`  | tint                      | Informational banners                   |

Semantic colours always come with an icon and text (ui.md §5). Never colour alone.

---

## 3. Spacing, shape, type

**Spacing scale (logical px):** `xs 4 · sm 8 · md 12 · lg 16 · xl 24 · xxl 32`. Use only these.

- Dialog/content margins: `xl` (24). Between form rows: `md` (12). Between sections: `xxl` (32).
- Button padding: `sm` vertical, `lg` horizontal. Minimum button height 32, input height 32.
- Table/list row height: 36 (files), 40 (navigation). Comfortable, not cramped.

**Shape:** radius 4 for inputs/buttons, 8 for cards and dialogs' inner panels. Borders are 1 px
hairlines in `border`. No drop shadows in QSS (Qt can't do them cleanly); separate with
surface colour and whitespace instead.

**Typography:** `Segoe UI Variable` → `Segoe UI` → system default (on macOS dev this falls back to
the system font; that's expected). Use the app font's point size as `body` and scale from it, so
the user's Windows text-size setting is respected:

| Role          | Size               | Weight | Use                                   |
| ------------- | ------------------ | ------ | ------------------------------------- |
| `title`       | body × 1.6         | 600    | Window/page title ("My files")        |
| `subtitle`    | body × 1.25        | 600    | Section and dialog headings           |
| `body`        | system (≈ 9–10 pt) | 400    | Default                               |
| `body_strong` | body               | 600    | File names in emphasis, button labels |
| `caption`     | body × 0.9         | 400    | Metadata, hints, status bar           |

Set roles with a dynamic property, not inline fonts: `label.setProperty("textRole", "title")`,
and the stylesheet targets `QLabel[textRole="title"]`. Never set point sizes in widget code.

---

## 4. Component styling (what the stylesheet covers)

Keep the QSS short (aim for under ~250 lines). Cover only these:

- **Buttons:** default = `bg_surface` + `border`; `QPushButton[variant="primary"]` = brand fill;
  `[variant="danger"]` = danger fill for "Delete permanently"; `[variant="subtle"]` = no border,
  hover overlay only (toolbar/icon buttons). One primary button per view or dialog.
- **Inputs** (`QLineEdit`, `QComboBox`, `QSpinBox`): surface fill, hairline border, 2 px primary
  border on focus. Error state via `[state="error"]` → danger border + caption message below.
- **Sidebar nav** (`QListView#navList`): no frame, `bg_sidebar`, rows 40 px, icon + label, active
  item = `bg_selected` + 3 px primary bar on the left.
- **File table** (`QTableView#fileTable`): no grid lines, no alternating colours, hairline header
  bottom border, header text in `caption`/`text_secondary`, hover overlay, selection `bg_selected`
  with `text_primary` (not white-on-primary).
- **Cards** (`QFrame[card="true"]`): surface, hairline border, radius 8, `lg` padding.
- **Banners** (`QFrame[banner="info|warning|danger"]`): tinted background (semantic at ~10%),
  icon + message + optional action button. Used for "Sync paused", "Can't sync 3 items", update available.
- **Progress bars:** thin (6 px), radius 3, primary chunk.
- **Scrollbars:** slim (8 px), rounded handle in `border` colour, no arrows.
- **Menus and tooltips:** surface, hairline border, radius 4, 6/12 px item padding.
- **Focus:** keep a visible 2 px primary focus outline on every focusable control. Never
  `outline: none` without a replacement.

---

## 5. Icons

- One family only: **Fluent UI System Icons** (Microsoft, MIT licence), "Regular" weight, 20 px
  grid. Copy only the SVGs used into `resources/icons/` and add the licence file next to them.
  This is an asset, not a Python dependency, but still tell me before adding the set.
- Tint at runtime from tokens: `theme.icon("folder", role="secondary")` renders the SVG with
  `QSvgRenderer` and recolours it (`CompositionMode_SourceIn`), cached per (name, colour, size, DPR).
  No pre-coloured duplicates.
- Sizes: 16 in tables and menus, 20 in sidebar and toolbars, 48 in empty/error states.
- File-type icons: a small set (folder, document, spreadsheet, PDF, image, archive, generic),
  mapped by extension in one place.
- Tray icons are the exception: separate pre-rendered multi-size icons per state (ui.md §1/§7),
  because the tray isn't styled by QSS.

---

## 6. Screen layouts

Sketches show structure, not pixels. All use layouts and the spacing scale.

**Main window** (min 900 × 600, logical):

```
┌───────────────────────────────────────────────────────────────────┐
│ [logo] DropShare                                  [⟳] [⏸] [⚙]     │ ← header, bg_surface, hairline below
├──────────────┬────────────────────────────────────────────────────┤
│ ▌My files    │ My files › Projects › 2026        [Upload] [New ▾] │ ← breadcrumb (title) + actions
│  Shared      │ ┌ banner: Sync paused · Resume ────────────────────┐ │ ← only when relevant
│  Trash       │ │Name                    Modified        Size  ●   │ │ ← table header, caption text
│  Activity    │ │📁 Claims reports       Today 10:42     —     ✓   │ │
│  Can't sync 3│ │📄 Q3 summary.pdf        Yesterday       2.1 MB ⟳  │ │ ← status icon + tooltip text
│              │ └──────────────────────────────────────────────────┘ │
│ ──────────── │                                                     │
│ 4.2 / 10 GB  │                                                     │ ← quota: caption + thin bar
├──────────────┴────────────────────────────────────────────────────┤
│ ✓ Up to date · Last synced 10:45                                  │ ← status bar, caption
└───────────────────────────────────────────────────────────────────┘
```

Sidebar ~220 px (logical, resizable via splitter). Count badges ("3") use `caption` in a pill,
danger-tinted for Can't sync.

**Sign-in dialog** (fixed width ~400, height by content):

```
┌──────────────────────────────┐
│▓▓▓▓▓▓ brand_primary strip ▓▓▓│ ← 4 px strip only, not a big header
│          [ logo ]            │ ← 64 px logo placeholder
│     Sign in to DropShare     │ ← subtitle
│  Use your State Life account │ ← caption, secondary
│                              │
│  Username or email           │
│  [________________________]  │
│  Password                    │
│  [____________________][👁]  │
│  ⚠ Incorrect username or…    │ ← inline error, danger, only when present
│                              │
│  [        Sign in        ]   │ ← primary, full width, default button
│  Server: files.slic… Change  │ ← caption + link-style subtle button
└──────────────────────────────┘
```

Change-password and sync-folder chooser use the same dialog frame: logo, subtitle, one short
explanation line, the form, one primary button.

**Empty / error / loading states** (inside `StatefulView`, centred, max width ~360):

```
            [48 px icon, text_secondary]
             This folder is empty           ← subtitle
   Drag files here or choose Upload.        ← body, secondary
                 [ Upload ]                 ← primary (empty) / "Try again" (error)
```

Loading: indeterminate thin progress bar + caption "Loading…" (after the 300 ms delay in ui.md §3).

**Settings:** left list of sections (General, Account, Sync, Notifications, About) and a scrolling
right pane of cards; each card = subtitle + rows of label/description on the left, control on the right.

**Tray menu:** native `QMenu` styled only by the global menu rules. First line is a disabled
status item ("✓ Up to date"), then the FR-CLIENT-01 actions.

---

## 7. Logo placeholder

- Files: `resources/brand/logo.svg` (full logo) and `resources/brand/logo-mark.svg` (square mark
  for small sizes and the window icon). Both start as the provided placeholders.
- Code never references these paths directly: use `theme.logo(size)` / `theme.logo_mark(size)`.
  Replacing the SVG files must require no code change.
- Placement: sign-in and related dialogs (64 px), main header (24 px, mark + "DropShare" wordmark
  as text), About section. Not in the tray (tray uses state icons).
- The placeholder must look obviously temporary (it says "LOGO"); don't design a substitute logo.

---

## 8. Restraint checklist (apply before calling a screen done)

- [ ] Only tokens used; no hex, px font sizes, or ad-hoc margins in widget code
- [ ] One primary button; brand colour on ≤ ~10% of the screen
- [ ] Spacing from the scale; content aligned to a consistent left edge
- [ ] Secondary information in `caption`/`text_secondary`, not more boxes
- [ ] No nested frames/borders around borders; separation by whitespace first
- [ ] Looks right in light, dark, and high-contrast (high contrast = system look)
- [ ] Text not clipped at 200% and with Windows text size at 150%
- [ ] Icons from the one family, correct size for context

## 9. Windows check (add to the task's list for visual changes)

- Screen at 100%, 150%, 200% scaling, light and dark: spacing even, nothing clipped, icons crisp.
- Turn on a contrast theme (Settings → Accessibility → Contrast themes): app falls back to the
  system look, everything readable.
- Font renders as Segoe UI Variable; compare against the Windows 11 Settings app for weight and size.
