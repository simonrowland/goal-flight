"""Journal-backed controller mail notice contracts."""

from __future__ import annotations

from dataclasses import replace
import io
from pathlib import Path
import shlex
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(SCRIPTS))

import goalflight_journal as journal  # noqa: E402
import goalflight_messages as messages  # noqa: E402
import goalflight_wake as wake  # noqa: E402


def _mail(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> tuple[Path, journal.Journal]:
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
        "notice",
        principal={"principal_id": "notice-principal"},
    ).committed
    return root, authority


def _monitor_command(root: Path, label: str, nonce: str) -> str:
    return shlex.join(
        [
            sys.executable,
            str(SCRIPTS / "goalflight_messages.py"),
            "follow",
            "--project-root",
            str(root),
            "--controller-label",
            label,
            "--lease-nonce",
            nonce,
        ]
    )


def _stub_live_monitor(
    monkeypatch: pytest.MonkeyPatch,
    root: Path,
    *,
    label: str,
    nonce: str,
    command: str | None = None,
    legacy: bool = False,
) -> None:
    pid = 4242
    record = wake.WaiterRecord(
        kind=wake.MONITOR_KIND,
        label_hash=wake._label_hash(label),
        pid=pid,
        start_hash="a" * 16,
        instance_id="b" * 32,
        path=root / "monitor.lock",
        generation_hash=None if legacy else wake._waiter_generation_hash(nonce),
    )

    def live_waiters(
        project_root: Path | str,
        *,
        controller_label: str | None = None,
        kinds: set[str] | None = None,
        prune_dead: bool = True,
        **_kwargs: object,
    ) -> list[wake.WaiterRecord]:
        assert Path(project_root) == root
        assert kinds == {wake.MONITOR_KIND}
        assert prune_dead is False
        return [record] if controller_label in {None, label} else []

    monkeypatch.setattr(wake, "live_waiters", live_waiters)

    def process_listing(
        *, timeout_s: float = 2.0, pids: tuple[int, ...] | None = None
    ) -> list[tuple[int, str]]:
        assert timeout_s == 0.2
        assert pids == (pid,)
        return [(pid, command or _monitor_command(root, label, nonce))]

    monkeypatch.setattr(wake, "_process_listing", process_listing)


def _stub_live_monitor_pair(
    monkeypatch: pytest.MonkeyPatch,
    root: Path,
    *,
    first_nonce: str,
    second_nonce: str,
) -> None:
    records = [
        wake.WaiterRecord(
            kind=wake.MONITOR_KIND,
            label_hash=wake._label_hash("notice"),
            pid=pid,
            start_hash="a" * 16,
            instance_id=str(pid) * 16,
            path=root / f"monitor-{pid}.lock",
            generation_hash=wake._waiter_generation_hash(nonce),
        )
        for pid, nonce in ((4242, first_nonce), (4243, second_nonce))
    ]

    def live_waiters(*_args: object, **kwargs: object) -> list[wake.WaiterRecord]:
        assert kwargs["controller_label"] == "notice"
        assert kwargs["kinds"] == {wake.MONITOR_KIND}
        return records

    def process_listing(**kwargs: object) -> list[tuple[int, str]]:
        assert kwargs["pids"] == (4242, 4243)
        return [
            (4242, _monitor_command(root, "notice", first_nonce)),
            (
                4243,
                _monitor_command(root, "notice", second_nonce) + " 'unterminated",
            ),
        ]

    monkeypatch.setattr(wake, "live_waiters", live_waiters)
    monkeypatch.setattr(wake, "_process_listing", process_listing)


def test_process_listing_limits_ps_to_requested_pids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: dict[str, object] = {}

    def check_output(command: list[str], **kwargs: object) -> str:
        observed["command"] = command
        observed.update(kwargs)
        return "17 python one.py\n23 python two.py\n"

    monkeypatch.setattr(wake.subprocess, "check_output", check_output)

    listing = wake._process_listing(timeout_s=0.2, pids=(23, 17, 23))

    assert observed["command"] == [
        "ps",
        "-ww",
        "-p",
        "17,23",
        "-o",
        "pid=,command=",
    ]
    assert observed["timeout"] == 0.2
    assert listing == [(17, "python one.py"), (23, "python two.py")]


def _summary_and_notice(root: Path) -> tuple[dict, str]:
    summary = messages.controller_mail_summary(task_store_project_root=root)
    stream = io.StringIO()
    messages.emit_controller_mail_notice(
        project_root=root,
        owned_dispatch_ids=set(),
        stream=stream,
    )
    return summary, stream.getvalue()


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


def test_live_monitor_with_stale_nonce_warns_orphaned(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root, authority = _mail(monkeypatch, tmp_path)
    active = authority.active_lease("notice")
    assert active is not None
    _stub_live_monitor(
        monkeypatch,
        root,
        label="notice",
        nonce=f"retired-{active.nonce}",
    )

    summary, output = _summary_and_notice(root)

    assert summary["monitor_lease"]["state"] == "orphaned"
    assert "ORPHANED" in output
    assert "active lease nonce" in output


def test_legacy_live_monitor_with_stale_nonce_warns_orphaned(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root, authority = _mail(monkeypatch, tmp_path)
    active = authority.active_lease("notice")
    assert active is not None
    _stub_live_monitor(
        monkeypatch,
        root,
        label="notice",
        nonce=f"retired-{active.nonce}",
        legacy=True,
    )

    summary, output = _summary_and_notice(root)

    assert summary["monitor_lease"]["state"] == "orphaned"
    assert "ORPHANED" in output


def test_live_monitor_with_current_nonce_is_silent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root, authority = _mail(monkeypatch, tmp_path)
    active = authority.active_lease("notice")
    assert active is not None
    _stub_live_monitor(monkeypatch, root, label="notice", nonce=active.nonce)

    summary, output = _summary_and_notice(root)

    assert summary["monitor_lease"]["state"] == "current"
    assert output == ""


def test_monitor_journal_probe_uses_short_budgets(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root, authority = _mail(monkeypatch, tmp_path)
    active = authority.active_lease("notice")
    assert active is not None
    _stub_live_monitor(monkeypatch, root, label="notice", nonce=active.nonce)
    real_open_reader = journal.Journal.open_reader.__func__
    calls: list[dict[str, object]] = []

    def open_reader(
        cls: type[journal.Journal],
        project_root: Path | str,
        **kwargs: object,
    ) -> journal.Journal:
        calls.append(kwargs)
        return real_open_reader(cls, project_root, **kwargs)

    monkeypatch.setattr(journal.Journal, "open_reader", classmethod(open_reader))

    summary, output = _summary_and_notice(root)

    assert summary["monitor_lease"]["state"] == "current"
    assert output == ""
    assert calls
    for kwargs in calls:
        assert kwargs["retry_budget_s"] == 0.05
        assert kwargs["open_retry_budget_s"] == 0.05
        assert kwargs["transaction_budget_s"] == 0.05


def test_live_monitor_with_overdue_current_lease_warns_orphaned(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root, authority = _mail(monkeypatch, tmp_path)
    active = authority.active_lease("notice")
    assert active is not None
    expired = replace(active, renew_deadline_at="2000-01-01T00:00:00+00:00")
    monkeypatch.setattr(journal.Journal, "active_lease", lambda _self, _label: expired)
    _stub_live_monitor(monkeypatch, root, label="notice", nonce=active.nonce)

    summary, output = _summary_and_notice(root)

    assert summary["monitor_lease"]["state"] == "orphaned"
    assert summary["monitor_lease"]["reason"] == "renew-deadline-past"
    assert "ORPHANED" in output
    assert "--join" in output


def test_stale_monitor_warns_when_renew_deadline_is_unreadable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root, authority = _mail(monkeypatch, tmp_path)
    active = authority.active_lease("notice")
    assert active is not None
    invalid = replace(active, renew_deadline_at="not-a-deadline")
    monkeypatch.setattr(journal.Journal, "active_lease", lambda _self, _label: invalid)
    _stub_live_monitor(
        monkeypatch,
        root,
        label="notice",
        nonce=f"retired-{active.nonce}",
    )

    summary, output = _summary_and_notice(root)

    assert summary["monitor_lease"]["state"] == "orphaned"
    assert summary["monitor_lease"]["reason"] == "lease-nonce-mismatch"
    assert "ORPHANED" in output


def test_current_monitor_with_unreadable_renew_deadline_is_unknown(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root, authority = _mail(monkeypatch, tmp_path)
    active = authority.active_lease("notice")
    assert active is not None
    invalid = replace(active, renew_deadline_at="not-a-deadline")
    monkeypatch.setattr(journal.Journal, "active_lease", lambda _self, _label: invalid)
    _stub_live_monitor(monkeypatch, root, label="notice", nonce=active.nonce)

    summary, output = _summary_and_notice(root)

    assert summary["monitor_lease"]["state"] == "unknown"
    assert summary["monitor_lease"]["reason"] == "renew-deadline-unreadable"
    assert "UNKNOWN" in output
    assert "ORPHANED" not in output


def test_live_monitor_with_unreadable_journal_reports_unknown(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root, authority = _mail(monkeypatch, tmp_path)
    active = authority.active_lease("notice")
    assert active is not None
    _stub_live_monitor(monkeypatch, root, label="notice", nonce=active.nonce)

    def unreadable(*_args: object, **_kwargs: object) -> journal.Journal:
        raise journal.JournalBusy("held by test writer")

    monkeypatch.setattr(journal.Journal, "open_reader", unreadable)
    summary, output = _summary_and_notice(root)

    assert summary["monitor_lease"]["state"] == "unknown"
    assert summary["monitor_lease"]["reason"] == "JournalBusy"
    assert "UNKNOWN" in output
    assert "ORPHANED" not in output


def test_live_monitor_with_invalid_journal_reports_unknown(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root, authority = _mail(monkeypatch, tmp_path)
    active = authority.active_lease("notice")
    assert active is not None
    _stub_live_monitor(monkeypatch, root, label="notice", nonce=active.nonce)

    def invalid(*_args: object, **_kwargs: object) -> journal.Journal:
        raise journal.JournalIntegrityError("invalid test journal")

    monkeypatch.setattr(journal.Journal, "open_reader", invalid)
    summary, output = _summary_and_notice(root)

    assert summary["monitor_lease"]["state"] == "unknown"
    assert summary["monitor_lease"]["reason"] == "JournalIntegrityError"
    assert "UNKNOWN" in output
    assert "ORPHANED" not in output


def test_unreadable_waiter_ledger_reports_status_unknown(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root, _authority = _mail(monkeypatch, tmp_path)

    def unreadable(*_args: object, **_kwargs: object) -> list[wake.WaiterRecord]:
        raise OSError("unreadable test waiter ledger")

    monkeypatch.setattr(wake, "live_waiters", unreadable)
    summary, output = _summary_and_notice(root)

    assert summary["monitor_lease"]["state"] == "unknown"
    assert summary["monitor_lease"]["reason"] == "monitor-waiter-probe-unavailable"
    assert "monitor status UNKNOWN" in output
    assert "live wake monitor" not in output
    assert "ORPHANED" not in output


def test_live_monitor_with_unparseable_argv_reports_unknown(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root, authority = _mail(monkeypatch, tmp_path)
    active = authority.active_lease("notice")
    assert active is not None
    command = _monitor_command(root, "notice", active.nonce) + " 'unterminated"
    _stub_live_monitor(
        monkeypatch,
        root,
        label="notice",
        nonce=active.nonce,
        command=command,
    )

    summary, output = _summary_and_notice(root)

    assert summary["monitor_lease"]["state"] == "unknown"
    assert summary["monitor_lease"]["reason"] == "monitor-argv-unparseable"
    assert "UNKNOWN" in output
    assert "ORPHANED" not in output


def test_live_monitor_with_truncated_nonce_reports_unknown(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root, authority = _mail(monkeypatch, tmp_path)
    active = authority.active_lease("notice")
    assert active is not None
    command = _monitor_command(root, "notice", active.nonce[:8])
    _stub_live_monitor(
        monkeypatch,
        root,
        label="notice",
        nonce=active.nonce,
        command=command,
    )

    summary, output = _summary_and_notice(root)

    assert summary["monitor_lease"]["state"] == "unknown"
    assert summary["monitor_lease"]["reason"] == "monitor-argv-nonce-unverifiable"
    assert "UNKNOWN" in output
    assert "ORPHANED" not in output


def test_legacy_live_monitor_with_truncated_nonce_reports_unknown(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root, authority = _mail(monkeypatch, tmp_path)
    active = authority.active_lease("notice")
    assert active is not None
    command = _monitor_command(root, "notice", active.nonce[:4])
    _stub_live_monitor(
        monkeypatch,
        root,
        label="notice",
        nonce=active.nonce,
        command=command,
        legacy=True,
    )

    summary, output = _summary_and_notice(root)

    assert summary["monitor_lease"]["state"] == "unknown"
    assert summary["monitor_lease"]["reason"] == "lease-nonce-prefix-ambiguous"
    assert "UNKNOWN" in output
    assert "ORPHANED" not in output


def test_legacy_live_monitor_with_longer_stale_nonce_warns_orphaned(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root, authority = _mail(monkeypatch, tmp_path)
    active = authority.active_lease("notice")
    assert active is not None
    stale_nonce = f"{active.nonce}-retired"
    _stub_live_monitor(
        monkeypatch,
        root,
        label="notice",
        nonce=stale_nonce,
        legacy=True,
    )

    summary, output = _summary_and_notice(root)

    assert summary["monitor_lease"]["state"] == "orphaned"
    assert summary["monitor_lease"]["reason"] == "lease-nonce-mismatch"
    assert "ORPHANED" in output


def test_stale_monitor_wins_over_second_unparseable_monitor(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root, authority = _mail(monkeypatch, tmp_path)
    active = authority.active_lease("notice")
    assert active is not None
    stale_nonce = f"retired-{active.nonce}"
    _stub_live_monitor_pair(
        monkeypatch,
        root,
        first_nonce=stale_nonce,
        second_nonce=active.nonce,
    )

    summary, output = _summary_and_notice(root)

    assert summary["monitor_lease"]["state"] == "orphaned"
    assert summary["monitor_lease"]["reason"] == "lease-nonce-mismatch"
    assert "ORPHANED" in output


def test_current_monitor_with_second_unparseable_monitor_is_unknown(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root, authority = _mail(monkeypatch, tmp_path)
    active = authority.active_lease("notice")
    assert active is not None
    _stub_live_monitor_pair(
        monkeypatch,
        root,
        first_nonce=active.nonce,
        second_nonce=active.nonce,
    )

    summary, output = _summary_and_notice(root)

    assert summary["monitor_lease"]["state"] == "unknown"
    assert summary["monitor_lease"]["reason"] == "monitor-argv-unparseable"
    assert "UNKNOWN" in output
    assert "ORPHANED" not in output


def test_peer_labels_orphaned_monitor_is_silent_for_caller(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root, _authority = _mail(monkeypatch, tmp_path)
    _stub_live_monitor(
        monkeypatch,
        root,
        label="peer-label",
        nonce="retired-peer-nonce",
    )

    summary, output = _summary_and_notice(root)

    assert summary["controller_label"] == "notice"
    assert summary["monitor_lease"]["state"] == "absent"
    assert output == ""
