# Agent Deck CLI Contract

Captured on 2026-07-11 from:

```text
Agent Deck v1.9.73
```

Archon v2 uses Agent Deck as the worker execution backend. Archon does not use
Agent Deck's conductor.

## Launch

Command checked:

```bash
agent-deck launch --help
```

Relevant contract:

```text
Usage: agent-deck launch [path] [options]

Create, start, and optionally send a message to a new session in one step.
Combines: add + session start + session send

Arguments:
  [path]    Project directory (defaults to current directory)

Options:
  -b
        Create new branch (use with --worktree)
  -c string
        Tool/command to run (short)
  -cmd string
        Tool/command to run (e.g., 'claude' or 'codex --dangerously-bypass-approvals-and-sandbox')
  -json
        Output as JSON
  -m string
        Initial message to send (short)
  -message string
        Initial message to send once agent is ready
  -model string
        Model ID/version to use for this session (claude, codex, gemini, opencode)
  -new-branch
        Create new branch
  -no-parent
        Disable automatic parent linking
  -p string
        Parent session (short)
  -parent string
        Parent session (creates sub-session, inherits group)
  -t string
        Session title (short)
  -title string
        Session title (defaults to folder name)
  -title-lock
        Lock session title so Claude's session name never overrides it (#697)
  -w string
        Create session in git worktree for branch
  -worktree string
        Create session in git worktree for branch
```

Archon currently calls:

```text
agent-deck launch <path> --title <title> --title-lock --no-parent --json -c <tool> --message <prompt> [--model <model>]
```

When Archon delegates worktree creation to Agent Deck, add:

```text
--worktree <branch> --new-branch
```

## Session

Command checked:

```bash
agent-deck session --help
```

Relevant contract:

```text
Usage: agent-deck session <command> [options]

Commands:
  start <id>              Start a session's tmux process
  stop <id>               Stop/kill session process
  attach <id>             Attach to session interactively
  show [id]               Show session details (auto-detect current if no id)
  send <id> <message>     Send a message to a running session
  output <id>             Get the last response from a session

Global Options:
  --json                 Output as JSON
  -q, --quiet            Minimal output (exit codes only)
```

Additional checked subcommands:

```text
agent-deck session show [id|title] --json
agent-deck session output [id|title] --json
agent-deck list --json
```

`agent-deck list --json` returned a JSON array with objects shaped like:

```json
{
  "id": "eb454df3-1782692133",
  "title": "testADeck",
  "path": "/home/taylo/projects/testADeck",
  "group": "projects",
  "tool": "claude",
  "command": "claude",
  "status": "error",
  "tmux_session": "agentdeck_testADeck_7066096d",
  "profile": "default",
  "created_at": "2026-06-28T19:15:33-05:00"
}
```

## Known Tech Debt

M1 preserves Archon's existing worktree creation path and launches Agent Deck
against the prepared worktree directory. The spec's final target is for Agent
Deck to own worktree creation via `--worktree <branch> --new-branch`; moving
prompt construction away from precomputed worktree paths is the remaining
integration step.
