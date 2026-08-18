#!/usr/bin/env python3
"""Conservative automatic reconciliation for abandoned dispatch records."""

from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path
import socket
import sys
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import goalflight_capacity as C  # noqa: E402
import goalflight_dispatch as D  # noqa: E402
import goalflight_ledger as L  # noqa: E402


SESSION_ID = "12345678-1234-4abc-8def-1234567890ab"


@pytest.fixture(autouse=True)
def _isolated_state(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    state = tmp_path / "state"
    monkeypatch.setenv("GOALFLIGHT_STATE_DIR", str(state))
    monkeypatch.setenv("GOALFLIGHT_CODEX_STATE_DIR", str(state))
    monkeypatch.setenv("GOALFLIGHT_TASK_STORE_DIR", str(tmp_path / "task-store"))
    monkeypatch.setattr(D, "_export_dashboard_status_for_project", lambda *_args: None)
    monkeypatch.setattr(D, "_start_dashboard_refresh_for_project", lambda *_args: None)


def _future_now(seconds: float = 900.0) -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=seconds)


def _record(
    tmp_path: Path,
    dispatch_id: str,
    *,
    tail_text: str = "worker output stopped without a verdict\n",
    worker_pid: object = None,
    worker_identity: dict | None = None,
    lease_id: str | None = None,
    parent_dispatch_id: str | None = None,
    codex_home: Path | None = None,
    controller_pid: int | None = None,
    controller_session_id: str | None = None,
    controller_label: str | None = None,
    state: str = "running",
) -> dict:
    tail = tmp_path / f"{dispatch_id}.tail"
    tail.write_text(tail_text, encoding="utf-8")
    status = tmp_path / f"{dispatch_id}.status.json"
    payload = {
        "schema": L.SCHEMA,
        "dispatch_id": dispatch_id,
        "agent": "codex",
        "engine": "codex",
        "shape": "bash",
        "transport": "dispatch",
        "project_root": str(tmp_path),
        "hostname": socket.gethostname(),
        "state": state,
        "terminal_state": "unknown",
        "started_at": (
            dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=2)
        ).isoformat(timespec="seconds"),
        "worker_pid": worker_pid,
        "worker_identity": worker_identity,
        "lease_id": lease_id,
        "stdout_path": str(tail),
        "status_path": str(status),
        "controller_pid": controller_pid,
        "controller_session_id": controller_session_id,
        "controller_label": controller_label,
    }
    if parent_dispatch_id:
        payload["parent_dispatch_id"] = parent_dispatch_id
    if codex_home is not None:
        payload.update(
            {
                "codex_session_id": SESSION_ID,
                "codex_home": str(codex_home),
                "codex_home_owner_dispatch_id": codex_home.name,
            }
        )
    L.write_record(payload)
    return payload


def _write_status(tmp_path: Path, record_dispatch_id: str, **updates: object) -> dict:
    payload = {
        "schema": "goalflight.status.v1",
        "dispatch_id": record_dispatch_id,
        **updates,
    }
    (tmp_path / f"{record_dispatch_id}.status.json").write_text(
        json.dumps(payload), encoding="utf-8"
    )
    return payload


def _read(dispatch_id: str) -> dict:
    return json.loads(L.record_path(dispatch_id).read_text(encoding="utf-8"))


def _run(tmp_path: Path, *, now: dt.datetime | None = None, dry_run: bool = False) -> dict:
    queue_dir = tmp_path / "state" / "dispatch-queue"
    queue_dir.mkdir(parents=True, exist_ok=True)
    return D.reconcile_abandoned_dispatches(
        queue_dir=queue_dir,
        dry_run=dry_run,
        now=now or _future_now(),
    )


def _write_active_lease(dispatch_id: str, lease_id: str) -> None:
    state = {
        "schema": C.SCHEMA,
        "machine_id": "fixture-machine",
        "leases": {
            lease_id: {
                "lease_id": lease_id,
                "dispatch_id": dispatch_id,
                "state": "active",
            }
        },
        "cooldowns": {},
    }
    with C.StateLock():
        C.save_state(state)


def _stub_resume_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[list[dict], list[int]]:
    spawn_calls: list[dict] = []
    worker_pids: list[int] = []
    monkeypatch.setattr(D, "_reap_quota_stuck_before_bash_launch", lambda: None)
    monkeypatch.setattr(D, "_resolve_account_env", lambda _args: {})
    monkeypatch.setattr(D, "_acquire_capacity", lambda *_args, **_kwargs: "lease-resume")
    monkeypatch.setattr(
        D,
        "_rebuild_codex_resume_home",
        lambda _root, _parent, expected_home, _session, **_kwargs: (
            str(expected_home),
            "fixture-seat",
        ),
    )
    monkeypatch.setattr(D, "_mark_queue_claim_launch_started", lambda _args: None)
    monkeypatch.setattr(D, "_mark_queue_claim_worker_spawn_intent", lambda _args: None)
    monkeypatch.setattr(D, "_mark_queue_claim_worker_spawned", lambda _args, _pid: None)
    monkeypatch.setattr(
        D,
        "_process_identity_after_spawn",
        lambda pid: {"pid": pid, "lstart": "fixture", "comm": "codex"},
    )
    monkeypatch.setattr(D, "process_group_id", lambda pid: pid)
    monkeypatch.setattr(D, "_start_caffeinate", lambda *_args, **_kwargs: (None, None))
    monkeypatch.setattr(D, "_attach_worker_to_lease", lambda *_args: None)
    monkeypatch.setattr(D, "_detach_lease_to_worker", lambda *_args: None)
    monkeypatch.setattr(D, "_write_pidfile", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(D, "_upsert_project_registry_for_dispatch", lambda *_args: None)

    def spawn(argv: list[str], **kwargs: object) -> int:
        pid = 42000 + len(spawn_calls) + 1
        spawn_calls.append({"argv": list(argv), "label": kwargs.get("label"), "pid": pid})
        if kwargs.get("label") == "worker":
            worker_pids.append(pid)
        return pid

    monkeypatch.setattr(D, "_spawn_daemonized_process", spawn)
    return spawn_calls, worker_pids


def _write_rollout(home: Path) -> None:
    rollout = (
        home
        / "sessions"
        / "2026"
        / "08"
        / "03"
        / f"rollout-2026-08-03T12-00-00-{SESSION_ID}.jsonl"
    )
    rollout.parent.mkdir(parents=True, exist_ok=True)
    rollout.write_text('{"type":"session_meta"}\n', encoding="utf-8")


def test_no_pid_no_lease_no_marker_closes_without_asserting_outcome(tmp_path: Path) -> None:
    dispatch_id = "abandoned-no-verdict"
    _record(tmp_path, dispatch_id)

    result = _run(tmp_path)
    closed = _read(dispatch_id)

    assert result["closed"] == 1
    assert closed["state"] == "inconclusive_no_final"
    assert closed["terminal_state"] == "inconclusive_no_final"
    assert closed["reason"] == "abandoned_without_verdict"
    reconciliation = closed["outcome"]["reconciliation"]
    assert reconciliation["source"] == "goalflight_dispatch.drain"
    assert reconciliation["basis"] == "inferred_abandonment"
    assert reconciliation["observed_outcome"] is False
    assert reconciliation["process_evidence"] == "no_recorded_pid"
    assert reconciliation["lease_evidence"] == "lease_absent"


def test_live_worker_pid_is_never_closed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    dispatch_id = "live-worker"
    _record(
        tmp_path,
        dispatch_id,
        worker_pid=os.getpid(),
        worker_identity=L.process_identity(os.getpid()),
    )
    real_pid = os.getpid()

    def identity_matches(record: dict) -> tuple[bool, str]:
        assert record["worker_pid"] == real_pid
        return True, "live"

    monkeypatch.setattr(D.goalflight_ledger, "identity_matches", identity_matches)

    result = _run(tmp_path)

    assert result["closed"] == 0
    assert _read(dispatch_id)["state"] == "running"
    assert result["kept_reasons"] == {"worker_live_or_indeterminate": 1}


def test_status_worker_alive_without_pid_is_never_closed(tmp_path: Path) -> None:
    dispatch_id = "status-says-live"
    _record(tmp_path, dispatch_id)
    _write_status(tmp_path, dispatch_id, worker_alive=True)

    result = _run(tmp_path)

    assert result["closed"] == 0
    assert _read(dispatch_id)["state"] == "running"
    assert result["kept_reasons"] == {"worker_live_or_indeterminate": 1}


def test_live_persisted_descendant_is_never_closed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    dispatch_id = "live-descendant"
    record = _record(tmp_path, dispatch_id)
    record["producer_descendants"] = [
        {"pid": os.getpid(), "identity": {"generation": "descendant"}}
    ]
    L.write_record(record)

    def identity_matches(probe: dict) -> tuple[bool, str]:
        assert probe["worker_pid"] == os.getpid()
        assert probe["worker_identity"]["generation"] == "descendant"
        return True, "live"

    monkeypatch.setattr(D.goalflight_ledger, "identity_matches", identity_matches)

    result = _run(tmp_path)

    assert result["closed"] == 0
    assert _read(dispatch_id)["state"] == "running"
    assert result["kept_reasons"] == {"worker_live_or_indeterminate": 1}


def test_malformed_nonempty_pid_is_ambiguous(tmp_path: Path) -> None:
    dispatch_id = "malformed-pid"
    _record(tmp_path, dispatch_id, worker_pid="not-a-pid")

    result = _run(tmp_path)

    assert result["closed"] == 0
    assert _read(dispatch_id)["state"] == "running"
    assert result["kept_reasons"] == {"worker_live_or_indeterminate": 1}


def test_conflicting_identities_for_same_pid_probe_both(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    dispatch_id = "same-pid-new-generation"
    pid = 44551
    _record(
        tmp_path,
        dispatch_id,
        worker_pid=pid,
        worker_identity={"generation": "stale"},
    )
    _write_status(
        tmp_path,
        dispatch_id,
        worker_pid=pid,
        expected_worker_identity={"generation": "live"},
    )
    probes: list[str] = []

    def identity_matches(probe: dict) -> tuple[bool, str]:
        generation = probe["worker_identity"]["generation"]
        probes.append(generation)
        return (generation == "live", "live" if generation == "live" else "dead")

    monkeypatch.setattr(D.goalflight_ledger, "identity_matches", identity_matches)

    result = _run(tmp_path)

    assert probes == ["stale", "live"]
    assert result["closed"] == 0
    assert _read(dispatch_id)["state"] == "running"


def test_terminal_marker_reconciles_observed_real_outcome(tmp_path: Path) -> None:
    dispatch_id = "observed-complete"
    _record(
        tmp_path,
        dispatch_id,
        tail_text=f"work log\nCOMPLETE: {dispatch_id} — verified result\n",
    )

    result = _run(tmp_path)
    closed = _read(dispatch_id)

    assert result["closed"] == 1
    assert closed["state"] == "complete"
    assert closed["terminal_state"] == "complete"
    assert closed["terminal_marker"]["kind"] == "COMPLETE"
    reconciliation = closed["outcome"]["reconciliation"]
    assert reconciliation["basis"] == "observed_terminal_marker"
    assert reconciliation["observed_outcome"] is True
    assert reconciliation["terminal_marker_kind"] == "COMPLETE"


def test_terminal_marker_arriving_after_final_evaluation_wins(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    dispatch_id = "marker-during-close"
    record = _record(tmp_path, dispatch_id)
    tail = Path(record["stdout_path"])
    original = D._abandoned_terminal_outcome
    injected = False

    def inject_marker(fresh: dict) -> tuple[str, object, dict | None]:
        nonlocal injected
        if not injected:
            injected = True
            with tail.open("a", encoding="utf-8") as stream:
                stream.write(f"COMPLETE: {dispatch_id} — terminal marker reached disk\n")
        return original(fresh)

    monkeypatch.setattr(D, "_abandoned_terminal_outcome", inject_marker)

    result = _run(tmp_path)
    closed = _read(dispatch_id)

    assert injected is True
    assert result["closed"] == 1
    assert closed["state"] == "complete"
    assert closed["outcome"]["reconciliation"]["basis"] == "observed_terminal_marker"


def test_recent_progress_is_ambiguous_and_left_open(tmp_path: Path) -> None:
    dispatch_id = "recent-progress"
    _record(tmp_path, dispatch_id)

    result = _run(tmp_path, now=dt.datetime.now(dt.timezone.utc))

    assert result["closed"] == 0
    assert _read(dispatch_id)["state"] == "running"
    assert result["kept_reasons"] == {"recent_progress": 1}


def test_young_partially_written_record_is_left_open(tmp_path: Path) -> None:
    dispatch_id = "young-partial-record"
    record = _record(tmp_path, dispatch_id)
    Path(record["stdout_path"]).unlink()

    result = _run(tmp_path, now=dt.datetime.now(dt.timezone.utc))

    assert result["closed"] == 0
    assert _read(dispatch_id)["state"] == "running"
    assert result["kept_reasons"] == {"recent_progress": 1}


def test_corrupt_status_is_ambiguous_and_left_open(tmp_path: Path) -> None:
    dispatch_id = "corrupt-status"
    _record(tmp_path, dispatch_id)
    (tmp_path / f"{dispatch_id}.status.json").write_text("{", encoding="utf-8")

    result = _run(tmp_path)

    assert result["closed"] == 0
    assert _read(dispatch_id)["state"] == "running"
    assert result["kept_reasons"] == {"status_indeterminate": 1}


def test_mismatched_status_is_ambiguous_and_left_open(tmp_path: Path) -> None:
    dispatch_id = "mismatched-status"
    _record(tmp_path, dispatch_id)
    _write_status(tmp_path, dispatch_id, dispatch_id="different-dispatch")

    result = _run(tmp_path)

    assert result["closed"] == 0
    assert _read(dispatch_id)["state"] == "running"
    assert result["kept_reasons"] == {"status_indeterminate": 1}


def test_missing_status_pointer_is_ambiguous_and_left_open(tmp_path: Path) -> None:
    dispatch_id = "missing-status-pointer"
    record = _record(tmp_path, dispatch_id)
    record.pop("status_path")
    L.write_record(record)

    result = _run(tmp_path)

    assert result["closed"] == 0
    assert _read(dispatch_id)["state"] == "running"
    assert result["kept_reasons"] == {"status_indeterminate": 1}


def test_missing_output_pointer_is_ambiguous_and_left_open(tmp_path: Path) -> None:
    dispatch_id = "missing-output-pointer"
    record = _record(tmp_path, dispatch_id)
    record.pop("stdout_path")
    L.write_record(record)

    result = _run(tmp_path)

    assert result["closed"] == 0
    assert _read(dispatch_id)["state"] == "running"
    assert result["kept_reasons"] == {"output_indeterminate": 1}


def test_active_lease_vetoes_reconciliation(tmp_path: Path) -> None:
    dispatch_id = "leased-worker"
    lease_id = "lease-live"
    _record(tmp_path, dispatch_id, lease_id=lease_id)
    _write_active_lease(dispatch_id, lease_id)

    result = _run(tmp_path)

    assert result["closed"] == 0
    assert _read(dispatch_id)["state"] == "running"
    assert result["kept_reasons"] == {"lease_live_or_indeterminate": 1}


def test_malformed_matching_lease_is_ambiguous(tmp_path: Path) -> None:
    dispatch_id = "malformed-lease"
    lease_id = "lease-malformed"
    _record(tmp_path, dispatch_id, lease_id=lease_id)
    with C.StateLock():
        C.save_state(
            {
                "schema": C.SCHEMA,
                "machine_id": "fixture-machine",
                "leases": {lease_id: "partially-written"},
                "cooldowns": {},
            }
        )

    result = _run(tmp_path)

    assert result["closed"] == 0
    assert _read(dispatch_id)["state"] == "running"
    assert result["kept_reasons"] == {"lease_live_or_indeterminate": 1}


def test_live_controller_beacon_vetoes_reconciliation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    dispatch_id = "controller-owned"
    controller_pid = os.getpid()
    _record(
        tmp_path,
        dispatch_id,
        controller_pid=controller_pid,
        controller_session_id="controller-session",
    )
    monkeypatch.setattr(
        D.goalflight_session_status,
        "live_session",
        lambda _project, *, label=None: (
            {"id": "controller-session", "pid": controller_pid}
            if label is None
            else pytest.fail(f"unexpected label: {label}")
        ),
    )

    result = _run(tmp_path)

    assert result["closed"] == 0
    assert _read(dispatch_id)["state"] == "running"
    assert result["kept_reasons"] == {"controller_live_or_indeterminate": 1}


def test_live_stable_controller_label_vetoes_after_session_rollover(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    dispatch_id = "stable-controller-owner"
    _record(
        tmp_path,
        dispatch_id,
        controller_pid=11111,
        controller_session_id="old-controller-session",
        controller_label="controller-a",
    )

    def live_session(_project: Path, *, label: str | None = None) -> dict:
        assert label == "controller-a"
        return {
            "id": "new-controller-session",
            "pid": os.getpid(),
            "label": "controller-a",
        }

    monkeypatch.setattr(D.goalflight_session_status, "live_session", live_session)

    result = _run(tmp_path)

    assert result["closed"] == 0
    assert _read(dispatch_id)["state"] == "running"
    assert result["kept_reasons"] == {"controller_live_or_indeterminate": 1}


def test_reconciliation_is_idempotent(tmp_path: Path) -> None:
    dispatch_id = "idempotent-close"
    _record(tmp_path, dispatch_id)

    first = _run(tmp_path)
    record_path = L.record_path(dispatch_id)
    after_first = record_path.read_bytes()
    second = _run(tmp_path, now=_future_now(1200.0))

    assert first["closed"] == 1
    assert second["closed"] == 0
    assert record_path.read_bytes() == after_first


def test_local_drain_tick_runs_reconciliation_automatically(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    queue_dir = tmp_path / "queue"
    queue_dir.mkdir()
    calls: list[Path] = []
    expected = {
        "schema": D.ABANDONED_RECONCILIATION_SCHEMA,
        "mode": "automatic",
        "closed": 2,
    }
    monkeypatch.setattr(D, "_release_stale_capacity_for_drain", lambda: None)
    monkeypatch.setattr(
        D,
        "_reconcile_abandoned_for_drain",
        lambda path: calls.append(path) or expected,
    )
    monkeypatch.setattr(
        D,
        "_recover_claimed_queue_entries",
        lambda *_args, **_kwargs: {"restored": 0, "failed": 0},
    )
    args = SimpleNamespace(
        queue_dir=str(queue_dir),
        remote_node=None,
        claim_stale_s=300.0,
        limit=0,
    )

    payload = D._drain_queue_once(args)

    assert calls == [queue_dir]
    assert payload["abandoned_reconciliation"] == expected


def test_inferred_abandonment_is_resumable_and_fresh_child_stays_live(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    parent_id = "resume-parent"
    stale_child_id = "resume-child-stale"
    fresh_child_id = "resume-child-fresh"
    home = tmp_path / "state" / "dispatch-homes" / parent_id
    _write_rollout(home)
    parent = _record(tmp_path, parent_id, codex_home=home)
    stale_child = _record(
        tmp_path,
        stale_child_id,
        parent_dispatch_id=parent_id,
        codex_home=home,
        state="waiting_capacity",
    )
    stale_child["codex_home_owner_dispatch_id"] = parent_id
    L.write_record(stale_child)

    first = _run(tmp_path)
    assert first["closed"] == 2
    assert _read(parent_id)["state"] == "inconclusive_no_final"
    assert _read(stale_child_id)["state"] == "inconclusive_no_final"

    prompt = tmp_path / "resume.md"
    prompt.write_text("Add one more feature.", encoding="utf-8")
    monkeypatch.setattr(D, "_reserve_auto_dispatch_id", lambda *_args: fresh_child_id)
    spawn_calls, worker_pids = _stub_resume_runtime(monkeypatch)

    def identity_matches(probe: dict) -> tuple[bool, str]:
        try:
            pid = int(probe.get("worker_pid") or 0)
        except (TypeError, ValueError):
            return False, "identity_indeterminate"
        return (pid in worker_pids, "live" if pid in worker_pids else "dead")

    monkeypatch.setattr(D.goalflight_ledger, "identity_matches", identity_matches)

    assert D._cmd_resume([parent_id, "--prompt-file", str(prompt)]) == 0
    resumed = _read(fresh_child_id)
    reopened_parent = _read(parent_id)
    assert [call["label"] for call in spawn_calls] == ["worker", "watcher"]
    assert resumed["state"] == "running"
    assert resumed["parent_dispatch_id"] == parent_id
    assert "outcome" not in resumed
    assert L.parse_utc(resumed["started_at"]) > L.parse_utc(parent["started_at"])
    assert reopened_parent["state"] == "superseded"
    assert reopened_parent["resumed_by_dispatch_id"] == fresh_child_id
    assert reopened_parent["resumed_at"] == resumed["started_at"]
    # `resumed_at` is COPIED from the child's started_at, so equality above is a
    # real contract. `updated_at` is stamped by write_record at write time, so it
    # equals started_at only when the write happens to land in the same second --
    # asserting equality here made the suite fail whenever the resume straddled a
    # tick (observed 2026-08-10: 18:02:36 vs 18:02:37). The actual guarantee is
    # ordering: the parent is updated at or after the child starts.
    assert L.parse_utc(reopened_parent["updated_at"]) >= L.parse_utc(resumed["started_at"])
    assert reopened_parent["outcome"]["reconciliation"]["basis"] == "inferred_abandonment"
    assert reopened_parent["outcome"]["resume"]["dispatch_id"] == fresh_child_id

    second = _run(tmp_path, now=_future_now(1800.0))
    assert second["closed"] == 0
    assert _read(fresh_child_id)["state"] == "running"
