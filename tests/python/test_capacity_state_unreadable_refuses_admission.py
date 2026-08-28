#!/usr/bin/env python3
"""An unreadable capacity.json REFUSES admission, never a false empty floor.

``load_state`` used to catch OSError/JSONDecodeError and return zero leases,
so ``cmd_acquire`` granted a new lease and persisted it over the live map.
Unknown on the admission path must refuse; a genuinely missing file still
admits. The refusal reason is ``capacity_state_unreadable``, distinct from
``machine_worker_cap`` (t-068).
"""

from __future__ import annotations

import contextlib
import datetime as dt
import io
import json
import os
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import goalflight_capacity as cap  # noqa: E402
import goalflight_dispatch as dispatch  # noqa: E402
import goalflight_session_status as sessions  # noqa: E402


def _project(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    root.mkdir()
    return root


def _seed_lease(project: Path, lease_id: str = "live-lease-1") -> dict:
    now = cap.utc_now()
    lease = {
        "lease_id": lease_id,
        "dispatch_id": f"disp-{lease_id}",
        "agent": "codex",
        "state": "active",
        "worker_pid": None,
        "controller_pid": os.getpid(),
        "mem_mb": 386,
        "project_root": str(project),
        "started_at": cap.iso(now),
        "expires_at": cap.iso(now + dt.timedelta(hours=1)),
    }
    cap.save_state(
        {
            "schema": cap.SCHEMA,
            "machine_id": cap.machine_id(),
            "leases": {lease["lease_id"]: lease},
            "cooldowns": {},
        }
    )
    return lease


def _acquire(*extra: str) -> tuple[int, dict]:
    argv = [
        "acquire",
        "--agent",
        "codex",
        "--ram-mb",
        "65536",
        "--ttl-s",
        "3600",
        "--max-total",
        "1",
        "--agent-cap",
        "1",
        *extra,
    ]
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        rc = cap.main(argv)
    return rc, json.loads(out.getvalue() or "{}")


def stat_mode(path: Path) -> int:
    return path.stat().st_mode & 0o777


@contextlib.contextmanager
def unreadable(path: Path):
    previous = stat_mode(path)
    os.chmod(path, 0o000)
    try:
        yield
    finally:
        os.chmod(path, previous)


def test_unreadable_capacity_state_refuses_admission_and_does_not_clobber(
    tmp_path: Path,
) -> None:
    project = _project(tmp_path)
    lease = _seed_lease(project)
    state_file = cap.state_path()
    assert state_file.is_file()
    before = state_file.read_text(encoding="utf-8")

    with unreadable(state_file):
        rc, payload = _acquire("--lease-id", "should-not-admit")
        assert rc == 2, payload
        assert payload["decision"] == "wait", payload
        assert payload["reason"] == "capacity_state_unreadable", payload
        assert payload.get("measured") is False
        assert payload["reason"] != "machine_worker_cap"
        assert payload["reason"] != "agent_worker_cap"
        assert payload["reason"] != "capacity_unavailable"
        try:
            data = cap.load_state()
        except Exception as exc:
            assert type(exc).__name__ == "CapacityStateUnreadable"
        else:
            pytest.fail(
                f"load_state returned leases={list((data.get('leases') or {}).keys())}"
            )

    after = state_file.read_text(encoding="utf-8")
    assert after == before, "unreadable acquire must not persist a false floor"
    restored = cap.load_state()
    assert lease["lease_id"] in restored["leases"]
    assert "should-not-admit" not in restored["leases"]


def test_corrupt_capacity_json_refuses_admission(tmp_path: Path) -> None:
    project = _project(tmp_path)
    _seed_lease(project)
    state_file = cap.state_path()
    state_file.write_text("{not json", encoding="utf-8")
    rc, payload = _acquire("--lease-id", "from-corrupt")
    assert rc == 2, payload
    assert payload["reason"] == "capacity_state_unreadable", payload
    assert payload["decision"] == "wait"
    try:
        cap.load_state()
        pytest.fail("load_state accepted corrupt JSON")
    except Exception as exc:
        assert type(exc).__name__ == "CapacityStateUnreadable"
    raw = state_file.read_text(encoding="utf-8")
    assert "{not json" in raw
    assert "from-corrupt" not in raw


def test_absent_capacity_state_still_admits(tmp_path: Path) -> None:
    project = _project(tmp_path)
    state_file = cap.state_path()
    if state_file.exists():
        state_file.unlink()
    data = cap.load_state()
    assert data["leases"] == {}
    rc, payload = _acquire("--lease-id", "first-lease", "--project-root", str(project))
    assert rc == 0, payload
    assert payload["decision"] == "allow"
    assert payload["lease"]["lease_id"] == "first-lease"
    assert cap.load_state()["leases"]["first-lease"]["state"] == "active"


def test_unreadable_refusal_is_distinct_from_cap_reached(tmp_path: Path) -> None:
    project = _project(tmp_path)
    _seed_lease(project)
    rc_cap, cap_payload = _acquire("--lease-id", "over-cap")
    assert rc_cap == 2, cap_payload
    assert cap_payload["reason"] == "machine_worker_cap"
    assert cap_payload["decision"] == "wait"

    with unreadable(cap.state_path()):
        rc_unk, unk_payload = _acquire("--lease-id", "over-unreadable")
    assert rc_unk == 2, unk_payload
    assert unk_payload["reason"] == "capacity_state_unreadable"
    assert unk_payload["reason"] != cap_payload["reason"]
    assert "active" not in unk_payload or unk_payload.get("measured") is False


def test_unreadable_capacity_file_makes_status_probe_unknown(tmp_path: Path) -> None:
    project = _project(tmp_path)
    _seed_lease(project)
    leases, error = sessions._active_leases_for(project)
    assert error is None and leases

    with unreadable(cap.state_path()):
        leases, error = sessions._active_leases_for(project)
        assert leases is None, "unreadable file is UNKNOWN, not a measured zero"
        assert error
        status = sessions.aggregate_status(project)
        assert status["capacity_probe_measured"] is False
        assert status["active_capacity_leases_in_project"] is None
        assert "capacity_leases=unknown" in sessions.to_text(status)


def test_drain_prefers_unreadable_reason_over_capacity_unavailable() -> None:
    both = (
        'DISPATCH-BLOCKED {"reason": {"reason": "capacity_state_unreadable"},'
        ' "state": "blocked_capacity"}\n'
    )
    assert (
        dispatch._drain_launch_capacity_reason(2, both, "")
        == "capacity_state_unreadable"
    )
    cap_only = (
        'DISPATCH-BLOCKED {"state": "blocked_capacity",'
        ' "reason": {"reason": "machine_worker_cap"}}\n'
    )
    assert (
        dispatch._drain_launch_capacity_reason(2, cap_only, "")
        == "capacity_unavailable"
    )
    assert dispatch._drain_launch_capacity_reason(0, both, "") is None
