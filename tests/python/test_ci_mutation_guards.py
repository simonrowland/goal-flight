"""Poison pairs for load-bearing branches that survived targeted mutations."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import shlex
import sys

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import goalflight_acp_run as acp_run  # noqa: E402
import goalflight_capacity as capacity  # noqa: E402
import goalflight_liveness as liveness  # noqa: E402
import goalflight_journal as journal  # noqa: E402
import goalflight_ledger as ledger  # noqa: E402
import goalflight_messages as messages  # noqa: E402
import goalflight_status as status  # noqa: E402
import goalflight_terminal as terminal  # noqa: E402


def test_attention_state_wakes_without_a_scraped_marker() -> None:
    for field, state_name in (
        ("state", "awaiting_user_confirm"),
        ("state", "awaiting_permission"),
        ("classification", "running_user_confirm"),
    ):
        record = {
            "dispatch_id": "attention-state-only",
            "state": "running",
            "classification": "expected_live",
        }
        record[field] = state_name
        assert status.done_code(record, worker_alive=True) == 0, record

    assert status.done_code(
        {"state": "running", "classification": "expected_live"},
        worker_alive=True,
    ) == 1


def test_async_capacity_wait_maps_confirmed_cancellation() -> None:
    clock = iter((10.0, 10.0, 11.0))

    def acquire_wait(_args: argparse.Namespace) -> int:
        print(json.dumps({"decision": "wait", "reason": "machine_worker_cap"}))
        return 1

    async def cancelled_sleep(_seconds: float) -> None:
        raise asyncio.CancelledError

    async def exercise() -> None:
        with pytest.raises(capacity.CapacityWaitInterrupted) as raised:
            await capacity.acquire_with_wait_async(
                argparse.Namespace(),
                lane="normal",
                wait_s=30.0,
                poll_s=1.0,
                jitter=0.0,
                interrupted=lambda: True,
                interrupted_signum=lambda: 15,
                monotonic_fn=lambda: next(clock),
                sleep_fn=cancelled_sleep,
                acquire_func=acquire_wait,
            )
        error = raised.value
        assert error.exit_code == 143
        assert error.signum == 15
        assert error.payload == {
            "decision": "wait",
            "reason": "wait_interrupted",
            "waited_s": 1.0,
            "attempts": 1,
        }

    asyncio.run(exercise())


def test_acp_permission_read_only_capability_is_fail_closed() -> None:
    assert acp_run.acp_permission_read_only_supported("claude")
    assert acp_run.acp_permission_read_only_supported("claude-acp")
    for adapter in (None, "codex", "cursor", "grok", "kimi"):
        assert not acp_run.acp_permission_read_only_supported(adapter), adapter


def test_acp_success_marker_helper_matches_terminal_contract() -> None:
    for kind in ("COMPLETE", "READY", "RESULT"):
        assert acp_run._successful_terminal_marker({kind: ["evidence"]}), kind
    for kind in ("FAILED", "BLOCKED", "USER-NEED", "USER-CONFIRM"):
        assert not acp_run._successful_terminal_marker({kind: ["evidence"]}), kind
    assert not acp_run._successful_terminal_marker({"COMPLETE": []})


def test_terminal_rate_limit_numbers_require_token_boundaries() -> None:
    assert terminal.rate_limit_signature_in_text("provider returned HTTP 429") == "429"
    assert terminal.rate_limit_signature_in_text("provider returned HTTP 529") == "529"
    assert terminal.rate_limit_signature_in_text("ordinary log line 1429") is None
    assert terminal.rate_limit_signature_in_text("ordinary log line 5290") is None


def test_attention_marker_helper_is_shared_by_status() -> None:
    for kind in terminal.ATTENTION_MARKERS:
        marker = {"kind": kind, "text": "attention-marker — needs controller"}
        assert terminal.attention_marker_present(marker), kind
        assert status._record_has_attention_marker(
            {"dispatch_id": "attention-marker", "terminal_marker": marker}
        ), kind
        assert not status._record_has_attention_marker(
            {
                "dispatch_id": "attention-marker",
                "terminal_marker": {
                    "kind": kind,
                    "text": "foreign-attention-marker — needs controller",
                },
            }
        ), kind
    for kind in terminal.SUCCESS_TERMINAL_MARKERS:
        marker = {"kind": kind, "text": "finished"}
        assert not terminal.attention_marker_present(marker), kind


def test_stale_cpu_sample_forces_a_fresh_pair(monkeypatch: pytest.MonkeyPatch) -> None:
    pgid = 4242
    snapshots = iter(({7: 10.0}, {7: 10.6}))
    clock = iter((100.0, 100.6))
    sleeps: list[float] = []

    liveness._cpu_samples.clear()
    liveness._cpu_samples[pgid] = (0.0, {7: 1.0})
    monkeypatch.setattr(liveness, "process_group_id", lambda _pid: pgid)
    monkeypatch.setattr(liveness, "_pgroup_cputime_snapshot", lambda _pgid: next(snapshots))
    monkeypatch.setattr(liveness.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(liveness.time, "sleep", lambda seconds: sleeps.append(seconds))

    measured = liveness.pgroup_cpu_pct(pgid)

    assert measured == pytest.approx(100.0)
    assert sleeps == [liveness._CPU_SAMPLE_COLD_WINDOW_S]
    assert liveness._cpu_samples[pgid] == (100.6, {7: 10.6})


def test_missing_narrow_journal_api_is_not_silently_skipped(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setattr(status.goalflight_task, "resolve_project_root", lambda _value: project)

    def expected_unavailable(_cls: object, _root: object) -> object:
        raise journal.JournalBusy("expected read outage")

    monkeypatch.setattr(journal.Journal, "open_reader", classmethod(expected_unavailable))
    assert status._mail_watermark(str(project), ["shape-check"]) == set()

    monkeypatch.setattr(journal.Journal, "open_reader", None)
    with pytest.raises(AttributeError):
        status._mail_watermark(str(project), ["shape-check"])

    monkeypatch.delattr(journal.Journal, "open_reader")
    with pytest.raises(AttributeError):
        status._mail_watermark(str(project), ["shape-check"])


def test_dispatch_projection_lookup_is_exact_id_not_history_scan(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    dispatch_id = "exact-projection"
    record_path = tmp_path / f"{dispatch_id}.json"
    record_path.write_text(
        json.dumps({"dispatch_id": dispatch_id, "project_root": str(tmp_path)}),
        encoding="utf-8",
    )
    monkeypatch.setattr(ledger, "record_path", lambda _dispatch_id, create=False: record_path)
    monkeypatch.setattr(
        ledger,
        "read_records",
        lambda: (_ for _ in ()).throw(AssertionError("history scan reached")),
    )

    record, error = messages._dispatch_record(dispatch_id)
    assert error is None
    assert record is not None and record["dispatch_id"] == dispatch_id

    record_path.write_text(
        json.dumps({"dispatch_id": "foreign-projection"}),
        encoding="utf-8",
    )
    record, error = messages._dispatch_record(dispatch_id)
    assert record is None
    assert error == "dispatch record is malformed or bound to a different dispatch id"


def test_cursor_position_command_round_trips_grouped_and_delimiter_bearing_streams() -> None:
    command = messages._cursor_advance_command(
        project_root=Path("/tmp/project with spaces"),
        controller_label="controller:primary",
        lease_nonce="lease-token",
        cursor_version=7,
        positions={
            "task-store:goal-flight-alpha": 13,
            "stream=with=equals": 17,
        },
        stream_snapshots={
            "task-store:goal-flight-alpha": "a" * 64,
            "stream=with=equals": "b" * 64,
        },
    )
    assert command is not None
    argv = shlex.split(command)
    snapshot_index = argv.index("--stream-snapshot")
    position_index = argv.index("--position")
    assert argv[snapshot_index + 1 : position_index] == [
        f"stream=with=equals={'b' * 64}",
        f"task-store:goal-flight-alpha={'a' * 64}",
    ]
    assert argv[position_index:] == [
        "--position",
        "stream=with=equals=17",
        "task-store:goal-flight-alpha=13",
    ]
    assert messages._parse_cursor_stream_snapshots(
        [argv[snapshot_index + 1 : position_index]]
    ) == {
        "stream=with=equals": "b" * 64,
        "task-store:goal-flight-alpha": "a" * 64,
    }
    assert messages._parse_cursor_positions([argv[position_index + 1 :]]) == {
        "stream=with=equals": 17,
        "task-store:goal-flight-alpha": 13,
    }

    assert messages._parse_cursor_positions(
        [["task-store:goal-flight-alpha=11"], ["task-store:goal-flight-alpha=13"]]
    ) == {"task-store:goal-flight-alpha": 13}
