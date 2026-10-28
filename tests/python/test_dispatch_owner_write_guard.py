"""Persisted controller labels must have the controller-label wire shape."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import goalflight_dispatch as dispatch  # noqa: E402
import goalflight_journal as journal  # noqa: E402
import goalflight_ledger as ledger  # noqa: E402
import goalflight_liveness as liveness  # noqa: E402
import goalflight_session_status as session_status  # noqa: E402


BAD_LABELS = [
    pytest.param({"a": 1}, "dict", id="dict"),
    pytest.param(["a"], "list", id="list"),
    pytest.param(7, "int", id="int"),
    pytest.param("{'a': 1}", "str", id="stringified-dict"),
]

LIVE_LABELS = [
    "battery-bugs",
    "battery-engine",
    "battery-main",
    "battery-perf",
    "battery-webui",
    "goal-flight",
    "kiln",
    "ny-map-r2",
    "ontario-tools",
    "project-b-species",
    "papers-main",
    "papers-propulsion",
    "project-a",
    "pm2-bugs",
    "pm2-engine",
    "pm2-main",
    "pon3414-plan",
    "example-sim-bugs",
    "example-sim-engine",
    "example-sim-main",
    "example-sim-physics",
    "example-sim",
    "project-c",
    "warpx-gpu",
]


@pytest.fixture(autouse=True)
def isolate_state(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    state = tmp_path / "state"
    monkeypatch.setenv("GOALFLIGHT_STATE_DIR", str(state))
    monkeypatch.setenv("GOALFLIGHT_DISPATCH_DIR", str(state / "dispatch"))
    monkeypatch.setenv("GOALFLIGHT_JOURNAL_DIR", str(tmp_path / "journals"))
    monkeypatch.setenv("GOALFLIGHT_MESSAGES_DIR", str(tmp_path / "messages"))
    monkeypatch.setenv("GOALFLIGHT_TASK_STORE_DIR", str(tmp_path / "task-store"))
    monkeypatch.setenv("GOALFLIGHT_WAKE_LEDGER", str(tmp_path / "wake-ledger.jsonl"))
    monkeypatch.setenv("GOAL_FLIGHT_PIDFILE_DIR", str(tmp_path / "pidfiles"))
    monkeypatch.setenv("GOALFLIGHT_CAPACITY_CONF", "/dev/null")
    monkeypatch.setenv("GOALFLIGHT_DISABLE_NUDGES", "1")


def _assert_typed_refusal(error: BaseException, *, field: str, value_type: str, raw: object) -> None:
    message = str(error)
    assert field in message
    assert value_type in message
    if isinstance(raw, str):
        assert raw not in message
    else:
        assert repr(raw) not in message


@pytest.mark.parametrize(("label", "value_type"), BAD_LABELS)
def test_ledger_record_write_refuses_malformed_controller_label(
    label: object,
    value_type: str,
) -> None:
    dispatch_id = f"bad-ledger-owner-{value_type}"
    record = {"dispatch_id": dispatch_id, "controller_label": label}

    with pytest.raises(journal.ControllerLabelValidationError) as exc_info:
        ledger.write_record(record)

    _assert_typed_refusal(
        exc_info.value,
        field="controller_label",
        value_type=value_type,
        raw=label,
    )
    assert not ledger.record_path(dispatch_id, create=False).exists()


@pytest.mark.parametrize(("label", "value_type"), BAD_LABELS)
def test_journal_attempt_write_refuses_malformed_owner_label(
    tmp_path: Path,
    label: object,
    value_type: str,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    authority = journal.open_or_create_journal(project)

    with pytest.raises(journal.ControllerLabelValidationError) as exc_info:
        authority.prepare_attempt(
            f"bad-journal-owner-{value_type}",
            owner_controller_label=label,
        )

    _assert_typed_refusal(
        exc_info.value,
        field="owner_controller_label",
        value_type=value_type,
        raw=label,
    )
    assert authority.read_all(
        "SELECT dispatch_id FROM dispatch_attempts WHERE dispatch_id = ?",
        (f"bad-journal-owner-{value_type}",),
    ) == []


@pytest.mark.parametrize(("label", "value_type"), BAD_LABELS)
def test_record_command_refuses_malformed_label_before_stringifying(
    tmp_path: Path,
    label: object,
    value_type: str,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    dispatch_id = f"bad-command-owner-{value_type}"
    args = argparse.Namespace(
        dispatch_id=dispatch_id,
        worker_pid=None,
        request_envelope_json=None,
        parent_dispatch_id=None,
        project_root=str(project),
        controller_pid=None,
        controller_session_id=None,
        controller_label=label,
        claimant_pid=None,
        os_sandbox_json=None,
        agent="codex",
        shape=None,
        account=None,
        effective_account=None,
        transport="dispatch",
        prompt_id=None,
        prompt_path=None,
        acp_session_id=None,
        logical_session_id=None,
        lease_id=None,
        stdout_path=None,
        stderr_path=None,
        status_path=None,
        state="queued",
        json=True,
    )

    with pytest.raises(journal.ControllerLabelValidationError) as exc_info:
        ledger.cmd_record(args)

    _assert_typed_refusal(
        exc_info.value,
        field="controller_label",
        value_type=value_type,
        raw=label,
    )
    assert not ledger.record_path(dispatch_id, create=False).exists()


@pytest.mark.parametrize(("label", "value_type"), BAD_LABELS)
def test_queue_recovery_refuses_coerced_malformed_owner_before_restore(
    tmp_path: Path,
    label: object,
    value_type: str,
) -> None:
    queue = tmp_path / "state" / "dispatch-queue"
    queue.mkdir(parents=True)
    dispatch_id = f"bad-restore-owner-{value_type}"
    entry = {
        "schema": dispatch.DISPATCH_QUEUE_SCHEMA,
        "dispatch_id": dispatch_id,
        "project_root": str(tmp_path),
        "dispatch_argv": ["--agent", "codex"],
        "controller_label": label,
        "queue_launch_token": f"token-{value_type}",
        "created_at": "2000-01-01T00:00:00+00:00",
        "request": {"cwd": str(tmp_path)},
    }
    claim = queue / f"{dispatch_id}.json.claimed-1"
    claim.write_text(json.dumps(entry), encoding="utf-8")

    with pytest.raises(journal.ControllerLabelValidationError) as exc_info:
        dispatch._bounded_restore_claim(claim, entry, queue)

    _assert_typed_refusal(
        exc_info.value,
        field="controller_label",
        value_type=value_type,
        raw=label,
    )
    assert not (queue / f"{dispatch_id}.json").exists()
    assert not ledger.record_path(dispatch_id, create=False).exists()


def test_all_live_controller_labels_round_trip_through_ledger_and_journal(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    subprocess.run(["git", "init", "--quiet", str(project)], check=True)
    monkeypatch.setenv(
        session_status.goalflight_task.ALLOW_VOLATILE_PROJECT_ROOT_ENV,
        str(tmp_path),
    )
    authority = journal.open_or_create_journal(project)

    assert len(LIVE_LABELS) == 24
    for index, label in enumerate(LIVE_LABELS):
        registration = session_status.register_controller(project, label)
        assert registration["registered"] is True
        dispatch_id = f"valid-owner-{index:02d}"
        ledger.write_record(
            {"dispatch_id": dispatch_id, "controller_label": label}
        )
        assert ledger.read_record(dispatch_id)["controller_label"] == label
        result = authority.prepare_attempt(
            dispatch_id,
            owner_controller_label=label,
        )
        assert result.committed
        assert authority.read_all(
            "SELECT owner_controller_label FROM dispatch_attempts WHERE dispatch_id = ?",
            (dispatch_id,),
        )[0]["owner_controller_label"] == label


@pytest.mark.parametrize("label", ["My Project", "{'a': 1}"])
def test_registration_refuses_labels_outside_persistence_rule(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    label: str,
) -> None:
    project = tmp_path / "valid-project"
    project.mkdir()
    subprocess.run(["git", "init", "--quiet", str(project)], check=True)
    monkeypatch.setenv(
        session_status.goalflight_task.ALLOW_VOLATILE_PROJECT_ROOT_ENV,
        str(tmp_path),
    )

    result = session_status.register_controller(project, label)

    assert result["registered"] is False
    assert result["reason"] == "invalid_controller_label"
    assert "controller label" in result["message"]
    assert "1-64" in result["message"]
    assert "starting with a letter or digit" in result["message"]


def test_invalid_project_directory_default_is_refused(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    project = tmp_path / "My Project"
    project.mkdir()
    subprocess.run(["git", "init", "--quiet", str(project)], check=True)
    monkeypatch.setenv(
        session_status.goalflight_task.ALLOW_VOLATILE_PROJECT_ROOT_ENV,
        str(tmp_path),
    )

    result = session_status.claim_controller_startup(project, environ={})

    assert result["claimed"] is False
    assert result["reason"] == "invalid_controller_label"
    assert "1-64" in result["message"]
    assert "starting with a letter or digit" in result["message"]
    assert "'goal-flight'" in result["message"]


@pytest.mark.parametrize(
    "payload",
    [
        {"dispatch_id": "owner-none", "controller_label": None},
        {"dispatch_id": "owner-missing"},
    ],
)
def test_absent_controller_owner_still_persists(payload: dict) -> None:
    path = ledger.write_record(payload)
    assert path.exists()


@pytest.mark.parametrize("label", ["", "   "])
def test_blank_controller_owner_persists_as_absent(label: str) -> None:
    path = ledger.write_record(
        {"dispatch_id": f"blank-owner-{len(label)}", "controller_label": label}
    )
    assert json.loads(path.read_text(encoding="utf-8"))["controller_label"] is None


@pytest.mark.parametrize("label", ["", "   "])
def test_status_writer_persists_blank_controller_owner_as_absent(
    tmp_path: Path,
    label: str,
) -> None:
    status_path = tmp_path / f"blank-{len(label)}.json"
    payload = {"state": "running", "controller_label": label}
    liveness.write_status(status_path, payload)
    assert json.loads(status_path.read_text(encoding="utf-8"))["controller_label"] is None


def test_status_writer_refuses_stringified_malformed_controller_label(
    tmp_path: Path,
) -> None:
    status_path = tmp_path / "dispatch.status.json"
    payload = {"controller_label": "{'a': 1}"}

    with pytest.raises(journal.ControllerLabelValidationError):
        liveness.write_status(status_path, payload)

    assert not status_path.exists()


def test_status_writer_accepts_absent_controller_label(tmp_path: Path) -> None:
    status_path = tmp_path / "dispatch.status.json"
    liveness.write_status(status_path, {"state": "running", "controller_label": None})
    assert status_path.exists()


def test_retry_child_queue_writer_refuses_stringified_malformed_controller_label(
    tmp_path: Path,
) -> None:
    queue_path = tmp_path / "retry-child.json"

    with pytest.raises(journal.ControllerLabelValidationError):
        dispatch._write_json_exclusive(
            queue_path,
            {"controller_label": "{'a': 1}"},
        )

    assert not queue_path.exists()
