# Fleet-View Refresh Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the `archon` unified agent view look like Claude Code's fleet view — a spartan-helmet mascot in the header, Claude-style status counts, and a transparent UI that shows the user's terminal (kitty) background through instead of a painted background.

**Architecture:** The real surface is `src/archon/agent_view.py` (`AgentView`), which `archon` opens via `cli.py`'s no-args callback. We move the root `mascot.py` art into the importable package (`src/archon/mascot.py`, root becomes a shim), switch `AgentView` to Textual's `ansi-dark` theme with transparent widget backgrounds and brand-cyan borders, restructure the 1-row `#topbar` into a mascot + wordmark/status band, and render a large spartan on the empty/idle state.

**Tech Stack:** Python 3.14, Textual 8.2.8 (Pilot `run_test()` harness), Rich `Text`, pytest (`.venv/bin/pytest`).

## Global Constraints

- Brand palette (verbatim): accent/plume cyan `#22d3ee`, cream body/text `#f5f5eb`, muted `#9aa1a9`, brand-dark `#0e1014`.
- The mascot renderer only accepts grid characters `X` (body), `P` (plume), `.` (empty); grid height must be even (2 pixel rows per terminal row).
- Do NOT touch the legacy `src/archon/tui/app.py`, `src/archon/tui/styles.tcss`, or `dash.py`.
- Do NOT change the keys returned by `sessions.model.summarize()` — only how they are presented.
- Run tests with `.venv/bin/pytest`. Per repo memory, `pip`/install steps need the sandbox disabled; plain test runs do not.
- Keep `spartan_final.png` in the repo root unchanged.

---

### Task 1: Move mascot into the package + add `spartan_tiny`

**Files:**
- Create: `src/archon/mascot.py` (canonical, moved from root)
- Modify: `mascot.py` (repo root) → re-export shim
- Test: `tests/test_mascot.py`

**Interfaces:**
- Produces: `archon.mascot.brand_mascot_text(name: str = "spartan") -> rich.text.Text`, `archon.mascot.mascot_text(name="owl")`, `archon.mascot.mascot_lines(...)`, and grid dict `GRIDS` — all importable as `from archon import mascot` / `from .mascot import brand_mascot_text`. New grid key `"spartan_tiny"` (4 grid rows = 2 terminal rows).

- [ ] **Step 1: Write the failing test**

Create `tests/test_mascot.py`:

```python
"""Mascot art module: package-importable, with a 2-row header helmet."""

from __future__ import annotations

import archon.mascot as pkg_mascot


def test_spartan_tiny_renders_two_terminal_rows():
    text = pkg_mascot.brand_mascot_text("spartan_tiny")
    lines = text.plain.split("\n")
    assert len(lines) == 2  # 4 grid rows -> 2 half-block rows
    assert any(ch.strip() for ch in lines[0])  # not blank


def test_spartan_still_available():
    text = pkg_mascot.brand_mascot_text("spartan")
    assert text.plain  # large helmet still renders


def test_root_shim_reexports_same_callable():
    import importlib.util
    from pathlib import Path

    root_file = Path(__file__).resolve().parent.parent / "mascot.py"
    spec = importlib.util.spec_from_file_location("_root_mascot", root_file)
    root_mascot = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(root_mascot)
    assert root_mascot.brand_mascot_text is pkg_mascot.brand_mascot_text
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_mascot.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'archon.mascot'`.

- [ ] **Step 3: Create the canonical package module**

Copy the entire current contents of the root `mascot.py` into a new file `src/archon/mascot.py` (unchanged), then add the `spartan_tiny` grid. In `src/archon/mascot.py`, add this grid definition next to the other SPARTAN grids (before the `GRIDS.update({...})` call for the spartan set) and include it in that update:

```python
SPARTAN_TINY = [  # 12 cols x 4 rows -> 2 terminal rows, header size
    "....PPPP....",
    "..XXXXXXXX..",
    ".XXX.XX.XXX.",
    "..XX....XX..",
]
```

Then extend the existing spartan `GRIDS.update`:

```python
GRIDS.update({
    "spartan": SPARTAN,
    "spartan_small": SPARTAN_SMALL,
    "spartan_orbit": SPARTAN_ORBIT,
    "spartan_tiny": SPARTAN_TINY,
})
```

- [ ] **Step 4: Replace the root file with a shim**

Overwrite the repo-root `mascot.py` with:

```python
"""Archon mascot art. Canonical module: archon.mascot.

Kept in the repo root as a brand asset and standalone preview; the real code
lives in the importable package so the TUI can use it.
"""

from archon.mascot import *  # noqa: F401,F403
from archon.mascot import brand_mascot_text, mascot_lines, mascot_text  # noqa: F401
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_mascot.py -v`
Expected: PASS (3 passed).

- [ ] **Step 6: Commit**

```bash
git add src/archon/mascot.py mascot.py tests/test_mascot.py
git commit -m "feat(mascot): move mascot art into package, add spartan_tiny header helmet"
```

---

### Task 2: Transparent theme + brand colors in AgentView

**Files:**
- Modify: `src/archon/agent_view.py` (the `AgentView.CSS` string, `on_mount`, and new module color constants; `_session_line` provider color)
- Test: `tests/test_agent_view.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: module constants `_ACCENT = "#22d3ee"`, `_CREAM = "#f5f5eb"`, `_MUTED = "#9aa1a9"`, `_BRAND_DARK = "#0e1014"` in `agent_view.py`; `AgentView` uses theme `"ansi-dark"`.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_agent_view.py`:

```python
async def test_agent_view_uses_transparent_ansi_theme(isolated_home, conn, tmp_path):
    from archon.sessions import ArchonSessionAdapter, SessionRegistry

    registry = SessionRegistry(adapters=[ArchonSessionAdapter(
        sessions_dir=isolated_home.sessions_dir,
        alive_fn=lambda pid: True,
    )])
    app = AgentView(cwd=tmp_path, poll_interval=1000, conn=conn, registry=registry)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert app.theme == "ansi-dark"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_agent_view.py::test_agent_view_uses_transparent_ansi_theme -v`
Expected: FAIL — `assert 'textual-dark' == 'ansi-dark'` (default theme).

- [ ] **Step 3: Add color constants**

In `src/archon/agent_view.py`, just below the imports (near the top, before `class ProviderPicker`), add:

```python
_ACCENT = "#22d3ee"   # brand cyan
_CREAM = "#f5f5eb"     # brand foreground
_MUTED = "#9aa1a9"     # brand muted
_BRAND_DARK = "#0e1014"  # brand near-black (modal fills only)
```

- [ ] **Step 4: Make backgrounds transparent and borders brand-cyan**

Replace the `CSS` class attribute of `AgentView` (currently agent_view.py:52-112) with:

```python
    CSS = """
    Screen {
        background: transparent;
        color: #f5f5eb;
    }

    #topbar {
        dock: top;
        height: 2;
        padding: 0 1;
        background: transparent;
        border-bottom: solid #22d3ee;
    }

    #mascot {
        width: auto;
        padding: 0 1 0 0;
        content-align: left middle;
    }

    #brand {
        width: 1fr;
        content-align: left middle;
    }

    #banner {
        dock: top;
        height: 1;
        padding: 0 1;
        background: transparent;
        color: #f5c542;
        display: none;
    }

    #banner.visible {
        display: block;
    }

    #sessions {
        width: 100%;
        height: 1fr;
        padding: 0 1;
        background: transparent;
    }

    #sessions > ListItem {
        height: auto;
        background: transparent;
    }

    #sessions > ListItem.--highlight {
        background: #22d3ee 25%;
    }

    #command {
        dock: bottom;
        height: 3;
        border: tall #22d3ee;
        background: transparent;
    }

    ProviderPicker {
        align: center middle;
    }

    ProviderPicker > #provider-picker {
        width: 56;
        height: auto;
        border: thick #22d3ee;
        background: #0e1014;
        padding: 1 2;
    }
    """
```

Note: `#topbar` is already 2 rows high here and references `#mascot`/`#brand`; those child widgets are added in Task 3. This CSS is valid before then because unmatched selectors are inert; the current single `#topbar` Static still renders inside the container height.

- [ ] **Step 5: Set the theme on mount and recolor the provider column**

In `AgentView.on_mount` (agent_view.py:145-148), set the theme as the first line:

```python
    def on_mount(self) -> None:
        self.theme = "ansi-dark"
        self.query_one("#command", Input).focus()
        self.refresh_sessions()
        self.set_interval(self.poll_interval, self.refresh_sessions)
```

In `_session_line` (agent_view.py:341), change the provider column color from `"cyan"` to the brand accent:

```python
        (f"{session.provider.upper():<7}", _ACCENT),
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_agent_view.py -v`
Expected: PASS (all existing agent_view tests plus the new theme test).

- [ ] **Step 7: Commit**

```bash
git add src/archon/agent_view.py tests/test_agent_view.py
git commit -m "feat(agent-view): transparent ansi theme with brand-cyan borders"
```

---

### Task 3: Mascot header band with Claude-style counts

**Files:**
- Modify: `src/archon/agent_view.py` (`compose`, `_render_topbar`, imports)
- Test: `tests/test_agent_view.py`

**Interfaces:**
- Consumes: `archon.mascot.brand_mascot_text` (Task 1); `#mascot`/`#brand` CSS (Task 2); `sessions.model.summarize(sessions) -> dict` with keys `working`, `need_you`, `done`, `failed`.
- Produces: `#topbar` is a `Horizontal` containing `#mascot` (Static) and `#brand` (Static). Status line text contains the substrings `"awaiting input"` and `"completed"`.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_agent_view.py`:

```python
async def test_topbar_has_mascot_and_relabeled_counts(isolated_home, conn, tmp_path):
    import json as _json

    from archon.sessions import ArchonSessionAdapter, SessionRegistry
    from textual.widgets import Static

    (isolated_home.sessions_dir / "s1.json").write_text(_json.dumps({
        "session_id": "s1", "provider": "codex", "pid": 999,
        "cwd": str(tmp_path), "title": "fix auth", "summary": "done",
        "status": "completed", "updated_at": "2026-07-18T00:00:00+00:00",
    }), encoding="utf-8")
    registry = SessionRegistry(adapters=[ArchonSessionAdapter(
        sessions_dir=isolated_home.sessions_dir,
        alive_fn=lambda pid: True,
    )])
    app = AgentView(cwd=tmp_path, poll_interval=1000, conn=conn, registry=registry)
    async with app.run_test() as pilot:
        await pilot.pause()
        mascot = app.query_one("#mascot", Static)
        brand = app.query_one("#brand", Static)
        assert mascot.render().plain.strip()          # helmet art present
        brand_text = brand.render().plain
        assert "awaiting input" in brand_text
        assert "completed" in brand_text
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_agent_view.py::test_topbar_has_mascot_and_relabeled_counts -v`
Expected: FAIL — `NoMatches` querying `#mascot` (topbar is still a single Static).

- [ ] **Step 3: Import Horizontal and the mascot module**

At the top of `src/archon/agent_view.py`, update the containers import and add the mascot import:

```python
from textual.containers import Horizontal, Vertical
```

and, with the other `from .` imports:

```python
from . import mascot
```

- [ ] **Step 4: Restructure the topbar in `compose`**

Replace `yield Static(id="topbar")` (agent_view.py:139) with:

```python
        with Horizontal(id="topbar"):
            yield Static(id="mascot")
            yield Static(id="brand")
```

- [ ] **Step 5: Set the mascot once on mount**

In `on_mount`, after setting the theme, populate the static mascot:

```python
    def on_mount(self) -> None:
        self.theme = "ansi-dark"
        self.query_one("#mascot", Static).update(mascot.brand_mascot_text("spartan_tiny"))
        self.query_one("#command", Input).focus()
        self.refresh_sessions()
        self.set_interval(self.poll_interval, self.refresh_sessions)
```

- [ ] **Step 6: Rewrite `_render_topbar` to update `#brand` with relabeled counts**

Replace `_render_topbar` (agent_view.py:306-321) with:

```python
    def _render_topbar(self) -> None:
        counts = summarize(self._sessions)
        text = Text.assemble(
            ("ARCHON", f"bold {_ACCENT}"),
            ("   ", ""),
            (f"{counts['need_you']} awaiting input", "bold yellow"),
            ("  ·  ", _MUTED),
            (f"{counts['working']} working", "green"),
            ("  ·  ", _MUTED),
            (f"{counts['done']} completed", "green"),
            *(((f"  ·  ", _MUTED), (f"{counts['failed']} failed", "red")) if counts["failed"] else ()),
            ("\n", ""),
            (str(self.cwd), _MUTED),
        )
        self.query_one("#brand", Static).update(text)
```

Note: the `*((...) if ... else ())` splat conditionally inserts the "failed" span pair only when `counts["failed"]` is non-zero; each element is a `(text, style)` tuple accepted by `Text.assemble`.

- [ ] **Step 7: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_agent_view.py -v`
Expected: PASS (all agent_view tests, including the new topbar test).

- [ ] **Step 8: Commit**

```bash
git add src/archon/agent_view.py tests/test_agent_view.py
git commit -m "feat(agent-view): spartan mascot header with Claude-style counts"
```

---

### Task 4: Large spartan on the idle / empty state

**Files:**
- Modify: `src/archon/agent_view.py` (`refresh_sessions` empty branch)
- Test: `tests/test_agent_view.py`

**Interfaces:**
- Consumes: `archon.mascot.brand_mascot_text` (Task 1).
- Produces: when `self._sessions` is empty, `#sessions` holds exactly one `ListItem` containing the large helmet, the `ARCHON` wordmark, and a hint line.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_agent_view.py`:

```python
async def test_idle_state_shows_large_spartan(isolated_home, conn, tmp_path):
    from archon.sessions import ArchonSessionAdapter, SessionRegistry
    from textual.widgets import ListView

    registry = SessionRegistry(adapters=[ArchonSessionAdapter(
        sessions_dir=isolated_home.sessions_dir,
        alive_fn=lambda pid: True,
    )])
    app = AgentView(cwd=tmp_path, poll_interval=1000, conn=conn, registry=registry)
    async with app.run_test() as pilot:
        await pilot.pause()
        view = app.query_one("#sessions", ListView)
        assert len(view.children) == 1          # single idle item
        rendered = view.children[0].query_one("Static").render().plain
        assert "ARCHON" in rendered
        assert "describe a task" in rendered.lower()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_agent_view.py::test_idle_state_shows_large_spartan -v`
Expected: FAIL — rendered idle text is "No sessions yet. Type a task below." with no "ARCHON" wordmark.

- [ ] **Step 3: Render the large spartan in the empty branch**

In `refresh_sessions` (agent_view.py:175-178), replace the empty-state block:

```python
        if not self._sessions:
            view.append(ListItem(Static(Text("No sessions yet. Type a task below.", style="dim"))))
            self._selected_session_id = None
            return
```

with:

```python
        if not self._sessions:
            idle = mascot.brand_mascot_text("spartan")
            idle.append("\n\n")
            idle.append("ARCHON", style=f"bold {_ACCENT}")
            idle.append("\n")
            idle.append(
                "No sessions yet — describe a task below and end with "
                "--claude, --codex, or --copilot.",
                style=_MUTED,
            )
            view.append(ListItem(Static(idle)))
            self._selected_session_id = None
            return
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_agent_view.py -v`
Expected: PASS (all agent_view tests, including the new idle test).

- [ ] **Step 5: Commit**

```bash
git add src/archon/agent_view.py tests/test_agent_view.py
git commit -m "feat(agent-view): large spartan helmet on the idle empty state"
```

---

### Task 5: Full suite + manual verification

**Files:** none (verification only)

- [ ] **Step 1: Run the full test suite**

Run: `.venv/bin/pytest -q`
Expected: PASS (no regressions; `test_tui_app.py` remains skipped as before).

- [ ] **Step 2: Manual visual check**

In a kitty terminal with a transparent/wallpapered background, run `archon` from the repo:

Run: `.venv/bin/archon` (or `.venv/bin/python -m archon`)
Expected: the terminal wallpaper shows through the whole UI; the header shows the cyan/cream spartan helmet beside `ARCHON  N awaiting input · N working · N completed  <cwd>`; with no sessions, the large spartan + wordmark appears in the body. Press `q` to quit.

- [ ] **Step 3: Run the verify skill (optional but recommended)**

If the visual check reveals layout issues (e.g. mascot clipped by the 2-row `#topbar`), adjust `#topbar { height: }` and `#mascot` padding in the CSS, re-run `.venv/bin/pytest tests/test_agent_view.py -v`, and commit the tweak.

---

## Self-Review

**Spec coverage:**
- Terminal-background pass-through (theme + CSS) → Task 2. ✓
- Brand color constants / recolor → Task 2. ✓
- Mascot header band + relabeled Claude-style counts → Task 3. ✓
- `spartan_tiny` grid → Task 1. ✓
- Large spartan idle state → Task 4. ✓
- mascot.py moved to package + root shim → Task 1. ✓
- ProviderPicker solid fill (readability exception) → Task 2 CSS. ✓
- Tests (mascot, theme, topbar, idle) → Tasks 1–4; full suite → Task 5. ✓
- Non-goals (no legacy tui/dash changes, summarize keys unchanged) → respected; `_render_topbar` reuses `summarize` keys `need_you`/`working`/`done`/`failed`. ✓

**Placeholder scan:** No TBD/TODO; every code step shows full code. ✓

**Type consistency:** `brand_mascot_text` used identically in Tasks 1/3/4; `summarize()` keys (`need_you`, `working`, `done`, `failed`) match `sessions/model.py`; color constants `_ACCENT`/`_CREAM`/`_MUTED`/`_BRAND_DARK` defined in Task 2 and used in Tasks 3/4. `#mascot`/`#brand` selectors defined in Task 2 CSS, created in Task 3 compose. ✓
