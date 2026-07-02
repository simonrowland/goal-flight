#!/usr/bin/env python3
"""Watch a worker log and emit compact goal-flight status JSON."""

from __future__ import annotations

import argparse
import atexit
import contextlib
import io
import json
import os
from pathlib import Path
import re
import signal
import sys
import time

SCRIPT_DIR = Path(__file__).resolve().parent
ROOT = SCRIPT_DIR.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import goalflight_compat
import goalflight_ledger
import goalflight_quota_stuck
import goalflight_task
import goalflight_terminal
from goalflight_liveness import (
    LivenessThresholds,
    active_monotonic,
    classify_liveness,
    cpu_confirmed_idle,
    pgroup_cpu_pct,
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
MARKER_KINDS = frozenset(kind for kind in _MARKER_KIND_ORDER if kind in TERMINAL_MARKERS or kind in {"STATUS", "STEER-ACK"})
_MARKER_KIND_ALTERNATION = "|".join(re.escape(kind) for kind in _MARKER_KIND_ORDER if kind in MARKER_KINDS)
_TERMINAL_MARKER_KIND_ALTERNATION = "|".join(re.escape(kind) for kind in _MARKER_KIND_ORDER if kind in TERMINAL_MARKERS)
SHELL_TERMINAL_MARKER_RE = rf"^\**({_TERMINAL_MARKER_KIND_ALTERNATION}):\**"
MARKER_RE = re.compile(rf"^\**({_MARKER_KIND_ALTERNATION}):\**\s*(.*)$")
FINAL_TERMINAL_MARKER_RE = re.compile(
    r"^(?:-\s+)?`?\**(?:STATUS:\s*)?"
    rf"({_TERMINAL_MARKER_KIND_ALTERNATION}):(.*)$"
)
COMPLETION_SIGNOFF_RE = re.compile(
    r"^(?:STATUS:\s*)?(DONE|COMPLETE|FINISHED)\s*:?\s*[.!?]?$",
    re.IGNORECASE,
)
BARE_TERMINAL_MARKER_RE = re.compile(
    rf"^(?:(?:{_TERMINAL_MARKER_KIND_ALTERNATION}):\s*.*|"
    r"(?:DONE|COMPLETE|FINISHED)\s*:?\s*[.!?]?)$",
    re.IGNORECASE,
)
HUNK_HEADER_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+\d+(?:,\d+)? @@")
PROMPT_ECHO_ANCHOR_SEARCH_LINES = 200
PROMPT_ECHO_MAX_ANCHORS = 10
# CPU-sampling-failure grace (codex 2026-05-20 P2): idle_timeout exits only on
# confirmed-idle CPU. Unavailable CPU (ps failure -> None) keeps waiting instead
# of false-killing a healthy quiet worker. The streak still protects against
# one-off noisy idle samples.
WEDGE_CONFIRM_SAMPLES = 2
BLOCKED_TASK_BREADCRUMB_STATE = "blocked_task_breadcrumb"


def _marker_state(marker: dict | None) -> str:
    if marker and marker.get("kind") in SUCCESS_TERMINAL_MARKERS:
        return "complete"
    return "blocked"


def _exit_code_for_state(state: str) -> int:
    if state == "complete":
        return 0
    if state == "worker_dead":
        return 1
    if state == "rate_limited":
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


def _finish_existing_ledger(
    dispatch_id: str,
    state: object,
    reason: object,
    *,
    worker_still_alive: object = None,
) -> dict | None:
    if not dispatch_id or not state:
        return None
    if state == "watcher_stopped" and worker_still_alive is True:
        return None
    path = goalflight_ledger.record_path(dispatch_id, create=False)
    if not path.exists():
        return None
    max_attempts = 3
    backoff_s = 0.05
    last_error: dict | None = None
    for attempt in range(max_attempts):
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                goalflight_ledger.cmd_finish(
                    argparse.Namespace(
                        dispatch_id=dispatch_id,
                        state=str(state),
                        reason=reason,
                        terminal_state=None,
                        elapsed_s=None,
                        worker_still_alive=worker_still_alive,
                    )
                )
            return None
        except Exception as exc:
            last_error = {"type": type(exc).__name__, "message": str(exc)}
            if attempt + 1 < max_attempts:
                time.sleep(backoff_s * (attempt + 1))
    return last_error


def _status_snapshot(payload: dict) -> dict:
    keys = (
        "schema",
        "dispatch_id",
        "agent",
        "shape",
        "state",
        "reason",
        "worker_pid",
        "pgid",
        "worker_alive",
        "worker_identity_reason",
        "pgroup_cpu_pct",
        "seconds_since_event",
        "liveness_state",
        "tail_path",
        "status_path",
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


def _is_fence_line(raw_line: str) -> bool:
    lstrip = raw_line.lstrip()
    return lstrip.startswith("```") or lstrip.startswith("~~~")


def _strip_terminal_marker_prefix(stripped: str) -> str:
    for prefix in ("+ ", "- ", "> "):
        if stripped.startswith(prefix):
            return stripped[len(prefix):].lstrip()
    if stripped.startswith(("+", "-")):
        return stripped[1:].lstrip()
    if stripped.startswith("**"):
        return stripped[2:].lstrip()
    return stripped


def _fence_state_unbalanced(lines: list[str], ignored_lines: set[int]) -> bool:
    fence_count = 0
    for idx, line in enumerate(lines):
        if idx in ignored_lines:
            continue
        if _is_fence_line(line):
            fence_count += 1
    return fence_count % 2 == 1


def _final_terminal_marker_from_line(
    raw_line: str,
    line_no: int,
    *,
    allow_prefixed_marker: bool = False,
) -> dict | None:
    if raw_line.startswith((" ", "\t")):
        return None
    if _is_diff_context_line(raw_line) and not allow_prefixed_marker:
        return None
    stripped = raw_line.strip()
    if not stripped:
        return None
    if allow_prefixed_marker:
        stripped = _strip_terminal_marker_prefix(stripped)
        if not stripped:
            return None
    signoff = _completion_signoff_marker(stripped, line_no)
    if signoff:
        return signoff
    match = FINAL_TERMINAL_MARKER_RE.match(stripped)
    if not match:
        return None
    return {
        "line": line_no,
        "kind": match.group(1),
        "text": _strip_marker_decoration(match.group(2))[:1000],
    }


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
    return {key: identity.get(key) for key in ("pid", "lstart", "comm") if identity.get(key)}


def _load_identity(raw: str | None) -> dict | None:
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _comm_base(value: object) -> str:
    text = str(value or "").strip().lower()
    if text.startswith("(") and ")" in text:
        text = text[1:text.find(")")]
    # A comm may be a bare name ("grok", "(grok-0.2.11-maco)") or a full
    # executable path ("/opt/homebrew/.../python"); take the basename so a path
    # tokenizes to its program name rather than an empty leading token.
    text = text.rsplit("/", 1)[-1]
    match = re.match(r"[a-z0-9_]+", text)
    return match.group(0) if match else ""


def _comm_matches(expected: object, actual: object) -> bool:
    expected_base = _comm_base(expected)
    actual_base = _comm_base(actual)
    return bool(
        expected_base
        and actual_base
        and (expected_base.startswith(actual_base) or actual_base.startswith(expected_base))
    )


def worker_alive(pid: int | None, expected_identity: dict | None) -> tuple[bool, str, dict | None]:
    if not pid:
        return False, "no_pid", None
    current = goalflight_ledger.process_identity(pid)
    if current is None:
        return False, "dead", None
    if expected_identity:
        if expected_identity.get("pid") and int(expected_identity["pid"]) != int(pid):
            return False, "identity_pid_mismatch", current

        expected_comm = expected_identity.get("comm")
        actual_comm = current.get("comm")

        expected_lstart = expected_identity.get("lstart")
        actual_lstart = current.get("lstart")
        if expected_lstart and actual_lstart:
            if actual_lstart != expected_lstart:
                return False, "pid_reused_lstart", current
            # lstart is a SECOND-granularity wall-clock string, so a pid reused
            # within the same formatted second yields an identical lstart. Trust
            # lstart as the primary anti-reuse key, but when comm is available on
            # both sides require comm-base compatibility too, so a same-second
            # reuse by a genuinely DIFFERENT process (different base comm) is
            # caught. _comm_matches is form-tolerant (base-token prefix), so a
            # cosmetic comm change ("grok" vs "(grok-0.2.11-maco)") at the same
            # lstart still reads live -- preserving the Mode B fix.
            if expected_comm and actual_comm and not _comm_matches(expected_comm, actual_comm):
                return False, "pid_reused_lstart_comm", current
            return True, "live", current

        if not expected_comm:
            missing = ["lstart", "comm"] if not expected_lstart else ["comm"]
            return True, "identity_inconclusive_missing_expected_" + "_".join(missing), current
        if not actual_comm:
            missing = ["lstart", "comm"] if not actual_lstart else ["comm"]
            return True, "identity_inconclusive_missing_current_" + "_".join(missing), current
        if not _comm_matches(expected_comm, actual_comm):
            return False, "pid_reused_comm", current
        return True, "live", current
    return True, "identity_inconclusive", current


def extract_markers(path: Path, max_bytes: int = 10 * 1024 * 1024,
                    ignore_prefix_lines: list[str] | None = None) -> tuple[list[dict], int]:
    if not path.exists():
        return [], 0
    size = path.stat().st_size
    start = max(0, size - max_bytes)
    prompt_prefix = ignore_prefix_lines or []
    markers: list[dict] = []
    in_fence = False
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
        if _is_fence_line(line):
            in_fence = not in_fence
            continue
        if in_fence and not ignore_fences:
            continue
        match = MARKER_RE.match(stripped)
        if match:
            markers.append({"line": idx, "kind": match.group(1), "text": match.group(2)[:1000]})
            continue
        signoff = _completion_signoff_marker(stripped, idx)
        if signoff:
            markers.append(signoff)
    return markers, size


# Worker markers the watcher bridges into the controller's mail inbox: a worker
# blocking on one of these is "you have mail" the controller should see on its next
# status check. (They are also terminal markers — the worker stops and waits — so
# each is normally emitted once.)
WORKER_MAIL_MARKER_KINDS = frozenset({"USER-NEED", "USER-CONFIRM", "BLOCKED"})

# Sentinel parked in the dedup set after any mail-layer failure: the bridge then
# no-ops for the rest of the watcher run. A real (type, text) key can never equal
# it (no message type is the empty marker below), so it cannot collide.
_BRIDGE_DISABLED = ("\x00bridge-disabled", "")


def post_worker_mail(dispatch_id: str, markers: list[dict], posted_keys: set) -> None:
    """Best-effort: post a worker's USER-NEED / USER-CONFIRM / BLOCKED markers as
    envelopes into the dispatch's mail inbox, so the controller's read-side status
    mail hint surfaces them with the question/blocker text.

    Liveness comes first and the bridge must never stall or storm the poll loop:
    - the common poll (no urgent marker) returns immediately and imports nothing —
      so the watcher's startup/steady path never touches the mail layer;
    - the inbox is read at most ONCE, lazily, when the first fresh urgent marker
      appears (rare; these markers are terminal), then the in-memory ``posted_keys``
      set short-circuits every later poll — no per-poll inbox scan;
    - each key is marked BEFORE the disk write, so a failed post can never re-attempt
      the same I/O every poll;
    - the FIRST mail-layer failure disables the bridge for the rest of the run.
    """
    if _BRIDGE_DISABLED in posted_keys:
        return
    try:
        urgent = [m for m in markers if m.get("kind") in WORKER_MAIL_MARKER_KINDS]
        if not urgent:
            return
        import goalflight_messages as gm  # lazy: the watcher must not hard-depend on mail

        messages_dir = gm.default_messages_dir()
        inbox = gm.inbox_path(messages_dir, dispatch_id)
        if inbox.exists() and not inbox.is_file():
            # Non-regular inbox (FIFO/device): read_envelopes()'s read or
            # post_message()'s open("a") could block the watcher's liveness loop
            # FOREVER — the broad except below can't catch a hang. Same hang class
            # as the read-side collect_inbox_paths guard. Refuse and disable the
            # bridge for the run; liveness must never wait on the mail layer.
            # is_file()/exists() are non-blocking stat()s (open() is what blocks).
            posted_keys.add(_BRIDGE_DISABLED)
            return
        inbox_seen: set | None = None  # loaded once, only on a fresh urgent marker (restart-safe dedup)
        for m in urgent:
            mtype = gm.MARKER_TO_TYPE.get(m["kind"], "blocked")
            text = _strip_marker_decoration(str(m.get("text") or "")).strip()
            key = (mtype, text)
            if key in posted_keys:
                continue
            if inbox_seen is None:
                inbox_seen = {
                    (str(e.get("type")), str((e.get("payload") or {}).get("text") or "").strip())
                    for e in gm.read_envelopes(inbox)
                }
            posted_keys.add(key)  # mark BEFORE I/O: a failed post must not retry every poll
            if key in inbox_seen:
                continue  # already delivered in a prior run; marked above, skip the re-post
            gm.post_message(
                dispatch_id=dispatch_id,
                msg_type=mtype,
                payload={"text": text},
                messages_dir=messages_dir,
                source={"node": "local", "adapter": "watcher", "transport": "marker-bridge"},
            )
    except Exception:
        posted_keys.add(_BRIDGE_DISABLED)  # one failure -> bridge off for this run; liveness first
        return


def _last_line_is_terminal_marker(
    path: Path,
    ignore_prefix_lines: list[str] | None = None,
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
    in_fence = False
    last_nonempty = ""
    last_nonempty_line = -1
    last_in_fence = False
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
        fence_line = _is_fence_line(line)
        if fence_line:
            in_fence = not in_fence
            if stripped:
                last_nonempty = stripped
                last_nonempty_line = idx
                last_in_fence = in_fence and not ignore_fences
            continue
        if in_fence and not ignore_fences:
            if stripped:
                last_nonempty = stripped
                last_nonempty_line = idx
                last_in_fence = True
            continue
        if stripped:
            last_nonempty = stripped
            last_nonempty_line = idx
            last_in_fence = False
    if not last_nonempty or last_in_fence:
        return None
    match = MARKER_RE.match(last_nonempty)
    if match and match.group(1) in TERMINAL_MARKERS:
        return {"line": last_nonempty_line, "kind": match.group(1), "text": match.group(2)[:1000]}
    signoff = _completion_signoff_marker(last_nonempty, last_nonempty_line)
    if signoff:
        return signoff
    return None


def _scan_final_terminal_marker(
    lines: list[str],
    *,
    prompt_echo_lines: set[int],
    echo_anchor_found: bool,
    prompt_line_set: set[str],
    suppress_unfenced_prompt_markers: bool,
    ignore_fences: bool,
) -> dict | None:
    in_fence = False
    in_hunk = False
    terminal: dict | None = None
    for idx, line in enumerate(lines, start=1):
        stripped = line.strip()
        if idx - 1 in prompt_echo_lines:
            continue
        if _is_fence_line(line):
            in_fence = not in_fence
            continue
        if in_fence and not ignore_fences:
            continue
        if in_hunk:
            if line.startswith((" ", "+", "-", "\\")):
                continue
            in_hunk = False
        if HUNK_HEADER_RE.match(line):
            in_hunk = True
            continue
        candidate = _final_terminal_marker_from_line(line, idx, allow_prefixed_marker=True)
        if candidate:
            if _is_unfenced_prompt_quoted_bare_marker(
                stripped,
                prompt_line_set=prompt_line_set,
                echo_anchor_found=echo_anchor_found,
                suppress_unfenced_prompt_markers=suppress_unfenced_prompt_markers,
            ):
                continue
            terminal = candidate
    return terminal


def _final_terminal_marker(
    path: Path,
    ignore_prefix_lines: list[str] | None = None,
    *,
    suppress_unfenced_prompt_markers: bool = True,
) -> dict | None:
    """Return the last terminal marker anywhere in the completed post-prompt tail.

    Live detection remains last-line-only. This reconciliation scan is for the
    worker-dead path, after no more output can arrive.
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
    )
    if not _fence_state_unbalanced(lines, prompt_echo_lines):
        return terminal
    fence_agnostic_terminal = _scan_final_terminal_marker(
        lines,
        prompt_echo_lines=prompt_echo_lines,
        echo_anchor_found=echo_anchor_found,
        prompt_line_set=prompt_line_set,
        suppress_unfenced_prompt_markers=suppress_unfenced_prompt_markers,
        ignore_fences=True,
    )
    if (
        fence_agnostic_terminal
        and (not terminal or fence_agnostic_terminal.get("line", -1) >= terminal.get("line", -1))
    ):
        return fence_agnostic_terminal
    return terminal


def main() -> int:
    parser = argparse.ArgumentParser(description="goal-flight compact log watcher")
    parser.add_argument("--pid", type=int, required=True)
    parser.add_argument("--tail", required=True)
    parser.add_argument("--status-json", required=True)
    parser.add_argument("--dispatch-id")
    parser.add_argument("--project-root")
    parser.add_argument("--task-ids")
    parser.add_argument("--agent", default="unknown")
    parser.add_argument("--poll-secs", type=float, default=2.0)
    parser.add_argument("--max-idle-secs", type=float, default=180.0)
    parser.add_argument("--cpu-epsilon", type=float, default=0.1)
    parser.add_argument("--pgid", type=int)
    parser.add_argument("--controller-pid", type=int)
    parser.add_argument("--detached", action="store_true",
                        help="Ignore launcher/controller pid liveness; worker pid identity is authoritative.")
    parser.add_argument("--worker-identity-json",
                        help="Process identity token captured at spawn; prevents PID-reuse false liveness.")
    parser.add_argument("--ignore-prompt-file",
                        help="Ignore marker lines appearing verbatim in this prompt file, so a worker's "
                             "echoed prompt can't trip the watcher on its own 'end with COMPLETE:' instruction.")
    parser.add_argument("--stay-after-terminal", action="store_true",
                        help="After a terminal marker, keep watching until the worker exits or this watcher is stopped.")
    args = parser.parse_args()

    ignore_prefix_lines: list[str] = []
    if args.ignore_prompt_file:
        _pf = Path(args.ignore_prompt_file)
        if _pf.exists():
            ignore_prefix_lines = [
                ln.strip()
                for ln in _pf.read_text(encoding="utf-8", errors="replace").splitlines()
            ]
    expected_identity = _load_identity(args.worker_identity_json)
    task_ids = _split_task_ids(args.task_ids)
    task_project_root = goalflight_task.resolve_project_root(args.project_root)

    tail = Path(args.tail)
    status_path = Path(args.status_json)
    if goalflight_compat.is_windows():
        payload = {
            "schema": "goalflight.status.v1",
            "dispatch_id": args.dispatch_id,
            "agent": args.agent,
            "worker_pid": args.pid,
            "detached": bool(args.detached),
            "state": "blocked_windows_dispatch",
            "reason": goalflight_compat.windows_watcher_skip(),
            "tail_path": str(tail),
            "updated_at": int(time.time()),
        }
        write_status(status_path, payload)
        print(json.dumps({"state": payload["state"], "reason": payload["reason"], "status_path": str(status_path)}, sort_keys=True))
        return 4
    last_size = -1
    # Idle accounting uses the sleep-excluding clock (active_monotonic):
    # macOS CLOCK_UPTIME_RAW / Linux CLOCK_MONOTONIC freeze across system
    # sleep, so a lid-close suspend does NOT count as worker idle time —
    # wall-clock deltas here produced phantom idle_timeout kills on wake
    # (same class as watch-dispatch-tail.sh's suspend-gap fix, 2026-06-09).
    # time.time() remains for epoch display fields (updated_at) only.
    last_change = active_monotonic()
    terminal = None
    markers: list[dict] = []
    exit_reason = "unknown"
    exit_code = 1
    wedge_streak = 0
    pgid = args.pgid or process_group_id(args.pid) or args.pid
    thresholds = LivenessThresholds(idle_timeout_s=args.max_idle_secs, cpu_epsilon_pct=args.cpu_epsilon)
    last_payload: dict | None = None
    terminal_seen: dict | None = None
    final_status_written = False
    working_breadcrumb_written = False

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
        nonlocal last_payload, final_status_written, working_breadcrumb_written
        if reason:
            payload["reason"] = reason
        if terminal_write:
            final_state, final_reason, _rate_limited = goalflight_terminal.terminal_rate_limit_outcome(
                payload.get("state"),
                payload.get("reason"),
                tail,
                terminal_marker_present=goalflight_terminal.terminal_marker_present(
                    payload.get("terminal_marker") or terminal_seen
                ),
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
                    original_state = payload.get("state")
                    payload["task_breadcrumb_error"] = terminal_error
                    payload["task_breadcrumb_failed_state"] = original_state
                    if payload.get("reason"):
                        payload["task_breadcrumb_failed_reason"] = payload.get("reason")
                    payload["state"] = BLOCKED_TASK_BREADCRUMB_STATE
                    payload["reason"] = "task_breadcrumb_error"
                    payload["liveness_state"] = goalflight_terminal.terminal_liveness_state(payload.get("state"))
        if terminal_write:
            payload["liveness_state"] = goalflight_terminal.terminal_liveness_state(payload.get("state"))
        write_status(status_path, payload)
        if terminal_write:
            ledger_error = _finish_existing_ledger(
                args.dispatch_id,
                payload.get("state"),
                payload.get("reason"),
                worker_still_alive=payload.get("worker_alive"),
            )
            if ledger_error:
                payload["ledger_finalize_error"] = ledger_error
                write_status(status_path, payload)
        last_payload = dict(payload)
        if terminal_write:
            final_status_written = True
        return terminal_error

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
        write_reason = payload.get("reason") if payload.get("state") == "rate_limited" else reason
        with contextlib.suppress(Exception):
            write_payload(payload, reason=write_reason, terminal_write=True)

    def handle_signal(signum: int, _frame) -> None:
        name = getattr(signal.Signals(signum), "name", str(signum))
        flush_terminal_status(f"signal:{name}")
        raise SystemExit(128 + signum)

    for signame in ("SIGTERM", "SIGHUP", "SIGINT"):
        sig = getattr(signal, signame, None)
        if sig is not None:
            signal.signal(sig, handle_signal)
    atexit.register(lambda: flush_terminal_status("watcher_exit"))

    posted_mail_keys: set = set()  # per-run dedup for the worker->controller mail bridge
    while True:
        markers, size = extract_markers(tail, ignore_prefix_lines=ignore_prefix_lines)
        # Bridge worker USER-NEED/USER-CONFIRM/BLOCKED markers into the dispatch
        # inbox so the controller's status mail hint surfaces them. Runs BEFORE the
        # terminal-exit checks below so a need is posted even on the iteration the
        # watcher resolves (these markers are themselves terminal). Best-effort.
        post_worker_mail(args.dispatch_id, markers, posted_mail_keys)
        if size != last_size:
            last_size = size
            last_change = active_monotonic()
        now = time.time()
        seconds_since_event = active_monotonic() - last_change
        terminal = _last_line_is_terminal_marker(tail, ignore_prefix_lines=ignore_prefix_lines)
        if terminal:
            # Stability recheck (minimal gap protection): if bytes arrive within a short
            # window after a terminal marker became the last line, it was a mid-output
            # emission; discard and keep watching. Genuine sign-off is worker's final act.
            time.sleep(0.05)
            terminal = _last_line_is_terminal_marker(tail, ignore_prefix_lines=ignore_prefix_lines)
        worker_is_alive, identity_reason, current_identity = worker_alive(args.pid, expected_identity)
        if worker_is_alive:
            pgid = args.pgid or process_group_id(args.pid) or pgid
            cpu_pct = pgroup_cpu_pct(pgid)
        else:
            cpu_pct = 0.0
        low_power_relax = (
            worker_is_alive
            and cpu_confirmed_idle(cpu_pct, args.cpu_epsilon)
            and args.max_idle_secs > 0
            and seconds_since_event >= args.max_idle_secs
            and system_starved()
        )
        liveness_state = classify_liveness(
            worker_is_alive,
            cpu_pct,
            seconds_since_event,
            thresholds,
            low_power_relax=low_power_relax,
        )
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
            "tail_path": str(tail),
            "markers": markers[-20:],
            "last_marker": markers[-1] if markers else None,
            "terminal_marker": terminal,
            "state": "running_quiet" if liveness_state == "running_quiet" else "running",
            "updated_at": int(now),
        }
        if low_power_relax:
            payload["low_power_relax"] = True
        if terminal:
            terminal_seen = terminal
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
        if terminal_seen and not post_terminal_wait:
            payload["state"] = terminal_state
            exit_code = _exit_code_for_state(payload["state"])
            exit_reason = terminal_reason
            write_payload(payload, reason=terminal_reason, terminal_write=True)
            exit_code = _exit_code_for_state(payload["state"])
            exit_reason = payload.get("reason", exit_reason)
            break
        if (
            args.controller_pid
            and not alive(args.controller_pid)
            and _controller_dead_is_terminal(detached=bool(args.detached))
        ):
            payload["state"] = "orphaned"
            exit_reason = "controller_dead"
            exit_code = 3
            write_payload(payload, reason=exit_reason, terminal_write=True)
            exit_code = _exit_code_for_state(payload["state"])
            exit_reason = payload.get("reason", exit_reason)
            break
        if not worker_is_alive:
            reconciled = _final_terminal_marker(tail, ignore_prefix_lines=ignore_prefix_lines)
            if reconciled:
                terminal_seen = reconciled
                payload["terminal_marker"] = terminal_seen
                payload["state"] = _marker_state(terminal_seen)
                exit_reason = f"marker:{terminal_seen['kind']}:final_reconciliation"
                exit_code = _exit_code_for_state(payload["state"])
            else:
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
                    "worker_dead_no_terminal_marker"
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
            wedge_streak += 1
            if wedge_streak >= WEDGE_CONFIRM_SAMPLES:
                if post_terminal_wait:
                    payload["state"] = terminal_state
                    exit_reason = f"{terminal_reason}:post_terminal_idle_timeout"
                    exit_code = _exit_code_for_state(payload["state"])
                else:
                    payload["state"] = "idle_timeout"
                    exit_reason = "idle_timeout"
                    exit_code = 2
                    if apply_tail_quota_status(payload, previous_state="idle_timeout", previous_reason=exit_reason):
                        exit_reason = payload["reason"]
                        exit_code = 1
                write_payload(payload, reason=exit_reason, terminal_write=True)
                exit_code = _exit_code_for_state(payload["state"])
                exit_reason = payload.get("reason", exit_reason)
                break
        else:
            wedge_streak = 0
        if post_terminal_wait:
            payload["state"] = "running_after_terminal"
            payload["terminal_pending_state"] = terminal_state
            write_payload(payload, reason=f"{terminal_reason}:worker_alive", terminal_write=False)
        else:
            write_payload(payload)
        time.sleep(args.poll_secs)

    print(json.dumps({"state": payload["state"], "reason": exit_reason, "status_path": str(status_path)}, sort_keys=True))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
