"""Controller lane registry, cadence ownership, and terse pulse behavior."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import subprocess
import sys
import threading
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(SCRIPTS))

import goalflight_cadence as cadence  # noqa: E402
import goalflight_capacity  # noqa: E402
import goalflight_session_status as sessions  # noqa: E402
import goalflight_task as task  # noqa: E402
import goalflight_wake_supervise as supervise  # noqa: E402


NOW = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _hermetic_state_dirs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GOALFLIGHT_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("GOALFLIGHT_TASK_STORE_DIR", str(tmp_path / "task-store"))


def _project(tmp_path: Path) -> tuple[Path, task.TaskStore]:
    root = tmp_path / "project"
    root.mkdir()
    return root, task.TaskStore(root)


def _registry(
    store: task.TaskStore,
    labels: list[str],
    *,
    owner: str | None = None,
) -> None:
    lanes = {
        label: {"lane": label.removesuffix("-main"), "charter": ["Own work", "Keep it moving", "Record outcomes"]}
        for label in labels
    }

    def update(state: dict[str, object]) -> None:
        state["lanes"] = lanes
        state["registry_order"] = labels
        state["project_duty_owner"] = owner

    store.mutate_controller_cadence(update)


def _ready_tasks(store: task.TaskStore, lane: str, count: int = 3) -> None:
    store.save_items_atomic([
        {
            "schema_version": 1,
            "id": f"t-{index:03}",
            "kind": "task",
            "title": f"Ready task {index}",
            "lane": lane,
            "blocked_by": [],
            "links": [],
            "done": False,
            "created_at": NOW.isoformat(),
            "created_by": "test",
        }
        for index in range(1, count + 1)
    ])


def _lease_probe(
    leases: dict[str, tuple[str, dict[str, object] | None]],
):
    def probe(_root: Path, *, label: str) -> tuple[str, dict[str, object] | None]:
        return leases.get(label, ("dead", None))

    return probe


def _live_lease(nonce: str, started_at: datetime) -> tuple[str, dict[str, object]]:
    return (
        "live",
        {"lease_nonce": nonce, "started_at": started_at.isoformat()},
    )


def test_lane_cli_records_charter_owner_and_filters_next(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root, store = _project(tmp_path)
    charter = "Own the build lane\nKeep tasks moving\nRecord decisions"

    assert task.main([
        "--project-root", str(root), "lanes", "set-charter", "alpha-main",
        "build", "--charter", charter,
    ]) == 0
    assert task.main([
        "--project-root", str(root), "lanes", "set-owner", "alpha-main",
    ]) == 0
    capsys.readouterr()
    store.save_items_atomic([
        {
            "schema_version": 1,
            "id": "t-501",
            "kind": "task",
            "title": "Build lane task",
            "lane": "build",
            "blocked_by": [],
            "links": [],
            "done": False,
            "created_at": "2026-10-01T00:00:00+00:00",
            "created_by": "test",
        },
        {
            "schema_version": 1,
            "id": "t-502",
            "kind": "task",
            "title": "Other lane task",
            "lane": "ops",
            "blocked_by": [],
            "links": [],
            "done": False,
            "created_at": "2026-10-01T00:00:00+00:00",
            "created_by": "test",
        },
    ])

    state = store.load_controller_cadence()
    assert state["lanes"]["alpha-main"]["charter"] == charter.splitlines()
    assert state["project_duty_owner"] == "alpha-main"
    assert state["registry_order"] == ["alpha-main"]

    assert task.main([
        "--project-root", str(root), "next", "--lane", "build", "--json",
    ]) == 0
    rows = json.loads(capsys.readouterr().out)
    assert [row["id"] for row in rows] == ["t-501"]


def test_due_and_done_keep_lane_and_project_scopes_separate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, store = _project(tmp_path)
    _registry(store, ["alpha-main", "beta"])
    monkeypatch.setattr(
        sessions,
        "probe_live_session",
        _lease_probe({
            "alpha-main": _live_lease("a1", NOW - timedelta(days=5)),
            "beta": _live_lease("b1", NOW - timedelta(days=2)),
        }),
    )

    assert {row["id"] for row in cadence.due_items(root, "alpha-main", now=NOW)} == {
        "look-ahead", "ci-audit", "tooling-survey", "dedup-scan", "task-store-audit",
    }
    assert {row["id"] for row in cadence.due_items(root, "beta", now=NOW)} == {
        "look-ahead", "ci-audit", "tooling-survey",
    }
    assert {row["id"] for row in cadence.due_items(root, "unknown", now=NOW)} == {
        "look-ahead", "ci-audit", "tooling-survey",
    }
    cadence.mark_done(root, "look-ahead", "unknown", now=NOW)

    cadence.mark_done(root, "look-ahead", "alpha-main", now=NOW)
    cadence.mark_done(root, "ci-audit", "alpha-main", now=NOW)
    cadence.mark_done(root, "tooling-survey", "alpha-main", now=NOW)
    cadence.mark_done(root, "dedup-scan", "alpha-main", now=NOW)
    cadence.mark_done(root, "task-store-audit", "alpha-main", now=NOW)

    owner_due = {row["id"] for row in cadence.due_items(root, "alpha-main", now=NOW)}
    sibling_due = {row["id"] for row in cadence.due_items(root, "beta", now=NOW)}
    assert not owner_due.intersection(cadence.DEFAULT_CADENCES)
    assert sibling_due == {"look-ahead", "ci-audit", "tooling-survey"}


def test_unchartered_due_done_and_pulse_keep_lane_cadence(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    root, store = _project(tmp_path)
    _ready_tasks(store, "beta")
    monkeypatch.setattr(cadence.goalflight_ledger, "read_records", lambda: [])

    assert cadence.main([
        "--project-root", str(root), "due", "--controller-label", "beta",
    ]) == 0
    output = capsys.readouterr().out
    hint = (
        f'LANE unset -> run: python3 {ROOT / "goalflight_task.py"} lanes set-charter '
        'beta <lane> --charter "<3-5 lines>"'
    )
    assert (ROOT / "goalflight_task.py").is_file()
    assert hint in output
    assert "look-ahead" in output and "ci-audit:" in output

    cadence.mark_done(root, "look-ahead", "beta", now=NOW)
    cadence.mark_done(root, "wake:dedup-scan:period-1", "beta", now=NOW)
    assert store.load_controller_cadence()["last_done"]["look-ahead"]["beta"]
    assert store.load_controller_cadence()["duty_wakes"]["dedup-scan"] == "period-1"

    pulse = cadence.build_pulse_record(root, "beta", now=NOW)
    text = str(pulse["text"])
    assert hint in text
    assert "NEXT:" not in text and "IDLE:" not in text
    assert "CAPACITY:" not in text and "DUE:" in text
    assert len(text.encode("utf-8")) <= cadence.PULSE_TEXT_MAX_BYTES

    store.mutate_controller_cadence(lambda state: state["lanes"].update({"beta": "bad"}))
    assert cadence.main([
        "--project-root", str(root), "due", "--controller-label", "beta",
    ]) == 0
    assert hint in capsys.readouterr().out
    malformed_pulse = cadence.build_pulse_record(root, "beta", now=NOW)
    assert hint in str(malformed_pulse["text"])


def test_pulse_unavailable_uses_allowlisted_reason_without_exception_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, _store = _project(tmp_path)

    def fail(*_args: object, **_kwargs: object) -> None:
        raise OSError("TOKEN=synthetic-secret argv=['worker', '--token', 'secret']")

    monkeypatch.setattr(task.TaskStore, "__init__", fail)
    record = cadence.build_pulse_record(root, "beta", now=NOW)
    reason = str(record["unavailable"])
    assert reason == "store-unreadable: OSError"
    assert "TOKEN=" not in reason and "synthetic-secret" not in reason
    assert "argv" not in reason and "--token" not in reason


def test_due_cli_prints_ci_audit_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root, store = _project(tmp_path)
    _registry(store, ["beta"])
    monkeypatch.setattr(
        sessions, "probe_live_session",
        _lease_probe({"beta": _live_lease("b1", NOW - timedelta(days=2))}),
    )

    assert cadence.main([
        "--project-root", str(root), "due", "--controller-label", "beta",
    ]) == 0
    expected = (
        f"ci-audit: python3 {ROOT / 'scripts' / 'goalflight_ci_audit.py'} "
        "--controller-label beta --since 24h"
    )
    assert expected in capsys.readouterr().out


def test_overdue_wake_line_remains_until_its_done_record_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, store = _project(tmp_path)
    _registry(store, ["alpha-main", "beta", "gamma"])
    leases = {
        "alpha-main": _live_lease("a1", NOW - timedelta(days=5)),
        "beta": _live_lease("b1", NOW - timedelta(days=2)),
        "gamma": _live_lease("g1", NOW - timedelta(days=1)),
    }
    monkeypatch.setattr(sessions, "probe_live_session", _lease_probe(leases))

    def make_overdue(state: dict[str, object]) -> None:
        state["last_done"] = {
            "dedup-scan": {"project": (NOW - timedelta(hours=49)).isoformat()},
        }

    store.mutate_controller_cadence(make_overdue)
    records = []
    for label in ("beta", "gamma"):
        records.append(cadence.build_pulse_record(
            root, label, now=NOW,
        ))
    wake_line = next(line for line in str(records[0]["text"]).splitlines() if line.startswith("WAKE-OWNER:"))
    assert "--to-controller alpha-main" in wake_line
    assert "done wake:dedup-scan:2026-09-29T11:00:00Z/86400 --controller-label beta" in wake_line
    assert "WAKE-OWNER:" in str(records[1]["text"])
    assert "dedup-scan" not in {row["id"] for row in cadence.due_items(root, "beta", now=NOW)}
    assert "dedup-scan" not in {row["id"] for row in cadence.due_items(root, "gamma", now=NOW)}

    cadence.mark_done(root, "wake:dedup-scan:2026-09-29T11:00:00Z/86400", "beta", now=NOW)
    after_done = cadence.build_pulse_record(
        root,
        "beta",
        now=NOW,
    )
    assert "WAKE-OWNER:" not in str(after_done["text"])

    state = store.load_controller_cadence()
    assert state["duty_wakes"]["dedup-scan"] == "2026-09-29T11:00:00Z/86400"


def test_default_project_owner_is_longest_held_live_lease_without_main_lane(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, store = _project(tmp_path)
    _registry(store, ["older", "newer"])
    monkeypatch.setattr(
        sessions,
        "probe_live_session",
        _lease_probe({
            "older": _live_lease("o1", NOW - timedelta(days=5)),
            "newer": _live_lease("n1", NOW - timedelta(days=1)),
        }),
    )

    owner = cadence._resolve_owner(root, store.load_controller_cadence(), {})
    assert owner == "older"


def test_pulse_is_five_lines_bounded_and_prints_idle_from_ledger_and_frontier(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, store = _project(tmp_path)
    _registry(store, ["alpha-main"])
    _ready_tasks(store, "alpha")
    monkeypatch.setattr(cadence.goalflight_ledger, "read_records", lambda: [])
    monkeypatch.setattr(
        sessions, "probe_live_session", _lease_probe({"alpha-main": _live_lease("a1", NOW)})
    )

    records = [
        cadence.build_pulse_record(
            root,
            "alpha-main",
            now=NOW + timedelta(minutes=minute),
        )
        for minute in (0, 1, 2)
    ]
    texts = [str(record["text"]) for record in records]
    assert all(len(text.splitlines()) <= 5 for text in texts)
    assert all(len(text.encode("utf-8")) <= cadence.PULSE_TEXT_MAX_BYTES for text in texts)
    assert all("IDLE: 3 ready, 0 workers; dispatch or record why not" in text for text in texts)
    assert "pulses" not in store.load_controller_cadence()

    serialized = supervise._supervise_line(records[-1])
    assert len(serialized.encode("utf-8")) <= supervise.STREAM_LINE_MAX_BYTES
    assert json.loads(serialized) == records[-1]


def test_pulse_idle_requires_ready_work_no_workers_and_no_recent_activity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, store = _project(tmp_path)
    _registry(store, ["alpha-main"])
    _ready_tasks(store, "alpha", 1)
    rows: list[dict[str, object]] = []
    monkeypatch.setattr(cadence.goalflight_ledger, "read_records", lambda: list(rows))

    idle = cadence.build_pulse_record(root, "alpha-main", now=NOW)
    assert "IDLE: 1 ready, 0 workers; dispatch or record why not" in str(idle["text"])

    rows.append({
        "dispatch_id": "active-worker",
        "controller_label": "alpha-main",
        "project_root": str(root),
        "started_at": (NOW - timedelta(minutes=1)).isoformat(),
        "state": "working",
    })
    with_worker = cadence.build_pulse_record(root, "alpha-main", now=NOW)
    assert "IDLE:" not in str(with_worker["text"])
    assert "CAPACITY:" not in str(with_worker["text"])

    rows[:] = [{
        "dispatch_id": "recent-review",
        "controller_label": "alpha-main",
        "project_root": str(tmp_path / "another-project"),
        "started_at": (NOW - timedelta(minutes=1)).isoformat(),
        "state": "complete",
    }]
    recent_activity = cadence.build_pulse_record(root, "alpha-main", now=NOW)
    assert "IDLE:" not in str(recent_activity["text"])

    rows[:] = [{
        "dispatch_id": "recent-landed-review",
        "controller_label": "alpha-main",
        "project_root": str(tmp_path / "another-project"),
        "started_at": (NOW - timedelta(hours=1)).isoformat(),
        "terminal_at": (NOW - timedelta(minutes=1)).isoformat(),
        "state": "complete",
    }]
    recent_landing = cadence.build_pulse_record(root, "alpha-main", now=NOW)
    assert "IDLE:" not in str(recent_landing["text"])

    rows.clear()
    _ready_tasks(store, "alpha", 0)
    no_ready_work = cadence.build_pulse_record(root, "alpha-main", now=NOW)
    assert "IDLE:" not in str(no_ready_work["text"])


def test_pulse_omits_idle_when_activity_inputs_are_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, store = _project(tmp_path)
    _registry(store, ["alpha-main"])
    _ready_tasks(store, "alpha", 1)

    def unreadable_records() -> list[dict[str, object]]:
        raise OSError("ledger unavailable")

    monkeypatch.setattr(cadence.goalflight_ledger, "read_records", unreadable_records)
    record = cadence.build_pulse_record(root, "alpha-main", now=NOW)
    assert "IDLE:" not in str(record["text"])

    monkeypatch.setattr(cadence.goalflight_ledger, "read_records", lambda: [])
    record = cadence.build_pulse_record(root, "alpha-main", now=NOW)
    assert "IDLE:" in str(record["text"])

    monkeypatch.setattr(cadence.goalflight_ledger, "read_records", lambda: [{
        "dispatch_id": "unreadable-row",
        "state": "unreadable",
        "path": str(tmp_path / "ledger.json"),
    }])
    record = cadence.build_pulse_record(root, "alpha-main", now=NOW)
    assert "IDLE:" not in str(record["text"])
    assert "CAPACITY:" not in str(record["text"])

    monkeypatch.setattr(cadence.goalflight_ledger, "read_records", lambda: [])
    monkeypatch.setattr(
        task.TaskStore,
        "load_items",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(task.TaskError("items unavailable")),
    )
    record = cadence.build_pulse_record(root, "alpha-main", now=NOW)
    assert "IDLE:" not in str(record["text"])


def test_pulse_text_keeps_idle_ahead_of_next_after_wakes_and_due() -> None:
    lines = cadence._pulse_text(
        wake_lines=["WAKE-OWNER: wake"],
        short_wake_lines=["WAKE-OWNER: wake"],
        due_text="DUE: due",
        lane_line="LANE lane",
        capacity_line="CAPACITY: 0 / ?",
        idle_line="IDLE: 1 ready, 0 workers; dispatch or record why not",
        next_line="NEXT: task",
    ).splitlines()
    assert lines == [
        "WAKE-OWNER: wake",
        "DUE: due",
        "LANE lane",
        "CAPACITY: 0 / ?",
        "IDLE: 1 ready, 0 workers; dispatch or record why not",
    ]


def test_pulse_does_not_write_or_run_subprocesses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, store = _project(tmp_path)
    _registry(store, ["alpha-main"])
    monkeypatch.setattr(task.TaskStore, "mutate_controller_cadence", lambda *_a, **_k: pytest.fail("pulse wrote cadence"))
    monkeypatch.setattr(subprocess, "run", lambda *_a, **_k: pytest.fail("pulse ran subprocess"))
    monkeypatch.setattr(subprocess, "Popen", lambda *_a, **_k: pytest.fail("pulse spawned subprocess"))
    monkeypatch.setattr(subprocess, "check_output", lambda *_a, **_k: pytest.fail("pulse ran subprocess"))
    monkeypatch.setattr(subprocess, "call", lambda *_a, **_k: pytest.fail("pulse ran subprocess"))

    record = cadence.build_pulse_record(
        root, "alpha-main", now=NOW,
    )
    assert record["kind"] == "pulse"


def test_pulse_never_profiles_capacity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, store = _project(tmp_path)
    _registry(store, ["alpha-main"])
    monkeypatch.setattr(
        goalflight_capacity,
        "_launch_slot_budget",
        lambda **_kwargs: pytest.fail("pulse profiled capacity"),
    )
    record = cadence.build_pulse_record(root, "alpha-main", now=NOW)
    assert record["kind"] == "pulse"
    assert "CAPACITY:" not in record["text"]


def test_pulse_keeps_due_and_lane_when_frontier_misses_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, store = _project(tmp_path)
    _registry(store, ["alpha-main"])
    monkeypatch.setattr(
        cadence,
        "_due_for_label",
        lambda *_args, **_kwargs: ([{"id": "ci-audit"}], []),
    )

    deadline = __import__("time").monotonic() + 0.15

    def slow_records() -> list[dict[str, object]]:
        import time
        time.sleep(0.5)
        return []

    monkeypatch.setattr(cadence.goalflight_ledger, "read_records", slow_records)
    started = __import__("time").monotonic()
    record = cadence.build_pulse_record(root, "alpha-main", now=NOW, deadline=deadline)
    elapsed = __import__("time").monotonic() - started

    assert elapsed < 0.3
    assert record["kind"] == "pulse"
    assert "DUE: ci-audit" in str(record["text"])
    assert "LANE alpha:" in str(record["text"])
    assert "unavailable" not in record


def test_pulse_keeps_cadence_fields_when_task_jsonl_is_malformed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, store = _project(tmp_path)
    _registry(store, ["alpha-main", "beta"], owner="alpha-main")
    _ready_tasks(store, "beta")
    store.mutate_controller_cadence(lambda state: state.update({"last_done": {
        "dedup-scan": {"project": (NOW - timedelta(hours=49)).isoformat()},
        "task-store-audit": {"project": (NOW - timedelta(days=15)).isoformat()},
    }}))
    store.tasks_path.write_text("{malformed jsonl\n", encoding="utf-8")
    monkeypatch.setattr(cadence.goalflight_ledger, "read_records", lambda: [])

    record = cadence.build_pulse_record(root, "beta", now=NOW)

    assert record["kind"] == "pulse"
    assert "unavailable" not in record
    text = str(record["text"])
    assert "LANE beta:" in text
    assert "DUE:" in text
    assert sum(line.startswith("WAKE-OWNER:") for line in text.splitlines()) == 2
    assert "NEXT:" not in text


def test_pulse_probe_does_not_hold_task_store_writer_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, store = _project(tmp_path)
    _registry(store, ["alpha-main"])
    entered = threading.Event()
    release = threading.Event()

    def stalled_probe() -> list[dict[str, object]]:
        entered.set()
        assert release.wait(timeout=2)
        return []

    monkeypatch.setattr(cadence.goalflight_ledger, "read_records", stalled_probe)
    result: list[dict[str, object]] = []
    pulse = threading.Thread(
        target=lambda: result.append(cadence.build_pulse_record(root, "alpha-main", now=NOW)),
        daemon=True,
    )
    pulse.start()
    assert entered.wait(timeout=1)
    # A blocked ledger read cannot retain the task-store writer lock.
    writer_acquired = threading.Event()

    def write_state() -> None:
        store.mutate_controller_cadence(lambda state: state.update({"probe": "writer-acquired"}))
        writer_acquired.set()

    writer = threading.Thread(target=write_state, daemon=True)
    writer.start()
    try:
        assert writer_acquired.wait(timeout=0.25)
    finally:
        release.set()
    writer.join(timeout=1)
    pulse.join(timeout=1)

    assert not pulse.is_alive()
    assert not writer.is_alive()
    assert result and result[0]["kind"] == "pulse"
    assert store.load_controller_cadence()["probe"] == "writer-acquired"


def test_pending_wakes_survive_pulse_byte_budget_with_long_charter_and_next(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root, store = _project(tmp_path)
    _registry(store, ["alpha-main", "beta"], owner="alpha-main")
    _ready_tasks(store, "beta")
    store.mutate_controller_cadence(lambda state: (
        state["lanes"]["beta"].update({"charter": ["Long charter " * 12]}),
        state.update({"last_done": {
            "dedup-scan": {"project": (NOW - timedelta(hours=49)).isoformat()},
            "task-store-audit": {"project": (NOW - timedelta(days=15)).isoformat()},
        }}),
    ))
    monkeypatch.setattr(cadence.goalflight_ledger, "read_records", lambda: [])

    record = cadence.build_pulse_record(root, "beta", now=NOW)
    lines = str(record["text"]).splitlines()

    assert sum(line.startswith("WAKE-OWNER:") for line in lines) == 2
    assert all(item_id in "\n".join(lines) for item_id in ("dedup-scan", "task-store-audit"))
    assert all(
        "goalflight_cadence.py due --controller-label beta" in line
        for line in lines if line.startswith("WAKE-OWNER:")
    )
    assert len(lines) <= 5
    assert len(str(record["text"]).encode("utf-8")) <= cadence.PULSE_TEXT_MAX_BYTES

    pointer = next(line for line in lines if line.startswith("WAKE-OWNER:"))
    assert "goalflight_cadence.py due --controller-label beta" in pointer
    assert cadence.main([
        "--project-root", str(root), "due", "--controller-label", "beta",
    ]) == 0
    due_output = capsys.readouterr().out
    for item_id in ("dedup-scan", "task-store-audit"):
        assert cadence._wake_owner_line(
            root,
            "beta",
            next(duty for duty in cadence._due_for_label(
                store, "beta", NOW, state=store.load_controller_cadence()
            )[1] if duty["id"] == item_id),
        ) in due_output

    overdue = cadence._due_for_label(
        store, "beta", NOW, state=store.load_controller_cadence()
    )[1]
    for duty in overdue:
        wake_item = f"wake:{duty['id']}:{cadence._wake_period(duty)}"
        assert cadence.main([
            "--project-root", str(root), "done", wake_item, "--controller-label", "beta",
        ]) == 0
    capsys.readouterr()
    after_done = cadence.build_pulse_record(root, "beta", now=NOW)
    for item_id in ("dedup-scan", "task-store-audit"):
        assert item_id not in str(after_done["text"])
    assert cadence.main([
        "--project-root", str(root), "due", "--controller-label", "beta",
    ]) == 0
    due_after_done = capsys.readouterr().out
    for item_id in ("dedup-scan", "task-store-audit"):
        assert item_id not in due_after_done


def test_pulse_uses_longest_held_live_project_owner_without_main_lane(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, store = _project(tmp_path)
    _registry(store, ["older", "newer"])
    store.mutate_controller_cadence(lambda state: state.update({"last_done": {
        "dedup-scan": {"project": (NOW - timedelta(hours=49)).isoformat()},
    }}))
    reader = SimpleNamespace(lease_records=lambda: [
        {"label": "older", "nonce": "o1", "claimed_at": (NOW - timedelta(days=5)).isoformat()},
        {"label": "newer", "nonce": "n1", "claimed_at": (NOW - timedelta(days=1)).isoformat()},
    ])
    monkeypatch.setattr(
        sessions.goalflight_journal.Journal,
        "_open_reader_for_resolved_root",
        lambda *_args, **_kwargs: reader,
        raising=False,
    )
    monkeypatch.setattr(
        sessions.goalflight_wake,
        "lease_holder_alive",
        lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr(
        sessions, "probe_live_session", lambda *_args, **_kwargs: pytest.fail("pulse resolved root via session probe")
    )
    monkeypatch.setattr(subprocess, "run", lambda *_args, **_kwargs: pytest.fail("pulse ran subprocess"))
    monkeypatch.setattr(subprocess, "Popen", lambda *_args, **_kwargs: pytest.fail("pulse spawned subprocess"))
    monkeypatch.setattr(subprocess, "check_output", lambda *_args, **_kwargs: pytest.fail("pulse ran subprocess"))
    monkeypatch.setattr(cadence.goalflight_ledger, "read_records", lambda: [])

    record = cadence.build_pulse_record(root, "newer", now=NOW)

    assert "WAKE-OWNER:" in str(record["text"])
    assert "--to-controller older" in str(record["text"])


def test_watchdog_live_branch_uses_cadence_due_command() -> None:
    prompt = (ROOT / "templates" / "goalflight-watchdog-prompt.md").read_text()
    assert 'goalflight_cadence.py --project-root "$PWD" due' in prompt
    assert "goalflight_cadence.py claim" not in prompt
