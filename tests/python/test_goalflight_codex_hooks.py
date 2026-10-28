from __future__ import annotations

import json
from pathlib import Path
import shlex
import time

import pytest

from scripts import goalflight_codex_hooks as hooks
from scripts import goalflight_messages as messages
from scripts import goalflight_steer_mailbox as mailbox


DISPATCH_ID = "codex-hook-test-123"


def _mailbox(tmp_path: Path, monkeypatch) -> Path:
    path = tmp_path / f"{DISPATCH_ID}.steer.jsonl"
    monkeypatch.setenv("GOALFLIGHT_DISPATCH_ID", DISPATCH_ID)
    monkeypatch.setenv("GOALFLIGHT_STEER_FILE", str(path))
    return path


def _append(path: Path, seq: int, text: str) -> None:
    mailbox.append_steer_entry(path, text, seq=seq, dispatch_id=DISPATCH_ID)


def _run_hook(monkeypatch, capsys, *, now: float = 1_800_000_000.0) -> dict | None:
    monkeypatch.setattr(hooks.time, "time", lambda: now)
    assert hooks.main(["posttooluse"]) == 0
    output = capsys.readouterr()
    assert output.err == ""
    return json.loads(output.out) if output.out else None


def test_two_pending_steers_are_injected_in_sequence_order(tmp_path, monkeypatch, capsys):
    path = _mailbox(tmp_path, monkeypatch)
    _append(path, 1, "first controller steer")
    _append(path, 2, "second controller steer")

    result = _run_hook(monkeypatch, capsys)

    context = result["hookSpecificOutput"]["additionalContext"]
    assert context.index("[STEER 1]") < context.index("[STEER 2]")
    assert "first controller steer" in context
    assert "second controller steer" in context
    assert path.with_name(f"{DISPATCH_ID}.steer-delivered.json").exists()


def test_new_steer_after_one_mib_of_acked_history_is_delivered(tmp_path, monkeypatch, capsys):
    path = _mailbox(tmp_path, monkeypatch)
    history_count = 24_000
    old_rows = [
        json.dumps({"seq": seq, "ts": "2026-01-01T00:00:00Z", "text": "old"}, separators=(",", ":"))
        for seq in range(1, history_count + 1)
    ]
    final_row = json.dumps(
        {"seq": history_count + 1, "ts": "2026-01-01T00:00:00Z", "text": "urgent HALT"},
        separators=(",", ":"),
    )
    path.write_text("\n".join([*old_rows, final_row]) + "\n", encoding="utf-8")
    assert path.stat().st_size > 1024 * 1024
    status = path.with_name(f"{DISPATCH_ID}.status.json")
    status.write_text(
        json.dumps({"markers": {"STEER-ACK": [str(seq) for seq in range(1, history_count + 1)]}}),
        encoding="utf-8",
    )

    result = _run_hook(monkeypatch, capsys)

    context = result["hookSpecificOutput"]["additionalContext"]
    assert "urgent HALT" in context
    assert "Older pending steers" not in context
    assert path.stat().st_size > 1024 * 1024
    assert path.with_name(f"{DISPATCH_ID}.steer-delivered.json").exists()


def test_large_controller_steer_is_accepted(tmp_path):
    path = tmp_path / f"{DISPATCH_ID}.steer.jsonl"
    entry = mailbox.append_steer_entry(path, "x" * (700 * 1024), dispatch_id=DISPATCH_ID)
    assert len(entry["text"]) == 700 * 1024


def test_append_row_max_does_not_exceed_hook_window():
    assert mailbox.maximum_steer_row_bytes() <= hooks._max_mailbox_window_bytes()


def test_large_newest_steer_is_delivered_behind_one_mib_history(tmp_path, monkeypatch, capsys):
    path = _mailbox(tmp_path, monkeypatch)
    old_rows = [
        json.dumps({"seq": seq, "ts": "2026-01-01T00:00:00Z", "text": "old"}, separators=(",", ":"))
        for seq in range(1, 24_000)
    ]
    path.write_text("\n".join(old_rows) + "\n", encoding="utf-8")
    _append(path, 24_000, "urgent large HALT " + "x" * (700 * 1024))
    status = path.with_name(f"{DISPATCH_ID}.status.json")
    status.write_text(
        json.dumps({"markers": {"STEER-ACK": [str(seq) for seq in range(1, 24_000)]}}),
        encoding="utf-8",
    )

    result = _run_hook(monkeypatch, capsys)

    context = result["hookSpecificOutput"]["additionalContext"]
    assert "steer seq 24000 is too large to inline; read it now:" in context
    assert str(path) in context
    assert "[STEER 24000]" in context


def test_largest_payload_steer_is_delivered_or_pointed_to(tmp_path, monkeypatch, capsys):
    path = _mailbox(tmp_path, monkeypatch)
    payload = {"text": "HALT " + "x" * (messages.MAX_PAYLOAD_JSON_BYTES - 16)}
    messages.validate_payload(payload)
    mailbox.append_steer_entry(
        path, payload["text"], seq=1, dispatch_id=DISPATCH_ID,
        context={"message_envelope": {"payload": payload}},
    )
    assert path.stat().st_size > 1_048_604

    result = _run_hook(monkeypatch, capsys)

    context = result["hookSpecificOutput"]["additionalContext"]
    assert "[STEER 1]" in context
    assert (
        "steer seq 1 is too large to inline; read it now:" in context
        or "x" * 1024 in context
    )


@pytest.mark.parametrize("characters", [350_000, (messages.MAX_PAYLOAD_JSON_BYTES - 13) // 2])
@pytest.mark.parametrize("field", ["text", "detail"])
def test_non_ascii_message_projects_and_is_pointed_to(tmp_path, monkeypatch, capsys, characters, field):
    path = _mailbox(tmp_path, monkeypatch)
    monkeypatch.setattr(mailbox, "steer_file", lambda *_args, **_kwargs: path)
    text = "é" * characters
    payload = {field: text}
    messages.validate_payload(payload)
    envelope = {
        "schema": "goalflight.message.v1",
        "schema_version": 1,
        "id": "00000000-0000-4000-8000-000000000024",
        "dispatch_id": DISPATCH_ID,
        "seq": 1,
        "ts": "2026-01-01T00:00:00Z",
        "source": {"node": "test", "adapter": "test", "transport": "steer"},
        "type": "steering",
        "payload": payload,
    }
    messages.validate_envelope(envelope)

    projected_path, entry = mailbox.append_message_view(DISPATCH_ID, envelope)

    assert projected_path == path
    if field == "text":
        assert entry["text"] == text
    else:
        assert json.loads(entry["text"]) == payload
    assert text.encode("utf-8") in path.read_bytes()
    assert mailbox.read_steer_entries(path)[0]["text"] == entry["text"]
    result = _run_hook(monkeypatch, capsys)
    context = result["hookSpecificOutput"]["additionalContext"]
    assert "steer seq 1 is too large to inline; read it now:" in context
    assert str(path) in context


@pytest.mark.parametrize("token", ['"', "\\", 'é"'])
def test_oversized_projection_appends_and_delivers_a_stored_message_pointer(
    tmp_path, monkeypatch, capsys, token
):
    path = _mailbox(tmp_path, monkeypatch)
    monkeypatch.setattr(mailbox, "steer_file", lambda *_args, **_kwargs: path)
    store = tmp_path / "messages"
    store.mkdir(exist_ok=True)
    monkeypatch.setenv("GOALFLIGHT_MESSAGES_DIR", str(store))
    unit_bytes = len(json.dumps(token, ensure_ascii=False).encode("utf-8")) - 2
    payload = {"detail": token * ((messages.MAX_PAYLOAD_JSON_BYTES - 13) // unit_bytes)}
    messages.validate_payload(payload)
    envelope = {
        "schema": "goalflight.message.v1",
        "schema_version": 1,
        "id": "00000000-0000-4000-8000-000000000024",
        "dispatch_id": DISPATCH_ID,
        "seq": 1,
        "ts": "2026-01-01T00:00:00Z",
        "source": {"node": "test", "adapter": "test", "transport": "steer"},
        "type": "steering",
        "payload": payload,
    }
    messages.validate_envelope(envelope)
    inbox = messages.inbox_path(store, DISPATCH_ID)
    inbox.write_text(messages.serialize_envelope_line(envelope), encoding="utf-8")
    _append(path, 1, "earlier steer")

    _, entry = mailbox.append_message_view(DISPATCH_ID, envelope)

    assert entry["seq"] == 2
    assert entry["kind"] == "message"
    assert entry.get("direction", mailbox.TO_WORKER) == mailbox.TO_WORKER
    assert entry["context"]["message_id"] == envelope["id"]
    assert entry["context"]["original_encoded_bytes"] > mailbox.maximum_steer_row_bytes()
    assert len(entry["context"]["preview"]) <= 160
    assert len(path.read_bytes().splitlines(keepends=True)[-1]) < 4096
    command = shlex.split(entry["context"]["read_command"])
    assert command[-4:] == ["read", "--dispatch-id", DISPATCH_ID, "--json"]
    assert next(item for item in messages.read_envelopes(inbox) if item["id"] == envelope["id"])["payload"] == payload
    _, repeated = mailbox.append_message_view(DISPATCH_ID, envelope)
    assert repeated == entry

    result = _run_hook(monkeypatch, capsys)

    context = result["hookSpecificOutput"]["additionalContext"]
    assert "[STEER 2]" in context
    assert "oversized: read via" in context
    assert envelope["id"] in context


def test_exact_append_maximum_row_is_pointed_to(tmp_path, monkeypatch, capsys):
    path = _mailbox(tmp_path, monkeypatch)
    _append(path, 1, "")
    overhead = path.stat().st_size
    text_size = mailbox.maximum_steer_row_bytes() - overhead
    _append(path, 2, "x" * text_size)
    assert len(path.read_bytes().splitlines(keepends=True)[-1]) == mailbox.maximum_steer_row_bytes()

    result = _run_hook(monkeypatch, capsys)

    context = result["hookSpecificOutput"]["additionalContext"]
    assert "steer seq 2 is too large to inline; read it now:" in context
    assert str(path) in context


def test_append_above_shared_row_limit_is_rejected(tmp_path):
    path = tmp_path / f"{DISPATCH_ID}.steer.jsonl"
    import pytest

    with pytest.raises(ValueError, match=f"{mailbox.maximum_steer_row_bytes()}-byte cap"):
        _append(path, 1, "x" * mailbox.maximum_steer_row_bytes())


def test_legacy_row_above_window_gets_a_throttled_pointer(tmp_path, monkeypatch, capsys):
    path = _mailbox(tmp_path, monkeypatch)
    path.write_text(
        json.dumps({"seq": 7, "text": "x" * (hooks._max_mailbox_window_bytes() + 100)}) + "\n",
        encoding="utf-8",
    )

    result = _run_hook(monkeypatch, capsys)

    context = result["hookSpecificOutput"]["additionalContext"]
    assert "Pending steers from seq 1 are outside the window" in context
    assert hooks._steer_read_command(DISPATCH_ID) in context
    assert _run_hook(monkeypatch, capsys, now=1_800_000_001.0) is None


def test_tail_reader_keeps_first_row_when_window_starts_on_boundary(tmp_path):
    path = tmp_path / f"{DISPATCH_ID}.steer.jsonl"
    rows = [
        json.dumps({"seq": seq, "ts": "2026-01-01T00:00:00Z", "text": f"row {seq}"}, separators=(",", ":"))
        for seq in range(1, 4)
    ]
    path.write_text("\n".join(rows) + "\n", encoding="utf-8")

    entries, truncated = mailbox.read_steer_entries_tail(
        path, max_bytes=len(rows[2]) + 1, max_window_bytes=len(rows[2]) + 1
    )

    assert truncated is True
    assert [entry["seq"] for entry in entries] == [3]


def test_new_steer_is_injected_after_older_one_is_acked(tmp_path, monkeypatch, capsys):
    path = _mailbox(tmp_path, monkeypatch)
    _append(path, 1, "first controller steer")
    first = _run_hook(monkeypatch, capsys)
    assert "first controller steer" in first["hookSpecificOutput"]["additionalContext"]

    status = path.with_name(f"{DISPATCH_ID}.status.json")
    status.write_text(json.dumps({"markers": {"STEER-ACK": ["1"]}}), encoding="utf-8")
    _append(path, 2, "new controller steer")

    second = _run_hook(monkeypatch, capsys, now=1_800_000_001.0)
    context = second["hookSpecificOutput"]["additionalContext"]
    assert "new controller steer" in context
    assert "first controller steer" not in context


def test_unacked_steer_repeat_is_throttled(tmp_path, monkeypatch, capsys):
    path = _mailbox(tmp_path, monkeypatch)
    _append(path, 1, "controller steer")

    assert _run_hook(monkeypatch, capsys) is not None
    assert _run_hook(monkeypatch, capsys, now=1_800_000_001.0) is None
    repeated = _run_hook(
        monkeypatch,
        capsys,
        now=1_800_000_000.0 + hooks.STEER_REDELIVERY_INTERVAL_SECS,
    )
    assert repeated is not None


@pytest.mark.parametrize("large_acked", [False, True])
def test_small_newer_steer_does_not_hide_pending_large_steer(
    tmp_path, monkeypatch, capsys, large_acked
):
    path = _mailbox(tmp_path, monkeypatch)
    _append(path, 1, "HALT " + "x" * (700 * 1024))
    _append(path, 2, "newer small steer")
    if large_acked:
        path.with_name(f"{DISPATCH_ID}.status.json").write_text(
            json.dumps({"markers": {"STEER-ACK": ["1"]}}), encoding="utf-8"
        )

    result = _run_hook(monkeypatch, capsys)

    context = result["hookSpecificOutput"]["additionalContext"]
    assert "[STEER 2]" in context
    assert "newer small steer" in context
    if large_acked:
        assert "[STEER 1]" not in context
    else:
        assert context.index("[STEER 1]") < context.index("[STEER 2]")
        assert "steer seq 1 is too large to inline; read it now:" in context


def test_max_window_notice_includes_exact_retrieval_command(tmp_path, monkeypatch, capsys):
    path = _mailbox(tmp_path, monkeypatch)
    for seq in range(1, 4):
        _append(path, seq, "x" * (700 * 1024))

    result = _run_hook(monkeypatch, capsys)

    context = result["hookSpecificOutput"]["additionalContext"]
    assert "Pending steers from seq 1 are outside the window; read them now with:" in context
    expected_command = shlex.join([
        hooks.goalflight_compat.python_executable(),
        str(Path(hooks.__file__).resolve().with_name("goalflight_dispatch.py")),
        "steer", DISPATCH_ID, "--list",
    ])
    assert expected_command in context
    assert "[STEER 2]" in context
    assert "[STEER 3]" in context


@pytest.mark.parametrize("size", [700 * 1024, 1400 * 1024])
@pytest.mark.parametrize("newer_acked", [False, True])
def test_pending_halt_survives_newer_pending_or_acked_steer(
    tmp_path, monkeypatch, capsys, size, newer_acked
):
    path = _mailbox(tmp_path, monkeypatch)
    _append(path, 1, "HALT " + "x" * size)
    _append(path, 2, "newer small steer")
    if newer_acked:
        path.with_name(f"{DISPATCH_ID}.status.json").write_text(
            json.dumps({"markers": {"STEER-ACK": ["2"]}}), encoding="utf-8"
        )

    result = _run_hook(monkeypatch, capsys)

    context = result["hookSpecificOutput"]["additionalContext"]
    assert "[STEER 1]" in context
    assert "steer seq 1 is too large to inline; read it now:" in context
    assert context.split("read it now: ", 1)[1].splitlines()[0] == hooks._steer_read_command(DISPATCH_ID)
    assert ("[STEER 2]" in context) is not newer_acked


def test_mixed_acknowledgements_deliver_the_gap(tmp_path, monkeypatch, capsys):
    path = _mailbox(tmp_path, monkeypatch)
    _append(path, 1, "already acked")
    _append(path, 2, "HALT " + "x" * (700 * 1024))
    _append(path, 3, "also acked")
    path.with_name(f"{DISPATCH_ID}.status.json").write_text(
        json.dumps({"markers": {"STEER-ACK": ["1", "3"]}}), encoding="utf-8"
    )

    result = _run_hook(monkeypatch, capsys)

    context = result["hookSpecificOutput"]["additionalContext"]
    assert "[STEER 2]" in context
    assert hooks._steer_read_command(DISPATCH_ID) in context
    assert "[STEER 1]" not in context
    assert "[STEER 3]" not in context


def test_window_notice_is_delivered_when_all_visible_rows_are_acked(tmp_path, monkeypatch, capsys):
    path = _mailbox(tmp_path, monkeypatch)
    for seq in range(1, 4):
        _append(path, seq, "x" * (700 * 1024))
    path.with_name(f"{DISPATCH_ID}.status.json").write_text(
        json.dumps({"markers": {"STEER-ACK": ["2", "3"]}}), encoding="utf-8"
    )

    result = _run_hook(monkeypatch, capsys)

    context = result["hookSpecificOutput"]["additionalContext"]
    assert "Pending steers from seq 1 are outside the window; read them now with:" in context
    assert hooks._steer_read_command(DISPATCH_ID) in context
    assert "[STEER 2]" not in context
    assert "[STEER 3]" not in context


def test_five_mib_mailbox_delivery_is_fast_and_does_not_rewrite_history(
    tmp_path, monkeypatch, capsys
):
    path = _mailbox(tmp_path, monkeypatch)
    history = (b'{"seq":1,"text":"old"}\n' * ((5 * 1024 * 1024) // 22))
    path.write_bytes(history + b'{"seq":999999,"text":"new HALT"}\n')
    path.with_name(f"{DISPATCH_ID}.status.json").write_text(
        json.dumps({"markers": {"STEER-ACK": ["1"]}}), encoding="utf-8"
    )
    before = path.stat().st_mtime_ns

    started = time.perf_counter()
    result = _run_hook(monkeypatch, capsys)
    elapsed = time.perf_counter() - started

    assert "new HALT" in result["hookSpecificOutput"]["additionalContext"]
    assert elapsed < 2.0
    assert path.stat().st_mtime_ns == before


def test_sidecar_write_failure_still_delivers(tmp_path, monkeypatch, capsys):
    path = _mailbox(tmp_path, monkeypatch)
    _append(path, 1, "deliver despite sidecar failure")
    monkeypatch.setattr(hooks, "_write_deliveries", lambda *_args: (_ for _ in ()).throw(OSError()))

    result = _run_hook(monkeypatch, capsys)

    assert "deliver despite sidecar failure" in result["hookSpecificOutput"]["additionalContext"]


def test_hook_errors_are_silent_and_exit_successfully(monkeypatch, capsys):
    monkeypatch.setenv("GOALFLIGHT_DISPATCH_ID", DISPATCH_ID)
    monkeypatch.setenv("GOALFLIGHT_STEER_FILE", "/nonexistent/other-dispatch.steer.jsonl")

    assert hooks.main(["posttooluse"]) == 0

    output = capsys.readouterr()
    assert output.out == ""
    assert output.err == ""


def test_corrupt_mailbox_rows_do_not_write_hook_stderr(tmp_path, monkeypatch, capsys):
    path = _mailbox(tmp_path, monkeypatch)
    row = json.dumps({"seq": 1, "ts": "2026-01-01T00:00:00Z", "text": "deliver despite corruption"})
    path.write_text("{malformed row}\n" + row + "\n", encoding="utf-8")

    result = _run_hook(monkeypatch, capsys)

    assert "deliver despite corruption" in result["hookSpecificOutput"]["additionalContext"]


def test_oversized_status_does_not_block_delivery(tmp_path, monkeypatch, capsys):
    path = _mailbox(tmp_path, monkeypatch)
    _append(path, 1, "deliver with unknown acknowledgements")
    status = path.with_name(f"{DISPATCH_ID}.status.json")
    status.write_text(" " * (2 * 1024 * 1024), encoding="utf-8")

    result = _run_hook(monkeypatch, capsys)

    assert "deliver with unknown acknowledgements" in result["hookSpecificOutput"]["additionalContext"]


def test_malformed_status_does_not_block_delivery(tmp_path, monkeypatch, capsys):
    path = _mailbox(tmp_path, monkeypatch)
    _append(path, 1, "deliver with malformed status")
    status = path.with_name(f"{DISPATCH_ID}.status.json")
    status.write_text("[]", encoding="utf-8")

    result = _run_hook(monkeypatch, capsys)

    assert "deliver with malformed status" in result["hookSpecificOutput"]["additionalContext"]


def test_worker_hooks_document_has_codex_hooks_and_trust_state(tmp_path):
    skill_root = tmp_path / "installed-skill"
    worker_home = tmp_path / "worker-home"

    document = hooks.worker_hooks_document(skill_root, worker_home)

    post_tool = document["hooks_json"]["hooks"]["PostToolUse"][0]["hooks"][0]
    command = shlex.split(post_tool["command"])
    assert command == [
        hooks.goalflight_compat.python_executable(),
        str(skill_root.resolve() / "scripts" / "goalflight_codex_hooks.py"),
        "posttooluse",
    ]
    assert post_tool["type"] == "command"
    assert post_tool["timeout"] == hooks.STEER_HOOK_TIMEOUT_SECS
    assert post_tool["additionalContextLimit"] == hooks.STEER_HOOK_ADDITIONAL_CONTEXT_LIMIT
    assert set(document["hooks_json"]["hooks"]) == {"PostToolUse", "UserPromptSubmit"}
    trust_key = f"{worker_home.resolve() / 'hooks.json'}:post_tool_use:0:0"
    assert trust_key in document["hooks_state"]
    assert document["hooks_state"][trust_key] == {
        "trusted_hash": hooks._hook_identity_hash("post_tool_use", post_tool["command"])
    }
