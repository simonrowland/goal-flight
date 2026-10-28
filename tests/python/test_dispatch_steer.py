#!/usr/bin/env python3
"""Regression tests for goalflight_dispatch.py steer mailbox routing."""

from __future__ import annotations

import argparse
import contextlib
import ctypes
import io
import json
import os
import shlex
import stat
import subprocess
import sys
import tempfile
import time
import traceback
from pathlib import Path
from types import SimpleNamespace

from support import skip_case_posix_on_native_windows

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
DISPATCH = SCRIPTS / "goalflight_dispatch.py"
sys.path.insert(0, str(SCRIPTS))

import goalflight_dispatch  # noqa: E402
import goalflight_compat  # noqa: E402
import goalflight_journal  # noqa: E402
import goalflight_ledger  # noqa: E402
import goalflight_messages  # noqa: E402
import goalflight_task  # noqa: E402
import goalflight_steer_mailbox  # noqa: E402
import goalflight_terminal  # noqa: E402
import goalflight_watch  # noqa: E402

PROJECT_ROOT = goalflight_task.resolve_project_root(str(ROOT))


def _attach_cleanup_failure(body_failure: BaseException, cleanup_failure: BaseException) -> None:
    if isinstance(body_failure, SystemExit):
        # CPython prints tracebacks (and, on 3.11+, notes) for uncaught
        # exceptions other than SystemExit. It handles SystemExit separately:
        # its code determines the process status, its traceback/notes are not
        # rendered, and a non-int code is printed separately. Render cleanup
        # before allowing that exit to propagate; other exceptions keep notes.
        # If stderr is closed or broken, there is no sink for this diagnostic;
        # losing it is deliberate, and must not replace the original exit.
        with contextlib.suppress(OSError, ValueError):
            print(
                f"additional cleanup failure: {cleanup_failure!r}",
                file=sys.stderr,
                flush=True,
            )
        return
    add_note = getattr(body_failure, "add_note", None)
    if callable(add_note):
        add_note(f"additional cleanup failure: {cleanup_failure!r}")
    else:
        raise body_failure from cleanup_failure


@contextlib.contextmanager
def _state_dir(tmp: Path, *, project_root: Path | None = PROJECT_ROOT):
    isolated = {
        "GOALFLIGHT_STATE_DIR": str(tmp),
        "GOALFLIGHT_DISPATCH_DIR": str(tmp / "dispatch"),
        "GOALFLIGHT_MESSAGES_DIR": str(tmp / "messages"),
        "GOAL_FLIGHT_PIDFILE_DIR": str(tmp / "pids"),
        "GOALFLIGHT_TASK_STORE_DIR": str(tmp / "task-store"),
        "GOALFLIGHT_JOURNAL_DIR": str(tmp / "journal"),
        "GOALFLIGHT_WAKE_LEDGER_DIR": str(tmp / "wake-ledger"),
    }
    if project_root is not None:
        isolated["GOALFLIGHT_PROJECT_ROOT"] = str(project_root)
    old = {key: os.environ.get(key) for key in isolated}
    os.environ.update(isolated)
    failure: BaseException | None = None
    try:
        yield
    except BaseException as exc:
        failure = exc
        raise
    finally:
        try:
            for key, value in old.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
        except BaseException as cleanup_error:
            if failure is None:
                raise
            _attach_cleanup_failure(failure, cleanup_error)


def _env(tmp: Path, *, project_root: Path = PROJECT_ROOT) -> dict[str, str]:
    env = os.environ.copy()
    env["GOALFLIGHT_STATE_DIR"] = str(tmp)
    env["GOALFLIGHT_DISPATCH_DIR"] = str(tmp / "dispatch")
    env["GOALFLIGHT_MESSAGES_DIR"] = str(tmp / "messages")
    env["GOALFLIGHT_PROJECT_ROOT"] = str(project_root)
    env["GOAL_FLIGHT_PIDFILE_DIR"] = str(tmp / "pids")
    env["GOALFLIGHT_TASK_STORE_DIR"] = str(tmp / "task-store")
    env["GOALFLIGHT_JOURNAL_DIR"] = str(tmp / "journal")
    env["GOALFLIGHT_WAKE_LEDGER_DIR"] = str(tmp / "wake-ledger")
    env["PYTHONPATH"] = str(SCRIPTS) + os.pathsep + env.get("PYTHONPATH", "")
    return env


def _host_pool_snapshot() -> tuple[object, str]:
    common_dir = subprocess.run(
        ["git", "-C", str(ROOT), "rev-parse", "--git-common-dir"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    common_path = Path(common_dir)
    if not common_path.is_absolute():
        common_path = ROOT / common_path
    lock_root = common_path.resolve() / "goalflight-worktree-seat-locks"
    locks: object = None
    if lock_root.exists():
        rows: list[tuple[object, ...]] = []
        for current, directories, files in os.walk(lock_root):
            directories.sort()
            files.sort()
            current_path = Path(current)
            rel = current_path.relative_to(lock_root)
            current_stat = current_path.stat()
            rows.append(("dir", str(rel), current_stat.st_mtime_ns))
            for name in files:
                path = current_path / name
                path_stat = path.stat()
                rows.append(("file", str(path.relative_to(lock_root)), path_stat.st_mtime_ns, path.read_bytes()))
        locks = tuple(rows)
    worktrees = subprocess.run(
        ["git", "-C", str(ROOT), "worktree", "list", "--porcelain"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return locks, worktrees


@contextlib.contextmanager
def _host_pool_guard():
    before = _host_pool_snapshot()
    failure: BaseException | None = None
    try:
        yield
    except BaseException as exc:
        failure = exc
        raise
    finally:
        after = _host_pool_snapshot()
        if before != after:
            message = (
                "test mutated the enclosing repository's worktree pool; "
                "dispatch launch was not isolated"
            )
            if failure is None:
                raise AssertionError(message)
            _attach_cleanup_failure(failure, AssertionError(message))


def _mailbox(tmp: Path, dispatch_id: str) -> Path:
    return tmp / "dispatch" / f"{dispatch_id}.steer.jsonl"


def _read_mailbox(tmp: Path, dispatch_id: str) -> list[dict]:
    path = _mailbox(tmp, dispatch_id)
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _read_messages(tmp: Path, dispatch_id: str) -> list[dict]:
    path = tmp / "messages" / f"{dispatch_id}.jsonl"
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _record(
    tmp: Path,
    dispatch_id: str,
    *,
    shape: str = "bash",
    worker_pid: int | None = None,
    controller_label: str | None = None,
    controller_session_id: str | None = None,
    stdout_path: Path | None = None,
    status_path: Path | None = None,
) -> None:
    status_path = status_path or (tmp / f"{dispatch_id}.status.json")
    with _state_dir(tmp), contextlib.redirect_stdout(io.StringIO()):
        def record_state(state: str) -> int:
            return goalflight_ledger.cmd_record(
                argparse.Namespace(
                dispatch_id=dispatch_id,
                prompt_id=None,
                prompt_path=None,
                agent="test-dispatch",
                engine="test",
                shape=shape,
                account="default",
                transport="dispatch",
                project_root=str(ROOT),
                controller_pid=os.getpid(),
                controller_label=controller_label,
                controller_session_id=controller_session_id,
                worker_pid=worker_pid,
                acp_session_id="session-1" if shape == "acp" else None,
                logical_session_id=dispatch_id,
                lease_id=None,
                stdout_path=str(stdout_path) if stdout_path else None,
                stderr_path=None,
                status_path=str(status_path),
                os_sandbox_json=json.dumps({"shape": shape}, sort_keys=True),
                state=state,
                json=True,
            )
        )
        assert record_state("waiting_capacity") == 0
        assert record_state("starting") == 0
        authority = goalflight_journal.Journal(ROOT)
        attempt = authority.attempt_for_dispatch(dispatch_id)
        assert attempt is not None
        assert authority.mark_attempt_running(
            attempt.attempt_id,
            attempt.launch_token,
            launch_epoch=attempt.launch_epoch,
            worker_instance={"pid": worker_pid or os.getpid(), "source": "steer-test"},
        ).committed
        assert record_state("running") == 0


def _run_steer(tmp: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(DISPATCH), "steer", *args],
        env=_env(tmp),
        capture_output=True,
        text=True,
        timeout=20,
    )


def _run_wait_reply_command(
    tmp: Path,
    command: str,
    *,
    answer: str,
    decision: str,
) -> subprocess.CompletedProcess[str]:
    tokens = shlex.split(command)
    assert tokens[0] == "goalflight_dispatch.py" and tokens[1] == "steer", tokens
    arguments = [
        answer if token == "<answer>"
        else decision if token == "<yes|no>"
        else token
        for token in tokens[1:]
    ]
    return subprocess.run(
        [sys.executable, str(DISPATCH), *arguments],
        env=_env(tmp),
        capture_output=True,
        text=True,
        timeout=20,
    )


def _reply_command_from_refusal(stderr: str) -> str:
    marker = "use: "
    assert marker in stderr, stderr
    return stderr.split(marker, 1)[1].split("; next:", 1)[0].strip()


def _worker_wait_env(tmp: Path, dispatch_id: str) -> dict[str, str]:
    env = _env(tmp)
    env["GOALFLIGHT_DISPATCH_ID"] = dispatch_id
    env["GOALFLIGHT_STEER_FILE"] = str(_mailbox(tmp, dispatch_id))
    with _state_dir(tmp):
        attempt = goalflight_journal.Journal(ROOT).attempt_for_dispatch(dispatch_id)
    if attempt is None:
        raise AssertionError(f"missing active attempt for {dispatch_id}")
    env["GOALFLIGHT_ATTEMPT_ID"] = attempt.attempt_id
    env["GOALFLIGHT_WORKER_TAIL_FILE"] = str(tmp / f"{dispatch_id}.tail")
    outbox = tmp / "dispatch" / f"{dispatch_id}.questions"
    outbox.mkdir(parents=True, exist_ok=True)
    env["GOALFLIGHT_QUESTION_OUTBOX_DIR"] = str(outbox)
    return env


def _run_worker_wait(tmp: Path, dispatch_id: str, *args: str) -> subprocess.CompletedProcess[str]:
    env = _worker_wait_env(tmp, dispatch_id)
    return subprocess.run(
        [sys.executable, str(DISPATCH), "steer", dispatch_id, "--wait", *args],
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
    )


def _start_watched_question(
    tmp: Path,
    dispatch_id: str,
    *,
    timeout_secs: float,
    pause_after_wait: bool,
) -> tuple[subprocess.Popen[str], subprocess.Popen[str], Path, Path, Path]:
    tail = tmp / f"{dispatch_id}.tail"
    status = tmp / f"{dispatch_id}.status.json"
    config_path = tmp / f"{dispatch_id}.worker.json"
    release = tmp / f"{dispatch_id}.continue"
    release.touch()
    env = _env(tmp)
    env["GOALFLIGHT_CAPACITY_CONF"] = "/dev/null"
    env["GOALFLIGHT_FLEET_CONSOLE_OUTPUT_DIR"] = str(tmp / "fleet-console")
    env["GOALFLIGHT_TEST_MODE"] = "1"
    worker_code = (
        "import json, os, pathlib, subprocess, sys, time\n"
        "config_path = pathlib.Path(sys.argv[1])\n"
        "release = pathlib.Path(sys.argv[2])\n"
        "while not config_path.exists(): time.sleep(0.01)\n"
        "config = json.loads(config_path.read_text(encoding='utf-8'))\n"
        "env = os.environ.copy()\n"
        "env.update(config['env'])\n"
        "result = subprocess.run(config['command'], env=env)\n"
        "if result.returncode != config['wait_returncode']:\n"
        "    raise SystemExit(20 + result.returncode)\n"
        "if config['pause_after_wait']:\n"
        "    while release.exists(): time.sleep(0.01)\n"
        "print('RESULT: ' + config['dispatch_id'] + ' — continued work ingested', flush=True)\n"
        "print('COMPLETE: ' + config['dispatch_id'] + ' — final result', flush=True)\n"
    )
    with tail.open("wb") as output:
        worker = subprocess.Popen(
            [sys.executable, "-c", worker_code, str(config_path), str(release)],
            cwd=tmp,
            env=env,
            stdout=output,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            text=False,
        )
    watcher = None
    try:
        _record(
            tmp,
            dispatch_id,
            worker_pid=worker.pid,
            stdout_path=tail,
            status_path=status,
        )
        wait_env = _worker_wait_env(tmp, dispatch_id)
        wait_env["GOALFLIGHT_CAPACITY_CONF"] = "/dev/null"
        watcher = subprocess.Popen(
            [
                sys.executable,
                str(SCRIPTS / "goalflight_watch.py"),
                "--pid",
                str(worker.pid),
                "--pgid",
                str(worker.pid),
                "--tail",
                str(tail),
                "--status-json",
                str(status),
                "--dispatch-id",
                dispatch_id,
                "--agent",
                "test-dispatch",
                "--project-root",
                str(ROOT),
                "--worker-cwd",
                str(tmp),
                "--poll-secs",
                "0.05",
                "--max-idle-secs",
                "30",
                "--stay-after-terminal",
            ],
            cwd=ROOT,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not status.exists():
            time.sleep(0.01)
        assert status.exists(), "watcher did not publish its initial status"
        config_path.write_text(
            json.dumps(
                {
                    "command": [
                        sys.executable,
                        str(DISPATCH),
                        "steer",
                        dispatch_id,
                        "need a controller decision",
                        "--wait",
                        "--question-kind",
                        "USER-NEED",
                        "--timeout-secs",
                        str(timeout_secs),
                        "--poll-secs",
                        "0.02",
                    ],
                    "dispatch_id": dispatch_id,
                    "env": {
                        key: wait_env[key]
                        for key in (
                            "GOALFLIGHT_DISPATCH_ID",
                            "GOALFLIGHT_STEER_FILE",
                            "GOALFLIGHT_ATTEMPT_ID",
                            "GOALFLIGHT_WORKER_TAIL_FILE",
                            "GOALFLIGHT_QUESTION_OUTBOX_DIR",
                        )
                    },
                    "pause_after_wait": pause_after_wait,
                    "wait_returncode": 1 if pause_after_wait else 0,
                },
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        return worker, watcher, tail, status, release
    except BaseException as setup_error:
        try:
            _stop_watched_question(worker, watcher, release)
        except BaseException as cleanup_error:
            raise setup_error from cleanup_error
        raise


def _stop_watched_question(
    worker: subprocess.Popen[str],
    watcher: subprocess.Popen[str] | None,
    release: Path,
) -> None:
    cleanup_errors: list[Exception] = []
    try:
        release.unlink(missing_ok=True)
    except Exception as exc:
        cleanup_errors.append(exc)
    for process in (worker, watcher):
        if process is None:
            continue
        try:
            if process.poll() is None:
                process.terminate()
        except Exception as exc:
            cleanup_errors.append(exc)
    for process in (worker, watcher):
        if process is None:
            continue
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                process.kill()
            except Exception as exc:
                cleanup_errors.append(exc)
            try:
                process.wait(timeout=5)
            except Exception as exc:
                cleanup_errors.append(exc)
        except Exception as exc:
            cleanup_errors.append(exc)
    if cleanup_errors:
        raise cleanup_errors[0]


def case_b723_timed_out_question_does_not_hide_completion() -> None:
    with tempfile.TemporaryDirectory() as directory:
        tmp = Path(directory)
        dispatch_id = "b723-timeout-continues"
        worker, watcher, tail, status, release = _start_watched_question(
            tmp,
            dispatch_id,
            timeout_secs=0.3,
            pause_after_wait=True,
        )
        try:
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                if tail.exists() and "- wait timed out;" in tail.read_text(
                    encoding="utf-8", errors="replace"
                ):
                    break
                time.sleep(0.02)
            else:
                raise AssertionError("worker did not publish its timed-out question")
            time.sleep(0.2)
            payload = json.loads(status.read_text(encoding="utf-8"))
            assert worker.poll() is None, "worker is held after its wait timed out"
            assert watcher.poll() is None, (watcher.returncode, payload)
            assert payload.get("state") != "blocked", payload
            assert not payload.get("terminal_marker"), payload
            attention_marker = payload.get("attention_marker")
            assert isinstance(attention_marker, dict), payload
            assert attention_marker.get("kind") == "BLOCKED", payload
            assert "wait timed out" in str(attention_marker.get("text") or "").lower(), payload
            with _state_dir(tmp):
                record = goalflight_dispatch._find_dispatch_record(dispatch_id)
                assert record is not None
                try:
                    goalflight_dispatch._assert_codex_resume_source_not_live(
                        record, dispatch_id
                    )
                except goalflight_dispatch.DispatchUsageError as exc:
                    message = str(exc)
                    assert "question wait timed out" in message, message
                    assert "need a controller decision" in message, message
                    assert "wait-id=" in message, message
                else:
                    raise AssertionError("resume must report the stale timed-out question")

            release.unlink()
            worker.wait(timeout=10)
            output, _error = watcher.communicate(timeout=10)
            assert watcher.returncode == 0, (watcher.returncode, output, payload)
            payload = json.loads(status.read_text(encoding="utf-8"))
            assert payload.get("state") == "complete", payload
            assert payload.get("terminal_marker", {}).get("kind") == "COMPLETE", payload
            with _state_dir(tmp):
                record = goalflight_dispatch._find_dispatch_record(dispatch_id)
                outbox = goalflight_journal.Journal(ROOT).read_all(
                    "SELECT event_type, payload_json FROM terminal_outbox WHERE recipient = ?",
                    (dispatch_id,),
                )
            assert record and record.get("terminal_state") == "complete", record
            assert str(record.get("headline") or "").endswith("final result"), record
            assert len(outbox) == 1 and outbox[0]["event_type"] == "result", outbox
            assert "final result" in json.loads(outbox[0]["payload_json"])["text"], outbox
        finally:
            _stop_watched_question(worker, watcher, release)


def case_b723_active_question_is_one_open_wait() -> None:
    with tempfile.TemporaryDirectory() as directory:
        tmp = Path(directory)
        dispatch_id = "b723-open-question"
        worker, watcher, _tail, status, release = _start_watched_question(
            tmp,
            dispatch_id,
            timeout_secs=30,
            pause_after_wait=False,
        )
        try:
            deadline = time.monotonic() + 10
            payload: dict = {}
            while time.monotonic() < deadline:
                try:
                    payload = json.loads(status.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    time.sleep(0.02)
                    continue
                if payload.get("state") == "awaiting_steer_reply" and isinstance(
                    payload.get("worker_wait"), dict
                ):
                    break
                time.sleep(0.02)
            else:
                raise AssertionError(f"question did not remain an open wait: {payload}")
            assert worker.poll() is None and watcher.poll() is None, payload
            wait_id = str(payload["worker_wait"].get("wait_id") or "")
            assert wait_id, payload
            inbox = tmp / "messages" / f"{dispatch_id}.jsonl"
            envelopes = [
                json.loads(line)
                for line in inbox.read_text(encoding="utf-8").splitlines()
            ]
            question = next(event for event in envelopes if event["type"] == "user_need")
            reply_command = question["payload"]["reply_command"]
            assert reply_command.index('"<answer>"') < reply_command.index("--reply-to"), reply_command

            with _state_dir(tmp):
                record = goalflight_dispatch._find_dispatch_record(dispatch_id)
                assert record is not None
                try:
                    goalflight_dispatch._assert_codex_resume_source_not_live(
                        record, dispatch_id
                    )
                except goalflight_dispatch.DispatchUsageError as exc:
                    assert "live and waiting for a question reply" in str(exc), str(exc)
                else:
                    raise AssertionError("resume must refuse the live open question")

            reply = _run_steer(tmp, dispatch_id, "approved", "--reply-to", wait_id)
            assert reply.returncode == 0, reply.stdout + reply.stderr
            assert "steer appended:" in reply.stdout, reply.stdout
            worker.wait(timeout=10)
            output, _error = watcher.communicate(timeout=10)
            assert watcher.returncode == 0, (watcher.returncode, output)
            payload = json.loads(status.read_text(encoding="utf-8"))
            assert payload.get("state") == "complete", payload
            with _state_dir(tmp):
                record = goalflight_dispatch._find_dispatch_record(dispatch_id)
            assert record and record.get("terminal_state") == "complete", record
        finally:
            _stop_watched_question(worker, watcher, release)


def case_b723_repair_keeps_attention_marker_for_indeterminate_worker() -> None:
    with tempfile.TemporaryDirectory() as directory:
        tmp = Path(directory)
        dispatch_id = "b723-repair-live-attention"
        status = tmp / "status.json"
        tail = tmp / "tail.txt"
        tail.write_text("worker continues after BLOCKED\n", encoding="utf-8")
        attention = {
            "kind": "BLOCKED",
            "dispatch_id": dispatch_id,
            "line": 4,
            "sequence": 4,
            "text": f"{dispatch_id} — waiting for controller",
            "timestamp": "2026-10-22T00:00:00+00:00",
        }
        status.write_text(
            json.dumps(
                {
                    "schema": "goalflight.status.v1",
                    "dispatch_id": dispatch_id,
                    "state": "running",
                    "worker_alive": False,
                    "worker_identity_reason": "identity_indeterminate",
                    "attention_marker": attention,
                }
            ),
            encoding="utf-8",
        )
        original_worker_probe = goalflight_dispatch._worker_alive_from_identity
        original_final_marker = goalflight_dispatch._final_terminal_marker
        goalflight_dispatch._worker_alive_from_identity = lambda _pid, _identity: (
            False,
            "identity_indeterminate",
        )
        goalflight_dispatch._final_terminal_marker = lambda *_args, **_kwargs: None
        try:
            repaired = goalflight_dispatch._repair_watcher_terminal_status(
                status,
                args=SimpleNamespace(dispatch_id=dispatch_id, agent="codex"),
                tail=tail,
                worker_pid=12345,
                worker_identity={"pid": 12345},
                pgid=None,
                prompt_path=None,
                reason="watcher_stopped",
            )
        finally:
            goalflight_dispatch._worker_alive_from_identity = original_worker_probe
            goalflight_dispatch._final_terminal_marker = original_final_marker
        persisted = json.loads(status.read_text(encoding="utf-8"))
        assert repaired.get("state") == "watcher_stopped", repaired
        assert repaired.get("terminal_pending_state") == "blocked", repaired
        assert repaired.get("terminal_marker") is None, repaired
        assert repaired.get("attention_marker") == attention, repaired
        assert persisted.get("attention_marker") == attention, persisted
        for identity_reason in ("no_pid", "identity_check_error:PermissionError"):
            assert (
                goalflight_terminal.worker_liveness_from_probe(
                    False, identity_reason
                )
                == "unknown"
            ), identity_reason


def case_b723_repair_clears_stale_blocked_pending_for_complete() -> None:
    with tempfile.TemporaryDirectory() as directory:
        tmp = Path(directory)
        dispatch_id = "b723-repair-newer-complete"
        status = tmp / "status.json"
        tail = tmp / "tail.txt"
        tail.write_text(f"!COMPLETE: {dispatch_id} — report\n", encoding="utf-8")
        old_attention = {
            "kind": "BLOCKED",
            "dispatch_id": dispatch_id,
            "line": 3,
            "sequence": 3,
            "text": f"{dispatch_id} — older blocked outcome",
        }
        status.write_text(
            json.dumps(
                {
                    "schema": "goalflight.status.v1",
                    "dispatch_id": dispatch_id,
                    "state": "watcher_stopped",
                    "terminal_pending_state": "blocked",
                    "attention_marker": old_attention,
                }
            ),
            encoding="utf-8",
        )
        originals = {
            "worker_probe": goalflight_dispatch._worker_alive_from_identity,
            "project_root": goalflight_dispatch._project_root,
            "export": goalflight_dispatch._export_dashboard_status_for_project,
        }
        goalflight_dispatch._worker_alive_from_identity = lambda _pid, _identity: (
            False,
            "identity_mismatch",
        )
        goalflight_dispatch._project_root = lambda _args: tmp
        goalflight_dispatch._export_dashboard_status_for_project = lambda _root: None
        try:
            repaired = goalflight_dispatch._repair_watcher_terminal_status(
                status,
                args=SimpleNamespace(dispatch_id=dispatch_id, agent="codex"),
                tail=tail,
                worker_pid=12345,
                worker_identity={"pid": 12345},
                pgid=None,
                prompt_path=None,
                reason="watcher_stopped",
            )
        finally:
            goalflight_dispatch._worker_alive_from_identity = originals["worker_probe"]
            goalflight_dispatch._project_root = originals["project_root"]
            goalflight_dispatch._export_dashboard_status_for_project = originals["export"]
        assert repaired.get("state") == "complete", repaired
        assert repaired.get("terminal_marker", {}).get("kind") == "COMPLETE", repaired
        assert "terminal_pending_state" not in repaired, repaired
        assert "attention_marker" not in repaired, repaired


def case_b723_repair_keeps_live_blocked_provisional_after_rate_limit_normalization() -> None:
    with tempfile.TemporaryDirectory() as directory:
        tmp = Path(directory)
        dispatch_id = "b723-repair-rate-limit-live-blocked"
        status = tmp / "status.json"
        tail = tmp / "tail.txt"
        tail.write_text(
            "You've hit your usage limit. Please try again at Jun 21st.\n",
            encoding="utf-8",
        )
        attention = {
            "kind": "BLOCKED",
            "dispatch_id": dispatch_id,
            "line": 3,
            "sequence": 3,
            "text": f"{dispatch_id} — waiting for controller",
        }
        status.write_text(
            json.dumps(
                {
                    "schema": "goalflight.status.v1",
                    "dispatch_id": dispatch_id,
                    "state": "watcher_stopped",
                    "worker_alive": True,
                    "attention_marker": attention,
                    "terminal_pending_state": "blocked",
                }
            ),
            encoding="utf-8",
        )
        originals = {
            "worker_probe": goalflight_dispatch._worker_alive_from_identity,
            "final_marker": goalflight_dispatch._final_terminal_marker,
            "project_root": goalflight_dispatch._project_root,
            "export": goalflight_dispatch._export_dashboard_status_for_project,
        }
        goalflight_dispatch._worker_alive_from_identity = lambda _pid, _identity: (
            True,
            "identity_match",
        )
        goalflight_dispatch._final_terminal_marker = lambda *_args, **_kwargs: None
        goalflight_dispatch._project_root = lambda _args: tmp
        goalflight_dispatch._export_dashboard_status_for_project = lambda _root: None
        try:
            repaired = goalflight_dispatch._repair_watcher_terminal_status(
                status,
                args=SimpleNamespace(dispatch_id=dispatch_id, agent="codex"),
                tail=tail,
                worker_pid=12345,
                worker_identity={"pid": 12345, "start_token": "test:12345:generation-1"},
                pgid=None,
                prompt_path=None,
                reason="watcher_stopped",
            )
        finally:
            goalflight_dispatch._worker_alive_from_identity = originals["worker_probe"]
            goalflight_dispatch._final_terminal_marker = originals["final_marker"]
            goalflight_dispatch._project_root = originals["project_root"]
            goalflight_dispatch._export_dashboard_status_for_project = originals["export"]
        assert repaired.get("state") == "watcher_stopped", repaired
        assert repaired.get("terminal_pending_state") == "blocked", repaired
        assert repaired.get("terminal_marker") is None, repaired
        assert repaired.get("attention_marker") == attention, repaired


def case_bash_append_and_list_with_ack() -> None:
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        dispatch_id = "steer-list"
        tail = tmp / "tail.log"
        tail.write_text("STATUS: running\nSTEER-ACK: 1\n", encoding="utf-8")
        _record(tmp, dispatch_id, worker_pid=os.getpid(), stdout_path=tail)

        first = _run_steer(tmp, dispatch_id, "hello one")
        second = _run_steer(tmp, dispatch_id, "hello two")
        assert first.returncode == 0, first.stderr
        assert second.returncode == 0, second.stderr
        assert "worker_alive=true" in first.stdout, first.stdout

        multiline = "first line\nsecond line\nthird line"
        multiline_result = _run_steer(tmp, dispatch_id, multiline)
        assert multiline_result.returncode == 0, multiline_result.stderr
        assert "worker_alive=true" in multiline_result.stdout, multiline_result.stdout

        entries = _read_mailbox(tmp, dispatch_id)
        assert [entry["seq"] for entry in entries] == [1, 2, 3], entries
        assert [
            entry["text"].startswith(text)
            for entry, text in zip(entries, ("hello one", "hello two", multiline))
        ] == [True, True, True], entries
        envelopes = [
            json.loads(line)
            for line in (tmp / "messages" / f"{dispatch_id}.jsonl").read_text(encoding="utf-8").splitlines()
        ]
        assert [envelope["payload"]["text"] for envelope in envelopes] == [
            "hello one", "hello two", multiline,
        ], envelopes

        listed = _run_steer(tmp, dispatch_id, "--list")
        assert listed.returncode == 0, listed.stderr
        assert "seq\tts\tacked\ttext" in listed.stdout, listed.stdout
        assert "\ttrue\t" in listed.stdout and "hello one" in listed.stdout, listed.stdout
        assert "\tfalse\t" in listed.stdout and "hello two" in listed.stdout, listed.stdout


def case_shape_routing_and_missing_record() -> None:
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        _record(tmp, "acp-mailbox", shape="acp", worker_pid=os.getpid())
        acp = _run_steer(tmp, "acp-mailbox", "redirect")
        assert acp.returncode == 0, acp.stdout + acp.stderr
        assert "steer appended:" in acp.stdout, acp.stdout
        entries = _read_mailbox(tmp, "acp-mailbox")
        assert len(entries) == 1 and entries[0]["text"].startswith("redirect"), entries

        missing = _run_steer(tmp, "missing-dispatch", "redirect")
        assert missing.returncode != 0, missing.stdout + missing.stderr
        assert missing.stderr.startswith("NOT RECORDED: no ledger record"), missing.stderr
        assert len(missing.stderr.splitlines()) == 1, missing.stderr


def case_append_refusals_keep_original_error() -> None:
    for error in (ValueError("bad steer value"), OSError("steer storage unavailable")):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            dispatch_id = "steer-append-refusal"
            _record(tmp, dispatch_id, worker_pid=os.getpid())
            original = goalflight_dispatch._append_steer_message

            def refuse(*_args, _error=error, **_kwargs):
                raise _error

            stderr = io.StringIO()
            try:
                goalflight_dispatch._append_steer_message = refuse
                with _state_dir(tmp), contextlib.redirect_stderr(stderr), contextlib.redirect_stdout(io.StringIO()):
                    rc = goalflight_dispatch._cmd_steer([dispatch_id, "notice"])
            finally:
                goalflight_dispatch._append_steer_message = original

            lines = stderr.getvalue().splitlines()
            assert rc == 64, (type(error).__name__, rc, lines)
            assert len(lines) == 1 and lines[0].startswith("NOT RECORDED: "), lines
            assert str(error) in lines[0], lines


def case_oversized_multiline_steer_refuses_before_write() -> None:
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        dispatch_id = "steer-oversized-multiline"
        _record(tmp, dispatch_id, worker_pid=os.getpid())
        text = "first line\n" + ("x" * goalflight_messages.MAX_PAYLOAD_JSON_BYTES)
        stderr = io.StringIO()
        with _state_dir(tmp), contextlib.redirect_stderr(stderr), contextlib.redirect_stdout(io.StringIO()):
            rc = goalflight_dispatch._cmd_steer([dispatch_id, text])

        lines = stderr.getvalue().splitlines()
        assert rc == 64, (rc, lines)
        assert len(lines) == 1 and lines[0].startswith("NOT RECORDED: "), lines
        assert "canonical JSON" in lines[0] and "limit is" in lines[0], lines
        assert _read_messages(tmp, dispatch_id) == []
        assert _read_mailbox(tmp, dispatch_id) == []


def case_steer_sender_identity_and_cross_project_guard() -> None:
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        dispatch_id = "steer-sender-identity"
        _record(tmp, dispatch_id, worker_pid=os.getpid())
        old_env = {
            key: os.environ.get(key)
            for key in (
                "GOALFLIGHT_CONTROLLER_LABEL",
                "GOALFLIGHT_CONTROLLER_PID",
                "GOALFLIGHT_PROJECT_ROOT",
            )
        }
        old_delivery = goalflight_messages._deliver_message_to_worker

        def pretend_delivery(_dispatch_id: str, _envelope: dict, **_kwargs) -> dict:
            return {
                "requested": True,
                "delivered": True,
                "worker_view_written": True,
                "status": "worker_view_written",
                "steer_seq": 2,
                "steer_path": str(_mailbox(tmp, dispatch_id)),
            }

        foreign = tmp / "foreign-project"
        foreign.mkdir()
        try:
            os.environ["GOALFLIGHT_CONTROLLER_LABEL"] = "controller-alpha"
            os.environ["GOALFLIGHT_CONTROLLER_PID"] = "4242"
            os.environ["GOALFLIGHT_PROJECT_ROOT"] = str(PROJECT_ROOT)
            goalflight_messages._deliver_message_to_worker = pretend_delivery
            with _state_dir(tmp):
                first = goalflight_messages.post_controller_steer(
                    dispatch_id,
                    "inspect the worker state",
                )
                envelope = first["envelope"]
                expected_sender_root = envelope["source"]["project_root"]
                _path, entry = goalflight_steer_mailbox.append_message_view(
                    dispatch_id,
                    envelope,
                    state_dir=tmp / "state",
                )
            sender = envelope["source"]
            assert sender["controller_label"] == "controller-alpha", sender
            assert sender["controller_pid"] == 4242, sender
            assert Path(sender["project_root"]).is_absolute(), sender
            assert entry["sender"] == {
                "controller_label": "controller-alpha",
                "controller_pid": 4242,
                "project_root": expected_sender_root,
            }, entry
            assert "controller_label=controller-alpha" in entry["text"], entry
            assert "controller_pid=4242" in entry["text"], entry
            assert expected_sender_root in entry["text"], entry

            os.environ["GOALFLIGHT_PROJECT_ROOT"] = str(ROOT)
            with _state_dir(tmp, project_root=ROOT):
                linked = goalflight_messages.post_controller_steer(
                    dispatch_id,
                    "same repository from linked worktree",
                )
            assert linked["recorded"] is True, linked
            messages_before_refusal = _read_messages(tmp, dispatch_id)
            mailbox_before_refusal = _read_mailbox(tmp, dispatch_id)

            os.environ["GOALFLIGHT_PROJECT_ROOT"] = str(foreign)
            with _state_dir(tmp, project_root=foreign):
                try:
                    goalflight_messages.post_controller_steer(
                        dispatch_id,
                        "foreign directive",
                    )
                except goalflight_messages.MessageError as exc:
                    refusal = str(exc)
                else:
                    raise AssertionError("foreign steer was not refused")
            assert "sender project_root" in refusal, refusal
            assert "target dispatch" in refusal, refusal
            assert "--cross-project" in refusal, refusal
            assert _read_messages(tmp, dispatch_id) == messages_before_refusal
            assert _read_mailbox(tmp, dispatch_id) == mailbox_before_refusal

            with _state_dir(tmp, project_root=foreign), contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                rc = goalflight_dispatch.main(
                    [
                        "steer",
                        dispatch_id,
                        "foreign directive",
                        "--cross-project",
                    ]
                )
            assert rc == 0
            allowed = _read_messages(tmp, dispatch_id)[-1]
            _path, allowed_entry = goalflight_steer_mailbox.append_message_view(
                dispatch_id,
                allowed,
                state_dir=tmp / "state",
            )
            assert allowed["source"]["cross_project"] is True
            assert allowed["payload"]["sender"]["cross_project"] is True
            assert allowed_entry["cross_project"] is True, allowed_entry
        finally:
            goalflight_messages._deliver_message_to_worker = old_delivery
            for key, value in old_env.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value


def case_steer_unknown_project_root_fails_closed() -> None:
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        dispatch_id = "steer-unknown-project"
        _record(tmp, dispatch_id, worker_pid=os.getpid())
        with _state_dir(tmp):
            record_path = goalflight_ledger.record_path(dispatch_id, create=False)
            record = json.loads(record_path.read_text(encoding="utf-8"))
            record.pop("project_root", None)
            record_path.write_text(json.dumps(record), encoding="utf-8")

        proc = _run_steer(tmp, dispatch_id, "halt")
        assert proc.returncode == 64, proc.stdout + proc.stderr
        assert "project_root identity is unknown" in proc.stderr, proc.stderr
        assert _read_messages(tmp, dispatch_id) == []


def case_acp_list_reads_status_ack_dict() -> None:
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        dispatch_id = "acp-status-ack"
        status = tmp / "status.json"
        status.write_text(json.dumps({"markers": {"STEER-ACK": ["1"]}}), encoding="utf-8")
        _record(tmp, dispatch_id, shape="acp", worker_pid=os.getpid(), status_path=status)

        proc = _run_steer(tmp, dispatch_id, "redirect")
        assert proc.returncode == 0, proc.stdout + proc.stderr

        listed = _run_steer(tmp, dispatch_id, "--list")
        assert listed.returncode == 0, listed.stderr
        assert "\ttrue\t" in listed.stdout and "redirect" in listed.stdout, listed.stdout


def case_prefixed_ack_is_parsed_by_both_call_sites() -> None:
    with tempfile.TemporaryDirectory() as d:
        tail = Path(d) / "tail.log"
        tail.write_text("!STEER-ACK: 7\n", encoding="utf-8")
        record = {"stdout_path": str(tail)}
        mailbox_acks = goalflight_steer_mailbox.acked_steer_seqs(record)
        dispatch_acks = goalflight_dispatch._acked_steer_seqs(record)
        regexes_share_definition = (
            goalflight_dispatch.STEER_ACK_RE
            is goalflight_steer_mailbox.STEER_ACK_RE
            is goalflight_terminal.STEER_ACK_RE
        )
        regex_values = [
            match.group(1) if match else None
            for match in (
                goalflight_dispatch.STEER_ACK_RE.match("!STEER-ACK: 7"),
                goalflight_steer_mailbox.STEER_ACK_RE.match("!STEER-ACK: 7"),
            )
        ]
        assert (
            mailbox_acks == {7}
            and dispatch_acks == {7}
            and regexes_share_definition
            and regex_values == ["7", "7"]
        ), (mailbox_acks, dispatch_acks, regexes_share_definition, regex_values)


def case_dead_worker_records_but_does_not_claim_delivery() -> None:
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        _record(tmp, "dead-worker")
        proc = _run_steer(tmp, "dead-worker", "halt")
        assert proc.returncode != 0, proc.stdout + proc.stderr
        assert proc.stderr.startswith("RECORDED, NOT DELIVERED:"), proc.stderr
        assert "no worker pid" in proc.stderr, proc.stderr
        assert len(proc.stderr.splitlines()) == 1, proc.stderr
        assert "message appended" not in proc.stderr, proc.stderr
        entries = _read_mailbox(tmp, "dead-worker")
        assert entries == [], entries
        envelopes = [
            json.loads(line)
            for line in (tmp / "messages" / "dead-worker.jsonl").read_text(encoding="utf-8").splitlines()
        ]
        assert envelopes[0]["payload"]["text"] == "halt", envelopes


def case_worker_view_failure_reports_replay_command() -> None:
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        dispatch_id = "worker-view-failure"
        _record(tmp, dispatch_id, worker_pid=os.getpid())
        original_delivery = goalflight_messages._deliver_message_to_worker

        def fail_delivery(*_args, **_kwargs):
            return {
                "requested": True,
                "delivered": False,
                "worker_view_written": False,
                "status": "worker_delivery_failed",
                "detail": "message recorded but worker delivery failed: OSError: disk unavailable",
            }

        stderr = io.StringIO()
        try:
            goalflight_messages._deliver_message_to_worker = fail_delivery
            with _state_dir(tmp), contextlib.redirect_stderr(stderr), contextlib.redirect_stdout(io.StringIO()):
                rc = goalflight_dispatch._cmd_steer([dispatch_id, "retry through read"])
        finally:
            goalflight_messages._deliver_message_to_worker = original_delivery

        lines = stderr.getvalue().splitlines()
        expected_next = (
            f"python3 scripts/goalflight_messages.py read --dispatch-id {dispatch_id} --json"
        )
        assert rc == 1, (rc, lines)
        assert lines == [
            "RECORDED, NOT DELIVERED: message recorded but worker delivery failed: "
            f"OSError: disk unavailable; next: {expected_next}"
        ], lines
        assert len(_read_messages(tmp, dispatch_id)) == 1
        assert _read_mailbox(tmp, dispatch_id) == []

        with _state_dir(tmp), contextlib.redirect_stdout(io.StringIO()):
            goalflight_messages.main(
                [
                    "--messages-dir",
                    str(tmp / "messages"),
                    "--fleet-dir",
                    str(tmp / "fleet"),
                    "read",
                    "--dispatch-id",
                    dispatch_id,
                    "--json",
                ]
            )
            goalflight_messages.main(
                [
                    "--messages-dir",
                    str(tmp / "messages"),
                    "--fleet-dir",
                    str(tmp / "fleet"),
                    "read",
                    "--dispatch-id",
                    dispatch_id,
                    "--json",
                ]
            )
        assert len(_read_messages(tmp, dispatch_id)) == 1
        entries = _read_mailbox(tmp, dispatch_id)
        assert len(entries) == 1 and entries[0]["text"].startswith("retry through read"), entries


def case_steer_is_no_worker_early_exit() -> None:
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        dispatch_id = "no-worker"
        _record(
            tmp,
            dispatch_id,
            controller_label="controller-test",
            controller_session_id="controller-session",
        )

        def boom(*_args, **_kwargs):
            raise AssertionError("steer path must not acquire leases, materialize prompts, or spawn workers")

        old_acquire = goalflight_dispatch._acquire_capacity
        old_materialize = goalflight_dispatch._materialize_steer_prompt
        old_popen = goalflight_dispatch.subprocess.Popen
        old_git_canonical_root = goalflight_task._git_canonical_root
        old_controller_env = {
            key: os.environ.get(key)
            for key in (
                "GOALFLIGHT_CONTROLLER_LABEL",
                "GOALFLIGHT_CONTROLLER_PID",
                "GOALFLIGHT_CONTROLLER_SESSION_ID",
            )
        }
        try:
            goalflight_dispatch._acquire_capacity = boom
            goalflight_dispatch._materialize_steer_prompt = boom
            goalflight_dispatch.subprocess.Popen = boom
            # Exercise the controller-attributed steer path. A no-worker
            # steer must record before any optional identity probe, and that
            # path must not invoke git.
            os.environ["GOALFLIGHT_CONTROLLER_LABEL"] = "controller-test"
            os.environ["GOALFLIGHT_CONTROLLER_PID"] = str(os.getpid())
            os.environ["GOALFLIGHT_CONTROLLER_SESSION_ID"] = "controller-session"

            def no_git(*_args, **_kwargs):
                raise AssertionError("steer path must not invoke git")

            goalflight_task._git_canonical_root = no_git
            with _state_dir(tmp):
                proc_out = io.StringIO()
                proc_err = io.StringIO()
                with contextlib.redirect_stdout(proc_out), contextlib.redirect_stderr(proc_err):
                    rc = goalflight_dispatch.main(["steer", dispatch_id, "redirect"])
        finally:
            goalflight_dispatch._acquire_capacity = old_acquire
            goalflight_dispatch._materialize_steer_prompt = old_materialize
            goalflight_dispatch.subprocess.Popen = old_popen
            goalflight_task._git_canonical_root = old_git_canonical_root
            for key, value in old_controller_env.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

        assert rc != 0, proc_err.getvalue()
        assert "no worker pid" in proc_err.getvalue(), proc_err.getvalue()
        assert "message appended" not in proc_err.getvalue(), proc_err.getvalue()
        assert "steer appended:" not in proc_out.getvalue(), proc_out.getvalue()
        entries = _read_mailbox(tmp, dispatch_id)
        assert entries == [], entries
        envelopes = [
            json.loads(line)
            for line in (tmp / "messages" / f"{dispatch_id}.jsonl").read_text(encoding="utf-8").splitlines()
        ]
        assert envelopes[0]["payload"]["text"] == "redirect", envelopes


def case_worker_wait_accepts_generic_backlog() -> None:
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        dispatch_id = "wait-backlog"
        _record(tmp, dispatch_id, worker_pid=os.getpid())
        with _state_dir(tmp):
            goalflight_steer_mailbox.append_steer_entry(
                _mailbox(tmp, dispatch_id),
                "answer arrived before arm",
                dispatch_id=dispatch_id,
            )

        started = time.monotonic()
        proc = _run_worker_wait(
            tmp,
            dispatch_id,
            "--question-kind",
            "USER-NEED",
            "need a decision",
            "--timeout-secs",
            "1",
        )
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert time.monotonic() - started < 10.0
        assert 'STEER-BACKLOG: {"kind": "steering", "seq": 1}' in proc.stdout, proc.stdout
        assert (
            'STEER-MESSAGE: {"decision": null, "seq": 1, '
            '"text": "answer arrived before arm"}'
        ) in proc.stdout, proc.stdout
        assert "STEER-REPLY:" not in proc.stdout, proc.stdout
        assert f"!BLOCKED: {dispatch_id} - wait timed out;" not in proc.stdout, proc.stdout
        entries = _read_mailbox(tmp, dispatch_id)
        assert [entry.get("kind") for entry in entries] == [
            goalflight_steer_mailbox.STEERING_KIND,
        ], entries
        questions = sorted(
            path
            for path in (tmp / "dispatch" / f"{dispatch_id}.questions").glob("*.json")
            if not path.name.endswith(".settled.json")
        )
        assert not questions, questions


def case_acked_backlog_does_not_suppress_a_new_question() -> None:
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        dispatch_id = "acked-backlog"
        _record(tmp, dispatch_id, worker_pid=os.getpid())
        with _state_dir(tmp):
            goalflight_steer_mailbox.append_steer_entry(
                _mailbox(tmp, dispatch_id),
                "already handled",
                dispatch_id=dispatch_id,
            )
        tail = tmp / f"{dispatch_id}.tail"
        tail.write_text("!STEER-ACK: 1\n", encoding="utf-8")

        proc = _run_worker_wait(
            tmp,
            dispatch_id,
            "--question-kind",
            "USER-NEED",
            "need a new decision",
            "--timeout-secs",
            "0.1",
            "--poll-secs",
            "0.05",
        )

        assert proc.returncode == 1, proc.stdout + proc.stderr
        assert "already handled" not in proc.stdout, proc.stdout
        assert "STEER-BACKLOG:" not in proc.stdout, proc.stdout
        assert f"!USER-NEED: {dispatch_id} — need a new decision" in proc.stdout
        questions = list((tmp / "dispatch" / f"{dispatch_id}.questions").glob("*.json"))
        assert len([path for path in questions if not path.name.endswith(".settled.json")]) == 1


def case_identified_late_reply_resumes_across_attempts() -> None:
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        dispatch_id = "resume-wait"
        wait_id = "identified-old-wait"
        _record(tmp, dispatch_id, worker_pid=os.getpid())
        mailbox = _mailbox(tmp, dispatch_id)
        outbox = tmp / "dispatch" / f"{dispatch_id}.questions"
        outbox.mkdir(parents=True, exist_ok=True)
        question = "authorize the guarded action?"
        with _state_dir(tmp):
            goalflight_steer_mailbox.write_worker_question_request(
                outbox,
                dispatch_id=dispatch_id,
                attempt_id="original-attempt",
                wait_id=wait_id,
                question_kind="USER-CONFIRM",
                question=question,
                timeout_s=30,
            )
            arm = goalflight_steer_mailbox.append_worker_wait_started(
                mailbox,
                dispatch_id=dispatch_id,
                timeout_secs=30,
                question_kind="USER-CONFIRM",
                question_text=question,
                wait_id=wait_id,
                attempt_id="original-attempt",
                waiter_pid=os.getpid(),
            )
            goalflight_steer_mailbox.append_worker_wait_ended(
                mailbox,
                arm,
                decision="timeout",
                context={"reason": "original worker timed out"},
            )
            late = goalflight_steer_mailbox.append_worker_wait_reply(
                mailbox,
                dispatch_id=dispatch_id,
                wait_id=wait_id,
                text="approved late",
                decision="yes",
            )

        env = _worker_wait_env(tmp, dispatch_id)
        env["GOALFLIGHT_ATTEMPT_ID"] = "resumed-attempt"
        proc = subprocess.run(
            [
                sys.executable,
                str(DISPATCH),
                "steer",
                dispatch_id,
                "--wait",
                "--question-kind",
                "USER-CONFIRM",
                "--resume-wait-id",
                wait_id,
                question,
            ],
            env=env,
            capture_output=True,
            text=True,
            timeout=20,
        )

        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert '"text": "approved late"' in proc.stdout, proc.stdout
        assert f'"reply_to": "{wait_id}"' in proc.stdout, proc.stdout
        assert late["context"]["attempt_id"] == "original-attempt"


def case_worker_confirm_does_not_accept_decision_free_backlog() -> None:
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        dispatch_id = "confirm-backlog"
        _record(tmp, dispatch_id, worker_pid=os.getpid())
        with _state_dir(tmp):
            goalflight_steer_mailbox.append_steer_entry(
                _mailbox(tmp, dispatch_id),
                "unrelated controller note",
                dispatch_id=dispatch_id,
            )

        proc = _run_worker_wait(
            tmp,
            dispatch_id,
            "--question-kind",
            "USER-CONFIRM",
            "authorize the guarded action?",
            "--timeout-secs",
            "0.2",
            "--poll-secs",
            "1",
        )

        assert proc.returncode == 1, proc.stdout + proc.stderr
        assert "unrelated controller note" in proc.stdout, proc.stdout
        assert 'STEER-BACKLOG: {"kind": "steering", "seq": 1}' in proc.stdout, proc.stdout
        assert (
            'STEER-MESSAGE: {"decision": null, "seq": 1, '
            '"text": "unrelated controller note"}'
        ) in proc.stdout, proc.stdout
        assert "STEER-REPLY:" not in proc.stdout, proc.stdout
        assert f"!USER-CONFIRM: {dispatch_id} — authorize the guarded action?" in proc.stdout
        assert f"!BLOCKED: {dispatch_id} - wait timed out;" in proc.stdout
        entries = _read_mailbox(tmp, dispatch_id)
        assert [entry.get("kind") for entry in entries] == [
            goalflight_steer_mailbox.STEERING_KIND,
        ], entries
        questions = sorted(
            path
            for path in (tmp / "dispatch" / f"{dispatch_id}.questions").glob("*.json")
            if not path.name.endswith(".settled.json")
        )
        assert len(questions) == 1, questions
        assert json.loads(questions[0].read_text(encoding="utf-8"))["question_kind"] == "USER-CONFIRM"


def case_worker_wait_atomic_question_has_own_deadline() -> None:
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        dispatch_id = "wait-deadline"
        _record(tmp, dispatch_id, worker_pid=os.getpid())

        started = time.monotonic()
        proc = _run_worker_wait(
            tmp,
            dispatch_id,
            "--question-kind",
            "USER-CONFIRM",
            "authorize the guarded action?",
            "--timeout-secs",
            "0.2",
            "--poll-secs",
            "1",
        )
        elapsed = time.monotonic() - started
        assert proc.returncode == 1, proc.stdout + proc.stderr
        assert elapsed < 10.0, elapsed
        lines = proc.stdout.splitlines()
        assert lines[0].startswith(
            f"!USER-CONFIRM: {dispatch_id} — authorize the guarded action? "
            "[wait-id:"
        ) and lines[0].endswith("]"), lines
        assert lines[1].startswith(f"STEER-WAIT: dispatch_id={dispatch_id} armed"), lines
        assert any(
            line.startswith(f"!BLOCKED: {dispatch_id} - wait timed out; wait-id=")
            for line in lines
        ), lines
        assert lines[-2] == f"STEER-WAIT: dispatch_id={dispatch_id} deadline reached", lines
        assert lines[-1].startswith(
            f"!BLOCKED: {dispatch_id} - wait timed out; wait-id="
        ), lines
        entries = _read_mailbox(tmp, dispatch_id)
        assert entries == [], entries
        questions = sorted(
            path
            for path in (tmp / "dispatch" / f"{dispatch_id}.questions").glob("*.json")
            if not path.name.endswith(".settled.json")
        )
        assert len(questions) == 1, questions
        question = json.loads(questions[0].read_text(encoding="utf-8"))
        assert question["question"] == "authorize the guarded action?", question
        assert question["timeout_s"] == 0.2, question


def case_worker_wait_question_is_written_before_waiting() -> None:
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        dispatch_id = "wait-question-published"
        _record(tmp, dispatch_id, worker_pid=os.getpid())

        proc = _run_worker_wait(
            tmp,
            dispatch_id,
            "--question-kind",
            "USER-NEED",
            "need a controller decision",
            "--timeout-secs",
            "0.2",
            "--poll-secs",
            "1",
        )
        assert proc.returncode == 1, proc.stdout + proc.stderr
        questions = sorted(
            path
            for path in (tmp / "dispatch" / f"{dispatch_id}.questions").glob("*.json")
            if not path.name.endswith(".settled.json")
        )
        assert len(questions) == 1, questions
        question = json.loads(questions[0].read_text(encoding="utf-8"))
        assert question["schema"] == goalflight_steer_mailbox.WORKER_QUESTION_SCHEMA, question
        assert question["question_kind"] == "USER-NEED", question
        assert question["question"] == "need a controller decision", question
        assert question["dispatch_id"] == dispatch_id, question
        assert _read_messages(tmp, dispatch_id) == []


def case_controller_reply_reaches_waiter() -> None:
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        dispatch_id = "wait-reply-roundtrip"
        _record(tmp, dispatch_id, worker_pid=os.getpid())
        env = _worker_wait_env(tmp, dispatch_id)
        waiter = subprocess.Popen(
            [
                sys.executable,
                str(DISPATCH),
                "steer",
                dispatch_id,
                "--wait",
                "--question-kind",
                "USER-NEED",
                "approve the next step",
                "--timeout-secs",
                "20",
                "--poll-secs",
                "0.02",
            ],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            deadline = time.monotonic() + 10
            question_files: list[Path] = []
            outbox = tmp / "dispatch" / f"{dispatch_id}.questions"
            while time.monotonic() < deadline:
                question_files = sorted(
                    path
                    for path in outbox.glob("*.json")
                    if not path.name.endswith(".settled.json")
                )
                if question_files:
                    break
                time.sleep(0.02)
            assert question_files, list(outbox.iterdir())
            request = json.loads(question_files[0].read_text(encoding="utf-8"))
            wait_id = str(request["wait_id"])
            identity = goalflight_compat.process_start_identity(waiter.pid) or {}
            assert identity.get("start_token"), identity
            with _state_dir(tmp):
                consumed_mtime = goalflight_watch.consume_worker_question_outbox(
                    outbox,
                    _mailbox(tmp, dispatch_id),
                    dispatch_id,
                    worker_pid=waiter.pid,
                    worker_start_token=str(identity["start_token"]),
                    worker_pgid=os.getpgid(waiter.pid),
                )
            assert consumed_mtime is not None
            assert (outbox / f"{wait_id}.published").is_file()
            envelope = _read_messages(tmp, dispatch_id)[0]
            assert envelope["type"] == "user_need", envelope
            assert envelope["payload"]["wait_id"] == wait_id, envelope
            reply = _run_steer(tmp, dispatch_id, "--reply-to", wait_id, "approved")
            assert reply.returncode == 0, reply.stdout + reply.stderr
            stdout, stderr = waiter.communicate(timeout=5)
            assert waiter.returncode == 0, stdout + stderr
            assert "approved" in stdout, stdout
            entries = _read_mailbox(tmp, dispatch_id)
            typed = next(
                entry
                for entry in entries
                if entry.get("kind") == goalflight_steer_mailbox.WORKER_WAIT_REPLY_KIND
            )
            assert typed["reply_to"] == wait_id, entries
        finally:
            if waiter.poll() is None:
                waiter.kill()
                waiter.wait(timeout=5)


def case_worker_wait_requires_an_atomic_question() -> None:
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        dispatch_id = "wait-requires-question"
        _record(tmp, dispatch_id, worker_pid=os.getpid())

        proc = _run_worker_wait(tmp, dispatch_id, "--timeout-secs", "0.1")
        assert proc.returncode == 64, proc.stdout + proc.stderr
        assert "requires question text and --question-kind" in proc.stderr, proc.stderr
        assert not _mailbox(tmp, dispatch_id).exists()

        proc = _run_worker_wait(
            tmp,
            dispatch_id,
            "--question-kind",
            "CUSTOM-QUESTION",
            "supply the missing value",
            "--timeout-secs",
            "0.1",
        )
        assert proc.returncode == 64, proc.stdout + proc.stderr
        assert "question-kind must be one of:" in proc.stderr, proc.stderr
        assert "USER-NEED" in proc.stderr, proc.stderr
        assert "USER-CONFIRM" in proc.stderr, proc.stderr
        assert not _mailbox(tmp, dispatch_id).exists()


def case_worker_wait_carrier_error_is_reported() -> None:
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        dispatch_id = "wait-carrier-error"
        _record(tmp, dispatch_id, worker_pid=os.getpid())
        worker_env = _worker_wait_env(tmp, dispatch_id)
        old_wait = goalflight_steer_mailbox.wait_for_worker_entries
        worker_env_keys = (
            "GOALFLIGHT_DISPATCH_ID",
            "GOALFLIGHT_STEER_FILE",
            "GOALFLIGHT_ATTEMPT_ID",
            "GOALFLIGHT_QUESTION_OUTBOX_DIR",
        )
        old_worker_env = {key: os.environ.get(key) for key in worker_env_keys}

        def broken_wait(*_args, **_kwargs):
            raise goalflight_steer_mailbox._carrier_module().MessageError(
                "carrier identity changed"
            )

        try:
            goalflight_steer_mailbox.wait_for_worker_entries = broken_wait
            for key in worker_env_keys:
                os.environ[key] = worker_env[key]
            stdout = io.StringIO()
            stderr = io.StringIO()
            with _state_dir(tmp), contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                rc = goalflight_dispatch.main(
                    [
                        "steer",
                        dispatch_id,
                        "--wait",
                        "--question-kind",
                        "USER-NEED",
                        "need a decision",
                    ]
                )
        finally:
            goalflight_steer_mailbox.wait_for_worker_entries = old_wait
            for key, value in old_worker_env.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

        assert rc == 1, stdout.getvalue() + stderr.getvalue()
        assert "steer --wait failed: carrier identity changed" in stderr.getvalue()


def case_controller_reply_is_typed_and_wait_id_correlated() -> None:
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        dispatch_id = "typed-wait-reply"
        _record(tmp, dispatch_id, worker_pid=os.getpid())
        with _state_dir(tmp):
            arm = goalflight_steer_mailbox.append_worker_wait_started(
                _mailbox(tmp, dispatch_id),
                dispatch_id=dispatch_id,
                timeout_secs=2,
                question_kind="USER-CONFIRM",
                question_text="approve?",
            )

        proc = _run_steer(
            tmp,
            dispatch_id,
            "approved",
            "--reply-to",
            str(arm["question_id"]),
            "--decision",
            "yes",
        )
        assert proc.returncode == 0, proc.stdout + proc.stderr
        reply = _read_mailbox(tmp, dispatch_id)[-1]
        assert reply["kind"] == goalflight_steer_mailbox.WORKER_WAIT_REPLY_KIND, reply
        assert reply["reply_to"] == arm["question_id"], reply
        assert reply["decision"] == "yes", reply

        duplicate = _run_steer(
            tmp,
            dispatch_id,
            "second answer",
            "--reply-to",
            str(arm["question_id"]),
            "--decision",
            "no",
        )
        assert duplicate.returncode == 1, duplicate.stdout + duplicate.stderr
        assert "already has a reply" in duplicate.stderr, duplicate.stderr

        missing_correlation = _run_steer(
            tmp,
            dispatch_id,
            "ambiguous answer",
            "--decision",
            "yes",
        )
        assert missing_correlation.returncode == 64
        assert "--decision requires --reply-to" in missing_correlation.stderr


def test_refused_wait_renewal_notifies_and_delivers_prior_reply() -> None:
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        dispatch_id = "refused-wait-renewal"
        _record(tmp, dispatch_id, worker_pid=os.getpid())
        mailbox = _mailbox(tmp, dispatch_id)
        outbox = tmp / "dispatch" / f"{dispatch_id}.questions"
        outbox.mkdir(parents=True)
        with _state_dir(tmp):
            attempt = goalflight_journal.Journal(ROOT).attempt_for_dispatch(dispatch_id)
            assert attempt is not None
            identity = goalflight_compat.process_start_identity(os.getpid()) or {}
            assert identity.get("start_token"), identity

            class WaitArmed(Exception):
                pass

            def arm_first_wait(event: dict) -> None:
                if event.get("state") != "armed":
                    return
                goalflight_watch.consume_worker_question_outbox(
                    outbox,
                    mailbox,
                    dispatch_id,
                    worker_pid=os.getpid(),
                    worker_start_token=identity.get("start_token"),
                    worker_pgid=os.getpgrp(),
                    active_attempt_id=attempt.attempt_id,
                )
                raise WaitArmed

            try:
                goalflight_steer_mailbox.wait_for_worker_entries(
                    mailbox,
                    dispatch_id=dispatch_id,
                    acked_seqs=set(),
                    question_kind="USER-CONFIRM",
                    question_text="authorize the guarded action?",
                    attempt_id=attempt.attempt_id,
                    outbox_dir=outbox,
                    timeout_secs=2,
                    poll_secs=0.02,
                    notify=arm_first_wait,
                )
            except WaitArmed:
                pass
            else:
                raise AssertionError("first wait did not arm")

            first_requests = [
                json.loads(path.read_text(encoding="utf-8"))
                for path in outbox.glob("*.json")
                if not path.name.endswith(".settled.json")
            ]
            assert len(first_requests) == 1, first_requests
            prior_wait_id = first_requests[0]["wait_id"]

            record, record_error = goalflight_messages._dispatch_record(dispatch_id)
            assert record_error is None, record_error
            assert record is not None, dispatch_id
            worker_pid = record.get("worker_pid")
            worker_pgid = record.get("worker_pgid")
            if isinstance(worker_pid, bool) or not isinstance(worker_pid, int) or worker_pid <= 0:
                worker_pid = None
            if isinstance(worker_pgid, bool) or not isinstance(worker_pgid, int) or worker_pgid <= 0:
                worker_pgid = None
            live_wait = goalflight_steer_mailbox.active_worker_wait(
                goalflight_steer_mailbox.read_steer_entries(mailbox),
                dispatch_id=dispatch_id,
                worker_pid=worker_pid,
                worker_pgid=worker_pgid,
            )
            assert live_wait is not None, goalflight_steer_mailbox.read_steer_entries(
                mailbox
            )
            assert live_wait.get("attempt_id") == record.get("attempt_id"), live_wait

            plain = _run_steer(tmp, dispatch_id, "approved")

            blocked_reply_commands: list[str] = []

            def refuse_renewal_then_reply(event: dict) -> None:
                if event.get("state") == "armed":
                    goalflight_watch.consume_worker_question_outbox(
                        outbox,
                        mailbox,
                        dispatch_id,
                        worker_pid=os.getpid(),
                        worker_start_token=identity.get("start_token"),
                        worker_pgid=os.getpgrp(),
                        active_attempt_id=attempt.attempt_id,
                    )
                    envelopes = goalflight_messages.read_envelopes(
                        goalflight_messages.inbox_path(
                            goalflight_messages.default_messages_dir(),
                            dispatch_id,
                        )
                    )
                    blocked = [envelope for envelope in envelopes if envelope["type"] == "blocked"]
                    assert len(blocked) == 1, blocked
                    reply_command = blocked[0]["payload"]["reply_command"]
                    blocked_reply_commands.append(reply_command)
                    reply = _run_wait_reply_command(
                        tmp,
                        reply_command,
                        answer="approved once",
                        decision="yes",
                    )
                    assert reply.returncode == 0, reply.stdout + reply.stderr

            result = goalflight_steer_mailbox.wait_for_worker_entries(
                mailbox,
                dispatch_id=dispatch_id,
                acked_seqs=set(),
                question_kind="USER-CONFIRM",
                question_text="continue with the next step?",
                attempt_id=attempt.attempt_id,
                outbox_dir=outbox,
                timeout_secs=0.25,
                poll_secs=0.02,
                notify=refuse_renewal_then_reply,
            )

            assert result["state"] == "messages", result
            assert result["wait_id"] == prior_wait_id, result
            assert result["entries"][0]["text"].startswith("approved once"), result
            assert plain.returncode == 64, plain.stdout + plain.stderr
            refusal_reply_command = _reply_command_from_refusal(plain.stderr)
            reply_command = goalflight_steer_mailbox.worker_wait_reply_command(
                dispatch_id, prior_wait_id, "USER-CONFIRM"
            )
            assert refusal_reply_command == reply_command, plain.stderr
            assert blocked_reply_commands == [reply_command], blocked_reply_commands
            assert reply_command in plain.stderr, plain.stderr
            for printed_command in (refusal_reply_command, *blocked_reply_commands):
                tokens = shlex.split(printed_command)
                assert tokens.index("<answer>") < tokens.index("--reply-to"), tokens

            goalflight_watch.consume_worker_question_outbox(
                outbox,
                mailbox,
                dispatch_id,
                worker_pid=os.getpid(),
                worker_start_token=identity.get("start_token"),
                worker_pgid=os.getpgrp(),
                active_attempt_id=attempt.attempt_id,
            )
            entries = goalflight_steer_mailbox.read_steer_entries(mailbox)
            assert any(
                entry.get("kind") == goalflight_steer_mailbox.WORKER_WAIT_ENDED_KIND
                and entry.get("reply_to") == prior_wait_id
                and entry.get("decision") == "reply"
                for entry in entries
            ), entries

            envelopes = goalflight_messages.read_envelopes(
                goalflight_messages.inbox_path(
                    goalflight_messages.default_messages_dir(),
                    dispatch_id,
                )
            )
            blocked = [envelope for envelope in envelopes if envelope["type"] == "blocked"]
            assert len(blocked) == 1, blocked
            payload = blocked[0]["payload"]
            assert payload["wait_id"] == prior_wait_id, payload
            assert payload["reply_command"] == reply_command, payload
            assert prior_wait_id in payload["text"], payload
            assert reply_command in payload["text"], payload


def test_plain_steer_refusal_command_delivers_typed_reply() -> None:
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        dispatch_id = "plain-steer-reply-command"
        _record(tmp, dispatch_id, worker_pid=os.getpid())
        mailbox = _mailbox(tmp, dispatch_id)
        with _state_dir(tmp):
            attempt = goalflight_journal.Journal(ROOT).attempt_for_dispatch(dispatch_id)
            assert attempt is not None
            identity = goalflight_compat.process_start_identity(os.getpid()) or {}
            wait_id = "confirm-command-wait"
            goalflight_steer_mailbox.append_worker_wait_started(
                mailbox,
                dispatch_id=dispatch_id,
                timeout_secs=30,
                question_kind="USER-CONFIRM",
                question_text="authorize the guarded action?",
                wait_id=wait_id,
                attempt_id=attempt.attempt_id,
                waiter_pid=os.getpid(),
                waiter_start_token=identity.get("start_token"),
                waiter_pgid=os.getpgrp(),
            )

        refused = _run_steer(tmp, dispatch_id, "plain update")
        assert refused.returncode == 64, refused.stdout + refused.stderr
        command = _reply_command_from_refusal(refused.stderr)
        reply = _run_wait_reply_command(
            tmp,
            command,
            answer="approved from refusal",
            decision="yes",
        )
        assert reply.returncode == 0, reply.stdout + reply.stderr
        entries = _read_mailbox(tmp, dispatch_id)
        typed_replies = [
            entry for entry in entries
            if entry.get("kind") == goalflight_steer_mailbox.WORKER_WAIT_REPLY_KIND
        ]
        assert len(typed_replies) == 1, entries
        assert typed_replies[0]["reply_to"] == wait_id, typed_replies
        assert typed_replies[0]["decision"] == "yes", typed_replies
        assert typed_replies[0]["text"].startswith("approved from refusal"), typed_replies
        tokens = shlex.split(command)
        assert tokens.index("<answer>") < tokens.index("--reply-to"), tokens


def test_plain_steer_rechecks_confirm_wait_under_append_lock() -> None:
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        dispatch_id = "plain-steer-append-lock-race"
        _record(tmp, dispatch_id, worker_pid=os.getpid())
        mailbox = _mailbox(tmp, dispatch_id)
        with _state_dir(tmp):
            attempt = goalflight_journal.Journal(ROOT).attempt_for_dispatch(dispatch_id)
            assert attempt is not None
            identity = goalflight_compat.process_start_identity(os.getpid()) or {}
            goalflight_steer_mailbox.append_worker_wait_started(
                mailbox,
                dispatch_id=dispatch_id,
                timeout_secs=30,
                question_kind="USER-CONFIRM",
                question_text="authorize the guarded action?",
                wait_id="confirm-append-lock-race",
                attempt_id=attempt.attempt_id,
                waiter_pid=os.getpid(),
                waiter_start_token=identity.get("start_token"),
                waiter_pgid=os.getpgrp(),
            )
            try:
                goalflight_steer_mailbox.append_message_view(
                    dispatch_id,
                    {
                        "id": "plain-steer-append-lock-message",
                        "type": "controller-notice",
                        "payload": {"text": "plain update during the race"},
                        "source": {
                            "transport": "steer",
                            "sender": {
                                "controller_label": "test",
                                "controller_pid": os.getpid(),
                                "project_root": str(ROOT),
                            },
                        },
                    },
                )
            except goalflight_messages.MessageError as exc:
                assert "requires a correlated answer" in str(exc), exc
            else:
                raise AssertionError("append-lock wait recheck did not refuse the steer")

        entries = _read_mailbox(tmp, dispatch_id)
        assert len(entries) == 1, entries
        assert entries[0].get("question_id") == "confirm-append-lock-race", entries


def test_watcher_rss_steer_is_delivered_during_confirm_wait() -> None:
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        dispatch_id = "watcher-rss-during-confirm"
        _record(tmp, dispatch_id, worker_pid=os.getpid())
        mailbox = _mailbox(tmp, dispatch_id)
        with _state_dir(tmp):
            attempt = goalflight_journal.Journal(ROOT).attempt_for_dispatch(dispatch_id)
            assert attempt is not None
            identity = goalflight_compat.process_start_identity(os.getpid()) or {}
            goalflight_steer_mailbox.append_worker_wait_started(
                mailbox,
                dispatch_id=dispatch_id,
                timeout_secs=30,
                question_kind="USER-CONFIRM",
                question_text="authorize the guarded action?",
                wait_id="confirm-rss-wait",
                attempt_id=attempt.attempt_id,
                waiter_pid=os.getpid(),
                waiter_start_token=identity.get("start_token"),
                waiter_pgid=os.getpgrp(),
            )

            goalflight_watch.post_worker_memory_event(
                dispatch_id,
                {
                    "command": "memory-child --alloc",
                    "peak_rss_mb": 64.0,
                    "ceiling_mb": 16,
                    "verdict": "descendant_terminated",
                },
            )

        entries = _read_mailbox(tmp, dispatch_id)
        assert entries[-1]["kind"] == "message", entries
        assert entries[-1]["text"].startswith("worker RSS ceiling exceeded:"), entries
        assert not any(
            entry.get("kind") == goalflight_steer_mailbox.WORKER_WAIT_REPLY_KIND
            for entry in entries
        ), entries


def test_plain_steer_refusal_requires_current_live_confirm_wait() -> None:
    cases = (
        ("stale-attempt", "continue", "older-attempt", os.getpid()),
        ("dead-waiter", "continue", None, 2**30),
        ("halt", "HALT", None, os.getpid()),
        ("stop", "please stop this worker", None, os.getpid()),
        ("withdraw", "withdraw this dispatch", None, os.getpid()),
    )
    for suffix, text, arm_attempt_override, waiter_pid in cases:
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            dispatch_id = f"plain-steer-{suffix}"
            _record(tmp, dispatch_id, worker_pid=os.getpid())
            with _state_dir(tmp):
                attempt = goalflight_journal.Journal(ROOT).attempt_for_dispatch(dispatch_id)
                assert attempt is not None
                identity = goalflight_compat.process_start_identity(waiter_pid) or {}
                goalflight_steer_mailbox.append_worker_wait_started(
                    _mailbox(tmp, dispatch_id),
                    dispatch_id=dispatch_id,
                    timeout_secs=30,
                    question_kind="USER-CONFIRM",
                    question_text="authorize the guarded action?",
                    wait_id=f"wait-{suffix}",
                    attempt_id=arm_attempt_override or attempt.attempt_id,
                    waiter_pid=waiter_pid,
                    waiter_start_token=identity.get("start_token") or "dead-waiter-token",
                    waiter_pgid=(os.getpgrp() if waiter_pid == os.getpid() else None),
                )

            result = _run_steer(tmp, dispatch_id, text)
            assert result.returncode == 0, result.stdout + result.stderr
            entries = _read_mailbox(tmp, dispatch_id)
            assert entries[-1]["kind"] == "message", entries
            assert entries[-1]["text"].startswith(text), entries
            assert not any(
                entry.get("kind") == goalflight_steer_mailbox.WORKER_WAIT_REPLY_KIND
                for entry in entries
            ), entries


def test_distinct_refused_wait_renewals_notify_once_each_without_retry() -> None:
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        dispatch_id = "distinct-refused-renewals"
        _record(tmp, dispatch_id, worker_pid=os.getpid())
        outbox = tmp / "dispatch" / f"{dispatch_id}.questions"
        outbox.mkdir(parents=True)
        mailbox = _mailbox(tmp, dispatch_id)
        with _state_dir(tmp):
            attempt = goalflight_journal.Journal(ROOT).attempt_for_dispatch(dispatch_id)
            assert attempt is not None
            identity = goalflight_compat.process_start_identity(os.getpid()) or {}
            prior_wait_id = "live-confirm"
            prior_request = goalflight_steer_mailbox.write_worker_question_request(
                outbox,
                dispatch_id=dispatch_id,
                attempt_id=attempt.attempt_id,
                wait_id=prior_wait_id,
                question_kind="USER-CONFIRM",
                question="authorize the first action?",
                timeout_s=30,
            )
            goalflight_steer_mailbox.append_worker_wait_started(
                mailbox,
                dispatch_id=dispatch_id,
                timeout_secs=30,
                question_kind="USER-CONFIRM",
                question_text=prior_request["question"],
                wait_id=prior_wait_id,
                attempt_id=attempt.attempt_id,
                waiter_pid=os.getpid(),
                waiter_start_token=identity.get("start_token"),
                waiter_pgid=os.getpgrp(),
            )
            goalflight_steer_mailbox.write_worker_question_published_marker(
                outbox / f"{prior_wait_id}.published"
            )
            rejected_wait_ids = ("renewal-a", "renewal-b")
            for wait_id in rejected_wait_ids:
                goalflight_steer_mailbox.write_worker_question_request(
                    outbox,
                    dispatch_id=dispatch_id,
                    attempt_id=attempt.attempt_id,
                    wait_id=wait_id,
                    question_kind="USER-CONFIRM",
                    question=f"authorize {wait_id}?",
                    timeout_s=30,
                )

            first_mtime = goalflight_watch.consume_worker_question_outbox(
                outbox,
                mailbox,
                dispatch_id,
                worker_pid=os.getpid(),
                worker_start_token=identity.get("start_token"),
                worker_pgid=os.getpgrp(),
                active_attempt_id=attempt.attempt_id,
                publish_func=lambda _event: None,
            )
            envelopes = goalflight_messages.read_envelopes(
                goalflight_messages.inbox_path(
                    goalflight_messages.default_messages_dir(),
                    dispatch_id,
                )
            )
            blocked = [envelope for envelope in envelopes if envelope["type"] == "blocked"]
            assert first_mtime is not None
            assert len(blocked) == 2, blocked
            assert len({envelope["id"] for envelope in blocked}) == 2, blocked
            assert {
                envelope["payload"]["rejected_wait_id"] for envelope in blocked
            } == set(rejected_wait_ids), blocked
            assert all(
                goalflight_steer_mailbox.worker_question_rejection_path(
                    outbox,
                    wait_id,
                ).is_file()
                for wait_id in rejected_wait_ids
            )

            second_mtime = goalflight_watch.consume_worker_question_outbox(
                outbox,
                mailbox,
                dispatch_id,
                worker_pid=os.getpid(),
                worker_start_token=identity.get("start_token"),
                worker_pgid=os.getpgrp(),
                active_attempt_id=attempt.attempt_id,
                last_mtime_ns=first_mtime,
                publish_func=lambda _event: None,
            )
            third_mtime = goalflight_watch.consume_worker_question_outbox(
                outbox,
                mailbox,
                dispatch_id,
                worker_pid=os.getpid(),
                worker_start_token=identity.get("start_token"),
                worker_pgid=os.getpgrp(),
                active_attempt_id=attempt.attempt_id,
                last_mtime_ns=second_mtime,
                publish_func=lambda _event: None,
            )
            assert second_mtime is not None
            assert third_mtime == second_mtime
            envelopes = goalflight_messages.read_envelopes(
                goalflight_messages.inbox_path(
                    goalflight_messages.default_messages_dir(),
                    dispatch_id,
                )
            )
            assert len([envelope for envelope in envelopes if envelope["type"] == "blocked"]) == 2


def case_concurrent_appends_have_monotonic_unique_seq() -> None:
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        dispatch_id = "steer-concurrent"
        _record(tmp, dispatch_id, worker_pid=os.getpid())
        procs = [
            subprocess.Popen(
                [sys.executable, str(DISPATCH), "steer", dispatch_id, f"msg-{idx}"],
                env=_env(tmp),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            for idx in range(12)
        ]
        outputs = [proc.communicate(timeout=20) + (proc.returncode,) for proc in procs]
        failures = [out for out in outputs if out[2] != 0]
        assert not failures, failures

        entries = _read_mailbox(tmp, dispatch_id)
        seqs = [entry["seq"] for entry in entries]
        assert sorted(seqs) == list(range(1, 13)), entries
        assert len(set(entry["text"] for entry in entries)) == 12, entries


def case_spawn_exports_steer_env() -> None:
    if skip_case_posix_on_native_windows(
        "case_spawn_exports_steer_env",
        "steer env export launches a POSIX/WSL bash-tail dispatch worker",
    ):
        return

    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        project_root, _worktree, _orientation = _repo_with_orientation(tmp)
        dispatch_id = "env-export"
        tail = tmp / "tail.log"
        status = tmp / "status.json"
        worker_code = (
            "import os; "
            "print(os.environ.get('GOALFLIGHT_STEER_FILE', '')); "
            "print(os.environ.get('GOALFLIGHT_DISPATCH_SCRIPT', '')); "
            f"print('COMPLETE: {dispatch_id} — env seen', flush=True)"
        )
        proc = subprocess.run(
            [
                sys.executable,
                str(DISPATCH),
                "--unregistered-forced",
                "--agent",
                "env-check",
                "--dispatch-id",
                dispatch_id,
                "--tail",
                str(tail),
                "--status-json",
                str(status),
                "--poll-secs",
                "0.1",
                "--max-idle-secs",
                "5",
                "--foreground",
                "--",
                sys.executable,
                "-c",
                worker_code,
            ],
            cwd=project_root,
            env=_env(tmp, project_root=project_root),
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert proc.returncode == 0, proc.stdout + proc.stderr
        tail_text = tail.read_text(encoding="utf-8")
        assert str(_mailbox(tmp, dispatch_id)) in tail_text, tail_text
        assert str(DISPATCH.resolve()) in tail_text, tail_text


def _run_prompt_env_case(
    tmp: Path,
    project_root: Path,
    dispatch_id: str,
    prompt_args: list[str],
    seen_path: Path,
    *,
    launch_cwd: Path | None = None,
) -> str:
    worker_code = (
        "import os; "
        "from pathlib import Path; "
        f"Path({str(seen_path)!r}).write_text("
        "os.environ.get('GOALFLIGHT_PROMPT_FILE', '') + '\\n' + "
        "os.environ.get('GOALFLIGHT_STEER_FILE', ''), encoding='utf-8'); "
        f"print('COMPLETE: {dispatch_id} — prompt env seen', flush=True)"
    )

    old_build_worker = goalflight_dispatch.build_worker

    def fake_build_worker(_args, _prompt_path, _raw_argv):
        return [sys.executable, "-c", worker_code], None

    try:
        goalflight_dispatch.build_worker = fake_build_worker
        with _state_dir(tmp, project_root=project_root):
            proc_out = io.StringIO()
            proc_err = io.StringIO()
            previous_cwd = Path.cwd()
            os.chdir(launch_cwd or project_root)
            try:
                with contextlib.redirect_stdout(proc_out), contextlib.redirect_stderr(proc_err):
                    rc = goalflight_dispatch.main(
                        [
                            "--agent",
                            "codex",
                            "--unregistered-forced",
                            "--dispatch-id",
                            dispatch_id,
                            "--tail",
                            str(tmp / f"{dispatch_id}.tail"),
                            "--status-json",
                            str(tmp / f"{dispatch_id}.status.json"),
                            "--poll-secs",
                            "0.1",
                            "--max-idle-secs",
                            "5",
                            "--capacity-wait-s",
                            "0",
                            "--foreground",
                            "--ignore-git-warn",
                            *prompt_args,
                        ]
                    )
            finally:
                os.chdir(previous_cwd)
    finally:
        goalflight_dispatch.build_worker = old_build_worker

    assert rc == 0, proc_out.getvalue() + proc_err.getvalue()
    return seen_path.read_text(encoding="utf-8")


def _run_git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=True,
    )


def _repo_with_orientation(tmp: Path) -> tuple[Path, Path, Path]:
    repo = tmp / "repo"
    worktree = tmp / "linked-worktree"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=True)
    _run_git(repo, "config", "user.email", "goalflight@example.test")
    _run_git(repo, "config", "user.name", "Goal Flight Test")
    repo.joinpath("README.md").write_text("fixture\n", encoding="utf-8")
    _run_git(repo, "add", "README.md")
    _run_git(repo, "commit", "-m", "fixture")
    orientation = repo / "docs-private" / "rag" / "ORIENTATION.md"
    orientation.parent.mkdir(parents=True)
    orientation.write_text("fixture orientation\n", encoding="utf-8")
    _run_git(repo, "worktree", "add", "--detach", str(worktree), "HEAD")
    return repo, worktree, orientation


def case_inline_prompt_exports_original_prompt_file() -> None:
    if skip_case_posix_on_native_windows(
        "case_inline_prompt_exports_original_prompt_file",
        "prompt env export launches a POSIX/WSL bash-tail dispatch worker",
    ):
        return

    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        project_root, _worktree, _orientation = _repo_with_orientation(tmp)
        dispatch_id = "inline-prompt-env"
        prompt_text = "Line one\n\nLine three\n"
        seen = tmp / "seen-inline.txt"
        seen_text = _run_prompt_env_case(
            tmp, project_root, dispatch_id, ["--prompt", prompt_text], seen
        )
        prompt_env, steer_env = seen_text.splitlines()
        expected_prompt = tmp / "dispatch" / f"{dispatch_id}.prompt"

        assert Path(prompt_env) == expected_prompt, seen_text
        assert expected_prompt.read_text(encoding="utf-8") == prompt_text
        assert steer_env == str(_mailbox(tmp, dispatch_id)), seen_text


def case_prompt_file_exports_given_path() -> None:
    if skip_case_posix_on_native_windows(
        "case_prompt_file_exports_given_path",
        "prompt env export launches a POSIX/WSL bash-tail dispatch worker",
    ):
        return

    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        project_root, _worktree, _orientation = _repo_with_orientation(tmp)
        dispatch_id = "file-prompt-env"
        prompt_file = tmp / "brief.md"
        prompt_file.write_text("Read the durable brief.\n", encoding="utf-8")
        seen = tmp / "seen-file.txt"
        seen_text = _run_prompt_env_case(
            tmp, project_root, dispatch_id, ["--prompt-file", str(prompt_file)], seen
        )
        prompt_env, steer_env = seen_text.splitlines()

        # Export contract: resolved absolute path (symlink-canonical), so the
        # worker's re-read works from any cwd.
        assert Path(prompt_env) == prompt_file.resolve(), seen_text
        assert steer_env == str(_mailbox(tmp, dispatch_id)), seen_text


def case_relative_prompt_file_exports_resolved_absolute_path() -> None:
    if skip_case_posix_on_native_windows(
        "case_relative_prompt_file_exports_resolved_absolute_path",
        "prompt env export launches a POSIX/WSL bash-tail dispatch worker",
    ):
        return

    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        dispatch_id = "rel-prompt-env"
        repo, _worktree, _orientation = _repo_with_orientation(tmp)
        prompt_dir = repo / "prompts"
        prompt_dir.mkdir()
        brief = prompt_dir / "brief.md"
        brief.write_text("Read the durable brief.\n", encoding="utf-8")
        seen = tmp / "seen-rel.txt"
        prev_cwd = os.getcwd()
        try:
            os.chdir(prompt_dir)
            seen_text = _run_prompt_env_case(
                tmp,
                repo,
                dispatch_id,
                ["--prompt-file", "brief.md"],
                seen,
                launch_cwd=prompt_dir,
            )
        finally:
            os.chdir(prev_cwd)
        prompt_env, _steer_env = seen_text.splitlines()

        # A relative --prompt-file must export resolved+absolute: the worker
        # re-reads $GOALFLIGHT_PROMPT_FILE from its OWN cwd, where a relative
        # path resolves against the wrong root.
        assert Path(prompt_env).is_absolute(), seen_text
        assert Path(prompt_env) == brief.resolve(), seen_text


def case_prompt_preamble_is_materialized() -> None:
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        body = tmp / "body.md"
        body_text = "Do work.\nCOMPLETE: prompt-case — done\n"
        body.write_text(body_text, encoding="utf-8")
        assembled = Path(goalflight_dispatch._materialize_steer_prompt(str(body), tmp / "dispatch", "prompt-case"))
        text = assembled.read_text(encoding="utf-8")
        expected = (
            goalflight_dispatch.WORKER_ONLY_PREAMBLE
            + "\n\n"
            + goalflight_dispatch.STEER_PROMPT_PREAMBLE
            + "\n\n"
            + goalflight_dispatch.PROMPT_FILE_PREAMBLE
            + "\n\n"
            + goalflight_dispatch.SCOPE_GUARD_PREAMBLE
            + "\n\n"
            + goalflight_dispatch.SEARCH_SCOPE_PREAMBLE
            + "\n\n"
            + goalflight_dispatch.INSTRUCTION_REFUSAL_PREAMBLE
            + "\n\nTerminal evidence identity contract:\n"
            + "- Every terminal marker payload starts with the exact dispatch id `prompt-case`.\n"
            + "- Successful final shape: `!COMPLETE: prompt-case — <summary>`.\n"
            + "- Use the same id prefix for READY, RESULT, FAILED, USER-NEED, "
            + "USER-CONFIRM, or BLOCKED. A generic or foreign marker is ignored."
            + "\n\n"
            + body_text
        )

        assert text == expected, text
        assert text.startswith(
            goalflight_dispatch.WORKER_ONLY_PREAMBLE
            + "\n\n"
            + goalflight_dispatch.STEER_PROMPT_PREAMBLE
            + "\n\n"
        ), text
        assert "`!STEER-ACK: <seq>`" in text, text
        assert "$GOALFLIGHT_DISPATCH_SCRIPT" in text, text
        assert "$GOALFLIGHT_PROMPT_FILE" in text, text
        assert "Re-read it after any internal compaction/summarization" in text, text
        assert "authoritative for the requested task" in text, text
        assert "does not contain all dispatch-level contracts" in text, text
        assert "Keep the contracts in this assembled prompt in force after compaction" in text, text
        assert "COMPLETE: prompt-case — done" in text, text
        assert assembled.name == "prompt-case.assembled.prompt", assembled
        assert stat.S_IMODE(assembled.stat().st_mode) == 0o600
        assert stat.S_IMODE(assembled.parent.stat().st_mode) == 0o700


def case_sandbox_and_instruction_status_contract_is_composed() -> None:
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        cwd = tmp / "worktree"
        cwd.mkdir()
        scratch = tmp / "scratch"
        scratch.mkdir()
        private_tmp = scratch / "grok-tmp"
        private_tmp.mkdir(mode=0o700)
        outbox = tmp / "question-outbox"
        outbox.mkdir(mode=0o700)
        environment = {
            "HOME": str(Path.home() / ".goal-flight" / "accounts" / "fixture" / "grok"),
            "TMPDIR": str(private_tmp),
            "GOALFLIGHT_SCRATCH_DIR": str(scratch),
            "GOALFLIGHT_QUESTION_OUTBOX_DIR": str(outbox),
        }
        grok_args = SimpleNamespace(
            agent="grok-code",
            shape="bash",
            read_only=True,
            os_sandbox=None,
            worker=None,
        )
        assert goalflight_dispatch._supported_os_sandbox_profile(grok_args) == (
            "read-only" if goalflight_compat.is_macos() else None
        )
        from goalflight_os_sandbox import macos_sandbox_profile

        expected_profile, _roots = macos_sandbox_profile(
            str(cwd),
            "read-only",
            agent="grok-code",
            command="grok",
            environment=environment,
        )
        body = tmp / "read-only.md"
        body.write_text(
            "Run this heredoc probe to gather context:\n"
            "```sh\npython3 - <<'PY'\nprint('probe')\nPY\n```\n"
            "[REQUIRED] Complete the analysis.\n",
            encoding="utf-8",
        )
        assembled = Path(goalflight_dispatch._materialize_steer_prompt(
            str(body), tmp / "dispatch", "read-only-contract",
            agent="grok-code", sandbox_profile="read-only",
            sandbox_cwd=str(cwd), sandbox_command="grok",
            sandbox_environment=environment,
        ))
        text = assembled.read_text(encoding="utf-8")
        assert expected_profile in text
        assert "(allow sysctl*)" in text
        assert str(private_tmp.resolve()) in text
        assert "permits writes only" in text and "allowed roots" in text
        assert "write files anywhere" not in text
        assert "denies host process and system inspection" not in text
        assert "exploratory diagnostic requested only for context is OPTIONAL" in text
        assert "[OPTIONAL] Run this heredoc" not in text
        assert "continue with the remaining work" in text
        assert "Verification named in the brief" in text
        assert "always REQUIRED even if labeled OPTIONAL" in text
        assert "[REQUIRED]" in text and "stop and emit the dispatch-id-bound BLOCKED marker" in text
        assert "prevents completing the actual task" not in text
        assert "do not retry a rejected command" in text

        codex = Path(goalflight_dispatch._materialize_steer_prompt(
            str(body), tmp / "dispatch", "codex-sandbox-contract",
            agent="codex", sandbox_profile="read-only",
        )).read_text(encoding="utf-8")
        assert "Codex is launched with `--sandbox read-only`" in codex
        assert "(deny default)" not in codex

        fallback = goalflight_dispatch._sandbox_worker_preamble(
            "off", agent="codex-acp", shape="acp", acp_permission_fallback=True,
        )
        assert "permission-only read-only fallback" in fallback
        assert "the OS profile is off" in fallback
        assert "Seatbelt filesystem or process rules are active" in fallback


def case_resume_contract_supersedes_nested_prompt_contracts() -> None:
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        body = tmp / "body.md"
        body.write_text("Continue the existing task.\n", encoding="utf-8")
        fresh = Path(goalflight_dispatch._materialize_steer_prompt(
            str(body), tmp / "dispatch", "fresh-contract",
        )).read_text(encoding="utf-8")
        resumed = Path(goalflight_dispatch._materialize_steer_prompt(
            str(body), tmp / "dispatch", "resumed-contract",
            resume_contract_supersession=True,
        )).read_text(encoding="utf-8")
        assert goalflight_dispatch.RESUME_CONTRACT_SUPERSESSION_PREAMBLE not in fresh
        assert resumed.startswith(
            goalflight_dispatch.WORKER_ONLY_PREAMBLE
            + "\n\n"
            + goalflight_dispatch.RESUME_CONTRACT_SUPERSESSION_PREAMBLE
        )


def case_commit_instructions_include_custody_block() -> None:
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        body = tmp / "clone-work.md"
        body.write_text("Use a throwaway clone to implement the fix.\n", encoding="utf-8")
        assembled = Path(goalflight_dispatch._materialize_steer_prompt(
            str(body), tmp / "dispatch", "commit-custody",
            agent="codex", sandbox_profile="workspace-write",
        ))
        text = assembled.read_text(encoding="utf-8")
        assert "Use a throwaway clone to implement the fix" in text
        assert "Commit in this dispatch's assigned seat/worktree" in text
        assert "Never leave the commit only in a temporary clone" in text
        assert "committing is not self-review" in text
        assert "The controller reviews the commit after it lands in the seat" in text

        assert goalflight_dispatch._prompt_requests_commit("Commit FIRST.")
        assert goalflight_dispatch._prompt_requests_commit("Commit incrementally.")
        # Explicit land/push requests are in scope because their change still
        # needs a seat-local commit before it leaves a temporary clone.
        assert goalflight_dispatch._prompt_requests_commit("Land the fix.")
        assert goalflight_dispatch._prompt_requests_commit("Push this change.")
        assert goalflight_dispatch._prompt_requests_commit("Pin the SHA.")

        for fragment in (
            "do not commit",
            "don't commit",
            "never commit",
            "no commit",
            "DO NOT COMMIT",
            "do **not** commit",
            "Commit under review.",
            "pre-commit reviewer",
        ):
            assert not goalflight_dispatch._prompt_requests_commit(fragment), fragment

        body.write_text("Commit incrementally, one logical chunk at a time.\n", encoding="utf-8")
        incremental = Path(goalflight_dispatch._materialize_steer_prompt(
            str(body), tmp / "dispatch", "incremental-custody",
            agent="codex",
        )).read_text(encoding="utf-8")
        assert "Commit FIRST" in incremental
        assert "committing is not self-review" in incremental
        assert "The controller reviews the commit after it lands in the seat" in incremental


def case_remote_ci_prompt_uses_project_command_id() -> None:
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        config = tmp / "remote-ci.json"
        config.write_text(json.dumps({"runner": {
            "heavy_command_ids": ["project-gate", "project-integration"]
        }}), encoding="utf-8")
        body = tmp / "body.md"
        body.write_text("Run the requested work.\n", encoding="utf-8")
        assembled = Path(goalflight_dispatch._materialize_steer_prompt(
            str(body), tmp / "dispatch", "remote-ci-prompt",
            remote_ci_environment={
                "GOALFLIGHT_REMOTE_RUN": "/repo/bin/goalflight-remote-run",
                "GOALFLIGHT_REMOTE_CI_CONFIG": str(config),
            },
        ))
        text = assembled.read_text(encoding="utf-8")
        assert '"$GOALFLIGHT_REMOTE_RUN" --kind gate --cmd-id project-gate' in text
        assert ('run the matched pair: "$GOALFLIGHT_REMOTE_RUN" --pair --base '
                '<base-sha> --cmd-id project-gate -- <pytest command>') in text
        assert "other configured IDs: project-integration" in text
        assert text.count("Remote CI: run test suites and other heavy CPU") == 1

        config.write_text(json.dumps({"runner": {"heavy_command_ids": []}}), encoding="utf-8")
        without_ids = goalflight_dispatch.goalflight_dispatch_paths.remote_ci_worker_prompt({
            "GOALFLIGHT_REMOTE_RUN": "/repo/bin/goalflight-remote-run",
            "GOALFLIGHT_REMOTE_CI_CONFIG": str(config),
        })
        assert without_ids is not None
        assert "--cmd-id <configured-command-id>" in without_ids


def case_resume_remote_ci_prompt_is_not_duplicated() -> None:
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        body = tmp / "resume.md"
        instruction = (
            'Remote CI: run test suites and other heavy CPU through '
            '"$GOALFLIGHT_REMOTE_RUN" --kind gate -- <command>.'
        )
        body.write_text(f"Continue work.\n\n{instruction}\n", encoding="utf-8")
        assembled = Path(goalflight_dispatch._materialize_steer_prompt(
            str(body), tmp / "dispatch", "resume-remote-ci-prompt",
            remote_ci_environment={"GOALFLIGHT_REMOTE_RUN": "/runner"},
        ))
        text = assembled.read_text(encoding="utf-8")
        assert text.count("Remote CI: run test suites and other heavy CPU") == 1


def case_orientation_preamble_is_materialized_when_present() -> None:
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        repo, _worktree, orientation = _repo_with_orientation(tmp)
        body = tmp / "body.md"
        body.write_text("Decide and implement.\n", encoding="utf-8")
        orientation_path = goalflight_dispatch._project_orientation_path(repo)
        assert orientation_path == orientation.resolve(), orientation_path

        assembled = Path(
            goalflight_dispatch._materialize_steer_prompt(
                str(body),
                tmp / "dispatch",
                "orientation-case",
                agent="codex",
                orientation_path=orientation_path,
            )
        )
        text = assembled.read_text(encoding="utf-8")

        assert "PROJECT ORIENTATION\n" in text, text
        assert f"Path: {orientation.resolve()}" in text, text
        assert goalflight_dispatch.PROJECT_ORIENTATION_SCOPE_RULE in text, text
        assert "Decide and implement." in text, text


def case_no_orientation_suppresses_orientation_path() -> None:
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        repo, _worktree, _orientation = _repo_with_orientation(tmp)
        assert goalflight_dispatch._project_orientation_path(repo, disabled=True) is None


def case_orientation_path_resolves_linked_worktree_to_main_root() -> None:
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        _repo, worktree, orientation = _repo_with_orientation(tmp)
        resolved = goalflight_dispatch._project_orientation_path(worktree)
        assert resolved == orientation.resolve(), resolved


def case_orientation_path_resolves_from_repo_subdirectory() -> None:
    # rE P1: git emits a RELATIVE --git-common-dir from the command cwd, so a
    # dispatch cwd nested inside the checkout must still resolve the repo root
    # (resolving the relative form against toplevel walked out of the tree).
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        repo, worktree, orientation = _repo_with_orientation(tmp)
        subdir = repo / "nested" / "leaf"
        subdir.mkdir(parents=True)
        resolved = goalflight_dispatch._project_orientation_path(subdir)
        assert resolved == orientation.resolve(), resolved
        wt_subdir = worktree / "nested" / "leaf"
        wt_subdir.mkdir(parents=True)
        wt_resolved = goalflight_dispatch._project_orientation_path(wt_subdir)
        assert wt_resolved == orientation.resolve(), wt_resolved


def case_grok_prompt_adds_execution_and_terminal_contract() -> None:
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        body = tmp / "body.md"
        body.write_text("Write target.txt with ok.\n", encoding="utf-8")
        assembled = Path(
            goalflight_dispatch._materialize_steer_prompt(
                str(body),
                tmp / "dispatch",
                "grok-prompt-case",
                agent="grok-code",
            )
        )
        text = assembled.read_text(encoding="utf-8")

        expected_prefix = (
            goalflight_dispatch.WORKER_ONLY_PREAMBLE
            + "\n\n"
            + goalflight_dispatch.STEER_PROMPT_PREAMBLE
            + "\n\n"
            + goalflight_dispatch.PROMPT_FILE_PREAMBLE
            + "\n\n"
            + goalflight_dispatch.WORKER_EXECUTION_PREAMBLE
            + "\n\n"
        )
        assert text.startswith(expected_prefix), text
        assert "Use your available tools to actually perform" in text, text
        assert "`!COMPLETE: grok-prompt-case — <summary>`" in text, text
        assert "without this dispatch's id is ignored" in text, text
        assert "last non-empty line" in text, text
        assert "Write target.txt with ok." in text, text


def case_execution_preamble_does_not_excuse_id_less_success_markers() -> None:
    # The watcher ignores a COMPLETE/READY/RESULT line that lacks this
    # dispatch's id (goalflight_watch._terminal_marker_matches_dispatch).
    # "Legacy unprefixed marker lines remain accepted" meant the optional `!`
    # sigil, but beside an id-prefix contract it reads as "the id prefix is
    # optional" -- the exact wrong shape that records a finished worker dead.
    preamble = goalflight_dispatch.WORKER_EXECUTION_PREAMBLE
    assert "remain accepted" not in preamble, preamble
    assert "without this dispatch's id is ignored" in preamble, preamble
    # The `!` sigil really is optional (goalflight_terminal.MARKER_SIGIL_OPT_RE);
    # the preamble may say so, but only about the sigil.
    assert "`!` sigil is optional" in preamble, preamble


def case_codex_prompt_does_not_add_grok_contract() -> None:
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        body = tmp / "body.md"
        body.write_text("Review only.\n", encoding="utf-8")
        assembled = Path(
            goalflight_dispatch._materialize_steer_prompt(
                str(body),
                tmp / "dispatch",
                "codex-prompt-case",
                agent="codex",
            )
        )
        text = assembled.read_text(encoding="utf-8")

        expected_prefix = (
            goalflight_dispatch.WORKER_ONLY_PREAMBLE
            + "\n\n"
            + goalflight_dispatch.STEER_PROMPT_PREAMBLE
            + "\n\n"
            + goalflight_dispatch.PROMPT_FILE_PREAMBLE
            + "\n\n"
        )
        assert text.startswith(expected_prefix), text
        assert goalflight_dispatch.WORKER_EXECUTION_PREAMBLE not in text, text
        assert "`!COMPLETE: codex-prompt-case — <summary>`" in text, text


def case_preamble_routing_matrix() -> None:
    search_rule = goalflight_dispatch.SEARCH_SCOPE_PREAMBLE
    for phrase in ("worktree", "git ls-files", "rg --files", "rg <pattern>",
                   "Never run `find` or recursive globbing", "above your worktree",
                   "repo root that contains `worktrees/`", "$HOME", "~/.goal-flight",
                   "/tmp", "/private/tmp", "$TMPDIR", "known path"):
        assert phrase in search_rule, phrase
    assert len(search_rule.splitlines()) == 7
    for agent in ("grok-code", "grok-research", "moonshot", "codex", "cursor",
                  "cursor-agent", "claude", "claude-acp", "codex-acp", "opencode", None):
        preamble = goalflight_dispatch._worker_prompt_preamble(agent)
        assert search_rule in preamble, agent
        assert search_rule.splitlines()[5] in preamble, agent
    for path in ("SKILL.md", "templates/worker-orientation.md",
                 "templates/codex-goal-prompt.md.tpl", "protocols/subagent-preamble.md",
                 "protocols/worker-context-package.md"):
        doc = (ROOT / path).read_text(encoding="utf-8")
        for line in (*search_rule.splitlines()[:4], search_rule.splitlines()[-1]):
            assert line in doc, (path, line)
        for phrase in (
            "`$TMPDIR` for scratch files, separate clones, logs and browser profiles",
            "/tmp/<name>",
            "/private/tmp/<name>",
        ):
            assert phrase in doc, (path, phrase)
    # SKILL.md is already over the 45,000-byte cap, so its own writable
    # dispatch contract remains the doc mirror for this line of the preamble.
    assert "Every writable dispatch acquires a **repository-scoped pooled worktree**" in (
        ROOT / "SKILL.md"
    ).read_text(encoding="utf-8")
    assert "--use-mock-keychain" in search_rule
    assert "--user-data-dir" in search_rule
    # Lock the shared execution-preamble routing across every agent label.
    worker_marker = goalflight_dispatch.WORKER_EXECUTION_PREAMBLE
    for agent in ("grok-code", "grok-research", "moonshot"):
        assert worker_marker in goalflight_dispatch._worker_prompt_preamble(agent), agent
    # "kimi" is the retired moonshot handle: not a preset, no worker preamble.
    for agent in ("codex", "cursor", "claude", "claude-acp", "codex-acp", "opencode", "kimi", None):
        assert worker_marker not in goalflight_dispatch._worker_prompt_preamble(agent), agent
    # The steer preamble is always present regardless of agent.
    for agent in ("grok-code", "grok-research", "codex", None):
        preamble = goalflight_dispatch._worker_prompt_preamble(agent)
        assert goalflight_dispatch.STEER_PROMPT_PREAMBLE in preamble, agent
        assert goalflight_dispatch.PROMPT_FILE_PREAMBLE in preamble, agent
    # The scope guard goes to every dispatch regardless of agent. It binds
    # code CHANGES, not the task in general, so it is inert for a reviewer
    # whose deliverable is findings rather than a diff -- which is why there
    # is no opt-out flag. (--read-only cannot serve as one: it refuses any
    # prompt that writes a review artifact, so no review dispatch sets it.)
    scope_marker = goalflight_dispatch.SCOPE_GUARD_PREAMBLE
    for agent in ("grok-code", "grok-research", "moonshot", "codex", "cursor",
                  "claude-acp", "codex-acp", "opencode", None):
        assert scope_marker in goalflight_dispatch._worker_prompt_preamble(agent), agent
    assert "where this task changes code" in scope_marker
    # Escalation must be named, or an unattended worker expands scope silently.
    assert "reason `scope`" in scope_marker
    # ...but never as a literal marker token: this text lands in every prompt,
    # and worker tails echo the prompt, so a bare "BLOCKED:" here would be
    # scraped back as a real escalation from every dispatch (b-109).
    assert "BLOCKED:" not in scope_marker
    # Scope limits must not read as permission to skip the brief's verification.
    assert "not on VERIFICATION" in scope_marker
    # The optional-refusal rule permits continuing without retrying a
    # diagnostic; Cursor's tool rule independently forbids retrying any
    # rejected command. They point to the same action.
    assert "do not retry a rejected command" in goalflight_dispatch.INSTRUCTION_REFUSAL_PREAMBLE
    assert "NEVER retry a rejected command" in goalflight_dispatch.CURSOR_TOOLING_PREAMBLE
    assert "For an OPTIONAL diagnostic, report the refusal and continue" in (
        goalflight_dispatch.CURSOR_TOOLING_PREAMBLE
    )


def _runner_exit_probe(code: int | str | None) -> subprocess.CompletedProcess[str]:
    probe = r"""
import importlib.util
import json
import sys
from pathlib import Path

source = Path(sys.argv[1])
sys.path.insert(0, str(source.parent))
spec = importlib.util.spec_from_file_location("dispatch_steer_probe", source)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
for name, value in tuple(vars(module).items()):
    if name.startswith("case_") and callable(value):
        delattr(module, name)
code = json.loads(sys.argv[2])
def case_probe():
    raise SystemExit(code)
module.case_probe = case_probe
module._RUNNER_REGISTRY = (case_probe,)
module._run_cases()
"""
    return subprocess.run(
        [sys.executable, "-c", probe, str(Path(__file__).resolve()), json.dumps(code)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=15,
    )


def _assert_runner_interruption(
    code: int | str | None, status: int, *, converted_to_assertion: bool
) -> None:
    result = _runner_exit_probe(code)
    summary = (
        "case runner interrupted: 1 of 1 executed, 0 passed, 0 failed; "
        "interrupted in case_probe"
    )
    assert result.returncode == status, (code, result.returncode, result.stdout, result.stderr)
    assert summary in result.stdout, (code, result.stdout, result.stderr)
    if converted_to_assertion:
        assert "AssertionError: " + summary in result.stderr, result.stderr
    else:
        assert "AssertionError:" not in result.stderr, result.stderr


def case_runner_interruption_systemexit_none_is_failure() -> None:
    if skip_case_posix_on_native_windows(
        "case_runner_interruption_systemexit_none_is_failure",
        "runner exit status mapping follows POSIX wait status",
    ):
        return
    _assert_runner_interruption(None, 1, converted_to_assertion=True)


def case_runner_interruption_systemexit_zero_is_failure() -> None:
    if skip_case_posix_on_native_windows(
        "case_runner_interruption_systemexit_zero_is_failure",
        "runner exit status mapping follows POSIX wait status",
    ):
        return
    _assert_runner_interruption(0, 1, converted_to_assertion=True)


def case_runner_interruption_systemexit_256_is_failure() -> None:
    if skip_case_posix_on_native_windows(
        "case_runner_interruption_systemexit_256_is_failure",
        "runner exit status mapping follows POSIX wait status",
    ):
        return
    _assert_runner_interruption(256, 1, converted_to_assertion=True)


def case_runner_interruption_noninteger_systemexit_fails() -> None:
    if skip_case_posix_on_native_windows(
        "case_runner_interruption_noninteger_systemexit_fails",
        "runner exit status mapping follows POSIX wait status",
    ):
        return
    result = _runner_exit_probe("unfinished case")
    summary = (
        "case runner interrupted: 1 of 1 executed, 0 passed, 0 failed; "
        "interrupted in case_probe"
    )
    assert result.returncode == 1, (result.returncode, result.stdout, result.stderr)
    assert summary in result.stdout, (result.stdout, result.stderr)
    assert result.stderr == "unfinished case\n", result.stderr


def case_runner_interruption_nonzero_systemexit_propagates() -> None:
    if skip_case_posix_on_native_windows(
        "case_runner_interruption_nonzero_systemexit_propagates",
        "runner exit status mapping follows POSIX wait status",
    ):
        return
    _assert_runner_interruption(7, 7, converted_to_assertion=False)


def case_runner_interruption_oversized_systemexit_propagates() -> None:
    if skip_case_posix_on_native_windows(
        "case_runner_interruption_oversized_systemexit_propagates",
        "runner exit status mapping follows POSIX wait status",
    ):
        return
    overflow = 1 << (ctypes.sizeof(ctypes.c_longlong) * 8 - 1)
    _assert_runner_interruption(overflow, 255, converted_to_assertion=False)


def case_state_dir_cleanup_preserves_body_failure() -> None:
    keys = (
        "GOALFLIGHT_STATE_DIR",
        "GOALFLIGHT_DISPATCH_DIR",
        "GOALFLIGHT_MESSAGES_DIR",
        "GOAL_FLIGHT_PIDFILE_DIR",
        "GOALFLIGHT_TASK_STORE_DIR",
        "GOALFLIGHT_JOURNAL_DIR",
        "GOALFLIGHT_WAKE_LEDGER_DIR",
        "GOALFLIGHT_PROJECT_ROOT",
    )
    original_environ = os.environ
    original_values = {key: original_environ.get(key) for key in keys}

    class FailingRestoreEnvironment(dict):
        def __setitem__(self, key, value):
            if key == "GOALFLIGHT_STATE_DIR":
                raise OSError("environment restore assignment failed")
            super().__setitem__(key, value)

        def pop(self, key, *default):
            if key == "GOALFLIGHT_STATE_DIR":
                raise OSError("environment restore removal failed")
            return super().pop(key, *default)

    body_failure = ValueError("original test failure")
    caught = None
    with tempfile.TemporaryDirectory() as directory:
        try:
            try:
                with _state_dir(Path(directory)):
                    os.environ = FailingRestoreEnvironment(original_environ.copy())
                    raise body_failure
            except BaseException as exc:
                caught = exc
        finally:
            os.environ = original_environ
            for key, value in original_values.items():
                if value is None:
                    original_environ.pop(key, None)
                else:
                    original_environ[key] = value

    assert caught is body_failure, caught
    attached = "\n".join(getattr(caught, "__notes__", ()))
    if caught.__cause__ is not None:
        attached += "\n" + repr(caught.__cause__)
    assert "environment restore" in attached, attached


def case_host_pool_guard_preserves_body_failure_and_mutation() -> None:
    original_snapshot = _host_pool_snapshot
    snapshots = iter(("before", "after"))
    globals()["_host_pool_snapshot"] = lambda: next(snapshots)
    body_failure = ValueError("original test failure")
    caught = None
    try:
        try:
            with _host_pool_guard():
                raise body_failure
        except BaseException as exc:
            caught = exc
    finally:
        globals()["_host_pool_snapshot"] = original_snapshot

    assert caught is body_failure, caught
    attached = "\n".join(getattr(caught, "__notes__", ()))
    if caught.__cause__ is not None:
        attached += "\n" + repr(caught.__cause__)
    assert "worktree pool" in attached, attached


def case_host_pool_guard_reports_mutation_with_systemexit() -> None:
    if skip_case_posix_on_native_windows(
        "case_host_pool_guard_reports_mutation_with_systemexit",
        "runner exit status mapping follows POSIX wait status",
    ):
        return

    probe = r"""
import importlib.util
import sys
from pathlib import Path

source = Path(sys.argv[1])
sys.path.insert(0, str(source.parent))
spec = importlib.util.spec_from_file_location("dispatch_steer_probe", source)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
for name, value in tuple(vars(module).items()):
    if name.startswith("case_") and callable(value):
        delattr(module, name)
snapshots = iter(("before", "after"))
module._host_pool_snapshot = lambda: next(snapshots)
def case_probe():
    raise SystemExit(7)
module.case_probe = case_probe
module._RUNNER_REGISTRY = (case_probe,)
with module._host_pool_guard():
    module._run_cases()
"""
    result = subprocess.run(
        [sys.executable, "-c", probe, str(Path(__file__).resolve())],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=15,
    )
    diagnostic = (
        "test mutated the enclosing repository's worktree pool; "
        "dispatch launch was not isolated"
    )
    summary = (
        "case runner interrupted: 1 of 1 executed, 0 passed, 0 failed; "
        "interrupted in case_probe"
    )
    assert result.returncode == 7, (result.returncode, result.stdout, result.stderr)
    assert summary in result.stdout, (result.stdout, result.stderr)
    assert diagnostic in result.stderr, (result.stdout, result.stderr)


def case_host_pool_guard_preserves_systemexit_with_closed_stderr() -> None:
    if skip_case_posix_on_native_windows(
        "case_host_pool_guard_preserves_systemexit_with_closed_stderr",
        "runner exit status mapping follows POSIX wait status",
    ):
        return

    probe = r"""
import importlib.util
import sys
from pathlib import Path

source = Path(sys.argv[1])
sys.path.insert(0, str(source.parent))
spec = importlib.util.spec_from_file_location("dispatch_steer_probe", source)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
for name, value in tuple(vars(module).items()):
    if name.startswith("case_") and callable(value):
        delattr(module, name)
sys.stderr.close()
snapshots = iter(("before", "after"))
module._host_pool_snapshot = lambda: next(snapshots)
def case_probe():
    raise SystemExit(7)
module.case_probe = case_probe
module._RUNNER_REGISTRY = (case_probe,)
with module._host_pool_guard():
    module._run_cases()
"""
    result = subprocess.run(
        [sys.executable, "-c", probe, str(Path(__file__).resolve())],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=15,
    )
    summary = (
        "case runner interrupted: 1 of 1 executed, 0 passed, 0 failed; "
        "interrupted in case_probe"
    )
    assert result.returncode == 7, (result.returncode, result.stdout, result.stderr)
    assert summary in result.stdout, (result.stdout, result.stderr)


_RUNNER_REGISTRY = (
    case_bash_append_and_list_with_ack,
    case_shape_routing_and_missing_record,
    case_append_refusals_keep_original_error,
    case_oversized_multiline_steer_refuses_before_write,
    case_steer_sender_identity_and_cross_project_guard,
    case_steer_unknown_project_root_fails_closed,
    case_acp_list_reads_status_ack_dict,
    case_prefixed_ack_is_parsed_by_both_call_sites,
    case_dead_worker_records_but_does_not_claim_delivery,
    case_worker_view_failure_reports_replay_command,
    case_steer_is_no_worker_early_exit,
    case_worker_wait_accepts_generic_backlog,
    case_acked_backlog_does_not_suppress_a_new_question,
    case_identified_late_reply_resumes_across_attempts,
    case_worker_confirm_does_not_accept_decision_free_backlog,
    case_worker_wait_atomic_question_has_own_deadline,
    case_worker_wait_question_is_written_before_waiting,
    case_controller_reply_reaches_waiter,
    case_worker_wait_requires_an_atomic_question,
    case_worker_wait_carrier_error_is_reported,
    case_controller_reply_is_typed_and_wait_id_correlated,
    test_refused_wait_renewal_notifies_and_delivers_prior_reply,
    test_plain_steer_refusal_requires_current_live_confirm_wait,
    test_plain_steer_refusal_command_delivers_typed_reply,
    test_plain_steer_rechecks_confirm_wait_under_append_lock,
    test_watcher_rss_steer_is_delivered_during_confirm_wait,
    test_distinct_refused_wait_renewals_notify_once_each_without_retry,
    case_b723_timed_out_question_does_not_hide_completion,
    case_b723_active_question_is_one_open_wait,
    case_b723_repair_keeps_attention_marker_for_indeterminate_worker,
    case_b723_repair_clears_stale_blocked_pending_for_complete,
    case_b723_repair_keeps_live_blocked_provisional_after_rate_limit_normalization,
    case_concurrent_appends_have_monotonic_unique_seq,
    case_spawn_exports_steer_env,
    case_inline_prompt_exports_original_prompt_file,
    case_prompt_file_exports_given_path,
    case_relative_prompt_file_exports_resolved_absolute_path,
    case_prompt_preamble_is_materialized,
    case_sandbox_and_instruction_status_contract_is_composed,
    case_resume_contract_supersedes_nested_prompt_contracts,
    case_commit_instructions_include_custody_block,
    case_remote_ci_prompt_uses_project_command_id,
    case_resume_remote_ci_prompt_is_not_duplicated,
    case_orientation_preamble_is_materialized_when_present,
    case_no_orientation_suppresses_orientation_path,
    case_orientation_path_resolves_linked_worktree_to_main_root,
    case_orientation_path_resolves_from_repo_subdirectory,
    case_grok_prompt_adds_execution_and_terminal_contract,
    case_execution_preamble_does_not_excuse_id_less_success_markers,
    case_codex_prompt_does_not_add_grok_contract,
    case_preamble_routing_matrix,
    case_runner_interruption_systemexit_none_is_failure,
    case_runner_interruption_systemexit_zero_is_failure,
    case_runner_interruption_systemexit_256_is_failure,
    case_runner_interruption_noninteger_systemexit_fails,
    case_runner_interruption_nonzero_systemexit_propagates,
    case_runner_interruption_oversized_systemexit_propagates,
    case_state_dir_cleanup_preserves_body_failure,
    case_host_pool_guard_preserves_body_failure_and_mutation,
    case_host_pool_guard_reports_mutation_with_systemexit,
    case_host_pool_guard_preserves_systemexit_with_closed_stderr,
)


def _run_cases() -> None:
    defined_cases = {
        name
        for name, value in globals().items()
        if name.startswith("case_") and callable(value)
    }
    registered_cases = [
        case.__name__ for case in _RUNNER_REGISTRY if case.__name__.startswith("case_")
    ]
    if len(registered_cases) != len(set(registered_cases)):
        raise AssertionError("case registry contains duplicate case callables")
    if set(registered_cases) != defined_cases:
        missing = sorted(defined_cases - set(registered_cases))
        stale = sorted(set(registered_cases) - defined_cases)
        raise AssertionError(
            "case registry does not match module case callables: "
            f"unregistered={missing}, undefined={stale}"
        )

    failures: list[tuple[str, str]] = []
    executed = 0
    passed = 0
    current_case: str | None = None
    try:
        for case in _RUNNER_REGISTRY:
            current_case = case.__name__
            try:
                try:
                    case()
                except Exception:
                    failures.append((case.__name__, traceback.format_exc()))
                else:
                    passed += 1
            finally:
                executed += 1
            current_case = None
    except BaseException as exc:
        summary = (
            f"case runner interrupted: {executed} of {len(_RUNNER_REGISTRY)} executed, "
            f"{passed} passed, {len(failures)} failed"
        )
        if current_case is not None:
            summary += f"; interrupted in {current_case}"
        print(summary, flush=True)
        if isinstance(exc, SystemExit):
            code = exc.code
            # CPython maps None to 0, non-integers to printed status 1, and
            # integers through signed long long then C int (overflow becomes
            # -1). POSIX wait status keeps only the low 8 bits of that C int.
            # Therefore success is None or a signed-long-long integer divisible
            # by 256; out-of-range integers remain failures with status 255.
            if code is None:
                successful_status = True
            elif isinstance(code, int):
                integer_code = int.__index__(code)
                long_long_bits = ctypes.sizeof(ctypes.c_longlong) * 8
                long_long_min = -(1 << (long_long_bits - 1))
                long_long_max = (1 << (long_long_bits - 1)) - 1
                successful_status = (
                    long_long_min <= integer_code <= long_long_max
                    and integer_code % 256 == 0
                )
            else:
                successful_status = False
            if successful_status:
                raise AssertionError(summary) from exc
        raise

    summary = f"case runner: {executed} executed, {passed} passed, {len(failures)} failed"
    if failures:
        details = "\n\n".join(
            f"FAIL: {name}\n{failure}" for name, failure in failures
        )
        raise AssertionError(f"{summary}\n\n{details}")
    print(summary)


def main() -> None:
    with _host_pool_guard():
        _run_cases()
    print("OK: goalflight_dispatch steer tests pass")


def test_dispatch_steer_cases() -> None:
    main()


if __name__ == "__main__":
    main()
