#!/usr/bin/env python3
"""Show live Goal Flight workers grouped by model and controller."""

from __future__ import annotations

import argparse
import collections
import json
import re
import subprocess
import time
from pathlib import Path
from typing import Mapping, Sequence

import goalflight_dispatch_paths
import goalflight_dispatch_states
import goalflight_ledger


JSON_KEY = "live_workers_by_model"
MODEL_FLAGS = {
    "sol": "poor value vs grok",
    "astra": "use sparingly",
}
POINTER_FAMILIES = ("luna", "grok", "astra", "sol")
STATUS_SCAN_LIMIT = 256


def _read_json_mapping(path: Path) -> dict[str, object] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError, TypeError):
        return None
    return payload if isinstance(payload, dict) else None


def _dispatch_status_payloads(
    dispatch_dir: Path,
    *,
    limit: int = STATUS_SCAN_LIMIT,
) -> tuple[list[dict[str, object]], str | None]:
    try:
        if not dispatch_dir.is_dir():
            return [], f"status directory unavailable: {dispatch_dir}"
        paths = list(dispatch_dir.glob("*.status.json"))
    except OSError as exc:
        return [], f"status directory unreadable: {type(exc).__name__}: {exc}"
    if limit > 0 and len(paths) > limit:
        def _mtime(path: Path) -> float:
            try:
                return path.stat().st_mtime
            except OSError:
                return 0.0

        paths = sorted(paths, key=_mtime, reverse=True)[:limit]
    else:
        paths.sort()
    payloads = []
    for path in paths:
        payload = _read_json_mapping(path)
        if payload is None or not payload.get("dispatch_id"):
            continue
        payload.setdefault("status_path", str(path))
        payloads.append(payload)
    return payloads, None


def _timestamp(value: object) -> float | None:
    if isinstance(value, bool) or value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        parsed = goalflight_ledger.parse_utc(value)
        if parsed is not None:
            return parsed.timestamp()
    return None


def _record_is_recent(record: Mapping[str, object], *, now: float) -> bool:
    cutoff = now - goalflight_ledger.STATUS_RECENT_WINDOW_DAYS * 86400.0
    observed = [
        timestamp
        for key in ("updated_at", "started_at", "created_at")
        if (timestamp := _timestamp(record.get(key))) is not None
    ]
    status_path = record.get("status_path")
    if isinstance(status_path, str) and status_path:
        try:
            observed.append(Path(status_path).expanduser().stat().st_mtime)
        except OSError:
            pass
    # Legacy/status-only fixtures without any age evidence stay visible. Any
    # row with evidence is bounded to the same warm window as usage/status.
    return not observed or max(observed) >= cutoff


def _dispatch_records(
    *,
    ledger_records: Sequence[Mapping[str, object]] | None = None,
    dispatch_dir: Path | None = None,
) -> list[dict[str, object]]:
    ledger_unreadable = False
    ledger_read_error: str | None = None
    if ledger_records is None:
        try:
            ledger_records = goalflight_ledger.read_records(
                skip_terminal=True,
                recent_window_days=goalflight_ledger.STATUS_RECENT_WINDOW_DAYS,
            )
        except (OSError, ValueError) as exc:
            ledger_records = []
            ledger_unreadable = True
            ledger_read_error = f"{type(exc).__name__}: {exc}"

    records: dict[str, dict[str, object]] = {}
    ledger_states: dict[str, tuple[object, object]] = {}
    ledger_unverified_ids: set[str] = set()
    unreadable_rows: list[tuple[str | None, str]] = []
    for record in ledger_records:
        if not isinstance(record, Mapping):
            unreadable_rows.append((None, "invalid ledger row"))
            continue
        record_dict = dict(record)
        if goalflight_ledger.record_is_unreadable(record_dict):
            unreadable_rows.append(
                (
                    str(record_dict.get("dispatch_id"))
                    if record_dict.get("dispatch_id")
                    else None,
                    str(record_dict.get("path") or "unreadable ledger row"),
                )
            )
        if not record_dict.get("dispatch_id"):
            continue
        dispatch_id = str(record_dict["dispatch_id"])
        records[dispatch_id] = record_dict
        ledger_states[dispatch_id] = (
            record_dict.get("state"),
            record_dict.get("terminal_state"),
        )
        if goalflight_ledger.record_is_unreadable(record_dict):
            ledger_unverified_ids.add(dispatch_id)

    status_dir = dispatch_dir or goalflight_dispatch_paths.dispatch_base_dir()
    # Status directories retain terminal history on some installations. A
    # bounded recent scan keeps status-only launches visible without reopening
    # every historical sidecar on each /usage invocation.
    status_payloads, status_error = _dispatch_status_payloads(status_dir)
    status_ids = {str(payload["dispatch_id"]) for payload in status_payloads}
    now = time.time()
    for record in list(records.values()):
        if str(record.get("dispatch_id")) in status_ids:
            continue
        status_path = record.get("status_path")
        if (
            isinstance(status_path, str)
            and status_path
            and _record_is_recent(record, now=now)
        ):
            payload = _read_json_mapping(Path(status_path).expanduser())
            if payload is not None and payload.get("dispatch_id"):
                status_payloads.append(payload)

    # ``read_records(skip_terminal=True)`` intentionally avoids parsing old
    # terminal rows. Recheck only status candidates so a terminal ledger row
    # still wins over a stale running sidecar without reopening the ledger.
    if not ledger_unreadable:
        for payload in status_payloads:
            dispatch_id = str(payload["dispatch_id"])
            worker_pid = payload.get("worker_pid")
            if dispatch_id in ledger_states or not (
                isinstance(worker_pid, int) and worker_pid > 0
            ):
                continue
            try:
                ledger_record = goalflight_ledger.read_record(dispatch_id)
            except (OSError, ValueError):
                ledger_record = None
            if not isinstance(ledger_record, Mapping):
                continue
            ledger_record = dict(ledger_record)
            if goalflight_ledger.record_is_unreadable(ledger_record):
                unreadable_rows.append(
                    (
                        dispatch_id,
                        str(ledger_record.get("path") or "unreadable ledger row"),
                    )
                )
                ledger_unverified_ids.add(dispatch_id)
            else:
                records.setdefault(dispatch_id, {}).update(ledger_record)
            ledger_states[dispatch_id] = (
                ledger_record.get("state"),
                ledger_record.get("terminal_state"),
            )

    for payload in status_payloads:
        dispatch_id = str(payload["dispatch_id"])
        record = records.setdefault(dispatch_id, {})
        for key, value in payload.items():
            if value is not None:
                record[key] = value
        # The ledger's lifecycle state is authoritative; status.json is a
        # heartbeat copy and can lag during terminal publication.
        if dispatch_id in ledger_states:
            ledger_state, ledger_terminal_state = ledger_states[dispatch_id]
            if ledger_state is not None:
                record["state"] = ledger_state
            if ledger_terminal_state is not None:
                record["terminal_state"] = ledger_terminal_state
        if ledger_unreadable or dispatch_id in ledger_unverified_ids:
            record["_ledger_unverified"] = True
        record.setdefault("status_path", payload.get("status_path"))
        if not record.get("stdout_path") and record.get("tail_path"):
            record["stdout_path"] = record["tail_path"]
        if not record.get("worker_identity"):
            expected = record.get("expected_worker_identity")
            if isinstance(expected, Mapping):
                record["worker_identity"] = dict(expected)

    for dispatch_id in ledger_unverified_ids:
        if dispatch_id in records:
            records[dispatch_id]["_ledger_unverified"] = True

    source_records: list[dict[str, object]] = []
    if status_error and not ledger_unreadable:
        source_records.append(
            {
                "_source_unverified_reason": status_error,
                "_source_unverified_count": 1,
            }
        )
    if ledger_unreadable:
        reason = f"ledger unreadable rows=1: {ledger_read_error or 'unknown error'}"
        if status_error:
            reason += "; " + status_error
        # A terminal sidecar cannot prove that its worker is gone while the
        # ledger is unreadable. Count every sidecar that the later candidate
        # pass will skip (terminal, stale, or missing a worker pid) as an
        # unverified source instead of turning an all-terminal scan into a
        # confident zero.
        unaccounted_sidecars = sum(
            1
            for payload in status_payloads
            if any(
                goalflight_dispatch_states.is_terminal_state(payload.get(field))
                for field in ("state", "terminal_state")
            )
            or not (
                isinstance(payload.get("worker_pid"), int)
                and payload["worker_pid"] > 0
                and _record_is_recent(payload, now=now)
            )
        )
        source_records.append(
            {
                "_source_unverified_reason": reason,
                "_source_unverified_count": (
                    unaccounted_sidecars if status_payloads else 1
                ),
            }
        )
    if unreadable_rows:
        paths = ", ".join(path for _dispatch_id, path in unreadable_rows)
        unresolved = sum(
            1
            for dispatch_id, _path in unreadable_rows
            if not dispatch_id
            or not isinstance(records.get(dispatch_id, {}).get("worker_pid"), int)
            or records.get(dispatch_id, {}).get("worker_pid", 0) <= 0
        )
        source_records.append(
            {
                "_source_unverified_reason": (
                    f"ledger unreadable rows={len(unreadable_rows)}: {paths}"
                ),
                "_source_unverified_count": unresolved,
            }
        )
    return source_records + list(records.values())


def _tail_model(tail_path: object) -> str | None:
    if not isinstance(tail_path, str) or not tail_path:
        return None
    try:
        with Path(tail_path).expanduser().open(
            encoding="utf-8", errors="replace"
        ) as tail:
            for line_number, line in enumerate(tail):
                match = re.match(r"model:\s*(\S+)", line)
                if match:
                    return match.group(1)
                if line_number >= 60:
                    break
    except OSError:
        pass
    return None


def _dispatch_model(record: Mapping[str, object]) -> str:
    agent = str(record.get("agent") or "?")
    recorded = record.get("model")
    if isinstance(recorded, str) and recorded.strip():
        return recorded.strip()
    tail_path = record.get("stdout_path") or record.get("tail_path")
    tail_model = _tail_model(tail_path)
    if tail_model:
        return tail_model
    if agent.startswith("grok"):
        return "grok"
    if agent.startswith("cursor"):
        return "cursor"
    return f"{agent}:?"


def _identity_probe_error(pid: int) -> dict[str, object]:
    return {
        "pid": pid,
        "identity_available": False,
        "identity_probe_error": True,
        "identity_source": "traffic_ps_probe_error",
    }


def _batch_process_identities(pids: Sequence[int]) -> dict[int, dict[str, object] | None]:
    """Read current identities for all candidate PIDs with one ps invocation."""
    unique_pids = sorted({pid for pid in pids if isinstance(pid, int) and pid > 0})
    if not unique_pids:
        return {}

    compat = goalflight_ledger.goalflight_compat
    if compat.is_windows():
        # Windows has no ps equivalent. Keep the existing native identity path
        # rather than weakening PID-reuse checks for the read-only view.
        return {pid: goalflight_ledger.process_identity(pid) for pid in unique_pids}

    liveness: dict[int, bool | None] = {}
    result: dict[int, dict[str, object] | None] = {}
    active: list[int] = []
    for pid in unique_pids:
        try:
            live = compat.pid_liveness(pid)
        except (OSError, subprocess.SubprocessError):
            live = None
        liveness[pid] = live
        if live is False:
            result[pid] = None
        else:
            active.append(pid)
    if not active:
        return result

    try:
        completed = subprocess.run(
            [
                "ps",
                "-o",
                "pid=,ppid=,pgid=,lstart=,comm=,args=",
                "-p",
                ",".join(str(pid) for pid in active),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=1.0,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        completed = None

    if completed is None or (completed.returncode != 0 and not completed.stdout):
        return {
            **result,
            **{
                pid: (None if liveness[pid] is False else _identity_probe_error(pid))
                for pid in active
            },
        }

    parsed: dict[int, dict[str, object]] = {}
    for line in completed.stdout.splitlines():
        fields = line.strip().split(None, 9)
        if len(fields) < 9 or not fields[0].isdigit():
            continue
        pid = int(fields[0])
        if pid not in active:
            continue
        parsed[pid] = {
            "pid": pid,
            "ppid": fields[1],
            "pgid": fields[2],
            "lstart": " ".join(fields[3:8]),
            "comm": fields[8],
            "args": fields[9] if len(fields) > 9 else None,
        }

    for pid in active:
        current = parsed.get(pid)
        if current is None:
            try:
                still_live = compat.pid_liveness(pid)
            except (OSError, subprocess.SubprocessError):
                still_live = None
            result[pid] = None if still_live is False else _identity_probe_error(pid)
            continue
        try:
            start_identity = compat.process_start_identity(pid)
        except (OSError, subprocess.SubprocessError):
            start_identity = None
        if isinstance(start_identity, Mapping) and start_identity.get("start_token"):
            current["start_token"] = start_identity["start_token"]
        if not current.get("lstart"):
            current.update(
                {
                    "identity_available": False,
                    "identity_probe_error": True,
                    "identity_source": "ps_identity_incomplete",
                }
            )
        result[pid] = current
    return result


def _record_bucket(
    buckets: dict[str, dict[str, object]],
    model: str,
    controller: str,
    bucket: str,
) -> None:
    details = buckets.setdefault(
        model,
        {
            "count": 0,
            "unverified": 0,
            "controllers": collections.Counter(),
            "unverified_controllers": collections.Counter(),
        },
    )
    details[bucket] = int(details[bucket]) + 1
    counter = details[
        "controllers" if bucket == "count" else "unverified_controllers"
    ]
    assert isinstance(counter, collections.Counter)
    counter[controller] += 1


def live_workers_by_model(
    *,
    ledger_records: Sequence[Mapping[str, object]] | None = None,
    dispatch_dir: Path | None = None,
) -> dict[str, object]:
    """Return live and identity-unverified workers by model/controller."""
    buckets: dict[str, dict[str, object]] = {}
    total = 0
    unverified_total = 0
    unknown_reasons: list[str] = []
    candidates: list[dict[str, object]] = []
    now = time.time()
    for record in _dispatch_records(
        ledger_records=ledger_records,
        dispatch_dir=dispatch_dir,
    ):
        if record.get("_source_unverified_reason"):
            unknown_reasons.append(str(record["_source_unverified_reason"]))
            try:
                source_count = max(0, int(record.get("_source_unverified_count", 1)))
            except (TypeError, ValueError):
                source_count = 1
            for _ in range(source_count):
                _record_bucket(buckets, "UNKNOWN", "UNKNOWN", "unverified")
            unverified_total += source_count
            continue
        if not isinstance(record.get("worker_pid"), int) or record["worker_pid"] <= 0:
            continue
        if any(
            goalflight_dispatch_states.is_terminal_state(record.get(field))
            for field in ("state", "terminal_state")
        ):
            continue
        if not _record_is_recent(record, now=now):
            continue
        candidates.append(record)

    identities = _batch_process_identities(
        [
            int(record["worker_pid"])
            for record in candidates
            if not record.get("_ledger_unverified")
        ]
    )
    for record in candidates:
        pid = int(record["worker_pid"])
        if record.get("_ledger_unverified"):
            liveness = "unknown"
        else:
            try:
                liveness, _reason = goalflight_ledger.worker_identity_liveness(
                    record,
                    current_identity=identities.get(pid),
                )
            except (OSError, TypeError, ValueError):
                continue
        model = _dispatch_model(record)
        controller = str(record.get("controller_label") or "?")
        if liveness == "live":
            _record_bucket(buckets, model, controller, "count")
            total += 1
        elif liveness == "unknown":
            _record_bucket(buckets, model, controller, "unverified")
            unverified_total += 1

    models = {}
    for model, details in sorted(
        buckets.items(),
        key=lambda item: (
            -(int(item[1]["count"]) + int(item[1]["unverified"])),
            item[0],
        ),
    ):
        controllers = details["controllers"]
        unverified_controllers = details["unverified_controllers"]
        assert isinstance(controllers, collections.Counter)
        assert isinstance(unverified_controllers, collections.Counter)
        models[model] = {
            "count": int(details["count"]),
            "unverified": int(details["unverified"]),
            "controllers": {
                label: number
                for label, number in sorted(
                    controllers.items(), key=lambda item: (-item[1], item[0])
                )
            },
            "unverified_controllers": {
                label: number
                for label, number in sorted(
                    unverified_controllers.items(),
                    key=lambda item: (-item[1], item[0]),
                )
            },
        }
    summary = {
        "total": total,
        "unverified_total": unverified_total,
        "models": models,
    }
    if unknown_reasons:
        summary["unknown_reasons"] = unknown_reasons
    return summary


def _model_flag(model: str) -> str | None:
    lowered = model.lower()
    for family, warning in MODEL_FLAGS.items():
        if family in lowered:
            return warning
    return None


def render(summary: Mapping[str, object]) -> str:
    lines = [
        "live workers by model:",
        "  MODEL              LIVE  UNVERIFIED  CONTROLLERS",
    ]
    models = summary.get("models")
    if isinstance(models, Mapping):
        for model, details in models.items():
            if not isinstance(details, Mapping):
                continue
            model_text = str(model)
            warning = _model_flag(model_text)
            warning_text = f"  <- {warning}" if warning else ""
            controllers = details.get("controllers")
            controller_text = ""
            if isinstance(controllers, Mapping):
                controller_text = ", ".join(
                    f"{label}:{number}" for label, number in controllers.items()
                )
            unverified_controllers = details.get("unverified_controllers")
            if isinstance(unverified_controllers, Mapping) and unverified_controllers:
                unknown_text = ", ".join(
                    f"{label}:{number}"
                    for label, number in unverified_controllers.items()
                )
                controller_text = (
                    f"{controller_text}; unverified {unknown_text}"
                    if controller_text
                    else f"unverified {unknown_text}"
                )
            lines.append(
                f"  {model_text:16s} {int(details.get('count', 0)):4d}"
                f"  {int(details.get('unverified', 0)):10d}  "
                f"{controller_text}{warning_text}"
            )
    reasons = summary.get("unknown_reasons")
    totals_unverified = isinstance(reasons, Sequence) and bool(reasons)
    if totals_unverified:
        lines.append("  UNKNOWN: " + "; ".join(str(reason) for reason in reasons))
    total_label = "total live"
    if totals_unverified:
        total_label += " (lower bound; unverified)"
    lines.append(f"  {total_label}: {int(summary.get('total', 0))}")
    lines.append(f"  total unverified: {int(summary.get('unverified_total', 0))}")
    return "\n".join(lines)


def live_mix_pointer(summary: Mapping[str, object]) -> str:
    models = summary.get("models")
    counts = {family: 0 for family in POINTER_FAMILIES}
    if isinstance(models, Mapping):
        for model, details in models.items():
            if not isinstance(details, Mapping):
                continue
            model_text = str(model).lower()
            for family in POINTER_FAMILIES:
                if family in model_text:
                    counts[family] += int(details.get("count", 0))
    joined = ", ".join(f"{family} {counts[family]}" for family in POINTER_FAMILIES)
    reasons = summary.get("unknown_reasons")
    totals_unverified = isinstance(reasons, Sequence) and bool(reasons)
    unknown = (
        "; UNKNOWN: " + "; ".join(str(reason) for reason in reasons)
        if totals_unverified
        else ""
    )
    lower_bound = "; totals lower bound (unverified)" if totals_unverified else ""
    return f"live mix: {joined}{lower_bound}{unknown}; see /goal-flight traffic"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Show live workers by model and controller."
    )
    parser.add_argument("--json", action="store_true", help="emit the live mix as JSON")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    summary = live_workers_by_model()
    if args.json:
        print(json.dumps({JSON_KEY: summary}, indent=2))
    else:
        print(render(summary))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
