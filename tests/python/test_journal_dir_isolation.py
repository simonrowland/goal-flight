#!/usr/bin/env python3
"""GOALFLIGHT_JOURNAL_DIR isolates the per-project journal index.

A test that creates a journal without this override (and without
GOALFLIGHT_TASK_STORE_DIR, which only isolates journals incidentally) writes
into ~/.local/state/goal-flight/journals/<slug>/. Measured 2026-08-27: four
live journal dirs recorded pytest temp roots named project-<hash> — the
task-store slug of tmp_path / "project" after the temp tree was reaped.

Python autouse isolation covers pytest, but the gate used to pin
GOALFLIGHT_MESSAGES_DIR and not GOALFLIGHT_JOURNAL_DIR. Bash tests, script
drivers, and any test that pops TASK_STORE_DIR to exercise the XDG fallback
then inherited the production default. The pin is the snapshot: a create must
resolve under the isolated root and must not mint the live slug.
"""

from __future__ import annotations

from contextlib import contextmanager
import os
from pathlib import Path
import subprocess
import sys

from support import isolated_machine_env


ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(ROOT))

import goalflight_journal as journal  # noqa: E402
import goalflight_task as task  # noqa: E402

PATH_OVERRIDE_KEYS = (
    "GOALFLIGHT_JOURNAL_DIR",
    "GOALFLIGHT_TASK_STORE_DIR",
    "GOALFLIGHT_STATE_DIR",
    "GOALFLIGHT_DISPATCH_DIR",
    "GOALFLIGHT_MESSAGES_DIR",
    "GOALFLIGHT_WAKE_LEDGER_DIR",
)


@contextmanager
def env_cleared(*names: str):
    sentinel = object()
    old = {name: os.environ.get(name, sentinel) for name in names}
    try:
        for name in names:
            os.environ.pop(name, None)
        yield
    finally:
        for name, value in old.items():
            if value is sentinel:
                os.environ.pop(name, None)
            else:
                os.environ[name] = str(value)


def _live_journals_index() -> Path:
    with env_cleared("GOALFLIGHT_JOURNAL_DIR", "GOALFLIGHT_TASK_STORE_DIR"):
        return journal.journals_index_dir()


def test_unscoped_resolver_still_points_at_live_xdg(tmp_path: Path) -> None:
    """No override => XDG journals index. This is the pre-fix defect shape."""
    project = tmp_path / "project"
    project.mkdir()
    with env_cleared(*PATH_OVERRIDE_KEYS):
        got = journal.resolve_journal_path(project)
        live_index = journal.journals_index_dir()
        expected_slug = task.resolve_task_store_dir(project).name
    assert got.parent.name == expected_slug
    assert got.parent.parent.resolve() == live_index.resolve()
    assert got.name == journal.JOURNAL_FILE_NAME
    assert got.resolve() != (tmp_path / "project").resolve()


def test_isolated_create_does_not_touch_live_journals(tmp_path: Path) -> None:
    """pop TASK_STORE_DIR, keep JOURNAL_DIR: create stays under the journal override.

    This is the measured leak shape. TASK_STORE_DIR only isolates journals
    incidentally; without JOURNAL_DIR the resolver would mint
    ~/.local/state/goal-flight/journals/project-<hash>.
    """
    project = tmp_path / "project"
    project.mkdir()
    live_index = _live_journals_index()
    with env_cleared("GOALFLIGHT_JOURNAL_DIR", "GOALFLIGHT_TASK_STORE_DIR"):
        slug = task.resolve_task_store_dir(project).name
    live_slug = live_index / slug
    assert not live_slug.exists()

    journal_override = Path(os.environ["GOALFLIGHT_JOURNAL_DIR"])
    with env_cleared("GOALFLIGHT_TASK_STORE_DIR"):
        resolved = journal.resolve_journal_path(project)
        authority = journal.Journal.create(project)
    assert resolved.resolve().is_relative_to(journal_override.resolve()), resolved
    assert not resolved.resolve().is_relative_to(live_index.resolve()), resolved
    assert authority.path.resolve() == resolved.resolve()
    assert resolved.is_file()
    assert not live_slug.exists(), f"leaked live journal dir: {live_slug}"


def test_journal_dir_loss_is_isolated_only_incidentally_by_task_store(
    tmp_path: Path,
) -> None:
    """pop only JOURNAL_DIR: live XDG is missed because TASK_STORE_DIR remains.

    This does not credit the JOURNAL_DIR pin. Losing JOURNAL_DIR while the
    store override is still set lands under the isolated task-store journals
    index. A pin that only checks its own live_slug stays green for that
    shape even though JOURNAL_DIR did no work.
    """
    project = tmp_path / "project"
    project.mkdir()
    live_index = _live_journals_index()
    with env_cleared("GOALFLIGHT_JOURNAL_DIR", "GOALFLIGHT_TASK_STORE_DIR"):
        slug = task.resolve_task_store_dir(project).name
    live_slug = live_index / slug
    assert not live_slug.exists()

    task_store = Path(os.environ["GOALFLIGHT_TASK_STORE_DIR"])
    with env_cleared("GOALFLIGHT_JOURNAL_DIR"):
        created = journal.Journal.create(project).path.resolve()
    assert created.is_file()
    assert created.is_relative_to(task_store.resolve()), created
    assert not created.is_relative_to(live_index.resolve()), created
    assert not live_slug.exists(), f"leaked live journal dir: {live_slug}"


def test_subprocess_create_inherits_gate_journal_dir(tmp_path: Path) -> None:
    """A child that only gets the gate-style env must still miss the live store.

    This is the bash-suite shape: no pytest autouse in the child, only the
    GOALFLIGHT_* assignments tests/run.sh exports.
    """
    gate_root = tmp_path / "gate-env"
    project = tmp_path / "project"
    project.mkdir()
    env = os.environ.copy()
    env.update(isolated_machine_env(gate_root))
    live_index = _live_journals_index()
    with env_cleared("GOALFLIGHT_JOURNAL_DIR", "GOALFLIGHT_TASK_STORE_DIR"):
        slug = task.resolve_task_store_dir(project).name
    live_slug = live_index / slug
    assert not live_slug.exists()

    probe = r"""
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
sys.path.insert(0, sys.argv[2])
import goalflight_journal as journal
project = Path(sys.argv[3])
path = journal.Journal.create(project).path
print(path)
"""
    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            probe,
            str(SCRIPTS),
            str(ROOT),
            str(project),
        ],
        capture_output=True,
        text=True,
        check=False,
        env=env,
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    created = Path(proc.stdout.strip())
    assert created.is_file(), proc.stdout
    assert created.resolve().is_relative_to(gate_root.resolve()), created
    assert not created.resolve().is_relative_to(live_index.resolve()), created
    assert not live_slug.exists(), f"leaked live journal dir: {live_slug}"
