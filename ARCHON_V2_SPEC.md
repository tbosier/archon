# Archon v2 — Orchestration Brain on Agent Deck

**Status:** Draft spec for implementation
**Repo:** github.com/tbosier/archon
**Decision:** Archon stops being a session runtime. It becomes a planning, delegation, and governance layer that uses [agent-deck](https://github.com/asheshgoplani/agent-deck) as its execution backend. Agent-deck owns terminal plumbing (tmux sessions, worktrees, status detection, cost collection, transcripts, notifications). Archon owns intent → plan → task graph → routing → policy → approval.

**One-line pitch:** Tell Archon the outcome. It plans the work, routes it to agents via agent-deck, enforces review/test/approval policy, and surfaces only the decisions that need a human.

---

## 1. Architecture

```
┌──────────────────────────────────────────────┐
│ ARCHON (Python, this repo)                   │
│                                              │
│  TUI (Textual)  ── command bar, job tree,    │
│                    attention inbox            │
│  Planner        ── LLM: intent → plan JSON   │
│  Task graph     ── existing taskgraph.py DAG │
│  Scheduler      ── existing scheduler.py     │
│  Budget/policy  ── existing budget.py        │
│  Handoff        ── existing handoff.py       │
│  State          ── existing db.py (SQLite)   │
└──────────────────┬───────────────────────────┘
                   │ ExecutionBackend interface
                   │ (subprocess → agent-deck CLI)
┌──────────────────▼───────────────────────────┐
│ AGENT DECK (Go, external dependency)         │
│  session lifecycle · tmux (isolated socket)  │
│  worktrees · status detection · costs        │
│  transcripts/search · notifications · MCP    │
└──────────────────────────────────────────────┘
```

The user keeps Zellij as their personal shell. Agent-deck runs workers on its own isolated tmux socket. Archon's TUI runs in a Zellij pane; "attach to worker" shells out to the agent-deck attach command.

**Conductor policy:** Archon does NOT use agent-deck's conductor. Archon *is* the supervisor. Two brains issuing commands is a failure mode. Agent-deck watchers/notifications may still be configured for passive alerting, but no conductor session is created.

---

## 2. Module disposition (current codebase)

### KEEP (largely unchanged)
| Module | Role |
|---|---|
| `db.py` | SQLite schema: jobs → tasks → task_runs, dependencies, events, attention items |
| `taskgraph.py` | DAG helpers (Kahn's ordering, cycle detection) |
| `scheduler.py` | tick loop: dispatch ready tasks, respect concurrency + budget |
| `budget.py` | spend gating |
| `handoff.py` | phase chaining (plan → execute → review → test) |
| `jobs.py` | job lifecycle |
| `attention.py` | escalation/approval inbox |
| `prompts.py` | phase prompt templates (extend, see §4) |
| `models.py`, `config.py`, `paths.py`, `util.py` | as-is |

### REPLACE
| Module | Replaced by |
|---|---|
| `zellij.py` | `backends/agentdeck.py` (see §3) |
| `dispatcher.py` pane-pasting half | backend calls; keep the worktree-naming + task-creation logic |
| `_feature_name_from_message` slugifier in `api.py` | `planner.py` LLM intake (see §4) |

### DELETE (agent-deck owns this)
- `provider_health.py`, `provider_login.py`, `provider_wizard.py`
- `transcript_index.py` (use agent-deck global search)
- `statusline.py`
- `providers/` pane-launch logic (keep the registry as a *routing* table: provider id → agent-deck tool name + model tier)
- The inline `CONTROL_CENTER_HTML` web UI in `api.py`. The FastAPI app survives only if a thin JSON API is still wanted for future remote use; otherwise delete `api.py` and make the TUI talk to the DB + backend directly. **Default: delete.**
- `git_worktree.py` — agent-deck creates worktrees. Keep only branch-naming conventions (move to `naming.py`).

---

## 3. ExecutionBackend

New package `src/archon/backends/`.

```python
# backends/base.py
from dataclasses import dataclass
from typing import Protocol

@dataclass
class WorkerSpec:
    title: str                 # agent-deck session title
    repo_path: str             # repo root; backend creates worktree
    branch: str                # branch name Archon chose
    tool: str                  # "claude" | "codex" | ...
    model: str | None          # model/tier flag if the tool supports it
    prompt: str                # first message pasted after boot
    use_worktree: bool = True
    parent_id: str | None = None   # archon run id, for traceability

@dataclass
class WorkerHandle:
    backend_id: str            # agent-deck session/instance id
    title: str

@dataclass
class WorkerStatus:
    state: str                 # running | waiting | idle | error | done | missing
    cost_usd: float | None
    last_output_tail: str      # last N lines for the TUI detail pane

class ExecutionBackend(Protocol):
    def launch(self, spec: WorkerSpec) -> WorkerHandle: ...
    def send(self, handle: WorkerHandle, message: str) -> None: ...
    def status(self, handle: WorkerHandle) -> WorkerStatus: ...
    def output(self, handle: WorkerHandle, lines: int = 200) -> str: ...
    def stop(self, handle: WorkerHandle) -> None: ...
    def attach_command(self, handle: WorkerHandle) -> list[str]: ...
    # command the TUI execs to drop the user into the worker terminal
    def list_all(self) -> list[tuple[WorkerHandle, WorkerStatus]]: ...
```

### `backends/agentdeck.py`
Implementation via `subprocess.run` against the agent-deck CLI:
- `launch` → `agent-deck launch <repo> -t <title> -c <tool> -m <prompt>` plus worktree/branch flags. **First implementation task: run `agent-deck launch --help` and `agent-deck session --help`, capture the real flag surface, and pin it in `backends/AGENTDECK_CLI.md`.** Do not guess flags from this spec.
- `send` → `agent-deck session send <title> "<msg>"`
- `output` → `agent-deck session output <title>`
- `status` / `list_all` → prefer a JSON output mode if the CLI has one; otherwise parse `agent-deck list` text and mark this as tech debt.
- `attach_command` → whatever agent-deck exposes for attaching (or `tmux -L <socket> attach -t <session>`).

### Version pinning + contract tests
Agent-deck is single-maintainer and fast-moving. Mitigations:
1. Record the tested agent-deck version in `config.toml` (`backend.agentdeck.tested_version`). On startup, warn if the installed version differs.
2. `tests/test_agentdeck_contract.py`: a small suite that (when agent-deck is installed) launches a trivial worker in a temp repo, sends a message, reads output, stops it. Run in CI behind a flag; run locally before releases.
3. All CLI strings live in one module. No agent-deck invocations anywhere else.

### `backends/local.py` (fallback)
A degenerate backend that launches `claude -p` / `codex exec` as headless subprocesses, no terminal. Used for tests and for CI-style unattended runs. Keeps Archon runnable without agent-deck installed.

---

## 4. Planner (the actual new capability)

New module `src/archon/planner.py`. This replaces the slugifier and is the core of Archon's identity.

### Flow
```
user message (+ repo context)
   → planner LLM call (headless: `claude -p --output-format json`, cheap/mid tier)
   → PlanProposal (validated JSON)
   → persisted as job + tasks + dependency edges (existing schema)
   → shown to user in TUI for approval (configurable: auto-approve low-risk)
   → scheduler dispatches ready tasks via backend
```

### PlanProposal schema (pydantic)
```python
class PlannedTask(BaseModel):
    key: str                      # local key for dependency refs, e.g. "investigate"
    title: str
    phase: Literal["plan","execute","review","test","docs"]
    tool: str                     # routing decision: claude | codex | ...
    model_tier: Literal["cheap","standard","high"]
    prompt: str                   # full prompt for the worker
    depends_on: list[str] = []    # keys
    risk: Literal["low","medium","high"]
    est_cost_usd: float | None

class PlanProposal(BaseModel):
    title: str
    objective: str
    repo: str
    constraints: list[str]
    acceptance_criteria: list[str]
    tasks: list[PlannedTask]
    clarifying_question: str | None   # planner may ask ONE question instead of planning
    overall_risk: Literal["low","medium","high"]
```

### Planner prompt requirements
- System prompt instructs: respond ONLY with JSON matching the schema, no markdown fences.
- Inputs: user message, repo name/path, list of enabled tools + their tier mapping from config, standing constraints from config (see policy below), recent job titles in this repo (for context).
- Rules encoded in the prompt:
  - Prefer the smallest plan that meets acceptance criteria. 1-task plans are fine.
  - Every `execute` task must be followed by a `review` task assigned to a *different* tool or higher tier, and a `test` task, unless the change is docs-only.
  - Never plan a push, merge, or PR submission step — those are human actions Archon surfaces in the attention inbox.
  - If the request is ambiguous on something that changes the plan shape, set `clarifying_question` and return no tasks.
- Parsing: strip fences defensively, `PlanProposal.model_validate_json`, on failure retry once with the validation error appended, then fail into the attention inbox.

### Model tiering (routing table in config.toml)
```toml
[routing]
cheap    = { tool = "claude", model = "haiku" }
standard = { tool = "codex",  model = "gpt-5-codex" }   # example
high     = { tool = "claude", model = "opus" }
review_must_differ_from_execute = true
```
Planner picks tiers; config maps tiers to concrete tool+model; backend passes them through.

### Policy (governance, enforced in code not prose)
`policy.py` (new, small) — hard checks applied at dispatch time regardless of what the planner produced:
- no worker is ever launched on the repo's default branch (worktree + branch always)
- review-required: an `execute` task cannot be marked done without a completed `review` task depending on it, unless job is flagged `docs_only`
- high-risk plans require human approval before any dispatch (attention item)
- per-job and daily spend caps (existing `budget.py`)
- retry limit per task (config, default 2)

---

## 5. TUI (Textual)

Delete the web UI. New `src/archon/tui/` package using **Textual** (not raw Rich). The existing `tui.py` tables become widgets.

### Design principles
Command-first, like lazygit/k9s: a persistent command bar plus single-key verbs. Dark theme using the existing Archon color tokens (port from DESIGN.md / frontend work). One accent color. Health glyphs: `●` working (green), `◐` waiting/needs input (yellow), `✗` error (red), `✓` done (dim green). No nested table borders — trees, panels, whitespace.

### Layout
```
┌ ARCHON ──────────────────────────── budget $4.20/$25 ┐
│ ATTENTION (2)                                        │
│  ◐ approve plan: "fix A1 regen" (3 tasks, ~$1.10)  y/n/e │
│  ◐ worker asks: "delete legacy config?" job#12   a=answer │
├──────────────────────────┬───────────────────────────┤
│ JOBS                     │ DETAIL: exec·fix-a1-regen │
│ ▾ fix A1 regen   ci_ampl │ tool codex · std · $0.41  │
│   ✓ plan    claude cheap │ branch archon/fix-a1-...  │
│   ● execute codex  std   │ ──────────────────────    │
│   ○ review  claude high  │ <last 200 lines of        │
│   ○ test    claude cheap │  worker output, live,     │
│ ▸ rebate scenarios  done │  tailing via backend>     │
├──────────────────────────┴───────────────────────────┤
│ > in ci_amplify_ai: fix why A1 stopped regenerating_ │
└──────────────────────────────────────────────────────┘
```

- **Attention inbox pinned on top.** Approvals answered inline (`y`/`n`/`e` to edit plan, `a` to type an answer relayed via `backend.send`). This is the primary surface — triage is the orchestrator's job.
- **Jobs tree** (left): Job → phase tasks with glyph, tool, tier, cost. Sorted: needs-attention, running, queued, done.
- **Detail pane** (right): selected run's metadata + live output tail (poll `backend.output` every 2s; make interval configurable).
- **Command bar** (bottom, `:` or `>` to focus): natural-language intent goes here. On submit → planner → **plan preview rendered inline as a tree with model assignments + cost estimate** → Enter approves, `e` edits, Esc discards. The plan-preview-before-dispatch interaction is the product's signature moment; get it right.

### Keybindings
`j/k` navigate · `Enter` attach to worker (exec `backend.attach_command`, resume TUI on exit) · `y/n` approve/reject · `a` answer worker · `d` show diff (`git -C <worktree> diff` in pager) · `s` stop run · `r` retry · `$` cost breakdown · `/` filter jobs · `:` command bar · `q` quit.

### Implementation notes
- Textual `App` with `Tree`, `RichLog`, `Input` widgets; CSS file for the token theme.
- All backend calls off the UI thread (`@work` decorator / asyncio.to_thread).
- The TUI must degrade gracefully if agent-deck is missing: read-only DB view + error banner.

---

## 6. CLI surface (keep thin)

`archon` (no args) → launch TUI.
`archon do "<message>" [--repo PATH] [--yes]` → plan + (approve|auto) + dispatch, headless. This is what scripts and cron use.
`archon status` → existing Rich table snapshot (keep, it's cheap).
`archon jobs show <id>`, `archon stop <run>`, `archon budget` → keep from current CLI.
Delete: `archon web`, `archon server`, `archon providers login/doctor/wizard`, `archon up` pane orchestration.

---

## 7. Milestones

**M1 — Backend seam (no behavior change visible yet)**
1. Create `backends/base.py` + `backends/agentdeck.py` + `backends/local.py`.
2. Capture real agent-deck CLI surface into `backends/AGENTDECK_CLI.md`; write contract test.
3. Rewire `dispatcher.py` to call the backend; delete zellij pane-pasting. `zellij.py` removed.
4. Green: `archon do "add a hello endpoint" --repo <toy>` launches a worker in agent-deck, phases chain via existing handoff.

**M2 — Planner**
5. `planner.py` + schema + prompt + retry/validation; wire into `archon do` and delete the slugifier path.
6. `policy.py` hard checks at dispatch.
7. Green: vague multi-step request produces an inspectable multi-task DAG with tiered routing; high-risk plans block on approval.

**M3 — TUI**
8. Textual app: jobs tree + detail tail + attention inbox + command bar with plan preview.
9. Attach/answer/approve flows working end-to-end.
10. Delete web UI (`api.py` HTML) and dead modules from §2.

**M4 — Hardening**
11. Version-pin warning, backend failure → attention item, retries, spend caps exercised in tests.
12. README rewrite: reposition as "orchestration brain on agent-deck", new demo gif = plan preview.

**Definition of done per milestone:** tests pass, `archon do` E2E works against a throwaway repo, no references to deleted modules remain.

---

## 8. Non-goals (v2)
- No remote/multi-machine support (agent-deck has it; expose later if needed).
- No Docker isolation management (configure in agent-deck directly).
- No custom transcript search (use agent-deck's).
- No web UI.
- No conductor integration.
