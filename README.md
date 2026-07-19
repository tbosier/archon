# Archon

One terminal view for coding agents.

Archon is a provider-neutral agent cockpit for Claude, Codex, Copilot, and other
coding CLIs. It is aimed at the gap left by provider-specific tools: Claude has
`claude agents`, Codex has its own CLI/app surfaces, and Copilot has its own
workflow. Archon puts them in one TUI.

## Core Flow

Run Archon:

```bash
archon
```

Type a task in the command box and choose a provider with a trailing flag:

```text
fix the auth race condition --claude
review the cache layer for correctness --codex
try a second implementation approach --copilot
```

If you leave off the provider flag, Archon prompts inside the TUI:

```text
fix the auth race condition
```

The dashboard shows all known sessions in one list:

- running
- needs assistance
- completed
- failed or disconnected

Archon discovers provider-native sessions where possible and also tracks sessions
it launches itself under its own data directory.

The command center stays focused on the session list. Open a session to see its
actual held terminal and complete conversation:

- `Up` from the command box moves into the session list
- `Enter` opens the selected agent in the same terminal
- `Left` detaches back to the command center without stopping the agent
- opening that session again resumes the same held terminal and conversation
- `s` stops the selected run
- `r` reruns the original prompt with the same provider
- `f` forgets a completed/failed run
- `Ctrl+R` refreshes immediately

## Commands

```bash
archon          # open the unified agent view
archon agents   # same view, explicit command
archon-dash     # read-only live dashboard
archon setup    # configure providers
archon providers doctor
```

Legacy orchestration commands still exist during the transition, but they are no
longer the product surface.

## Development

Use `uv`:

```bash
uv venv .venv
uv pip install -e .
.venv/bin/python -m pytest
```

Current verified status:

```text
407 passed, 9 skipped
```
