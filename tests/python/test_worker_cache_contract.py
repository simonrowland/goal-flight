from __future__ import annotations

import argparse
import importlib
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
sys.path.insert(0, str(ROOT / "scripts" / "hosts" / "codex"))

import bash_tail_controller  # noqa: E402
import goalflight_codex_sandbox as codex_sandbox  # noqa: E402
import goalflight_acp_run as acp_run  # noqa: E402
import goalflight_agent_limits  # noqa: E402
import goalflight_dispatch as dispatch  # noqa: E402
import goalflight_journal  # noqa: E402
import goalflight_messages  # noqa: E402
import goalflight_os_sandbox as os_sandbox  # noqa: E402
import goalflight_profile  # noqa: E402
import goalflight_review_job  # noqa: E402
import goalflight_task  # noqa: E402
import goalflight_wake  # noqa: E402
import grok_seats  # noqa: E402
import grok_usage  # noqa: E402


def _shared_cache(home: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    cache = home / ".cache" / "uv"
    cache.mkdir(parents=True, exist_ok=True)
    resolved = cache.resolve()
    monkeypatch.setattr(codex_sandbox, "_REAL_USER_HOME", home.resolve(), raising=False)
    monkeypatch.setattr(
        codex_sandbox,
        "_SHARED_WORKER_UV_CACHE_DIR",
        resolved,
        raising=False,
    )
    return resolved


def test_codex_worker_grants_shared_cache(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home = tmp_path / "controller-home"
    home.mkdir()
    cache = _shared_cache(home, monkeypatch)
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

    # Keep the remote-plugin flag absent; it triggers an 88 MB fallback checkout.
    assert "features.remote_plugin=false" not in configs, argv
    assert configs.count("features.plugins=false") == 1, configs
    writable_config = next(
        value
        for value in configs
        if value.startswith("sandbox_workspace_write.writable_roots=")
    )
    writable_roots = json.loads(writable_config.split("=", 1)[1])
    assert str(cache) in writable_roots, str(writable_roots)


def test_codex_raw_argv_is_unchanged() -> None:
    raw_argv = ["codex", "exec", "--help"]

    argv, stdin_path = dispatch.build_worker(
        argparse.Namespace(agent="codex"), "/tmp/worker.md", raw_argv
    )

    assert argv == raw_argv
    assert stdin_path is None


def test_native_codex_read_only_keeps_sandbox_and_shared_cache_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home = tmp_path / "controller-home"
    home.mkdir()
    cache = _shared_cache(home, monkeypatch)
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


def test_codex_acp_worker_keeps_default_args() -> None:
    _binary, argv = acp_run.agent_command("codex-acp")

    configs = [argv[i + 1] for i, arg in enumerate(argv[:-1]) if arg == "-c"]
    assert "features.remote_plugin=false" not in argv, argv
    assert configs.count("features.plugins=false") == 1, argv


def test_codex_review_job_uses_plugins_off_config(tmp_path: Path) -> None:
    argv = goalflight_review_job.command_for(
        argparse.Namespace(
            agent="codex",
            codex_bin="codex",
            output_dir=str(tmp_path),
            name="review",
            repo=str(tmp_path),
        )
    )

    configs = [argv[i + 1] for i, arg in enumerate(argv[:-1]) if arg == "-c"]
    assert configs.count("features.plugins=false") == 1, argv
    assert "features.remote_plugin=false" not in configs, argv


def test_codex_bash_tail_controller_uses_plugins_off_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    prompt_file = tmp_path / "prompt.md"
    prompt_file.write_text("prompt\n", encoding="utf-8")
    tail_path = tmp_path / "tail.log"
    captured: dict[str, object] = {}
    process = object()

    def fake_popen(argv: list[str], **kwargs: object) -> object:
        captured["argv"] = argv
        for name in ("stdin", "stdout"):
            stream = kwargs[name]
            stream.close()
        return process

    monkeypatch.setattr(bash_tail_controller.subprocess, "Popen", fake_popen)

    spawned = bash_tail_controller.spawn_codex_worker(
        tmp_path, prompt_file, tail_path
    )

    argv = captured["argv"]
    assert spawned is process
    assert isinstance(argv, list)
    configs = [argv[i + 1] for i, arg in enumerate(argv[:-1]) if arg == "-c"]
    assert configs.count("features.plugins=false") == 1, argv
    assert "features.remote_plugin=false" not in configs, argv


def test_codex_adapter_invocation_declares_plugins_off_once() -> None:
    adapter = json.loads((ROOT / "adapters" / "codex.json").read_text())
    invocation = adapter["invocation"]["exec"]
    args = invocation["args"]
    configs = [args[i + 1] for i, arg in enumerate(args[:-1]) if arg == "-c"]

    assert configs.count("features.plugins=false") == 1, args
    assert "features.remote_plugin=false" not in configs, args


@pytest.mark.parametrize("agent", ["codex", "grok-code", "cursor"])
def test_direct_dispatch_environment_forces_one_shared_uv_cache(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, agent: str
) -> None:
    home = tmp_path / "controller-home"
    home.mkdir()
    cache = _shared_cache(home, monkeypatch)
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


@pytest.mark.parametrize("agent", ["grok-code", "cursor"])
def test_account_worker_state_paths_ignore_account_xdg_redirect(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, agent: str
) -> None:
    controller_home = tmp_path / "controller-home"
    account_home = tmp_path / f"{agent}-account-home"
    controller_home.mkdir()
    account_home.mkdir()
    grok_dir = account_home / ".grok"
    grok_dir.mkdir()
    (grok_dir / "auth.json").write_text("{}\n", encoding="utf-8")
    (grok_dir / "config.toml").write_text(
        '[ui]\npermission_mode = "always-approve"\n', encoding="utf-8"
    )
    _shared_cache(controller_home, monkeypatch)

    controller_xdg = tmp_path / "controller-xdg"
    monkeypatch.setenv("HOME", str(controller_home))
    monkeypatch.setenv("XDG_STATE_HOME", str(controller_xdg))
    for key in (
        "GOALFLIGHT_TASK_STORE_DIR",
        "GOALFLIGHT_JOURNAL_DIR",
        "GOALFLIGHT_MESSAGES_DIR",
        "GOALFLIGHT_WAKE_LEDGER_DIR",
        "GOALFLIGHT_FLEET_DIR",
        "GOALFLIGHT_CAPACITY_CONF",
        "GOALFLIGHT_GROK_SEAT_STATE_FILE",
        "GOALFLIGHT_GROK_ACCOUNTS_DIR",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(
        dispatch, "_account_home", lambda _account, _engine: account_home
    )
    monkeypatch.setattr(
        dispatch, "_cursor_account_probe", lambda _env: (True, "")
    )
    monkeypatch.setattr(dispatch, "_guard_grok_seat_permission_mode", lambda *_args: None)
    seat_state = controller_home / ".goal-flight" / "grok-seat-states.json"
    grok_accounts = controller_home / ".goal-flight" / "accounts"
    monkeypatch.setattr(grok_seats, "STATE_PATH", seat_state)
    monkeypatch.setattr(grok_usage, "ACCOUNTS_DIR", grok_accounts)

    account_env = dispatch._resolve_launch_account_env(
        SimpleNamespace(agent=agent, account="probe")
    )
    worker_env = os.environ.copy()
    worker_env.update(account_env)

    assert worker_env["XDG_STATE_HOME"] == str(account_home / ".local" / "state")
    assert worker_env["GOALFLIGHT_TASK_STORE_DIR"] == str(
        controller_xdg / "goal-flight"
    )
    assert worker_env["GOALFLIGHT_JOURNAL_DIR"] == str(
        controller_xdg / "goal-flight"
    )
    assert worker_env["GOALFLIGHT_MESSAGES_DIR"] == str(
        controller_home / ".goal-flight" / "messages"
    )
    assert worker_env["GOALFLIGHT_WAKE_LEDGER_DIR"] == str(
        controller_xdg / "goal-flight" / "wake-ledger"
    )
    assert worker_env["GOALFLIGHT_FLEET_DIR"] == str(
        controller_home / ".goal-flight" / "fleet"
    )
    assert worker_env["GOALFLIGHT_CAPACITY_CONF"] == str(
        controller_home / ".goal-flight" / "capacity.local.json"
    )
    assert worker_env["GOALFLIGHT_GROK_SEAT_STATE_FILE"] == str(seat_state)
    assert worker_env["GOALFLIGHT_GROK_ACCOUNTS_DIR"] == str(grok_accounts)

    for key in (
        "HOME",
        "XDG_STATE_HOME",
        "GOALFLIGHT_TASK_STORE_DIR",
        "GOALFLIGHT_JOURNAL_DIR",
        "GOALFLIGHT_MESSAGES_DIR",
        "GOALFLIGHT_WAKE_LEDGER_DIR",
        "GOALFLIGHT_FLEET_DIR",
        "GOALFLIGHT_CAPACITY_CONF",
    ):
        monkeypatch.setenv(key, worker_env[key])

    real_store = controller_xdg / "goal-flight"
    assert goalflight_task.resolve_state_base_dir() == real_store
    assert goalflight_task.resolve_task_store_dir(ROOT).parents[1] == real_store
    assert goalflight_journal.journals_index_dir() == real_store / "journals"
    assert (
        goalflight_journal.resolve_journal_path(ROOT).parent.parent
        == real_store / "journals"
    )
    assert goalflight_messages.default_messages_dir() == (
        controller_home / ".goal-flight" / "messages"
    )
    assert goalflight_wake.ledger_base_dir() == real_store / "wake-ledger"
    assert goalflight_messages.default_fleet_dir() == (
        controller_home / ".goal-flight" / "fleet"
    )
    assert goalflight_agent_limits._local_conf_path() == (
        controller_home / ".goal-flight" / "capacity.local.json"
    )


def test_grok_machine_path_overrides_are_read_at_import_time(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    seat_state = tmp_path / "controller" / "grok-seat-states.json"
    accounts = tmp_path / "controller" / "accounts"
    monkeypatch.setenv("GOALFLIGHT_GROK_SEAT_STATE_FILE", str(seat_state))
    monkeypatch.setenv("GOALFLIGHT_GROK_ACCOUNTS_DIR", str(accounts))
    monkeypatch.setattr(grok_seats, "STATE_PATH", grok_seats.STATE_PATH)
    monkeypatch.setattr(grok_usage, "ACCOUNTS_DIR", grok_usage.ACCOUNTS_DIR)

    importlib.reload(grok_seats)
    importlib.reload(grok_usage)

    assert grok_seats.STATE_PATH == seat_state
    assert grok_usage.ACCOUNTS_DIR == accounts


def test_account_worker_state_pins_respect_controller_overrides(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    controller_home = tmp_path / "controller-home"
    account_home = tmp_path / "cursor-account-home"
    controller_home.mkdir()
    account_home.mkdir()
    monkeypatch.setenv("HOME", str(controller_home))
    monkeypatch.setattr(
        dispatch, "_account_home", lambda _account, _engine: account_home
    )
    monkeypatch.setattr(dispatch, "_cursor_account_probe", lambda _env: (True, ""))
    expected = {
        "GOALFLIGHT_TASK_STORE_DIR": tmp_path / "task-store",
        "GOALFLIGHT_JOURNAL_DIR": tmp_path / "journal",
        "GOALFLIGHT_MESSAGES_DIR": tmp_path / "messages",
        "GOALFLIGHT_WAKE_LEDGER_DIR": tmp_path / "wake-ledger",
        "GOALFLIGHT_FLEET_DIR": tmp_path / "fleet",
        "GOALFLIGHT_CAPACITY_CONF": tmp_path / "capacity.json",
        "GOALFLIGHT_GROK_SEAT_STATE_FILE": tmp_path / "grok-seat-states.json",
        "GOALFLIGHT_GROK_ACCOUNTS_DIR": tmp_path / "accounts",
    }
    for key, path in expected.items():
        monkeypatch.setenv(key, str(path))

    account_env = dispatch._resolve_account_env(
        SimpleNamespace(agent="cursor", account="probe")
    )

    assert {
        key: account_env[key] for key in expected
    } == {key: str(path.resolve()) for key, path in expected.items()}


def test_accountless_grok_resume_preflight_pins_controller_state_paths(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    controller_home = tmp_path / "controller-home"
    account_home = tmp_path / "grok-account-home"
    controller_home.mkdir()
    account_home.mkdir()
    grok_dir = account_home / ".grok"
    grok_dir.mkdir()
    (grok_dir / "auth.json").write_text("{}\n", encoding="utf-8")
    (grok_dir / "config.toml").write_text(
        '[ui]\npermission_mode = "always-approve"\n', encoding="utf-8"
    )
    _shared_cache(controller_home, monkeypatch)
    controller_xdg = tmp_path / "controller-xdg"
    monkeypatch.setenv("HOME", str(controller_home))
    monkeypatch.setenv("XDG_STATE_HOME", str(controller_xdg))
    for key in (
        "GOALFLIGHT_TASK_STORE_DIR",
        "GOALFLIGHT_JOURNAL_DIR",
        "GOALFLIGHT_MESSAGES_DIR",
        "GOALFLIGHT_WAKE_LEDGER_DIR",
        "GOALFLIGHT_FLEET_DIR",
        "GOALFLIGHT_CAPACITY_CONF",
        "GOALFLIGHT_GROK_SEAT_STATE_FILE",
        "GOALFLIGHT_GROK_ACCOUNTS_DIR",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(dispatch, "_select_healthy_grok_account", lambda **_kwargs: "probe")
    monkeypatch.setattr(
        dispatch, "_account_home", lambda _account, _engine: account_home
    )
    monkeypatch.setattr(
        grok_seats,
        "STATE_PATH",
        controller_home / ".goal-flight" / "grok-seat-states.json",
    )
    monkeypatch.setattr(
        grok_usage,
        "ACCOUNTS_DIR",
        controller_home / ".goal-flight" / "accounts",
    )

    args = dispatch._build_launch_parser().parse_args(
        ["--agent", "grok-code", "--shape", "bash", "--prompt", "continue"]
    )
    resume_env = dispatch._validate_before_side_effects(args, [])
    worker_env = os.environ.copy()
    worker_env.update(resume_env)

    assert args.account is None
    assert worker_env["HOME"] == str(account_home)
    assert worker_env["XDG_STATE_HOME"] == str(account_home / ".local" / "state")
    assert worker_env["GOALFLIGHT_TASK_STORE_DIR"] == str(
        controller_xdg / "goal-flight"
    )
    assert worker_env["GOALFLIGHT_JOURNAL_DIR"] == str(
        controller_xdg / "goal-flight"
    )
    assert worker_env["GOALFLIGHT_MESSAGES_DIR"] == str(
        controller_home / ".goal-flight" / "messages"
    )
    assert worker_env["GOALFLIGHT_WAKE_LEDGER_DIR"] == str(
        controller_xdg / "goal-flight" / "wake-ledger"
    )


@pytest.mark.parametrize("agent", ["codex", "grok-code", "cursor"])
def test_acp_dispatch_environment_keeps_shared_cache_over_profile_values(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, agent: str
) -> None:
    home = tmp_path / "controller-home"
    home.mkdir()
    cache = _shared_cache(home, monkeypatch)
    monkeypatch.setenv("HOME", str(home))
    inherited_task_cache = tmp_path / "task-cache"
    monkeypatch.setenv("UV_CACHE_DIR", str(inherited_task_cache))

    env = goalflight_profile.dispatch_env(
        agent,
        base={"UV_CACHE_DIR": str(inherited_task_cache)},
    )

    assert env.get("UV_CACHE_DIR") == str(cache), env.get("UV_CACHE_DIR")


def test_acp_worker_spawn_keeps_controller_cache_after_account_home_swap(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home = tmp_path / "controller-home"
    account_home = tmp_path / "account-home"
    home.mkdir()
    account_home.mkdir()
    cache = _shared_cache(home, monkeypatch)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(
        dispatch,
        "_resolve_account_env",
        lambda _args: {"HOME": str(home), "UV_CACHE_DIR": str(tmp_path / "task-cache")},
    )
    monkeypatch.setattr(dispatch, "_guard_grok_seat_permission_mode", lambda *_args: None)
    args = SimpleNamespace(agent="grok-acp", account=None)
    controller_env = dispatch._resolve_launch_account_env(args)
    assert controller_env["UV_CACHE_DIR"] == str(cache), controller_env

    cfg = SimpleNamespace(
        agent="grok-acp",
        dispatch_id="acp-cache-home-probe",
        capacity_wait_s=0,
        preserve_capacity_refusal_attempt=False,
        watcher_prompt_file=None,
    )
    spawned_env: dict[str, str] = {}

    async def capture_worker_spawn(_cfg):
        assert os.environ["HOME"] == str(account_home)
        spawned_env.update(
            acp_run._worker_spawn_env(
                SimpleNamespace(agent="grok-acp", install_slot=None, prompt=None),
                None,
            )
        )
        return {
            "state": "completed",
            "dispatch_id": cfg.dispatch_id,
            "agent": cfg.agent,
            "worker_pid": None,
            "worker_alive": False,
        }

    monkeypatch.setattr(dispatch, "_refuse_reused_dispatch_id_for_launch", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(dispatch, "_build_acp_cfg", lambda *_args, **_kwargs: cfg)
    monkeypatch.setattr(dispatch, "_emit_dispatch_warnings", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(dispatch, "_run_test_acp_shape_if_requested", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(dispatch, "_web_qa_env_plan", lambda *_args: ({}, []))
    monkeypatch.setattr(dispatch, "_project_root", lambda _args: tmp_path)
    monkeypatch.setattr(dispatch, "_dispatch_end_reattach_hint", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(acp_run, "run_acp_dispatch", capture_worker_spawn)
    monkeypatch.setattr(acp_run, "acp_dispatch_exit_code", lambda _payload: 0)
    args = SimpleNamespace(
        dispatch_id=cfg.dispatch_id,
        agent=cfg.agent,
        billing="api",
        status_json=None,
        tail=None,
        dispatch_warnings=[],
        launch_detached=False,
        read_only=False,
        hints=False,
        _worktree_seat=None,
    )
    account_env = {**controller_env, "HOME": str(account_home)}
    monkeypatch.setenv("GOALFLIGHT_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv(
        "GOALFLIGHT_DISPATCH_DIR", str(tmp_path / "state" / "dispatch")
    )

    assert dispatch._run_acp_shape(args, base=tmp_path, account_env=account_env) == 0
    assert spawned_env["HOME"] == str(account_home), spawned_env
    assert spawned_env["UV_CACHE_DIR"] == str(cache), spawned_env


def test_grok_read_only_wrapper_keeps_private_tmp_and_shared_uv_cache(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home = tmp_path / "controller-home"
    home.mkdir()
    cache = _shared_cache(home, monkeypatch)
    account_home = home / ".goal-flight" / "accounts" / "probe" / "grok"
    account_home.mkdir(parents=True)
    scratch = tmp_path / "dispatch.scratch"
    scratch.mkdir()
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
            "GOALFLIGHT_SCRATCH_DIR": str(scratch),
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
    )
    args._account_env = dispatch._resolve_launch_account_env(args)

    dispatch._wrap_grok_read_only_os_sandbox(["grok"], args)

    environment = captured["environment"]
    assert environment["UV_CACHE_DIR"] == str(cache), environment["UV_CACHE_DIR"]
    private_tmp = Path(environment["TMPDIR"])
    assert private_tmp.parent == scratch, environment
    assert private_tmp.is_dir()
    assert Path(environment["GOALFLIGHT_SCRATCH_DIR"]) == scratch
    assert Path(environment["UV_CACHE_DIR"]) != private_tmp / "uv-cache"


def test_macos_sandbox_profiles_grant_cache_only_to_writable_workers(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home = tmp_path / "controller-home"
    home.mkdir()
    cache = _shared_cache(home, monkeypatch)
    task_cache = tmp_path / "task-specific-cache"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("UV_CACHE_DIR", str(task_cache))
    scratch = tmp_path / f"goalflight-dispatch-scratch-{os.getpid()}"
    private_tmp = scratch / f"goalflight-grok-read-only-test-{os.getpid()}"
    scratch.mkdir()
    private_tmp.mkdir(parents=True)
    account_home = home / ".goal-flight" / "accounts" / "probe" / "grok"
    try:
        for agent in ("codex", "codex-acp", "grok-code", "cursor"):
            for profile in ("read-only", "workspace-write"):
                environment = {
                    "HOME": str(home),
                    "UV_CACHE_DIR": str(cache),
                    "GOALFLIGHT_SCRATCH_DIR": str(scratch),
                    "TMPDIR": str(scratch),
                }
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
                            "GOALFLIGHT_SCRATCH_DIR": str(scratch),
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

                if profile == "workspace-write":
                    assert str(cache) in roots, (agent, profile)
                else:
                    assert not any(
                        os_sandbox._path_contains(root, str(cache)) for root in roots
                    ), (agent, profile, roots)
                if agent == "grok-code" and profile == "read-only":
                    assert str(private_tmp.resolve()) in roots, "private TMPDIR grant missing"
                assert str(Path("/tmp").resolve()) not in roots, roots
                assert str(Path("/private/tmp").resolve()) not in roots, roots

        cursor_roots = os_sandbox.macos_write_roots(
            str(ROOT),
            "read-only",
            agent="cursor-agent",
            command="cursor-agent",
            environment={"HOME": str(home)},
        )
        assert not any(
            os_sandbox._path_contains(root, str(cache)) for root in cursor_roots
        ), cursor_roots
        assert str(task_cache.resolve(strict=False)) not in cursor_roots, cursor_roots
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


@pytest.mark.parametrize("symlink_component", [".cache", "uv"])
def test_macos_sandbox_refuses_symlinked_cache_write_root(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    symlink_component: str,
) -> None:
    home = tmp_path / "controller-home"
    home.mkdir()
    target = tmp_path / "cache-target"
    target.mkdir()
    if symlink_component == ".cache":
        (target / "uv").mkdir()
        (home / ".cache").symlink_to(target, target_is_directory=True)
    else:
        (home / ".cache").mkdir()
        (home / ".cache" / "uv").symlink_to(target, target_is_directory=True)
    cache = (home / ".cache" / "uv").resolve()
    monkeypatch.setattr(codex_sandbox, "_REAL_USER_HOME", home.resolve(), raising=False)
    monkeypatch.setattr(codex_sandbox, "_SHARED_WORKER_UV_CACHE_DIR", cache, raising=False)
    monkeypatch.setenv("HOME", str(home))
    roots = os_sandbox.macos_write_roots(
        str(ROOT),
        "workspace-write",
        agent="grok-code",
        command="grok",
        environment={"HOME": str(home)},
    )
    configs = codex_sandbox.codex_workspace_write_args(str(ROOT), "workspace-write")
    codex_roots = json.loads(configs[1].split("=", 1)[1])
    warning = capsys.readouterr().err
    symlink_path = (
        home / ".cache"
        if symlink_component == ".cache"
        else home / ".cache" / "uv"
    )

    assert not any(os_sandbox._path_contains(root, str(cache)) for root in roots), roots
    assert not any(
        os_sandbox._path_contains(root, str(cache)) for root in codex_roots
    ), codex_roots
    assert "symlink" in warning.lower(), warning
    assert str(symlink_path) in warning, warning


def _brief_with_cache_examples() -> bytes:
    return (
        "Quoted example: `UV_CACHE_DIR=/var/cache/app`.\r\n"
        "export UV_CACHE_DIR=$(mktemp -d)\r\n"
        "UV_CACHE_DIR=\r\n"
        "uv run pytest\r\n"
        "# UV_CACHE_DIR=/tmp/keep-this-comment\r\n"
        "UV_CACHE_DIR=${OTHER:-/tmp/default}\r\n"
    ).encode("utf-8")


def test_codex_prompt_is_byte_identical_and_warns_on_cache_lines(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    home = tmp_path / "controller-home"
    home.mkdir()
    _shared_cache(home, monkeypatch)
    monkeypatch.setenv("HOME", str(home))
    brief = tmp_path / "brief.md"
    brief_bytes = _brief_with_cache_examples()
    brief.write_bytes(brief_bytes)

    assembled = dispatch._materialize_steer_prompt(
        str(brief), tmp_path / "dispatch", "cache-prompt", agent="codex"
    )
    delivered = Path(assembled).read_bytes()
    warning = capsys.readouterr().err

    assert brief_bytes in delivered
    assert "WARN" in warning
    assert "line(s) 1, 2, 3, 5, 6" in warning, warning


def test_acp_prompt_is_byte_identical_and_warns_on_cache_lines(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    home = tmp_path / "controller-home"
    home.mkdir()
    _shared_cache(home, monkeypatch)
    monkeypatch.setenv("HOME", str(home))
    brief = tmp_path / "brief.md"
    brief_bytes = _brief_with_cache_examples()
    brief.write_bytes(brief_bytes)
    monkeypatch.setattr(
        dispatch,
        "_project_orientation_path",
        lambda *_args, **_kwargs: tmp_path / "orientation.md",
    )
    monkeypatch.setattr(
        dispatch,
        "_project_orientation_preamble",
        lambda _path: "orientation mentions UV_CACHE_DIR but is not the brief",
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
        no_orientation=False,
    )

    cfg = dispatch._build_acp_cfg(
        args,
        status_json=tmp_path / "status.json",
        base=tmp_path,
    )

    warning = capsys.readouterr().err

    assert brief_bytes in cfg.prompt_text.encode("utf-8")
    assert "WARN" in warning
    assert "line(s) 1, 2, 3, 5, 6" in warning, warning
