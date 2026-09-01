#!/usr/bin/env python3
"""Focused dispatcher tests for first-class ACP agents."""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import os
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import goalflight_acp_run  # noqa: E402
import goalflight_dispatch as dispatch_mod  # noqa: E402
import goalflight_journal  # noqa: E402
import goalflight_watch  # noqa: E402


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def _normalize(agent: str) -> str:
    args = SimpleNamespace(agent=agent)
    dispatch_mod._normalize_acp_agent(args)
    return args.agent


def test_normalize_acp_agents() -> None:
    assert _normalize("worker") == "codex-acp"
    assert _normalize("codex") == "codex-acp"
    assert _normalize("codex-acp") == "codex-acp"
    assert _normalize("cursor") == "cursor"
    assert _normalize("cursor-agent") == "cursor"
    assert _normalize("claude") == "claude"
    assert _normalize("claude-acp") == "claude"
    assert _normalize("claude-code-cli-acp") == "claude"
    assert _normalize("grok-acp") == "grok-acp"

    try:
        _normalize("not-real")
    except dispatch_mod.DispatchUsageError as exc:
        assert "codex-acp, grok-acp, cursor, or claude-acp" in str(exc)
    else:
        raise AssertionError("bogus ACP agent did not raise")


def _base_acp_args(tmp: Path, *, agent: str, dispatch_id: str) -> SimpleNamespace:
    return SimpleNamespace(
        agent=agent,
        model=None,
        prompt_file=None,
        cwd=str(tmp),
        read_only=False,
        prompt="COMPLETE: no-op",
        max_idle_secs="300",
        poll_secs="0.1",
        dispatch_id=dispatch_id,
        status_json=None,
        permission_mode="auto",
        permission_dir=None,
        permission_inline_timeout_s=None,
        permission_user_timeout_s=None,
        billing="sub",
        tail=None,
        priority="normal",
        capacity_wait_s=None,
    )


def test_build_acp_cfg_agent_liveness_defaults() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        for agent in ("cursor", "claude"):
            args = _base_acp_args(tmp, agent=agent, dispatch_id=f"{agent}-cfg")
            cfg = dispatch_mod._build_acp_cfg(args, status_json=tmp / f"{agent}.json")
            assert cfg.agent == agent
            assert cfg.liveness_profile == "remote_api"

        args = _base_acp_args(tmp, agent="codex-acp", dispatch_id="codex-cfg")
        cfg = dispatch_mod._build_acp_cfg(args, status_json=tmp / "codex.json")
        assert cfg.agent == "codex-acp"
        assert cfg.liveness_profile is None

        args = _base_acp_args(tmp, agent="codex-acp", dispatch_id="priority-cfg")
        args.priority = "bulk"
        args.capacity_wait_s = 12.5
        args.account = "explicit-seat"
        cfg = dispatch_mod._build_acp_cfg(args, status_json=tmp / "priority.json")
        assert cfg.priority == "bulk"
        assert cfg.capacity_wait_s == 12.5
        assert cfg.account == "explicit-seat"


def test_build_acp_cfg_injects_orientation_prompt_text() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        prompt = tmp / "prompt.md"
        prompt.write_text("Do ACP work.\n", encoding="utf-8")
        orientation = tmp / "docs-private" / "rag" / "ORIENTATION.md"
        orientation.parent.mkdir(parents=True)
        orientation.write_text("project orientation\n", encoding="utf-8")

        args = _base_acp_args(tmp, agent="codex-acp", dispatch_id="orientation-acp")
        args.prompt = None
        args.prompt_file = str(prompt)
        cfg = dispatch_mod._build_acp_cfg(args, status_json=tmp / "orientation.json")

        assert cfg.prompt is None
        assert cfg.original_prompt_file == str(prompt.resolve())
        assert "PROJECT ORIENTATION\n" in cfg.prompt_text
        assert f"Path: {orientation.resolve()}" in cfg.prompt_text
        assert dispatch_mod.PROJECT_ORIENTATION_SCOPE_RULE in cfg.prompt_text
        assert "Do ACP work." in cfg.prompt_text

        args.no_orientation = True
        suppressed = dispatch_mod._build_acp_cfg(args, status_json=tmp / "suppressed.json")
        assert suppressed.prompt == str(prompt.resolve())
        assert suppressed.prompt_text is None
        assert suppressed.original_prompt_file == str(prompt.resolve())


def test_acp_production_path_persists_and_reminds_with_assembled_prompt() -> None:
    captured: dict[str, object] = {}
    old_run = goalflight_acp_run.run_acp_dispatch

    async def fake_run(cfg):
        captured["cfg"] = cfg
        return {
            "state": "complete",
            "dispatch_id": cfg.dispatch_id,
            "agent": cfg.agent,
            "worker_pid": 4242,
            "worker_alive": False,
        }

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        dispatch_dir = tmp / "dispatch"
        prompt = tmp / "brief.md"
        prompt.write_text("Do ACP production work.\n", encoding="utf-8")
        orientation = tmp / "docs-private" / "rag" / "ORIENTATION.md"
        orientation.parent.mkdir(parents=True)
        orientation.write_text("project orientation\n", encoding="utf-8")
        status_json = dispatch_dir / "acp-production.status.json"

        args = _base_acp_args(tmp, agent="codex-acp", dispatch_id="acp-production")
        args.prompt = None
        args.prompt_file = str(prompt)
        args.status_json = str(status_json)
        args.tail = str(dispatch_dir / "acp-production.tail")
        old_state = os.environ.get("GOALFLIGHT_STATE_DIR")
        stderr = io.StringIO()
        try:
            os.environ["GOALFLIGHT_STATE_DIR"] = str(tmp / "state")
            goalflight_acp_run.run_acp_dispatch = fake_run
            with contextlib.redirect_stderr(stderr):
                rc = dispatch_mod._run_acp_shape(
                    args,
                    base=dispatch_dir,
                    account_env={},
                )
        finally:
            goalflight_acp_run.run_acp_dispatch = old_run
            if old_state is None:
                os.environ.pop("GOALFLIGHT_STATE_DIR", None)
            else:
                os.environ["GOALFLIGHT_STATE_DIR"] = old_state

        assert rc == 0
        cfg = captured["cfg"]
        watcher_prompt = Path(cfg.watcher_prompt_file)
        assert watcher_prompt.parent == status_json.parent.resolve()
        assert watcher_prompt.is_file()
        assert _mode(watcher_prompt) == 0o600
        assert _mode(watcher_prompt.parent) == 0o700
        persisted = watcher_prompt.read_text(encoding="utf-8")
        assert dispatch_mod.PROMPT_FILE_PREAMBLE in persisted
        assert "PROJECT ORIENTATION\n" in persisted
        assert f"Path: {orientation.resolve()}" in persisted
        assert "Do ACP production work." in persisted
        # The post-dispatch reminder is one line now: dispatch id + status path.
        # The watcher invocation that names --ignore-prompt-file moved behind
        # --hints, because repeating it on every launch turned a real guard rail
        # into wallpaper. Assert BOTH halves so neither can silently regress:
        # the terse default, and the hinted block still naming the prompt file.
        assert "[goal-flight] dispatched" in stderr.getvalue()
        assert f"--ignore-prompt-file {watcher_prompt}" not in stderr.getvalue()
        hinted = "\n".join(
            dispatch_mod._status_reminder_lines(
                "acp-agents",
                status_json=status_json,
                tail_path=status_json.with_suffix(".tail"),
                worker_pid=4321,
                shape="acp",
                prompt_path=watcher_prompt,
                hints=True,
            )
        )
        assert f"--ignore-prompt-file {watcher_prompt}" in hinted


def test_acp_inline_prompt_uses_same_assembled_prompt_path() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        status_json = tmp / "dispatch" / "inline-acp.status.json"
        args = _base_acp_args(tmp, agent="codex-acp", dispatch_id="inline-acp")
        args.prompt = "inline ACP task with !COMPLETE: inline-acp — example"

        cfg = dispatch_mod._build_acp_cfg(
            args,
            status_json=status_json,
            base=tmp / "dispatch",
        )

        watcher_prompt = Path(cfg.watcher_prompt_file)
        assert watcher_prompt == status_json.resolve().with_name(
            "inline-acp.assembled.prompt"
        )
        assert watcher_prompt.read_text(encoding="utf-8") == (
            f"{dispatch_mod.PROMPT_FILE_PREAMBLE}\n\n{args.prompt}"
        )


def test_acp_prompt_history_rewrite_remains_private() -> None:
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "dispatch" / "private.assembled.prompt"
        cfg = SimpleNamespace(watcher_prompt_file=str(path))

        assert (
            goalflight_acp_run._persist_watcher_prompt_turn(cfg, "first")
            == path.resolve()
        )
        assert path.read_text(encoding="utf-8") == "first"
        assert _mode(path) == 0o600
        assert _mode(path.parent) == 0o700

        path.chmod(0o644)
        goalflight_acp_run._persist_watcher_prompt_turn(cfg, "replacement")
        assert path.read_text(encoding="utf-8") == "replacement"
        assert _mode(path) == 0o600


def test_dispatch_help_skips_legacy_prompt_sweep_mutation_pair() -> None:
    with tempfile.TemporaryDirectory() as td:
        state_dir = Path(td) / "state"
        dispatch_dir = state_dir / "dispatch"
        dispatch_dir.mkdir(parents=True, mode=0o755)
        dispatch_dir.chmod(0o755)
        legacy_prompt = dispatch_dir / "legacy.assembled.prompt"
        legacy_prompt.write_text("legacy private prompt", encoding="utf-8")
        legacy_prompt.chmod(0o644)

        with patch.dict(os.environ, _capacity_env(state_dir), clear=True):
            with contextlib.redirect_stdout(io.StringIO()):
                try:
                    dispatch_mod.main(["--help"])
                except SystemExit as exc:
                    assert exc.code == 0
                else:  # pragma: no cover - argparse --help always exits
                    raise AssertionError("dispatcher --help did not exit")

        assert _mode(dispatch_dir) == 0o755
        assert _mode(legacy_prompt) == 0o644
        assert not (dispatch_dir / dispatch_mod.LEGACY_PROMPT_SWEEP_MARKER).exists()


def test_dispatch_startup_sweep_is_once_and_best_effort_mutation_pair() -> None:
    with tempfile.TemporaryDirectory() as td:
        state_dir = Path(td) / "state"
        dispatch_dir = state_dir / "dispatch"
        dispatch_dir.mkdir(parents=True, mode=0o755)
        dispatch_dir.chmod(0o755)
        repaired_prompt = dispatch_dir / "repaired.assembled.prompt"
        repaired_prompt.write_text("repair me", encoding="utf-8")
        repaired_prompt.chmod(0o644)
        error_prompt = dispatch_dir / "error.assembled.prompt"
        error_prompt.write_text("constructed chmod failure", encoding="utf-8")
        error_prompt.chmod(0o644)
        original_chmod = Path.chmod
        chmod_errors = 0

        def selective_chmod(path: Path, mode: int, *args, **kwargs):
            nonlocal chmod_errors
            if path == error_prompt:
                chmod_errors += 1
                raise PermissionError("constructed sidecar chmod failure")
            return original_chmod(path, mode, *args, **kwargs)

        stderr = io.StringIO()
        with patch.dict(os.environ, _capacity_env(state_dir), clear=True):
            with patch.object(Path, "chmod", selective_chmod):
                with contextlib.redirect_stderr(stderr):
                    assert dispatch_mod._prepare_private_dispatch_dir() == dispatch_dir

            assert _mode(dispatch_dir) == 0o700
            assert _mode(repaired_prompt) == 0o600
            assert _mode(error_prompt) == 0o644
            marker = dispatch_dir / dispatch_mod.LEGACY_PROMPT_SWEEP_MARKER
            assert marker.is_file()
            assert "constructed sidecar chmod failure" in stderr.getvalue()

            # Mutation control: without the marker guard, the second call would
            # repair this deliberately re-loosened file and retry the error.
            repaired_prompt.chmod(0o644)
            with patch.object(Path, "chmod", selective_chmod):
                assert dispatch_mod._prepare_private_dispatch_dir() == dispatch_dir

        assert _mode(repaired_prompt) == 0o644
        assert chmod_errors == 1


def test_dispatch_startup_sweep_marker_serializes_concurrent_first_run() -> None:
    with tempfile.TemporaryDirectory() as td:
        state_dir = Path(td) / "state"
        dispatch_dir = state_dir / "dispatch"
        dispatch_dir.mkdir(parents=True)
        legacy_prompt = dispatch_dir / "concurrent.assembled.prompt"
        legacy_prompt.write_text("repair once", encoding="utf-8")
        legacy_prompt.chmod(0o644)
        original_chmod = Path.chmod
        prompt_chmods = 0
        count_lock = threading.Lock()
        start = threading.Barrier(3)
        outcomes: list[Path] = []

        def counted_chmod(path: Path, mode: int, *args, **kwargs):
            nonlocal prompt_chmods
            if path == legacy_prompt:
                with count_lock:
                    prompt_chmods += 1
                time.sleep(0.05)
            return original_chmod(path, mode, *args, **kwargs)

        def prepare() -> None:
            start.wait()
            outcomes.append(dispatch_mod._prepare_private_dispatch_dir())

        with patch.dict(os.environ, _capacity_env(state_dir), clear=True):
            with patch.object(Path, "chmod", counted_chmod):
                workers = [threading.Thread(target=prepare) for _ in range(2)]
                for worker in workers:
                    worker.start()
                start.wait()
                for worker in workers:
                    worker.join(timeout=5)
                    assert not worker.is_alive()

        assert outcomes == [dispatch_dir, dispatch_dir]
        assert prompt_chmods == 1
        assert _mode(legacy_prompt) == 0o600


def test_watcher_with_retired_prompt_and_status_exits_without_status_write() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        tail = tmp / "retired.tail"
        status_json = tmp / "retired.status.json"
        prompt = dispatch_mod._acp_watcher_prompt_path(status_json)
        tail.write_text("stale worker output\n", encoding="utf-8")
        env = os.environ.copy()
        env["GOALFLIGHT_STATE_DIR"] = str(tmp / "state")
        env["GOALFLIGHT_TASK_STORE_DIR"] = str(tmp / "task-store")
        env["GOALFLIGHT_JOURNAL_DIR"] = str(tmp / "journal")
        env["GOALFLIGHT_MESSAGES_DIR"] = str(tmp / "messages")
        env["GOALFLIGHT_WAKE_LEDGER_DIR"] = str(tmp / "wake-ledger")

        proc = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts" / "goalflight_watch.py"),
                "--pid",
                "99999999",
                "--tail",
                str(tail),
                "--status-json",
                str(status_json),
                "--dispatch-id",
                "retired-startup",
                "--ignore-prompt-file",
                str(prompt),
            ],
            capture_output=True,
            text=True,
            timeout=10,
            env=env,
        )

        assert proc.returncode == 0, (proc.returncode, proc.stdout, proc.stderr)
        assert proc.stdout.count("dispatch retired") == 1, proc.stdout
        assert not status_json.exists()


def test_watcher_both_absent_returns_before_scanner_initialization() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        tail = tmp / "retired.tail"
        status_json = tmp / "retired.status.json"
        prompt = dispatch_mod._acp_watcher_prompt_path(status_json)
        tail.write_text("stale worker output\n", encoding="utf-8")
        argv = [
            str(ROOT / "scripts" / "goalflight_watch.py"),
            "--pid",
            "4242",
            "--tail",
            str(tail),
            "--status-json",
            str(status_json),
            "--dispatch-id",
            "retired-before-scan",
            "--ignore-prompt-file",
            str(prompt),
        ]
        output = io.StringIO()

        with patch.object(sys, "argv", argv), \
                patch.object(
                    goalflight_watch,
                    "IncrementalTailScanner",
                    side_effect=AssertionError("retired watcher initialized scanner"),
                ), \
                contextlib.redirect_stdout(output):
            rc = goalflight_watch.main()

        assert rc == 0
        assert output.getvalue().count("dispatch retired") == 1
        assert not status_json.exists()


def test_nonterminal_dispatch_record_authorizes_missing_status_creation() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        tail = tmp / "recoverable.tail"
        status_json = tmp / "recoverable.status.json"
        prompt = dispatch_mod._acp_watcher_prompt_path(status_json)
        tail.write_text("worker stopped without a marker\n", encoding="utf-8")
        prompt.write_text("live dispatch prompt\n", encoding="utf-8")

        class FinishedScanner:
            def __init__(self, *_args, **_kwargs):
                pass

            def scan(self, **_kwargs):
                return goalflight_watch.TailScanResult(
                    markers=[],
                    mail_markers=[],
                    terminal=None,
                    size=tail.stat().st_size,
                    content_bytes=0,
                    validation_bytes=0,
                    lines_materialized=0,
                    resynced=False,
                    resync_reason=None,
                    fence_unbalanced=False,
                )

        class NoTrace:
            def __init__(self, **_kwargs):
                pass

            def sample(self, **_kwargs):
                return {"trace_active": False}

        argv = [
            str(ROOT / "scripts" / "goalflight_watch.py"),
            "--pid",
            "4242",
            "--tail",
            str(tail),
            "--status-json",
            str(status_json),
            "--dispatch-id",
            "recoverable-status",
            "--ignore-prompt-file",
            str(prompt),
        ]
        output = io.StringIO()
        with patch.object(sys, "argv", argv), \
                patch.object(goalflight_watch, "IncrementalTailScanner", FinishedScanner), \
                patch.object(goalflight_watch, "TraceLiveness", NoTrace), \
                patch.object(goalflight_watch, "worker_alive", return_value=(False, "dead", None)), \
                patch.object(goalflight_watch, "_dispatch_record_is_nonterminal", return_value=True), \
                patch.object(goalflight_watch, "_finish_existing_ledger", return_value=None), \
                patch.object(goalflight_watch.signal, "signal", return_value=None), \
                patch.object(goalflight_watch.atexit, "register", return_value=None), \
                contextlib.redirect_stdout(output):
            rc = goalflight_watch.main()

        payload = json.loads(status_json.read_text(encoding="utf-8"))
        assert rc == 1, (rc, output.getvalue(), payload)
        assert payload.get("state") == "worker_dead", payload
        assert "dispatch retired" not in output.getvalue()


def test_missing_status_without_nonterminal_record_returns_before_scan() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        tail = tmp / "orphan.tail"
        status_json = tmp / "orphan.status.json"
        prompt = dispatch_mod._acp_watcher_prompt_path(status_json)
        tail.write_text("stale worker output\n", encoding="utf-8")
        prompt.write_text("orphaned prompt\n", encoding="utf-8")
        argv = [
            str(ROOT / "scripts" / "goalflight_watch.py"),
            "--pid",
            "4242",
            "--tail",
            str(tail),
            "--status-json",
            str(status_json),
            "--dispatch-id",
            "orphan-without-record",
            "--ignore-prompt-file",
            str(prompt),
        ]
        output = io.StringIO()
        with patch.object(sys, "argv", argv), \
                patch.object(
                    goalflight_watch,
                    "IncrementalTailScanner",
                    side_effect=AssertionError("retired orphan initialized scanner"),
                ), \
                patch.object(
                    goalflight_watch,
                    "_dispatch_record_is_nonterminal",
                    return_value=False,
                ), \
                contextlib.redirect_stdout(output):
            rc = goalflight_watch.main()

        assert rc == 0
        assert output.getvalue().count("dispatch retired") == 1
        assert not status_json.exists()


def test_watcher_prompt_history_retains_original_and_bounded_recent_turns() -> None:
    history = goalflight_acp_run._WatcherPromptHistory()
    total_recent = goalflight_acp_run.WATCHER_PROMPT_HISTORY_RECENT_TURNS + 3
    history.append("original prompt")
    for turn in range(1, total_recent + 1):
        history.append(f"steer turn {turn}")

    persisted = history.render()
    assert history.original == "original prompt"
    assert len(history.recent) == goalflight_acp_run.WATCHER_PROMPT_HISTORY_RECENT_TURNS
    assert history.recent[0] == "steer turn 4"
    assert history.recent[-1] == f"steer turn {total_recent}"
    assert "steer turn 1\n\n" not in persisted
    assert "omitted 3 earlier turn(s)" in persisted
    assert "original prompt" in persisted
    assert persisted.count("watcher prompt history truncated") == 1


def _capacity_env(state_dir: Path, **extra: str) -> dict[str, str]:
    env = os.environ.copy()
    isolation_root = state_dir.parent
    env["GOALFLIGHT_STATE_DIR"] = str(state_dir)
    env["GOALFLIGHT_DISPATCH_DIR"] = str(state_dir / "dispatch")
    env["GOALFLIGHT_JOURNAL_DIR"] = str(isolation_root / "journal")
    env["GOALFLIGHT_TASK_STORE_DIR"] = str(isolation_root / "task-store")
    env["GOALFLIGHT_MESSAGES_DIR"] = str(isolation_root / "messages")
    env["GOALFLIGHT_WAKE_LEDGER_DIR"] = str(isolation_root / "wake-ledger")
    env["GOAL_FLIGHT_PIDFILE_DIR"] = str(isolation_root / "pids")
    env["GOALFLIGHT_CAPACITY_MAX_TOTAL"] = "1"
    env.update(extra)
    return env


def test_capacity_env_ignores_constructed_live_journal() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        live_journal_dir = tmp / "live-journal"
        with patch.dict(
            os.environ,
            {"GOALFLIGHT_JOURNAL_DIR": str(live_journal_dir)},
        ):
            live_authority = goalflight_journal.open_or_create_journal(ROOT)
            prepared = live_authority.prepare_attempt("queued-acp")
            assert prepared.committed and prepared.value is not None
            terminal = live_authority.commit_terminal(
                prepared.value.attempt_id,
                terminal_state="complete",
                observation={"source": "constructed live epoch-5 journal"},
            )
            assert terminal.committed
            env = _capacity_env(tmp / "isolated" / "state")

        expected = {
            "GOALFLIGHT_JOURNAL_DIR",
            "GOALFLIGHT_TASK_STORE_DIR",
            "GOALFLIGHT_MESSAGES_DIR",
            "GOALFLIGHT_WAKE_LEDGER_DIR",
            "GOAL_FLIGHT_PIDFILE_DIR",
        }
        assert expected <= env.keys()
        assert env["GOALFLIGHT_JOURNAL_DIR"] != str(live_journal_dir)
        with patch.dict(os.environ, env, clear=True):
            isolated_authority = goalflight_journal.open_or_create_journal(ROOT)
            assert isolated_authority.attempt_for_dispatch("queued-acp") is None

        live_attempt = live_authority.attempt_for_dispatch("queued-acp")
        assert live_attempt is not None
        assert live_attempt.lifecycle_state == goalflight_journal.ATTEMPT_TERMINAL


def _capacity_cmd(state_dir: Path, args: list[str]) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(
        [sys.executable, "scripts/goalflight_capacity.py", *args],
        cwd=ROOT,
        env=_capacity_env(state_dir),
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if proc.returncode != 0:
        raise AssertionError(f"capacity command failed: {proc.args}\nstdout={proc.stdout}\nstderr={proc.stderr}")
    return proc


def _hold_capacity(state_dir: Path, *, agent: str = "fake-acp", dispatch_id: str = "held-acp-capacity") -> str:
    proc = _capacity_cmd(
        state_dir,
        [
            "acquire",
            "--agent",
            agent,
            "--dispatch-id",
            dispatch_id,
            "--project-root",
            str(ROOT),
            "--ttl-s",
            "60",
        ],
    )
    return json.loads(proc.stdout)["lease"]["lease_id"]


def _release_capacity(state_dir: Path, lease_id: str) -> None:
    _capacity_cmd(state_dir, ["release", "--lease-id", lease_id])


def _wait_for_status(path: Path, state: str, *, timeout_s: float = 5.0) -> dict:
    deadline = time.time() + timeout_s
    last: dict | None = None
    while time.time() < deadline:
        if path.exists():
            try:
                last = json.loads(path.read_text())
            except (json.JSONDecodeError, OSError):
                pass
            else:
                if last.get("state") == state:
                    return last
        time.sleep(0.05)
    raise AssertionError(f"status {path} did not reach {state}; last={last}")


def _acp_cfg(tmp: Path, *, dispatch_id: str, status_json: Path, capacity_wait_s: float | None) -> SimpleNamespace:
    return goalflight_acp_run.normalized_acp_dispatch_cfg(
        SimpleNamespace(
            agent="fake-acp",
            model=None,
            install_slot=None,
            cwd=str(ROOT),
            worktree="off",
            session_id=None,
            dispatch_id=dispatch_id,
            priority="normal",
            capacity_wait_s=capacity_wait_s,
            prompt_id=None,
            prompt=None,
            prompt_text="COMPLETE: fake ACP done",
            prompt_b64=None,
            mode="one-shot",
            idle_timeout=5.0,
            status_json=str(status_json),
            steer_file=str(tmp / f"{dispatch_id}.steer.jsonl"),
            context_mode="disabled",
            os_sandbox=goalflight_acp_run.OS_SANDBOX_OFF,
            permission_mode="auto",
            permission_dir=None,
            permission_inline_timeout_s=None,
            permission_user_timeout_s=None,
            permission_allow_tool_title_pattern=[],
            read_only=False,
            interactive=False,
            heartbeat_interval=0.05,
            wedge_samples=1,
            max_tool_s=5.0,
            max_quiet_s=2.0,
            progress_stall_s=2.0,
            liveness_profile="local_compute",
            remote_turn_silence_s=None,
            remote_turn_cancel_grace_s=1.0,
            cpu_epsilon=0.1,
            unregistered_forced=True,
            json=False,
        )
    )


class _FakeAcpConn:
    def __init__(self, proc: subprocess.Popen) -> None:
        self.proc = proc
        self.client = SimpleNamespace(_prompt_in_use=False)
        self.acp_session_id = None
        self.os_sandbox_metadata = None

    async def close_gracefully(self) -> None:
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=2)

    async def kill(self) -> None:
        await self.close_gracefully()

    async def cancel(self) -> None:
        return None


def _install_fake_acp_after_capacity():
    old_spawn = goalflight_acp_run.spawn_and_handshake_with_retry
    old_prompt = goalflight_acp_run.run_prompt
    old_validate = goalflight_acp_run.validate_acp_dispatch_readiness

    async def fake_spawn(command, command_args, **kwargs):
        proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            cwd=kwargs.get("cwd"),
            env=kwargs.get("env"),
        )
        return proc, _FakeAcpConn(proc)

    async def fake_prompt(_conn, _text, **_kwargs):
        return goalflight_acp_run.PromptResult(
            text="COMPLETE: fake ACP done\n",
            stop_reason="end_turn",
        )

    goalflight_acp_run.spawn_and_handshake_with_retry = fake_spawn
    goalflight_acp_run.run_prompt = fake_prompt
    goalflight_acp_run.validate_acp_dispatch_readiness = lambda *_args, **_kwargs: None
    return old_spawn, old_prompt, old_validate


def _restore_fake_acp(saved) -> None:
    old_spawn, old_prompt, old_validate = saved
    goalflight_acp_run.spawn_and_handshake_with_retry = old_spawn
    goalflight_acp_run.run_prompt = old_prompt
    goalflight_acp_run.validate_acp_dispatch_readiness = old_validate


def _run_acp_thread(cfg: SimpleNamespace):
    result: dict[str, object] = {}

    def target() -> None:
        try:
            result["payload"] = asyncio.run(goalflight_acp_run.run_acp_dispatch(cfg))
        except BaseException as exc:  # pragma: no cover - re-raised below
            result["exc"] = exc

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    return thread, result


def test_acp_capacity_wait_queues_until_slot_frees() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        state_dir = tmp / "state"
        status_json = tmp / "queued-acp.status.json"
        lease_id = _hold_capacity(state_dir)
        cfg = _acp_cfg(tmp, dispatch_id="queued-acp", status_json=status_json, capacity_wait_s=6.0)
        saved = _install_fake_acp_after_capacity()
        try:
            with patch.dict(os.environ, _capacity_env(state_dir), clear=True):
                thread, result = _run_acp_thread(cfg)
                waiting = _wait_for_status(status_json, "waiting_capacity", timeout_s=5.0)
                assert waiting["reason"]["decision"] == "wait", waiting
                ledger = json.loads(
                    dispatch_mod.goalflight_ledger.record_path("queued-acp").read_text(
                        encoding="utf-8"
                    )
                )
                assert ledger["state"] == "waiting_capacity", ledger
                _release_capacity(state_dir, lease_id)
                thread.join(timeout=20)
                if thread.is_alive():
                    raise AssertionError("ACP queued run did not finish after capacity release")
                if "exc" in result:
                    raise result["exc"]  # type: ignore[misc]
                payload = result["payload"]
        finally:
            _restore_fake_acp(saved)
        final = json.loads(status_json.read_text())
        assert payload["state"] == "complete", payload
        assert final["state"] == "complete", final


def test_acp_capacity_acquire_error_terminalizes_direct_attempt() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        state_dir = tmp / "state"
        dispatch_id = "acp-capacity-acquire-error"
        status_json = tmp / f"{dispatch_id}.status.json"
        cfg = _acp_cfg(
            tmp,
            dispatch_id=dispatch_id,
            status_json=status_json,
            capacity_wait_s=0.0,
        )
        saved = _install_fake_acp_after_capacity()
        try:
            with (
                patch.dict(os.environ, _capacity_env(state_dir), clear=True),
                patch.object(
                    goalflight_acp_run.goalflight_capacity,
                    "acquire_with_wait_async",
                    side_effect=RuntimeError("capacity backend failed"),
                ),
            ):
                try:
                    asyncio.run(goalflight_acp_run.run_acp_dispatch(cfg))
                except RuntimeError as exc:
                    assert str(exc) == "capacity backend failed"
                else:
                    raise AssertionError("capacity acquisition error did not propagate")
                ledger_path = dispatch_mod.goalflight_ledger.record_path(dispatch_id)
        finally:
            _restore_fake_acp(saved)

        status = json.loads(status_json.read_text(encoding="utf-8"))
        ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
        assert status["state"] == "failed", status
        assert ledger["state"] == "failed", ledger
        assert ledger["terminal_state"] == "error", ledger


def test_acp_capacity_acquire_error_keeps_queue_attempt_prepared() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        state_dir = tmp / "state"
        dispatch_id = "queued-acp-capacity-acquire-error"
        status_json = tmp / f"{dispatch_id}.status.json"
        cfg = _acp_cfg(
            tmp,
            dispatch_id=dispatch_id,
            status_json=status_json,
            capacity_wait_s=0.0,
        )
        cfg.preserve_capacity_refusal_attempt = True
        saved = _install_fake_acp_after_capacity()
        try:
            with (
                patch.dict(os.environ, _capacity_env(state_dir), clear=True),
                patch.object(
                    goalflight_acp_run.goalflight_capacity,
                    "acquire_with_wait_async",
                    side_effect=RuntimeError("capacity backend failed"),
                ),
            ):
                try:
                    asyncio.run(goalflight_acp_run.run_acp_dispatch(cfg))
                except RuntimeError as exc:
                    assert str(exc) == "capacity backend failed"
                else:
                    raise AssertionError("capacity acquisition error did not propagate")

                status = json.loads(status_json.read_text(encoding="utf-8"))
                ledger = json.loads(
                    dispatch_mod.goalflight_ledger.record_path(dispatch_id).read_text(
                        encoding="utf-8"
                    )
                )
                attempt = goalflight_journal.open_or_create_journal(
                    ROOT
                ).attempt_for_dispatch(dispatch_id)
        finally:
            _restore_fake_acp(saved)

        assert attempt is not None
        assert attempt.lifecycle_state == goalflight_journal.ATTEMPT_PREPARED
        assert status["state"] == "starting", status
        assert "terminal_state" not in status, status
        assert ledger["state"] == "waiting_capacity", ledger
        assert ledger["terminal_state"] == "unknown", ledger


def test_acp_capacity_wait_deadline_blocks() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        state_dir = tmp / "state"
        status_json = tmp / "deadline-acp.status.json"
        lease_id = _hold_capacity(state_dir)
        cfg = _acp_cfg(tmp, dispatch_id="deadline-acp", status_json=status_json, capacity_wait_s=0.2)
        saved = _install_fake_acp_after_capacity()
        try:
            with patch.dict(os.environ, _capacity_env(state_dir), clear=True):
                payload = asyncio.run(goalflight_acp_run.run_acp_dispatch(cfg))
        finally:
            _restore_fake_acp(saved)
            _release_capacity(state_dir, lease_id)
        status = json.loads(status_json.read_text())
        assert payload["state"] == "blocked_capacity", payload
        assert status["reason"]["decision"] == "wait", status
        assert status["reason"]["attempts"] >= 2, status
        assert status["reason"]["waited_s"] >= 0.0, status


def test_acp_capacity_wait_zero_single_shot() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        state_dir = tmp / "state"
        status_json = tmp / "zero-acp.status.json"
        lease_id = _hold_capacity(state_dir)
        cfg = _acp_cfg(tmp, dispatch_id="zero-acp", status_json=status_json, capacity_wait_s=0.0)
        saved = _install_fake_acp_after_capacity()
        try:
            with patch.dict(os.environ, _capacity_env(state_dir), clear=True):
                payload = asyncio.run(goalflight_acp_run.run_acp_dispatch(cfg))
        finally:
            _restore_fake_acp(saved)
            _release_capacity(state_dir, lease_id)
        status = json.loads(status_json.read_text())
        assert payload["state"] == "blocked_capacity", payload
        assert "attempts" not in status["reason"] and "waited_s" not in status["reason"], status


def test_acp_capacity_wait_sigterm_terminalizes() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        state_dir = tmp / "state"
        status_json = tmp / "sigterm-acp.status.json"
        lease_id = _hold_capacity(state_dir)
        cfg = _acp_cfg(tmp, dispatch_id="sigterm-acp", status_json=status_json, capacity_wait_s=6.0)
        saved = _install_fake_acp_after_capacity()
        signal_thread_errors: list[BaseException] = []

        def send_sigterm_after_waiting() -> None:
            try:
                _wait_for_status(status_json, "waiting_capacity", timeout_s=5.0)
                os.kill(os.getpid(), signal.SIGTERM)
            except BaseException as exc:  # pragma: no cover - surfaced below
                signal_thread_errors.append(exc)

        signal_thread = threading.Thread(target=send_sigterm_after_waiting, daemon=True)
        try:
            with patch.dict(os.environ, _capacity_env(state_dir), clear=True):
                signal_thread.start()
                payload = asyncio.run(goalflight_acp_run.run_acp_dispatch(cfg))
        finally:
            _restore_fake_acp(saved)
            _release_capacity(state_dir, lease_id)
        signal_thread.join(timeout=1)
        if signal_thread_errors:
            raise signal_thread_errors[0]
        status = json.loads(status_json.read_text())
        assert payload["state"] == "blocked_capacity", payload
        assert status["state"] == "blocked_capacity", status
        assert status["reason"]["reason"] == "wait_interrupted", status
        assert status["reason"]["attempts"] == 1, status
        assert status["reason"]["waited_s"] < 6.0, status


def _main_capture_for(agent: str) -> tuple[int, dict[str, object]]:
    captured: dict[str, object] = {}
    old_argv = sys.argv[:]
    old_run = dispatch_mod._run_acp_shape
    old_assign_session = dispatch_mod._ensure_assigned_engine_session
    old_auth_preflight = dispatch_mod._validate_claude_auth_before_attempt
    old_state_dir = os.environ.get("GOALFLIGHT_STATE_DIR")

    class ShapeResolved(Exception):
        pass

    def fake_run(args, *, base: Path, account_env: dict[str, str]) -> int:
        captured["agent"] = args.agent
        captured["shape"] = args.shape
        captured["base"] = str(base)
        captured["account_env"] = dict(account_env)
        return 0

    def stop_before_bash_launch(args) -> None:
        if args.shape == "bash":
            captured["agent"] = args.agent
            captured["shape"] = args.shape
            raise ShapeResolved
        old_assign_session(args)

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        try:
            os.environ["GOALFLIGHT_STATE_DIR"] = str(tmp / "state")
            dispatch_mod._run_acp_shape = fake_run
            dispatch_mod._ensure_assigned_engine_session = stop_before_bash_launch
            dispatch_mod._validate_claude_auth_before_attempt = lambda *_args, **_kwargs: None
            sys.argv = [
                "goalflight_dispatch.py",
                "--agent",
                agent,
                "--unregistered-forced",
                "--prompt",
                "COMPLETE: no-op",
                "--cwd",
                str(tmp),
            ]
            try:
                rc = dispatch_mod.main()
            except ShapeResolved:
                rc = 0
        finally:
            dispatch_mod._run_acp_shape = old_run
            dispatch_mod._ensure_assigned_engine_session = old_assign_session
            dispatch_mod._validate_claude_auth_before_attempt = old_auth_preflight
            sys.argv = old_argv
            if old_state_dir is None:
                os.environ.pop("GOALFLIGHT_STATE_DIR", None)
            else:
                os.environ["GOALFLIGHT_STATE_DIR"] = old_state_dir
    return rc, captured


def test_auto_shape_routes_claude_to_acp_and_cursor_to_bash() -> None:
    """Cursor moved to bash-tail; claude keeps acp.

    This asserted that cursor auto-routed to acp. It no longer does: bash-tail is
    measured good for cursor (five of five trials correct, exit 0, ~50s each)
    while acp is the path with no evidence behind it, so cursor now resolves to
    bash and acp is refused outright rather than merely deprioritised.

    The claude half is unchanged and still checked here — the point of the
    original test was that auto-resolution routes deliberately per engine, and
    that is still the property worth pinning.
    """
    # Stop the bash route after shape resolution: this test must not launch a
    # real cursor worker merely to observe which transport main() selected.
    rc, captured = _main_capture_for("cursor")
    assert rc == 0
    assert captured["shape"] == "bash"
    assert captured["agent"] == "cursor"

    rc, captured = _main_capture_for("claude-acp")
    assert rc == 0
    assert captured["shape"] == "acp"
    assert captured["agent"] == "claude"


def test_claude_auth_preflight_refuses_before_attempt() -> None:
    completed = subprocess.CompletedProcess(
        ["claude", "auth", "status", "--json"],
        0,
        stdout='{"loggedIn": false, "authMethod": "none"}\n',
        stderr="",
    )
    auth_probe_calls = 0
    validate_auth = dispatch_mod._validate_claude_auth_before_attempt

    def fake_auth_preflight(args, account_env) -> None:
        nonlocal auth_probe_calls

        def fake_runner(argv, **kwargs):
            nonlocal auth_probe_calls
            auth_probe_calls += 1
            assert argv == ["claude", "auth", "status", "--json"]
            assert "ANTHROPIC_API_KEY" not in kwargs["env"]
            return completed

        validate_auth(args, account_env, runner=fake_runner)

    for shape in ("acp", "bash"):
        dispatch_id = f"claude-auth-preflight-refusal-{shape}"
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            env = {
                "GOALFLIGHT_STATE_DIR": str(tmp / "state"),
                "GOALFLIGHT_JOURNAL_DIR": str(tmp / "journal"),
                "GOALFLIGHT_MESSAGES_DIR": str(tmp / "messages"),
                "GOALFLIGHT_WAKE_LEDGER_DIR": str(tmp / "wake-ledger"),
                "GOALFLIGHT_TASK_STORE_DIR": str(tmp / "task-store"),
                "GOAL_FLIGHT_PIDFILE_DIR": str(tmp / "pidfiles"),
                "GOALFLIGHT_CAPACITY_CONF": "/dev/null",
            }
            stderr = io.StringIO()
            with (
                patch.dict(os.environ, env, clear=False),
                patch.object(
                    dispatch_mod,
                    "_validate_claude_auth_before_attempt",
                    side_effect=fake_auth_preflight,
                ),
                patch.object(
                    dispatch_mod,
                    "_run_acp_shape",
                    side_effect=AssertionError("unauthenticated Claude reached ACP launch"),
                ),
                patch.object(
                    dispatch_mod,
                    "_spawn_daemonized_process",
                    side_effect=AssertionError("unauthenticated Claude reached bash launch"),
                ),
                contextlib.redirect_stderr(stderr),
            ):
                rc = dispatch_mod.main(
                    [
                        "--agent",
                        "claude",
                        "--shape",
                        shape,
                        "--foreground",
                        "--unregistered-forced",
                        "--dispatch-id",
                        dispatch_id,
                        "--prompt",
                        "write a trivial probe file",
                        "--cwd",
                        str(tmp),
                    ]
                )
                assert not dispatch_mod.goalflight_ledger.record_path(dispatch_id).exists()
                assert not list((tmp / "journal").rglob("state-journal.sqlite3"))

        assert rc == 64, stderr.getvalue()
        assert "not authenticated" in stderr.getvalue(), stderr.getvalue()
        assert "claude auth login" in stderr.getvalue(), stderr.getvalue()
    assert auth_probe_calls == 2


def test_claude_auth_preflight_accepts_logged_in_session() -> None:
    calls = 0

    def fake_runner(argv, **kwargs):
        nonlocal calls
        calls += 1
        assert argv == ["claude", "auth", "status", "--json"]
        assert "ANTHROPIC_API_KEY" not in kwargs["env"]
        return subprocess.CompletedProcess(
            argv,
            0,
            stdout='{"loggedIn": true, "authMethod": "oauth"}\n',
            stderr="",
        )

    args = SimpleNamespace(agent="claude", billing="sub")
    with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "must-be-stripped"}):
        dispatch_mod._validate_claude_auth_before_attempt(
            args,
            {},
            runner=fake_runner,
        )
    assert calls == 1


def _run_acp_shape_env_capture(agent: str, env_key: str) -> dict[str, str | None]:
    captured: dict[str, str | None] = {}
    old_run = goalflight_acp_run.run_acp_dispatch
    old_value = os.environ.get(env_key)

    async def fake_run(cfg):
        captured[env_key] = os.environ.get(env_key)
        return {
            "state": "complete",
            "dispatch_id": cfg.dispatch_id,
            "agent": cfg.agent,
            "worker_pid": None,
            "worker_alive": False,
        }

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        args = _base_acp_args(tmp, agent=agent, dispatch_id=f"{agent}-env")
        try:
            os.environ[env_key] = "must-not-leak"
            goalflight_acp_run.run_acp_dispatch = fake_run
            rc = dispatch_mod._run_acp_shape(args, base=tmp / "dispatch", account_env={})
        finally:
            goalflight_acp_run.run_acp_dispatch = old_run
            if old_value is None:
                os.environ.pop(env_key, None)
            else:
                os.environ[env_key] = old_value
    assert rc == 0
    return captured


def test_subscription_env_scrub_for_cursor_and_claude_acp() -> None:
    assert _run_acp_shape_env_capture("cursor", "CURSOR_API_KEY")["CURSOR_API_KEY"] is None
    assert _run_acp_shape_env_capture("claude", "ANTHROPIC_API_KEY")["ANTHROPIC_API_KEY"] is None


def main() -> None:
    test_normalize_acp_agents()
    test_build_acp_cfg_agent_liveness_defaults()
    test_build_acp_cfg_injects_orientation_prompt_text()
    test_acp_production_path_persists_and_reminds_with_assembled_prompt()
    test_acp_inline_prompt_uses_same_assembled_prompt_path()
    test_acp_prompt_history_rewrite_remains_private()
    test_dispatch_help_skips_legacy_prompt_sweep_mutation_pair()
    test_dispatch_startup_sweep_is_once_and_best_effort_mutation_pair()
    test_dispatch_startup_sweep_marker_serializes_concurrent_first_run()
    test_watcher_with_retired_prompt_and_status_exits_without_status_write()
    test_watcher_both_absent_returns_before_scanner_initialization()
    test_nonterminal_dispatch_record_authorizes_missing_status_creation()
    test_missing_status_without_nonterminal_record_returns_before_scan()
    test_watcher_prompt_history_retains_original_and_bounded_recent_turns()
    test_capacity_env_ignores_constructed_live_journal()
    test_acp_capacity_wait_queues_until_slot_frees()
    test_acp_capacity_acquire_error_terminalizes_direct_attempt()
    test_acp_capacity_acquire_error_keeps_queue_attempt_prepared()
    test_acp_capacity_wait_deadline_blocks()
    test_acp_capacity_wait_zero_single_shot()
    test_acp_capacity_wait_sigterm_terminalizes()
    test_auto_shape_routes_claude_to_acp_and_cursor_to_bash()
    test_claude_auth_preflight_refuses_before_attempt()
    test_claude_auth_preflight_accepts_logged_in_session()
    test_subscription_env_scrub_for_cursor_and_claude_acp()
    print("OK: goalflight_dispatch ACP agent tests pass")


if __name__ == "__main__":
    main()
