#!/usr/bin/env python3
"""OS sandbox dispatch tests."""

from __future__ import annotations

from support import note_skip, skip_case_posix_on_native_windows

import argparse
import asyncio
import contextlib
import io
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import time


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import goalflight_acp_run  # noqa: E402
import goalflight_adapter_readiness  # noqa: E402
import goalflight_dispatch  # noqa: E402
import goalflight_journal  # noqa: E402
import goalflight_os_sandbox as goalflight_os_sandbox_mod  # noqa: E402
from goalflight_acp_client import (  # noqa: E402
    AcpError,
    AcpProcessPool,
    permission_policy_for_dispatch,
)
from goalflight_os_sandbox import (  # noqa: E402
    OS_SANDBOX_OFF,
    OS_SANDBOX_READ_ONLY,
    OS_SANDBOX_WORKSPACE_WRITE,
    OsSandboxError,
    canonical_os_sandbox,
    preflight_os_sandbox,
    prepare_os_sandbox_command,
)


FAKE = ROOT / "tests/fixtures/acp_fake_agent.py"


def _sandbox_available() -> bool:
    if platform.system() != "Darwin" or shutil.which("sandbox-exec") is None:
        return False
    try:
        result = subprocess.run(
            ["sandbox-exec", "-p", "(version 1)\n(allow default)", "/usr/bin/true"],
            text=True,
            capture_output=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def _skip_unless_posix_shell_case(case_name: str) -> bool:
    return skip_case_posix_on_native_windows(case_name, "requires POSIX shell execution")


def _skip_unless_sandbox_exec_case(case_name: str) -> bool:
    if _sandbox_available():
        return False
    note_skip(case_name, "sandbox-exec unavailable")
    return True


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


def case_adapter_os_sandbox_is_platform_scoped() -> None:
    old_system = goalflight_os_sandbox_mod.platform.system
    old_adapters_dir = goalflight_adapter_readiness.ADAPTERS_DIR
    try:
        with tempfile.TemporaryDirectory(prefix="gf-os-sandbox-platform-") as tmp:
            tmp_path = Path(tmp)
            goalflight_adapter_readiness.ADAPTERS_DIR = tmp_path
            _write_supported_adapter_manifest(tmp_path, "fake-sandbox")
            manifest = json.loads((tmp_path / "fake-sandbox.json").read_text())
            manifest["permission_surface"]["os_sandbox"]["platform_supported_profiles"] = {
                "darwin": ["off", "read-only", "workspace-write"],
                "linux": ["off"],
                "wsl": ["off"],
                "windows": ["off"],
            }
            (tmp_path / "fake-sandbox.json").write_text(json.dumps(manifest))

            goalflight_os_sandbox_mod.platform.system = lambda: "Linux"
            blocked = goalflight_adapter_readiness.validate_os_sandbox_request(
                "fake-sandbox", OS_SANDBOX_READ_ONLY
            )
            assert blocked is not None
            assert blocked["reason"] == "os_sandbox_platform_unsupported", blocked
            assert blocked["supported_profiles"] == ["off"], blocked

            goalflight_os_sandbox_mod.platform.system = lambda: "Darwin"
            assert goalflight_adapter_readiness.validate_os_sandbox_request(
                "fake-sandbox", OS_SANDBOX_READ_ONLY
            ) is None
    finally:
        goalflight_os_sandbox_mod.platform.system = old_system
        goalflight_adapter_readiness.ADAPTERS_DIR = old_adapters_dir


def _supporting_os_sandbox_manifest() -> dict:
    return {
        "agent_id": "grok",
        "permission_surface": {
            "os_sandbox": {
                "supported_profiles": ["off", "read-only", "workspace-write"],
                "default_profile": "off",
                "implementation": "runner:sandbox-exec",
                "platform_supported_profiles": {
                    "darwin": ["off", "read-only", "workspace-write"],
                    "linux": ["off"],
                    "wsl": ["off"],
                    "windows": ["off"],
                },
            }
        },
    }


def case_os_sandbox_request_distinguishes_manifest_read_failures() -> None:
    """Unreadable/invalid are retryable; missing file and missing capability are not.

    Classification must run the real reader against real bytes and modes, not a
    pre-made verdict handed to the gate.
    """
    old_adapters_dir = goalflight_adapter_readiness.ADAPTERS_DIR
    try:
        with tempfile.TemporaryDirectory(prefix="gf-os-sandbox-read-") as tmp:
            tmp_path = Path(tmp)
            goalflight_adapter_readiness.ADAPTERS_DIR = tmp_path

            missing, missing_reason = goalflight_adapter_readiness.load_manifest_with_reason(
                "grok-acp"
            )
            assert missing is None, missing
            assert missing_reason == "adapter_manifest_missing", missing_reason
            missing_gate = goalflight_adapter_readiness.validate_os_sandbox_request(
                "grok-acp", OS_SANDBOX_READ_ONLY
            )
            assert missing_gate is not None
            assert missing_gate["reason"] == "adapter_manifest_missing", missing_gate
            assert missing_gate["retryable"] is False, missing_gate
            assert not goalflight_adapter_readiness.os_sandbox_refusal_is_retryable(
                missing_gate
            )

            invalid_path = tmp_path / "grok.json"
            invalid_path.write_bytes(b"{not-json")
            invalid, invalid_reason = goalflight_adapter_readiness.load_manifest_with_reason(
                "grok-acp"
            )
            assert invalid is None, invalid
            assert invalid_reason == "adapter_manifest_invalid", invalid_reason
            invalid_gate = goalflight_adapter_readiness.validate_os_sandbox_request(
                "grok-acp", OS_SANDBOX_READ_ONLY
            )
            assert invalid_gate is not None
            assert invalid_gate["reason"] == "adapter_manifest_invalid", invalid_gate
            assert invalid_gate["retryable"] is True, invalid_gate
            assert goalflight_adapter_readiness.os_sandbox_refusal_is_retryable(
                invalid_gate
            )
            invalid_path.unlink()

            (tmp_path / "grok.json").write_text(
                json.dumps({"agent_id": "grok", "permission_surface": {}})
            )
            undeclared = goalflight_adapter_readiness.validate_os_sandbox_request(
                "grok-acp", OS_SANDBOX_READ_ONLY
            )
            assert undeclared is not None
            assert undeclared["reason"] == "os_sandbox_undeclared", undeclared
            assert undeclared["retryable"] is False, undeclared
            assert not goalflight_adapter_readiness.os_sandbox_refusal_is_retryable(
                undeclared
            )

            (tmp_path / "grok.json").write_text(
                json.dumps(
                    {
                        "agent_id": "grok",
                        "permission_surface": {
                            "os_sandbox": {
                                "supported_profiles": ["off"],
                                "default_profile": "off",
                            }
                        },
                    }
                )
            )
            unsupported = goalflight_adapter_readiness.validate_os_sandbox_request(
                "grok-acp", OS_SANDBOX_READ_ONLY
            )
            assert unsupported is not None
            assert unsupported["reason"] == "os_sandbox_unsupported", unsupported
            assert unsupported["retryable"] is False, unsupported
            assert unsupported["supported_profiles"] == ["off"], unsupported

            readable = tmp_path / "grok.json"
            readable.write_text(json.dumps(_supporting_os_sandbox_manifest()))
            readable.chmod(0)
            try:
                unread, unread_reason = (
                    goalflight_adapter_readiness.load_manifest_with_reason("grok-acp")
                )
                assert unread is None, unread
                assert unread_reason == "adapter_manifest_unreadable", unread_reason
                unread_gate = goalflight_adapter_readiness.validate_os_sandbox_request(
                    "grok-acp", OS_SANDBOX_READ_ONLY
                )
                assert unread_gate is not None
                assert unread_gate["reason"] == "adapter_manifest_unreadable", unread_gate
                assert unread_gate["retryable"] is True, unread_gate
                assert goalflight_adapter_readiness.os_sandbox_refusal_is_retryable(
                    unread_gate
                )
            finally:
                readable.chmod(0o644)

            restored = goalflight_adapter_readiness.validate_os_sandbox_request(
                "grok-acp", OS_SANDBOX_READ_ONLY
            )
            assert not goalflight_adapter_readiness.os_sandbox_refusal_is_retryable(
                restored
            ), restored
    finally:
        goalflight_adapter_readiness.ADAPTERS_DIR = old_adapters_dir


def case_repo_runner_sandbox_adapters_are_platform_scoped() -> None:
    expected = {
        "darwin": ["off", "read-only", "workspace-write"],
        "linux": ["off"],
        "wsl": ["off"],
        "windows": ["off"],
    }
    for path in sorted((ROOT / "adapters").glob("*.json")):
        manifest = json.loads(path.read_text(encoding="utf-8"))
        os_sandbox = manifest.get("permission_surface", {}).get("os_sandbox", {})
        if os_sandbox.get("implementation") != "runner:sandbox-exec":
            continue
        if os_sandbox.get("supported_profiles") == ["off"]:
            continue
        assert os_sandbox.get("platform_supported_profiles") == expected, path.name


def case_dispatch_acp_cfg_preserves_existing_codex_platform_behavior() -> None:
    old_is_macos = goalflight_dispatch.goalflight_compat.is_macos
    args = argparse.Namespace(
        agent="codex-acp",
        cwd=str(ROOT),
        prompt_file=None,
        prompt="probe",
        dispatch_id="linux-sandbox-off",
        read_only=True,
        permission_mode="auto",
        permission_dir=None,
        permission_inline_timeout_s=None,
        permission_user_timeout_s=None,
        max_idle_secs=30.0,
        poll_secs=0.2,
    )
    try:
        goalflight_dispatch.goalflight_compat.is_macos = lambda: False
        linux_cfg = goalflight_dispatch._build_acp_cfg(args, status_json=ROOT / "status.json")
        assert linux_cfg.os_sandbox == OS_SANDBOX_OFF

        goalflight_dispatch.goalflight_compat.is_macos = lambda: True
        darwin_cfg = goalflight_dispatch._build_acp_cfg(args, status_json=ROOT / "status.json")
        assert darwin_cfg.os_sandbox == OS_SANDBOX_READ_ONLY
    finally:
        goalflight_dispatch.goalflight_compat.is_macos = old_is_macos


def case_claude_read_only_requests_profile_on_unsupported_platform() -> None:
    old_is_macos = goalflight_dispatch.goalflight_compat.is_macos
    args = argparse.Namespace(
        agent="claude",
        cwd=str(ROOT),
        prompt_file=None,
        prompt="probe",
        dispatch_id="claude-read-only-fallback",
        read_only=True,
        permission_mode="auto",
        permission_dir=None,
        permission_inline_timeout_s=None,
        permission_user_timeout_s=None,
        max_idle_secs=30.0,
        poll_secs=0.2,
    )
    try:
        goalflight_dispatch.goalflight_compat.is_macos = lambda: False
        cfg = goalflight_dispatch._build_acp_cfg(args, status_json=ROOT / "status.json")
        assert cfg.os_sandbox == OS_SANDBOX_READ_ONLY
        assert cfg.read_only is True
    finally:
        goalflight_dispatch.goalflight_compat.is_macos = old_is_macos


def _dispatch_shell_argv_for_platform(system_name: str) -> list[str]:
    run_dir: Path | None = None
    with tempfile.TemporaryDirectory(prefix="gf-dispatch-wrapper-") as tmp:
        tmp_path = Path(tmp)
        prompt = tmp_path / "prompt.md"
        prompt.write_text("STATUS: probe\n", encoding="utf-8")
        argv_path = tmp_path / "argv.txt"
        fakebin = tmp_path / "bin"
        fakebin.mkdir()
        uname = fakebin / "uname"
        uname.write_text(
            "#!/usr/bin/env sh\nprintf '%s\\n' \"$GF_FAKE_UNAME\"\n",
            encoding="utf-8",
        )
        fake_python = fakebin / "python3"
        fake_python.write_text(
            "#!/usr/bin/env sh\nprintf '%s\\n' \"$@\" > \"$GF_ARGV_OUT\"\n",
            encoding="utf-8",
        )
        uname.chmod(0o755)
        fake_python.chmod(0o755)
        env = os.environ.copy()
        env["PATH"] = f"{fakebin}{os.pathsep}{env.get('PATH', '')}"
        env["GF_FAKE_UNAME"] = system_name
        env["GF_ARGV_OUT"] = str(argv_path)
        try:
            result = subprocess.run(
                [
                    "bash",
                    str(ROOT / "scripts" / "goalflight_dispatch.sh"),
                    str(prompt),
                    "--slug",
                    f"wrapper-{system_name.lower()}",
                ],
                cwd=str(ROOT),
                env=env,
                text=True,
                capture_output=True,
                timeout=10,
                check=False,
            )
            for line in result.stdout.splitlines():
                if line.startswith("status-path: "):
                    run_dir = Path(line.split(": ", 1)[1]).parent
                    break
            assert result.returncode == 0, result.stderr
            for _ in range(100):
                if argv_path.exists():
                    return argv_path.read_text(encoding="utf-8").splitlines()
                time.sleep(0.02)
            raise AssertionError(f"fake python did not capture argv: {result}")
        finally:
            if run_dir is not None:
                shutil.rmtree(run_dir, ignore_errors=True)


def case_shell_wrapper_guards_os_sandbox_to_darwin() -> None:
    if _skip_unless_posix_shell_case("case_shell_wrapper_guards_os_sandbox_to_darwin"):
        return
    text = (ROOT / "scripts" / "goalflight_dispatch.sh").read_text(encoding="utf-8")
    assert "os_sandbox_args=()" in text
    assert "permission_allow_args=()" in text
    assert 'uname -s' in text
    assert 'os_sandbox_args=(--os-sandbox workspace-write)' in text
    assert "permission_allow_args=(--permission-allow-tool-title-pattern '.*')" in text
    assert '${os_sandbox_args[@]+"${os_sandbox_args[@]}"}' in text
    assert '${permission_allow_args[@]+"${permission_allow_args[@]}"}' in text

    linux_argv = _dispatch_shell_argv_for_platform("Linux")
    assert "--os-sandbox" not in linux_argv, linux_argv
    assert "--permission-allow-tool-title-pattern" not in linux_argv, linux_argv

    darwin_argv = _dispatch_shell_argv_for_platform("Darwin")
    assert "--os-sandbox" in darwin_argv, darwin_argv
    assert "workspace-write" in darwin_argv, darwin_argv
    assert "--permission-allow-tool-title-pattern" in darwin_argv, darwin_argv
    assert ".*" in darwin_argv, darwin_argv


def case_prepare_wrapper_blocks_home_write() -> None:
    if _skip_unless_sandbox_exec_case("case_prepare_wrapper_blocks_home_write"):
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
    if _skip_unless_sandbox_exec_case("case_profile_string_escapes_workspace_path"):
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


def case_profile_grants_dev_null_write() -> None:
    # Regression (2026-05-28): the workspace-write sandbox restricted file-write*
    # to write_roots subpaths, so git's `2>/dev/null` redirect could not open the
    # device for write and 5+ codex-acp workers hit BLOCKED at the commit step.
    # /dev/null + /dev/zero must be write-allowed device literals under BOTH
    # profiles (safe data sink / zero source; not a /dev subpath grant). Hermetic:
    # macos_sandbox_profile only builds the policy string, so this runs on any
    # platform regardless of sandbox-exec availability.
    for profile in (OS_SANDBOX_READ_ONLY, OS_SANDBOX_WORKSPACE_WRITE):
        profile_text, _ = goalflight_os_sandbox_mod.macos_sandbox_profile(
            str(ROOT), profile
        )
        assert "(allow file-write*" in profile_text, (profile, profile_text)
        assert '(literal "/dev/null")' in profile_text, (profile, profile_text)
        assert '(literal "/dev/zero")' in profile_text, (profile, profile_text)
        # the device literals must sit inside the file-write* block, not after it
        write_block = profile_text.split("(allow file-write*", 1)[1]
        assert '(literal "/dev/null")' in write_block, (profile, profile_text)
        # scope lock: /dev must appear ONLY as exact literals, never as a
        # subpath grant. A `(subpath "/dev")` (or any `/dev/...` subpath) would
        # silently widen write access to the whole device tree (/dev/disk*,
        # /dev/mem, ...). This catches a future careless widen that the
        # presence checks above would NOT — they only assert /dev/null is there.
        assert '(subpath "/dev' not in profile_text, (profile, profile_text)


def case_rejects_cwd_under_temp_root() -> None:
    if _skip_unless_sandbox_exec_case("case_rejects_cwd_under_temp_root"):
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
    if _skip_unless_sandbox_exec_case("case_agent_state_roots_are_explicit_exception"):
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
    if _skip_unless_sandbox_exec_case("case_rejects_cwd_under_agent_state_root"):
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


@contextlib.contextmanager
def _isolated_journal_workspace():
    """Journal dir outside the sandboxed cwd and outside /tmp (already granted)."""
    base = ROOT / f".goalflight-os-sandbox-journal-{os.getpid()}"
    workspace = base / "workspace"
    journal_state = base / "journal-state"
    shutil.rmtree(base, ignore_errors=True)
    workspace.mkdir(parents=True)
    old = os.environ.get("GOALFLIGHT_JOURNAL_DIR")
    os.environ["GOALFLIGHT_JOURNAL_DIR"] = str(journal_state)
    try:
        opened = goalflight_journal.Journal.create(str(workspace))
        yield workspace, opened.path
    finally:
        if old is None:
            os.environ.pop("GOALFLIGHT_JOURNAL_DIR", None)
        else:
            os.environ["GOALFLIGHT_JOURNAL_DIR"] = old
        shutil.rmtree(base, ignore_errors=True)


def _sandboxed_journal_open(workspace: Path, journal_path: Path):
    code = (
        "import sys; "
        f"sys.path.insert(0, r'{ROOT / 'scripts'}'); "
        f"sys.path.insert(0, r'{ROOT}'); "
        "import goalflight_journal as gj; "
        f"opened = gj.Journal(r'{workspace}'); "
        "print('journal-ok'); "
        f"print(opened.path)"
    )
    prepared = prepare_os_sandbox_command(
        sys.executable,
        ["-c", code],
        cwd=str(workspace),
        os_sandbox=OS_SANDBOX_WORKSPACE_WRITE,
    )
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join(
        [str(ROOT / "scripts"), str(ROOT), env.get("PYTHONPATH", "")]
    )
    result = subprocess.run(
        [prepared.command, *prepared.args],
        cwd=str(workspace),
        text=True,
        capture_output=True,
        timeout=20,
        check=False,
        env=env,
    )
    return prepared, result


def case_journal_dir_is_not_a_write_root() -> None:
    """Watcher owns the RUNNING claim; the seatbelt must not grant the journal."""
    with _isolated_journal_workspace() as (workspace, journal_path):
        roots = goalflight_os_sandbox_mod.macos_write_roots(
            str(workspace), OS_SANDBOX_WORKSPACE_WRITE
        )
        journal_dir = str(journal_path.parent.resolve())
        journals_parent = str(Path(journal_dir).parent.resolve())
        resolved_roots = [Path(root).resolve() for root in roots]
        assert Path(journal_dir) not in resolved_roots, (journal_dir, roots)
        assert Path(journals_parent) not in resolved_roots, roots


def case_sandboxed_journal_open_is_denied() -> None:
    """Journal lock sits outside the workspace; workspace-write must deny it."""
    if _skip_unless_sandbox_exec_case("case_sandboxed_journal_open_is_denied"):
        return
    with _isolated_journal_workspace() as (workspace, journal_path):
        _prepared, result = _sandboxed_journal_open(workspace, journal_path)
        assert result.returncode != 0, result
        combined = result.stdout + result.stderr
        assert "journal-ok" not in result.stdout, result
        assert "PermissionError" in combined or "Operation not permitted" in combined, result


def case_sandboxed_launch_worker_cannot_lock_journal() -> None:
    """In-sandbox launch_worker is no longer a journal writer; the lock is denied."""
    if _skip_unless_sandbox_exec_case("case_sandboxed_launch_worker_cannot_lock_journal"):
        return
    launcher = ROOT / "scripts" / "goalflight_launch_worker.py"
    with _isolated_journal_workspace() as (workspace, _journal_path):
        prepared = prepare_os_sandbox_command(
            sys.executable,
            [
                str(launcher),
                "--project-root",
                str(workspace),
                "--attempt-id",
                "00000000-0000-4000-8000-000000000001",
                "--launch-token",
                "00000000-0000-4000-8000-000000000002",
                "--launch-epoch",
                "1",
                "--",
                "/usr/bin/true",
            ],
            cwd=str(workspace),
            os_sandbox=OS_SANDBOX_WORKSPACE_WRITE,
        )
        env = os.environ.copy()
        env["PYTHONPATH"] = os.pathsep.join(
            [str(ROOT / "scripts"), str(ROOT), env.get("PYTHONPATH", "")]
        )
        result = subprocess.run(
            [prepared.command, *prepared.args],
            cwd=str(workspace),
            text=True,
            capture_output=True,
            timeout=20,
            check=False,
            env=env,
        )
        combined = result.stdout + result.stderr
        assert result.returncode != 0, result
        assert "journal-ok" not in result.stdout, result
        assert "PermissionError" in combined or "Operation not permitted" in combined, result


def case_dispatch_help_exposes_title_allow_pattern() -> None:
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "goalflight_dispatch.py"), "--help"],
        cwd=str(ROOT),
        text=True,
        capture_output=True,
        timeout=20,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "--permission-allow-tool-title-pattern" in result.stdout, result.stdout


def case_dispatch_forwards_title_allow_pattern_to_acp_cfg() -> None:
    pattern = r"^\./tests/run\.sh$"
    old_state = os.environ.get("GOALFLIGHT_STATE_DIR")
    with tempfile.TemporaryDirectory(prefix="gf-title-allow-dispatch-") as tmp:
        tmp_path = Path(tmp)
        os.environ["GOALFLIGHT_STATE_DIR"] = str(tmp_path / "state")
        args = argparse.Namespace(
            agent="cursor",
            cwd=str(ROOT),
            prompt_file=None,
            prompt="probe",
            dispatch_id="title-allow-forward",
            read_only=False,
            os_sandbox="workspace-write",
            permission_mode="auto",
            permission_dir=None,
            permission_inline_timeout_s=None,
            permission_user_timeout_s=None,
            permission_allow_tool_title_pattern=[pattern],
            max_idle_secs=30.0,
            poll_secs=0.2,
        )
        try:
            cfg = goalflight_dispatch._build_acp_cfg(
                args, status_json=tmp_path / "status.json", base=tmp_path / "dispatch"
            )
            assert cfg.permission_allow_tool_title_pattern == [pattern], cfg
            empty = argparse.Namespace(**{**vars(args), "permission_allow_tool_title_pattern": []})
            empty_cfg = goalflight_dispatch._build_acp_cfg(
                empty, status_json=tmp_path / "empty.status.json", base=tmp_path / "dispatch"
            )
            assert empty_cfg.permission_allow_tool_title_pattern == [], empty_cfg
        finally:
            if old_state is None:
                os.environ.pop("GOALFLIGHT_STATE_DIR", None)
            else:
                os.environ["GOALFLIGHT_STATE_DIR"] = old_state


def case_dispatch_replay_keeps_title_allow_pattern() -> None:
    pattern = r"^\./tests/run\.sh$"
    args = argparse.Namespace(
        agent="cursor",
        dispatch_id="title-allow-replay",
        cwd=str(ROOT),
        shape="acp",
        priority="normal",
        billing="sub",
        poll_secs=2.0,
        max_idle_secs=600.0,
        prompt_file=None,
        prompt="hi",
        task_ids=[],
        model=None,
        os_sandbox="workspace-write",
        read_only=False,
        web_research_ok=False,
        web_qa=False,
        ignore_git_warn=False,
        no_orientation=False,
        capacity_wait_s=None,
        account=None,
        interactive=False,
        permission_mode="auto",
        permission_dir=None,
        permission_inline_timeout_s=None,
        permission_user_timeout_s=None,
        permission_allow_tool_title_pattern=[pattern],
        controller_pid=None,
        fast=False,
    )
    argv = goalflight_dispatch._canonical_replay_argv(
        args, [], tail=Path("/tmp/t"), status_json=Path("/tmp/s")
    )
    assert "--permission-allow-tool-title-pattern" in argv, argv
    assert pattern in argv, argv
    # No default pattern when the caller omitted the flag.
    omitted = argparse.Namespace(**{**vars(args), "permission_allow_tool_title_pattern": []})
    omitted_argv = goalflight_dispatch._canonical_replay_argv(
        omitted, [], tail=Path("/tmp/t"), status_json=Path("/tmp/s")
    )
    assert "--permission-allow-tool-title-pattern" not in omitted_argv, omitted_argv


def case_title_allow_from_dispatch_still_hard_gates() -> None:
    """Matching titles fast-path; execute/outside-cwd still escalate without sandbox."""
    pattern = re.compile(r"^\./tests/run\.sh$")
    off = goalflight_acp_run.make_title_allow_policy(
        [pattern], base=permission_policy_for_dispatch("off")
    )
    on = goalflight_acp_run.make_title_allow_policy(
        [pattern], base=permission_policy_for_dispatch("workspace-write")
    )
    cwd = str(ROOT)
    matching_execute = {
        "title": "./tests/run.sh",
        "kind": "execute",
        "locations": [],
    }
    matching_read = {
        "title": "./tests/run.sh",
        "kind": "read",
        "locations": [{"path": str(ROOT / "tests" / "run.sh")}],
    }
    outside = {
        "title": "./tests/run.sh",
        "kind": "edit",
        "locations": [{"path": "/etc/passwd"}],
    }
    assert off(matching_read, [], cwd) == "allow"
    assert off(matching_execute, [], cwd) == "escalate"
    assert off(outside, [], cwd) == "escalate"
    # Sandbox-aware base allows in-cwd execute after the hard-gate handoff.
    assert on(matching_execute, [], cwd) == "allow"
    assert on(outside, [], cwd) == "escalate"


def case_sandboxed_acp_handshake_with_isolated_journal() -> None:
    """ACP spawn under workspace-write must complete handshake when journal is off-tmp."""
    if _skip_unless_sandbox_exec_case("case_sandboxed_acp_handshake_with_isolated_journal"):
        return
    old_journal = os.environ.get("GOALFLIGHT_JOURNAL_DIR")
    old_steer = os.environ.pop("GOALFLIGHT_STEER_FILE", None)
    journal_state = ROOT / f".goalflight-os-sandbox-hs-journal-{os.getpid()}"
    shutil.rmtree(journal_state, ignore_errors=True)
    try:
        os.environ["GOALFLIGHT_JOURNAL_DIR"] = str(journal_state)
        goalflight_journal.Journal.create(str(ROOT))
        payload = asyncio.run(_run_sandbox_probe(OS_SANDBOX_WORKSPACE_WRITE))
        assert payload["state"] == "complete", payload
        assert payload.get("ok") is True, payload
        assert payload["os_sandbox"]["profile"] == OS_SANDBOX_WORKSPACE_WRITE
    finally:
        if old_journal is None:
            os.environ.pop("GOALFLIGHT_JOURNAL_DIR", None)
        else:
            os.environ["GOALFLIGHT_JOURNAL_DIR"] = old_journal
        if old_steer is None:
            os.environ.pop("GOALFLIGHT_STEER_FILE", None)
        else:
            os.environ["GOALFLIGHT_STEER_FILE"] = old_steer
        shutil.rmtree(journal_state, ignore_errors=True)


def case_broad_pattern_sandbox_off_warning_still_fires() -> None:
    """Startup warning must still fire; plumbing must not swallow it."""
    old_agent_command = goalflight_acp_run.agent_command
    old_adapters_dir = goalflight_adapter_readiness.ADAPTERS_DIR
    old_scenario = os.environ.get("GOALFLIGHT_FAKE_ACP_SCENARIO")
    old_state_dir = os.environ.get("GOALFLIGHT_STATE_DIR")
    old_journal = os.environ.get("GOALFLIGHT_JOURNAL_DIR")
    old_steer = os.environ.pop("GOALFLIGHT_STEER_FILE", None)
    os.environ["GOALFLIGHT_FAKE_ACP_SCENARIO"] = "echo"
    goalflight_acp_run.agent_command = (
        lambda agent, model=None, fast=False: (sys.executable, [str(FAKE)])
    )
    workspace = ROOT / f".goalflight-os-sandbox-warn-{os.getpid()}"
    shutil.rmtree(workspace, ignore_errors=True)
    workspace.mkdir()
    try:
        with tempfile.TemporaryDirectory(prefix="gf-os-sandbox-warn-") as tmp:
            tmp_path = Path(tmp)
            # Journal must sit outside /tmp so this is not accidentally
            # granted by the temp-root rule; create it next to the workspace.
            journal_state = workspace.parent / f".goalflight-os-sandbox-warn-journal-{os.getpid()}"
            shutil.rmtree(journal_state, ignore_errors=True)
            os.environ["GOALFLIGHT_JOURNAL_DIR"] = str(journal_state)
            goalflight_journal.Journal.create(str(workspace))
            goalflight_adapter_readiness.ADAPTERS_DIR = tmp_path
            os.environ["GOALFLIGHT_STATE_DIR"] = str(tmp_path / "state")
            _write_supported_adapter_manifest(tmp_path, "fake-sandbox")
            status_path = tmp_path / "status.json"
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                payload = asyncio.run(
                    goalflight_acp_run.run(
                        argparse.Namespace(
                            agent="fake-sandbox",
                            unregistered_forced=True,
                            cwd=str(workspace),
                            session_id="title-allow-warn-session",
                            dispatch_id=f"test-title-allow-warn-{os.getpid()}",
                            prompt_id=None,
                            prompt=None,
                            prompt_text="COMPLETE: warn probe",
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
                            context_mode="disabled",
                            permission_mode="auto",
                            permission_dir=None,
                            permission_inline_timeout_s=None,
                            permission_user_timeout_s=None,
                            permission_allow_tool_title_pattern=[".*"],
                            os_sandbox=OS_SANDBOX_OFF,
                            json=True,
                        )
                    )
                )
            text = stderr.getvalue()
            assert "WARNING — broad title-allow pattern" in text, text
            assert payload.get("state") in {"complete", "failed", "blocked_os_sandbox", "blocked_adapter_gate"}, payload
    finally:
        goalflight_acp_run.agent_command = old_agent_command
        goalflight_adapter_readiness.ADAPTERS_DIR = old_adapters_dir
        if old_scenario is None:
            os.environ.pop("GOALFLIGHT_FAKE_ACP_SCENARIO", None)
        else:
            os.environ["GOALFLIGHT_FAKE_ACP_SCENARIO"] = old_scenario
        if old_state_dir is None:
            os.environ.pop("GOALFLIGHT_STATE_DIR", None)
        else:
            os.environ["GOALFLIGHT_STATE_DIR"] = old_state_dir
        if old_journal is None:
            os.environ.pop("GOALFLIGHT_JOURNAL_DIR", None)
        else:
            os.environ["GOALFLIGHT_JOURNAL_DIR"] = old_journal
        if old_steer is None:
            os.environ.pop("GOALFLIGHT_STEER_FILE", None)
        else:
            os.environ["GOALFLIGHT_STEER_FILE"] = old_steer
        shutil.rmtree(workspace, ignore_errors=True)
        shutil.rmtree(
            ROOT / f".goalflight-os-sandbox-warn-journal-{os.getpid()}",
            ignore_errors=True,
        )


async def _run_sandbox_probe(profile: str) -> dict:
    old_agent_command = goalflight_acp_run.agent_command
    old_adapters_dir = goalflight_adapter_readiness.ADAPTERS_DIR
    old_scenario = os.environ.get("GOALFLIGHT_FAKE_ACP_SCENARIO")
    old_state_dir = os.environ.get("GOALFLIGHT_STATE_DIR")
    os.environ["GOALFLIGHT_FAKE_ACP_SCENARIO"] = "sandbox_write_probe"
    goalflight_acp_run.agent_command = lambda agent, model=None, fast=False: (sys.executable, [str(FAKE)])
    workspace = ROOT / f".goalflight-os-sandbox-run-{profile}-{os.getpid()}"
    shutil.rmtree(workspace, ignore_errors=True)
    workspace.mkdir()
    try:
        with tempfile.TemporaryDirectory(prefix="gf-os-sandbox-adapters-") as tmp:
            tmp_path = Path(tmp)
            goalflight_adapter_readiness.ADAPTERS_DIR = tmp_path
            os.environ["GOALFLIGHT_STATE_DIR"] = str(tmp_path / "state")
            _write_supported_adapter_manifest(tmp_path, "fake-sandbox")
            status_path = tmp_path / f"{profile}.status.json"
            dispatch_id = f"test-os-sandbox-{profile}-{os.getpid()}"
            return await goalflight_acp_run.run(
                argparse.Namespace(
                    agent="fake-sandbox",
                    unregistered_forced=True,
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
        if old_state_dir is None:
            os.environ.pop("GOALFLIGHT_STATE_DIR", None)
        else:
            os.environ["GOALFLIGHT_STATE_DIR"] = old_state_dir
        shutil.rmtree(workspace, ignore_errors=True)


def case_runner_workspace_write_blocks_home_write() -> None:
    if _skip_unless_sandbox_exec_case("case_runner_workspace_write_blocks_home_write"):
        return
    payload = asyncio.run(_run_sandbox_probe(OS_SANDBOX_WORKSPACE_WRITE))
    assert payload["state"] == "complete", payload
    assert payload["ok"] is True, payload
    assert payload["os_sandbox"]["profile"] == OS_SANDBOX_WORKSPACE_WRITE
    assert "sandbox_fallback" not in payload
    assert "inside_write=true" in payload["text_excerpt"], payload["text_excerpt"]
    assert "outside_write=false" in payload["text_excerpt"], payload["text_excerpt"]


def case_runner_read_only_blocks_workspace_write() -> None:
    if _skip_unless_sandbox_exec_case("case_runner_read_only_blocks_workspace_write"):
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
    old_state_dir = os.environ.get("GOALFLIGHT_STATE_DIR")
    goalflight_acp_run.agent_command = lambda agent, model=None, fast=False: (sys.executable, [str(FAKE)])
    try:
        with tempfile.TemporaryDirectory(prefix="gf-os-sandbox-unsupported-") as tmp:
            tmp_path = Path(tmp)
            goalflight_adapter_readiness.ADAPTERS_DIR = tmp_path
            os.environ["GOALFLIGHT_STATE_DIR"] = str(tmp_path / "state")
            _write_supported_adapter_manifest(tmp_path, "fake-no-sandbox")
            manifest = json.loads((tmp_path / "fake-no-sandbox.json").read_text())
            manifest["permission_surface"]["os_sandbox"]["supported_profiles"] = ["off"]
            manifest["permission_surface"]["os_sandbox"]["default_profile"] = "off"
            (tmp_path / "fake-no-sandbox.json").write_text(json.dumps(manifest))
            status_path = tmp_path / "status.json"
            payload = asyncio.run(goalflight_acp_run.run(
                argparse.Namespace(
                    agent="fake-no-sandbox",
                    unregistered_forced=True,
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
                    read_only=True,
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
        if old_state_dir is None:
            os.environ.pop("GOALFLIGHT_STATE_DIR", None)
        else:
            os.environ["GOALFLIGHT_STATE_DIR"] = old_state_dir


def case_runner_translates_claude_read_only_to_acp_permissions() -> None:
    old_agent_command = goalflight_acp_run.agent_command
    old_spawn = goalflight_acp_run.spawn_and_handshake_with_retry
    old_adapters_dir = goalflight_adapter_readiness.ADAPTERS_DIR
    old_state_dir = os.environ.get("GOALFLIGHT_STATE_DIR")
    old_scenario = os.environ.get("GOALFLIGHT_FAKE_ACP_SCENARIO")
    goalflight_acp_run.agent_command = (
        lambda agent, model=None, fast=False: (sys.executable, [str(FAKE)])
    )
    captured: dict[str, object] = {}

    async def capture_spawn(*args, **kwargs):
        captured["permission_policy"] = kwargs.get("permission_policy")
        captured["os_sandbox"] = kwargs.get("os_sandbox")
        return await old_spawn(*args, **kwargs)

    goalflight_acp_run.spawn_and_handshake_with_retry = capture_spawn
    try:
        with tempfile.TemporaryDirectory(prefix="gf-os-sandbox-fallback-") as tmp:
            tmp_path = Path(tmp)
            goalflight_adapter_readiness.ADAPTERS_DIR = tmp_path
            os.environ["GOALFLIGHT_STATE_DIR"] = str(tmp_path / "state")
            os.environ["GOALFLIGHT_FAKE_ACP_SCENARIO"] = "echo"
            _write_supported_adapter_manifest(tmp_path, "claude-code")
            manifest_path = tmp_path / "claude-code.json"
            manifest = json.loads(manifest_path.read_text())
            manifest["permission_surface"]["os_sandbox"].update(
                {
                    "supported_profiles": ["off"],
                    "default_profile": "off",
                    "implementation": "unsupported",
                }
            )
            manifest_path.write_text(json.dumps(manifest))
            status_path = tmp_path / "status.json"
            payload = asyncio.run(
                goalflight_acp_run.run(
                    argparse.Namespace(
                        agent="claude",
                        unregistered_forced=True,
                        cwd=str(ROOT),
                        session_id="claude-read-only-fallback",
                        dispatch_id=f"test-claude-read-only-fallback-{os.getpid()}",
                        prompt_id=None,
                        prompt=None,
                        prompt_text="COMPLETE: fallback probe",
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
                        context_mode="disabled",
                        permission_mode="auto",
                        permission_dir=None,
                        permission_inline_timeout_s=None,
                        permission_user_timeout_s=None,
                        permission_allow_tool_title_pattern=[],
                        read_only=True,
                        os_sandbox=OS_SANDBOX_READ_ONLY,
                        json=True,
                    )
                )
            )
            expected = {
                "requested": "read-only",
                "applied": "off",
                "enforcement": "acp-permissions",
            }
            assert payload["state"] == "complete", payload
            assert payload["sandbox_fallback"] == expected, payload
            assert json.loads(status_path.read_text())["sandbox_fallback"] == expected
            assert captured["os_sandbox"] == OS_SANDBOX_OFF, captured
            policy = captured["permission_policy"]
            assert callable(policy), captured
            assert policy(
                {"kind": "edit", "locations": [{"path": str(ROOT / "README.md")}]},
                [],
                str(ROOT),
            ) == "deny"

            no_flag_status = tmp_path / "no-flag.status.json"
            no_flag_payload = asyncio.run(
                goalflight_acp_run.run(
                    argparse.Namespace(
                        agent="claude",
                        unregistered_forced=True,
                        cwd=str(ROOT),
                        session_id="claude-no-sandbox-request",
                        dispatch_id=f"test-claude-no-sandbox-request-{os.getpid()}",
                        prompt_id=None,
                        prompt=None,
                        prompt_text="COMPLETE: no-flag probe",
                        mode="one-shot",
                        status_json=str(no_flag_status),
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
                        context_mode="disabled",
                        permission_mode="auto",
                        permission_dir=None,
                        permission_inline_timeout_s=None,
                        permission_user_timeout_s=None,
                        permission_allow_tool_title_pattern=[],
                        read_only=False,
                        os_sandbox=OS_SANDBOX_OFF,
                        json=True,
                    )
                )
            )
            assert no_flag_payload["state"] == "complete", no_flag_payload
            assert "sandbox_fallback" not in no_flag_payload
            assert json.loads(no_flag_status.read_text())["os_sandbox"] == {
                "requested": "off",
                "profile": "off",
                "enabled": False,
                "implementation": None,
                "write_roots": [],
            }, no_flag_payload["os_sandbox"]
    finally:
        goalflight_acp_run.agent_command = old_agent_command
        goalflight_acp_run.spawn_and_handshake_with_retry = old_spawn
        goalflight_adapter_readiness.ADAPTERS_DIR = old_adapters_dir
        if old_state_dir is None:
            os.environ.pop("GOALFLIGHT_STATE_DIR", None)
        else:
            os.environ["GOALFLIGHT_STATE_DIR"] = old_state_dir
        if old_scenario is None:
            os.environ.pop("GOALFLIGHT_FAKE_ACP_SCENARIO", None)
        else:
            os.environ["GOALFLIGHT_FAKE_ACP_SCENARIO"] = old_scenario


def case_runner_blocks_temp_cwd_before_capacity() -> None:
    if _skip_unless_sandbox_exec_case("case_runner_blocks_temp_cwd_before_capacity"):
        return
    old_agent_command = goalflight_acp_run.agent_command
    old_adapters_dir = goalflight_adapter_readiness.ADAPTERS_DIR
    old_state_dir = os.environ.get("GOALFLIGHT_STATE_DIR")
    goalflight_acp_run.agent_command = lambda agent, model=None, fast=False: (sys.executable, [str(FAKE)])
    try:
        with tempfile.TemporaryDirectory(prefix="gf-os-sandbox-temp-run-") as cwd:
            with tempfile.TemporaryDirectory(prefix="gf-os-sandbox-adapters-") as tmp:
                tmp_path = Path(tmp)
                goalflight_adapter_readiness.ADAPTERS_DIR = tmp_path
                os.environ["GOALFLIGHT_STATE_DIR"] = str(tmp_path / "state")
                _write_supported_adapter_manifest(tmp_path, "fake-sandbox")
                status_path = tmp_path / "status.json"
                payload = asyncio.run(goalflight_acp_run.run(
                    argparse.Namespace(
                        agent="fake-sandbox",
                        unregistered_forced=True,
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
        if old_state_dir is None:
            os.environ.pop("GOALFLIGHT_STATE_DIR", None)
        else:
            os.environ["GOALFLIGHT_STATE_DIR"] = old_state_dir


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
    case_adapter_os_sandbox_is_platform_scoped()
    case_os_sandbox_request_distinguishes_manifest_read_failures()
    case_repo_runner_sandbox_adapters_are_platform_scoped()
    case_dispatch_acp_cfg_preserves_existing_codex_platform_behavior()
    case_claude_read_only_requests_profile_on_unsupported_platform()
    case_shell_wrapper_guards_os_sandbox_to_darwin()
    case_prepare_wrapper_blocks_home_write()
    case_profile_string_escapes_workspace_path()
    case_profile_grants_dev_null_write()
    case_rejects_cwd_under_temp_root()
    case_agent_state_roots_are_explicit_exception()
    case_rejects_cwd_under_agent_state_root()
    case_journal_dir_is_not_a_write_root()
    case_sandboxed_journal_open_is_denied()
    case_sandboxed_launch_worker_cannot_lock_journal()
    case_sandboxed_acp_handshake_with_isolated_journal()
    case_dispatch_help_exposes_title_allow_pattern()
    case_dispatch_forwards_title_allow_pattern_to_acp_cfg()
    case_dispatch_replay_keeps_title_allow_pattern()
    case_title_allow_from_dispatch_still_hard_gates()
    case_broad_pattern_sandbox_off_warning_still_fires()
    case_runner_workspace_write_blocks_home_write()
    case_runner_read_only_blocks_workspace_write()
    case_runner_blocks_undeclared_os_sandbox_before_capacity()
    case_runner_translates_claude_read_only_to_acp_permissions()
    case_runner_blocks_temp_cwd_before_capacity()
    case_pool_canonicalizes_os_sandbox_alias_for_reuse()
    case_pool_blocks_undeclared_os_sandbox()
    case_pool_blocks_alias_undeclared_os_sandbox()
    print("OK: OS sandbox tests pass")


if __name__ == "__main__":
    main()
