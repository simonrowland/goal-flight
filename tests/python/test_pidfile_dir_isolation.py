#!/usr/bin/env python3
"""The test runners isolate every machine-global writable directory.

A test that launches a watcher or ACP client without this override writes
into /tmp/goal-flight-acp-pids.d, the machine-global directory every project
on the box shares. Python autouse isolation covers pytest, but the gate used
to pin GOALFLIGHT_MESSAGES_DIR / GOALFLIGHT_JOURNAL_DIR and not the pidfile
dir. Bash tests and any child that imports acp_client then inherited the
production default.

The pin is the snapshot: a write under the gate-style env must land in the
isolated root and must not create a uniquely-named probe file in the live
directory. An explicit outer value still passes through.
"""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import shlex
import subprocess
import sys
import time

from support import AMBIENT_IDENTITY_ENV, AMBIENT_WEBHOOK_ENV, MACHINE_PATH_ENV

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
RUN_SH = ROOT / "tests" / "run.sh"
LIVE_PIDFILE_DIR = Path("/tmp/goal-flight-acp-pids.d")
PIDFILE_OVERRIDE_KEYS = ("GOAL_FLIGHT_PIDFILE_DIR", "GOALFLIGHT_PIDFILE_DIR")


def _run_sh_isolation_snippet(text: str) -> str:
    """Preamble + run_isolated_test_env from the real runner, not a copy."""
    start = text.index("_GF_TEST_ENV_BASE=")
    preamble_end = text.index("\npass=0\n", start)
    fn = text.index("run_isolated_test_env() {", preamble_end)
    close = text.index("\n}\n", fn)
    return text[start:preamble_end] + "\n" + text[fn : close + 3]


def _load_affected_tests_module():
    path = ROOT / "scripts" / "goalflight_affected_tests.py"
    spec = importlib.util.spec_from_file_location("goalflight_affected_tests_probe", path)
    assert spec is not None and spec.loader is not None
    affected = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(affected)
    return affected


def _run_isolated_probe(
    argv: list[str],
    *,
    extra_env: dict[str, str] | None = None,
    unset: tuple[str, ...] = (),
) -> subprocess.CompletedProcess[str]:
    snippet = _run_sh_isolation_snippet(RUN_SH.read_text(encoding="utf-8"))
    env = os.environ.copy()
    for key in unset:
        env.pop(key, None)
    if extra_env:
        env.update(extra_env)
    quoted = " ".join(shlex.quote(part) for part in argv)
    script = snippet + "\nrun_isolated_test_env " + quoted + "\n"
    return subprocess.run(
        ["bash", "-c", script],
        cwd=str(ROOT),
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )


def test_run_sh_pins_pidfile_dir_in_isolated_env() -> None:
    text = RUN_SH.read_text(encoding="utf-8")
    snippet = _run_sh_isolation_snippet(text)
    missing = [
        fragment
        for fragment in (
            '_GF_PIDFILE_BASE="${GOAL_FLIGHT_PIDFILE_DIR:-${GOALFLIGHT_PIDFILE_DIR:-$_GF_TEST_ENV_BASE/pids}}"',
            'GOAL_FLIGHT_PIDFILE_DIR="$_GF_PIDFILE_BASE"',
            'GOALFLIGHT_PIDFILE_DIR="$_GF_PIDFILE_BASE"',
            'XDG_STATE_HOME="$_GF_XDG_BASE"',
        )
        if fragment not in snippet
    ]
    assert not missing, (
        "tests/run.sh run_isolated_test_env lost pidfile isolation; "
        "the suite would write /tmp/goal-flight-acp-pids.d. missing: "
        + ", ".join(missing)
    )


def test_run_sh_pins_all_machine_state_in_isolated_env() -> None:
    snippet = _run_sh_isolation_snippet(RUN_SH.read_text(encoding="utf-8"))
    missing = [
        fragment
        for fragment in (
            'GOALFLIGHT_STATE_DIR="$_GF_STATE_BASE"',
            'GOALFLIGHT_CODEX_STATE_DIR="$_GF_CODEX_STATE_BASE"',
            "-u GOALFLIGHT_DISPATCH_DIR",
            'GOALFLIGHT_WAKE_LEDGER_DIR="$_GF_WAKE_LEDGER_BASE"',
            'GOALFLIGHT_FLEET_DIR="$_GF_FLEET_BASE"',
            "-u GOALFLIGHT_WAKE_LEDGER",
        )
        if fragment not in snippet
    ]
    assert not missing, (
        "tests/run.sh run_isolated_test_env lost machine-state isolation; "
        "missing: " + ", ".join(missing)
    )
    missing_unsets = [
        key
        for key in AMBIENT_IDENTITY_ENV + AMBIENT_WEBHOOK_ENV
        if key != "GOALFLIGHT_WAKE_WEBHOOK_CONFIG" and f"-u {key}" not in snippet
    ]
    assert not missing_unsets, (
        "tests/run.sh run_isolated_test_env leaked ambient runtime identity; "
        "missing unsets: " + ", ".join(missing_unsets)
    )


def test_affected_runner_uses_a_private_complete_machine_root(
    monkeypatch, tmp_path: Path
) -> None:
    for key in MACHINE_PATH_ENV:
        monkeypatch.setenv(key, str(tmp_path / "ambient" / key.lower()))
    for key in AMBIENT_IDENTITY_ENV + AMBIENT_WEBHOOK_ENV:
        monkeypatch.setenv(key, f"ambient-{key.lower()}")

    affected = _load_affected_tests_module()
    base = tmp_path / "module"
    env = affected.isolated_env(base)
    expected = {
        "GOALFLIGHT_MESSAGES_DIR": base / "messages",
        "GOALFLIGHT_FLEET_DIR": base / "fleet",
        "GOALFLIGHT_JOURNAL_DIR": base / "journals",
        "GOALFLIGHT_TASK_STORE_DIR": base / "task-store",
        "GOALFLIGHT_STATE_DIR": base / "state",
        "GOALFLIGHT_CODEX_STATE_DIR": base / "codex-state",
        "GOALFLIGHT_WAKE_LEDGER_DIR": base / "wake-ledger",
        "GOAL_FLIGHT_PIDFILE_DIR": base / "pids",
        "GOALFLIGHT_PIDFILE_DIR": base / "pids",
        "XDG_STATE_HOME": base / "xdg",
    }
    assert {key: Path(env[key]) for key in expected} == expected
    assert "GOALFLIGHT_DISPATCH_DIR" not in env
    assert env["GOALFLIGHT_CAPACITY_CONF"] == os.devnull
    assert env["GOALFLIGHT_WAKE_WEBHOOK_CONFIG"] == os.devnull
    assert env["PYTEST_CURRENT_TEST"] == "goalflight_affected_tests.py (call)"
    assert "GOALFLIGHT_WAKE_LEDGER" not in env
    assert all(key not in env for key in AMBIENT_IDENTITY_ENV)
    assert all(
        key not in env
        for key in AMBIENT_WEBHOOK_ENV
        if key != "GOALFLIGHT_WAKE_WEBHOOK_CONFIG"
    )


def test_affected_runner_direct_scripts_do_not_union_live_codex_state(
    monkeypatch, tmp_path: Path
) -> None:
    home = tmp_path / "operator-home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    affected = _load_affected_tests_module()
    base = tmp_path / "module"
    env = affected.isolated_env(base)
    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys\n"
            "sys.path.insert(0, sys.argv[1])\n"
            "import goalflight_dispatch as dispatch\n"
            "print('\\n'.join(str(path) for path in dispatch._quota_state_roots()))\n",
            str(SCRIPTS),
        ],
        cwd=str(ROOT),
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    roots = {Path(line) for line in proc.stdout.splitlines() if line.strip()}
    assert roots == {base / "codex-state", base / "state"}, roots
    assert home / ".goal-flight" not in roots


def test_unscoped_acp_client_default_is_live_pidfile_dir() -> None:
    """Production default is unchanged: no override => live shared dir."""
    env = os.environ.copy()
    for key in PIDFILE_OVERRIDE_KEYS:
        env.pop(key, None)
    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys\n"
            "sys.path.insert(0, sys.argv[1])\n"
            "import goalflight_acp_client as client\n"
            "print(client._PIDFILE_DIR)\n",
            str(SCRIPTS),
        ],
        cwd=str(ROOT),
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    assert Path(proc.stdout.strip()) == LIVE_PIDFILE_DIR, proc.stdout


def test_gate_isolation_does_not_write_live_pidfile_dir() -> None:
    """A child that only gets run_isolated_test_env must miss the live dir.

    Probe name is unique so this does not assert on the live listing: other
    projects write that directory concurrently.
    """
    probe = f"suite-certifies-{os.getpid()}-{time.time_ns()}.jsonl"
    live_probe = LIVE_PIDFILE_DIR / probe
    assert not live_probe.exists(), f"pre-existing live probe {live_probe}"

    proc = _run_isolated_probe(
        [
            sys.executable,
            "-c",
            "from pathlib import Path\n"
            "import os\n"
            "target = Path(os.environ['GOAL_FLIGHT_PIDFILE_DIR'])\n"
            "alias = Path(os.environ['GOALFLIGHT_PIDFILE_DIR'])\n"
            "assert target == alias, (target, alias)\n"
            "target.mkdir(parents=True, exist_ok=True)\n"
            f"probe_path = target / {probe!r}\n"
            "probe_path.write_text('isolated\\n', encoding='utf-8')\n"
            f"live = Path({str(LIVE_PIDFILE_DIR)!r}) / {probe!r}\n"
            "print(target)\n"
            "print('content', probe_path.read_text(encoding='utf-8').rstrip())\n"
            "print('isolated_exists', probe_path.is_file())\n"
            "print('live_exists', live.exists())\n"
            "print('same_dir', target.resolve() == live.parent.resolve())\n",
        ],
        unset=PIDFILE_OVERRIDE_KEYS,
    )
    assert proc.returncode == 0, proc.stderr or proc.stdout
    lines = [line for line in proc.stdout.splitlines() if line.strip()]
    isolated = Path(lines[0])
    report = dict(line.split(" ", 1) for line in lines[1:])
    assert report.get("content") == "isolated", proc.stdout
    assert report.get("isolated_exists") == "True", proc.stdout
    assert report.get("live_exists") == "False", proc.stdout
    assert report.get("same_dir") == "False", proc.stdout
    assert isolated.resolve() != LIVE_PIDFILE_DIR.resolve(), isolated
    assert not live_probe.exists(), (
        f"gate isolation leaked pidfile into live dir: {live_probe}"
    )


def test_explicit_outer_pidfile_dir_passes_through(tmp_path: Path) -> None:
    outer = tmp_path / "explicit-outer-pids"
    outer.mkdir()
    proc = _run_isolated_probe(
        ["printenv", "GOAL_FLIGHT_PIDFILE_DIR"],
        extra_env={"GOAL_FLIGHT_PIDFILE_DIR": str(outer)},
    )
    assert proc.returncode == 0, proc.stderr
    assert Path(proc.stdout.strip()) == outer, proc.stdout

    alias_proc = _run_isolated_probe(
        ["printenv", "GOALFLIGHT_PIDFILE_DIR"],
        extra_env={"GOAL_FLIGHT_PIDFILE_DIR": str(outer)},
    )
    assert alias_proc.returncode == 0, alias_proc.stderr
    assert Path(alias_proc.stdout.strip()) == outer, alias_proc.stdout


def test_alias_outer_pidfile_dir_passes_through_when_production_name_unset(
    tmp_path: Path,
) -> None:
    outer = tmp_path / "explicit-alias-pids"
    outer.mkdir()
    proc = _run_isolated_probe(
        ["printenv", "GOAL_FLIGHT_PIDFILE_DIR"],
        extra_env={"GOALFLIGHT_PIDFILE_DIR": str(outer)},
        unset=("GOAL_FLIGHT_PIDFILE_DIR",),
    )
    assert proc.returncode == 0, proc.stderr
    assert Path(proc.stdout.strip()) == outer, proc.stdout
