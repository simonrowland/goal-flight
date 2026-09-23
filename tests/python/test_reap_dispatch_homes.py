#!/usr/bin/env python3
"""Fixture-only safety tests for the per-dispatch Codex home reaper."""

from __future__ import annotations

import datetime as dt
import io
import json
import os
from contextlib import redirect_stdout
from pathlib import Path
import sys

import pytest


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import goalflight_reap_dispatch_homes as reaper  # noqa: E402


NOW = dt.datetime(2026, 1, 10, 12, 0, tzinfo=dt.timezone.utc)
RETENTION = dt.timedelta(days=7)


def _fixture_tree(tmp_path: Path) -> tuple[Path, Path]:
    homes_dir = tmp_path / "dispatch-homes"
    ledger_dir = tmp_path / "ledger" / "runs"
    homes_dir.mkdir(parents=True)
    ledger_dir.mkdir(parents=True)
    assert homes_dir.resolve().is_relative_to(tmp_path.resolve())
    assert ledger_dir.resolve().is_relative_to(tmp_path.resolve())
    return homes_dir, ledger_dir


def _home(homes_dir: Path, dispatch_id: str) -> Path:
    home = homes_dir / dispatch_id
    home.mkdir()
    (home / "state.sqlite").write_bytes(b"fixture-state" * 1024)
    return home


def _record(
    ledger_dir: Path,
    dispatch_id: str,
    *,
    state: str,
    ended_at: dt.datetime,
    worker_pid: int | None = 999_999_999,
) -> None:
    payload = {
        "schema": "goalflight.dispatch.v1",
        "dispatch_id": dispatch_id,
        "state": state,
        "ended_at": ended_at.isoformat().replace("+00:00", "Z"),
    }
    if worker_pid is not None:
        payload["worker_pid"] = worker_pid
    (ledger_dir / f"{dispatch_id}.json").write_text(json.dumps(payload), encoding="utf-8")


def _run(
    homes_dir: Path,
    ledger_dir: Path,
    *,
    delete: bool,
    probe=None,
) -> dict:
    return reaper.reap_dispatch_homes(
        homes_dir=homes_dir,
        ledger_dir=ledger_dir,
        retention=RETENTION,
        delete=delete,
        now=NOW,
        identity_probe=probe,
    )


def test_terminal_past_retention_is_reclaimed(tmp_path: Path) -> None:
    homes_dir, ledger_dir = _fixture_tree(tmp_path)
    home = _home(homes_dir, "old-terminal")
    _record(
        ledger_dir,
        home.name,
        state="complete",
        ended_at=NOW - RETENTION - dt.timedelta(seconds=1),
    )

    payload = _run(homes_dir, ledger_dir, delete=True)

    assert not home.exists()
    assert payload["eligible_count"] == 1
    assert payload["deleted_count"] == 1
    assert payload["entries"][0]["reason"] == "terminal_past_retention"


def test_authoritative_terminal_state_vocabulary_is_used(tmp_path: Path) -> None:
    homes_dir, ledger_dir = _fixture_tree(tmp_path)
    home = _home(homes_dir, "blocked-terminal")
    _record(
        ledger_dir,
        home.name,
        state="blocked_future_reason",
        ended_at=NOW - RETENTION - dt.timedelta(seconds=1),
    )

    payload = _run(homes_dir, ledger_dir, delete=True)

    assert not home.exists()
    assert payload["deleted_count"] == 1
    assert payload["entries"][0]["reason"] == "terminal_past_retention"


def test_terminal_inside_retention_is_kept(tmp_path: Path) -> None:
    homes_dir, ledger_dir = _fixture_tree(tmp_path)
    home = _home(homes_dir, "recent-terminal")
    _record(
        ledger_dir,
        home.name,
        state="complete",
        ended_at=NOW - RETENTION + dt.timedelta(seconds=1),
    )

    payload = _run(homes_dir, ledger_dir, delete=True)

    assert home.is_dir()
    assert payload["deleted_count"] == 0
    assert payload["entries"][0]["reason"] == "inside_retention"


def test_non_terminal_dispatch_is_kept(tmp_path: Path) -> None:
    homes_dir, ledger_dir = _fixture_tree(tmp_path)
    home = _home(homes_dir, "running")
    _record(
        ledger_dir,
        home.name,
        state="running",
        ended_at=NOW - RETENTION - dt.timedelta(days=100),
    )

    payload = _run(
        homes_dir,
        ledger_dir,
        delete=True,
        probe=lambda _record: (_ for _ in ()).throw(AssertionError("liveness must not be reached")),
    )

    assert home.is_dir()
    assert payload["deleted_count"] == 0
    assert payload["entries"][0]["reason"] == "non_terminal"


def test_live_worker_vetoes_terminal_ledger(tmp_path: Path) -> None:
    homes_dir, ledger_dir = _fixture_tree(tmp_path)
    home = _home(homes_dir, "ledger-conflict")
    _record(
        ledger_dir,
        home.name,
        state="complete",
        ended_at=NOW - RETENTION - dt.timedelta(days=100),
        worker_pid=os.getpid(),
    )

    payload = _run(homes_dir, ledger_dir, delete=True)

    assert home.is_dir()
    assert payload["deleted_count"] == 0
    # Windows can prove the PID is live without providing a comparable process
    # identity; both outcomes must fail closed and preserve the home.
    assert payload["entries"][0]["liveness"] in {"live", "identity_indeterminate"}
    assert payload["entries"][0]["reason"] in {"live_worker", "liveness_indeterminate"}


def test_missing_pid_is_not_proof_worker_is_dead(tmp_path: Path) -> None:
    homes_dir, ledger_dir = _fixture_tree(tmp_path)
    home = _home(homes_dir, "missing-pid")
    _record(
        ledger_dir,
        home.name,
        state="complete",
        ended_at=NOW - RETENTION - dt.timedelta(days=100),
        worker_pid=None,
    )

    payload = _run(homes_dir, ledger_dir, delete=True)

    assert home.is_dir()
    assert payload["deleted_count"] == 0
    assert payload["entries"][0]["liveness"] == "no_pid"
    assert payload["entries"][0]["reason"] == "liveness_indeterminate"


def test_liveness_is_rechecked_immediately_before_delete(tmp_path: Path) -> None:
    homes_dir, ledger_dir = _fixture_tree(tmp_path)
    home = _home(homes_dir, "became-live")
    _record(
        ledger_dir,
        home.name,
        state="complete",
        ended_at=NOW - RETENTION - dt.timedelta(days=100),
    )
    probe_results = iter(((False, "dead"), (True, "live")))

    payload = _run(
        homes_dir,
        ledger_dir,
        delete=True,
        probe=lambda _record: next(probe_results),
    )

    assert home.is_dir()
    assert payload["deleted_count"] == 0
    assert payload["entries"][0]["liveness"] == "live"
    assert payload["entries"][0]["reason"] == "changed_before_delete:live_worker"


def test_home_without_ledger_record_is_kept(tmp_path: Path) -> None:
    homes_dir, ledger_dir = _fixture_tree(tmp_path)
    home = _home(homes_dir, "unknown-provenance")

    payload = _run(
        homes_dir,
        ledger_dir,
        delete=True,
        probe=lambda _record: (_ for _ in ()).throw(AssertionError("liveness must not be reached")),
    )

    assert home.is_dir()
    assert payload["deleted_count"] == 0
    assert payload["entries"][0]["reason"] == "missing_ledger_record"


def test_default_dry_run_reports_size_and_deletes_nothing(tmp_path: Path, monkeypatch) -> None:
    homes_dir, ledger_dir = _fixture_tree(tmp_path)
    home = _home(homes_dir, "dry-run")
    _record(
        ledger_dir,
        home.name,
        state="complete",
        ended_at=NOW - RETENTION - dt.timedelta(days=100),
    )
    expected_bytes = reaper.allocated_tree_bytes(home)
    monkeypatch.setattr(reaper.goalflight_ledger, "identity_matches", lambda _record: (False, "dead"))

    output = io.StringIO()
    with redirect_stdout(output):
        code = reaper.main(
            [
                "--json",
                "--homes-dir",
                str(homes_dir),
                "--ledger-dir",
                str(ledger_dir),
                "--retention-days",
                "7",
            ]
        )
    payload = json.loads(output.getvalue())

    assert code == 0
    assert home.is_dir()
    assert payload["mode"] == "dry-run"
    assert payload["eligible_count"] == 1
    assert payload["eligible_allocated_bytes"] == expected_bytes
    assert payload["total_allocated_bytes"] == expected_bytes
    assert payload["entries"][0]["allocated_bytes"] == expected_bytes
    assert payload["kept_reasons"] == {}
    assert payload["deleted_count"] == 0


def test_default_paths_follow_codex_and_machine_state_overrides(tmp_path: Path, monkeypatch) -> None:
    codex_state_dir = tmp_path / "codex-state"
    machine_state_dir = tmp_path / "machine-state"
    homes_dir = codex_state_dir / "dispatch-homes"
    ledger_dir = machine_state_dir / "runs.d"
    homes_dir.mkdir(parents=True)
    ledger_dir.mkdir(parents=True)
    home = _home(homes_dir, "state-override")
    _record(
        ledger_dir,
        home.name,
        state="complete",
        ended_at=NOW - RETENTION - dt.timedelta(days=100),
    )
    monkeypatch.setenv("GOALFLIGHT_CODEX_STATE_DIR", str(codex_state_dir))
    monkeypatch.setenv("GOALFLIGHT_STATE_DIR", str(machine_state_dir))
    monkeypatch.setattr(reaper.goalflight_ledger, "identity_matches", lambda _record: (False, "dead"))

    output = io.StringIO()
    with redirect_stdout(output):
        code = reaper.main(["--json", "--retention-days", "7"])
    payload = json.loads(output.getvalue())

    assert code == 0
    assert home.is_dir()
    assert payload["mode"] == "dry-run"
    assert payload["homes_dir"] == str(homes_dir)
    assert payload["ledger_dir"] == str(ledger_dir)
    assert payload["eligible_count"] == 1


def test_delete_rejects_mistyped_homes_root(tmp_path: Path) -> None:
    homes_dir = tmp_path / "wrong-root"
    ledger_dir = tmp_path / "ledger" / "runs"
    homes_dir.mkdir()
    ledger_dir.mkdir(parents=True)
    home = _home(homes_dir, "old-terminal")
    _record(
        ledger_dir,
        home.name,
        state="complete",
        ended_at=NOW - RETENTION - dt.timedelta(days=100),
    )

    with pytest.raises(ValueError, match="named 'dispatch-homes'"):
        _run(homes_dir, ledger_dir, delete=True)

    assert home.is_dir()


def test_delete_rejects_symlinked_homes_root(tmp_path: Path) -> None:
    target_homes_dir = tmp_path / "target" / "dispatch-homes"
    ledger_dir = tmp_path / "ledger" / "runs"
    target_homes_dir.mkdir(parents=True)
    ledger_dir.mkdir(parents=True)
    home = _home(target_homes_dir, "old-terminal")
    _record(
        ledger_dir,
        home.name,
        state="complete",
        ended_at=NOW - RETENTION - dt.timedelta(days=100),
    )
    homes_dir = tmp_path / "dispatch-homes"
    homes_dir.symlink_to(target_homes_dir, target_is_directory=True)

    with pytest.raises(ValueError, match="non-symlink"):
        _run(homes_dir, ledger_dir, delete=True)

    assert home.is_dir()
