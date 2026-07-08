<p align="center">
  <img src="docs/assets/archon-hero.png" alt="Archon" width="100%" />
</p>

<h1 align="center">Archon</h1>

<p align="center">
  <strong>A Zellij-native command center for orchestrating parallel AI coding agents.</strong><br/>
  Run Claude Code, OpenAI Codex, and GitHub Copilot as controlled, observable workers — from one cockpit.
</p>

<p align="center">
  <img src="https://img.shields.io/badge/tests-125%20passing-3fb950?style=flat-square&logo=pytest&logoColor=white" alt="Tests: 125 passing" />
  <img src="https://img.shields.io/badge/python-3.11%2B-3776AB?style=flat-square&logo=python&logoColor=white" alt="Python 3.11+" />
  <img src="https://img.shields.io/badge/cockpit-Zellij-22d3ee?style=flat-square" alt="Zellij" />
  <img src="https://img.shields.io/badge/CLI-Typer%20%2B%20Rich-2b6cff?style=flat-square" alt="Built with Typer + Rich" />
  <img src="https://img.shields.io/badge/license-MIT-8b5cf6?style=flat-square" alt="MIT License" />
  <img src="https://img.shields.io/badge/status-MVP-a855f7?style=flat-square" alt="Status: MVP" />
</p>

<p align="center">
  <img src="https://img.shields.io/badge/Claude_Code-supported-3fb950?style=flat-square" alt="Claude Code supported" />
  <img src="https://img.shields.io/badge/OpenAI_Codex-supported-3fb950?style=flat-square" alt="OpenAI Codex supported" />
  <img src="https://img.shields.io/badge/GitHub_Copilot-supported-3fb950?style=flat-square" alt="GitHub Copilot supported" />
  <img src="https://img.shields.io/badge/custom_providers-pluggable-8b5cf6?style=flat-square" alt="Custom providers pluggable" />
</p>

<p align="center">
  <a href="#-quick-start">Quick start</a> ·
  <a href="#-the-mvp-demo">Demo</a> ·
  <a href="#-commands">Commands</a> ·
  <a href="#-providers">Providers</a> ·
  <a href="#-architecture">Architecture</a> ·
  <a href="#-safety">Safety</a>
</p>

---

## What is Archon?

Running several AI coding CLIs in parallel is powerful and chaotic. You end up
juggling terminal panes, `cd`-ing between worktrees, re-pasting prompts, and
squinting to see which agent is blocked on a permission prompt or burning budget.

**Archon turns [Zellij](https://zellij.dev) into a cockpit for that work.** You run
one command; Archon picks the providers, creates isolated Git worktrees per task,
launches the right CLI in the right pane, injects a high-quality prompt, and tracks
every run's state, cost, and telemetry in one dashboard.

It is **provider-agnostic from day one** — Claude, Codex, and Copilot are just
adapters, and custom CLIs plug in via config.

```
┌─ ARCHON ─ parallel AI coding cockpit ──────────────────────────────────────┐
│ PROVIDERS                                                                   │
│  claude    enabled=yes  installed=yes  auth=ready        mode=interactive   │
│  codex     enabled=yes  installed=yes  auth=ready        mode=exec          │
│  copilot   enabled=no   installed=yes  auth=needs_login  mode=interactive   │
│                                                                             │
│ TASK RUNS                          (sorted by urgency — blocked first)      │
│  PR #552 review   claude  ● blocked   review/pr-552/claude   $0.88          │
│  PR #552 review   codex   ● running   review/pr-552/codex    14k tok        │
│  newButton4User   claude  ● running   feature/newButton4User $0.21          │
└─────────────────────────────────────────────────────────────────────────────┘
```

### Why it's different

| Principle | What it means |
|---|---|
| 🧭 **Zellij is the cockpit, not the brain** | Archon owns state, routing, budgets, and telemetry; Zellij owns panes and layout. |
| 🔌 **Provider-agnostic core** | Claude / Codex / Copilot / custom CLIs are adapters. The dashboard shows normalized state. |
| 🧵 **One run = one provider = one worktree = one branch** | Two agents never fight in the same branch, and nothing casually edits `main`. |
| 🚨 **Attention routing beats status display** | A permission prompt turns the pane red, focuses it, and fires a desktop notification. |
| 🛟 **Safety by default** | Never merges, pushes, submits reviews, or deletes dirty worktrees without a human. |

---

## ✨ Features

- **Provider setup wizard** — detects installed CLIs, best-effort auth checks, saves your choice.
- **`review-pr` / `feature` dispatch** — isolated worktree + branch + pane per provider, with the prompt injected for you.
- **Live Rich dashboard** — provider readiness and task-run telemetry, color-coded and sorted by urgency.
- **Normalized telemetry** — statusline + hooks ingest cost, tokens, context %, and rate limits; permission prompts mark runs `blocked`.
- **Transcript search** — full-text search across transcripts and logs (`archon search`, `archon touched`).
- **Dry-run everything** — `--dry-run` (or `ARCHON_DRY_RUN=1`) prints the exact commands and worktree plan without touching Zellij, Git, or any provider.

---

## 🚀 Quick start

**Requirements:** Python 3.11+, [Zellij](https://zellij.dev), `git`, and at least one
provider CLI ([`claude`](https://docs.claude.com/claude-code),
[`codex`](https://developers.openai.com/codex/cli), or
[`copilot`](https://docs.github.com/copilot)).

```bash
# 1. Install
git clone https://github.com/tbosier/archon
cd archon
python -m venv .venv && source .venv/bin/activate
pip install -e .

# 2. Initialise config + database
archon init

# 3. Pick your providers (interactive wizard)
archon setup
```

Then, from any Git repo:

```bash
cd ~/ci_amplify_ai
archon up
```

> 💡 **Try it safely first.** Everything runs without side effects under
> `ARCHON_DRY_RUN=1` — great for seeing the worktree/branch/launch plan before
> Archon touches your machine.

---

## 🎬 The MVP demo

The end-to-end flow Archon is built to make effortless:

```bash
cd ~/ci_amplify_ai
archon up
#   first run shows the provider selector in the initial pane → pick Claude + Codex

archon review-pr 552
#   multiple providers enabled → Archon asks who should review → pick Claude + Codex
#   → two read-only worktrees, two panes, PR-review prompt injected into each

archon feature newButton4User
#   → Archon asks which single provider should implement → pick Claude
#   → one feature worktree + branch + pane, implementation prompt injected

archon status --watch
#   → live dashboard: provider readiness + task-run state, cost, and context
```

Archon creates the Zellij session, panes, and worktrees; you never manually
`cd` into a worktree or start a provider CLI by hand.

---

## 🧭 Commands

| Command | What it does |
|---|---|
| `archon up` | Start/attach the cockpit for a repo. First run shows the provider wizard. |
| `archon setup` | Run the provider-selection wizard and save the choice. |
| `archon providers` | List provider state. Subcommands: `doctor`, `enable`, `disable`, `login`, `refresh`. |
| `archon review-pr <N>` | Review a PR — one isolated **read-only** worktree/pane per provider. |
| `archon feature <name>` | Implement a feature — single writer by default, `--variants` for parallel variants. |
| `archon status [--watch]` | Provider readiness + task-run dashboard, sorted by urgency. |
| `archon focus <selector>` | Focus the Zellij pane for a task/run. |
| `archon stop <selector>` | Gracefully stop a run (after confirmation). |
| `archon search <query>` | Full-text search across transcripts and logs. |
| `archon touched <path>` | Show which runs touched a file. |
| `archon statusline` / `archon hook <name>` | Provider integration endpoints (called by statuslines/hooks). |

Useful flags: `--provider` (repeatable), `--all-providers`, `--ask-providers`,
`--base`, `--branch`, `--prompt`, `--variants`, and `--dry-run` on every
outward-facing command.

---

## 🔌 Providers

| Provider | ID | Default mode | Login | Telemetry |
|---|---|---|---|---|
| **Claude Code CLI** | `claude` | `interactive` | `claude` | statusline + hooks |
| **OpenAI Codex CLI** | `codex` | `exec` (JSONL) | `codex login` | `codex exec --json` stream |
| **GitHub Copilot CLI** | `copilot` | `interactive` / `-p` | `copilot login` | stdout capture |
| **Custom** | `custom:<name>` | configurable | configurable | stdout capture |

- **PR reviews** run in a **read-only** sandbox; **features** run with **workspace-write**.
- Auth checks are cheap and best-effort — Archon **never spends a paid model call** just to detect login state.
- If a provider needs login, Archon opens a pane running its **native** login flow instead of failing the cockpit.

Claude integration is wired via [`examples/claude-settings.json`](examples/claude-settings.json)
(statusline + `PermissionRequest` / `Notification` / `Stop` / `StopFailure` / `SessionEnd` hooks).

---

## 🏗 Architecture

```
Archon
  ├── Zellij cockpit            panes, tabs, focus, colours (via `zellij action`)
  ├── Git worktree manager      one isolated worktree + branch per run
  ├── task queue / dispatcher   review-pr / feature → task → task runs
  ├── provider wizard           detect · auth · enable · login
  ├── provider adapters         claude · codex · copilot · custom
  ├── telemetry / events        statusline + hooks → normalized ProviderEvents
  └── dashboard                 Rich TUI, urgency-sorted
```

State lives in **SQLite** (`repos`, `providers`, `tasks`, `task_runs`, `events`,
`transcript_events` + FTS5, `file_touches`). Config is a single YAML file.

```
~/.config/archon/config.yaml            # provider choices + startup behavior
~/.local/share/archon/archon.db         # tasks, runs, providers, telemetry
~/.local/share/archon/events.jsonl      # append-only event log
```

Override with `ARCHON_CONFIG_HOME` and `ARCHON_HOME`. See
[`examples/archon-config.yaml`](examples/archon-config.yaml) for a full config.

---

## 🛟 Safety

Archon will **never**, without explicit human confirmation:

- merge, push, or force-reset a branch
- submit / approve / request-changes on a GitHub PR
- delete a dirty worktree
- let two provider runs write to the same worktree
- auto-submit provider output as final

Every injected prompt tells the agent to stop before PR creation or external
submission unless the human explicitly asks.

---

## 🧪 Development

```bash
pip install -e ".[dev]"
pytest            # 125 tests, fully offline (subprocess/Zellij/Git are mocked or dry-run)
```

The suite mirrors the acceptance criteria: DB schema, config round-trips, provider
registry + wizard, Zellij command building, Git worktree naming, per-provider
launch commands, prompt safety rules, and malformed-input tolerance for
statusline/hooks.

```
src/archon/
  cli.py            typer app — every command
  dispatcher.py     review-pr / feature orchestration
  config.py db.py   typed config + SQLite schema
  zellij.py         `zellij action` wrapper (dry-run aware)
  git_worktree.py   isolated worktrees + safe reuse
  providers/        base contract + claude · codex · copilot · custom adapters
  statusline.py hooks.py transcript_index.py   telemetry + search
  tui.py            Rich dashboard
```

---

## 🗺 Roadmap

Archon ships the MVP milestones — skeleton CLI, provider registry + wizard, Zellij
orchestration, provider launch, `review-pr`, `feature`, statusline/hooks, and
transcript search. Planned next:

- true task queue + idle worker pool
- side-by-side provider output comparison
- automatic reviewer/tester handoff after a feature
- global budget / rate-limit scheduler
- native Zellij plugin & multi-repo workspaces

---

## 📄 License

MIT. See [`ARCHON_CODEX_BUILD_SPEC.md`](ARCHON_CODEX_BUILD_SPEC.md) for the full
build specification behind this implementation.

<p align="center"><sub>Built to make parallel AI coding deterministic, repeatable, and easy to operate.</sub></p>
