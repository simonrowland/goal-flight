#!/usr/bin/env python3
"""Read-only summary of retained remote-CI receipts for one project."""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import heapq
import json
import math
import os
from pathlib import Path
import re
import shlex
import statistics
import sys
import time
from typing import Any, Mapping

import goalflight_dispatch_paths
import goalflight_fs
import goalflight_ledger
import goalflight_task


MAX_RESULT_FILES = 5000
MAX_RESULT_BYTES = 2 * 1024 * 1024
MAX_JSON_DEPTH = 64
SCAN_TIME_BUDGET_SECONDS = 10.0
MAX_TAIL_EXEC_BLOCKS = 5000
TAIL_PARSE_CHECK_INTERVAL = 64
MAX_COMMAND_LINE_CHARS = 4096
_EXEC_DURATION_RE = re.compile(
    r"^ ?(?:succeeded|exited -?\d+) in (\d+)(ms|s):"
)
_IGNORED_COMMANDS = {"mktemp", "cd", "export", "set", "source", "printf", "if", "test"}
_SAFE_MODULE = re.compile(r"^[A-Za-z0-9_.]+$")
_SAFE_SCRIPT = re.compile(r"^[A-Za-z0-9_./-]+\.(?:py|sh|mjs|js)$")
_SAFE_SUBCOMMAND = re.compile(r"^[a-z][a-z-]{1,24}$")
_CODEX_COMMAND_HEADER_RE = re.compile(r"^(.+) in (/\S+)$")
_ASSIGNMENT_SUBSTITUTION_RE = re.compile(
    r"(?:^|\s)[A-Za-z_][A-Za-z0-9_]*=(?:\$\([^)]*\)|`[^`]*`|[<>]\([^)]*\))"
)
_SHELL_KEYWORDS = {"then", "do", "else", "elif", "fi", "done", "esac", "{", "}", "!", "time"}


def _duration(value: str) -> float:
    units = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    raw = value.strip().lower()
    if raw and raw[-1] in units:
        number, multiplier = raw[:-1], units[raw[-1]]
    else:
        number, multiplier = raw, 1
    seconds = float(number) * multiplier
    if not math.isfinite(seconds) or seconds < 0:
        raise ValueError("duration must be finite and non-negative")
    return seconds


def _timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) and result >= 0 else None


def _mappings(value: Any):
    if isinstance(value, Mapping):
        yield value
        for child in value.values():
            yield from _mappings(child)
    elif isinstance(value, list):
        for child in value:
            yield from _mappings(child)


def _json_depth(value: Any, deadline: float) -> int | None:
    maximum = 0
    pending = [(value, 1)]
    visited = 0
    while pending:
        if visited % 256 == 0 and time.monotonic() >= deadline:
            return None
        current, depth = pending.pop()
        visited += 1
        maximum = max(maximum, depth)
        if isinstance(current, Mapping):
            pending.extend((child, depth + 1) for child in current.values())
        elif isinstance(current, list):
            pending.extend((child, depth + 1) for child in current)
    return maximum


def _project_config(project_root: Path) -> tuple[Path, str | None, bool]:
    configured = os.environ.get("GOALFLIGHT_REMOTE_CI_SPOOL_DIR", "").strip()
    config_path = Path(os.environ.get(
        "GOALFLIGHT_REMOTE_CI_CONFIG",
        str(project_root / ".goal-flight" / "remote-ci.json"),
    )).expanduser()
    repo_id = None
    spool = None
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
        if isinstance(config, Mapping):
            repo = config.get("repo")
            if isinstance(repo, str) and repo.strip():
                repo_id = repo.strip()
            paths = config.get("paths")
            if isinstance(paths, Mapping):
                queue = Path(str(paths["queue_dir"])).expanduser()
                results = Path(str(paths["result_dir"])).expanduser()
                if (queue.is_absolute() and results.is_absolute()
                        and queue.name == "requests" and results.name == "results"
                        and queue.parent.resolve(strict=False)
                        == results.parent.resolve(strict=False)):
                    spool = queue.parent
    except (OSError, ValueError, TypeError, KeyError):
        pass
    if configured:
        return Path(configured).expanduser(), repo_id, True
    if spool is not None:
        return spool, repo_id, True
    return goalflight_dispatch_paths.remote_ci_spool_dir(), repo_id, False


def _command_shape(command_line: str) -> str:
    if len(command_line) > MAX_COMMAND_LINE_CHARS:
        return "<unparsed>"
    line = command_line.strip()
    if line.startswith("+ "):
        line = line[2:].lstrip()
    for prefix in ("• Ran ", "Ran ", "command: ", "cmd: "):
        if line.startswith(prefix):
            line = line[len(prefix):]
            break
    had_substitution = bool(re.search(r"\$\(|`|[<>]\(", line))
    try:
        wrapper = re.match(r"^(\S+)\s+-lc\s+(['\"])(.*)$", line)
        if wrapper and Path(wrapper.group(1)).name == "zsh":
            payload = wrapper.group(3)
            if payload.endswith(wrapper.group(2)):
                payload = payload[:-1]
            line = payload
        line = _ASSIGNMENT_SUBSTITUTION_RE.sub(" ", line)
        lexer = shlex.shlex(line, posix=True, punctuation_chars=";&|")
        lexer.whitespace_split = True
        lexer.commenters = ""
        tokens = list(lexer)
    except ValueError:
        tokens = [token.lstrip("'\"") for token in line.split()]
    if not tokens:
        return "<subst>" if had_substitution else "<unparsed>"
    segments: list[list[str]] = [[]]
    for token in tokens:
        if token in {"&&", "||", ";", "|"}:
            segments.append([])
        else:
            segments[-1].append(token)
    for segment in segments:
        words = [token for token in segment if "=" not in token]
        if not words:
            continue
        # Strip only conventional command prefixes; all remaining arguments
        # are considered untrusted and are included solely by explicit allowlist.
        while words:
            name = Path(words[0]).name
            if name in _SHELL_KEYWORDS:
                words = words[1:]
            elif name == "env":
                words = words[1:]
            elif name == "timeout" and len(words) > 2:
                words = words[2:]
            elif name == "nice":
                words = words[3:] if len(words) > 3 and words[1] == "-n" else words[1:]
            else:
                break
        if not words:
            continue
        executable = words[0].strip("'\"`)")
        if executable == '"$GOALFLIGHT_REMOTE_RUN"':
            executable = "goalflight-remote-run"
        elif executable == "$GOALFLIGHT_REMOTE_RUN":
            executable = "goalflight-remote-run"
        if not executable:
            continue
        if any(char in executable for char in "=:@"):
            continue
        program = Path(executable).name
        if program.startswith("-") or not re.fullmatch(r"[A-Za-z0-9_.+-]{1,40}", program):
            continue
        if program.lower() in _IGNORED_COMMANDS:
            continue
        args = [token.strip("'\"`") for token in words[1:]]
        if any(token.startswith("<<") for token in args) or (args and args[0] == "-"):
            return f"{program} <heredoc>"
        if args and args[0] == "-c":
            return program
        python_invocation = re.fullmatch(r"python(?:3(?:\.\d+)?)?", program) is not None
        module_args = args
        if program == "goalflight-remote-run":
            python_index = next(
                (index for index, token in enumerate(args)
                 if re.fullmatch(r"python(?:3(?:\.\d+)?)?", Path(token).name)),
                None,
            )
            if python_index is not None:
                python_invocation = True
                module_args = args[python_index + 1:]
        if python_invocation:
            for index, token in enumerate(module_args[:-1]):
                if token == "-m" and _SAFE_MODULE.fullmatch(module_args[index + 1]):
                    return f"{program} -m {module_args[index + 1]}"
        if args:
            script = args[0]
            if script.startswith("~/"):
                script = script
            elif script.startswith(str(Path.home()) + "/"):
                script = "~" + script[len(str(Path.home())):]
            if _SAFE_SCRIPT.fullmatch(script):
                return f"{program} {script}"
            if program == "git" and _SAFE_SUBCOMMAND.fullmatch(args[0]):
                return f"{program} {args[0]}"
        return program
    return "<subst>" if had_substitution else "<unparsed>"


def _tail_execs(
    payload: bytes, deadline: float | None = None,
) -> tuple[list[tuple[str, float]], int, str | None]:
    lines = payload.decode("utf-8", "replace").splitlines()
    calls: list[tuple[str, float]] = []
    unpaired = 0
    index = 0
    blocks = 0
    while index < len(lines):
        if index % TAIL_PARSE_CHECK_INTERVAL == 0 and deadline is not None and time.monotonic() >= deadline:
            return calls, unpaired, "budget"
        if lines[index] != "exec":
            index += 1
            continue
        blocks += 1
        if blocks > MAX_TAIL_EXEC_BLOCKS:
            return calls, unpaired, "block-cap"
        command_index = index + 1
        if command_index >= len(lines):
            unpaired += 1
            index += 1
            continue

        # Codex can wrap a shell command over multiple lines, leaving the cwd
        # suffix on the last continuation line. Require that suffix before any
        # hook/result boundary so heredoc contents cannot masquerade as execs.
        # A forged exec/cwd/result sequence inside heredoc output remains possible;
        # this controller-only audit assumes a single trusted operator and no tampering.
        header_end = None
        command_limit = min(len(lines), command_index + 400)
        for line_index in range(command_index, command_limit):
            if line_index % TAIL_PARSE_CHECK_INTERVAL == 0 and deadline is not None and time.monotonic() >= deadline:
                return calls, unpaired, "budget"
            line = lines[line_index]
            if line == "exec" or line.startswith("hook:") or _EXEC_DURATION_RE.match(line):
                break
            if _CODEX_COMMAND_HEADER_RE.fullmatch(line):
                header_end = line_index
                break
        if header_end is None:
            unpaired += 1
            index += 1
            continue

        first_line_header = _CODEX_COMMAND_HEADER_RE.fullmatch(lines[command_index])
        command = first_line_header.group(1) if first_line_header else lines[command_index]
        next_exec = header_end + 1
        while next_exec < len(lines) and lines[next_exec] != "exec":
            if next_exec % TAIL_PARSE_CHECK_INTERVAL == 0 and deadline is not None and time.monotonic() >= deadline:
                return calls, unpaired, "budget"
            next_exec += 1
        duration = None
        for line_index in range(index + 1, next_exec):
            if line_index % TAIL_PARSE_CHECK_INTERVAL == 0 and deadline is not None and time.monotonic() >= deadline:
                return calls, unpaired, "budget"
            match = _EXEC_DURATION_RE.search(lines[line_index])
            if match is not None:
                duration = float(match.group(1))
                if match.group(2) == "ms":
                    duration /= 1000.0
                break
        shape = _command_shape(command) if duration is not None else None
        if duration is None:
            unpaired += 1
        elif shape is not None:
            calls.append((shape, duration))
        index = next_exec
    return calls, unpaired, None


def _in_worker_report(
    project_root: Path,
    controller_label: str | None,
    cutoff: datetime,
    window_seconds: float,
    deadline: float,
) -> tuple[dict[str, Any], bool, list[str]]:
    report: dict[str, Any] = {"commands": [], "unmeasured": [], "unpaired_exec_blocks": 0}
    if not controller_label:
        return report, False, []
    try:
        ledger_dir = goalflight_ledger.runs_dir(create=False)
        presence = goalflight_fs.path_presence(ledger_dir)
        if presence == "unknown":
            raise OSError("ledger directory unreadable")
        names = os.listdir(ledger_dir) if presence != "absent" else []
    except (OSError, ValueError) as exc:
        return report, False, [f"dispatch ledger unavailable: {type(exc).__name__}"]
    records = []
    ledger_timed_out = False
    cutoff_mtime = cutoff.timestamp()
    for name in sorted(names):
        if time.monotonic() >= deadline:
            ledger_timed_out = True
            break
        if not name.endswith(".json"):
            continue
        path = ledger_dir / name
        try:
            if path.stat().st_mtime < cutoff_mtime:
                continue
            record = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(record, Mapping):
                records.append(record)
        except (OSError, ValueError):
            continue
    if ledger_timed_out:
        return report, True, ["dispatch ledger scan budget exhausted; remaining records omitted"]
    project_identity = goalflight_task.resolve_project_root_for_read(str(project_root))
    identities: dict[str, Path | None] = {}
    dispatch_dir = goalflight_dispatch_paths.dispatch_base_dir()
    candidates: list[tuple[float, str, str, Path]] = []
    for record in records:
        if time.monotonic() >= deadline:
            return report, True, []
        record_root = record.get("project_root")
        if record.get("controller_label") != controller_label or not isinstance(record_root, str):
            continue
        if record_root not in identities:
            identities[record_root] = goalflight_task.resolve_project_root_for_read(record_root)
        record_identity = identities[record_root]
        if project_identity is not None and record_identity is not None:
            same_project = project_identity.resolve(strict=False) == record_identity.resolve(strict=False)
        else:
            try:
                same_project = (
                    goalflight_task._strip_managed_worktree(Path(record_root).expanduser()).resolve(strict=False)
                    == goalflight_task._strip_managed_worktree(project_root).resolve(strict=False)
                )
            except (OSError, RuntimeError, TypeError, ValueError):
                same_project = Path(record_root).expanduser().resolve(strict=False) == project_root.resolve(strict=False)
        if not same_project:
            continue
        dispatch_id = str(record.get("dispatch_id") or "")
        if not dispatch_id:
            continue
        tail = dispatch_dir / f"{goalflight_dispatch_paths.safe_dispatch_filename(dispatch_id)}.tail"
        try:
            if tail.is_symlink() or not tail.is_file():
                continue
            stat = tail.stat()
        except OSError:
            continue
        if stat.st_mtime < cutoff.timestamp():
            continue
        engine = str(record.get("engine") or record.get("agent") or "unknown")
        candidates.append((stat.st_mtime, dispatch_id, engine, tail))
    candidates.sort(reverse=True)
    omitted = max(0, len(candidates) - MAX_RESULT_FILES)
    candidates = candidates[:MAX_RESULT_FILES]
    groups: dict[str, dict[str, Any]] = {}
    unmeasured: dict[str, int] = {}
    unreadable = oversized = 0
    timed_out = False
    notes: list[str] = []
    for _mtime, dispatch_id, engine, path in candidates:
        if time.monotonic() >= deadline:
            timed_out = True
            break
        try:
            size = path.stat().st_size
            with path.open("rb") as stream:
                if size > MAX_RESULT_BYTES:
                    stream.seek(size - MAX_RESULT_BYTES)
                    oversized += 1
                payload = stream.read(MAX_RESULT_BYTES)
        except OSError:
            unreadable += 1
            continue
        calls, unpaired, parse_stop = (
            _tail_execs(payload, deadline) if engine.lower().startswith("codex")
            else ([], 0, None)
        )
        report["unpaired_exec_blocks"] += unpaired
        if parse_stop == "budget":
            timed_out = True
            notes.append("dispatch tail parser budget exhausted; remaining tails omitted")
            break
        if parse_stop == "block-cap":
            notes.append(
                f"dispatch tail exec-block cap reached ({MAX_TAIL_EXEC_BLOCKS}); remaining blocks omitted"
            )
        if not calls:
            unmeasured[engine] = unmeasured.get(engine, 0) + 1
            continue
        per_dispatch: dict[str, int] = {}
        for command, seconds in calls:
            entry = groups.setdefault(command, {
                "command": command, "calls": 0, "wall_seconds": 0.0,
                "max_wall_seconds": 0.0, "dispatch_calls": {},
            })
            entry["calls"] += 1
            entry["wall_seconds"] += seconds
            entry["max_wall_seconds"] = max(entry["max_wall_seconds"], seconds)
            entry["dispatch_calls"][dispatch_id] = entry["dispatch_calls"].get(dispatch_id, 0) + 1
            per_dispatch[command] = per_dispatch.get(command, 0) + 1
        # A command repeated within one tail is a repeated in-worker run for that job.
        for command, count in per_dispatch.items():
            if count > 1:
                groups[command].setdefault("repeated_dispatch_ids", set()).add(dispatch_id)
    report["commands"] = [
        {
            "command": entry["command"], "calls": entry["calls"],
            "wall_seconds": entry["wall_seconds"],
            "max_wall_seconds": entry["max_wall_seconds"],
            "repeated_dispatch_ids": sorted(entry.get("repeated_dispatch_ids", set())),
        }
        for entry in sorted(groups.values(), key=lambda row: (-row["wall_seconds"], row["command"]))[:5]
    ]
    report["unmeasured"] = [
        {"engine": engine, "tails": count}
        for engine, count in sorted(unmeasured.items())
    ]
    if unreadable + oversized:
        notes.append(
            f"{unreadable + oversized} dispatch tail(s) unreadable or truncated by byte limit"
        )
    if omitted:
        notes.append(
            f"{omitted} dispatch tail(s) omitted by newest-first limit ({MAX_RESULT_FILES})"
        )
    return report, timed_out, notes


def _p95(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, math.ceil(0.95 * len(ordered)) - 1)]


def analyze(
    project_root: Path,
    *,
    since_seconds: float = 24 * 60 * 60,
    controller_label: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    deadline = time.monotonic() + SCAN_TIME_BUDGET_SECONDS
    project_root = project_root.resolve(strict=False)
    spool, project_id, project_scoped = _project_config(project_root)
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    cutoff = now - timedelta(seconds=since_seconds)
    result: dict[str, Any] = {
        "project_root": str(project_root),
        "controller_label": controller_label or "all",
        "window_seconds": since_seconds,
        "scope": "project",
        "attribution": {"controller_label": "unavailable", "dispatch_id": "unavailable"},
        "commands": [],
        "slowest": [],
        "repeated_runs": {"available": False, "groups": []},
        "notes": [],
    }
    if not project_scoped:
        project_id = project_id or project_root.name
        result["project_id"] = project_id
    receipt_dirs = (
        (spool / "results", lambda name: name.endswith(".json")),
        (spool / "requests", lambda name: name.endswith(".local.json")),
    )
    newest: list[tuple[float, str]] = []
    seen_files = 0
    unreadable_metadata = 0
    scan_timed_out = False
    unreadable_dirs = 0
    readable_dirs = 0
    for directory, select_name in receipt_dirs:
        try:
            with os.scandir(directory) as entries:
                readable_dirs += 1
                for entry in entries:
                    if time.monotonic() >= deadline:
                        scan_timed_out = True
                        break
                    if not select_name(entry.name) or not entry.is_file(follow_symlinks=False):
                        continue
                    try:
                        modified = entry.stat(follow_symlinks=False).st_mtime
                    except OSError:
                        unreadable_metadata += 1
                        continue
                    seen_files += 1
                    item = (modified, str(directory / entry.name))
                    if len(newest) < MAX_RESULT_FILES:
                        heapq.heappush(newest, item)
                    elif item > newest[0]:
                        heapq.heapreplace(newest, item)
        except OSError:
            unreadable_dirs += 1
        if scan_timed_out:
            break
    if not readable_dirs:
        result["commands"] = None
        result["slowest"] = None
        result["notes"].append("receipt spool unavailable: OSError")
        return result
    if unreadable_dirs:
        result["notes"].append(f"{unreadable_dirs} receipt directory(ies) unavailable")
    paths = [Path(path) for _, path in sorted(newest, reverse=True)]

    grouped: dict[tuple[str, str], dict[str, Any]] = {}
    repeated: dict[tuple[str, str], dict[str, set[str]]] = {}
    seen_controller_labels: set[str] = set()
    seen_dispatch_ids: set[str] = set()
    saw_repeat_identity = False
    malformed = 0
    oversize = 0
    too_deep = 0
    project_unattributed = 0
    unattributed_requests: dict[str, float] = {}
    unattributed_reasons: dict[str, set[str]] = {}
    dispatch_records: dict[str, tuple[list[Mapping[str, Any]], str | None]] = {}

    def records_for_dispatch(
        dispatch_id: str,
    ) -> tuple[list[Mapping[str, Any]], str | None]:
        nonlocal scan_timed_out
        if dispatch_id in dispatch_records:
            return dispatch_records[dispatch_id]
        matches: list[Mapping[str, Any]] = []
        foreign = False
        path = goalflight_ledger.record_path(dispatch_id, create=False)
        paths = [path]
        archive_dir = goalflight_ledger.runs_archive_dir(create=False)
        archive_presence = goalflight_fs.path_presence(archive_dir)
        if archive_presence == "unknown":
            dispatch_records[dispatch_id] = ([], "dispatch ledger unavailable")
            return dispatch_records[dispatch_id]
        if archive_presence == "present":
            try:
                paths.extend(archive_dir / month / path.name for month in os.listdir(archive_dir))
            except OSError:
                dispatch_records[dispatch_id] = ([], "dispatch ledger unavailable")
                return dispatch_records[dispatch_id]
        for record_path in paths:
            if time.monotonic() >= deadline:
                scan_timed_out = True
                dispatch_records[dispatch_id] = ([], "dispatch ledger scan timed out")
                return dispatch_records[dispatch_id]
            presence = goalflight_fs.path_presence(record_path)
            if presence == "unknown":
                dispatch_records[dispatch_id] = ([], "dispatch ledger unavailable")
                return dispatch_records[dispatch_id]
            if presence != "present":
                continue
            try:
                record = json.loads(record_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                dispatch_records[dispatch_id] = ([], "dispatch ledger unreadable")
                return dispatch_records[dispatch_id]
            if not isinstance(record, Mapping) or record.get("dispatch_id") != dispatch_id:
                continue
            root = record.get("project_root")
            if not isinstance(root, str):
                continue
            if Path(root).expanduser().resolve(strict=False) == project_root:
                matches.append(record)
            else:
                foreign = True
        reason = "foreign-project dispatch ledger row" if foreign and not matches else None
        dispatch_records[dispatch_id] = (matches, reason)
        return dispatch_records[dispatch_id]

    def receipt_attribution(
        receipt: Mapping[str, Any],
    ) -> tuple[str | None, str | None]:
        receipt_label = receipt.get("controller_label")
        if not isinstance(receipt_label, str) or not receipt_label.strip():
            receipt_label = None
        dispatch_id = receipt.get("dispatch_id")
        if not isinstance(dispatch_id, str) or not dispatch_id:
            return (receipt_label, None) if receipt_label else (None, "missing dispatch id and controller label")
        records, lookup_reason = records_for_dispatch(dispatch_id)
        if lookup_reason:
            return None, lookup_reason
        labels = {
            label.strip() if isinstance(label, str) and label.strip() else None
            for record in records
            for label in (record.get("controller_label"),)
        }
        if len(labels) > 1:
            return None, "ambiguous dispatch ledger labels"
        ledger_label = next(iter(labels), None)
        if receipt_label and ledger_label and receipt_label != ledger_label:
            return None, "receipt label disagrees with dispatch ledger"
        if not records and receipt_label is None:
            return None, "dispatch id has no project ledger row or receipt label"
        return ledger_label or receipt_label, None

    def mark_unattributed(request_id: str, receipt: Mapping[str, Any], reason: str) -> None:
        unattributed_requests[request_id] = unattributed_requests.get(request_id, 0.0) + (
            _number(receipt.get("process_cpu_seconds")) or 0.0
        )
        unattributed_reasons.setdefault(reason, set()).add(request_id)

    for path in paths:
        if time.monotonic() >= deadline:
            scan_timed_out = True
            break
        try:
            with path.open("rb") as stream:
                payload = stream.read(MAX_RESULT_BYTES + 1)
            if len(payload) > MAX_RESULT_BYTES:
                oversize += 1
                continue
            outer = json.loads(payload)
            stat = path.stat()
        except (OSError, ValueError, RecursionError):
            malformed += 1
            continue
        depth = _json_depth(outer, deadline)
        if depth is None:
            scan_timed_out = True
            break
        if depth > MAX_JSON_DEPTH:
            too_deep += 1
            continue
        if not isinstance(outer, Mapping):
            malformed += 1
            continue
        try:
            finished = _timestamp(outer.get("finished_at"))
            if finished is None:
                finished = datetime.fromtimestamp(stat.st_mtime, timezone.utc)
        except (OSError, OverflowError, ValueError):
            malformed += 1
            continue
        if finished < cutoff:
            continue
        all_maps = list(_mappings(outer))
        if time.monotonic() >= deadline:
            scan_timed_out = True
            break
        receipts = [
            item for item in all_maps
            if isinstance(item.get("command_id"), str)
            and ("process_cpu_seconds" in item or "wall_seconds" in item
                 or "duration_seconds" in item)
        ]
        if not receipts:
            continue
        record_project = outer.get("project_id")
        if not isinstance(record_project, str):
            record_project = next((item.get("project_id") for item in receipts
                                   if isinstance(item.get("project_id"), str)), None)
        if not project_scoped and project_id:
            if record_project is None:
                project_unattributed += 1
                continue
            if record_project != project_id:
                continue
        request_id = outer.get("request_id")
        if not isinstance(request_id, str) or not request_id:
            request_id = path.name.removesuffix(".local.json").removesuffix(".json")
        if controller_label:
            selected_receipts = []
            for item in receipts:
                label, reason = receipt_attribution(item)
                if reason:
                    mark_unattributed(request_id, item, reason)
                elif label == controller_label:
                    selected_receipts.append(item)
            receipts = selected_receipts
        if not receipts:
            continue
        seen_controller_labels.update(
            label for item in receipts if (label := receipt_attribution(item)[0])
        )
        seen_dispatch_ids.update(
            item["dispatch_id"] for item in receipts
            if isinstance(item.get("dispatch_id"), str) and item["dispatch_id"]
        )
        fallback = (
            outer.get("status") == "LOCAL_FALLBACK"
            or any(item.get("route") == "local-fallback" for item in receipts)
        )
        for receipt in receipts:
            command_id = receipt["command_id"]
            key = (command_id, request_id)
            run = grouped.setdefault(key, {
                "command_id": command_id,
                "request_id": request_id,
                "cpu_seconds": 0.0,
                "has_cpu": False,
                "wall_seconds": 0.0,
                "has_wall": False,
                "local_fallback": fallback,
                "controller_labels": set(),
                "dispatch_ids": set(),
            })
            label, reason = receipt_attribution(receipt)
            if reason:
                mark_unattributed(request_id, receipt, reason)
            dispatch_id = receipt.get("dispatch_id")
            command_sha = receipt.get("command_sha")
            if isinstance(label, str) and label:
                run["controller_labels"].add(label)
            if isinstance(dispatch_id, str) and dispatch_id:
                run["dispatch_ids"].add(dispatch_id)
            if isinstance(command_sha, str) and command_sha:
                if isinstance(dispatch_id, str) and dispatch_id:
                    saw_repeat_identity = True
                    repeat = repeated.setdefault(
                        (dispatch_id, command_sha), {"requests": set(), "commands": set()},
                    )
                    repeat["requests"].add(request_id)
                    repeat["commands"].add(command_id)
            cpu = _number(receipt.get("process_cpu_seconds"))
            wall = _number(receipt.get("wall_seconds"))
            if wall is None:
                wall = _number(receipt.get("duration_seconds"))
            if cpu is not None:
                run["cpu_seconds"] += cpu
                run["has_cpu"] = True
            if wall is not None:
                run["wall_seconds"] += wall
                run["has_wall"] = True
            run["local_fallback"] = run["local_fallback"] or fallback

    result["unattributed"] = {
        "runs": len(unattributed_requests),
        "cpu_seconds": sum(unattributed_requests.values()),
    }
    for reason, requests in sorted(unattributed_reasons.items()):
        result["notes"].append(
            f"unattributed receipts: {len(requests)} runs ({reason})"
        )
    result["attribution"] = {
        "controller_label": "available" if seen_controller_labels else "unavailable",
        "dispatch_id": "available" if seen_dispatch_ids else "unavailable",
    }
    repeated_groups = [
        {
            "dispatch_id": dispatch_id,
            "command_sha": command_sha,
            "command_ids": sorted(value["commands"]),
            "runs": len(value["requests"]),
        }
        for (dispatch_id, command_sha), value in sorted(repeated.items())
        if len(value["requests"]) > 1
    ]
    result["repeated_runs"] = {
        "available": saw_repeat_identity,
        "groups": repeated_groups,
    }
    if not saw_repeat_identity:
        result["notes"].append(
            "repeated identical runs unavailable: receipts omit command_sha and/or dispatch_id"
        )
    if malformed:
        result["notes"].append(f"{malformed} unreadable or invalid receipt file(s) omitted")
    if unreadable_metadata:
        result["notes"].append(
            f"{unreadable_metadata} receipt file(s) omitted because metadata was unreadable"
        )
    if oversize:
        result["notes"].append(
            f"{oversize} oversize receipt file(s) omitted (limit {MAX_RESULT_BYTES} bytes)"
        )
    if too_deep:
        result["notes"].append(
            f"{too_deep} over-depth receipt file(s) omitted (limit {MAX_JSON_DEPTH})"
        )
    if seen_files > len(newest):
        result["notes"].append(
            f"{seen_files - len(newest)} receipt file(s) omitted by newest-first limit "
            f"({MAX_RESULT_FILES})"
        )
    if scan_timed_out:
        result["notes"].append(
            f"analysis time budget reached ({SCAN_TIME_BUDGET_SECONDS:g}s); remaining results omitted"
        )
    if project_unattributed:
        result["notes"].append(
            f"{project_unattributed} result file(s) lack project_id and were omitted"
        )

    in_worker, tail_timed_out, tail_notes = _in_worker_report(
        project_root, controller_label, cutoff, since_seconds, deadline,
    ) if time.monotonic() < deadline else ({"commands": [], "unmeasured": []}, True, [])
    result["in_worker"] = in_worker
    if tail_timed_out:
        if not scan_timed_out:
            result["notes"].append(
                f"analysis time budget reached ({SCAN_TIME_BUDGET_SECONDS:g}s); remaining dispatch tails omitted"
            )
        scan_timed_out = True
    result["notes"].extend(tail_notes)

    by_command: dict[str, list[dict[str, Any]]] = {}
    observations = []
    for run in grouped.values():
        by_command.setdefault(run["command_id"], []).append(run)
        observations.append(run)
    commands = []
    for command_id, runs in sorted(by_command.items()):
        cpu_values = [run["cpu_seconds"] for run in runs if run["has_cpu"]]
        wall_values = [run["wall_seconds"] for run in runs if run["has_wall"]]
        cpu_total = sum(cpu_values) if cpu_values else None
        wall_total = sum(wall_values) if wall_values else None
        commands.append({
            "command_id": command_id,
            "runs": len(runs),
            "cpu_seconds": cpu_total,
            "wall_seconds": wall_total,
            "wall_cpu_ratio": wall_total / cpu_total if wall_total is not None and cpu_total else None,
            "local_fallbacks": sum(bool(run["local_fallback"]) for run in runs),
            "median_wall_seconds": statistics.median(wall_values) if wall_values else None,
            "p95_wall_seconds": _p95(wall_values),
            "controller_labels": sorted({
                label for run in runs for label in run["controller_labels"]
            }),
            "dispatch_ids": sorted({
                dispatch_id for run in runs for dispatch_id in run["dispatch_ids"]
            }),
        })
    observations.sort(key=lambda run: (
        -(run["cpu_seconds"] if run["has_cpu"] else -1),
        -(run["wall_seconds"] if run["has_wall"] else -1),
        run["command_id"], run["request_id"],
    ))
    commands.sort(key=lambda row: (
        -(row["cpu_seconds"] if row["cpu_seconds"] is not None else -1),
        -(row["wall_seconds"] if row["wall_seconds"] is not None else -1),
        row["command_id"],
    ))
    result["commands"] = commands
    result["slowest"] = [
        {"command_id": run["command_id"], "cpu_seconds": run["cpu_seconds"] if run["has_cpu"] else None,
         "wall_seconds": run["wall_seconds"] if run["has_wall"] else None}
        for run in observations[:5]
    ]
    return result


def _seconds(value: float | None) -> str:
    return "?" if value is None else f"{value:.1f}s"


def _text(report: Mapping[str, Any]) -> str:
    window = report.get("window_seconds")
    window_text = f"last {window / 3600:g}h" if isinstance(window, (int, float)) else "window unavailable"
    requested_controller = report.get("controller_label")
    unattributed = report.get("unattributed") or {}
    notes = report.get("notes", [])
    incomplete_reasons = [
        note for note in notes
        if (
            "analysis time budget reached" in note
            or "receipt spool unavailable" in note
            or "receipt directory(ies) unavailable" in note
            or "receipt file(s) omitted" in note
            or "receipt file(s) unreadable" in note
            or "receipt file(s) over" in note
            or "receipt file(s) omitted by newest-first limit" in note
            or "result file(s) lack project_id" in note
        )
    ]
    scan_incomplete = report.get("commands") is None or bool(incomplete_reasons)
    no_attributed_runs = (
        isinstance(requested_controller, str)
        and requested_controller != "all"
        and not scan_incomplete
        and not (report.get("commands") or [])
        and not unattributed.get("runs", 0)
    )
    lines = [f"CI audit: {requested_controller or 'all'}; {window_text}"]
    if report.get("project_id"):
        lines.append(f"project-wide: {report['project_id']}")
    if no_attributed_runs:
        lines.append(f"no remote runs attributed to {requested_controller} in {window_text}")
    elif scan_incomplete:
        reason = "; ".join(incomplete_reasons) or "receipt scan did not complete"
        lines.append(f"scan incomplete: {reason}")
    attribution = report.get("attribution", {})
    if not no_attributed_runs and not scan_incomplete and (
        attribution.get("controller_label") != "available"
        or attribution.get("dispatch_id") != "available"
    ):
        lines.append("attribution unavailable: receipts omit controller_label and/or dispatch_id")
    for command in (report.get("commands") or [])[:5]:
        ratio = command["wall_cpu_ratio"]
        cpu = command.get("cpu_seconds")
        wall = command.get("wall_seconds")
        ratio_text = (
            "inf" if cpu == 0 and isinstance(wall, (int, float)) and wall > 0
            else f"{ratio:.2f}" if ratio is not None else "?"
        )
        lines.append(
            f"{command['command_id']}: runs={command['runs']} cpu={_seconds(command['cpu_seconds'])} "
            f"wall={_seconds(command['wall_seconds'])} wall/cpu={ratio_text} "
            f"local-fallback={command['local_fallbacks']} median={_seconds(command['median_wall_seconds'])} "
            f"p95={_seconds(command['p95_wall_seconds'])} "
            f"controllers={','.join(command['controller_labels']) or '?'} "
            f"dispatches={','.join(command['dispatch_ids']) or '?'}"
        )
    for group in (report.get("repeated_runs") or {}).get("groups", []):
        lines.append(
            f"repeat: {','.join(group['command_ids'])} dispatch={group['dispatch_id']} runs={group['runs']}"
        )
    if unattributed.get("runs", 0):
        lines.append(
            f"unattributed: {unattributed['runs']} runs, "
            f"cpu {_seconds(unattributed.get('cpu_seconds'))}"
        )
    in_worker = report.get("in_worker") or {}
    for row in in_worker.get("commands", []):
        repeated_ids = row.get("repeated_dispatch_ids") or []
        example_ids = [
            dispatch_id if len(dispatch_id) <= 16
            else f"{dispatch_id[:5]}…{dispatch_id[-6:]}"
            for dispatch_id in repeated_ids[:3]
        ]
        metrics = (
            f"in-worker wall:  calls={row['calls']} wall={_seconds(row['wall_seconds'])} "
            f"max-wall={_seconds(row['max_wall_seconds'])}"
        )
        repeated_text = ""
        if repeated_ids:
            while example_ids:
                repeated_text = (
                    f" repeated in {len(repeated_ids)} jobs (e.g. "
                    f"{', '.join(example_ids)})"
                )
                if len(metrics) + len(row["command"]) + len(repeated_text) <= 160:
                    break
                example_ids.pop()
            if not example_ids:
                repeated_text = f" repeated in {len(repeated_ids)} jobs"
        command = row["command"]
        command_limit = max(0, 160 - len(metrics) - len(repeated_text))
        if len(command) > command_limit:
            command = command[:max(0, command_limit - 1)] + "…"
        lines.append(f"in-worker wall: {command} calls={row['calls']} "
                     f"wall={_seconds(row['wall_seconds'])} max-wall={_seconds(row['max_wall_seconds'])}"
                     f"{repeated_text}")
    for row in in_worker.get("unmeasured", []):
        lines.append(f"unmeasured: {row['tails']} tails ({row['engine']})")
    if in_worker.get("unpaired_exec_blocks", 0):
        lines.append(f"unpaired: {in_worker['unpaired_exec_blocks']} exec blocks")
    for index, run in enumerate(report.get("slowest") or [], start=1):
        lines.append(
            f"slow #{index}: {run['command_id']} cpu={_seconds(run['cpu_seconds'])} "
            f"wall={_seconds(run['wall_seconds'])}"
        )
    for note in notes:
        if (no_attributed_runs or scan_incomplete) and (
            "repeated identical runs unavailable" in note
            or "attribution unavailable" in note
        ):
            continue
        lines.append(f"note: {note}")
    if len(lines) > 15:
        lines = lines[:14] + ["note: additional findings omitted"]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    class SafeArgumentParser(argparse.ArgumentParser):
        def error(self, _message: str) -> None:
            print("invalid-input: argument error", file=sys.stderr)
            raise SystemExit(2)

    parser = SafeArgumentParser(description=__doc__)
    parser.add_argument("--project-root", type=Path, default=Path.cwd())
    parser.add_argument("--controller-label")
    parser.add_argument("--since", default="24h")
    parser.add_argument("--json", action="store_true", dest="json_output")
    try:
        args = parser.parse_args(argv)
        try:
            since_seconds = _duration(args.since)
            report = analyze(
                args.project_root, since_seconds=since_seconds,
                controller_label=args.controller_label,
            )
        except (OSError, OverflowError, ValueError, TypeError) as exc:
            category = (
                "invalid-input" if isinstance(exc, (OverflowError, ValueError, TypeError))
                else "audit-unavailable"
            )
            report = {
                "project_root": str(args.project_root), "window_seconds": None,
                "controller_label": args.controller_label or "all",
                "scope": "project", "attribution": {}, "commands": None,
                "slowest": None, "repeated_runs": None,
                "notes": [f"audit unavailable: {category}: {type(exc).__name__}"],
            }
    except SystemExit as exc:
        return 0 if exc.code == 0 else 0
    print(json.dumps(report, sort_keys=True) if args.json_output else _text(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
