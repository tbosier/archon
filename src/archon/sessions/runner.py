"""Background process used by ``archon`` to run one provider prompt."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

from ..paths import resolve_paths
from ..util import atomic_write_json, utc_now


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--provider", required=True, choices=("claude", "codex", "copilot"))
    parser.add_argument("--cwd", required=True)
    parser.add_argument("--prompt", required=True)
    args = parser.parse_args(argv)

    paths = resolve_paths().ensure()
    state_path = paths.sessions_dir / f"{args.session_id}.json"
    out_path = paths.sessions_dir / f"{args.session_id}.out.log"
    err_path = paths.sessions_dir / f"{args.session_id}.err.log"

    command = provider_command(args.provider, args.prompt)
    _write_state(
        state_path,
        session_id=args.session_id,
        provider=args.provider,
        cwd=args.cwd,
        prompt=args.prompt,
        argv=command,
        out_path=str(out_path),
        err_path=str(err_path),
        status="running",
    )
    try:
        with out_path.open("ab") as out, err_path.open("ab") as err:
            proc = subprocess.Popen(command, cwd=args.cwd, stdout=out, stderr=err, env=os.environ.copy())
            _patch_state(state_path, provider_pid=proc.pid)
            code = proc.wait()
    except FileNotFoundError:
        _patch_state(state_path, status="failed", exit_code=127, summary=f"{args.provider} command not found")
        return 127
    except Exception as exc:
        _patch_state(state_path, status="failed", exit_code=1, summary=str(exc))
        return 1

    _patch_state(
        state_path,
        status=("completed" if code == 0 else "failed"),
        exit_code=code,
        summary=_tail_text(out_path) or _tail_text(err_path),
        **_usage_from_output(out_path),
    )
    return int(code)


def provider_command(provider: str, prompt: str) -> list[str]:
    if provider == "claude":
        # Claude's native background mode is excellent when available, but this
        # runner needs a process it can track uniformly, so use print mode here.
        return ["claude", "-p", prompt]
    if provider == "codex":
        return ["codex", "exec", "--json", "--sandbox", "workspace-write", prompt]
    if provider == "copilot":
        return ["copilot", "-p", prompt]
    raise ValueError(provider)


def _base_state(**values) -> dict:
    now = utc_now()
    prompt = str(values["prompt"])
    return {
        "session_id": values["session_id"],
        "provider": values["provider"],
        "pid": os.getpid(),
        "provider_pid": None,
        "cwd": values["cwd"],
        "title": prompt[:60],
        "summary": "starting",
        "prompt": prompt,
        "argv": values["argv"],
        "out_path": values["out_path"],
        "err_path": values["err_path"],
        "status": values["status"],
        "started_at": now,
        "updated_at": now,
        "exit_code": None,
        "cost_usd": None,
        "total_tokens": None,
    }


def _write_state(path: Path, **values) -> None:
    atomic_write_json(path, _base_state(**values))


def _patch_state(path: Path, **updates) -> None:
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        state = {}
    state.update(updates)
    state["updated_at"] = utc_now()
    atomic_write_json(path, state)


def _tail_text(path: Path, *, max_chars: int = 500) -> str | None:
    try:
        text = path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return None
    if not text:
        return None
    return text[-max_chars:].replace("\n", " ")[:160]


def _usage_from_output(path: Path) -> dict:
    usage: dict[str, float | int] = {}
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return usage
    for line in lines[-200:]:
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(rec, dict):
            continue
        payload = rec.get("usage") if isinstance(rec.get("usage"), dict) else rec
        total = payload.get("total_tokens") or payload.get("total")
        cost = payload.get("cost_usd") or payload.get("total_cost_usd")
        if total is not None:
            try:
                usage["total_tokens"] = int(total)
            except (TypeError, ValueError):
                pass
        if cost is not None:
            try:
                usage["cost_usd"] = float(cost)
            except (TypeError, ValueError):
                pass
    return usage


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main(sys.argv[1:]))
