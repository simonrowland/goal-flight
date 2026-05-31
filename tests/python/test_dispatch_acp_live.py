#!/usr/bin/env python3
"""Gated live smoke for real codex-acp dispatch through goalflight_dispatch.py."""

from __future__ import annotations

from support import skip_posix_on_native_windows

skip_posix_on_native_windows("live ACP dispatch is WSL/POSIX-only")

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
DISPATCH = ROOT / "scripts" / "goalflight_dispatch.py"
STATUS = ROOT / "scripts" / "goalflight_status.py"


def _dispatch_end(stdout: str) -> dict:
    for line in stdout.splitlines():
        if line.startswith("DISPATCH-END "):
            return json.loads(line.split(" ", 1)[1])
    raise AssertionError(f"missing DISPATCH-END in stdout:\n{stdout}")


def _record(payload: dict, dispatch_id: str) -> dict | None:
    for row in payload["dispatch"].get("records", []):
        if row.get("dispatch_id") == dispatch_id:
            return row
    return None


def _leases(payload: dict, dispatch_id: str) -> list[dict]:
    return [
        lease
        for lease in payload["capacity_state"].get("leases", {}).values()
        if lease.get("dispatch_id") == dispatch_id
    ]


def main() -> int:
    if os.environ.get("GOALFLIGHT_ACP_LIVE") != "1":
        print("SKIP: set GOALFLIGHT_ACP_LIVE=1 to run real codex-acp dispatch smoke")
        return 0
    if not shutil.which("codex-acp"):
        print("FAIL: GOALFLIGHT_ACP_LIVE=1 but codex-acp is not on PATH")
        return 1

    with tempfile.TemporaryDirectory(prefix="goalflight-acp-live-") as td:
        tmp = Path(td)
        env = os.environ.copy()
        env["GOALFLIGHT_STATE_DIR"] = str(tmp / "state")
        env["GOAL_FLIGHT_PIDFILE_DIR"] = str(tmp / "pids")
        dispatch_id = "live-codex-acp-dispatch"
        status_path = tmp / "status.json"
        proc = subprocess.run(
            [
                sys.executable,
                str(DISPATCH),
                "--shape",
                "acp",
                "--agent",
                "codex-acp",
                "--dispatch-id",
                dispatch_id,
                "--cwd",
                str(ROOT),
                "--prompt",
                "Reply exactly: COMPLETE: codex-acp live smoke",
                "--status-json",
                str(status_path),
                "--poll-secs",
                "0.2",
                "--max-idle-secs",
                "300",
            ],
            cwd=ROOT,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=420,
        )
        end = _dispatch_end(proc.stdout)
        assert proc.returncode == 0, f"dispatch rc={proc.returncode}\nstdout={proc.stdout}\nstderr={proc.stderr}"
        assert end.get("terminal_state") == "complete", end
        assert end.get("worker_pid"), end

        status = subprocess.run(
            [sys.executable, str(STATUS), "--json"],
            cwd=ROOT,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
        )
        aggregate = json.loads(status.stdout)
        row = _record(aggregate, dispatch_id)
        assert row and row.get("state") == "complete", row
        assert row.get("terminal_state") == "complete", row
        assert row.get("shape") == "acp", row
        assert row.get("worker_pid") == end.get("worker_pid"), (row, end)
        leases = _leases(aggregate, dispatch_id)
        assert leases, "lease missing"
        assert all(lease.get("state") == "complete" for lease in leases), leases
        assert all(lease.get("released_at") for lease in leases), leases
    print("OK: live codex-acp dispatch smoke passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
