"""Launch-time guard: a grok seat home with no permission_mode must refuse.

Locks the config.toml check, not the grok CLI --permission-mode flag (that
omit is already locked in test_acp_model_passthrough.py and must stay).
"""

from __future__ import annotations

import argparse
from pathlib import Path
import os
import stat
import subprocess
import sys
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import goalflight_dispatch as D  # noqa: E402
import grok_permission_mode as G  # noqa: E402
import goalflight_doctor  # noqa: E402

DISPATCH_PY = SCRIPTS / "goalflight_dispatch.py"


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _fresh_config() -> str:
    return "[cli]\n# freshly provisioned home: no [ui], no permission_mode\n"


def _present_config(mode: str = "always-approve") -> str:
    return f'[cli]\n\n[ui]\npermission_mode = "{mode}"\n'


def _args(*, agent: str = "grok-code", account: str | None = None) -> SimpleNamespace:
    return SimpleNamespace(agent=agent, account=account)


def test_inspect_missing_file(tmp_path: Path) -> None:
    path = tmp_path / ".grok" / "config.toml"
    inspection = G.inspect_config(path)
    assert inspection.status == "missing"
    assert inspection.mode is None
    assert str(path) in G.refusal_message(inspection)


def test_inspect_absent_permission_mode(tmp_path: Path) -> None:
    path = _write(tmp_path / ".grok" / "config.toml", _fresh_config())
    inspection = G.inspect_config(path)
    assert inspection.status == "absent"
    assert inspection.mode is None
    message = G.refusal_message(inspection)
    assert str(path) in message
    assert "worker_dead_no_terminal_marker" in message
    assert 'permission_mode = "always-approve"' in message
    assert "[ui]" in message


def test_inspect_commented_out_mode_is_absent(tmp_path: Path) -> None:
    path = _write(
        tmp_path / ".grok" / "config.toml",
        '# permission_mode = "always-approve"\n[ui]\nscreen_mode = "minimal"\n',
    )
    assert G.inspect_config(path).status == "absent"


def test_inspect_empty_value_is_absent(tmp_path: Path) -> None:
    path = _write(tmp_path / ".grok" / "config.toml", '[ui]\npermission_mode = ""\n')
    assert G.inspect_config(path).status == "absent"


def test_inspect_present_always_approve(tmp_path: Path) -> None:
    path = _write(tmp_path / ".grok" / "config.toml", _present_config())
    inspection = G.inspect_config(path)
    assert inspection.status == "present"
    assert inspection.mode == "always-approve"


@pytest.mark.parametrize(
    "mode",
    ["always-approve", "default", "acceptEdits", "auto", "dontAsk", "plan", "custom-value"],
)
def test_inspect_does_not_judge_present_values(tmp_path: Path, mode: str) -> None:
    _write(tmp_path / ".grok" / "config.toml", _present_config(mode))
    inspection = G.inspect_home(tmp_path)
    assert inspection.status == "present"
    assert inspection.mode == mode


def test_inspect_cli_table_counts_as_present(tmp_path: Path) -> None:
    _write(
        tmp_path / ".grok" / "config.toml",
        '[cli]\npermission_mode = "always-approve"\n',
    )
    inspection = G.inspect_home(tmp_path)
    assert inspection.status == "present"
    assert inspection.mode == "always-approve"


def test_inspect_garbage_without_key_does_not_crash(tmp_path: Path) -> None:
    path = _write(tmp_path / ".grok" / "config.toml", "this is not toml [[[\n")
    inspection = G.inspect_config(path)
    assert inspection.status == "absent"
    assert inspection.mode is None


def test_inspect_unreadable_directory(tmp_path: Path) -> None:
    path = tmp_path / ".grok" / "config.toml"
    path.mkdir(parents=True)
    inspection = G.inspect_config(path)
    assert inspection.status == "unreadable"
    message = G.refusal_message(inspection)
    assert str(path) in message
    assert "could not be read" in message


def test_inspect_unreadable_permission_denied(tmp_path: Path) -> None:
    path = _write(tmp_path / ".grok" / "config.toml", _present_config())
    path.chmod(0)
    try:
        if os.access(path, os.R_OK):
            pytest.skip("platform still allows reading mode-0 files")
        inspection = G.inspect_config(path)
        assert inspection.status == "unreadable"
        assert str(path) in G.refusal_message(inspection)
    finally:
        path.chmod(stat.S_IRUSR | stat.S_IWUSR)


def test_home_comes_from_account_env_not_default(tmp_path: Path) -> None:
    selected = tmp_path / "selected"
    host = tmp_path / "host"
    _write(selected / ".grok" / "config.toml", _fresh_config())
    _write(host / ".grok" / "config.toml", _present_config())
    home = G.home_from_account_env({"HOME": str(selected)}, default_home=host)
    assert home == selected
    assert G.inspect_home(home).status == "absent"


def test_guard_skips_non_grok_agents(tmp_path: Path) -> None:
    _write(tmp_path / ".grok" / "config.toml", _fresh_config())
    assert D._guard_grok_seat_permission_mode(
        _args(agent="codex"),
        {"HOME": str(tmp_path)},
    ) is None
    assert D._guard_grok_seat_permission_mode(
        _args(agent="moonshot"),
        {"HOME": str(tmp_path)},
    ) is None


@pytest.mark.parametrize("agent", ["grok-code", "grok-research", "grok-acp"])
def test_guard_refuses_absent_mode_and_names_file(tmp_path: Path, agent: str) -> None:
    config = _write(tmp_path / ".grok" / "config.toml", _fresh_config())
    with pytest.raises(D.DispatchUsageError) as raised:
        D._guard_grok_seat_permission_mode(_args(agent=agent), {"HOME": str(tmp_path)})
    text = str(raised.value)
    assert str(config) in text
    assert "permission_mode" in text
    assert "worker_dead_no_terminal_marker" in text
    assert "always-approve" in text


def test_guard_uses_resolved_home_not_a_rederived_path(tmp_path: Path) -> None:
    resolved = tmp_path / "resolved-home"
    other = tmp_path / "other-home"
    _write(resolved / ".grok" / "config.toml", _fresh_config())
    _write(other / ".grok" / "config.toml", _present_config())
    with pytest.raises(D.DispatchUsageError) as raised:
        D._guard_grok_seat_permission_mode(
            _args(),
            {"HOME": str(resolved)},
            default_home=other,
        )
    assert str(resolved / ".grok" / "config.toml") in str(raised.value)
    assert str(other / ".grok" / "config.toml") not in str(raised.value)


def test_guard_passes_present_mode_and_returns_it(tmp_path: Path) -> None:
    _write(tmp_path / ".grok" / "config.toml", _present_config("dontAsk"))
    assert (
        D._guard_grok_seat_permission_mode(_args(), {"HOME": str(tmp_path)})
        == "dontAsk"
    )


def test_guard_host_default_uses_threaded_home(tmp_path: Path) -> None:
    _write(tmp_path / ".grok" / "config.toml", _fresh_config())
    with pytest.raises(D.DispatchUsageError) as raised:
        D._guard_grok_seat_permission_mode(_args(), {}, default_home=tmp_path)
    assert str(tmp_path / ".grok" / "config.toml") in str(raised.value)


def test_present_mode_leaves_worker_argv_unchanged(tmp_path: Path) -> None:
    prompt = _write(tmp_path / "prompt.md", "do the work\n")
    ns = argparse.Namespace(agent="grok-code", cwd="/tmp/x", read_only=False, model=None)
    argv, stdin_path = D.build_worker(ns, prompt, None)
    assert argv == ["grok", "--prompt-file", str(prompt), "--cwd", "/tmp/x"]
    assert stdin_path is None
    assert "--permission-mode" not in argv
    assert "acceptEdits" not in argv
    _write(tmp_path / "home" / ".grok" / "config.toml", _present_config())
    assert D._guard_grok_seat_permission_mode(
        _args(),
        {"HOME": str(tmp_path / "home")},
    ) == "always-approve"
    argv_after, _ = D.build_worker(ns, prompt, None)
    assert argv_after == argv


def test_launch_wrapper_is_the_only_account_env_call_in_main() -> None:
    source = (SCRIPTS / "goalflight_dispatch.py").read_text(encoding="utf-8")
    assert source.count("_resolve_launch_account_env(args)") == 3
    assert "else _resolve_launch_account_env(args)" in source
    assert "account_env = _resolve_launch_account_env(args)" in source
    assert "account_env = _resolve_account_env(args)" in source
    # The raw resolver stays for the wrapper and for tests; launch sites use
    # the wrapper so a missing permission_mode cannot reach spawn.
    after_def = source.split("def main(", 1)[1]
    assert "_resolve_account_env(args)" not in after_def


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


def _seat_home(root_home: Path, name: str = "seat") -> Path:
    return root_home / ".goal-flight" / "accounts" / name / "grok"


def test_launch_refuses_absent_mode_before_spawn(tmp_path: Path) -> None:
    home = tmp_path / "home"
    seat = _seat_home(home)
    config = _write(seat / ".grok" / "config.toml", _fresh_config())
    _write(seat / ".grok" / "auth.json", "{}\n")
    prompt = _write(tmp_path / "prompt.md", "COMPLETE: no-op\n")
    spawn_log = tmp_path / "grok-spawned"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_grok = fake_bin / "grok"
    fake_grok.write_text(
        "#!/bin/sh\nprintf 'spawned\\n' >> \"$GROK_SPAWN_LOG\"\nexit 0\n",
        encoding="utf-8",
    )
    fake_grok.chmod(0o755)

    env = _isolated_env(tmp_path, home)
    env["PATH"] = f"{fake_bin}{os.pathsep}{env.get('PATH', '')}"
    env["GROK_SPAWN_LOG"] = str(spawn_log)

    proc = subprocess.run(
        [
            sys.executable,
            str(DISPATCH_PY),
            "--agent",
            "grok-code",
            "--account",
            "seat",
            "--prompt-file",
            str(prompt),
            "--cwd",
            str(tmp_path),
            "--foreground",
            "--dispatch-id",
            "perm-guard-absent",
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
    assert proc.returncode == 64, proc.stderr
    assert str(config) in proc.stderr
    assert "permission_mode" in proc.stderr
    assert "worker_dead_no_terminal_marker" in proc.stderr
    assert not spawn_log.exists(), spawn_log.read_text(encoding="utf-8")


def test_launch_with_present_mode_reaches_the_worker(tmp_path: Path) -> None:
    home = tmp_path / "home"
    seat = _seat_home(home)
    _write(seat / ".grok" / "config.toml", _present_config())
    _write(seat / ".grok" / "auth.json", "{}\n")
    prompt = _write(tmp_path / "prompt.md", "COMPLETE: no-op\n")
    spawn_log = tmp_path / "grok-spawned"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_grok = fake_bin / "grok"
    fake_grok.write_text(
        "#!/bin/sh\nprintf 'spawned\\n' >> \"$GROK_SPAWN_LOG\"\nexit 0\n",
        encoding="utf-8",
    )
    fake_grok.chmod(0o755)

    env = _isolated_env(tmp_path, home)
    env["PATH"] = f"{fake_bin}{os.pathsep}{env.get('PATH', '')}"
    env["GROK_SPAWN_LOG"] = str(spawn_log)

    proc = subprocess.run(
        [
            sys.executable,
            str(DISPATCH_PY),
            "--agent",
            "grok-code",
            "--account",
            "seat",
            "--prompt-file",
            str(prompt),
            "--cwd",
            str(tmp_path),
            "--foreground",
            "--dispatch-id",
            "perm-guard-present",
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
    assert "has no permission_mode" not in proc.stderr
    assert "config.toml is missing" not in proc.stderr
    assert spawn_log.exists(), proc.stderr
    assert spawn_log.read_text(encoding="utf-8").strip() == "spawned"


def test_launch_host_default_refuses_absent_mode_before_spawn(tmp_path: Path) -> None:
    home = tmp_path / "home"
    config = _write(home / ".grok" / "config.toml", _fresh_config())
    prompt = _write(tmp_path / "prompt.md", "COMPLETE: no-op\n")
    spawn_log = tmp_path / "grok-spawned"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_grok = fake_bin / "grok"
    fake_grok.write_text(
        "#!/bin/sh\nprintf 'spawned\\n' >> \"$GROK_SPAWN_LOG\"\nexit 0\n",
        encoding="utf-8",
    )
    fake_grok.chmod(0o755)

    env = _isolated_env(tmp_path, home)
    env["PATH"] = f"{fake_bin}{os.pathsep}{env.get('PATH', '')}"
    env["GROK_SPAWN_LOG"] = str(spawn_log)

    proc = subprocess.run(
        [
            sys.executable,
            str(DISPATCH_PY),
            "--agent",
            "grok-code",
            "--prompt-file",
            str(prompt),
            "--cwd",
            str(tmp_path),
            "--foreground",
            "--dispatch-id",
            "perm-guard-host",
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
    assert proc.returncode == 64, proc.stderr
    assert str(config) in proc.stderr
    assert "permission_mode" in proc.stderr
    assert not spawn_log.exists()


def test_launch_missing_config_refuses_without_spawn(tmp_path: Path) -> None:
    home = tmp_path / "home"
    seat = _seat_home(home)
    _write(seat / ".grok" / "auth.json", "{}\n")
    prompt = _write(tmp_path / "prompt.md", "COMPLETE: no-op\n")
    spawn_log = tmp_path / "grok-spawned"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_grok = fake_bin / "grok"
    fake_grok.write_text(
        "#!/bin/sh\nprintf 'spawned\\n' >> \"$GROK_SPAWN_LOG\"\nexit 0\n",
        encoding="utf-8",
    )
    fake_grok.chmod(0o755)

    env = _isolated_env(tmp_path, home)
    env["PATH"] = f"{fake_bin}{os.pathsep}{env.get('PATH', '')}"
    env["GROK_SPAWN_LOG"] = str(spawn_log)

    proc = subprocess.run(
        [
            sys.executable,
            str(DISPATCH_PY),
            "--agent",
            "grok-code",
            "--account",
            "seat",
            "--prompt-file",
            str(prompt),
            "--cwd",
            str(tmp_path),
            "--foreground",
            "--dispatch-id",
            "perm-guard-missing",
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
    assert proc.returncode == 64, proc.stderr
    assert str(seat / ".grok" / "config.toml") in proc.stderr
    assert "is missing" in proc.stderr
    assert not spawn_log.exists()


def test_doctor_reports_absent_and_present_homes(tmp_path: Path) -> None:
    absent = tmp_path / "absent"
    present = tmp_path / "present"
    _write(absent / ".grok" / "auth.json", "{}\n")
    _write(absent / ".grok" / "config.toml", _fresh_config())
    _write(present / ".grok" / "auth.json", "{}\n")
    _write(present / ".grok" / "config.toml", _present_config("always-approve"))
    rows = goalflight_doctor.check_grok_permission_modes(
        accounts=[
            (None, absent / ".grok" / "auth.json"),
            ("seat", present / ".grok" / "auth.json"),
        ]
    )
    assert [row["status"] for row in rows] == ["absent", "present"]
    assert rows[0]["permission_mode"] is None
    assert rows[1]["permission_mode"] == "always-approve"
    assert rows[0]["config"].endswith("config.toml")
    warn = goalflight_doctor.status_line(
        False,
        "Grok permission_mode missing",
        f"{rows[0]['config']}: {rows[0]['detail']}",
    )
    assert warn.startswith("[WARN]")
    assert rows[0]["config"] in warn
