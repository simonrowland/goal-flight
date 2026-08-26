#!/usr/bin/env python3
"""b-217 / b-227: queued and resumed launches must keep dispatch-affecting flags.

Two reconstruction sites used to rebuild a launch argv from a remembered list.
Each dropped whatever its author did not think to copy: submit collapsed
``--cwd`` through ``resolve_project_root``, and resume omitted ``--os-sandbox``,
``--read-only``, and the original worker cwd. Assert on the launched argv
(``-C`` / dispatch flags), not on artifacts — artifacts pass while the process
is still rooted in the shared checkout.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import goalflight_dispatch as D  # noqa: E402
import goalflight_ledger as L  # noqa: E402
import goalflight_task  # noqa: E402


SESSION_ID = "12345678-1234-4abc-8def-1234567890ab"

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="worktree cwd and worker-CLI resume are local POSIX-only",
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
    monkeypatch.setenv("GOALFLIGHT_JOURNAL_DIR", str(tmp_path / "journal"))
    monkeypatch.setenv("GOALFLIGHT_MESSAGES_DIR", str(tmp_path / "messages"))
    monkeypatch.setenv("GOALFLIGHT_WAKE_LEDGER_DIR", str(tmp_path / "wake-ledger"))
    monkeypatch.setenv("GOALFLIGHT_PIDFILE_DIR", str(tmp_path / "pidfiles"))
    monkeypatch.setenv("GOALFLIGHT_CAPACITY_CONF", "/dev/null")
    monkeypatch.setenv("GOALFLIGHT_CAPACITY_WAIT_S", "0")
    monkeypatch.setenv("GOALFLIGHT_DISABLE_NUDGES", "1")
    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.delenv("GOALFLIGHT_PROJECT_ROOT", raising=False)
    for key in (
        "GOALFLIGHT_CONTROLLER_LABEL",
        "GOALFLIGHT_CONTROLLER_PID",
        "GOALFLIGHT_CONTROLLER_SESSION_ID",
        "GOALFLIGHT_CONTROLLER_LEASE_NONCE",
    ):
        monkeypatch.delenv(key, raising=False)


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=str(repo),
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env={
            **os.environ,
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@t",
        },
    )


def _make_repo_with_worktree(root: Path) -> tuple[Path, Path]:
    main = root / "repo"
    main.mkdir()
    _git(main, "init", "-b", "main")
    (main / "tracked.txt").write_text("base\n", encoding="utf-8")
    _git(main, "add", "tracked.txt")
    _git(main, "commit", "-m", "base")
    worktree = root / "wt"
    _git(main, "worktree", "add", "-q", "-b", "feat", str(worktree))
    return main.resolve(), worktree.resolve()


def _option_value(argv: list[str], flag: str) -> str | None:
    prefix = flag + "="
    for index, token in enumerate(argv):
        if token == "--":
            break
        if token == flag:
            if index + 1 >= len(argv):
                return None
            return argv[index + 1]
        if token.startswith(prefix):
            return token[len(prefix) :]
    return None


def _codex_dash_c(cwd: str) -> str:
    argv, _stdin = D.build_worker(
        argparse.Namespace(
            agent="codex",
            cwd=cwd,
            model=None,
            os_sandbox=None,
            read_only=False,
            parent_dispatch_id=None,
            codex_session_id=None,
        ),
        "/tmp/p.md",
        [],
    )
    value = _option_value(argv, "-C")
    assert value is not None, argv
    return value


def _replay_namespace(cwd: str, **over) -> argparse.Namespace:
    base = dict(
        agent="codex",
        dispatch_id="cwd-replay",
        cwd=cwd,
        shape="bash",
        priority="normal",
        billing="sub",
        poll_secs=2.0,
        max_idle_secs=600.0,
        prompt_file="/tmp/p.md",
        prompt=None,
        task_ids=[],
        model=None,
        os_sandbox=None,
        read_only=False,
        fast=False,
        web_research_ok=False,
        web_qa=False,
        ignore_git_warn=True,
        no_orientation=True,
        capacity_wait_s=None,
        account=None,
        interactive=False,
        permission_mode="auto",
        permission_dir=None,
        permission_inline_timeout_s=None,
        permission_user_timeout_s=None,
        permission_allow_tool_title_pattern=[],
        controller_pid=None,
        unregistered_forced=True,
    )
    base.update(over)
    return argparse.Namespace(**base)


def test_submit_explicit_worktree_cwd_launches_with_that_C(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Queued replay must keep the worktree as the worker ``-C``, not the main repo.

    ``resolve_project_root`` collapses worktrees on purpose (one task store).
    That collapse must not leak into the launched worker argv.
    """
    main, worktree = _make_repo_with_worktree(tmp_path)
    assert goalflight_task.resolve_project_root(str(worktree)) == main

    prompt = worktree / "brief.md"
    prompt.write_text("do the work\n", encoding="utf-8")
    dispatch_id = "cwd-fidelity"
    rc = D.main(
        [
            "--agent",
            "codex",
            "--submit",
            "--no-drain-on-submit",
            "--unregistered-forced",
            "--dispatch-id",
            dispatch_id,
            "--prompt-file",
            str(prompt),
            "--cwd",
            str(worktree),
            "--ignore-git-warn",
            "--no-orientation",
        ]
    )
    assert rc == 0

    queue_path = tmp_path / "state" / "dispatch-queue" / f"{dispatch_id}.json"
    entry = json.loads(queue_path.read_text(encoding="utf-8"))
    stored_cwd = _option_value(list(entry["dispatch_argv"]), "--cwd")
    assert stored_cwd is not None, entry["dispatch_argv"]
    assert Path(stored_cwd).resolve() == worktree
    assert Path(stored_cwd).resolve() != main

    captured: list[list[str]] = []
    old_run = D.subprocess.run

    def fake_run(argv, **kwargs):
        argv = list(argv)
        if not any("goalflight_dispatch.py" in str(part) for part in argv):
            return old_run(argv, **kwargs)
        captured.append(argv)
        try:
            launched_id = argv[argv.index("--dispatch-id") + 1]
            token = argv[argv.index("--queue-launch-token") + 1]
        except (ValueError, IndexError):
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
        L.write_record(
            {
                "schema": L.SCHEMA,
                "dispatch_id": launched_id,
                "agent": "codex",
                "engine": "codex",
                "shape": "bash",
                "transport": "dispatch",
                "project_root": str(main),
                "worker_pid": os.getpid(),
                "worker_identity": L.process_identity(os.getpid()),
                "stdout_path": str(tmp_path / "t.tail"),
                "status_path": str(tmp_path / "t.status.json"),
                "state": "running",
                "terminal_state": "unknown",
                "queue_launch_token": token,
                "started_at": L.utc_now(),
            }
        )
        return subprocess.CompletedProcess(
            argv,
            0,
            stdout=f"DISPATCH-LAUNCHED {launched_id}\n",
            stderr="",
        )

    monkeypatch.setattr(D.subprocess, "run", fake_run)
    drain_rc = D.main(["drain", "--capacity-wait-s", "0", "--json"])
    assert drain_rc == 0
    assert captured, "drain did not launch the queued dispatch"

    launched = captured[0]
    launched_cwd = _option_value(launched, "--cwd")
    assert launched_cwd is not None, launched
    assert Path(launched_cwd).resolve() == worktree
    assert _codex_dash_c(launched_cwd) == str(Path(launched_cwd))
    assert Path(_codex_dash_c(launched_cwd)).resolve() == worktree
    assert Path(_codex_dash_c(launched_cwd)).resolve() != main


def test_canonical_replay_from_original_argv_does_not_collapse_worktree_cwd(
    tmp_path: Path,
) -> None:
    main, worktree = _make_repo_with_worktree(tmp_path)
    args = _replay_namespace(
        str(worktree),
        _original_argv=[
            "--agent",
            "codex",
            "--cwd",
            str(worktree),
            "--prompt-file",
            "/tmp/p.md",
            "--dispatch-id",
            "cwd-replay",
            "--submit",
            "--no-drain-on-submit",
        ],
    )
    argv = D._canonical_replay_argv(
        args,
        [],
        tail=tmp_path / "t.tail",
        status_json=tmp_path / "t.status.json",
    )
    stored = _option_value(argv, "--cwd")
    assert stored is not None, argv
    assert Path(stored).resolve() == worktree
    assert Path(stored).resolve() != main
    assert "--submit" not in argv
    assert "--no-drain-on-submit" not in argv
    assert _codex_dash_c(stored) == str(Path(stored))


def _write_codex_parent(
    tmp_path: Path,
    *,
    dispatch_id: str,
    worktree: Path,
    dispatch_argv: list[str] | None = None,
    os_sandbox: dict | None = None,
    worker_cwd: str | None = None,
) -> Path:
    home = tmp_path / "state" / "dispatch-homes" / dispatch_id
    rollout = (
        home
        / "sessions"
        / "2026"
        / "07"
        / "28"
        / f"rollout-2026-07-28T12-00-00-{SESSION_ID}.jsonl"
    )
    rollout.parent.mkdir(parents=True, exist_ok=True)
    rollout.write_text('{"type":"session_meta"}\n', encoding="utf-8")
    status_path = tmp_path / f"{dispatch_id}.status.json"
    record = {
        "schema": L.SCHEMA,
        "dispatch_id": dispatch_id,
        "agent": "codex",
        "engine": "codex",
        "shape": "bash",
        "account": "old-seat",
        "transport": "dispatch",
        "project_root": str(tmp_path / "repo"),
        "status_path": str(status_path),
        "state": "blocked",
        "terminal_state": "blocked",
        "started_at": L.utc_now(),
        "task_ids": ["t-123"],
        "codex_session_id": SESSION_ID,
        "codex_home": str(home),
        "codex_home_owner_dispatch_id": dispatch_id,
    }
    if dispatch_argv is not None:
        record["dispatch_argv"] = list(dispatch_argv)
        record["request_envelope"] = {"dispatch_argv": list(dispatch_argv)}
    if os_sandbox is not None:
        record["os_sandbox"] = os_sandbox
    if worker_cwd is not None:
        record["worker_cwd"] = worker_cwd
    L.write_record(record)
    return home


def test_resume_preserves_os_sandbox_read_only_and_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    main, worktree = _make_repo_with_worktree(tmp_path)
    parent_id = "resume-flags-parent"
    recorded = [
        "--agent",
        "codex",
        "--shape",
        "bash",
        "--dispatch-id",
        parent_id,
        "--cwd",
        str(worktree),
        "--prompt-file",
        str(worktree / "old.md"),
        "--os-sandbox",
        "off",
        "--read-only",
        "--task",
        "t-123",
    ]
    _write_codex_parent(
        tmp_path,
        dispatch_id=parent_id,
        worktree=worktree,
        dispatch_argv=recorded,
        worker_cwd=str(worktree),
    )
    prompt = tmp_path / "revisions.md"
    prompt.write_text("continue.\n", encoding="utf-8")
    captured: list[list[str]] = []
    monkeypatch.setattr(
        D,
        "_reserve_auto_dispatch_id",
        lambda _agent, _base: "codex-resume-child",
    )
    monkeypatch.setattr(
        D,
        "main",
        lambda argv=None: captured.append(list(argv or [])) or 0,
    )

    assert (
        D._cmd_resume(
            [parent_id, "--prompt-file", str(prompt), "--unregistered-forced"]
        )
        == 0
    )
    launch = captured[0]
    assert Path(_option_value(launch, "--cwd") or "").resolve() == worktree
    assert Path(_option_value(launch, "--cwd") or "").resolve() != main
    assert _option_value(launch, "--os-sandbox") == "off"
    assert "--read-only" in launch
    assert launch[launch.index("--parent-dispatch-id") + 1] == parent_id
    assert launch[launch.index("--dispatch-id") + 1] == "codex-resume-child"


def test_resume_recovers_sandbox_and_cwd_from_record_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Older records may lack dispatch_argv; posture + worker_cwd still bind."""
    main, worktree = _make_repo_with_worktree(tmp_path)
    parent_id = "resume-meta-parent"
    _write_codex_parent(
        tmp_path,
        dispatch_id=parent_id,
        worktree=worktree,
        worker_cwd=str(worktree),
        os_sandbox={
            "shape": "bash",
            "requested_profile": "off",
            "supported_profile": "off",
            "enforced_profile": "off",
        },
    )
    prompt = tmp_path / "revisions.md"
    prompt.write_text("continue.\n", encoding="utf-8")
    captured: list[list[str]] = []
    monkeypatch.setattr(
        D,
        "_reserve_auto_dispatch_id",
        lambda _agent, _base: "codex-resume-meta-child",
    )
    monkeypatch.setattr(
        D,
        "main",
        lambda argv=None: captured.append(list(argv or [])) or 0,
    )
    assert (
        D._cmd_resume(
            [parent_id, "--prompt-file", str(prompt), "--unregistered-forced"]
        )
        == 0
    )
    launch = captured[0]
    assert Path(_option_value(launch, "--cwd") or "").resolve() == worktree
    assert Path(_option_value(launch, "--cwd") or "").resolve() != main
    assert _option_value(launch, "--os-sandbox") == "off"


def test_every_launch_flag_is_classified() -> None:
    """A new launch-parser flag must be classified, or it will be dropped silently.

    Preserve-class flags are carried by reconstructing from the original
    invocation. Replace/inject/strip are the per-attempt exceptions. Adding a
    dispatch-affecting flag without classifying it must fail here.
    """
    parser = D._build_launch_parser()
    observed = set(parser._option_string_actions)
    classified = set(D.LAUNCH_ARGV_CLASS)
    extra = sorted(observed - classified)
    missing = sorted(classified - observed)
    assert extra == [], f"unclassified launch flags: {extra}"
    assert missing == [], f"classified flags missing from parser: {missing}"
    boolean_types = {"_StoreTrueAction", "_StoreFalseAction", "_HelpAction"}
    for flag, cls in D.LAUNCH_ARGV_CLASS.items():
        if cls not in {"strip", "inject", "ignore", "replace"}:
            continue
        action = parser._option_string_actions[flag]
        takes_value = type(action).__name__ not in boolean_types
        if takes_value and flag != "--stats":
            assert flag in D._REPLAY_VALUE_OPTIONS, flag


def test_preserve_class_flags_survive_original_argv_replay(tmp_path: Path) -> None:
    parser = D._build_launch_parser()
    original: list[str] = [
        "--agent",
        "codex",
        "--dispatch-id",
        "class-replay",
        "--prompt-file",
        "/tmp/p.md",
        "--submit",
        "--no-drain-on-submit",
    ]
    dummies = {
        "--cwd": "/tmp/class-cwd",
        "--os-sandbox": "off",
        "--model": "gpt-test",
        "--priority": "bulk",
        "--account": "seat-a",
        "--billing": "sub",
        "--shape": "bash",
        "--permission-mode": "auto",
        "--poll-secs": "3.0",
        "--max-idle-secs": "90",
        "--capacity-wait-s": "1.0",
        "--task": "t-1",
        "--permission-dir": "/tmp/perm",
        "--permission-inline-timeout-s": "4",
        "--permission-user-timeout-s": "5",
        "--permission-allow-tool-title-pattern": "title",
        "--controller-label": "lab",
        "--controller-pid": "1",
        "--controller-beacon-pid": "2",
        "--controller-session-id": "nonce",
        "--parent-dispatch-id": "parent",
        "--engine-session-id": "sess",
        "--codex-session-id": "sess",
        "--codex-resume-home": "/tmp/home",
        "--codex-home-owner-dispatch-id": "parent",
    }
    boolean_preserve = []
    for flag, cls in D.LAUNCH_ARGV_CLASS.items():
        if cls != "preserve":
            continue
        if flag in {
            "--agent",
            "--dispatch-id",
            "--prompt-file",
            "--prompt",
            "--session-label",
        }:
            continue
        action = parser._option_string_actions[flag]
        if type(action).__name__ in {"_StoreTrueAction", "_StoreFalseAction"}:
            boolean_preserve.append(flag)
            original.append(flag)
        elif flag in dummies:
            original.extend([flag, dummies[flag]])
        else:
            raise AssertionError(f"preserve flag {flag} has no dummy value")

    args = parser.parse_args(original)
    args.task_ids = D._parse_task_ids(getattr(args, "tasks", None))
    args._original_argv = list(original)
    replay = D._canonical_replay_argv(
        args,
        [],
        tail=tmp_path / "t.tail",
        status_json=tmp_path / "t.status.json",
    )
    for flag in boolean_preserve:
        if flag == "--readonly":
            continue
        assert flag in replay, flag
    for flag, value in dummies.items():
        got = _option_value(replay, flag)
        if flag == "--cwd":
            assert got is not None, flag
            assert Path(got).resolve() == Path(value).resolve(), (flag, got, value)
        else:
            assert got == value, (flag, got, value)
    assert "--submit" not in replay
    assert "--no-drain-on-submit" not in replay
