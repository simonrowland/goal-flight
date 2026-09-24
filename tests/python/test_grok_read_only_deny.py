#!/usr/bin/env python3
"""A read-only grok dispatch must carry enforcement, not just a polite brief.

On macOS the dispatcher keeps Grok's shell available under the shared
``sandbox-exec`` profile; on other hosts it retains Grok's ``--deny Bash``
fallback. The live Grok tool probe is controller-only: this suite verifies the
argv and the profile with plain shell commands, not a model-consuming launch.
Controller follow-up must run the live probe with ``GOALFLIGHT_LIVE_GROK=1``;
it is intentionally absent from this worker's test run.

`--deny` rules are the surface the grok CLI actually honours. Probed on grok
1.0.0 with a write-a-file prompt: without deny rules the file was written; with
`--deny Write --deny Edit --deny Bash` the model tried the write tool, then a
shell command, then a relative path, and finally reported that writes were
blocked. The rules held against the model's own bypass attempts — which also
shows what an un-denied tool would have meant: the model reaches for the next
tool on its own initiative.

The broken `--permission-mode` flag (documented at length in
goalflight_dispatch.py, with its failure set MOVING between grok releases) is a
different surface and stays omitted; nothing here touches it.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import goalflight_dispatch as D  # noqa: E402
import goalflight_os_sandbox as S  # noqa: E402


def _args(**over):
    base = dict(
        agent="grok-code",
        shape="bash",
        model=None,
        cwd=None,
        read_only=False,
        os_sandbox=None,
        account=None,
        billing="sub",
        dispatch_id="t",
        parent_dispatch_id=None,
        engine_session_id=None,
        web_qa=False,
        web_research_ok=False,
        no_orientation=True,
        fast=False,
    )
    base.update(over)
    return SimpleNamespace(**base)


def _grok_argv(args) -> list[str]:
    with tempfile.TemporaryDirectory() as td:
        prompt = Path(td) / "prompt.md"
        prompt.write_text("Review the change.\n", encoding="utf-8")
        argv, _stdin = D.build_worker(args, prompt, [])
    return argv


def _deny_values(argv: list[str]) -> list[str]:
    return [argv[i + 1] for i, tok in enumerate(argv[:-1]) if tok == "--deny"]


def test_read_only_grok_denies_every_write_capable_tool() -> None:
    denied = _deny_values(_grok_argv(_args(read_only=True)))
    # Write/Edit stay Grok-level denies everywhere. Bash is denied only where
    # the shared macOS profile cannot be used; on macOS shell writes are
    # rejected by Seatbelt instead.
    assert set(denied) >= {"Write", "Edit"}, denied
    if D.goalflight_compat.is_macos():
        argv = _grok_argv(_args(read_only=True))
        assert Path(argv[0]).name == "sandbox-exec", argv
        assert "Bash" not in denied, denied
    else:
        assert "Bash" in denied, denied


def test_macos_read_only_grok_wraps_profile_without_bash_deny() -> None:
    account_home = Path.home() / ".goal-flight" / "accounts" / "probe" / "grok"
    args = _args(
        read_only=True,
        cwd=str(ROOT),
        _account_env={
            "HOME": str(account_home),
            "XDG_CONFIG_HOME": str(account_home / ".config"),
            "XDG_STATE_HOME": str(account_home / ".local" / "state"),
            "XDG_DATA_HOME": str(account_home / ".local" / "share"),
        },
    )
    with (
        mock.patch.object(D.goalflight_compat, "is_macos", return_value=True),
        mock.patch.object(S, "preflight_os_sandbox", return_value="read-only"),
        mock.patch.object(S.shutil, "which", return_value="/usr/bin/sandbox-exec"),
    ):
        argv = _grok_argv(args)
    assert Path(argv[0]).name == "sandbox-exec", argv
    denied = _deny_values(argv)
    assert set(denied) >= {"Write", "Edit"}, denied
    assert "Bash" not in denied, denied
    profile = argv[argv.index("-p") + 1]
    assert f'(subpath "{ROOT}")' not in profile


def test_writable_grok_carries_no_deny_rules() -> None:
    """The default write dispatch must be untouched.

    The measured history in goalflight_dispatch.py shows grok permission flags
    causing silent no-op workers; enforcement must therefore be scoped to
    dispatches that ASKED for read-only, never ambient.
    """
    argv = _grok_argv(_args(read_only=False))
    assert "--deny" not in argv, argv


def test_read_only_enforcement_is_scoped_to_grok() -> None:
    """codex read-only goes through its own sandbox; no grok flags may leak."""
    with tempfile.TemporaryDirectory() as td:
        prompt = Path(td) / "prompt.md"
        prompt.write_text("Review.\n", encoding="utf-8")
        args = _args(agent="codex", read_only=True, os_sandbox="read-only")
        argv, _ = D.build_worker(args, prompt, [])
    assert "--deny" not in argv, argv


def test_read_only_git_disables_optional_index_locks() -> None:
    read_only_env: dict[str, str] = {}
    D._apply_read_only_env(read_only_env, _args(read_only=True))
    assert read_only_env == {"GIT_OPTIONAL_LOCKS": "0"}

    writable_env: dict[str, str] = {}
    D._apply_read_only_env(writable_env, _args(read_only=False))
    assert writable_env == {}


def test_grok_read_only_profile_fences_project_and_allows_account_state() -> None:
    account_home = Path.home() / ".goal-flight" / "accounts" / "probe" / "grok"
    environment = {
        "HOME": str(account_home),
        "XDG_CONFIG_HOME": str(account_home / ".config"),
        "XDG_STATE_HOME": str(account_home / ".local" / "state"),
        "XDG_DATA_HOME": str(account_home / ".local" / "share"),
    }
    profile, roots = S.macos_sandbox_profile(
        str(ROOT),
        S.OS_SANDBOX_READ_ONLY,
        agent="grok-code",
        command="grok",
        environment=environment,
    )
    expected = {
        account_home / ".grok",
        account_home / ".goal-flight",
        account_home / ".config" / "grok",
        account_home / ".local" / "state" / "goal-flight",
        account_home / ".local" / "share" / "grok",
        account_home / ".cache" / "grok",
    }
    assert expected <= {Path(root) for root in roots}, roots
    assert f'(subpath "{ROOT}")' not in profile
    scripts_dir = ROOT / "scripts"
    assert f'(subpath "{scripts_dir}")' not in profile
    for root in expected:
        assert f'(subpath "{root}")' in profile, root


def _sandbox_exec_available() -> bool:
    if not D.goalflight_compat.is_macos() or shutil.which("sandbox-exec") is None:
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


def test_grok_read_only_profile_blocks_shell_writes_but_allows_git_show() -> None:
    """Designed-red: shell writes fail while repository inspection succeeds."""
    if not _sandbox_exec_available():
        # The controller must run the live Grok probe; this plain-shell profile
        # probe is skipped when the enclosing sandbox blocks sandbox-exec.
        import pytest

        pytest.skip("sandbox-exec unavailable or blocked in this worker sandbox")

    probe_dir = ROOT / f".goalflight-grok-read-only-probe-{os.getpid()}"
    shutil.rmtree(probe_dir, ignore_errors=True)
    probe_dir.mkdir()

    def run(command: str, args: list[str]) -> subprocess.CompletedProcess[str]:
        prepared = S.prepare_os_sandbox_command(
            command,
            args,
            cwd=str(probe_dir),
            os_sandbox=S.OS_SANDBOX_READ_ONLY,
            agent="grok-code",
        )
        return subprocess.run(
            [prepared.command, *prepared.args],
            cwd=str(probe_dir),
            env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"},
            text=True,
            capture_output=True,
            timeout=10,
            check=False,
        )

    try:
        shell_write = run("/bin/sh", ["-c", "echo x > f"])
        assert shell_write.returncode != 0, shell_write
        assert not (probe_dir / "f").exists(), shell_write

        python_write = run(
            sys.executable,
            ["-c", "open('f', 'w', encoding='utf-8').write('x')"],
        )
        assert python_write.returncode != 0, python_write
        assert not (probe_dir / "f").exists(), python_write

        git_show = run("git", ["show", "HEAD", "--stat"])
        assert git_show.returncode == 0, git_show
    finally:
        shutil.rmtree(probe_dir, ignore_errors=True)
