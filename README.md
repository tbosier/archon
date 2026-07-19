# Archon

Archon runs Claude Code, Codex, and GitHub Copilot sessions from one terminal.
It gives you a single command center for starting work, seeing which agents need
help, and opening any session you started through Archon.

Each agent runs in a held terminal session. Pressing `Enter` opens that terminal.
Pressing `Left Arrow` returns to Archon without stopping the agent. Opening it
again takes you back to the same conversation. Archon does not create another
Zellij tab for each agent.

## Requirements

- Python 3.11 or newer
- At least one supported provider CLI installed and signed in
- Claude Code, Codex, or GitHub Copilot CLI

Archon uses the provider CLIs already installed on your machine. It does not ask
for or store provider API keys.

Check what Archon can find:

```bash
archon providers doctor
```

To see the native login command for a provider:

```bash
archon providers login claude
archon providers login codex
archon providers login copilot
```

## Install

From this repository, install the command with `uv`:

```bash
uv tool install --editable .
archon setup
```

The editable install means changes in this checkout are available without
reinstalling the package.

For local development:

```bash
uv venv .venv
uv pip install --python .venv/bin/python -e ".[dev]"
.venv/bin/python -m pytest
```

## Start Archon

Run either command:

```bash
archon
archon agents
```

Type a task in the command box and finish it with the provider you want:

```text
fix the auth race condition --claude
review the cache layer for correctness --codex
try a second implementation --copilot
```

You can also leave off the provider. Archon will ask which one to use before it
starts the session.

```text
fix the auth race condition
```

An agent starts as soon as the task is submitted. You do not need to open its
terminal for it to keep working.

## Controls

| Key | Action |
| --- | --- |
| `Up Arrow` | Move from the command box to the session list |
| `Enter` | Open the selected agent terminal |
| `Left Arrow` | Return from an agent terminal without stopping it |
| `s` | Stop the selected Archon session |
| `r` | Start the original task again with the same provider |
| `f` | Forget a completed or failed Archon session |
| `Ctrl+R` | Refresh the session list |
| `q` | Quit Archon |

## Dashboard

`archon-dash` is a read-only view of the same session registry:

```bash
archon-dash
```

It shows session state, current work, age, token usage, AI credits, and cost when
the provider makes that information available. Usage data is not identical
across providers, so Archon displays the values each CLI records instead of
estimating missing costs.

## Session Discovery

Archon tracks sessions it starts and also discovers provider sessions from the
local state written by Claude Code, Codex, and Copilot. This lets the command
center show work that was started outside Archon too.

Externally discovered sessions can be listed and monitored, but Archon cannot
always open or control them. Held terminal access is guaranteed for sessions
started through Archon.

Archon stores its own data in `~/.local/share/archon` and configuration in
`~/.config/archon`. Set `ARCHON_HOME` or `ARCHON_CONFIG_HOME` to override those
locations.

## Status

The current test suite has 407 passing tests and 9 skipped tests.
