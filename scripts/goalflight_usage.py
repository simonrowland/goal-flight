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

import goalflight_dispatch_states
import goalflight_ledger
import goalflight_rate_pressure


DEFAULT_TIMEOUT_S = 20.0
# --deep opens a real TUI per Claude account. A measured three-account sweep runs
# ~120s, but per-capture time is highly variable and a capture can overrun its
# own nominal cap, so a budget sized to the typical case turns a slow run into a
# fake failure. This bound exists only to stop a genuine hang, not to pace the
# work - size it far above the observed worst case.
DEEP_TIMEOUT_S = 900.0
DEFAULT_READERS_DIR = Path(__file__).resolve().parent / "ext"


@dataclass(frozen=True)
class ReaderSpec:
    key: str
    provider: str
    filename: str
    # extra_args: the claude sweep defaults to full TUI captures, which exceed the
    # aggregator's per-reader timeout; the table only needs the fast login-health
    # pass (run the reader directly for full numbers).
    extra_args: tuple = ()
    # deep_args: what --deep passes instead, for readers whose headroom and reset
    # numbers are only reachable through a slow capture. None keeps extra_args.
    deep_args: tuple | None = None

    def args_for(self, deep: bool) -> tuple:
        if deep and self.deep_args is not None:
            return self.deep_args
        return self.extra_args


READERS = (
    ReaderSpec("codex", "codex", "codex_usage.py"),
    ReaderSpec("grok", "grok", "grok_usage.py"),
    # The ext-zone reader (scripts/ext/kimi_usage.py) and its payload contract
    # (source "kimi_code_usages", label "kimi-code") are not ours to rename; the
    # dispatch handle is "moonshot", so the DISPLAY label maps at this boundary.
    ReaderSpec("kimi", "moonshot", "kimi_usage.py"),
    ReaderSpec("cursor", "cursor", "cursor_usage.py"),
    # QUARANTINED: no deep variant. Letting the claude reader run its full TUI
    # capture is not merely slow, it is unsafe - the capture does not reliably
    # isolate per account. Observed 2026-07-27: a sweep left the 'work' label
    # resolving to the live account instead of its own, and the sweep's
    # sync-back then propagates that into the label's stored backup, so one
    # account's credential silently replaces another's. It also never yields the
    # reset text it exists to collect (percentages parse, reset lines do not).
    # Restore deep_args=() only once per-account isolation is proven.
    ReaderSpec("claude", "claude", "claude_usage.py", ("--skip-tui",)),
)

ROW_KEYS = ("provider", "account", "remaining", "reset_at", "flags")
REPORT_ROW_KEYS = ROW_KEYS + ("evidence",)
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
    "timeout": "⚠timeout",
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


def timed_out_row(provider: str) -> dict[str, object]:
    """A reader that ran out of budget measured nothing - it did not measure bad.

    Rendering that as "unavailable" is the same mistake as reporting a missing
    binary as a version mismatch: it points the reader at the account when the
    fault is in the harness, and the account then gets debugged for hours.
    """
    return _row(
        provider,
        account=None,
        remaining="timed out",
        reset_at=None,
        flags=("timeout",),
    )


def _failed_record(record: Mapping[str, Any]) -> tuple[str, str] | None:
    if record.get("ok") is not False:
        return None
    error = record.get("error")
    lowered = error.lower() if isinstance(error, str) else ""
    # A lapsed token with a live refresh grant auto-heals on the provider
    # CLI's next use — real headroom state unknown, but no human login needed.
    if lowered.startswith("token_lapsed"):
        return "lapsed (auto-heals)", ""
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


def _normalize_grok(record: Mapping[str, Any], now: float) -> dict[str, object]:
    """One subscription credit pool, shaped like a codex seat window.

    A reader failure keeps its reason rather than resolving to a percentage:
    the backing endpoint is undocumented, so a contract change must read as
    "could not measure", never as full headroom.
    """
    del now
    reset_at = parse_reset(record.get("reset_at"))
    failure = _failed_record(record)
    if failure is not None:
        remaining, flag = failure
        return _row(
            "grok",
            account=None,
            remaining=remaining,
            reset_at=reset_at,
            flags=(flag,) if flag else (),
        )

    used = _number(record.get("used_percent"))
    if used is None:
        return _row("grok", account=None, remaining="unknown", reset_at=reset_at)
    remaining_value = _percent_remaining(used)
    flags = ("walled",) if used >= 100 or remaining_value <= 0 else ()
    return _row(
        "grok",
        account=None,
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
            "moonshot",
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
        "moonshot",
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


# The claude reader emits one record per saved account label, each carrying its
# own "login_status": "ok", "pending" (saved but not materialized yet),
# "expired"/"logged out", "error (<stage>)", or "unknown".  Every record becomes
# its own table row so a healthy label is never masked by a stale sibling.
CLAUDE_LOGGED_OUT_STATES = ("expired", "logged out", "logged-out")
CLAUDE_PENDING_STATES = ("pending",)


def _claude_login_status(record: Mapping[str, Any]) -> str:
    status = _label(record.get("login_status"))
    return status.lower() if status else ""


def _normalize_claude(record: Mapping[str, Any], now: float) -> dict[str, object]:
    account = _label(record.get("label"))
    status = _claude_login_status(record)
    usage = record.get("usage")
    usage_mapping = usage if isinstance(usage, Mapping) else {}
    resets = _reset_candidates(record) + _reset_candidates(usage_mapping)
    reset_at = min(resets) if resets else None
    cooldown_s = _number(record.get("cooldown_s"))
    if reset_at is None and cooldown_s is not None and cooldown_s > 0:
        reset_at = now + cooldown_s

    if record.get("logged_in") is False or status in CLAUDE_LOGGED_OUT_STATES:
        return _row(
            "claude",
            account=account,
            remaining="needs-login",
            reset_at=reset_at,
            flags=("auth-broken",),
        )
    # Pending is a saved label whose credentials have not been materialized yet:
    # honestly distinct from "unknown" (indeterminate) and "unavailable" (the
    # reader itself failed), so it is reported before any error treatment.
    if status in CLAUDE_PENDING_STATES:
        return _row(
            "claude",
            account=account,
            remaining="pending",
            reset_at=reset_at,
        )
    if status.startswith("error") or (
        record.get("logged_in") is None and record.get("error")
    ):
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
        if not remaining:
            # A login the reader confirmed is healthy but carries no usage
            # numbers (the fast --skip-tui pass) is "ok", not "unknown" - the
            # login state IS known, only the percentages are missing.
            healthy = status == "ok" or record.get("logged_in") is True
            remaining = "ok" if healthy else "unknown"
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
    "grok": _normalize_grok,
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
    rows = []
    for record in records:
        if not isinstance(record, Mapping):
            continue
        row = normalizer(record, current_time)
        for key in ("probed_at", "measured_at", "checked_at", "updated_at"):
            observed_at = parse_reset(record.get(key))
            if observed_at is not None:
                row["_probed_at"] = observed_at
                break
        rows.append(row)
    return rows or [unavailable_row(spec.provider)]


def run_reader(
    spec: ReaderSpec,
    *,
    readers_dir: Path = DEFAULT_READERS_DIR,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    now: float | None = None,
    deep: bool = False,
) -> list[dict[str, object]]:
    """Run one optional reader; every failure becomes one unavailable row."""
    reader_path = readers_dir / spec.filename
    try:
        if not reader_path.is_file():
            return [unavailable_row(spec.provider)]
        completed = subprocess.run(
            [sys.executable, str(reader_path), "--json", *spec.args_for(deep)],
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
        )
        # A nonzero exit with parseable rows is a reader REPORTING problems
        # (stale logins, cooling seats), not failing to report - keep the rows.
        try:
            payload = json.loads(completed.stdout)
        except (json.JSONDecodeError, ValueError):
            return [unavailable_row(spec.provider)]
        if completed.returncode != 0 and not payload:
            return [unavailable_row(spec.provider)]
        return normalize_payload(spec, payload, now=now)
    except subprocess.TimeoutExpired:
        return [timed_out_row(spec.provider)]
    except (OSError, UnicodeError, ValueError):
        return [unavailable_row(spec.provider)]
    except Exception:
        return [unavailable_row(spec.provider)]


def collect_usage(
    *,
    readers_dir: Path = DEFAULT_READERS_DIR,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    reader_specs: Sequence[ReaderSpec] = READERS,
    now: float | None = None,
    deep: bool = False,
    ledger_records: Sequence[Mapping[str, object]] | None = None,
) -> list[dict[str, object]]:
    current_time = time.time() if now is None else now
    rows = []
    for spec in reader_specs:
        spec_rows = run_reader(
                spec,
                readers_dir=readers_dir,
                timeout_s=timeout_s,
                now=current_time,
                deep=deep,
            )
        for row in spec_rows:
            probed_at = parse_reset(row.pop("_probed_at", None))
            if probed_at is None:
                probed_at = current_time
            row["evidence"] = {
                "probe": {
                    "source": "quota_probe",
                    "state": _probe_state(row),
                    "observed_at": probed_at,
                },
                "dispatch": None,
                "conflict": False,
            }
            rows.append(row)
    if ledger_records is None:
        try:
            ledger_records = goalflight_ledger.read_records()
        except (OSError, ValueError):
            ledger_records = []
    return overlay_dispatch_evidence(rows, ledger_records)


def _probe_state(row: Mapping[str, object]) -> str:
    flags = set(row.get("flags") or []) if isinstance(row.get("flags"), list) else set()
    if "walled" in flags:
        return "walled"
    if "auth-broken" in flags:
        return "auth_broken"
    if "timeout" in flags:
        return "timed_out"
    if "unavailable" in flags:
        return "unavailable"
    return "reported"


def _record_account(record: Mapping[str, object]) -> str | None:
    for key in ("effective_account", "account"):
        value = _label(record.get(key))
        if value and value != "default":
            return value
    return None


def _record_provider(record: Mapping[str, object]) -> str | None:
    provider = goalflight_rate_pressure.provider_for(str(record.get("agent") or ""))
    return {
        "openai": "codex",
        "xai": "grok",
        "moonshot": "moonshot",
        "cursor": "cursor",
        "anthropic-session": "claude",
        "anthropic-cli-acp": "claude",
        "anthropic-api": "claude",
    }.get(str(provider or ""))


def _record_observed_at(record: Mapping[str, object]) -> float | None:
    for key in ("ended_at", "updated_at", "started_at"):
        parsed = parse_reset(record.get(key))
        if parsed is not None:
            return parsed
    return None


def _record_evidence_value(record: Mapping[str, object], key: str) -> object:
    for source in (
        record,
        record.get("reason"),
        record.get("error"),
        record.get("outcome"),
    ):
        if isinstance(source, Mapping) and source.get(key) not in (None, ""):
            return source.get(key)
    return None


def _dispatch_outcome(record: Mapping[str, object]) -> dict[str, object] | None:
    state = str(record.get("state") or record.get("terminal_state") or "")
    observed_at = _record_observed_at(record)
    if state in goalflight_dispatch_states.SUCCESS_TERMINAL_RECORD_STATES:
        return {
            "source": "dispatch",
            "state": "served",
            "observed_at": observed_at,
            "reset_at": None,
            "retry_after": None,
            "dispatch_id": record.get("dispatch_id"),
        }
    kind = goalflight_dispatch_states.limit_kind_for_record(record)
    if kind is None:
        return None
    return {
        "source": "dispatch",
        "state": goalflight_dispatch_states.limit_state_for_kind(kind),
        "limit_kind": kind,
        "observed_at": observed_at,
        "reset_at": _record_evidence_value(record, "reset_at"),
        "retry_after": _record_evidence_value(record, "retry_after"),
        "dispatch_id": record.get("dispatch_id"),
    }


def _evidence_conflicts(
    probe: Mapping[str, object],
    dispatch: Mapping[str, object],
) -> bool:
    """True only when the two sources disagree about the PRESENT.

    A conflict requires (a) incompatible claims and (b) the dispatch evidence
    being newer than the probe observation. A seat that served at 14:29 and
    was probed walled at 16:36 is not a conflict — time alone explains it
    (it walled in between), and shouting there teaches the operator to ignore
    the banner. When either timestamp is unmeasured the ordering cannot be
    proven coherent, so the disagreement stays loud.
    """
    probe_state = probe.get("state")
    dispatch_state = dispatch.get("state")
    disagree = False
    if probe_state == "walled":
        disagree = dispatch_state in {
            "served",
            goalflight_dispatch_states.TRANSIENT_THROTTLE_STATE,
        }
    elif probe_state == "reported":
        disagree = dispatch_state == goalflight_dispatch_states.QUOTA_EXHAUSTED_STATE
    if not disagree:
        return False
    probed_at = parse_reset(probe.get("observed_at"))
    dispatched_at = parse_reset(dispatch.get("observed_at"))
    if probed_at is not None and dispatched_at is not None:
        return dispatched_at > probed_at
    return True


def overlay_dispatch_evidence(
    rows: Sequence[dict[str, object]],
    records: Sequence[Mapping[str, object]],
) -> list[dict[str, object]]:
    latest: dict[tuple[str, str], dict[str, object]] = {}
    for record in records:
        provider = _record_provider(record)
        account = _record_account(record)
        outcome = _dispatch_outcome(record)
        if not provider or not account or not outcome:
            continue
        key = (provider, account)
        existing = latest.get(key)
        observed = parse_reset(outcome.get("observed_at")) or float("-inf")
        existing_observed = (
            parse_reset(existing.get("observed_at")) if existing else None
        )
        if existing is None or observed >= (existing_observed or float("-inf")):
            latest[key] = outcome

    out: list[dict[str, object]] = []
    for original in rows:
        row = dict(original)
        evidence = row.get("evidence")
        if not isinstance(evidence, dict):
            evidence = {
                "probe": {
                    "source": "quota_probe",
                    "state": _probe_state(row),
                    "observed_at": None,
                },
                "dispatch": None,
                "conflict": False,
            }
        account = _label(row.get("account"))
        dispatch = latest.get((str(row.get("provider") or ""), account or ""))
        evidence["dispatch"] = dispatch
        probe = evidence.get("probe") if isinstance(evidence.get("probe"), Mapping) else {}
        evidence["conflict"] = bool(dispatch and _evidence_conflicts(probe, dispatch))
        row["evidence"] = evidence
        out.append(row)
    return out


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


def _observed_text(value: object, *, now: float) -> str:
    observed_at = parse_reset(value)
    if observed_at is None:
        return "unknown time"
    local = datetime.fromtimestamp(observed_at).astimezone().strftime("%b %d %H:%M")
    return f"{local}, age {humanize_delta(now - observed_at)}"


def _evidence_text(row: Mapping[str, object], *, now: float) -> str:
    evidence = row.get("evidence") if isinstance(row.get("evidence"), Mapping) else {}
    probe = evidence.get("probe") if isinstance(evidence.get("probe"), Mapping) else {}
    probe_state = probe.get("state") or _probe_state(row)
    parts = [
        f"probe: {probe_state} (as of {_observed_text(probe.get('observed_at'), now=now)})"
    ]
    dispatch = evidence.get("dispatch") if isinstance(evidence.get("dispatch"), Mapping) else None
    if dispatch:
        dispatch_text = (
            f"dispatch: {dispatch.get('state') or 'unknown'} "
            f"(as of {_observed_text(dispatch.get('observed_at'), now=now)})"
        )
        reset_at = parse_reset(dispatch.get("reset_at"))
        if reset_at is not None:
            dispatch_text += f", reset {_local_reset(reset_at)}"
        retry_after = _number(dispatch.get("retry_after"))
        if retry_after is not None:
            dispatch_text += f", retry-after {humanize_delta(retry_after)}"
        parts.append(dispatch_text)
    else:
        parts.append("dispatch: none")
    prefix = "⚠CONFLICT — " if evidence.get("conflict") is True else ""
    return prefix + "; ".join(parts)


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
    headers = (
        "PROVIDER/ACCOUNT",
        "PROBE READING",
        "RESETS (local HH:MM)",
        "EVIDENCE",
    )
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
        display_rows.append(
            (
                _provider_account(row),
                remaining,
                reset_text,
                _evidence_text(row, now=current_time),
            )
        )

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
        "--deep",
        action="store_true",
        help=(
            "let slow readers run their full capture so Claude rows carry "
            "headroom and reset times (minutes, not seconds)"
        ),
    )
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
    rows = collect_usage(
        readers_dir=args.readers_dir,
        timeout_s=DEEP_TIMEOUT_S if args.deep else DEFAULT_TIMEOUT_S,
        now=now,
        deep=args.deep,
    )
    if args.json:
        print(json.dumps(rows, indent=2))
    else:
        print(render_table(rows, now=now))
        try:
            import goalflight_messages
        except Exception:
            pass
        else:
            goalflight_messages.emit_controller_mail_notice(
                project_root=Path.cwd(),
            )
            goalflight_messages.emit_controller_milestone_notice(
                project_root=Path.cwd(),
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
