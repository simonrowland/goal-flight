#!/usr/bin/env python3
"""Hot-loop terseness: one-line launch hint, one-line argparse failures."""

from __future__ import annotations

from support import skip_posix_on_native_windows

skip_posix_on_native_windows("dispatch terseness launch test uses POSIX isolation")

import json
import contextlib
import io
import os
import signal
import subprocess
import sys
import tempfile
from pathlib import Path
import pytest

ROOT = Path(__file__).resolve().parents[2]
DISPATCH = ROOT / "scripts" / "goalflight_dispatch.py"
sys.path.insert(0, str(ROOT / "scripts"))

import goalflight_dispatch as D  # noqa: E402

# Fields observed on a real isolated launch at HEAD 9896bf6 before this change.
# DISPATCH-START / DISPATCH-LAUNCHED are a parsed contract; do not drop keys.
_START_REQUIRED = ("dispatch_id", "agent", "worker_pid", "tail", "status_json")
_LAUNCHED_REQUIRED = _START_REQUIRED + (
    "lease_id",
    "state",
    "watcher_pid",
    "watcher_log",
    "worker_identity",
)


def _run_dispatch_cli(*argv: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(DISPATCH), *argv],
        cwd=str(ROOT),
        env=env or os.environ.copy(),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=15,
    )


def _isolate_env(tmp: Path) -> dict[str, str]:
    env = os.environ.copy()
    env["GOALFLIGHT_STATE_DIR"] = str(tmp / "state")
    env["GOALFLIGHT_TASK_STORE_DIR"] = str(tmp / "task-store")
    env["GOALFLIGHT_JOURNAL_DIR"] = str(tmp / "journal")
    env["GOALFLIGHT_MESSAGES_DIR"] = str(tmp / "messages")
    env["GOALFLIGHT_WAKE_LEDGER_DIR"] = str(tmp / "wake-ledger")
    env["GOAL_FLIGHT_PIDFILE_DIR"] = str(tmp / "pids")
    env["GOALFLIGHT_CAPACITY_CONF"] = "/dev/null"
    env["GOALFLIGHT_CAPACITY_WAIT_S"] = "0"
    env["GOALFLIGHT_CONTROLLER_SESSION_ID"] = f"terse-{tmp.name}"
    env.pop("GOALFLIGHT_CONTROLLER_LABEL", None)
    return env


def _kill(pid: int | None) -> None:
    if not pid:
        return
    try:
        os.kill(int(pid), signal.SIGKILL)
    except ProcessLookupError:
        return


def _json_line(stdout: str, prefix: str) -> dict:
    for line in stdout.splitlines():
        if line.startswith(prefix):
            return json.loads(line.split(" ", 1)[1])
    raise AssertionError(f"missing {prefix!r} in stdout:\n{stdout}")


def test_unrecognized_flag_is_one_line() -> None:
    proc = _run_dispatch_cli("--no-such-flag")
    assert proc.returncode == 2, proc.stderr
    err = proc.stderr.strip()
    assert "\n" not in err, err
    assert "usage:" not in err.lower(), err
    assert "--no-such-flag" in err
    assert "--help" in err
    assert "--agent" in err
    assert len(err.encode()) < 250, len(err.encode())


def test_invalid_os_sandbox_is_one_line() -> None:
    proc = _run_dispatch_cli("--os-sandbox", "bogus")
    assert proc.returncode == 2, proc.stderr
    err = proc.stderr.strip()
    assert "\n" not in err, err
    assert "usage:" not in err.lower(), err
    assert "bogus" in err
    assert "read-only" in err
    assert len(err.encode()) < 300, len(err.encode())


def test_steer_missing_id_is_one_line() -> None:
    proc = _run_dispatch_cli("steer")
    assert proc.returncode == 2, proc.stderr
    err = proc.stderr.strip()
    assert "\n" not in err, err
    assert "usage:" not in err.lower(), err
    assert "dispatch_id" in err or "dispatch-id" in err
    assert "--list" in err


def test_help_still_prints_full_map() -> None:
    proc = _run_dispatch_cli("--help")
    assert proc.returncode == 0, proc.stderr
    assert "--hints" in proc.stdout
    assert "--agent" in proc.stdout
    assert len(proc.stdout.encode()) > 4000


def test_readonly_alias_is_accepted() -> None:
    with tempfile.TemporaryDirectory(prefix="gf-terse-ro-") as td:
        tmp = Path(td)
        proc = _run_dispatch_cli("--readonly", "--cwd", str(tmp), env=_isolate_env(tmp))
    assert proc.returncode == 64, proc.stderr
    assert "unrecognized arguments" not in proc.stderr
    assert "no worker" in proc.stderr


def test_os_sandbox_readonly_alias_is_accepted() -> None:
    assert D._parse_os_sandbox_arg("readonly") == "read-only"
    assert D._parse_os_sandbox_arg("read_only") == "read-only"
    assert D._parse_os_sandbox_arg("workspace_write") == "workspace-write"
    assert D._parse_os_sandbox_arg("off") == "off"
    with tempfile.TemporaryDirectory(prefix="gf-terse-os-") as td:
        tmp = Path(td)
        proc = _run_dispatch_cli(
            "--os-sandbox", "readonly", "--cwd", str(tmp), env=_isolate_env(tmp)
        )
    assert proc.returncode == 64, proc.stderr
    assert "invalid --os-sandbox" not in proc.stderr
    assert "unrecognized arguments" not in proc.stderr
    assert "no worker" in proc.stderr


def test_refused_launch_ends_with_not_launched_marker() -> None:
    with tempfile.TemporaryDirectory(prefix="gf-not-launched-") as td:
        tmp = Path(td)
        proc = _run_dispatch_cli("--agent", "codex", "--not-a-launch-option", env=_isolate_env(tmp))
    assert proc.returncode != 0
    assert proc.stdout.splitlines()[-1].startswith("NOT LAUNCHED: "), proc.stdout
    assert "DISPATCH-LAUNCHED" not in proc.stdout
    assert "DISPATCH-START" not in proc.stdout


def test_refused_resume_ends_with_not_launched_marker() -> None:
    with tempfile.TemporaryDirectory(prefix="gf-resume-not-launched-") as td:
        tmp = Path(td)
        proc = _run_dispatch_cli(
            "resume",
            "missing-dispatch",
            "--prompt-file",
            str(tmp / "missing-prompt.md"),
            env=_isolate_env(tmp),
        )
    assert proc.returncode != 0
    assert proc.stdout.splitlines()[-1].startswith("NOT LAUNCHED: "), proc.stdout
    assert "DISPATCH-LAUNCHED" not in proc.stdout
    assert "DISPATCH-START" not in proc.stdout


@pytest.mark.parametrize("shape", ["bash", "acp-detached", "acp-foreground"])
def test_post_spawn_failure_ends_with_unknown_launch_state(shape, monkeypatch, tmp_path) -> None:
    did = f"post-spawn-{shape}"

    def fail_after_spawn(_argv, *, resume_plan=None):
        # Each launch path reaches its spawn boundary before a later failure;
        # the marker must describe uncertainty instead of inviting a retry.
        D._mark_spawn_intent()
        return 1

    monkeypatch.setattr(D, "_main_impl", fail_after_spawn)
    stdout = io.StringIO()
    project_root = tmp_path / "dispatch-project"
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(io.StringIO()):
        result = D.main([
            "--dispatch-id", did,
            "--project-root", str(project_root),
        ])

    assert result != 0
    last_line = stdout.getvalue().splitlines()[-1]
    assert last_line.startswith(f"LAUNCH STATE UNKNOWN: {did}; check: "), last_line
    assert (
        f"goalflight_status.py --project {project_root} --dispatch {did}"
        in last_line
    )
    assert "NOT LAUNCHED:" not in stdout.getvalue()


def test_default_launch_hint_and_json_contract() -> None:
    with tempfile.TemporaryDirectory(prefix="gf-terse-") as td:
        tmp = Path(td)
        subprocess.run(["git", "init", "--quiet", str(tmp)], check=True)
        env = _isolate_env(tmp)
        did = "terse-contract"
        status = tmp / f"{did}.status.json"
        tail = tmp / f"{did}.tail"
        proc = subprocess.run(
            [
                sys.executable,
                str(DISPATCH),
                "--agent",
                "test-dispatch",
                "--unregistered-forced",
                "--dispatch-id",
                did,
                "--cwd",
                str(tmp),
                "--in-place",
                "--tail",
                str(tail),
                "--status-json",
                str(status),
                "--poll-secs",
                "0.2",
                "--max-idle-secs",
                "8",
                "--",
                sys.executable,
                "-c",
                "import time; print('hi', flush=True); time.sleep(2)",
            ],
            cwd=str(ROOT),
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=20,
        )
        start = launched = None
        try:
            assert proc.returncode == 0, proc.stderr
            start = _json_line(proc.stdout, "DISPATCH-START ")
            launched = _json_line(proc.stdout, "DISPATCH-LAUNCHED ")
            for key in _START_REQUIRED:
                assert key in start, (key, start)
            for key in _LAUNCHED_REQUIRED:
                assert key in launched, (key, launched)
            assert launched["dispatch_id"] == did
            assert launched["status_json"]
            assert launched["state"] == "running"

            reminder_lines = [
                line
                for line in proc.stderr.splitlines()
                if line.startswith("[goal-flight] dispatched ")
            ]
            assert len(reminder_lines) == 1, proc.stderr
            reminder = reminder_lines[0]
            assert did in reminder
            assert launched["status_json"] in reminder or str(status.resolve()) in reminder
            assert "do NOT hand-roll" not in proc.stderr
            assert "watch-dispatch-tail.sh" not in proc.stderr
            assert "BACKGROUND" not in proc.stderr
            assert len(reminder.encode()) < 400, reminder
        finally:
            for payload in (start, launched):
                if not payload:
                    continue
                _kill(payload.get("worker_pid"))
                _kill(payload.get("watcher_pid"))
                _kill(payload.get("caffeinate_pid"))


def test_hints_flag_restores_teaching_block() -> None:
    with tempfile.TemporaryDirectory(prefix="gf-terse-hints-") as td:
        tmp = Path(td)
        subprocess.run(["git", "init", "--quiet", str(tmp)], check=True)
        env = _isolate_env(tmp)
        did = "terse-hints"
        proc = subprocess.run(
            [
                sys.executable,
                str(DISPATCH),
                "--hints",
                "--agent",
                "test-dispatch",
                "--unregistered-forced",
                "--dispatch-id",
                did,
                "--cwd",
                str(tmp),
                "--in-place",
                "--tail",
                str(tmp / f"{did}.tail"),
                "--status-json",
                str(tmp / f"{did}.status.json"),
                "--poll-secs",
                "0.2",
                "--max-idle-secs",
                "8",
                "--",
                sys.executable,
                "-c",
                "import time; print('hi', flush=True); time.sleep(1)",
            ],
            cwd=str(ROOT),
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=20,
        )
        launched = None
        try:
            assert proc.returncode == 0, proc.stderr
            launched = _json_line(proc.stdout, "DISPATCH-LAUNCHED ")
            assert "do NOT hand-roll" in proc.stderr
            assert "--wait" in proc.stderr
            assert "BACKGROUND" in proc.stderr
        finally:
            if launched:
                _kill(launched.get("worker_pid"))
                _kill(launched.get("watcher_pid"))
                _kill(launched.get("caffeinate_pid"))


def test_dispatch_subcommands_are_discoverable() -> None:
    """Every positional subcommand must be nameable from `--help`.

    Subcommands are matched before argparse is built, so argparse never learns
    they exist and they get no help entry of their own. `resume` was therefore
    invisible: the word appeared ZERO times in `--help`, and controllers
    looking for a resume capability the usual way concluded there was none and
    redispatched instead, throwing away accumulated worker context. Measured
    2026-08-24 across several independent controllers.

    Pins the advertised list against the ACTUAL dispatch table by parsing
    `main`, so adding a subcommand without documenting it fails here.
    """
    import ast
    import inspect
    import subprocess
    import sys

    sys.path.insert(0, str(ROOT / "scripts"))
    import goalflight_dispatch as gd

    # Every `if argv[0] == "<name>"` guard at the top of main().
    dispatched: set[str] = set()
    for node in ast.walk(ast.parse(inspect.getsource(gd._main_impl))):
        if not isinstance(node, ast.Compare) or not node.comparators:
            continue
        left, right = node.left, node.comparators[0]
        subscripts_argv = (
            isinstance(left, ast.Subscript)
            and isinstance(left.value, ast.Name)
            and left.value.id == "argv"
        )
        if subscripts_argv and isinstance(right, ast.Constant) and isinstance(right.value, str):
            dispatched.add(right.value)
    # The daemon-spawn sentinel is internal plumbing, not an operator command.
    dispatched.discard(gd.DAEMON_SPAWN_ARG)

    advertised = {name for name, _ in gd._SUBCOMMAND_HELP}
    assert dispatched, "found no subcommand guards; the parse shape changed"
    missing = dispatched - advertised
    assert not missing, f"subcommands dispatched but not advertised in --help: {sorted(missing)}"

    help_text = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "goalflight_dispatch.py"), "--help"],
        capture_output=True, text=True, timeout=120,
    ).stdout
    for name in sorted(dispatched):
        assert name in help_text, f"{name!r} is dispatched but absent from --help output"


def test_routed_help_and_launch_classification_stay_consistent() -> None:
    """Pin positional routes, help entries, and launch-marker classification together."""
    import ast
    import inspect

    dispatch_tree = ast.parse(inspect.getsource(D._main_impl))
    routed_handlers: set[str] = set()
    internal_daemon_route = False
    for node in ast.walk(dispatch_tree):
        if not isinstance(node, ast.Compare) or not node.comparators:
            continue
        left, right = node.left, node.comparators[0]
        is_argv_zero = (
            isinstance(left, ast.Subscript)
            and isinstance(left.value, ast.Name)
            and left.value.id == "argv"
            and isinstance(left.slice, ast.Constant)
            and left.slice.value == 0
        )
        if not is_argv_zero:
            continue
        if isinstance(right, ast.Constant) and isinstance(right.value, str):
            routed_handlers.add(right.value)
        elif isinstance(right, ast.Name) and right.id == "DAEMON_SPAWN_ARG":
            internal_daemon_route = True

    routed = set(D._ROUTED_SUBCOMMANDS)
    help_entries = {name for name, _purpose in D._SUBCOMMAND_HELP}
    assert len(routed) == len(D._ROUTED_SUBCOMMANDS), "duplicate routed subcommand"
    assert len(help_entries) == len(D._SUBCOMMAND_HELP), "duplicate help entry"
    assert routed_handlers == routed, (
        "_ROUTED_SUBCOMMANDS must exactly match positional handlers; "
        f"missing={sorted(routed_handlers - routed)}, extra={sorted(routed - routed_handlers)}"
    )
    assert help_entries == routed, (
        "every routed positional command must have one help entry; "
        f"missing={sorted(routed - help_entries)}, extra={sorted(help_entries - routed)}"
    )

    # `is_launch` negates membership in this literal set. The internal daemon
    # sentinel is plumbing, not an operator verb, so it is absent from both
    # help and routing but present here. `--stats` is the actual stats entry
    # point; its bare-word guard is a marker-only option-path entry, not a verb.
    # `final` and `resume` remain the explicitly reviewed marker-only
    # exceptions: their omission from this set affects failure-marker output,
    # while both remain ordinary help/routing entries.
    main_tree = ast.parse(inspect.getsource(D.main))
    is_launch_assignment = next(
        node
        for node in ast.walk(main_tree)
        if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == "is_launch" for target in node.targets)
    )
    launch_guard = next(
        node for node in ast.walk(is_launch_assignment.value) if isinstance(node, ast.Set)
    )
    marker_guard_words = {
        D.DAEMON_SPAWN_ARG if isinstance(item, ast.Name) and item.id == "DAEMON_SPAWN_ARG"
        else item.value
        for item in launch_guard.elts
        if (isinstance(item, ast.Constant) and isinstance(item.value, str))
        or (isinstance(item, ast.Name) and item.id == "DAEMON_SPAWN_ARG")
    }
    final_resume_marker_exceptions = {"final", "resume"}
    option_marker_guards = {"stats"}
    assert final_resume_marker_exceptions <= routed_handlers, (
        "the marker-only exceptions must remain real positional commands"
    )
    assert D.DAEMON_SPAWN_ARG not in routed | help_entries, (
        "the internal daemon sentinel must not become an operator command"
    )
    assert option_marker_guards.isdisjoint(routed | help_entries), (
        "option marker guards must not silently become positional commands"
    )
    expected_marker_guard = (
        (routed - final_resume_marker_exceptions)
        | {D.DAEMON_SPAWN_ARG}
        | option_marker_guards
    )
    assert internal_daemon_route, "the internal daemon sentinel route disappeared"
    assert marker_guard_words == expected_marker_guard, (
        "launch-marker command classification diverged; "
        f"missing={sorted(expected_marker_guard - marker_guard_words)}, "
        f"extra={sorted(marker_guard_words - expected_marker_guard)}"
    )


def main() -> None:
    test_unrecognized_flag_is_one_line()
    test_invalid_os_sandbox_is_one_line()
    test_steer_missing_id_is_one_line()
    test_help_still_prints_full_map()
    test_dispatch_subcommands_are_discoverable()
    test_routed_help_and_launch_classification_stay_consistent()
    test_readonly_alias_is_accepted()
    test_os_sandbox_readonly_alias_is_accepted()
    test_default_launch_hint_and_json_contract()
    test_hints_flag_restores_teaching_block()
    print("OK: dispatch CLI terseness tests pass")


if __name__ == "__main__":
    main()
