"""Advancing the cursor should not require transcribing what a peek already knows.

Clearing nine read items previously meant supplying three CAS inputs by hand —
`--cursor-version`, a `--stream-snapshot STREAM=TOKEN` per stream, and a
`--position STREAM=SEQ` per stream. All three come from the same peek, and the
tokens are opaque hashes printed only by the doorbell, so a controller that had
already read its mail had to re-derive them: read the journal for the version,
import the module for the tokens, then assemble a nine-pair command line. That is
friction without safety — the compare-and-swap is what makes the write safe, and
it still runs either way.

`--acked` does the peek itself. These tests cover the position arithmetic and the
refusals, since a convenience flag that silently advanced past unread mail would
be much worse than the friction it removes.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import types

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import goalflight_journal as journal  # noqa: E402
import goalflight_messages as gm  # noqa: E402


def _item(stream_id: str, stream_seq: int):
    return types.SimpleNamespace(stream_id=stream_id, stream_seq=stream_seq)


def _peek(items, *, cursor_version=1, snapshots=None):
    return types.SimpleNamespace(
        items=items,
        cursor_version=cursor_version,
        stream_snapshots=snapshots or {},
    )


def test_highest_seq_per_stream_is_the_target() -> None:
    """Advancing means 'seen up to here', so the target is the largest seq."""
    got = gm._acked_positions(_peek([
        _item("alpha", 1), _item("alpha", 4), _item("alpha", 2),
        _item("beta", 7),
    ]))
    assert got == {"alpha": 4, "beta": 7}


def test_out_of_order_items_do_not_lower_the_target() -> None:
    """A later-listed lower seq must not walk the cursor backwards."""
    got = gm._acked_positions(_peek([_item("alpha", 9), _item("alpha", 3)]))
    assert got == {"alpha": 9}


def test_empty_peek_yields_no_advance() -> None:
    assert gm._acked_positions(_peek([])) == {}
    assert gm._acked_positions(_peek(None)) == {}


def test_dict_shaped_items_are_accepted() -> None:
    """The peek may hand back mappings rather than objects."""
    got = gm._acked_positions(_peek([
        {"stream_id": "alpha", "stream_seq": 2},
        {"stream_id": "alpha", "stream_seq": 5},
    ]))
    assert got == {"alpha": 5}


def test_malformed_items_are_skipped_not_guessed() -> None:
    """An item without a stream or seq is skipped; inventing one would advance
    past something never actually seen."""
    got = gm._acked_positions(_peek([
        _item("alpha", 3),
        types.SimpleNamespace(stream_id=None, stream_seq=9),
        types.SimpleNamespace(stream_id="beta", stream_seq=None),
    ]))
    assert got == {"alpha": 3}


def _run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPTS / "goalflight_messages.py"), "advance",
         "--controller-label", "probe", "--lease-nonce", "nonce", *args],
        capture_output=True, text=True,
    )


def test_acked_refuses_to_mix_with_hand_supplied_cas() -> None:
    """Half-derived, half-supplied CAS inputs are the one way to get this wrong."""
    done = _run("--acked", "--cursor-version", "5")
    assert done.returncode != 0
    assert "pass it alone" in done.stderr


def test_cursor_version_still_required_without_acked() -> None:
    """The old contract is unchanged for callers that do supply the inputs."""
    done = _run("--position", "alpha=1")
    assert done.returncode != 0
    assert "--cursor-version is required" in done.stderr
    assert "--acked" in done.stderr, "the error should name the easier route"


STREAM = "engine-fleet-laptop-load"
FANOUT = (
    "battery-main",
    "battery-bugs",
    "battery-perf",
    "battery-webui",
    "battery-webui",
)


def _set_state_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict[str, str]:
    values = {
        "GOALFLIGHT_TASK_STORE_DIR": str(tmp_path / "task-store"),
        "GOALFLIGHT_JOURNAL_DIR": str(tmp_path / "journal-state"),
        "GOALFLIGHT_MESSAGES_DIR": str(tmp_path / "messages"),
        "GOALFLIGHT_STATE_DIR": str(tmp_path / "dispatch-state"),
        "GOALFLIGHT_WAKE_LEDGER_DIR": str(tmp_path / "wake-ledger"),
        "GOAL_FLIGHT_PIDFILE_DIR": str(tmp_path / "pidfiles"),
        "GOALFLIGHT_CAPACITY_CONF": "/dev/null",
        "GOALFLIGHT_TEST_MODE": "1",
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)
        if value != "/dev/null":
            Path(value).mkdir(parents=True, exist_ok=True)
    monkeypatch.delenv("GOALFLIGHT_DISPATCH_ID", raising=False)
    env = os.environ.copy()
    env.update(values)
    return env


def _git_project(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    project.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=project, check=True)
    return project


def _claim(authority: journal.Journal, label: str):
    claimed = authority.claim_or_renew_lease(
        label, principal={"principal_id": f"{label}-principal"}
    )
    assert claimed.committed and claimed.value is not None, claimed.reason
    return claimed.value


def _post(
    project: Path,
    messages_dir: Path,
    label: str,
    text: str,
    *,
    dispatch_id: str = STREAM,
    allow_duplicate: bool = False,
    event_ts: str | None = None,
) -> dict:
    kwargs: dict = {
        "dispatch_id": dispatch_id,
        "msg_type": "controller-notice",
        "payload": {"text": text},
        "messages_dir": messages_dir,
        "source": {"node": "test", "adapter": "pytest", "transport": "controller"},
        "addressee": gm.controller_addressee(label, project_root=project),
    }
    if allow_duplicate:
        kwargs["skip_if"] = lambda _item: False
    if event_ts is not None:
        kwargs["event_ts"] = event_ts
    return gm.post_message(**kwargs)


def _pending(authority: journal.Journal, label: str) -> list[tuple[str, int]]:
    return [
        (str(row["stream_id"]), int(row["stream_seq"]))
        for row in authority.pending_delivery_events(label, waking_only=False)
    ]


def _advance_acked(project: Path, label: str, nonce: str) -> int:
    stdout, stderr = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        rc = gm.main(
            [
                "advance",
                "--acked",
                "--controller-label",
                label,
                "--lease-nonce",
                nonce,
                "--json",
                "--project-root",
                str(project),
            ]
        )
    assert rc == 0, stderr.getvalue() or stdout.getvalue()
    return rc


def test_acked_advances_past_duplicate_envelopes_to_one_addressee(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Two seqs to one controller on one stream: --acked must drain both.

    A truncated peek locates only the first; completing the mentioned stream
    is what makes unread reach zero. Revert complete_addressed_streams and
    this fails with peek limit 1.
    """
    _set_state_env(monkeypatch, tmp_path)
    monkeypatch.setattr(gm, "_ACKED_PEEK_LIMIT", 1)
    project = _git_project(tmp_path)
    authority = journal.open_or_create_journal(project)
    lease = _claim(authority, "battery-webui")
    messages_dir = Path(os.environ["GOALFLIGHT_MESSAGES_DIR"])
    first = _post(project, messages_dir, "battery-webui", "one message")
    second = _post(
        project,
        messages_dir,
        "battery-webui",
        "one message",
        allow_duplicate=True,
    )
    assert first["envelope"]["seq"] == 1
    assert second["envelope"]["seq"] == 2
    assert _pending(authority, "battery-webui") == [(STREAM, 1), (STREAM, 2)]
    _advance_acked(project, "battery-webui", lease.nonce)
    assert _pending(authority, "battery-webui") == []
    cursor = authority.cursor_status("battery-webui")
    assert cursor is not None
    assert cursor["positions"] == {STREAM: 2}


def test_acked_does_not_skip_unshown_mail_on_another_stream(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _set_state_env(monkeypatch, tmp_path)
    monkeypatch.setattr(gm, "_ACKED_PEEK_LIMIT", 1)
    project = _git_project(tmp_path)
    authority = journal.open_or_create_journal(project)
    lease = _claim(authority, "battery-webui")
    messages_dir = Path(os.environ["GOALFLIGHT_MESSAGES_DIR"])
    _post(project, messages_dir, "battery-webui", "shown", dispatch_id="aaa-shown")
    _post(
        project,
        messages_dir,
        "battery-webui",
        "never shown",
        dispatch_id="zzz-unshown",
    )
    _advance_acked(project, "battery-webui", lease.nonce)
    assert _pending(authority, "battery-webui") == [("zzz-unshown", 1)]


def test_acked_single_envelope_still_drains(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _set_state_env(monkeypatch, tmp_path)
    project = _git_project(tmp_path)
    authority = journal.open_or_create_journal(project)
    lease = _claim(authority, "controller")
    messages_dir = Path(os.environ["GOALFLIGHT_MESSAGES_DIR"])
    _post(project, messages_dir, "controller", "solo", dispatch_id="solo-stream")
    _advance_acked(project, "controller", lease.nonce)
    assert _pending(authority, "controller") == []
    cursor = authority.cursor_status("controller")
    assert cursor is not None
    assert cursor["positions"] == {"solo-stream": 1}


def test_acked_fanout_with_duplicate_addressee_drains_every_controller(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _set_state_env(monkeypatch, tmp_path)
    project = _git_project(tmp_path)
    authority = journal.open_or_create_journal(project)
    leases = {label: _claim(authority, label) for label in dict.fromkeys(FANOUT)}
    messages_dir = Path(os.environ["GOALFLIGHT_MESSAGES_DIR"])
    for index, label in enumerate(FANOUT):
        _post(
            project,
            messages_dir,
            label,
            "one message",
            allow_duplicate=index == len(FANOUT) - 1,
        )
    for label, lease in leases.items():
        _advance_acked(project, label, lease.nonce)
        assert _pending(authority, label) == [], label
        cursor = authority.cursor_status(label)
        assert cursor is not None
        assert STREAM in cursor["positions"]


def test_identical_addressee_payload_retry_is_not_recorded(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _set_state_env(monkeypatch, tmp_path)
    project = _git_project(tmp_path)
    authority = journal.open_or_create_journal(project)
    _claim(authority, "battery-webui")
    messages_dir = Path(os.environ["GOALFLIGHT_MESSAGES_DIR"])
    first = _post(project, messages_dir, "battery-webui", "one message")
    second = _post(project, messages_dir, "battery-webui", "one message")
    assert first["recorded"] is True
    assert second["recorded"] is False
    assert second["envelope"]["seq"] == first["envelope"]["seq"]


def test_relay_summary_only_since_is_headline_and_count_without_bodies(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    env = _set_state_env(monkeypatch, tmp_path)
    project = _git_project(tmp_path)
    monkeypatch.setenv("GOALFLIGHT_CONTROLLER_LABEL", "battery-webui")
    monkeypatch.setattr(gm, "_current_project_root", lambda: project)
    authority = journal.open_or_create_journal(project)
    _claim(authority, "battery-webui")
    messages_dir = Path(env["GOALFLIGHT_MESSAGES_DIR"])
    _post(
        project,
        messages_dir,
        "battery-webui",
        "old body that must not print",
        dispatch_id="old-stream",
        event_ts="2026-01-01T00:00:00+00:00",
    )
    _post(
        project,
        messages_dir,
        "battery-webui",
        "new headline",
        dispatch_id="new-stream",
        event_ts="2026-08-01T00:00:00+00:00",
    )
    stdout, stderr = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        rc = gm.main(
            ["relay", "--new", "--summary-only", "--since", "2026-07-01T00:00:00Z"]
        )
    assert rc == 0, stderr.getvalue()
    text = stdout.getvalue()
    assert "old body that must not print" not in text
    assert "new-stream" in text
    assert "new headline" in text
    assert "old-stream" not in text
    assert "pending counts: new-stream=1" in text
    assert '"text"' not in text


def test_parse_since_rejects_garbage() -> None:
    with pytest.raises(ValueError, match="RFC3339"):
        gm._parse_since_timestamp("yesterday")


def test_same_addressed_payload_requires_matching_label_and_body() -> None:
    def addressed(label: str) -> dict:
        return {
            "type": "controller-notice",
            "payload": {"text": "one message"},
            "addressee": {
                "kind": "controller",
                "label": label,
                "project_root": "/tmp/example-project",
            },
        }

    incoming = addressed("battery-webui")
    assert gm._same_addressed_payload(incoming, incoming)
    assert not gm._same_addressed_payload(addressed("battery-main"), incoming)
    different_text = addressed("battery-webui")
    different_text["payload"] = {"text": "other"}
    assert not gm._same_addressed_payload(different_text, incoming)
