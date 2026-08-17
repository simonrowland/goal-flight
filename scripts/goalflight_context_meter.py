#!/usr/bin/env python3
"""Cheap context-window meter from a Claude session transcript tail.

The newest assistant ``usage`` block carries the live context size:

    input_tokens + cache_read_input_tokens + cache_creation_input_tokens

A full JSONL scan is the slow path (transcripts run to many MB). This
reader seeks to EOF and inspects a bounded tail. If that tail has no
usage block it widens once, then reports unknown. The window is never
guessed: a missing or unrecognized window/model is unknown, not a number.

The hook stays silent below 80% and fires each band (80 → 90 → 95) once.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
from dataclasses import dataclass
from pathlib import Path
import sys
from typing import Any, Mapping


TAIL_BYTES = 64 * 1024
# One retry only, still O(1). Unbounded widening recreates a full scan.
WIDEN_BYTES = 1024 * 1024
BANDS = (80, 90, 95)
RECHECK_EVERY_CALLS = 20
RECHECK_GROWTH_BYTES = 1024 * 1024
# Empty on purpose. The same model id can be 200k or 1M depending on the
# session; a hardcoded map produced a 340% reading. Unlisted → unknown.
KNOWN_MODEL_WINDOWS: dict[str, int] = {}
STATE_SCHEMA = "goalflight.context-meter.v1"
USAGE_KEY = b'"usage"'


@dataclass(frozen=True)
class Reading:
    unknown: bool
    reason: str | None
    tokens: int | None
    window: int | None
    pct: float | None
    verdict: str
    band: int | None
    bytes_read: int


def context_tokens(usage: Mapping[str, Any]) -> int:
    """Live context size from a transcript usage block.

    Claude reports the current prompt as input plus the cache-read and
    cache-creation slices of that same prompt. Summing those three is the
    occupied window; output_tokens are the reply and do not occupy the
    next turn's input. Missing cache fields count as 0.
    """
    return (
        max(0, int(usage.get("input_tokens") or 0))
        + max(0, int(usage.get("cache_read_input_tokens") or 0))
        + max(0, int(usage.get("cache_creation_input_tokens") or 0))
    )


def context_window(value: object = None) -> int | None:
    """Resolve an explicit context window. None means 'not provided'.

    Garbage raises (caller decides whether to surface or stay silent).
    There is no default: a wrong window is worse than no reading.
    """
    raw = value
    if raw is None:
        raw = os.environ.get("GOALFLIGHT_CONTEXT_WINDOW")
    if raw is None:
        return None
    text = str(raw).strip()
    if text == "":
        return None
    try:
        window = int(text)
    except (TypeError, ValueError) as exc:
        raise ValueError("context window must be a positive integer") from exc
    if window <= 0:
        raise ValueError("context window must be a positive integer")
    return window


def window_for_model(model: object = None) -> int | None:
    """Fail-closed model → window lookup. Unrecognized is unknown, not 200k."""
    raw = model
    if raw is None:
        raw = os.environ.get("GOALFLIGHT_CONTEXT_MODEL")
    if raw is None:
        return None
    key = str(raw).strip()
    if not key:
        return None
    mapped = KNOWN_MODEL_WINDOWS.get(key)
    if mapped is None:
        mapped = KNOWN_MODEL_WINDOWS.get(key.lower())
    return int(mapped) if mapped is not None else None


def resolve_window(*, window: object = None, model: object = None) -> int | None:
    explicit = context_window(window)
    if explicit is not None:
        return explicit
    return window_for_model(model)


def recheck_every_calls(value: object = None) -> int:
    raw = value
    if raw is None:
        raw = os.environ.get("GOALFLIGHT_CONTEXT_METER_EVERY")
    if raw is None or str(raw).strip() == "":
        return RECHECK_EVERY_CALLS
    try:
        parsed = int(str(raw).strip())
    except (TypeError, ValueError) as exc:
        raise ValueError("context meter recheck interval must be an integer") from exc
    return max(1, min(parsed, 10_000))


def recheck_growth_bytes(value: object = None) -> int:
    raw = value
    if raw is None:
        raw = os.environ.get("GOALFLIGHT_CONTEXT_METER_GROWTH")
    if raw is None or str(raw).strip() == "":
        return RECHECK_GROWTH_BYTES
    try:
        parsed = int(str(raw).strip())
    except (TypeError, ValueError) as exc:
        raise ValueError("context meter growth threshold must be an integer") from exc
    return max(1, parsed)


def band_for(pct: float) -> int | None:
    reached = None
    for band in BANDS:
        if pct >= band:
            reached = band
    return reached


def verdict_for(pct: float) -> str:
    band = band_for(pct)
    if band is None:
        return "ok"
    return f"band-{band}"


def _parse_usage_object(buf: bytes, key_at: int) -> dict[str, Any] | None:
    idx = key_at + len(USAGE_KEY)
    length = len(buf)
    while idx < length and buf[idx] in b" \t\r\n":
        idx += 1
    if idx >= length or buf[idx] != ord(":"):
        return None
    idx += 1
    while idx < length and buf[idx] in b" \t\r\n":
        idx += 1
    if idx >= length or buf[idx] != ord("{"):
        return None
    start = idx
    depth = 0
    in_str = False
    escape = False
    for cursor in range(start, length):
        char = buf[cursor]
        if in_str:
            if escape:
                escape = False
            elif char == 0x5C:  # backslash
                escape = True
            elif char == 0x22:  # quote
                in_str = False
            continue
        if char == 0x22:
            in_str = True
            continue
        if char == 0x7B:  # {
            depth += 1
        elif char == 0x7D:  # }
            depth -= 1
            if depth == 0:
                try:
                    obj = json.loads(buf[start : cursor + 1])
                except json.JSONDecodeError:
                    return None
                if isinstance(obj, dict) and "input_tokens" in obj:
                    return obj
                return None
    return None


def newest_usage_in_bytes(buf: bytes) -> dict[str, Any] | None:
    pos = len(buf)
    while True:
        pos = buf.rfind(USAGE_KEY, 0, pos)
        if pos < 0:
            return None
        usage = _parse_usage_object(buf, pos)
        if usage is not None:
            return usage


def _read_tail(path: Path, nbytes: int) -> bytes:
    if nbytes <= 0:
        return b""
    with path.open("rb") as handle:
        handle.seek(0, os.SEEK_END)
        size = handle.tell()
        take = min(size, nbytes)
        if take <= 0:
            return b""
        handle.seek(size - take)
        return handle.read(take)


def read_newest_usage(path: Path) -> tuple[dict[str, Any] | None, int]:
    """Return (usage, bytes_read). Never scans the whole file."""
    try:
        size = path.stat().st_size
    except OSError:
        return None, 0
    if size <= 0:
        return None, 0
    first = min(size, TAIL_BYTES)
    usage = newest_usage_in_bytes(_read_tail(path, first))
    bytes_read = first
    if usage is not None:
        return usage, bytes_read
    if size <= TAIL_BYTES:
        return None, bytes_read
    second = min(size, WIDEN_BYTES)
    if second <= first:
        return None, bytes_read
    usage = newest_usage_in_bytes(_read_tail(path, second))
    return usage, bytes_read + second


def reading_from_usage(
    usage: dict[str, Any] | None,
    window: int | None,
    *,
    bytes_read: int = 0,
    reason: str | None = None,
) -> Reading:
    if window is None:
        return Reading(
            unknown=True,
            reason=reason or "no-window",
            tokens=None,
            window=None,
            pct=None,
            verdict="unknown",
            band=None,
            bytes_read=bytes_read,
        )
    if usage is None:
        return Reading(
            unknown=True,
            reason=reason or "no-usage",
            tokens=None,
            window=window,
            pct=None,
            verdict="unknown",
            band=None,
            bytes_read=bytes_read,
        )
    tokens = context_tokens(usage)
    pct = (tokens / window) * 100.0
    band = band_for(pct)
    return Reading(
        unknown=False,
        reason=None,
        tokens=tokens,
        window=window,
        pct=pct,
        verdict=verdict_for(pct),
        band=band,
        bytes_read=bytes_read,
    )


def measure_transcript(path: Path, window: int | None) -> Reading:
    if window is None:
        return reading_from_usage(None, None, reason="no-window")
    if not path.is_file():
        return reading_from_usage(None, window, reason="missing-transcript")
    usage, bytes_read = read_newest_usage(path)
    return reading_from_usage(usage, window, bytes_read=bytes_read)


def format_pct(pct: float) -> str:
    rounded = round(pct)
    if abs(pct - rounded) < 0.05:
        return f"{rounded}%"
    return f"{pct:.1f}%"


def reading_to_json(reading: Reading) -> dict[str, Any]:
    return {
        "band": reading.band,
        "bytes_read": reading.bytes_read,
        "pct": reading.pct,
        "reason": reading.reason,
        "tokens": reading.tokens,
        "unknown": reading.unknown,
        "verdict": reading.verdict,
        "window": reading.window,
    }


def render_text(reading: Reading) -> str:
    if reading.unknown or reading.pct is None:
        return "unknown"
    return f"{format_pct(reading.pct)} {reading.verdict}"


def cue_line(reading: Reading, *, today: dt.date | None = None) -> str:
    day = (today or dt.date.today()).isoformat()
    pct = 0 if reading.pct is None else round(reading.pct)
    band = reading.band if reading.band is not None else BANDS[0]
    return (
        f"CONTEXT {pct}% (band {band}): write docs-private/RESUME-NOTES-{day}.md "
        "now and prepare a directed compaction prompt before compaction fires."
    )


def hook_payload(reading: Reading, *, today: dt.date | None = None) -> dict[str, Any]:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PostToolUse",
            "additionalContext": cue_line(reading, today=today),
        }
    }


def default_state_path(transcript: Path, session_id: str | None = None) -> Path:
    explicit = os.environ.get("GOALFLIGHT_CONTEXT_METER_STATE", "").strip()
    if explicit:
        return Path(explicit).expanduser()
    state_dir = os.environ.get("GOALFLIGHT_CONTEXT_METER_STATE_DIR", "").strip()
    if not state_dir:
        base = os.environ.get("GOALFLIGHT_STATE_DIR", "").strip()
        if base:
            state_dir = str(Path(base).expanduser() / "context-meter")
        else:
            uid = os.getuid() if hasattr(os, "getuid") else os.getpid()
            state_dir = f"/tmp/goal-flight-{uid}/context-meter"
    key = (session_id or "").strip() or _path_key(transcript)
    return Path(state_dir) / f"{key}.json"


def _path_key(path: Path) -> str:
    digest = 0
    for byte in str(path).encode("utf-8"):
        digest = (digest * 33 + byte) & 0xFFFFFFFF
    return f"{digest:08x}"


def _is_nonneg_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _state_in_contract(data: object) -> dict[str, Any] | None:
    """Return *data* if it is a usable meter state, else None.

    Parseable-but-wrong values (``calls="x"``, ``last_fired_band=999``) must
    not limp along: they either raise every tick or permanently mute the
    95% cue. Out of contract is treated like an unreadable file — reset
    to empty so the next tick can fail open and re-warn.
    """
    if not isinstance(data, dict):
        return None
    calls = data.get("calls")
    if calls is not None and not _is_nonneg_int(calls):
        return None
    last_checked_calls = data.get("last_checked_calls")
    if last_checked_calls is not None and not _is_nonneg_int(last_checked_calls):
        return None
    last_checked_size = data.get("last_checked_size")
    if last_checked_size is not None and not _is_nonneg_int(last_checked_size):
        return None
    last_fired = data.get("last_fired_band")
    if last_fired is not None and last_fired not in BANDS:
        return None
    last_pct = data.get("last_pct")
    if last_pct is not None and not (
        isinstance(last_pct, (int, float)) and not isinstance(last_pct, bool)
    ):
        return None
    return data


def load_state(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    valid = _state_in_contract(data)
    return {} if valid is None else valid


def save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = dict(state)
    payload.setdefault("schema", STATE_SCHEMA)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def should_recheck(
    state: Mapping[str, Any],
    size: int,
    *,
    every: int | None = None,
    growth: int | None = None,
) -> bool:
    if state.get("last_checked_calls") is None:
        return True
    interval = recheck_every_calls(every)
    growth_limit = recheck_growth_bytes(growth)
    calls_since = int(state.get("calls") or 0) - int(state.get("last_checked_calls") or 0)
    grew = size - int(state.get("last_checked_size") or 0)
    return calls_since >= interval or grew >= growth_limit


def hook_tick(
    payload: Mapping[str, Any],
    *,
    window: object = None,
    model: object = None,
    state_path: str | Path | None = None,
    today: dt.date | None = None,
) -> dict[str, Any] | None:
    """Advance hook state. Return a PostToolUse inject payload or None.

    None is the healthy / unknown / already-fired case. The caller prints
    nothing. A thrown hook is the caller's problem; this function still
    raises on garbage window env so tests can see the validation, while
    ``run_hook_main`` swallows it.
    """
    transcript_raw = payload.get("transcript_path") or os.environ.get(
        "CLAUDE_TRANSCRIPT_PATH"
    )
    if not isinstance(transcript_raw, str) or not transcript_raw.strip():
        return None
    transcript = Path(transcript_raw)
    try:
        size = transcript.stat().st_size
    except OSError:
        return None

    session_id = payload.get("session_id") or payload.get("sessionId")
    session_text = session_id if isinstance(session_id, str) else None
    state_file = (
        Path(state_path) if state_path is not None else default_state_path(transcript, session_text)
    )
    state = load_state(state_file)
    state["calls"] = int(state.get("calls") or 0) + 1

    payload_model = payload.get("model")
    resolved_model = model if model is not None else payload_model
    resolved = resolve_window(window=window, model=resolved_model)
    if resolved is None:
        save_state(state_file, state)
        return None

    if not should_recheck(state, size):
        save_state(state_file, state)
        return None

    reading = measure_transcript(transcript, resolved)
    state["last_checked_calls"] = state["calls"]
    state["last_checked_size"] = size
    if reading.unknown or reading.pct is None:
        save_state(state_file, state)
        return None

    state["last_pct"] = reading.pct
    new_band = reading.band
    if new_band is None:
        save_state(state_file, state)
        return None
    last_fired = state.get("last_fired_band")
    if last_fired is not None and int(new_band) <= int(last_fired):
        save_state(state_file, state)
        return None
    state["last_fired_band"] = new_band
    save_state(state_file, state)
    return hook_payload(reading, today=today)


def run_hook_main(args: argparse.Namespace) -> int:
    try:
        raw = sys.stdin.read()
        try:
            payload = json.loads(raw or "{}")
        except json.JSONDecodeError:
            return 0
        if not isinstance(payload, dict):
            return 0
        out = hook_tick(
            payload,
            window=args.window,
            model=args.model,
            state_path=args.state,
        )
        if out:
            print(json.dumps(out, separators=(",", ":")))
    except Exception:
        return 0
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Report how full a Claude session context window is."
    )
    parser.add_argument("--transcript", help="Session transcript JSONL path")
    parser.add_argument(
        "--window",
        help="Context window in tokens. Required for a numeric reading; never defaulted.",
    )
    parser.add_argument(
        "--model",
        help="Optional model id. Unrecognized models yield unknown, not a guessed window.",
    )
    parser.add_argument("--json", action="store_true", help="JSON object on stdout")
    parser.add_argument("--hook", action="store_true", help="PostToolUse hook mode (stdin JSON)")
    parser.add_argument("--state", help="Hook state file (tests / override)")
    return parser


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = _build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        if "--hook" in argv:
            return 0
        raise exc
    if args.hook:
        return run_hook_main(args)
    if not args.transcript:
        if "--hook" in argv:
            return 0
        parser.error("--transcript is required")
    try:
        window = resolve_window(window=args.window, model=args.model)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if args.window is None and args.model and window is None:
        reading = Reading(
            unknown=True,
            reason="unrecognized-model",
            tokens=None,
            window=None,
            pct=None,
            verdict="unknown",
            band=None,
            bytes_read=0,
        )
    else:
        reading = measure_transcript(Path(args.transcript), window)
        if window is None:
            reading = reading_from_usage(
                None, None, bytes_read=reading.bytes_read, reason="no-window"
            )
    if args.json:
        print(json.dumps(reading_to_json(reading), sort_keys=True))
    else:
        print(render_text(reading))
    return 0


if __name__ == "__main__":
    sys.exit(main())
