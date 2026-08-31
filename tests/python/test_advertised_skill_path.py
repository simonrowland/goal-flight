"""Copy-paste hints name the installed skill, not the generating checkout."""

from __future__ import annotations

import errno
import os
import shlex
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import goalflight_compat as compat  # noqa: E402
import goalflight_dispatch as dispatch  # noqa: E402
import goalflight_fleet_console as fleet_console  # noqa: E402
import goalflight_messages as messages  # noqa: E402
import goalflight_wake as wake  # noqa: E402


def _plant_skill(root: Path) -> Path:
    scripts = root / "scripts"
    scripts.mkdir(parents=True)
    (scripts / "goalflight_messages.py").write_text("# skill\n", encoding="utf-8")
    (scripts / "goalflight_status.py").write_text("# status\n", encoding="utf-8")
    (scripts / "goalflight_session_status.py").write_text("# session\n", encoding="utf-8")
    (scripts / "goalflight_dispatch.py").write_text("# dispatch\n", encoding="utf-8")
    (scripts / "goalflight_watch.py").write_text("# watch\n", encoding="utf-8")
    (scripts / "install-drainer.sh").write_text("# drainer\n", encoding="utf-8")
    (scripts / "watch-dispatch-tail.sh").write_text("# tail\n", encoding="utf-8")
    (root / "SKILL.md").write_text("# Goal Flight\n", encoding="utf-8")
    return root


def _assert_copyable(path_text: str) -> Path:
    path = Path(path_text)
    assert path.is_absolute(), path_text
    assert "~" not in path_text, path_text
    assert not path_text.startswith("scripts/"), path_text
    return path


def test_dev_checkout_hint_names_installed_skill(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    install = _plant_skill(home / ".goal-flight" / "skill")
    checkout = _plant_skill(tmp_path / "checkout")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls, _home=home: _home))
    monkeypatch.delenv("GOALFLIGHT_ROOT", raising=False)

    running = checkout / "scripts" / "goalflight_wake.py"
    cmd = wake.listener_start_command(tmp_path / "proj", controller_label="ctl")
    argv = shlex.split(cmd)
    script = _assert_copyable(argv[1])
    assert script == install / "scripts" / "goalflight_messages.py"
    assert str(checkout) not in cmd
    assert compat.advertised_script(
        "goalflight_messages.py", running_file=running
    ) == install / "scripts" / "goalflight_messages.py"


def test_installed_copy_hint_stays_on_the_install(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    install = _plant_skill(home / ".goal-flight" / "skill")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls, _home=home: _home))
    monkeypatch.delenv("GOALFLIGHT_ROOT", raising=False)

    running = install / "scripts" / "goalflight_wake.py"
    script = compat.advertised_script(
        "goalflight_messages.py", running_file=running
    )
    assert script == install / "scripts" / "goalflight_messages.py"
    cmd = wake.listener_start_command(tmp_path / "proj", controller_label="ctl")
    assert shlex.split(cmd)[1] == str(script)


def test_missing_install_falls_back_to_running_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "empty-home"
    home.mkdir()
    checkout = _plant_skill(tmp_path / "checkout")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls, _home=home: _home))
    monkeypatch.delenv("GOALFLIGHT_ROOT", raising=False)

    running = checkout / "scripts" / "goalflight_wake.py"
    script = compat.advertised_script(
        "goalflight_messages.py", running_file=running
    )
    assert script == (checkout / "scripts" / "goalflight_messages.py").resolve()
    _assert_copyable(str(script))


def test_failed_pin_marker_probe_keeps_the_pinned_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    install = _plant_skill(home / ".goal-flight" / "skill")
    checkout = _plant_skill(tmp_path / "checkout")
    marker = install / "scripts" / "goalflight_messages.py"
    original_stat = os.stat

    def fail_marker_stat(path, *args, **kwargs):
        if isinstance(path, (str, os.PathLike)) and Path(path) == marker:
            raise OSError(errno.ESTALE, "stale install marker")
        return original_stat(path, *args, **kwargs)

    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls, _home=home: _home))
    monkeypatch.delenv("GOALFLIGHT_ROOT", raising=False)
    monkeypatch.setattr(os, "stat", fail_marker_stat)

    assert compat._looks_like_skill_root(install) is None  # noqa: SLF001
    assert compat.installed_skill_root() == install
    assert compat.advertised_skill_root(
        running_file=checkout / "scripts" / "goalflight_wake.py"
    ) == install


def test_mutation_lookup_fail_still_emits_absolute_running_copy(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(compat, "installed_skill_root", lambda: None)
    cmd = wake.listener_start_command(tmp_path / "proj", controller_label="ctl")
    argv = shlex.split(cmd)
    script = _assert_copyable(argv[1])
    assert script == Path(wake.__file__).resolve().with_name("goalflight_messages.py")
    assert Path(argv[4]).is_absolute()
    assert "~" not in cmd


def test_mutation_lookup_succeeds_from_dev_checkout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install = _plant_skill(tmp_path / "install")
    monkeypatch.setattr(compat, "installed_skill_root", lambda: install)
    before = Path(wake.__file__).resolve().with_name("goalflight_messages.py")
    cmd = wake.listener_start_command(tmp_path / "proj", controller_label="ctl")
    argv = shlex.split(cmd)
    script = _assert_copyable(argv[1])
    assert script == install / "scripts" / "goalflight_messages.py"
    assert script != before
    assert str(ROOT / "scripts" / "goalflight_messages.py") not in cmd


def test_goalflight_root_tilde_expands_when_pin_is_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    custom = _plant_skill(home / "custom-skill")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls, _home=home: _home))
    monkeypatch.setenv("GOALFLIGHT_ROOT", "~/custom-skill")
    root = compat.installed_skill_root()
    assert root == custom
    assert "~" not in str(root)


def test_relative_goalflight_root_is_ignored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "empty-home"
    home.mkdir()
    relative = _plant_skill(tmp_path / "relative-skill")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls, _home=home: _home))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GOALFLIGHT_ROOT", "relative-skill")
    assert compat.installed_skill_root() is None
    assert relative.exists()


def test_goalflight_root_overrides_the_pin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same precedence as doctor and the AGENTS.md pin: env wins, pin defaults.

    ``goalflight_setup`` writes ``${GOALFLIGHT_ROOT:-~/.goal-flight/skill}``
    and ``goalflight_doctor._goalflight_skill_root`` probes the env var first.
    If hints advertised the pin while doctor probed the override, the two would
    name different installs — the exact split this module exists to close.
    """
    home = tmp_path / "home"
    _plant_skill(home / ".goal-flight" / "skill")
    elsewhere = _plant_skill(tmp_path / "elsewhere")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls, _home=home: _home))
    monkeypatch.setenv("GOALFLIGHT_ROOT", str(elsewhere))
    assert compat.installed_skill_root() == elsewhere


def test_absolute_override_is_honoured_even_when_it_does_not_exist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Agreement with doctor beats our own judgement about the override.

    ``goalflight_doctor._goalflight_skill_root`` honours ANY nonempty
    ``$GOALFLIGHT_ROOT``, existing or not. An earlier draft second-guessed an
    unusable override and fell back to the pin, which put the advertiser and
    doctor on different roots -- the exact split this module closes. Measured
    at review time: advertiser said ``~/.goal-flight/skill`` while doctor said
    ``/definitely/missing-goal-flight-root``.
    """
    home = tmp_path / "home"
    _plant_skill(home / ".goal-flight" / "skill")
    missing = tmp_path / "definitely-missing"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls, _home=home: _home))
    monkeypatch.setenv("GOALFLIGHT_ROOT", str(missing))
    assert compat.installed_skill_root() == missing
    assert not missing.exists()


def test_exact_listener_rearm_command_names_the_install(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The re-arm hint is the most-copied string we emit; it must not self-seed.

    Every doorbell prints one as it exits and controllers paste it verbatim to
    restore depth, so a ``__file__``-derived re-arm makes a development listener
    re-seed its own checkout on every single wake.
    """
    install = _plant_skill(tmp_path / "install")
    monkeypatch.setattr(compat, "installed_skill_root", lambda: install)
    monkeypatch.setattr(
        sys, "argv", ["goalflight_messages.py", "listen", "--project-root", str(tmp_path)]
    )
    argv = shlex.split(messages._exact_listener_command())
    script = _assert_copyable(argv[1])
    assert script == install / "scripts" / "goalflight_messages.py"
    assert str(Path(messages.__file__).resolve().parent) not in " ".join(argv)
    assert "--report-pending" in argv


def test_hints_survive_an_install_path_containing_spaces(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Hints are pasted into someone else's shell, so they must be quoted.

    An unquoted ``/tmp/skill root/scripts/x.py`` tokenizes into two arguments
    and fails on a machine we never see.
    """
    install = _plant_skill(tmp_path / "skill root")
    monkeypatch.setattr(compat, "installed_skill_root", lambda: install)
    for shape in ("acp", "bash"):
        text = "\n".join(
            dispatch._status_reminder_lines(
                "spacey",
                status_json=tmp_path / "spacey.status.json",
                tail_path=tmp_path / "spacey.tail",
                worker_pid=11,
                shape=shape,
                hints=True,
            )
        )
        for line in text.splitlines():
            if ": python3 " not in line and ": bash " not in line and "watch:" not in line:
                continue
            argv = shlex.split(line.split(":", 1)[1])
            # the interpreter/bash token, then a path that survived as ONE token
            assert len(argv) >= 2, line
            assert Path(argv[1]).is_absolute(), line
            assert Path(argv[1]).exists() or argv[1].endswith(".py"), line
            assert "skill root" in argv[1] or str(tmp_path) in argv[1], line


def test_dispatch_and_drain_hints_use_advertised_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install = _plant_skill(tmp_path / "install")
    monkeypatch.setattr(compat, "installed_skill_root", lambda: install)
    assert dispatch._skill_root() == install
    lines = dispatch._status_reminder_lines(
        "hint-id",
        status_json=tmp_path / "hint-id.status.json",
        tail_path=tmp_path / "hint-id.tail",
        worker_pid=7,
        shape="acp",
        hints=True,
    )
    text = "\n".join(lines)
    install_status = str(install / "scripts" / "goalflight_status.py")
    install_watch = str(install / "scripts" / "goalflight_watch.py")
    assert install_status in text
    assert install_watch in text
    assert str(ROOT / "scripts" / "goalflight_status.py") not in text
    _assert_copyable(install_status)

    bash_lines = dispatch._status_reminder_lines(
        "hint-id",
        status_json=tmp_path / "hint-id.status.json",
        tail_path=tmp_path / "hint-id.tail",
        worker_pid=7,
        shape="bash",
        hints=True,
    )
    bash_text = "\n".join(bash_lines)
    assert str(install / "scripts" / "watch-dispatch-tail.sh") in bash_text


def test_fleet_console_accepts_its_own_advertised_listener_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install = _plant_skill(tmp_path / "install")
    monkeypatch.setattr(compat, "installed_skill_root", lambda: install)
    command = wake.listener_start_command(
        tmp_path / "project with spaces",
        controller_label="main controller",
    )
    assert fleet_console._is_listener_start_action(command)
    checkout_cmd = shlex.join(
        [
            "python3",
            str(Path(messages.__file__).resolve()),
            "listen",
            "--project-root",
            str(tmp_path / "project with spaces"),
            "--controller-label",
            "main controller",
            "--report-pending",
        ]
    )
    if Path(messages.__file__).resolve() != install / "scripts" / "goalflight_messages.py":
        assert not fleet_console._is_listener_start_action(checkout_cmd)
    relative = "python3 scripts/goalflight_messages.py listen --project-root /tmp/p --report-pending"
    assert not fleet_console._is_listener_start_action(relative)


def test_registration_and_advance_hints_are_absolute(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install = _plant_skill(tmp_path / "install")
    monkeypatch.setattr(compat, "installed_skill_root", lambda: install)
    command = messages._cursor_advance_command(
        project_root=tmp_path / "proj",
        controller_label="ctl",
        lease_nonce="nonce",
        cursor_version=1,
        positions={"stream": 1},
        stream_snapshots={"stream": "a" * 64},
    )
    assert command is not None
    argv = shlex.split(command)
    script = _assert_copyable(argv[1])
    assert script == install / "scripts" / "goalflight_messages.py"
