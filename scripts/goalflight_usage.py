#!/usr/bin/env python3
"""Render provider headroom from optional local usage readers."""

from __future__ import annotations

import argparse
import json
import math
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping, Sequence


DEFAULT_TIMEOUT_S = 20.0
DEFAULT_READERS_DIR = Path(__file__).resolve().parent / "ext"


@dataclass(frozen=True)
class ReaderSpec:
    key: str
    provider: str
    filename: str


READERS = (
    ReaderSpec("codex", "codex", "codex_usage.py"),
    ReaderSpec("kimi", "kimi-code", "kimi_usage.py"),
    ReaderSpec("cursor", "cursor", "cursor_usage.py"),
    ReaderSpec("claude", "claude", "claude_usage.py"),
)

ROW_KEYS = ("provider", "account", "remaining", "reset_at", "flags")
AUTH_MARKERS = (
    "auth",
    "credential",
    "login",
    "token",
    "http 401",
    "http 403",
)
FLAG_TEXT = {
    "walled": "⛔wall",
    "auth-broken": "⚠auth",
    "unavailable": "⚠unavailable",
}


def _number(value: object) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        parsed = float(value)
    elif isinstance(value, str):
        text = value.strip()
        if text.endswith("%"):
            text = text[:-1].strip()
        try:
            parsed = float(text)
        except ValueError:
            return None
    else:
        return None
    return parsed if math.isfinite(parsed) else None


def _format_number(value: float) -> str:
    if value.is_integer():
        return str(int(value))
    return f"{value:.1f}".rstrip("0").rstrip(".")


def _label(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = " ".join(value.split())
    return cleaned[:80] or None


def parse_reset(value: object) -> float | None:
    """Normalize epoch seconds or an ISO timestamp to epoch seconds."""
    numeric = _number(value)
    if numeric is not None:
        if abs(numeric) >= 100_000_000_000:
            numeric /= 1000
        return numeric
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    if text.endswith(("Z", "z")):
        text = f"{text[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            parsed = parsed.astimezone()
        return parsed.timestamp()
    except (OverflowError, ValueError):
        return None


def _reset_candidates(mapping: Mapping[str, Any] | None) -> list[float]:
    if not isinstance(mapping, Mapping):
        return []
    values = []
    for key in (
        "reset_at",
        "resets_at",
        "resetTime",
        "reset_time",
        "session_reset_at",
        "weekly_reset_at",
        "weekly_sonnet_reset_at",
    ):
        parsed = parse_reset(mapping.get(key))
        if parsed is not None:
            values.append(parsed)
    return values


def _window_reset(usage: Mapping[str, Any]) -> float | None:
    windows = usage.get("windows")
    if not isinstance(windows, list):
        return None
    candidates = [
        parsed
        for window in windows
        if isinstance(window, Mapping)
        for parsed in _reset_candidates(window)
    ]
    return min(candidates) if candidates else None


def _row(
    provider: str,
    *,
    account: str | None,
    remaining: str,
    reset_at: float | None,
    flags: Sequence[str] = (),
) -> dict[str, object]:
    return {
        "provider": provider,
        "account": account,
        "remaining": remaining,
        "reset_at": reset_at,
        "flags": list(dict.fromkeys(flags)),
    }


def unavailable_row(provider: str) -> dict[str, object]:
    return _row(
        provider,
        account=None,
        remaining="unavailable",
        reset_at=None,
        flags=("unavailable",),
    )


def _failed_record(record: Mapping[str, Any]) -> tuple[str, str] | None:
    if record.get("ok") is not False:
        return None
    error = record.get("error")
    lowered = error.lower() if isinstance(error, str) else ""
    if any(marker in lowered for marker in AUTH_MARKERS):
        return "needs-login", "auth-broken"
    return "unavailable", "unavailable"


def _percent_remaining(used_percent: float) -> float:
    return min(100.0, max(0.0, 100.0 - used_percent))


def _usage_remaining(
    usage: object,
) -> tuple[str | None, float | None, float | None]:
    """Return display text, numeric remaining, and numeric used percent."""
    if isinstance(usage, str):
        return _label(usage), None, None
    if not isinstance(usage, Mapping):
        return None, None, None

    remaining = _number(usage.get("remaining"))
    limit = _number(usage.get("limit"))
    remaining_percent = _number(usage.get("remaining_percent"))
    used_percent = _number(usage.get("used_percent"))
    unit = _label(usage.get("unit"))

    if remaining is not None and limit is not None:
        return (
            f"{_format_number(remaining)}/{_format_number(limit)}",
            remaining,
            used_percent,
        )
    if remaining_percent is not None:
        return f"{_format_number(remaining_percent)}%", remaining_percent, used_percent
    if used_percent is not None:
        computed = _percent_remaining(used_percent)
        return f"{_format_number(computed)}%", computed, used_percent
    if remaining is not None and unit:
        return f"{_format_number(remaining)} {unit}", remaining, used_percent
    if remaining is not None:
        return _format_number(remaining), remaining, used_percent
    return None, None, used_percent


def _normalize_codex(record: Mapping[str, Any], now: float) -> dict[str, object]:
    del now
    account = _label(record.get("seat"))
    reset_at = parse_reset(record.get("reset_at"))
    failure = _failed_record(record)
    if failure is not None:
        remaining, flag = failure
        return _row(
            "codex",
            account=account,
            remaining=remaining,
            reset_at=reset_at,
            flags=(flag,),
        )

    used = _number(record.get("used_percent"))
    if used is None:
        return _row(
            "codex",
            account=account,
            remaining="unknown",
            reset_at=reset_at,
        )
    remaining_value = _percent_remaining(used)
    flags = ("walled",) if used >= 100 or remaining_value <= 0 else ()
    return _row(
        "codex",
        account=account,
        remaining=f"{_format_number(remaining_value)}%",
        reset_at=reset_at,
        flags=flags,
    )


def _normalize_kimi(record: Mapping[str, Any], now: float) -> dict[str, object]:
    del now
    source = record.get("source")
    label = _label(record.get("label"))
    account = None
    if label not in (None, "kimi", "kimi-code") or source != "kimi_code_usages":
        account = label

    usage = record.get("usage")
    usage_mapping = usage if isinstance(usage, Mapping) else None
    reset_at = None
    if usage_mapping is not None:
        resets = _reset_candidates(usage_mapping)
        reset_at = min(resets) if resets else _window_reset(usage_mapping)
    if reset_at is None:
        resets = _reset_candidates(record)
        reset_at = min(resets) if resets else None

    failure = _failed_record(record)
    if failure is not None:
        remaining, flag = failure
        return _row(
            "kimi-code",
            account=account,
            remaining=remaining,
            reset_at=reset_at,
            flags=(flag,),
        )

    remaining, remaining_value, used = _usage_remaining(usage)
    flags = ()
    if (remaining_value is not None and remaining_value <= 0) or (
        used is not None and used >= 100
    ):
        flags = ("walled",)
    return _row(
        "kimi-code",
        account=account,
        remaining=remaining or "unknown",
        reset_at=reset_at,
        flags=flags,
    )


def _normalize_cursor(record: Mapping[str, Any], now: float) -> dict[str, object]:
    del now
    label = _label(record.get("label"))
    account = label if label not in (None, "cursor") else None
    usage = record.get("usage")
    usage_mapping = usage if isinstance(usage, Mapping) else None
    resets = _reset_candidates(usage_mapping) + _reset_candidates(record)
    reset_at = min(resets) if resets else None

    failure = _failed_record(record)
    if failure is not None:
        remaining, flag = failure
        return _row(
            "cursor",
            account=account,
            remaining=remaining,
            reset_at=reset_at,
            flags=(flag,),
        )

    remaining, remaining_value, used = _usage_remaining(usage)
    if remaining is None:
        remaining = _label(record.get("note")) or "unknown"
    flags = ()
    if (remaining_value is not None and remaining_value <= 0) or (
        used is not None and used >= 100
    ):
        flags = ("walled",)
    return _row(
        "cursor",
        account=account,
        remaining=remaining,
        reset_at=reset_at,
        flags=flags,
    )


def _first_numeric(*values: object) -> float | None:
    for value in values:
        parsed = _number(value)
        if parsed is not None:
            return parsed
    return None


def _normalize_claude(record: Mapping[str, Any], now: float) -> dict[str, object]:
    account = _label(record.get("label"))
    usage = record.get("usage")
    usage_mapping = usage if isinstance(usage, Mapping) else {}
    resets = _reset_candidates(record) + _reset_candidates(usage_mapping)
    reset_at = min(resets) if resets else None
    cooldown_s = _number(record.get("cooldown_s"))
    if reset_at is None and cooldown_s is not None and cooldown_s > 0:
        reset_at = now + cooldown_s

    if record.get("logged_in") is False:
        return _row(
            "claude",
            account=account,
            remaining="needs-login",
            reset_at=reset_at,
            flags=("auth-broken",),
        )
    if record.get("logged_in") is None and record.get("error"):
        failure = _failed_record({**record, "ok": False})
        assert failure is not None
        remaining, flag = failure
        return _row(
            "claude",
            account=account,
            remaining=remaining,
            reset_at=reset_at,
            flags=(flag,),
        )
    failure = _failed_record(record)
    if failure is not None:
        remaining, flag = failure
        return _row(
            "claude",
            account=account,
            remaining=remaining,
            reset_at=reset_at,
            flags=(flag,),
        )

    parts = []
    walled = False
    for title, key in (
        ("session", "session"),
        ("week", "weekly"),
        ("sonnet", "weekly_sonnet"),
    ):
        remaining_percent = _first_numeric(
            record.get(f"{key}_remaining_percent"),
            usage_mapping.get(f"{key}_remaining_percent"),
        )
        used_percent = _first_numeric(
            record.get(f"{key}_used_percent"),
            usage_mapping.get(f"{key}_used_percent"),
        )
        nested = usage_mapping.get(key)
        if isinstance(nested, Mapping):
            nested_text, nested_remaining, nested_used = _usage_remaining(nested)
            if remaining_percent is None and used_percent is None and nested_text:
                parts.append(f"{title} {nested_text}")
                walled = walled or (
                    nested_remaining is not None and nested_remaining <= 0
                )
                walled = walled or (nested_used is not None and nested_used >= 100)
                continue
        if remaining_percent is None and used_percent is not None:
            remaining_percent = _percent_remaining(used_percent)
        if remaining_percent is None:
            continue
        parts.append(f"{title} {_format_number(remaining_percent)}%")
        walled = walled or remaining_percent <= 0 or (
            used_percent is not None and used_percent >= 100
        )

    if parts:
        remaining = ", ".join(parts)
    else:
        remaining, remaining_value, used = _usage_remaining(usage)
        remaining = remaining or "unknown"
        walled = walled or (
            remaining_value is not None and remaining_value <= 0
        )
        walled = walled or (used is not None and used >= 100)
    return _row(
        "claude",
        account=account,
        remaining=remaining,
        reset_at=reset_at,
        flags=("walled",) if walled else (),
    )


NORMALIZERS = {
    "codex": _normalize_codex,
    "kimi": _normalize_kimi,
    "cursor": _normalize_cursor,
    "claude": _normalize_claude,
}


def normalize_payload(
    spec: ReaderSpec,
    payload: object,
    *,
    now: float | None = None,
) -> list[dict[str, object]]:
    """Normalize one reader payload, accepting a single mapping as drift."""
    current_time = time.time() if now is None else now
    if isinstance(payload, Mapping):
        records = [payload]
    elif isinstance(payload, list):
        records = payload
    else:
        return [unavailable_row(spec.provider)]

    normalizer = NORMALIZERS[spec.key]
    rows = [
        normalizer(record, current_time)
        for record in records
        if isinstance(record, Mapping)
    ]
    return rows or [unavailable_row(spec.provider)]


def run_reader(
    spec: ReaderSpec,
    *,
    readers_dir: Path = DEFAULT_READERS_DIR,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    now: float | None = None,
) -> list[dict[str, object]]:
    """Run one optional reader; every failure becomes one unavailable row."""
    reader_path = readers_dir / spec.filename
    try:
        if not reader_path.is_file():
            return [unavailable_row(spec.provider)]
        completed = subprocess.run(
            [sys.executable, str(reader_path), "--json"],
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
        )
        if completed.returncode != 0:
            return [unavailable_row(spec.provider)]
        payload = json.loads(completed.stdout)
        return normalize_payload(spec, payload, now=now)
    except (OSError, subprocess.TimeoutExpired, UnicodeError, ValueError):
        return [unavailable_row(spec.provider)]
    except Exception:
        return [unavailable_row(spec.provider)]


def collect_usage(
    *,
    readers_dir: Path = DEFAULT_READERS_DIR,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    reader_specs: Sequence[ReaderSpec] = READERS,
    now: float | None = None,
) -> list[dict[str, object]]:
    current_time = time.time() if now is None else now
    rows = []
    for spec in reader_specs:
        rows.extend(
            run_reader(
                spec,
                readers_dir=readers_dir,
                timeout_s=timeout_s,
                now=current_time,
            )
        )
    return rows


def humanize_delta(seconds: float) -> str:
    seconds = max(0.0, seconds)
    minutes = seconds / 60
    if minutes < 90:
        return f"{int(minutes)}m"
    hours = seconds / 3600
    if hours < 48:
        return f"{hours:.1f}h"
    return f"{hours / 24:.1f}d"


def _local_reset(reset_at: float) -> str:
    try:
        return datetime.fromtimestamp(reset_at).astimezone().strftime("%b %d %H:%M")
    except (OSError, OverflowError, ValueError):
        return "—"


def _provider_account(row: Mapping[str, object]) -> str:
    provider = str(row.get("provider") or "unknown")
    account = _label(row.get("account"))
    return f"{provider} {account}" if account else provider


def soonest_reset(
    rows: Sequence[Mapping[str, object]],
    *,
    now: float | None = None,
) -> Mapping[str, object] | None:
    current_time = time.time() if now is None else now
    upcoming = []
    for row in rows:
        reset_at = parse_reset(row.get("reset_at"))
        if reset_at is not None and reset_at > current_time:
            upcoming.append((reset_at, row))
    return min(upcoming, key=lambda item: item[0])[1] if upcoming else None


def render_table(
    rows: Sequence[Mapping[str, object]],
    *,
    now: float | None = None,
) -> str:
    current_time = time.time() if now is None else now
    headers = ("PROVIDER/ACCOUNT", "REMAINING", "RESETS (local HH:MM)")
    display_rows = []
    for row in rows:
        flags = row.get("flags")
        flag_text = ""
        if isinstance(flags, list):
            rendered = [FLAG_TEXT[flag] for flag in flags if flag in FLAG_TEXT]
            if rendered:
                flag_text = f"  {' '.join(rendered)}"
        remaining = f"{row.get('remaining') or 'unknown'}{flag_text}"
        reset_at = parse_reset(row.get("reset_at"))
        reset_text = "—"
        if reset_at is not None:
            local = _local_reset(reset_at)
            reset_text = f"{local}  ({humanize_delta(reset_at - current_time)})"
        display_rows.append((_provider_account(row), remaining, reset_text))

    widths = [
        max([len(headers[index]), *(len(row[index]) for row in display_rows)])
        for index in range(len(headers))
    ]
    lines = [
        "  ".join(value.ljust(widths[index]) for index, value in enumerate(headers))
    ]
    lines.extend(
        "  ".join(value.ljust(widths[index]) for index, value in enumerate(row))
        for row in display_rows
    )
    lines.append("---")

    soonest = soonest_reset(rows, now=current_time)
    if soonest is None:
        lines.append("soonest reset: none")
    else:
        reset_at = parse_reset(soonest.get("reset_at"))
        assert reset_at is not None
        lines.append(
            f"soonest reset: {_provider_account(soonest)} "
            f"in {humanize_delta(reset_at - current_time)} "
            f"({_local_reset(reset_at)})"
        )
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Show provider headroom and the soonest upcoming reset."
    )
    parser.add_argument("--json", action="store_true", help="emit normalized JSON rows")
    parser.add_argument(
        "--readers-dir",
        type=Path,
        default=DEFAULT_READERS_DIR,
        help=argparse.SUPPRESS,
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    now = time.time()
    rows = collect_usage(readers_dir=args.readers_dir, now=now)
    if args.json:
        print(json.dumps(rows, indent=2))
    else:
        print(render_table(rows, now=now))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
