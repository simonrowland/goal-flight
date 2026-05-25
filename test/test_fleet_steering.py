"""Tests for fleet steering lifecycle (A5) and messaging hook (C6)."""

from __future__ import annotations

import json
import sys
import threading
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import goalflight_fleet as fleet  # noqa: E402
import goalflight_fleet_steering as steering  # noqa: E402
import goalflight_messages as messages  # noqa: E402


def _fleet_tmp() -> Path:
    path = Path("/tmp") / f"goal-flight-steer-{uuid.uuid4().hex}"
    fleet.bootstrap(path)
    return path


def test_steering_survives_restart():
    fleet_dir = _fleet_tmp()
    proposal = steering.propose_steering(
        fleet_dir,
        patch=[{"op": "add", "path": "/node_policy/priority/0", "value": "build-1"}],
        reason="test prefer build-1",
        created_by={"controller_id": "test", "host_adapter": "pytest"},
    )
    with fleet.RegistryLock(fleet_dir):
        result = steering.apply_proposal(fleet_dir, proposal["proposal_id"])
    assert result["ok"] is True
    reloaded = steering.load_steering_doc(fleet_dir)
    assert reloaded["node_policy"]["priority"][0] == "build-1"


def test_concurrent_apply_fails_closed():
    fleet_dir = _fleet_tmp()
    proposal = steering.propose_steering(
        fleet_dir,
        patch=[{"op": "add", "path": "/node_policy/priority/0", "value": "build-2"}],
        reason="race test",
        created_by={"controller_id": "test", "host_adapter": "pytest"},
    )
    errors: list[str] = []

    def worker():
        try:
            with fleet.RegistryLock(fleet_dir):
                steering.apply_proposal(fleet_dir, proposal["proposal_id"])
        except steering.SteeringError as exc:
            errors.append(str(exc))

    t1 = threading.Thread(target=worker)
    t2 = threading.Thread(target=worker)
    t1.start()
    t2.start()
    t1.join()
    t2.join()
    assert len(errors) >= 1


def test_steering_apply_writes_envelope():
    fleet_dir = _fleet_tmp()
    messages_dir = fleet_dir / "messages"
    proposal = steering.propose_steering(
        fleet_dir,
        patch=[{"op": "add", "path": "/node_policy/priority/0", "value": "build-3"}],
        reason="envelope test",
        created_by={"controller_id": "test", "host_adapter": "pytest"},
    )
    with fleet.RegistryLock(fleet_dir):
        steering.apply_proposal(fleet_dir, proposal["proposal_id"])
    register = messages.steering_register_path(fleet_dir)
    assert register.exists()
    envelopes = messages.read_envelopes(register)
    assert any(env.get("type") == "steering" for env in envelopes)
    aggregate = json.loads((fleet_dir / "register" / "aggregate.json").read_text())
    assert aggregate.get("last_steering") is not None


def test_mirror_remote_user_need():
    fleet_dir = _fleet_tmp()
    remote = Path("/tmp") / f"goal-flight-remote-{fleet.controller_id().replace(':', '-')}.jsonl"
    dispatch_id = "remote-dispatch-1"
    envelope = {
        "schema": "goalflight.message.v1",
        "schema_version": 1,
        "id": "env-1",
        "dispatch_id": dispatch_id,
        "seq": 1,
        "ts": "2026-05-24T12:00:00+00:00",
        "source": {"node": "remote", "adapter": "codex", "transport": "acp"},
        "type": "user_need",
        "payload": {"text": "approve deploy?"},
    }
    remote.write_text(json.dumps(envelope) + "\n")
    try:
        result = messages.merge_remote_register(fleet_dir, remote)
        assert result["appended"] == 1
        aggregate = messages.build_aggregate(
            messages_dir=messages.default_messages_dir(),
            fleet_dir=fleet_dir,
        )
        assert any(item.get("dispatch_id") == dispatch_id for item in aggregate["open_user_needs"])
    finally:
        remote.unlink(missing_ok=True)


def _run_tests():
    failed = []
    passed = 0
    for name, fn in sorted(globals().items()):
        if not name.startswith("test_") or not callable(fn):
            continue
        try:
            fn()
            passed += 1
        except Exception as exc:
            failed.append((name, str(exc)))
    return passed, failed


if __name__ == "__main__":
    passed, failed = _run_tests()
    if failed:
        print(f"FAIL test/test_fleet_steering.py ({len(failed)} failed of {passed + len(failed)})")
        for name, err in failed:
            print(f"  - {name}: {err}")
        sys.exit(1)
    print(f"PASS test/test_fleet_steering.py ({passed} tests)")
