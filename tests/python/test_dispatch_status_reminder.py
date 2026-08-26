#!/usr/bin/env python3
"""Unit tests for post-dispatch status-tooling reminder lines."""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import goalflight_dispatch  # noqa: E402


def _reminder_text(shape: str, *, hints: bool = False) -> tuple[str, Path, Path]:
    status_json = Path("/tmp/goal-flight-state/dispatch/reminder-dispatch-42.status.json")
    tail_path = Path("/tmp/goal-flight-state/dispatch/reminder-dispatch-42.tail")
    prompt_path = Path("/tmp/goal-flight-state/dispatch/reminder-dispatch-42.prompt.md")
    lines = goalflight_dispatch._status_reminder_lines(
        "reminder-dispatch-42",
        status_json=status_json,
        tail_path=tail_path,
        worker_pid=4242,
        shape=shape,
        skill_root=ROOT,
        agent="codex",
        controller_pid=9999,
        poll_secs=2.0,
        max_idle_secs=180.0,
        prompt_path=prompt_path,
        hints=hints,
    )
    return "\n".join(lines), status_json.resolve(), tail_path.resolve()


def test_default_reminder_is_one_line_with_id_and_status() -> None:
    for shape in ("bash", "acp"):
        text, status_json, _tail = _reminder_text(shape, hints=False)
        lines = [line for line in text.splitlines() if line]
        assert len(lines) == 1, f"{shape}: default reminder must be one line, got {lines!r}"
        assert "reminder-dispatch-42" in text, shape
        assert str(status_json) in text, shape
        assert "do NOT hand-roll" not in text, shape
        assert "--wait" not in text, shape
        assert "goalflight_watch.py" not in text, shape
        assert "watch-dispatch-tail.sh" not in text, shape
        assert "goalflight_messages.py" not in text, shape
        assert "BACKGROUND" not in text, shape


def test_status_reminder_bash_shape() -> None:
    text, _status_json, tail = _reminder_text("bash", hints=True)
    assert "reminder-dispatch-42" in text
    assert "--dispatch reminder-dispatch-42" in text
    assert "--wait reminder-dispatch-42" in text
    assert "--done reminder-dispatch-42" in text
    assert "goalflight_messages.py relay" in text
    assert "current project, open + unread" in text
    assert "0=terminal" in text and "1=running" in text and "2=ambiguous" in text
    assert "do NOT hand-roll" in text
    assert "watch-dispatch-tail.sh" in text
    assert "--pid 4242" in text
    assert f"--tail {tail}" in text
    assert "--controller-pid 9999" in text
    assert "--agent codex-bash-tail" in text
    assert "--session-id reminder-dispatch-42" in text
    assert "--poll-secs 2.0" in text
    assert "--max-idle-secs 180.0" in text
    prompt = Path("/tmp/goal-flight-state/dispatch/reminder-dispatch-42.prompt.md").resolve()
    assert f"--ignore-prompt-file {prompt}" in text
    assert "goalflight_watch.py" not in text


def test_status_reminder_acp_shape() -> None:
    text, status_json, tail = _reminder_text("acp", hints=True)
    assert "reminder-dispatch-42" in text
    assert "--dispatch reminder-dispatch-42" in text
    assert "--wait reminder-dispatch-42" in text
    assert "--done reminder-dispatch-42" in text
    assert "goalflight_messages.py relay" in text
    assert "current project, open + unread" in text
    assert "0=terminal" in text and "1=running" in text and "2=ambiguous" in text
    assert "do NOT hand-roll" in text
    assert "goalflight_watch.py" in text
    assert "--pid 4242" in text
    assert f"--tail {tail}" in text
    assert f"--status-json {status_json}" in text
    prompt = Path("/tmp/goal-flight-state/dispatch/reminder-dispatch-42.prompt.md").resolve()
    assert f"--ignore-prompt-file {prompt}" in text
    assert "watch-dispatch-tail.sh" not in text


def test_wait_hint_teaches_the_backgrounded_form() -> None:
    """--hints must show a wait a controller can actually run.

    Printed as a bare foreground command it collided with the rule against long
    foreground calls, leaving no legal move: the same hint forbids hand-rolled
    watchers, so controllers hand-rolled pollers anyway. One was seen polling a
    harness task file, which cannot observe `awaiting_user_confirm` at all -- a
    worker paused for approval looked exactly like a worker still working.

    The teaching block is opt-in (--hints) so the hot loop stays one line.
    Pinned because stripping this advice left every existing assertion green:
    the hint's *commands* were tested, the guidance that makes them usable was
    not.
    """
    for shape in ("bash", "acp"):
        text, _status_json, _tail = _reminder_text(shape, hints=True)
        assert "--wait reminder-dispatch-42" in text, shape
        assert "BACKGROUND" in text, f"{shape}: hint must say to background the wait"
        assert "timer" in text, f"{shape}: hint must warn against substituting a timer"
        # The reason has to travel with the instruction, or it reads as arbitrary.
        assert "clock" in text, f"{shape}: hint must say why a timer is wrong"


def _assert_generated_bash_watch_command(
    *,
    extra_args: list[str],
    expected_max_idle: float,
) -> None:
    """Execute the exact production-generated argv, including float spellings."""
    parser = goalflight_dispatch._build_launch_parser()
    args = parser.parse_args(
        [
            "--agent",
            "codex",
            "--prompt",
            "generated watcher",
            "--read-only",
            *extra_args,
        ]
    )
    goalflight_dispatch._apply_max_idle_default(args)
    assert args.poll_secs == 2.0, args
    assert args.max_idle_secs == expected_max_idle, args

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        tail = tmp / "generated.tail"
        tail.write_text(
            "COMPLETE: generated-decimal-argv — done\n",
            encoding="utf-8",
        )
        prompt = tmp / "generated.prompt.md"
        prompt.write_text("fixture\n", encoding="utf-8")
        worker = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            start_new_session=True,
        )
        watcher = None
        try:
            lines = goalflight_dispatch._status_reminder_lines(
                "generated-decimal-argv",
                status_json=tmp / "generated.status.json",
                tail_path=tail,
                worker_pid=worker.pid,
                shape="bash",
                skill_root=ROOT,
                agent="codex",
                controller_pid=os.getpid(),
                poll_secs=args.poll_secs,
                max_idle_secs=args.max_idle_secs,
                prompt_path=prompt,
                hints=True,
            )
            generated = next(line for line in lines if line.startswith("  watch:  "))
            command = shlex.split(generated.removeprefix("  watch:  "))
            env = os.environ.copy()
            env["GOAL_FLIGHT_PIDFILE_DIR"] = str(tmp / "pidfiles")
            watcher = subprocess.Popen(
                command,
                cwd=ROOT,
                env=env,
                text=True,
                encoding="utf-8",
                errors="replace",
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            pidfile_dir = tmp / "pidfiles"
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and not list(pidfile_dir.glob("*.jsonl")):
                time.sleep(0.02)
            assert list(pidfile_dir.glob("*.jsonl")), "generated watcher did not start"
            # Reap the fixture so kill(0) does not keep seeing a zombie as live.
            worker.kill()
            worker.wait(timeout=5)
            stdout, stderr = watcher.communicate(timeout=10)
            result = subprocess.CompletedProcess(
                command,
                watcher.returncode,
                stdout,
                stderr,
            )
        finally:
            if watcher is not None and watcher.poll() is None:
                watcher.kill()
                watcher.communicate(timeout=5)
            if worker.poll() is None:
                worker.kill()
            worker.wait(timeout=5)

    assert result.returncode == 0, (command, result.stdout, result.stderr)
    assert "invalid arithmetic operator" not in result.stderr
    assert "WATCHER-EXIT: marker exit_code=0" in result.stdout


def test_generated_bash_watch_command_accepts_decimal_defaults() -> None:
    _assert_generated_bash_watch_command(
        extra_args=[],
        expected_max_idle=900.0,
    )


def test_generated_bash_watch_command_accepts_disabled_idle_gate() -> None:
    _assert_generated_bash_watch_command(
        extra_args=["--max-idle-secs", "0"],
        expected_max_idle=0.0,
    )


def test_generated_bash_watch_command_rejects_invalid_idle_values() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        tail = tmp / "invalid.tail"
        tail.write_text("fixture\n", encoding="utf-8")
        for value in ("-1", "nan"):
            result = subprocess.run(
                [
                    "bash",
                    str(ROOT / "scripts" / "watch-dispatch-tail.sh"),
                    "--pid",
                    str(os.getpid()),
                    "--tail",
                    str(tail),
                    "--poll-secs",
                    "2.0",
                    "--max-idle-secs",
                    value,
                ],
                cwd=ROOT,
                text=True,
                encoding="utf-8",
                errors="replace",
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=10,
            )
            assert result.returncode == 64, (value, result)
            assert "finite and nonnegative" in result.stderr, (value, result)


def main() -> None:
    test_default_reminder_is_one_line_with_id_and_status()
    test_status_reminder_bash_shape()
    test_status_reminder_acp_shape()
    test_wait_hint_teaches_the_backgrounded_form()
    test_generated_bash_watch_command_accepts_decimal_defaults()
    test_generated_bash_watch_command_accepts_disabled_idle_gate()
    test_generated_bash_watch_command_rejects_invalid_idle_values()
    print("OK: 7 dispatch status reminder tests pass")


if __name__ == "__main__":
    main()
