# Contributing

Open an issue before starting a large change so the scope can be agreed on.
Small bug fixes can go directly to a pull request.

## Development setup

```bash
uv venv .venv
uv sync --extra dev
```

Run the same checks used by CI:

```bash
uv run ruff check .
uv run pytest
uv build
```

Keep changes focused. Add tests for behavior changes and do not include API
keys, access tokens, provider transcripts, or local Archon state in commits.

All changes to `main` go through a pull request and must pass the required CI
checks.
