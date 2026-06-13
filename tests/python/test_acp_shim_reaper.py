#!/usr/bin/env python3
"""Hermetic tests for orphaned claude-acp shim reaper predicates."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import goalflight_acp_client  # noqa: E402


SHIM_PATH = "/opt/homebrew/lib/node_modules/claude-code-cli-acp-darwin-arm64/bin/claude-code-cli-acp"


def _fake_rows() -> list[dict]:
    return [
        {"pid": 101, "ppid": 1, "comm": "claude-code-cli-acp", "age_s": 1200.0},
        {"pid": 102, "ppid": 4242, "comm": "claude-code-cli-acp", "age_s": 1200.0},
        {"pid": 103, "ppid": 1, "comm": "claude-code-cli-acp", "age_s": 1200.0},
        {"pid": 104, "ppid": 1, "comm": "claude-code-cli-acp", "age_s": 30.0},
        {"pid": 105, "ppid": 1, "comm": "SkyComputerUseClient", "age_s": 1200.0},
        {
            "pid": 106,
            "ppid": 1,
            "comm": "helper",
            "age_s": 1200.0,
        },
    ]


def _own_all(_pid: int) -> bool:
    """Provenance stub: every candidate is goal-flight-owned (no shell-out)."""
    return True


def case_reaper_selects_only_qualifying_orphans() -> None:
    killed: list[int] = []

    def fake_terminate(pgid: int) -> str:
        killed.append(pgid)
        return "SIGTERM+SIGKILL"

    tracked = {103}
    with patch(
        "goalflight_acp_client._claude_acp_shim_executable_paths",
        return_value={SHIM_PATH},
    ), patch.dict(os.environ, {}, clear=False):
        os.environ.pop("GOALFLIGHT_NO_SHIM_REAP", None)
        result = goalflight_acp_client.reap_orphaned_acp_shims(
            active_worker_pids=tracked,
            ttl_s=600.0,
            process_rows=_fake_rows(),
            terminate_group=fake_terminate,
            provenance_check=_own_all,
        )
    assert killed == [101], f"expected only pid 101 reaped, got {killed}"
    assert len(result["reaped"]) == 1
    assert result["reaped"][0]["pid"] == 101


def case_reaper_does_not_reap_foreign_editor_orphan() -> None:
    """NO-GO regression: an editor-launched orphan (no goal-flight marker) that is
    a shim, ppid==1, age>TTL, and not ledger-tracked MUST NOT be reaped.

    This test FAILS if the provenance gate is removed (the reaper would then kill
    pid 101, an editor/foreign shim). The provenance stub denies the foreign pid
    and allows nothing else, so nothing is reaped.
    """
    killed: list[int] = []

    def fake_terminate(pgid: int) -> str:
        killed.append(pgid)
        return "SIGTERM+SIGKILL"

    # pid 101 qualifies on every round-1 predicate but is editor-launched: no
    # goal-flight owner marker in its env -> provenance default-denies.
    def no_provenance(_pid: int) -> bool:
        return False

    tracked = {103}
    with patch(
        "goalflight_acp_client._claude_acp_shim_executable_paths",
        return_value={SHIM_PATH},
    ), patch.dict(os.environ, {}, clear=False):
        os.environ.pop("GOALFLIGHT_NO_SHIM_REAP", None)
        result = goalflight_acp_client.reap_orphaned_acp_shims(
            active_worker_pids=tracked,
            ttl_s=600.0,
            process_rows=_fake_rows(),
            terminate_group=fake_terminate,
            provenance_check=no_provenance,
        )
    assert killed == [], f"foreign editor orphan must NOT be reaped, got {killed}"
    assert result["reaped"] == []
    assert 101 not in {row["pid"] for row in result["candidates"]}


def case_reaper_reaps_goalflight_launched_orphan() -> None:
    """A goal-flight-launched orphan carries the owner marker -> reaped."""
    killed: list[int] = []

    def fake_terminate(pgid: int) -> str:
        killed.append(pgid)
        return "SIGTERM+SIGKILL"

    # Only pid 101 is goal-flight-owned; the rest are denied.
    def owns_101(pid: int) -> bool:
        return pid == 101

    tracked = {103}
    with patch(
        "goalflight_acp_client._claude_acp_shim_executable_paths",
        return_value={SHIM_PATH},
    ), patch.dict(os.environ, {}, clear=False):
        os.environ.pop("GOALFLIGHT_NO_SHIM_REAP", None)
        result = goalflight_acp_client.reap_orphaned_acp_shims(
            active_worker_pids=tracked,
            ttl_s=600.0,
            process_rows=_fake_rows(),
            terminate_group=fake_terminate,
            provenance_check=owns_101,
        )
    assert killed == [101], f"expected goal-flight-owned pid 101 reaped, got {killed}"
    assert {row["pid"] for row in result["reaped"]} == {101}


def case_reaper_default_denies_on_unreadable_env() -> None:
    """DEFAULT-DENY: when the marker probe can't read a candidate's env (returns
    False, as the real _read_process_environ does on failure), nothing is reaped.
    """
    killed: list[int] = []

    def fake_terminate(pgid: int) -> str:
        killed.append(pgid)
        return "SIGTERM+SIGKILL"

    # Simulate the real default helper's failure mode: env unreadable -> deny.
    def unreadable_env(_pid: int) -> bool:
        return False

    tracked = {103}
    with patch(
        "goalflight_acp_client._claude_acp_shim_executable_paths",
        return_value={SHIM_PATH},
    ), patch.dict(os.environ, {}, clear=False):
        os.environ.pop("GOALFLIGHT_NO_SHIM_REAP", None)
        result = goalflight_acp_client.reap_orphaned_acp_shims(
            active_worker_pids=tracked,
            ttl_s=600.0,
            process_rows=_fake_rows(),
            terminate_group=fake_terminate,
            provenance_check=unreadable_env,
        )
    assert killed == [], f"unreadable env must default-deny, got {killed}"
    assert result["reaped"] == []


def case_provenance_default_denies_when_env_missing_marker() -> None:
    """Unit-level: _shim_has_goalflight_provenance is default-deny.

    None env (unreadable) -> False; env without the marker -> False; env WITH the
    marker -> True. Injected via _read_process_environ so no real ps shell-out.
    """
    marker = goalflight_acp_client.GOALFLIGHT_ACP_SHIM_OWNER_ENV
    with patch("goalflight_acp_client._read_process_environ", return_value=None):
        assert goalflight_acp_client._shim_has_goalflight_provenance(999) is False
    with patch(
        "goalflight_acp_client._read_process_environ",
        return_value="claude-code-cli-acp acp PATH=/usr/bin HOME=/Users/x",
    ):
        assert goalflight_acp_client._shim_has_goalflight_provenance(999) is False
    with patch(
        "goalflight_acp_client._read_process_environ",
        return_value=f"claude-code-cli-acp acp {marker}=goal-flight:42 PATH=/usr/bin",
    ):
        assert goalflight_acp_client._shim_has_goalflight_provenance(999) is True


def case_reaper_opt_out_is_noop() -> None:
    killed: list[int] = []

    def fake_terminate(pgid: int) -> str:
        killed.append(pgid)
        return "SIGTERM"

    with patch.dict(os.environ, {"GOALFLIGHT_NO_SHIM_REAP": "1"}):
        result = goalflight_acp_client.reap_orphaned_acp_shims(
            process_rows=_fake_rows(),
            terminate_group=fake_terminate,
        )
    assert result.get("skipped") == "GOALFLIGHT_NO_SHIM_REAP"
    assert killed == []
    assert result["reaped"] == []


def case_count_orphans_ignores_ttl() -> None:
    tracked = {103}
    # Count enumerates ALL orphans (no TTL, no provenance filter); the provenance
    # stub only LABELS the reapable subset. pid 104 is foreign (not owned).
    def owns_101(pid: int) -> bool:
        return pid == 101

    with patch(
        "goalflight_acp_client._claude_acp_shim_executable_paths",
        return_value={SHIM_PATH},
    ):
        payload = goalflight_acp_client.count_orphaned_acp_shims(
            active_worker_pids=tracked,
            process_rows=_fake_rows(),
            provenance_check=owns_101,
        )
    orphan_pids = {item["pid"] for item in payload["orphans"]}
    assert payload["orphan_count"] == 2
    assert orphan_pids == {101, 104}
    # The total is labelled as possibly including foreign shims, and the reapable
    # subset is reported separately so the operator can't read orphan_count as
    # "all reapable".
    assert payload["count_includes_foreign_shims"] is True
    assert payload["reapable_count"] == 1
    owned = {item["pid"]: item["goalflight_owned"] for item in payload["orphans"]}
    assert owned == {101: True, 104: False}


def case_cleanup_ghosts_runs_shim_reaper_when_pidfile_dir_missing() -> None:
    with patch("goalflight_acp_client._PIDFILE_DIR", Path("/tmp/nonexistent-goal-flight-pid-dir")), \
        patch(
            "goalflight_acp_client.reap_orphaned_acp_shims",
            return_value={"reaped": [{"pid": 101, "action": "SIGTERM+SIGKILL"}]},
        ) as reap:
        count = goalflight_acp_client.cleanup_ghosts()
    assert count == 1
    reap.assert_called_once()


def main() -> None:
    case_reaper_selects_only_qualifying_orphans()
    case_reaper_does_not_reap_foreign_editor_orphan()
    case_reaper_reaps_goalflight_launched_orphan()
    case_reaper_default_denies_on_unreadable_env()
    case_provenance_default_denies_when_env_missing_marker()
    case_reaper_opt_out_is_noop()
    case_count_orphans_ignores_ttl()
    case_cleanup_ghosts_runs_shim_reaper_when_pidfile_dir_missing()
    print("OK: ACP shim reaper tests pass")


if __name__ == "__main__":
    main()