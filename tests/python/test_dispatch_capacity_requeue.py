#!/usr/bin/env python3
"""Capacity refusals stay visible; only pre-existing queue carriers retry."""

from __future__ import annotations

from support import skip_posix_on_native_windows

skip_posix_on_native_windows("capacity requeue tests launch POSIX bash workers")

import json
import os
import shlex
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
DISPATCH = ROOT / "scripts" / "goalflight_dispatch.py"
CAPACITY = ROOT / "scripts" / "goalflight_capacity.py"
FAKE_ACP_AGENT = ROOT / "tests" / "fixtures" / "acp_fake_agent.py"
sys.path.insert(0, str(ROOT / "scripts"))

import goalflight_dispatch as D  # noqa: E402


def _env(tmp: Path) -> dict[str, str]:
    env = os.environ.copy()
    env.update(
        {
            "GOALFLIGHT_STATE_DIR": str(tmp / "state"),
            "GOALFLIGHT_TASK_STORE_DIR": str(tmp / "task-store"),
            "GOALFLIGHT_JOURNAL_DIR": str(tmp / "journal"),
            "GOALFLIGHT_MESSAGES_DIR": str(tmp / "messages"),
            "GOALFLIGHT_WAKE_LEDGER_DIR": str(tmp / "wake-ledger"),
            "GOAL_FLIGHT_PIDFILE_DIR": str(tmp / "pids"),
            "GOALFLIGHT_CAPACITY_CONF": "/dev/null",
            "GOALFLIGHT_CAPACITY_MAX_TOTAL": "1",
            "GOALFLIGHT_TEST_PROJECT_ROOT": str(tmp),
        }
    )
    env.pop("GOALFLIGHT_CAPACITY_WAIT_S", None)
    return env


def _run(
    argv: list[str],
    env: dict[str, str],
    *,
    cwd: Path = ROOT,
    timeout: float = 30.0,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        cwd=cwd,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
    )


def _hold_capacity(tmp: Path, env: dict[str, str], dispatch_id: str) -> str:
    held = _run(
        [
            sys.executable,
            str(CAPACITY),
            "acquire",
            "--agent",
            "test-dispatch",
            "--dispatch-id",
            dispatch_id,
            "--project-root",
            str(tmp),
            "--controller-pid",
            str(os.getpid()),
            "--ttl-s",
            "60",
        ],
        env,
    )
    assert held.returncode == 0, (held.stdout, held.stderr)
    payload = json.loads(held.stdout)
    assert payload["decision"] == "allow", payload
    return str(payload["lease"]["lease_id"])


def _release_capacity(env: dict[str, str], lease_id: str) -> None:
    released = _run(
        [
            sys.executable,
            str(CAPACITY),
            "release",
            "--lease-id",
            lease_id,
        ],
        env,
    )
    assert released.returncode == 0, (released.stdout, released.stderr)


def _dispatch_command(
    tmp: Path,
    dispatch_id: str,
    worker_code: str,
    *,
    extra: list[str] | None = None,
) -> list[str]:
    return [
        sys.executable,
        str(DISPATCH),
        "--unregistered-forced",
        "--agent",
        "test-dispatch",
        "--dispatch-id",
        dispatch_id,
        "--tail",
        str(tmp / f"{dispatch_id}.tail"),
        "--status-json",
        str(tmp / f"{dispatch_id}.status.json"),
        "--poll-secs",
        "0.1",
        "--max-idle-secs",
        "10",
        "--cwd",
        str(tmp),
        *(extra or []),
        "--",
        sys.executable,
        "-c",
        worker_code,
    ]


def _write_fake_codex_acp(tmp: Path, spawned: Path) -> Path:
    wrapper = tmp / "codex-acp"
    wrapper.write_text(
        "#!/usr/bin/env bash\n"
        f"printf spawned > {shlex.quote(str(spawned))}\n"
        f"exec {shlex.quote(sys.executable)} {shlex.quote(str(FAKE_ACP_AGENT))}\n",
        encoding="utf-8",
    )
    wrapper.chmod(0o755)
    return wrapper


def _acp_dispatch_command(
    tmp: Path,
    dispatch_id: str,
    *,
    extra: list[str] | None = None,
) -> list[str]:
    return [
        sys.executable,
        str(DISPATCH),
        "--unregistered-forced",
        "--agent",
        "codex-acp",
        "--shape",
        "acp",
        "--dispatch-id",
        dispatch_id,
        "--prompt",
        f"COMPLETE: {dispatch_id} — ACP replay accepted",
        "--tail",
        str(tmp / f"{dispatch_id}.tail"),
        "--status-json",
        str(tmp / f"{dispatch_id}.status.json"),
        "--poll-secs",
        "0.1",
        "--max-idle-secs",
        "10",
        "--cwd",
        str(tmp),
        *(extra or []),
    ]


def _wait_for(predicate, *, label: str, timeout: float = 20.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    assert predicate(), f"{label} not met before timeout"


def _read_json(path: Path) -> dict:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _write_queue_entry(
    tmp: Path,
    *,
    dispatch_id: str,
    agent: str,
    shape: str,
    replay_argv: list[str],
) -> Path:
    queue_path = tmp / "state" / "dispatch-queue" / f"{dispatch_id}.json"
    queue_path.parent.mkdir(parents=True)
    D._write_json_atomic(
        queue_path,
        {
            "schema": D.DISPATCH_QUEUE_SCHEMA,
            "state": "queued",
            "dispatch_id": dispatch_id,
            "agent": agent,
            "shape": shape,
            "project_root": str(tmp),
            "process_cwd": str(tmp),
            "worker_cwd": str(tmp),
            "created_at": "2026-01-01T00:00:00+00:00",
            "updated_at": "2026-01-01T00:00:00+00:00",
            "queue_path": str(queue_path),
            "dispatch_argv": replay_argv,
            "request": {
                "agent": agent,
                "cwd": str(tmp),
                "tail": str(tmp / f"{dispatch_id}.tail"),
                "status_json": str(tmp / f"{dispatch_id}.status.json"),
            },
        },
    )
    return queue_path


def test_detached_capacity_refusal_is_visible_and_not_queued() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        env = _env(tmp)
        _hold_capacity(tmp, env, "held-for-visible-refusal")
        dispatch_id = "detached-visible-capacity-refusal"
        status_path = tmp / f"{dispatch_id}.status.json"
        queue_path = tmp / "state" / "dispatch-queue" / f"{dispatch_id}.json"

        refused = _run(
            _dispatch_command(
                tmp,
                dispatch_id,
                "raise SystemExit('must not run')",
                extra=["--capacity-wait-s", "0"],
            ),
            env,
        )

        assert refused.returncode == 2, (refused.stdout, refused.stderr)
        assert "DISPATCH-BLOCKED" in refused.stdout, refused.stdout
        assert _read_json(status_path).get("state") == "blocked_capacity"
        assert not queue_path.exists(), "capacity refusal silently created a queue entry"


def test_capacity_refusal_guard_mirrors() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        env = _env(tmp)
        _hold_capacity(tmp, env, "held-for-guard-mirrors")

        for dispatch_id, guard in (
            ("from-queue-refusal", ["--from-queue"]),
            ("foreground-refusal", ["--foreground"]),
        ):
            result = _run(
                _dispatch_command(
                    tmp,
                    dispatch_id,
                    "raise SystemExit('must not run')",
                    extra=["--capacity-wait-s", "0", *guard],
                ),
                env,
            )
            assert result.returncode == 2, (dispatch_id, result.stdout, result.stderr)
            status = _read_json(tmp / f"{dispatch_id}.status.json")
            assert status.get("state") == "blocked_capacity", (dispatch_id, status)
            ledger = _read_json(tmp / "state" / "runs.d" / f"{dispatch_id}.json")
            assert ledger.get("state") == "blocked_capacity", (dispatch_id, ledger)
            queue_path = tmp / "state" / "dispatch-queue" / f"{dispatch_id}.json"
            assert not queue_path.exists(), f"{dispatch_id} incorrectly created a queue entry"

def test_acp_capacity_refusal_foreground() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        env = _env(tmp)
        spawned = tmp / "guard-acp-worker-spawned"
        _write_fake_codex_acp(tmp, spawned)
        env["PATH"] = f"{tmp}{os.pathsep}{env.get('PATH', '')}"
        env["GOALFLIGHT_ACP_PYTHON"] = sys.executable
        dispatch_id = "acp-foreground-refusal"
        held_lease = _hold_capacity(tmp, env, f"held-for-{dispatch_id}")
        try:
            result = _run(
                _acp_dispatch_command(
                    tmp,
                    dispatch_id,
                    extra=["--capacity-wait-s", "0", "--foreground"],
                ),
                env,
            )
        finally:
            _release_capacity(env, held_lease)
        assert result.returncode == 1, (dispatch_id, result.stdout, result.stderr)
        status = _read_json(tmp / f"{dispatch_id}.status.json")
        assert status.get("state") == "blocked_capacity", (dispatch_id, status)
        queue_path = tmp / "state" / "dispatch-queue" / f"{dispatch_id}.json"
        assert not queue_path.exists(), f"{dispatch_id} incorrectly re-enqueued"


def test_acp_detached_capacity_refusal_is_visible_and_not_queued() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        env = _env(tmp)
        spawned = tmp / "guard-acp-worker-spawned"
        _write_fake_codex_acp(tmp, spawned)
        env["PATH"] = f"{tmp}{os.pathsep}{env.get('PATH', '')}"
        env["GOALFLIGHT_ACP_PYTHON"] = sys.executable
        dispatch_id = "acp-detached-visible-refusal"
        held_lease = _hold_capacity(tmp, env, f"held-for-{dispatch_id}")
        try:
            result = _run(
                _acp_dispatch_command(
                    tmp,
                    dispatch_id,
                    extra=["--capacity-wait-s", "0"],
                ),
                env,
            )
        finally:
            _release_capacity(env, held_lease)
        assert result.returncode == 2, (dispatch_id, result.stdout, result.stderr)
        assert "DISPATCH-BLOCKED" in result.stdout, result.stdout
        status = _read_json(tmp / f"{dispatch_id}.status.json")
        assert status.get("state") == "blocked_capacity", (dispatch_id, status)
        queue_path = tmp / "state" / "dispatch-queue" / f"{dispatch_id}.json"
        assert not queue_path.exists(), f"{dispatch_id} incorrectly re-enqueued"
        assert not spawned.exists(), "ACP worker spawned despite capacity refusal"


def test_detached_capacity_wait_interrupt_does_not_enqueue() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        env = _env(tmp)
        _hold_capacity(tmp, env, "held-for-interrupt")
        dispatch_id = "detached-wait-interrupted"
        status_path = tmp / f"{dispatch_id}.status.json"
        proc = subprocess.Popen(
            _dispatch_command(
                tmp,
                dispatch_id,
                "raise SystemExit('must not run')",
                extra=["--capacity-wait-s", "30"],
            ),
            cwd=ROOT,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            _wait_for(
                lambda: _read_json(status_path).get("state") == "waiting_capacity",
                label="bash waiting-capacity status",
            )
            proc.send_signal(signal.SIGTERM)
            stdout, stderr = proc.communicate(timeout=20)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=10)
        assert proc.returncode == 143, (stdout, stderr)
        status = _read_json(status_path)
        assert status.get("state") == "blocked_capacity", status
        assert (status.get("reason") or {}).get("reason") == "wait_interrupted", status
        queue_path = tmp / "state" / "dispatch-queue" / f"{dispatch_id}.json"
        assert not queue_path.exists(), "operator interrupt was mistaken for capacity refusal"


def test_preexisting_queue_capacity_refusal_still_restores_one_entry() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        env = _env(tmp)
        held_lease = _hold_capacity(tmp, env, "held-for-queue-regression")
        dispatch_id = "preexisting-queue-capacity-regression"
        queue_dir = tmp / "state" / "dispatch-queue"
        marker = tmp / "queued-worker-ran"
        worker_code = (
            "from pathlib import Path; "
            f"Path({str(marker)!r}).write_text('once', encoding='utf-8'); "
            f"print('COMPLETE: {dispatch_id} — restored queue entry ran', flush=True)"
        )
        replay_argv = _dispatch_command(tmp, dispatch_id, worker_code)[2:]
        queue_path = _write_queue_entry(
            tmp,
            dispatch_id=dispatch_id,
            agent="test-dispatch",
            shape="bash",
            replay_argv=replay_argv,
        )

        old_env = os.environ.copy()
        original_release = D._release_stale_capacity_for_drain
        original_hook = D._run_drain_prelaunch_hook
        try:
            os.environ.clear()
            os.environ.update(env)
            D._release_stale_capacity_for_drain = lambda: None
            D._run_drain_prelaunch_hook = lambda _agents: None
            payload = D._drain_queue_once(
                SimpleNamespace(
                    queue_dir=str(queue_dir),
                    remote_node=None,
                    capacity_wait_s=0.0,
                    claim_stale_s=D.QUEUE_CLAIM_STALE_S,
                    limit=1,
                    dispatch_id=None,
                )
            )
        finally:
            D._release_stale_capacity_for_drain = original_release
            D._run_drain_prelaunch_hook = original_hook
            os.environ.clear()
            os.environ.update(old_env)
        assert payload["launched"] == 0, f"launched={payload['launched']} payload={payload}"
        assert payload["left_queued"] == 1, (
            f"detail={payload['details'][0] if payload['details'] else None} "
            f"left={payload['left_queued']} launched={payload['launched']} "
            f"failed={payload['failed']} remaining={payload['remaining']} "
            f"pending={payload['pending_claims']} busy={payload['skipped_busy']} "
            f"error={payload['skipped_error']} details={payload['details']} "
            f"holds={payload['holds']}"
        )
        assert payload["pending_claims"] == 0, f"pending_claims={payload['pending_claims']} payload={payload}"
        assert queue_path.exists(), "capacity refusal lost the pre-existing queue entry"
        assert list(queue_dir.glob("*.json")) == [queue_path], "drain duplicated the queue entry"
        assert not list(queue_dir.glob("*.claimed.*")), "drain stranded a claimed carrier"

        held_record = _read_json(tmp / "state" / "runs.d" / f"{dispatch_id}.json")
        assert held_record.get("state") == "queued", held_record
        _release_capacity(env, held_lease)
        launched = _run(
            [sys.executable, str(DISPATCH), "drain", "--capacity-wait-s", "0", "--json"],
            env,
            cwd=tmp,
        )
        assert launched.returncode == 0, (launched.stdout, launched.stderr)
        launched_payload = json.loads(launched.stdout)
        assert launched_payload["launched"] == 1, launched_payload
        assert not queue_path.exists(), "restored queue entry was not consumed"
        _wait_for(
            lambda: marker.exists() and marker.read_text(encoding="utf-8") == "once",
            label="restored backlog worker marker",
        )
        _wait_for(
            lambda: _read_json(tmp / f"{dispatch_id}.status.json").get("state") == "complete"
            and _read_json(tmp / f"{dispatch_id}.status.json").get("worker_alive") is not True,
            label="restored backlog terminal status",
        )


def test_acp_preexisting_queue_capacity_refusal_restores_claim() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        env = _env(tmp)
        spawned = tmp / "queued-acp-worker-spawned"
        _write_fake_codex_acp(tmp, spawned)
        env["PATH"] = f"{tmp}{os.pathsep}{env.get('PATH', '')}"
        env["GOALFLIGHT_ACP_PYTHON"] = sys.executable
        held_lease = _hold_capacity(tmp, env, "held-for-acp-queue-regression")
        dispatch_id = "preexisting-acp-queue-capacity-regression"
        queue_dir = tmp / "state" / "dispatch-queue"
        replay_argv = _acp_dispatch_command(tmp, dispatch_id)[2:]
        queue_path = _write_queue_entry(
            tmp,
            dispatch_id=dispatch_id,
            agent="codex-acp",
            shape="acp",
            replay_argv=replay_argv,
        )

        old_env = os.environ.copy()
        original_release = D._release_stale_capacity_for_drain
        original_hook = D._run_drain_prelaunch_hook
        try:
            os.environ.clear()
            os.environ.update(env)
            D._release_stale_capacity_for_drain = lambda: None
            D._run_drain_prelaunch_hook = lambda _agents: None
            payload = D._drain_queue_once(
                SimpleNamespace(
                    queue_dir=str(queue_dir),
                    remote_node=None,
                    capacity_wait_s=0.0,
                    claim_stale_s=D.QUEUE_CLAIM_STALE_S,
                    limit=1,
                    dispatch_id=None,
                )
            )
        finally:
            D._release_stale_capacity_for_drain = original_release
            D._run_drain_prelaunch_hook = original_hook
            os.environ.clear()
            os.environ.update(old_env)
            _release_capacity(env, held_lease)

        assert payload["launched"] == 0, f"launched={payload['launched']} payload={payload}"
        assert payload["left_queued"] == 1, (
            f"detail={payload['details'][0] if payload['details'] else None} "
            f"left={payload['left_queued']} launched={payload['launched']} "
            f"failed={payload['failed']} remaining={payload['remaining']} "
            f"pending={payload['pending_claims']} busy={payload['skipped_busy']} "
            f"error={payload['skipped_error']} details={payload['details']} "
            f"holds={payload['holds']}"
        )
        assert payload["pending_claims"] == 0, f"pending_claims={payload['pending_claims']} payload={payload}"
        assert queue_path.exists(), "ACP refusal lost the pre-existing queue entry"
        assert list(queue_dir.glob("*.json")) == [queue_path], "drain duplicated the ACP queue entry"
        assert not list(queue_dir.glob("*.json.claimed-*")), "drain stranded the ACP queue claim"
        assert not spawned.exists(), "ACP worker spawned despite capacity refusal"


def test_detached_default_wait_matches_foreground_and_keeps_overrides() -> None:
    old = os.environ.pop("GOALFLIGHT_CAPACITY_WAIT_S", None)
    try:
        detached = SimpleNamespace(
            priority="normal", capacity_wait_s=None, foreground=False, from_queue=False
        )
        foreground = SimpleNamespace(
            priority="normal", capacity_wait_s=None, foreground=True, from_queue=False
        )
        explicit = SimpleNamespace(
            priority="normal", capacity_wait_s=7.0, foreground=False, from_queue=False
        )
        assert D._capacity_wait_seconds(detached) == 600.0
        assert D._capacity_wait_seconds(foreground) == 600.0
        assert D._capacity_wait_seconds(explicit) == 7.0
        os.environ["GOALFLIGHT_CAPACITY_WAIT_S"] = "4.5"
        assert D._capacity_wait_seconds(detached) == 4.5
    finally:
        if old is None:
            os.environ.pop("GOALFLIGHT_CAPACITY_WAIT_S", None)
        else:
            os.environ["GOALFLIGHT_CAPACITY_WAIT_S"] = old


def test_acp_cfg_uses_the_shared_detached_wait_policy() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        args = SimpleNamespace(
            agent="codex-acp",
            cwd=str(tmp),
            prompt_file=None,
            prompt="COMPLETE: cfg only",
            no_orientation=True,
            dispatch_id="acp-wait-policy",
            task_ids=[],
            priority="normal",
            capacity_wait_s=None,
            foreground=False,
            from_queue=False,
            max_idle_secs=10.0,
            read_only=False,
            poll_secs=0.1,
            permission_mode="auto",
            permission_dir=None,
            permission_inline_timeout_s=None,
            permission_user_timeout_s=None,
            permission_allow_tool_title_pattern=[],
            interactive=False,
            unregistered_forced=True,
            queue_launch_token=None,
            queue_claim_path=None,
        )
        old_state = os.environ.get("GOALFLIGHT_STATE_DIR")
        old_wait = os.environ.pop("GOALFLIGHT_CAPACITY_WAIT_S", None)
        os.environ["GOALFLIGHT_STATE_DIR"] = str(tmp / "state")
        try:
            cfg = D._build_acp_cfg(
                args,
                status_json=tmp / "acp-wait-policy.status.json",
                base=tmp,
            )
            assert cfg.capacity_wait_s == 600.0
            assert cfg.preserve_capacity_refusal_attempt is False
            args.from_queue = True
            args.queue_launch_token = "queue-token"
            args.queue_claim_path = str(tmp / "queued.claimed")
            queued = D._build_acp_cfg(
                args,
                status_json=tmp / "acp-queued-wait.status.json",
                base=tmp,
            )
            assert queued.preserve_capacity_refusal_attempt is True
            args.from_queue = False
            args.queue_launch_token = None
            args.queue_claim_path = None
            args.capacity_wait_s = 3.25
            explicit = D._build_acp_cfg(
                args,
                status_json=tmp / "acp-explicit-wait.status.json",
                base=tmp,
            )
            assert explicit.capacity_wait_s == 3.25
            args.capacity_wait_s = None
            os.environ["GOALFLIGHT_CAPACITY_WAIT_S"] = "4.5"
            overridden = D._build_acp_cfg(
                args,
                status_json=tmp / "acp-env-wait.status.json",
                base=tmp,
            )
            assert overridden.capacity_wait_s == 4.5
        finally:
            if old_state is None:
                os.environ.pop("GOALFLIGHT_STATE_DIR", None)
            else:
                os.environ["GOALFLIGHT_STATE_DIR"] = old_state
            if old_wait is None:
                os.environ.pop("GOALFLIGHT_CAPACITY_WAIT_S", None)
            else:
                os.environ["GOALFLIGHT_CAPACITY_WAIT_S"] = old_wait


if __name__ == "__main__":
    test_detached_capacity_refusal_is_visible_and_not_queued()
    test_capacity_refusal_guard_mirrors()
    test_acp_capacity_refusal_foreground()
    test_acp_detached_capacity_refusal_is_visible_and_not_queued()
    test_detached_capacity_wait_interrupt_does_not_enqueue()
    test_preexisting_queue_capacity_refusal_still_restores_one_entry()
    test_acp_preexisting_queue_capacity_refusal_restores_claim()
    test_detached_default_wait_matches_foreground_and_keeps_overrides()
    test_acp_cfg_uses_the_shared_detached_wait_policy()
    print("ok: dispatch capacity refusal requeue")
