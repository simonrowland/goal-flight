#!/usr/bin/env python3
"""Goal Flight task-backlog store and mirror writer."""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
from typing import Any, Callable
from urllib.parse import quote


ROOT = Path(__file__).resolve().parent
LIST_TYPE = list
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import goalflight_compat as fcntl  # noqa: E402

try:  # noqa: E402
    import goalflight_dispatch_states
    import goalflight_ledger
except Exception:  # pragma: no cover - import failures surface at runtime.
    goalflight_dispatch_states = None  # type: ignore[assignment]
    goalflight_ledger = None  # type: ignore[assignment]


CHECKER = ROOT / "scripts" / "check_tasks_mirror.js"
ITEM_SCHEMA_VERSION = 1
FAMILY_PREFIX_BY_KIND = {"task": "t", "bug": "b", "decision": "q"}
VALID_FAMILIES = {"t", "b", "q", "ADR"}
TASK_DISPATCH_STATES = {"working", "worker-finished", "worker-failed"}
ITEM_ID_RE = re.compile(r"\b((?:ADR|bp|[tbq])-\d+)\b", re.IGNORECASE)
CANNED_LIST_STATUSES = {
    "outstanding",
    "awaiting-review",
    "working",
    "delegated",
    "waiting",
    "done-reviewed",
}
MARKDOWN_SECTIONS = (
    ("pending", "To do"),
    ("working", "In progress"),
    ("awaiting-review", "Awaiting review"),
    ("worker-failed", "Failed / needs attention"),
    ("waiting", "Waiting"),
)
STATUS_LABELS = {
    "pending": "to do",
    "working": "in progress",
    "awaiting-review": "awaiting review",
    "waiting": "waiting",
    "done-reviewed": "done reviewed",
    "worker-failed": "worker failed",
}


class TaskError(Exception):
    """User-facing task-store failure."""


class FileLock:
    def __init__(self, path: Path):
        self.path = path
        self._fh = None

    def __enter__(self) -> "FileLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = self.path.open("a+", encoding="utf-8")
        fcntl.flock(self._fh, fcntl.LOCK_EX)
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        assert self._fh is not None
        fcntl.flock(self._fh, fcntl.LOCK_UN)
        self._fh.close()


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def _resolve_loose(path: Path) -> Path:
    return path.expanduser().resolve(strict=False)


def _strip_managed_worktree(path: Path) -> Path:
    resolved = _resolve_loose(path)
    parts = resolved.parts
    for index, part in enumerate(parts[:-2]):
        if part == ".claude" and parts[index + 1] == "worktrees":
            return _resolve_loose(Path(*parts[:index]))
    return resolved


def resolve_project_root(value: str | None = None) -> Path:
    explicit = value or os.environ.get("GOALFLIGHT_PROJECT_ROOT")
    if explicit:
        return _strip_managed_worktree(Path(explicit))

    cwd = Path.cwd()
    try:
        top = Path(
            subprocess.check_output(
                ["git", "rev-parse", "--show-toplevel"],
                cwd=str(cwd),
                text=True,
                stderr=subprocess.DEVNULL,
            ).strip()
        )
        common_raw = subprocess.check_output(
            ["git", "rev-parse", "--git-common-dir"],
            cwd=str(cwd),
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        return _strip_managed_worktree(cwd)

    common = Path(common_raw)
    if not common.is_absolute():
        common = top / common
    if common.name == ".git":
        return _strip_managed_worktree(common.parent)
    return _strip_managed_worktree(top)


def _actor(args: argparse.Namespace) -> str:
    if getattr(args, "by", None):
        return args.by
    dispatch_id = os.environ.get("GOALFLIGHT_DISPATCH_ID")
    if dispatch_id:
        return f"worker:{dispatch_id}"
    return os.environ.get("GOALFLIGHT_TASK_ACTOR") or "controller"


def _audit(action: str, actor: str, **extra: Any) -> dict[str, Any]:
    entry: dict[str, Any] = {"at": utc_now(), "actor": actor, "action": action}
    entry.update({k: v for k, v in extra.items() if v not in (None, [], {})})
    return entry


def _append_audit(item: dict[str, Any], action: str, actor: str, **extra: Any) -> None:
    audit = item.setdefault("audit", [])
    if not isinstance(audit, LIST_TYPE):
        raise TaskError(f"item {item.get('id', '<unknown>')}: audit must be an array")
    audit.append(_audit(action, actor, **extra))


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _item_json_line(item: dict[str, Any]) -> str:
    return json.dumps(item, ensure_ascii=False, separators=(",", ":"))


def _items_jsonl(items: list[dict[str, Any]]) -> str:
    if not items:
        return ""
    return "".join(_item_json_line(item) + "\n" for item in items)


def _items_data_js(items: list[dict[str, Any]]) -> str:
    payload = _json_for_script(items)
    return (
        "// tasks-data.js - generated by goalflight_task.py; do not edit by hand.\n"
        "window.GF_ITEMS = "
        + payload
        + ";\n"
        "if (typeof module !== \"undefined\" && module.exports) { module.exports = window.GF_ITEMS; }\n"
    )


def _json_for_script(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, indent=2)
    return (
        payload.replace("&", "\\u0026")
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )


def _md_text(value: Any) -> str:
    text = str(value if value is not None else "")
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _id_lookup(items: list[dict[str, Any]]) -> dict[str, str]:
    return {str(item.get("id", "")).lower(): str(item.get("id", "")) for item in items if item.get("id")}


def _canonical_item_id(value: str, lookup: dict[str, str]) -> str:
    return lookup.get(value.lower(), value)


def _ticket_link(item_id: str) -> str:
    return f"[{_md_text(item_id)}](ticket.html?id={quote(item_id, safe='')})"


def _md_autolink_ids(value: Any, lookup: dict[str, str]) -> str:
    text = _md_text(value)
    return ITEM_ID_RE.sub(lambda match: _ticket_link(_canonical_item_id(match.group(1), lookup)), text)


def _md_value_link(value: Any, lookup: dict[str, str]) -> str:
    text = str(value if value is not None else "")
    if ITEM_ID_RE.fullmatch(text):
        return _ticket_link(_canonical_item_id(text, lookup))
    return _md_autolink_ids(text, lookup)


def _md_list(values: Any, lookup: dict[str, str]) -> str:
    if not isinstance(values, LIST_TYPE) or not values:
        return ""
    return ", ".join(_md_value_link(value, lookup) for value in values if value not in (None, ""))


def _section_rows(rows: list[dict[str, Any]], section: str) -> list[dict[str, Any]]:
    return [row for row in rows if row.get("derived_status") == section]


def _parse_jsonl(path: Path) -> tuple[list[dict[str, Any]], dict[str, int]]:
    if not path.exists():
        return [], {}
    items: list[dict[str, Any]] = []
    lines_by_id: dict[str, int] = {}
    for lineno, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not raw.strip():
            continue
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise TaskError(f"{path}:{lineno}: invalid JSON: {exc.msg}") from exc
        if not isinstance(obj, dict):
            raise TaskError(f"{path}:{lineno}: expected a JSON object")
        item_id = obj.get("id")
        if not isinstance(item_id, str) or not item_id:
            raise TaskError(f"{path}:{lineno}: missing string id")
        if "status" in obj:
            raise TaskError(f"{path}:{lineno}: top-level status key is forbidden")
        if item_id in lines_by_id:
            raise TaskError(f"{path}:{lineno}: duplicate id {item_id} (first seen on line {lines_by_id[item_id]})")
        lines_by_id[item_id] = lineno
        items.append(_migrate_item_for_read(obj))
    return items, lines_by_id


def _migrate_item_for_read(item: dict[str, Any]) -> dict[str, Any]:
    migrated = dict(item)
    legacy = "schema_version" not in migrated
    version = migrated.get("schema_version")
    if not isinstance(version, int) or version < 1:
        migrated["schema_version"] = ITEM_SCHEMA_VERSION
    migrated.setdefault("blocked_by", [])
    migrated.setdefault("links", [])
    migrated.setdefault("done", False)
    if legacy and migrated.get("done") is True and "done_reviewed" not in migrated:
        migrated["done_reviewed"] = True
    return migrated


def _validate_items_for_write(items: list[dict[str, Any]], source: str = "tasks.jsonl") -> None:
    seen: dict[str, int] = {}
    for lineno, item in enumerate(items, start=1):
        if not isinstance(item, dict):
            raise TaskError(f"{source}: line {lineno}: expected a JSON object")
        legacy = "schema_version" not in item
        item_id = item.get("id")
        if not isinstance(item_id, str) or not item_id:
            raise TaskError(f"{source}: line {lineno}: missing string id")
        if "status" in item:
            raise TaskError(f"{source}: line {lineno}: item {item_id} carries forbidden top-level status key")
        if item_id in seen:
            raise TaskError(f"{source}: line {lineno}: duplicate id {item_id} (first seen on line {seen[item_id]})")
        seen[item_id] = lineno
        item.setdefault("blocked_by", [])
        item.setdefault("links", [])
        item.setdefault("done", False)
        if legacy and item.get("done") is True and "done_reviewed" not in item:
            item["done_reviewed"] = True
        item.setdefault("schema_version", ITEM_SCHEMA_VERSION)
        if not isinstance(item["blocked_by"], LIST_TYPE):
            raise TaskError(f"{source}: line {lineno}: item {item_id} blocked_by must be an array")
        if not isinstance(item["links"], LIST_TYPE):
            raise TaskError(f"{source}: line {lineno}: item {item_id} links must be an array")
        if not isinstance(item["schema_version"], int) or item["schema_version"] < 1:
            raise TaskError(f"{source}: line {lineno}: item {item_id} schema_version must be a positive integer")
        if "dispatches" in item and not isinstance(item["dispatches"], LIST_TYPE):
            raise TaskError(f"{source}: line {lineno}: item {item_id} dispatches must be an array")


def _run_checker(directory: Path) -> None:
    node = shutil.which("node")
    if not node:
        raise TaskError("scripts/check_tasks_mirror.js: node not found; refusing to write without strict mirror validation")
    if not CHECKER.is_file():
        raise TaskError(f"{CHECKER}: missing strict mirror checker")
    proc = subprocess.run(
        [node, str(CHECKER), str(directory)],
        cwd=str(ROOT),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=30,
        check=False,
    )
    if proc.returncode != 0:
        msg = (proc.stderr or proc.stdout).strip() or f"checker exited {proc.returncode}"
        raise TaskError(msg)


def _parse_time(value: Any) -> dt.datetime:
    if not isinstance(value, str) or not value:
        return dt.datetime.min.replace(tzinfo=dt.timezone.utc)
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return dt.datetime.min.replace(tzinfo=dt.timezone.utc)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def _record_sort_time(record: dict[str, Any]) -> dt.datetime:
    for key in ("ts", "updated_at", "ended_at", "started_at"):
        parsed = _parse_time(record.get(key))
        if parsed != dt.datetime.min.replace(tzinfo=dt.timezone.utc):
            return parsed
    return dt.datetime.min.replace(tzinfo=dt.timezone.utc)


def _record_terminal_state(record: dict[str, Any]) -> str:
    terminal = record.get("terminal_state")
    if isinstance(terminal, str) and terminal:
        return terminal
    if goalflight_ledger is not None:
        return str(goalflight_ledger.terminal_state_for(record.get("state"), record.get("reason") or record.get("error")))
    return "unknown"


def _record_task_ids(record: dict[str, Any]) -> list[str]:
    out: list[str] = []
    plural = record.get("task_ids")
    values = plural if isinstance(plural, LIST_TYPE) else ([plural] if isinstance(plural, str) else [])
    legacy = record.get("task_id")
    if isinstance(legacy, str):
        values = [legacy, *values]
    for value in values:
        if not isinstance(value, str):
            continue
        for part in value.split(","):
            task_id = part.strip()
            if task_id and task_id not in out:
                out.append(task_id)
    return out


def _dispatch_status_from_record(record: dict[str, Any], *, live: bool) -> str | None:
    raw_state = record.get("state")
    if isinstance(raw_state, str) and raw_state in TASK_DISPATCH_STATES:
        return raw_state
    terminal = _record_terminal_state(record)
    normalized = None
    if goalflight_dispatch_states is not None:
        normalized = goalflight_dispatch_states.normalize_dispatch_state(raw_state)

    if terminal == "complete" or normalized == "complete":
        return "worker-finished"
    if terminal not in {"", "unknown", None}:
        return "worker-failed"
    if isinstance(raw_state, str) and raw_state.startswith("blocked"):
        return "worker-failed"

    if live:
        if normalized in {"waiting", "starting", "running", "running_quiet"} or raw_state in {
            "queued",
            "waiting_capacity",
            "handshaking",
        }:
            if goalflight_ledger is not None:
                classification = goalflight_ledger.classify(record)
                if classification.startswith("stale_") or classification in {"unknown_no_pid", "watcher_stopped"}:
                    return "worker-failed"
            return "working"
        return "worker-failed"

    return None


def _status_snapshot_from_record(record: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "dispatch_id",
        "agent",
        "shape",
        "state",
        "terminal_state",
        "reason",
        "outcome",
        "worker_pid",
        "worker_alive",
        "worker_still_alive",
        "stdout_path",
        "stderr_path",
        "status_path",
        "updated_at",
        "started_at",
        "ended_at",
    )
    return {key: record.get(key) for key in keys if record.get(key) not in (None, "", [], {})}


def _breadcrumb_from_record(record: dict[str, Any]) -> dict[str, Any]:
    state = (
        _dispatch_status_from_record(record, live=True)
        or _dispatch_status_from_record(record, live=False)
        or record.get("state")
    )
    ts = record.get("ended_at") or record.get("updated_at") or record.get("started_at") or utc_now()
    crumb: dict[str, Any] = {
        "dispatch_id": record.get("dispatch_id"),
        "agent": record.get("agent"),
        "log": record.get("stdout_path") or record.get("log"),
        "stdout_path": record.get("stdout_path"),
        "stderr_path": record.get("stderr_path"),
        "status_path": record.get("status_path"),
        "started_at": record.get("started_at"),
        "ended_at": record.get("ended_at"),
        "state": state,
        "ts": ts,
        "terminal_state": _record_terminal_state(record),
        "marker": record.get("marker"),
        "worker_pid": record.get("worker_pid"),
        "hostname": record.get("hostname"),
        "last_worker_state": record.get("last_worker_state") or _status_snapshot_from_record(record),
    }
    return {k: v for k, v in crumb.items() if v not in (None, "", [], {})}


def _breadcrumb_key(crumb: dict[str, Any]) -> str:
    marker = crumb.get("marker")
    marker_key = _canonical_json(marker) if isinstance(marker, dict) else str(marker or "")
    return "|".join(
        [
            str(crumb.get("dispatch_id") or ""),
            str(crumb.get("state") or ""),
            str(crumb.get("ts") or ""),
            marker_key,
        ]
    )


def _latest_dispatch_breadcrumb(item: dict[str, Any]) -> dict[str, Any] | None:
    dispatches = item.get("dispatches")
    if not isinstance(dispatches, LIST_TYPE):
        return None
    candidates = []
    for crumb in dispatches:
        if not isinstance(crumb, dict):
            continue
        if _dispatch_status_from_record(crumb, live=False):
            candidates.append(crumb)
    if not candidates:
        return None
    return sorted(candidates, key=_record_sort_time)[-1]


def _latest_breadcrumb(item: dict[str, Any], *, role: str | None = None) -> dict[str, Any] | None:
    dispatches = item.get("dispatches")
    if not isinstance(dispatches, LIST_TYPE):
        return None
    candidates = []
    for crumb in dispatches:
        if not isinstance(crumb, dict):
            continue
        if role is not None and crumb.get("role") != role:
            continue
        candidates.append(crumb)
    if not candidates:
        return None
    return sorted(candidates, key=_record_sort_time)[-1]


def _latest_review_breadcrumb(item: dict[str, Any]) -> dict[str, Any] | None:
    return _latest_breadcrumb(item, role="review")


def _iso_from_epoch(epoch: int) -> str | None:
    if epoch <= 0:
        return None
    return dt.datetime.fromtimestamp(epoch, tz=dt.timezone.utc).isoformat(timespec="seconds")


def _epoch_from_time(value: Any) -> int:
    parsed = _parse_time(value)
    if parsed == dt.datetime.min.replace(tzinfo=dt.timezone.utc):
        return 0
    return int(parsed.timestamp())


def _parse_since(value: str | None) -> int | None:
    if value in (None, ""):
        return None
    raw = str(value).strip()
    now = int(dt.datetime.now(dt.timezone.utc).timestamp())
    match = re.fullmatch(r"(\d+)([smhdw])", raw)
    if match:
        amount = int(match.group(1))
        multiplier = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}[match.group(2)]
        return now - amount * multiplier
    match = re.fullmatch(r"now([+-])(\d+)", raw)
    if match:
        delta = int(match.group(2))
        return now - delta if match.group(1) == "-" else now + delta
    if raw.isdigit():
        return int(raw)
    parsed = _parse_time(raw)
    if parsed == dt.datetime.min.replace(tzinfo=dt.timezone.utc):
        raise TaskError(f"invalid --since value {value!r}; use 1h, now-3600, epoch seconds, or an ISO timestamp")
    return int(parsed.timestamp())


def _item_query_epoch(item: dict[str, Any]) -> int:
    epochs = [_epoch_from_time(item.get("created_at"))]
    crumb = _latest_breadcrumb(item)
    if crumb:
        epochs.append(_epoch_from_time(crumb.get("ts") or crumb.get("ended_at") or crumb.get("updated_at") or crumb.get("started_at")))
    return max(epochs) if epochs else 0


def _item_dispatch_epoch(item: dict[str, Any]) -> int:
    crumb = _latest_dispatch_breadcrumb(item)
    if not crumb:
        return 0
    return _epoch_from_time(crumb.get("ts") or crumb.get("ended_at") or crumb.get("updated_at") or crumb.get("started_at"))


def _is_done_reviewed(item: dict[str, Any]) -> bool:
    if item.get("done_reviewed") is True:
        return True
    return item.get("kind") == "decision" and item.get("done") is True


def _matches_canned_status(row: dict[str, Any], status: str | None) -> bool:
    if not status:
        return True
    if status == "outstanding":
        return not _is_done_reviewed(row)
    if status == "awaiting-review":
        return not _is_done_reviewed(row) and row.get("derived_status") == "awaiting-review"
    if status == "working":
        return not _is_done_reviewed(row) and row.get("derived_status") == "working"
    if status == "waiting":
        return not _is_done_reviewed(row) and row.get("derived_status") == "waiting"
    if status == "delegated":
        return _latest_dispatch_breadcrumb(row) is not None
    if status == "done-reviewed":
        return _is_done_reviewed(row)
    raise TaskError(f"unknown list status {status!r}; expected one of {', '.join(sorted(CANNED_LIST_STATUSES))}")


def _coerce_query(query: Any = None, **overrides: Any) -> dict[str, Any]:
    if query is None:
        out: dict[str, Any] = {}
    elif isinstance(query, str):
        out = {"status": query}
    elif isinstance(query, dict):
        out = dict(query)
    else:
        raise TaskError("query must be None, a canned status string, or a dict")
    out.update({k: v for k, v in overrides.items() if v not in (None, [], {})})
    blocked_by = out.get("blocked_by")
    if isinstance(blocked_by, str):
        out["blocked_by"] = _split_csv([blocked_by])
    elif isinstance(blocked_by, LIST_TYPE):
        values: list[str] = []
        for value in blocked_by:
            if isinstance(value, str):
                values.extend(_split_csv([value]))
        out["blocked_by"] = values
    status = out.get("status")
    if status not in (None, "") and status not in CANNED_LIST_STATUSES:
        raise TaskError(f"unknown list status {status!r}; expected one of {', '.join(sorted(CANNED_LIST_STATUSES))}")
    if out.get("since") not in (None, ""):
        out["since_epoch"] = _parse_since(str(out["since"]))
    return out


class TaskStore:
    def __init__(self, project_root: Path):
        self.project_root = _strip_managed_worktree(project_root)
        self.docs_dir = self.project_root / "docs-private"
        self.tasks_path = self.docs_dir / "tasks.jsonl"
        self.data_js_path = self.docs_dir / "tasks-data.js"
        self.task_decomposition_path = self.docs_dir / "task-decomposition.md"
        self.tasks_done_path = self.docs_dir / "tasks-done.md"
        self.bug_backlog_path = self.docs_dir / "bug-backlog.md"
        self.bugs_done_path = self.docs_dir / "bugs-done.md"
        self.store_lock_path = self.docs_dir / "tasks.lock"
        self.seq_path = self.docs_dir / ".task-seq"
        self.seq_lock_path = self.docs_dir / ".task-seq.lock"
        self.log_dir = self.docs_dir / "log"

    @contextlib.contextmanager
    def store_lock(self):
        self.docs_dir.mkdir(parents=True, exist_ok=True)
        with FileLock(self.store_lock_path):
            yield

    def load_items(self) -> list[dict[str, Any]]:
        items, _ = _parse_jsonl(self.tasks_path)
        return items

    def items_by_id(self) -> dict[str, dict[str, Any]]:
        return {item["id"]: item for item in self.load_items()}

    def reserve_id(self, family: str) -> str:
        if family not in VALID_FAMILIES:
            raise TaskError(f"unknown id family {family!r}; expected one of {', '.join(sorted(VALID_FAMILIES))}")
        self.docs_dir.mkdir(parents=True, exist_ok=True)
        with FileLock(self.seq_lock_path):
            seq = self._read_seq()
            current = int(seq.get(family, 0))
            next_value = current + 1
            seq[family] = next_value
            tmp = self.seq_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(seq, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            tmp.replace(self.seq_path)
        return f"ADR-{next_value:03d}" if family == "ADR" else f"{family}-{next_value:03d}"

    def _read_seq(self) -> dict[str, int]:
        if not self.seq_path.exists():
            return {}
        text = self.seq_path.read_text(encoding="utf-8").strip()
        if not text:
            return {}
        if text.isdigit():
            return {"t": int(text)}
        try:
            raw = json.loads(text)
        except json.JSONDecodeError as exc:
            raise TaskError(f"{self.seq_path}:1: invalid JSON: {exc.msg}") from exc
        if not isinstance(raw, dict):
            raise TaskError(f"{self.seq_path}:1: expected a JSON object")
        out: dict[str, int] = {}
        for key, value in raw.items():
            if key not in VALID_FAMILIES:
                raise TaskError(f"{self.seq_path}:1: unknown id family {key!r}")
            if not isinstance(value, int) or value < 0:
                raise TaskError(f"{self.seq_path}:1: family {key!r} must be a non-negative integer")
            out[key] = value
        return out

    def mutate_items(self, update: Callable[[list[dict[str, Any]]], Any], *, allow_invalid_live_mirror: bool = False) -> Any:
        with self.store_lock():
            self._snapshot_last_good(require_valid=not allow_invalid_live_mirror)
            items = self.load_items()
            result = update(items)
            self.save_items_atomic(items)
            return result

    def save_items_atomic(self, items: list[dict[str, Any]]) -> None:
        self.docs_dir.mkdir(parents=True, exist_ok=True)
        _validate_items_for_write(items)
        staging = Path(tempfile.mkdtemp(prefix=".tasks-stage-", dir=str(self.docs_dir)))
        try:
            (staging / "tasks.jsonl").write_text(_items_jsonl(items), encoding="utf-8")
            (staging / "tasks-data.js").write_text(_items_data_js(items), encoding="utf-8")
            for name, content in self.generated_markdown(items).items():
                (staging / name).write_text(content, encoding="utf-8")
            _run_checker(staging)
            targets = {
                self.tasks_path: staging / "tasks.jsonl",
                self.data_js_path: staging / "tasks-data.js",
                self.task_decomposition_path: staging / "task-decomposition.md",
                self.tasks_done_path: staging / "tasks-done.md",
                self.bug_backlog_path: staging / "bug-backlog.md",
                self.bugs_done_path: staging / "bugs-done.md",
            }
            old_bytes = {path: path.read_bytes() if path.exists() else None for path in targets}
            try:
                for target, source in targets.items():
                    source.replace(target)
            except OSError:
                for target, data in old_bytes.items():
                    self._restore_bytes(target, data)
                raise
        finally:
            shutil.rmtree(staging, ignore_errors=True)

    @staticmethod
    def _restore_bytes(path: Path, data: bytes | None) -> None:
        if data is None:
            with contextlib.suppress(FileNotFoundError):
                path.unlink()
            return
        tmp = path.with_suffix(path.suffix + ".restore")
        tmp.write_bytes(data)
        tmp.replace(path)

    def _snapshot_last_good(self, *, require_valid: bool = True) -> None:
        if not self.tasks_path.exists() and not self.data_js_path.exists():
            return
        if not self.tasks_path.exists() or not self.data_js_path.exists():
            if not require_valid and self.tasks_path.exists():
                return
            raise TaskError(f"{self.docs_dir}: missing tasks.jsonl or tasks-data.js; refusing mutation without a valid last-known-good pair")
        try:
            _run_checker(self.docs_dir)
        except TaskError:
            if require_valid:
                raise
            return
        self.log_dir.mkdir(parents=True, exist_ok=True)
        stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        shutil.copy2(self.tasks_path, self.log_dir / f"tasks-{stamp}.jsonl")
        shutil.copy2(self.data_js_path, self.log_dir / f"tasks-data-{stamp}.js")
        self._prune_backups()

    def _prune_backups(self, keep: int = 20) -> None:
        for pattern in ("tasks-*.jsonl", "tasks-data-*.js"):
            backups = sorted(self.log_dir.glob(pattern), key=lambda p: p.stat().st_mtime, reverse=True)
            for path in backups[keep:]:
                with contextlib.suppress(OSError):
                    path.unlink()

    def project_ledger_records(self) -> list[dict[str, Any]]:
        if goalflight_ledger is None:
            return []
        target = _strip_managed_worktree(self.project_root)
        records = []
        for record in goalflight_ledger.read_records():
            if not _record_task_ids(record):
                continue
            raw_root = record.get("project_root")
            if not isinstance(raw_root, str) or not raw_root:
                continue
            if _strip_managed_worktree(Path(raw_root)) == target:
                records.append(record)
        return records

    def _records_by_task(self) -> dict[str, list[dict[str, Any]]]:
        records_by_task: dict[str, list[dict[str, Any]]] = {}
        for record in self.project_ledger_records():
            for task_id in _record_task_ids(record):
                records_by_task.setdefault(task_id, []).append(record)
        return records_by_task

    def sync_dispatch_breadcrumbs(self, items: list[dict[str, Any]], actor: str) -> int:
        by_id = {item["id"]: item for item in items}
        changed = 0
        for record in self.project_ledger_records():
            dispatch_id = record.get("dispatch_id")
            if not isinstance(dispatch_id, str):
                continue
            crumb = _breadcrumb_from_record(record)
            for task_id in _record_task_ids(record):
                item = by_id.get(task_id)
                if item is None:
                    continue
                dispatches = item.setdefault("dispatches", [])
                if not isinstance(dispatches, LIST_TYPE):
                    raise TaskError(f"item {task_id}: dispatches must be an array")
                existing_keys = {
                    _breadcrumb_key(entry)
                    for entry in dispatches
                    if isinstance(entry, dict)
                }
                if _breadcrumb_key(crumb) in existing_keys:
                    continue
                dispatches.append(crumb)
                changed += 1
                _append_audit(item, "dispatch-sync", actor, dispatch_id=dispatch_id, state=crumb.get("state"))
        return changed

    def append_dispatch_breadcrumbs(self, task_ids: list[str], breadcrumb: dict[str, Any], actor: str) -> int:
        clean_ids = []
        for task_id in task_ids:
            if isinstance(task_id, str) and task_id and task_id not in clean_ids:
                clean_ids.append(task_id)
        if not clean_ids:
            return 0
        dispatch_id = breadcrumb.get("dispatch_id")
        if not isinstance(dispatch_id, str) or not dispatch_id:
            raise TaskError("dispatch breadcrumb requires dispatch_id")
        state = breadcrumb.get("state")
        if not isinstance(state, str) or state not in TASK_DISPATCH_STATES:
            raise TaskError(f"dispatch breadcrumb requires state in {sorted(TASK_DISPATCH_STATES)}")
        crumb = {k: v for k, v in dict(breadcrumb).items() if v not in (None, "", [], {})}
        crumb.setdefault("ts", utc_now())

        def update(items: list[dict[str, Any]]) -> int:
            by_id = {item["id"]: item for item in items}
            changed = 0
            for task_id in clean_ids:
                item = by_id.get(task_id)
                if item is None:
                    raise TaskError(f"{self.tasks_path}: item not found: {task_id}")
                dispatches = item.setdefault("dispatches", [])
                if not isinstance(dispatches, LIST_TYPE):
                    raise TaskError(f"item {task_id}: dispatches must be an array")
                dispatches.append(dict(crumb))
                changed += 1
                _append_audit(item, "dispatch-breadcrumb", actor, dispatch_id=dispatch_id, state=state)
            return changed

        return self.mutate_items(update, allow_invalid_live_mirror=True)

    def derived_rows(self) -> list[dict[str, Any]]:
        return self.derived_rows_for_items(self.load_items())

    def derived_rows_for_items(self, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        records_by_task = self._records_by_task()
        by_id = {item["id"]: item for item in items}
        rows = []
        for item in items:
            item_id = item["id"]
            row = dict(item)
            row["derived_status"] = self._derive_status(item, records_by_task.get(item_id, []), by_id)
            row["query_epoch"] = _item_query_epoch(row)
            query_time = _iso_from_epoch(row["query_epoch"])
            if query_time:
                row["query_time"] = query_time
            rows.append(row)
        return rows

    def get_item(self, item_id: str) -> dict[str, Any]:
        for row in self.derived_rows():
            if row.get("id") == item_id:
                return row
        raise TaskError(f"{self.tasks_path}: item not found: {item_id}")

    def query_items(self, query: Any = None, **overrides: Any) -> list[dict[str, Any]]:
        q = _coerce_query(query, **overrides)
        rows = self.derived_rows()
        status = q.get("status")
        kind = q.get("kind")
        blockers = q.get("blocked_by") or []
        since_epoch = q.get("since_epoch")

        def matches(row: dict[str, Any]) -> bool:
            if status and not _matches_canned_status(row, status):
                return False
            if kind and row.get("kind", "task") != kind:
                return False
            if blockers:
                row_blockers = row.get("blocked_by")
                if not isinstance(row_blockers, LIST_TYPE) or any(blocker not in row_blockers for blocker in blockers):
                    return False
            filter_epoch = _item_dispatch_epoch(row) if status == "delegated" else int(row.get("query_epoch") or 0)
            if since_epoch is not None and filter_epoch < int(since_epoch):
                return False
            return True

        return [row for row in rows if matches(row)]

    def _derive_status(
        self,
        item: dict[str, Any],
        live_records: list[dict[str, Any]],
        by_id: dict[str, dict[str, Any]],
    ) -> str:
        if _is_done_reviewed(item):
            return "done-reviewed"
        candidates: list[tuple[dt.datetime, str]] = []
        if live_records:
            for record in live_records:
                status = _dispatch_status_from_record(record, live=True)
                if status:
                    candidates.append((_record_sort_time(record), status))
        crumb = _latest_dispatch_breadcrumb(item)
        if crumb:
            status = _dispatch_status_from_record(crumb, live=False)
            if status:
                candidates.append((_record_sort_time(crumb), status))
        if item.get("done") is True:
            candidates.append((_record_sort_time({"ts": item.get("done_at") or item.get("closed_at") or item.get("created_at")}), "awaiting-review"))
        if candidates:
            latest = sorted(candidates, key=lambda row: row[0])[-1][1]
            return "awaiting-review" if latest == "worker-finished" else latest
        blockers = item.get("blocked_by")
        if isinstance(blockers, LIST_TYPE) and blockers:
            unresolved = any(not _is_done_reviewed(by_id.get(str(blocker), {})) for blocker in blockers)
            if unresolved:
                return "waiting"
        return "pending"

    def generated_markdown(self, items: list[dict[str, Any]]) -> dict[str, str]:
        rows = self.derived_rows_for_items(items)
        lookup = _id_lookup(items)
        tasks = [row for row in rows if row.get("kind", "task") == "task"]
        bugs = [row for row in rows if row.get("kind") == "bug"]
        return {
            "task-decomposition.md": self._render_task_decomposition(tasks, lookup),
            "tasks-done.md": self._render_tasks_done(tasks, lookup),
            "bug-backlog.md": self._render_bug_backlog(bugs, lookup),
            "bugs-done.md": self._render_bugs_done(bugs, lookup),
        }

    def _render_header(self, title: str) -> list[str]:
        return [
            f"# {title}",
            "",
            "> Generated by `goalflight_task.py sync` from `tasks.jsonl` plus the project dispatch ledger. Do not edit by hand.",
            "> Section authority: `protocols/progress-dashboard.md`.",
            "",
        ]

    def _render_item_block(self, row: dict[str, Any], lookup: dict[str, str], *, done_view: bool = False) -> list[str]:
        item_id = str(row.get("id", ""))
        lines = [f"### {item_id}", "", f"**{_md_autolink_ids(row.get('title', '(untitled)'), lookup)}**", ""]
        if row.get("derived_status") and not done_view:
            lines.append(f"- Status: {_md_text(STATUS_LABELS.get(str(row['derived_status']), row['derived_status']))}")
        blockers = _md_list(row.get("blocked_by"), lookup)
        if blockers:
            lines.append(f"- Blocked by: {blockers}")
        links = _md_list(row.get("links"), lookup)
        if links:
            lines.append(f"- Links: {links}")
        for key, label in (
            ("severity", "Severity"),
            ("pattern", "Pattern"),
            ("source", "Source"),
            ("acceptance", "Acceptance"),
            ("prompt_path", "Prompt"),
        ):
            value = row.get(key)
            if isinstance(value, str) and value:
                lines.append(f"- {label}: {_md_autolink_ids(value, lookup)}")
        if isinstance(row.get("prompt"), str) and row["prompt"]:
            lines.extend(["- Prompt:", ""])
            lines.extend(f"  {line}" if line else "" for line in _md_text(row["prompt"]).splitlines())
        lines.append("")
        return lines

    def _render_sections(
        self,
        rows: list[dict[str, Any]],
        lookup: dict[str, str],
        sections: tuple[tuple[str, str], ...],
        *,
        empty: str,
    ) -> list[str]:
        lines: list[str] = []
        any_rows = False
        for key, label in sections:
            lines.extend([f"## {label}", ""])
            section = _section_rows(rows, key)
            if section:
                any_rows = True
                for row in section:
                    lines.extend(self._render_item_block(row, lookup))
            else:
                lines.extend([empty, ""])
        if not any_rows and not rows:
            return [empty, ""]
        return lines

    def _render_task_decomposition(self, tasks: list[dict[str, Any]], lookup: dict[str, str]) -> str:
        open_tasks = [row for row in tasks if row.get("derived_status") != "done-reviewed"]
        lines = self._render_header("goal-flight — Task Decomposition")
        lines.extend(self._render_sections(open_tasks, lookup, MARKDOWN_SECTIONS, empty="_(none)_"))
        return "\n".join(lines).rstrip() + "\n"

    def _render_tasks_done(self, tasks: list[dict[str, Any]], lookup: dict[str, str]) -> str:
        done = [row for row in tasks if row.get("derived_status") == "done-reviewed"]
        lines = self._render_header("goal-flight — Tasks Done")
        lines.extend(["## Done", ""])
        if done:
            for row in reversed(done):
                lines.extend(self._render_item_block(row, lookup, done_view=True))
        else:
            lines.extend(["_(none)_", ""])
        return "\n".join(lines).rstrip() + "\n"

    def _render_bug_backlog(self, bugs: list[dict[str, Any]], lookup: dict[str, str]) -> str:
        open_bugs = [row for row in bugs if row.get("derived_status") != "done-reviewed"]
        lines = self._render_header("goal-flight — Bug Backlog")
        lines.extend(self._render_sections(open_bugs, lookup, MARKDOWN_SECTIONS, empty="_(none)_"))
        return "\n".join(lines).rstrip() + "\n"

    def _render_bugs_done(self, bugs: list[dict[str, Any]], lookup: dict[str, str]) -> str:
        done = [row for row in bugs if row.get("derived_status") == "done-reviewed"]
        lines = self._render_header("goal-flight — Bugs Done")
        lines.extend(["## Done", ""])
        if done:
            for row in reversed(done):
                lines.extend(self._render_item_block(row, lookup, done_view=True))
        else:
            lines.extend(["_(none)_", ""])
        return "\n".join(lines).rstrip() + "\n"


def _split_csv(values: list[str] | None) -> list[str]:
    out: list[str] = []
    for value in values or []:
        for part in value.split(","):
            part = part.strip()
            if part:
                out.append(part)
    return out


def _cmd_new(store: TaskStore, args: argparse.Namespace) -> int:
    family = args.id_family or FAMILY_PREFIX_BY_KIND[args.kind]
    item_id = store.reserve_id(family)
    actor = _actor(args)

    def update(items: list[dict[str, Any]]) -> str:
        if any(item.get("id") == item_id for item in items):
            raise TaskError(f"{store.tasks_path}: id {item_id} already exists; .task-seq is stale")
        item: dict[str, Any] = {
            "schema_version": ITEM_SCHEMA_VERSION,
            "id": item_id,
            "kind": args.kind,
            "title": args.title,
            "blocked_by": _split_csv(args.blocked_by),
            "links": _split_csv(args.links),
            "done": False,
            "tags": _split_csv(args.tags),
            "created_at": utc_now(),
            "created_by": actor,
            "audit": [_audit("new", actor)],
        }
        for key in ("acceptance", "prompt", "prompt_path", "pattern", "severity", "source"):
            value = getattr(args, key, None)
            if value not in (None, "", [], {}):
                item[key] = value
        items.append(item)
        return item_id

    created = store.mutate_items(update)
    if args.json:
        print(json.dumps({"id": created}, sort_keys=True))
    else:
        print(created)
    return 0


def _cmd_show(store: TaskStore, args: argparse.Namespace) -> int:
    item = store.get_item(args.item_id)
    if args.prompt:
        prompt = item.get("prompt")
        if isinstance(prompt, str):
            print(prompt)
            return 0
        prompt_path = item.get("prompt_path")
        if isinstance(prompt_path, str) and prompt_path:
            path = Path(prompt_path)
            if not path.is_absolute():
                path = store.project_root / path
            print(path.read_text(encoding="utf-8"), end="")
            return 0
        raise TaskError(f"{args.item_id}: no prompt or prompt_path")
    if args.json:
        print(json.dumps(item, ensure_ascii=False, sort_keys=True))
    else:
        print(json.dumps(item, indent=2, ensure_ascii=False, sort_keys=True))
    return 0


def _cmd_block(store: TaskStore, args: argparse.Namespace) -> int:
    blockers = _split_csv(args.on)
    if not blockers:
        raise TaskError("block: --on requires at least one blocker id")
    actor = _actor(args)

    def update(items: list[dict[str, Any]]) -> None:
        by_id = {item["id"]: item for item in items}
        item = by_id.get(args.item_id)
        if item is None:
            raise TaskError(f"{store.tasks_path}: item not found: {args.item_id}")
        blocked_by = item.setdefault("blocked_by", [])
        if not isinstance(blocked_by, LIST_TYPE):
            raise TaskError(f"item {args.item_id}: blocked_by must be an array")
        for blocker in blockers:
            if blocker not in blocked_by:
                blocked_by.append(blocker)
        _append_audit(item, "block", actor, on=blockers)

    store.mutate_items(update)
    print(args.item_id)
    return 0


def _cmd_unblock(store: TaskStore, args: argparse.Namespace) -> int:
    actor = _actor(args)
    remove = set(_split_csv(args.on))

    def update(items: list[dict[str, Any]]) -> None:
        by_id = {item["id"]: item for item in items}
        item = by_id.get(args.item_id)
        if item is None:
            raise TaskError(f"{store.tasks_path}: item not found: {args.item_id}")
        blocked_by = item.setdefault("blocked_by", [])
        if not isinstance(blocked_by, LIST_TYPE):
            raise TaskError(f"item {args.item_id}: blocked_by must be an array")
        item["blocked_by"] = [value for value in blocked_by if remove and value not in remove] if remove else []
        _append_audit(item, "unblock", actor, on=sorted(remove) if remove else None)

    store.mutate_items(update)
    print(args.item_id)
    return 0


def _cmd_done(store: TaskStore, args: argparse.Namespace) -> int:
    actor = _actor(args)

    def update(items: list[dict[str, Any]]) -> None:
        by_id = {item["id"]: item for item in items}
        item = by_id.get(args.item_id)
        if item is None:
            raise TaskError(f"{store.tasks_path}: item not found: {args.item_id}")
        if item.get("done") is True:
            raise TaskError(f"{args.item_id}: already done")
        if item.get("blocked_by") and not args.force:
            raise TaskError(f"{args.item_id}: blocked_by is non-empty; use --force to close anyway")
        item["done"] = True
        item["done_at"] = utc_now()
        item["done_by"] = actor
        item.setdefault("closed_at", item["done_at"])
        item.setdefault("closed_by", actor)
        item["resolution"] = args.resolution
        _append_audit(item, "done", actor, resolution=args.resolution)

    store.mutate_items(update)
    print(args.item_id)
    return 0


def _cmd_review(store: TaskStore, args: argparse.Namespace) -> int:
    actor = _actor(args)
    crumb: dict[str, Any] = {
        "dispatch_id": args.dispatch,
        "role": "review",
        "verdict": args.verdict,
        "findings_ref": args.findings,
        "ts": utc_now(),
    }

    def update(items: list[dict[str, Any]]) -> None:
        by_id = {item["id"]: item for item in items}
        item = by_id.get(args.item_id)
        if item is None:
            raise TaskError(f"{store.tasks_path}: item not found: {args.item_id}")
        dispatches = item.setdefault("dispatches", [])
        if not isinstance(dispatches, LIST_TYPE):
            raise TaskError(f"item {args.item_id}: dispatches must be an array")
        dispatches.append({k: v for k, v in crumb.items() if v not in (None, "", [], {})})
        _append_audit(item, "review", actor, dispatch_id=args.dispatch, verdict=args.verdict, findings_ref=args.findings)

    store.mutate_items(update, allow_invalid_live_mirror=True)
    print(args.item_id)
    return 0


def _cmd_accept(store: TaskStore, args: argparse.Namespace) -> int:
    actor = _actor(args)

    def update(items: list[dict[str, Any]]) -> None:
        rows = store.derived_rows_for_items(items)
        by_id = {row["id"]: row for row in rows}
        row = by_id.get(args.item_id)
        if row is None:
            raise TaskError(f"{store.tasks_path}: item not found: {args.item_id}")
        if _is_done_reviewed(row):
            raise TaskError(f"{args.item_id}: already done-reviewed")
        if row.get("derived_status") != "awaiting-review":
            raise TaskError(f"{args.item_id}: not DONE/awaiting-review; run task done or wait for worker completion first")
        review = _latest_review_breadcrumb(row)
        if not review:
            raise TaskError(f"{args.item_id}: no logged review; run task review before accept")
        if review.get("verdict") != "clean":
            raise TaskError(f"{args.item_id}: latest review verdict is {review.get('verdict')!r}, not clean")

        item = next(item for item in items if item.get("id") == args.item_id)
        item["done"] = True
        item["done_reviewed"] = True
        item["done_reviewed_at"] = utc_now()
        item["done_reviewed_by"] = actor
        item["closed_at"] = item["done_reviewed_at"]
        item["closed_by"] = actor
        item["accepted_review_dispatch_id"] = review.get("dispatch_id")
        if review.get("findings_ref"):
            item["accepted_review_findings_ref"] = review.get("findings_ref")
        _append_audit(item, "accept", actor, review_dispatch_id=review.get("dispatch_id"))

    store.mutate_items(update, allow_invalid_live_mirror=True)
    print(args.item_id)
    return 0


def _cmd_list(store: TaskStore, args: argparse.Namespace) -> int:
    rows = store.query_items(
        {
            "status": args.status,
            "kind": args.kind,
            "blocked_by": args.blocked_by,
            "since": args.since,
        }
    )
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, sort_keys=True))
        return 0
    for row in rows:
        print(f"{row['id']} {row['derived_status']} {row.get('title', '')}")
    return 0


def _cmd_status(store: TaskStore, args: argparse.Namespace) -> int:
    rows = store.derived_rows()
    if args.json:
        payload = {"project_root": str(store.project_root), "items": rows}
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        return 0
    for row in rows:
        print(f"{row['id']} {row['derived_status']} {row.get('title', '')}")
    return 0


def _cmd_sync(store: TaskStore, args: argparse.Namespace) -> int:
    actor = _actor(args)

    def update(items: list[dict[str, Any]]) -> int:
        return store.sync_dispatch_breadcrumbs(items, actor)

    changed = store.mutate_items(update, allow_invalid_live_mirror=True)
    print(f"OK: synced tasks-data.js and generated markdown views ({changed} dispatch breadcrumb updates)")
    return 0


def _api_store(project_root: str | Path | None = None) -> TaskStore:
    return TaskStore(resolve_project_root(str(project_root) if project_root is not None else None))


def get(item_id: str, project_root: str | Path | None = None) -> dict[str, Any]:
    """Read one item by id with derived status and schema migration applied."""
    return _api_store(project_root).get_item(item_id)


def list(query: Any = None, project_root: str | Path | None = None, **filters: Any) -> list[dict[str, Any]]:  # noqa: A001
    """Query items with the same row shape emitted by `goalflight_task.py list --json`."""
    return _api_store(project_root).query_items(query, **filters)


def outstanding(project_root: str | Path | None = None, **filters: Any) -> list[dict[str, Any]]:
    """Return all items that are not DONE-REVIEWED."""
    return list("outstanding", project_root=project_root, **filters)


def build_parser() -> argparse.ArgumentParser:
    examples = """examples:
  goalflight_task.py new "Tighten review gate" --kind task --prompt-path docs-private/briefs/t-014.md
  goalflight_task.py show t-014 --json
  goalflight_task.py block t-014 --on q-002
  goalflight_task.py done t-014 --resolution worker-complete
  goalflight_task.py list outstanding
  goalflight_task.py list delegated --since 1h --json
  goalflight_task.py review t-014 --verdict clean --dispatch codex-review-123
  goalflight_task.py accept t-014
  goalflight_task.py sync
"""
    parser = argparse.ArgumentParser(
        description="Goal Flight task-backlog store/writer.",
        epilog=examples,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--project-root", help="Canonical project root. Defaults to git common-dir parent.")
    parser.add_argument("--by", help="Actor stamp override, e.g. user or watcher.")
    sub = parser.add_subparsers(dest="command", required=True)

    new = sub.add_parser(
        "new",
        help="Create a task, bug, or decision item.",
        epilog='example: goalflight_task.py new "Fix stale status" --kind bug --blocked-by q-002',
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    new.add_argument("--by", help=argparse.SUPPRESS)
    new.add_argument("title")
    new.add_argument("--kind", choices=["task", "bug", "decision"], default="task")
    new.add_argument("--id-family", choices=sorted(VALID_FAMILIES), help="Override id family; default follows --kind.")
    new.add_argument("--blocked-by", action="append", default=[])
    new.add_argument("--link", dest="links", action="append", default=[])
    new.add_argument("--tag", dest="tags", action="append", default=[])
    new.add_argument("--acceptance")
    new.add_argument("--prompt")
    new.add_argument("--prompt-path")
    new.add_argument("--pattern")
    new.add_argument("--severity")
    new.add_argument("--source")
    new.add_argument("--json", action="store_true")
    new.set_defaults(func=_cmd_new)

    show = sub.add_parser(
        "show",
        help="Read one item by id.",
        epilog="examples:\n  goalflight_task.py show t-014\n  goalflight_task.py show t-014 --json\n  goalflight_task.py show t-014 --prompt",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    show.add_argument("--by", help=argparse.SUPPRESS)
    show.add_argument("item_id")
    show.add_argument("--prompt", action="store_true")
    show.add_argument("--json", action="store_true")
    show.set_defaults(func=_cmd_show)

    block = sub.add_parser(
        "block",
        help="Add blockers to an item.",
        epilog="example: goalflight_task.py block t-014 --on q-002",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    block.add_argument("--by", help=argparse.SUPPRESS)
    block.add_argument("item_id")
    block.add_argument("--on", action="append", required=True)
    block.set_defaults(func=_cmd_block)

    unblock = sub.add_parser(
        "unblock",
        help="Remove blockers from an item, or all blockers if --on is omitted.",
        epilog="example: goalflight_task.py unblock t-014 --on q-002",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    unblock.add_argument("--by", help=argparse.SUPPRESS)
    unblock.add_argument("item_id")
    unblock.add_argument("--on", action="append", default=[])
    unblock.set_defaults(func=_cmd_unblock)

    done = sub.add_parser(
        "done",
        help="Mark an item DONE/awaiting-review; accept moves it to DONE-REVIEWED.",
        epilog="example: goalflight_task.py done t-014 --resolution worker-complete",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    done.add_argument("--by", help=argparse.SUPPRESS)
    done.add_argument("item_id")
    done.add_argument("--resolution", default="done")
    done.add_argument("--force", action="store_true")
    done.set_defaults(func=_cmd_done)

    review = sub.add_parser(
        "review",
        help="Append a review breadcrumb to an item.",
        epilog="example: goalflight_task.py review t-014 --verdict clean --dispatch codex-review-123 --findings docs-private/reviews/t-014.md",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    review.add_argument("--by", help=argparse.SUPPRESS)
    review.add_argument("item_id")
    review.add_argument("--verdict", choices=["clean", "findings"], required=True)
    review.add_argument("--dispatch", required=True)
    review.add_argument("--findings")
    review.set_defaults(func=_cmd_review)

    accept = sub.add_parser(
        "accept",
        help="Move DONE/awaiting-review to DONE-REVIEWED after a clean review.",
        epilog="example: goalflight_task.py accept t-014",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    accept.add_argument("--by", help=argparse.SUPPRESS)
    accept.add_argument("item_id")
    accept.set_defaults(func=_cmd_accept)

    list_cmd = sub.add_parser(
        "list",
        help="Query items with canned dashboard statuses and AND filters.",
        epilog="examples:\n  goalflight_task.py list outstanding\n  goalflight_task.py list awaiting-review --kind task\n  goalflight_task.py list delegated --since 1h --json\n  goalflight_task.py list --blocked-by q-002",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    list_cmd.add_argument("--by", help=argparse.SUPPRESS)
    list_cmd.add_argument("status", nargs="?", choices=sorted(CANNED_LIST_STATUSES))
    list_cmd.add_argument("--since", help="UTC lower bound: 1h, now-3600, epoch seconds, or ISO timestamp.")
    list_cmd.add_argument("--kind", choices=["task", "bug", "decision"])
    list_cmd.add_argument("--blocked-by", action="append", default=[])
    list_cmd.add_argument("--json", action="store_true")
    list_cmd.set_defaults(func=_cmd_list)

    status = sub.add_parser(
        "status",
        help="Print derived task status.",
        epilog="example: goalflight_task.py status --json",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    status.add_argument("--by", help=argparse.SUPPRESS)
    status.add_argument("--json", action="store_true")
    status.set_defaults(func=_cmd_status)

    sync = sub.add_parser(
        "sync",
        help="Write tasks-data.js and sync project-scoped dispatch breadcrumbs.",
        epilog="example: goalflight_task.py sync",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sync.add_argument("--by", help=argparse.SUPPRESS)
    sync.set_defaults(func=_cmd_sync)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    store = TaskStore(resolve_project_root(args.project_root))
    try:
        return args.func(store, args)
    except TaskError as exc:
        print(f"goalflight_task.py: {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"goalflight_task.py: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
