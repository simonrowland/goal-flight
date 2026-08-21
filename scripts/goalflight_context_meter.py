#!/usr/bin/env python3
"""Cheap context-window meter from a Claude session transcript tail.

The newest assistant ``usage`` block carries the live context size:

    input_tokens + cache_read_input_tokens + cache_creation_input_tokens

A full JSONL scan is the slow path (transcripts run to many MB). This
reader seeks to EOF and inspects a bounded tail. If that tail has no
usage block it widens once, then reports unknown. The same tail also
carries ``message.model``; that is how a reading can exist with no
export. An unrecognized model stays unknown, not a guessed number.

Window precedence, stated once so it cannot drift: explicit ``--window``
> ``GOALFLIGHT_CONTEXT_WINDOW`` > model map > unknown/silent.

If observed tokens exceed the resolved window the map (or override) is
too small. That contradiction is reported as ``stale-window``, never as
a confident percent over 100.

The hook stays silent below 80% and fires each band (80 → 90 → 95) once.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
from dataclasses import dataclass
from pathlib import Path
import re
import sys
from typing import Any, Mapping


TAIL_BYTES = 64 * 1024
# One retry only, still O(1). Unbounded widening recreates a full scan.
WIDEN_BYTES = 1024 * 1024
BANDS = (80, 90, 95)
RECHECK_EVERY_CALLS = 20
RECHECK_GROWTH_BYTES = 1024 * 1024
# Only the two Claude Code models this fleet actually runs. Unlisted
# models (sonnet, haiku, anything else) stay unknown. A too-large
# unverified entry is the silent under-report this meter exists to
# prevent; do not add one.
KNOWN_MODEL_WINDOWS: dict[str, int] = {
    # operator directive 2026-08-17 about this fleet, not a vendor guarantee
    "claude-opus-5": 1_000_000,
    # operator directive 2026-08-17 about this fleet, not a vendor guarantee
    "claude-fable-5": 1_000_000,
}
STATE_SCHEMA = "goalflight.context-meter.v1"
USAGE_KEY = b'"usage"'
_MODEL_STRING = re.compile(rb'"model"\s*:\s*"([^"\\]+)"')


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
    model: str | None = None


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
    There is no numeric default: unknown stays silent. Precedence lives
    in ``resolve_window`` / ``measure_transcript``.
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


def _model_key(model: object = None) -> str | None:
    raw = model
    if raw is None:
        raw = os.environ.get("GOALFLIGHT_CONTEXT_MODEL")
    if raw is None:
        return None
    key = str(raw).strip()
    return key or None


def classify_model(model: object = None) -> tuple[str, int | None]:
    """Classify a model id against the fleet map.

    Match policy (evidence, not preference): exact key, or that key plus
    a ``YYYYMMDD`` date suffix. Dated ids are a real shape in this
    ecosystem (``claude-haiku-4-5-20251001``). Opus and Fable are
    unsuffixed today; a future ``claude-opus-5-20260501`` must keep
    resolving or the meter goes quietly dark — close to the defect we
    are preventing.

    A naive ``startswith`` would also swallow ``claude-opus-5-mini`` (or
    any future smaller-window variant). That is the dangerous
    too-large direction. So only an 8-digit date suffix inherits the
    family window. Any other suffix on a known family is
    ``unmapped-variant``: observable, no number. Unknown families stay
    silent.
    """
    key = _model_key(model)
    if key is None:
        return "absent", None
    mapped = KNOWN_MODEL_WINDOWS.get(key)
    if mapped is None:
        mapped = KNOWN_MODEL_WINDOWS.get(key.lower())
    if mapped is not None:
        return "mapped", int(mapped)
    lower = key.lower()
    for family, window in KNOWN_MODEL_WINDOWS.items():
        prefix = family.lower() + "-"
        if not lower.startswith(prefix):
            continue
        suffix = lower[len(prefix) :]
        if len(suffix) == 8 and suffix.isdigit():
            return "dated-family", int(window)
        return "unmapped-variant", None
    return "unknown", None


def window_for_model(model: object = None) -> int | None:
    """Fail-closed model → window lookup. Unrecognized is unknown, not 200k."""
    _kind, mapped = classify_model(model)
    return mapped


def resolve_window(*, window: object = None, model: object = None) -> int | None:
    """explicit --window > env > model map > unknown/silent.

    Does not read a transcript. ``measure_transcript`` applies the same
    order and then the newest assistant ``message.model`` as the model
    source when neither an explicit window nor an explicit model is set.
    """
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


def _clean_model(value: object) -> str | None:
    if isinstance(value, str):
        text = value.strip()
        if text:
            return text
    return None


def _model_from_obj(obj: object) -> str | None:
    if not isinstance(obj, dict):
        return None
    message = obj.get("message")
    if isinstance(message, dict):
        found = _clean_model(message.get("model"))
        if found is not None:
            return found
    return _clean_model(obj.get("model"))


def _last_model_string(raw: bytes) -> str | None:
    found = None
    for match in _MODEL_STRING.finditer(raw):
        text = match.group(1).decode("ascii", errors="ignore").strip()
        if text:
            found = text
    return found


def _model_in_record(buf: bytes, usage_at: int) -> str | None:
    """Model on the JSONL record that contains ``usage`` at *usage_at*.

    Same buffer as the usage parse — no extra read. A truncated line
    falls back to the last ``"model":"..."`` before the usage key.
    """
    line_start = buf.rfind(b"\n", 0, usage_at) + 1
    line_end = buf.find(b"\n", usage_at)
    if line_end < 0:
        line_end = len(buf)
    raw = buf[line_start:line_end]
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError:
        return _last_model_string(raw)
    return _model_from_obj(obj)


def newest_assistant_in_bytes(
    buf: bytes,
) -> tuple[dict[str, Any], str | None] | None:
    """Newest parseable usage in *buf*, plus that record's model if present."""
    pos = len(buf)
    while True:
        pos = buf.rfind(USAGE_KEY, 0, pos)
        if pos < 0:
            return None
        usage = _parse_usage_object(buf, pos)
        if usage is not None:
            return usage, _model_in_record(buf, pos)


# Removed newest_usage_in_bytes: its model-blind result could mis-size the active context window.


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


def read_newest_assistant(
    path: Path,
) -> tuple[dict[str, Any] | None, str | None, int]:
    """Return (usage, model, bytes_read). Never scans the whole file.

    Model is taken from the same tail that produced usage. Finding usage
    in the first tail does not widen just to chase a model — that would
    change the O(1) bound.
    """
    try:
        size = path.stat().st_size
    except OSError:
        return None, None, 0
    if size <= 0:
        return None, None, 0
    first = min(size, TAIL_BYTES)
    found = newest_assistant_in_bytes(_read_tail(path, first))
    bytes_read = first
    if found is not None:
        return found[0], found[1], bytes_read
    if size <= TAIL_BYTES:
        return None, None, bytes_read
    second = min(size, WIDEN_BYTES)
    if second <= first:
        return None, None, bytes_read
    found = newest_assistant_in_bytes(_read_tail(path, second))
    bytes_read = bytes_read + second
    if found is None:
        return None, None, bytes_read
    return found[0], found[1], bytes_read


def read_newest_usage(path: Path) -> tuple[dict[str, Any] | None, int]:
    """Return (usage, bytes_read). Never scans the whole file."""
    usage, _model, bytes_read = read_newest_assistant(path)
    return usage, bytes_read


def reading_from_usage(
    usage: dict[str, Any] | None,
    window: int | None,
    *,
    bytes_read: int = 0,
    reason: str | None = None,
    model: str | None = None,
) -> Reading:
    if window is None:
        tokens = context_tokens(usage) if usage is not None else None
        verdict = "unknown"
        if reason == "unmapped-variant":
            verdict = "unmapped-variant"
        elif reason == "stale-window":
            verdict = "stale-window"
        return Reading(
            unknown=True,
            reason=reason or "no-window",
            tokens=tokens if reason == "unmapped-variant" else None,
            window=None,
            pct=None,
            verdict=verdict,
            band=None,
            bytes_read=bytes_read,
            model=model,
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
            model=model,
        )
    tokens = context_tokens(usage)
    if tokens > window:
        # Too-small window (stale map or bad override). A confident
        # 340% is nonsensical; name the contradiction instead.
        return Reading(
            unknown=True,
            reason="stale-window",
            tokens=tokens,
            window=window,
            pct=None,
            verdict="stale-window",
            band=None,
            bytes_read=bytes_read,
            model=model,
        )
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
        model=model,
    )


def _missing_window_reason(model: object) -> str:
    kind, _window = classify_model(model)
    if kind == "absent":
        return "no-window"
    if kind == "unmapped-variant":
        return "unmapped-variant"
    return "unrecognized-model"


def measure_transcript(
    path: Path,
    window: int | None = None,
    *,
    model: object = None,
) -> Reading:
    """Measure a transcript.

    *window* is an already-chosen token count, or None to resolve via
    env / *model* / the newest assistant record's ``message.model``.
    """
    resolved = resolve_window(window=window, model=model)
    if not path.is_file():
        source = _clean_model(model)
        if resolved is None:
            return reading_from_usage(
                None,
                None,
                reason=_missing_window_reason(model),
                model=source,
            )
        return reading_from_usage(
            None, resolved, reason="missing-transcript", model=source
        )
    usage, found_model, bytes_read = read_newest_assistant(path)
    if resolved is None and model is None:
        resolved = window_for_model(found_model)
    source_model = _clean_model(model if model is not None else found_model)
    if resolved is None:
        source = model if model is not None else found_model
        return reading_from_usage(
            usage,
            None,
            bytes_read=bytes_read,
            reason=_missing_window_reason(source),
            model=source_model,
        )
    return reading_from_usage(
        usage, resolved, bytes_read=bytes_read, model=source_model
    )


def format_pct(pct: float) -> str:
    rounded = round(pct)
    if abs(pct - rounded) < 0.05:
        return f"{rounded}%"
    return f"{pct:.1f}%"


def reading_to_json(reading: Reading) -> dict[str, Any]:
    return {
        "band": reading.band,
        "bytes_read": reading.bytes_read,
        "model": reading.model,
        "pct": reading.pct,
        "reason": reading.reason,
        "tokens": reading.tokens,
        "unknown": reading.unknown,
        "verdict": reading.verdict,
        "window": reading.window,
    }


def render_text(reading: Reading) -> str:
    if reading.reason == "stale-window":
        return (
            f"stale-window: {reading.tokens} tokens exceed "
            f"window {reading.window}"
        )
    if reading.reason == "unmapped-variant":
        label = reading.model or "unknown-id"
        return f"unmapped-variant: {label}"
    if reading.unknown or reading.pct is None:
        return "unknown"
    return f"{format_pct(reading.pct)} {reading.verdict}"


def cue_line(reading: Reading, *, today: dt.date | None = None) -> str:
    day = (today or dt.date.today()).isoformat()
    if reading.reason == "stale-window":
        return (
            f"CONTEXT stale-window: {reading.tokens} tokens exceed "
            f"window {reading.window}. The resolved window is too small; "
            f"write docs-private/RESUME-NOTES-{day}.md now and do not trust a percent."
        )
    if reading.reason == "unmapped-variant":
        label = reading.model or "unknown-id"
        return (
            f"CONTEXT unmapped-variant: {label} is a known family without a "
            "mapped window (only exact ids and YYYYMMDD suffixes resolve). "
            "Meter will not emit a percent."
        )
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

    # Shell owns the 1-in-20 spawn. Once Python is running, measure:
    # band de-dupe needs the reading, and growth is visible in size.
    reading = measure_transcript(transcript, window, model=resolved_model)
    state["last_checked_calls"] = state["calls"]
    state["last_checked_size"] = size
    if reading.reason in {"stale-window", "unmapped-variant"}:
        flag = f"{reading.reason}_fired"
        if state.get(flag):
            save_state(state_file, state)
            return None
        state[flag] = True
        save_state(state_file, state)
        return hook_payload(reading, today=today)
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
        help="Context window in tokens. Overrides env and the model map.",
    )
    parser.add_argument(
        "--model",
        help="Optional model id. Overrides the transcript model. Unrecognized is unknown.",
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
        explicit = context_window(args.window)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    reading = measure_transcript(
        Path(args.transcript), explicit, model=args.model
    )
    if args.json:
        print(json.dumps(reading_to_json(reading), sort_keys=True))
    else:
        print(render_text(reading))
    return 0


if __name__ == "__main__":
    sys.exit(main())
