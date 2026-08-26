#!/usr/bin/env python3
"""Watch a worker log and emit compact goal-flight status JSON."""

from __future__ import annotations

import argparse
import atexit
from collections import deque
import contextlib
import io
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time
import uuid

SCRIPT_DIR = Path(__file__).resolve().parent
ROOT = SCRIPT_DIR.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import goalflight_compat
import goalflight_codex_sessions
import goalflight_dispatch_states
import goalflight_engine_sessions
import goalflight_ledger
import goalflight_quota_stuck
import goalflight_steer_mailbox
import goalflight_task
import goalflight_terminal
from goalflight_agent_limits import moonshot_family
from goalflight_liveness import (
    LivenessThresholds,
    active_monotonic,
    classify_liveness,
    cpu_confirmed_idle,
    cputime_delta_seconds,
    pgroup_cpu_pct,
    pgroup_cputime_snapshot,
    process_group_id,
    system_starved,
    write_status,
)

# `\**` tolerance: grok (and other markdown-emitting workers) write **COMPLETE:**
# etc.; without it the bold marker is never matched and the worker idle-times-out
# instead of waking the orchestrator (grok review, 2026-05-30). Mirrors watch-dispatch-tail.sh.
#
# Hardening (C-P1/D-P1 marker injection): only lines outside ```/~~~ fences are considered
# for markers. Terminal markers (RESULT/COMPLETE/etc) only trigger completion when they are
# the last non-empty line (post prefix-ignore, outside fence). READY/COMPLETE/RESULT/etc.
# Prevents cat/echo/print of marker tokens mid-output or inside fenced examples from
# false-completing the watcher.
_MARKER_KIND_ORDER = (
    "STATUS",
    "STEER-ACK",
    "STEER-REPLY",
    "RESULT",
    "USER-NEED",
    "USER-CONFIRM",
    "BLOCKED",
    "FAILED",
    "COMPLETE",
    "READY",
)
TERMINAL_MARKERS = frozenset(goalflight_terminal.TERMINAL_MARKERS)
TERMINAL_MARKER_KINDS = TERMINAL_MARKERS
SUCCESS_TERMINAL_MARKERS = frozenset(goalflight_terminal.SUCCESS_TERMINAL_MARKERS)
BLOCKING_TERMINAL_MARKERS = TERMINAL_MARKERS - SUCCESS_TERMINAL_MARKERS
MARKER_KINDS = frozenset(
    kind
    for kind in _MARKER_KIND_ORDER
    if kind in TERMINAL_MARKERS or kind in {"STATUS", "STEER-ACK", "STEER-REPLY"}
)
_MARKER_KIND_ALTERNATION = "|".join(re.escape(kind) for kind in _MARKER_KIND_ORDER if kind in MARKER_KINDS)
_TERMINAL_MARKER_KIND_ALTERNATION = "|".join(re.escape(kind) for kind in _MARKER_KIND_ORDER if kind in TERMINAL_MARKERS)
# Optional `!` comes from the shared grammar used by ACP extraction, permission
# guarding, ACK parsing, and this watcher. Consumer-specific fence, prompt-echo,
# markdown, and terminal-position rules remain local.
_SIGIL_OPT = goalflight_terminal.MARKER_SIGIL_OPT_RE
SHELL_TERMINAL_MARKER_RE = rf"^{_SIGIL_OPT}\**({_TERMINAL_MARKER_KIND_ALTERNATION}):\**"
MARKER_RE = re.compile(rf"^\**{_SIGIL_OPT}\**({_MARKER_KIND_ALTERNATION}):\**\s*(.*)$")
FINAL_TERMINAL_MARKER_RE = re.compile(
    rf"^(?:-\s+)?`?\**{_SIGIL_OPT}\**(?:STATUS:\s*)?"
    rf"({_TERMINAL_MARKER_KIND_ALTERNATION}):(.*)$"
)
MARKER_VOCAB_BULLET_RE = re.compile(
    rf"^-\s+`{_SIGIL_OPT}(?:{_MARKER_KIND_ALTERNATION}):`\s*$"
)
COMPLETION_SIGNOFF_RE = re.compile(
    r"^(?:STATUS:\s*)?(DONE|COMPLETE|FINISHED)\s*:?\s*[.!?]?$",
    re.IGNORECASE,
)
BARE_TERMINAL_MARKER_RE = re.compile(
    rf"^(?:{_SIGIL_OPT}(?:{_TERMINAL_MARKER_KIND_ALTERNATION}):\s*.*|"
    r"(?:DONE|COMPLETE|FINISHED)\s*:?\s*[.!?]?)$",
    re.IGNORECASE,
)
HARNESS_HOOK_TRAILER_LINES = frozenset({"hook: Stop", "hook: Stop Completed"})
HARNESS_TOKEN_COUNT_RE = re.compile(r"^\d{1,3}(?:,\d{3})*$|^\d+$")
# Session-resume footer some agent CLIs print AFTER the worker's own final line,
# e.g. "To resume this session: kimi -r session_<id>". The worker emitted its
# terminal marker correctly; the CLI then appended a line of its own, so the
# marker was no longer last and the live watcher scored a finished worker as
# dead. Observed repeatedly on kimi/cursor dispatches, each needing manual
# salvage of work that was already complete and staged.
HARNESS_RESUME_FOOTER_RE = re.compile(
    r"^(?:to\s+)?(?:resume|continue)\s+(?:this\s+)?session\s*:",
    re.IGNORECASE,
)


def _task_breadcrumb_error_is_missing_item(error: object) -> bool:
    """True when the breadcrumb failed only because the task id is not in the store.

    Distinguishes a bad dispatch input (a task id this repo does not have) from a
    broken store (corrupt or unwritable). Only the former leaves the worker's
    verdict intact; anything unrecognised is treated as the latter, so an
    unfamiliar error still blocks.
    """
    if not isinstance(error, dict):
        return False
    message = str(error.get("message") or "").lower()
    return "item not found" in message


def _is_harness_trailer_line(stripped: str) -> bool:
    """A line the AGENT CLI appended, not worker output."""
    return (
        stripped in HARNESS_HOOK_TRAILER_LINES
        or bool(HARNESS_RESUME_FOOTER_RE.match(stripped))
    )
HUNK_HEADER_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+\d+(?:,\d+)? @@")
PROMPT_ECHO_ANCHOR_SEARCH_LINES = 200
PROMPT_ECHO_MAX_ANCHORS = 10
TAIL_SCAN_CHUNK_BYTES = 64 * 1024
TAIL_SCAN_BOUNDARY_BYTES = 64
# Completed-tail reconciliation may scan beyond the bounded live window, but a
# single physical line must never defeat that memory bound. Every text read is
# capped; overlong physical lines keep only this prefix and skip the remainder.
STREAM_READ_CHUNK_CHARS = 64 * 1024
# CPU-sampling-failure grace (codex 2026-05-20 P2): idle_timeout exits only on
# confirmed-idle CPU. Unavailable CPU (ps failure -> None) keeps waiting instead
# of false-killing a healthy quiet worker. The streak still protects against
# one-off noisy idle samples.
WEDGE_CONFIRM_SAMPLES = 2
REPLY_WAIT_MARKER_KINDS = frozenset({"USER-NEED", "USER-CONFIRM"})
WORKER_WAIT_ARM_GRACE_SECS = 1.0
# Live salvage CANDIDATE: tail stale + tree quiet + cumulative CPU flat.
# Detection only; the watcher never kills. 5 minutes was rejected: healthy
# grok tails grow in bursts (521→841→985→1130 bytes) with multi-minute
# silences and CPU 0 between bursts; a 5-minute window sat inside that
# burst-gap range, and one healthy worker was quiet 27 minutes. Default
# 15 minutes. Even then this is not a verdict — a worker waiting on a
# remote/studio job matches all three legs while healthy. A clean
# separation in a small same-source sample is not a property of the world.
WORKER_STALLED_CANDIDATE_STATE = "worker_stalled_candidate"
WORKER_WEDGED_STATE = WORKER_STALLED_CANDIDATE_STATE  # alias; not an authoritative verdict
DEFAULT_WEDGE_IDLE_SECS = 900.0
WEDGE_CPU_DELTA_EPSILON_S = 0.05
WEDGE_CAVEAT = (
    "remote-wait and burst-gap workers match this signature while healthy; "
    "controller judgment required"
)
WEDGE_EVIDENCE_KEYS = (
    "tail_age_s",
    "tree_age_s",
    "cpu_delta_s",
    "sample_interval_s",
    "threshold_s",
    "tail_bytes_grown",
    "tree_scan_kind",
    "tree_scan_root",
    "authoritative",
    "caveat",
)
WEDGE_TREE_LEG_WORKER_CWD = "worker_cwd"
WEDGE_TREE_LEG_INDETERMINATE = "indeterminate"
_TREE_SKIP_DIR_NAMES = frozenset(
    {
        ".git",
        "node_modules",
        "__pycache__",
        ".venv",
        "venv",
        ".mypy_cache",
        ".pytest_cache",
        ".tox",
        ".ruff_cache",
        "dist",
        "build",
        ".eggs",
        ".goal-flight",
    }
)
# A terminal marker is the worker's final act. Three default two-second poll
# intervals give wrappers time to flush and exit before the watcher decides
# whether later output disproved the candidate. This is a minimum decision
# grace, not a hard timeout for a live worker.
POST_TERMINAL_EXIT_GRACE_SECS = 6.0
BLOCKED_TASK_BREADCRUMB_STATE = "blocked_task_breadcrumb"
TRACE_RESOLUTION_RETRY_SECS = 300.0
TRACE_LSOF_TIMEOUT_SECS = 1.0
TRACE_LONG_RUNNING_SECS = 12 * 60 * 60.0
TRACE_REVIEW_SECS = 48 * 60 * 60.0


def classify_worker_wedge(
    *,
    worker_alive: bool,
    tail_age_s: float | None,
    tree_age_s: float | None,
    cpu_delta_s: float | None,
    sample_interval_s: float | None,
    threshold_s: float = DEFAULT_WEDGE_IDLE_SECS,
    cpu_epsilon_s: float = WEDGE_CPU_DELTA_EPSILON_S,
    tail_bytes_grown: int | None = None,
) -> dict | None:
    """Return a stall CANDIDATE when all three legs are sustained, else None.

    Legs, all required: tail mtime stale, worker-tree quiet, cumulative CPU
    time flat across two spaced samples. A single ``%cpu`` snapshot is not
    evidence — between operations it reads 0.0 and lies.

    This is not a verdict. A worker waiting on a remote/studio job is CPU-0,
    tail-quiet, and tree-quiet indefinitely while healthy; burst-gap grok
    workers look the same for many minutes between writes. ``tail_bytes_grown``
    is recorded for the controller, not used as a discriminator.
    """
    if not worker_alive:
        return None
    if threshold_s is None or threshold_s <= 0:
        return None
    if tail_age_s is None or tree_age_s is None:
        return None
    if cpu_delta_s is None or sample_interval_s is None:
        return None
    if sample_interval_s <= 0:
        return None
    if tail_age_s < threshold_s:
        return None
    if tree_age_s < threshold_s:
        return None
    if cpu_delta_s > cpu_epsilon_s:
        return None
    grown = 0 if tail_bytes_grown is None else max(0, int(tail_bytes_grown))
    return {
        "state": WORKER_STALLED_CANDIDATE_STATE,
        "tail_age_s": float(tail_age_s),
        "tree_age_s": float(tree_age_s),
        "cpu_delta_s": float(cpu_delta_s),
        "sample_interval_s": float(sample_interval_s),
        "threshold_s": float(threshold_s),
        "tail_bytes_grown": grown,
        "authoritative": False,
        "caveat": WEDGE_CAVEAT,
    }


def wedge_transition(*, was_wedged: bool, is_wedged: bool) -> str | None:
    if is_wedged and not was_wedged:
        return "enter"
    if was_wedged and not is_wedged:
        return "recover"
    return None


def emit_wedge_event(kind: str, dispatch_id: str, evidence: dict | None) -> None:
    tag = "WATCHER-STALL-CANDIDATE" if kind == "enter" else "WATCHER-STALL-CLEAR"
    body: dict[str, object] = {
        "dispatch_id": dispatch_id,
        "state": WORKER_STALLED_CANDIDATE_STATE if kind == "enter" else "running",
        "authoritative": False,
    }
    if isinstance(evidence, dict):
        for key in WEDGE_EVIDENCE_KEYS:
            if key in evidence:
                body[key] = evidence[key]
    print(tag + " " + json.dumps(body, sort_keys=True), flush=True)


def apply_worker_wedge(
    payload: dict,
    *,
    evidence: dict | None,
    previously_wedged: bool,
    dispatch_id: str,
) -> dict:
    """Flag a live payload as a stall candidate. Never kills, never terminalizes."""
    is_wedged = evidence is not None
    event = wedge_transition(was_wedged=previously_wedged, is_wedged=is_wedged)
    if is_wedged:
        payload["state"] = WORKER_STALLED_CANDIDATE_STATE
        payload["liveness_state"] = WORKER_STALLED_CANDIDATE_STATE
        payload["reason"] = "worker_stalled_candidate"
        payload["wedge_evidence"] = {
            key: evidence[key] for key in WEDGE_EVIDENCE_KEYS if key in evidence
        }
    if event is not None:
        emit_wedge_event(
            event,
            dispatch_id,
            evidence if evidence is not None else payload.get("wedge_evidence"),
        )
    return {"event": event, "wedged": is_wedged}


def newest_mtime_under(
    root: Path | None,
    *,
    skip_names: frozenset[str] = _TREE_SKIP_DIR_NAMES,
    stop_if_newer_than: float | None = None,
) -> float | None:
    """Newest file mtime under ``root``, skipping cache/VCS noise dirs.

    When ``stop_if_newer_than`` is set, return as soon as any file is newer
    than that cutoff (the tree is not quiet). Does not follow symlinks.
    """
    if root is None:
        return None
    try:
        if not root.is_dir():
            return None
    except OSError:
        return None
    newest: float | None = None
    try:
        for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
            dirnames[:] = [name for name in dirnames if name not in skip_names]
            for name in filenames:
                path = Path(dirpath) / name
                try:
                    mtime = path.stat().st_mtime
                except OSError:
                    continue
                if newest is None or mtime > newest:
                    newest = mtime
                if stop_if_newer_than is not None and mtime > stop_if_newer_than:
                    return mtime
    except OSError:
        return newest
    return newest


def _expand_path(raw: object) -> Path | None:
    if raw in (None, ""):
        return None
    try:
        return Path(str(raw)).expanduser()
    except (TypeError, ValueError):
        return None


def _resolved_path(path: Path) -> Path:
    try:
        return path.resolve(strict=False)
    except OSError:
        return path


def _argv_option_value(argv: object, flag: str) -> str | None:
    if not isinstance(argv, list):
        return None
    prefix = flag + "="
    for index, item in enumerate(argv):
        if not isinstance(item, str):
            continue
        if item == flag and index + 1 < len(argv):
            nxt = argv[index + 1]
            if isinstance(nxt, str) and not nxt.startswith("-"):
                return nxt
        if item.startswith(prefix) and len(item) > len(prefix):
            return item[len(prefix) :]
    return None


def _first_path(*raws: object) -> Path | None:
    for raw in raws:
        path = _expand_path(raw)
        if path is not None:
            return path
    return None


def resolve_wedge_tree_leg(
    record: dict | None = None,
    *,
    project_root: Path | str | None = None,
    worker_cwd: Path | str | None = None,
    status: dict | None = None,
) -> dict:
    """Choose the tree the stall detector may scan.

    A distinct per-dispatch cwd (b-217 ``worker_cwd`` / ``-C``) is the only
    shape this leg can measure: sibling worktrees under the canonical root
    are other workers' writes. When cwd is missing or *is* the canonical
    root, the leg is indeterminate — do not pretend the shared tree is this
    worker's life.
    """
    rec = record if isinstance(record, dict) else {}
    sidecar = status if isinstance(status, dict) else {}
    envelope = rec.get("request_envelope") if isinstance(rec.get("request_envelope"), dict) else {}
    env_request = envelope.get("request") if isinstance(envelope.get("request"), dict) else {}
    request = rec.get("request") if isinstance(rec.get("request"), dict) else {}
    cwd = _first_path(
        worker_cwd,
        rec.get("worker_cwd"),
        _argv_option_value(rec.get("dispatch_argv"), "--cwd"),
        env_request.get("cwd"),
        request.get("cwd"),
        sidecar.get("worker_cwd"),
        sidecar.get("worktree_path"),
    )
    canonical = _first_path(
        rec.get("project_root"),
        project_root,
        sidecar.get("project_root"),
    )
    cwd_s = str(_resolved_path(cwd)) if cwd is not None else None
    canonical_s = str(_resolved_path(canonical)) if canonical is not None else None
    if cwd is None:
        return {
            "kind": WEDGE_TREE_LEG_INDETERMINATE,
            "reason": "missing_worker_cwd",
            "scan_root": None,
            "worker_cwd": None,
            "canonical_root": canonical_s,
        }
    if canonical is None:
        return {
            "kind": WEDGE_TREE_LEG_INDETERMINATE,
            "reason": "missing_canonical_root",
            "scan_root": None,
            "worker_cwd": cwd_s,
            "canonical_root": None,
        }
    cwd_cmp = _resolved_path(cwd)
    canonical_cmp = _resolved_path(canonical)
    if cwd_cmp == canonical_cmp:
        return {
            "kind": WEDGE_TREE_LEG_INDETERMINATE,
            "reason": "cwd_is_canonical_root",
            "scan_root": None,
            "worker_cwd": str(cwd_cmp),
            "canonical_root": str(canonical_cmp),
        }
    return {
        "kind": WEDGE_TREE_LEG_WORKER_CWD,
        "reason": "distinct_worker_cwd",
        "scan_root": cwd_cmp,
        "worker_cwd": str(cwd_cmp),
        "canonical_root": str(canonical_cmp),
    }


def serialize_cputime_sample(sample: dict[int, float] | None) -> dict[str, float] | None:
    if not sample:
        return None
    out: dict[str, float] = {}
    for pid, seconds in sample.items():
        try:
            out[str(int(pid))] = float(seconds)
        except (TypeError, ValueError):
            continue
    return out or None


def deserialize_cputime_sample(raw: object) -> dict[int, float] | None:
    if not isinstance(raw, dict) or not raw:
        return None
    out: dict[int, float] = {}
    for key, value in raw.items():
        try:
            out[int(key)] = float(value)
        except (TypeError, ValueError):
            continue
    return out or None


def load_wedge_watch_state(payload: dict | None) -> dict:
    """Restore last CPU sample + announcement from the status sidecar.

    Chosen persistence: the watcher's own status.json, written every poll.
    CPU samples cannot be derived from other durable inputs; announcement
    can also be inferred from ``state == worker_stalled_candidate``.
    """
    empty = {
        "cputime_sample": None,
        "cputime_sampled_at": None,
        "candidate_announced_at": None,
    }
    if not isinstance(payload, dict):
        return dict(empty)
    blob = payload.get("wedge_watch")
    if not isinstance(blob, dict):
        blob = {}
    sampled_at = blob.get("cputime_sampled_at")
    announced = blob.get("candidate_announced_at")
    try:
        sampled_at_f = float(sampled_at) if sampled_at is not None else None
    except (TypeError, ValueError):
        sampled_at_f = None
    try:
        announced_f = float(announced) if announced is not None else None
    except (TypeError, ValueError):
        announced_f = None
    if announced_f is None and payload.get("state") in {
        WORKER_STALLED_CANDIDATE_STATE,
        "worker_wedged",
    }:
        updated = payload.get("updated_at")
        try:
            announced_f = float(updated) if updated is not None else 0.0
        except (TypeError, ValueError):
            announced_f = 0.0
    return {
        "cputime_sample": deserialize_cputime_sample(blob.get("cputime_sample")),
        "cputime_sampled_at": sampled_at_f,
        "candidate_announced_at": announced_f,
    }


def dump_wedge_watch_state(
    *,
    cputime_sample: dict[int, float] | None,
    cputime_sampled_at: float | None,
    candidate_announced_at: float | None,
) -> dict:
    return {
        "cputime_sample": serialize_cputime_sample(cputime_sample),
        "cputime_sampled_at": cputime_sampled_at,
        "candidate_announced_at": candidate_announced_at,
    }


def _read_json_object(path: Path) -> dict | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _load_dispatch_record(dispatch_id: str) -> dict | None:
    if not dispatch_id:
        return None
    try:
        path = goalflight_ledger.record_path(dispatch_id, create=False)
        return _read_json_object(path)
    except OSError:
        return None


def _tail_mtime_age_s(path: Path, *, now: float) -> float | None:
    try:
        return max(0.0, now - path.stat().st_mtime)
    except OSError:
        return None


def _post_terminal_candidate_action(
    *,
    worker_alive: bool,
    tail_grew: bool,
    grace_expired: bool,
    idle_confirmed: bool,
) -> str:
    """Choose the pure state transition for a pending success marker.

    Continued output from the same live worker disproves a terminal candidate
    once the short exit grace has elapsed. Without growth, the watcher keeps
    the candidate pending until either process identity says the worker died or
    the ordinary max-idle path is confirmed.
    """
    if not worker_alive:
        return "terminalize"
    if not grace_expired:
        return "pending"
    if tail_grew:
        return "discard"
    if idle_confirmed:
        return "terminalize"
    return "pending"


def _known_trace_roots(*, state_dir: Path | None = None, home: Path | None = None) -> tuple[Path, ...]:
    user_home = (home or Path.home()).expanduser().resolve(strict=False)
    machine_state = (state_dir or goalflight_ledger.state_dir()).expanduser().resolve(strict=False)
    roots = [
        user_home / ".codex" / "sessions",
        user_home / ".kimi-code" / "sessions",
    ]
    dispatch_homes = (machine_state / "dispatch-homes").resolve(strict=False)
    try:
        roots.extend(
            path / "sessions"
            for path in dispatch_homes.iterdir()
            if path.is_dir()
            and not path.is_symlink()
            and path.resolve(strict=False).parent == dispatch_homes
        )
    except OSError:
        pass
    return tuple(path.resolve(strict=False) for path in roots)


def _path_under_known_trace_root(path: Path, roots: tuple[Path, ...]) -> bool:
    try:
        resolved = path.expanduser().resolve(strict=False)
        return any(resolved != root and resolved.is_relative_to(root) for root in roots)
    except (OSError, RuntimeError, ValueError):
        return False


def _newest_trace_file(root: Path, roots: tuple[Path, ...]) -> Path | None:
    try:
        candidates = [
            path.resolve(strict=False)
            for path in root.rglob("*")
            if path.is_file() and _path_under_known_trace_root(path, roots)
        ]
        return max(candidates, key=lambda path: (path.stat().st_mtime, str(path))) if candidates else None
    except (OSError, RuntimeError, ValueError):
        return None


def _parse_ppid_children(ps_output: str) -> dict[int, list[int]]:
    children: dict[int, list[int]] = {}
    for line in ps_output.splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue
        try:
            children.setdefault(int(parts[1]), []).append(int(parts[0]))
        except ValueError:
            continue
    return children


def _walk_process_tree(pid: int, children: dict[int, list[int]]) -> tuple[int, ...]:
    found: list[int] = []
    pending = [pid]
    while pending:
        current = pending.pop()
        if current in found:
            continue
        found.append(current)
        pending.extend(children.get(current, ()))
    return tuple(found)


def _worker_process_tree(pid: int, *, ps_runner=None) -> tuple[int, ...]:
    runner = ps_runner or subprocess.run
    try:
        proc = runner(
            ["ps", "-axo", "pid=,ppid="],
            capture_output=True,
            text=True,
            timeout=TRACE_LSOF_TIMEOUT_SECS,
            check=False,
        )
        children = _parse_ppid_children(proc.stdout or "")
        return _walk_process_tree(pid, children)
    except (OSError, subprocess.TimeoutExpired, TypeError, ValueError):
        return (pid,)


def live_descendant_count(pid: int, *, ps_runner=None) -> int | None:
    """Live descendants of ``pid``, excluding itself.

    None means the sample was unavailable. 0 means the walk ran and found
    no children. Callers must not treat None as idle.
    """
    runner = ps_runner or subprocess.run
    try:
        proc = runner(
            ["ps", "-axo", "pid=,ppid="],
            capture_output=True,
            text=True,
            timeout=2.0,
            check=False,
        )
        stdout = proc.stdout or ""
        if not stdout.strip():
            return None
        tree = _walk_process_tree(pid, _parse_ppid_children(stdout))
        return max(0, len(tree) - 1)
    except (OSError, subprocess.TimeoutExpired, TypeError, ValueError):
        return None


def _trace_from_lsof(
    pid: int,
    roots: tuple[Path, ...],
    *,
    lsof_runner=None,
    ps_runner=None,
) -> Path | None:
    runner = lsof_runner or subprocess.run
    pids = _worker_process_tree(pid, ps_runner=ps_runner)
    try:
        proc = runner(
            ["lsof", "-Fn", "-p", ",".join(str(value) for value in pids)],
            capture_output=True,
            text=True,
            timeout=TRACE_LSOF_TIMEOUT_SECS,
            check=False,
        )
        candidates = []
        for line in (proc.stdout or "").splitlines():
            if not line.startswith("n"):
                continue
            path = Path(line[1:])
            if _path_under_known_trace_root(path, roots) and path.is_file():
                candidates.append(path.resolve(strict=False))
        return max(candidates, key=lambda path: (path.stat().st_mtime, str(path))) if candidates else None
    except (OSError, subprocess.TimeoutExpired, RuntimeError, ValueError):
        return None


class TraceLiveness:
    """Resolve a deterministic worker trace once, then use stat-only polling."""

    def __init__(
        self,
        *,
        dispatch_id: str | None,
        worker_pid: int,
        effective_account: str | None = None,
        cached_path: str | None = None,
        state_dir: Path | None = None,
        home: Path | None = None,
        started_mono: float | None = None,
        retry_secs: float = TRACE_RESOLUTION_RETRY_SECS,
        lsof_runner=None,
        ps_runner=None,
    ):
        self.dispatch_id = dispatch_id
        self.worker_pid = worker_pid
        self.effective_account = effective_account
        self.state_dir = (state_dir or goalflight_ledger.state_dir()).resolve(strict=False)
        self.roots = _known_trace_roots(state_dir=self.state_dir, home=home)
        self.started_mono = active_monotonic() if started_mono is None else started_mono
        self.retry_secs = retry_secs
        self.lsof_runner = lsof_runner
        self.ps_runner = ps_runner
        self.path = None
        if cached_path:
            candidate = Path(cached_path)
            if _path_under_known_trace_root(candidate, self.roots):
                self.path = candidate.resolve(strict=False)

    def _resolve(self, now_mono: float) -> None:
        if self.path is not None or now_mono - self.started_mono > self.retry_secs:
            return
        if self.dispatch_id and self.effective_account:
            dispatch_homes = (self.state_dir / "dispatch-homes").resolve(strict=False)
            dispatch_home = (dispatch_homes / self.dispatch_id).resolve(strict=False)
            if dispatch_home.parent == dispatch_homes:
                pinned_root = (dispatch_home / "sessions").resolve(strict=False)
                if pinned_root.parent == dispatch_home:
                    pinned_roots = self.roots + (pinned_root,)
                    self.path = _newest_trace_file(pinned_root, pinned_roots)
                    if self.path is not None:
                        self.roots = pinned_roots
                        return
        self.path = _trace_from_lsof(
            self.worker_pid,
            self.roots,
            lsof_runner=self.lsof_runner,
            ps_runner=self.ps_runner,
        )

    def sample(self, *, now_epoch: float, now_mono: float, idle_threshold: float) -> dict:
        try:
            self._resolve(now_mono)
            if self.path is None:
                return {}
            mtime = self.path.stat().st_mtime
            return {
                "trace_path": str(self.path),
                "trace_mtime": mtime,
                "trace_active": bool(
                    idle_threshold > 0
                    and 0 <= now_epoch - mtime < idle_threshold
                ),
            }
        except (OSError, RuntimeError, ValueError):
            return {"trace_path": str(self.path)} if self.path is not None else {}


def _trace_vetoes_idle(*, trace_active: bool) -> bool:
    # reports of my death are greatly exaggerated.
    return bool(trace_active)


def _trace_attention_state(
    *,
    trace_active: bool,
    runtime_secs: float,
    long_running_secs: float,
    review_secs: float,
) -> str | None:
    if not trace_active:
        return None
    if review_secs > 0 and runtime_secs >= review_secs:
        return "long_running_review"
    if long_running_secs > 0 and runtime_secs >= long_running_secs:
        return "long_running"
    return None


def post_trace_attention(
    dispatch_id: str | None,
    state: str | None,
    posted_states: set[str],
    *,
    post_func=None,
) -> None:
    if not dispatch_id or state not in {"long_running", "long_running_review"} or state in posted_states:
        return
    posted_states.add(state)
    try:
        if post_func is None:
            import goalflight_messages as gm

            def post_func(**kwargs):
                return gm.post_message(messages_dir=gm.default_messages_dir(), **kwargs)

        post_func(
            dispatch_id=dispatch_id,
            msg_type="monitor",
            payload={
                "text": (
                    f"{state}: quiet console with an actively growing session trace; "
                    "worker remains live and requires controller attention."
                )
            },
            source={"node": "local", "adapter": "watcher", "transport": "trace-liveness"},
        )
    except Exception:
        return


def _read_active_worker_wait(
    path: Path,
    dispatch_id: str,
    *,
    now_mono: float,
    worker_pid: int | None = None,
    worker_pgid: int | None = None,
) -> tuple[dict | None, bool]:
    try:
        entries = goalflight_steer_mailbox.read_steer_entries(
            path,
            lock_timeout_secs=0.05,
            quarantine_errors=False,
        )
        return (
            goalflight_steer_mailbox.active_worker_wait(
                entries,
                dispatch_id=dispatch_id,
                now_mono=now_mono,
                worker_pid=worker_pid,
                worker_pgid=worker_pgid,
            ),
            True,
        )
    except (OSError, RuntimeError, ValueError):
        return None, False
    except Exception as exc:
        if goalflight_steer_mailbox.is_carrier_error(exc):
            return None, False
        raise


def _active_worker_wait(
    path: Path,
    dispatch_id: str,
    *,
    now_mono: float,
    worker_pid: int | None = None,
    worker_pgid: int | None = None,
) -> dict | None:
    wait_state, _read_succeeded = _read_active_worker_wait(
        path,
        dispatch_id,
        now_mono=now_mono,
        worker_pid=worker_pid,
        worker_pgid=worker_pgid,
    )
    return wait_state


def _cached_worker_wait_is_valid(
    wait_state: dict | None,
    *,
    now_mono: float,
    worker_pid: int,
    worker_pgid: int | None,
) -> bool:
    """Revalidate cached identity/deadline while the mailbox lock is unavailable."""
    if not wait_state:
        return False
    deadline_ns = wait_state.get("deadline_awake_mono_ns")
    waiter_pid = wait_state.get("waiter_pid")
    waiter_token = wait_state.get("waiter_start_token")
    waiter_pgid = wait_state.get("waiter_pgid")
    if (
        not isinstance(deadline_ns, int)
        or isinstance(deadline_ns, bool)
        or int(now_mono * 1_000_000_000) >= deadline_ns
        or isinstance(waiter_pid, bool)
        or not isinstance(waiter_pid, int)
        or waiter_pid <= 0
        or not isinstance(waiter_token, str)
        or not waiter_token
        or goalflight_compat.process_identity_matches(waiter_pid, waiter_token) is not True
    ):
        return False
    if worker_pgid is not None:
        return bool(
            isinstance(waiter_pgid, int)
            and not isinstance(waiter_pgid, bool)
            and waiter_pgid == worker_pgid
            and process_group_id(waiter_pid) == worker_pgid
        )
    return waiter_pid == worker_pid


def _worker_wait_marker_matches(
    marker: dict | None,
    wait_state: dict,
    dispatch_id: str,
) -> bool:
    return bool(
        marker
        and marker.get("kind") == wait_state.get("question_kind")
        and marker.get("text") == wait_state.get("question_marker_text")
        and _terminal_marker_matches_dispatch(marker, dispatch_id)
    )


def _worker_wait_reply_output_matches(
    marker: dict | None,
    wait_state: dict,
) -> bool:
    if not marker or marker.get("kind") != "STEER-REPLY":
        return False
    try:
        payload = json.loads(str(marker.get("text") or ""))
    except (json.JSONDecodeError, TypeError, ValueError):
        return False
    expected_reply_seq = wait_state.get("reply_seq")
    reply_seq = payload.get("seq") if isinstance(payload, dict) else None
    return bool(
        isinstance(payload, dict)
        and wait_state.get("phase") == "reply_pending"
        and payload.get("kind") == goalflight_steer_mailbox.WORKER_WAIT_REPLY_KIND
        and payload.get("reply_to") == wait_state.get("wait_id")
        and isinstance(expected_reply_seq, int)
        and not isinstance(expected_reply_seq, bool)
        and expected_reply_seq > 0
        and isinstance(reply_seq, int)
        and not isinstance(reply_seq, bool)
        and reply_seq > 0
        and reply_seq == expected_reply_seq
    )


def post_worker_wait_attention(
    dispatch_id: str,
    wait_state: dict,
    marker: dict | None,
    posted: set[tuple[str, int, str]],
    *,
    post_func=None,
) -> None:
    """Bridge a non-terminal waiting question without claiming a listener lease."""
    if (
        not _worker_wait_marker_matches(marker, wait_state, dispatch_id)
    ):
        return
    wait_id = str(wait_state.get("wait_id") or "")
    try:
        line = int(marker.get("line") or 0)
    except (TypeError, ValueError, OverflowError):
        return
    key = (wait_id, line, str(marker["kind"]))
    if not wait_id or line <= 0 or key in posted:
        return
    try:
        if post_func is None:
            import goalflight_messages as gm

            def post_func(**kwargs):
                return gm.post_message(messages_dir=gm.default_messages_dir(), **kwargs)

            marker_type = gm.marker_type(str(marker["kind"]))
            payload = gm.marker_payload(
                str(marker["kind"]),
                str(marker.get("text") or ""),
            )
        else:
            marker_type = (
                "user_need" if marker["kind"] == "USER-NEED" else "user_confirm"
            )
            payload = {"text": str(marker.get("text") or "")}
        payload.update({"awaiting_reply": True, "wait_id": wait_id})
        post_func(
            dispatch_id=dispatch_id,
            msg_type=marker_type,
            payload=payload,
            source={
                "node": "local",
                "adapter": "watcher",
                "transport": "steer-wait",
            },
            event_id=str(
                uuid.uuid5(
                    uuid.NAMESPACE_URL,
                    f"goalflight:steer-wait:{dispatch_id}:{wait_id}:{line}:{marker['kind']}",
                )
            ),
        )
        posted.add(key)
    except Exception:
        # A stable event id makes the next poll an idempotent retry if the
        # carrier write succeeded before a later delivery step failed.
        return


def _trace_ledger_account(dispatch_id: str | None) -> str | None:
    if not dispatch_id:
        return None
    try:
        payload = json.loads(
            goalflight_ledger.record_path(dispatch_id, create=False).read_text(
                encoding="utf-8"
            )
        )
        account = payload.get("effective_account")
        return account if isinstance(account, str) and account else None
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return None


def _trace_ledger_started_epoch(dispatch_id: str | None) -> float | None:
    if not dispatch_id:
        return None
    try:
        payload = json.loads(
            goalflight_ledger.record_path(dispatch_id, create=False).read_text(
                encoding="utf-8"
            )
        )
        started = goalflight_ledger.parse_utc(payload.get("started_at"))
        return started.timestamp() if started is not None else None
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return None


def _cached_trace_path(status_path: Path) -> str | None:
    try:
        payload = json.loads(status_path.read_text(encoding="utf-8"))
        value = payload.get("trace_path")
        return value if isinstance(value, str) and value else None
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return None


def _marker_state(marker: dict | None) -> str:
    if marker and marker.get("kind") in SUCCESS_TERMINAL_MARKERS:
        return "complete"
    return "blocked"


def _exit_code_for_state(state: str) -> int:
    if state == "complete":
        return 0
    if state == "worker_dead":
        return 1
    if goalflight_dispatch_states.is_limit_state(state):
        return 1
    if state == "idle_timeout":
        return 2
    if state in {"orphaned", "controller_dead"}:
        return 3
    if state == "blocked" or state.startswith("blocked"):
        return 4
    return 1


def _split_task_ids(value: str | None) -> list[str]:
    out: list[str] = []
    for part in (value or "").split(","):
        task_id = part.strip()
        if task_id and task_id not in out:
            out.append(task_id)
    return out


def _task_state_for_terminal(dispatch_state: object) -> str:
    return "worker-finished" if dispatch_state == "complete" else "worker-failed"


def _cleanup_codex_dispatch_home(
    dispatch_id: str,
    agent: object,
    *,
    detached: bool,
    home_resolved: bool,
    codex_session_id: str | None = None,
) -> None:
    """Best-effort watcher ownership for terminal detached-bash cleanup."""
    if (
        not detached
        or not home_resolved
        or codex_session_id is not None
        or goalflight_ledger.infer_engine(agent) != "codex"
    ):
        return
    try:
        import goalflight_dispatch

        goalflight_dispatch.cleanup_codex_dispatch_home(dispatch_id)
    except BaseException as exc:
        print(
            "goalflight_watch: per-dispatch home cleanup warning: "
            f"{type(exc).__name__}",
            file=sys.stderr,
        )


def _maybe_note_grok_quota(dispatch_id: object, state: object) -> None:
    """Best-effort: a grok 402 should flip the seat cache now, not after the TTL."""
    if state != "quota_exhausted":
        return
    try:
        import grok_seats

        path = goalflight_ledger.record_path(str(dispatch_id), create=False)
        record = json.loads(path.read_text(encoding="utf-8"))
        grok_seats.note_exhausted_if_proven(
            record, state=str(state) if state is not None else None
        )
    except BaseException:
        # Optional seat-cache freshness only: terminal authority is already
        # committed, and the next seat probe repairs a missed cache flip.
        return


def _finish_existing_ledger(
    dispatch_id: str,
    state: object,
    reason: object,
    *,
    worker_still_alive: object = None,
    agent: object = "unknown",
    detached: bool = False,
    codex_dispatch_home_resolved: bool = False,
    codex_session_id: str | None = None,
    terminal_marker: dict | None = None,
    headline: str | None = None,
) -> dict | None:
    if not dispatch_id or not state:
        return None
    if state in {WORKER_STALLED_CANDIDATE_STATE, "worker_wedged"}:
        # Live salvage candidate: the process is still here. Committing
        # terminal authority would trip worker_dead-shaped cleanup.
        return None
    if state == "watcher_stopped" and worker_still_alive is True:
        return None
    emitter_reason: object = reason
    headline_text = headline.strip() if isinstance(headline, str) else ""
    if (
        isinstance(terminal_marker, dict)
        and terminal_marker.get("kind") in WORKER_MAIL_MARKER_KINDS
    ):
        marker_text = _strip_marker_decoration(
            str(terminal_marker.get("text") or "")
        ).strip()
        emitter_reason = {
            "reason": reason,
            "marker_kind": terminal_marker.get("kind"),
            "text": marker_text,
        }
        if not headline_text:
            headline_text = marker_text
    elif (
        not headline_text
        and isinstance(terminal_marker, dict)
        and terminal_marker.get("kind") in HEADLINE_MAIL_MARKER_KINDS
    ):
        headline_text = _strip_marker_decoration(
            str(terminal_marker.get("text") or "")
        ).strip()
    headline_text = headline_text or None
    try:
        path = goalflight_ledger.record_path(dispatch_id, create=False)
        if not path.exists():
            committed = goalflight_ledger.commit_terminal_authority(
                {
                    "dispatch_id": dispatch_id,
                    "project_root": str(Path.cwd()),
                },
                state=str(state),
                reason=emitter_reason,
                worker_still_alive=(
                    worker_still_alive if isinstance(worker_still_alive, bool) else None
                ),
                headline=headline_text,
            )
            if committed.committed:
                return None
            return {
                "type": "TerminalCommitRefused",
                "message": (
                    f"journal terminal emitter returned {committed.disposition.value}: "
                    f"{committed.reason}"
                ),
            }
        max_attempts = 3
        backoff_s = 0.05
        last_error: dict | None = None
        for attempt in range(max_attempts):
            try:
                with contextlib.redirect_stdout(io.StringIO()):
                    code = goalflight_ledger.cmd_finish(
                        argparse.Namespace(
                            dispatch_id=dispatch_id,
                            state=str(state),
                            reason=emitter_reason,
                            terminal_state=None,
                            elapsed_s=None,
                            worker_still_alive=worker_still_alive,
                            headline=headline_text,
                        )
                    )
                if code == 0:
                    _maybe_note_grok_quota(dispatch_id, state)
                    return None
                last_error = {
                    "type": "TerminalCommitRefused",
                    "message": f"journal terminal emitter exited {code}",
                }
                if attempt + 1 < max_attempts:
                    time.sleep(backoff_s * (attempt + 1))
            except Exception as exc:
                # Exit publication remains nonfatal here: losing this attempt
                # gives up only immediate journal completion. The returned
                # error is embedded in final watcher status for reconciliation.
                last_error = {"type": type(exc).__name__, "message": str(exc)}
                if attempt + 1 < max_attempts:
                    time.sleep(backoff_s * (attempt + 1))
        return last_error
    finally:
        _cleanup_codex_dispatch_home(
            dispatch_id,
            agent,
            detached=detached,
            home_resolved=(
                codex_dispatch_home_resolved
                and worker_still_alive is not True
            ),
            codex_session_id=codex_session_id,
        )


def _status_snapshot(payload: dict) -> dict:
    keys = (
        "schema",
        "dispatch_id",
        "agent",
        "shape",
        "effective_account",
        "codex_session_id",
        "engine_session_id",
        "codex_home",
        "codex_home_owner_dispatch_id",
        "parent_dispatch_id",
        "state",
        "reason",
        "worker_pid",
        "controller_alive",
        "pgid",
        "worker_alive",
        "worker_identity_reason",
        "pgroup_cpu_pct",
        "seconds_since_event",
        "liveness_state",
        "tail_path",
        "status_path",
        "trace_path",
        "trace_mtime",
        "trace_active",
        "terminal_marker",
        "last_marker",
        "updated_at",
    )
    return {key: payload.get(key) for key in keys if payload.get(key) not in (None, "", [], {})}


def _controller_dead_is_terminal(*, detached: bool) -> bool:
    if detached:
        return False
    return True


def _strip_marker_decoration(text: str) -> str:
    value = text.strip()
    while value.startswith("*") or value.startswith("`"):
        value = value[1:].lstrip()
    while value.endswith("*") or value.endswith("`"):
        value = value[:-1].rstrip()
    return value


def _completion_signoff_marker(stripped: str, line_no: int) -> dict | None:
    match = COMPLETION_SIGNOFF_RE.match(_strip_marker_decoration(stripped))
    if not match:
        return None
    return {"line": line_no, "kind": "COMPLETE", "text": ""}


def _is_diff_context_line(raw_line: str) -> bool:
    if raw_line.startswith((" ", "\t", "+")):
        return True
    return raw_line.startswith("-") and not raw_line.startswith("- ")


def _strip_terminal_marker_prefix(stripped: str) -> str:
    if stripped.startswith(("+", "-")):
        # Strip exactly one diff marker and at most its one separator space.
        # Keeping any further indentation makes the anchored marker regex fail.
        remainder = stripped[1:]
        return remainder[1:] if remainder.startswith(" ") else remainder
    return stripped


def _strip_kimi_terminal_marker_prefix(stripped: str) -> str:
    """Strip Kimi text renderer's first-line bullet; whitespace is stripped by callers."""
    if stripped.startswith("• "):
        return stripped[2:].lstrip()
    return stripped


def _fence_state_unbalanced(lines: list[str], ignored_lines: set[int]) -> bool:
    fence = goalflight_terminal.MarkdownFenceTracker()
    for idx, line in enumerate(lines):
        if idx in ignored_lines:
            continue
        fence.consume_boundary(line)
    return fence.in_fence


def _marker_survives_unbalanced_fence(marker: dict | None) -> bool:
    """Recover genuine success sign-offs without promoting fenced escalations."""
    return bool(marker and marker.get("kind") in SUCCESS_TERMINAL_MARKERS)


def _payload_binds_to_dispatch(
    marker: dict,
    expected: str,
    *,
    require_terminated: bool = False,
) -> bool:
    """True when the payload is this dispatch's id, optionally plus a summary."""
    embedded = str(marker.get("dispatch_id") or "").strip()
    if embedded:
        return embedded == expected
    text = _strip_marker_decoration(str(marker.get("text") or "")).strip()
    if text == expected:
        # A bounded oversized-line prefix that ends exactly at the expected id
        # has not proved the binding: the discarded byte may continue the id.
        return not require_terminated
    if not text.startswith(expected):
        return False
    suffix = text[len(expected) :]
    return bool(suffix and (suffix[0].isspace() or suffix[0] in ":;|\N{EM DASH}\N{EN DASH}"))


def _terminal_marker_matches_dispatch(
    marker: dict | None,
    expected_dispatch_id: str | None,
    *,
    require_terminated: bool = False,
) -> bool:
    """Bind scraped terminal evidence to the dispatch that owns the tail.

    Success markers (COMPLETE/READY/RESULT) must carry this dispatch's id.
    Attention markers (BLOCKED, USER-NEED, USER-CONFIRM, FAILED) bind without
    that prefix so a deliberate escalation is not dropped as a missing sign-off.
    Empty sign-offs remain usable only when the caller has no expected id.
    """
    expected = str(expected_dispatch_id or "").strip()
    if not expected:
        return marker is not None
    if not isinstance(marker, dict):
        return False
    if _payload_binds_to_dispatch(
        marker, expected, require_terminated=require_terminated
    ):
        return True
    return marker.get("kind") in goalflight_terminal.ATTENTION_MARKERS


def _final_terminal_marker_from_line(
    raw_line: str,
    line_no: int,
    *,
    allow_prefixed_marker: bool = False,
    allow_quote_prefix: bool = False,
    allow_status_prefix: bool = False,
    kimi_output: bool = False,
    expected_dispatch_id: str | None = None,
) -> dict | None:
    kimi_continuation = raw_line.startswith("  ") and not raw_line.startswith("   ")
    if raw_line.startswith("\t") or (
        raw_line.startswith(" ")
        and not (allow_prefixed_marker and kimi_output and kimi_continuation)
    ):
        return None
    if _is_diff_context_line(raw_line) and not allow_prefixed_marker:
        return None
    stripped = raw_line.strip()
    if not stripped:
        return None
    if allow_prefixed_marker and MARKER_VOCAB_BULLET_RE.match(stripped):
        return None
    if allow_prefixed_marker:
        if allow_quote_prefix and stripped.startswith("> "):
            stripped = stripped[2:].lstrip()
        else:
            stripped = _strip_terminal_marker_prefix(stripped)
        if kimi_output:
            stripped = _strip_kimi_terminal_marker_prefix(stripped)
        if not stripped:
            return None
    direct_match = MARKER_RE.match(stripped)
    if not allow_status_prefix and direct_match and direct_match.group(1) == "STATUS":
        # Progress ``STATUS:`` is not terminal. ``STATUS: BLOCKED:`` is the
        # allowlisted own-signal form; live last-line used to return here
        # before the shared predicate ran, so harvest/dead accepted a line
        # live ignored. Consult the allowlist instead of a second STATUS
        # rejector. ``STATUS: COMPLETE:`` stays live-rejected (SUCCESS still
        # needs allow_status_prefix, as before).
        own = goalflight_terminal.parse_own_signal_attention_line(
            raw_line, line_no, kimi_output=kimi_output
        )
        if own is None:
            return None
        return (
            own
            if _terminal_marker_matches_dispatch(own, expected_dispatch_id)
            else None
        )
    signoff = _completion_signoff_marker(stripped, line_no)
    if signoff:
        return (
            signoff
            if _terminal_marker_matches_dispatch(signoff, expected_dispatch_id)
            else None
        )
    match = FINAL_TERMINAL_MARKER_RE.match(stripped)
    if not match:
        return None
    if goalflight_terminal.marker_is_template_example(match.group(1), match.group(2)):
        return None
    marker = {
        "line": line_no,
        "kind": match.group(1),
        "text": _strip_marker_decoration(match.group(2))[:1000],
    }
    if marker["kind"] in goalflight_terminal.ATTENTION_MARKERS:
        # Prefix stripping above is for SUCCESS renderer forms (``+READY:``,
        # ``- COMPLETE:``, ``> COMPLETE:``). Attention uses the shared
        # allowlist on the *raw* line so a list-item or quote cannot become
        # an escalation by surviving one of those strippers.
        own = goalflight_terminal.parse_own_signal_attention_line(
            raw_line, line_no, kimi_output=kimi_output
        )
        if own is None:
            return None
        marker = own
    return marker if _terminal_marker_matches_dispatch(marker, expected_dispatch_id) else None


def _prompt_echo_anchor_indices(prompt_prefix: list[str]) -> list[int]:
    anchors: list[int] = []
    seen: set[str] = set()
    at_segment_start = True
    for idx, line in enumerate(prompt_prefix):
        if not line:
            at_segment_start = True
            continue
        if (
            at_segment_start
            and line not in seen
            and not BARE_TERMINAL_MARKER_RE.match(line)
        ):
            anchors.append(idx)
            seen.add(line)
            if len(anchors) >= PROMPT_ECHO_MAX_ANCHORS:
                break
        at_segment_start = False
    return anchors


def _prompt_echo_scan(lines: list[str], prompt_prefix: list[str]) -> tuple[set[int], bool, set[str]]:
    prompt_line_set = {line for line in prompt_prefix if line}
    anchor_indices = _prompt_echo_anchor_indices(prompt_prefix)
    if not anchor_indices:
        return set(), False, prompt_line_set

    anchor_limit = min(len(lines), PROMPT_ECHO_ANCHOR_SEARCH_LINES)
    matched_single_lines: list[int] = []
    matched_multi_lines: list[int] = []
    for idx in range(anchor_limit):
        tail_line = lines[idx].strip()
        for anchor_idx in anchor_indices:
            if tail_line != prompt_prefix[anchor_idx]:
                continue
            prompt_idx = anchor_idx
            line_idx = idx
            span: list[int] = []
            while line_idx < len(lines) and prompt_idx < len(prompt_prefix):
                if lines[line_idx].strip() != prompt_prefix[prompt_idx]:
                    break
                span.append(line_idx)
                line_idx += 1
                prompt_idx += 1
            if len(span) > 1:
                matched_multi_lines.extend(span)
            elif span:
                matched_single_lines.extend(span)
    if matched_multi_lines:
        # Multi-line sequential match = a real echo block; single-line
        # lookalikes are fenced but do NOT count as a located anchor, so the
        # fence-less verbatim-quote suppression stays armed (a one-line
        # coincidence must not unlock reconciliation trust elsewhere).
        return set(matched_multi_lines) | set(matched_single_lines), True, prompt_line_set
    if matched_single_lines:
        return set(matched_single_lines), False, prompt_line_set
    return set(), False, prompt_line_set


def _attention_marker_kind_in_text(text: str) -> str | None:
    """Return the last unfenced, unquoted attention-marker kind in *text*, if any.

    death_cause=no_evidence is invalid while one of these lines is in view.
    Leading whitespace is stripped, matching ``extract_markers``, so an
    indented attention line cannot leave ``last_marker.kind`` determinate
    while this helper claims the tail contains no evidence. Markdown quotes
    (``> ``) and fences still do not count: those are relayed content, not
    the worker's own signal. Indentation is evidence for death-cause only;
    the dead-path scan still refuses to terminalize an indented line.
    """
    kind = None
    fence = goalflight_terminal.MarkdownFenceTracker()
    for idx, raw in enumerate(text.splitlines(), start=1):
        if fence.consume_boundary(raw):
            continue
        if fence.in_fence:
            continue
        marker = _final_terminal_marker_from_line(raw.lstrip(" \t"), idx)
        if marker and marker.get("kind") in goalflight_terminal.ATTENTION_MARKERS:
            kind = str(marker["kind"])
    return kind


def _attention_kind_from_last_marker(last_marker: object) -> str | None:
    """Return an attention kind already recorded on *last_marker*, if any."""

    if not isinstance(last_marker, dict):
        return None
    kind = last_marker.get("kind")
    if kind in goalflight_terminal.ATTENTION_MARKERS:
        return str(kind)
    return None


def _worker_dead_no_marker_reason(
    path: Path,
    prompt_prefix: list[str] | None = None,
    *,
    prompt_provenance_available: bool = True,
    prompt_path: Path | None = None,
    prompt_signature: tuple[int, int, int] | None = None,
    last_marker: dict | None = None,
) -> str:
    """Add postmortem evidence without changing the worker-dead verdict."""

    if not prompt_provenance_available:
        return "worker_dead_no_terminal_marker:death_cause=no_evidence"
    effective_prompt_prefix = prompt_prefix or []
    if prompt_path is not None:
        prompt_snapshot = _read_prompt_exclusion_snapshot(prompt_path)
        if (
            prompt_snapshot is None
            or prompt_signature is None
            or prompt_snapshot[1] != prompt_signature
            or not any(line.strip() for line in prompt_snapshot[0])
        ):
            return "worker_dead_no_terminal_marker:death_cause=no_evidence"
        effective_prompt_prefix = prompt_snapshot[0]
    text = goalflight_terminal.read_tail_excerpt(
        path,
        goalflight_terminal.FINAL_RECONCILIATION_TAIL_BYTES,
    )
    lines = text.splitlines()
    prompt_echo_lines, _echo_anchor_found, _prompt_line_set = _prompt_echo_scan(
        lines,
        effective_prompt_prefix,
    )
    normalized_lines = [_normalize_death_cause_prompt_line(line) for line in lines]
    normalized_echo_lines, _normalized_anchor_found, _normalized_prompt_lines = (
        _prompt_echo_scan(normalized_lines, effective_prompt_prefix)
    )
    prompt_echo_lines |= normalized_echo_lines
    visible_text = "\n".join(
        line for index, line in enumerate(lines) if index not in prompt_echo_lines
    )[-goalflight_terminal.WORKER_DEATH_CAUSE_TAIL_BYTES :]
    cause = goalflight_terminal.classify_worker_death_text(visible_text)
    prompt_causes = goalflight_terminal.worker_death_causes_mentioned_in_text(
        "\n".join(effective_prompt_prefix)
    )
    if prompt_path is not None:
        final_prompt_snapshot = _read_prompt_exclusion_snapshot(prompt_path)
        if (
            final_prompt_snapshot is None
            or final_prompt_snapshot[1] != prompt_signature
            or not any(line.strip() for line in final_prompt_snapshot[0])
        ):
            cause = goalflight_terminal.WORKER_DEATH_CAUSE_NO_EVIDENCE
    if cause in prompt_causes:
        cause = goalflight_terminal.WORKER_DEATH_CAUSE_NO_EVIDENCE
    if cause == goalflight_terminal.WORKER_DEATH_CAUSE_NO_EVIDENCE:
        attention_kind = _attention_marker_kind_in_text(visible_text)
        if not attention_kind:
            attention_kind = _attention_kind_from_last_marker(last_marker)
        if attention_kind:
            cause = f"attention_marker:{attention_kind}"
    return f"worker_dead_no_terminal_marker:death_cause={cause}"


def _normalize_death_cause_prompt_line(line: str) -> str:
    """Remove one observed renderer prefix for prompt-echo comparison only."""

    stripped = line.strip()
    for prefix in ("> ", "+ ", "- ", "• "):
        if stripped.startswith(prefix):
            return stripped[len(prefix) :].lstrip()
    return stripped


def _is_unfenced_prompt_quoted_bare_marker(
    stripped: str,
    *,
    prompt_line_set: set[str],
    echo_anchor_found: bool,
    suppress_unfenced_prompt_markers: bool,
) -> bool:
    return bool(
        suppress_unfenced_prompt_markers
        and not echo_anchor_found
        and stripped in prompt_line_set
        and BARE_TERMINAL_MARKER_RE.match(stripped)
    )


def alive(pid: int | None) -> bool:
    if not pid:
        return False
    return goalflight_compat.pid_alive(pid)


def _identity_token(identity: dict | None) -> dict | None:
    if not identity:
        return None
    return {
        key: identity.get(key)
        for key in ("pid", "start_token", "lstart", "comm")
        if identity.get(key)
    }


def _load_identity(raw: str | None) -> dict | None:
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def worker_alive(pid: int | None, expected_identity: dict | None) -> tuple[bool, str, dict | None]:
    if not pid:
        return False, "no_pid", None
    current = goalflight_ledger.process_identity(pid)
    is_alive, reason = goalflight_ledger.compare_process_identities(
        int(pid), expected_identity, current
    )
    return is_alive, reason, current


class TailScanResult:
    __slots__ = (
        "markers",
        "mail_markers",
        "terminal",
        "size",
        "content_bytes",
        "validation_bytes",
        "lines_materialized",
        "resynced",
        "resync_reason",
        "fence_unbalanced",
        "terminal_observed_size",
    )

    def __init__(
        self,
        *,
        markers: list[dict],
        mail_markers: list[dict],
        terminal: dict | None,
        size: int,
        content_bytes: int,
        validation_bytes: int,
        lines_materialized: int,
        resynced: bool,
        resync_reason: str | None,
        fence_unbalanced: bool,
        terminal_observed_size: int | None = None,
    ) -> None:
        self.markers = markers
        self.mail_markers = mail_markers
        self.terminal = terminal
        self.size = size
        self.content_bytes = content_bytes
        self.validation_bytes = validation_bytes
        self.lines_materialized = lines_materialized
        self.resynced = resynced
        self.resync_reason = resync_reason
        self.fence_unbalanced = fence_unbalanced
        self.terminal_observed_size = (
            terminal_observed_size
            if terminal_observed_size is not None
            else (size if terminal is not None else None)
        )

    def metrics(self) -> dict:
        result = {
            "bytes_read": self.content_bytes + self.validation_bytes,
            "content_bytes": self.content_bytes,
            "validation_bytes": self.validation_bytes,
            "lines_materialized": self.lines_materialized,
            "offset": self.size,
            "resynced": self.resynced,
            "fence_unbalanced": self.fence_unbalanced,
        }
        if self.resync_reason:
            result["resync_reason"] = self.resync_reason
        return result


def _combine_tail_scan_results(first: TailScanResult, second: TailScanResult) -> TailScanResult:
    """Combine a normal poll scan with its terminal-stability recheck."""

    mail_markers: list[dict] = []
    seen: set[tuple[object, object, object]] = set()
    for marker in [*first.mail_markers, *second.mail_markers]:
        key = (marker.get("line"), marker.get("kind"), marker.get("text"))
        if key in seen:
            continue
        seen.add(key)
        mail_markers.append(marker)
    terminal_observed_size: int | None = None
    if second.terminal is not None:
        if first.terminal == second.terminal:
            # The winning candidate survived from scan one. Bytes that arrived
            # during the recheck are growth after that first observation.
            terminal_observed_size = first.terminal_observed_size
        else:
            # The recheck found a new winner, so its own offset is the baseline.
            terminal_observed_size = second.terminal_observed_size
    return TailScanResult(
        markers=second.markers,
        mail_markers=mail_markers,
        terminal=second.terminal,
        size=second.size,
        content_bytes=first.content_bytes + second.content_bytes,
        validation_bytes=first.validation_bytes + second.validation_bytes,
        lines_materialized=first.lines_materialized + second.lines_materialized,
        resynced=first.resynced or second.resynced,
        resync_reason=second.resync_reason or first.resync_reason,
        fence_unbalanced=second.fence_unbalanced,
        terminal_observed_size=terminal_observed_size,
    )


class _IncrementalMarkerState:
    """Marker/fence/last-line state derived from completed tail lines."""

    def __init__(self) -> None:
        self.fence = goalflight_terminal.MarkdownFenceTracker()
        self.all_markers: deque[dict] = deque(maxlen=20)
        self.outside_markers: deque[dict] = deque(maxlen=20)
        self.last_candidate: tuple[int, str, bool, int | None] | None = None
        self.previous_candidate = ""

    def clone(self) -> _IncrementalMarkerState:
        clone = _IncrementalMarkerState()
        clone.fence._delimiter = self.fence._delimiter
        clone.fence._minimum_length = self.fence._minimum_length
        clone.all_markers = deque((dict(marker) for marker in self.all_markers), maxlen=20)
        clone.outside_markers = deque(
            (dict(marker) for marker in self.outside_markers), maxlen=20
        )
        clone.last_candidate = self.last_candidate
        clone.previous_candidate = self.previous_candidate
        return clone

    def _consume_candidate(
        self,
        line_no: int,
        line: str,
        in_fence: bool,
        observed_offset: int | None,
    ) -> None:
        stripped = line.strip()
        if not stripped:
            return
        if HARNESS_TOKEN_COUNT_RE.fullmatch(stripped):
            if self.previous_candidate != "tokens used":
                self.last_candidate = (line_no, line, in_fence, observed_offset)
        elif stripped != "tokens used" and not _is_harness_trailer_line(stripped):
            self.last_candidate = (line_no, line, in_fence, observed_offset)
        self.previous_candidate = stripped

    def consume(
        self,
        line_no: int,
        line: str,
        *,
        ignored: bool = False,
        observed_offset: int | None = None,
    ) -> tuple[dict, bool] | None:
        if ignored:
            return None

        stripped = line.strip()
        fence_was_open = self.fence.in_fence
        fence_line = self.fence.consume_boundary(line)
        line_in_fence = fence_was_open or self.fence.in_fence
        self._consume_candidate(line_no, line, line_in_fence, observed_offset)
        if fence_line:
            return None

        marker: dict | None = None
        match = MARKER_RE.match(stripped)
        if match and not goalflight_terminal.marker_is_template_example(
            match.group(1), match.group(2)
        ):
            marker = {
                "line": line_no,
                "kind": match.group(1),
                "text": match.group(2)[:1000],
            }
        if marker is None:
            marker = _completion_signoff_marker(stripped, line_no)
        if marker is None:
            return None

        self.all_markers.append(marker)
        if not line_in_fence:
            self.outside_markers.append(marker)
        return marker, line_in_fence

    def visible_markers(self) -> list[dict]:
        if not self.fence.in_fence:
            return [dict(marker) for marker in self.outside_markers]
        outside = {
            (marker.get("line"), marker.get("kind"), marker.get("text"))
            for marker in self.outside_markers
        }
        return [
            dict(marker)
            for marker in self.all_markers
            if (
                (marker.get("line"), marker.get("kind"), marker.get("text"))
                in outside
                or _marker_survives_unbalanced_fence(marker)
            )
        ]

    def terminal(
        self,
        *,
        kimi_output: bool,
        expected_dispatch_id: str | None = None,
    ) -> dict | None:
        if self.last_candidate is None:
            return None
        line_no, line, candidate_in_fence, _observed_offset = self.last_candidate
        if candidate_in_fence and not self.fence.in_fence:
            return None
        marker = _final_terminal_marker_from_line(
            line,
            line_no,
            allow_prefixed_marker=True,
            kimi_output=kimi_output,
            expected_dispatch_id=expected_dispatch_id,
        )
        if candidate_in_fence and not _marker_survives_unbalanced_fence(marker):
            return None
        return marker

    def terminal_observed_offset(self) -> int | None:
        return self.last_candidate[3] if self.last_candidate is not None else None


class IncrementalTailScanner:
    """Scan only appended tail bytes while retaining marker context.

    The bounded prompt prefix is retained until echo classification can no
    longer change. Fence and last-nonempty-line state then advance one complete
    line at a time. An unfinished physical line stays as bytes and is previewed
    without committing it, so a marker split across polls is neither lost nor
    duplicated when its newline eventually arrives.
    """

    def __init__(
        self,
        path: Path,
        ignore_prefix_lines: list[str] | None = None,
        *,
        expected_dispatch_id: str | None = None,
    ) -> None:
        self.path = path
        self.prompt_prefix = ignore_prefix_lines or []
        self.expected_dispatch_id = expected_dispatch_id
        self._prompt_has_anchors = bool(_prompt_echo_anchor_indices(self.prompt_prefix))
        self._prompt_record_limit = PROMPT_ECHO_ANCHOR_SEARCH_LINES + len(self.prompt_prefix)
        self._identity: tuple[int, int] | None = None
        self._mtime_ns: int | None = None
        self._offset = 0
        self._boundary = b""
        self._partial = bytearray()
        self._accepted_bytes = 0
        self._next_line_no = 1
        self._prompt_stable = not self._prompt_has_anchors
        self._prompt_records: list[tuple[int, str, int]] = []
        self._state = _IncrementalMarkerState()

    def _reset_content_state(self) -> None:
        self._offset = 0
        self._boundary = b""
        self._partial = bytearray()
        self._accepted_bytes = 0
        self._next_line_no = 1
        self._prompt_stable = not self._prompt_has_anchors
        self._prompt_records = []
        self._state = _IncrementalMarkerState()

    def update_prompt_prefix(self, prompt_prefix: list[str]) -> None:
        """Replace prompt exclusions and replay the tail under the new window."""

        self.prompt_prefix = list(prompt_prefix)
        self._prompt_has_anchors = bool(
            _prompt_echo_anchor_indices(self.prompt_prefix)
        )
        self._prompt_record_limit = (
            PROMPT_ECHO_ANCHOR_SEARCH_LINES + len(self.prompt_prefix)
        )
        self._identity = None
        self._mtime_ns = None
        self._reset_content_state()

    def _parse_prompt_records(
        self, records: list[tuple[int, str, int]]
    ) -> tuple[_IncrementalMarkerState, list[tuple[dict, bool]]]:
        lines = [line for _line_no, line, _offset in records]
        prompt_echo_lines, _anchor_found, _prompt_line_set = _prompt_echo_scan(
            lines, self.prompt_prefix
        )
        state = _IncrementalMarkerState()
        found: list[tuple[dict, bool]] = []
        for index, (line_no, line, observed_offset) in enumerate(records):
            marker = state.consume(
                line_no,
                line,
                ignored=index in prompt_echo_lines,
                observed_offset=observed_offset,
            )
            if marker:
                found.append(marker)
        return state, found

    def _accept_completed_line(
        self,
        line: str,
        observed_offset: int,
        scan_markers: list[tuple[dict, bool]],
    ) -> None:
        record = (self._next_line_no, line, observed_offset)
        self._next_line_no += 1
        if not self._prompt_stable:
            self._prompt_records.append(record)
            if len(self._prompt_records) >= self._prompt_record_limit:
                self._state, rebuilt = self._parse_prompt_records(self._prompt_records)
                scan_markers[:] = rebuilt
                self._prompt_records = []
                self._prompt_stable = True
            return
        marker = self._state.consume(
            record[0], record[1], observed_offset=record[2]
        )
        if marker:
            scan_markers.append(marker)

    def _feed_bytes(
        self, chunk: bytes, scan_markers: list[tuple[dict, bool]]
    ) -> int:
        self._partial.extend(chunk)
        line_start = 0
        line_count = 0
        while True:
            newline = self._partial.find(b"\n", line_start)
            if newline < 0:
                break
            raw_line = bytes(self._partial[line_start:newline])
            if raw_line.endswith(b"\r"):
                raw_line = raw_line[:-1]
            self._accept_completed_line(
                raw_line.decode("utf-8", errors="replace"),
                self._accepted_bytes + newline + 1,
                scan_markers,
            )
            line_start = newline + 1
            line_count += 1
        if line_start:
            del self._partial[:line_start]
            self._accepted_bytes += line_start
        return line_count

    def _preview(
        self, scan_markers: list[tuple[dict, bool]]
    ) -> tuple[_IncrementalMarkerState, list[tuple[dict, bool]], int]:
        if not self._prompt_stable:
            records = list(self._prompt_records)
            materialized = 0
            if self._partial:
                records.append(
                    (
                        self._next_line_no,
                        self._partial.decode("utf-8", errors="replace"),
                        self._accepted_bytes + len(self._partial),
                    )
                )
                materialized = 1
            state, found = self._parse_prompt_records(records)
            return state, found, materialized

        state = self._state.clone()
        found = list(scan_markers)
        materialized = 0
        if self._partial:
            marker = state.consume(
                self._next_line_no,
                self._partial.decode("utf-8", errors="replace"),
                observed_offset=self._accepted_bytes + len(self._partial),
            )
            if marker:
                found.append(marker)
            materialized = 1
        return state, found, materialized

    def scan(self, *, kimi_output: bool = False) -> TailScanResult:
        content_bytes = 0
        validation_bytes = 0
        lines_materialized = 0
        resync_reason: str | None = None
        scan_markers: list[tuple[dict, bool]] = []

        try:
            handle = self.path.open("rb")
        except OSError:
            if self._identity is not None:
                self._identity = None
                self._mtime_ns = None
                self._reset_content_state()
                resync_reason = "missing"
            state, found, preview_lines = self._preview(scan_markers)
            lines_materialized += preview_lines
            return self._result(
                state,
                found,
                size=0,
                content_bytes=content_bytes,
                validation_bytes=validation_bytes,
                lines_materialized=lines_materialized,
                resync_reason=resync_reason,
                kimi_output=kimi_output,
            )

        with handle:
            opened = os.fstat(handle.fileno())
            identity = (opened.st_dev, opened.st_ino)
            if self._identity is None:
                resync_reason = "initial"
            elif identity != self._identity:
                resync_reason = "replacement"
            elif opened.st_size < self._offset:
                resync_reason = "truncated"
            elif (
                self._offset
                and self._boundary
                and opened.st_mtime_ns != self._mtime_ns
            ):
                handle.seek(self._offset - len(self._boundary))
                observed = handle.read(len(self._boundary))
                validation_bytes += len(observed)
                if observed != self._boundary:
                    resync_reason = "boundary_rewritten"

            if resync_reason is not None:
                self._reset_content_state()
            self._identity = identity
            handle.seek(self._offset)
            while True:
                chunk = handle.read(TAIL_SCAN_CHUNK_BYTES)
                if not chunk:
                    break
                content_bytes += len(chunk)
                lines_materialized += self._feed_bytes(chunk, scan_markers)
                self._boundary = (self._boundary + chunk)[-TAIL_SCAN_BOUNDARY_BYTES:]
            self._offset = handle.tell()
            final_stat = os.fstat(handle.fileno())
            self._mtime_ns = final_stat.st_mtime_ns

        if not self._prompt_stable:
            self._state, rebuilt = self._parse_prompt_records(self._prompt_records)
            scan_markers = rebuilt
        state, found, preview_lines = self._preview(scan_markers)
        lines_materialized += preview_lines
        return self._result(
            state,
            found,
            size=self._offset,
            content_bytes=content_bytes,
            validation_bytes=validation_bytes,
            lines_materialized=lines_materialized,
            resync_reason=resync_reason,
            kimi_output=kimi_output,
        )

    def _result(
        self,
        state: _IncrementalMarkerState,
        found: list[tuple[dict, bool]],
        *,
        size: int,
        content_bytes: int,
        validation_bytes: int,
        lines_materialized: int,
        resync_reason: str | None,
        kimi_output: bool,
    ) -> TailScanResult:
        visible = state.visible_markers()
        mail_candidates = [
            marker
            for marker, marker_in_fence in found
            if (
                not marker_in_fence
                or (
                    state.fence.in_fence
                    and _marker_survives_unbalanced_fence(marker)
                )
            )
        ]
        mail_markers: list[dict] = []
        seen: set[tuple[object, object, object]] = set()
        for marker in [*visible, *mail_candidates]:
            key = (marker.get("line"), marker.get("kind"), marker.get("text"))
            if key in seen:
                continue
            seen.add(key)
            mail_markers.append(dict(marker))
        terminal = state.terminal(
            kimi_output=kimi_output,
            expected_dispatch_id=self.expected_dispatch_id,
        )
        return TailScanResult(
            markers=visible,
            mail_markers=mail_markers,
            terminal=terminal,
            size=size,
            content_bytes=content_bytes,
            validation_bytes=validation_bytes,
            lines_materialized=lines_materialized,
            resynced=resync_reason is not None,
            resync_reason=resync_reason,
            fence_unbalanced=state.fence.in_fence,
            terminal_observed_size=(
                state.terminal_observed_offset() if terminal is not None else None
            ),
        )


def extract_markers(path: Path, max_bytes: int = 10 * 1024 * 1024,
                    ignore_prefix_lines: list[str] | None = None) -> tuple[list[dict], int]:
    if not path.exists():
        return [], 0
    size = path.stat().st_size
    start = max(0, size - max_bytes)
    prompt_prefix = ignore_prefix_lines or []
    markers: list[dict] = []
    fence = goalflight_terminal.MarkdownFenceTracker()
    with path.open("rb") as f:
        f.seek(start)
        text = f.read().decode(errors="replace")
    lines = text.splitlines()
    prompt_echo_lines, _echo_anchor_found, _prompt_line_set = _prompt_echo_scan(lines, prompt_prefix)
    ignore_fences = _fence_state_unbalanced(lines, prompt_echo_lines)
    for idx, line in enumerate(lines, start=1):
        stripped = line.strip()
        # Skip only the initial echoed-prompt span. If the worker later emits a
        # byte-identical real terminal marker, it must still wake the orchestrator.
        if idx - 1 in prompt_echo_lines:
            continue
        # Fence skip (hardening): do not match markers inside ``` or ~~~ blocks.
        # Worker output containing example marker vocab in code fences must not inject terminals.
        if fence.consume_boundary(line):
            continue
        marker_in_fence = fence.in_fence
        if marker_in_fence and not ignore_fences:
            continue
        match = MARKER_RE.match(stripped)
        if match:
            if goalflight_terminal.marker_is_template_example(match.group(1), match.group(2)):
                continue
            marker = {"line": idx, "kind": match.group(1), "text": match.group(2)[:1000]}
            if marker_in_fence and not _marker_survives_unbalanced_fence(marker):
                continue
            markers.append(marker)
            continue
        # Bare sign-off ("Done.", "complete", "FINISHED!") carries no payload and
        # is indistinguishable from ordinary output -- notably the `done`
        # terminator of a shell loop a worker echoes into its tail.
        #
        # It is deliberately NOT filtered here. This list feeds the mail bridge
        # and status; the terminal DECISION for a live worker goes through
        # _last_line_is_terminal_marker (position-disciplined), and for a dead
        # worker through final reconciliation, which must still honour a sign-off
        # followed by a trailing summary (D022). Filtering by position here would
        # break that case. Consumers that turn markers into a terminal verdict
        # must cross-check liveness -- see goalflight_status wait verdicts.
        signoff = _completion_signoff_marker(stripped, idx)
        if signoff and (
            not marker_in_fence or _marker_survives_unbalanced_fence(signoff)
        ):
            markers.append(signoff)
    return markers, size


# Worker markers the watcher bridges into the controller's mail inbox: a worker
# blocking on one of these is "you have mail" the controller should see on its next
# status check. (They are also terminal markers — the worker stops and waits — so
# each is normally emitted once.)
WORKER_MAIL_MARKER_KINDS = frozenset({"USER-NEED", "USER-CONFIRM", "BLOCKED"})
# Headline kinds whose text becomes the terminal outbox line even when the
# live verdict was idle_timeout (or another liveness miss). COMPLETE belongs
# here: a finished worker whose watcher called idle first still delivered work.
HEADLINE_MAIL_MARKER_KINDS = (
    SUCCESS_TERMINAL_MARKERS | BLOCKING_TERMINAL_MARKERS | WORKER_MAIL_MARKER_KINDS
)

# Sentinel parked in the dedup set after any mail-layer failure: the bridge then
# no-ops for the rest of the watcher run. A real (type, text) key can never equal
# it (no message type is the empty marker below), so it cannot collide.


def _last_line_is_terminal_marker(
    path: Path,
    ignore_prefix_lines: list[str] | None = None,
    *,
    kimi_output: bool = False,
    expected_dispatch_id: str | None = None,
) -> dict | None:
    """Return a terminal marker dict iff the *last non-empty line* of the tail
    (after prefix-echo ignore and skipping inside code fences) matches a
    terminal marker kind. This is the only trustworthy position; mid-output
    marker lines (from prints, cats, logs, or fenced examples) are ignored.
    """
    if not path.exists():
        return None
    size = path.stat().st_size
    start = max(0, size - 10 * 1024 * 1024)
    prompt_prefix = ignore_prefix_lines or []
    fence = goalflight_terminal.MarkdownFenceTracker()
    candidates: list[tuple[int, str, bool]] = []
    with path.open("rb") as f:
        f.seek(start)
        text = f.read().decode(errors="replace")
    lines = text.splitlines()
    prompt_echo_lines, _echo_anchor_found, _prompt_line_set = _prompt_echo_scan(lines, prompt_prefix)
    ignore_fences = _fence_state_unbalanced(lines, prompt_echo_lines)
    for idx, line in enumerate(lines, start=1):
        stripped = line.strip()
        if idx - 1 in prompt_echo_lines:
            # prompt echo line (even if looks like marker): do not use for last_nonempty terminal decision
            continue
        fence_was_open = fence.in_fence
        fence_line = fence.consume_boundary(line)
        if fence_line:
            if stripped:
                candidates.append((idx, line, (fence_was_open or fence.in_fence) and not ignore_fences))
            continue
        if fence.in_fence and not ignore_fences:
            if stripped:
                candidates.append((idx, line, True))
            continue
        if stripped:
            # Preserve leading whitespace until the agent-specific check below.
            # Stripping here lets indented examples false-complete live workers.
            candidates.append((idx, line, fence.in_fence))
    if not candidates:
        return None

    # Strip trailers the AGENT CLI appends after the worker's own final line.
    # A loop rather than a fixed order: a run can end with a usage footer, hook
    # lines, a resume hint, or several of them, and the previous ordered chain
    # only tolerated the combinations it happened to list. Anything it does not
    # recognise stops the loop, so unknown output still blocks the marker --
    # failing closed, as before.
    candidate_idx = len(candidates) - 1
    while candidate_idx >= 0:
        trailing = candidates[candidate_idx][1].strip()
        if HARNESS_TOKEN_COUNT_RE.fullmatch(trailing):
            # A bare number counts as a trailer only directly under "tokens
            # used"; otherwise it is worker output and must not be skipped.
            if candidate_idx == 0 or candidates[candidate_idx - 1][1].strip() != "tokens used":
                return None
            candidate_idx -= 2
            continue
        if trailing == "tokens used" or _is_harness_trailer_line(trailing):
            candidate_idx -= 1
            continue
        break
    if candidate_idx < 0:
        return None

    line_no, candidate_line, candidate_in_fence = candidates[candidate_idx]
    marker = _final_terminal_marker_from_line(
        candidate_line,
        line_no,
        allow_prefixed_marker=True,
        kimi_output=kimi_output,
        expected_dispatch_id=expected_dispatch_id,
    )
    if candidate_in_fence and not (
        ignore_fences and _marker_survives_unbalanced_fence(marker)
    ):
        return None
    return marker


def _is_headline_kind(marker: object) -> bool:
    return isinstance(marker, dict) and marker.get("kind") in HEADLINE_MAIL_MARKER_KINDS


def _is_success_headline_kind(marker: object) -> bool:
    return isinstance(marker, dict) and marker.get("kind") in SUCCESS_TERMINAL_MARKERS


def harvest_headline_marker(
    payload: dict,
    tail: Path,
    *,
    ignore_prefix_lines: list[str] | None = None,
    kimi_output: bool = False,
    expected_dispatch_id: str | None = None,
) -> dict | None:
    """Return COMPLETE/BLOCKED/… even when the live verdict skipped harvesting.

    Live detection is last-line-only and idle_timeout used to stop without a
    final scan, so a worker that signed off and then went quiet arrived with
    ``last_marker is None``. Terminal writes rescan the tail; the verdict is
    not rewritten.

    Attention headlines use the same own-signal predicate as the dead-path
    verdict (``parse_own_signal_attention_line`` inside ``_final_terminal_marker``).
    ``extract_markers`` / payload ``markers`` still list mid-tail BLOCKED as
    diagnostic vocabulary; they must not become the harvest/outbox headline.

    Remaining extract_markers consumers that still answer "was attention
    vocabulary seen?" rather than "did this worker escalate?": ACP
    early-cancel and IncrementalTailScanner diagnostic ``markers``.
    Death-cause (``_attention_marker_kind_in_text``) now goes through
    ``_final_terminal_marker_from_line`` and therefore the allowlist; it
    is evidence, not a harvest headline. Those leftover scrapes are not
    harvest/outbox.
    """
    harvested = _final_terminal_marker(
        tail,
        ignore_prefix_lines=ignore_prefix_lines,
        kimi_output=kimi_output,
        expected_dispatch_id=expected_dispatch_id,
        full_file_fallback=True,
    )
    if _is_headline_kind(harvested):
        return harvested
    # Success-only fallbacks: a completed worker's COMPLETE/READY/RESULT may
    # sit behind extra summary. Attention is never taken from extract_markers.
    for candidate in (payload.get("terminal_marker"), payload.get("last_marker")):
        if _is_success_headline_kind(candidate):
            return candidate  # type: ignore[return-value]
    for marker in reversed(list(payload.get("markers") or [])):
        if _is_success_headline_kind(marker):
            return marker
    markers, _size = extract_markers(tail, ignore_prefix_lines=ignore_prefix_lines)
    for marker in reversed(markers):
        if _is_success_headline_kind(marker):
            return marker
    return None


def _recorded_terminal_success_marker(
    payload: dict,
    *,
    expected_dispatch_id: str | None = None,
) -> dict | None:
    """Return a schema-valid success marker already recorded by the watcher."""
    terminal_marker = payload.get("terminal_marker")
    if isinstance(terminal_marker, dict):
        candidates = (terminal_marker,)
    else:
        candidates = (payload.get("last_marker"),)
    for marker in candidates:
        if not isinstance(marker, dict) or marker.get("kind") not in SUCCESS_TERMINAL_MARKERS:
            continue
        try:
            line_no = int(marker.get("line") or 0)
        except (TypeError, ValueError):
            continue
        if line_no > 0 and _terminal_marker_matches_dispatch(
            marker, expected_dispatch_id
        ):
            return marker
    return None


def _advance_last_own_output_line(
    stripped: str,
    line_no: int,
    *,
    previous_stripped: str,
    last_output_line_no: int | None,
) -> tuple[int | None, str]:
    """Track the last non-empty worker line, skipping known harness trailers.

    Empty lines, ``hook: Stop`` / resume footers, and a ``tokens used`` count
    pair are not the worker keeping going. Any other non-empty line is.
    """
    if not stripped:
        return last_output_line_no, previous_stripped
    if HARNESS_TOKEN_COUNT_RE.fullmatch(stripped):
        if previous_stripped != "tokens used":
            last_output_line_no = line_no
    elif stripped != "tokens used" and not _is_harness_trailer_line(stripped):
        last_output_line_no = line_no
    return last_output_line_no, stripped


def _dead_path_terminal_from_line(
    line: str,
    line_no: int,
    *,
    kimi_output: bool,
    expected_dispatch_id: str | None,
) -> tuple[dict | None, bool]:
    """Parse one dead-path candidate.

    Success markers (COMPLETE/READY/RESULT) may be quote-prefixed: a dead
    worker that signed off with a renderer or markdown quote has still
    finished. Attention uses the shared own-signal allowlist
    (``parse_own_signal_attention_line``) so ``> BLOCKED:``, ``- BLOCKED:``,
    and other relay forms cannot count as this worker's own escalation.

    Position (last own line, unfenced, not in a hunk) is the caller's job.
    """
    candidate = _final_terminal_marker_from_line(
        line,
        line_no,
        allow_prefixed_marker=True,
        allow_quote_prefix=True,
        allow_status_prefix=True,
        kimi_output=kimi_output,
        expected_dispatch_id=expected_dispatch_id,
    )
    if not candidate:
        return None, False
    return candidate, candidate.get("kind") in goalflight_terminal.ATTENTION_MARKERS


def _select_dead_path_terminal(
    last_success: dict | None,
    last_attention: dict | None,
    last_output_line_no: int | None,
) -> dict | None:
    """Dead-path terminal: success may be anywhere; attention only as last own line.

    An attention marker terminalizes only as the worker's own terminal
    signal: the last non-empty non-trailer line, already parsed by the
    shared own-signal allowlist, and not fenced (the caller skipped fences
    for the candidate). Quoted / list-item / fenced / indented / mid-tail
    attention does not terminalize.

    Success markers still win from anywhere in the completed tail, including
    quote-prefixed forms, because a dead worker that signed off and then had
    a summary has finished. A later own-signal attention line outranks an
    earlier success (the worker stopped to escalate after previously signing
    off).
    """
    if (
        last_attention
        and last_output_line_no is not None
        and last_attention.get("line") == last_output_line_no
    ):
        return last_attention
    return last_success


def _scan_final_terminal_marker(
    lines: list[str],
    *,
    prompt_echo_lines: set[int],
    echo_anchor_found: bool,
    prompt_line_set: set[str],
    suppress_unfenced_prompt_markers: bool,
    ignore_fences: bool,
    kimi_output: bool = False,
    expected_dispatch_id: str | None = None,
) -> dict | None:
    fence = goalflight_terminal.MarkdownFenceTracker()
    in_hunk = False
    last_success: dict | None = None
    last_attention: dict | None = None
    last_output_line_no: int | None = None
    previous_stripped = ""
    for idx, line in enumerate(lines, start=1):
        stripped = line.strip()
        if idx - 1 in prompt_echo_lines:
            continue
        last_output_line_no, previous_stripped = _advance_last_own_output_line(
            stripped,
            idx,
            previous_stripped=previous_stripped,
            last_output_line_no=last_output_line_no,
        )
        fence_was_open = fence.in_fence
        if fence.consume_boundary(line):
            continue
        marker_in_fence = fence_was_open or fence.in_fence
        if marker_in_fence and not ignore_fences:
            continue
        if in_hunk:
            if line.startswith((" ", "+", "-", "\\")):
                continue
            in_hunk = False
        if HUNK_HEADER_RE.match(line):
            in_hunk = True
            continue
        candidate, is_attention = _dead_path_terminal_from_line(
            line,
            idx,
            kimi_output=kimi_output,
            expected_dispatch_id=expected_dispatch_id,
        )
        if candidate:
            if marker_in_fence and not _marker_survives_unbalanced_fence(candidate):
                continue
            prompt_candidate = stripped
            if kimi_output:
                # Compare Kimi's renderer-normalized marker to normalized prompt
                # lines; otherwise a bullet-prefixed prompt echo evades poison
                # suppression during worker-dead reconciliation.
                prompt_candidate = _strip_kimi_terminal_marker_prefix(prompt_candidate)
            if _is_unfenced_prompt_quoted_bare_marker(
                prompt_candidate,
                prompt_line_set=prompt_line_set,
                echo_anchor_found=echo_anchor_found,
                suppress_unfenced_prompt_markers=suppress_unfenced_prompt_markers,
            ):
                continue
            if is_attention:
                last_attention = candidate
            else:
                last_success = candidate
    return _select_dead_path_terminal(
        last_success, last_attention, last_output_line_no
    )


def _iter_bounded_text_lines(handle, max_chars: int = STREAM_READ_CHUNK_CHARS):
    """Yield one capped prefix per physical line without unbounded readline.

    The boolean result marks a physical line whose remainder was discarded.
    Such a line can still update fence/diff state from its bounded prefix. A
    dispatch-bound marker is also decided from that prefix because marker kind
    and dispatch id are prefix-positioned; other oversized lines are rejected.
    """

    line_no = 0
    while True:
        prefix = handle.readline(max_chars)
        if not prefix:
            return
        line_no += 1
        oversized = False
        chunk = prefix
        while not chunk.endswith("\n"):
            chunk = handle.readline(max_chars)
            if not chunk:
                break
            oversized = True
        yield line_no, prefix.rstrip("\r\n"), oversized


def _stream_final_terminal_marker(
    path: Path,
    *,
    prompt_prefix: list[str],
    suppress_unfenced_prompt_markers: bool,
    ignore_fences: bool,
    kimi_output: bool,
    expected_dispatch_id: str,
) -> tuple[dict | None, bool]:
    """Scan a completed tail from disk without materializing the whole file."""

    prompt_buffer_lines = PROMPT_ECHO_ANCHOR_SEARCH_LINES + len(prompt_prefix)
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        line_iter = iter(_iter_bounded_text_lines(handle))
        leading_records: list[tuple[int, str, bool]] = []
        for _ in range(prompt_buffer_lines):
            try:
                record = next(line_iter)
            except StopIteration:
                break
            leading_records.append(record)
        leading = [line for _line_no, line, _oversized in leading_records]
        prompt_echo_lines, echo_anchor_found, prompt_line_set = _prompt_echo_scan(
            leading,
            prompt_prefix,
        )

        fence = goalflight_terminal.MarkdownFenceTracker()
        in_hunk = False
        last_success: dict | None = None
        last_attention: dict | None = None
        last_output_line_no: int | None = None
        previous_stripped = ""

        def consume(line: str, line_no: int, *, oversized: bool) -> None:
            nonlocal in_hunk, last_success, last_attention
            nonlocal last_output_line_no, previous_stripped
            stripped = line.strip()
            if line_no - 1 in prompt_echo_lines:
                return
            last_output_line_no, previous_stripped = _advance_last_own_output_line(
                stripped,
                line_no,
                previous_stripped=previous_stripped,
                last_output_line_no=last_output_line_no,
            )
            fence_was_open = fence.in_fence
            if fence.consume_boundary(line):
                return
            marker_in_fence = fence_was_open or fence.in_fence
            if marker_in_fence and not ignore_fences:
                return
            if in_hunk:
                if line.startswith((" ", "+", "-", "\\")):
                    return
                in_hunk = False
            if HUNK_HEADER_RE.match(line):
                in_hunk = True
                return
            candidate, is_attention = _dead_path_terminal_from_line(
                line,
                line_no,
                kimi_output=kimi_output,
                expected_dispatch_id=expected_dispatch_id,
            )
            # An oversized prefix is trustworthy only when the dispatch-id
            # match is terminated by the normal separator grammar before the
            # cut. A prefix ending exactly at the id could continue with a
            # foreign suffix in discarded bytes and therefore never binds.
            if oversized and not _terminal_marker_matches_dispatch(
                candidate,
                expected_dispatch_id,
                require_terminated=True,
            ):
                return
            if not candidate:
                return
            if marker_in_fence and not _marker_survives_unbalanced_fence(candidate):
                return
            prompt_candidate = stripped
            if kimi_output:
                prompt_candidate = _strip_kimi_terminal_marker_prefix(prompt_candidate)
            if _is_unfenced_prompt_quoted_bare_marker(
                prompt_candidate,
                prompt_line_set=prompt_line_set,
                echo_anchor_found=echo_anchor_found,
                suppress_unfenced_prompt_markers=suppress_unfenced_prompt_markers,
            ):
                return
            if is_attention:
                last_attention = candidate
            else:
                last_success = candidate

        for line_no, line, oversized in leading_records:
            consume(line, line_no, oversized=oversized)
        for line_no, line, oversized in line_iter:
            consume(line, line_no, oversized=oversized)
    return (
        _select_dead_path_terminal(last_success, last_attention, last_output_line_no),
        fence.in_fence,
    )


def _full_file_terminal_marker(
    path: Path,
    *,
    prompt_prefix: list[str],
    suppress_unfenced_prompt_markers: bool,
    kimi_output: bool,
    expected_dispatch_id: str,
) -> dict | None:
    terminal, fence_unbalanced = _stream_final_terminal_marker(
        path,
        prompt_prefix=prompt_prefix,
        suppress_unfenced_prompt_markers=suppress_unfenced_prompt_markers,
        ignore_fences=False,
        kimi_output=kimi_output,
        expected_dispatch_id=expected_dispatch_id,
    )
    if not fence_unbalanced:
        return terminal
    fence_agnostic, _still_unbalanced = _stream_final_terminal_marker(
        path,
        prompt_prefix=prompt_prefix,
        suppress_unfenced_prompt_markers=suppress_unfenced_prompt_markers,
        ignore_fences=True,
        kimi_output=kimi_output,
        expected_dispatch_id=expected_dispatch_id,
    )
    if fence_agnostic and (
        not terminal or fence_agnostic.get("line", -1) >= terminal.get("line", -1)
    ):
        return fence_agnostic
    return terminal


def _final_terminal_marker(
    path: Path,
    ignore_prefix_lines: list[str] | None = None,
    *,
    suppress_unfenced_prompt_markers: bool = True,
    kimi_output: bool = False,
    expected_dispatch_id: str | None = None,
    full_file_fallback: bool = False,
) -> dict | None:
    """Return the terminal marker from a completed post-prompt tail.

    Live detection remains last-line-only. This reconciliation scan is for the
    worker-dead path, after no more output can arrive.

    Success markers (COMPLETE/READY/RESULT) may appear anywhere in the
    completed tail, including quote-prefixed renderer forms. Attention
    markers (BLOCKED/USER-NEED/USER-CONFIRM/FAILED) terminalize only as the
    worker's own final signal: last non-empty non-trailer line matching
    ``parse_own_signal_attention_line``, unfenced. Quoted, list-item, fenced,
    indented, or mid-tail attention is relayed or abandoned content and
    does not stop the dispatch.
    """
    if not path.exists():
        return None
    size = path.stat().st_size
    start = max(0, size - 10 * 1024 * 1024)
    prompt_prefix = ignore_prefix_lines or []
    with path.open("rb") as f:
        f.seek(start)
        text = f.read().decode(errors="replace")
    lines = text.splitlines()
    prompt_echo_lines, echo_anchor_found, prompt_line_set = _prompt_echo_scan(lines, prompt_prefix)
    terminal = _scan_final_terminal_marker(
        lines,
        prompt_echo_lines=prompt_echo_lines,
        echo_anchor_found=echo_anchor_found,
        prompt_line_set=prompt_line_set,
        suppress_unfenced_prompt_markers=suppress_unfenced_prompt_markers,
        ignore_fences=False,
        kimi_output=kimi_output,
        expected_dispatch_id=expected_dispatch_id,
    )
    if _fence_state_unbalanced(lines, prompt_echo_lines):
        fence_agnostic_terminal = _scan_final_terminal_marker(
            lines,
            prompt_echo_lines=prompt_echo_lines,
            echo_anchor_found=echo_anchor_found,
            prompt_line_set=prompt_line_set,
            suppress_unfenced_prompt_markers=suppress_unfenced_prompt_markers,
            ignore_fences=True,
            kimi_output=kimi_output,
            expected_dispatch_id=expected_dispatch_id,
        )
        if fence_agnostic_terminal and (
            not terminal
            or fence_agnostic_terminal.get("line", -1) >= terminal.get("line", -1)
        ):
            terminal = fence_agnostic_terminal
    if terminal:
        return terminal
    if full_file_fallback and start > 0 and expected_dispatch_id:
        # The bounded live scan is intentionally cheap. Once worker identity is
        # dead, output is immutable, so one streamed whole-file pass is safe and
        # prevents >10 MiB of post-marker logs from erasing terminal evidence.
        return _full_file_terminal_marker(
            path,
            prompt_prefix=prompt_prefix,
            suppress_unfenced_prompt_markers=suppress_unfenced_prompt_markers,
            kimi_output=kimi_output,
            expected_dispatch_id=expected_dispatch_id,
        )
    return None


def _terminal_marker_from_ignored_prompt(
    path: Path,
    marker: object,
    ignore_prefix_lines: list[str] | None,
) -> bool:
    if not isinstance(marker, dict) or not ignore_prefix_lines:
        return False
    try:
        line_no = int(marker.get("line") or 0)
    except (TypeError, ValueError):
        return False
    if line_no <= 0 or not path.exists():
        return False
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return False
    prompt_echo_lines, _echo_anchor_found, prompt_line_set = _prompt_echo_scan(lines, ignore_prefix_lines)
    line_idx = line_no - 1
    if line_idx in prompt_echo_lines:
        return True
    if 0 <= line_idx < len(lines) and lines[line_idx].strip() in prompt_line_set:
        return True
    return False


def _prompt_file_signature(stat_result: os.stat_result) -> tuple[int, int, int]:
    return (stat_result.st_mtime_ns, stat_result.st_size, stat_result.st_ino)


def _read_prompt_exclusion_snapshot(
    path: Path,
) -> tuple[list[str], tuple[int, int, int]] | None:
    """Read exclusions and replacement signature from one opened sidecar."""

    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            lines = [line.strip() for line in handle.read().splitlines()]
            opened = os.fstat(handle.fileno())
    except OSError:
        return None
    return lines, _prompt_file_signature(opened)


def _prompt_reload_due(
    current_signature: tuple[int, int, int] | None,
    loaded_signature: tuple[int, int, int] | None,
    *,
    last_reload_at: float | None,
    now: float,
    poll_secs: float,
) -> bool:
    if current_signature is None or current_signature == loaded_signature:
        return False
    interval = max(0.0, poll_secs)
    return last_reload_at is None or now - last_reload_at >= interval - 1e-9


def _prompt_provenance_matches_loaded_snapshot(
    current_signature: tuple[int, int, int] | None,
    loaded_signature: tuple[int, int, int] | None,
    snapshot_available: bool,
) -> bool:
    """Trust prompt exclusions only while they describe the current sidecar."""

    return bool(
        snapshot_available
        and current_signature is not None
        and current_signature == loaded_signature
    )


def _discarded_terminal_candidate_matches(
    evidence: dict | None,
    marker: dict | None,
    observed_offset: int | None,
) -> bool:
    if not evidence or not marker or observed_offset is None:
        return False
    try:
        vetoed_offset = int(evidence.get("offset"))
    except (TypeError, ValueError):
        return False
    return evidence.get("marker") == marker and vetoed_offset == observed_offset


def _dispatch_record_is_nonterminal(dispatch_id: str) -> bool:
    try:
        path = goalflight_ledger.record_path(dispatch_id, create=False)
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return False
    state = record.get("state")
    terminal = record.get("terminal_state") or goalflight_ledger.terminal_state_for(
        state,
        record.get("reason") or record.get("error"),
    )
    return terminal == "unknown" and not goalflight_dispatch_states.is_terminal_state(
        state
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="goal-flight compact log watcher")
    parser.add_argument("--pid", type=int, required=True)
    parser.add_argument("--tail", required=True)
    parser.add_argument("--status-json", required=True)
    parser.add_argument(
        "--dispatch-id",
        required=True,
        help="Expected dispatch identity; terminal evidence for any other id is ignored.",
    )
    parser.add_argument("--project-root")
    parser.add_argument(
        "--worker-cwd",
        help=(
            "Dispatch working directory (effective -C). The stall tree-leg "
            "scans this when it is distinct from --project-root; a root-rooted "
            "cwd is indeterminate."
        ),
    )
    parser.add_argument("--task-ids")
    parser.add_argument("--agent", default="unknown")
    parser.add_argument("--poll-secs", type=float, default=2.0)
    parser.add_argument(
        "--max-idle-secs",
        type=float,
        default=900.0,
        help=(
            "Backstop for a wedged worker, NOT a liveness test. Reaching it does "
            "not kill the worker: this records the verdict and exits while the "
            "process keeps running, so a short value abandons live workers rather "
            "than ending them. Controllers wake on delivered events, so a generous "
            "value costs no latency."
        ),
    )
    parser.add_argument(
        "--wedge-idle-secs",
        type=float,
        default=DEFAULT_WEDGE_IDLE_SECS,
        help=(
            "Sustained tail mtime + worker-tree silence + flat cumulative CPU "
            "before flagging worker_stalled_candidate. Detection only: never "
            "kills the worker and is not a verdict (remote-wait workers match "
            "this signature while healthy). 5 minutes was inside grok burst-gap "
            "range; default is 900s. 0 disables."
        ),
    )
    parser.add_argument("--cpu-epsilon", type=float, default=0.1)
    parser.add_argument(
        "--trace-long-running-secs",
        type=float,
        default=TRACE_LONG_RUNNING_SECS,
        help="Active-trace runtime before a non-terminal long_running notice.",
    )
    parser.add_argument(
        "--trace-review-secs",
        type=float,
        default=TRACE_REVIEW_SECS,
        help="Active-trace absolute runtime before long_running_review (never auto-kills).",
    )
    parser.add_argument("--pgid", type=int)
    parser.add_argument("--controller-pid", type=int)
    parser.add_argument("--controller-session-id")
    parser.add_argument("--controller-label")
    parser.add_argument("--detached", action="store_true",
                        help="Ignore controller-beacon liveness; worker pid identity is authoritative.")
    parser.add_argument(
        "--codex-dispatch-home-resolved",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    parser.add_argument("--codex-dispatch-home", help=argparse.SUPPRESS)
    parser.add_argument("--codex-session-id", help=argparse.SUPPRESS)
    parser.add_argument("--engine-session-id", help=argparse.SUPPRESS)
    parser.add_argument("--codex-home-owner-dispatch-id", help=argparse.SUPPRESS)
    parser.add_argument("--parent-dispatch-id", help=argparse.SUPPRESS)
    parser.add_argument("--worker-identity-json",
                        help="Process identity token captured at spawn; prevents PID-reuse false liveness.")
    parser.add_argument("--ignore-prompt-file",
                        help="Ignore marker lines appearing verbatim in this prompt file, so a worker's "
                             "echoed prompt can't trip the watcher on its own 'end with COMPLETE:' instruction.")
    parser.add_argument("--stay-after-terminal", action="store_true",
                        help="After a terminal marker, keep watching until the worker exits or this watcher is stopped.")
    args = parser.parse_args()

    tail = Path(args.tail)
    status_path = Path(args.status_json)
    status_existed_at_startup = status_path.exists()

    controller_session_id = args.controller_session_id
    controller_pid = args.controller_pid
    controller_label = args.controller_label
    if not controller_session_id or controller_pid is None:
        controller_session_id = None
        controller_pid = None
        controller_label = None

    ignore_prefix_lines: list[str] = []
    ignore_prompt_path: Path | None = None
    ignore_prompt_signature: tuple[int, int, int] | None = None
    prompt_snapshot_available = False
    prompt_provenance_available = False
    prompt_snapshot_needs_retry = False
    dispatch_record_nonterminal_at_startup = False
    if args.ignore_prompt_file:
        ignore_prompt_path = Path(args.ignore_prompt_file)
        prompt_snapshot = _read_prompt_exclusion_snapshot(ignore_prompt_path)
        if prompt_snapshot is not None:
            ignore_prefix_lines, ignore_prompt_signature = prompt_snapshot
            prompt_snapshot_available = any(
                line.strip() for line in ignore_prefix_lines
            )
            prompt_provenance_available = prompt_snapshot_available
        else:
            prompt_snapshot_needs_retry = True
        if not ignore_prompt_path.exists() and not status_existed_at_startup:
            print(
                "goalflight_watch: dispatch retired; prompt sidecar and status "
                f"are absent for {args.dispatch_id}",
                flush=True,
            )
            return 0
        dispatch_record_nonterminal_at_startup = _dispatch_record_is_nonterminal(
            args.dispatch_id
        )
        if (
            not status_existed_at_startup
            and not dispatch_record_nonterminal_at_startup
        ):
            print(
                "goalflight_watch: dispatch retired; status is absent without "
                f"a non-terminal record for {args.dispatch_id}",
                flush=True,
            )
            return 0
    expected_identity = _load_identity(args.worker_identity_json)
    task_ids = _split_task_ids(args.task_ids)
    task_project_root = goalflight_task.resolve_project_root(args.project_root)

    effective_account = _trace_ledger_account(args.dispatch_id)
    codex_home = (
        Path(args.codex_dispatch_home).expanduser()
        if args.codex_dispatch_home
        else None
    )
    resume_engine = goalflight_engine_sessions.resume_engine(args.agent)
    engine_session_id = goalflight_engine_sessions.valid_session_id(
        resume_engine, getattr(args, "engine_session_id", None)
    )
    codex_session_id = goalflight_codex_sessions.valid_session_id(
        args.codex_session_id
    )
    if resume_engine == "codex" and engine_session_id and not codex_session_id:
        codex_session_id = engine_session_id
    if engine_session_id is None and codex_session_id is not None:
        engine_session_id = codex_session_id
    engine_session_recorded = engine_session_id is not None
    codex_session_recorded = False
    if goalflight_compat.is_windows():
        payload = {
            "schema": "goalflight.status.v1",
            "dispatch_id": args.dispatch_id,
            "agent": args.agent,
            "worker_pid": args.pid,
            "detached": bool(args.detached),
            "controller_session_id": controller_session_id,
            "controller_pid": controller_pid,
            "controller_label": controller_label,
            "state": "blocked_windows_dispatch",
            "reason": goalflight_compat.windows_watcher_skip(),
            "tail_path": str(tail),
            "updated_at": int(time.time()),
        }
        if effective_account:
            payload["effective_account"] = effective_account
        if args.codex_home_owner_dispatch_id:
            payload["codex_home_owner_dispatch_id"] = (
                args.codex_home_owner_dispatch_id
            )
        if args.parent_dispatch_id:
            payload["parent_dispatch_id"] = args.parent_dispatch_id
        if engine_session_id is not None:
            payload["engine_session_id"] = engine_session_id
        if codex_session_id is not None:
            payload["codex_session_id"] = codex_session_id
        if codex_home is not None:
            payload["codex_home"] = str(codex_home)
        write_status(status_path, payload)
        print(json.dumps({"state": payload["state"], "reason": payload["reason"], "status_path": str(status_path)}, sort_keys=True))
        return 4
    tail_scanner = IncrementalTailScanner(
        tail,
        ignore_prefix_lines,
        expected_dispatch_id=args.dispatch_id,
    )
    steer_mailbox = goalflight_steer_mailbox.steer_file(args.dispatch_id)
    last_size = -1
    trace_liveness = TraceLiveness(
        dispatch_id=args.dispatch_id,
        worker_pid=args.pid,
        effective_account=effective_account,
        cached_path=_cached_trace_path(status_path),
    )
    # Idle accounting uses the sleep-excluding clock (active_monotonic):
    # macOS CLOCK_UPTIME_RAW / Linux CLOCK_MONOTONIC freeze across system
    # sleep, so a lid-close suspend does NOT count as worker idle time —
    # wall-clock deltas here produced phantom idle_timeout kills on wake
    # (same class as watch-dispatch-tail.sh's suspend-gap fix, 2026-06-09).
    # time.time() remains for epoch display fields and the deliberate wall-clock
    # controller-attention thresholds; it never drives idle termination.
    last_change = active_monotonic()
    watcher_started_epoch = _trace_ledger_started_epoch(args.dispatch_id) or time.time()
    terminal = None
    markers: list[dict] = []
    exit_reason = "unknown"
    exit_code = 1
    wedge_streak = 0
    tracked_worker_pgid = args.pgid or process_group_id(args.pid)
    pgid = tracked_worker_pgid or args.pid
    prior_status = _read_json_object(status_path) if status_existed_at_startup else None
    dispatch_record = _load_dispatch_record(args.dispatch_id)
    tree_leg = resolve_wedge_tree_leg(
        dispatch_record,
        project_root=getattr(args, "project_root", None),
        worker_cwd=getattr(args, "worker_cwd", None),
        status=prior_status,
    )
    tree_root = tree_leg.get("scan_root")
    restored_watch = load_wedge_watch_state(prior_status)
    prev_cputime_sample: dict[int, float] | None = restored_watch["cputime_sample"]
    prev_cputime_at_epoch: float | None = restored_watch["cputime_sampled_at"]
    prev_cputime_at_mono: float | None = None
    candidate_announced_at: float | None = restored_watch["candidate_announced_at"]
    previously_wedged = candidate_announced_at is not None or (
        isinstance(prior_status, dict)
        and prior_status.get("state") in {WORKER_STALLED_CANDIDATE_STATE, "worker_wedged"}
    )
    tail_size_samples: deque[tuple[float, int]] = deque()
    pgid = args.pgid or process_group_id(args.pid) or args.pid
    thresholds = LivenessThresholds(idle_timeout_s=args.max_idle_secs, cpu_epsilon_pct=args.cpu_epsilon)
    last_payload: dict | None = None
    terminal_seen: dict | None = None
    terminal_seen_at: float | None = None
    terminal_seen_size: int | None = None
    last_discarded_terminal_evidence: dict | None = None
    cached_worker_wait: dict | None = None
    disproved_worker_wait_ids: set[str] = set()
    final_status_written = False
    working_breadcrumb_written = False
    dispatch_retired = False
    last_prompt_reload_at: float | None = None
    status_recreation_authorized_at_startup = (
        not status_existed_at_startup
        and dispatch_record_nonterminal_at_startup
    )

    def status_write_allowed() -> bool:
        """Refuse to recreate an ACP status deliberately retired with its prompt."""

        if status_path.exists() or ignore_prompt_path is None:
            return True
        if not status_existed_at_startup:
            return status_recreation_authorized_at_startup
        if ignore_prompt_path.exists():
            return True
        return _dispatch_record_is_nonterminal(args.dispatch_id)

    def append_task_breadcrumb(state: str, payload: dict) -> dict | None:
        if not task_ids:
            return None
        try:
            store = goalflight_task.TaskStore(task_project_root)
            marker = payload.get("terminal_marker") or payload.get("last_marker")
            breadcrumb = {
                "dispatch_id": args.dispatch_id,
                "state": state,
                "ts": goalflight_task.utc_now(),
                "marker": marker,
                "agent": args.agent,
                "worker_pid": payload.get("worker_pid"),
                "status_path": str(status_path),
                "last_worker_state": _status_snapshot(payload),
            }
            store.append_dispatch_breadcrumbs(task_ids, breadcrumb, actor="watcher")
            return None
        except Exception as exc:  # task store errors must be durable in status.
            return {"type": type(exc).__name__, "message": str(exc)}

    def write_payload(payload: dict, *, reason: str | None = None, terminal_write: bool = False) -> dict | None:
        nonlocal codex_session_id, codex_session_recorded
        nonlocal engine_session_id, engine_session_recorded
        nonlocal last_payload, final_status_written, working_breadcrumb_written
        nonlocal dispatch_retired
        if not status_write_allowed():
            dispatch_retired = True
            final_status_written = True
            return {"type": "DispatchRetired", "message": "status recreation refused"}
        if effective_account:
            payload["effective_account"] = effective_account
        payload["controller_session_id"] = controller_session_id
        payload["controller_pid"] = controller_pid
        payload["controller_label"] = controller_label
        if codex_home is not None:
            payload["codex_home"] = str(codex_home)
            if codex_session_id is None:
                codex_session_id = goalflight_codex_sessions.discover_session_id(
                    codex_home
                )
        if codex_session_id is not None:
            payload["codex_session_id"] = codex_session_id
            if engine_session_id is None:
                engine_session_id = codex_session_id
            if not codex_session_recorded and args.dispatch_id:
                try:
                    goalflight_ledger.record_codex_session_id(
                        args.dispatch_id, codex_session_id
                    )
                    codex_session_recorded = True
                    engine_session_recorded = True
                except Exception as exc:
                    payload["codex_session_record_error"] = {
                        "type": type(exc).__name__,
                        "message": str(exc),
                    }
        if engine_session_id is None and resume_engine in {"moonshot", "grok"}:
            work_dir = (
                Path(args.project_root)
                if getattr(args, "project_root", None)
                else Path.cwd()
            )
            if resume_engine == "moonshot":
                engine_session_id = (
                    goalflight_engine_sessions.harvest_kimi_session_id(
                        Path.home(), work_dir
                    )
                )
                if engine_session_id is None:
                    try:
                        for line in Path(tail).read_text(
                            encoding="utf-8", errors="replace"
                        ).splitlines()[-80:]:
                            raw = goalflight_engine_sessions.parse_resume_footer_handle(
                                line
                            )
                            engine_session_id = (
                                goalflight_engine_sessions.valid_session_id(
                                    "moonshot", raw
                                )
                            )
                            if engine_session_id is not None:
                                break
                    except OSError:
                        pass
            elif resume_engine == "grok":
                engine_session_id = (
                    goalflight_engine_sessions.harvest_grok_session_id(
                        Path.home(), work_dir
                    )
                )
        if engine_session_id is not None:
            payload["engine_session_id"] = engine_session_id
            if not engine_session_recorded and args.dispatch_id:
                try:
                    goalflight_ledger.record_engine_session_id(
                        args.dispatch_id, engine_session_id
                    )
                    engine_session_recorded = True
                except Exception as exc:
                    payload["engine_session_record_error"] = {
                        "type": type(exc).__name__,
                        "message": str(exc),
                    }
        if args.parent_dispatch_id:
            payload["parent_dispatch_id"] = args.parent_dispatch_id
        if args.codex_home_owner_dispatch_id:
            payload["codex_home_owner_dispatch_id"] = (
                args.codex_home_owner_dispatch_id
            )
        if reason:
            payload["reason"] = reason
        mail_marker: dict | None = None
        terminal_marker: dict | object | None = None
        if terminal_write:
            harvested_headline = harvest_headline_marker(
                payload,
                tail,
                ignore_prefix_lines=ignore_prefix_lines,
                kimi_output=moonshot_family(args.agent),
                expected_dispatch_id=args.dispatch_id,
            )
            if harvested_headline is not None and not _is_headline_kind(
                payload.get("last_marker")
            ):
                payload["last_marker"] = harvested_headline
            mail_marker = harvested_headline
            terminal_marker = payload.get("terminal_marker") or terminal_seen
            final_state, final_reason, _limit_reached = goalflight_terminal.terminal_rate_limit_outcome(
                payload.get("state"),
                payload.get("reason"),
                tail,
                terminal_marker_present=goalflight_terminal.terminal_marker_present(
                    terminal_marker
                ),
            )
            final_state, final_reason, _vetoed = goalflight_terminal.final_reconciliation_error_veto_outcome(
                final_state,
                final_reason,
                tail,
                terminal_marker,
            )
            payload["state"] = final_state
            if final_reason not in (None, ""):
                payload["reason"] = final_reason
            payload["liveness_state"] = goalflight_terminal.terminal_liveness_state(payload.get("state"))
        terminal_error = None
        if task_ids:
            payload["task_ids"] = list(task_ids)
            if not working_breadcrumb_written:
                # Working breadcrumbs are advisory. Terminal breadcrumbs are
                # load-bearing for status after volatile dispatch state is reaped.
                working_payload = {**payload, "state": "working"}
                working_payload.pop("terminal_marker", None)
                working_payload.pop("last_marker", None)
                working_breadcrumb_written = True
                working_error = append_task_breadcrumb("working", working_payload)
                if working_error:
                    payload["task_breadcrumb_error"] = working_error
            if terminal_write:
                terminal_error = append_task_breadcrumb(_task_state_for_terminal(payload.get("state")), payload)
                if terminal_error:
                    payload["task_breadcrumb_error"] = terminal_error
                    payload["task_breadcrumb_failed"] = True
                    payload["task_breadcrumb_failed_state"] = payload.get("state")
                    if payload.get("reason"):
                        payload["task_breadcrumb_failed_reason"] = payload.get("reason")
                    # A store that cannot be trusted still blocks: if the file is
                    # corrupt or unwritable, the task system is broken and the
                    # run should stop for a human.
                    #
                    # A MISSING TASK ID is a different thing and must not. The
                    # dispatch referenced an item this repo's store does not have
                    # -- a bad input, not a broken store -- and the worker's own
                    # result is unaffected. Blocking on it rewrote a finished run
                    # as blocked: a worker emitted its terminal marker, staged its
                    # work, and the state flipped from `complete` to
                    # `blocked_task_breadcrumb` because a note about it could not
                    # be filed against t-482. The run was then hand-salvaged for
                    # work that was already done and already detected.
                    #
                    # If the message shape ever changes this falls back to
                    # blocking, which is the safe direction.
                    if not _task_breadcrumb_error_is_missing_item(terminal_error):
                        payload["state"] = BLOCKED_TASK_BREADCRUMB_STATE
                        payload["reason"] = "task_breadcrumb_error"
                        payload["liveness_state"] = goalflight_terminal.terminal_liveness_state(
                            payload.get("state")
                        )
                elif (
                    _task_state_for_terminal(payload.get("state")) == "worker-finished"
                    and not _terminal_marker_from_ignored_prompt(
                        tail,
                        payload.get("terminal_marker") or terminal_seen,
                        ignore_prefix_lines,
                    )
                ):
                    # Optional post-done suggestion only. Losing it gives up a
                    # convenience nudge, never task completion or terminal mail.
                    with contextlib.suppress(Exception):
                        goalflight_task.post_done_suggest_nudge(task_ids, task_project_root, args.dispatch_id)
        if terminal_write:
            payload["liveness_state"] = goalflight_terminal.terminal_liveness_state(payload.get("state"))
        ledger_error = None
        if terminal_write:
            blocking_marker = (
                terminal_marker
                if isinstance(terminal_marker, dict)
                and terminal_marker.get("kind") in WORKER_MAIL_MARKER_KINDS
                else None
            )
            headline_marker = (
                mail_marker
                if isinstance(mail_marker, dict)
                else blocking_marker
            )
            headline_text = None
            if isinstance(headline_marker, dict):
                headline_text = _strip_marker_decoration(
                    str(headline_marker.get("text") or "")
                ).strip() or None
            ledger_error = _finish_existing_ledger(
                args.dispatch_id,
                payload.get("state"),
                payload.get("reason"),
                worker_still_alive=payload.get("worker_alive"),
                agent=args.agent,
                detached=bool(args.detached),
                codex_dispatch_home_resolved=bool(
                    args.codex_dispatch_home_resolved
                ),
                codex_session_id=codex_session_id,
                terminal_marker=blocking_marker,
                headline=headline_text,
            )
            if ledger_error:
                payload["terminal_pending_state"] = payload.get("state")
                payload["state"] = "terminal_pending"
                payload["liveness_state"] = "terminal_pending"
                payload["ledger_finalize_error"] = ledger_error
        # Recheck immediately before the atomic status write: cleanup retires
        # prompt first and status second, so a watcher started in that gap must
        # observe the paired absence instead of recreating the cleaned status.
        if not status_write_allowed():
            dispatch_retired = True
            final_status_written = True
            return {"type": "DispatchRetired", "message": "status recreation refused"}
        write_status(status_path, payload)
        last_payload = dict(payload)
        if terminal_write and not ledger_error:
            final_status_written = True
        return terminal_error or ledger_error

    def apply_tail_quota_status(
        payload: dict,
        *,
        previous_state: str,
        previous_reason: object,
    ) -> bool:
        if not goalflight_quota_stuck.record_quota_signature({"tail_path": str(tail)}, require_tail=True):
            return False
        return goalflight_quota_stuck.apply_rate_limited_status(
            payload,
            agent=args.agent,
            tail=tail,
            previous_state=previous_state,
            previous_reason=previous_reason,
            effective_account=effective_account,
        )

    def flush_terminal_status(reason: str) -> None:
        nonlocal final_status_written
        if final_status_written:
            return
        now = time.time()
        worker_is_alive, identity_reason, current_identity = worker_alive(args.pid, expected_identity)
        if worker_is_alive:
            current_pgid = args.pgid or process_group_id(args.pid) or pgid
            cpu_pct = pgroup_cpu_pct(current_pgid)
        else:
            current_pgid = pgid
            cpu_pct = 0.0
        if terminal_seen and not (
            args.stay_after_terminal and worker_is_alive and _marker_state(terminal_seen) == "complete"
        ):
            state = _marker_state(terminal_seen)
        elif worker_is_alive:
            state = "watcher_stopped"
        else:
            state = "worker_dead"
        payload = dict(last_payload or {})
        payload.update({
            "schema": "goalflight.status.v1",
            "dispatch_id": args.dispatch_id,
            "agent": args.agent,
            "worker_pid": args.pid,
            "detached": bool(args.detached),
            "pgid": current_pgid,
            "worker_alive": worker_is_alive,
            "worker_identity_reason": identity_reason,
            "worker_identity": _identity_token(current_identity),
            "expected_worker_identity": _identity_token(expected_identity),
            "pgroup_cpu_pct": cpu_pct,
            "tail_path": str(tail),
            "terminal_marker": terminal_seen or (payload.get("terminal_marker") if isinstance(payload, dict) else None),
            "state": state,
            "updated_at": int(now),
        })
        if state in {"worker_dead", "watcher_stopped"} and not terminal_seen:
            apply_tail_quota_status(payload, previous_state=state, previous_reason=reason)
        write_reason = (
            payload.get("reason")
            if goalflight_dispatch_states.is_limit_state(payload.get("state"))
            else reason
        )
        try:
            write_payload(payload, reason=write_reason, terminal_write=True)
        except Exception as exc:
            # Signal/atexit cleanup must preserve the primary exit and any
            # terminal event already published by write_payload, but a failed
            # final status/state publication must remain operator-visible.
            with contextlib.suppress(OSError, ValueError):
                print(
                    "goalflight_watch: final state write failed: "
                    f"{type(exc).__name__}: {exc}",
                    file=sys.stderr,
                    flush=True,
                )

    def handle_signal(signum: int, _frame) -> None:
        name = getattr(signal.Signals(signum), "name", str(signum))
        flush_terminal_status(f"signal:{name}")
        raise SystemExit(128 + signum)

    for signame in ("SIGTERM", "SIGHUP", "SIGINT"):
        sig = getattr(signal, signame, None)
        if sig is not None:
            signal.signal(sig, handle_signal)
    atexit.register(lambda: flush_terminal_status("watcher_exit"))

    posted_trace_attention: set[str] = set()
    posted_worker_wait_attention: set[tuple[str, int, str]] = set()
    while True:
        if dispatch_retired:
            break
        if ignore_prompt_path is not None:
            try:
                current_prompt_stat = ignore_prompt_path.stat()
                current_prompt_signature = _prompt_file_signature(
                    current_prompt_stat
                )
            except OSError:
                current_prompt_signature = None
                prompt_provenance_available = False
                prompt_snapshot_needs_retry = True
            prompt_provenance_available = (
                _prompt_provenance_matches_loaded_snapshot(
                    current_prompt_signature,
                    ignore_prompt_signature,
                    prompt_snapshot_available,
                )
            )
            reload_now = active_monotonic()
            reload_prompt = (
                current_prompt_signature is not None
                and prompt_snapshot_needs_retry
            ) or _prompt_reload_due(
                current_prompt_signature,
                ignore_prompt_signature,
                last_reload_at=last_prompt_reload_at,
                now=reload_now,
                poll_secs=args.poll_secs,
            )
            if reload_prompt:
                prompt_snapshot = _read_prompt_exclusion_snapshot(
                    ignore_prompt_path
                )
                if prompt_snapshot is not None:
                    ignore_prefix_lines, ignore_prompt_signature = prompt_snapshot
                    prompt_snapshot_available = any(
                        line.strip() for line in ignore_prefix_lines
                    )
                    prompt_provenance_available = prompt_snapshot_available
                    prompt_snapshot_needs_retry = False
                    last_prompt_reload_at = reload_now
                    # ACP steers regenerate the delivered prompt between turns.
                    # A startup-only snapshot lets a later echoed steer marker
                    # escape exclusions during recovery, so replay the tail
                    # after this cheap per-poll signature check observes a change.
                    # Coalescing permits at most one zero-offset replay per poll
                    # interval, bounding replay work to O(changes x tail size)
                    # for the coalesced sidecar changes a watcher observes.
                    # Clear any candidate parsed in the narrow stat/replace
                    # race under the prior exclusions before trusting replay.
                    tail_scanner.update_prompt_prefix(ignore_prefix_lines)
                    terminal_seen = None
                    terminal_seen_at = None
                else:
                    prompt_provenance_available = False
                    prompt_snapshot_needs_retry = True
                    terminal_seen_size = None
        scan = tail_scanner.scan(kimi_output=moonshot_family(args.agent))
        markers = scan.markers
        size = scan.size
        if size != last_size:
            last_size = size
            last_change = active_monotonic()
        now = time.time()
        loop_mono = active_monotonic()
        fresh_worker_wait, wait_read_succeeded = _read_active_worker_wait(
            steer_mailbox,
            args.dispatch_id,
            now_mono=loop_mono,
            worker_pid=args.pid,
            worker_pgid=tracked_worker_pgid,
        )
        if wait_read_succeeded:
            if fresh_worker_wait is None:
                cached_worker_wait = None
            else:
                fresh_wait_id = str(fresh_worker_wait.get("wait_id") or "")
                same_cached_wait = bool(
                    cached_worker_wait
                    and cached_worker_wait.get("wait_id")
                    == fresh_wait_id
                )
                observed_marker = next(
                    (
                        marker
                        for marker in reversed(markers)
                        if _worker_wait_marker_matches(
                            marker,
                            fresh_worker_wait,
                            args.dispatch_id,
                        )
                    ),
                    None,
                )
                if fresh_wait_id in disproved_worker_wait_ids:
                    cached_worker_wait = None
                elif same_cached_wait or observed_marker is not None:
                    cached_worker_wait = fresh_worker_wait
                else:
                    # An arm is only an intent record. It cannot suspend the
                    # tracked worker until this watcher observes its exact
                    # wait-id-bound question marker in the tracked tail.
                    cached_worker_wait = None
        elif not _cached_worker_wait_is_valid(
            cached_worker_wait,
            now_mono=loop_mono,
            worker_pid=args.pid,
            worker_pgid=tracked_worker_pgid,
        ):
            cached_worker_wait = None
        if cached_worker_wait and any(
            _worker_wait_reply_output_matches(marker, cached_worker_wait)
            for marker in markers
        ):
            disproved_worker_wait_ids.add(
                str(cached_worker_wait.get("wait_id") or "")
            )
            cached_worker_wait = None
        worker_wait = cached_worker_wait
        if worker_wait:
            # Option (a): suspend the idle clock for an explicit, independently
            # bounded reply wait. This represents controller-blocked work without
            # coupling the reply deadline to max_idle_secs; the mailbox arm owns
            # its capped deadline, and ordinary idle accounting resumes afterward.
            last_change = loop_mono
        seconds_since_event = loop_mono - last_change
        now_mono = active_monotonic()
        seconds_since_event = now_mono - last_change
        wedge_idle_s = float(getattr(args, "wedge_idle_secs", DEFAULT_WEDGE_IDLE_SECS) or 0.0)
        tail_size_samples.append((now_mono, int(size)))
        if wedge_idle_s > 0:
            cutoff = now_mono - wedge_idle_s
            while tail_size_samples and tail_size_samples[0][0] < cutoff:
                tail_size_samples.popleft()
        tail_bytes_grown = (
            max(0, int(size) - tail_size_samples[0][1]) if tail_size_samples else 0
        )
        trace_sample = trace_liveness.sample(
            now_epoch=now,
            now_mono=active_monotonic(),
            idle_threshold=args.max_idle_secs,
        )
        terminal = scan.terminal
        if terminal:
            # Stability recheck (minimal gap protection): if bytes arrive within a short
            # window after a terminal marker became the last line, it was a mid-output
            # emission; discard and keep watching. Genuine sign-off is worker's final act.
            time.sleep(0.05)
            recheck = tail_scanner.scan(
                kimi_output=moonshot_family(args.agent),
            )
            scan = _combine_tail_scan_results(scan, recheck)
            markers = scan.markers
            terminal = scan.terminal
            # A terminal found by the stability recheck belongs to the recheck's
            # offset. Using the first scan's size makes those marker bytes look
            # like later growth and falsely vetoes the new candidate.
            size = scan.size
            if scan.size != last_size:
                last_size = scan.size
                last_change = active_monotonic()
        retained_discarded_candidate = _discarded_terminal_candidate_matches(
            last_discarded_terminal_evidence,
            terminal,
            scan.terminal_observed_size,
        )
        if retained_discarded_candidate:
            # Prompt replay (or any same-offset reparse) must not resurrect a
            # candidate already vetoed by later live-worker growth. Keep the
            # durable evidence, but leave the candidate unarmed until a marker
            # is genuinely re-emitted at a new byte offset.
            terminal = None
            scan.terminal = None
        worker_is_alive, identity_reason, current_identity = worker_alive(args.pid, expected_identity)
        cpu_delta_s: float | None = None
        sample_interval_s: float | None = None
        if worker_is_alive:
            tracked_worker_pgid = args.pgid or process_group_id(args.pid)
            pgid = tracked_worker_pgid or pgid
            cpu_pct = pgroup_cpu_pct(pgid)
            cpu_sample = pgroup_cputime_snapshot(pgid)
            if cpu_sample is not None and prev_cputime_sample is not None:
                if prev_cputime_at_mono is not None:
                    sample_interval_s = now_mono - prev_cputime_at_mono
                elif prev_cputime_at_epoch is not None:
                    sample_interval_s = max(0.0, now - prev_cputime_at_epoch)
                if sample_interval_s is not None and sample_interval_s > 0:
                    cpu_delta_s = cputime_delta_seconds(prev_cputime_sample, cpu_sample)
                else:
                    sample_interval_s = None
            if cpu_sample is not None:
                prev_cputime_sample = cpu_sample
                prev_cputime_at_mono = now_mono
                prev_cputime_at_epoch = now
        else:
            cpu_pct = 0.0
            prev_cputime_sample = None
            prev_cputime_at_mono = None
            prev_cputime_at_epoch = None
        live_descendants: int | None = None
        idle_tree_age_s: float | None = None
        idle_window_expired = (
            worker_is_alive
            and args.max_idle_secs > 0
            and seconds_since_event >= args.max_idle_secs
        )
        # Extra activity is consulted only on the path that would otherwise
        # kill: tail quiet AND CPU measured idle. A busy group already
        # vetoes; missing CPU already fails open.
        if idle_window_expired and cpu_confirmed_idle(cpu_pct, args.cpu_epsilon):
            live_descendants = live_descendant_count(args.pid)
            if not (isinstance(live_descendants, int) and live_descendants > 0):
                if (
                    tree_leg.get("kind") == WEDGE_TREE_LEG_WORKER_CWD
                    and isinstance(tree_root, Path)
                ):
                    newest_idle_tree = newest_mtime_under(
                        tree_root,
                        stop_if_newer_than=now - args.max_idle_secs,
                    )
                    if newest_idle_tree is not None:
                        idle_tree_age_s = max(0.0, now - newest_idle_tree)
        low_power_relax = (
            idle_window_expired
            and cpu_confirmed_idle(cpu_pct, args.cpu_epsilon)
            and system_starved()
        )
        liveness_state = classify_liveness(
            worker_is_alive,
            cpu_pct,
            seconds_since_event,
            thresholds,
            low_power_relax=low_power_relax,
            live_descendants=live_descendants,
            tree_age_s=idle_tree_age_s,
        )
        intentionally_waiting = bool(worker_wait and worker_is_alive)
        if intentionally_waiting:
            liveness_state = "intentionally_blocked"
        trace_idle_veto = (
            liveness_state == "wedged"
            and _trace_vetoes_idle(
                trace_active=bool(trace_sample.get("trace_active")),
            )
        )
        if trace_idle_veto:
            liveness_state = "running_via_trace"
        payload = {
            "schema": "goalflight.status.v1",
            "dispatch_id": args.dispatch_id,
            "agent": args.agent,
            "worker_pid": args.pid,
            "detached": bool(args.detached),
            "pgid": pgid,
            "worker_alive": worker_is_alive,
            "worker_identity_reason": identity_reason,
            "worker_identity": _identity_token(current_identity),
            "expected_worker_identity": _identity_token(expected_identity),
            "pgroup_cpu_pct": cpu_pct,
            "seconds_since_event": seconds_since_event,
            "liveness_state": liveness_state,
            "live_descendants": live_descendants,
            "idle_tree_age_s": idle_tree_age_s,
            "tail_path": str(tail),
            "tail_scan": scan.metrics(),
            "markers": markers[-20:],
            "last_marker": markers[-1] if markers else None,
            "terminal_marker": terminal,
            "state": (
                "awaiting_steer_reply"
                if intentionally_waiting
                else "running_quiet"
                if liveness_state == "running_quiet"
                else "running"
            ),
            # b-054: record the EFFECTIVE idle budget.  The launcher's initial
            # status may say None (its record predates default resolution), and
            # a budget that cannot be audited from status.json reads as
            # unhonored -- the flag was honored all along, but the record lied.
            "max_idle_secs": args.max_idle_secs,
            "wedge_idle_secs": float(
                getattr(args, "wedge_idle_secs", DEFAULT_WEDGE_IDLE_SECS) or 0.0
            ),
            "updated_at": int(now),
        }
        if ignore_prompt_signature is not None:
            payload["ignore_prompt_mtime_ns"] = ignore_prompt_signature[0]
            payload["ignore_prompt_signature"] = {
                "mtime_ns": ignore_prompt_signature[0],
                "size": ignore_prompt_signature[1],
                "ino": ignore_prompt_signature[2],
            }
        payload.update(trace_sample)
        if intentionally_waiting and worker_wait is not None:
            payload["worker_wait"] = dict(worker_wait)
        if last_discarded_terminal_evidence:
            payload["last_discarded_terminal_evidence"] = dict(
                last_discarded_terminal_evidence
            )
        if retained_discarded_candidate:
            payload["replayed_discarded_terminal_evidence"] = True
        if trace_idle_veto:
            payload["reason"] = "quiet_console_active_trace"
        trace_attention = _trace_attention_state(
            trace_active=bool(trace_sample.get("trace_active")),
            runtime_secs=max(0.0, now - watcher_started_epoch),
            long_running_secs=args.trace_long_running_secs,
            review_secs=args.trace_review_secs,
        )
        if trace_attention and not intentionally_waiting:
            payload["state"] = trace_attention
            payload["reason"] = "quiet_console_active_trace"
            post_trace_attention(
                args.dispatch_id,
                trace_attention,
                posted_trace_attention,
            )
        trace_active = bool(trace_sample.get("trace_active"))
        tail_age_s = _tail_mtime_age_s(tail, now=now)
        tree_age_s: float | None = None
        if wedge_idle_s > 0:
            payload["wedge_tree_leg"] = {
                "kind": tree_leg.get("kind"),
                "reason": tree_leg.get("reason"),
                "scan_root": (
                    str(tree_leg["scan_root"]) if tree_leg.get("scan_root") else None
                ),
                "worker_cwd": tree_leg.get("worker_cwd"),
                "canonical_root": tree_leg.get("canonical_root"),
            }
        if (
            wedge_idle_s > 0
            and tree_leg.get("kind") == WEDGE_TREE_LEG_WORKER_CWD
            and worker_is_alive
            and not trace_active
            and not trace_attention
            and tail_age_s is not None
            and tail_age_s >= wedge_idle_s
            and cpu_delta_s is not None
            and sample_interval_s is not None
            and sample_interval_s > 0
            and cpu_delta_s <= WEDGE_CPU_DELTA_EPSILON_S
            and isinstance(tree_root, Path)
        ):
            newest_tree = newest_mtime_under(
                tree_root,
                stop_if_newer_than=now - wedge_idle_s,
            )
            if newest_tree is not None:
                tree_age_s = max(0.0, now - newest_tree)
        if terminal:
            # A sign-off is not a recover event. Leave wedge classification
            # off this payload so the marker path can terminalize cleanly.
            previously_wedged = False
        else:
            wedge_evidence = classify_worker_wedge(
                worker_alive=worker_is_alive and not trace_active and not trace_attention,
                tail_age_s=tail_age_s,
                tree_age_s=tree_age_s,
                cpu_delta_s=cpu_delta_s,
                sample_interval_s=sample_interval_s,
                threshold_s=wedge_idle_s,
                tail_bytes_grown=tail_bytes_grown,
            )
            if wedge_evidence is not None:
                wedge_evidence["tree_scan_kind"] = tree_leg.get("kind")
                if tree_leg.get("scan_root") is not None:
                    wedge_evidence["tree_scan_root"] = str(tree_leg["scan_root"])
            wedge_applied = apply_worker_wedge(
                payload,
                evidence=wedge_evidence,
                previously_wedged=previously_wedged,
                dispatch_id=args.dispatch_id,
            )
            previously_wedged = bool(wedge_applied["wedged"])
            if wedge_applied["event"] == "enter":
                candidate_announced_at = now
            elif wedge_applied["event"] == "recover":
                candidate_announced_at = None
        payload["wedge_watch"] = dump_wedge_watch_state(
            cputime_sample=prev_cputime_sample,
            cputime_sampled_at=prev_cputime_at_epoch,
            candidate_announced_at=candidate_announced_at,
        )
        if low_power_relax:
            payload["low_power_relax"] = True
        if liveness_state == "wedged":
            wedge_streak += 1
        else:
            wedge_streak = 0
        idle_confirmed = (
            liveness_state == "wedged" and wedge_streak >= WEDGE_CONFIRM_SAMPLES
        )
        if terminal and terminal != terminal_seen:
            terminal_seen = terminal
            terminal_seen_at = active_monotonic()
            terminal_seen_size = (
                scan.terminal_observed_size
                if scan.terminal_observed_size is not None
                else size
            )
        if intentionally_waiting and worker_wait is not None:
            waiting_marker = next(
                (
                    marker
                    for marker in reversed(markers)
                    if _worker_wait_marker_matches(
                        marker,
                        worker_wait,
                        args.dispatch_id,
                    )
                ),
                None,
            )
            post_worker_wait_attention(
                args.dispatch_id,
                worker_wait,
                waiting_marker,
                posted_worker_wait_attention,
            )
            if (
                terminal_seen
                and _worker_wait_marker_matches(
                    terminal_seen,
                    worker_wait,
                    args.dispatch_id,
                )
            ):
                # The durable wait arm makes this an attention event, not a
                # dispatch terminal. The later reply/timeout output disproves
                # final-line status and receives a fresh idle budget.
                terminal_seen = None
                terminal_seen_at = None
                terminal_seen_size = None
                payload.pop("terminal_marker", None)
        terminal_state = _marker_state(terminal_seen) if terminal_seen else None
        terminal_reason = f"marker:{terminal_seen['kind']}" if terminal_seen else None
        post_terminal_wait = (
            bool(terminal_seen)
            and args.stay_after_terminal
            and worker_is_alive
            and terminal_state == "complete"
        )
        if terminal_seen:
            payload["terminal_marker"] = terminal_seen
        post_terminal_wait_elapsed = (
            max(0.0, active_monotonic() - terminal_seen_at)
            if post_terminal_wait and terminal_seen_at is not None
            else None
        )
        post_terminal_action = "pending"
        if post_terminal_wait:
            post_terminal_action = _post_terminal_candidate_action(
                worker_alive=worker_is_alive,
                tail_grew=(
                    terminal_seen_size is not None and size > terminal_seen_size
                ),
                grace_expired=(
                    post_terminal_wait_elapsed is not None
                    and post_terminal_wait_elapsed >= POST_TERMINAL_EXIT_GRACE_SECS
                ),
                idle_confirmed=idle_confirmed,
            )
        if post_terminal_action == "discard":
            discarded_marker = terminal_seen
            last_discarded_terminal_evidence = {
                "kind": str((discarded_marker or {}).get("kind") or ""),
                "dispatch_id_binding": args.dispatch_id,
                "offset": int(terminal_seen_size if terminal_seen_size is not None else size),
                "marker": dict(discarded_marker or {}),
            }
            print(
                "WATCHER-DISCARD "
                + json.dumps(
                    {
                        "marker": discarded_marker,
                        "reason": "worker_alive_tail_grew_since_marker",
                    },
                    sort_keys=True,
                ),
                flush=True,
            )
            payload["discarded_terminal_marker"] = discarded_marker
            payload["last_discarded_terminal_evidence"] = dict(
                last_discarded_terminal_evidence
            )
            payload.pop("terminal_marker", None)
            terminal_seen = None
            terminal_seen_at = None
            terminal_seen_size = None
            wedge_streak = 0
            write_payload(
                payload,
                reason="discarded_terminal_marker:worker_alive_tail_grew_since_marker",
                terminal_write=False,
            )
            time.sleep(args.poll_secs)
            continue
        reply_wait_arm_grace = bool(
            terminal_seen
            and terminal_seen.get("kind") in REPLY_WAIT_MARKER_KINDS
            and worker_is_alive
            and terminal_seen_at is not None
            and active_monotonic() - terminal_seen_at
            < max(WORKER_WAIT_ARM_GRACE_SECS, args.poll_secs * 2.0)
        )
        if terminal_seen and not post_terminal_wait and not reply_wait_arm_grace:
            payload["state"] = terminal_state
            exit_code = _exit_code_for_state(payload["state"])
            exit_reason = terminal_reason
            write_payload(payload, reason=terminal_reason, terminal_write=True)
            exit_code = _exit_code_for_state(payload["state"])
            exit_reason = payload.get("reason", exit_reason)
            break
        if (
            controller_pid
            and not alive(controller_pid)
            and _controller_dead_is_terminal(detached=bool(args.detached))
        ):
            # Controller disappearance is an observation, not worker evidence.
            # Recheck the identity-qualified worker after the controller probe so
            # every path to controller_dead is gated by current worker reality.
            payload["controller_alive"] = False
            worker_is_alive, identity_reason, current_identity = worker_alive(
                args.pid, expected_identity
            )
            payload["worker_alive"] = worker_is_alive
            payload["worker_identity_reason"] = identity_reason
            payload["worker_identity"] = _identity_token(current_identity)
            if not worker_is_alive or bool(trace_sample.get("trace_active")):
                reconciled = _final_terminal_marker(
                    tail,
                    ignore_prefix_lines=ignore_prefix_lines,
                    kimi_output=moonshot_family(args.agent),
                    expected_dispatch_id=args.dispatch_id,
                    full_file_fallback=not worker_is_alive,
                )
                if reconciled:
                    terminal_seen = reconciled
                    payload["terminal_marker"] = terminal_seen
                    payload["state"] = _marker_state(terminal_seen)
                    exit_reason = f"marker:{terminal_seen['kind']}:final_reconciliation"
                    exit_code = _exit_code_for_state(payload["state"])
                    write_payload(payload, reason=exit_reason, terminal_write=True)
                    break
            if worker_is_alive:
                write_payload(payload, reason="controller_dead_worker_alive")
                time.sleep(args.poll_secs)
                continue
            payload["state"] = "orphaned"
            exit_reason = "controller_dead"
            exit_code = 3
            write_payload(payload, reason=exit_reason, terminal_write=True)
            exit_code = _exit_code_for_state(payload["state"])
            exit_reason = payload.get("reason", exit_reason)
            break
        if not worker_is_alive:
            reconciled = _final_terminal_marker(
                tail,
                ignore_prefix_lines=ignore_prefix_lines,
                kimi_output=moonshot_family(args.agent),
                expected_dispatch_id=args.dispatch_id,
                full_file_fallback=True,
            )
            if not reconciled:
                recorded = _recorded_terminal_success_marker(
                    payload,
                    expected_dispatch_id=args.dispatch_id,
                )
                if (
                    recorded
                    and terminal
                    and recorded.get("line") == terminal.get("line")
                ):
                    # A generic last_marker can be a mid-tail prompt quote. It
                    # only wins after the same final-line scan validates it.
                    reconciled = recorded
            if reconciled:
                terminal_seen = reconciled
                payload["terminal_marker"] = terminal_seen
                payload["state"] = _marker_state(terminal_seen)
                exit_reason = f"marker:{terminal_seen['kind']}:final_reconciliation"
                exit_code = _exit_code_for_state(payload["state"])
            else:
                if bool(trace_sample.get("trace_active")):
                    # Re-check identity and output at the terminal boundary.
                    # A genuinely dead pid cannot keep its validated trace
                    # fresh, so this veto is bounded by the trace activity
                    # window without adding a separate confirmation counter.
                    worker_is_alive, identity_reason, current_identity = worker_alive(
                        args.pid, expected_identity
                    )
                    reconciled = _final_terminal_marker(
                        tail,
                        ignore_prefix_lines=ignore_prefix_lines,
                        kimi_output=moonshot_family(args.agent),
                        expected_dispatch_id=args.dispatch_id,
                        full_file_fallback=True,
                    )
                    if reconciled:
                        terminal_seen = reconciled
                        payload["terminal_marker"] = terminal_seen
                        payload["state"] = _marker_state(terminal_seen)
                        exit_reason = f"marker:{terminal_seen['kind']}:trace_reconciliation"
                        exit_code = _exit_code_for_state(payload["state"])
                        write_payload(payload, reason=exit_reason, terminal_write=True)
                        break
                    payload["worker_alive"] = worker_is_alive
                    payload["worker_identity_reason"] = identity_reason
                    payload["worker_identity"] = _identity_token(current_identity)
                    payload["liveness_state"] = "running_via_trace"
                    write_payload(payload, reason="pid_resolved_dead_active_trace_reverify")
                    time.sleep(args.poll_secs)
                    continue
                # Output-is-truth veto, gated on ACTIVE growth (not the whole idle
                # window): the tracked pid is often a launcher/wrapper that exits while
                # a detached worker child keeps streaming. Veto worker_dead only while
                # the tail grew within the last couple of poll cycles. A worker that
                # emits a final line then crashes goes stale within ~2 polls -> caught
                # fast (crash-safe); a worker still streaming stays alive. Death stays
                # bounded by this small window, independent of --max-idle-secs.
                active_growth_window = max(args.poll_secs * 2.0, 0.2)
                if seconds_since_event < active_growth_window:
                    payload["state"] = "running"
                    payload["liveness_state"] = "running_via_output"
                    payload["worker_alive"] = True
                    write_payload(payload, reason="pid_resolved_dead_output_fresh")
                    time.sleep(args.poll_secs)
                    continue
                payload["state"] = "worker_dead"
                exit_reason = (
                    _worker_dead_no_marker_reason(
                        tail,
                        ignore_prefix_lines,
                        prompt_provenance_available=prompt_provenance_available,
                        prompt_path=ignore_prompt_path,
                        prompt_signature=ignore_prompt_signature,
                        last_marker=(
                            payload.get("last_marker")
                            if isinstance(payload.get("last_marker"), dict)
                            else None
                        ),
                    )
                    if identity_reason == "dead"
                    else f"worker_identity_mismatch:{identity_reason}"
                )
                exit_code = 1
                if apply_tail_quota_status(payload, previous_state="worker_dead", previous_reason=exit_reason):
                    exit_reason = payload["reason"]
            write_payload(payload, reason=exit_reason, terminal_write=True)
            exit_code = _exit_code_for_state(payload["state"])
            exit_reason = payload.get("reason", exit_reason)
            break
        if liveness_state == "wedged":
            if idle_confirmed:
                terminal_liveness_exit = False
                if post_terminal_wait and post_terminal_action == "terminalize":
                    payload["state"] = "inconclusive_timeout"
                    payload["terminal_pending_state"] = terminal_state
                    if post_terminal_wait_elapsed is not None:
                        payload["post_terminal_wait_elapsed_secs"] = round(
                            post_terminal_wait_elapsed, 3
                        )
                    payload["post_terminal_wait_limit_secs"] = POST_TERMINAL_EXIT_GRACE_SECS
                    exit_reason = f"{terminal_reason}:post_terminal_idle_timeout"
                    exit_code = _exit_code_for_state(payload["state"])
                    terminal_liveness_exit = True
                elif not post_terminal_wait:
                    if last_discarded_terminal_evidence:
                        evidence_kind = str(
                            last_discarded_terminal_evidence.get("kind") or "COMPLETE"
                        )
                        payload["state"] = "inconclusive_timeout"
                        payload["terminal_pending_state"] = _marker_state(
                            last_discarded_terminal_evidence
                        )
                        payload["last_discarded_terminal_evidence"] = dict(
                            last_discarded_terminal_evidence
                        )
                        exit_reason = (
                            f"marker:{evidence_kind}:post_terminal_idle_timeout"
                        )
                        exit_code = _exit_code_for_state(payload["state"])
                    else:
                        payload["state"] = "idle_timeout"
                        exit_reason = "idle_timeout"
                        exit_code = 2
                        if apply_tail_quota_status(
                            payload,
                            previous_state="idle_timeout",
                            previous_reason=exit_reason,
                        ):
                            exit_reason = payload["reason"]
                            exit_code = 1
                    terminal_liveness_exit = True
                if terminal_liveness_exit:
                    write_payload(payload, reason=exit_reason, terminal_write=True)
                    exit_code = _exit_code_for_state(payload["state"])
                    exit_reason = payload.get("reason", exit_reason)
                    break
        if post_terminal_wait:
            payload["state"] = "running_after_terminal"
            payload["terminal_pending_state"] = terminal_state
            write_payload(payload, reason=f"{terminal_reason}:worker_alive", terminal_write=False)
        else:
            write_payload(payload)
        time.sleep(args.poll_secs)

    if dispatch_retired:
        print(
            "goalflight_watch: dispatch retired; prompt sidecar and status "
            f"are absent for {args.dispatch_id}",
            flush=True,
        )
        return 0
    print(json.dumps({"state": payload["state"], "reason": exit_reason, "status_path": str(status_path)}, sort_keys=True))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
