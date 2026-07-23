# Archon Fleet-View Refresh — Design

**Date:** 2026-07-22
**Status:** Approved (design), pending implementation plan

## Goal

Make the interactive `archon` TUI look like Claude Code's fleet/multi-session
view: a spartan-helmet mascot in the header, Claude-style status counts, and a
transparent UI that lets the user's terminal (kitty) background show through
instead of painting a predetermined background. Brand accents come from the
Archon brand sheet (cyan `#22D3EE`, cream `#F5F5EB`).

Scope is the interactive Textual cockpit (`archon` with no args) plus the mascot
module. The legacy Rich snapshot tables (`archon status` / `archon up`) are left
unchanged to keep this focused.

## Current state

- `src/archon/tui/app.py` — Textual `App`. Header (`#topbar`) is a single line of
  text `ARCHON · repo · budget`, styled with `_ACCENT = "cyan"` (an ANSI name).
  The spartan mascot in `mascot.py` is never wired in. `WelcomeScreen` shows a
  text-only resume summary.
- `src/archon/tui/styles.tcss` — paints backgrounds: `Screen { background:
  $background }`, plus `$panel`/`$surface` fills on `#topbar`, `#detail`,
  `#attention`, `#command`, and the modals. This is the "predetermined
  background" the user wants gone.
- `mascot.py` (repo root) — half-block pixel-art renderer with owl, helmet, and
  spartan grids, plus `brand_mascot_text()` (cyan plume `#22D3EE`, cream body
  `#F5F5EB`). Not importable from the `archon` package (lives outside `src/`).
- `spartan_final.png` (repo root) — brand reference image. Keep as-is.
- Textual version is 8.2.8, which ships `ansi-dark` / `ansi-light` themes with
  `ansi=True` (renders the Screen background as the terminal default → terminal
  wallpaper shows through).

## Design

### 1. Terminal-background pass-through (theme + styles)

- On mount, set `self.theme = "ansi-dark"`. With `ansi=True`, unset/default cells
  emit the terminal's default background, so kitty transparency shows through.
- In `styles.tcss`, set `background: transparent` on `Screen`, `#topbar`,
  `#banner` (keep its error tint only via border/text), `#attention`, `#jobs`,
  `#detail`, and `#command`. Replace panel-fill separation with thin borders in
  brand cyan `#22D3EE` (e.g. `border-right`/`border-bottom`).
- Use explicit truecolor brand values for foreground elements so they stay exact
  regardless of the ANSI base:
  - accent / primary: `#22D3EE`
  - foreground text: `#F5F5EB`
  - muted / secondary: `#9AA1A9`
- Change the Python constant `_ACCENT = "cyan"` → `_ACCENT = "#22d3ee"`.
- **Readability exception:** modal dialogs (`PlanModal`, `AnswerModal`,
  `WelcomeScreen`) keep a solid fill (`#0E1014` from the brand sheet, or a near-
  opaque panel) so their text is readable over a busy wallpaper. They are
  bordered in cyan. This is intentional and does not apply to the main surfaces.

### 2. Mascot + header (fleet-view look)

Rebuild the header into a compact band (~3 rows) that mirrors Claude Code:

```
▟█▙  ARCHON   agent command center · <repo>
▜█▛  1 awaiting input · 0 working · 3 completed          <budget> [<action>]
```

- **Mascot:** add a compact `spartan_tiny` grid to `mascot.py` — 4 grid rows = 2
  terminal rows (matching Claude Code's small mascot), cyan plume + cream body,
  rendered with `brand_mascot_text("spartan_tiny")`. Placed at the header's left.
- **Wordmark:** `ARCHON` in bold `#22D3EE`, with a dim subtitle
  `agent command center · <repo>` (repo = `ctx.name` or `no-repo`).
- **Status counts line:** `N awaiting input · N working · N completed`, computed
  from the snapshot:
  - `awaiting` = number of attention rows that need the user (plan approvals +
    permission requests) — i.e. `len(snap.attention)`.
  - `working` = tasks whose status is in `models._HEALTH_WORKING`
    (`{"running", "starting"}`) across all jobs.
  - `completed` = tasks whose status is in `models._HEALTH_DONE`
    (`{"done", "ready"}`) across all jobs. The canonical terminal status string
    is `"done"` (there is no `"completed"` status in the model).
  Budget summary (`snap.header_budget`) and policy action (`snap.budget_action`,
  colored) are right-aligned on the same line.
- A small snapshot helper computes the three counts so both the header and the
  existing `_maybe_welcome` logic can share it. Implemented in
  `src/archon/tui/data.py` (e.g. `Snapshot.status_counts()` returning
  `(awaiting, working, completed)`), reusing the running-count logic already in
  `_maybe_welcome`.

**Layout mechanism:** `#topbar` becomes a `Horizontal` container holding a
`#mascot` Static (2 lines) and a `#brand` Static (2 lines: wordmark line +
status line). `_render_topbar` updates `#brand` each poll with current counts;
`#mascot` is static and set once on mount. Header height grows from 1 to 3.

### 3. Big spartan on the welcome / idle screen

`WelcomeScreen` gains the large `SPARTAN` helmet (`brand_mascot_text("spartan")`)
centered at the top with the `ARCHON` wordmark beneath/beside it, above the
existing "resume prior work" summary. Dialog keeps a solid fill (see readability
exception) so the art and text read cleanly.

### 4. mascot.py home

- Move the canonical mascot code to `src/archon/mascot.py` so the package can
  `from .. import mascot` / `from ..mascot import brand_mascot_text`.
- Root `mascot.py` becomes a thin re-export shim so the file still exists in root
  and runs standalone against the installed (editable) package:

  ```python
  """Archon mascot art. Canonical module: archon.mascot."""
  from archon.mascot import *  # noqa: F401,F403
  ```

- `spartan_final.png` stays in root untouched.

## Testing

- `mascot.py`: unit test that `brand_mascot_text("spartan_tiny")` renders 2 lines
  of half-block Text without error, and that the root shim re-exports the same
  callables as `archon.mascot` (identity check).
- `data.py`: unit test for `Snapshot.status_counts()` on a fixture snapshot with
  a known mix of awaiting/working/completed → exact tuple.
- `app.py` (`test_tui_app.py`): existing tests must still pass; add/adjust a test
  that the header (`#topbar`) contains the mascot Static and a status-count line,
  and that `self.theme == "ansi-dark"` after mount.
- Manual verification: run `archon` in a kitty terminal with a transparent/
  wallpapered background and confirm the wallpaper shows through and the spartan
  header + welcome render correctly.

## Non-goals

- No changes to the legacy Rich tables (`archon status`, `archon up`,
  `archon providers`) or `dash.py`.
- No new mascot animation; static art only.
- No change to `spartan_final.png` or the SVG brand marks.

## Files touched

- `src/archon/mascot.py` — new canonical module (moved from root).
- `mascot.py` (root) — becomes re-export shim; gains nothing else.
- `src/archon/tui/app.py` — set ansi theme, rebuild header compose + render,
  `_ACCENT`, welcome-screen big spartan.
- `src/archon/tui/styles.tcss` — transparent backgrounds, cyan borders, header
  layout, modal fill exception.
- `src/archon/tui/data.py` — `Snapshot.status_counts()` helper.
- `tests/` — `test_tui_app.py`, `test_tui_data.py`, and a mascot test.
