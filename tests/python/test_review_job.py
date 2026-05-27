#!/usr/bin/env python3
"""Focused tests for file-backed review-job liveness monitoring."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import time

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import goalflight_review_job


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


def pgroup_has_processes(pgid: int) -> bool:
    output = subprocess.check_output(["ps", "-A", "-o", "pgid="], text=True)
    return any(line.strip() == str(pgid) for line in output.splitlines())


def process_exists(pid: int) -> bool:
    result = subprocess.run(["ps", "-p", str(pid), "-o", "pid="], text=True, capture_output=True, check=False)
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
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        status_path = tmp / "out" / "final-early.status.json"
        saw_final_running = False
        deadline = time.time() + 4
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


def main() -> None:
    tests = [
        test_active_worker_can_complete_after_soft_timeout,
        test_final_file_detected_before_process_exit,
        test_no_progress_timeout_kills_process_group,
        test_jsonl_partial_and_malformed_lines_are_tolerated,
        test_prompt_write_cannot_block_monitor_before_timeout,
        test_parent_exit_with_live_child_is_inconclusive_and_reaped,
        test_auth_classifier_ignores_negative_probe_metadata,
    ]
    for test in tests:
        test()
    print(f"ok - {len(tests)} review-job tests")


if __name__ == "__main__":
    main()
