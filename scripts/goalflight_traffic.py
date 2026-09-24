#!/usr/bin/env python3
"""Show live Goal Flight workers grouped by model and controller."""

from __future__ import annotations

import argparse
import collections
import json
import re
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


def _read_json_mapping(path: Path) -> dict[str, object] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError, TypeError):
        return None
    return payload if isinstance(payload, dict) else None


def _dispatch_status_payloads(dispatch_dir: Path) -> list[dict[str, object]]:
    try:
        paths = sorted(dispatch_dir.glob("*.status.json"))
    except OSError:
        return []
    payloads = []
    for path in paths:
        payload = _read_json_mapping(path)
        if payload is None or not payload.get("dispatch_id"):
            continue
        payload.setdefault("status_path", str(path))
        payloads.append(payload)
    return payloads


def _dispatch_records(
    *,
    ledger_records: Sequence[Mapping[str, object]] | None = None,
    dispatch_dir: Path | None = None,
) -> list[dict[str, object]]:
    if ledger_records is None:
        try:
            ledger_records = goalflight_ledger.read_records()
        except (OSError, ValueError):
            ledger_records = []

    records: dict[str, dict[str, object]] = {}
    ledger_states: dict[str, object] = {}
    for record in ledger_records:
        if not isinstance(record, Mapping) or not record.get("dispatch_id"):
            continue
        dispatch_id = str(record["dispatch_id"])
        records[dispatch_id] = dict(record)
        ledger_states[dispatch_id] = record.get("state")

    status_dir = dispatch_dir or goalflight_dispatch_paths.dispatch_base_dir()
    status_payloads = _dispatch_status_payloads(status_dir)
    for record in list(records.values()):
        status_path = record.get("status_path")
        if isinstance(status_path, str) and status_path:
            payload = _read_json_mapping(Path(status_path).expanduser())
            if payload is not None and payload.get("dispatch_id"):
                status_payloads.append(payload)

    for payload in status_payloads:
        dispatch_id = str(payload["dispatch_id"])
        record = records.setdefault(dispatch_id, {})
        for key, value in payload.items():
            if value is not None:
                record[key] = value
        # The ledger's lifecycle state is authoritative; status.json is a
        # heartbeat copy and can lag during terminal publication.
        if dispatch_id in ledger_states and ledger_states[dispatch_id] is not None:
            record["state"] = ledger_states[dispatch_id]
        record.setdefault("status_path", payload.get("status_path"))
        if not record.get("stdout_path") and record.get("tail_path"):
            record["stdout_path"] = record["tail_path"]
        if not record.get("worker_identity"):
            expected = record.get("expected_worker_identity")
            if isinstance(expected, Mapping):
                record["worker_identity"] = dict(expected)

    return list(records.values())


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
    if agent.startswith("grok"):
        return "grok"
    if agent.startswith("cursor"):
        return "cursor"
    recorded = record.get("model")
    if isinstance(recorded, str) and recorded.strip():
        return recorded.strip()
    tail_path = record.get("stdout_path") or record.get("tail_path")
    return _tail_model(tail_path) or f"{agent}:?"


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
    for record in _dispatch_records(
        ledger_records=ledger_records,
        dispatch_dir=dispatch_dir,
    ):
        if not isinstance(record.get("worker_pid"), int) or record["worker_pid"] <= 0:
            continue
        if any(
            record.get(field) in goalflight_dispatch_states.TERMINAL_STATES
            for field in ("state", "terminal_state")
        ):
            continue
        try:
            liveness, _reason = goalflight_ledger.worker_identity_liveness(record)
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
    return {
        "total": total,
        "unverified_total": unverified_total,
        "models": models,
    }


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
    lines.append(f"  total live: {int(summary.get('total', 0))}")
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
    return f"live mix: {joined}; see /goal-flight traffic"


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
