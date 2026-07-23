# Archon Fleet-View Refresh — Design

**Date:** 2026-07-22
**Status:** Approved (design), pending implementation plan

## Goal

Make the interactive `archon` TUI look like Claude Code's fleet/multi-session
view: a spartan-helmet mascot in the header, Claude-style status counts, and a
transparent UI that lets the user's terminal (kitty) background show through
instead of painting a predetermined background. Brand accents come from the
Archon brand sheet (cyan `#22D3EE`, cream `#F5F5EB`).

Scope is the **unified agent view** — the surface `archon` (no args) actually
opens — plus the mascot module.

## Current state

- The `archon` no-args entrypoint (`cli.py:59-63`, `@app.callback`) calls
  `agent_view.run`. **`src/archon/agent_view.py` (`AgentView`) is the real
  surface**, not the legacy `tui/app.py` cockpit (whose smoke tests are skipped
  with "archon now opens the unified agent view").
- `AgentView` has an **inline `CSS` string** (not a `.tcss` file). It paints
  `Screen { background: $background }`, `#topbar { background: $panel }`,
  `#command { background: $panel }`, and a `$accent`-tinted `#sessions`
  highlight — the "predetermined background" to remove.
- `AgentView._render_topbar` (agent_view.py:306-321) **already renders a
  Claude-style status line**: `ARCHON  N running  N need assistance  N
  completed  N failed  <cwd>`, using `summarize(sessions)` →
  `{"working","need_you","failed","done","idle","disconnected"}` (ints). The
  `#topbar` is a single 1-row `Static`. No mascot.
- Empty state: when there are no sessions, `refresh_sessions` appends one
  `ListItem` with `Static(Text("No sessions yet. Type a task below.", ...))` —
  this is the idle moment for a large mascot.
- `mascot.py` (repo root) — half-block pixel-art renderer with spartan grids and
  `brand_mascot_text()` (cyan plume `#22D3EE`, cream body `#F5F5EB`). Lives
  outside `src/`, so it is **not importable** from the `archon` package.
- `spartan_final.png` (repo root) — brand reference image. Keep as-is.
- Textual is 8.2.8, which ships `ansi-dark` themes with `ansi=True` (renders the
  Screen background as the terminal default → terminal wallpaper shows through).
- Active tests live in `tests/test_agent_view.py` (Pilot `run_test()` harness).
  The legacy `tests/test_tui_app.py` is skipped.

## Design

### 1. Terminal-background pass-through (theme + CSS)

- In `AgentView.on_mount`, set `self.theme = "ansi-dark"`. With `ansi=True`,
  unset/default cells emit the terminal's default background, so kitty
  transparency shows through.
- In the inline `CSS`, set `background: transparent` on `Screen`, `#topbar`,
  `#command`, and `#sessions`. Replace panel-fill separation with thin borders in
  brand cyan `#22D3EE` (e.g. `#topbar { border-bottom: solid #22d3ee }`,
  `#command { border: tall #22d3ee }`). The `#sessions` highlight uses a cyan
  tint (`#22d3ee 25%`) which composites fine over the transparent base.
- Use explicit truecolor brand values for foreground so they stay exact
  regardless of the ANSI base:
  - accent: `#22D3EE`, foreground text: `#F5F5EB`, muted: `#9AA1A9`.
- Add module constants `_ACCENT = "#22d3ee"`, `_CREAM = "#f5f5eb"`,
  `_MUTED = "#9aa1a9"` and use them where the code currently hardcodes `"cyan"`
  (topbar wordmark, `_session_line` provider column).
- **Readability exception:** the `ProviderPicker` modal keeps a solid fill
  (`#0E1014` from the brand sheet) so its text stays readable over a busy
  wallpaper; bordered in cyan. This exception does not apply to the main
  surfaces.

### 2. Mascot + header (fleet-view look)

Grow `#topbar` from 1 row to a 2-row band that mirrors Claude Code:

```
▟█▙  ARCHON   0 awaiting input · 1 working · 3 completed        <cwd>
▜█▛
```

- **Compose change:** replace the single `#topbar` Static with a `Horizontal`
  `#topbar` containing a `#mascot` Static (2 lines) and a `#brand` Static (the
  wordmark + status line).
- **Mascot:** add a compact `spartan_tiny` grid to the mascot module — 4 grid
  rows = 2 terminal rows (Claude Code's small-mascot size), cyan plume + cream
  body, rendered via `brand_mascot_text("spartan_tiny")`. Set once on mount into
  `#mascot`.
- **Status line:** keep the existing counts from `summarize()` but relabel to the
  Claude phrasing and brand palette:
  `ARCHON   {need_you} awaiting input · {working} working · {done} completed`,
  then `· {failed} failed` appended only when `failed > 0`, then the `<cwd>` dim.
  `_render_topbar` updates `#brand` each poll; `#mascot` is static.

### 3. Big spartan on the idle / empty state

When `self._sessions` is empty, replace the plain "No sessions yet" line with a
single `ListItem` whose `Static` stacks the large `brand_mascot_text("spartan")`
helmet, the `ARCHON` wordmark, and the hint "No sessions yet — describe a task
below and end with --claude, --codex, or --copilot." Keeping it one `ListItem`
preserves the existing child-count semantics the tests rely on.

### 4. mascot.py home

- Move the canonical mascot code to `src/archon/mascot.py` so the package can
  `from . import mascot` / `from .mascot import brand_mascot_text`.
- Root `mascot.py` becomes a thin re-export shim so the file still exists in root
  and runs standalone against the installed (editable) package:

  ```python
  """Archon mascot art. Canonical module: archon.mascot."""
  from archon.mascot import *  # noqa: F401,F403
  ```

- `spartan_final.png` stays in root untouched.

## Testing

- **mascot** (`tests/test_mascot.py`, new): `brand_mascot_text("spartan_tiny")`
  renders exactly 2 lines of half-block `Text` without error; the root shim
  exposes the same `brand_mascot_text` object as `archon.mascot`
  (identity check).
- **agent_view** (`tests/test_agent_view.py`): existing tests must still pass.
  Add a test that, after mount with sessions present, `#topbar` contains a
  `#mascot` Static whose render is non-empty and a `#brand` Static whose text
  contains "awaiting input" and "completed"; and that `app.theme == "ansi-dark"`.
  Add a test that with zero sessions the `#sessions` ListView has exactly one
  child (the idle mascot item).
- **Manual:** run `archon` in a kitty terminal with a transparent/wallpapered
  background; confirm the wallpaper shows through and the spartan header + idle
  mascot render correctly.

## Non-goals

- No changes to the legacy `tui/app.py` cockpit, `styles.tcss`, or `dash.py`.
- No changes to `summarize()`'s returned keys (only its presentation).
- No mascot animation; static art only.
- No change to `spartan_final.png` or the SVG brand marks.

## Files touched

- `src/archon/mascot.py` — new canonical module (moved from root).
- `mascot.py` (root) — becomes re-export shim; add `spartan_tiny` grid to the
  canonical module.
- `src/archon/agent_view.py` — set ansi theme; brand-color constants; restructure
  `#topbar` compose into mascot + brand; relabel status line; big-spartan idle
  state; solid `ProviderPicker` fill.
- `tests/test_mascot.py` (new), `tests/test_agent_view.py` (added cases).
