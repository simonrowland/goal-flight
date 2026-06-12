#!/usr/bin/env python3
"""Focused tests for file-backed review-job liveness monitoring."""

from __future__ import annotations

from support import skip_posix_on_native_windows

skip_posix_on_native_windows("review job tests use POSIX process groups and ps")

import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import textwrap
import time

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import goalflight_review_job
import goalflight_rate_pressure


def run(
    args: list[str],
    *,
    state_dir: Path,
    env: dict[str, str] | None = None,
    check: bool = True,
    timeout: float | None = None,
) -> subprocess.CompletedProcess[str]:
    merged_env = os.environ.copy()
    merged_env["GOALFLIGHT_STATE_DIR"] = str(state_dir)
    if env:
        merged_env.update(env)
    proc = subprocess.run(
        args,
        cwd=ROOT,
        env=merged_env,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
    )
    if check and proc.returncode != 0:
        raise AssertionError(f"{args} exited {proc.returncode}\nstdout={proc.stdout}\nstderr={proc.stderr}")
    return proc


def assert_true(name: str, condition: bool) -> None:
    if not condition:
        raise AssertionError(name)


def skipif(condition: bool, reason: str):
    def _decorator(func):
        def _wrapped(*args, **kwargs):
            if condition:
                print(f"SKIP: {func.__name__}: {reason}")
                return None
            return func(*args, **kwargs)
        return _wrapped
    return _decorator


def ps_pgid_available() -> bool:
    try:
        subprocess.check_output(
            ["ps", "-A", "-o", "pgid="],
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=2.0,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return True


def pgroup_has_processes(pgid: int) -> bool:
    output = subprocess.check_output(["ps", "-A", "-o", "pgid="], text=True, encoding="utf-8", errors="replace")
    return any(line.strip() == str(pgid) for line in output.splitlines())


def process_exists(pid: int) -> bool:
    result = subprocess.run(
        ["ps", "-p", str(pid), "-o", "pid="],
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
    )
    return bool(result.stdout.strip())


def write_fake_codex(path: Path) -> None:
    path.write_text(
        textwrap.dedent(
            r'''
            #!/usr/bin/env python3
            import json
            import os
            from pathlib import Path
            import signal
            import subprocess
            import sys
            import time

            def final_path() -> Path:
                args = sys.argv[1:]
                if "-o" not in args:
                    raise SystemExit("missing -o")
                return Path(args[args.index("-o") + 1])

            def emit(obj: dict) -> None:
                print(json.dumps(obj), flush=True)

            mode = os.environ.get("FAKE_REVIEW_MODE", "active")
            final = final_path()
            final.parent.mkdir(parents=True, exist_ok=True)
            _ = sys.stdin.read()

            if mode == "active":
                for idx in range(7):
                    emit({"type": "tick", "idx": idx})
                    time.sleep(0.25)
                final.write_text("complete\n")
                time.sleep(0.2)
                raise SystemExit(0)

            if mode == "final_early":
                emit({"type": "start"})
                final.write_text("ready before process exit\n")
                time.sleep(1.4)
                emit({"type": "done"})
                raise SystemExit(0)

            if mode == "partial_malformed":
                sys.stdout.write('{"type": "partial"')
                sys.stdout.flush()
                time.sleep(0.2)
                sys.stdout.write("}\n")
                sys.stdout.write("not-json\n")
                sys.stdout.write(json.dumps({"msg": {"type": "done"}}) + "\n")
                sys.stdout.flush()
                final.write_text("complete\n")
                raise SystemExit(0)

            if mode == "stderr_rate_limit_fail":
                sys.stderr.write("provider blocked: Check your settings to continue\\n")
                sys.stderr.flush()
                raise SystemExit(1)

            if mode == "complete_with_stderr":
                sys.stderr.write("warning: harmless stderr on successful review\\n")
                sys.stderr.flush()
                final.write_text("complete\\n")
                raise SystemExit(0)

            if mode == "stall_with_child":
                child_pid_file = Path(os.environ["FAKE_CHILD_PID_FILE"])
                child_term_file = Path(os.environ["FAKE_CHILD_TERM_FILE"])
                child_code = (
                    "import os\n"
                    "from pathlib import Path\n"
                    "import signal\n"
                    "import sys\n"
                    "import time\n"
                    "pid_file = Path(sys.argv[1])\n"
                    "term_file = Path(sys.argv[2])\n"
                    "pid_file.write_text(str(os.getpid()))\n"
                    "def handle(sig, frame):\n"
                    "    term_file.write_text('term')\n"
                    "    raise SystemExit(0)\n"
                    "signal.signal(signal.SIGTERM, handle)\n"
                    "while True:\n"
                    "    time.sleep(1)\n"
                )
                subprocess.Popen([sys.executable, "-c", child_code, str(child_pid_file), str(child_term_file)])
                emit({"type": "start"})
                while True:
                    time.sleep(1)

            if mode == "leak_child":
                child_pid_file = Path(os.environ["FAKE_CHILD_PID_FILE"])
                child_term_file = Path(os.environ["FAKE_CHILD_TERM_FILE"])
                child_code = (
                    "import os\n"
                    "from pathlib import Path\n"
                    "import signal\n"
                    "import sys\n"
                    "import time\n"
                    "pid_file = Path(sys.argv[1])\n"
                    "term_file = Path(sys.argv[2])\n"
                    "pid_file.write_text(str(os.getpid()))\n"
                    "def handle(sig, frame):\n"
                    "    term_file.write_text('term')\n"
                    "    raise SystemExit(0)\n"
                    "signal.signal(signal.SIGTERM, handle)\n"
                    "while True:\n"
                    "    time.sleep(1)\n"
                )
                subprocess.Popen([sys.executable, "-c", child_code, str(child_pid_file), str(child_term_file)])
                deadline = time.time() + 5
                while not child_pid_file.exists() and time.time() < deadline:
                    time.sleep(0.01)
                emit({"type": "parent-done"})
                final.write_text("parent exited but child stayed alive\n")
                raise SystemExit(0)

            raise SystemExit(f"unknown mode: {mode}")
            '''
        ).lstrip()
    )
    path.chmod(0o755)


def review_command(tmp: Path, fake_codex: Path, name: str, *, timeout_s: float = 1, max_quiet_s: float = 5) -> list[str]:
    prompt = tmp / f"{name}.prompt.md"
    prompt.write_text("review without editing\n")
    out_dir = tmp / "out"
    out_dir.mkdir(exist_ok=True)
    return [
        sys.executable,
        "scripts/goalflight_review_job.py",
        "--agent",
        "codex",
        "--name",
        name,
        "--repo",
        str(ROOT),
        "--prompt",
        str(prompt),
        "--output-dir",
        str(out_dir),
        "--codex-bin",
        str(fake_codex),
        "--timeout-s",
        str(timeout_s),
        "--max-quiet-s",
        str(max_quiet_s),
        "--heartbeat-interval",
        "0.1",
        "--json",
    ]


def capacity_test_env(state_dir: Path, **extra: str) -> dict[str, str]:
    env = os.environ.copy()
    env["GOALFLIGHT_STATE_DIR"] = str(state_dir)
    env["GOALFLIGHT_CAPACITY_MAX_TOTAL"] = "1"
    env.update(extra)
    return env


def hold_capacity(state_dir: Path, *, agent: str = "codex", dispatch_id: str = "held-review-capacity") -> str:
    proc = run(
        [
            sys.executable,
            "scripts/goalflight_capacity.py",
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
        state_dir=state_dir,
        env={"GOALFLIGHT_CAPACITY_MAX_TOTAL": "1"},
    )
    return json.loads(proc.stdout)["lease"]["lease_id"]


def release_capacity(state_dir: Path, lease_id: str) -> None:
    run(
        [sys.executable, "scripts/goalflight_capacity.py", "release", "--lease-id", lease_id],
        state_dir=state_dir,
    )


def wait_for_status(path: Path, state: str, *, timeout_s: float = 5.0) -> dict:
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


@skipif(os.name == "nt", reason="native Windows review-job dispatch is refused in Phase 1")
def test_review_job_capacity_wait_queues_until_slot_frees() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        state_dir = tmp / "state"
        fake = tmp / "fake-codex"
        write_fake_codex(fake)
        lease_id = hold_capacity(state_dir)
        cmd = review_command(tmp, fake, "queued-capacity", timeout_s=5, max_quiet_s=5)
        env = capacity_test_env(
            state_dir,
            GOALFLIGHT_CAPACITY_WAIT_S="6",
            FAKE_REVIEW_MODE="complete_with_stderr",
        )
        proc = subprocess.Popen(
            cmd,
            cwd=ROOT,
            env=env,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            status_path = tmp / "out" / "queued-capacity.status.json"
            waiting = wait_for_status(status_path, "waiting_capacity", timeout_s=5.0)
            assert_true("review waiting status records queue reason", waiting["reason"]["decision"] == "wait")
            release_capacity(state_dir, lease_id)
            stdout, stderr = proc.communicate(timeout=12)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.communicate(timeout=5)
        status = json.loads((tmp / "out" / "queued-capacity.status.json").read_text())
        assert_true(f"queued review completed: stdout={stdout} stderr={stderr}", proc.returncode == 0)
        assert_true("queued review final complete", status["state"] == "complete")
        assert_true("review emitted capacity wait progress", "CAPACITY-WAIT " in stderr)


@skipif(os.name == "nt", reason="native Windows review-job dispatch is refused in Phase 1")
def test_review_job_capacity_wait_deadline_blocks() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        state_dir = tmp / "state"
        fake = tmp / "fake-codex"
        write_fake_codex(fake)
        lease_id = hold_capacity(state_dir)
        try:
            proc = run(
                review_command(tmp, fake, "deadline-capacity", timeout_s=5, max_quiet_s=5),
                state_dir=state_dir,
                env={
                    "GOALFLIGHT_CAPACITY_MAX_TOTAL": "1",
                    "GOALFLIGHT_CAPACITY_WAIT_S": "0.2",
                    "FAKE_REVIEW_MODE": "complete_with_stderr",
                },
                check=False,
                timeout=6,
            )
        finally:
            release_capacity(state_dir, lease_id)
        status = json.loads((tmp / "out" / "deadline-capacity.status.json").read_text())
        assert_true("deadline review blocks", proc.returncode == 2 and status["state"] == "blocked_capacity")
        assert_true("deadline review enriches post-wait reason", status["reason"]["decision"] == "wait" and status["reason"]["attempts"] >= 2)
        assert_true("deadline review records waited_s", status["reason"]["waited_s"] >= 0.0)


@skipif(os.name == "nt", reason="native Windows review-job dispatch is refused in Phase 1")
def test_review_job_capacity_wait_zero_single_shot() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        state_dir = tmp / "state"
        fake = tmp / "fake-codex"
        write_fake_codex(fake)
        lease_id = hold_capacity(state_dir)
        try:
            proc = run(
                review_command(tmp, fake, "zero-capacity", timeout_s=5, max_quiet_s=5),
                state_dir=state_dir,
                env={
                    "GOALFLIGHT_CAPACITY_MAX_TOTAL": "1",
                    "GOALFLIGHT_CAPACITY_WAIT_S": "0",
                    "FAKE_REVIEW_MODE": "complete_with_stderr",
                },
                check=False,
                timeout=6,
            )
        finally:
            release_capacity(state_dir, lease_id)
        status = json.loads((tmp / "out" / "zero-capacity.status.json").read_text())
        assert_true("zero wait review blocks immediately", proc.returncode == 2 and status["state"] == "blocked_capacity")
        assert_true("zero wait preserves single-shot reason payload", "attempts" not in status["reason"] and "waited_s" not in status["reason"])


@skipif(os.name == "nt", reason="native Windows review-job dispatch is refused in Phase 1")
def test_review_job_capacity_wait_sigterm_terminalizes() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        state_dir = tmp / "state"
        fake = tmp / "fake-codex"
        write_fake_codex(fake)
        lease_id = hold_capacity(state_dir)
        cmd = review_command(tmp, fake, "sigterm-capacity", timeout_s=5, max_quiet_s=5)
        env = capacity_test_env(
            state_dir,
            GOALFLIGHT_CAPACITY_WAIT_S="6",
            FAKE_REVIEW_MODE="complete_with_stderr",
        )
        proc = subprocess.Popen(
            cmd,
            cwd=ROOT,
            env=env,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            status_path = tmp / "out" / "sigterm-capacity.status.json"
            wait_for_status(status_path, "waiting_capacity", timeout_s=5.0)
            proc.send_signal(signal.SIGTERM)
            stdout, stderr = proc.communicate(timeout=5)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.communicate(timeout=5)
            release_capacity(state_dir, lease_id)
        status = json.loads((tmp / "out" / "sigterm-capacity.status.json").read_text())
        assert_true(f"sigterm review exited 143: stdout={stdout} stderr={stderr}", proc.returncode == 143)
        assert_true("sigterm review terminalized capacity block", status["state"] == "blocked_capacity")
        assert_true("sigterm reason is interrupted", status["reason"]["reason"] == "wait_interrupted")
        assert_true("sigterm reason records attempts", status["reason"]["attempts"] == 1)
        assert_true("sigterm reason records prompt wait", status["reason"]["waited_s"] < 6.0)


@skipif(os.name == "nt", reason="native Windows review-job dispatch is refused in Phase 1")
def test_active_worker_can_complete_after_soft_timeout() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        state_dir = tmp / "state"
        fake = tmp / "fake-codex"
        write_fake_codex(fake)
        started = time.time()
        proc = run(
            review_command(tmp, fake, "active-past-timeout", timeout_s=1, max_quiet_s=5),
            state_dir=state_dir,
            env={"FAKE_REVIEW_MODE": "active"},
        )
        duration = time.time() - started
        status = json.loads((tmp / "out" / "active-past-timeout.status.json").read_text())
        assert_true("active review exits complete", proc.returncode == 0 and status["state"] == "complete")
        assert_true("review ran beyond soft timeout", duration >= 1.0 and status["soft_timeout_elapsed"] is True)
        assert_true("events tracked", status["events_seen"] >= 7 and status["last_event_kind"] == "tick")
        assert_true("final tracked", status["final_detected"] is True and status["final_bytes"] > 0)


@skipif(os.name == "nt", reason="native Windows review-job dispatch is refused in Phase 1")
def test_final_file_detected_before_process_exit() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        state_dir = tmp / "state"
        fake = tmp / "fake-codex"
        write_fake_codex(fake)
        env = os.environ.copy()
        env["GOALFLIGHT_STATE_DIR"] = str(state_dir)
        env["FAKE_REVIEW_MODE"] = "final_early"
        proc = subprocess.Popen(
            review_command(tmp, fake, "final-early", timeout_s=3, max_quiet_s=5),
            cwd=ROOT,
            env=env,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        status_path = tmp / "out" / "final-early.status.json"
        saw_final_running = False
        deadline = time.time() + 8
        while time.time() < deadline and proc.poll() is None:
            if status_path.exists():
                status = json.loads(status_path.read_text())
                if status.get("state") == "final_detected" and status.get("final_bytes", 0) > 0:
                    saw_final_running = True
                    break
            time.sleep(0.05)
        stdout, stderr = proc.communicate(timeout=5)
        assert_true(f"runner output clean: stdout={stdout} stderr={stderr}", proc.returncode == 0)
        final_status = json.loads(status_path.read_text())
        assert_true("final detected while worker still running", saw_final_running)
        assert_true("terminal complete after process exit", final_status["state"] == "complete")


@skipif(os.name == "nt", reason="POSIX process-group kill test")
def test_no_progress_timeout_kills_process_group() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        state_dir = tmp / "state"
        fake = tmp / "fake-codex"
        child_pid_file = tmp / "child.pid"
        child_term_file = tmp / "child.term"
        write_fake_codex(fake)
        proc = run(
            review_command(tmp, fake, "stall", timeout_s=1, max_quiet_s=0.5),
            state_dir=state_dir,
            env={
                "FAKE_REVIEW_MODE": "stall_with_child",
                "FAKE_CHILD_PID_FILE": str(child_pid_file),
                "FAKE_CHILD_TERM_FILE": str(child_term_file),
            },
            check=False,
        )
        status = json.loads((tmp / "out" / "stall.status.json").read_text())
        assert_true("stall exits nonzero", proc.returncode != 0)
        assert_true("stall classified inconclusive timeout", status["state"] == "inconclusive_timeout")
        assert_true("stall reason recorded", status["timeout_reason"] == "no_progress_timeout")
        assert_true("child process group received terminate", child_term_file.exists())


@skipif(os.name == "nt", reason="native Windows review-job dispatch is refused in Phase 1")
def test_jsonl_partial_and_malformed_lines_are_tolerated() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        state_dir = tmp / "state"
        fake = tmp / "fake-codex"
        write_fake_codex(fake)
        proc = run(
            review_command(tmp, fake, "partial", timeout_s=2, max_quiet_s=5),
            state_dir=state_dir,
            env={"FAKE_REVIEW_MODE": "partial_malformed"},
        )
        status = json.loads((tmp / "out" / "partial.status.json").read_text())
        assert_true("partial review complete", proc.returncode == 0 and status["state"] == "complete")
        assert_true("valid events counted after partial line", status["events_seen"] >= 2)
        assert_true("malformed line recorded", status["stdout_json_parse_errors"] == 1)
        assert_true("nested event kind detected", status["last_event_kind"] == "msg.done")


@skipif(os.name == "nt", reason="native Windows review-job dispatch is refused in Phase 1")
def test_prompt_write_cannot_block_monitor_before_timeout() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        state_dir = tmp / "state"
        out_dir = tmp / "out"
        out_dir.mkdir()
        prompt = tmp / "large.prompt.md"
        prompt.write_text("x" * (2 * 1024 * 1024))
        proc = run(
            [
                sys.executable,
                "scripts/goalflight_review_job.py",
                "--agent",
                "custom",
                "--name",
                "stdin-block",
                "--repo",
                str(ROOT),
                "--prompt",
                str(prompt),
                "--output-dir",
                str(out_dir),
                "--timeout-s",
                "1",
                "--max-quiet-s",
                "0.5",
                "--heartbeat-interval",
                "0.1",
                "--json",
                "--command",
                sys.executable,
                "-c",
                "import time; time.sleep(60)",
            ],
            state_dir=state_dir,
            check=False,
            timeout=8,
        )
        status = json.loads((out_dir / "stdin-block.status.json").read_text())
        assert_true("stdin-block exits nonzero", proc.returncode != 0)
        assert_true("stdin-block classified timeout", status["state"] == "inconclusive_timeout")
        assert_true("stdin-block reason recorded", status["timeout_reason"] == "no_progress_timeout")


@skipif(os.name == "nt" or not ps_pgid_available(), reason="POSIX process-group kill test requires ps pgid listing")
def test_parent_exit_with_live_child_is_inconclusive_and_reaped() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        state_dir = tmp / "state"
        fake = tmp / "fake-codex"
        child_pid_file = tmp / "child.pid"
        child_term_file = tmp / "child.term"
        write_fake_codex(fake)
        proc = run(
            review_command(tmp, fake, "leak-child", timeout_s=2, max_quiet_s=5),
            state_dir=state_dir,
            env={
                "FAKE_REVIEW_MODE": "leak_child",
                "FAKE_CHILD_PID_FILE": str(child_pid_file),
                "FAKE_CHILD_TERM_FILE": str(child_term_file),
            },
            check=False,
        )
        status = json.loads((tmp / "out" / "leak-child.status.json").read_text())
        assert_true("leak-child exits nonzero", proc.returncode != 0)
        assert_true("leak-child classified inconclusive", status["state"] == "inconclusive_timeout")
        assert_true("leak-child reason recorded", status["timeout_reason"] == "process_group_alive_after_parent_exit")
        assert_true("leak-child process group drained before release", status["process_group_drained"] is True)
        assert_true("leaked child process group was terminated", child_term_file.exists())
        assert_true("leaked child pid was recorded", child_pid_file.exists())
        assert_true("leaked child pid is gone", not process_exists(int(child_pid_file.read_text())))
        assert_true("worker process group is gone", not pgroup_has_processes(int(status["pgid"])))


def test_auth_classifier_ignores_negative_probe_metadata() -> None:
    state = goalflight_review_job.classify("error: server failed needs_auth=false", "", 1, False, None)
    assert_true("negative auth metadata is not auth block", state == "failed")
    state = goalflight_review_job.classify("authentication failed: please log in", "", 1, False, None)
    assert_true("explicit auth failure still blocked", state == "blocked_auth")


@skipif(os.name == "nt", reason="native Windows review-job dispatch is refused in Phase 1")
def test_failed_stderr_excerpt_reaches_pressure_scanner() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        state_dir = tmp / "state"
        fake = tmp / "fake-codex"
        write_fake_codex(fake)
        proc = run(
            review_command(tmp, fake, "stderr-rate-limit", timeout_s=2, max_quiet_s=5),
            state_dir=state_dir,
            env={"FAKE_REVIEW_MODE": "stderr_rate_limit_fail"},
            check=False,
        )
        status = json.loads((tmp / "out" / "stderr-rate-limit.status.json").read_text())
        record = json.loads((state_dir / "runs.d" / f"{status['dispatch_id']}.json").read_text())
        scanned = json.dumps(record.get("error")) + json.dumps(status.get("error"))
        assert_true("stderr failure exits nonzero", proc.returncode != 0 and status["state"] == "failed")
        assert_true("status error carries bounded stderr", "Check your settings to continue" in scanned)
        assert_true(
            "stderr-only provider block reaches scanner",
            goalflight_rate_pressure.detect_pressure_scope(record, status)
            == goalflight_rate_pressure.ACCOUNT_RATE_LIMIT_SCOPE,
        )


@skipif(os.name == "nt", reason="native Windows review-job dispatch is refused in Phase 1")
def test_complete_review_does_not_copy_stderr_to_error_fields() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        state_dir = tmp / "state"
        fake = tmp / "fake-codex"
        write_fake_codex(fake)
        proc = run(
            review_command(tmp, fake, "complete-stderr", timeout_s=2, max_quiet_s=5),
            state_dir=state_dir,
            env={"FAKE_REVIEW_MODE": "complete_with_stderr"},
        )
        status = json.loads((tmp / "out" / "complete-stderr.status.json").read_text())
        record = json.loads((state_dir / "runs.d" / f"{status['dispatch_id']}.json").read_text())
        assert_true("complete stderr review succeeds", proc.returncode == 0 and status["state"] == "complete")
        assert_true("complete status has no error", "error" not in status)
        assert_true("complete ledger has no error", "error" not in record)
        assert_true("complete ledger has no stderr excerpt", "stderr_excerpt" not in json.dumps(record))


def main() -> None:
    tests = [
        test_review_job_capacity_wait_queues_until_slot_frees,
        test_review_job_capacity_wait_deadline_blocks,
        test_review_job_capacity_wait_zero_single_shot,
        test_review_job_capacity_wait_sigterm_terminalizes,
        test_active_worker_can_complete_after_soft_timeout,
        test_final_file_detected_before_process_exit,
        test_no_progress_timeout_kills_process_group,
        test_jsonl_partial_and_malformed_lines_are_tolerated,
        test_prompt_write_cannot_block_monitor_before_timeout,
        test_parent_exit_with_live_child_is_inconclusive_and_reaped,
        test_auth_classifier_ignores_negative_probe_metadata,
        test_failed_stderr_excerpt_reaches_pressure_scanner,
        test_complete_review_does_not_copy_stderr_to_error_fields,
    ]
    for test in tests:
        test()
    print(f"ok - {len(tests)} review-job tests")


if __name__ == "__main__":
    main()
