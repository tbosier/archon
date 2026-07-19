# Archon Pivot — Terminal-native control center for coding agents

**Framing:** Archon is not only a job launcher. It is the terminal cockpit that
shows *every* coding-agent session you run — Claude, Codex, Copilot — including
sessions you started yourself, what each is doing, and which ones need you.

> "See what is running, what is blocked, and what needs you — without opening
> three desktop applications."

This document translates the four-milestone pitch into an implementation plan
against the current codebase, and records what was **verified on this machine**
vs. what the pitch got wrong.

---

## 1. What is actually available here (verified 2026-07-13)

All three CLIs are installed: `claude` (`~/.local/bin/claude`), `codex`
(`/usr/bin/codex`), `copilot` (`/usr/bin/copilot`).

### Claude — strongest integration ✅
- **Live session registry:** `~/.claude/sessions/<pid>.json`, one per running
  Claude process, e.g.
  `{"pid":101073,"sessionId":"…","cwd":"/home/taylo/projects/archon",
    "name":"archon-d5","entrypoint":"claude-desktop","kind":"interactive",...}`.
  This includes sessions the **user launched themselves** — exactly the gap the
  pivot targets. Liveness = is `pid` alive.
- **Transcripts:** `~/.claude/projects/<encoded-cwd>/<sessionId>.jsonl`, event
  `type`s: `assistant`, `user`, `system`, `last-prompt`, `mode`,
  `queue-operation`, `attachment`. Each record carries `cwd`, `gitBranch`,
  `timestamp`, `message.role`. State is derivable from the tail + recency.
- Plus Archon's own hook stream (built earlier: `PreToolUse` / `PermissionRequest`
  / `Stop`) gives real-time approval/completion signals for Archon-launched runs.

### Copilot — real event stream (better than the pitch assumed) ✅
- `~/.copilot/session-state/<uuid>/` per session, with:
  - `workspace.yaml`: `{id, cwd, name (the task), created_at, updated_at}`
  - `events.jsonl`: a rich stream — `session.start`, `assistant.turn_start`,
    `assistant.message`, `assistant.turn_end`, `tool.execution_start`,
    `tool.execution_complete`, **`permission.requested` / `permission.completed`**,
    `hook.start` / `hook.end`, `user.message`, `system.message`.
  - `inuse.<pid>.lock`: liveness.
- **Attention detection** = tail `events.jsonl`: a `permission.requested` with no
  following `permission.completed` → needs approval; `assistant.turn_end` with a
  live lock and no pending permission → idle; dead lock → disconnected.

### Codex — session rollouts + experimental app-server ⚠️
- `~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl` (`session_meta`, `turn_context`,
  `response_item`, `event_msg`), `~/.codex/history.jsonl` (`{session_id,text,ts}`),
  `~/.codex/hooks.json` (only `SessionStart` wired, to a third-party "herdr" script).
- `codex app-server` and `codex mcp-server` exist but are **flagged experimental**.

## 2. Where the pitch is wrong / unverifiable — flagged

1. **Copilot hook names are wrong.** The pitch names `agent_idle`,
   `subagent_started/completed/failed`, `session_end`. The actual stream uses
   `assistant.turn_*`, `tool.execution_*`, `permission.requested/completed`,
   `session.start`. The *capability* is real (a full JSONL event log to tail),
   and the pitch's intended mappings are still achievable via the real events.
2. **Codex App Server is experimental**, not a stable integration surface today.
   It is a legitimate long-term path (as the pitch says) but must not be built
   against yet. PTY parsing is likewise deferred.
3. **PID liveness is host-accurate but under-reports in a sandbox** (different PID
   namespace). Adapters must treat "pid not found" conservatively.
4. **No live push stream from any CLI.** These are on-disk artifacts (registry
   files + JSONL event logs + pid locks). So the normalized model is produced by
   **polling/tailing**, not a socket the CLI pushes to. `discover()` snapshots
   are the honest API, not an event subscription (that's a later refinement).

## 3. Normalized model (the key architectural decision)

`AgentState` (as pitched): `STARTING, WORKING, WAITING_FOR_APPROVAL,
WAITING_FOR_INPUT, IDLE, COMPLETED, FAILED, DISCONNECTED`.

`AgentSession` (registry unit the TUI lists): `session_id, provider, cwd, repo,
branch, state, summary, title, updated_at, pid, source ("archon"|"external"),
needs_attention`. Adapters emit these; the view consumes only these — never
provider-specific shapes.

## 4. Reuse vs. new

**Reused (a lot, as the pitch predicted):** provider registry/config,
worktree/git helpers, the DB (jobs/tasks/runs/attention) for Archon-launched
sessions, the Textual TUI shell + rendering, the hook ingestion (real-time
Claude signals), `models.health_of` glyphs.

**Genuinely new:**
- `sessions/model.py` — `AgentState` + `AgentSession` (provider-agnostic).
- `sessions/base.py` — `SessionAdapter` protocol (`discover() -> list[AgentSession]`).
- `sessions/claude_adapter.py` — observes `~/.claude/sessions` + transcripts
  (external sessions), overlaid with Archon DB attention for launched runs.
- `sessions/copilot_adapter.py` — observes `~/.copilot/session-state` events.
- `sessions/registry.py` — aggregates adapters + Archon DB, dedups, returns the
  unified list.
- `archon sessions` CLI view (the pitch's dashboard mockup) as the MVP surface.

## 5. Milestones (this pass = 1 + 2)

1. **Unified session registry — DISCOVER across providers.** Read the on-disk
   artifacts above; surface Archon-launched *and* user-launched sessions in one
   list. (Launch/terminate already partly exist via dispatch; the new capability
   is discovery of external sessions.)
2. **Reliable attention detection — the killer feature.** `working` vs
   `waiting_for_approval` vs `idle` vs `failed`/`disconnected` must be trustworthy,
   from the real signals, Claude first.

**Deferred (explicitly not this pass):** attach/detach TUI polish (M3), git/PR
metadata display (M4), DAG scheduling, budget routing, reviewer handoffs, and any
deep Copilot/Codex app-server integration. A lean Copilot adapter is included
because the data was right there; Codex observation is left as a documented stub.
