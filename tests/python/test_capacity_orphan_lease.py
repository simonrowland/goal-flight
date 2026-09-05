"""A lease with no worker is reclaimable when its claimant is gone.

Measured 2026-08-31: 29 active leases had worker=none, claimant=dead,
controller=alive -- oldest 14.2 hours. `stale_active_leases` keyed the
no-worker branch on the CONTROLLER pid, and a controller is a long-running
session that outlives every lease it requests, so those leases were immortal.

20 of them were held by dispatches whose own state was `queued`. Queued work
therefore held the capacity that queued work needed in order to launch, and the
codex pool sat at 31/30 while grok-code sat at 2/30. `release-stale` reclaimed
nothing, correctly by its own logic and uselessly in practice.

Three cases pin the fix, and the middle one is why the controller check existed
at all: the acquire-then-spawn window must stay protected.
"""

from __future__ import annotations

import datetime as dt
import io
import json
import os
import sys
import tempfile
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

_FAILS: list[str] = []


def check(name: str, cond: bool) -> None:
    print(f"{'PASS' if cond else 'FAIL'}  {name}")
    if not cond:
        _FAILS.append(name)


# check() records every failed condition rather than raising on the first, so
# one run reports all of them; main() turns that into exit 1. pytest never
# reaches main(), so this teardown reports them to pytest too -- without it,
# `pytest <file>::<node>` prints "passed" for a failing assertion.
_REPORTED_FAILS = 0


def teardown_function(function) -> None:
    del function
    global _REPORTED_FAILS
    if len(_FAILS) > _REPORTED_FAILS:
        new = _FAILS[_REPORTED_FAILS:]
        _REPORTED_FAILS = len(_FAILS)
        raise AssertionError(f"{len(new)} failed check(s): {new}")


def _reaped_pid() -> int:
    """A pid that is definitely gone: forked, exited, and waited on."""
    pid = os.fork()
    if pid == 0:  # pragma: no cover - child exits immediately
        os._exit(0)
    os.waitpid(pid, 0)
    return pid


def _capacity_module():
    import importlib

    os.environ["GOALFLIGHT_STATE_DIR"] = tempfile.mkdtemp(prefix="gf-cap-orphan-")
    import goalflight_capacity

    return importlib.reload(goalflight_capacity)


def _lease(lease_id: str, *, worker, claimant, controller) -> dict:
    return {
        "lease_id": lease_id,
        "agent": "codex",
        "state": "active",
        "worker_pid": worker,
        "claimant_pid": claimant,
        "controller_pid": controller,
    }


def test_orphaned_lease_is_stale_while_acquiring_and_working_are_not() -> None:
    cap = _capacity_module()
    dead = _reaped_pid()
    live = os.getpid()

    data = {
        "leases": {
            # No worker ever attached and the acquiring process is gone. Nothing
            # can attach one now, whatever the requesting controller is doing.
            "orphan": _lease("orphan", worker=None, claimant=dead, controller=live),
            # The acquire-then-spawn window. A live claimant is about to attach a
            # worker; reclaiming here would race a legitimate launch.
            "acquiring": _lease("acquiring", worker=None, claimant=live, controller=live),
            # Doing actual work.
            "working": _lease("working", worker=live, claimant=live, controller=live),
        }
    }

    stale = {row["lease_id"] for row in cap.stale_active_leases(data)}

    check("orphaned lease (claimant dead, no worker) is reclaimable", "orphan" in stale)
    check("acquire-then-spawn window is protected", "acquiring" not in stale)
    check("lease with a live worker is never reclaimed", "working" not in stale)


def test_a_live_controller_does_not_keep_an_orphaned_lease_alive() -> None:
    """The regression itself: controller liveness must not decide this."""
    cap = _capacity_module()
    dead = _reaped_pid()

    data = {
        "leases": {
            "orphan": _lease("orphan", worker=None, claimant=dead, controller=os.getpid()),
        }
    }
    stale = {row["lease_id"] for row in cap.stale_active_leases(data)}
    check("a live controller no longer pins an orphaned lease", "orphan" in stale)


def test_unprobeable_claimant_is_not_reclaimed() -> None:
    """An indeterminate read must not authorise a destructive act.

    pid 1 exists but is not ours; the probe cannot prove it gone, so the lease
    holds. Reclaiming on "cannot tell" is the failure mode this whole change is
    meant to remove, not an acceptable cost of it.
    """
    cap = _capacity_module()
    data = {"leases": {"unknown": _lease("unknown", worker=None, claimant=1, controller=1)}}
    stale = {row["lease_id"] for row in cap.stale_active_leases(data)}
    check("unprobeable claimant holds its lease", "unknown" not in stale)


def test_aged_unprobeable_claimant_is_not_released_at_any_age() -> None:
    """Age plus an unprobeable claimant is not proof the holder is gone.

    release-stale may drop a lease only when the claimant is measured dead.
    An EPERM/unreadable claimant stays held, however old, and status reports
    it as unknown_claimant rather than converting unknown into stale.
    """
    cap = _capacity_module()
    now = cap.utc_now()
    lease = _lease(
        "aged-unknown",
        worker=None,
        claimant=1,
        controller=_reaped_pid(),
    )
    lease["started_at"] = cap.iso(
        now - dt.timedelta(seconds=cap.INDETERMINATE_LIVE_RETENTION_S + 120)
    )
    lease["expires_at"] = cap.iso(now - dt.timedelta(seconds=1))
    data = {"leases": {"aged-unknown": lease}, "cooldowns": {}}

    check(
        "aged unprobeable claimant is not stale",
        lease not in cap.stale_active_leases(data),
    )
    cap.prune_state(data)
    kept = data["leases"].get("aged-unknown")
    check(
        "aged unprobeable claimant survives TTL prune",
        kept is not None and kept.get("state") == "active",
    )

    cap.save_state(
        {
            "schema": cap.SCHEMA,
            "machine_id": cap.machine_id(),
            "leases": data["leases"],
            "cooldowns": {},
        }
    )
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = cap.main(["release-stale", "--keep"])
    payload = json.loads(buf.getvalue())
    check("release-stale of unknown claimant exits 0", rc == 0)
    check(
        "release-stale does not drop an unprobeable claimant at any age",
        "aged-unknown" not in payload.get("released", []),
    )
    on_disk = json.loads(cap.state_path().read_text())
    check(
        "unknown claimant lease remains active after release-stale",
        on_disk.get("leases", {}).get("aged-unknown", {}).get("state") == "active",
    )

    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = cap.main(["status", "--json", "--ram-mb", "65536"])
    status = json.loads(buf.getvalue())
    unknown_ids = {
        row.get("lease_id") for row in (status.get("unknown_claimant") or [])
    }
    check("status reports unknown_claimant", rc == 0 and "aged-unknown" in unknown_ids)

    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = cap.main(["release-stale", "--keep", "--include-unknown-claimant"])
    forced = json.loads(buf.getvalue())
    check("operator flag exits 0", rc == 0)
    check(
        "operator flag is the only age-independent unknown-claimant release",
        "aged-unknown" in forced.get("released", []),
    )


def main() -> int:
    test_orphaned_lease_is_stale_while_acquiring_and_working_are_not()
    test_a_live_controller_does_not_keep_an_orphaned_lease_alive()
    test_unprobeable_claimant_is_not_reclaimed()
    test_aged_unprobeable_claimant_is_not_released_at_any_age()
    if _FAILS:
        print(f"\n{len(_FAILS)} FAILED: {_FAILS}")
        return 1
    print("\nall capacity orphan-lease tests passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
