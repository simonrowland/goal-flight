#!/usr/bin/env python3
"""Tests for fleet SSH allowlist."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
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
    assert_true("remote tail", cmd[-2:] == ["echo", "goal-flight-probe-ok"])


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


def main() -> None:
    for test in (
        test_allowed_command_classes_build,
        test_shell_metachar_rejected,
        test_unsafe_remote_gate,
        test_unknown_class_blocked,
        test_build_ssh_uses_separator,
        test_parse_ssh_config,
        test_parse_ssh_config_loopback_without_stanza,
    ):
        test()
        print(f"PASS {test.__name__}")


if __name__ == "__main__":
    main()
