# Archon Control Center Plan

## Executive take

The strongest direction is to evolve Archon into a multi-agent engineering control
center, not a terminal multiplexer with nicer status output. The existing Python
core already has the hard runtime primitives: provider adapters, isolated
worktrees, a scheduler, DAG handoff, budget gates, telemetry ingestion, Zellij
launching, and a SQLite event store. The next product layer should be a local web
dashboard that turns those primitives into an intent-driven orchestration system.

The web app should not replace Zellij. Zellij should become the hidden runtime
surface: agents still run there, terminals remain available on demand, and the
browser becomes the primary command, observation, and escalation UI.

In short:

- Keep the Python orchestrator as the source of truth for jobs, runs, worktrees,
  providers, scheduler decisions, hooks, and local process control.
- Add a local API server over the existing core.
- Add a Next.js frontend as the default control center.
- Preserve Zellij as the live terminal escape hatch, reachable from agent detail
  pages.
- Promote "attention required" into a first-class queue with explicit decisions,
  not just a yellow blocked state.

If a Next.js frontend is built, use the existing repo assets referenced by the
README: `docs/assets/archon-hero.png` for the product identity and `archon.png`
or the existing JPG examples only where they are intentionally part of the app or
docs. Do not invent a replacement logo.

## Current codebase review

### What already exists

Archon is currently a Python 3.11 CLI/TUI app packaged through `pyproject.toml`.
The main implementation is under `src/archon`.

Important existing pieces:

- CLI entrypoint: `src/archon/cli.py`
- Rich dashboard: `src/archon/tui.py`
- SQLite schema/accessors: `src/archon/db.py`
- Core models: `src/archon/models.py`
- Scheduler: `src/archon/scheduler.py`
- Dispatch/worktree/Zellij bridge: `src/archon/dispatcher.py`
- Provider contract and adapters: `src/archon/providers/*`
- Statusline telemetry ingestion: `src/archon/statusline.py`
- Hook/event ingestion: `src/archon/hooks.py`
- Worktree management: `src/archon/git_worktree.py`
- Handoff/task graph logic: `src/archon/handoff.py`, `src/archon/taskgraph.py`
- Transcript search/indexing: `src/archon/transcript_index.py`

The README and `NEXT_STEPS.md` already point in the right direction: Archon is
positioned as a shared command center across repos, with isolated worktrees, a
provider-agnostic core, model tiering, scheduler gating, and attention routing.

### Strong foundations

- The "one run = one provider = one worktree = one branch/pane" invariant is the
  right safety model.
- Provider adapters are already isolated behind a small `AgentProvider` contract.
- The scheduler is side-effect-light and testable because dispatch is injected.
- SQLite is a reasonable local source of truth for an MVP.
- The existing `events` table and `transcript_events` table are exactly the
  substrate needed for a live activity feed.
- `archon hook` and `archon statusline` are the right integration points for
  converting provider-native activity into normalized Archon state.
- The current Rich dashboard is useful as a fallback and developer view.

### Gaps versus the desired product

- There is no first-class `job` concept. Current tasks model phases, but the
  user's intent should become a durable job with objective, constraints,
  acceptance criteria, plan, assignments, and final outcome.
- There is no first-class `agent` concept. Current providers/workers/runs are
  close, but the product needs named role agents such as Lead, Backend,
  Frontend, Tester, Reviewer, and Security.
- "Attention required" is currently inferred from statuses like `blocked`. It
  should become a queue of decision records with choices, recommendations,
  ownership, and resolution history.
- The dashboard is read-only terminal UI. The desired product needs command
  submission, approvals, decisions, agent detail pages, diffs, logs, and terminal
  handoff.
- The plan-to-execute flow still has manual seams. `NEXT_STEPS.md` correctly
  identifies hook-driven auto-advance as the first runtime gap.
- Status does not yet distinguish enough productive states: actively working,
  waiting on dependency, waiting on user, rate limited, build/test running,
  failed, complete, idle, crashed, stale.
- GitHub PR creation/merge/review lifecycle is not yet a complete product flow.
- There is no server process exposing normalized state over HTTP/SSE/WebSockets.

## Target product

Archon should feel like chatting with an engineering organization:

```text
User intent
  -> Lead agent turns intent into a job
  -> Job becomes a plan with acceptance criteria
  -> Plan becomes scoped tasks
  -> Specialized agents execute in isolated worktrees
  -> Review/test/security agents verify
  -> Human only sees decisions, failures, and final approval gates
```

The core product promise:

- Issue intent once.
- Watch structured progress, not terminal spam.
- Step in only when human judgment is required.
- Keep every terminal, diff, log, and worktree reachable when deeper inspection is
  needed.

## Recommended architecture

```text
Next.js Control Center
  - command/chat
  - observer dashboard
  - attention queue
  - job detail
  - agent detail
  - logs/diff/terminal tabs
        |
        | HTTP + SSE or WebSocket
        v
Archon API / Orchestrator
  - job planner
  - scheduler facade
  - agent registry
  - attention router
  - event stream
  - GitHub integration facade
        |
        v
Existing Archon Core
  - SQLite
  - provider adapters
  - worktrees
  - Zellij panes
  - hooks/statusline
  - transcripts
```

### Backend choice

Use FastAPI for the local API server. This fits the Python core, keeps process
control local, and avoids duplicating orchestration logic in a JavaScript server.
The Next.js app should call the Python API rather than becoming the orchestrator.

Suggested new modules:

- `src/archon/api.py` or `src/archon/server.py`
- `src/archon/jobs.py`
- `src/archon/agents.py`
- `src/archon/attention.py`
- `src/archon/events.py`
- `src/archon/planner.py`

Suggested CLI:

```bash
archon server
archon web
```

`archon web` can eventually start both the API and frontend in dev/local mode.

### Frontend choice

Use Next.js for the dashboard once the API contract exists. Keep it in a clear
subdirectory such as:

```text
web/
```

Use the repo's existing brand image assets:

- Primary brand/hero reference: `docs/assets/archon-hero.png`
- Existing root image: `archon.png`
- Existing examples: `Ex.jpg`, `Ex2.jpg`

The first screen should be the usable control center, not a marketing landing
page.

## Product model

### Job

A job is the durable unit created from user intent.

Fields to add:

- `id`
- `repo_id`
- `title`
- `objective`
- `constraints_json`
- `acceptance_criteria_json`
- `status`
- `lead_agent_id`
- `current_plan_json`
- `created_at`
- `updated_at`
- `finished_at`

Statuses:

- `intake`
- `planning`
- `awaiting_plan_approval`
- `running`
- `attention_required`
- `verifying`
- `ready_for_pr`
- `ready_to_merge`
- `complete`
- `failed`
- `cancelled`

### Agent

An agent is a role-bearing worker identity. It may map to a provider and a live
task run, but it is not the same thing as a provider.

Fields to add:

- `id`
- `job_id`
- `role`
- `provider_id`
- `display_name`
- `state`
- `current_task_id`
- `current_task_run_id`
- `last_summary`
- `created_at`
- `updated_at`

States:

- `idle`
- `planning`
- `working`
- `waiting_on_dependency`
- `waiting_on_user`
- `rate_limited`
- `running_tests`
- `reviewing`
- `failed`
- `complete`
- `stale`
- `crashed`

### Attention item

An attention item is a human decision record. This is the highest-leverage new
model.

Fields to add:

- `id`
- `job_id`
- `agent_id`
- `task_id`
- `task_run_id`
- `kind`
- `severity`
- `title`
- `summary`
- `options_json`
- `recommended_option`
- `status`
- `resolution`
- `created_at`
- `resolved_at`

Kinds:

- `plan_approval`
- `architecture_decision`
- `api_contract_conflict`
- `security_concern`
- `test_failure_decision`
- `permission_request`
- `budget_or_rate_limit`
- `merge_approval`
- `provider_auth`

Statuses:

- `open`
- `resolved`
- `dismissed`
- `superseded`

### Event

The current `events` table should be kept, but events should become the backbone
of the live UI. Add enough fields to support job-level feeds:

- `job_id`
- `agent_id`
- `visibility`
- `requires_attention`
- `summary`
- `details_json`

Keep raw provider payloads, but make the UI consume normalized events.

## UI plan

### Main layout

The best default layout is close to the GPT sketch, but with clearer product
sections:

```text
┌─────────────────────────────────────────────────────────────────────┐
│ Archon                                      Budget / Usage / Health │
├──────────────────┬──────────────────────────────┬───────────────────┤
│ AGENTS / REPOS   │ COMMAND + ACTIVE JOB         │ ACTIVITY          │
│                  │                              │                   │
│ Lead Agent       │ Natural language command     │ Structured events │
│ Backend Agent    │ Current plan                 │ Agent updates     │
│ Frontend Agent   │ Acceptance criteria          │ Test/build states │
│ Test Agent       │ Progress by phase            │                   │
│ Reviewer         │                              │                   │
├──────────────────┴──────────────────────────────┴───────────────────┤
│ ATTENTION REQUIRED                                                   │
│ Open decisions with recommended defaults and one-click resolution     │
└─────────────────────────────────────────────────────────────────────┘
```

### Required views

- Command Center: command input, repo selector, provider/model policy, job status.
- Jobs: list of active and historical jobs across repos.
- Job Detail: objective, plan, task graph, acceptance criteria, artifacts, events.
- Agents: current agent roster and live state.
- Agent Detail: overview, logs, files touched, git diff, transcript, terminal.
- Attention Queue: open decisions, recommendations, actions, history.
- Repositories: repo health, active branches/worktrees, recent jobs.
- Settings: providers, concurrency, budgets, GitHub connection, workspace paths.

### Interaction rules

- Terminals are hidden by default.
- Every terminal should be reachable from Agent Detail.
- Every attention item should have a recommended default when possible.
- Destructive or irreversible actions require explicit approval.
- Merge, push, PR review submission, and deleting dirty worktrees stay gated.
- The UI should show productive state, not just process liveness.

## Build phases

### Phase 0: finish the live runtime tether

This should happen before the web frontend, because the frontend will only be as
useful as the state it can observe.

Work:

- Wire provider hooks/statusline into launched worktrees reliably.
- On provider stop/session end, mark runs complete or failed based on outcome.
- Auto-advance plan -> execute -> review -> test where appropriate.
- Ensure blocked permission prompts create events and open attention items.
- Add stale detection using heartbeat timestamps.

Acceptance:

- A real feature run updates telemetry live.
- A stopped/completed provider process changes DB state without manual
  `archon complete`.
- The scheduler advances the chain without manual intervention.
- Permission prompts appear as attention records.

### Phase 1: add job, agent, and attention models

Work:

- Add DB tables and migrations for `jobs`, `agents`, and `attention_items`.
- Map existing tasks/task_runs into a parent job.
- Add service functions for creating jobs from structured input.
- Add service functions for opening/resolving attention items.
- Add tests around job lifecycle and attention resolution.

Acceptance:

- A command can create a job with objective, constraints, and acceptance criteria.
- Existing plan/execute/review/test tasks are visible under that job.
- Blocked runs create attention items.
- Resolving an attention item records the decision and unblocks the relevant
  workflow where applicable.

### Phase 2: local API server

Work:

- Add FastAPI dependency.
- Implement read endpoints:
  - `GET /api/health`
  - `GET /api/repos`
  - `GET /api/jobs`
  - `GET /api/jobs/{id}`
  - `GET /api/agents`
  - `GET /api/attention`
  - `GET /api/events`
- Implement write endpoints:
  - `POST /api/jobs`
  - `POST /api/jobs/{id}/approve-plan`
  - `POST /api/attention/{id}/resolve`
  - `POST /api/tasks/{id}/cancel`
  - `POST /api/runs/{id}/focus-terminal`
- Implement live stream:
  - `GET /api/events/stream` using SSE first.

Acceptance:

- API can drive the current CLI workflow without the frontend.
- SSE streams new events to a simple test client.
- Existing CLI tests still pass.

### Phase 3: Next.js control center

Work:

- Create `web/` Next.js app.
- Use existing Archon imagery from `docs/assets/archon-hero.png`.
- Build the main command center screen first.
- Add typed API client.
- Add live event subscription through SSE.
- Build attention queue with action buttons.
- Build job detail and agent detail pages.

Acceptance:

- `archon server` exposes API.
- `npm run dev` in `web/` shows a usable dashboard.
- User can submit a job from the browser.
- User can approve a plan and resolve an attention item.
- Live activity updates without refresh.
- Agent detail can focus/open the underlying Zellij pane.

### Phase 4: planner and lead-agent behavior

Work:

- Add a structured planner prompt/output schema.
- Convert natural language into:
  - repository
  - objective
  - constraints
  - acceptance criteria
  - proposed task graph
  - proposed agent roles
- Gate the first generated plan behind user approval.
- Let the lead decide minimal staffing based on task complexity.

Acceptance:

- A request such as "In contract-ai, add rebate tier forecasting..." becomes a
  structured job with acceptance criteria.
- The UI shows the proposed plan before work begins.
- User can approve, edit, or cancel.
- The scheduler launches only the needed worker roles.

### Phase 5: GitHub product loop

Work:

- Add durable PR metadata to jobs.
- Add PR creation endpoint/action.
- Add review summary aggregation.
- Add merge approval attention item.
- Keep merge gated behind explicit user action.

Acceptance:

- Completed job can create or prepare a PR.
- Review/test/security outcomes are summarized.
- UI can ask "merge?" only after checks pass and reviewers approve.

## What not to build first

- Do not start with a full real-time collaborative web app.
- Do not build a random free-for-all agent swarm.
- Do not replace SQLite until local MVP pressure proves it is insufficient.
- Do not make terminal streaming the centerpiece.
- Do not spawn every possible role for every job.
- Do not build a marketing homepage before the operational dashboard.

## Suggested immediate implementation order

1. Finish hook/statusline auto-advance and stale/blocked detection.
2. Add `jobs`, `agents`, and `attention_items` to the database.
3. Add a small service layer around job creation and attention routing.
4. Add FastAPI with read-only endpoints and SSE.
5. Add write endpoints for create job, approve plan, resolve attention, and focus
   terminal.
6. Scaffold `web/` Next.js and build the dashboard against the API.
7. Add planner/lead-agent structured output.
8. Add GitHub PR lifecycle actions.

## Open decisions before implementation

- Should the web app live inside this repo under `web/`, or should it be a
  separate package/repo?
- Should the API be FastAPI, or should the project avoid a server dependency and
  use a thinner local JSON/SSE bridge?
- Should `archon up` eventually start the API and frontend, or should `archon
  web` be a separate explicit command?
- Should plan approval be mandatory for every job, or configurable by risk level?
- Should job planning use the same provider selected for implementation, or
  always use the strongest available model/provider?
- Should GitHub integration be required for the first web MVP, or deferred until
  the local job loop feels excellent?

## Recommendation

Build the control center in layers. The "goated" version is not the prettiest
dashboard; it is the one that makes human attention scarce and valuable. The
highest-leverage feature is the attention queue backed by real workflow state.

Next.js is a good fit for the interface, but only after the backend exposes
job-level state and decisions. A browser UI over the current task/run tables would
look better, but it would still be a terminal supervisor. A browser UI over jobs,
agents, events, and attention items becomes the engineering control center.
