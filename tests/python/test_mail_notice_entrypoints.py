"""Journal-backed controller mail notice contracts."""

from __future__ import annotations

import io
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(SCRIPTS))

import goalflight_journal as journal  # noqa: E402
import goalflight_messages as messages  # noqa: E402


def _mail(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> tuple[Path, journal.Journal]:
    for key, value in {
        "GOALFLIGHT_TASK_STORE_DIR": tmp_path / "task-store",
        "GOALFLIGHT_JOURNAL_DIR": tmp_path / "journal",
        "GOALFLIGHT_MESSAGES_DIR": tmp_path / "messages",
    }.items():
        monkeypatch.setenv(key, str(value))
    monkeypatch.setenv("GOALFLIGHT_CONTROLLER_LABEL", "notice")
    root = tmp_path / "project"
    root.mkdir()
    authority = journal.open_or_create_journal(root)
    assert authority.claim_or_renew_lease(
        "notice", principal={"principal_id": "notice-principal"}
    ).committed
    return root, authority


def test_notice_is_body_free_sanitized_and_journal_derived(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root, _authority = _mail(monkeypatch, tmp_path)
    messages.post_message(
        dispatch_id="notice-stream",
        msg_type="controller-notice",
        payload={"text": "secret body\nwith control \x1b[31m"},
        messages_dir=tmp_path / "messages",
        source={"node": "test-node", "adapter": "test", "transport": "controller"},
        addressee=messages.controller_addressee("notice", project_root=root),
    )
    summary = messages.controller_mail_summary(task_store_project_root=root)
    assert summary["count"] == 1
    assert "\n" not in str(summary["needs"][0]["text"])
    stream = io.StringIO()
    notice = messages.emit_controller_mail_notice(
        project_root=root, owned_dispatch_ids=set(), stream=stream
    )
    assert notice == "1 new mail; peek: goalflight_messages.py relay --new"
    assert "secret body" not in stream.getvalue()


def test_corrupt_assigned_carrier_surfaces_warning_without_advancing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root, authority = _mail(monkeypatch, tmp_path)
    posted = messages.post_message(
        dispatch_id="corrupt-stream",
        msg_type="controller-notice",
        payload={"text": "body"},
        messages_dir=tmp_path / "messages",
        source={"node": "test-node", "adapter": "test", "transport": "controller"},
        addressee=messages.controller_addressee("notice", project_root=root),
    )
    with Path(posted["path"]).open("ab") as handle:
        handle.write(b"{broken\n")
    before = authority.cursor_status("notice")
    summary = messages.controller_mail_summary(task_store_project_root=root)
    assert summary["count"] == 0
    assert len(summary["carrier_errors"]) == 1
    assert authority.cursor_status("notice") == before
