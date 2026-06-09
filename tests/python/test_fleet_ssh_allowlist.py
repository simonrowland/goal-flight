#!/usr/bin/env python3
"""Tests for fleet SSH allowlist."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import goalflight_fleet_ssh as ssh


def assert_true(name: str, condition: bool) -> None:
    if not condition:
        raise AssertionError(name)


def test_allowed_command_classes_build() -> None:
    remote = ssh.build_remote_command("doctor", repo_root="/srv/goal-flight")
    assert_true("doctor argv", remote[1].endswith("goalflight_doctor.py"))
    git = ssh.build_remote_command("git_fetch", repo_root="/srv/goal-flight")
    assert_true("git fetch", git[:3] == ["git", "-C", "/srv/goal-flight"])
    cleanup = ssh.build_remote_command("git_prune_claude_refs", repo_root="/srv/goal-flight")
    assert_true("cleanup python3", cleanup[0] == "python3")
    assert_true("cleanup helper", cleanup[1].endswith("goalflight_cleanup_dispatch_refs.py"))
    assert_true("cleanup repo root", cleanup[-3:] == ["--repo-root", "/srv/goal-flight", "--json"])


def test_shell_metachar_rejected() -> None:
    try:
        ssh.validate_remote_argv(["echo", "hello; rm -rf /"])
        assert_true("should reject", False)
    except ssh.SshAllowlistError:
        pass


def test_unsafe_remote_gate() -> None:
    try:
        ssh.assert_allowed("shell", unsafe_remote=False)
        assert_true("shell blocked", False)
    except ssh.SshAllowlistError as exc:
        assert_true("unsafe code", exc.code == "unsafe_remote_blocked")
    ssh.assert_allowed("shell", unsafe_remote=True)


def test_unknown_class_blocked() -> None:
    try:
        ssh.assert_allowed("rm_rf")
        assert_true("unknown blocked", False)
    except ssh.SshAllowlistError:
        pass


def test_build_ssh_uses_separator() -> None:
    host = ssh.SshHostSpec(alias="build-1", hostname="build-1.local", user="dev", port=2222)
    remote = ssh.build_remote_command("probe_echo")
    cmd = ssh.build_ssh_command(host, remote, command_class="probe_echo")
    assert_true("ssh prefix", cmd[0] == "ssh")
    assert_true("double dash", "--" in cmd)
    zsh_idx = cmd.index("/bin/zsh")
    assert_true("zsh wrapper", cmd[zsh_idx + 1] == "-c")
    assert_true("remote echo in script", "goal-flight-probe-ok" in cmd[zsh_idx + 2])
    assert_true("homebrew path in script", "/opt/homebrew/bin" in cmd[zsh_idx + 2])
    assert_true("local bin path in script", "$HOME/.local/bin" in cmd[zsh_idx + 2])
    assert_true("home bootstrap", "HOME=${HOME:-" in cmd[zsh_idx + 2])


def test_parse_ssh_config() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        cfg = Path(td) / "config"
        cfg.write_text(
            "Host build-1\n  HostName 10.0.0.5\n  User simon\n  Port 2222\n  IdentityFile ~/.ssh/id_ed25519\n"
        )
        host = ssh.parse_ssh_config("build-1", cfg)
        assert_true("hostname", host.hostname == "10.0.0.5")
        assert_true("user", host.user == "simon")
        assert_true("port", host.port == 2222)


def test_parse_ssh_config_loopback_without_stanza() -> None:
    import getpass
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        cfg = Path(td) / "config"
        cfg.write_text("Host build-1\n  HostName 10.0.0.5\n  User simon\n")
        host = ssh.parse_ssh_config("localhost", cfg)
        assert_true("loopback hostname", host.hostname == "127.0.0.1")
        assert_true("loopback user", host.user == getpass.getuser())


def test_auth_probe_remote_argv_order() -> None:
    argv = ssh.build_remote_command(
        "auth_probe",
        repo_root="/srv/goal-flight",
        state_dir="/Users/dev/.goal-flight",
        account_key="openai/default",
    )
    assert_true("auth probe venv python", argv[0] == "/Users/dev/.goal-flight/venvs/acp-0.10/bin/python")
    assert_true("fleet-dir before subcommand", argv.index("--fleet-dir") < argv.index("probe"))
    assert_true("probe subcommand", "probe" in argv)


def test_wrap_remote_argv_expands_tilde_paths() -> None:
    wrapped = ssh.wrap_remote_argv(["~/.goal-flight/venvs/acp-0.10/bin/python", "ok"])
    script = wrapped[-1]
    assert_true("home bootstrap before exec", script.index("HOME=${HOME:-") < script.index("exec"))
    assert_true("tilde python via home", '"${HOME}/.goal-flight/venvs/acp-0.10/bin/python"' in script)
    assert_true("no quoted tilde literal", "'~/.goal-flight" not in script)


def test_acp_run_uses_prompt_b64_and_acp_python() -> None:
    import base64

    prompt = "Reply with exactly: fleet-smoke-ok"
    argv = ssh.build_remote_command(
        "acp_run",
        repo_root="/srv/goal-flight",
        state_dir="/Users/dev/.goal-flight",
        dispatch_id="acp-test",
        agent="codex-acp",
        prompt=prompt,
        cwd="/Users/dev/.goal-flight/worktrees/acp-test",
        status_json="/Users/dev/.goal-flight/dispatches/acp-test/status.json",
    )
    assert_true("acp venv python", argv[0].endswith("/venvs/acp-0.10/bin/python"))
    b64_idx = argv.index("--prompt-b64")
    decoded = base64.b64decode(argv[b64_idx + 1].encode("ascii")).decode("utf-8")
    assert_true("prompt roundtrip", decoded == prompt)
    assert_true("no prompt-text", "--prompt-text" not in argv)
    status_idx = argv.index("--status-json")
    assert_true(
        "status json path",
        argv[status_idx + 1] == "/Users/dev/.goal-flight/dispatches/acp-test/status.json",
    )


def main() -> None:
    for test in (
        test_allowed_command_classes_build,
        test_shell_metachar_rejected,
        test_unsafe_remote_gate,
        test_unknown_class_blocked,
        test_build_ssh_uses_separator,
        test_parse_ssh_config,
        test_parse_ssh_config_loopback_without_stanza,
        test_auth_probe_remote_argv_order,
        test_wrap_remote_argv_expands_tilde_paths,
        test_acp_run_uses_prompt_b64_and_acp_python,
    ):
        test()
        print(f"PASS {test.__name__}")


if __name__ == "__main__":
    main()
