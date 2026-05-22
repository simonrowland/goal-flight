#!/usr/bin/env python3
"""OS sandbox dispatch tests."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tempfile


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import goalflight_acp_run  # noqa: E402
import goalflight_adapter_readiness  # noqa: E402
import goalflight_os_sandbox as goalflight_os_sandbox_mod  # noqa: E402
from goalflight_acp_client import AcpError, AcpProcessPool  # noqa: E402
from goalflight_os_sandbox import (  # noqa: E402
    OS_SANDBOX_OFF,
    OS_SANDBOX_READ_ONLY,
    OS_SANDBOX_WORKSPACE_WRITE,
    OsSandboxError,
    canonical_os_sandbox,
    preflight_os_sandbox,
    prepare_os_sandbox_command,
)


FAKE = ROOT / "test/fixtures/acp_fake_agent.py"


def _sandbox_available() -> bool:
    return platform.system() == "Darwin" and shutil.which("sandbox-exec") is not None


def _write_supported_adapter_manifest(directory: Path, name: str) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{name}.json").write_text(json.dumps({
        "support": {
            "controller": {"capability": "supported", "fallback": "worker_only"},
            "worker": {"capability": "supported", "transport": ["acp"], "fallback": "tail_file"},
        },
        "local_readiness_state": {
            "controller": "probe_required",
            "worker": "probe_required",
            "last_probe_ids": ["python-version"],
        },
        "live_gate": {"function": "validate_adapter_gate", "default": "deny"},
        "status_contract": {"terminal_states": ["complete"], "stale_after_s": 60},
        "permission_surface": {
            "plugin_sandbox": {},
            "os_sandbox": {
                "supported_profiles": ["off", "read-only", "workspace-write"],
                "default_profile": "off",
                "implementation": "runner:sandbox-exec",
            },
            "auto_approve_detection": {"strict_fail": True},
        },
        "discovery": {
            "probes": [{
                "id": "python-version",
                "argv": [sys.executable, "--version"],
                "safe_for_setup": True,
                "network": False,
                "model_consuming": False,
            }],
        },
        "invocation": {"exec": {"arg_policy": {"forbidden_args": []}}},
    }))


def case_canonical_profiles() -> None:
    assert canonical_os_sandbox(None) == OS_SANDBOX_OFF
    assert canonical_os_sandbox("host-default") == OS_SANDBOX_OFF
    assert canonical_os_sandbox("none") == OS_SANDBOX_OFF
    assert canonical_os_sandbox("readonly") == OS_SANDBOX_READ_ONLY
    assert canonical_os_sandbox("read-only") == OS_SANDBOX_READ_ONLY
    assert canonical_os_sandbox("workspace") == OS_SANDBOX_WORKSPACE_WRITE
    assert canonical_os_sandbox("workspace-write") == OS_SANDBOX_WORKSPACE_WRITE
    try:
        canonical_os_sandbox("evil")
    except OsSandboxError:
        pass
    else:
        raise AssertionError("invalid OS sandbox profile should fail closed")


def case_requested_sandbox_fails_closed_on_unsupported_hosts() -> None:
    old_system = goalflight_os_sandbox_mod.platform.system
    old_which = goalflight_os_sandbox_mod.shutil.which
    try:
        goalflight_os_sandbox_mod.shutil.which = lambda name: "/usr/bin/sandbox-exec"
        for host in ("Windows", "Linux"):
            goalflight_os_sandbox_mod.platform.system = lambda host=host: host
            assert preflight_os_sandbox(OS_SANDBOX_OFF) == OS_SANDBOX_OFF
            for profile in (OS_SANDBOX_READ_ONLY, OS_SANDBOX_WORKSPACE_WRITE):
                try:
                    preflight_os_sandbox(profile)
                except OsSandboxError as e:
                    assert "requires macOS sandbox-exec" in str(e), e
                    assert f"platform={host}" in str(e), e
                else:
                    raise AssertionError(f"{host} OS sandbox request should fail closed")
    finally:
        goalflight_os_sandbox_mod.platform.system = old_system
        goalflight_os_sandbox_mod.shutil.which = old_which


def case_prepare_wrapper_blocks_home_write() -> None:
    if not _sandbox_available():
        print("SKIP: sandbox-exec unavailable")
        return
    workspace = ROOT / f".goalflight-os-sandbox-direct-{os.getpid()}"
    outside = Path.home() / ".goalflight-sandbox-outside-probe"
    shutil.rmtree(workspace, ignore_errors=True)
    workspace.mkdir()
    if outside.exists():
        outside.unlink()
    try:
        inside = workspace / "inside.txt"
        code = (
            "from pathlib import Path; "
            f"Path(r'{inside}').write_text('inside'); "
            "print('inside-ok'); "
            f"Path(r'{outside}').write_text('outside'); "
            "print('outside-ok')"
        )
        prepared = prepare_os_sandbox_command(
            sys.executable,
            ["-c", code],
            cwd=str(workspace),
            os_sandbox=OS_SANDBOX_WORKSPACE_WRITE,
        )
        result = subprocess.run(
            [prepared.command, *prepared.args],
            cwd=str(workspace),
            text=True,
            capture_output=True,
            timeout=10,
            check=False,
        )
        assert result.returncode != 0, result
        assert inside.exists(), result.stderr
        assert not outside.exists(), "sandbox allowed write outside workspace"
        assert prepared.metadata()["profile"] == OS_SANDBOX_WORKSPACE_WRITE
    finally:
        if outside.exists():
            outside.unlink()
        shutil.rmtree(workspace, ignore_errors=True)


def case_profile_string_escapes_workspace_path() -> None:
    if not _sandbox_available():
        print("SKIP: sandbox-exec unavailable")
        return
    base = ROOT / f".goalflight-os-sandbox-injection-{os.getpid()}"
    outside = Path.home() / ".goalflight-sandbox-injection-probe"
    workspace = base / 'bad") (allow file-write* (subpath "/Users") ;'
    shutil.rmtree(base, ignore_errors=True)
    workspace.mkdir(parents=True)
    if outside.exists():
        outside.unlink()
    try:
        inside = workspace / "inside.txt"
        code = (
            "from pathlib import Path; "
            f"Path(r'{inside}').write_text('inside'); "
            "print('inside-ok'); "
            f"Path(r'{outside}').write_text('outside'); "
            "print('outside-ok')"
        )
        prepared = prepare_os_sandbox_command(
            sys.executable,
            ["-c", code],
            cwd=str(workspace),
            os_sandbox=OS_SANDBOX_WORKSPACE_WRITE,
        )
        result = subprocess.run(
            [prepared.command, *prepared.args],
            cwd=str(workspace),
            text=True,
            capture_output=True,
            timeout=10,
            check=False,
        )
        assert result.returncode != 0, result
        assert inside.exists(), result.stderr
        assert not outside.exists(), "sandbox profile string injection allowed outside write"
    finally:
        if outside.exists():
            outside.unlink()
        shutil.rmtree(base, ignore_errors=True)


def case_rejects_cwd_under_temp_root() -> None:
    if not _sandbox_available():
        print("SKIP: sandbox-exec unavailable")
        return
    with tempfile.TemporaryDirectory(prefix="gf-os-sandbox-temp-cwd-") as tmp:
        try:
            prepare_os_sandbox_command(
                sys.executable,
                ["-c", "print('x')"],
                cwd=tmp,
                os_sandbox=OS_SANDBOX_READ_ONLY,
            )
        except OsSandboxError as e:
            assert "inside allowed temp root" in str(e)
        else:
            raise AssertionError("cwd under temp root should fail closed")


def case_agent_state_roots_are_explicit_exception() -> None:
    if not _sandbox_available():
        print("SKIP: sandbox-exec unavailable")
        return
    old_home = os.environ.get("HOME")
    base = ROOT / f".goalflight-os-sandbox-agent-state-{os.getpid()}"
    workspace = base / "workspace"
    fake_home = base / "home"
    outside = fake_home / "outside.txt"
    state_file = fake_home / ".grok" / "state.txt"
    shutil.rmtree(base, ignore_errors=True)
    workspace.mkdir(parents=True)
    state_file.parent.mkdir(parents=True)
    try:
        os.environ["HOME"] = str(fake_home)
        code = (
            "from pathlib import Path; "
            f"Path(r'{state_file}').write_text('state'); "
            "print('state-ok'); "
            f"Path(r'{outside}').write_text('outside'); "
            "print('outside-ok')"
        )
        prepared = prepare_os_sandbox_command(
            sys.executable,
            ["-c", code],
            cwd=str(workspace),
            os_sandbox=OS_SANDBOX_READ_ONLY,
            agent="grok",
        )
        result = subprocess.run(
            [prepared.command, *prepared.args],
            cwd=str(workspace),
            text=True,
            capture_output=True,
            timeout=10,
            check=False,
            env={**os.environ, "HOME": str(fake_home)},
        )
        assert result.returncode != 0, result
        assert state_file.exists(), result.stderr
        assert not outside.exists(), "sandbox allowed non-agent-state home write"
    finally:
        if old_home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = old_home
        shutil.rmtree(base, ignore_errors=True)


def case_rejects_cwd_under_agent_state_root() -> None:
    if not _sandbox_available():
        print("SKIP: sandbox-exec unavailable")
        return
    old_home = os.environ.get("HOME")
    base = ROOT / f".goalflight-os-sandbox-agent-cwd-{os.getpid()}"
    fake_home = base / "home"
    workspace = fake_home / ".grok" / "checkout"
    shutil.rmtree(base, ignore_errors=True)
    workspace.mkdir(parents=True)
    try:
        os.environ["HOME"] = str(fake_home)
        try:
            prepare_os_sandbox_command(
                sys.executable,
                ["-c", "print('x')"],
                cwd=str(workspace),
                os_sandbox=OS_SANDBOX_READ_ONLY,
                agent="grok",
            )
        except OsSandboxError as e:
            assert "inside allowed agent state root" in str(e)
        else:
            raise AssertionError("cwd under agent state root should fail closed")
    finally:
        if old_home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = old_home
        shutil.rmtree(base, ignore_errors=True)


async def _run_sandbox_probe(profile: str) -> dict:
    old_agent_command = goalflight_acp_run.agent_command
    old_adapters_dir = goalflight_adapter_readiness.ADAPTERS_DIR
    old_scenario = os.environ.get("GOALFLIGHT_FAKE_ACP_SCENARIO")
    os.environ["GOALFLIGHT_FAKE_ACP_SCENARIO"] = "sandbox_write_probe"
    goalflight_acp_run.agent_command = lambda agent: (sys.executable, [str(FAKE)])
    workspace = ROOT / f".goalflight-os-sandbox-run-{profile}-{os.getpid()}"
    shutil.rmtree(workspace, ignore_errors=True)
    workspace.mkdir()
    try:
        with tempfile.TemporaryDirectory(prefix="gf-os-sandbox-adapters-") as tmp:
            tmp_path = Path(tmp)
            goalflight_adapter_readiness.ADAPTERS_DIR = tmp_path
            _write_supported_adapter_manifest(tmp_path, "fake-sandbox")
            status_path = tmp_path / f"{profile}.status.json"
            dispatch_id = f"test-os-sandbox-{profile}-{os.getpid()}"
            return await goalflight_acp_run.run(
                argparse.Namespace(
                    agent="fake-sandbox",
                    cwd=str(workspace),
                    session_id=f"{dispatch_id}-session",
                    dispatch_id=dispatch_id,
                    prompt_id=None,
                    prompt=None,
                    prompt_text="probe writes",
                    mode="one-shot",
                    status_json=str(status_path),
                    idle_timeout=5.0,
                    heartbeat_interval=0.2,
                    wedge_samples=100,
                    max_tool_s=60.0,
                    max_quiet_s=60.0,
                    progress_stall_s=60.0,
                    liveness_profile="local_compute",
                    remote_turn_silence_s=None,
                    remote_turn_cancel_grace_s=0.0,
                    cpu_epsilon=0.1,
                    context_mode="enabled",
                    permission_mode="auto",
                    permission_dir=None,
                    permission_inline_timeout_s=None,
                    permission_user_timeout_s=None,
                    os_sandbox=profile,
                    json=True,
                )
            )
    finally:
        goalflight_acp_run.agent_command = old_agent_command
        goalflight_adapter_readiness.ADAPTERS_DIR = old_adapters_dir
        if old_scenario is None:
            os.environ.pop("GOALFLIGHT_FAKE_ACP_SCENARIO", None)
        else:
            os.environ["GOALFLIGHT_FAKE_ACP_SCENARIO"] = old_scenario
        shutil.rmtree(workspace, ignore_errors=True)


def case_runner_workspace_write_blocks_home_write() -> None:
    if not _sandbox_available():
        print("SKIP: sandbox-exec unavailable")
        return
    payload = asyncio.run(_run_sandbox_probe(OS_SANDBOX_WORKSPACE_WRITE))
    assert payload["state"] == "complete", payload
    assert payload["ok"] is True, payload
    assert payload["os_sandbox"]["profile"] == OS_SANDBOX_WORKSPACE_WRITE
    assert "inside_write=true" in payload["text_excerpt"], payload["text_excerpt"]
    assert "outside_write=false" in payload["text_excerpt"], payload["text_excerpt"]


def case_runner_read_only_blocks_workspace_write() -> None:
    if not _sandbox_available():
        print("SKIP: sandbox-exec unavailable")
        return
    payload = asyncio.run(_run_sandbox_probe(OS_SANDBOX_READ_ONLY))
    assert payload["state"] == "complete", payload
    assert payload["ok"] is True, payload
    assert payload["os_sandbox"]["profile"] == OS_SANDBOX_READ_ONLY
    assert "inside_write=false" in payload["text_excerpt"], payload["text_excerpt"]
    assert "outside_write=false" in payload["text_excerpt"], payload["text_excerpt"]


def case_runner_blocks_undeclared_os_sandbox_before_capacity() -> None:
    old_agent_command = goalflight_acp_run.agent_command
    old_adapters_dir = goalflight_adapter_readiness.ADAPTERS_DIR
    goalflight_acp_run.agent_command = lambda agent: (sys.executable, [str(FAKE)])
    try:
        with tempfile.TemporaryDirectory(prefix="gf-os-sandbox-unsupported-") as tmp:
            tmp_path = Path(tmp)
            goalflight_adapter_readiness.ADAPTERS_DIR = tmp_path
            _write_supported_adapter_manifest(tmp_path, "fake-no-sandbox")
            manifest = json.loads((tmp_path / "fake-no-sandbox.json").read_text())
            manifest["permission_surface"]["os_sandbox"]["supported_profiles"] = ["off"]
            manifest["permission_surface"]["os_sandbox"]["default_profile"] = "off"
            (tmp_path / "fake-no-sandbox.json").write_text(json.dumps(manifest))
            status_path = tmp_path / "status.json"
            payload = asyncio.run(goalflight_acp_run.run(
                argparse.Namespace(
                    agent="fake-no-sandbox",
                    cwd=str(ROOT),
                    session_id="unsupported-os-sandbox",
                    dispatch_id=f"test-unsupported-os-sandbox-{os.getpid()}",
                    prompt_id=None,
                    prompt=None,
                    prompt_text="probe",
                    mode="one-shot",
                    status_json=str(status_path),
                    idle_timeout=5.0,
                    heartbeat_interval=0.2,
                    wedge_samples=100,
                    max_tool_s=60.0,
                    max_quiet_s=60.0,
                    progress_stall_s=60.0,
                    liveness_profile="local_compute",
                    remote_turn_silence_s=None,
                    remote_turn_cancel_grace_s=0.0,
                    cpu_epsilon=0.1,
                    context_mode="enabled",
                    permission_mode="auto",
                    permission_dir=None,
                    permission_inline_timeout_s=None,
                    permission_user_timeout_s=None,
                    os_sandbox=OS_SANDBOX_WORKSPACE_WRITE,
                    json=True,
                )
            ))
            assert payload["state"] == "blocked_os_sandbox", payload
            assert payload["lease_id"] is None, payload
            assert payload["worker_pid"] is None, payload
    finally:
        goalflight_acp_run.agent_command = old_agent_command
        goalflight_adapter_readiness.ADAPTERS_DIR = old_adapters_dir


def case_runner_blocks_temp_cwd_before_capacity() -> None:
    if not _sandbox_available():
        print("SKIP: sandbox-exec unavailable")
        return
    old_agent_command = goalflight_acp_run.agent_command
    old_adapters_dir = goalflight_adapter_readiness.ADAPTERS_DIR
    goalflight_acp_run.agent_command = lambda agent: (sys.executable, [str(FAKE)])
    try:
        with tempfile.TemporaryDirectory(prefix="gf-os-sandbox-temp-run-") as cwd:
            with tempfile.TemporaryDirectory(prefix="gf-os-sandbox-adapters-") as tmp:
                tmp_path = Path(tmp)
                goalflight_adapter_readiness.ADAPTERS_DIR = tmp_path
                _write_supported_adapter_manifest(tmp_path, "fake-sandbox")
                status_path = tmp_path / "status.json"
                payload = asyncio.run(goalflight_acp_run.run(
                    argparse.Namespace(
                        agent="fake-sandbox",
                        cwd=cwd,
                        session_id="temp-cwd-os-sandbox",
                        dispatch_id=f"test-temp-cwd-os-sandbox-{os.getpid()}",
                        prompt_id=None,
                        prompt=None,
                        prompt_text="probe",
                        mode="one-shot",
                        status_json=str(status_path),
                        idle_timeout=5.0,
                        heartbeat_interval=0.2,
                        wedge_samples=100,
                        max_tool_s=60.0,
                        max_quiet_s=60.0,
                        progress_stall_s=60.0,
                        liveness_profile="local_compute",
                        remote_turn_silence_s=None,
                        remote_turn_cancel_grace_s=0.0,
                        cpu_epsilon=0.1,
                        context_mode="enabled",
                        permission_mode="auto",
                        permission_dir=None,
                        permission_inline_timeout_s=None,
                        permission_user_timeout_s=None,
                        os_sandbox=OS_SANDBOX_READ_ONLY,
                        json=True,
                    )
                ))
                assert payload["state"] == "blocked_os_sandbox", payload
                assert payload["lease_id"] is None, payload
                assert payload["worker_pid"] is None, payload
                assert "inside allowed temp root" in str(payload["error"]), payload
    finally:
        goalflight_acp_run.agent_command = old_agent_command
        goalflight_adapter_readiness.ADAPTERS_DIR = old_adapters_dir


def case_pool_canonicalizes_os_sandbox_alias_for_reuse() -> None:
    async def _run() -> None:
        old_scenario = os.environ.get("GOALFLIGHT_FAKE_ACP_SCENARIO")
        os.environ["GOALFLIGHT_FAKE_ACP_SCENARIO"] = "echo"
        pool = AcpProcessPool(
            {"fake": {"command": sys.executable, "acp_args": [str(FAKE)]}},
            max_processes=2,
            auto_allow_tools=True,
            os_sandbox="host-default",
        )
        try:
            c1 = await pool.get_or_create("fake", "s", cwd=str(ROOT))
            c2 = await pool.get_or_create("fake", "s", cwd=str(ROOT), os_sandbox="none")
            assert c1 is c2
            assert c1.os_sandbox == OS_SANDBOX_OFF
        finally:
            await pool.shutdown()
            if old_scenario is None:
                os.environ.pop("GOALFLIGHT_FAKE_ACP_SCENARIO", None)
            else:
                os.environ["GOALFLIGHT_FAKE_ACP_SCENARIO"] = old_scenario

    asyncio.run(_run())


def case_pool_blocks_undeclared_os_sandbox() -> None:
    async def _run() -> None:
        old_adapters_dir = goalflight_adapter_readiness.ADAPTERS_DIR
        old_scenario = os.environ.get("GOALFLIGHT_FAKE_ACP_SCENARIO")
        os.environ["GOALFLIGHT_FAKE_ACP_SCENARIO"] = "echo"
        try:
            with tempfile.TemporaryDirectory(prefix="gf-os-sandbox-pool-unsupported-") as tmp:
                tmp_path = Path(tmp)
                goalflight_adapter_readiness.ADAPTERS_DIR = tmp_path
                _write_supported_adapter_manifest(tmp_path, "fake")
                manifest = json.loads((tmp_path / "fake.json").read_text())
                manifest["permission_surface"]["os_sandbox"]["supported_profiles"] = ["off"]
                manifest["permission_surface"]["os_sandbox"]["default_profile"] = "off"
                (tmp_path / "fake.json").write_text(json.dumps(manifest))
                pool = AcpProcessPool(
                    {"fake": {"command": sys.executable, "acp_args": [str(FAKE)]}},
                    max_processes=2,
                    auto_allow_tools=True,
                    os_sandbox=OS_SANDBOX_READ_ONLY,
                )
                try:
                    try:
                        await pool.get_or_create("fake", "s", cwd=str(ROOT))
                    except AcpError as e:
                        assert "os_sandbox_unsupported" in str(e)
                    else:
                        raise AssertionError("pool accepted undeclared OS sandbox profile")
                finally:
                    await pool.shutdown()
        finally:
            goalflight_adapter_readiness.ADAPTERS_DIR = old_adapters_dir
            if old_scenario is None:
                os.environ.pop("GOALFLIGHT_FAKE_ACP_SCENARIO", None)
            else:
                os.environ["GOALFLIGHT_FAKE_ACP_SCENARIO"] = old_scenario

    asyncio.run(_run())


def case_pool_blocks_alias_undeclared_os_sandbox() -> None:
    async def _run() -> None:
        old_adapters_dir = goalflight_adapter_readiness.ADAPTERS_DIR
        old_scenario = os.environ.get("GOALFLIGHT_FAKE_ACP_SCENARIO")
        os.environ["GOALFLIGHT_FAKE_ACP_SCENARIO"] = "echo"
        try:
            with tempfile.TemporaryDirectory(prefix="gf-os-sandbox-pool-alias-") as tmp:
                tmp_path = Path(tmp)
                goalflight_adapter_readiness.ADAPTERS_DIR = tmp_path
                _write_supported_adapter_manifest(tmp_path, "codex")
                manifest = json.loads((tmp_path / "codex.json").read_text())
                manifest["permission_surface"]["os_sandbox"]["supported_profiles"] = ["off"]
                manifest["permission_surface"]["os_sandbox"]["default_profile"] = "off"
                (tmp_path / "codex.json").write_text(json.dumps(manifest))
                pool = AcpProcessPool(
                    {"codex-acp": {"command": sys.executable, "acp_args": [str(FAKE)]}},
                    max_processes=2,
                    auto_allow_tools=True,
                    os_sandbox=OS_SANDBOX_READ_ONLY,
                )
                try:
                    try:
                        await pool.get_or_create("codex-acp", "s", cwd=str(ROOT))
                    except AcpError as e:
                        assert "os_sandbox_unsupported" in str(e)
                    else:
                        raise AssertionError("pool alias accepted undeclared OS sandbox profile")
                finally:
                    await pool.shutdown()
        finally:
            goalflight_adapter_readiness.ADAPTERS_DIR = old_adapters_dir
            if old_scenario is None:
                os.environ.pop("GOALFLIGHT_FAKE_ACP_SCENARIO", None)
            else:
                os.environ["GOALFLIGHT_FAKE_ACP_SCENARIO"] = old_scenario

    asyncio.run(_run())


def main() -> None:
    case_canonical_profiles()
    case_requested_sandbox_fails_closed_on_unsupported_hosts()
    case_prepare_wrapper_blocks_home_write()
    case_profile_string_escapes_workspace_path()
    case_rejects_cwd_under_temp_root()
    case_agent_state_roots_are_explicit_exception()
    case_rejects_cwd_under_agent_state_root()
    case_runner_workspace_write_blocks_home_write()
    case_runner_read_only_blocks_workspace_write()
    case_runner_blocks_undeclared_os_sandbox_before_capacity()
    case_runner_blocks_temp_cwd_before_capacity()
    case_pool_canonicalizes_os_sandbox_alias_for_reuse()
    case_pool_blocks_undeclared_os_sandbox()
    case_pool_blocks_alias_undeclared_os_sandbox()
    print("OK: OS sandbox tests pass")


if __name__ == "__main__":
    main()
