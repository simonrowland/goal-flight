from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import goalflight_codex_sandbox as codex_sandbox  # noqa: E402
import goalflight_acp_run as acp_run  # noqa: E402
import goalflight_dispatch as dispatch  # noqa: E402
import goalflight_os_sandbox as os_sandbox  # noqa: E402
import goalflight_profile  # noqa: E402


def _shared_cache(home: Path) -> Path:
    cache = home / ".cache" / "uv"
    cache.mkdir(parents=True, exist_ok=True)
    return cache.resolve()


def test_codex_worker_disables_remote_plugin_catalog_and_grants_shared_cache(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home = tmp_path / "controller-home"
    home.mkdir()
    cache = _shared_cache(home)
    monkeypatch.setenv("HOME", str(home))
    args = argparse.Namespace(
        agent="codex",
        cwd=str(ROOT),
        model=None,
        os_sandbox="workspace-write",
        read_only=False,
        parent_dispatch_id=None,
        codex_session_id=None,
        reasoning_effort=None,
    )

    argv, _stdin = dispatch.build_worker(args, "/tmp/worker.md", [])
    configs = [argv[i + 1] for i, arg in enumerate(argv[:-1]) if arg == "-c"]

    assert "features.remote_plugin=false" in configs, argv
    assert "features.plugins=false" not in configs, configs
    writable_config = next(
        value
        for value in configs
        if value.startswith("sandbox_workspace_write.writable_roots=")
    )
    writable_roots = json.loads(writable_config.split("=", 1)[1])
    assert str(cache) in writable_roots, str(writable_roots)


def test_native_codex_read_only_keeps_sandbox_and_shared_cache_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home = tmp_path / "controller-home"
    home.mkdir()
    cache = _shared_cache(home)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("UV_CACHE_DIR", str(tmp_path / "task-cache"))
    monkeypatch.setattr(
        dispatch,
        "_resolve_account_env",
        lambda _args: {"HOME": str(home), "UV_CACHE_DIR": str(tmp_path / "task-cache")},
    )
    monkeypatch.setattr(dispatch, "_guard_grok_seat_permission_mode", lambda *_args: None)

    args = argparse.Namespace(
        agent="codex",
        cwd=str(ROOT),
        model=None,
        os_sandbox="read-only",
        read_only=True,
        parent_dispatch_id=None,
        codex_session_id=None,
        reasoning_effort=None,
    )
    argv, _stdin = dispatch.build_worker(args, "/tmp/worker.md", [])
    env = dispatch._resolve_launch_account_env(SimpleNamespace(agent="codex", account=None))

    sandbox_index = argv.index("--sandbox")
    assert argv[sandbox_index + 1] == "read-only", argv
    assert "workspace-write" not in argv, argv
    assert not any(
        value.startswith("sandbox_workspace_write.writable_roots=")
        for value in argv
    ), argv
    assert env["UV_CACHE_DIR"] == str(cache), env["UV_CACHE_DIR"]


def test_codex_acp_worker_disables_remote_plugin_catalog() -> None:
    _binary, argv = acp_run.agent_command("codex-acp")

    assert "features.remote_plugin=false" in argv, argv


@pytest.mark.parametrize("agent", ["codex", "grok-code", "cursor"])
def test_direct_dispatch_environment_forces_one_shared_uv_cache(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, agent: str
) -> None:
    home = tmp_path / "controller-home"
    home.mkdir()
    cache = _shared_cache(home)
    task_cache = tmp_path / f"{agent}-task-cache"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("UV_CACHE_DIR", str(task_cache))
    monkeypatch.setattr(
        dispatch,
        "_resolve_account_env",
        lambda _args: {"UV_CACHE_DIR": str(task_cache), "HOME": str(tmp_path / "account-home")},
    )
    monkeypatch.setattr(dispatch, "_guard_grok_seat_permission_mode", lambda *_args: None)

    env = dispatch._resolve_launch_account_env(SimpleNamespace(agent=agent, account=None))

    assert env.get("UV_CACHE_DIR") == str(cache), env.get("UV_CACHE_DIR")


@pytest.mark.parametrize("agent", ["codex", "grok-code", "cursor"])
def test_acp_dispatch_environment_keeps_shared_cache_over_profile_values(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, agent: str
) -> None:
    home = tmp_path / "controller-home"
    home.mkdir()
    cache = _shared_cache(home)
    monkeypatch.setenv("HOME", str(home))
    inherited_task_cache = tmp_path / "task-cache"
    monkeypatch.setenv("UV_CACHE_DIR", str(inherited_task_cache))

    env = goalflight_profile.dispatch_env(
        agent,
        base={"UV_CACHE_DIR": str(inherited_task_cache)},
    )

    assert env.get("UV_CACHE_DIR") == str(cache), env.get("UV_CACHE_DIR")


def test_grok_read_only_wrapper_keeps_private_tmp_and_shared_uv_cache(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home = tmp_path / "controller-home"
    home.mkdir()
    cache = _shared_cache(home)
    account_home = home / ".goal-flight" / "accounts" / "probe" / "grok"
    account_home.mkdir(parents=True)
    private_tmp = tmp_path / "private-dispatch-tmp"
    private_tmp.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("UV_CACHE_DIR", str(tmp_path / "task-cache"))
    monkeypatch.setattr(dispatch, "_account_home", lambda _account, _engine: account_home)
    monkeypatch.setattr(
        dispatch,
        "_resolve_account_env",
        lambda _args: {
            "HOME": str(account_home),
            "XDG_CONFIG_HOME": str(account_home / ".config"),
            "XDG_STATE_HOME": str(account_home / ".local" / "state"),
            "XDG_DATA_HOME": str(account_home / ".local" / "share"),
        },
    )
    monkeypatch.setattr(dispatch, "_guard_grok_seat_permission_mode", lambda *_args: None)
    monkeypatch.setattr(dispatch.goalflight_compat, "is_macos", lambda: True)
    monkeypatch.setattr(
        dispatch.goalflight_dispatch_paths,
        "steer_file",
        lambda _dispatch_id: tmp_path / "steer.jsonl",
    )
    captured: dict[str, object] = {}

    def capture_profile(command: str, argv: list[str], **kwargs):
        captured.update(kwargs)
        return SimpleNamespace(command=command, args=argv)

    monkeypatch.setattr(os_sandbox, "prepare_os_sandbox_command", capture_profile)
    args = SimpleNamespace(
        agent="grok-code",
        account="probe",
        dispatch_id="grok-cache-probe",
        cwd=str(ROOT),
        read_only=True,
        os_sandbox="read-only",
        _grok_read_only_tmpdir=str(private_tmp),
    )
    args._account_env = dispatch._resolve_launch_account_env(args)

    dispatch._wrap_grok_read_only_os_sandbox(["grok"], args)

    environment = captured["environment"]
    assert environment["UV_CACHE_DIR"] == str(cache), environment["UV_CACHE_DIR"]
    assert environment["TMPDIR"] == str(private_tmp), environment
    assert Path(environment["UV_CACHE_DIR"]) != private_tmp / "uv-cache"


def test_macos_sandbox_profiles_grant_shared_cache_without_widening_grok_tmp(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home = tmp_path / "controller-home"
    home.mkdir()
    cache = _shared_cache(home)
    task_cache = tmp_path / "task-specific-cache"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("UV_CACHE_DIR", str(task_cache))
    temp_root = Path(tempfile.gettempdir()).resolve()
    private_tmp = temp_root / f"goalflight-grok-read-only-test-{os.getpid()}"
    private_tmp.mkdir()
    account_home = home / ".goal-flight" / "accounts" / "probe" / "grok"
    try:
        for agent in ("codex", "grok-code", "cursor"):
            for profile in ("read-only", "workspace-write"):
                environment = {"HOME": str(home), "UV_CACHE_DIR": str(cache)}
                command = agent
                if agent == "grok-code":
                    command = "grok"
                if agent == "grok-code" and profile == "read-only":
                    environment.update(
                        {
                            "HOME": str(account_home),
                            "XDG_CONFIG_HOME": str(account_home / ".config"),
                            "XDG_STATE_HOME": str(account_home / ".local" / "state"),
                            "XDG_DATA_HOME": str(account_home / ".local" / "share"),
                            "XDG_CACHE_HOME": str(account_home / ".cache"),
                            "TMPDIR": str(private_tmp),
                            "GOALFLIGHT_STEER_FILE": str(tmp_path / "steer.jsonl"),
                        }
                    )

                roots = os_sandbox.macos_write_roots(
                    str(ROOT),
                    profile,
                    agent=agent,
                    command=command,
                    environment=environment,
                )

                assert str(cache) in roots, (agent, profile)
                if agent == "grok-code" and profile == "read-only":
                    assert str(private_tmp.resolve()) in roots, "private TMPDIR grant missing"
                    assert str(temp_root) not in roots, "shared temp root was granted"

        cursor_roots = os_sandbox.macos_write_roots(
            str(ROOT),
            "read-only",
            agent="cursor-agent",
            command="cursor-agent",
        )
        assert str(cache) in cursor_roots, cursor_roots
        assert str(task_cache.resolve(strict=False)) not in cursor_roots, cursor_roots
    finally:
        shutil.rmtree(private_tmp, ignore_errors=True)


def test_codex_prompt_cache_override_warns_and_is_rewritten(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    home = tmp_path / "controller-home"
    home.mkdir()
    cache = _shared_cache(home)
    monkeypatch.setenv("HOME", str(home))
    brief = tmp_path / "brief.md"
    brief.write_text(
        "export UV_CACHE_DIR=/tmp/task-specific-uv-cache\nuv run pytest\n",
        encoding="utf-8",
    )

    assembled = dispatch._materialize_steer_prompt(
        str(brief), tmp_path / "dispatch", "cache-prompt", agent="codex"
    )
    delivered = Path(assembled).read_text(encoding="utf-8")

    assert "/tmp/task-specific-uv-cache" not in delivered
    assert f"UV_CACHE_DIR={cache}" in delivered
    assert "shared `UV_CACHE_DIR`" in delivered
    assert "WARN" in capsys.readouterr().err
    assert "If the brief specifies its own `UV_CACHE_DIR` assignment" in (
        dispatch.PROMPT_FILE_PREAMBLE
    )


def test_acp_prompt_cache_override_warns_and_is_rewritten(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    home = tmp_path / "controller-home"
    home.mkdir()
    cache = _shared_cache(home)
    monkeypatch.setenv("HOME", str(home))
    brief = tmp_path / "brief.md"
    brief.write_text(
        "UV_CACHE_DIR='/tmp/task-specific-uv-cache' uv run pytest\n",
        encoding="utf-8",
    )
    args = SimpleNamespace(
        agent="cursor",
        model=None,
        prompt_file=str(brief),
        cwd=str(ROOT),
        read_only=False,
        prompt=None,
        max_idle_secs="300",
        poll_secs="0.1",
        dispatch_id="cache-acp",
        status_json=None,
        permission_mode="auto",
        permission_dir=None,
        permission_inline_timeout_s=None,
        permission_user_timeout_s=None,
        billing="sub",
        tail=None,
        priority="normal",
        capacity_wait_s=None,
        no_orientation=True,
    )

    cfg = dispatch._build_acp_cfg(
        args,
        status_json=tmp_path / "status.json",
        base=tmp_path,
    )

    assert "/tmp/task-specific-uv-cache" not in cfg.prompt_text
    assert str(cache) in cfg.prompt_text
    assert "shared `UV_CACHE_DIR`" in cfg.prompt_text
    assert "WARN" in capsys.readouterr().err
