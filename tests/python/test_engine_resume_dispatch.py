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
CURSOR_SESSION_UUID = "d128e49d-883f-44e2-8cca-1fe16902cba9"
PRIOR_CURSOR_SESSION = "11111111-2222-4333-8444-555555555555"


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
    calls: list[set[str] | None] = []

    def select_seat(*, exclude=None):
        calls.append(exclude)
        return "d78343" if exclude == {"cf9f50"} else "cf9f50"

    monkeypatch.setattr("grok_seats.select_seat", select_seat)
    args = _grok_args(account=None)
    assert D.grok_selected_account(args) == "d78343"
    assert calls == [None, {"cf9f50"}]


def test_grok_default_selection_refuses_when_the_only_usable_seat_is_blocked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import grok_seats

    calls: list[set[str] | None] = []

    def select_seat(*, exclude=None):
        calls.append(exclude)
        if exclude == {"cf9f50"}:
            raise grok_seats.NoUsableSeat("no usable grok seat")
        return "cf9f50"

    monkeypatch.setattr(grok_seats, "select_seat", select_seat)
    monkeypatch.setattr(D, "_account_quota_blocked", lambda *_args, **_kwargs: True)

    with pytest.raises(D.DispatchUsageError, match="no usable grok seat"):
        D.grok_selected_account(_grok_args(account=None))
    assert calls == [None, {"cf9f50"}]


def test_grok_default_selection_refuses_when_no_seat_is_usable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import grok_seats

    def no_usable_seat(**_kwargs):
        raise grok_seats.NoUsableSeat("no usable grok seat")

    monkeypatch.setattr(grok_seats, "select_seat", no_usable_seat)
    with pytest.raises(D.DispatchUsageError, match="no usable grok seat"):
        D.grok_selected_account(_grok_args(account=None))


def test_unpinned_codex_host_fallback_records_labelled_host(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """All-walled / unselectable managed seats must bill a labelled host, not None."""
    monkeypatch.setattr(
        D,
        "_codex_seat_api",
        lambda: SimpleNamespace(
            resolve_codex_seat=lambda *_args, **_kwargs: (None, None)
        ),
    )
    home, account = D.resolve_codex_home(tmp_path, None, "walled-fleet")
    assert home is None
    assert account == "host"
    err = capsys.readouterr().err
    assert "billing host" in err
    assert "walled" in err or "discovery failed" in err


def test_codex_seat_api_absent_does_not_label_a_host_fallback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """None still means we did not look; that is not a measured host fallback."""
    monkeypatch.setattr(D, "_codex_seat_api", lambda: None)
    assert D.resolve_codex_home(tmp_path, None, "no-lib") == (None, None)


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


# ---------------------------------------------------------------------------
# cursor session harvest (b-338)
#
# Cursor was the only engine in the fleet that could not be resumed at all, so
# every cursor worker that hit a wall or stopped to ask cost a full redispatch.
# It was not a cursor limitation: measured 2026-09-06, `~/.cursor/acp-sessions`
# held 238 sessions and none had a pooled-seat cwd, while the dispatch that had
# just run left its transcript under `~/.cursor/projects/<encoded cwd>/
# agent-transcripts/<id>/`. We were looking at the wrong tree.
# ---------------------------------------------------------------------------


def _seed_cursor_transcript(
    home: Path, work_dir: Path, session_id: str, *, mtime: float | None = None
) -> Path:
    encoded = str(work_dir).lstrip("/").replace("/", "-")
    entry = home / ".cursor" / "projects" / encoded / "agent-transcripts" / session_id
    entry.mkdir(parents=True)
    (entry / f"{session_id}.jsonl").write_text('{"role":"user"}\n', encoding="utf-8")
    if mtime is not None:
        os.utime(entry, (mtime, mtime))
    return entry


def test_cursor_handle_is_harvested_from_the_projects_tree(tmp_path: Path) -> None:
    home = tmp_path / "home"
    work = Path("/Users/x/Repos/proj/worktrees/label/s-1")
    _seed_cursor_transcript(home, work, CURSOR_SESSION_UUID)
    assert (
        E.harvest_cursor_session_id(home, work) == CURSOR_SESSION_UUID
    )


def test_cursor_harvest_is_bounded_to_this_dispatchs_run(tmp_path: Path) -> None:
    """Pooled seats accumulate one transcript per dispatch that used them.

    Without the window this returns None forever after the second dispatch --
    correctly refusing to guess, but never resumable either.
    """
    home = tmp_path / "home"
    work = Path("/Users/x/Repos/proj/worktrees/label/s-1")
    now = time.time()
    _seed_cursor_transcript(home, work, PRIOR_CURSOR_SESSION, mtime=now - 86400)
    _seed_cursor_transcript(home, work, CURSOR_SESSION_UUID, mtime=now)

    assert E.harvest_cursor_session_id(home, work) is None, (
        "unbounded, two candidates must refuse rather than pick one"
    )
    assert (
        E.harvest_cursor_session_id(home, work, after_mtime=now - 3600)
        == CURSOR_SESSION_UUID
    ), "the window must select this run's transcript"


def test_cursor_harvest_refuses_a_path_it_cannot_encode(tmp_path: Path) -> None:
    """Cursor truncates a long cwd and appends a hash we cannot reconstruct.

    An absent directory is an unknown, and must not become a guess.
    """
    home = tmp_path / "home"
    (home / ".cursor" / "projects").mkdir(parents=True)
    assert E.harvest_cursor_session_id(home, Path("/some/never/used/path")) is None


def test_cursor_harvest_ignores_a_directory_that_is_not_a_handle(tmp_path: Path) -> None:
    home = tmp_path / "home"
    work = Path("/Users/x/Repos/proj/worktrees/label/s-1")
    encoded = str(work).lstrip("/").replace("/", "-")
    root = home / ".cursor" / "projects" / encoded / "agent-transcripts"
    root.mkdir(parents=True)
    (root / "not-a-session-id").mkdir()
    assert E.harvest_cursor_session_id(home, work) is None


# ---------------------------------------------------------------------------
# the worker's cwd is not the project root (t-288)
#
# Every engine keys its session store by the directory the worker ran in. For a
# seat dispatch that is the worktree, so harvesting with the PROJECT ROOT looks
# somewhere the session never was and the worker silently loses its resumable
# handle. This was fixed for cursor and left unfixed for the others; a reviewer
# caught the missed half.
# ---------------------------------------------------------------------------


SEAT_CWD = Path("/Users/x/Repos/proj/worktrees/label/s-3")
PROJECT_ROOT = Path("/Users/x/Repos/proj")


def test_grok_handle_is_lost_when_harvested_at_the_project_root(tmp_path: Path) -> None:
    home = tmp_path / "home"
    import urllib.parse

    d = (home / ".grok" / "sessions"
         / urllib.parse.quote(str(SEAT_CWD), safe="") / GROK_SESSION)
    d.mkdir(parents=True)
    (d / "chat_history.jsonl").write_text("{}\n", encoding="utf-8")

    assert E.harvest_grok_session_id(home, SEAT_CWD) == GROK_SESSION
    assert E.harvest_grok_session_id(home, PROJECT_ROOT) is None, (
        "the project root is a different directory; it must not resolve, and "
        "passing it is what loses the handle"
    )


def test_kimi_handle_is_lost_when_harvested_at_the_project_root(tmp_path: Path) -> None:
    home = tmp_path / "home"
    sess = home / ".kimi-code" / "sessions" / KIMI_SESSION
    sess.mkdir(parents=True)
    index = home / ".kimi-code" / "session_index.jsonl"
    index.write_text(
        json.dumps({"workDir": str(SEAT_CWD), "sessionId": KIMI_SESSION,
                    "sessionDir": str(sess)}) + "\n",
        encoding="utf-8",
    )

    assert E.harvest_kimi_session_id(home, SEAT_CWD) == KIMI_SESSION
    assert E.harvest_kimi_session_id(home, PROJECT_ROOT) is None, (
        "the index matches on an exact workDir; the project root never matches"
    )


def test_the_watcher_prefers_the_worker_cwd_for_every_engine() -> None:
    """Pin the WIRING, not just the harvesters.

    The harvesters were always correct; the bug was the caller handing them the
    project root. This asserts the preference is applied once, before the
    per-engine branches, so a new engine cannot inherit the old defect.
    """
    src = (ROOT / "scripts" / "goalflight_watch.py").read_text(encoding="utf-8")
    block = src.split('if engine_session_id is None and resume_engine in')[1]
    head = block.split('if resume_engine == "moonshot"')[0]
    assert 'worker_cwd = getattr(args, "worker_cwd", None)' in head, head[:400]
    assert "Path(worker_cwd)" in head, "the worker cwd must win over project_root"
