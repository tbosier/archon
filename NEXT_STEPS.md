# Archon — Next Steps

A working plan to pick up tomorrow. Ordered by value. Each item has a goal, the
"why", concrete work, and an acceptance check.

## Where things stand (end of today)

- **Done:** full MVP + task queue / dependency DAG / scheduler / reviewer-tester
  handoff / per-phase model tiering (Opus plan → Sonnet execute; gpt-5.5 high→med)
  / budget + rate-limit gating / shared **command center across repos** with a
  Repo column and traffic-light health cues (● working / ● needs help / ● problem
  / ✓ done / ○ waiting).
- **Fixed today:**
  - Prompt now actually submits — added a TUI boot delay before paste + a beat
    before Enter (`ARCHON_PANE_BOOT_DELAY`=3.5s, `ARCHON_PANE_ENTER_DELAY`=0.8s in
    `dispatcher.py`).
  - Dry-run no longer pollutes the real DB (`cli._open(dry)` uses an in-memory DB).
  - `archon feature` auto-creates the `archon` Zellij session (no prior `up` needed).
  - Dashboard footer no longer implies a manual `r` refresh — it auto-refreshes 2s.
- **Health:** `179 passing`, editable install (src changes are live, no reinstall).
- **Sandbox test project:** scaffold script at
  `/tmp/claude-.../scratchpad/setup_testarchsim.sh` builds `~/projects/testArchSim`
  (chip-factory sim: simulation / backend / frontend / visuals, one feature branch
  each). Not yet committed anywhere permanent — re-copy into the repo if we want it.

---

## 1. Wire the Claude hooks → a true live tether  ⭐ do first

**Goal:** the dashboard updates itself as agents work — cost/context tick, runs
flip to `blocked` (red/yellow) on a permission prompt, and the plan→execute→
review→test chain **auto-advances** without manual `archon complete`.

**Why:** right now the dashboard shows "running" forever because nothing updates
the DB while an agent works. This is the single biggest gap between what we have
and the live "command center" feel (and what agent-deck/cc-dash get from their
tmux preview + telemetry).

**Work:**
- Install `examples/claude-settings.json` into the launched worktree (or point
  `CLAUDE_*`/settings at it) so `archon statusline` + `archon hook` actually fire.
  Verify the paths/flags match the installed `claude` (2.1.205).
- In `hooks.handle_hook`, on `Stop`/`SessionEnd`: mark the run `done`, then call
  `dispatcher.complete_task(...)` so the handoff + next scheduler tick happen
  automatically. (Today `complete` is manual.)
- On `PermissionRequest`: already flips to `blocked` — confirm it colors/focuses
  the pane + notifies.
- Decide: does the statusline write telemetry for the *right* run? Check the
  `ARCHON_TASK_RUN_ID` env is actually present in the pane (it is injected via
  `build_pane_command`) and that `statusline.infer_task_run_id` picks it up.

**Acceptance:** launch one real Claude feature; watch `archon status --watch` show
cost/context tick, then auto-advance plan→execute→review→test with no manual step,
and turn yellow when it asks permission.

---

## 2. One kitty: two-tab `archon up` (control tab + agents tab)

**Goal:** attach to a single Zellij session and get **tab 1 = live dashboard**,
**tab 2 = the agent panes** — no second kitty window.

**Why:** user wants "one page = platform, one page = agents." Everything is already
one `archon` session; we just need the layout + the dashboard living *inside* it.

**Work:**
- Prereq: recommend `pipx install ~/projects/archon` so `archon` is on PATH inside
  panes (the auto dashboard-pane currently fails because archon is venv-only).
- Add `zellij.new_tab(session, name)` + `go_to_tab_name` to `zellij.py`.
- In `cli.up`: create a `control` tab running `archon dashboard`, create an
  `agents` tab, and leave focus on `agents` so dispatched feature panes land there.
- Make dispatch target the agents tab (or document that features open in the
  focused tab). Test tab-focus behavior on real Zellij (couldn't headlessly).

**Acceptance:** `archon up --repo ~/projects/testArchSim` → `zellij attach archon`
shows a control tab (live dashboard) and an agents tab; launching features adds
panes to the agents tab; `Alt+←/→` switches. One kitty only.

---

## 3. `archon analytics` — cc-dash-style graded view

**Goal:** a dense analytics screen: per-run/session **Grade · Cost · Flow ·
Efficiency · Model · Skills · Age**, cost bars per repo/project, model breakdown.

**Why:** the two reference screenshots (Ex.jpg / Ex2.jpg) are cc-dash analytics;
it's the natural "at scale" companion to the live panes. We already persist cost,
tokens, model, and phase per run.

**Work:**
- New `analytics.py`: aggregate `task_runs` (cost, tokens, duration) into grades
  (define an efficiency metric: output/cost or tokens/$), per-repo cost totals,
  model usage. New `tui.analytics_view` + `archon analytics [--window 7d]`.
- Optional: a detail panel (select a run → tools used, cost, context peak).

**Acceptance:** `archon analytics` prints a sortable graded table + per-repo cost
bars from real run data.

---

## 4. agent-deck-style "step into" polish

**Goal:** quick keyboard switching to the agent that needs you.

**Why:** user liked agent-deck's "step into a session to accept/deny." We do this
via Zellij pane focus already, but it can be smoother.

**Work:**
- `archon focus <n>` by dashboard row number (not just selector).
- Consider a tiny interactive dashboard (textual/urwid, or Rich `Live` + key
  reader) where pressing a number focuses that pane in Zellij. Optional; the
  Rich `Live` view is currently non-interactive by design.

**Acceptance:** from the dashboard, one key jumps the Zellij focus to the pane of
the agent that's `● needs help`.

---

## Smaller follow-ups / caveats to keep in mind

- **Model IDs:** defaults are `claude-opus-4-8` / `claude-sonnet-5` / `gpt-5.5`.
  Confirm they're valid on the user's accounts; overridable in
  `~/.config/archon/config.yaml → providers.<id>.models`.
- **Same-provider parallelism:** to run >1 task on one provider at once (e.g. all
  Claude), set `scheduler.per_provider_concurrency: 2+`. `--now` bypasses the
  queue and launches immediately regardless.
- **Cleanup helper:** consider `archon reset`/`archon clean` (drop DB, prune stray
  worktrees/branches) — today it's manual `rm ~/.local/share/archon/archon.db` +
  `git worktree prune`.
- **Nothing is committed yet** — the whole project is uncommitted on `main`.
  Decide whether to make the initial commit tomorrow.

## Suggested order for tomorrow

**#1 (hooks/live tether)** → **#2 (two-tab one-kitty)** → verify end-to-end on the
chip-sim → then **#3 (analytics)** and **#4 (step-into)** as polish.
