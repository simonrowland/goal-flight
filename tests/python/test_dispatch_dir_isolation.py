#!/usr/bin/env python3
"""GOALFLIGHT_DISPATCH_DIR isolates the dispatch status surface.

A test that launches a worker without this override writes status into the
live uid-keyed directory (/tmp/goal-flight-<uid>/dispatch/), where the
operator's controller cannot tell it from a real worker death.

Journal/mail are a separate existing gap: they isolate via
GOALFLIGHT_JOURNAL_DIR / GOALFLIGHT_MESSAGES_DIR, not via this override.
"""

from __future__ import annotations

from support import skip_posix_on_native_windows

skip_posix_on_native_windows("dispatch launch isolation tests need POSIX workers")

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DISPATCH = ROOT / "scripts" / "goalflight_dispatch.py"
sys.path.insert(0, str(ROOT / "scripts"))

import goalflight_compat as compat  # noqa: E402
import goalflight_dispatch_paths as paths  # noqa: E402
import goalflight_journal as journal  # noqa: E402
import goalflight_messages as messages  # noqa: E402

LIVE_DISPATCH_DIR = compat.default_state_dir() / "dispatch"
PATH_OVERRIDE_KEYS = (
    "GOALFLIGHT_DISPATCH_DIR",
    "GOALFLIGHT_STATE_DIR",
    "GOALFLIGHT_JOURNAL_DIR",
    "GOALFLIGHT_MESSAGES_DIR",
    "GOALFLIGHT_TASK_STORE_DIR",
    "GOALFLIGHT_WAKE_LEDGER_DIR",
    "GOAL_FLIGHT_PIDFILE_DIR",
    "GOALFLIGHT_PIDFILE_DIR",
)


@contextmanager
def env_var(name: str, value: str | None):
    sentinel = object()
    old = os.environ.get(name, sentinel)
    try:
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value
        yield
    finally:
        if old is sentinel:
            os.environ.pop(name, None)
        else:
            os.environ[name] = str(old)


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


def assert_eq(name: str, got: object, expected: object) -> None:
    if got != expected:
        raise AssertionError(f"{name}: got {got!r}, expected {expected!r}")


def assert_true(name: str, condition: bool) -> None:
    if not condition:
        raise AssertionError(name)


def _live_journal_path() -> Path:
    with env_cleared("GOALFLIGHT_JOURNAL_DIR", "GOALFLIGHT_TASK_STORE_DIR", "GOALFLIGHT_STATE_DIR"):
        return journal.resolve_journal_path(ROOT)


def _live_messages_dir() -> Path:
    with env_cleared("GOALFLIGHT_MESSAGES_DIR"):
        return messages.default_messages_dir()


def _live_artifacts(dispatch_id: str) -> dict[str, list[Path]]:
    found: dict[str, list[Path]] = {"dispatch": [], "runs": [], "messages": []}
    if LIVE_DISPATCH_DIR.is_dir():
        found["dispatch"] = sorted(LIVE_DISPATCH_DIR.glob(f"{dispatch_id}*"))
        id_dir = LIVE_DISPATCH_DIR / ".dispatch-ids" / f"{dispatch_id}.json"
        if id_dir.exists():
            found["dispatch"].append(id_dir)
    runs = compat.default_state_dir() / "runs.d" / f"{dispatch_id}.json"
    if runs.exists():
        found["runs"] = [runs]
    msg = _live_messages_dir() / f"{dispatch_id}.jsonl"
    if msg.exists():
        found["messages"] = [msg]
    return found


def _journal_has_dispatch(dispatch_id: str) -> bool:
    path = _live_journal_path()
    if not path.exists():
        return False
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        row = con.execute(
            "SELECT 1 FROM dispatch_attempts WHERE dispatch_id = ? LIMIT 1",
            (dispatch_id,),
        ).fetchone()
    except sqlite3.Error:
        return False
    finally:
        con.close()
    return row is not None


def _isolate_launch_env(env: dict[str, str], tmp: Path) -> None:
    env["GOALFLIGHT_DISPATCH_DIR"] = str(tmp / "dispatch")
    env["GOALFLIGHT_STATE_DIR"] = str(tmp / "state")
    env["GOALFLIGHT_TASK_STORE_DIR"] = str(tmp / "task-store")
    env["GOALFLIGHT_JOURNAL_DIR"] = str(tmp / "journal")
    env["GOALFLIGHT_MESSAGES_DIR"] = str(tmp / "messages")
    env["GOALFLIGHT_WAKE_LEDGER_DIR"] = str(tmp / "wake-ledger")
    env["GOAL_FLIGHT_PIDFILE_DIR"] = str(tmp / "pids")
    env["GOALFLIGHT_CAPACITY_CONF"] = "/dev/null"
    for key in (
        "GOALFLIGHT_STEER_FILE",
        "GOALFLIGHT_ALLOW_EXTERNAL_STEER_FILE",
        "GOALFLIGHT_PROMPT_FILE",
        "GOALFLIGHT_DISPATCH_ID",
        "GOALFLIGHT_ISOLATED_TEST_FILE",
    ):
        env.pop(key, None)


def test_unscoped_resolver_still_points_at_live_default() -> None:
    """The production path is unchanged: no override => uid-keyed live dir.

    This is the pre-fix defect shape: a launch that forgets isolation resolves
    here. The override must not break that default.
    """
    with env_cleared(*PATH_OVERRIDE_KEYS):
        got = paths.dispatch_base_dir()
        expected = compat.default_state_dir() / "dispatch"
        assert_eq("dispatch_base_dir default", got, expected)
        assert_eq("default is the live dispatch dir", got.resolve(), LIVE_DISPATCH_DIR.resolve())
        assert_true("live dispatch dir exists", got.is_dir())


def test_override_is_honored_and_beats_state_dir() -> None:
    with tempfile.TemporaryDirectory(prefix="gf-dispatch-dir-") as tmp:
        isolated = Path(tmp) / "isolated-dispatch"
        other_state = Path(tmp) / "other-state"
        with env_var("GOALFLIGHT_STATE_DIR", str(other_state)):
            with env_var("GOALFLIGHT_DISPATCH_DIR", str(isolated)):
                assert_eq("override wins", paths.dispatch_base_dir(), isolated)
            with env_var("GOALFLIGHT_DISPATCH_DIR", None):
                assert_eq(
                    "state_dir fallback",
                    paths.dispatch_base_dir(),
                    other_state / "dispatch",
                )


def test_blank_override_falls_back() -> None:
    expected = compat.default_state_dir() / "dispatch"
    for poison in ("", " \t "):
        with env_cleared(*PATH_OVERRIDE_KEYS):
            with env_var("GOALFLIGHT_DISPATCH_DIR", poison):
                got = paths.dispatch_base_dir()
                assert_eq(f"blank {poison!r} fallback", got, expected)
                assert_true("must not resolve to cwd", got.resolve() != Path.cwd().resolve())


def test_journal_mail_are_not_covered_by_dispatch_dir() -> None:
    """DISPATCH_DIR must not pretend to isolate journal/mail.

    The live journal already exists for this repo. resolve_journal_path is
    keyed off GOALFLIGHT_JOURNAL_DIR (else the task-store base), so a launch
    that only sets DISPATCH_DIR still registers in the live journal and the
    journal-outbox adapter still writes ~/.goal-flight/messages/<id>.jsonl.
    Those knobs already exist; do not invent a second pattern.
    """
    with tempfile.TemporaryDirectory(prefix="gf-dispatch-dir-only-") as tmp:
        isolated = Path(tmp) / "dispatch"
        with env_cleared("GOALFLIGHT_JOURNAL_DIR", "GOALFLIGHT_TASK_STORE_DIR", "GOALFLIGHT_MESSAGES_DIR"):
            with env_var("GOALFLIGHT_DISPATCH_DIR", str(isolated)):
                assert_eq("dispatch override works alone", paths.dispatch_base_dir(), isolated)
                live_journal = journal.resolve_journal_path(ROOT)
                live_messages = messages.default_messages_dir()
        assert_eq("journal stays on the live path", live_journal, _live_journal_path())
        assert_true("live journal exists (the mail source)", live_journal.exists())
        assert_eq("messages stay on the live path", live_messages, _live_messages_dir())
        assert_true("live messages dir exists", live_messages.exists())


def test_isolated_launch_does_not_touch_live_dispatch_or_mail() -> None:
    """A real launch with the override set must not write live status or mail.

    Paths default from dispatch_base_dir() (no --tail / --status-json) so this
    fails pre-fix: the launch would land under /tmp/goal-flight-<uid>/dispatch/
    and the live journal-outbox would emit [blocked] mail.
    """
    dispatch_id = f"t276-diso-{os.getpid()}-{int(time.time())}"
    before = _live_artifacts(dispatch_id)
    assert_eq("pre-existing live dispatch artifacts", before["dispatch"], [])
    assert_eq("pre-existing live run artifacts", before["runs"], [])
    assert_eq("pre-existing live mail", before["messages"], [])
    assert_true("pre-existing live journal row", not _journal_has_dispatch(dispatch_id))

    with tempfile.TemporaryDirectory(prefix="gf-dispatch-dir-launch-") as tmp_raw:
        tmp = Path(tmp_raw)
        env = os.environ.copy()
        _isolate_launch_env(env, tmp)
        isolated = Path(env["GOALFLIGHT_DISPATCH_DIR"])
        proc = subprocess.run(
            [
                sys.executable,
                str(DISPATCH),
                "--cwd",
                str(tmp),
                "--agent",
                "test",
                "--dispatch-id",
                dispatch_id,
                "--poll-secs",
                "0.1",
                "--max-idle-secs",
                "8",
                "--foreground",
                "--",
                sys.executable,
                "-c",
                "print('t276-isolated-launch', flush=True)",
            ],
            capture_output=True,
            text=True,
            timeout=30,
            env=env,
        )
        status = isolated / f"{dispatch_id}.status.json"
        tail = isolated / f"{dispatch_id}.tail"
        assert_true(
            f"isolated status written ({proc.returncode=}\nstdout={proc.stdout}\nstderr={proc.stderr})",
            status.is_file(),
        )
        assert_true("isolated tail written", tail.is_file())
        payload = json.loads(status.read_text(encoding="utf-8"))
        assert_eq("status dispatch_id", payload.get("dispatch_id"), dispatch_id)

        after = _live_artifacts(dispatch_id)
        assert_eq("live dispatch artifacts after launch", after["dispatch"], [])
        assert_eq("live run artifacts after launch", after["runs"], [])
        assert_eq("live mail after launch", after["messages"], [])
        assert_true("live journal row after launch", not _journal_has_dispatch(dispatch_id))


def main() -> None:
    tests = [
        test_unscoped_resolver_still_points_at_live_default,
        test_override_is_honored_and_beats_state_dir,
        test_blank_override_falls_back,
        test_journal_mail_are_not_covered_by_dispatch_dir,
        test_isolated_launch_does_not_touch_live_dispatch_or_mail,
    ]
    for test in tests:
        test()
    print(f"PASS tests/python/test_dispatch_dir_isolation.py ({len(tests)} tests)")


if __name__ == "__main__":
    main()
