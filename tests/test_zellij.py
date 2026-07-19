"""Tests for the Zellij wrapper (spec §10)."""

from __future__ import annotations

import json
from unittest import mock


from archon.zellij import Zellij, build_new_pane_argv


# --- argv builder ---------------------------------------------------------


def test_build_new_pane_argv_full():
    argv = build_new_pane_argv(
        "ci-amplify-ai-archon",
        "claude-feature-newButton4User",
        "/home/user/ci_amplify_ai-newButton4User",
        ["bash", "-lc", "claude -n claude-feature"],
    )
    assert argv == [
        "zellij",
        "--session",
        "ci-amplify-ai-archon",
        "action",
        "new-pane",
        "--name",
        "claude-feature-newButton4User",
        "--cwd",
        "/home/user/ci_amplify_ai-newButton4User",
        "--",
        "bash",
        "-lc",
        "claude -n claude-feature",
    ]


def test_build_new_pane_argv_no_cwd():
    argv = build_new_pane_argv("s", "login", None, ["bash", "-lc", "codex login"])
    assert "--cwd" not in argv
    assert argv[:7] == ["zellij", "--session", "s", "action", "new-pane", "--name", "login"]
    assert argv[-4:] == ["--", "bash", "-lc", "codex login"]


def test_build_new_pane_argv_empty_command():
    argv = build_new_pane_argv("s", "pane", None, [])
    assert "--" not in argv


# --- dry-run records commands, never executes -----------------------------


def test_dry_run_records_and_never_calls_subprocess():
    z = Zellij(dry_run=True)
    with mock.patch("archon.zellij.subprocess.run") as run:
        pane = z.new_pane("sess", "dashboard", "/repo", ["bash", "-lc", "archon tui"])
        z.attach_or_create_background("sess")
        z.paste("sess", "terminal_1", "hello")
        z.send_enter("sess", "terminal_1")
        z.focus_pane("sess", "terminal_1")
    run.assert_not_called()
    # new_pane returns None in dry-run (no live session to diff).
    assert pane is None
    # The new-pane argv (and the pre-creation list-panes) were recorded.
    assert build_new_pane_argv("sess", "dashboard", "/repo", ["bash", "-lc", "archon tui"]) in z.commands
    # In dry-run we probe list-panes before creation, then short-circuit.
    list_calls = [c for c in z.commands if "list-panes" in c]
    assert len(list_calls) == 1


def test_real_run_strips_surrounding_zellij_env(monkeypatch):
    monkeypatch.setenv("ZELLIJ", "0")
    monkeypatch.setenv("ZELLIJ_SESSION_NAME", "wrong-session")
    monkeypatch.setenv("ZELLIJ_PANE_ID", "99")
    z = Zellij(dry_run=False)
    with mock.patch("archon.zellij.subprocess.run", return_value=_completed("")) as run:
        z.attach_or_create_background("archon")
    kwargs = run.call_args.kwargs
    assert "ZELLIJ" not in kwargs["env"]
    assert "ZELLIJ_SESSION_NAME" not in kwargs["env"]
    assert "ZELLIJ_PANE_ID" not in kwargs["env"]
    assert run.call_args.args[0] == [
        "zellij", "attach", "--create-background", "--forget", "archon",
    ]


def test_dry_run_new_pane_argv_content():
    z = Zellij(dry_run=True)
    z.new_pane("s", "codex-login", "/repo", ["bash", "-lc", "codex login"])
    new_pane_cmds = [c for c in z.commands if "new-pane" in c]
    assert new_pane_cmds == [build_new_pane_argv("s", "codex-login", "/repo", ["bash", "-lc", "codex login"])]


# --- interaction argv shapes ---------------------------------------------


def test_paste_and_enter_argv():
    z = Zellij(dry_run=True)
    z.paste("s", "terminal_8", "PROMPT")
    z.send_enter("s", "terminal_8")
    assert z.commands[0][:4] == ["zellij", "--session", "s", "action"]
    assert "write-chars" in z.commands[0]
    assert "terminal_8" in z.commands[0]
    assert "PROMPT" in z.commands[0]
    assert "write" in z.commands[1]


def test_close_and_dump_screen_argv():
    z = Zellij(dry_run=True)
    z.close_pane("s", "terminal_2")
    z.dump_screen("s", "terminal_2", "/tmp/screen.txt")
    close = [c for c in z.commands if "close-pane" in c][0]
    assert "--pane-id" in close
    assert "terminal_2" in close
    dump = [c for c in z.commands if "dump-screen" in c][0]
    assert "/tmp/screen.txt" in dump


# --- list_panes parsing ---------------------------------------------------


def _completed(stdout: str, returncode: int = 0):
    proc = mock.Mock()
    proc.stdout = stdout
    proc.returncode = returncode
    return proc


def test_list_panes_parses_tab_keyed_json():
    z = Zellij(dry_run=False)
    payload = json.dumps(
        {
            "Tab #1": [
                {"id": "terminal_1", "title": "dashboard", "cwd": "/repo"},
                {"id": "terminal_2", "title": "logs"},
            ]
        }
    )
    with mock.patch("archon.zellij.subprocess.run", return_value=_completed(payload)):
        panes = z.list_panes("s")
    assert len(panes) == 2
    assert panes[0]["id"] == "terminal_1"
    assert panes[0]["tab"] == "Tab #1"


def test_list_panes_parses_ndjson():
    z = Zellij(dry_run=False)
    payload = "\n".join(
        [
            json.dumps({"id": "terminal_1", "title": "a"}),
            json.dumps({"id": "terminal_2", "title": "b"}),
        ]
    )
    with mock.patch("archon.zellij.subprocess.run", return_value=_completed(payload)):
        panes = z.list_panes("s")
    assert [p["id"] for p in panes] == ["terminal_1", "terminal_2"]


def test_list_panes_returns_empty_on_failure():
    z = Zellij(dry_run=False)
    with mock.patch("archon.zellij.subprocess.run", return_value=_completed("", returncode=1)):
        assert z.list_panes("s") == []


def test_list_panes_returns_empty_on_garbage():
    z = Zellij(dry_run=False)
    with mock.patch("archon.zellij.subprocess.run", return_value=_completed("not json")):
        assert z.list_panes("s") == []


# --- new_pane infers id by diffing list_panes -----------------------------


def test_new_pane_infers_id_by_diff():
    z = Zellij(dry_run=False)
    before = json.dumps({"t": [{"id": "terminal_1", "title": "dashboard"}]})
    after = json.dumps(
        {
            "t": [
                {"id": "terminal_1", "title": "dashboard"},
                {"id": "terminal_5", "title": "claude-feature-x", "cwd": "/wt"},
            ]
        }
    )
    # Calls: list_panes(before), new-pane (no capture), list_panes(after).
    side_effects = [
        _completed(before),   # before list-panes
        _completed(""),       # new-pane itself
        _completed(after),    # after list-panes
    ]
    with mock.patch("archon.zellij.subprocess.run", side_effect=side_effects):
        pane_id = z.new_pane("s", "claude-feature-x", "/wt", ["bash", "-lc", "claude"])
    assert pane_id == "terminal_5"


def test_new_pane_returns_none_when_unresolvable():
    z = Zellij(dry_run=False)
    same = json.dumps({"t": [{"id": "terminal_1", "title": "dashboard"}]})
    with mock.patch(
        "archon.zellij.subprocess.run",
        side_effect=[_completed(same), _completed(""), _completed(same)],
    ):
        pane_id = z.new_pane("s", "brand-new", "/wt", ["bash"])
    assert pane_id is None


# --- failures never crash the caller --------------------------------------


def test_subprocess_oserror_is_swallowed():
    z = Zellij(dry_run=False)
    with mock.patch("archon.zellij.subprocess.run", side_effect=OSError("boom")):
        # None of these should raise.
        z.attach_or_create_background("s")
        assert z.list_panes("s") == []
        z.paste("s", "terminal_1", "x")
