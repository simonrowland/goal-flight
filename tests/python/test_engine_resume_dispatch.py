#!/usr/bin/env python3
"""Resume wiring for every worker CLI, not just Codex."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import goalflight_dispatch as D  # noqa: E402
import goalflight_engine_sessions as E  # noqa: E402
import goalflight_ledger as L  # noqa: E402


GROK_SESSION = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
KIMI_SESSION = "session_bbbbbbbb-cccc-4ddd-8eee-ffffffffffff"
CURSOR_SESSION = "0123456789abcdef0123456789abcdef"
CLAUDE_SESSION = "cccccccc-dddd-4eee-8fff-000000000000"


pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="worker-CLI resume is local POSIX-only",
)


@pytest.fixture(autouse=True)
def _isolated_state(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    state = tmp_path / "state"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("GOALFLIGHT_STATE_DIR", str(state))
    monkeypatch.setenv("GOALFLIGHT_CODEX_STATE_DIR", str(state))
    monkeypatch.setenv("GOALFLIGHT_TASK_STORE_DIR", str(tmp_path / "task-store"))
    monkeypatch.setenv("GOALFLIGHT_CAPACITY_CONF", "/dev/null")
    monkeypatch.setenv("GOALFLIGHT_CAPACITY_WAIT_S", "0")
    monkeypatch.setenv("GOALFLIGHT_DISABLE_NUDGES", "1")


def _write_parent(
    tmp_path: Path,
    *,
    dispatch_id: str,
    agent: str,
    engine: str,
    session_id: str | None,
    state: str = "blocked",
) -> dict:
    status_path = tmp_path / f"{dispatch_id}.status.json"
    record = {
        "schema": L.SCHEMA,
        "dispatch_id": dispatch_id,
        "agent": agent,
        "engine": engine,
        "shape": "bash",
        "account": "default",
        "transport": "dispatch",
        "project_root": str(tmp_path),
        "worker_cwd": str(tmp_path),
        "status_path": str(status_path),
        "state": state,
        "terminal_state": "blocked" if state == "blocked" else "unknown",
        "started_at": L.utc_now(),
        "task_ids": ["t-288"],
    }
    if session_id is not None:
        record["engine_session_id"] = session_id
    L.write_record(record)
    return record


def _grok_args(**overrides):
    base = dict(
        agent="grok-code",
        cwd="/tmp/x",
        model=None,
        os_sandbox=None,
        read_only=False,
        parent_dispatch_id=None,
        engine_session_id=None,
        codex_session_id=None,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def test_fork_policy_is_reuse() -> None:
    assert E.RESUME_FORK_POLICY == "reuse"
    assert "grok" in E.FORK_CAPABLE
    assert "claude" in E.FORK_CAPABLE


def test_session_argv_never_forks_or_continues() -> None:
    grok_resume = E.session_argv("grok", GROK_SESSION, resume=True)
    grok_new = E.session_argv("grok", GROK_SESSION, resume=False)
    claude_resume = E.session_argv("claude", CLAUDE_SESSION, resume=True)
    for argv in (grok_resume, grok_new, claude_resume):
        joined = " ".join(argv)
        assert "--fork-session" not in argv
        assert "--continue" not in argv
        assert "-c" not in argv
        assert joined
    assert grok_resume == ["--resume", GROK_SESSION]
    assert grok_new == ["--session-id", GROK_SESSION]
    assert E.session_argv("moonshot", KIMI_SESSION, resume=True) == [
        "-S",
        KIMI_SESSION,
    ]
    assert E.session_argv("cursor", CURSOR_SESSION, resume=True) == [
        "--resume",
        CURSOR_SESSION,
    ]


def test_grok_launch_assigns_session_id_without_parent() -> None:
    prompt = "/tmp/p.md"
    args = _grok_args(engine_session_id=GROK_SESSION)
    argv, stdin_path = D.build_worker(args, prompt, [])
    assert argv[:3] == ["grok", "--prompt-file", prompt]
    assert "--session-id" in argv
    assert argv[argv.index("--session-id") + 1] == GROK_SESSION
    assert "--resume" not in argv
    assert "--fork-session" not in argv
    assert stdin_path is None


def test_grok_resume_argv_reuses_handle() -> None:
    prompt = "/tmp/p.md"
    args = _grok_args(
        engine_session_id=GROK_SESSION,
        parent_dispatch_id="parent-dispatch",
    )
    argv, _ = D.build_worker(args, prompt, [])
    assert argv[argv.index("--resume") + 1] == GROK_SESSION
    assert "--session-id" not in argv
    assert "--fork-session" not in argv


def test_kimi_resume_argv_passes_dash_s(tmp_path: Path) -> None:
    prompt = tmp_path / "p.md"
    prompt.write_text("continue\n", encoding="utf-8")
    args = SimpleNamespace(
        agent="moonshot",
        cwd="/tmp/x",
        model=None,
        parent_dispatch_id="parent-dispatch",
        engine_session_id=KIMI_SESSION,
        codex_session_id=None,
    )
    argv, _ = D.build_worker(args, str(prompt), [])
    assert "-S" in argv
    assert argv[argv.index("-S") + 1] == KIMI_SESSION


def test_cursor_and_claude_resume_argv() -> None:
    prompt = "/tmp/p.md"
    cursor = SimpleNamespace(
        agent="cursor-agent",
        cwd="/tmp/x",
        model=None,
        parent_dispatch_id="parent",
        engine_session_id=CURSOR_SESSION,
        codex_session_id=None,
    )
    argv, stdin_path = D.build_worker(cursor, prompt, [])
    assert argv[0] == "cursor-agent"
    assert argv[argv.index("--resume") + 1] == CURSOR_SESSION
    assert "--force" in argv
    assert "--trust" in argv
    assert stdin_path == prompt

    claude = SimpleNamespace(
        agent="claude",
        cwd="/tmp/x",
        model=None,
        parent_dispatch_id="parent",
        engine_session_id=CLAUDE_SESSION,
        codex_session_id=None,
    )
    argv, stdin_path = D.build_worker(claude, prompt, [])
    assert argv[0] == "claude"
    assert argv[argv.index("--resume") + 1] == CLAUDE_SESSION
    assert "--fork-session" not in argv
    assert stdin_path == prompt


def test_resume_refuses_grok_without_recorded_handle(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent_id = "grok-no-handle"
    _write_parent(
        tmp_path,
        dispatch_id=parent_id,
        agent="grok-code",
        engine="grok",
        session_id=None,
    )
    prompt = tmp_path / "brief.md"
    prompt.write_text("continue.\n", encoding="utf-8")
    monkeypatch.setattr(
        D,
        "_reserve_auto_dispatch_id",
        lambda *_a, **_k: pytest.fail("missing handle must not allocate a child"),
    )
    rc = D.main(["resume", parent_id, "--prompt-file", str(prompt)])
    assert rc == 64
    assert capsys.readouterr().err == (
        "goalflight_dispatch: dispatch grok-no-handle has no recorded "
        "grok session handle\n"
    )


def test_resume_verb_passes_grok_lineage(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    parent_id = "grok-parent"
    _write_parent(
        tmp_path,
        dispatch_id=parent_id,
        agent="grok-code",
        engine="grok",
        session_id=GROK_SESSION,
    )
    prompt = tmp_path / "brief.md"
    prompt.write_text("continue.\n", encoding="utf-8")
    captured: list[list[str]] = []
    monkeypatch.setattr(
        D,
        "_reserve_auto_dispatch_id",
        lambda agent, _base: f"{agent}-child",
    )
    monkeypatch.setattr(
        D,
        "main",
        lambda argv=None: captured.append(list(argv or [])) or 0,
    )
    assert D._cmd_resume(
        [parent_id, "--prompt-file", str(prompt), "--unregistered-forced"]
    ) == 0
    launch = captured[0]
    assert launch[launch.index("--agent") + 1] == "grok-code"
    assert launch[launch.index("--parent-dispatch-id") + 1] == parent_id
    assert launch[launch.index("--engine-session-id") + 1] == GROK_SESSION
    assert "--unregistered-forced" in launch
    assert "--fork-session" not in launch
    assert "--codex-resume-home" not in launch


def test_resume_honors_explicit_grok_account(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    parent_id = "grok-quota-parent"
    worktree = tmp_path / "b-3363"
    worktree.mkdir()
    record = _write_parent(
        tmp_path,
        dispatch_id=parent_id,
        agent="grok-code",
        engine="grok",
        session_id=GROK_SESSION,
        state="quota_exhausted",
    )
    record.update(
        {
            "worker_cwd": str(worktree),
            "effective_account": "cf9f50",
            "account": "cf9f50",
            "terminal_state": "quota_exhausted",
            "dispatch_argv": [
                "--agent",
                "grok-code",
                "--cwd",
                str(worktree),
                "--worktree",
                "HEAD",
                "--account",
                "cf9f50",
            ],
        }
    )
    L.write_record(record)
    prompt = tmp_path / "brief.md"
    prompt.write_text("continue on a live seat.\n", encoding="utf-8")
    captured: list[list[str]] = []
    monkeypatch.setattr(
        D,
        "_reserve_auto_dispatch_id",
        lambda agent, _base: f"{agent}-child",
    )
    monkeypatch.setattr(
        D,
        "main",
        lambda argv=None: captured.append(list(argv or [])) or 0,
    )
    assert (
        D._cmd_resume(
            [
                parent_id,
                "--prompt-file",
                str(prompt),
                "--unregistered-forced",
                "--account",
                "d78343",
            ]
        )
        == 0
    )
    launch = captured[0]
    assert launch[launch.index("--account") + 1] == "d78343"
    assert "--worktree" not in launch
    assert Path(launch[launch.index("--cwd") + 1]).resolve() == worktree.resolve()


def test_grok_default_selection_skips_recently_exhausted_seat(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    accounts = tmp_path / "home" / ".goal-flight" / "accounts"
    for name in ("cf9f50", "d78343"):
        (accounts / name / "grok").mkdir(parents=True)
    L.write_record(
        {
            "schema": L.SCHEMA,
            "dispatch_id": "grok-dead-seat",
            "agent": "grok-code",
            "engine": "grok",
            "effective_account": "cf9f50",
            "state": "quota_exhausted",
            "terminal_state": "quota_exhausted",
            "reset_at": "2033-09-07T02:18:00+00:00",
            "started_at": L.utc_now(),
        }
    )
    monkeypatch.setattr(
        "grok_seats.select_seat",
        lambda **_kwargs: "cf9f50",
    )
    args = _grok_args(account=None)
    assert D.grok_selected_account(args) == "d78343"


def test_grok_default_selection_refuses_when_no_seat_is_usable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import grok_seats

    def no_usable_seat(**_kwargs):
        raise grok_seats.NoUsableSeat("no usable grok seat")

    monkeypatch.setattr(grok_seats, "select_seat", no_usable_seat)
    with pytest.raises(D.DispatchUsageError, match="no usable grok seat"):
        D.grok_selected_account(_grok_args(account=None))


def test_resume_refuses_live_grok_source(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    parent_id = "live-grok"
    record = _write_parent(
        tmp_path,
        dispatch_id=parent_id,
        agent="grok-code",
        engine="grok",
        session_id=GROK_SESSION,
        state="running",
    )
    record.update(
        {
            "worker_pid": 43210,
            "worker_identity": {
                "pid": 43210,
                "lstart": "Mon Jul 28 12:00:00 2026",
                "comm": "grok",
            },
        }
    )
    L.write_record(record)
    prompt = tmp_path / "brief.md"
    prompt.write_text("continue.\n", encoding="utf-8")
    monkeypatch.setattr(L, "identity_matches", lambda _record: (True, "live"))
    monkeypatch.setattr(
        D,
        "_reserve_auto_dispatch_id",
        lambda *_a, **_k: pytest.fail("live-source refusal must not allocate"),
    )
    rc = D.main(["resume", parent_id, "--prompt-file", str(prompt)])
    assert rc == 64
    assert capsys.readouterr().err == (
        "goalflight_dispatch: dispatch live-grok is still live; "
        "wait for terminal before resume\n"
    )


def test_kimi_harvest_never_guesses_among_many(tmp_path: Path) -> None:
    home = tmp_path / "home"
    work = tmp_path / "repo"
    work.mkdir()
    index = home / ".kimi-code" / "session_index.jsonl"
    index.parent.mkdir(parents=True)
    rows = [
        {
            "sessionId": "session_aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
            "workDir": str(work),
            "sessionDir": str(home / "s1"),
        },
        {
            "sessionId": "session_bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
            "workDir": str(work),
            "sessionDir": str(home / "s2"),
        },
    ]
    index.write_text(
        "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8"
    )
    assert E.harvest_kimi_session_id(home, work) is None


def test_kimi_harvest_returns_sole_match(tmp_path: Path) -> None:
    home = tmp_path / "home"
    work = tmp_path / "repo"
    work.mkdir()
    index = home / ".kimi-code" / "session_index.jsonl"
    index.parent.mkdir(parents=True)
    handle = "session_aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    index.write_text(
        json.dumps(
            {
                "sessionId": handle,
                "workDir": str(work),
                "sessionDir": str(home / "s1"),
            }
        )
        + "\n",
        encoding="utf-8",
    )
    assert E.harvest_kimi_session_id(home, work) == handle


def test_footer_parser_reads_kimi_dash_s() -> None:
    assert (
        E.parse_resume_footer_handle(
            "To resume this session: kimi -S session_aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
        )
        == "session_aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    )


def test_ensure_assigned_names_grok_uuid() -> None:
    args = _grok_args()
    assigned = D._ensure_assigned_engine_session(args)
    assert assigned is not None
    assert E.valid_session_id("grok", assigned) == assigned
    assert args.engine_session_id == assigned


def test_ensure_assigned_does_not_invent_kimi_handle() -> None:
    args = SimpleNamespace(
        agent="moonshot",
        parent_dispatch_id=None,
        engine_session_id=None,
        codex_session_id=None,
    )
    assert D._ensure_assigned_engine_session(args) is None


def test_resume_refuses_unsupported_engine(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    parent_id = "custom-parent"
    _write_parent(
        tmp_path,
        dispatch_id=parent_id,
        agent="custom",
        engine="custom",
        session_id=GROK_SESSION,
    )
    prompt = tmp_path / "brief.md"
    prompt.write_text("continue.\n", encoding="utf-8")
    rc = D.main(["resume", parent_id, "--prompt-file", str(prompt)])
    assert rc == 64
    err = capsys.readouterr().err
    assert "is not a resumable worker CLI" in err
    assert parent_id in err


DISPATCH_PY = ROOT / "scripts" / "goalflight_dispatch.py"


def _isolated_env(tmp_path: Path, home: Path) -> dict[str, str]:
    env = os.environ.copy()
    env["HOME"] = str(home)
    env["GOALFLIGHT_STATE_DIR"] = str(tmp_path / "state")
    env["GOALFLIGHT_DISPATCH_DIR"] = str(tmp_path / "dispatch")
    env["GOALFLIGHT_JOURNAL_DIR"] = str(tmp_path / "journals")
    env["GOALFLIGHT_WAKE_LEDGER"] = str(tmp_path / "wake-ledger")
    env["GOALFLIGHT_WAKE_LEDGER_DIR"] = str(tmp_path / "wake-ledger")
    env["GOALFLIGHT_MESSAGES_DIR"] = str(tmp_path / "messages")
    env["GOALFLIGHT_TASK_STORE"] = str(tmp_path / "tasks")
    env["GOALFLIGHT_TASK_STORE_DIR"] = str(tmp_path / "tasks")
    env["GOALFLIGHT_PIDFILE_DIR"] = str(tmp_path / "pids")
    env["GOALFLIGHT_CAPACITY_CONF"] = os.devnull
    env.pop("GOALFLIGHT_STEER_FILE", None)
    env.pop("GROK_HOME", None)
    return env


def test_grok_launch_then_resume_reuses_assigned_handle(tmp_path: Path) -> None:
    home = tmp_path / "home"
    seat = home / ".goal-flight" / "accounts" / "seat" / "grok"
    (seat / ".grok").mkdir(parents=True)
    (seat / ".grok" / "config.toml").write_text(
        '[cli]\n\n[ui]\npermission_mode = "always-approve"\n',
        encoding="utf-8",
    )
    (seat / ".grok" / "auth.json").write_text("{}\n", encoding="utf-8")
    prompt = tmp_path / "brief.md"
    prompt.write_text("COMPLETE: first turn\n", encoding="utf-8")
    resume_prompt = tmp_path / "resume.md"
    resume_prompt.write_text("COMPLETE: resumed turn\n", encoding="utf-8")
    spawn_log = tmp_path / "grok-argv.log"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_grok = fake_bin / "grok"
    fake_grok.write_text(
        "#!/bin/sh\n"
        'printf \'%s\\n\' "$*" >> "$GROK_SPAWN_LOG"\n'
        "exit 0\n",
        encoding="utf-8",
    )
    fake_grok.chmod(0o755)
    env = _isolated_env(tmp_path, home)
    env["PATH"] = f"{fake_bin}{os.pathsep}{env.get('PATH', '')}"
    env["GROK_SPAWN_LOG"] = str(spawn_log)

    first = subprocess.run(
        [
            sys.executable,
            str(DISPATCH_PY),
            "--agent",
            "grok-code",
            "--unregistered-forced",
            "--account",
            "seat",
            "--prompt-file",
            str(prompt),
            "--cwd",
            str(tmp_path),
            "--foreground",
            "--dispatch-id",
            "grok-first",
            "--max-idle-secs",
            "2",
            "--poll-secs",
            "0.1",
        ],
        cwd=str(tmp_path),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert spawn_log.is_file(), first.stderr
    first_argv = spawn_log.read_text(encoding="utf-8").strip().splitlines()[0]
    assert "--session-id" in first_argv
    assert "--resume" not in first_argv
    assert "--fork-session" not in first_argv
    session_id = first_argv.split("--session-id", 1)[1].split()[0]
    assert E.valid_session_id("grok", session_id) == session_id
    ledger_path = tmp_path / "state" / "runs.d" / "grok-first.json"
    if not ledger_path.is_file():
        found = list((tmp_path / "state").rglob("grok-first.json"))
        assert found, first.stderr + first.stdout
        ledger_path = found[0]
    ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
    assert ledger.get("engine_session_id") == session_id

    spawn_log.write_text("", encoding="utf-8")
    resumed = subprocess.run(
        [
            sys.executable,
            str(DISPATCH_PY),
            "resume",
            "grok-first",
            "--prompt-file",
            str(resume_prompt),
            "--unregistered-forced",
        ],
        cwd=str(tmp_path),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    # Child is detached by default; wait briefly for the fake grok spawn.
    deadline = time.time() + 8
    resume_argv = ""
    while time.time() < deadline:
        if spawn_log.is_file() and spawn_log.stat().st_size:
            resume_argv = spawn_log.read_text(encoding="utf-8")
            if "--resume" in resume_argv:
                break
        time.sleep(0.1)
    assert "--resume" in resume_argv, resumed.stderr + resumed.stdout
    assert session_id in resume_argv
    assert "--fork-session" not in resume_argv
    assert "--session-id" not in resume_argv
