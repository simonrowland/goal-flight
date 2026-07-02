#!/usr/bin/env python3
"""Provider quota-stuck worker helpers.

The kill path is intentionally narrower than the advisory path. Advisory/counting
may use ledger/status evidence, but reaping requires a live tail quota signature.
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any

import goalflight_ledger
import goalflight_rate_pressure


QUOTA_STUCK_TAIL_BYTES = 8192
QUOTA_STUCK_MIN_SIGNATURE_AGE_S = 180.0
QUOTA_STUCK_REAP_ENABLE_ENV = "GOALFLIGHT_QUOTA_STUCK_REAP"
QUOTA_STUCK_REAP_MIN_AGE_ENV = "GOALFLIGHT_QUOTA_STUCK_REAP_MIN_AGE_S"
QUOTA_STUCK_CONTROLLER_DISPATCH_ID = "controller-quota-advisory"
QUOTA_STUCK_ADVISORY_TYPE = "advisory"

BASH_TAIL_WORKER_COMM_ALLOWLIST = frozenset(
    {
        "claude",
        "codex",
        "cursor",
        "cursor-agent",
        "gemini",
        "grok",
        "opencode",
    }
)

RATE_LIMITED_STATES = frozenset(
    {
        "idle_timeout",
        "inconclusive_timeout",
        "rate_limited",
        "running_quiet",
        "stalled",
        "watcher_stopped",
        "wedged",
        "worker_dead",
    }
)

_DRAFT_ARTIFACT_FIELDS = (
    "artifact_path",
    "draft_path",
    "output_path",
    "result_path",
)
_DRAFT_ARTIFACT_LIST_FIELDS = (
    "artifact_paths",
    "artifacts",
    "declared_artifacts",
    "draft_paths",
    "output_paths",
    "result_paths",
)
_DRAFT_LINE_WORDS = ("artifact", "created", "draft", "file", "output", "result", "saved", "wrote")
_DRAFT_PATH_RE = re.compile(
    r"(?P<path>(?:\.?/)?(?:docs-private|docs|outputs|reports)/[^\s`\"')\]]+\."
    r"(?:diff|json|md|patch|txt|yaml|yml))"
)
_ERROR_CONTEXT_PREFIX_RE = re.compile(
    r"^\s*(?:"
    r"(?:api|provider|upstream)?\s*error\b|"
    r"exception\b|fatal\b|failed\b|failure\b|traceback\b|"
    r"status(?:\s+code)?\s*[:=]?\s*(?:4\d\d|5\d\d)\b|"
    r"http\s*(?:4\d\d|5\d\d)\b"
    r")",
    re.IGNORECASE,
)
_STRUCTURED_ERROR_FIELD_RE = re.compile(
    r"['\"](?:error|errors|code|type|message|status|status_code)['\"]\s*:",
    re.IGNORECASE,
)


def positive_float_env(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw in (None, ""):
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def quota_reap_enabled() -> bool:
    return os.environ.get(QUOTA_STUCK_REAP_ENABLE_ENV, "").strip().lower() in {
        "1",
        "true",
        "yes",
    }


def quota_reap_min_age_s() -> float:
    return positive_float_env(QUOTA_STUCK_REAP_MIN_AGE_ENV, QUOTA_STUCK_MIN_SIGNATURE_AGE_S)


def tail_excerpt(path: Path | str | None, max_bytes: int = QUOTA_STUCK_TAIL_BYTES) -> str:
    if not path:
        return ""
    try:
        p = Path(path).expanduser()
        size = p.stat().st_size
        with p.open("rb") as fh:
            fh.seek(max(0, size - max_bytes))
            return fh.read(max_bytes).decode("utf-8", errors="replace")
    except OSError:
        return ""


def _quota_error_context(text: str) -> str:
    lines: list[str] = []
    in_fence = False
    for raw in str(text or "").splitlines():
        stripped = raw.strip()
        if stripped.startswith(("```", "~~~")):
            in_fence = not in_fence
            continue
        if in_fence or not stripped:
            continue
        if stripped.startswith((">", "|", "`", '"', "'")):
            continue
        if _ERROR_CONTEXT_PREFIX_RE.search(raw) or _STRUCTURED_ERROR_FIELD_RE.search(raw):
            lines.append(raw)
    return "\n".join(lines)


def _quota_signature_in_error_context(text: str) -> str | None:
    status = {"error": text}
    record = {"state": "worker_dead"}
    if goalflight_rate_pressure.detect_pressure_scope(record, status) != goalflight_rate_pressure.ACCOUNT_RATE_LIMIT_SCOPE:
        return None
    direct = goalflight_rate_pressure.rate_limit_signature_in_text(text)
    if direct:
        return direct
    lowered = text.lower()
    for status_code, anchors in goalflight_rate_pressure.RATE_LIMIT_HTTP_STATUS_ANCHORS.items():
        if any(anchor in lowered for anchor in anchors):
            return f"http_{status_code}"
    return None


def quota_signature_in_text(text: str) -> str | None:
    context = _quota_error_context(text)
    if not context:
        return None
    return _quota_signature_in_error_context(context)


def tail_quota_signature(path: Path | str | None) -> dict[str, Any] | None:
    if not path:
        return None
    p = Path(path).expanduser()
    excerpt = tail_excerpt(p)
    signature = quota_signature_in_text(excerpt)
    if not signature:
        return None
    try:
        stat = p.stat()
        mtime = float(stat.st_mtime)
    except OSError:
        mtime = None
    return {
        "signature": signature,
        "tail_path": str(p),
        "tail_excerpt": excerpt.strip(),
        "tail_mtime": int(mtime) if mtime is not None else None,
    }


def provider_for_agent(agent: object) -> str | None:
    return goalflight_rate_pressure.provider_for(str(agent or "").strip())


def budget_key_for_agent(agent: object, *, pool_map: dict[str, str] | None = None) -> str | None:
    return goalflight_rate_pressure.budget_key_for_agent(str(agent or "").strip(), pool_map=pool_map)


def _read_json(path: Path | str | None) -> dict | None:
    if not path:
        return None
    try:
        payload = json.loads(Path(str(path)).expanduser().read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def tail_path_from_record(record: dict) -> Path | None:
    for key in ("stdout_path", "tail_path"):
        value = record.get(key)
        if value:
            return Path(str(value)).expanduser()
    status = _read_json(record.get("status_path"))
    if status:
        tail = status.get("tail_path") or status.get("stdout_path")
        if tail:
            return Path(str(tail)).expanduser()
    return None


def _record_haystack(record: dict) -> str:
    parts: list[str] = []
    for key in ("reason", "error"):
        value = record.get(key)
        if value:
            parts.append(json.dumps(value, sort_keys=True) if isinstance(value, (dict, list)) else str(value))
    status = _read_json(record.get("status_path"))
    if status:
        for key in ("reason", "error", "text_excerpt", "result_text"):
            value = status.get(key)
            if value:
                parts.append(json.dumps(value, sort_keys=True) if isinstance(value, (dict, list)) else str(value))
    return " ".join(parts)


def record_quota_signature(record: dict, *, require_tail: bool = False) -> dict[str, Any] | None:
    tail_info = tail_quota_signature(tail_path_from_record(record))
    if tail_info:
        return {**tail_info, "source": "tail"}
    if require_tail:
        return None
    signature = quota_signature_in_text(_record_haystack(record))
    if not signature:
        return None
    return {"signature": signature, "source": "record"}


def quota_limited_reason(
    *,
    agent: object,
    tail: Path | str | None,
    previous_state: object,
    previous_reason: object,
) -> dict[str, Any] | None:
    if (
        previous_state == "rate_limited"
        and isinstance(previous_reason, dict)
        and previous_reason.get("rate_limit_signature")
    ):
        return previous_reason
    info = tail_quota_signature(tail)
    if not info:
        return None
    provider = provider_for_agent(agent)
    reason: dict[str, Any] = {
        "message": "dispatch_worker_rate_limited",
        "provider": provider,
        "rate_limit_signature": info["signature"],
        "tail_path": info.get("tail_path"),
        "previous_state": previous_state,
        "previous_reason": previous_reason,
        "tail_excerpt": info.get("tail_excerpt"),
    }
    return reason


def apply_rate_limited_status(
    payload: dict,
    *,
    agent: object,
    tail: Path | str | None,
    previous_state: object,
    previous_reason: object,
) -> bool:
    reason = quota_limited_reason(
        agent=agent,
        tail=tail,
        previous_state=previous_state,
        previous_reason=previous_reason,
    )
    if not reason:
        return False
    payload["state"] = "rate_limited"
    payload["terminal_state"] = "rate_limited"
    payload["reason"] = reason
    payload["rate_limit_provider"] = reason.get("provider")
    payload["rate_limit_signature"] = reason.get("rate_limit_signature")
    return True


def _parse_record_time(record: dict) -> float | None:
    for key in ("updated_at", "ended_at", "started_at"):
        value = record.get(key)
        if isinstance(value, (int, float)):
            return float(value)
        parsed = goalflight_ledger.parse_utc(value)
        if parsed:
            return parsed.timestamp()
    return None


def _record_recent_or_live(record: dict, info: dict[str, Any], *, now_ts: float, window_seconds: int) -> bool:
    if record.get("worker_alive") is True or record.get("worker_still_alive") is True:
        return True
    if record.get("classification") == "expected_live":
        return True
    tail_mtime = info.get("tail_mtime")
    if isinstance(tail_mtime, (int, float)) and float(tail_mtime) >= now_ts - window_seconds:
        return True
    record_time = _parse_record_time(record)
    return bool(record_time is not None and record_time >= now_ts - window_seconds)


def _record_non_complete(record: dict) -> bool:
    state = record.get("state")
    classification = record.get("classification")
    terminal = record.get("terminal_state")
    return not (
        state in {"complete", "released"}
        or classification in {"complete", "released"}
        or terminal == "complete"
    )


def _entry_matches_record(entry: dict, record: dict, *, pool_map: dict[str, str] | None = None) -> bool:
    agent = str(record.get("agent") or "").strip()
    labels = {str(label).strip() for label in entry.get("labels") or [] if str(label).strip()}
    if labels and agent in labels:
        return True
    budget_key = entry.get("budget_key")
    if budget_key and budget_key_for_agent(agent, pool_map=pool_map) == budget_key:
        return True
    provider = entry.get("provider")
    return bool(provider and provider_for_agent(agent) == provider)


def stuck_workers_for_entry(
    entry: dict,
    records: list[dict],
    *,
    window_seconds: int,
    now_ts: float | None = None,
    pool_map: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    now = time.time() if now_ts is None else now_ts
    stuck: list[dict[str, Any]] = []
    for record in records:
        if not _record_non_complete(record) or not _entry_matches_record(entry, record, pool_map=pool_map):
            continue
        info = record_quota_signature(record, require_tail=True)
        if not info or not _record_recent_or_live(record, info, now_ts=now, window_seconds=window_seconds):
            continue
        stuck.append(
            {
                "dispatch_id": record.get("dispatch_id"),
                "agent": record.get("agent"),
                "provider": provider_for_agent(record.get("agent")),
                "worker_pid": record.get("worker_pid"),
                "state": record.get("state"),
                "classification": record.get("classification"),
                "signature": info.get("signature"),
                "tail_path": info.get("tail_path"),
                "tail_mtime": info.get("tail_mtime"),
            }
        )
    return stuck


def quota_pressure_per_provider(
    records: list[dict],
    *,
    window_seconds: int,
    now_ts: float | None = None,
    pool_map: dict[str, str] | None = None,
) -> dict[str, int]:
    now = time.time() if now_ts is None else now_ts
    counts: dict[str, int] = {}
    for record in records:
        agent = record.get("agent")
        if not agent or not _record_non_complete(record):
            continue
        info = record_quota_signature(record, require_tail=True)
        if not info or not _record_recent_or_live(record, info, now_ts=now, window_seconds=window_seconds):
            continue
        key = budget_key_for_agent(agent, pool_map=pool_map)
        if not key:
            continue
        counts[key] = counts.get(key, 0) + 1
    return counts


def decorate_pressure_payload(
    payload: dict,
    records: list[dict],
    *,
    window_seconds: int,
    pool_map: dict[str, str] | None = None,
) -> dict:
    out = dict(payload)
    decorated: list[dict] = []
    for raw in payload.get("providers_under_pressure") or []:
        entry = dict(raw)
        stuck = stuck_workers_for_entry(entry, records, window_seconds=window_seconds, pool_map=pool_map)
        entry["stuck_worker_count"] = len(stuck)
        entry["stuck_workers"] = stuck
        if entry.get("scope") != "agent" and stuck:
            labels = [str(label) for label in entry.get("labels") or []]
            entry["quota_hard_stop"] = True
            entry["effective_caps"] = {label: 0 for label in labels}
        else:
            entry["quota_hard_stop"] = False
            entry.pop("effective_caps", None)
        decorated.append(entry)
    out["providers_under_pressure"] = decorated
    return out


def advisory_key(entry: dict) -> str:
    ids = ",".join(
        sorted(str(item.get("dispatch_id") or "") for item in entry.get("stuck_workers") or [] if item.get("dispatch_id"))
    )
    return f"{entry.get('budget_key') or entry.get('provider')}:{entry.get('stuck_worker_count', 0)}:{ids}"


def advisory_payload(entry: dict) -> dict[str, Any]:
    provider = entry.get("provider") or entry.get("budget_key") or "unknown"
    count = int(entry.get("stuck_worker_count") or 0)
    text = (
        f"{provider} quota exhausted: {count} agent(s) stuck "
        "(will not self-recover) - re-dispatch their tasks; holding new provider dispatch"
    )
    return {
        "text": text,
        "provider": entry.get("provider"),
        "budget_key": entry.get("budget_key"),
        "limit_pool_id": entry.get("limit_pool_id"),
        "stuck_worker_count": count,
        "stuck_workers": entry.get("stuck_workers") or [],
        "labels": entry.get("labels") or [],
        "advisory_key": advisory_key(entry),
    }


def advisory_lines(pressure: dict | None, *, limit: int = 5) -> list[str]:
    if not pressure:
        return []
    lines: list[str] = []
    for entry in (pressure.get("providers_under_pressure") or [])[:limit]:
        if not entry.get("quota_hard_stop"):
            continue
        payload = advisory_payload(entry)
        lines.append(f"quota: {payload['text']}")
    return lines


def _coerce_artifact_path(value: object, *, base: Path | None) -> Path | None:
    if not value:
        return None
    if isinstance(value, dict):
        value = value.get("path") or value.get("file")
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip().strip("`'\"")
    path = Path(raw).expanduser()
    if not path.is_absolute() and base is not None:
        path = base / path
    return path


def _candidate_artifact_values(record: dict, tail_text: str) -> list[object]:
    values: list[object] = []
    for key in _DRAFT_ARTIFACT_FIELDS:
        if record.get(key):
            values.append(record[key])
    for key in _DRAFT_ARTIFACT_LIST_FIELDS:
        raw = record.get(key)
        if isinstance(raw, list):
            values.extend(raw)
        elif raw:
            values.append(raw)
    status = _read_json(record.get("status_path"))
    if status:
        for key in _DRAFT_ARTIFACT_FIELDS:
            if status.get(key):
                values.append(status[key])
        for key in _DRAFT_ARTIFACT_LIST_FIELDS:
            raw = status.get(key)
            if isinstance(raw, list):
                values.extend(raw)
            elif raw:
                values.append(raw)
    for line in tail_text.splitlines():
        lowered = line.lower()
        if not any(word in lowered for word in _DRAFT_LINE_WORDS):
            continue
        values.extend(match.group("path") for match in _DRAFT_PATH_RE.finditer(line))
    return values


def _artifact_mtime_ok(path: Path, record: dict) -> bool:
    try:
        stat = path.stat()
    except OSError:
        return False
    if not path.is_file() or stat.st_size <= 0:
        return False
    started = goalflight_ledger.parse_utc(record.get("started_at"))
    if started and stat.st_mtime + 2.0 < started.timestamp():
        return False
    if stat.st_mtime > time.time() + 300.0:
        return False
    return True


def draft_artifact_for_record(record: dict) -> Path | None:
    tail = tail_path_from_record(record)
    tail_text = tail_excerpt(tail)
    base = Path(str(record.get("project_root"))).expanduser() if record.get("project_root") else None
    seen: set[str] = set()
    for value in _candidate_artifact_values(record, tail_text):
        path = _coerce_artifact_path(value, base=base)
        if path is None:
            continue
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        if _artifact_mtime_ok(path, record):
            return path
    return None
