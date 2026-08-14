"""Status mail hints read journal authority and never own a cursor."""

from __future__ import annotations

from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(SCRIPTS))

import goalflight_journal as journal  # noqa: E402
import goalflight_messages as messages  # noqa: E402
import goalflight_status as status  # noqa: E402


def _setup(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    for key, value in {
        "GOALFLIGHT_TASK_STORE_DIR": tmp_path / "task-store",
        "GOALFLIGHT_JOURNAL_DIR": tmp_path / "journal",
        "GOALFLIGHT_MESSAGES_DIR": tmp_path / "messages",
    }.items():
        monkeypatch.setenv(key, str(value))
    monkeypatch.setenv("GOALFLIGHT_CONTROLLER_LABEL", "status")
    root = tmp_path / "project"
    root.mkdir()
    authority = journal.open_or_create_journal(root)
    lease = authority.claim_or_renew_lease(
        "status", principal={"principal_id": "status-principal"}
    ).value
    assert lease is not None
    messages.post_message(
        dispatch_id="status-stream",
        msg_type="controller-notice",
        payload={"text": "status mail"},
        messages_dir=tmp_path / "messages",
        source={"node": "test-node", "adapter": "test", "transport": "controller"},
        addressee=messages.controller_addressee("status", project_root=root),
    )
    return root, authority, lease


def test_status_summary_uses_journal_pending_rows(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root, _authority, _lease = _setup(monkeypatch, tmp_path)
    summary = status._mail_summary(set(), project_root=root)
    assert summary["count"] == 1
    assert summary["needs"][0]["dispatch_id"] == "status-stream"
    assert summary["hint"] == "1 new mail; peek: goalflight_messages.py relay --new"


def test_wait_watermark_survives_cursor_advancement(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root, authority, lease = _setup(monkeypatch, tmp_path)
    before = status._mail_watermark(str(root), ["status-stream"] )
    assert before is not None and len(before) == 1
    batch = authority.cursor_batch("status", nonce=lease.nonce, limit=10)
    assert authority.advance_cursor(batch.token, actor="status").committed
    assert status._mail_watermark(str(root), ["status-stream"] ) == before
