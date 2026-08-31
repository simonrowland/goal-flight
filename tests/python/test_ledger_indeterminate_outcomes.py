#!/usr/bin/env python3
"""Unknown ledger observations must not become definite lifecycle answers."""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
from pathlib import Path
import sys

import pytest


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import goalflight_dispatch as dispatch  # noqa: E402
import goalflight_dispatch_states as states  # noqa: E402
import goalflight_ledger as ledger  # noqa: E402


REQUIRES_UNSEARCHABLE_ANCESTOR = pytest.mark.skipif(
    sys.platform == "win32" or (hasattr(os, "geteuid") and os.geteuid() == 0),
    reason="an unsearchable ancestor requires POSIX mode enforcement as non-root",
)
REQUIRES_POSIX_SHELL = pytest.mark.skipif(
    sys.platform == "win32", reason="controlled ps executable uses /bin/sh"
)


@contextlib.contextmanager
def _mode(path: Path, mode: int, restore: int = 0o700):
    os.chmod(path, mode)
    try:
        yield
    finally:
        os.chmod(path, restore)


def _finish(dispatch_id: str) -> tuple[int, dict]:
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        code = ledger.cmd_finish(
            argparse.Namespace(
                dispatch_id=dispatch_id,
                state="complete",
                reason=None,
                terminal_state=None,
                elapsed_s=None,
                worker_still_alive=False,
            )
        )
    return code, json.loads(output.getvalue())


def test_f1_nonterminal_states_remain_nonterminal_through_ledger_wrapper() -> None:
    """Reverting F1 maps each state to ``error`` and makes the consumer terminal."""
    for state in (
        states.WORKER_STALLED_CANDIDATE_STATE,
        "awaiting_permission",
        "launch_unconfirmed",
    ):
        assert ledger.terminal_state_for(state) == "unknown"
        assert dispatch._dispatch_record_is_terminal({"state": state}) is False


def test_f1_known_terminal_states_still_close_dispatches() -> None:
    for state in ("complete", "worker_dead", "wedged"):
        assert ledger.terminal_state_for(state) != "unknown"
        assert dispatch._dispatch_record_is_terminal({"state": state}) is True


@REQUIRES_UNSEARCHABLE_ANCESTOR
def test_f2_unsearchable_ledger_ancestor_is_not_an_empty_ledger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reverting F2 returns ``[]`` before the real directory listing is attempted."""
    hidden = tmp_path / "hidden"
    runs = hidden / "state" / "runs.d"
    runs.mkdir(parents=True)
    (runs / "queued.json").write_text(
        json.dumps({"dispatch_id": "queued", "state": "queued"}),
        encoding="utf-8",
    )
    monkeypatch.setenv("GOALFLIGHT_STATE_DIR", str(hidden / "state"))

    with _mode(hidden, 0o000):
        assert runs.exists() is False
        with pytest.raises(OSError, match="ledger directory unreadable"):
            ledger.read_records()

    assert [row["dispatch_id"] for row in ledger.read_records()] == ["queued"]


def test_f2_genuinely_absent_ledger_directory_is_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GOALFLIGHT_STATE_DIR", str(tmp_path / "missing-state"))
    assert ledger.read_records() == []
    assert ledger.last_read_work() == {
        "listed": 0,
        "parsed": 0,
        "skipped_terminal": 0,
    }


@REQUIRES_UNSEARCHABLE_ANCESTOR
def test_f3_finish_reports_inaccessible_existing_dispatch_as_retryable_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reverting F3 emits ``missing_dispatch`` for the inaccessible real row."""
    hidden = tmp_path / "hidden"
    state_dir = hidden / "state"
    monkeypatch.setenv("GOALFLIGHT_STATE_DIR", str(state_dir))
    ledger.write_record(
        {
            "schema": ledger.SCHEMA,
            "dispatch_id": "existing-finish",
            "state": "running",
            "started_at": ledger.utc_now(),
        }
    )

    with _mode(hidden, 0o000):
        code, payload = _finish("existing-finish")

    assert code == 2
    assert payload == {
        "dispatch_id": "existing-finish",
        "error": "ledger_record_unreadable",
        "ledger_presence": "unknown",
        "ok": False,
        "retryable": True,
    }
    assert ledger.read_record("existing-finish")["state"] == "running"


def test_f3_genuinely_absent_dispatch_is_still_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GOALFLIGHT_STATE_DIR", str(tmp_path / "state"))
    code, payload = _finish("absent-finish")
    assert code == 1
    assert payload["error"] == "missing_dispatch"
    assert payload["dispatch_id"] == "absent-finish"


def test_f6_malformed_present_row_is_preserved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reverting F6 replaces the malformed terminal prefix with ``running``."""
    monkeypatch.setenv("GOALFLIGHT_STATE_DIR", str(tmp_path / "state"))
    path = ledger.record_path("malformed-row", create=True)
    original = b'{"dispatch_id":"malformed-row","state":"complete"'
    path.write_bytes(original)

    with pytest.raises(OSError, match="refusing to overwrite"):
        ledger.write_record({"dispatch_id": "malformed-row", "state": "running"})

    assert path.read_bytes() == original


def test_f6_genuinely_absent_row_can_still_be_created(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GOALFLIGHT_STATE_DIR", str(tmp_path / "state"))
    path = ledger.write_record({"dispatch_id": "new-row", "state": "running"})
    assert json.loads(path.read_text(encoding="utf-8"))["state"] == "running"


def test_f7_relative_retry_delay_without_anchor_is_indeterminate() -> None:
    """Reverting F7 declares the malformed-anchor record immediately eligible."""
    policy = states.retry_policy_for_record(
        {
            "state": states.TRANSIENT_THROTTLE_STATE,
            "retry_after": 60,
            "ended_at": "not-a-timestamp",
        },
        now=2_000.0,
    )
    assert policy == {
        "kind": states.LIMIT_KIND_TRANSIENT,
        "eligible": None,
        "not_before": None,
        "mode": "hold_retry_anchor_unknown",
    }


def test_f7_anchored_retry_delay_becomes_eligible_after_deadline() -> None:
    record = {
        "state": states.TRANSIENT_THROTTLE_STATE,
        "retry_after": 60,
        "observed_at": "1970-01-01T00:16:40+00:00",
    }
    before = states.retry_policy_for_record(record, now=1_059.0)
    after = states.retry_policy_for_record(record, now=1_060.0)
    assert before == {
        "kind": states.LIMIT_KIND_TRANSIENT,
        "eligible": False,
        "not_before": "1970-01-01T00:17:40+00:00",
        "mode": "retry_soon",
    }
    assert after == {
        "kind": states.LIMIT_KIND_TRANSIENT,
        "eligible": True,
        "not_before": None,
        "mode": "retry_soon",
    }


def test_f9_failed_process_scan_is_published_as_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reverting F9 publishes the real executable lookup failure as ``[]``."""
    empty_path = tmp_path / "empty-path"
    empty_path.mkdir()
    monkeypatch.setenv("PATH", str(empty_path))

    payload = ledger.status_payload()
    assert payload["surplus_processes"] is None
    assert "surplus worker-like process scan unavailable" in ledger.format_status_lines(
        payload
    )


@REQUIRES_POSIX_SHELL
def test_f9_successful_process_scan_still_reports_real_surplus_match(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    ps = bin_dir / "ps"
    ps.write_text(
        "#!/bin/sh\nprintf '%s\\n' '424242 codex codex exec worker'\n",
        encoding="utf-8",
    )
    ps.chmod(0o700)
    monkeypatch.setenv("PATH", str(bin_dir))

    assert ledger.scan_surplus([]) == [
        {"pid": 424242, "comm": "codex", "args": "codex exec worker"}
    ]
    assert ledger.scan_surplus([{"worker_pid": 424242}]) == []


def test_find_dispatch_record_oserror_is_unreadable_not_free(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reverting the OSError arm to ``return None`` fabricates "id is free"."""
    monkeypatch.setenv("GOALFLIGHT_STATE_DIR", str(tmp_path / "state"))
    ledger.write_record(
        {
            "schema": ledger.SCHEMA,
            "dispatch_id": "real-row",
            "state": "running",
            "started_at": ledger.utc_now(),
        }
    )
    found = dispatch._find_dispatch_record("real-row")
    assert found is not None and found.get("dispatch_id") == "real-row"
    assert found.get("state") == "running"
    assert dispatch._find_dispatch_record("no-such-id") is None

    def boom(_dispatch_id: str):
        raise OSError("injected unlistable ledger")

    monkeypatch.setattr(dispatch.goalflight_ledger, "read_record", boom)
    record = dispatch._find_dispatch_record("maybe-there")
    assert record is not None, "OSError must not read as a free id"
    assert ledger.record_is_unreadable(record), record


@REQUIRES_UNSEARCHABLE_ANCESTOR
def test_unlistable_runs_dir_lookup_is_not_a_free_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unlistable parent cannot make either lookup invent absence."""
    hidden = tmp_path / "hidden"
    monkeypatch.setenv("GOALFLIGHT_STATE_DIR", str(hidden / "state"))
    ledger.write_record(
        {
            "schema": ledger.SCHEMA,
            "dispatch_id": "existing-lookup",
            "state": "running",
            "started_at": ledger.utc_now(),
        }
    )

    with _mode(hidden, 0o000):
        via_read = ledger.read_record("existing-lookup")
        via_find = dispatch._find_dispatch_record("existing-lookup")
        via_read_missing = ledger.read_record("never-created")
        via_find_missing = dispatch._find_dispatch_record("never-created")

    assert ledger.record_is_unreadable(via_read), via_read
    assert ledger.record_is_unreadable(via_find), via_find
    assert ledger.record_is_unreadable(via_read_missing), via_read_missing
    assert ledger.record_is_unreadable(via_find_missing), via_find_missing
    assert via_read.get("state") != "running"
    assert via_find.get("state") != "running"

    restored = ledger.read_record("existing-lookup")
    assert restored is not None and restored.get("state") == "running"
    assert dispatch._find_dispatch_record("never-created") is None
