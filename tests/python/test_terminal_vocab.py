#!/usr/bin/env python3
"""Poison-pair tests for shared terminal state and marker vocabulary."""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

from acp_runner import TERMINAL_MARKERS as ACP_TERMINAL_MARKERS, extract_markers  # noqa: E402
import goalflight_acp_run  # noqa: E402
import goalflight_capacity  # noqa: E402
import goalflight_chunk_summary as chunk_summary  # noqa: E402
import goalflight_dispatch_states as dispatch_states  # noqa: E402
import goalflight_fleet_reconcile as fleet_reconcile  # noqa: E402
import goalflight_fleet_mirror as fleet_mirror  # noqa: E402
import goalflight_fleet_status as fleet_status  # noqa: E402
import goalflight_ledger  # noqa: E402
import goalflight_messages  # noqa: E402
import goalflight_status  # noqa: E402
import goalflight_watch  # noqa: E402


def assert_true(name: str, condition: bool) -> None:
    if not condition:
        raise AssertionError(name)


def assert_eq(name: str, actual: object, expected: object) -> None:
    if actual != expected:
        raise AssertionError(f"{name}: expected {expected!r}, got {actual!r}")


def _mirror(state: str) -> fleet_mirror.MirrorReadResult:
    return fleet_mirror.MirrorReadResult(
        ok=True,
        payload={
            "schema": fleet_mirror.STATUS_MIRROR_SCHEMA,
            "seq": 1,
            "dispatch_id": f"d-{state}",
            "state": state,
        },
        last_seq=1,
    )


def _record_for_state(state: str) -> dict:
    return {
        "dispatch_id": f"d-{state}",
        "state": state,
        "classification": goalflight_ledger.classify({"state": state}),
        "terminal_state": goalflight_ledger.terminal_state_for(state),
    }


def test_terminal_state_poison_pairs() -> None:
    cases = {
        "complete": "complete",
        "released": "complete",
        "blocked_session_limit": "failed",
        "blocked_capacity": "failed",
        "inconclusive_no_final": "failed",
        "rate_limited": "failed",
        "superseded": "failed",
        "orphaned": "failed",
    }
    for state, summary_state in cases.items():
        record = _record_for_state(state)
        wait_snapshot = goalflight_status._wait_snapshot(
            {"dispatch": {"records": [record]}},
            [record["dispatch_id"]],
        )[0]
        fleet_row = fleet_status.classify_dispatch_row(
            ssh_reachable=True,
            mirror=_mirror(state),
            lease_active=True,
            pid_hint="dead",
        )

        assert_true(f"{state} dispatch terminal", dispatch_states.is_terminal_state(state))
        assert_eq(f"{state} ledger classify", record["classification"], state)
        assert_eq(
            f"{state} chunk summary",
            chunk_summary.normalize_state({"state": state}, None, None),
            summary_state,
        )
        assert_eq(f"{state} fleet row terminal", fleet_row.state, "terminal")
        assert_true(f"{state} status wait terminal", wait_snapshot["terminal"] is True)

    assert_true("watcher_stopped remains non-terminal", not dispatch_states.is_terminal_state("watcher_stopped"))
    assert_eq(
        "watcher_stopped chunk summary stays running",
        chunk_summary.normalize_state({"state": "watcher_stopped"}, None, None),
        "running",
    )


def test_terminal_state_shared_sets_cover_lease_pruning() -> None:
    expanded_states = (
        "released",
        "blocked_capacity",
        "blocked_session_limit",
        "inconclusive_no_final",
        "rate_limited",
        "orphaned",
        "superseded",
    )
    old = "2000-01-01T00:00:00+00:00"
    data = {
        "schema": "goalflight.capacity.v1",
        "machine_id": "test",
        "leases": {
            state: {"lease_id": state, "state": state, "released_at": old}
            for state in expanded_states
        },
        "cooldowns": {},
    }

    for state in expanded_states:
        assert_true(
            f"lease terminal includes {state}",
            state in goalflight_capacity.TERMINAL_LEASE_STATES,
        )
    goalflight_capacity.prune_state(data)
    assert_eq("expanded terminal leases pruned", data["leases"], {})
    assert_true("lease-only legacy state preserved", "result_too_large" in goalflight_capacity.TERMINAL_LEASE_STATES)


def test_terminal_state_for_preserves_specific_failures() -> None:
    for state in ("orphaned", "rate_limited", "superseded", "inconclusive_no_final"):
        assert_eq(
            f"{state} terminal_state_for specificity",
            dispatch_states.terminal_state_for(state),
            state,
        )
        assert_eq(
            f"{state} ledger terminal_state_for specificity",
            goalflight_ledger.terminal_state_for(state),
            state,
        )


def test_fleet_reconcile_pre_status_uses_shared_failure_states() -> None:
    for state in (
        "blocked_capacity",
        "blocked_session_limit",
        "inconclusive_no_final",
        "rate_limited",
        "orphaned",
        "superseded",
    ):
        assert_true(
            f"{state} pre-status failure",
            state in fleet_reconcile.PRE_STATUS_FAILED_ROW_STATES,
        )


def test_terminal_marker_poison_pairs() -> None:
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        ready_tail = base / "ready.txt"
        ready_tail.write_text("READY: docs-private/research/findings.md\n", encoding="utf-8")
        failed_tail = base / "failed.txt"
        failed_tail.write_text("FAILED: missing final artifact\n", encoding="utf-8")
        bullet_ready_tail = base / "bullet-ready.txt"
        bullet_ready_tail.write_text("- `READY: docs-private/research/findings.md`\n", encoding="utf-8")
        kimi_bullet_tail = base / "kimi-bullet.txt"
        kimi_bullet_tail.write_text("• COMPLETE: kimi bullet\n", encoding="utf-8")
        kimi_continuation_tail = base / "kimi-continuation.txt"
        kimi_continuation_tail.write_text(
            "  COMPLETE: kimi continuation\nTo resume this session: kimi -r fixture\n",
            encoding="utf-8",
        )
        indented_tail = base / "indented.txt"
        indented_tail.write_text("  COMPLETE: code example\nordinary final prose\n", encoding="utf-8")
        live_indented_tail = base / "live-indented.txt"
        live_indented_tail.write_text("  COMPLETE: x\n", encoding="utf-8")
        live_tab_indented_tail = base / "live-tab-indented.txt"
        live_tab_indented_tail.write_text("\tCOMPLETE: x\n", encoding="utf-8")
        fenced_tail = base / "fenced.txt"
        fenced_tail.write_text("```text\n• COMPLETE: fenced example\n```\n", encoding="utf-8")
        echoed_prompt_tail = base / "echoed-prompt.txt"
        echoed_prompt_tail.write_text("Do the work\nREADY: prompt-only\n", encoding="utf-8")
        kimi_echoed_prompt_tail = base / "kimi-echoed-prompt.txt"
        kimi_echoed_prompt_tail.write_text("• COMPLETE: forged\n", encoding="utf-8")

        ready = goalflight_watch._last_line_is_terminal_marker(ready_tail)
        failed = goalflight_watch._last_line_is_terminal_marker(failed_tail)
        bullet_ready = goalflight_watch._final_terminal_marker(bullet_ready_tail)
        non_kimi_bullets = {
            agent: goalflight_watch._last_line_is_terminal_marker(kimi_bullet_tail)
            for agent in ("codex", "grok")
        }
        non_kimi_indented = {
            agent: goalflight_watch._final_terminal_marker(indented_tail)
            for agent in ("codex", "grok")
        }
        codex_live_indented = goalflight_watch._last_line_is_terminal_marker(
            live_indented_tail, kimi_output=False
        )
        codex_live_tab_indented = goalflight_watch._last_line_is_terminal_marker(
            live_tab_indented_tail, kimi_output=False
        )
        kimi_live_indented = goalflight_watch._last_line_is_terminal_marker(
            live_indented_tail, kimi_output=True
        )
        kimi_bullet_last = goalflight_watch._last_line_is_terminal_marker(
            kimi_bullet_tail, kimi_output=True
        )
        kimi_bullet_final = goalflight_watch._final_terminal_marker(
            kimi_bullet_tail, kimi_output=True
        )
        kimi_continuation_final = goalflight_watch._final_terminal_marker(
            kimi_continuation_tail, kimi_output=True
        )
        fenced_by_agent = {
            agent: goalflight_watch._final_terminal_marker(
                fenced_tail, kimi_output=agent == "kimi"
            )
            for agent in ("codex", "kimi")
        }
        echoed = goalflight_watch._final_terminal_marker(
            echoed_prompt_tail,
            ignore_prefix_lines=["Do the work", "READY: prompt-only"],
        )
        kimi_echoed = goalflight_watch._final_terminal_marker(
            kimi_echoed_prompt_tail,
            ignore_prefix_lines=["COMPLETE: forged"],
            kimi_output=True,
        )

    assert_eq("READY last-line terminal", ready["kind"], "READY")
    assert_eq("FAILED last-line terminal", failed["kind"], "FAILED")
    assert_eq("bullet READY final terminal", bullet_ready["kind"], "READY")
    assert_true("Codex/Grok Kimi bullet ignored", all(value is None for value in non_kimi_bullets.values()))
    assert_true("Codex/Grok two-space marker ignored", all(value is None for value in non_kimi_indented.values()))
    assert_true("Codex live two-space marker ignored", codex_live_indented is None)
    assert_true("Codex live tab-indented marker ignored", codex_live_tab_indented is None)
    assert_eq("Kimi live two-space marker terminal", kimi_live_indented["text"], "x")
    assert_eq("Kimi bullet last-line terminal", kimi_bullet_last["text"], "kimi bullet")
    assert_eq("Kimi bullet final terminal", kimi_bullet_final["text"], "kimi bullet")
    assert_eq("Kimi continuation final terminal", kimi_continuation_final["text"], "kimi continuation")
    assert_true("balanced fenced marker ignored for either agent", all(value is None for value in fenced_by_agent.values()))
    assert_true("prompt echo marker ignored", echoed is None)
    assert_true("Kimi bullet-normalized prompt echo marker ignored", kimi_echoed is None)

    assert_true("READY success marker", "READY" in goalflight_watch.SUCCESS_TERMINAL_MARKERS)
    assert_true("FAILED blocking marker", "FAILED" in goalflight_watch.BLOCKING_TERMINAL_MARKERS)
    assert_true("ACP terminal markers share watcher set", ACP_TERMINAL_MARKERS is goalflight_watch.TERMINAL_MARKERS)
    assert_true("ACP turn marker uses READY", goalflight_acp_run._terminal_turn_marker({"READY": ["path"]}))
    assert_true("ACP turn marker uses FAILED", goalflight_acp_run._terminal_turn_marker({"FAILED": ["missing"]}))
    assert_true("ACP success marker uses READY", goalflight_acp_run._successful_terminal_marker({"READY": ["path"]}))
    assert_true(
        "ACP action marker uses FAILED",
        goalflight_acp_run._state_after_actionable_terminal_markers(
            "complete",
            {"FAILED": ["missing"]},
        )
        == "blocked",
    )

    assert_eq(
        "READY chunk summary complete",
        chunk_summary.normalize_state(None, {"last_marker": ready}, None),
        "complete",
    )
    assert_eq(
        "FAILED chunk summary failed",
        chunk_summary.normalize_state(None, {"last_marker": failed}, None),
        "failed",
    )

    ready_env = goalflight_messages.markers_to_envelopes({"READY": ["docs-private/research/findings.md"]}, dispatch_id="d-ready")
    failed_env = goalflight_messages.markers_to_envelopes({"FAILED": ["missing final artifact"]}, dispatch_id="d-failed")
    assert_eq("READY envelope type", ready_env[0]["type"], "result")
    assert_eq("FAILED envelope type", failed_env[0]["type"], "blocked")
    assert_eq("ACP extract FAILED", extract_markers("FAILED: missing final artifact\n")["FAILED"], ["missing final artifact"])


def main() -> None:
    test_terminal_state_poison_pairs()
    test_terminal_state_shared_sets_cover_lease_pruning()
    test_terminal_state_for_preserves_specific_failures()
    test_fleet_reconcile_pre_status_uses_shared_failure_states()
    test_terminal_marker_poison_pairs()
    print("OK: terminal vocabulary poison-pair tests pass")


if __name__ == "__main__":
    main()
