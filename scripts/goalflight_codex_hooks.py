"""Codex lifecycle hooks for Goal Flight workers."""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import shlex
import sys
import tempfile
import time
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

try:
    import goalflight_compat
    import goalflight_steer_mailbox
    import goalflight_dispatch_paths
except Exception:  # pragma: no cover - exercised by the silent-failure test
    goalflight_compat = None  # type: ignore[assignment]
    goalflight_steer_mailbox = None  # type: ignore[assignment]
    goalflight_dispatch_paths = None  # type: ignore[assignment]


STEER_REDELIVERY_INTERVAL_SECS = 60.0
STEER_HOOK_MAX_MAILBOX_BYTES = 256 * 1024
STEER_HOOK_MAX_STATUS_BYTES = 1024 * 1024
STEER_HOOK_MAX_SIDECAR_BYTES = 128 * 1024
STEER_HOOK_MAX_SIDECAR_ENTRIES = 4096
STEER_HOOK_LOCK_TIMEOUT_SECS = 0.05
STEER_HOOK_TIMEOUT_SECS = 2
STEER_HOOK_ADDITIONAL_CONTEXT_LIMIT = 0

_EVENTS = {
    "posttooluse": ("PostToolUse", "post_tool_use"),
    "userpromptsubmit": ("UserPromptSubmit", "user_prompt_submit"),
}


def _hook_identity_hash(event_key: str, command: str) -> str:
    """Match Codex's normalized hook identity fingerprint for this command."""
    identity = {
        "event_name": event_key,
        "hooks": [
            {
                "type": "command",
                "command": command,
                "timeout": STEER_HOOK_TIMEOUT_SECS,
                "async": False,
                "additionalContextLimit": STEER_HOOK_ADDITIONAL_CONTEXT_LIMIT,
            }
        ],
    }
    canonical = json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "sha256:" + hashlib.sha256(canonical).hexdigest()


def worker_hooks_document(skill_root: str | Path, worker_home: str | Path) -> dict[str, Any]:
    """Return the per-worker hooks.json and matching config.toml trust state."""
    skill_path = Path(skill_root).expanduser().resolve(strict=False)
    home_path = Path(worker_home).expanduser().resolve(strict=False)
    hook_script = skill_path / "scripts" / "goalflight_codex_hooks.py"
    hook_groups: dict[str, list[dict[str, Any]]] = {}
    hook_states: dict[str, dict[str, str]] = {}
    for subcommand, (event_name, event_key) in _EVENTS.items():
        python = goalflight_compat.python_executable() if goalflight_compat else sys.executable
        command = shlex.join([python, str(hook_script), subcommand])
        hook_groups[event_name] = [
            {
                "hooks": [
                    {
                        "type": "command",
                        "command": command,
                        "timeout": STEER_HOOK_TIMEOUT_SECS,
                        "additionalContextLimit": STEER_HOOK_ADDITIONAL_CONTEXT_LIMIT,
                    }
                ]
            }
        ]
        key = f"{home_path / 'hooks.json'}:{event_key}:0:0"
        hook_states[key] = {"trusted_hash": _hook_identity_hash(event_key, command)}
    return {
        "hooks_json": {"hooks": hook_groups},
        "hooks_state": hook_states,
    }


def _acked_seqs(dispatch_id: str, mailbox: Path) -> set[int]:
    if goalflight_steer_mailbox is None or goalflight_dispatch_paths is None:
        raise RuntimeError("Goal Flight hook modules are unavailable")
    filename = goalflight_dispatch_paths.safe_dispatch_filename(dispatch_id)
    status_path = mailbox.parent / f"{filename}.status.json"
    try:
        if not status_path.exists() or status_path.stat().st_size > STEER_HOOK_MAX_STATUS_BYTES:
            return set()
        return goalflight_steer_mailbox.acked_steer_seqs(
            {"status_path": str(status_path)}
        )
    except Exception:
        # Missing, oversized, or malformed status means acknowledgements are unknown.
        return set()


def _max_mailbox_window_bytes() -> int:
    return goalflight_steer_mailbox.maximum_steer_row_bytes()


def _delivery_sidecar_path(dispatch_id: str, mailbox: Path) -> Path:
    filename = goalflight_dispatch_paths.safe_dispatch_filename(dispatch_id)
    return mailbox.parent / f"{filename}.steer-delivered.json"


def _read_deliveries(path: Path) -> dict[str, float]:
    try:
        if path.stat().st_size > STEER_HOOK_MAX_SIDECAR_BYTES:
            return {}
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            return {}
        return {
            key: stamp
            for key, stamp in value.items()
            if isinstance(key, str)
            and key.isdigit()
            and isinstance(stamp, (int, float))
            and not isinstance(stamp, bool)
        }
    except (OSError, UnicodeError, ValueError, RecursionError):
        return {}


def _write_deliveries(path: Path, deliveries: dict[str, float]) -> None:
    merged = _read_deliveries(path)
    merged.update(deliveries)
    bounded = dict(
        sorted(merged.items(), key=lambda item: int(item[0]))[
            -STEER_HOOK_MAX_SIDECAR_ENTRIES:
        ]
    )
    encoded = json.dumps(bounded, separators=(",", ":")).encode("utf-8")
    if len(encoded) > STEER_HOOK_MAX_SIDECAR_BYTES:
        bounded = dict(list(bounded.items())[-STEER_HOOK_MAX_SIDECAR_ENTRIES // 2 :])
        encoded = json.dumps(bounded, separators=(",", ":")).encode("utf-8")
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        if hasattr(os, "fchmod"):
            os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        Path(temporary).unlink(missing_ok=True)


def pending_for_delivery(
    dispatch_id: str,
    mailbox_path: str | Path,
    *,
    now: float | None = None,
) -> tuple[list[dict[str, Any]], int | None, dict[str, float]]:
    """Select due, unacknowledged worker steers in mailbox sequence order."""
    if goalflight_steer_mailbox is None:
        raise RuntimeError("Goal Flight mailbox module is unavailable")
    mailbox = Path(mailbox_path).expanduser()
    expected_name = f"{goalflight_dispatch_paths.safe_dispatch_filename(dispatch_id)}.steer.jsonl"
    if mailbox.name != expected_name:
        raise ValueError("steer mailbox does not match dispatch id")
    acked = _acked_seqs(dispatch_id, mailbox)
    entries, truncated = goalflight_steer_mailbox.read_steer_entries_tail(
        mailbox,
        max_bytes=STEER_HOOK_MAX_MAILBOX_BYTES,
        max_window_bytes=_max_mailbox_window_bytes(),
        acked_seqs=acked,
        lock_timeout_secs=STEER_HOOK_LOCK_TIMEOUT_SECS,
    )
    highest_acked = goalflight_steer_mailbox.contiguous_acked_seq(acked)
    outside_window = (
        highest_acked + 1
        if truncated and (not entries or entries[0]["seq"] > highest_acked + 1)
        else None
    )
    pending = goalflight_steer_mailbox.pending_worker_entries(entries, acked)
    sidecar_path = _delivery_sidecar_path(dispatch_id, mailbox)
    deliveries = _read_deliveries(sidecar_path)
    current = time.time() if now is None else now
    due = [
        entry
        for entry in pending
        if str(entry["seq"]) not in deliveries
        or (
            current - deliveries[str(entry["seq"])] >= STEER_REDELIVERY_INTERVAL_SECS
        )
    ]
    updates = {str(entry["seq"]): current for entry in due}
    return sorted(due, key=lambda entry: int(entry["seq"])), outside_window, updates


def _steer_read_command(dispatch_id: str) -> str:
    return shlex.join([
        goalflight_compat.python_executable(),
        str(Path(__file__).resolve().with_name("goalflight_dispatch.py")),
        "steer", dispatch_id, "--list",
    ])


def _inject(
    dispatch_id: str, mailbox_path: str | Path, event_name: str
) -> tuple[dict[str, Any] | None, dict[str, float]]:
    if goalflight_steer_mailbox is None:
        return None, {}
    mailbox = Path(mailbox_path).expanduser()
    now = time.time()
    entries, outside_window, updates = pending_for_delivery(dispatch_id, mailbox, now=now)
    if outside_window is not None:
        deliveries = _read_deliveries(_delivery_sidecar_path(dispatch_id, mailbox))
        if now - deliveries.get("0", 0) < STEER_REDELIVERY_INTERVAL_SECS:
            outside_window = None
        else:
            updates["0"] = now
    if not entries and outside_window is None:
        return None, {}
    read_command = _steer_read_command(dispatch_id)
    block = ["Goal Flight controller steers (sequence order):"]
    for entry in entries:
        text = str(entry.get("text") or "")
        if len(text.encode("utf-8")) > STEER_HOOK_MAX_MAILBOX_BYTES:
            text = (
                f"steer seq {entry['seq']} is too large to inline; read it now: "
                f"{read_command}\nMailbox: {mailbox}"
            )
        block.extend((f"\n[STEER {entry['seq']}]", text))
    if outside_window is not None:
        block.append(
            f"\nPending steers from seq {outside_window} are outside the window; "
            f"read them now with: {read_command}"
        )
    block.append("\nAcknowledge each steer with `!STEER-ACK: <seq>` on its own line.")
    return {
        "hookSpecificOutput": {
            "hookEventName": event_name,
            "additionalContext": "\n".join(block),
        }
    }, updates


def main(argv: list[str] | None = None) -> int:
    try:
        args = sys.argv[1:] if argv is None else argv
        if len(args) != 1 or args[0] not in _EVENTS:
            return 0
        dispatch_id = os.environ.get("GOALFLIGHT_DISPATCH_ID", "").strip()
        mailbox = os.environ.get("GOALFLIGHT_STEER_FILE", "").strip()
        if not dispatch_id or not mailbox:
            return 0
        event_name, _event_key = _EVENTS[args[0]]
        with contextlib.redirect_stderr(io.StringIO()):
            output, updates = _inject(dispatch_id, mailbox, event_name)
        if output is not None:
            print(json.dumps(output, ensure_ascii=False, separators=(",", ":")))
            sys.stdout.flush()
            try:
                _write_deliveries(_delivery_sidecar_path(dispatch_id, Path(mailbox)), updates)
            except Exception:
                pass
    except BaseException:
        # A mailbox or hook failure must never interfere with the worker tool call.
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
