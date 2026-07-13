<p align="center">
  <img src="docs/assets/archon-hero.png" alt="Archon" width="100%" />
</p>

<h1 align="center">Archon</h1>

<p align="center">
  <strong>An orchestration brain for AI coding agents.</strong><br/>
  Tell Archon the outcome; it plans the work, routes workers through Agent Deck, and surfaces approval decisions.
</p>

<p align="center">
  <img src="https://img.shields.io/badge/tests-194%20passing-3fb950?style=flat-square&logo=pytest&logoColor=white" alt="Tests: 194 passing" />
  <img src="https://img.shields.io/badge/python-3.11%2B-3776AB?style=flat-square&logo=python&logoColor=white" alt="Python 3.11+" />
  <img src="https://img.shields.io/badge/backend-Agent%20Deck-22d3ee?style=flat-square" alt="Agent Deck" />
  <img src="https://img.shields.io/badge/CLI-Typer%20%2B%20Rich-2b6cff?style=flat-square" alt="Built with Typer + Rich" />
  <img src="https://img.shields.io/badge/license-MIT-8b5cf6?style=flat-square" alt="MIT License" />
  <img src="https://img.shields.io/badge/status-MVP-a855f7?style=flat-square" alt="Status: MVP" />
</p>

<p align="center">
  <img src="https://img.shields.io/badge/Claude_Code-supported-3fb950?style=flat-square" alt="Claude Code supported" />
  <img src="https://img.shields.io/badge/OpenAI_Codex-supported-3fb950?style=flat-square" alt="OpenAI Codex supported" />
  <img src="https://img.shields.io/badge/GitHub_Copilot-supported-3fb950?style=flat-square" alt="GitHub Copilot supported" />
  <img src="https://img.shields.io/badge/task_graph-DAG_scheduler-2b6cff?style=flat-square" alt="Task graph scheduler" />
  <img src="https://img.shields.io/badge/model_tiering-plan%20%2F%20execute-8b5cf6?style=flat-square" alt="Model tiering" />
</p>

<p align="center">
  <a href="#quick-start">Quick start</a> ·
  <a href="#daily-use">Daily use</a> ·
  <a href="#example-workflows">Workflows</a> ·
  <a href="#the-pipeline">Pipeline</a> ·
  <a href="#commands">Commands</a> ·
  <a href="#providers">Providers</a> ·
  <a href="#architecture">Architecture</a> ·
  <a href="#safety">Safety</a>
</p>

---

## What is Archon?

Running several AI coding CLIs in parallel is powerful and chaotic. Archon is the
planning and governance layer for that work: it turns a natural-language outcome
into a validated task graph, applies routing and approval policy, then launches
workers through [Agent Deck](https://github.com/asheshgoplani/agent-deck).

Agent Deck owns terminal/session plumbing. Archon owns intent intake, planning,
task dependencies, review/test policy, budget gates, and the attention inbox.

One shared cockpit spans **every repo** — watch all your agents on one screen,
no tab-switching:

```
┌─ ARCHON ─ command center (session: archon) ─────────────────────────────────┐
│ WORKER POOL                                                                  │
│  claude-w1   claude    busy                                                  │
│  codex-w1    codex     busy                                                  │
│                                                                              │
│ TASK RUNS                        (sorted by urgency — blocked first)         │
│  Repo       Task            Provider Phase    Model             State        │
│  alpha      health-endpoint claude   execute  claude-sonnet-5   running      │
│  beta       dark-mode       codex    review   gpt-5.5           running      │
│  gamma      rate-limiter    claude   plan     claude-opus-4-8   queued       │
│  ci_amplify PR #552 review  codex    review   gpt-5.5           running      │
└──────────────────────────────────────────────────────────────────────────────┘
```

Three different repositories (`alpha`, `beta`, `gamma`) plus a PR review, four
agents, **one dashboard**. Each run stays isolated in its own worktree/branch;
only the view is unified.

### Design principles

| Principle | What it means |
|---|---|
| Agent Deck is the execution backend | Archon owns state, routing, budgets, and policy; Agent Deck owns worker sessions. |
| Provider-agnostic core | Claude / Codex / Copilot / custom CLIs are adapters. The dashboard shows normalized state. |
| One run = one provider = one worktree = one branch | Two agents never fight in the same branch, and nothing casually edits `main`. |
| Attention routing beats status display | A permission prompt turns the pane red, focuses it, and fires a desktop notification. |
| Safety by default | Never merges, pushes, submits reviews, or deletes dirty worktrees without a human. |

---

## Features

- **Planner intake** — `archon do "..."` asks a planner model for a validated JSON plan.
- **Plan preview + approval** — low/medium-risk plans can be approved with `--yes`; high-risk plans require a human.
- **Agent Deck backend** — workers launch as Agent Deck sessions; attach with the command Archon prints.
- **Task queue + idle worker pool** — enqueue work; the scheduler dispatches ready tasks to idle providers within concurrency limits.
- **Dependency graph (DAG)** — every task chain is a graph; a task runs only when its dependencies are `done`.
- **Model tiering** — a strong model plans, a cheaper model executes (per provider, configurable).
- **Reviewer/tester handoff** — when a feature's implementation finishes, Archon automatically queues a review and a test pass.
- **Budget + rate-limit scheduler** — soft/hard cost caps and five-hour rate-limit thresholds gate dispatch.
- **Live Rich dashboard** — provider readiness, worker pool, and task-run telemetry, color-coded and sorted by urgency.
- **Normalized telemetry** — statusline + hooks ingest cost, tokens, context %, and rate limits; permission prompts mark runs `blocked`.
- **Transcript search** — full-text search across transcripts and logs (`archon search`, `archon touched`).
- **Dry-run everything** — `--dry-run` (or `ARCHON_DRY_RUN=1`) previews plans without touching Git or any provider.

---

## The pipeline

A feature is not one shot — it's a dependency chain, and Archon drives it end to
end. Each phase uses a model tier chosen for the job: **plan and review** get the
strong model; **execute and test** get the cheaper one.

```
        strong model              cheaper model            strong model            cheaper model
      ┌──────────────┐          ┌──────────────┐         ┌──────────────┐        ┌──────────────┐
      │     PLAN     │  ──▶     │   EXECUTE    │  ──▶    │    REVIEW    │  ──▶   │     TEST     │
      │  Opus 4.8 /  │          │  Sonnet 5 /  │         │  Opus 4.8 /  │        │  Sonnet 5 /  │
      │ GPT-5.5 high │          │ GPT-5.5 med  │         │ GPT-5.5 high │        │ GPT-5.5 med  │
      └──────────────┘          └──────────────┘         └──────────────┘        └──────────────┘
         analyse the             implement on the          review the diff          run + verify
         codebase                shared worktree           (read-only)              the tests
```

The scheduler walks this graph: a phase becomes *ready* only once its predecessor
is `done`, dispatch is gated by the budget/rate-limit policy, and the
reviewer/tester phases are appended automatically after execution.

```
$ archon graph
TASK-20260708-001  [done]        plan     · claude-opus-4-8
    \- TASK-20260708-002  [done]        execute  · claude-sonnet-5
        \- TASK-20260708-003  [queued]      review   · claude-opus-4-8
            \- TASK-20260708-004  [queued]      test     · claude-sonnet-5
```

---

## Three agents, three tasks, one screen

Archon runs a single shared **command-center session** that spans repositories, so
agents working on entirely separate projects appear side by side — each provider
in its own pane, its own worktree, its own branch. Nothing collides.

```
╔═══════════════════════ ci-amplify-ai-archon (Zellij) ═══════════════════════╗
║ dashboard ▾            ║ claude ● running      ║ codex ● running             ║
║ WORKER POOL            ║ feature/newButton4User║ review/pr-552/codex         ║
║  claude   busy         ║                       ║                             ║
║  codex    busy         ║ > Editing            ║ $ codex exec --sandbox      ║
║  copilot  busy         ║   UserButton.tsx …    ║   read-only …               ║
║                        ║   +42 -8              ║ ok 14k tokens · $0.02       ║
║ TASK RUNS              ╠═══════════════════════╬═════════════════════════════╣
║  nb4u   claude execute ║ copilot (!) BLOCKED   ║ logs ▾                      ║
║  pr-552 codex  review  ║ feature/dark-mode     ║ 18:22 claude edit auth.py   ║
║  dark   copilot execute║                       ║ 18:22 codex  read app.tsx   ║
║  ● blocked → focus!    ║ Allow write to        ║ 18:23 copilot PermissionReq ║
║                        ║ src/theme.ts? (y/N)   ║ → pane turned red, notified ║
╚════════════════════════╩═══════════════════════╩═════════════════════════════╝
   claude → new feature       codex → PR review        copilot → second feature
   (Sonnet, workspace-write)  (GPT-5.5, read-only)     (blocked on permission)
```

The dashboard pane owns state and routing; each provider pane is a real worker.
When Copilot hits a permission prompt, Archon marks the run `blocked`, colors the
pane red, focuses it, and sends a desktop notification — attention routing, not a
wall of green checkmarks.

---

## Quick start

**Requirements:** Python 3.11+, `git`, [Agent Deck](https://github.com/asheshgoplani/agent-deck),
and at least one worker CLI such as `claude` or `codex`. Real planning currently
uses `claude -p --output-format json`; `--dry-run` uses a deterministic local planner.

```bash
# 1. Install
git clone https://github.com/tbosier/archon
cd archon
uv venv
uv pip install -e .

# 2. Initialise config + database
.venv/bin/archon init

# 3. Check Agent Deck is available
agent-deck --version

# 4. Preview a plan without side effects
.venv/bin/archon do "add a hello endpoint" --repo /path/to/repo --dry-run --plan-only

# 5. Approve and dispatch a low/medium-risk plan
.venv/bin/archon do "add a hello endpoint" --repo /path/to/repo --yes
```

Use `--dry-run --yes` to exercise the full plan/enqueue/dispatch path against an
in-memory database. It is the safest first command on a new machine.

If you prefer an already-activated virtualenv, `archon ...` works the same as
`.venv/bin/archon ...`.

---

## Daily use

The primary v2 flow is command-first:

```bash
archon do "fix the flaky checkout test" --repo ~/work/my-app --plan-only
archon do "fix the flaky checkout test" --repo ~/work/my-app --yes
archon status
archon jobs show <job-id>
archon focus <run-or-task>   # prints: agent-deck session attach <id>
archon stop <run-or-task> --yes
```

What happens on `archon do`:

1. Archon sends your request, repo path, routing table, constraints, and recent jobs to the planner.
2. The planner returns strict JSON matching `PlanProposal`.
3. Archon validates policy: execute tasks need dependent review and test tasks; high-risk plans need approval.
4. Approved plans become a job, tasks, and dependency edges in SQLite.
5. The scheduler dispatches ready tasks through the configured backend.
6. Agent Deck creates and manages the worker session.

Repositories must already be Git repos. For a brand-new project:

```bash
mkdir -p ~/projects/TestArchFE
cd ~/projects/TestArchFE
git init
git commit --allow-empty -m "chore: init"
```

Then run `archon do ... --repo /home/taylo/projects/TestArchFE`.

### Configuration

Archon stores config in the normal XDG config location. The relevant v2 defaults are:

```yaml
backend:
  kind: agentdeck
  agentdeck:
    command: agent-deck
    tested_version: Agent Deck v1.9.73
routing:
  cheap:
    tool: claude
    model: haiku
  standard:
    tool: codex
    model: gpt-5-codex
  high:
    tool: claude
    model: opus
policy:
  auto_approve_low_risk: false
  retry_limit_per_task: 2
```

---

## Example workflows

### 1. A brand-new project

Starting from an empty directory. Archon needs a Git repo to anchor worktrees, so
initialise one first, then let the cockpit take over.

```bash
mkdir my-app && cd my-app
git init && git commit --allow-empty -m "chore: init"

archon up                      # first run → provider wizard in the initial pane
                               #   select Claude (and Codex if you like)

archon feature "scaffold a FastAPI service with a /health endpoint"
#   → queues PLAN (Opus) → EXECUTE (Sonnet)
#   → Archon opens a worker pane on branch feature/scaffold-a-fastapi-service
#   → the plan agent surveys the (empty) repo; the execute agent scaffolds it

archon status --watch          # follow the run: phase, model, cost, state
archon complete <plan-task>    # when the plan looks good, advance to execute
#   → on execute completion, Archon auto-queues REVIEW then TEST
```

### 2. Reviewing a pull request

Read-only, one isolated worktree per provider — safe to run several reviewers at once.

```bash
cd ~/ci_amplify_ai
archon review-pr 552
#   multiple providers enabled → Archon asks who should review
#   select Claude + Codex

#   → git worktree add ../ci_amplify_ai-pr-552-review-claude  (branch review/pr-552/claude)
#   → git worktree add ../ci_amplify_ai-pr-552-review-codex   (branch review/pr-552/codex)
#   → each provider launches in read-only mode with the PR-review prompt injected
#     (Claude interactive · Codex `exec --sandbox read-only`, both on the strong model)

archon status                  # watch both reviews; focus one with:
archon focus codex-pr-552-review
```

Explicit provider selection and single-reviewer forms:

```bash
archon review-pr 552 --provider claude
archon review-pr 552 --provider claude --provider codex
archon review-pr 552 --all-providers
```

### 3. Implementing a new feature

One writer, model-tiered, with an automatic review + test chain.

```bash
cd ~/ci_amplify_ai
archon feature newButton4User --provider claude
#   → PLAN task on Opus 4.8, EXECUTE task on Sonnet 5 (queued as a chain)
#   → the plan task dispatches immediately into feature/newButton4User

archon graph                   # see plan → execute → review → test
archon complete <plan-task>    # plan approved → scheduler dispatches execute
archon complete <execute-task> # implementation done → auto-handoff:
#   → REVIEW (strong model, read-only) and TEST (cheaper model) are queued
archon schedule                # dispatch whatever is now ready, within budget
```

Want several providers to attempt the same feature as competing variants?

```bash
archon feature newButton4User --provider claude --provider codex --variants
#   → feature/newButton4User/claude  and  feature/newButton4User/codex
#     each in its own worktree — never the same branch
```

### 4. Many repos at once, one command center

`archon up` in any number of repos joins the **same** shared session, so agents
across projects show up on one screen.

```bash
archon up --repo ~/alpha    # each attaches to session `archon`
archon up --repo ~/beta
archon up --repo ~/gamma

archon feature "health endpoint" --repo ~/alpha --provider claude
archon feature "dark mode"       --repo ~/beta  --provider codex
archon feature "rate limiter"    --repo ~/gamma --provider claude

archon dashboard            # one screen: Repo · Task · Provider · Phase · Model · State
zellij attach archon        # or attach the cockpit and watch the live panes
```

Prefer isolated per-repo cockpits instead? Set `command_center.shared: false` in
`config.yaml` (or pass `archon up --session <name>`).

---

## Model tiering

Each provider defines a **plan** tier (strong, for analysis) and an **execute**
tier (cheaper, for doing). Analytical phases (`plan`, `review`) use the plan tier;
doing phases (`execute`, `test`) use the execute tier. Defaults:

| Provider | Plan / review | Execute / test |
|---|---|---|
| Claude Code | `claude-opus-4-8` | `claude-sonnet-5` |
| OpenAI Codex | `gpt-5.5` · reasoning **high** | `gpt-5.5` · reasoning **medium** |
| GitHub Copilot | (configurable) | (configurable) |

Override per provider in `config.yaml`:

```yaml
providers:
  claude:
    models:
      plan:    { model: claude-opus-4-8 }
      execute: { model: claude-sonnet-5 }
  codex:
    models:
      plan:    { model: gpt-5.5, reasoning: high }
      execute: { model: gpt-5.5, reasoning: medium }
```

Archon injects the right flags at launch — `claude --model …`,
`codex exec --model … -c model_reasoning_effort=…` — and records which model each
run used.

---

## Queue, graph, and budget

- **Queue + workers.** `archon feature` enqueues a task chain; the scheduler
  dispatches *ready* tasks to *idle* workers, respecting `max_concurrency` and
  `per_provider_concurrency` (one writer per provider by default).
- **Dependency graph.** `archon graph` renders the DAG; a task is ready only when
  every dependency is `done`. `archon queue` lists what is pending vs waiting.
- **Budget / rate limits.** Before each dispatch the scheduler consults the budget
  policy (spec §14):

  | Five-hour rate limit | Action |
  |---|---|
  | 0–70% | dispatch normally |
  | 70–85% | prefer small tasks (reviews / tests) |
  | 85–95% | no new implementation agents |
  | 95–100% | pause the queue |

  Cost caps work the same way: past `soft_usd` Archon prefers small work; past
  `hard_usd` it pauses. `archon budget` shows the current status; `archon pause` /
  `archon resume` are manual overrides.

---

## Commands

| Command | What it does |
|---|---|
| `archon init` | Initialise config and the SQLite database; safe to run repeatedly. |
| `archon up` | Start/attach the cockpit for a repo. First run shows the provider wizard; registers the worker pool. |
| `archon web` | Start the local browser control center at `http://127.0.0.1:8716`. |
| `archon server` | Start only the local FastAPI/SSE API; `archon web` currently delegates to this. |
| `archon setup` | Run the provider-selection wizard and save the choice. |
| `archon providers` | List provider state. Subcommands: `doctor`, `enable`, `disable`, `login`, `refresh`. |
| `archon jobs` / `attention` | Inspect control-center jobs and open attention items. |
| `archon review-pr <N>` | Review a PR — one isolated read-only worktree/pane per provider. |
| `archon feature <name>` | Queue a feature as plan → execute (→ review → test). `--variants` for parallel variants, `--now` to skip the queue. |
| `archon queue` | Show queued and ready tasks. |
| `archon graph` | Render the task dependency graph. |
| `archon schedule [--watch]` | Dispatch ready tasks, gated by concurrency + budget. |
| `archon complete <selector>` | Mark a task done; trigger the reviewer/tester handoff and next dispatch. |
| `archon budget` / `pause` / `resume` | Inspect and control the scheduler. |
| `archon dashboard` | The command center: live view of every agent across all repos, one screen. |
| `archon tui` | Launch the Rich live dashboard directly. |
| `archon status [--watch]` | Provider readiness, worker pool, and task-run dashboard (all repos). |
| `archon focus <selector>` / `stop <selector>` | Focus or gracefully stop a run's pane. |
| `archon search <query>` / `touched <path>` | Full-text search transcripts/logs; show which runs touched a file. |
| `archon statusline` / `hook <name>` | Provider integration endpoints (called by statuslines/hooks). |

Useful flags: `--provider` (repeatable), `--all-providers`, `--ask-providers`,
`--base`, `--branch`, `--prompt`, `--variants`, `--now`, and `--dry-run`.

---

## Providers

| Provider | ID | Default mode | Login | Telemetry |
|---|---|---|---|---|
| Claude Code CLI | `claude` | `interactive` | `claude` | statusline + hooks |
| OpenAI Codex CLI | `codex` | `exec` (JSONL) | `codex login` | `codex exec --json` stream |
| GitHub Copilot CLI | `copilot` | `interactive` / `-p` | `copilot login` | stdout capture |
| Custom | `custom:<name>` | configurable | configurable | stdout capture |

- **Reviews** run in a read-only sandbox; **execution** runs with workspace-write.
- Auth checks are cheap and best-effort — Archon never spends a paid model call just to detect login state.
- If a provider needs login, Archon opens a pane running its native login flow instead of failing the cockpit.

Claude integration is wired via [`examples/claude-settings.json`](examples/claude-settings.json)
(statusline + `PermissionRequest` / `Notification` / `Stop` / `StopFailure` / `SessionEnd` hooks).

---

## Architecture

```
Archon
  ├── local web control center  browser UI + FastAPI/SSE API
  ├── Zellij cockpit            panes, tabs, focus, colours (via `zellij action`)
  ├── Git worktree manager      one isolated worktree + branch per run
  ├── jobs / agents / attention normalized control-center objects
  ├── task queue + scheduler    DAG-aware dispatch to an idle worker pool
  ├── budget / rate-limit gate  soft/hard cost caps + rate-limit thresholds
  ├── provider wizard           detect · auth · enable · login
  ├── provider adapters         claude · codex · copilot · custom (model-tiered)
  ├── reviewer/tester handoff   feature done → review → test
  ├── telemetry / events        statusline + hooks → normalized ProviderEvents
  └── dashboards                browser console + Rich TUI, urgency-sorted
```

State lives in **SQLite** (`repos`, `providers`, `jobs`, `agents`,
`attention_items`, `tasks`, `task_dependencies`, `task_runs`, `workers`,
`events`, `transcript_events` + FTS5, `file_touches`).
Config is a single YAML file.

```
~/.config/archon/config.yaml            # provider choices, model tiers, scheduler + budget
~/.local/share/archon/archon.db         # tasks, runs, dependency graph, workers, telemetry (all repos)
~/.local/share/archon/events.jsonl      # append-only event log
```

The command center is one shared session by default:

```yaml
command_center:
  shared: true       # every repo's agents land in one session → one screen
  session: archon    # the shared Zellij session name
```

Override with `ARCHON_CONFIG_HOME` and `ARCHON_HOME`. See
[`examples/archon-config.yaml`](examples/archon-config.yaml) for a full config.

---

## Safety

Archon will **never**, without explicit human confirmation:

- merge, push, or force-reset a branch
- submit / approve / request-changes on a GitHub PR
- delete a dirty worktree
- let two provider runs write to the same worktree
- auto-submit provider output as final

Every injected prompt tells the agent to stop before PR creation or external
submission unless the human explicitly asks.

---

## Development

```bash
pip install -e ".[dev]"
pytest            # 183 tests, fully offline (subprocess/Zellij/Git are mocked or dry-run)
```

The suite mirrors the acceptance criteria: DB schema + migrations, config
round-trips, provider registry + wizard, model tiering, Zellij command building,
Git worktree naming, per-provider launch commands, the dependency graph +
scheduler + budget policy, the reviewer/tester handoff, prompt safety rules, and
malformed-input tolerance for statusline/hooks.

```
src/archon/
  cli.py                 typer app — every command
  api.py                 FastAPI + embedded browser control center
  jobs.py agents.py      control-center job and agent lifecycle helpers
  attention.py           user-attention records and resolution
  dispatcher.py          review-pr / feature orchestration + queue launch
  queue.py taskgraph.py  task queue + dependency DAG
  scheduler.py budget.py idle-worker dispatch + cost/rate-limit gating
  handoff.py             feature → review → test
  phases.py              per-phase model-tier resolution
  config.py db.py        typed config + SQLite schema
  zellij.py              `zellij action` wrapper (dry-run aware)
  git_worktree.py        isolated worktrees + safe reuse
  providers/             base contract + claude · codex · copilot · custom adapters
  statusline.py hooks.py transcript_index.py   telemetry + search
  tui.py                 Rich dashboard
```

---

## Roadmap

Delivered: the full MVP plus the task queue + idle worker pool, the dependency
graph, per-phase model tiering, the reviewer/tester handoff, and the global
budget / rate-limit scheduler. Planned next:

- side-by-side provider output comparison pane
- native Zellij plugin & pipe integration
- auto-restart of stale panes
- multi-repo workspaces
- GitHub review-comment drafting workflow

---

## License

MIT. See [`ARCHON_CODEX_BUILD_SPEC.md`](ARCHON_CODEX_BUILD_SPEC.md) for the full
build specification behind this implementation.

<p align="center"><sub>Built to make parallel AI coding deterministic, repeatable, and easy to operate.</sub></p>
