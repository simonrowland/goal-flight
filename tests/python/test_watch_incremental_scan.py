"""Incremental tail-scan regressions through the real watcher entry point."""

from __future__ import annotations

import json
from pathlib import Path
import sys
from typing import Callable

import pytest


ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import goalflight_watch as watch  # noqa: E402


POLL_SECS = 0.125


def _run_live_watcher(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    initial: bytes,
    on_poll_sleep: Callable[[int, Path], None],
) -> tuple[int, list[dict], Path]:
    """Run ``main`` against disk files without invoking process/CPU probes."""

    tail = tmp_path / "worker.tail"
    status = tmp_path / "watcher.status.json"
    tail.write_bytes(initial)
    payloads: list[dict] = []
    poll_sleeps = 0
    real_write_status = watch.write_status

    def capture_status(path: Path, payload: dict) -> None:
        payloads.append(json.loads(json.dumps(payload)))
        real_write_status(path, payload)

    def controlled_sleep(seconds: float) -> None:
        nonlocal poll_sleeps
        if seconds == POLL_SECS:
            on_poll_sleep(poll_sleeps, tail)
            poll_sleeps += 1

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "goalflight_watch.py",
            "--pid",
            "424242",
            "--tail",
            str(tail),
            "--status-json",
            str(status),
            "--poll-secs",
            str(POLL_SECS),
            "--max-idle-secs",
            "999999",
            "--agent",
            "test",
        ],
    )
    monkeypatch.setattr(watch, "write_status", capture_status)
    monkeypatch.setattr(watch.time, "sleep", controlled_sleep)
    monkeypatch.setattr(watch.atexit, "register", lambda _callback: None)
    monkeypatch.setattr(watch.signal, "signal", lambda *_args: None)
    monkeypatch.setattr(
        watch,
        "worker_alive",
        lambda pid, _identity: (True, "live", {"pid": pid}),
    )
    monkeypatch.setattr(watch, "process_group_id", lambda pid: pid)
    monkeypatch.setattr(watch, "pgroup_cpu_pct", lambda _pgid: 1.0)
    monkeypatch.setattr(watch, "system_starved", lambda: False)
    monkeypatch.setattr(watch.TraceLiveness, "sample", lambda self, **_kwargs: {})

    return watch.main(), payloads, tail


def _append(path: Path, content: bytes) -> None:
    with path.open("ab") as handle:
        handle.write(content)


def test_live_entry_point_scans_only_appended_bytes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Input path: main loop -> IncrementalTailScanner.scan -> status tail_scan."""

    initial = b"x\n" * 500_000
    appends = [
        b"STATUS: delta one\n",
        b"STATUS: delta two\n",
        b"COMPLETE: incremental accounting\n",
    ]

    def grow(index: int, tail: Path) -> None:
        if index >= len(appends):
            raise AssertionError("watcher missed the terminal marker")
        _append(tail, appends[index])

    returncode, payloads, _tail = _run_live_watcher(
        monkeypatch, tmp_path, initial, grow
    )

    assert returncode == 0
    scans = [payload["tail_scan"] for payload in payloads if "tail_scan" in payload]
    assert scans[0]["content_bytes"] == len(initial)
    for scan, appended in zip(scans[1:], appends, strict=True):
        assert scan["content_bytes"] == len(appended)
        assert scan["bytes_read"] <= len(appended) + watch.TAIL_SCAN_BOUNDARY_BYTES
        assert scan["lines_materialized"] <= appended.count(b"\n") + 1


def test_live_entry_point_detects_split_marker_once(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Input path: partial EOF -> next poll suffix -> main terminal payload."""

    suffix = b"LETE: split across polls\n"

    def finish_line(index: int, tail: Path) -> None:
        if index == 0:
            _append(tail, suffix)
            return
        raise AssertionError("split marker was not detected on its completing poll")

    returncode, payloads, _tail = _run_live_watcher(
        monkeypatch, tmp_path, b"COMP", finish_line
    )

    assert returncode == 0
    terminal_payload = payloads[-1]
    assert terminal_payload["terminal_marker"]["kind"] == "COMPLETE"
    assert [m["kind"] for m in terminal_payload["markers"]].count("COMPLETE") == 1
    assert terminal_payload["tail_scan"]["content_bytes"] == len(suffix)


def test_live_entry_point_resyncs_replaced_tail(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Input path: inode replacement -> scanner reset -> status marker -> terminal."""

    old = b"A" * 64 + b"B" * 64
    rotated_prefix = b"STATUS: rotated generation\n"
    rotated = rotated_prefix + b"C" * (64 - len(rotated_prefix)) + b"B" * 64

    def rotate_then_finish(index: int, tail: Path) -> None:
        if index == 0:
            replacement = tail.with_suffix(".replacement")
            replacement.write_bytes(rotated)
            replacement.replace(tail)
            return
        if index == 1:
            _append(tail, b"\nCOMPLETE: replacement observed\n")
            return
        raise AssertionError("watcher failed to finish after tail replacement")

    returncode, payloads, _tail = _run_live_watcher(
        monkeypatch, tmp_path, old, rotate_then_finish
    )

    assert returncode == 0
    replacement_payloads = [
        payload
        for payload in payloads
        if payload.get("tail_scan", {}).get("resync_reason") == "replacement"
    ]
    assert len(replacement_payloads) == 1
    assert any(
        marker["kind"] == "STATUS" and marker["text"] == "rotated generation"
        for marker in replacement_payloads[0]["markers"]
    )
    assert payloads[-1]["terminal_marker"]["kind"] == "COMPLETE"


def test_live_entry_point_resyncs_truncated_tail(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Input path: same-inode shrink -> scanner reset -> later terminal append."""

    def truncate_then_finish(index: int, tail: Path) -> None:
        if index == 0:
            tail.write_bytes(b"")
            return
        if index == 1:
            _append(tail, b"COMPLETE: after truncation\n")
            return
        raise AssertionError("watcher failed to finish after tail truncation")

    returncode, payloads, _tail = _run_live_watcher(
        monkeypatch, tmp_path, b"STATUS: old generation\n", truncate_then_finish
    )

    assert returncode == 0
    truncated_payloads = [
        payload
        for payload in payloads
        if payload.get("tail_scan", {}).get("resync_reason") == "truncated"
    ]
    assert len(truncated_payloads) == 1
    assert truncated_payloads[0]["markers"] == []
    assert payloads[-1]["terminal_marker"]["kind"] == "COMPLETE"


def test_live_entry_point_keeps_unbalanced_fence_state_across_polls(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Input path: prior-poll fence opener -> next-poll marker -> live terminal."""

    marker = b"COMPLETE: unbalanced fence remains permissive\n"

    def append_marker(index: int, tail: Path) -> None:
        if index == 0:
            _append(tail, marker)
            return
        raise AssertionError("unbalanced cross-poll fence hid the terminal marker")

    returncode, payloads, _tail = _run_live_watcher(
        monkeypatch, tmp_path, b"```text\n", append_marker
    )

    assert returncode == 0
    terminal_payload = payloads[-1]
    assert terminal_payload["terminal_marker"]["kind"] == "COMPLETE"
    assert terminal_payload["tail_scan"]["fence_unbalanced"] is True
