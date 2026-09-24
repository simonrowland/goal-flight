from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import goalflight_traffic as traffic  # noqa: E402


def test_live_traffic_excludes_terminal_and_pid_reused_dispatches(
    tmp_path: Path, monkeypatch, capsys
):
    dispatch_dir = tmp_path / "state" / "dispatch"
    dispatch_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("GOALFLIGHT_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("GOALFLIGHT_DISPATCH_DIR", str(dispatch_dir))

    live_pid = os.getpid()
    live_identity = {
        "pid": live_pid,
        "start_token": "fixture-live-process",
        "identity_available": True,
    }
    monkeypatch.setattr(
        traffic,
        "_batch_process_identities",
        lambda pids: {
            pid: live_identity if pid == live_pid else None for pid in set(pids)
        },
    )
    reused_identity = dict(live_identity)
    reused_identity["start_token"] = "different-process"

    def write_status(dispatch_id: str, **payload: object) -> None:
        status = {
            "schema": "goalflight.status.v1",
            "dispatch_id": dispatch_id,
            "state": "running",
            "worker_pid": live_pid,
            "worker_identity": live_identity,
            **payload,
        }
        (dispatch_dir / f"{dispatch_id}.status.json").write_text(
            json.dumps(status), encoding="utf-8"
        )

    write_status(
        "live-sol",
        agent="codex",
        model="gpt-5.6-sol",
        controller_label="alpha",
    )
    astra_tail = dispatch_dir / "live-astra.tail"
    astra_tail.write_text("model: gpt-6-astra\n", encoding="utf-8")
    write_status(
        "live-astra",
        agent="codex",
        controller_label="beta",
        tail_path=str(astra_tail),
    )
    write_status(
        "live-luna",
        agent="codex",
        model="gpt-5.5-luna",
        controller_label="alpha",
    )
    write_status(
        "live-grok",
        agent="grok-code",
        controller_label="gamma",
    )
    write_status(
        "unverified-sol",
        agent="codex",
        model="gpt-5.6-sol",
        controller_label="delta",
        worker_identity=None,
    )
    write_status(
        "terminal-reused",
        agent="codex",
        model="gpt-5.6-sol",
        controller_label="terminal",
        state="quota_exhausted",
    )
    write_status(
        "pid-reused",
        agent="codex",
        model="gpt-5.6-sol",
        controller_label="reused",
        worker_identity=reused_identity,
    )
    (dispatch_dir / "dead.status.json").write_text(
        json.dumps(
            {
                "schema": "goalflight.status.v1",
                "dispatch_id": "dead",
                "state": "running",
                "agent": "codex",
                "model": "gpt-5.5-luna",
                "controller_label": "dead",
                "worker_pid": 999_999_999,
                "worker_identity": {"pid": 999_999_999, "start_token": "gone"},
            }
        ),
        encoding="utf-8",
    )

    summary = traffic.live_workers_by_model()

    assert summary == {
        "total": 4,
        "unverified_total": 1,
        "models": {
            "gpt-5.6-sol": {
                "count": 1,
                "unverified": 1,
                "controllers": {"alpha": 1},
                "unverified_controllers": {"delta": 1},
            },
            "gpt-5.5-luna": {
                "count": 1,
                "unverified": 0,
                "controllers": {"alpha": 1},
                "unverified_controllers": {},
            },
            "gpt-6-astra": {
                "count": 1,
                "unverified": 0,
                "controllers": {"beta": 1},
                "unverified_controllers": {},
            },
            "grok": {
                "count": 1,
                "unverified": 0,
                "controllers": {"gamma": 1},
                "unverified_controllers": {},
            },
        },
    }
    rendered = traffic.render(summary)
    assert "MODEL" in rendered and "UNVERIFIED" in rendered
    assert "poor value vs grok" in rendered
    assert "use sparingly" in rendered
    assert "delta:1" in rendered
    assert traffic.live_mix_pointer(summary) == (
        "live mix: luna 1, grok 1, astra 1, sol 1; see /goal-flight traffic"
    )

    assert traffic.main(["--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload[traffic.JSON_KEY] == summary


def test_recorded_model_precedes_agent_family_and_tail(tmp_path: Path) -> None:
    tail = tmp_path / "worker.tail"
    tail.write_text("model: tail-model\n", encoding="utf-8")

    assert traffic._dispatch_model(
        {"agent": "grok-code", "model": "recorded-model", "tail_path": str(tail)}
    ) == "recorded-model"
    assert traffic._dispatch_model(
        {"agent": "cursor-agent", "model": "cursor-recorded", "tail_path": str(tail)}
    ) == "cursor-recorded"
    assert traffic._dispatch_model(
        {"agent": "cursor-agent", "tail_path": str(tail)}
    ) == "tail-model"
    assert traffic._dispatch_model({"agent": "grok-code"}) == "grok"
    assert traffic._dispatch_model({"agent": "cursor-agent"}) == "cursor"


def test_normalized_terminal_state_is_excluded(tmp_path: Path, monkeypatch) -> None:
    dispatch_dir = tmp_path / "dispatch"
    dispatch_dir.mkdir()
    pid = os.getpid()
    record = {
        "dispatch_id": "attention-terminal",
        "agent": "codex",
        "model": "gpt-5.6-sol",
        "controller_label": "terminal",
        "worker_pid": pid,
        "worker_identity": {"pid": pid, "start_token": "same"},
        "state": "blocked_user_confirm",
    }
    monkeypatch.setattr(traffic, "_batch_process_identities", lambda pids: {pid: {"pid": pid}})

    assert traffic.live_workers_by_model(
        ledger_records=[record], dispatch_dir=dispatch_dir
    ) == {"total": 0, "unverified_total": 0, "models": {}}


def test_unreadable_ledger_marks_status_worker_unverified(
    tmp_path: Path, monkeypatch
) -> None:
    dispatch_dir = tmp_path / "dispatch"
    dispatch_dir.mkdir()
    pid = os.getpid()
    identity = {
        "pid": pid,
        "start_token": "same-process",
        "identity_available": True,
    }
    (dispatch_dir / "ledger-unknown.status.json").write_text(
        json.dumps(
            {
                "dispatch_id": "ledger-unknown",
                "state": "running",
                "agent": "codex",
                "model": "gpt-5.6-sol",
                "controller_label": "unknown-ledger",
                "worker_pid": pid,
                "worker_identity": identity,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        traffic.goalflight_ledger,
        "read_records",
        lambda **_kwargs: (_ for _ in ()).throw(OSError("ledger denied")),
    )
    probed = []

    def fake_batch(pids):
        probed.extend(pids)
        return {pid: identity for pid in pids}

    monkeypatch.setattr(traffic, "_batch_process_identities", fake_batch)

    summary = traffic.live_workers_by_model(dispatch_dir=dispatch_dir)

    assert probed == []
    assert summary == {
        "total": 0,
        "unverified_total": 1,
        "models": {
            "gpt-5.6-sol": {
                "count": 0,
                "unverified": 1,
                "controllers": {},
                "unverified_controllers": {"unknown-ledger": 1},
            }
        },
    }
    assert "total unverified: 1" in traffic.render(summary)


def test_unreadable_ledger_and_status_dir_report_unknown(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    dispatch_dir = tmp_path / "missing-dispatch"
    monkeypatch.setenv("GOALFLIGHT_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("GOALFLIGHT_DISPATCH_DIR", str(dispatch_dir))
    monkeypatch.setattr(
        traffic.goalflight_ledger,
        "read_records",
        lambda **_kwargs: (_ for _ in ()).throw(OSError("ledger denied")),
    )

    assert traffic.main(["--json"]) == 0
    summary = json.loads(capsys.readouterr().out)[traffic.JSON_KEY]

    assert summary["total"] == 0
    assert summary["unverified_total"] == 1
    assert summary["models"]["UNKNOWN"]["unverified"] == 1
    assert summary["unknown_reasons"] == [
        f"ledger unreadable; status directory unavailable: {dispatch_dir}"
    ]
    assert "UNKNOWN:" in traffic.render(summary)
    assert "UNKNOWN:" in traffic.live_mix_pointer(summary)


def test_terminal_ledger_state_wins_over_live_sidecar(
    tmp_path: Path, monkeypatch
) -> None:
    state_dir = tmp_path / "state"
    runs_dir = state_dir / "runs.d"
    dispatch_dir = state_dir / "dispatch"
    runs_dir.mkdir(parents=True)
    dispatch_dir.mkdir(exist_ok=True)
    monkeypatch.setenv("GOALFLIGHT_STATE_DIR", str(state_dir))
    monkeypatch.setenv("GOALFLIGHT_DISPATCH_DIR", str(dispatch_dir))
    pid = os.getpid()
    identity = {
        "pid": pid,
        "start_token": "same-process",
        "identity_available": True,
    }
    ledger_record = {
        "dispatch_id": "terminal-live",
        "state": "complete",
        "terminal_state": "complete",
        "worker_pid": pid,
        "worker_identity": identity,
    }
    (runs_dir / "terminal-live.json").write_text(
        json.dumps(ledger_record, indent=2, sort_keys=True), encoding="utf-8"
    )
    (dispatch_dir / "terminal-live.status.json").write_text(
        json.dumps(
            {
                "dispatch_id": "terminal-live",
                "state": "running",
                "agent": "codex",
                "model": "gpt-5.6-sol",
                "controller_label": "stale-sidecar",
                "worker_pid": pid,
                "worker_identity": identity,
            }
        ),
        encoding="utf-8",
    )
    probed = []

    def fake_batch(pids):
        probed.extend(pids)
        return {pid: identity for pid in pids}

    monkeypatch.setattr(traffic, "_batch_process_identities", fake_batch)

    assert traffic.live_workers_by_model(dispatch_dir=dispatch_dir) == {
        "total": 0,
        "unverified_total": 0,
        "models": {},
    }
    assert probed == []


def test_batch_identity_probe_uses_one_ps_call(monkeypatch) -> None:
    pids = [111, 222]
    calls = []
    monkeypatch.setattr(traffic.goalflight_ledger.goalflight_compat, "is_windows", lambda: False)
    monkeypatch.setattr(traffic.goalflight_ledger.goalflight_compat, "pid_liveness", lambda pid: True)
    monkeypatch.setattr(
        traffic.goalflight_ledger.goalflight_compat,
        "process_start_identity",
        lambda pid: {"pid": pid, "start_token": f"start-{pid}"},
    )

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(
            returncode=0,
            stdout=(
                "111 1 1 Mon Jan  1 00:00:00 2024 worker worker --one\n"
                "222 1 1 Mon Jan  1 00:00:01 2024 worker worker --two\n"
            ),
        )

    monkeypatch.setattr(traffic.subprocess, "run", fake_run)

    identities = traffic._batch_process_identities([111, 222, 111])

    assert len(calls) == 1
    assert calls[0][0][-1] == "111,222"
    assert identities[111]["start_token"] == "start-111"
    assert identities[222]["start_token"] == "start-222"


def test_traffic_reads_terminal_skipping_ledger_and_bounds_status_scan(
    tmp_path: Path, monkeypatch
) -> None:
    dispatch_dir = tmp_path / "dispatch"
    dispatch_dir.mkdir()
    for index in range(traffic.STATUS_SCAN_LIMIT + 20):
        (dispatch_dir / f"old-{index}.status.json").write_text(
            json.dumps({"dispatch_id": f"old-{index}", "state": "complete"}),
            encoding="utf-8",
        )
    observed = {}

    def fake_read_records(**kwargs):
        observed.update(kwargs)
        return []

    read_payloads = 0
    original_read = traffic._read_json_mapping

    def counting_read(path):
        nonlocal read_payloads
        read_payloads += 1
        return original_read(path)

    monkeypatch.setattr(traffic.goalflight_ledger, "read_records", fake_read_records)
    monkeypatch.setattr(traffic, "_read_json_mapping", counting_read)

    assert traffic.live_workers_by_model(dispatch_dir=dispatch_dir)["total"] == 0
    assert observed == {
        "skip_terminal": True,
        "recent_window_days": traffic.goalflight_ledger.STATUS_RECENT_WINDOW_DAYS,
    }
    assert read_payloads <= traffic.STATUS_SCAN_LIMIT


def test_traffic_does_not_probe_old_nonterminal_ledger_rows(monkeypatch) -> None:
    old = {
        "dispatch_id": "old-live",
        "agent": "codex",
        "worker_pid": 111,
        "worker_identity": {"pid": 111, "start_token": "old"},
        "state": "running",
        "started_at": time.time() - 8 * 86400,
    }
    recent = {
        **old,
        "dispatch_id": "recent-live",
        "worker_pid": 222,
        "worker_identity": {"pid": 222, "start_token": "recent"},
        "started_at": time.time(),
    }
    probed = []

    def fake_batch(pids):
        probed.extend(pids)
        return {pid: {"pid": pid} for pid in pids}

    monkeypatch.setattr(traffic, "_batch_process_identities", fake_batch)
    traffic.live_workers_by_model(ledger_records=[old, recent])

    assert probed == [222]
