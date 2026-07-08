# Archon — Codex Build Spec

> **Codex:** Build **Archon**, a Zellij-native command center for running one or more AI coding CLIs as controlled, observable workers. Archon should make parallel AI coding workflows deterministic, repeatable, and easy to operate from a single cockpit.

This file is intended to be dropped into a new repo or an existing prototype repo and used as the primary implementation prompt/spec.

---

## 0. Product Summary

**Archon** is a CLI/TUI tool that turns Zellij into a cockpit for AI coding workers.

Archon must **not** be hard-coded as a Claude-only tool. It should support a provider model from day one:

```text
Archon
  ├── Zellij cockpit
  ├── Git worktree manager
  ├── task queue / dispatcher
  ├── provider selection wizard
  ├── provider adapters
  │   ├── Claude Code CLI adapter       command: claude
  │   ├── OpenAI Codex CLI adapter      command: codex
  │   ├── GitHub Copilot CLI adapter    command: copilot
  │   └── custom CLI adapter            command: user-configured
  ├── telemetry / events / transcripts
  └── dashboard
```

The user should be able to run:

```bash
cd ~/ci_amplify_ai
archon up
```

On first run, the **initial Archon pane** should show a setup wizard asking:

```text
Which AI coding CLIs do you want Archon to use?

[x] Claude Code CLI      command: claude
[x] OpenAI Codex CLI     command: codex
[ ] GitHub Copilot CLI   command: copilot
[ ] Custom provider

Use Space to select, Enter to continue.
```

Then Archon should:

1. detect whether each selected CLI is installed;
2. detect or best-effort verify whether it appears authenticated;
3. if already authenticated, spin up worker panes immediately;
4. if not authenticated, open a login/auth pane and let the selected CLI's native login flow prompt the user;
5. save selected providers to Archon config;
6. create dashboard/log/git/provider panes;
7. create isolated Git worktrees per task;
8. launch the selected provider CLI(s) in the right panes/worktrees;
9. inject high-quality task prompts;
10. track task state, pane IDs, provider names, provider run/session IDs, transcript paths, costs/tokens when available, context/rate-limit telemetry when available;
11. mark blocked/stale/crashed/budget-capped agents visually;
12. provide searchable history of tasks/transcripts/file touches.

The user should **not** manually create panes, manually `cd` into worktrees, or manually start each provider CLI in every pane.

---

## 1. Core User Workflows

### 1.1 First run: start the cockpit and choose providers

Command:

```bash
archon up --repo ~/ci_amplify_ai
```

Expected first-run behavior:

- Resolve repo root via `git rev-parse --show-toplevel`.
- Create or attach to a Zellij session.
- Open an initial pane named `archon-setup` or `dashboard`.
- Show an interactive provider-selection wizard inside that pane.
- Present installed/missing/auth status for each known provider.
- Let the user select one or more providers.
- Save provider choices to config.
- Start selected provider panes or auth panes as needed.

Example first-run TUI:

```text
┌─ Archon Setup ─────────────────────────────────────────────────────────┐
│ Repo: /home/john/ci_amplify_ai                                         │
│ Zellij session: ci-amplify-ai-archon                                   │
├────────────────────────────────────────────────────────────────────────┤
│ Select AI coding CLIs to enable:                                       │
│                                                                        │
│  [x] Claude Code CLI        claude     installed     auth: unknown     │
│  [x] OpenAI Codex CLI       codex      installed     auth: ready       │
│  [ ] GitHub Copilot CLI     copilot    installed     auth: needs login │
│  [ ] Custom provider                                                +  │
│                                                                        │
│ Startup behavior:                                                      │
│  (•) Launch idle panes for enabled providers now                       │
│  ( ) Only spawn provider panes when tasks start                        │
│                                                                        │
│ [Continue] [Doctor] [Quit]                                             │
└────────────────────────────────────────────────────────────────────────┘
```

After Continue:

- For providers marked ready: launch provider worker pane(s).
- For providers marked `needs_login` or `auth: unknown`: open an auth pane with the provider's native login command or CLI startup command.
- Dashboard should show provider readiness and instructions.

Example provider dashboard:

```text
PROVIDERS
Provider    Enabled  Installed  Auth          Mode          Pane
claude      yes      yes        unknown       interactive   terminal_4
codex       yes      yes        ready         exec          ready
copilot     no       yes        needs_login   interactive   -

TASKS
Task                Provider  Status   Branch     Pane       Cost/Tokens
none                -         -        -          -          -
```

### 1.2 Normal startup after providers are configured

Command:

```bash
archon up --repo ~/ci_amplify_ai
```

Expected behavior:

- If provider config exists, do not force the wizard every time.
- Show the dashboard immediately.
- Launch provider panes according to saved startup behavior.
- If a selected provider command is missing, mark it as `missing` and show install/login instructions.
- If a provider requires login, open a login pane rather than failing the entire cockpit.

Useful flags:

```bash
archon up --provider claude
archon up --provider codex --provider copilot
archon up --all-providers
archon up --ask-providers
archon up --skip-provider-prompt
archon up --spawn-provider-panes
archon up --spawn-on-task
```

Rules:

- `--ask-providers` always shows the provider selector.
- `--provider` can be repeated.
- If no provider is configured and `--skip-provider-prompt` is passed, fail with a clear error.
- If multiple providers are enabled and a task command does not specify provider(s), prompt the user.

### 1.3 Login/auth flow

The user has probably already logged in to these tools manually. Archon should not make this annoying.

Expected behavior:

- If a selected provider is installed and appears ready, just launch it.
- If a selected provider is installed but not authenticated, open an auth pane and let the provider's own login prompt handle authentication.
- If a provider is not installed, show the missing command and do not select it by default.
- Auth checks should be best-effort and cheap. Do not burn paid model calls solely to check auth.

Provider defaults:

```yaml
providers:
  claude:
    display_name: Claude Code CLI
    command: claude
    default_mode: interactive
    login_command: claude
    notes: Launching claude should surface native login/setup if needed.

  codex:
    display_name: OpenAI Codex CLI
    command: codex
    default_mode: exec
    login_command: codex login
    alt_login_command: codex login --device-auth
    notes: Codex supports explicit login and non-interactive exec mode.

  copilot:
    display_name: GitHub Copilot CLI
    command: copilot
    default_mode: interactive
    login_command: copilot login
    alt_login_command: copilot
    notes: Copilot can authenticate through copilot login or the interactive /login flow.
```

`archon providers login <provider>` should open a Zellij pane running the provider login command.

Examples:

```bash
archon providers login codex
archon providers login copilot
archon providers login claude
```

Auth pane behavior:

```text
┌─ codex-login ─────────────────────────────────────────────┐
│ Running: codex login                                      │
│ Complete the native provider login flow.                  │
│ When done, return to the dashboard and press r to refresh. │
└───────────────────────────────────────────────────────────┘
```

### 1.4 Review a GitHub PR

Command:

```bash
archon review-pr 552
```

If exactly one provider is enabled, use it automatically.

If multiple providers are enabled and no provider is specified, prompt:

```text
Which provider(s) should review PR #552?

[x] Claude Code CLI
[x] OpenAI Codex CLI
[ ] GitHub Copilot CLI
[ ] All enabled providers

Review tasks are safe to run with multiple providers because each gets its own read-only/review worktree.
```

Explicit examples:

```bash
archon review-pr 552 --provider claude
archon review-pr 552 --provider codex
archon review-pr 552 --provider claude --provider codex
archon review-pr 552 --all-providers
```

Expected behavior for each selected provider:

- Use the active repo from `archon up`, config, or `--repo`.
- Create a dedicated task record.
- Create a provider-specific isolated review worktree, for example:

```bash
git fetch origin
git worktree add ../ci_amplify_ai-pr-552-review-claude origin/main
cd ../ci_amplify_ai-pr-552-review-claude
gh pr checkout 552 -b review/pr-552/claude --force
```

- Open a new Zellij pane named something like `claude-pr-552-review`, `codex-pr-552-review`, or `copilot-pr-552-review`.
- Set the pane cwd to the review worktree.
- Launch the selected provider according to its adapter mode.
- Inject a PR review prompt.
- The pane/task should become visible in the dashboard.

Provider launch examples:

```bash
# Claude interactive pane
ARCHON_TASK_ID=TASK-... claude -n claude-pr-552-review

# Codex non-interactive runner pane or background process
ARCHON_TASK_ID=TASK-... codex exec --json --sandbox read-only "<prompt>"

# Copilot programmatic or interactive pane
ARCHON_TASK_ID=TASK-... copilot -p "<prompt>"
# or
ARCHON_TASK_ID=TASK-... copilot
```

PR review prompt template:

```text
You are reviewing PR #{pr_number} in {repo_name}.

Provider: {provider_name}
Worktree: {worktree_path}
Branch: {branch}

Rules:
- Do not modify files unless explicitly asked.
- Do not submit a GitHub review until the human approves.
- Review the PR diff against the base branch.
- Use gh pr view #{pr_number} and gh pr diff #{pr_number} as needed.
- Inspect changed files directly.
- Run focused tests only when helpful.

Look for:
- correctness bugs
- auth or data-access mistakes
- security issues
- broken types
- bad generated code
- missing tests
- regressions
- maintainability issues

Produce:
1. executive summary
2. must-fix issues
3. nice-to-fix issues
4. tests run and results
5. suggested GitHub review comments
6. final recommendation: approve / comment / request changes
```

### 1.5 Implement a feature

Command:

```bash
archon feature newButton4User
```

If exactly one provider is enabled, use it automatically.

If multiple providers are enabled and no provider is specified, prompt:

```text
Which provider should implement `newButton4User`?

( ) Claude Code CLI
( ) OpenAI Codex CLI
( ) GitHub Copilot CLI
( ) Multiple providers as separate implementation variants

Note: by default, only one provider should write code for a feature branch.
Multiple providers are allowed only as separate variant worktrees/branches.
```

Explicit examples:

```bash
archon feature newButton4User --provider claude
archon feature newButton4User --provider codex
archon feature newButton4User --provider copilot
archon feature newButton4User --provider claude --provider codex --variants
```

Rules for feature implementation:

- Default to **one writer provider**.
- If multiple providers are selected, require `--variants` or an explicit confirmation.
- Each provider must get its own branch/worktree.
- Never let two providers write to the same worktree/branch.

Single-provider worktree example:

```bash
git fetch origin
git worktree add ../ci_amplify_ai-newButton4User -b feature/newButton4User origin/main
cd ../ci_amplify_ai-newButton4User
```

Multi-provider variant worktree examples:

```text
../ci_amplify_ai-newButton4User-claude   branch: feature/newButton4User/claude
../ci_amplify_ai-newButton4User-codex    branch: feature/newButton4User/codex
../ci_amplify_ai-newButton4User-copilot  branch: feature/newButton4User/copilot
```

Feature implementation prompt template:

```text
Implement feature `{feature_name}` in {repo_name}.

Provider: {provider_name}
Worktree: {worktree_path}
Branch: {branch}

Rules:
- Work only in this branch/worktree.
- Keep the diff focused and minimal.
- First inspect the project structure before editing.
- Find the correct frontend/backend locations before making changes.
- Follow nearby code patterns.
- Add or update tests if the repo has a nearby test pattern.
- Run the smallest useful validation commands.
- Do not create a PR until the human approves.

Feature request:
{feature_description}

At the end, summarize:
1. files changed
2. behavior added
3. tests run
4. risks / follow-up questions
5. exact commands the human should run next
```

---

## 2. Design Principles

1. **Zellij is the cockpit, not the brain.**
   - Archon owns state, provider selection, task routing, metadata, hooks, budgets, transcript indexing, and alerts.
   - Zellij owns panes, tabs, focus, colors, and layout.

2. **Provider-agnostic core.**
   - Claude, Codex, Copilot, and custom tools are provider adapters.
   - The dashboard should show normalized task state regardless of provider.
   - Do not hard-code the database, task model, or dashboard around Claude-only fields.

3. **One task run = one provider = one worktree = one branch/session.**
   - Never let multiple providers fight in one branch.
   - Never let an agent casually edit `main`.

4. **No manual pane management.**
   - The user runs `archon up`, selects provider CLIs, then runs `archon review-pr`, `archon feature`, etc.
   - Archon creates panes and launches provider CLIs.

5. **Prompt the human when ambiguity matters.**
   - At startup: ask which provider CLIs to use.
   - Per task: if multiple providers are enabled and no provider is specified, ask which provider(s) should run.
   - For write tasks: default to one writer unless `--variants` is explicitly chosen.

6. **Never rely on screen scraping as primary truth.**
   - Use provider-native structured output, hooks, statuslines, transcript files, SQLite, and Git metadata where available.
   - Use Zellij screen dumps only for debugging stale/hung panes.

7. **Attention routing beats status display.**
   - Blocked agents must visually escalate.
   - A permission prompt should turn the pane red, focus it, and send a desktop notification.

8. **Budget/rate-limit safety matters.**
   - Track cost/tokens per provider when available.
   - Track context/rate-limit usage when available.
   - Allow soft caps and hard caps.

9. **MVP first. Fancy later.**
   - Build a reliable CLI, provider wizard, and simple TUI before attempting a native Zellij plugin.

---

## 3. Recommended Implementation Stack

Use Python for the first implementation because it is excellent for CLI orchestration.

Recommended dependencies:

```toml
[project]
requires-python = ">=3.11"
dependencies = [
  "typer>=0.12",
  "rich>=13.7",
  "pydantic>=2.7",
  "pyyaml>=6.0",
  "questionary>=2.0",
]

[project.scripts]
archon = "archon.cli:app"
```

Use:

- `typer` for CLI
- `rich` for dashboard tables/status
- `questionary` for provider selection prompts, with a fallback to plain numbered prompts if unavailable
- `sqlite3` from the Python standard library for state
- `subprocess` for Zellij/Git/GitHub/provider CLI process control
- `pydantic` for typed config/models if useful

Do **not** add a heavy framework until the MVP works.

---

## 4. Target Repo Layout

```text
archon/
  pyproject.toml
  README.md
  ARCHON_CODEX_BUILD_SPEC.md
  src/
    archon/
      __init__.py
      cli.py
      config.py
      db.py
      models.py
      paths.py
      zellij.py
      git_worktree.py
      github.py
      prompts.py
      dispatcher.py
      providers/
        __init__.py
        base.py
        registry.py
        claude.py
        codex.py
        copilot.py
        custom.py
      provider_wizard.py
      provider_health.py
      provider_login.py
      hooks.py
      statusline.py
      tui.py
      transcript_index.py
      notify.py
      util.py
  examples/
    archon-config.yaml
    claude-settings.json
    zellij-layout.kdl
  tests/
    test_cli.py
    test_config.py
    test_db.py
    test_git_worktree.py
    test_prompts.py
    test_zellij.py
    test_provider_registry.py
    test_provider_wizard.py
    test_provider_launch.py
    test_hooks.py
```

---

## 5. Config and Data Directories

Config directory:

```text
~/.config/archon/
  config.yaml
```

Data directory:

```text
~/.local/share/archon/
  archon.db
  events.jsonl
  hooks.log
  panes.json
  queue.yaml
  screens/
  transcripts/
```

Allow overrides:

```bash
ARCHON_CONFIG_HOME=/some/config archon up
ARCHON_HOME=/some/data archon up
```

Example config:

```yaml
version: 1
startup:
  show_provider_wizard: auto   # auto | always | never
  provider_panes: launch_now   # launch_now | spawn_on_task
  default_task_provider_policy: ask_if_multiple

providers:
  claude:
    enabled: true
    display_name: Claude Code CLI
    command: claude
    default_mode: interactive
    login_command: claude
    telemetry: claude_statusline_hooks

  codex:
    enabled: true
    display_name: OpenAI Codex CLI
    command: codex
    default_mode: exec
    login_command: codex login
    exec_args:
      - exec
      - --json
      - --sandbox
      - workspace-write
    review_args:
      - exec
      - --json
      - --sandbox
      - read-only
    telemetry: jsonl_stdout

  copilot:
    enabled: false
    display_name: GitHub Copilot CLI
    command: copilot
    default_mode: interactive
    login_command: copilot login
    prompt_args:
      - -p
    telemetry: stdout_text

  custom: []
```

---

## 6. SQLite Schema

Create `archon.db` with these tables.

```sql
CREATE TABLE IF NOT EXISTS repos (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  root_path TEXT NOT NULL UNIQUE,
  zellij_session TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS providers (
  id TEXT PRIMARY KEY,             -- claude | codex | copilot | custom:<name>
  display_name TEXT NOT NULL,
  command TEXT NOT NULL,
  enabled INTEGER NOT NULL DEFAULT 0,
  installed INTEGER NOT NULL DEFAULT 0,
  auth_status TEXT NOT NULL,        -- ready | needs_login | unknown | missing | error
  default_mode TEXT NOT NULL,       -- interactive | exec | prompt
  login_command TEXT,
  last_checked_at TEXT,
  last_error TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS provider_panes (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  provider_id TEXT NOT NULL,
  repo_id INTEGER,
  zellij_session TEXT NOT NULL,
  zellij_pane_id TEXT,
  zellij_pane_name TEXT NOT NULL,
  purpose TEXT NOT NULL,            -- worker | login | task | dashboard | logs | git
  status TEXT NOT NULL,             -- starting | ready | needs_login | running | exited | error
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  FOREIGN KEY(provider_id) REFERENCES providers(id),
  FOREIGN KEY(repo_id) REFERENCES repos(id)
);

CREATE TABLE IF NOT EXISTS tasks (
  id TEXT PRIMARY KEY,
  repo_id INTEGER NOT NULL,
  type TEXT NOT NULL,               -- pr_review | feature | test | security | custom
  name TEXT NOT NULL,
  status TEXT NOT NULL,             -- queued | starting | running | blocked | stale | crashed | done | failed | budget_capped | awaiting_provider
  priority INTEGER NOT NULL DEFAULT 0,
  pr_number INTEGER,
  prompt TEXT NOT NULL,
  provider_policy TEXT NOT NULL,    -- single | multi_review | variants | ask
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  finished_at TEXT,
  FOREIGN KEY(repo_id) REFERENCES repos(id)
);

CREATE TABLE IF NOT EXISTS task_runs (
  id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL,
  provider_id TEXT NOT NULL,
  status TEXT NOT NULL,             -- queued | starting | running | blocked | stale | crashed | done | failed | budget_capped
  branch TEXT,
  base_branch TEXT,
  worktree_path TEXT,
  zellij_session TEXT,
  zellij_pane_id TEXT,
  zellij_pane_name TEXT,
  provider_session_name TEXT,
  provider_session_id TEXT,
  provider_run_id TEXT,
  transcript_path TEXT,
  stdout_log_path TEXT,
  stderr_log_path TEXT,
  cost_usd REAL DEFAULT 0,
  input_tokens INTEGER,
  output_tokens INTEGER,
  total_tokens INTEGER,
  context_used_pct REAL,
  rate_limit_five_hour_pct REAL,
  rate_limit_seven_day_pct REAL,
  last_heartbeat_at TEXT,
  last_output_at TEXT,
  soft_budget_usd REAL,
  hard_budget_usd REAL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  finished_at TEXT,
  FOREIGN KEY(task_id) REFERENCES tasks(id),
  FOREIGN KEY(provider_id) REFERENCES providers(id)
);

CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id TEXT,
  task_run_id TEXT,
  provider_id TEXT,
  event_type TEXT NOT NULL,
  severity TEXT NOT NULL,           -- info | warn | error | critical
  message TEXT,
  raw_json TEXT,
  created_at TEXT NOT NULL,
  FOREIGN KEY(task_id) REFERENCES tasks(id),
  FOREIGN KEY(task_run_id) REFERENCES task_runs(id),
  FOREIGN KEY(provider_id) REFERENCES providers(id)
);

CREATE TABLE IF NOT EXISTS transcript_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id TEXT,
  task_run_id TEXT,
  provider_id TEXT,
  provider_session_id TEXT,
  transcript_path TEXT,
  role TEXT,
  tool_name TEXT,
  file_path TEXT,
  text TEXT,
  raw_json TEXT,
  created_at TEXT NOT NULL,
  FOREIGN KEY(task_id) REFERENCES tasks(id),
  FOREIGN KEY(task_run_id) REFERENCES task_runs(id),
  FOREIGN KEY(provider_id) REFERENCES providers(id)
);

CREATE VIRTUAL TABLE IF NOT EXISTS transcript_fts USING fts5(
  task_id,
  task_run_id,
  provider_id,
  file_path,
  text
);

CREATE TABLE IF NOT EXISTS file_touches (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id TEXT NOT NULL,
  task_run_id TEXT NOT NULL,
  provider_id TEXT NOT NULL,
  file_path TEXT NOT NULL,
  action TEXT NOT NULL,             -- read | write | edit | bash | test | unknown
  first_seen_at TEXT NOT NULL,
  last_seen_at TEXT NOT NULL,
  FOREIGN KEY(task_id) REFERENCES tasks(id),
  FOREIGN KEY(task_run_id) REFERENCES task_runs(id),
  FOREIGN KEY(provider_id) REFERENCES providers(id)
);
```

---

## 7. CLI Commands

Implement these commands.

### 7.1 `archon up`

```bash
archon up --repo ~/ci_amplify_ai --session ci-amplify-ai-archon
```

Responsibilities:

- resolve repo root via `git rev-parse --show-toplevel`
- create/update repo record
- create/attach Zellij session
- if providers are not configured, show provider wizard in the initial pane
- if providers are configured, show dashboard immediately
- create dashboard/log/git/provider panes if missing
- start selected provider panes according to startup config
- do not fail the cockpit just because one optional provider is missing or needs login

### 7.2 `archon setup`

```bash
archon setup
archon setup --repo ~/ci_amplify_ai
archon setup --inside-zellij
```

Responsibilities:

- run provider-selection wizard
- detect installed/missing provider commands
- let user enable/disable providers
- let user choose launch mode: `launch_now` or `spawn_on_task`
- save config
- optionally open auth panes

### 7.3 `archon providers`

```bash
archon providers
archon providers doctor
archon providers enable claude codex
archon providers disable copilot
archon providers login codex
archon providers login copilot
archon providers login claude
archon providers refresh
```

Responsibilities:

- list provider state
- run best-effort installation/auth checks
- update config and DB
- open login panes when requested

### 7.4 `archon review-pr`

```bash
archon review-pr 552 --repo ~/ci_amplify_ai
archon review-pr 552 --provider claude
archon review-pr 552 --provider claude --provider codex
archon review-pr 552 --all-providers
```

Responsibilities:

- if provider ambiguous, prompt user
- create parent review task
- create one task run per selected provider
- create provider-specific review worktree per run
- launch provider pane/process
- inject PR review prompt
- mark task run running

### 7.5 `archon feature`

```bash
archon feature newButton4User --repo ~/ci_amplify_ai
```

Optional flags:

```bash
archon feature newButton4User \
  --provider claude \
  --branch feature/newButton4User \
  --base origin/main \
  --prompt "Add a new user-facing button to the account page"

archon feature newButton4User \
  --provider claude \
  --provider codex \
  --variants
```

Responsibilities:

- if provider ambiguous, prompt user
- default to one writer provider
- require `--variants` or confirmation for multiple writing providers
- create parent feature task
- create one task run per selected provider
- create isolated feature worktree per run
- launch provider pane/process
- inject implementation prompt
- mark task run running

### 7.6 `archon status`

```bash
archon status
archon status --watch
```

Show tables:

```text
PROVIDERS
Provider    Enabled  Installed  Auth         Mode          Pane
claude      yes      yes        ready        interactive   terminal_4
codex       yes      yes        ready        exec          -
copilot     yes      yes        needs_login  interactive   terminal_9

TASK RUNS
TASK                 PROVIDER  STATE     BRANCH                         PANE       COST/TOKENS
PR #552 review       claude    running   review/pr-552/claude           terminal_5 $0.21
PR #552 review       codex     done      review/pr-552/codex            terminal_6 14k tok
newButton4User       claude    blocked   feature/newButton4User         terminal_8 $0.88
```

Sort task runs by urgency:

1. blocked
2. budget-capped
3. stale
4. crashed
5. failed
6. running
7. queued
8. done

### 7.7 `archon focus`

```bash
archon focus pr-552-review
archon focus TASK-20260707-001
archon focus RUN-20260707-001-codex
```

Focus the matching Zellij pane.

### 7.8 `archon stop`

```bash
archon stop RUN-20260707-001-claude
archon stop TASK-20260707-001
```

Gracefully send Ctrl+C or close the pane after confirmation.

### 7.9 `archon search`

```bash
archon search "auth.py"
archon search "permission denied" --since 7d
archon touched app/auth.py
```

Search transcript FTS and file-touches table.

### 7.10 `archon statusline`

Called by provider statusline integrations, initially Claude Code.

```bash
archon statusline
```

Behavior:

- read JSON from stdin
- infer task run from `ARCHON_TASK_RUN_ID`, `ARCHON_TASK_ID`, `ARCHON_PROVIDER_ID`, pane ID, session name, or transcript path
- update task-run telemetry
- print a short one-line statusline to stdout
- never crash the provider if Archon fails

### 7.11 `archon hook`

Called by provider hooks where supported.

```bash
archon hook PermissionRequest
archon hook Notification
archon hook Stop
archon hook StopFailure
archon hook SessionEnd
archon hook ProviderEvent
```

Behavior:

- read hook JSON from stdin
- write to DB and `events.jsonl`
- classify severity
- update task-run state
- for permission prompts: mark blocked, color/focus pane, notify human

---

## 8. Provider Adapter Architecture

Create `providers/base.py`.

```python
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Literal, Protocol

ProviderMode = Literal["interactive", "exec", "prompt"]
AuthStatus = Literal["ready", "needs_login", "unknown", "missing", "error"]

@dataclass
class ProviderInfo:
    id: str
    display_name: str
    command: str
    default_mode: ProviderMode
    login_command: list[str] | None
    installed: bool
    auth_status: AuthStatus
    notes: str | None = None

@dataclass
class ProviderLaunch:
    argv: list[str]
    cwd: Path
    env: dict[str, str]
    mode: ProviderMode
    expects_prompt_paste: bool
    captures_jsonl: bool

@dataclass
class ProviderEvent:
    type: str
    provider_id: str
    task_run_id: str | None
    severity: str = "info"
    message: str | None = None
    raw: dict | None = None

class AgentProvider(Protocol):
    id: str
    display_name: str
    command: str
    default_mode: ProviderMode

    def detect_installed(self) -> bool: ...
    def detect_auth(self) -> AuthStatus: ...
    def login_launch(self, repo: Path | None = None) -> ProviderLaunch: ...
    def worker_launch(self, task_run: "TaskRun", prompt: str) -> ProviderLaunch: ...
    def parse_event_line(self, line: str) -> ProviderEvent | None: ...
    def compact_status(self, raw: dict) -> str | None: ...
```

### Provider behavior rules

- Provider adapters should normalize events into Archon's `ProviderEvent` format.
- If a provider has structured JSONL output, parse it.
- If a provider is interactive only, Archon can still create a pane and paste prompts.
- If auth detection is uncertain, return `unknown`, not `error`.
- Do not spend paid model calls just to detect auth.
- Login commands must run in their own visible Zellij pane.
- Provider failures should mark that provider or task run failed, not kill the whole dashboard.

---

## 9. Provider Implementations

### 9.1 Claude Code CLI adapter

Provider ID: `claude`

Default command:

```bash
claude
```

Default mode: `interactive`

Launch example:

```bash
ARCHON_TASK_ID="TASK-..."
ARCHON_TASK_RUN_ID="RUN-..."
ARCHON_PROVIDER_ID="claude"
ARCHON_REPO_ROOT="/home/user/ci_amplify_ai"
ARCHON_WORKTREE="/home/user/ci_amplify_ai-newButton4User"
ARCHON_ZELLIJ_SESSION="ci-amplify-ai-archon"
ARCHON_PANE_NAME="claude-feature-newButton4User"
claude -n "claude-feature-newButton4User"
```

Prompt delivery:

- Launch pane.
- Wait until pane exists.
- Paste prompt into the pane.
- Send Enter.

Telemetry:

- Use Claude statusline JSON via `archon statusline`.
- Use Claude hooks via `archon hook`.
- Read transcript path from statusline/hook payload when available.

Example `examples/claude-settings.json`:

```json
{
  "statusLine": {
    "type": "command",
    "command": "archon statusline",
    "refreshInterval": 5
  },
  "hooks": {
    "PermissionRequest": [
      {
        "matcher": ".*",
        "hooks": [
          { "type": "command", "command": "archon hook PermissionRequest" }
        ]
      }
    ],
    "Notification": [
      {
        "matcher": ".*",
        "hooks": [
          { "type": "command", "command": "archon hook Notification" }
        ]
      }
    ],
    "Stop": [
      {
        "hooks": [
          { "type": "command", "command": "archon hook Stop" }
        ]
      }
    ],
    "StopFailure": [
      {
        "hooks": [
          { "type": "command", "command": "archon hook StopFailure" }
        ]
      }
    ],
    "SessionEnd": [
      {
        "hooks": [
          { "type": "command", "command": "archon hook SessionEnd" }
        ]
      }
    ]
  }
}
```

### 9.2 OpenAI Codex CLI adapter

Provider ID: `codex`

Default command:

```bash
codex
```

Login command:

```bash
codex login
```

Alternative device auth command:

```bash
codex login --device-auth
```

Default modes:

- PR review: `exec` with read-only sandbox
- Feature implementation: `exec` with workspace-write sandbox
- Optional: interactive pane using `codex`

Launch examples:

```bash
# Review mode
ARCHON_TASK_ID="TASK-..." \
ARCHON_TASK_RUN_ID="RUN-..." \
ARCHON_PROVIDER_ID="codex" \
codex exec --json --sandbox read-only "<PR review prompt>"

# Feature mode
ARCHON_TASK_ID="TASK-..." \
ARCHON_TASK_RUN_ID="RUN-..." \
ARCHON_PROVIDER_ID="codex" \
codex exec --json --sandbox workspace-write "<feature prompt>"
```

Telemetry:

- Capture stdout/stderr to log files.
- Parse JSONL lines from `codex exec --json` when available.
- Normalize command executions, file changes, model messages, plan updates, and token usage into `ProviderEvent` and `transcript_events`.

Auth behavior:

- If user selected Codex and it is installed, try to use existing login.
- If login is missing or unknown, open a pane named `codex-login` running `codex login`.
- The dashboard should tell the user to complete the native login flow, then press `r` or run `archon providers refresh`.

### 9.3 GitHub Copilot CLI adapter

Provider ID: `copilot`

Default command:

```bash
copilot
```

Login command:

```bash
copilot login
```

Alternative login path:

```text
Launch `copilot`, then use `/login` when prompted by Copilot CLI.
```

Default modes:

- Interactive pane: `copilot`
- Programmatic prompt: `copilot -p "<prompt>"`

Launch examples:

```bash
# Programmatic one-shot prompt
ARCHON_TASK_ID="TASK-..." \
ARCHON_TASK_RUN_ID="RUN-..." \
ARCHON_PROVIDER_ID="copilot" \
copilot -p "<prompt>"

# Interactive pane
ARCHON_TASK_ID="TASK-..." \
ARCHON_TASK_RUN_ID="RUN-..." \
ARCHON_PROVIDER_ID="copilot" \
copilot
```

Telemetry:

- MVP can capture stdout/stderr text.
- If Copilot hooks or structured outputs are available in the user's version, add support later.
- Mark permission prompts as blocked if the pane output or hook events indicate human approval is needed.

Auth behavior:

- If user selected Copilot and it is installed, launch it if ready.
- If login is missing or unknown, open a pane named `copilot-login` running `copilot login`.
- If that command is unavailable in the user's local version, fall back to opening `copilot` and let the native `/login` flow prompt the user.

### 9.4 Custom provider adapter

Support later, but design config now:

```yaml
providers:
  custom:
    - id: aider
      display_name: Aider
      command: aider
      enabled: false
      default_mode: interactive
      login_command: null
      prompt_delivery: paste
```

---

## 10. Zellij Integration

Create `zellij.py` with a small wrapper around `zellij action`.

Required operations:

```python
class Zellij:
    def attach_or_create_background(self, session: str) -> None: ...
    def attach(self, session: str) -> None: ...
    def list_panes(self, session: str) -> list[dict]: ...
    def new_pane(self, session: str, name: str, cwd: str | None, command: list[str]) -> str: ...
    def paste(self, session: str, pane_id: str, text: str) -> None: ...
    def send_enter(self, session: str, pane_id: str) -> None: ...
    def focus_pane(self, session: str, pane_id: str) -> None: ...
    def rename_pane(self, session: str, pane_id: str, name: str) -> None: ...
    def set_pane_color(self, session: str, pane_id: str, fg: str | None = None, bg: str | None = None) -> None: ...
    def close_pane(self, session: str, pane_id: str) -> None: ...
    def dump_screen(self, session: str, pane_id: str, path: str) -> None: ...
```

Important implementation notes:

- Prefer `zellij --session <session> action ...`.
- If `new-pane` does not reliably return a pane ID, call `list-panes --json` before and after creation and infer the new pane by name/cwd/command.
- Do not assume pane IDs are stable across detached/restarted sessions. Refresh from `list-panes --json` when needed.
- Add `--dry-run` support globally so tests can verify commands without launching Zellij.

Initial pane launch pattern:

```bash
zellij --session ci-amplify-ai-archon action new-pane \
  --name dashboard \
  --cwd /home/user/ci_amplify_ai \
  -- bash -lc 'archon tui --inside-zellij'
```

Provider login pane pattern:

```bash
zellij --session ci-amplify-ai-archon action new-pane \
  --name codex-login \
  --cwd /home/user/ci_amplify_ai \
  -- bash -lc 'codex login'
```

Provider worker/task pane pattern:

```bash
zellij --session ci-amplify-ai-archon action new-pane \
  --name claude-feature-newButton4User \
  --cwd ../ci_amplify_ai-newButton4User \
  -- bash -lc 'ARCHON_TASK_ID=TASK-... ARCHON_TASK_RUN_ID=RUN-... ARCHON_PROVIDER_ID=claude claude -n claude-feature-newButton4User'
```

Then wait briefly and paste the prompt if the provider mode requires paste:

```bash
zellij --session ci-amplify-ai-archon action paste --pane-id terminal_8 "$PROMPT"
zellij --session ci-amplify-ai-archon action send-keys --pane-id terminal_8 Enter
```

---

## 11. Git Worktree Integration

Create `git_worktree.py`.

Required functions:

```python
def repo_root(path: str) -> Path: ...
def default_base_branch(repo: Path) -> str: ...
def sanitize_branch_component(value: str) -> str: ...
def create_feature_worktree(repo: Path, feature_name: str, branch: str | None, base: str, provider_id: str | None, variants: bool) -> WorktreeInfo: ...
def create_pr_review_worktree(repo: Path, pr_number: int, base: str, provider_id: str) -> WorktreeInfo: ...
def get_git_state(worktree: Path) -> GitState: ...
```

Worktree naming:

```text
../<repo-name>-pr-552-review-claude
../<repo-name>-pr-552-review-codex
../<repo-name>-newButton4User
../<repo-name>-newButton4User-claude
../<repo-name>-newButton4User-codex
../<repo-name>-TASK-20260707-001-claude
```

Branch naming:

```text
review/pr-552/claude
review/pr-552/codex
feature/newButton4User
feature/newButton4User/claude
feature/newButton4User/codex
agent/<task-id>/<provider>/<slug>
```

Safety rules:

- Never create or edit a task in the main repo checkout unless explicitly requested.
- If the target worktree already exists, detect whether it is safe to reuse.
- If dirty, do not delete or overwrite.
- Never force-delete worktrees without confirmation.
- Never let two providers write to the same worktree.

---

## 12. Provider Selection UX Requirements

### 12.1 First-run wizard

Implement in `provider_wizard.py`.

Requirements:

- Use `questionary.checkbox` if available.
- Fall back to a simple numbered prompt if TTY support is limited.
- Must work inside a Zellij pane.
- Must work outside Zellij too.
- Must allow CLI flags to bypass interactive prompts for automation.

Pseudo-code:

```python
def run_provider_wizard(repo: Path, existing_config: Config | None) -> Config:
    candidates = registry.known_providers()
    health = [check_provider(p) for p in candidates]

    selected = ask_checkbox(
        "Which AI coding CLIs do you want Archon to use?",
        choices=[
            Choice("Claude Code CLI (claude)", value="claude", checked=health["claude"].installed),
            Choice("OpenAI Codex CLI (codex)", value="codex", checked=health["codex"].installed),
            Choice("GitHub Copilot CLI (copilot)", value="copilot", checked=False),
            Choice("Custom provider", value="custom", checked=False),
        ],
    )

    launch_mode = ask_select(
        "Startup behavior",
        choices=["launch_now", "spawn_on_task"],
        default="launch_now",
    )

    return build_config(selected, launch_mode, health)
```

### 12.2 Per-task provider prompt

When multiple providers are enabled and the command lacks `--provider`, prompt.

Review tasks:

```text
Which provider(s) should review PR #552?
[x] claude
[x] codex
[ ] copilot
```

Feature tasks:

```text
Which provider should implement `newButton4User`?
( ) claude
( ) codex
( ) copilot
( ) Multiple providers as separate variants
```

Rules:

- PR review allows multi-select by default.
- Feature implementation defaults to single-select.
- Feature multi-select requires `--variants` or explicit confirmation.
- Non-interactive mode must fail with a helpful message if provider choice is ambiguous.

### 12.3 Initial dashboard pane

The initial dashboard pane must include provider controls:

```text
Keys:
  p  provider selector
  l  login selected provider
  r  refresh provider status
  n  new task
  q  quit dashboard
```

Provider selector inside dashboard:

```text
Provider Selector
[x] claude    installed=yes  auth=ready        mode=interactive
[x] codex     installed=yes  auth=ready        mode=exec
[ ] copilot   installed=yes  auth=needs_login  mode=interactive

Actions:
  Space toggle provider
  Enter save
  l login highlighted provider
  r refresh
```

---

## 13. TUI Dashboard

Implement a simple Rich-based dashboard first.

Command:

```bash
archon status --watch
```

Or:

```bash
archon tui
```

Provider table columns:

```text
Provider
Enabled
Installed
Auth
Mode
Command
Pane
Last checked
```

Task-run table columns:

```text
Task ID
Run ID
Name
Type
Provider
Status
Pane
Branch
Dirty
Ahead/Behind
Cost
Tokens
Context %
5h %
Last heartbeat
Worktree
```

Status colors:

```text
blocked       red
budget_capped red
stale         yellow
crashed       magenta
failed        red
running       blue
queued        white
starting      cyan
done          green
needs_login   yellow
missing       red
unknown       dim
```

Dashboard should sort task runs by urgency:

```text
blocked > budget_capped > stale > crashed > failed > running > queued > done
```

---

## 14. Budget and Rate-Limit Controls

MVP:

- Store `cost_usd`, token fields, `context_used_pct`, `rate_limit_five_hour_pct`, and `rate_limit_seven_day_pct` when available.
- Display whichever fields are available.
- Do not require all providers to expose the same telemetry.

V2:

```bash
archon feature newButton4User --soft-budget 2.00 --hard-budget 5.00
```

Behavior:

- Soft budget: mark warning, color orange/yellow, notify.
- Hard budget: send Ctrl+C or pause queue; do not close dirty worktree automatically.

Global rate-limit policy when telemetry exists:

```text
0-70% five-hour:     dispatch normally
70-85%:              prefer small tasks/reviews/tests
85-95%:              no new implementation agents
95-100%:             pause queue, alert human
100%:                stop dispatch until reset
```

---

## 15. Stale / Crash Detection

Implement later, but design now.

Signals:

1. `zellij list-panes --json`
2. last Archon statusline/provider heartbeat
3. transcript/stdout log mtime
4. hook/provider events
5. optional Zellij `dump-screen`

Rules:

```text
pane exited                   -> crashed
no heartbeat for 5 min        -> stale
no transcript/log growth 10m  -> maybe hung
permission prompt             -> blocked, not stale
login prompt                  -> needs_login, not stale
```

When stale:

- dump screen to `~/.local/share/archon/screens/<task-run-id>.txt`
- mark yellow
- notify human

---

## 16. Transcript Search

MVP:

```bash
archon search "auth.py"
archon search "newButton4User"
archon touched app/auth.py
```

Implementation:

- Use transcript paths from provider payloads when available.
- For exec providers, capture stdout/stderr logs.
- Tail/index JSONL transcript files or JSONL stdout when available.
- Insert text into `transcript_events` and `transcript_fts`.
- Extract file paths from tool use events where possible.

Search output:

```text
TASK-20260707-001 | RUN-20260707-001-codex | codex | app/components/UserButton.tsx
...relevant excerpt...
```

---

## 17. README Hero Image

Use the generated hero image as:

```text
docs/assets/archon-hero.png
```

README snippet:

```markdown
<p align="center">
  <img src="docs/assets/archon-hero.png" alt="Archon" width="100%" />
</p>

<h1 align="center">Archon</h1>

<p align="center">
  A Zellij-native command center for orchestrating parallel AI coding agents.
</p>
```

Do not put too much text on the image itself. The image should be the icon/banner; README text should carry the title/subtitle.

---

## 18. MVP Milestones

### Milestone 1 — Skeleton CLI, DB, and config

Deliver:

- `pyproject.toml`
- `archon --help`
- `archon init`
- `archon up --dry-run`
- SQLite schema creation
- config/path resolution

Acceptance:

```bash
archon init
archon status
archon up --dry-run --repo ~/ci_amplify_ai
```

Works without launching real Zellij.

### Milestone 2 — Provider registry and setup wizard

Deliver:

- known provider registry: `claude`, `codex`, `copilot`
- installed-command detection via `shutil.which`
- first-run provider selector
- config save/load
- `archon providers` and `archon providers doctor`

Acceptance:

```bash
archon setup --dry-run
archon providers
archon providers enable claude codex
```

shows/selects providers without launching real CLIs.

### Milestone 3 — Zellij pane orchestration

Deliver:

- `archon up`
- initial dashboard/setup pane
- dashboard/log/git panes
- provider login panes
- `zellij.py` wrapper
- pane discovery via `list-panes --json`

Acceptance:

```bash
archon up --repo ~/ci_amplify_ai --ask-providers
```

opens/attaches a Zellij session, shows provider selection, and creates panes after selection.

### Milestone 4 — Provider launch MVP

Deliver:

- Claude interactive launch adapter
- Codex exec launch adapter
- Copilot prompt/interactive launch adapter
- prompt delivery abstraction
- stdout/stderr capture for exec/prompt providers

Acceptance:

```bash
archon feature newButton4User --provider claude --dry-run
archon feature newButton4User --provider codex --dry-run
archon review-pr 552 --provider copilot --dry-run
```

prints correct provider-specific launch commands and worktree plans.

### Milestone 5 — PR review task

Deliver:

- `archon review-pr 552`
- per-provider PR-review worktree creation
- provider prompt selection if ambiguous
- provider launch
- prompt injection
- task/task-run state tracking

Acceptance:

```bash
archon review-pr 552 --repo ~/ci_amplify_ai --provider claude --provider codex
```

creates two worktrees, opens/launches two provider task runs, and injects PR-review prompts.

### Milestone 6 — Feature task

Deliver:

- `archon feature newButton4User`
- single-writer provider default
- `--variants` for multiple providers
- feature worktree creation
- provider launch
- prompt injection

Acceptance:

```bash
archon feature newButton4User --repo ~/ci_amplify_ai --provider claude
archon feature newButton4User --repo ~/ci_amplify_ai --provider claude --provider codex --variants
```

creates isolated branches/worktrees and launches the correct providers.

### Milestone 7 — Statusline, hooks, and normalized events

Deliver:

- `archon statusline`
- `archon hook`
- example Claude settings
- JSONL/stdout parser for Codex
- stdout parser placeholder for Copilot
- task telemetry updates
- blocked-state attention routing

Acceptance:

- statusline JSON updates task-run cost/context/session/transcript metadata
- provider JSONL events are stored
- permission hook marks a task run blocked and focuses/colors pane

### Milestone 8 — Transcript search

Deliver:

- transcript indexer
- stdout/stderr log indexer
- `archon search`
- `archon touched`

Acceptance:

```bash
archon search "auth.py"
```

finds transcript/log events that mention or touched `auth.py`.

---

## 19. Testing Strategy

Use tests with mocked subprocess calls.

Required tests:

- DB schema initializes cleanly
- provider config saves/loads
- provider registry returns known providers
- provider install detection uses `shutil.which`
- provider wizard returns selected providers
- ambiguous provider selection prompts when multiple providers are enabled
- non-interactive ambiguous provider selection fails with a helpful message
- task IDs and task-run IDs are unique and stable
- branch/worktree names sanitize weird input
- multi-provider PR review creates one worktree per provider
- multi-provider feature without `--variants` refuses or asks confirmation
- PR prompt contains expected safety rules
- feature prompt contains expected safety rules
- Zellij command builder produces expected commands
- provider launch builders produce expected commands for Claude/Codex/Copilot
- `--dry-run` does not call real Zellij/Git/provider CLIs
- statusline handler tolerates missing/null fields
- hook handler tolerates malformed JSON
- permission hook sets task-run status to blocked

Add a smoke test mode:

```bash
ARCHON_DRY_RUN=1 archon review-pr 552 --repo /fake/repo --provider codex
```

It should print the commands it would run and create a fake task/task-run record, but not call external tools.

---

## 20. Safety Requirements

Archon must never do these without explicit human confirmation:

- merge a branch
- push a branch
- submit a GitHub review
- approve a PR
- request changes on a PR
- delete a dirty worktree
- force-reset a branch
- run broad destructive shell commands
- let two provider runs write to the same worktree
- auto-submit external provider output as final without human review

Prompt all providers to stop before PR creation or external submission unless the human explicitly asks.

---

## 21. Good Default Prompts

### PR Review Prompt Template

```text
You are reviewing PR #{pr_number} in {repo_name}.

Provider: {provider_name}
Worktree: {worktree_path}
Branch: {branch}

Rules:
- Do not modify files unless explicitly asked.
- Do not submit a GitHub review until the human approves.
- Review the PR diff against the base branch.
- Use gh pr view #{pr_number} and gh pr diff #{pr_number} as needed.
- Inspect changed files directly.
- Run focused tests only when helpful.

Look for:
- correctness bugs
- auth or data-access mistakes
- security issues
- broken types
- bad generated code
- missing tests
- regressions
- maintainability issues

Produce:
1. executive summary
2. must-fix issues
3. nice-to-fix issues
4. tests run and results
5. suggested GitHub review comments
6. final recommendation: approve / comment / request changes
```

### Feature Prompt Template

```text
Implement feature `{feature_name}` in {repo_name}.

Provider: {provider_name}
Worktree: {worktree_path}
Branch: {branch}

Rules:
- Work only in this branch/worktree.
- Keep the diff focused and minimal.
- First inspect the project structure before editing.
- Find the correct frontend/backend locations before making changes.
- Follow nearby code patterns.
- Add or update tests if the repo has a nearby test pattern.
- Run the smallest useful validation commands.
- Do not create a PR until the human approves.

Feature request:
{feature_description}

At the end, summarize:
1. files changed
2. behavior added
3. tests run
4. risks / follow-up questions
5. exact commands the human should run next
```

### Provider Comparison Prompt

Use when multiple providers reviewed the same PR or produced multiple feature variants.

```text
Compare the outputs from these provider runs:

{provider_run_summaries}

Produce:
1. agreements
2. disagreements
3. highest-confidence issues
4. suspicious or low-quality findings
5. recommended next action

Do not merge, push, submit a review, or delete worktrees.
```

---

## 22. Done Definition for MVP

The MVP is done when this exact demo works:

```bash
cd ~/ci_amplify_ai
archon up
# first run shows provider selector in the initial pane
# select Claude and Codex
archon review-pr 552
# because multiple providers are enabled, Archon asks which providers should review
# select Claude and Codex
archon feature newButton4User
# because multiple providers are enabled, Archon asks which single provider should implement
# select Claude
archon status --watch
```

And the result is:

- Zellij session opens automatically.
- Initial pane shows provider selection on first run.
- Selected providers are saved to config.
- If provider CLIs are already logged in, they launch right away.
- If a selected CLI is not logged in, Archon opens a login pane and lets the native CLI prompt the user.
- Dashboard pane exists.
- Provider readiness table exists.
- PR review task has separate provider-specific worktrees and panes/runs.
- Feature task has its own worktree and pane/run.
- At least Claude interactive and Codex exec paths work in dry-run and real modes.
- Copilot provider can be enabled and launched if installed.
- Archon injects the correct prompt into each provider pane/run.
- Statusline and hook commands can ingest JSON without crashing.
- Dashboard shows task states and basic telemetry.
- No manual pane creation is required.

---

## 23. Future Enhancements

Do not build these until MVP is reliable:

- true task queue and idle worker pool
- side-by-side provider output comparison pane
- automatic reviewer/tester handoff after feature completion
- Zellij native WASM plugin
- Zellij pipe integration
- GitHub review comment drafting and submission workflow
- auto-restart stale panes
- global budget/rate-limit scheduler
- multi-repo workspaces
- web UI
- MCP integration
- skill packs / prompt packs
- branch diff summarizer
- custom provider marketplace
- provider performance analytics by task type

---

## 24. Implementation Notes for Codex

Build this incrementally. Do not attempt the entire end-state at once.

Recommended order:

1. Create project skeleton.
2. Implement DB and config.
3. Implement dry-run command execution.
4. Implement provider registry and provider config.
5. Implement provider-selection wizard.
6. Implement provider login panes.
7. Implement Git worktree helpers.
8. Implement prompt templates.
9. Implement Zellij wrapper.
10. Implement `archon up` with initial provider pane.
11. Implement Claude provider launch.
12. Implement Codex provider launch.
13. Implement Copilot provider launch.
14. Implement `archon review-pr`.
15. Implement `archon feature`.
16. Implement `archon status` / `archon tui`.
17. Implement `archon statusline`.
18. Implement `archon hook`.
19. Add tests.
20. Polish README.

Bias toward clear, boring, debuggable code.

Use logging liberally. Every external command should be logged to `events.jsonl` in non-secret form.

---

## 25. Official Docs To Check While Implementing

These links are implementation references. Re-check local CLI help too, because user-installed versions may differ.

- OpenAI Codex CLI auth: https://developers.openai.com/codex/auth
- OpenAI Codex CLI command reference: https://developers.openai.com/codex/cli/reference
- OpenAI Codex non-interactive mode: https://developers.openai.com/codex/noninteractive
- GitHub Copilot CLI authentication: https://docs.github.com/en/copilot/how-tos/copilot-cli/set-up-copilot-cli/authenticate-copilot-cli
- GitHub Copilot CLI programmatic usage: https://docs.github.com/en/copilot/how-tos/copilot-cli/automate-copilot-cli/run-cli-programmatically
- GitHub Copilot CLI command reference: https://docs.github.com/en/copilot/reference/copilot-cli-reference/cli-command-reference
