#!/usr/bin/env python3
"""Reaping listeners is a KILL path, so its refusals matter more than its hits.

b-340: listeners outlive their controller generation. The fix kills them at
startup. The dangerous failure is not leaving an orphan alive, it is mistaking
a LIVE generation for a dead one -- so every question this module cannot answer
must yield known=False and kill nothing.

The worst case is a merely-BUSY journal: with no lease records the "known
nonces" set is empty, every listener looks orphaned, and a naive implementation
takes out the whole live fleet in one sweep. That is the first test here.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import goalflight_listener_reap as R  # noqa: E402


LIVE = "1111111111111111"
DEAD = "2222222222222222"


def _fake_ps(mapping: dict[str, list[int]]):
    return lambda _root: {
        nonce: [
            {"pid": pid, "start_token": f"test:{pid}"}
            for pid in pids
        ]
        for nonce, pids in mapping.items()
    }


def _fake_identity(pid: int) -> dict[str, object]:
    return {"pid": pid, "start_token": f"test:{pid}"}


def _fake_argv(monkeypatch, argv_by_pid: dict[int, list[str]]) -> None:
    monkeypatch.setattr(R, "_process_argv", lambda pid: argv_by_pid.get(pid))


def _ps_python_row(pid: int, *, uid: int | None = None, comm: str = "python3") -> str:
    owner = os.getuid() if uid is None else uid
    return f"{pid} {owner} {comm}"


def _ps_liveness_available(pid: int) -> bool:
    try:
        result = subprocess.run(
            ["ps", "-o", "pid=,state=", "-p", str(pid)],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return False
    return result.returncode == 0


# --------------------------------------------------------------------------
# refusals
# --------------------------------------------------------------------------


def test_unreadable_lease_records_refuse_rather_than_reap_everything(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A busy journal must not read as 'no generation is alive'."""
    monkeypatch.setattr(R, "listener_processes_by_nonce", _fake_ps({LIVE: [111], DEAD: [222]}))
    monkeypatch.setattr(R, "_known_lease_nonces", lambda _root: None)

    seen = R.orphaned_listeners(tmp_path)
    assert seen["known"] is False, seen
    assert "orphans" not in seen, "an unknown must not carry a kill list"

    killed: list[int] = []
    monkeypatch.setattr(os, "kill", lambda pid, sig: killed.append(pid))
    out = R.reap_orphaned_listeners(tmp_path)
    assert out["reaped"] == 0 and killed == [], out
    assert "lease records unreadable" in out["refused"], out


def test_unreadable_process_table_refuses(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(R, "listener_processes_by_nonce", lambda _root: None)
    killed: list[int] = []
    monkeypatch.setattr(os, "kill", lambda pid, sig: killed.append(pid))
    out = R.reap_orphaned_listeners(tmp_path)
    assert out["reaped"] == 0 and killed == [], out
    assert "process table unreadable" in out["refused"], out


def test_the_current_generation_is_never_reaped(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Startup races the first lease write; our own listeners are not orphans."""
    monkeypatch.setattr(R, "listener_processes_by_nonce", _fake_ps({LIVE: [111]}))
    monkeypatch.setattr(R, "_known_lease_nonces", lambda _root: set())  # journal empty
    seen = R.orphaned_listeners(tmp_path, current_nonce=LIVE)
    assert seen["orphans"] == [], seen
    without = R.orphaned_listeners(tmp_path)
    assert without["orphans"] == [111], "absent the pin it would be reapable"


def test_a_process_with_no_nonce_is_never_attributed(monkeypatch) -> None:
    """Unattributable means it may belong to a LIVE generation. Never reapable."""
    listing = (
        _ps_python_row(101) + "\n"
        + _ps_python_row(102) + "\n"
        + _ps_python_row(103) + "\n"
    )
    monkeypatch.setattr(
        R.subprocess, "run",
        lambda *a, **k: type("P", (), {"stdout": listing})(),
    )
    _fake_argv(
        monkeypatch,
        {
            101: [
                "python3", "/s/goalflight_messages.py", "listen",
                "--project-root", "/repos/mine", "--lease-nonce", DEAD,
            ],
            102: [
                "python3", "/s/goalflight_messages.py", "status",
                "--project-root", "/repos/mine",
            ],
            103: [
                "python3", "/s/unrelated.py", "--project-root", "/repos/mine",
                "--lease-nonce", DEAD,
            ],
        },
    )
    monkeypatch.setattr(R.goalflight_compat, "process_start_identity", _fake_identity)
    got = R.listener_processes_by_nonce(Path("/repos/mine"))
    assert got == {DEAD: [{"pid": 101, "start_token": "test:101"}]}, got


def test_argv_poison_process_is_not_selected(monkeypatch) -> None:
    listing = _ps_python_row(101) + "\n"
    monkeypatch.setattr(
        R.subprocess, "run", lambda *a, **k: type("P", (), {"stdout": listing})()
    )
    _fake_argv(
        monkeypatch,
        {
            101: [
                "python3", "/s/other.py", "goalflight_messages.py", "listen",
                "--project-root", "/repos/mine", "--lease-nonce", DEAD,
            ],
        },
    )
    monkeypatch.setattr(R.goalflight_compat, "process_start_identity", _fake_identity)
    assert R.listener_processes_by_nonce(Path("/repos/mine")) == {}


def test_unreadable_non_candidates_do_not_poison_the_scan(monkeypatch) -> None:
    listing = "\n".join(
        [
            _ps_python_row(401),
            _ps_python_row(402, comm="launchd"),
            _ps_python_row(403, uid=os.getuid() + 1),
        ]
    )
    monkeypatch.setattr(
        R.subprocess,
        "run",
        lambda *a, **k: type("P", (), {"stdout": listing})(),
    )
    calls: list[int] = []

    def fake_argv(pid: int) -> list[str]:
        calls.append(pid)
        assert pid == 401
        return ["python3", "-c", "pass"]

    monkeypatch.setattr(R, "_process_argv", fake_argv)
    monkeypatch.setattr(R.goalflight_compat, "process_start_identity", _fake_identity)

    assert R.listener_processes_by_nonce(Path("/repos/mine")) == {}
    assert calls == [401], calls


def test_unreadable_candidate_makes_the_scan_unknown(monkeypatch) -> None:
    listing = _ps_python_row(402) + "\n"
    monkeypatch.setattr(
        R.subprocess,
        "run",
        lambda *a, **k: type("P", (), {"stdout": listing})(),
    )
    _fake_argv(monkeypatch, {402: None})
    monkeypatch.setattr(R.goalflight_compat, "process_start_identity", _fake_identity)

    assert R.listener_processes_by_nonce(Path("/repos/mine")) is None


def test_start_token_change_during_argv_read_makes_the_scan_unknown(monkeypatch) -> None:
    listing = _ps_python_row(404) + "\n"
    monkeypatch.setattr(
        R.subprocess,
        "run",
        lambda *a, **k: type("P", (), {"stdout": listing})(),
    )
    _fake_argv(
        monkeypatch,
        {
            404: [
                "python3", "/s/goalflight_messages.py", "listen",
                "--project-root", "/repos/mine", "--lease-nonce", DEAD,
            ],
        },
    )
    identities = iter([_fake_identity(404), {"pid": 404, "start_token": "changed"}])
    monkeypatch.setattr(R.goalflight_compat, "process_start_identity", lambda _pid: next(identities))

    assert R.listener_processes_by_nonce(Path("/repos/mine")) is None


def test_nonzero_process_listing_refuses_even_with_matching_stdout(monkeypatch) -> None:
    listing = _ps_python_row(101) + "\n"
    result = subprocess.CompletedProcess(
        ["ps"], 1, stdout=listing, stderr="ps: permission denied"
    )
    monkeypatch.setattr(R.subprocess, "run", lambda *a, **k: result)
    monkeypatch.setattr(R, "_known_lease_nonces", lambda _root: set())
    killed: list[int] = []
    monkeypatch.setattr(R.os, "kill", lambda pid, sig: killed.append(pid))
    out = R.reap_orphaned_listeners(Path("/repos/mine"))
    assert killed == [] and out["reaped"] == 0, out
    assert out["detail"]["known"] is False, out


def test_truncated_listener_argv_refuses_the_entire_scan(monkeypatch) -> None:
    """A clipped nonce must not leave other listeners actionable."""
    listing = _ps_python_row(101) + "\n" + _ps_python_row(102) + "\n"
    calls: list[list[str]] = []

    def fake_run(command, *args, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, stdout=listing, stderr="")

    monkeypatch.setattr(R.subprocess, "run", fake_run)
    _fake_argv(
        monkeypatch,
        {
            101: [
                "python3", "/s/goalflight_messages.py", "listen",
                "--project-root", "/repos/mine", "--lease-nonce",
            ],
            102: [
                "python3", "/s/goalflight_messages.py", "listen",
                "--project-root", "/repos/mine", "--lease-nonce", DEAD,
            ],
        },
    )
    monkeypatch.setattr(R.goalflight_compat, "process_start_identity", _fake_identity)
    monkeypatch.setattr(R, "_known_lease_nonces", lambda _root: {LIVE})
    monkeypatch.setattr(R, "_liveness", lambda _pids: {})
    killed: list[int] = []
    monkeypatch.setattr(R.os, "kill", lambda pid, sig: killed.append(pid))

    assert R.listener_processes_by_nonce(Path("/repos/mine")) is None
    out = R.reap_orphaned_listeners(Path("/repos/mine"))

    assert calls[0][1] == "-axo", calls
    assert killed == [] and out["reaped"] == 0, out
    assert out["detail"]["known"] is False, out


def test_own_pid_is_protected(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        R, "listener_processes_by_nonce", _fake_ps({DEAD: [os.getpid()]})
    )
    monkeypatch.setattr(R, "_known_lease_nonces", lambda _root: {LIVE})
    out = R.reap_orphaned_listeners(tmp_path, dry_run=True)
    assert out["would_reap"] == [], out


# --------------------------------------------------------------------------
# hits, verified by liveness rather than by having sent a signal
# --------------------------------------------------------------------------


def test_an_orphan_is_actually_killed_and_the_kill_is_verified(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    if not _ps_liveness_available(os.getpid()):
        pytest.skip("sandbox denies the post-signal ps liveness probe")
    victim = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    try:
        monkeypatch.setattr(R.goalflight_compat, "process_start_identity", _fake_identity)
        monkeypatch.setattr(
            R, "listener_processes_by_nonce", _fake_ps({DEAD: [victim.pid]})
        )
        monkeypatch.setattr(R, "_known_lease_nonces", lambda _root: {LIVE})
        out = R.reap_orphaned_listeners(tmp_path)
        assert out["reaped"] == 1, out
        assert out["reaped_pids"] == [victim.pid], out
        assert "stubborn" not in out, out
        victim.wait(timeout=10)
        assert victim.poll() is not None, "the process must really be gone"
    finally:
        if victim.poll() is None:
            victim.kill()
            victim.wait(timeout=10)


def test_a_survivor_is_reported_stubborn_not_counted_as_reaped(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Sending a signal is not evidence the process died."""
    if not _ps_liveness_available(os.getpid()):
        pytest.skip("sandbox denies the post-signal ps liveness probe")
    victim = subprocess.Popen(
        [sys.executable, "-c",
         "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(120)"]
    )
    try:
        time.sleep(0.4)  # let the handler install before we signal
        monkeypatch.setattr(R.goalflight_compat, "process_start_identity", _fake_identity)
        monkeypatch.setattr(
            R, "listener_processes_by_nonce", _fake_ps({DEAD: [victim.pid]})
        )
        monkeypatch.setattr(R, "_known_lease_nonces", lambda _root: {LIVE})
        out = R.reap_orphaned_listeners(tmp_path)
        assert out["reaped"] == 0, out
        assert out["stubborn"][0]["pid"] == victim.pid, out
        assert out["stubborn"][0]["why"] == "still-alive-after-term", out
    finally:
        victim.send_signal(signal.SIGKILL)
        victim.wait(timeout=10)


def test_only_generations_absent_from_the_leases_are_orphans(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        R, "listener_processes_by_nonce", _fake_ps({LIVE: [11, 12], DEAD: [21]})
    )
    monkeypatch.setattr(R, "_known_lease_nonces", lambda _root: {LIVE})
    seen = R.orphaned_listeners(tmp_path)
    assert seen["listeners"] == 3 and seen["generations"] == 2, seen
    assert seen["orphans"] == [21], seen
    assert seen["orphan_generations"] == [DEAD], seen


# --------------------------------------------------------------------------
# ★ project scope: the defect that made this dangerous
# --------------------------------------------------------------------------


def test_listeners_of_other_projects_are_never_enumerated(monkeypatch) -> None:
    """The process table is machine-wide; lease records are per-project.

    Comparing an unscoped listing against ONE project's journal makes every
    OTHER project's healthy listeners look orphaned. That is not theoretical:
    the first version of this module did exactly that and SIGTERM'd ~19 live
    listeners belonging to three other projects on the same machine, each of
    which was running correctly under its own controller.
    """
    listing = _ps_python_row(101) + "\n" + _ps_python_row(102) + "\n"
    monkeypatch.setattr(
        R.subprocess, "run", lambda *a, **k: type("P", (), {"stdout": listing})()
    )
    _fake_argv(
        monkeypatch,
        {
            101: [
                "python3", "/s/goalflight_messages.py", "supervise",
                "--project-root", "/repos/mine", "--lease-nonce", DEAD,
            ],
            102: [
                "python3", "/s/goalflight_messages.py", "listen",
                "--project-root", "/repos/other", "--lease-nonce", DEAD,
            ],
        },
    )
    monkeypatch.setattr(R.goalflight_compat, "process_start_identity", _fake_identity)
    got = R.listener_processes_by_nonce(Path("/repos/mine"))
    assert got == {DEAD: [{"pid": 101, "start_token": "test:101"}]}, (
        f"only this project's listener may be enumerated, got {got}"
    )


def test_a_foreign_projects_live_generation_is_not_reapable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """End to end: a foreign listener must survive even with an empty journal."""
    listing = _ps_python_row(201) + "\n"
    monkeypatch.setattr(
        R.subprocess, "run", lambda *a, **k: type("P", (), {"stdout": listing})()
    )
    _fake_argv(
        monkeypatch,
        {
            201: [
                "python3", "/s/goalflight_messages.py", "supervise",
                "--project-root", "/repos/other", "--lease-nonce", DEAD,
            ],
        },
    )
    monkeypatch.setattr(R.goalflight_compat, "process_start_identity", _fake_identity)
    monkeypatch.setattr(R, "_known_lease_nonces", lambda _root: set())
    killed: list[int] = []
    monkeypatch.setattr(os, "kill", lambda pid, sig: killed.append(pid))
    out = R.reap_orphaned_listeners(tmp_path)
    assert killed == [], f"a foreign project's listener must not be signalled: {killed}"
    assert out["reaped"] == 0, out


def test_reused_pid_with_new_start_token_is_not_signalled(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        R,
        "listener_processes_by_nonce",
        lambda _root: {DEAD: [{"pid": 777, "start_token": "old"}]},
    )
    monkeypatch.setattr(R, "_known_lease_nonces", lambda _root: {LIVE})
    monkeypatch.setattr(
        R.goalflight_compat,
        "process_start_identity",
        lambda pid: {"pid": pid, "start_token": "new"},
    )
    killed: list[int] = []
    monkeypatch.setattr(R.os, "kill", lambda pid, sig: killed.append(pid))
    out = R.reap_orphaned_listeners(tmp_path)
    assert killed == [] and out["reaped"] == 0, out
    assert out["refused_identity"] == [{"pid": 777, "why": "identity-unverified"}], out


def test_project_root_with_spaces_uses_exact_argv_boundaries(monkeypatch) -> None:
    root = "/repos/project with spaces"
    listing = _ps_python_row(303) + "\n"
    monkeypatch.setattr(
        R.subprocess,
        "run",
        lambda *a, **k: type("P", (), {"returncode": 0, "stdout": listing})(),
    )
    _fake_argv(
        monkeypatch,
        {
            303: [
                "python3", "/s/goalflight_messages.py", "supervise",
                "--project-root", root, "--lease-nonce", DEAD,
            ],
        },
    )
    monkeypatch.setattr(R.goalflight_compat, "process_start_identity", _fake_identity)

    got = R.listener_processes_by_nonce(Path(root))

    assert got == {DEAD: [{"pid": 303, "start_token": "test:303"}]}, got


@pytest.mark.parametrize(
    ("argc", "payload", "expected"),
    [
        (
            2,
            b"/bin/python\0\0python\0script with spaces\0" + b"X=" + b"x" * 8192 + b"\0",
            ["python", "script with spaces"],
        ),
        # Empty argv[0] cannot be distinguished from exec-path padding. With
        # too few remaining strings, the only safe interpretation is unknown.
        (2, b"/bin/python\0\0\0script\0", None),
        # Empty argv[0] followed by padding: the skip shifts the list onto an
        # environment string or trailing padding. argv[0] is then not Python,
        # so the result must be unknown rather than a shifted argv.
        (2, b"/bin/python\0\0\0\0" + b"3\0K=V\0", None),
        (2, b"/bin/python\0\0\0\0" + b"3\0\0\0", None),
        (2, b"/bin/python\0\0python\0unterminated", None),
        (0, b"/bin/python\0python\0", None),
        (65536, b"/bin/python\0python\0", None),
    ],
)
def test_macos_argv_buffer_and_parser(monkeypatch, argc, payload, expected) -> None:
    raw = argc.to_bytes(4, sys.byteorder, signed=True) + payload
    calls = []

    def sysctl(mib, mib_len, buffer, size, new, new_len):
        assert list(mib) == [1, 49, 123]
        assert mib_len == 3
        calls.append(buffer is None)
        if buffer is not None:
            assert size._obj.value == len(raw)
            R.ctypes.memmove(buffer, raw, len(raw))
        size._obj.value = len(raw)
        return 0

    monkeypatch.setattr(R.sys, "platform", "darwin")
    monkeypatch.setattr(
        R.ctypes, "CDLL",
        lambda *a, **k: type("Libc", (), {"sysctl": staticmethod(sysctl)})(),
    )

    assert R._process_argv(123) == expected
    assert calls == [True, False]


@pytest.mark.skipif(sys.platform != "darwin", reason="exercises KERN_PROCARGS2 on macOS")
def test_real_python_listener_with_spaces_is_parsed_exactly(tmp_path: Path) -> None:
    project_root = tmp_path / "project with spaces"
    project_root.mkdir()
    script = tmp_path / "goalflight_messages.py"
    script.write_text("import time; time.sleep(30)\n", encoding="utf-8")
    victim = subprocess.Popen(
        [
            sys.executable,
            str(script),
            "supervise",
            "--project-root",
            str(project_root),
            "--lease-nonce",
            DEAD,
        ],
        env={**os.environ, "GOALFLIGHT_TEST_LARGE_ENV": "x" * 8192},
    )
    try:
        assert R._process_argv(victim.pid) == [
            sys.executable,
            str(script),
            "supervise",
            "--project-root",
            str(project_root),
            "--lease-nonce",
            DEAD,
        ]
        # sysctl can read our child even when the sandbox denies ps. Always
        # exercise argv above; verify full enumeration where ps is available.
        if _ps_liveness_available(os.getpid()):
            deadline = time.monotonic() + 5
            got = None
            while time.monotonic() < deadline:
                got = R.listener_processes_by_nonce(project_root)
                if got and got.get(DEAD) and got[DEAD][0]["pid"] == victim.pid:
                    break
                time.sleep(0.05)
            assert got is not None and DEAD in got, got
            assert got[DEAD][0]["pid"] == victim.pid, got
            assert got[DEAD][0]["start_token"], got
    finally:
        victim.terminate()
        victim.wait(timeout=10)
