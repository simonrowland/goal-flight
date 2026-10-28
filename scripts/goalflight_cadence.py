#!/usr/bin/env python3
"""Project cadence ledger and the compact controller lane pulse."""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import json
import shlex
import sys
import threading
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import goalflight_ledger  # noqa: E402
import goalflight_journal  # noqa: E402
import goalflight_session_status  # noqa: E402
import goalflight_task  # noqa: E402
import goalflight_wake  # noqa: E402


DEFAULT_CADENCES = {
    "look-ahead": ("lane", 4 * 60 * 60),
    "ci-audit": ("lane", 24 * 60 * 60),
    "dedup-scan": ("project", 24 * 60 * 60),
    "tooling-survey": ("lane", 3 * 24 * 60 * 60),
    "task-store-audit": ("project", 7 * 24 * 60 * 60),
}
PULSE_TEXT_MAX_BYTES = 430
IDLE_ACTIVITY_WINDOW_S = 15 * 60


class CadenceError(RuntimeError):
    pass


def _normalise_state(state: dict[str, Any]) -> dict[str, Any]:
    defaults: dict[str, object] = {
        "lanes": {},
        "registry_order": [],
        "project_duty_owner": None,
        "intervals_seconds": {},
        "last_done": {},
        "duty_wakes": {},
    }
    for key, default in defaults.items():
        if key not in state:
            state[key] = default.copy() if isinstance(default, (dict, list)) else default
    for key in ("lanes", "intervals_seconds", "last_done", "duty_wakes"):
        if not isinstance(state.get(key), dict):
            raise CadenceError(f"invalid cadence state field: {key}")
    if not isinstance(state.get("registry_order"), list):
        raise CadenceError("invalid cadence state field: registry_order")
    if state.get("project_duty_owner") is not None and not isinstance(
        state.get("project_duty_owner"), str
    ):
        raise CadenceError("invalid cadence state field: project_duty_owner")
    intervals = state["intervals_seconds"]
    for item_id, (_scope, default_seconds) in DEFAULT_CADENCES.items():
        value = intervals.get(item_id, default_seconds)
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise CadenceError(f"invalid interval for {item_id}")
        intervals[item_id] = value
    unknown = set(intervals) - set(DEFAULT_CADENCES)
    if unknown:
        raise CadenceError(f"unknown cadence item: {sorted(unknown)[0]}")
    return state


def _load_state(store: goalflight_task.TaskStore) -> dict[str, Any]:
    try:
        return _normalise_state(store.load_controller_cadence())
    except goalflight_task.TaskError as exc:
        raise CadenceError(str(exc)) from exc


def _registry_order(state: dict[str, Any]) -> list[str]:
    lanes = state["lanes"]
    order = [label for label in state["registry_order"] if isinstance(label, str)]
    order.extend(sorted(label for label in lanes if isinstance(label, str) and label not in order))
    return order


def _parse_time(value: object) -> datetime | None:
    parsed = goalflight_task._parse_utc_iso(value)
    return parsed


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _now(value: datetime | None = None) -> datetime:
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc)


def _last_done_at(
    state: dict[str, Any], item_id: str, *, scope: str, label: str
) -> datetime | None:
    per_scope = state["last_done"].get(item_id, {})
    if not isinstance(per_scope, dict):
        raise CadenceError(f"invalid last-done entry: {item_id}")
    key = label if scope == "lane" else "project"
    value = per_scope.get(key)
    if value is None:
        return None
    parsed = _parse_time(value)
    if parsed is None:
        raise CadenceError(f"invalid last-done time: {item_id}")
    return parsed


def _due_info(
    state: dict[str, Any], item_id: str, label: str, now: datetime
) -> dict[str, Any]:
    scope, _default = DEFAULT_CADENCES[item_id]
    interval = int(state["intervals_seconds"][item_id])
    last_done = _last_done_at(state, item_id, scope=scope, label=label)
    age = None if last_done is None else max(0.0, (now - last_done).total_seconds())
    return {
        "id": item_id,
        "scope": scope,
        "interval_seconds": interval,
        "age_seconds": age,
        "due": last_done is None or age >= interval,
        "overdue_2x": last_done is not None and age >= interval * 2,
    }


def _probe(
    project_root: Path,
    label: str,
    cache: dict[str, tuple[str, dict[str, Any] | None]],
) -> tuple[str, dict[str, Any] | None]:
    if label not in cache:
        try:
            cache[label] = goalflight_session_status.probe_live_session(
                project_root, label=label
            )
        except Exception as exc:
            raise CadenceError(f"controller lease unreadable: {label}") from exc
    state, session = cache[label]
    if state not in {"live", "dead", "unreadable"}:
        raise CadenceError(f"controller lease state unknown: {label}")
    return state, session


def _resolve_owner(
    project_root: Path,
    state: dict[str, Any],
    cache: dict[str, tuple[str, dict[str, Any] | None]],
) -> str | None:
    lanes = state["lanes"]
    order = _registry_order(state)
    explicit = state.get("project_duty_owner")
    if explicit:
        if explicit not in lanes:
            raise CadenceError(f"project duty owner has no lane charter: {explicit}")
        return explicit
    main_labels = [label for label in order if label.endswith("-main")]
    if main_labels:
        return main_labels[0]
    live: list[tuple[datetime, int, str]] = []
    for index, label in enumerate(order):
        status, session = _probe(project_root, label, cache)
        if status == "unreadable":
            raise CadenceError(f"controller lease unreadable: {label}")
        if status != "live" or session is None:
            continue
        started = _parse_time(session.get("started_at"))
        if started is None:
            raise CadenceError(f"controller lease start time unreadable: {label}")
        live.append((started, index, label))
    return min(live)[2] if live else None


def _pulse_owner_cache(
    store: goalflight_task.TaskStore, state: dict[str, Any]
) -> dict[str, tuple[str, dict[str, Any] | None]]:
    explicit = state.get("project_duty_owner")
    order = _registry_order(state)
    if explicit or any(label.endswith("-main") for label in order):
        return {}

    cache: dict[str, tuple[str, dict[str, Any] | None]] = {}
    try:
        authority = goalflight_journal.Journal._open_reader_for_resolved_root(
            store.project_root,
            retry_budget_s=0.05,
            open_retry_budget_s=0.05,
            transaction_budget_s=0.1,
            jitter_min_s=0,
            jitter_max_s=0,
        )
        leases = authority.lease_records()
    except goalflight_journal.JournalDisappeared:
        leases = []

    by_label = {str(row.get("label")): row for row in leases}
    for label in order:
        row = by_label.get(label)
        if row is None:
            cache[label] = ("dead", None)
            continue
        try:
            alive = goalflight_wake.lease_holder_alive(
                store.project_root,
                controller_label=label,
                lease_nonce=str(row.get("nonce") or ""),
            )
        except (OSError, RuntimeError, TypeError, ValueError):
            alive = None
        if alive is True:
            cache[label] = ("live", {"started_at": row.get("claimed_at")})
        elif alive is False:
            cache[label] = ("dead", None)
        else:
            cache[label] = ("unreadable", None)
    return cache


def _due_for_label(
    store: goalflight_task.TaskStore,
    label: str,
    now: datetime,
    *,
    state: dict[str, Any] | None = None,
    owner_cache: dict[str, tuple[str, dict[str, Any] | None]] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    state = _normalise_state(state or _load_state(store))
    root = store.project_root
    cache = owner_cache if owner_cache is not None else {}
    due: list[dict[str, Any]] = []
    overdue_duties: list[dict[str, Any]] = []

    for item_id, (scope, _seconds) in DEFAULT_CADENCES.items():
        info = _due_info(state, item_id, label, now)
        if not info["due"]:
            continue
        if scope == "lane":
            due.append(info)
            continue

        owner = _resolve_owner(root, state, cache)
        if owner is None:
            continue
        if label == owner:
            due.append(info)
        elif info["overdue_2x"]:
            last_done = _last_done_at(state, item_id, scope="project", label=label)
            overdue_duties.append(
                {
                    "id": item_id,
                    "owner": owner,
                    "period": {
                        "last_done_at": _iso(last_done) if last_done is not None else None,
                        "interval_seconds": info["interval_seconds"],
                    },
                }
            )
    return due, overdue_duties


def _due_instruction(item: dict[str, Any], controller_label: str) -> str:
    if item.get("id") == "ci-audit":
        label = shlex.quote(controller_label)
        audit_script = Path(__file__).resolve().with_name("goalflight_ci_audit.py")
        return (
            f"ci-audit: python3 {shlex.quote(str(audit_script))} "
            f"--controller-label {label} --since 24h"
        )
    return str(item["id"])


def _lane_hint(controller_label: str) -> str:
    task_script = Path(__file__).resolve().parents[1] / "goalflight_task.py"
    return (
        f'LANE unset -> run: python3 {task_script} lanes set-charter '
        f'{shlex.quote(controller_label)} <lane> --charter "<3-5 lines>"'
    )


def due_items(
    project_root: str | Path,
    controller_label: str,
    *,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    store = goalflight_task.TaskStore(Path(project_root))
    due, _overdue_duties = _due_for_label(store, controller_label, _now(now))
    return due


def _require_item(item_id: str) -> tuple[str, int]:
    item = DEFAULT_CADENCES.get(item_id)
    if item is None:
        raise CadenceError(f"unknown cadence item: {item_id}")
    return item


def mark_done(
    project_root: str | Path,
    item_id: str,
    controller_label: str,
    *,
    now: datetime | None = None,
) -> None:
    if item_id.startswith("wake:"):
        _prefix, duty_id, period = item_id.split(":", 2)
        _require_item(duty_id)
        if not period:
            raise CadenceError("wake period is required")
        store = goalflight_task.TaskStore(Path(project_root))

        def record_wake(state: dict[str, Any]) -> None:
            _normalise_state(state)
            state["duty_wakes"][duty_id] = period

        try:
            store.mutate_controller_cadence(record_wake)
        except goalflight_task.TaskError as exc:
            raise CadenceError(str(exc)) from exc
        return
    scope, _seconds = _require_item(item_id)
    current = _now(now)
    store = goalflight_task.TaskStore(Path(project_root))

    def update(state: dict[str, Any]) -> None:
        _normalise_state(state)
        if scope == "project":
            cache: dict[str, tuple[str, dict[str, Any] | None]] = {}
            owner = _resolve_owner(store.project_root, state, cache)
            if controller_label != owner:
                raise CadenceError("only the project-duty owner may mark this item done")
            scope_key = "project"
        else:
            scope_key = controller_label
        per_scope = state["last_done"].setdefault(item_id, {})
        if not isinstance(per_scope, dict):
            raise CadenceError(f"invalid last-done entry: {item_id}")
        per_scope[scope_key] = _iso(current)

    try:
        store.mutate_controller_cadence(update)
    except goalflight_task.TaskError as exc:
        raise CadenceError(str(exc)) from exc


def set_interval(item_id: str, seconds: int, project_root: str | Path) -> None:
    _require_item(item_id)
    if isinstance(seconds, bool) or not isinstance(seconds, int) or seconds <= 0:
        raise CadenceError("interval seconds must be positive")
    store = goalflight_task.TaskStore(Path(project_root))

    def update(state: dict[str, Any]) -> None:
        _normalise_state(state)
        state["intervals_seconds"][item_id] = int(seconds)

    try:
        store.mutate_controller_cadence(update)
    except goalflight_task.TaskError as exc:
        raise CadenceError(str(exc)) from exc


def _clip(value: object, max_bytes: int) -> str:
    text = " ".join(str(value or "").split()).replace("|", "/")
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    return encoded[: max(0, max_bytes - 3)].decode("utf-8", "ignore").rstrip() + "…"


def _wake_period(duty: dict[str, Any]) -> str:
    period = duty["period"]
    return f"{period['last_done_at'] or 'never'}/{period['interval_seconds']}"


def _wake_owner_line(
    project_root: Path, controller_label: str, duty: dict[str, Any]
) -> str:
    owner = str(duty["owner"])
    item_id = str(duty["id"])
    period = _wake_period(duty)
    stream_id = "cadence-" + hashlib.sha256(
        str(project_root.resolve()).encode("utf-8")
    ).hexdigest()[:16]
    post = " ".join(shlex.quote(part) for part in (
        "python3", "scripts/goalflight_messages.py", "post",
        "--dispatch-id", stream_id, "--type", "controller-notice",
        "--to-controller", owner, "--text", f"{item_id} overdue 2x",
    ))
    done = " ".join(shlex.quote(part) for part in (
        "python3", "scripts/goalflight_cadence.py", "done",
        f"wake:{item_id}:{period}", "--controller-label", controller_label,
    ))
    return f"WAKE-OWNER: {item_id} overdue 2x -> run: {post} && {done}"


def _pending_wake_owner_lines(
    project_root: Path,
    controller_label: str,
    state: dict[str, Any],
    overdue_duties: list[dict[str, Any]],
) -> tuple[list[str], list[str]]:
    full: list[str] = []
    short: list[str] = []
    for duty in overdue_duties:
        item_id = str(duty["id"])
        period = _wake_period(duty)
        recorded = state["duty_wakes"].get(item_id)
        recorded_period = recorded.get("period") if isinstance(recorded, dict) else recorded
        if recorded_period == period:
            continue
        full.append(_wake_owner_line(project_root, controller_label, duty))
        short.append(_short_wake_owner_line(duty, controller_label))
    return full, short


def _pulse_frontier(
    store: goalflight_task.TaskStore,
    lane: str,
    items: list[dict[str, Any]],
    records: list[dict[str, Any]] | None,
) -> list[dict[str, Any]] | None:
    if records is None:
        return None
    target = goalflight_task._strip_managed_worktree(store.project_root)
    dispatched: set[str] = set()
    for record in records:
        if record.get("superseded_by") or not isinstance(record.get("project_root"), str):
            continue
        if goalflight_task._strip_managed_worktree(Path(record["project_root"])) != target:
            continue
        dispatched.update(goalflight_task._record_task_ids(record))
    by_id = {str(item["id"]): item for item in items}
    return [
        item
        for item in items
        if item.get("kind", "task") in {"task", "bug"}
        and item.get("lane") == lane
        and item.get("lane") not in goalflight_task.RESERVED_LANES
        and not goalflight_task._is_done_reviewed(item)
        and item.get("done") is not True
        and not goalflight_task._latest_dispatch_breadcrumb(item)
        and str(item.get("id") or "") not in dispatched
        and not goalflight_task.unsatisfied_blockers(item, by_id)
    ]


def _short_wake_owner_line(duty: dict[str, Any], controller_label: str) -> str:
    item_id = str(duty["id"])
    return (
        f"WAKE-OWNER: {item_id} overdue 2x -> python3 scripts/goalflight_cadence.py "
        f"due --controller-label {controller_label}"
    )


def _controller_active_since(
    records: list[dict[str, Any]] | None, label: str, since: datetime
) -> bool | None:
    if records is None:
        return None
    if any(goalflight_ledger.record_is_unreadable(record) for record in records):
        return None
    for record in records:
        if str(record.get("controller_label") or "") != label:
            continue
        for key in ("started_at", "terminal_at"):
            timestamp = _parse_time(record.get(key))
            if timestamp is not None and timestamp >= since:
                return True
    return False


def _pulse_text(
    *,
    wake_lines: list[str],
    short_wake_lines: list[str],
    due_text: str,
    lane_line: str,
    capacity_line: str,
    idle_line: str,
    next_line: str,
) -> str:
    required_wakes = wake_lines
    mandatory = [*required_wakes, *([due_text] if due_text else [])]
    if sum(len(line.encode("utf-8")) + (1 if index else 0) for index, line in enumerate(mandatory)) > PULSE_TEXT_MAX_BYTES:
        required_wakes = short_wake_lines
        mandatory = [*required_wakes, *([due_text] if due_text else [])]

    lines = list(mandatory)
    for candidate in (lane_line, capacity_line):
        if candidate and len(lines) < 5:
            text = "\n".join([*lines, candidate])
            if len(text.encode("utf-8")) <= PULSE_TEXT_MAX_BYTES:
                lines.append(candidate)

    if idle_line and len(lines) < 5:
        text = "\n".join([*lines, idle_line])
        if len(text.encode("utf-8")) <= PULSE_TEXT_MAX_BYTES:
            lines.append(idle_line)

    if next_line and len(lines) < 5:
        variants = [next_line]
        if next_line.startswith("NEXT: "):
            ids_only = "NEXT: " + " ".join(
                part.split()[0] for part in next_line[6:].split(" | ") if part.split()
            )
            variants.extend([ids_only, "NEXT: -"])
        for candidate in variants:
            text = "\n".join([*lines, candidate])
            if len(text.encode("utf-8")) <= PULSE_TEXT_MAX_BYTES:
                lines.append(candidate)
                break
    return "\n".join(lines)


def build_pulse_record(
    project_root: str | Path,
    controller_label: str,
    *,
    now: datetime | None = None,
    deadline: float | None = None,
) -> dict[str, object]:
    root = Path(project_root)
    current = _now(now)
    deadline = deadline if deadline is not None else time.monotonic() + 3.0
    try:
        store = goalflight_task.TaskStore(root)
        state = _load_state(store)
        lane_entry = state["lanes"].get(controller_label)
        chartered = isinstance(lane_entry, dict)
        lane = str(lane_entry.get("lane") or controller_label) if chartered else controller_label
        charter = lane_entry.get("charter") if chartered else None
        first_charter_line = (
            _clip(charter[0], 60)
            if isinstance(charter, list) and charter and isinstance(charter[0], str)
            else "unset"
        )

        due, overdue_duties = _due_for_label(
            store,
            controller_label,
            current,
            state=state,
            owner_cache=_pulse_owner_cache(store, state),
        )
        due_ids = " ".join(str(item["id"]) for item in due)
        due_text = f"DUE: {due_ids}" if due_ids else ""
        wake_lines, short_wake_lines = _pending_wake_owner_lines(
            root, controller_label, state, overdue_duties
        )
        lane_line = (
            f"LANE {_clip(lane, 20)}: {first_charter_line}"
            if chartered else _lane_hint(controller_label)
        )

        # The full ledger and task frontier can be slow. Keep them off the
        # caller's deadline and publish their fields only when they finish.
        optional: dict[str, object] = {}
        finished = threading.Event()

        def load_optional_fields() -> None:
            try:
                try:
                    items = store.load_items(recover_publish=False)
                except (goalflight_task.TaskError, OSError):
                    items = None
                try:
                    all_records: list[dict[str, Any]] | None = goalflight_ledger.read_records()
                except Exception:
                    all_records = None

                workers: int | None = None
                if all_records is not None and not any(
                    goalflight_ledger.record_is_unreadable(record)
                    for record in all_records
                ):
                    target = goalflight_task._strip_managed_worktree(store.project_root)
                    workers = len({
                        str(record.get("dispatch_id"))
                        for record in all_records
                        if str(record.get("controller_label") or "") == controller_label
                        and isinstance(record.get("project_root"), str)
                        and goalflight_task._strip_managed_worktree(Path(record["project_root"])) == target
                        and record.get("state") in {
                            "working", "running", "running_quiet", "starting",
                            "waiting", "queued", "waiting_capacity", "handshaking",
                        }
                        and record.get("dispatch_id")
                    })

                frontier = (
                    _pulse_frontier(store, lane, items, all_records)
                    if items is not None and chartered
                    else None
                )
                ready_count = len(frontier) if frontier is not None else None
                if frontier is None:
                    next_text = "?"
                elif not frontier:
                    next_text = "-"
                else:
                    next_text = " | ".join(
                        f"{_clip(row.get('id'), 10)} {_clip(row.get('title'), 20)}"
                        for row in frontier[:3]
                    )
                active = _controller_active_since(
                    all_records,
                    controller_label,
                    current - timedelta(seconds=IDLE_ACTIVITY_WINDOW_S),
                )
                idle = bool(ready_count and workers == 0 and active is False)
                optional.update(
                    idle_line=(
                        f"IDLE: {ready_count} ready, 0 workers; dispatch or record why not"
                        if idle and chartered else ""
                    ),
                    next_line=f"NEXT: {next_text}" if items is not None and chartered else "",
                )
            except BaseException:
                # Optional fields are omitted on failure; due remains useful.
                pass
            finally:
                finished.set()

        threading.Thread(
            target=load_optional_fields, name="goalflight-pulse-fields", daemon=True
        ).start()
        finished.wait(max(0.0, deadline - 0.5 - time.monotonic()))

        text = _pulse_text(
            wake_lines=wake_lines,
            short_wake_lines=short_wake_lines,
            due_text=_clip(due_text, 160) if due_text else "",
            lane_line=lane_line,
            capacity_line="",
            idle_line=str(optional.get("idle_line", "")),
            next_line=str(optional.get("next_line", "")),
        )
        return {"kind": "pulse", "text": text}
    except Exception as exc:
        if isinstance(exc, TimeoutError):
            category = "deadline"
        elif isinstance(exc, goalflight_task.TaskError):
            category = "task-store-error"
        elif isinstance(exc, OSError):
            category = "store-unreadable"
        elif isinstance(exc, CadenceError):
            category = "cadence-state-invalid"
        else:
            category = "internal"
        reason = f"{category}: {type(exc).__name__}"
        return {"kind": "pulse", "unavailable": reason}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", default=str(Path.cwd()))
    sub = parser.add_subparsers(dest="command", required=True)
    due = sub.add_parser("due", help="Print due cadence items for one controller.")
    due.add_argument("--controller-label", required=True)
    done = sub.add_parser("done", help="Record completion of one cadence item.")
    done.add_argument("item")
    done.add_argument("--controller-label", required=True)
    interval = sub.add_parser("set-interval", help="Tune one of the four cadence intervals, in seconds.")
    interval.add_argument("item")
    interval.add_argument("seconds", type=int)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        root = goalflight_task.resolve_project_root(args.project_root)
        if args.command == "due":
            store = goalflight_task.TaskStore(root)
            state = _load_state(store)
            due, overdue_duties = _due_for_label(
                store, args.controller_label, _now(), state=state
            )
            for item in due:
                print(_due_instruction(item, args.controller_label))
            if not isinstance(state["lanes"].get(args.controller_label), dict):
                print(_lane_hint(args.controller_label))
            wake_lines, _short_wake_lines = _pending_wake_owner_lines(
                root, args.controller_label, state, overdue_duties
            )
            for line in wake_lines:
                print(line)
            return 0
        if args.command == "done":
            mark_done(root, args.item, args.controller_label)
            print(f"done {args.item}")
            return 0
        if args.command == "set-interval":
            set_interval(args.item, args.seconds, root)
            print(f"interval {args.item}={args.seconds}s")
            return 0
        raise CadenceError(f"unknown command: {args.command}")
    except (CadenceError, goalflight_task.TaskError, OSError, ValueError) as exc:
        print(f"goalflight_cadence: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
