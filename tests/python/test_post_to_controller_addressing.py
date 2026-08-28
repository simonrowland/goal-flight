"""CLI ``post --to-controller`` addressing: reachable, hinted, or UNKNOWN.

Built against real journals (b-235). Isolated GOALFLIGHT_* dirs; never posts
to a live controller. Callers detect a miss by exit code and structured
``controller_delivery.status``, not by scraping prose.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(ROOT / "tests" / "python"))

import goalflight_journal as journal  # noqa: E402
import goalflight_task as task  # noqa: E402
from support import isolated_machine_env  # noqa: E402

SCRIPT = SCRIPTS / "goalflight_messages.py"


def _isolate(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict[str, str]:
    env = os.environ.copy()
    env.update(isolated_machine_env(tmp_path))
    env["GOALFLIGHT_TEST_MODE"] = "1"
    env.pop("GOALFLIGHT_DISPATCH_ID", None)
    env.pop("GOALFLIGHT_PROJECT_ROOT", None)
    env.pop("GOALFLIGHT_CONTROLLER_LABEL", None)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    for key in (
        "GOALFLIGHT_DISPATCH_ID",
        "GOALFLIGHT_PROJECT_ROOT",
        "GOALFLIGHT_CONTROLLER_LABEL",
    ):
        monkeypatch.delenv(key, raising=False)
    return env


def _git_project(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    return path


def _canonical(path: Path) -> str:
    return str(task.resolve_project_root(str(path)))


def _claim(project: Path, label: str) -> None:
    authority = journal.open_or_create_journal(project)
    claimed = authority.claim_or_renew_lease(
        label,
        principal={"principal_id": f"{label}-addressing-test"},
    )
    assert claimed.committed, claimed.reason


def _post(
    env: dict[str, str],
    *,
    cwd: Path,
    label: str,
    dispatch_id: str,
    text: str = "addressing probe",
    controller_project_root: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    argv = [
        sys.executable,
        str(SCRIPT),
        "--messages-dir",
        env["GOALFLIGHT_MESSAGES_DIR"],
        "--fleet-dir",
        env["GOALFLIGHT_FLEET_DIR"],
        "post",
        "--dispatch-id",
        dispatch_id,
        "--type",
        "controller-answer",
        "--text",
        text,
        "--to-controller",
        label,
    ]
    if controller_project_root is not None:
        argv.extend(["--controller-project-root", str(controller_project_root)])
    return subprocess.run(
        argv,
        cwd=str(cwd),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def _payload(posted: subprocess.CompletedProcess[str]) -> dict:
    return json.loads(posted.stdout)


def test_cross_project_to_controller_without_root_flag_is_detectable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = _isolate(monkeypatch, tmp_path)
    sender = _git_project(tmp_path / "sender")
    recipient = _git_project(tmp_path / "battery-tool-v2")
    label = "battery-webui"
    _claim(recipient, label)
    recipient_root = _canonical(recipient)
    sender_root = _canonical(sender)

    posted = _post(
        env,
        cwd=sender,
        label=label,
        dispatch_id="cross-project-miss",
        text="queue-audit answer of record",
    )
    result = _payload(posted)
    delivery = result["controller_delivery"]

    assert posted.returncode != 0, posted.stderr
    assert delivery["requested"] is True
    assert delivery["delivered"] is False
    assert delivery["status"] == "controller_addressee_other_project"
    assert delivery["suggested_controller_project_root"] == recipient_root
    assert f"--controller-project-root {recipient_root}" in delivery["detail"]
    assert f"label '{label}' is not registered in {sender_root}" in delivery["detail"]
    assert f"it is registered in {recipient_root}" in delivery["detail"]
    assert result["recorded"] is True


def test_same_project_to_controller_without_root_flag_still_delivers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = _isolate(monkeypatch, tmp_path)
    project = _git_project(tmp_path / "project")
    label = "same-project-controller"
    _claim(project, label)

    posted = _post(
        env,
        cwd=project,
        label=label,
        dispatch_id="same-project-hit",
        text="same project must keep working",
    )
    result = _payload(posted)
    delivery = result["controller_delivery"]

    assert posted.returncode == 0, posted.stderr
    assert delivery["status"] == "delivered_to_controller"
    assert delivery["delivered"] is True
    assert delivery["recipient_label"] == label
    assert delivery["project_root"] == _canonical(project)


def test_unreadable_registry_is_unknown_not_label_not_found(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = _isolate(monkeypatch, tmp_path)
    project = _git_project(tmp_path / "project")
    missing_label = "missing-controller"
    _claim(project, "local-controller")
    journal_dir = journal.resolve_journal_path(project).parent

    missing = _post(
        env,
        cwd=project,
        label=missing_label,
        dispatch_id="readable-miss",
        text="readable registry, no such label",
    )
    missing_result = _payload(missing)
    missing_delivery = missing_result["controller_delivery"]
    assert missing.returncode != 0, missing.stderr
    assert missing_delivery["status"] == "controller_addressee_unresolved"
    assert missing_result["recorded"] is True

    os.chmod(journal_dir, 0o000)
    try:
        unknown = _post(
            env,
            cwd=project,
            label=missing_label,
            dispatch_id="unreadable-registry",
            text="must not look like a miss",
        )
    finally:
        os.chmod(journal_dir, 0o700)

    unknown_result = _payload(unknown)
    unknown_delivery = unknown_result["controller_delivery"]
    assert unknown.returncode == 2, unknown.stderr
    assert unknown_delivery["status"] == "controller_registry_unknown"
    assert unknown_delivery["delivered"] is False
    assert unknown_result["recorded"] is False
    assert unknown_delivery["status"] != missing_delivery["status"]
    assert "not registered" not in unknown_delivery["detail"]
    assert "not found" not in unknown_delivery["detail"].lower()
    assert "UNKNOWN" in unknown_delivery["detail"]
    inbox = Path(env["GOALFLIGHT_MESSAGES_DIR"]) / "unreadable-registry.jsonl"
    assert not inbox.exists()
