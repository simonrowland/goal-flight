#!/usr/bin/env python3
"""Run one ACP prompt with compact status, capacity, and ledger records.

Timeout model (two signals):

- ``--progress-stall-s`` (default 300) — **operative stall detector**. By
  default, exits the runner and leaves the worker alive so the host is woken and
  can re-attach. ``--stall-kill`` restores the old kill-on-stall behavior. Raw
  vendor noise does not reset it. Tune this for the worker's expected per-event
  quiet pattern.

- ``--max-tool-s`` (default 3600, the harness clamp) — **wall-clock safety net**
  for one outstanding tool call. Activity-naive. Use a lower value only when you
  know the task is fast; do not use it as the primary stuck-detection knob.

- ``--max-consecutive-tool-errors`` (default 5) and ``--max-acp-events``
  (default 5000) — **busy-loop safety nets**. These cap workers that keep
  emitting events but do not converge. They are runaway backstops, not
  performance limits.

Explicit ``--max-tool-s`` on the command line (or ``GOALFLIGHT_MAX_TOOL_S``)
still wins over the default.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import importlib.util
import inspect
import io
import json
import os
from pathlib import Path
import signal
import shutil
import subprocess
import sys
import time
from typing import Any, Callable
import uuid

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
import goalflight_compat  # noqa: E402
import goalflight_dispatch_paths  # noqa: E402
import goalflight_steer_mailbox  # noqa: E402
from goalflight_watch import (  # noqa: E402
    BLOCKING_TERMINAL_MARKERS,
    SUCCESS_TERMINAL_MARKERS,
    TERMINAL_MARKERS,
)

DEFAULT_REMOTE_TURN_SILENCE_S = 1200.0
DEFAULT_REMOTE_TURN_CANCEL_GRACE_S = 15.0
DEFAULT_MAX_TOOL_S = 3600.0
DEFAULT_MAX_CONSECUTIVE_TOOL_ERRORS = 5
DEFAULT_MAX_ACP_EVENTS = 5000
DEFAULT_STALL_WAKE_CAP = 3
DEFAULT_BETWEEN_TURN_STEER_GRACE_S = 10.0
DEFAULT_EMPTY_BETWEEN_TURN_STEER_POLL_S = 0.25
DEFAULT_USER_CONFIRM_TIMEOUT_S = 600.0
AGENT_STDERR_CAPTURE_BYTES = 64 * 1024
AGENT_STDERR_ERROR_TAIL_CHARS = 1000
LIVENESS_PROFILES = {"remote_api", "local_compute", "hybrid"}
STEER_FILE_ALLOW_ENV = "GOALFLIGHT_ALLOW_EXTERNAL_STEER_FILE"
PROMPT_FILE_ENV = "GOALFLIGHT_PROMPT_FILE"
USER_CONFIRM_MARKER = "USER-CONFIRM"
ACTIONABLE_BLOCKING_TERMINAL_MARKERS = BLOCKING_TERMINAL_MARKERS - {
    USER_CONFIRM_MARKER,
}


def _acp_reexec_target() -> str | None:
    """Return the python path to re-exec into for acp, or None to stay put."""
    if importlib.util.find_spec("acp") is not None:
        return None
    # Env interpreter selectors are accepted-watch per the SC-13 sweep: command
    # source overrides, but outside the source/write/safety-disable predicate.
    override = os.environ.get("GOALFLIGHT_ACP_PYTHON")
    target = Path(override).expanduser() if override else Path.home() / ".goal-flight/venvs/acp-0.10/bin/python"
    if not target.exists():
        return None
    target_path = os.path.normpath(str(target))
    current_path = os.path.normpath(sys.executable)
    if target_path == current_path:
        return None
    return str(target)


def _ensure_acp_sdk_python() -> None:
    if goalflight_compat.is_windows():
        return
    target = _acp_reexec_target()
    if target is not None:
        os.execv(target, [target, *sys.argv])

import goalflight_capacity
import goalflight_ledger
from goalflight_rate_pressure import RATE_LIMIT_PATTERNS
from goalflight_adapter_readiness import (
    load_manifest,
    manifest_candidates,
    validate_acp_dispatch_readiness,
    validate_os_sandbox_request,
)
from goalflight_acp_boundaries import permission_boundary_warning
from goalflight_codex_sandbox import codex_workspace_write_args
from goalflight_acp_client import (
    AcpConnection,
    AcpError,
    AcpLivenessActivity,
    MAX_PERMISSION_ROUTER_DECISIONS,
    PERMISSION_ALLOW,
    PERMISSION_DENY,
    cleanup_ghosts,
    mark_connection_detached,
    default_permission_policy,
    spawn_acp_connection,
)
import goalflight_acp_permits as permits
import re as _re


_ACP_PERMISSION_READ_ONLY_AGENTS = frozenset({"claude", "claude-acp"})


def acp_permission_read_only_supported(agent: str | None) -> bool:
    """Whether this adapter routes writes through ACP request_permission.

    Keep this allowlist explicit. Cursor/Grok bypass the permission gate for
    writes, and Codex is deliberately excluded because its existing sandbox
    behavior is outside the fallback introduced for Claude ACP.
    """
    return str(agent or "").strip().lower() in _ACP_PERMISSION_READ_ONLY_AGENTS


def _read_only_permission_policy(tool_call, options, cwd):
    """Deny all permission-gated writes; retain the normal read-safe policy."""
    try:
        from goalflight_acp_client import _WRITE_KINDS, _tc_get
    except ImportError:
        return PERMISSION_DENY
    if str(_tc_get(tool_call, "kind") or "") in _WRITE_KINDS:
        return PERMISSION_DENY
    return default_permission_policy(tool_call, options, cwd)


_CODEX_CONTEXT_MODE_TABLE_RE = _re.compile(
    r"""(?mx)
    ^\s*\[\s*mcp_servers\s*\.\s*
    (?:"context-mode"|'context-mode'|context-mode)
    \s*\]\s*(?:\#.*)?$
    """
)


def _codex_context_mode_defined(env: dict[str, str]) -> bool:
    """Fail-safe local check used even when dispatcher integration is absent."""
    raw_home = env.get("CODEX_HOME")
    home = Path(raw_home).expanduser() if raw_home else Path.home() / ".codex"
    try:
        config = (home / "config.toml").read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return False
    return _CODEX_CONTEXT_MODE_TABLE_RE.search(config) is not None


def _tool_call_title(tool_call: Any) -> str:
    """Extract a tool-call title without depending on the client's _tc_get helper."""
    for key in ("title", "Title"):
        try:
            value = tool_call.get(key) if hasattr(tool_call, "get") else getattr(tool_call, key, None)
        except Exception:
            value = None
        if isinstance(value, str) and value:
            return value
    return ""


_INLINE_PERMISSION_MARKER = "PERMISSION-PENDING"
_INLINE_PERMISSION_STR_MAX = 500
_INLINE_PERMISSION_LIST_MAX = 20


def _compact_permission_str(value: Any, limit: int = _INLINE_PERMISSION_STR_MAX) -> str:
    text = str(value or "")
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _compact_permission_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for item in value[:_INLINE_PERMISSION_LIST_MAX]:
        out.append(_compact_permission_str(item))
    return out


def _select_request_allow_option(options: Any) -> str | None:
    if not isinstance(options, list):
        return None
    for preferred in ("allow_once", "allow_always"):
        for option in options:
            if not isinstance(option, dict):
                continue
            if option.get("kind") == preferred and option.get("option_id"):
                return str(option["option_id"])
    for option in options:
        if not isinstance(option, dict):
            continue
        kind = option.get("kind")
        option_id = option.get("option_id")
        if option_id and isinstance(kind, str) and kind.startswith("allow_"):
            return str(option_id)
    return None


def _inline_permission_relay_decision(record: dict[str, Any]) -> tuple[str, str | None, str]:
    """Apply the same conservative boundary policy to an inline IPC request.

    Safe in-cwd/read requests are allowed. Boundary-crossing, network/shell,
    unverified writes, and unknown future kinds get an explicit deny so the
    worker proceeds without waiting for the inline timeout.
    """
    kind = str(record.get("kind") or "")
    locations = _compact_permission_list(record.get("locations"))
    targets_outside_cwd = _compact_permission_list(record.get("targets_outside_cwd"))
    if targets_outside_cwd:
        return permits.DECISION_DENY, None, "target_outside_cwd"
    if kind in {"fetch", "execute"}:
        return permits.DECISION_DENY, None, f"{kind}_requires_controller"
    if kind in {"edit", "delete", "move"} and not locations:
        return permits.DECISION_DENY, None, "unverified_write_locations"
    if kind in {"edit", "delete", "move", "", "read", "search", "think"}:
        option_id = _select_request_allow_option(record.get("options"))
        if option_id:
            return permits.DECISION_ALLOW, option_id, "in_cwd_safe"
        return permits.DECISION_DENY, None, "no_allow_option"
    return permits.DECISION_DENY, None, "unknown_kind"


def _inline_permission_summary(
    record: dict[str, Any],
    *,
    decision: str,
    option_id: str | None,
    reason: str,
) -> dict[str, object]:
    summary: dict[str, object] = {
        "key": _compact_permission_str(record.get("key"), 160),
        "tool": _compact_permission_str(record.get("tool_call_id"), 160),
        "title": _compact_permission_str(record.get("title")),
        "kind": _compact_permission_str(record.get("kind"), 80),
        "locations": _compact_permission_list(record.get("locations")),
        "targets_outside_cwd": _compact_permission_list(record.get("targets_outside_cwd")),
        "decision": decision,
        "reason": reason,
    }
    if option_id:
        summary["option_id"] = _compact_permission_str(option_id, 160)
    return summary


def _inline_permission_marker_text(summary: dict[str, object]) -> str:
    marker = {
        "key": summary.get("key"),
        "tool": summary.get("tool"),
        "title": summary.get("title"),
        "kind": summary.get("kind"),
        "decision": summary.get("decision"),
        "reason": summary.get("reason"),
    }
    return json.dumps(marker, sort_keys=True)


def make_title_allow_policy(
    patterns: list[Any],
    base: Callable[[Any, list[Any], str | None], str] | None = None,
) -> Callable[[Any, list[Any], str | None], str]:
    """Wrap a base permission policy with an allow-by-title-pattern fast-path
    layered AFTER the base policy's hard safety gates.

    Patterns are pre-compiled regex objects (anything with ``.search(s)``). The
    layering is intentional:

      1. Hard safety gates ALWAYS run first — no title regex can bypass them:
           - target outside cwd                        → escalate
           - destructive shell/network (kind=execute, kind=fetch) without OS
             sandbox enabled → falls through to base, which escalates
      2. For tool-calls that pass (1), title regex fast-paths the
         already-permittable subset (read-safe, in-cwd writes with locations).
      3. Anything not matched by title falls through to ``base``
         (defaults to ``default_permission_policy``).

    Original use case: a dispatched chunk's acceptance criteria includes
    "run ``./tests/run.sh``" — the orchestrator passes a pattern like
    ``^./tests/run\\.sh$`` to fast-path that exact shape. The pattern is
    PRECISE and intentional, not broad.

    Why this layering matters (sweep B P1, 2026-05-27): the previous
    implementation ran title regex BEFORE hard gates, so a broad ``.*``
    "YOLO" pattern would auto-allow ANY tool call — including
    destructive execute, network fetch, and writes outside cwd. The pattern
    became a silent over-authorization rather than a scope fast-path. The
    new layering ensures even ``.*`` cannot bypass the outside-cwd gate or
    sandbox-less execute/fetch gate; broad patterns only ever fast-path the
    safe kinds. For workers that legitimately need execute/fetch, enable
    OS sandbox (--os-sandbox read-only) — the sandbox is the backstop, and
    ``permission_policy_for_dispatch`` auto-allows execute/fetch when
    sandbox is on.

    R26 — restores the original ACP-passthrough design: the orchestrator
    exercises discretion (approves authorized scope) and escalates only
    requests that genuinely need user judgment (destructive, out-of-scope,
    ambiguous).
    """
    base_policy = base or default_permission_policy

    def policy(tool_call: Any, options: list[Any], cwd: str | None) -> str:
        # Defer hard-gate evaluation to a helper that mirrors
        # default_permission_policy's escalation rules WITHOUT the
        # final permittable-kind allows. If the base policy would
        # escalate on a hard-gate basis, title regex must NOT bypass.
        if _hard_gate_escalates(tool_call, cwd):
            return base_policy(tool_call, options, cwd)
        title = _tool_call_title(tool_call)
        for pattern in patterns:
            try:
                if pattern.search(title):
                    return PERMISSION_ALLOW
            except Exception:
                continue
        return base_policy(tool_call, options, cwd)

    return policy


def _hard_gate_escalates(tool_call: Any, cwd: str | None) -> bool:
    """Return True if the tool-call would escalate on hard-gate grounds
    that title regex must not bypass.

    Hard gates (mirrors default_permission_policy's full escalate path —
    sweep B P1 follow-up tightened from 2 gates to 4):
      - target outside cwd                                  → True
      - kind ∈ {fetch, execute}                             → True
      - kind ∈ {edit, delete, move} with no locations
        (write whose target can't be proven in-cwd)         → True
      - unknown / future kinds (anything not in the
        {fetch, execute, edit, delete, move, "", read,
         search, think} allowlist)                          → True

    Read-safe kinds (`""`, `read`, `search`, `think`) and
    located in-cwd writes (`edit`, `delete`, `move` with locations)
    are eligible for title-allow fast-path; everything else falls
    through to the base policy.

    The base policy is responsible for the final allow/escalate
    decision. When OS sandbox is on,
    ``permission_policy_for_dispatch`` is the base — its sandbox-aware
    behavior re-evaluates after this check, so execute/fetch with
    sandbox on still gets auto-allowed by the base.

    Import failure (extremely unlikely): treat the call as hard-gated
    so we defer to the base instead of fast-pathing (fail-closed).
    """
    try:
        from goalflight_acp_client import (
            _READ_SAFE_KINDS,
            _WRITE_KINDS,
            _targets_outside_cwd,
            _tc_get,
            _tool_call_locations,
        )
    except ImportError:
        return True  # fail-closed
    if _targets_outside_cwd(tool_call, cwd):
        return True
    kind = str(_tc_get(tool_call, "kind") or "")
    if kind in {"fetch", "execute"}:
        return True
    if kind in _WRITE_KINDS:
        locations = _tool_call_locations(tool_call)
        if not locations:
            return True  # write with no targets we can verify in-cwd
        return False  # in-cwd write — title-allow may fast-path
    if kind in _READ_SAFE_KINDS:
        return False  # read-safe — title-allow may fast-path
    # Unknown / future kind — refuse to fast-path.
    return True
from goalflight_liveness import (
    active_monotonic,
    cpu_confirmed_idle,
    heartbeat_wedge_decision,
    IdleLivenessGate,
    pgroup_cpu_pct,
    progress_stall_decision,
    process_group_id,
    system_sleep_pause_note,
    system_sleep_pause_s,
    write_status,
)
from goalflight_os_sandbox import (
    OS_SANDBOX_ARG_CHOICES,
    OS_SANDBOX_OFF,
    OsSandboxError,
    canonical_os_sandbox,
    prepare_os_sandbox_command,
    preflight_os_sandbox,
)
from goalflight_profile import dispatch_env
from goalflight_startup_gate import StartupGate
from acp_runner import PromptResult, extract_markers, has_actionable_marker_values, run_prompt


class _SigtermCancelBridge:
    """Convert process termination signals into asyncio task cancellation.

    The default SIGTERM action exits the interpreter immediately, bypassing
    coroutine ``finally`` blocks. ACP dispatch owns lease/ledger finalization in
    ``run_acp_dispatch``; cancelling the main task lets that finalizer run.
    """

    def __init__(self, loop: asyncio.AbstractEventLoop, task: asyncio.Task | None):
        self.loop = loop
        self.task = task
        self.received = False
        self.signum: int | None = None
        self._installed = False
        self._prior_handlers: dict[int, Any] = {}
        self._used_loop_handler: dict[int, bool] = {}
        self._installed_signals: list[int] = []

    def _cancel(self, signum: int) -> None:
        self.received = True
        self.signum = signum
        if self.task is not None and not self.task.done():
            self.task.cancel()

    def install(self) -> None:
        for signame in ("SIGTERM", "SIGINT"):
            sig = getattr(signal, signame, None)
            if sig is None:
                continue
            self._prior_handlers[sig] = signal.getsignal(sig)
            try:
                self.loop.add_signal_handler(sig, self._cancel, sig)
            except (NotImplementedError, RuntimeError, ValueError):
                try:
                    signal.signal(sig, lambda signum, _frame: self._cancel(signum))
                except (ValueError, OSError):
                    continue
                self._used_loop_handler[sig] = False
            else:
                self._used_loop_handler[sig] = True
            self._installed_signals.append(sig)
        self._installed = bool(self._installed_signals)

    def restore(self) -> None:
        if not self._installed:
            return
        for sig in self._installed_signals:
            if self._used_loop_handler.get(sig):
                with contextlib.suppress(Exception):
                    self.loop.remove_signal_handler(sig)
            if sig in self._prior_handlers:
                with contextlib.suppress(Exception):
                    signal.signal(sig, self._prior_handlers[sig])
        self._installed = False


def _dispatch_base_dir() -> Path:
    return goalflight_dispatch_paths.dispatch_base_dir()


def _default_status_json_path(dispatch_id: str) -> Path:
    base = _dispatch_base_dir()
    base.mkdir(parents=True, exist_ok=True)
    return base / f"{status_filename_segment(dispatch_id)}.status.json"


def _resolve_status_json_path(configured: str | None, dispatch_id: str) -> Path:
    return Path(configured).expanduser() if configured else _default_status_json_path(dispatch_id)


def _agent_stderr_log_path(status_path: Path) -> Path:
    if status_path.name == "status.json":
        return status_path.with_name("agent-stderr.log")
    if status_path.name.endswith(".status.json"):
        stem = status_path.name[: -len(".status.json")]
    else:
        stem = status_path.stem
    return status_path.with_name(f"{stem}.agent-stderr.log")


def _tail_text(path: Path, *, chars: int) -> str | None:
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    if not raw:
        return None
    text = raw[-max(chars * 4, chars):].decode("utf-8", errors="replace")
    return text[-chars:]


class AgentStderrCapture:
    def __init__(self, path: Path, *, max_bytes: int = AGENT_STDERR_CAPTURE_BYTES) -> None:
        self.path = path
        self.max_bytes = max(1, int(max_bytes))
        self._buffer = bytearray()

    async def attach(self, conn: AcpConnection) -> None:
        old_task = getattr(conn, "_stderr_task", None)
        if old_task is not None and not old_task.done():
            old_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await old_task
        stream = getattr(getattr(conn, "proc", None), "stderr", None)
        if stream is None:
            return
        task = asyncio.create_task(self._drain(stream))
        with contextlib.suppress(Exception):
            setattr(conn, "_stderr_task", task)

    def tail_text(self, chars: int = AGENT_STDERR_ERROR_TAIL_CHARS) -> str | None:
        return _tail_text(self.path, chars=chars)

    async def _drain(self, stream: asyncio.StreamReader) -> None:
        try:
            while True:
                chunk = await stream.read(AGENT_STDERR_CAPTURE_BYTES)
                if not chunk:
                    break
                self._append(chunk)
                await asyncio.sleep(0)
        except asyncio.CancelledError:
            raise
        except Exception:
            pass

    def _append(self, chunk: bytes) -> None:
        if not chunk:
            return
        self._buffer.extend(chunk)
        if len(self._buffer) > self.max_bytes:
            del self._buffer[: len(self._buffer) - self.max_bytes]
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            tmp.write_bytes(bytes(self._buffer))
            tmp.replace(self.path)
        except OSError:
            pass


def _error_with_agent_stderr_tail(error: object, stderr_tail: str) -> dict[str, object]:
    if isinstance(error, dict):
        out = dict(error)
        out["agent_stderr_tail"] = stderr_tail
        return out
    if error is None:
        return {"agent_stderr_tail": stderr_tail}
    return {"message": str(error), "agent_stderr_tail": stderr_tail}


def _attach_agent_stderr_tail(payload: dict, capture: AgentStderrCapture) -> None:
    state = str(payload.get("state") or "")
    if state not in {"failed", "wedged", "tool_timeout", "remote_turn_silence", "stalled"}:
        return
    tail = capture.tail_text()
    if not tail:
        return
    payload["error"] = _error_with_agent_stderr_tail(payload.get("error"), tail)


def _default_steer_file(dispatch_id: str) -> Path:
    base = _dispatch_base_dir()
    base.mkdir(parents=True, exist_ok=True)
    return base / f"{dispatch_id}.steer.jsonl"


def _ipc_allowed_roots() -> list[Path]:
    dispatch_base = _dispatch_base_dir()
    return [dispatch_base.parent, dispatch_base]


def _resolve_steer_file(cfg: argparse.Namespace, dispatch_id: str) -> tuple[Path, str]:
    configured = getattr(cfg, "steer_file", None)
    if configured:
        return Path(str(configured)).expanduser(), "cli"
    env_value = os.environ.get("GOALFLIGHT_STEER_FILE")
    if env_value:
        path = Path(env_value).expanduser()
        if goalflight_compat.path_is_under(path, _ipc_allowed_roots()):
            goalflight_compat.env_override_warning(
                "GOALFLIGHT_STEER_FILE",
                "active",
                "path_under_state_root",
                source=path,
            )
            return path, "env:state_root"
        if os.environ.get(STEER_FILE_ALLOW_ENV) == "1":
            goalflight_compat.env_override_warning(
                "GOALFLIGHT_STEER_FILE",
                "active",
                f"{STEER_FILE_ALLOW_ENV}=1",
                source=path,
            )
            return path, "env:allow"
        goalflight_compat.env_override_warning(
            "GOALFLIGHT_STEER_FILE",
            "ignored",
            "outside_state_root",
            source=path,
        )
        roots = ", ".join(str(root) for root in _ipc_allowed_roots())
        raise ValueError(
            f"GOALFLIGHT_STEER_FILE must be under a dispatch/state directory "
            f"({roots}) unless {STEER_FILE_ALLOW_ENV}=1"
        )
    return _default_steer_file(dispatch_id), "default"


def _prompt_file_preamble() -> str:
    from goalflight_dispatch import PROMPT_FILE_PREAMBLE

    return PROMPT_FILE_PREAMBLE


def _resolve_original_prompt_file(cfg: argparse.Namespace) -> str | None:
    configured = getattr(cfg, "original_prompt_file", None)
    prompt_path = configured or getattr(cfg, "prompt", None)
    if not prompt_path:
        return None
    return str(Path(str(prompt_path)).expanduser().resolve())


def _worker_spawn_env(cfg: argparse.Namespace, original_prompt_file: str | None) -> dict[str, str]:
    install_slot = getattr(cfg, "install_slot", None)
    spawn_env = dispatch_env(cfg.agent, install_slot)
    if original_prompt_file:
        spawn_env[PROMPT_FILE_ENV] = original_prompt_file
    else:
        spawn_env.pop(PROMPT_FILE_ENV, None)
    return spawn_env


def _prompt_with_original_prompt_file_preamble(base_prompt: str, original_prompt_file: str | None) -> str:
    if not original_prompt_file:
        return base_prompt
    return f"{_prompt_file_preamble()}\n\n{base_prompt}"


def _read_steer_entries(path: Path) -> list[dict]:
    return goalflight_steer_mailbox.read_steer_entries(path)


def _steer_ack_seqs(markers: dict[str, list[str]]) -> set[int]:
    seqs: set[int] = set()
    for value in markers.get("STEER-ACK") or []:
        try:
            seqs.add(int(str(value or "").split()[0]))
        except (IndexError, ValueError):
            pass
    return seqs


def _pending_steer_entries(mailbox: Path, seen_seqs: set[int]) -> list[dict]:
    return [
        entry
        for entry in goalflight_steer_mailbox.worker_entries(_read_steer_entries(mailbox))
        if entry["seq"] not in seen_seqs
    ]


def _user_confirm_reply(entry: dict) -> tuple[str, str] | None:
    question_id = str(entry.get("reply_to") or "").strip()
    decision = str(entry.get("decision") or "").strip().lower()
    if not question_id:
        match = _re.match(
            r"^\s*USER-CONFIRM-ANSWER:\s*(\S+)\s+(yes|no)\b",
            str(entry.get("text") or ""),
            _re.IGNORECASE,
        )
        if match:
            question_id, decision = match.group(1), match.group(2).lower()
    if not question_id or decision not in {"yes", "no"}:
        return None
    return question_id, decision


_USER_CONFIRM_AUTHORIZE_TOKEN = _re.compile(
    r"(USER-CONFIRM-ANSWER:\s+\S+\s+)yes\b",
    _re.IGNORECASE,
)


def _sanitize_quoted_user_confirm_tokens(text: object) -> str:
    """Keep quoted/instructional text from becoming an authorization signal."""
    return _USER_CONFIRM_AUTHORIZE_TOKEN.sub(
        r"\1quoted-yes-not-authorization",
        str(text or ""),
    )


def _user_confirm_reply_arrived_before_deadline(
    entry: dict,
    question: dict[str, object],
    decision: str,
) -> bool:
    """Require durable pre-deadline arrival evidence for affirmative replies."""
    if decision != "yes":
        return True
    deadline_raw = question.get("reply_deadline_awake_mono_ns")
    if deadline_raw is None:
        # Unit/legacy records created before this field have no active deadline.
        return True
    arrival_ns = entry.get("awake_mono_ns")
    if (
        not isinstance(arrival_ns, int)
        or isinstance(arrival_ns, bool)
        or not isinstance(deadline_raw, int)
        or isinstance(deadline_raw, bool)
    ):
        return False
    return 0 < arrival_ns <= deadline_raw


def _user_confirm_is_resolved(question: dict[str, object]) -> bool:
    return question.get("controller_decision") in {"yes", "no"}


def _user_confirm_denies_run(question: dict[str, object]) -> bool:
    """Return whether an explicit or synthesized denial qualifies the run."""
    return question.get("controller_decision") == "no"


def _user_confirm_records_denied_action(question: dict[str, object]) -> bool:
    """Return whether the resolved row records an action that stayed denied."""
    return _user_confirm_is_resolved(question) and (
        _user_confirm_denies_run(question)
        or question.get("origin") == "permission_escalation"
    )


def _user_confirm_generation(question: dict[str, object]) -> str:
    """Return a hashable generation key, grouping malformed legacy values."""
    generation = question.get("generation")
    return generation if isinstance(generation, str) else ""


def _user_confirm_decision_scope(question: dict[str, object]) -> str:
    """Return the fail-closed arbitration cohort for one guarded decision."""
    return str(question.get("decision_scope") or question.get("question_id") or "")


def _refresh_user_confirm_authorizations(
    questions: dict[str, dict[str, object]],
) -> None:
    """Recompute authorization by generation and decision cohort."""
    questions_by_scope: dict[tuple[str, str], list[dict[str, object]]] = {}
    denied_generations = {
        _user_confirm_generation(question)
        for question in questions.values()
        if _user_confirm_denies_run(question)
    }
    for question in questions.values():
        questions_by_scope.setdefault(
            (
                _user_confirm_generation(question),
                _user_confirm_decision_scope(question),
            ),
            [],
        ).append(question)
    authorized_scopes = {
        generation_scope
        for generation_scope, members in questions_by_scope.items()
        if generation_scope[0] not in denied_generations
        and members
        and all(
            member.get("origin") == "worker_marker"
            and member.get("controller_decision") == "yes"
            for member in members
        )
    }
    for question in questions.values():
        question["guarded_action_authorized"] = bool(
            (
                _user_confirm_generation(question),
                _user_confirm_decision_scope(question),
            )
            in authorized_scopes
            and question.get("origin") == "worker_marker"
            and question.get("controller_decision") == "yes"
        )


def _apply_user_confirm_reply_batch(
    entries: list[dict],
    questions: dict[str, dict[str, object]],
    accepted_reply_seqs: set[int],
    rejected_reply_seqs: set[int],
    *,
    wall_clock: Callable[[], int] | None = None,
    defer_yes: bool = False,
) -> set[int]:
    """Apply one mailbox batch with permanent, deny-biased arbitration."""
    resolved_at = wall_clock or _now
    accepted: set[int] = set()
    candidates: dict[str, list[tuple[int, str]]] = {}
    for entry in entries:
        reply = _user_confirm_reply(entry)
        if reply is None:
            continue
        seq = int(entry["seq"])
        if seq in accepted_reply_seqs:
            accepted.add(seq)
            continue
        question_id, decision = reply
        question = questions.get(question_id)
        if (
            question is not None
            and not _user_confirm_reply_arrived_before_deadline(
                entry,
                question,
                decision,
            )
        ):
            rejected_reply_seqs.add(seq)
            continue
        candidates.setdefault(question_id, []).append((seq, decision))

    for question_id, replies in candidates.items():
        question = questions.get(question_id)
        if question is None:
            rejected_reply_seqs.update(seq for seq, _decision in replies)
            continue
        if question.get("arbitration_closed_at") is not None:
            # Once a finalized answer has been exposed to the worker, later
            # mailbox rows are audit evidence only. Revoking an authorization
            # after delivery cannot undo the action and would make status lie
            # about the decision the worker actually received.
            rejected_reply_seqs.update(seq for seq, _decision in replies)
            continue

        no_replies = sorted(seq for seq, decision in replies if decision == "no")
        yes_replies = sorted(seq for seq, decision in replies if decision == "yes")
        prior_decision = question.get("controller_decision")

        if prior_decision == "no":
            rejected_reply_seqs.update(seq for seq, _decision in replies)
            continue

        if no_replies:
            winning_seq = no_replies[0]
            provisional_yes_seq = question.pop(
                "provisional_yes_reply_steer_seq", None
            )
            if prior_decision == "yes":
                question["superseded_reply_steer_seq"] = question.get("reply_steer_seq")
            closed_at = resolved_at()
            question["controller_decision"] = "no"
            question["reply_steer_seq"] = winning_seq
            question["resolved_at"] = closed_at
            question["arbitration_closed_at"] = closed_at
            question["guarded_action_authorized"] = False
            if prior_decision == "yes" or provisional_yes_seq is not None or yes_replies:
                question["decision_conflict"] = True
            accepted_reply_seqs.add(winning_seq)
            accepted.add(winning_seq)
            if provisional_yes_seq is not None:
                rejected_reply_seqs.add(int(provisional_yes_seq))
            rejected_reply_seqs.update(no_replies[1:])
            rejected_reply_seqs.update(yes_replies)
            continue

        if prior_decision == "yes":
            rejected_reply_seqs.update(yes_replies)
            continue

        winning_seq = yes_replies[0]
        if defer_yes:
            provisional_yes_seq = question.get("provisional_yes_reply_steer_seq")
            if provisional_yes_seq is None:
                question["provisional_yes_reply_steer_seq"] = winning_seq
                rejected_reply_seqs.update(yes_replies[1:])
            else:
                rejected_reply_seqs.update(
                    seq for seq in yes_replies if seq != provisional_yes_seq
                )
            continue
        question["controller_decision"] = "yes"
        question["reply_steer_seq"] = winning_seq
        question["resolved_at"] = resolved_at()
        accepted_reply_seqs.add(winning_seq)
        accepted.add(winning_seq)
        rejected_reply_seqs.update(yes_replies[1:])

    _refresh_user_confirm_authorizations(questions)
    return accepted


def _finalize_provisional_user_confirm_yes(
    questions: list[dict[str, object]],
    all_questions: dict[str, dict[str, object]],
    accepted_reply_seqs: set[int],
    *,
    wall_clock: Callable[[], int] | None = None,
) -> None:
    """Resolve timely affirmative replies only after arbitration closes."""
    resolved_at = wall_clock or _now
    for candidate in questions:
        question = all_questions.get(str(candidate.get("question_id")))
        if question is None:
            continue
        if _user_confirm_is_resolved(question):
            continue
        reply_seq = question.pop("provisional_yes_reply_steer_seq", None)
        if reply_seq is None:
            continue
        closed_at = resolved_at()
        question["controller_decision"] = "yes"
        question["reply_steer_seq"] = int(reply_seq)
        question["resolved_at"] = closed_at
        question["arbitration_closed_at"] = closed_at
        accepted_reply_seqs.add(int(reply_seq))

    _refresh_user_confirm_authorizations(all_questions)


def _user_confirm_deadline_reached(
    deadline_mono: float,
    *,
    awake_clock: Callable[[], float] = active_monotonic,
) -> bool:
    return awake_clock() >= deadline_mono


async def _wait_for_user_confirm_replies(
    *,
    deadline_mono: float,
    poll_s: float,
    read_entries: Callable[[], list[dict]],
    apply_replies: Callable[[list[dict]], set[int]],
    has_denial: Callable[[], bool],
    awake_clock: Callable[[], float] = active_monotonic,
    sleep: Callable[[float], Any] = asyncio.sleep,
) -> list[dict]:
    """Collect replies through the deadline; only an explicit denial ends early.

    An affirmative reply stays provisional so another controller's later denial
    can join arbitration before any authorizing prompt reaches the worker.
    """
    entries: list[dict] = []
    while not _user_confirm_deadline_reached(deadline_mono, awake_clock=awake_clock):
        await sleep(poll_s)
        if _user_confirm_deadline_reached(deadline_mono, awake_clock=awake_clock):
            break
        entries = read_entries()
        if _user_confirm_deadline_reached(deadline_mono, awake_clock=awake_clock):
            entries = []
            break
        apply_replies(entries)
        if has_denial():
            break
    return entries


async def _read_user_confirm_replies_denying_expired(
    *,
    read_entries: Callable[[], list[dict]],
    expired_questions: Callable[[], list[dict[str, object]]],
    deny_expired: Callable[[list[dict[str, object]]], Any],
    apply_replies: Callable[[list[dict]], set[int]],
) -> tuple[list[dict], list[dict[str, object]], set[int]]:
    """Arbitrate provably timely rows at the deadline boundary, then deny."""
    entries = read_entries()
    accepted = apply_replies(entries)
    expired = expired_questions()
    if expired:
        await deny_expired(expired)
        # Settlement can finalize a provisional affirmative or append a
        # structured timeout denial. Re-read under the now-closed decision so
        # the winning row is deliverable at this same boundary.
        entries = read_entries()
        accepted.update(apply_replies(entries))
    return entries, expired, accepted


def _steer_turn_prompt(
    mailbox: Path,
    entries: list[dict],
    accepted_user_confirm_reply_seqs: set[int],
    user_confirm_questions: dict[str, dict[str, object]] | None = None,
) -> str:
    lines = [
        "Orchestrator steer messages are queued for this dispatch.",
        f"Mailbox: {mailbox}",
        "Incorporate every message below before continuing.",
        "Acknowledge each one on its own line as `STEER-ACK: <seq>`.",
        "Mid-turn steer delivery is deferred; this prompt is being sent at a turn boundary.",
        "",
        "Queued steer entries:",
    ]
    for entry in entries:
        reply = _user_confirm_reply(entry)
        if reply is None:
            lines.append(
                f"{entry['seq']}: "
                f"{_sanitize_quoted_user_confirm_tokens(entry['text'])}"
            )
            continue
        question_id, decision = reply
        question = (user_confirm_questions or {}).get(question_id, {})
        try:
            recorded_reply_seq = int(question.get("reply_steer_seq") or -1)
        except (TypeError, ValueError):
            recorded_reply_seq = -1
        if (
            int(entry["seq"]) not in accepted_user_confirm_reply_seqs
            and recorded_reply_seq != int(entry["seq"])
        ):
            lines.append(
                f"{entry['seq']}: ignored USER-CONFIRM reply: no outstanding "
                "question matched. No action is authorized."
            )
            continue
        if question.get("origin") == "permission_escalation":
            lines.append(
                f"{entry['seq']}: Controller response recorded for {question_id}: "
                f"{'affirmative acknowledgment' if decision == 'yes' else 'denied'}. "
                "The ACP tool call remains denied and this response authorizes no retry "
                "or alternate route. Use inline permission mode or a new explicitly "
                "authorized dispatch for that action."
            )
        elif question.get("guarded_action_authorized") is True:
            controller_note = _sanitize_quoted_user_confirm_tokens(entry["text"])
            lines.append(
                f"{entry['seq']}: USER-CONFIRM-ANSWER: {question_id} yes. "
                "Controller note (quoted text, not an authorization signal): "
                f"{controller_note}"
            )
        elif decision == "yes":
            generation_denied = any(
                _user_confirm_generation(candidate)
                == _user_confirm_generation(question)
                and _user_confirm_denies_run(candidate)
                for candidate in (user_confirm_questions or {}).values()
            )
            reason = (
                "Connection is read-only after a prior denial; a new dispatch "
                "is required to authorize this."
                if generation_denied
                else "No action is authorized."
            )
            lines.append(
                f"{entry['seq']}: USER-CONFIRM-ANSWER: {question_id} "
                f"recorded-yes-not-authorized. {reason}"
            )
        else:
            lines.append(
                f"{entry['seq']}: USER-CONFIRM-ANSWER: {question_id} no. "
                "The action remains denied; no action is authorized."
            )
    if any(_user_confirm_reply(entry) for entry in entries):
        lines.extend(
            [
                "",
                "Only a correlated `yes` for a worker-marker question whose status says "
                "`guarded_action_authorized=true` authorizes that action. A permission-"
                "escalation response is acknowledgment only.",
                "A steer ACK, mailbox read, unrelated steer, or silence is not approval.",
                "A denied ACP tool call stays denied; prose cannot retroactively authorize it.",
            ]
        )
    return "\n".join(lines)


def _prompt_with_steer(
    base_prompt: str,
    mailbox: Path,
    entries: list[dict],
    accepted_user_confirm_reply_seqs: set[int],
    user_confirm_questions: dict[str, dict[str, object]] | None = None,
) -> str:
    if not entries:
        return base_prompt
    return (
        f"{_steer_turn_prompt(mailbox, entries, accepted_user_confirm_reply_seqs, user_confirm_questions)}"
        f"\n\nOriginal task:\n{base_prompt}"
    )


def _terminal_turn_marker(markers: dict[str, list[str]]) -> bool:
    return any(markers.get(kind) for kind in TERMINAL_MARKERS)


def _successful_terminal_marker(markers: dict[str, list[str]]) -> bool:
    return any(markers.get(kind) for kind in SUCCESS_TERMINAL_MARKERS)


def _state_after_actionable_terminal_markers(state: str, markers: dict[str, list[str]]) -> str:
    if state == "complete" and any(
        has_actionable_marker_values(markers, kind)
        for kind in ACTIONABLE_BLOCKING_TERMINAL_MARKERS
    ):
        return "blocked"
    return state


def _pressure_text(part: object) -> str:
    if part is None:
        return ""
    if isinstance(part, str):
        return part
    try:
        return json.dumps(part, sort_keys=True)
    except TypeError:
        return str(part)


def _rate_limit_signature_excerpt(*parts: object, context_chars: int = 160) -> tuple[str, str] | None:
    text = "\n".join(piece for piece in (_pressure_text(part) for part in parts) if piece)
    haystack = text.casefold()
    if not haystack:
        return None
    for pattern in RATE_LIMIT_PATTERNS:
        needle = pattern.casefold()
        idx = haystack.find(needle)
        if idx == -1:
            continue
        start = max(0, idx - context_chars)
        end = min(len(text), idx + len(pattern) + context_chars)
        excerpt = " ".join(text[start:end].split())
        return pattern, excerpt
    return None


def _merge_prompt_results(results: list[PromptResult]) -> PromptResult:
    merged = PromptResult()
    for result in results:
        merged.text += str(getattr(result, "text", "") or "")
        merged.thoughts += str(getattr(result, "thoughts", "") or "")
        merged.tool_calls.extend(list(getattr(result, "tool_calls", []) or []))
        merged.plan_entries.extend(list(getattr(result, "plan_entries", []) or []))
        merged.raw_events.extend(list(getattr(result, "raw_events", []) or []))
        merged.out_of_scope_writes.extend(list(getattr(result, "out_of_scope_writes", []) or []))
        merged.permission_escalations.extend(list(getattr(result, "permission_escalations", []) or []))
        merged.permission_auto_declined.extend(list(getattr(result, "permission_auto_declined", []) or []))
        merged.permission_router_decisions.extend(list(getattr(result, "permission_router_decisions", []) or []))
        if len(merged.permission_router_decisions) > MAX_PERMISSION_ROUTER_DECISIONS:
            merged.permission_router_decisions = merged.permission_router_decisions[-MAX_PERMISSION_ROUTER_DECISIONS:]
        result_error = getattr(result, "error", None)
        if result_error is not None and merged.error is None:
            merged.error = result_error
        if getattr(result, "cancelled_for_marker", False) and not merged.cancelled_for_marker:
            merged.cancelled_for_marker = True
            merged.early_marker = getattr(result, "early_marker", None)
        merged.stop_reason = getattr(result, "stop_reason", None)
    return merged


def _last_prompt_result(results: list[PromptResult], merged: PromptResult) -> PromptResult:
    return results[-1] if results else merged


class _AcpWorkerDetached(Exception):
    """Internal control-flow signal: runner exits while worker stays alive."""


def _resolve_manifest_binary(binary: str) -> str:
    if binary == "grok":
        return shutil.which("grok") or str(Path.home() / ".grok/bin/grok")
    if binary == "cursor-agent":
        return shutil.which("cursor-agent") or str(Path.home() / ".local/bin/cursor-agent")
    if binary == "opencode":
        return shutil.which("opencode") or str(Path.home() / ".local/bin/opencode")
    if "/" in binary:
        return str(Path(binary).expanduser())
    return shutil.which(binary) or binary


def _manifest_acp_command(agent: str) -> tuple[str, list[str]] | None:
    manifest = load_manifest(agent)
    exec_spec = ((manifest or {}).get("invocation") or {}).get("exec") or {}
    if exec_spec.get("kind") != "acp":
        return None
    binary = exec_spec.get("binary")
    args = exec_spec.get("args")
    if not isinstance(binary, str) or not isinstance(args, list):
        return None
    if not all(isinstance(part, str) for part in args):
        return None
    return _resolve_manifest_binary(binary), list(args)


def _acp_model_args(agent: str, args: list[str], model: str) -> list[str]:
    """Insert a model selector into an ACP command's args, PER-AGENT.

    The selector must sit in the FLAGS region, not after a terminal positional
    (grok's `stdio`), and the flag itself differs by agent, so a blind append is
    wrong. Verified forms: codex uses the global config override `-c model=<id>`;
    grok needs `--model <id>` BEFORE its `stdio` terminal. cursor/opencode take
    `--model <id>` ahead of their subcommand (cursor/opencode arg-position is
    best-effort — see protocols/dispatch-routing.md). The id format is the agent's
    own (grok-composer-2.5-fast, anthropic/claude-haiku, ...).

    claude-code-cli-acp is the exception: it enters ACP stdio server mode only
    with no argv flags. Passing Claude CLI flags like `--model` switches it away
    from ACP handshake handling, so model selection cannot happen here.
    """
    a = agent.strip().lower()
    if a in ("codex", "codex-acp"):
        return ["-c", f"model={model}", *args]
    if a in ("grok", "grok-acp") and args and args[-1] == "stdio":
        return [*args[:-1], "--model", model, "stdio"]
    if a in ("claude", "claude-acp"):
        return args
    return ["--model", model, *args]


def _uses_session_model(agent: str) -> bool:
    return agent.strip().lower() in {"claude", "claude-acp"}


# Quality-by-default: when no model is selected, dispatch the agent's strongest
# model where it is UNAMBIGUOUS and above the agent's own default. codex already
# defaults strong (user config). grok (bash AND acp) now omits --model so grok's
# own CLI default applies (grok-4.5) — it writes reliably through ACP (validated
# 2026-07-08 via a live grok-acp file-write smoke), retiring the old
# grok-composer-2.5-fast ACP pin and auto-tracking grok's default forward.
# opencode "strongest" is ambiguous, and claude-code-cli-acp must be launched
# with no argv flags to stay in ACP server mode. An explicit --model still
# passes through for any agent.
#
# cursor resolves differently: its surface carries several vendors' models, so
# the default is a ROUTING choice rather than a strength claim. Kimi K3 is the
# default here for a HOSTING reason - Kimi's own CLI sends prompts and repo
# contents to its vendor's service, while cursor serves the same model family
# from US infrastructure under the cursor account. Same model, different
# jurisdiction for the data; do not "simplify" this back to the direct CLI.
# `--model cursor-grok-4.5-high` selects Grok on the same lane. Verify any id
# against `cursor-agent --list-models` before pinning it - that list changes
# without notice.
# NOT auto-applied: pinning an id here would violate the no-default-model
# invariant that retired the grok ACP pin in 2026-07 (pinned ids go stale and
# stop tracking the CLI's own default). These are the ids to pass explicitly.
CURSOR_KIMI_MODEL = "kimi-k3-high"
CURSOR_GROK_MODEL = "cursor-grok-4.5-high"
_DEFAULT_STRONG_MODEL: dict[str, str] = {}


def _grok_acp_base_command() -> tuple[str, list[str]]:
    return _resolve_manifest_binary("grok"), ["agent", "stdio"]


def agent_command(
    agent: str,
    model: str | None = None,
) -> tuple[str, list[str]]:
    agent_key = str(agent).strip().lower()
    manifest_command = _manifest_acp_command(agent)
    if manifest_command is not None:
        binary, args = manifest_command
    elif agent_key in {"grok", "grok-acp"}:
        binary, args = _grok_acp_base_command()
    elif agent_key in {"claude", "claude-acp"}:
        binary, args = "claude-code-cli-acp", []
    else:
        binary, args = agent, []
    if model is None:
        model = _DEFAULT_STRONG_MODEL.get(agent_key)
    if model:
        args = _acp_model_args(agent, args, str(model))
    return binary, args


def _codex_workspace_write_acp_args(
    agent: str,
    args: list[str],
    *,
    cwd: str | None,
    os_sandbox: str | None,
) -> list[str]:
    if str(agent).strip().lower() not in {"codex", "codex-acp"}:
        return args
    return [*codex_workspace_write_args(cwd, os_sandbox), *args]


def _compact_tool_call_summaries(tool_calls: list[Any], limit: int = 50) -> list[dict[str, object]]:
    """Small status-safe surface for live matrix assertions."""
    out: list[dict[str, object]] = []
    for item in list(tool_calls or [])[:limit]:
        raw_locations = None
        try:
            raw_locations = item.get("locations") if isinstance(item, dict) else getattr(item, "locations", None)
        except Exception:
            raw_locations = None
        locations: list[str] = []
        if isinstance(raw_locations, list):
            for loc in raw_locations[:_INLINE_PERMISSION_LIST_MAX]:
                path = None
                try:
                    path = loc.get("path") if isinstance(loc, dict) else getattr(loc, "path", None)
                except Exception:
                    path = None
                if path:
                    locations.append(_compact_permission_str(path))
        summary: dict[str, object] = {
            "title": _compact_permission_str(_tool_call_title(item)),
            "locations": locations,
        }
        for key in ("kind", "status", "toolCallId", "tool_call_id"):
            try:
                value = item.get(key) if isinstance(item, dict) else getattr(item, key, None)
            except Exception:
                value = None
            if value is not None:
                summary[key] = _compact_permission_str(value, 160)
        out.append(summary)
    return out


def _permission_audit_surface_enabled(permission_mode: str) -> bool:
    return permission_mode == "inline" or os.environ.get("GOALFLIGHT_ACP_LIVE_MATRIX") == "1"


def _adapter_manifest_candidates(agent: str) -> list[Path]:
    return manifest_candidates(agent)


def adapter_liveness_config(agent: str) -> tuple[str, float]:
    for path in _adapter_manifest_candidates(agent):
        if not path.exists():
            continue
        try:
            manifest = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            break
        status_contract = manifest.get("status_contract") or {}
        profile = str(status_contract.get("liveness_profile") or "local_compute")
        if profile not in LIVENESS_PROFILES:
            profile = "local_compute"
        silence_s = status_contract.get("remote_turn_silence_s")
        try:
            remote_turn_silence_s = float(silence_s)
        except (TypeError, ValueError):
            remote_turn_silence_s = DEFAULT_REMOTE_TURN_SILENCE_S
        if remote_turn_silence_s <= 0:
            remote_turn_silence_s = DEFAULT_REMOTE_TURN_SILENCE_S
        return profile, remote_turn_silence_s
    return "local_compute", DEFAULT_REMOTE_TURN_SILENCE_S


def _now() -> int:
    return int(time.time())


def worktree_path_for_dispatch(project_root: Path, dispatch_id: str) -> Path:
    """Return the managed local worktree path for a dispatch id.

    Dispatch ids become path segments under ``worktrees/``. Reject separators
    and traversal up front so a caller cannot route writes outside the project.
    """
    if not dispatch_id or dispatch_id in {".", ".."} or dispatch_id.startswith("."):
        raise ValueError("dispatch_id must be a non-empty path segment")
    if "/" in dispatch_id or "\\" in dispatch_id or ".." in Path(dispatch_id).parts:
        raise ValueError(f"dispatch_id is not a safe path segment: {dispatch_id!r}")
    if not _re.fullmatch(r"[A-Za-z0-9._-]+", dispatch_id):
        raise ValueError(f"dispatch_id contains unsupported characters: {dispatch_id!r}")
    return project_root / "worktrees" / dispatch_id


def status_filename_segment(dispatch_id: str) -> str:
    """Return a filesystem-safe status filename segment for a dispatch id."""
    segment = _re.sub(r"[^A-Za-z0-9._-]+", "_", dispatch_id).strip("._-")
    return segment or "invalid-dispatch"


def create_dispatch_worktree(project_root: Path, dispatch_id: str) -> Path:
    """Create and return the per-dispatch git worktree path."""
    project_root = project_root.resolve()
    worktree_path = worktree_path_for_dispatch(project_root, dispatch_id)
    top = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=str(project_root),
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if top.returncode != 0:
        detail = (top.stderr or top.stdout or "").strip()
        raise RuntimeError(f"--cwd is not a git repository root: {project_root}: {detail}")
    if Path(top.stdout.strip()).resolve() != project_root:
        raise RuntimeError(f"--cwd must be the git repository root: {project_root}")
    managed_root = worktree_path.parent
    if managed_root.is_symlink():
        raise ValueError(f"managed worktree root must not be a symlink: {managed_root}")
    if managed_root.exists() and not managed_root.is_dir():
        raise ValueError(f"managed worktree root is not a directory: {managed_root}")
    managed_root.mkdir(parents=True, exist_ok=True)
    if worktree_path.exists() or worktree_path.is_symlink():
        raise ValueError(f"dispatch worktree path already exists: {worktree_path}")
    result = subprocess.run(
        ["git", "worktree", "add", str(worktree_path), "HEAD"],
        cwd=str(project_root),
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise RuntimeError(f"git worktree add failed for {worktree_path}: {detail}")
    if worktree_path.is_symlink():
        raise RuntimeError(f"dispatch worktree path became a symlink: {worktree_path}")
    try:
        worktree_path.resolve().relative_to(managed_root.resolve())
    except ValueError as exc:
        raise RuntimeError(f"dispatch worktree escaped managed root: {worktree_path}") from exc
    return worktree_path


def create_and_route_dispatch_worktree(_cfg: argparse.Namespace, project_root: Path, dispatch_id: str) -> Path:
    """Create a dispatch worktree; caller routes worker cwd with a local value."""
    created = create_dispatch_worktree(project_root, dispatch_id)
    return created


def _event_kind(event: dict) -> str:
    if "_prompt_result" in event:
        return "prompt_result"
    return str(event.get("params", {}).get("update", {}).get("sessionUpdate") or event.get("method") or "event")


_MODEL_PROGRESS_EVENT_KINDS = {"agent_message_chunk", "agent_thought_chunk", "plan"}
_TOOL_ERROR_EVENT_KINDS = {
    "tool_output_error",
    "tool_error",
    "tool_result_error",
}
_TOOL_SUCCESS_EVENT_KINDS = {
    "tool_output",
    "tool_result",
}
_TOOL_SUCCESS_STATUSES = {"complete", "completed", "success", "succeeded", "ok"}
_TOOL_ERROR_STATUSES = {"error", "errored", "failed"}
_RUNAWAY_TERMINAL_REASONS = {"runaway_tool_error_loop", "runaway_event_cap"}


def _event_update(event: dict) -> dict:
    update = (event.get("params") or {}).get("update") or {}
    return update if isinstance(update, dict) else {}


def _compact_event_value(value: object, limit: int = 500) -> str:
    if isinstance(value, str):
        text = value
    else:
        try:
            text = json.dumps(value, sort_keys=True, separators=(",", ":"))
        except (TypeError, ValueError):
            text = str(value)
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _tool_name_from_event(event: dict) -> str:
    update = _event_update(event)
    for container in (update, update.get("toolCall"), update.get("tool_call")):
        if not isinstance(container, dict):
            continue
        for key in ("title", "tool", "toolName", "tool_name", "name", "toolCallId", "tool_call_id", "id"):
            value = container.get(key)
            if isinstance(value, str) and value:
                return _compact_event_value(value, 160)
    return "unknown"


def _tool_error_from_event(event: dict) -> str:
    update = _event_update(event)
    content = update.get("content")
    candidates = [
        update.get("error"),
        update.get("message"),
        update.get("details"),
        content.get("text") if isinstance(content, dict) else None,
    ]
    for value in candidates:
        if value:
            return _compact_event_value(value)
    return _compact_event_value(update)


def _event_is_model_progress(event: dict) -> bool:
    return _event_kind(event) in _MODEL_PROGRESS_EVENT_KINDS


def _event_is_tool_success(event: dict) -> bool:
    kind = _event_kind(event)
    update = _event_update(event)
    status = str(update.get("status") or "").lower()
    return kind in _TOOL_SUCCESS_EVENT_KINDS or status in _TOOL_SUCCESS_STATUSES


def _event_is_tool_error(event: dict) -> bool:
    kind = _event_kind(event)
    update = _event_update(event)
    status = str(update.get("status") or "").lower()
    return kind in _TOOL_ERROR_EVENT_KINDS or (
        kind in {"tool_call", "tool_call_update"} and status in _TOOL_ERROR_STATUSES
    )


def _is_runaway_terminal(heartbeat_terminal: str | None, heartbeat_error: dict | None) -> bool:
    return (
        heartbeat_terminal == "failed"
        and isinstance(heartbeat_error, dict)
        and heartbeat_error.get("reason") in _RUNAWAY_TERMINAL_REASONS
    )


class AcpRunawayCaps:
    def __init__(self, *, max_consecutive_tool_errors: int, max_acp_events: int) -> None:
        self.max_consecutive_tool_errors = max(1, int(max_consecutive_tool_errors))
        self.max_acp_events = max(1, int(max_acp_events))
        self.consecutive_tool_errors = 0
        self.repeated_tool = "unknown"
        self.last_tool_error = ""

    def observe(self, event: dict, *, events_seen: int) -> dict[str, object] | None:
        if _event_is_model_progress(event) or _event_is_tool_success(event):
            self.consecutive_tool_errors = 0
            self.repeated_tool = "unknown"
            self.last_tool_error = ""
        elif _event_is_tool_error(event):
            self.consecutive_tool_errors += 1
            self.repeated_tool = _tool_name_from_event(event)
            self.last_tool_error = _tool_error_from_event(event)
            if self.consecutive_tool_errors >= self.max_consecutive_tool_errors:
                return {
                    "code": -1,
                    "message": "runaway_tool_error_loop",
                    "reason": "runaway_tool_error_loop",
                    "tool": self.repeated_tool,
                    "last_error": self.last_tool_error,
                    "consecutive_tool_errors": self.consecutive_tool_errors,
                    "max_consecutive_tool_errors": self.max_consecutive_tool_errors,
                }
        if events_seen > self.max_acp_events:
            return {
                "code": -1,
                "message": "runaway_event_cap",
                "reason": "runaway_event_cap",
                "events_seen": events_seen,
                "max_acp_events": self.max_acp_events,
            }
        return None


def decide_terminal_state(
    *,
    result_ok: bool,
    result_error: dict | None,
    result_text: str | None = None,
    assistant_output: str | None = None,
    stop_reason: str | None = None,
    agent: str | None = None,
    events_seen: int = 0,
    heartbeat_outcome: str | None,
    killed_by_heartbeat: bool,
    cancelled_for_marker: bool,
    early_marker: str | None,
    heartbeat_error: dict | None,
    terminal_marker_present: bool = False,
    successful_terminal_marker: bool = False,
) -> tuple[str, dict | None]:
    """Resolve the runner's terminal (state, error) from the prompt result and
    the heartbeat verdict, in priority order.

    A genuine end_turn (``result_ok``) refutes the SILENCE-class heartbeat
    terminals — the dead-sample wedge, ``progress_stall``, and ``max_quiet_s``,
    all reported as ``"wedged"`` and all gated on ``outstanding_count == 0``.
    Those fire on inactivity; the heartbeat loop keeps sampling until the outer
    ``finally`` cancels it, so a worker that has ALREADY completed its turn is
    briefly alive-and-silent (returned from the turn, waiting to be closed) and
    on an aggressive cadence one of them can trip in that tail AFTER end_turn was
    received. result_ok wins that race, then still has to satisfy the completion
    evidence gate below. This can never mask a real silence wedge, because a
    worker killed mid-turn cannot emit end_turn (the SDK rejects the pending
    prompt on the closed pipe), so a real wedge always has ``result_ok`` False.

    ``tool_timeout`` is NOT a silence signal: it fires while a tool is still
    OUTSTANDING (``outstanding_count > 0``) past its absolute wall
    (``--max-tool-s``). end_turn does not refute it — a worker that ends its turn
    leaving a tool it opened unresolved past the wall is a real anomaly the
    operator must see — so tool_timeout wins even over result_ok.

    A killed-but-no-recorded-outcome race defaults to the silence-class
    ``"wedged"`` (the dead-sample wedge is what the kill-without-outcome path is).

    Clean close and end_turn are transport evidence, not work evidence. Complete
    therefore also requires a terminal marker or non-whitespace assistant output.
    Tool activity alone, regardless of ``events_seen``, is not sufficient: Goal
    Flight workers are required to emit a terminal marker.
    """
    heartbeat_terminal = heartbeat_outcome or ("wedged" if killed_by_heartbeat else None)
    if _is_runaway_terminal(heartbeat_terminal, heartbeat_error):
        return "failed", heartbeat_error
    # tool_timeout is an outstanding-tool anomaly, not silence — never masked.
    if heartbeat_terminal == "tool_timeout":
        return "tool_timeout", heartbeat_error or {"code": -1, "message": "tool_timeout"}
    # Silence-class heartbeat terminals win only if the turn didn't complete;
    # a genuine end_turn refutes them.
    if heartbeat_terminal and not result_ok:
        return heartbeat_terminal, heartbeat_error or {"code": -1, "message": heartbeat_terminal}
    if cancelled_for_marker:
        return "blocked", {
            "code": 0,
            "message": "early_marker_cancelled",
            "marker": early_marker,
        }
    if result_ok:
        if not successful_terminal_marker:
            matched = _rate_limit_signature_excerpt(result_text, result_error, stop_reason)
            if matched:
                signature, excerpt = matched
                return "blocked", {
                    "code": 0,
                    "message": "provider_limit_signature_without_terminal_marker",
                    "signature": signature,
                    "excerpt": excerpt,
                }
        has_terminal_marker = terminal_marker_present or successful_terminal_marker
        has_assistant_output = bool(
            str(result_text or "").strip() or str(assistant_output or "").strip()
        )
        if not has_terminal_marker and not has_assistant_output:
            empty_error: dict[str, object] = {
                "code": -1,
                "message": "empty_session",
                "reason": "empty_session",
                "events_seen": max(0, int(events_seen)),
            }
            if stop_reason:
                empty_error["stop_reason"] = stop_reason
            if str(agent or "").lower().startswith("claude"):
                empty_error["hint"] = (
                    "Check Claude worker authentication with `claude auth status`."
                )
            else:
                empty_error["hint"] = "Check worker authentication for the selected agent."
            return "failed", empty_error
        return "complete", result_error
    return "failed", result_error


async def spawn_and_handshake_with_retry(
    command: str,
    acp_args: list[str],
    *,
    agent: str,
    session_id: str,
    cwd: str,
    attempts: int = 2,
    handshake_timeout: float = 60.0,
    auto_allow_tools: bool = True,
    activity: AcpLivenessActivity | None = None,
    on_attempt: Callable[[int, asyncio.subprocess.Process], Any] | None = None,
    context_mode: bool = True,
    permission_mode: str = "auto",
    permission_dir: str | None = None,
    permission_inline_timeout_s: float | None = None,
    permission_user_timeout_s: float | None = None,
    permission_policy: Callable[[Any, list[Any], str | None], str] | None = None,
    os_sandbox: str = OS_SANDBOX_OFF,
    session_model: str | None = None,
    env: dict[str, str] | None = None,
    stderr_capture: AgentStderrCapture | None = None,
) -> tuple[asyncio.subprocess.Process, AcpConnection]:
    """Spawn the worker and run the ACP handshake, retrying once on AcpError.

    The codex-acp wedge is INTERMITTENT — the worker spawns but never answers
    initialize/session_new (0% CPU, empty log, no status JSON), yet the bare
    handshake works in isolation. So a single kill+respawn usually clears it.
    The 0.4.2 handshake timeout is what makes this possible: it turns the
    otherwise-infinite `await fut` into a catchable AcpError.

    On each failed attempt the wedged worker is killed BEFORE respawning, so no
    identity-matched worker PID is ever left alive (the never-retry-while-old-
    PID-alive invariant from the converged design, applied at the handshake).

    on_attempt(attempt_index, proc): optional sync/async callback fired right
    after each spawn (before the handshake) so the caller can publish worker
    pid/pgid into its status JSON per attempt. Returns the (proc, conn) of the
    first successful handshake; raises AcpError if every attempt fails.
    """
    last_err: AcpError | None = None
    for attempt in range(max(1, attempts)):
        conn = await spawn_acp_connection(
            command,
            acp_args,
            agent=agent,
            session_id=session_id,
            cwd=cwd,
            auto_allow_tools=auto_allow_tools,
            activity=activity,
            context_mode=context_mode,
            permission_mode=permission_mode,
            permission_dir=permission_dir,
            permission_inline_timeout_s=permission_inline_timeout_s,
            permission_user_timeout_s=permission_user_timeout_s,
            permission_policy=permission_policy,
            os_sandbox=os_sandbox,
            env=env,
        )
        if stderr_capture is not None:
            await stderr_capture.attach(conn)
        proc = conn.proc
        if on_attempt is not None:
            maybe = on_attempt(attempt, proc)
            if inspect.isawaitable(maybe):
                await maybe
        try:
            await conn.initialize(timeout=handshake_timeout)
            await conn.new_session(cwd, timeout=handshake_timeout)
            if session_model and _uses_session_model(agent):
                try:
                    await conn.set_session_model(str(session_model), timeout=handshake_timeout)
                except Exception as model_error:
                    print(
                        "goalflight_acp_run: WARNING: "
                        f"session model {session_model!r} for {agent} was not applied: "
                        f"{type(model_error).__name__}: {model_error}",
                        file=sys.stderr,
                    )
            return proc, conn
        except AcpError as e:
            last_err = e
            # Reap the wedged worker before respawning — never leave an
            # identity-matched PID alive.
            with contextlib.suppress(Exception):
                await conn.kill()
    raise AcpError(f"handshake failed after {max(1, attempts)} attempt(s): {last_err}")


async def run_acp_dispatch(
    cfg: argparse.Namespace,
    *,
    wall_clock: Callable[[], int] | None = None,
    awake_clock: Callable[[], float] | None = None,
) -> dict:
    """Run one ACP dispatch with a SIGTERM bridge that preserves finalizers."""
    if goalflight_compat.is_windows():
        payload, _status_path = write_windows_refusal_status(cfg)
        return payload
    bridge = _SigtermCancelBridge(asyncio.get_running_loop(), asyncio.current_task())
    bridge.install()
    result: dict | None = None
    try:
        result = await _run_acp_dispatch_impl(
            cfg,
            sigterm_received=lambda: bridge.received,
            signal_signum=lambda: bridge.signum,
            wall_clock=wall_clock,
            awake_clock=awake_clock,
        )
        return result
    except asyncio.CancelledError:
        if bridge.received:
            dispatch_id = cfg.dispatch_id or f"acp-{cfg.agent}-{uuid.uuid4().hex[:8]}"
            result = {
                "schema": "goalflight.acp-run.v1",
                "dispatch_id": dispatch_id,
                "lease_id": None,
                "agent": cfg.agent,
                "session_id": cfg.session_id,
                "state": "failed",
                "ok": False,
                "error": {"code": -int(signal.SIGTERM), "message": "sigterm"},
                "terminated_by_signal": "SIGTERM",
                "worker_pid": None,
                "worker_alive": False,
                "updated_at": _now(),
            }
            return result
        raise
    finally:
        bridge.restore()
        if (
            getattr(cfg, "_codex_dispatch_home_resolved", False)
            and not (isinstance(result, dict) and result.get("worker_still_alive") is True)
        ):
            try:
                import goalflight_dispatch

                goalflight_dispatch.cleanup_codex_dispatch_home(
                    str(getattr(cfg, "dispatch_id", "") or "")
                )
            except BaseException as exc:
                print(
                    "goalflight_acp_run: per-dispatch home cleanup warning: "
                    f"{type(exc).__name__}",
                    file=sys.stderr,
                )


async def _run_acp_dispatch_impl(
    cfg: argparse.Namespace,
    *,
    sigterm_received: Callable[[], bool] | None = None,
    signal_signum: Callable[[], int | None] | None = None,
    wall_clock: Callable[[], int] | None = None,
    awake_clock: Callable[[], float] | None = None,
) -> dict:
    """Run one ACP dispatch from an argparse.Namespace config.

    Not thread-callable: ACP connection registries and pidfiles are process-
    scoped, and the asyncio objects created here are bound to the active loop.
    """
    wall_now = wall_clock or _now
    awake_now = awake_clock or active_monotonic
    progress_stall_s = getattr(cfg, "progress_stall_s", None)
    if progress_stall_s is None:
        progress_stall_s = 300.0
    stall_kill = bool(getattr(cfg, "stall_kill", False))
    worker_cwd = cfg.cwd
    manifest_profile, manifest_remote_turn_silence_s = adapter_liveness_config(cfg.agent)
    liveness_profile = getattr(cfg, "liveness_profile", None) or manifest_profile
    if liveness_profile not in LIVENESS_PROFILES:
        liveness_profile = "local_compute"
    read_only_dispatch = bool(getattr(cfg, "read_only", False))
    requested_os_sandbox = getattr(cfg, "os_sandbox", OS_SANDBOX_OFF)
    try:
        os_sandbox_profile: str | None = canonical_os_sandbox(requested_os_sandbox)
    except OsSandboxError as e:
        os_sandbox_profile = None
        os_sandbox_error = str(e)
    else:
        os_sandbox_error = None
    sandbox_fallback: dict[str, str] | None = None
    read_only_intent = (
        read_only_dispatch or os_sandbox_profile == "read-only"
    )
    if os_sandbox_error is None and read_only_intent:
        os_sandbox_gate = validate_os_sandbox_request(cfg.agent, os_sandbox_profile)
        if os_sandbox_gate is not None and acp_permission_read_only_supported(cfg.agent):
            sandbox_fallback = {
                "requested": str(os_sandbox_profile),
                "applied": OS_SANDBOX_OFF,
                "enforcement": "acp-permissions",
            }
            os_sandbox_profile = OS_SANDBOX_OFF
    remote_turn_silence_s = getattr(cfg, "remote_turn_silence_s", None)
    if remote_turn_silence_s is None:
        remote_turn_silence_s = manifest_remote_turn_silence_s
    try:
        remote_turn_silence_s = float(remote_turn_silence_s)
    except (TypeError, ValueError):
        remote_turn_silence_s = DEFAULT_REMOTE_TURN_SILENCE_S
    if remote_turn_silence_s <= 0:
        remote_turn_silence_s = DEFAULT_REMOTE_TURN_SILENCE_S
    remote_turn_cancel_grace_s = getattr(cfg, "remote_turn_cancel_grace_s", DEFAULT_REMOTE_TURN_CANCEL_GRACE_S)
    try:
        remote_turn_cancel_grace_s = float(remote_turn_cancel_grace_s)
    except (TypeError, ValueError):
        remote_turn_cancel_grace_s = DEFAULT_REMOTE_TURN_CANCEL_GRACE_S
    if remote_turn_cancel_grace_s < 0:
        remote_turn_cancel_grace_s = DEFAULT_REMOTE_TURN_CANCEL_GRACE_S
    dispatch_id = cfg.dispatch_id or f"acp-{cfg.agent}-{uuid.uuid4().hex[:8]}"
    steer_file, steer_file_source = _resolve_steer_file(cfg, dispatch_id)
    original_prompt_file = _resolve_original_prompt_file(cfg)
    run_started = time.time()
    project_root = Path(cfg.cwd).resolve()
    permission_mode = str(getattr(cfg, "permission_mode", "auto") or "auto")
    try:
        user_confirm_timeout_s = float(
            getattr(cfg, "user_confirm_timeout_s", DEFAULT_USER_CONFIRM_TIMEOUT_S)
            or DEFAULT_USER_CONFIRM_TIMEOUT_S
        )
    except (TypeError, ValueError):
        user_confirm_timeout_s = DEFAULT_USER_CONFIRM_TIMEOUT_S
    if user_confirm_timeout_s <= 0:
        user_confirm_timeout_s = DEFAULT_USER_CONFIRM_TIMEOUT_S
    resolved_permission_dir: str | None = None
    resolved_permission_dir_source: str | None = None
    if permission_mode == "inline":
        configured_permission_dir = getattr(cfg, "permission_dir", None)
        resolved_permission_dir = str(
            permits.permission_dir(
                configured_permission_dir,
                allowed_roots=_ipc_allowed_roots(),
            )
        )
        if configured_permission_dir:
            resolved_permission_dir_source = "cli"
        elif os.environ.get(permits.ENV_PERMISSION_DIR):
            env_permission_dir = Path(os.environ[permits.ENV_PERMISSION_DIR]).expanduser()
            resolved_permission_dir_source = (
                "env:state_root"
                if goalflight_compat.path_is_under(env_permission_dir, _ipc_allowed_roots())
                else "env:allow"
            )
        else:
            resolved_permission_dir_source = "default"
    worktree_mode = getattr(cfg, "worktree", "off")
    worktree_path: Path | None = None
    worktree_error: str | None = None
    if worktree_mode == "create":
        try:
            worktree_path = worktree_path_for_dispatch(project_root, dispatch_id)
        except Exception as e:
            worktree_error = f"{type(e).__name__}: {e}"
    status_path = _resolve_status_json_path(getattr(cfg, "status_json", None), dispatch_id)
    cfg.status_json = str(status_path)
    agent_stderr_path = _agent_stderr_log_path(status_path)
    agent_stderr_capture = AgentStderrCapture(agent_stderr_path)
    user_confirm_generation = uuid.uuid4().hex
    prior_user_confirm_tombstones: list[dict[str, object]] = []
    try:
        prior_payload = json.loads(status_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        prior_payload = {}
    if (
        isinstance(prior_payload, dict)
        and prior_payload.get("dispatch_id") == dispatch_id
    ):
        for raw_question in (prior_payload.get("user_confirm_pending") or [])[:100]:
            if not isinstance(raw_question, dict) or not raw_question.get("question_id"):
                continue
            tombstone = dict(raw_question)
            tombstone["origin"] = tombstone.get("origin") or "worker_marker"
            tombstone["controller_decision"] = "no"
            tombstone["guarded_action_authorized"] = False
            tombstone["resolved_at"] = wall_now()
            tombstone["resolution_reason"] = "runner_restarted"
            prior_user_confirm_tombstones.append(tombstone)
    payload: dict = {
        "schema": "goalflight.acp-run.v1",
        "dispatch_id": dispatch_id,
        "steer_mailbox": str(steer_file),
        "steer_mailbox_source": steer_file_source,
        "steer_delivered_seqs": [],
        "steer_acked_seqs": [],
        "steer_pending_seqs": [],
        "steer_mid_turn_delivery": "deferred",
        "lease_id": None,
        "agent": cfg.agent,
        "priority": getattr(cfg, "priority", "normal"),
        "session_id": cfg.session_id,
        "project_root": str(project_root),
        "worker_cwd": worker_cwd,
        "worktree_mode": worktree_mode,
        "planned_worktree_path": str(worktree_path) if worktree_path is not None else None,
        "worktree_path": None,
        "status_path": str(status_path),
        "agent_stderr_path": str(agent_stderr_path),
        "state": "starting",
        "ok": False,
        "worker_pid": None,
        "pgid": None,
        "worker_alive": False,
        "pgroup_cpu_pct": None,
        "events_seen": 0,
        "max_consecutive_tool_errors": int(getattr(cfg, "max_consecutive_tool_errors", DEFAULT_MAX_CONSECUTIVE_TOOL_ERRORS)),
        "consecutive_tool_errors": 0,
        "max_acp_events": int(getattr(cfg, "max_acp_events", DEFAULT_MAX_ACP_EVENTS)),
        "acp_dropped_frames": 0,
        "acp_dropped_frame_records": [],
        "last_event_at": None,
        "last_event_kind": None,
        "heartbeat_at": None,
        "progress_quiet_for_s": 0.0,
        "progress_stall_s": None,
        "stall_kill": stall_kill,
        "stall_wake_count": 0,
        "stall_wake_cap": DEFAULT_STALL_WAKE_CAP,
        "liveness_profile": liveness_profile,
        "remote_turn_silence_s": remote_turn_silence_s,
        "turn_in_flight": False,
        "turn_silent_for_s": 0.0,
        "os_sandbox": {
            "requested": requested_os_sandbox,
            "profile": os_sandbox_profile,
            "enabled": os_sandbox_profile not in {None, OS_SANDBOX_OFF},
        },
        "permission_mode": permission_mode,
        "user_confirm_timeout_s": user_confirm_timeout_s,
        "user_confirm_generation": user_confirm_generation,
        "user_confirm_pending": [],
        "user_confirm_resolved": prior_user_confirm_tombstones,
        "permission_dir": resolved_permission_dir,
        "permission_dir_source": resolved_permission_dir_source,
        "pending_permissions": [],
        "permission_decisions": [],
        "updated_at": _now(),
    }
    if sandbox_fallback is not None:
        payload["sandbox_fallback"] = sandbox_fallback
    boundary_warning = permission_boundary_warning(
        agent=cfg.agent,
        permission_mode=permission_mode,
        os_sandbox_profile=os_sandbox_profile,
        read_only=read_only_dispatch,
        interactive=bool(getattr(cfg, "interactive", False)),
    )
    if boundary_warning:
        payload["permission_boundary_warning"] = boundary_warning
    status_lock = asyncio.Lock()

    async def update_status(**updates: object) -> None:
        async with status_lock:
            payload.update(updates)
            with contextlib.suppress(NameError):
                snapshot = activity.snapshot(active_monotonic())
                payload["acp_dropped_frames"] = int(snapshot.get("dropped_frames", 0))
                payload["acp_dropped_frame_records"] = list(snapshot.get("dropped_frame_records", []))
            payload["updated_at"] = _now()
            write_status(status_path, payload)

    write_status(status_path, payload)
    if boundary_warning:
        print(f"goalflight_acp_run: WARNING: {boundary_warning}", file=sys.stderr)
    if worktree_error is not None:
        payload.update(
            state="failed_worktree",
            ok=False,
            error=worktree_error,
            updated_at=_now(),
        )
        write_status(status_path, payload)
        return payload
    try:
        if cfg.prompt:
            prompt = Path(original_prompt_file or cfg.prompt).read_text()
        elif getattr(cfg, "prompt_b64", None):
            import base64

            prompt = base64.b64decode(cfg.prompt_b64.encode("ascii")).decode("utf-8")
        else:
            prompt = cfg.prompt_text
        if not prompt:
            raise ValueError("--prompt, --prompt-text, or --prompt-b64 required")
        prompt = _prompt_with_original_prompt_file_preamble(prompt, original_prompt_file)
    except Exception as e:
        payload.update({"state": "failed", "error": f"{type(e).__name__}: {e}"})
        write_status(status_path, payload)
        return payload

    command, acp_args = agent_command(
        cfg.agent,
        model=getattr(cfg, "model", None),
    )
    acp_args = _codex_workspace_write_acp_args(
        cfg.agent, acp_args, cwd=worker_cwd, os_sandbox=os_sandbox_profile
    )
    spawn_env = _worker_spawn_env(cfg, original_prompt_file)
    gate = validate_acp_dispatch_readiness(cfg.agent, [command, *acp_args])
    if gate is not None:
        payload.update({"state": "blocked_adapter_gate", "error": gate})
        write_status(status_path, payload)
        return payload
    if os_sandbox_error is not None:
        payload.update({"state": "blocked_os_sandbox", "error": os_sandbox_error})
        write_status(status_path, payload)
        return payload
    os_sandbox_gate = validate_os_sandbox_request(cfg.agent, os_sandbox_profile)
    if os_sandbox_gate is not None:
        payload.update({"state": "blocked_os_sandbox", "error": os_sandbox_gate})
        write_status(status_path, payload)
        return payload
    try:
        preflight_os_sandbox(os_sandbox_profile)
    except OsSandboxError as e:
        payload.update({"state": "blocked_os_sandbox", "error": str(e)})
        write_status(status_path, payload)
        return payload

    # Sweep D-class pre-acquire sandbox cwd check (test_os_sandbox case
    # `blocks_temp_cwd_before_capacity`): macos_write_roots refuses cwds
    # inside an allowed temp root (the sandbox can't enforce workspace
    # boundaries when cwd is itself inside the always-allowed dirs). Run
    # the check NOW so a blocked sandbox shape doesn't waste a capacity
    # lease. The same check still runs again during `prepare_os_sandbox_
    # command` after spawn — this just short-circuits early.
    if os_sandbox_profile not in {None, OS_SANDBOX_OFF}:
        try:
            from goalflight_os_sandbox import macos_write_roots
            _ = macos_write_roots(
                worker_cwd,
                os_sandbox_profile,
                agent=cfg.agent,
                command=command if isinstance(command, str) else "",
            )
        except OsSandboxError as e:
            payload.update({"state": "blocked_os_sandbox", "error": str(e)})
            write_status(status_path, payload)
            return payload
        except Exception:
            # Linux / unsupported platform: defer to spawn-time check.
            pass

    # Lease TTL covers the worst-case run length. Derive from idle-timeout.
    lease_ttl_s = max(int(cfg.idle_timeout or (36000 if cfg.mode == "goal" else 300)) * 4, 3600)
    acquire_args = argparse.Namespace(
        agent=cfg.agent,
        dispatch_id=dispatch_id,
        prompt_id=cfg.prompt_id,
        project_root=str(project_root),
        worktree_path=str(worktree_path) if worktree_path is not None else None,
        worker_cwd=str(worktree_path) if worktree_path is not None else worker_cwd,
        controller_pid=os.getpid(),
        worker_pid=None,
        lease_id=None,
        mem_mb=None,
        agent_cap=None,
        priority=getattr(cfg, "priority", "normal"),
        ttl_s=lease_ttl_s,
        ram_mb=None,
        reserve_mb=goalflight_capacity.DEFAULT_RESERVE_MB,
        worst_worker_mb=goalflight_capacity.DEFAULT_WORST_WORKER_MB,
        hard_cap=goalflight_capacity.DEFAULT_HARD_CAP,
        max_total=None,
    )
    wait_budget_s = goalflight_capacity.resolve_capacity_wait_s(
        lane=acquire_args.priority,
        wait_s=getattr(cfg, "capacity_wait_s", None),
        log_prefix="goalflight_acp_run",
    )

    capacity_wait_started = active_monotonic()
    last_capacity_wait = {"attempt": 0}

    def on_capacity_wait(attempt: int, remaining_s: float, reason: dict) -> None:
        last_capacity_wait["attempt"] = attempt
        waited_s = round(max(0.0, wait_budget_s - remaining_s), 1)
        payload.update(
            {
                "state": "waiting_capacity",
                "reason": reason,
                "waited_s": waited_s,
                "wait_budget_s": wait_budget_s,
                "updated_at": _now(),
            }
        )
        write_status(status_path, payload)
        if attempt == 1 or attempt % 4 == 0:
            print(
                "CAPACITY-WAIT "
                + json.dumps(
                    {
                        "dispatch_id": dispatch_id,
                        "agent": cfg.agent,
                        "priority": acquire_args.priority,
                        "reason": reason.get("reason"),
                        "waited_s": waited_s,
                        "wait_budget_s": wait_budget_s,
                    },
                    sort_keys=True,
                ),
                flush=True,
            )

    try:
        acquire_payload = await goalflight_capacity.acquire_with_wait_async(
            acquire_args,
            lane=acquire_args.priority,
            wait_s=wait_budget_s,
            on_wait=on_capacity_wait,
            interrupted=sigterm_received,
            interrupted_signum=signal_signum,
        )
    except goalflight_capacity.CapacityWaitInterrupted as exc:
        payload.update(
            {
                "state": "blocked_capacity",
                "ok": False,
                "reason": exc.payload,
                "updated_at": _now(),
            }
        )
        write_status(status_path, payload)
        return payload
    if acquire_payload.get("decision") != "allow":
        if last_capacity_wait["attempt"]:
            acquire_payload = dict(acquire_payload)
            acquire_payload.setdefault(
                "waited_s",
                round(max(0.0, active_monotonic() - capacity_wait_started), 1),
            )
            acquire_payload.setdefault("attempts", int(last_capacity_wait["attempt"]) + 1)
        payload.update({"state": "blocked_capacity", "reason": acquire_payload})
        write_status(status_path, payload)
        return payload

    lease_id = acquire_payload.get("lease", {}).get("lease_id")

    proc: asyncio.subprocess.Process | None = None
    conn: AcpConnection | None = None
    heartbeat_task: asyncio.Task | None = None
    ledger_recorded = False
    state = "failed"
    effective_account: str | None = None
    activity = AcpLivenessActivity()
    heartbeat_outcome: str | None = None
    heartbeat_error: dict[str, object] | None = None
    wedged_by_heartbeat = False
    detach_worker = False
    stall_wake_count = 0
    stall_detach_event = asyncio.Event()
    relayed_permission_keys: set[str] = set()
    user_confirm_questions: dict[str, dict[str, object]] = {
        str(question["question_id"]): question
        for question in prior_user_confirm_tombstones
    }
    user_confirm_keys: dict[tuple[str, str], str] = {}
    user_confirm_deadlines_mono: dict[str, float] = {}
    accepted_user_confirm_reply_seqs: set[int] = set()
    rejected_user_confirm_reply_seqs: set[int] = set()
    user_confirm_sequence = 0
    user_confirm_timeout_continuations = 0
    user_confirm_exhausted = False
    awaiting_user_confirm = False
    user_confirm_expiry_lock = asyncio.Lock()
    # CPU-aware idle gate: keeps a busy-but-silent worker (running_quiet) but
    # enforces a hard wall (lease lifetime) so a pathological CPU spinner that
    # never emits an event can't hang the runner forever.
    idle_gate = IdleLivenessGate(cfg.cpu_epsilon, cfg.max_quiet_s)
    runaway_caps = AcpRunawayCaps(
        max_consecutive_tool_errors=int(getattr(cfg, "max_consecutive_tool_errors", DEFAULT_MAX_CONSECUTIVE_TOOL_ERRORS)),
        max_acp_events=int(getattr(cfg, "max_acp_events", DEFAULT_MAX_ACP_EVENTS)),
    )
    runaway_terminal_error: dict[str, object] | None = None
    awaiting_next_prompt = False
    awaiting_next_prompt_deadline_mono = 0.0
    pending_steer_first_seen_mono: dict[int, float] = {}
    try:
        configured_idle_timeout = float(cfg.idle_timeout or 0.0)
    except (TypeError, ValueError):
        configured_idle_timeout = 0.0
    between_turn_steer_grace_s = DEFAULT_BETWEEN_TURN_STEER_GRACE_S
    if configured_idle_timeout > 0:
        between_turn_steer_grace_s = min(
            DEFAULT_BETWEEN_TURN_STEER_GRACE_S,
            configured_idle_timeout,
        )
    empty_poll_override = os.environ.get("GOALFLIGHT_EMPTY_BETWEEN_TURN_STEER_POLL_S")
    try:
        configured_empty_poll = (
            float(empty_poll_override) if empty_poll_override not in (None, "") else None
        )
    except (TypeError, ValueError):
        configured_empty_poll = None
    base_empty_poll = (
        configured_empty_poll
        if configured_empty_poll is not None
        else DEFAULT_EMPTY_BETWEEN_TURN_STEER_POLL_S
    )
    empty_between_turn_steer_poll_s = min(
        base_empty_poll,
        between_turn_steer_grace_s,
    )

    def record_ledger_state(*, worker_pid: int | None, state: str) -> None:
        with contextlib.redirect_stdout(io.StringIO()):
            goalflight_ledger.cmd_record(
                argparse.Namespace(
                    dispatch_id=dispatch_id,
                    prompt_id=cfg.prompt_id,
                    prompt_path=cfg.prompt,
                    task_ids=getattr(cfg, "task_ids", []),
                    agent=cfg.agent,
                    engine=goalflight_ledger.infer_engine(cfg.agent),
                    shape="acp",
                    account=getattr(cfg, "account", None) or "default",
                    effective_account=effective_account,
                    request_envelope_json=(
                        json.dumps(cfg.request_envelope, sort_keys=True)
                        if isinstance(
                            getattr(cfg, "request_envelope", None), dict
                        )
                        else None
                    ),
                    transport="acp",
                    # project_root MUST be the original --cwd (main repo
                    # toplevel), NOT worker_cwd. For a worktree dispatch
                    # worker_cwd is reassigned to the per-dispatch worktree dir
                    # (line ~1770), but goalflight_status.scope_payload() filters
                    # records by exact project_root == this-repo toplevel. If we
                    # recorded the worktree path here the record would be scoped
                    # OUT of `status --done/--dispatch/--json` for its whole
                    # lifetime. The worktree path stays tracked in the ACP status
                    # JSON (worktree_path/worker_cwd) and the lease's worker_cwd.
                    # This mirrors the capacity lease, which already records the
                    # unmodified project_root.
                    project_root=str(project_root),
                    controller_pid=os.getpid(),
                    worker_pid=worker_pid,
                    acp_session_id=cfg.session_id,
                    logical_session_id=cfg.session_id,
                    lease_id=lease_id,
                    stdout_path=None,
                    stderr_path=None,
                    status_path=str(status_path),
                    os_sandbox_json=json.dumps(payload.get("os_sandbox") or {}, sort_keys=True),
                    queue_launch_token=getattr(cfg, "queue_launch_token", None),
                    state=state,
                    json=True,
                )
            )

    def attach_worker_to_lease(worker_pid: int) -> None:
        if not lease_id:
            return
        with goalflight_capacity.StateLock():
            data = goalflight_capacity.load_state()
            lease = data.get("leases", {}).get(lease_id)
            if lease:
                lease["worker_pid"] = worker_pid
                goalflight_capacity.save_state(data)

    def detach_lease_to_worker(worker_pid: int, reason: object) -> None:
        if not lease_id:
            return
        goalflight_capacity.detach_lease_to_worker(lease_id, worker_pid, reason)

    def stalled_markers(message: str) -> dict[str, list[str]]:
        markers = dict(payload.get("markers") or {})
        values = list(markers.get("STALLED") or [])
        values.append(message)
        markers["STALLED"] = values[-DEFAULT_STALL_WAKE_CAP:]
        return markers

    def user_confirm_status_lists() -> tuple[list[dict[str, object]], list[dict[str, object]]]:
        pending: list[dict[str, object]] = []
        resolved: list[dict[str, object]] = []
        for question in user_confirm_questions.values():
            target = resolved if _user_confirm_is_resolved(question) else pending
            target.append(dict(question))
        return pending, resolved

    def current_generation_user_confirms() -> list[dict[str, object]]:
        return [
            question
            for question in user_confirm_questions.values()
            if _user_confirm_generation(question) == user_confirm_generation
        ]

    def refresh_user_confirm_guard() -> None:
        questions = current_generation_user_confirms()
        # Deliberate F4 boundary: after any denial this ACP connection remains
        # read-only. Worker markers do not carry a stable permission/action key,
        # so trying to reopen only a "correlated" non-read call would be guessy
        # and fail-open. Restart tombstones remain audit-visible but belong to
        # the dead connection; this new generation is the reopening boundary.
        if any(_user_confirm_denies_run(question) for question in questions):
            state = "denied"
        elif any(not _user_confirm_is_resolved(question) for question in questions):
            state = "pending"
        else:
            state = "none"
        setter = getattr(getattr(conn, "client", None), "set_user_confirm_guard", None)
        if callable(setter):
            setter(state)

    def apply_user_confirm_replies(entries: list[dict]) -> set[int]:
        accepted = _apply_user_confirm_reply_batch(
            entries,
            user_confirm_questions,
            accepted_user_confirm_reply_seqs,
            rejected_user_confirm_reply_seqs,
            wall_clock=wall_now,
            defer_yes=True,
        )
        refresh_user_confirm_guard()
        return accepted

    async def deny_timed_out_user_confirms(
        questions: list[dict[str, object]],
    ) -> list[dict]:
        nonlocal user_confirm_timeout_continuations
        timeout_entries: list[dict] = []
        for question in questions:
            timeout_entries.append(
                await asyncio.to_thread(
                    goalflight_steer_mailbox.append_steer_entry,
                    steer_file,
                    (
                        "No controller answer arrived before the unattended timeout. "
                        "The guarded action remains denied. Continue only independent "
                        "safe work; do not take the requested action."
                    ),
                    dispatch_id=dispatch_id,
                    kind="user_confirm_reply",
                    reply_to=str(question["question_id"]),
                    decision="no",
                )
            )
        user_confirm_timeout_continuations += 1
        apply_user_confirm_replies(timeout_entries)
        return timeout_entries

    def deny_terminal_user_confirms(
        questions: list[dict[str, object]],
        *,
        reason: str,
    ) -> None:
        """Resolve questions that cannot outlive a terminal run."""
        for candidate in questions:
            question = user_confirm_questions.get(str(candidate.get("question_id")))
            if question is None or _user_confirm_is_resolved(question):
                continue
            provisional_yes_seq = question.pop(
                "provisional_yes_reply_steer_seq", None
            )
            if provisional_yes_seq is not None:
                rejected_user_confirm_reply_seqs.add(int(provisional_yes_seq))
                question["decision_conflict"] = True
            question["controller_decision"] = "no"
            question["resolved_at"] = wall_now()
            question["arbitration_closed_at"] = question["resolved_at"]
            question["resolution_reason"] = reason
            question["guarded_action_authorized"] = False
        _refresh_user_confirm_authorizations(user_confirm_questions)
        refresh_user_confirm_guard()

    async def settle_expired_user_confirms(
        questions: list[dict[str, object]],
    ) -> list[dict]:
        unanswered = [
            question
            for question in questions
            if question.get("provisional_yes_reply_steer_seq") is None
        ]
        timeout_entries = (
            await deny_timed_out_user_confirms(unanswered) if unanswered else []
        )
        _finalize_provisional_user_confirm_yes(
            questions,
            user_confirm_questions,
            accepted_user_confirm_reply_seqs,
            wall_clock=wall_now,
        )
        refresh_user_confirm_guard()
        return timeout_entries

    def expired_user_confirm_questions() -> list[dict[str, object]]:
        now_mono = awake_now()
        # Return canonical mutable records. Status snapshots deliberately copy
        # questions; using those copies here loses a provisional reply applied
        # at the expiry boundary and can synthesize a conflicting timeout no.
        pending = [
            question
            for question in user_confirm_questions.values()
            if not _user_confirm_is_resolved(question)
        ]
        return [
            question
            for question in pending
            if user_confirm_deadlines_mono.get(
                str(question["question_id"]),
                now_mono + user_confirm_timeout_s,
            )
            <= now_mono
        ]

    async def expire_midturn_user_confirms() -> bool:
        """Settle crossed deadlines and end the temporary liveness exemption."""
        nonlocal awaiting_user_confirm
        async with user_confirm_expiry_lock:
            expired = expired_user_confirm_questions()
            if not expired:
                return False
            # An open ACP prompt holds the turn lock, so ordinary between-turn
            # polling cannot observe a controller reply. Reconcile the mailbox
            # once at the first expiry boundary before synthesizing a denial.
            # Only a correlated affirmative stamped at or before its deadline
            # is explicit authorization; late or unstamped yes rows and
            # unanswered questions still fail closed.
            pending_reply_entries = _pending_steer_entries(
                steer_file,
                accepted_user_confirm_reply_seqs
                | rejected_user_confirm_reply_seqs,
            )
            apply_user_confirm_replies(pending_reply_entries)
            unsettled = [
                question
                for question in expired
                if not _user_confirm_is_resolved(
                    user_confirm_questions.get(str(question["question_id"]), {})
                )
            ]
            timeout_entries = (
                await settle_expired_user_confirms(unsettled) if unsettled else []
            )
            awaiting_user_confirm = bool(
                [
                    question
                    for question in current_generation_user_confirms()
                    if not _user_confirm_is_resolved(question)
                ]
            )
            confirm_pending, confirm_resolved = user_confirm_status_lists()
            await update_status(
                user_confirm_overdue=bool(timeout_entries),
                user_confirm_overdue_question_ids=[
                    str(entry["reply_to"]) for entry in timeout_entries
                ],
                user_confirm_timeout_count=user_confirm_timeout_continuations,
                user_confirm_pending=confirm_pending,
                user_confirm_resolved=confirm_resolved,
            )
            return True

    async def route_user_confirm(event: dict[str, Any]) -> None:
        nonlocal user_confirm_sequence, awaiting_user_confirm
        origin = str(event.get("origin") or "worker_marker")
        question_text = str(event.get("text") or "authorization required").strip()
        permission_key = (
            str((event.get("permission") or {}).get("key") or "")
            if isinstance(event.get("permission"), dict)
            else ""
        )
        key = (origin, f"{permission_key}:{question_text}")
        prior_id = user_confirm_keys.get(key)
        if prior_id and not _user_confirm_is_resolved(user_confirm_questions.get(prior_id, {})):
            return
        prior_question = user_confirm_questions.get(prior_id or "", {})
        decision_scope = (
            _user_confirm_decision_scope(prior_question)
            if prior_question
            else (
                f"{user_confirm_generation}:turn:{int(turn_index)}:"
                f"{origin}"
            )
        )

        user_confirm_sequence += 1
        question_id = f"{dispatch_id}-{user_confirm_generation}-q{user_confirm_sequence}"
        permission = event.get("permission")
        permission_context: dict[str, object] | None = None
        if isinstance(permission, dict):
            permission_context = {
                field: permission.get(field)
                for field in (
                    "title",
                    "kind",
                    "targets_outside_cwd",
                    "locations",
                    "tool_call_id",
                )
                if permission.get(field) not in (None, [], "")
            }
        partial_text = str(event.get("result_text") or "")
        question: dict[str, object] = {
            "question_id": question_id,
            "dispatch_id": dispatch_id,
            "generation": user_confirm_generation,
            "origin": origin,
            "text": question_text,
            "turn_index": int(turn_index),
            # Multiple same-origin questions emitted in one turn are one
            # authorization decision: a crossed denial poisons that cohort.
            # Permission escalations are acknowledgment-only and cannot join a
            # worker-marker cohort. Repeating the same guarded action keeps its
            # prior cohort; a different later action or restarted generation gets
            # a new cohort.
            "decision_scope": decision_scope,
            "asked_at": wall_now(),
            "reply_deadline_awake_mono_ns": int(
                (active_monotonic() + user_confirm_timeout_s) * 1_000_000_000
            ),
            "guarded_action_authorized": False,
        }
        if permission_context:
            question["permission"] = permission_context
        user_confirm_questions[question_id] = question
        user_confirm_keys[key] = question_id
        user_confirm_deadlines_mono[question_id] = awake_now() + user_confirm_timeout_s
        awaiting_user_confirm = True
        refresh_user_confirm_guard()

        relay_errors: list[str] = []
        mailbox_context = {
            "origin": origin,
            "turn_index": question["turn_index"],
            "guarded_action_authorized": False,
            "result_excerpt": partial_text[-1000:],
        }
        if permission_context:
            mailbox_context["permission"] = permission_context
        try:
            entry = await asyncio.to_thread(
                goalflight_steer_mailbox.append_user_confirm,
                steer_file,
                dispatch_id=dispatch_id,
                question_id=question_id,
                text=question_text,
                context=mailbox_context,
            )
            question["question_mailbox_seq"] = int(entry["seq"])
            question["reply_deadline_awake_mono_ns"] = (
                int(entry["awake_mono_ns"])
                + int(user_confirm_timeout_s * 1_000_000_000)
            )
        except Exception as exc:
            relay_errors.append(f"steer:{type(exc).__name__}: {exc}")

        try:
            import goalflight_messages

            posted = await asyncio.to_thread(
                goalflight_messages.post_message,
                dispatch_id=dispatch_id,
                msg_type="user_confirm",
                payload={
                    "question_id": question_id,
                    "text": question_text,
                    "origin": origin,
                    "turn_index": question["turn_index"],
                    "guarded_action_authorized": False,
                    "context": mailbox_context,
                },
                messages_dir=goalflight_messages.default_messages_dir(),
                source={
                    "node": "local",
                    "adapter": str(cfg.agent),
                    "transport": "acp-user-confirm",
                },
            )
            question["controller_mail_seq"] = int(posted["envelope"]["seq"])
        except Exception as exc:
            relay_errors.append(f"controller-mail:{type(exc).__name__}: {exc}")

        markers = dict(payload.get("markers") or {})
        marker_values = list(markers.get(USER_CONFIRM_MARKER) or [])
        if question_text not in marker_values:
            marker_values.append(question_text)
        markers[USER_CONFIRM_MARKER] = marker_values
        for kind, values in extract_markers(partial_text).items():
            existing = list(markers.get(kind) or [])
            existing.extend(value for value in values if value not in existing)
            markers[kind] = existing
        pending, resolved = user_confirm_status_lists()
        updates: dict[str, object] = {
            "state": "running_user_confirm",
            "markers": markers,
            "last_marker": {USER_CONFIRM_MARKER: question_text},
            "user_confirm_pending": pending,
            "user_confirm_resolved": resolved,
            # Persist the full partial turn at the moment the question is routed.
            # A later liveness failure must not erase pre-question RESULT lines.
            "result_text": partial_text or payload.get("result_text"),
            "text_excerpt": partial_text[-4000:] or payload.get("text_excerpt"),
        }
        if relay_errors:
            question["relay_errors"] = relay_errors
            updates["user_confirm_relay_errors"] = relay_errors
        await update_status(**updates)

    async def relay_inline_permissions() -> None:
        if permission_mode != "inline" or not resolved_permission_dir:
            return
        try:
            requests = await asyncio.to_thread(permits.list_requests, resolved_permission_dir)
        except Exception as exc:
            await update_status(permission_relay_error=f"{type(exc).__name__}: {exc}")
            return
        if not requests:
            if payload.get("pending_permissions"):
                await update_status(pending_permissions=[])
            return

        pending: list[dict[str, object]] = []
        decisions: list[tuple[str, str, str | None, dict[str, object]]] = []
        for record in requests:
            key = str(record.get("key") or "")
            if not key:
                continue
            decision, option_id, reason = _inline_permission_relay_decision(record)
            summary = _inline_permission_summary(
                record,
                decision=decision,
                option_id=option_id,
                reason=reason,
            )
            pending.append(summary)
            decisions.append((key, decision, option_id, summary))

        if not pending:
            return

        # Keep the dedup set bounded to keys still pending this tick. Once the
        # worker clears a decided request, its uuid-suffixed key vanishes from
        # list_requests, so dropping it here keeps the set sized to live requests.
        current_keys = {str(s.get("key") or "") for s in pending}
        relayed_permission_keys.intersection_update(current_keys)

        async with status_lock:
            markers = dict(payload.get("markers") or {})
            marker_values = list(markers.get(_INLINE_PERMISSION_MARKER) or [])
            new_marker_values: list[str] = []
            for summary in pending:
                key = str(summary.get("key") or "")
                marker_text = _inline_permission_marker_text(summary)
                # Mark "seen" on first sight. A transient write_decision failure
                # must not re-print the marker every tick until success; the
                # print is the only thing this guards.
                if key and key not in relayed_permission_keys:
                    relayed_permission_keys.add(key)
                    print(f"{_INLINE_PERMISSION_MARKER}: {marker_text}", flush=True)
                    marker_values.append(marker_text)
                    new_marker_values.append(marker_text)
            if marker_values:
                markers[_INLINE_PERMISSION_MARKER] = marker_values[-20:]
            payload.update(
                pending_permissions=pending,
                markers=markers,
                updated_at=_now(),
            )
            if new_marker_values:
                payload["last_marker"] = {_INLINE_PERMISSION_MARKER: new_marker_values[-1]}
            write_status(status_path, payload)

        written: list[dict[str, object]] = []
        failed: list[dict[str, object]] = []
        for key, decision, option_id, summary in decisions:
            try:
                await asyncio.to_thread(
                    permits.write_decision,
                    resolved_permission_dir,
                    key,
                    decision,
                    option_id,
                )
            except Exception as exc:
                failed_summary = dict(summary)
                failed_summary["write_error"] = f"{type(exc).__name__}: {exc}"
                failed.append(failed_summary)
                continue
            decided = dict(summary)
            decided["decided_at"] = _now()
            written.append(decided)

        async with status_lock:
            history = list(payload.get("permission_decisions") or [])
            history.extend(written)
            payload.update(
                permission_decisions=history[-50:],
                pending_permissions=failed,
                updated_at=_now(),
            )
            if failed:
                payload["permission_relay_error"] = failed[-1].get("write_error")
            write_status(status_path, payload)

    try:
        await update_status(lease_id=lease_id)
    except asyncio.CancelledError:
        if sigterm_received is not None and sigterm_received():
            payload.update(
                {
                    "state": "failed",
                    "ok": False,
                    "error": {"code": -int(signal.SIGTERM), "message": "sigterm"},
                    "terminated_by_signal": "SIGTERM",
                }
            )
            write_status(status_path, payload)
            if lease_id:
                with contextlib.redirect_stdout(io.StringIO()):
                    goalflight_capacity.cmd_release(
                        argparse.Namespace(
                            lease_id=lease_id,
                            state=payload["state"],
                            reason=payload["error"],
                            keep=True,
                        )
                    )
            return payload
        raise

    async def mark_heartbeat_terminal(outcome: str, error: dict[str, object]) -> None:
        nonlocal heartbeat_outcome, heartbeat_error, wedged_by_heartbeat
        if conn is not None:
            setattr(conn, "killed_by_heartbeat", True)
            setattr(conn, "heartbeat_outcome", outcome)
        async with status_lock:
            heartbeat_outcome = outcome
            heartbeat_error = error
            wedged_by_heartbeat = outcome == "wedged"
            payload.update(
                state=outcome,
                ok=False,
                error=error,
                killed_by_heartbeat=True,
                wedged_by_heartbeat=wedged_by_heartbeat,
                updated_at=_now(),
            )
            write_status(status_path, payload)

    async def mark_runaway_terminal(error: dict[str, object]) -> None:
        nonlocal heartbeat_outcome, heartbeat_error, wedged_by_heartbeat
        if conn is not None:
            setattr(conn, "killed_by_heartbeat", True)
            setattr(conn, "heartbeat_outcome", "failed")
        async with status_lock:
            heartbeat_outcome = "failed"
            heartbeat_error = error
            wedged_by_heartbeat = False
            payload.update(
                state="failed",
                ok=False,
                error=error,
                killed_by_heartbeat=True,
                wedged_by_heartbeat=False,
                runaway_reason=error.get("reason") or error.get("message"),
                updated_at=_now(),
            )
            write_status(status_path, payload)

    async def heartbeat_loop() -> None:
        nonlocal detach_worker, heartbeat_outcome, stall_wake_count
        dead_samples = 0
        last_sample_progress_seen = 0
        last_sample_turn_completed_count = 0
        prev_wall = time.time()
        prev_active = active_monotonic()
        total_paused_s = 0.0
        # Created only after a successful handshake, so it tracks the single
        # committed worker. Exit early once that worker exits (grok 2026-05-20
        # P2) rather than sampling a dead pid until the outer finally cancels us.
        while True:
            if proc is None or proc.returncode is not None:
                return
            await relay_inline_permissions()
            wall_now = time.time()
            active_now = active_monotonic()
            freeze_s = system_sleep_pause_s(
                prev_wall=prev_wall,
                prev_active=prev_active,
                wall_now=wall_now,
                active_now=active_now,
                heartbeat_interval_s=cfg.heartbeat_interval,
            )
            if freeze_s > 0:
                total_paused_s += freeze_s
                # Offload the lease-expiry extend to a thread (grok D3 P1): it takes
                # the contended StateLock (fcntl.flock) + json load/save, so calling
                # it directly would block the async heartbeat loop. Matches the
                # to_thread treatment of the other hot-path I/O (cpu sampling).
                with contextlib.suppress(Exception):
                    await asyncio.to_thread(
                        goalflight_capacity.extend_active_lease_expiry, lease_id, freeze_s
                    )
                await update_status(
                    note=system_sleep_pause_note(freeze_s, total_paused_s),
                    total_paused_s=round(total_paused_s, 3),
                    heartbeat_at=_now(),
                )
                prev_wall, prev_active = wall_now, active_now
                await asyncio.sleep(cfg.heartbeat_interval)
                continue
            prev_wall, prev_active = wall_now, active_now
            pgid = payload.get("pgid") or process_group_id(proc.pid) or proc.pid
            cpu_pct = await asyncio.to_thread(pgroup_cpu_pct, pgid)
            now_mono = active_now
            snapshot = activity.snapshot(now_mono)
            seen = int(snapshot["raw_events_seen"])
            progress_seen = int(snapshot["wedge_progress_seen"])
            outstanding_count = int(snapshot["outstanding_count"])
            dropped_frames = int(snapshot.get("dropped_frames", 0))
            quiet_for_s = float(snapshot["quiet_for_s"])
            progress_quiet_s = float(snapshot["progress_quiet_for_s"])
            turn_in_flight = bool(snapshot.get("turn_in_flight"))
            turn_silent_for_s = float(snapshot.get("turn_silent_for_s", 0.0))
            turn_completed_for_s = float(snapshot.get("turn_completed_for_s", 0.0))
            turn_completed_count = int(snapshot.get("turn_completed_count", 0))
            pid_alive = proc.returncode is None
            if (
                not turn_in_flight
                and turn_completed_count > last_sample_turn_completed_count
            ):
                dead_samples = 0
                last_sample_progress_seen = progress_seen
                last_sample_turn_completed_count = turn_completed_count
            # Absolute per-tool wall is checked EVERY tick, BEFORE the inline-hold
            # short-circuit, so the max_tool_s backstop for a never-answered inline
            # hold (added to activity.timed_out) is actually reachable. A tool (or
            # held permission) outstanding longer than --max-tool-s is stuck
            # regardless of CPU (it may be CPU-busy in a hung retry loop, or CPU may
            # be unsamplable). Gating it on CPU≤ε would let those hang forever
            # (codex 2026-05-20 P1). The grace (don't wedge while a tool is
            # outstanding) still applies UP TO the wall; past it, it's the wedge.
            timed_out_tool = activity.timed_out(now_mono, cfg.max_tool_s)
            if timed_out_tool is not None and pid_alive:
                tool_id, age_s = timed_out_tool
                await mark_heartbeat_terminal(
                    "tool_timeout",
                    {
                        "code": -1,
                        "message": "tool_timeout",
                        "toolCallId": tool_id,
                        "age_s": round(age_s, 3),
                    },
                )
                await conn.kill()
                return
            if (
                awaiting_user_confirm
                and turn_in_flight
                and outstanding_count == 0
                and pid_alive
            ):
                overdue = await expire_midturn_user_confirms()
                if not overdue:
                    # A prose USER-CONFIRM is not an ACP permission hold. Before
                    # its absolute awake-time deadline the worker may keep doing
                    # safe work or wait to end its turn without being reaped as
                    # silence. Once the deadline crosses, the question is
                    # explicitly denied above and every normal silence/wedge
                    # backstop below is active again.
                    dead_samples = 0
                    confirm_pending, confirm_resolved = user_confirm_status_lists()
                    await update_status(
                        state="awaiting_user_confirm",
                        user_confirm_pending=confirm_pending,
                        user_confirm_resolved=confirm_resolved,
                        worker_pid=proc.pid,
                        pgid=pgid,
                        worker_alive=pid_alive,
                        pgroup_cpu_pct=cpu_pct,
                        heartbeat_at=_now(),
                        heartbeat_dead_samples=dead_samples,
                        wedge_progress_seen=progress_seen,
                        outstanding_tool_calls=outstanding_count,
                        acp_dropped_frames=dropped_frames,
                        quiet_for_s=round(quiet_for_s, 3),
                        progress_quiet_for_s=round(progress_quiet_s, 3),
                        turn_in_flight=True,
                        turn_silent_for_s=round(turn_silent_for_s, 3),
                    )
                    await asyncio.sleep(cfg.heartbeat_interval)
                    continue
            if (
                liveness_profile == "remote_api"
                and turn_in_flight
                and outstanding_count == 0
                and pid_alive
            ):
                if turn_silent_for_s >= remote_turn_silence_s:
                    await mark_heartbeat_terminal(
                        "remote_turn_silence",
                        {
                            "code": -1,
                            "message": "remote_turn_silence",
                            "turn_silent_for_s": round(turn_silent_for_s, 3),
                            "remote_turn_silence_s": round(remote_turn_silence_s, 3),
                        },
                    )
                    with contextlib.suppress(Exception):
                        await asyncio.wait_for(conn.cancel(), timeout=1.0)
                    if remote_turn_cancel_grace_s > 0:
                        await asyncio.sleep(remote_turn_cancel_grace_s)
                    await conn.kill()
                    return
                dead_samples = 0
                await update_status(
                    state="running_remote_turn",
                    worker_pid=proc.pid,
                    pgid=pgid,
                    worker_alive=pid_alive,
                    pgroup_cpu_pct=cpu_pct,
                    heartbeat_at=_now(),
                    heartbeat_dead_samples=dead_samples,
                    wedge_progress_seen=progress_seen,
                    outstanding_tool_calls=outstanding_count,
                    acp_dropped_frames=dropped_frames,
                    quiet_for_s=round(quiet_for_s, 3),
                    progress_quiet_for_s=round(progress_quiet_s, 3),
                    progress_stall_s=progress_stall_s,
                    turn_in_flight=True,
                    turn_silent_for_s=round(turn_silent_for_s, 3),
                    liveness_profile=liveness_profile,
                    remote_turn_silence_s=round(remote_turn_silence_s, 3),
                )
                await asyncio.sleep(cfg.heartbeat_interval)
                continue
            if activity.has_inline_holds() and pid_alive:
                # The inline permission router is holding a request open awaiting a
                # controller/user decision (permission_mode="inline"). The worker is
                # paused by design; publish a visible state and skip the SILENCE-class
                # wedge checks (progress stall / max_quiet / CPU dead-samples) this
                # tick -- but NOT the absolute max_tool_s wall above, and the
                # handler's own inline timeout + finally-release still bound the hold.
                await update_status(
                    state="awaiting_permission",
                    worker_pid=proc.pid,
                    pgid=pgid,
                    worker_alive=pid_alive,
                    pgroup_cpu_pct=cpu_pct,
                    heartbeat_at=_now(),
                    heartbeat_dead_samples=dead_samples,
                    wedge_progress_seen=progress_seen,
                    outstanding_tool_calls=outstanding_count,
                    acp_dropped_frames=dropped_frames,
                    quiet_for_s=round(quiet_for_s, 3),
                    progress_quiet_for_s=round(progress_quiet_s, 3),
                    progress_stall_s=progress_stall_s,
                    inline_held=int(snapshot.get("inline_held", 0)),
                    turn_in_flight=turn_in_flight,
                    turn_silent_for_s=round(turn_silent_for_s, 3),
                    liveness_profile=liveness_profile,
                    remote_turn_silence_s=round(remote_turn_silence_s, 3),
                )
                await asyncio.sleep(cfg.heartbeat_interval)
                continue
            pending_steer_entries: list[dict] = []
            if pid_alive:
                seen_steer = {
                    int(seq)
                    for seq in list(payload.get("steer_delivered_seqs") or [])
                    + list(payload.get("steer_acked_seqs") or [])
                    if isinstance(seq, int)
                }
                for seq in list(pending_steer_first_seen_mono):
                    if seq in seen_steer:
                        pending_steer_first_seen_mono.pop(seq, None)
                with contextlib.suppress(Exception):
                    pending_steer_entries = _pending_steer_entries(steer_file, seen_steer)
                for entry in pending_steer_entries:
                    pending_steer_first_seen_mono.setdefault(int(entry["seq"]), now_mono)
                pending_seqs = {int(entry["seq"]) for entry in pending_steer_entries}
                for seq in list(pending_steer_first_seen_mono):
                    if seq not in pending_seqs:
                        pending_steer_first_seen_mono.pop(seq, None)
            turn_boundary_deadline_s = max(
                0.0,
                between_turn_steer_grace_s - turn_completed_for_s,
            ) if turn_completed_count > 0 else 0.0
            explicit_boundary_deadline_s = (
                max(0.0, awaiting_next_prompt_deadline_mono - now_mono)
                if awaiting_next_prompt_deadline_mono > 0
                else 0.0
            )
            awaiting_steer_boundary = (
                not turn_in_flight
                and max(turn_boundary_deadline_s, explicit_boundary_deadline_s) > 0
            )
            if (
                pid_alive
                and awaiting_steer_boundary
            ):
                # At a completed turn boundary, allow a short quiet window for
                # late mailbox appends before starting the next prompt. Mid-turn
                # steers are only recorded as pending; they must not suppress the
                # silence-class wedge backstops.
                dead_samples = 0
                last_sample_progress_seen = progress_seen
                await update_status(
                    state=(
                        "awaiting_user_confirm"
                        if awaiting_user_confirm
                        else "awaiting_next_prompt"
                    ),
                    steer_pending_seqs=[
                        int(entry["seq"]) for entry in pending_steer_entries
                    ] or payload.get("steer_pending_seqs", []),
                    steer_delivery_state="between_turns",
                    worker_pid=proc.pid,
                    pgid=pgid,
                    worker_alive=pid_alive,
                    pgroup_cpu_pct=cpu_pct,
                    heartbeat_at=_now(),
                    heartbeat_dead_samples=dead_samples,
                    wedge_progress_seen=progress_seen,
                    outstanding_tool_calls=outstanding_count,
                    acp_dropped_frames=dropped_frames,
                    quiet_for_s=round(quiet_for_s, 3),
                    progress_quiet_for_s=round(progress_quiet_s, 3),
                    progress_stall_s=progress_stall_s,
                    turn_in_flight=turn_in_flight,
                    turn_silent_for_s=round(turn_silent_for_s, 3),
                    turn_completed_for_s=round(turn_completed_for_s, 3),
                    awaiting_next_prompt_deadline_s=round(
                        max(turn_boundary_deadline_s, explicit_boundary_deadline_s),
                        3,
                    ),
                )
                await asyncio.sleep(cfg.heartbeat_interval)
                continue
            if turn_in_flight and progress_stall_decision(
                pid_alive=pid_alive,
                progress_quiet_s=progress_quiet_s,
                progress_stall_s=progress_stall_s,
                outstanding_count=outstanding_count,
            ):
                stall_error = {
                    "code": -1,
                    "message": "progress_stall",
                    "progress_quiet_s": round(progress_quiet_s, 3),
                    "progress_stall_s": round(progress_stall_s, 3),
                }
                if stall_kill:
                    await mark_heartbeat_terminal("wedged", stall_error)
                    await conn.kill()
                    return
                stall_wake_count += 1
                detach_worker = True
                marker_text = (
                    "progress_stall "
                    f"quiet={round(progress_quiet_s, 3)}s "
                    f"limit={round(progress_stall_s, 3)}s"
                )
                markers = stalled_markers(marker_text)
                async with status_lock:
                    payload.update(
                        state="stalled",
                        ok=False,
                        error=stall_error,
                        killed_by_heartbeat=False,
                        wedged_by_heartbeat=False,
                        stalled_by_heartbeat=True,
                        stall_wake_count=stall_wake_count,
                        stall_wake_cap=DEFAULT_STALL_WAKE_CAP,
                        markers=markers,
                        last_marker={"STALLED": marker_text},
                        worker_pid=proc.pid,
                        pgid=pgid,
                        worker_alive=pid_alive,
                        worker_still_alive=pid_alive,
                        heartbeat_at=_now(),
                        progress_quiet_for_s=round(progress_quiet_s, 3),
                        progress_stall_s=progress_stall_s,
                        outstanding_tool_calls=outstanding_count,
                        acp_dropped_frames=dropped_frames,
                    )
                    write_status(status_path, payload)
                # Disarm the ghost reaper for this intentionally-detached worker
                # BEFORE the runner unwinds and exits: rewrite the pidfile entry
                # with detached=true so a later cleanup_ghosts (this orchestrator's
                # next dispatch or a sibling project sharing the pidfile dir) skips
                # it instead of SIGKILLing the still-running worker.
                mark_connection_detached(proc.pid)
                stall_detach_event.set()
                return
            if (
                cfg.max_quiet_s > 0
                and outstanding_count == 0
                and quiet_for_s >= cfg.max_quiet_s
                and pid_alive
                and cpu_confirmed_idle(cpu_pct, cfg.cpu_epsilon)
            ):
                await mark_heartbeat_terminal(
                    "wedged",
                    {
                        "code": -1,
                        "message": "max_quiet_s",
                        "quiet_for_s": round(quiet_for_s, 3),
                        "cpu_pct": cpu_pct,
                    },
                )
                await conn.kill()
                return
            decision = heartbeat_wedge_decision(
                pid_alive=pid_alive,
                pgroup_cpu=cpu_pct,
                wedge_progress_seen=progress_seen,
                previous_wedge_progress_seen=last_sample_progress_seen,
                outstanding_count=outstanding_count,
                cpu_epsilon_pct=cfg.cpu_epsilon,
                previous_dead_samples=dead_samples,
                wedge_samples=cfg.wedge_samples,
            )
            dead_samples = decision.dead_samples
            last_sample_progress_seen = progress_seen
            await update_status(
                worker_pid=proc.pid,
                pgid=pgid,
                worker_alive=pid_alive,
                pgroup_cpu_pct=cpu_pct,
                heartbeat_at=_now(),
                heartbeat_dead_samples=dead_samples,
                wedge_progress_seen=progress_seen,
                outstanding_tool_calls=outstanding_count,
                acp_dropped_frames=dropped_frames,
                quiet_for_s=round(quiet_for_s, 3),
                progress_quiet_for_s=round(progress_quiet_s, 3),
                progress_stall_s=progress_stall_s,
                turn_in_flight=turn_in_flight,
                turn_silent_for_s=round(turn_silent_for_s, 3),
            )
            if decision.wedged:
                await mark_heartbeat_terminal(
                    "wedged",
                    {"code": -1, "message": "wedged_by_heartbeat"},
                )
                await conn.kill()
                return
            await asyncio.sleep(cfg.heartbeat_interval)

    async def note_event(event: dict) -> None:
        nonlocal runaway_terminal_error
        now_mono = active_monotonic()
        idle_gate.note_event()  # any incoming SDK observer event keeps idle gate alive
        snapshot = activity.snapshot(now_mono)
        seen = int(snapshot["raw_events_seen"])
        progress_seen = int(snapshot["wedge_progress_seen"])
        outstanding_count = int(snapshot["outstanding_count"])
        dropped_frames = int(snapshot.get("dropped_frames", 0))
        progress_quiet_s = float(snapshot["progress_quiet_for_s"])
        turn_in_flight = bool(snapshot.get("turn_in_flight"))
        turn_silent_for_s = float(snapshot.get("turn_silent_for_s", 0.0))
        runaway_error = None
        if runaway_terminal_error is None:
            runaway_error = runaway_caps.observe(event, events_seen=seen)
        await update_status(
            state="running",
            events_seen=seen,
            consecutive_tool_errors=runaway_caps.consecutive_tool_errors,
            repeated_tool_error_tool=(
                runaway_caps.repeated_tool if runaway_caps.consecutive_tool_errors else None
            ),
            last_tool_error=(
                runaway_caps.last_tool_error if runaway_caps.consecutive_tool_errors else None
            ),
            acp_dropped_frames=dropped_frames,
            wedge_progress_seen=progress_seen,
            progress_quiet_for_s=round(progress_quiet_s, 3),
            progress_stall_s=progress_stall_s,
            last_event_at=_now(),
            last_event_kind=str(snapshot.get("last_event_kind") or _event_kind(event)),
            outstanding_tool_calls=outstanding_count,
            turn_in_flight=turn_in_flight,
            turn_silent_for_s=round(turn_silent_for_s, 3),
            worker_pid=proc.pid if proc else None,
            pgid=payload.get("pgid"),
            worker_alive=(proc.returncode is None) if proc else False,
        )
        if runaway_error is not None:
            runaway_terminal_error = runaway_error
            await mark_runaway_terminal(runaway_error)
            if conn is not None:
                await conn.kill()

    async def on_idle_check() -> bool:
        # Runner's CPU-aware liveness gate for session_prompt's idle path — the
        # codex P1 fix. A worker can be silent yet healthy (running_quiet):
        # grinding a long test/compile with no agent_message_chunks. The gate
        # samples process-group CPU (with the transient-ps-failure grace):
        # > epsilon ⇒ keep waiting (the false-positive killer); at/below epsilon
        # (or unsamplable after grace), OR past the running_quiet hard wall ⇒
        # wedged ⇒ let the runner cancel.
        if proc is None or proc.returncode is not None:
            return False
        if awaiting_user_confirm and activity.turn_in_flight():
            overdue = await expire_midturn_user_confirms()
            if not overdue:
                confirm_pending, confirm_resolved = user_confirm_status_lists()
                await update_status(
                    state="awaiting_user_confirm",
                    user_confirm_pending=confirm_pending,
                    user_confirm_resolved=confirm_resolved,
                    worker_alive=True,
                    heartbeat_at=_now(),
                    turn_in_flight=True,
                )
                return True
        if liveness_profile == "remote_api" and activity.turn_in_flight():
            now_mono = active_monotonic()
            await update_status(
                state="running_remote_turn",
                worker_alive=True,
                heartbeat_at=_now(),
                turn_in_flight=True,
                turn_silent_for_s=round(activity.turn_silent_for(now_mono), 3),
                outstanding_tool_calls=activity.outstanding_count(now_mono),
                liveness_profile=liveness_profile,
                remote_turn_silence_s=round(remote_turn_silence_s, 3),
            )
            return True
        pgid = payload.get("pgid") or process_group_id(proc.pid) or proc.pid
        keep_waiting, cpu = await idle_gate.keep_waiting(
            lambda: asyncio.to_thread(pgroup_cpu_pct, pgid)
        )
        await update_status(
            state="running_quiet" if keep_waiting else "wedged",
            pgid=pgid,
            pgroup_cpu_pct=cpu,
            worker_alive=(proc.returncode is None),
            heartbeat_at=_now(),
        )
        return keep_waiting

    async def mark_attempt(attempt: int, p: asyncio.subprocess.Process) -> None:
        nonlocal proc
        proc = p  # publish to heartbeat/note_event/on_idle closures + finally
        pgid = process_group_id(p.pid) or p.pid
        with contextlib.suppress(Exception):
            attach_worker_to_lease(p.pid)
        updates: dict[str, object] = dict(
            worker_pid=p.pid, pgid=pgid, worker_alive=True, state="handshaking"
        )
        if attempt > 0:
            updates["handshake_attempt"] = attempt + 1
        await update_status(**updates)

    # Non-Codex agents keep the captured environment and avoid importing the
    # optional dispatcher-side Codex integration.
    is_local_codex = (
        not goalflight_compat.is_windows()
        and goalflight_ledger.infer_engine(cfg.agent) == "codex"
    )
    if is_local_codex:
        setattr(cfg, "_codex_dispatch_home_resolved", False)
        # The adapter spawn consumes this captured dict, so re-finalize the
        # Codex environment only after capacity succeeds and immediately before
        # the starting-ledger/spawn block.
        spawn_env = dict(spawn_env)
        dispatch_module = None
        try:
            import goalflight_dispatch as dispatch_module
        except BaseException:
            dispatch_module = None
        codex_home = None
        if dispatch_module is not None:
            try:
                codex_home, effective_account = dispatch_module.resolve_codex_home(
                    project_root,
                    getattr(cfg, "account", None),
                    dispatch_id,
                )
            except BaseException:
                codex_home, effective_account = None, None
        if codex_home is not None:
            spawn_env["CODEX_HOME"] = codex_home
            setattr(cfg, "_codex_dispatch_home_resolved", True)
        if getattr(cfg, "context_mode", "enabled") == "disabled":
            try:
                context_mode_defined = (
                    dispatch_module.codex_context_mode_defined(spawn_env)
                    if dispatch_module is not None
                    else _codex_context_mode_defined(spawn_env)
                )
            except BaseException:
                context_mode_defined = _codex_context_mode_defined(spawn_env)
            if not context_mode_defined:
                cfg.context_mode = "enabled"

    try:
        if worktree_path is not None:
            try:
                created = create_and_route_dispatch_worktree(cfg, project_root, dispatch_id)
            except Exception as e:
                await update_status(
                    state="failed_worktree",
                    ok=False,
                    error=f"{type(e).__name__}: {e}",
                )
                return payload
            worker_cwd = str(created)
            command, acp_args = agent_command(
                cfg.agent,
                model=getattr(cfg, "model", None),
            )
            acp_args = _codex_workspace_write_acp_args(
                cfg.agent, acp_args, cwd=worker_cwd, os_sandbox=os_sandbox_profile
            )
            await update_status(
                state="worktree_created",
                worker_cwd=worker_cwd,
                worktree_path=str(created),
            )
        try:
            if os_sandbox_profile != OS_SANDBOX_OFF:
                prepare_os_sandbox_command(
                    command,
                    acp_args,
                    cwd=worker_cwd,
                    os_sandbox=os_sandbox_profile,
                    agent=cfg.agent,
                )
        except OsSandboxError as e:
            await update_status(state="blocked_os_sandbox", error=str(e))
            return payload
        cleanup_ghosts()
        record_ledger_state(worker_pid=None, state="starting")
        ledger_recorded = True
        # Spawn + handshake, retrying once on AcpError (the intermittent
        # codex-acp wedge). The helper kills a wedged worker before respawning,
        # so no identity-matched PID is ever left alive. Status progresses
        # starting → handshaking [→ handshake_attempt=2] → running.
        # StartupGate serializes the heavy startup of fragile adapters (the
        # Claude TUI) so concurrent launches don't starve each other on init;
        # it releases the instant the handshake completes, so TURNS overlap.
        # R26: controller-discretion permission policy. When the caller passed
        # --permission-allow-tool-title-pattern flags, build a policy that
        # auto-allows matching titles before falling through to the default
        # scope-aware policy. Otherwise leave permission_policy=None (the worker
        # uses default_permission_policy via the client).
        spawn_os_sandbox_profile = os_sandbox_profile
        allow_patterns_raw = getattr(cfg, "permission_allow_tool_title_pattern", None) or []
        if allow_patterns_raw:
            try:
                _compiled_patterns = [_re.compile(p) for p in allow_patterns_raw]
                # Sweep B P1 follow-up: wire the sandbox-aware base into the
                # title-allow policy. The client's spawn path only installs
                # permission_policy_for_dispatch when permission_policy is
                # None — when we pass a title-allow policy, we must compose
                # the sandbox-aware policy as its base so --os-sandbox
                # actually auto-allows in-cwd execute/fetch under the
                # documented contract.
                policy_os_sandbox_profile = getattr(cfg, "os_sandbox", OS_SANDBOX_OFF)
                spawn_os_sandbox_profile = policy_os_sandbox_profile
                try:
                    from goalflight_acp_client import permission_policy_for_dispatch
                    base_policy = permission_policy_for_dispatch(policy_os_sandbox_profile)
                except ImportError:
                    base_policy = None  # default to default_permission_policy
                permission_policy = make_title_allow_policy(
                    _compiled_patterns, base=base_policy
                )
            except _re.error as exc:
                # Invalid regex — fail loud rather than silently mis-authorize.
                # Return a dict matching the function's declared `-> dict` return
                # type so callers reading the return value get a consistent
                # shape; the status JSON has already been updated identically.
                _err = {"code": -1, "message": f"invalid --permission-allow-tool-title-pattern: {exc}"}
                await update_status(state="failed", error=_err)
                return {"state": "failed", "error": _err}
            # Broad-pattern + sandbox-off audit warning: even with the
            # post-fix layering (sweep B P1), execute/fetch escalate when
            # sandbox is off. A `.*` pattern paired with sandbox-off means
            # workers needing execute/fetch will block on every tool call.
            # Warn the operator at startup so they know to add --os-sandbox
            # read-only (or scope patterns more precisely).
            # Reuse policy_os_sandbox_profile from base-policy wiring above.
            # Broaden the "broad pattern" detection beyond exact strings:
            # any pattern that matches an arbitrary string of safe-titles
            # qualifies. Probe by compiling and searching against representative
            # benign titles; if ALL match, treat as broad.
            sentinel_titles = (
                "read foo.txt", "edit bar.py", "search docs", "run ls",
            )
            broad: list[str] = []
            for raw, compiled in zip(allow_patterns_raw, _compiled_patterns):
                if all(compiled.search(t) for t in sentinel_titles):
                    broad.append(raw)
            if broad and policy_os_sandbox_profile in ("off", "none", "host-default"):
                import sys as _sys
                _sys.stderr.write(
                    "goalflight_acp_run: WARNING — broad title-allow pattern(s) "
                    f"{broad!r} with --os-sandbox=off. Execute/fetch tool calls "
                    "will still escalate (hard gate). Either pair with "
                    "--os-sandbox=read-only (workers get auto-allow on "
                    "execute/fetch via sandbox backstop) or scope patterns more "
                    "precisely. See scripts/goalflight_acp_run.py "
                    "make_title_allow_policy docstring.\n"
                )
        else:
            permission_policy = None
        if sandbox_fallback is not None:
            # Read-only is a hard posture: title allowlists must not re-enable
            # located edits after the OS sandbox has been removed.
            permission_policy = _read_only_permission_policy

        async with StartupGate(cfg.agent):
            proc, conn = await spawn_and_handshake_with_retry(
                command,
                acp_args,
                agent=cfg.agent,
                session_id=cfg.session_id,
                cwd=worker_cwd,
                activity=activity,
                on_attempt=mark_attempt,
                context_mode=(getattr(cfg, "context_mode", "enabled") != "disabled"),
                permission_mode=getattr(cfg, "permission_mode", "auto"),
                permission_dir=resolved_permission_dir,
                permission_inline_timeout_s=getattr(cfg, "permission_inline_timeout_s", None),
                permission_user_timeout_s=getattr(cfg, "permission_user_timeout_s", None),
                permission_policy=permission_policy,
                os_sandbox=spawn_os_sandbox_profile,
                session_model=getattr(cfg, "model", None),
                env=spawn_env,
                stderr_capture=agent_stderr_capture,
            )
        refresh_user_confirm_guard()
        await update_status(os_sandbox=getattr(conn, "os_sandbox_metadata", None) or payload["os_sandbox"])
        test_marker = goalflight_compat.allowed_env_override(
            "GOALFLIGHT_TEST_ACP_BEFORE_PID_LEDGER_UPDATE_FILE",
            "",
            test_mode=True,
        )
        if test_marker:
            with contextlib.suppress(Exception):
                Path(test_marker).write_text(str(proc.pid), encoding="utf-8")
        test_delay_raw = goalflight_compat.allowed_env_override(
            "GOALFLIGHT_TEST_ACP_BEFORE_PID_LEDGER_UPDATE_S",
            "",
            test_mode=True,
        )
        try:
            test_delay_s = float(test_delay_raw or "0")
        except ValueError:
            test_delay_s = 0.0
        if test_delay_s > 0:
            await asyncio.sleep(test_delay_s)
        record_ledger_state(worker_pid=proc.pid, state="running")
        activity.reset_progress_clock(active_monotonic())
        heartbeat_task = asyncio.create_task(heartbeat_loop())
        await update_status(state="running")
        prompt_results: list[PromptResult] = []
        delivered_steer_seqs: set[int] = set()
        acked_steer_seqs: set[int] = set()
        turn_index = 0
        next_prompt = prompt
        while True:
            seen_steer_seqs = delivered_steer_seqs | acked_steer_seqs
            (
                pending_steers,
                expired_questions,
                valid_confirm_reply_seqs,
            ) = await _read_user_confirm_replies_denying_expired(
                read_entries=lambda: _pending_steer_entries(
                    steer_file, seen_steer_seqs
                ),
                expired_questions=expired_user_confirm_questions,
                deny_expired=settle_expired_user_confirms,
                apply_replies=apply_user_confirm_replies,
            )
            if expired_questions:
                confirm_pending, confirm_resolved = user_confirm_status_lists()
                await update_status(
                    user_confirm_timeout_count=user_confirm_timeout_continuations,
                    user_confirm_pending=confirm_pending,
                    user_confirm_resolved=confirm_resolved,
                    steer_pending_seqs=[
                        int(entry["seq"]) for entry in pending_steers
                    ],
                )
            if not user_confirm_status_lists()[0]:
                awaiting_user_confirm = False
            turn_steer_seqs: set[int] = set()
            if pending_steers:
                pending_seqs = [int(entry["seq"]) for entry in pending_steers]
                turn_steer_seqs = set(pending_seqs)
                confirm_pending, confirm_resolved = user_confirm_status_lists()
                await update_status(
                    steer_pending_seqs=pending_seqs,
                    steer_delivery_state="delivering_at_turn_boundary",
                    steer_turn_index=turn_index,
                    user_confirm_pending=confirm_pending,
                    user_confirm_resolved=confirm_resolved,
                    user_confirm_rejected_reply_seqs=(
                        sorted(rejected_user_confirm_reply_seqs) or None
                    ),
                )
                next_prompt = (
                    _prompt_with_steer(
                        prompt,
                        steer_file,
                        pending_steers,
                        valid_confirm_reply_seqs,
                        user_confirm_questions,
                    )
                    if turn_index == 0
                    else _steer_turn_prompt(
                        steer_file,
                        pending_steers,
                        valid_confirm_reply_seqs,
                        user_confirm_questions,
                    )
                )
                delivered_steer_seqs.update(pending_seqs)
            elif turn_index == 0:
                next_prompt = prompt

            if getattr(getattr(conn, "client", None), "_prompt_in_use", False):
                await update_status(
                    steer_delivery_state="queued_until_prompt_free",
                    steer_pending_seqs=[int(entry["seq"]) for entry in pending_steers],
                )
                raise RuntimeError("internal error: ACP steer delivery attempted while prompt in flight")

            prompt_task = asyncio.create_task(run_prompt(
                conn,
                next_prompt,
                idle_timeout=cfg.idle_timeout,
                on_event=note_event,
                on_idle=on_idle_check,
                user_confirm_policy="route",
                on_user_confirm=route_user_confirm,
            ))
            await asyncio.sleep(0)
            awaiting_next_prompt = False
            awaiting_next_prompt_deadline_mono = 0.0
            stall_wait_task = asyncio.create_task(stall_detach_event.wait())
            done, _pending = await asyncio.wait(
                {prompt_task, stall_wait_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if prompt_task not in done:
                detach_worker = True
                if conn is not None:
                    # Cancel local awaiting without sending session/cancel; the
                    # worker remains alive and detached for orchestrator recovery.
                    conn.acp_session_id = None
                prompt_task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await prompt_task
                raise _AcpWorkerDetached()
            stall_wait_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await stall_wait_task
            turn_result = await prompt_task
            prompt_results.append(turn_result)
            turn_markers = extract_markers(turn_result.text)
            acked_steer_seqs.update(_steer_ack_seqs(turn_markers))
            for seq in delivered_steer_seqs | acked_steer_seqs:
                pending_steer_first_seen_mono.pop(seq, None)
            awaiting_next_prompt = bool(turn_result.ok and not turn_result.cancelled_for_marker)
            awaiting_next_prompt_deadline_mono = (
                active_monotonic() + between_turn_steer_grace_s
                if awaiting_next_prompt and between_turn_steer_grace_s > 0
                else 0.0
            )
            turn_index += 1
            current_turn_was_steer = bool(turn_steer_seqs)
            if not turn_result.ok or turn_result.cancelled_for_marker:
                awaiting_next_prompt = False
                awaiting_next_prompt_deadline_mono = 0.0
                confirm_pending, confirm_resolved = user_confirm_status_lists()
                await update_status(
                    steer_delivered_seqs=sorted(delivered_steer_seqs),
                    steer_acked_seqs=sorted(acked_steer_seqs),
                    steer_delivery_state="between_turns",
                    user_confirm_pending=confirm_pending,
                    user_confirm_resolved=confirm_resolved,
                )
                break
            (
                pending_after_turn,
                _expired_after_turn,
                _valid_after_turn_reply_seqs,
            ) = await _read_user_confirm_replies_denying_expired(
                read_entries=lambda: _pending_steer_entries(
                    steer_file,
                    delivered_steer_seqs | acked_steer_seqs,
                ),
                expired_questions=expired_user_confirm_questions,
                deny_expired=settle_expired_user_confirms,
                apply_replies=apply_user_confirm_replies,
            )
            confirm_pending, confirm_resolved = user_confirm_status_lists()
            if not confirm_pending:
                awaiting_user_confirm = False
            await update_status(
                steer_pending_seqs=[int(entry["seq"]) for entry in pending_after_turn],
                steer_delivered_seqs=sorted(delivered_steer_seqs),
                steer_acked_seqs=sorted(acked_steer_seqs),
                steer_delivery_state="between_turns",
                user_confirm_pending=confirm_pending,
                user_confirm_resolved=confirm_resolved,
            )
            if confirm_pending:
                if user_confirm_timeout_continuations >= 1:
                    # One fail-closed denial continuation already ran. A fresh
                    # USER-CONFIRM means the worker could not separate safe work
                    # from the guarded action; preserve the run and stop cleanly.
                    user_confirm_exhausted = True
                    awaiting_next_prompt = False
                    awaiting_next_prompt_deadline_mono = 0.0
                    break

                poll_s = min(max(float(cfg.heartbeat_interval or 0.1), 0.05), 0.25)
                confirm_deadline_mono = min(
                    user_confirm_deadlines_mono.get(
                        str(question["question_id"]),
                        awake_now() + user_confirm_timeout_s,
                    )
                    for question in confirm_pending
                )
                awaiting_user_confirm = True
                awaiting_next_prompt = True
                awaiting_next_prompt_deadline_mono = confirm_deadline_mono
                confirm_wait_scopes = {
                    _user_confirm_decision_scope(question)
                    for question in confirm_pending
                }
                await update_status(
                    state="awaiting_user_confirm",
                    user_confirm_pending=confirm_pending,
                    user_confirm_resolved=confirm_resolved,
                    user_confirm_timeout_at_mono=round(confirm_deadline_mono, 3),
                )
                pending_after_turn = await _wait_for_user_confirm_replies(
                    deadline_mono=confirm_deadline_mono,
                    poll_s=poll_s,
                    read_entries=lambda: _pending_steer_entries(
                        steer_file,
                        delivered_steer_seqs | acked_steer_seqs,
                    ),
                    apply_replies=apply_user_confirm_replies,
                    has_denial=lambda: any(
                        _user_confirm_decision_scope(question) in confirm_wait_scopes
                        and _user_confirm_denies_run(question)
                        for question in user_confirm_questions.values()
                    ),
                    awake_clock=awake_now,
                )
                denied_scopes = {
                    _user_confirm_decision_scope(question)
                    for question in user_confirm_questions.values()
                    if _user_confirm_denies_run(question)
                }
                provisional_yes_in_denied_scopes = [
                    question
                    for question in user_confirm_questions.values()
                    if not _user_confirm_is_resolved(question)
                    and question.get("provisional_yes_reply_steer_seq") is not None
                    and _user_confirm_decision_scope(question) in denied_scopes
                ]
                if provisional_yes_in_denied_scopes:
                    # A denial has already made this decision cohort permanently
                    # non-authorizing. Record the crossed affirmative now so the
                    # continuation can report it explicitly without emitting the
                    # bare authorization token or waiting out the full deadline.
                    _finalize_provisional_user_confirm_yes(
                        provisional_yes_in_denied_scopes,
                        user_confirm_questions,
                        accepted_user_confirm_reply_seqs,
                        wall_clock=wall_now,
                    )
                    refresh_user_confirm_guard()
                confirm_pending, confirm_resolved = user_confirm_status_lists()
                if confirm_pending and _user_confirm_deadline_reached(
                    confirm_deadline_mono,
                    awake_clock=awake_now,
                ):
                    # Timeout assumption: 600 awake seconds is long enough for
                    # normal mailbox polls. Silence is a DENIAL, never approval.
                    # The deadline wins over unrelated steer already waiting at
                    # this boundary. Send one correlated "no" turn so the worker
                    # can finish independent work; a repeated question blocks.
                    await settle_expired_user_confirms(confirm_pending)
                    pending_after_turn = _pending_steer_entries(
                        steer_file,
                        delivered_steer_seqs | acked_steer_seqs,
                    )
                    confirm_pending, confirm_resolved = user_confirm_status_lists()
                    await update_status(
                        user_confirm_timeout_count=user_confirm_timeout_continuations,
                        user_confirm_pending=confirm_pending,
                        user_confirm_resolved=confirm_resolved,
                        steer_pending_seqs=[
                            int(entry["seq"]) for entry in pending_after_turn
                        ],
                    )
                awaiting_user_confirm = False
                awaiting_next_prompt = False
                awaiting_next_prompt_deadline_mono = 0.0
                next_prompt = ""
                continue
            if (
                not pending_after_turn
                and not _terminal_turn_marker(turn_markers)
                and empty_between_turn_steer_poll_s > 0
            ):
                poll_s = min(max(float(cfg.heartbeat_interval or 0.1), 0.05), 0.25)
                empty_poll_deadline_mono = active_monotonic() + empty_between_turn_steer_poll_s
                # Mark the between-turn wait so the heartbeat's awaiting_steer_boundary
                # exemption applies to THIS bounded window (the turn ended cleanly and we
                # are quietly waiting for a late steer before the next prompt). Without
                # this the heartbeat would reap the legitimate between-turn wait as wedged.
                # Scoped to not-turn_in_flight + this deadline only, so a genuinely-stuck
                # mid-turn worker is unaffected.
                awaiting_next_prompt = True
                awaiting_next_prompt_deadline_mono = empty_poll_deadline_mono
                while active_monotonic() <= empty_poll_deadline_mono:
                    await asyncio.sleep(poll_s)
                    pending_after_turn = _pending_steer_entries(
                        steer_file,
                        delivered_steer_seqs | acked_steer_seqs,
                    )
                    if pending_after_turn:
                        # Found a late steer: end the wait + clear the exemption so the
                        # next turn (which sets turn_in_flight) is governed by the normal
                        # backstops, not this between-turn grace.
                        awaiting_next_prompt = False
                        awaiting_next_prompt_deadline_mono = 0.0
                        await update_status(
                            steer_pending_seqs=[int(entry["seq"]) for entry in pending_after_turn],
                            steer_delivery_state="between_turns",
                        )
                        break
                else:
                    # Poll window expired with no steer: clear the exemption.
                    awaiting_next_prompt = False
                    awaiting_next_prompt_deadline_mono = 0.0
            if not pending_after_turn:
                if _terminal_turn_marker(turn_markers):
                    awaiting_next_prompt = False
                    awaiting_next_prompt_deadline_mono = 0.0
                    break
                if current_turn_was_steer:
                    next_prompt = ""
                    continue
                awaiting_next_prompt = False
                awaiting_next_prompt_deadline_mono = 0.0
                break
            next_prompt = ""

        # No asynchronous status writer may survive into terminal
        # reconciliation. Otherwise a final heartbeat sampled during the
        # between-turn grace can overwrite state=complete with
        # state=awaiting_next_prompt after completion evidence is recorded.
        awaiting_user_confirm = False
        awaiting_next_prompt = False
        awaiting_next_prompt_deadline_mono = 0.0
        if heartbeat_task is not None:
            heartbeat_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await heartbeat_task
            heartbeat_task = None

        result = _merge_prompt_results(prompt_results)
        final_prompt_result = _last_prompt_result(prompt_results, result)
        markers = extract_markers(result.text)
        relay_markers = dict(payload.get("markers") or {})
        if relay_markers:
            merged_markers = dict(relay_markers)
            for kind, values in markers.items():
                merged_markers.setdefault(kind, [])
                merged_markers[kind].extend(
                    value for value in values if value not in merged_markers[kind]
                )
            markers = merged_markers
        # A harder terminal marker can cancel the turn before the streaming
        # callback observes a preceding USER-CONFIRM chunk. Reconcile the final
        # marker evidence so the unanswered question remains visible and denied.
        for question_text in markers.get(USER_CONFIRM_MARKER) or []:
            if any(
                question.get("text") == question_text
                for question in user_confirm_questions.values()
            ):
                continue
            await route_user_confirm(
                {
                    "origin": "worker_marker",
                    "text": question_text,
                    "result_text": result.text,
                }
            )
        async with status_lock:
            terminal_by_heartbeat = heartbeat_outcome or getattr(conn, "heartbeat_outcome", None)
            terminal_error = heartbeat_error
        state, error = decide_terminal_state(
            result_ok=result.ok,
            result_error=final_prompt_result.error,
            result_text=final_prompt_result.text,
            assistant_output=result.text,
            stop_reason=final_prompt_result.stop_reason,
            agent=cfg.agent,
            events_seen=int(payload.get("events_seen") or 0),
            heartbeat_outcome=terminal_by_heartbeat,
            killed_by_heartbeat=bool(getattr(conn, "killed_by_heartbeat", False)),
            cancelled_for_marker=result.cancelled_for_marker,
            early_marker=result.early_marker,
            heartbeat_error=terminal_error,
            terminal_marker_present=_terminal_turn_marker(markers),
            successful_terminal_marker=_successful_terminal_marker(markers),
        )
        state = _state_after_actionable_terminal_markers(state, markers)
        confirm_pending, confirm_resolved = user_confirm_status_lists()
        if state == "complete" and (confirm_pending or user_confirm_exhausted):
            state = "blocked_user_confirm"
            error = {
                "code": 0,
                "message": (
                    "user_confirm_unresolved_after_denial"
                    if user_confirm_exhausted
                    else "user_confirm_unresolved"
                ),
                "question_ids": [
                    str(question.get("question_id")) for question in confirm_pending
                ],
            }
        if state != "complete" and confirm_pending:
            deny_terminal_user_confirms(
                confirm_pending,
                reason=f"run_terminal:{state}",
            )
            confirm_pending, confirm_resolved = user_confirm_status_lists()
        denied_questions = [
            question
            for question in current_generation_user_confirms()
            if _user_confirm_records_denied_action(question)
        ]
        if state == "complete" and denied_questions:
            permission_denied = any(
                question.get("origin") == "permission_escalation"
                for question in denied_questions
            )
            state = (
                "blocked_permission_denied"
                if permission_denied
                else "blocked_user_confirm_denied"
            )
            error = {
                "code": 0,
                "message": "requested_action_denied_safe_work_preserved",
                "question_ids": [
                    str(question.get("question_id")) for question in denied_questions
                ],
            }
        # Sweep B P1 (2026-05-27): denied permissions can look like success.
        # If the worker had any inline permission auto-declined (timeout or
        # explicit deny) and no PERMISSION-OK-PROCEEDED marker says it
        # explicitly worked around the denial, refuse to record complete.
        # False negatives are recoverable (operator re-dispatches with
        # tighter scope or a permission override); false positives —
        # "complete" while the actual write was blocked — are not.
        if state == "complete" and result.permission_auto_declined:
            proceeded_ok = markers.get("PERMISSION-OK-PROCEEDED") or markers.get(
                "PERMISSION-OK-PROCEEDED:"
            )
            if not proceeded_ok:
                state = "blocked_permission_denied"
        runaway_terminal = _is_runaway_terminal(terminal_by_heartbeat, terminal_error)
        final_updates = {
            "state": state,
            "ok": result.ok and state == "complete",
            "stop_reason": result.stop_reason,
            "error": error,
            "markers": markers,
            "last_marker": {kind: values[-1] for kind, values in markers.items() if values} or None,
            "text_excerpt": result.text[-4000:],
            # Full output is evidence even when the turn blocks or fails. Keeping
            # it only for complete states destroyed all pre-question work.
            "result_text": result.text or payload.get("result_text"),
            "user_confirm_pending": confirm_pending,
            "user_confirm_resolved": confirm_resolved,
            "user_confirm_timeout_count": (
                user_confirm_timeout_continuations or None
            ),
            "had_denied_action": bool(denied_questions),
            "had_user_confirm_denial": any(
                question.get("controller_decision") == "no"
                for question in current_generation_user_confirms()
            ),
            "user_confirm_rejected_reply_seqs": (
                sorted(rejected_user_confirm_reply_seqs) or None
            ),
            "out_of_scope_writes": result.out_of_scope_writes,
            # Permission requests the orchestrator router escalated to the user
            # (boundary crossings it would not auto-allow). The individual ACP
            # action was denied before the question was routed. The worker may
            # continue safe work, but prose never retroactively authorizes it.
            "permission_pending": result.permission_escalations or None,
            # Inline permissions the orchestrator auto-declined on timeout. Sweep B
            # P1: this list NOW influences terminal state — if non-empty and the
            # worker didn't emit PERMISSION-OK-PROCEEDED, complete is downgraded
            # to blocked_permission_denied. Worker can emit
            # `PERMISSION-OK-PROCEEDED: <reason>` to explicitly opt out of the
            # downgrade when it knows it worked around the decline.
            "permission_auto_declined": result.permission_auto_declined or None,
            # Reconcile the heartbeat flags with the FINAL verdict. A tail-race
            # heartbeat may have written killed/wedged into the payload before
            # decide_terminal_state ruled the turn complete on a genuine
            # end_turn; without this the record would be self-contradictory
            # (state=complete, killed_by_heartbeat=true) and mislead an orchestrator
            # keying retry off the flag.
            "killed_by_heartbeat": state in ("wedged", "tool_timeout", "remote_turn_silence")
            or runaway_terminal,
            "wedged_by_heartbeat": state == "wedged",
        }
        if isinstance(error, dict) and error.get("reason") == "empty_session":
            final_updates["reason"] = "empty_session"
            final_updates["hint"] = error.get("hint")
        if runaway_terminal:
            final_updates["runaway_reason"] = terminal_error.get("reason") or terminal_error.get("message")
        if _permission_audit_surface_enabled(permission_mode):
            # Extra audit surface is intentionally scoped to inline permission
            # relays and the live ACP matrix. Normal dispatch status/ledger shape
            # stays unchanged, avoiding permanent payload creep.
            final_updates["permission_router_decisions"] = (
                result.permission_router_decisions[-MAX_PERMISSION_ROUTER_DECISIONS:] or None
            )
            final_updates["tool_calls"] = _compact_tool_call_summaries(result.tool_calls)
        else:
            payload.pop("permission_router_decisions", None)
            payload.pop("tool_calls", None)
        payload.update(final_updates)
        if user_confirm_questions and state == "complete":
            try:
                import goalflight_messages

                await asyncio.to_thread(
                    goalflight_messages.post_message,
                    dispatch_id=dispatch_id,
                    msg_type="result",
                    payload={
                        "complete": True,
                        "text": "USER-CONFIRM resolved; dispatch completed.",
                        "question_ids": sorted(user_confirm_questions),
                    },
                    messages_dir=goalflight_messages.default_messages_dir(),
                    source={
                        "node": "local",
                        "adapter": str(cfg.agent),
                        "transport": "acp-user-confirm",
                    },
                )
            except Exception as exc:
                payload["user_confirm_resolution_relay_error"] = (
                    f"{type(exc).__name__}: {exc}"
                )
    except asyncio.CancelledError:
        if sigterm_received is not None and sigterm_received():
            payload.update(
                {
                    "state": "failed",
                    "ok": False,
                    "error": {"code": -int(signal.SIGTERM), "message": "sigterm"},
                    "terminated_by_signal": "SIGTERM",
                }
            )
        else:
            raise
    except _AcpWorkerDetached:
        if payload.get("state") != "stalled":
            payload.update(
                {
                    "state": "stalled",
                    "ok": False,
                    "error": payload.get("error") or {"code": -1, "message": "progress_stall"},
                }
            )
    except Exception as e:
        async with status_lock:
            terminal_by_heartbeat = heartbeat_outcome or (getattr(conn, "heartbeat_outcome", None) if conn else None)
            terminal_error = heartbeat_error
        if terminal_by_heartbeat or (getattr(conn, "killed_by_heartbeat", False) if conn else False):
            payload.update({
                "state": terminal_by_heartbeat or "wedged",
                "ok": False,
                "error": terminal_error or {"code": -1, "message": terminal_by_heartbeat or "wedged"},
                "killed_by_heartbeat": True,
                "wedged_by_heartbeat": (terminal_by_heartbeat or "wedged") == "wedged",
            })
        else:
            payload.update({"state": "failed", "error": f"{type(e).__name__}: {e}"})
    finally:
        # State by exit path:
        #   success      → proc + conn = the committed worker.
        #   total-failure (both handshake attempts exhausted) → conn is None and
        #     proc is the LAST attempt (already reaped by the retry helper);
        #     sampling its now-dead CPU below is harmless and records which
        #     worker we gave up on.
        if heartbeat_task is not None:
            heartbeat_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await heartbeat_task
        if conn is not None and not detach_worker:
            with contextlib.suppress(Exception):
                await conn.close_gracefully()
        if proc is not None:
            snapshot = activity.snapshot(active_monotonic())
            payload.update(
                events_seen=int(snapshot["raw_events_seen"]),
                wedge_progress_seen=int(snapshot["wedge_progress_seen"]),
                outstanding_tool_calls=int(snapshot["outstanding_count"]),
                acp_dropped_frames=int(snapshot.get("dropped_frames", 0)),
                acp_dropped_frame_records=list(snapshot.get("dropped_frame_records", [])),
                progress_quiet_for_s=round(float(snapshot["progress_quiet_for_s"]), 3),
                progress_stall_s=progress_stall_s,
                turn_in_flight=bool(snapshot.get("turn_in_flight")),
                turn_silent_for_s=round(float(snapshot.get("turn_silent_for_s", 0.0)), 3),
                worker_alive=proc.returncode is None,
                pgid=payload.get("pgid") or process_group_id(proc.pid) or proc.pid,
                heartbeat_at=_now(),
            )
            payload["worker_still_alive"] = payload.get("worker_alive")
            payload["pgroup_cpu_pct"] = pgroup_cpu_pct(payload.get("pgid"))
        if ledger_recorded:
            with contextlib.redirect_stdout(io.StringIO()):
                goalflight_ledger.cmd_finish(
                    argparse.Namespace(
                        dispatch_id=dispatch_id,
                        state=payload.get("state", state),
                        reason=payload.get("error"),
                        terminal_state="stalled" if payload.get("state") == "stalled" else None,
                        elapsed_s=round(time.time() - run_started, 3),
                        worker_still_alive=payload.get("worker_still_alive"),
                    )
                )
        leave_lease_active = bool(detach_worker and proc is not None and proc.returncode is None)
        if leave_lease_active and proc is not None:
            with contextlib.suppress(Exception):
                detach_lease_to_worker(proc.pid, payload.get("error"))
        elif lease_id:
            with contextlib.redirect_stdout(io.StringIO()):
                goalflight_capacity.cmd_release(argparse.Namespace(lease_id=lease_id, state=payload.get("state", state), reason=payload.get("error"), keep=True))
    _attach_agent_stderr_tail(payload, agent_stderr_capture)
    write_status(status_path, payload)
    return payload


async def run(args: argparse.Namespace) -> dict:
    """Compatibility wrapper for callers that imported the old entrypoint."""
    return await run_acp_dispatch(args)


def normalized_acp_dispatch_cfg(args: argparse.Namespace) -> argparse.Namespace:
    values = vars(args).copy()
    if values.get("idle_timeout") is None:
        values["idle_timeout"] = 36000.0 if values.get("mode") == "goal" else 300.0
    if values.get("heartbeat_interval", 0) <= 0:
        values["heartbeat_interval"] = 15.0
    if values.get("wedge_samples", 0) <= 0:
        values["wedge_samples"] = 4
    if values.get("max_tool_s", 0) <= 0:
        values["max_tool_s"] = DEFAULT_MAX_TOOL_S
    if values.get("max_consecutive_tool_errors", 0) <= 0:
        values["max_consecutive_tool_errors"] = DEFAULT_MAX_CONSECUTIVE_TOOL_ERRORS
    if values.get("max_acp_events", 0) <= 0:
        values["max_acp_events"] = DEFAULT_MAX_ACP_EVENTS
    if not values.get("user_confirm_timeout_s"):
        values["user_confirm_timeout_s"] = os.environ.get(
            "GOALFLIGHT_USER_CONFIRM_TIMEOUT_S",
            DEFAULT_USER_CONFIRM_TIMEOUT_S,
        )
    try:
        values["user_confirm_timeout_s"] = float(values["user_confirm_timeout_s"])
    except (TypeError, ValueError):
        values["user_confirm_timeout_s"] = DEFAULT_USER_CONFIRM_TIMEOUT_S
    if values["user_confirm_timeout_s"] <= 0:
        values["user_confirm_timeout_s"] = DEFAULT_USER_CONFIRM_TIMEOUT_S
    if values.get("max_quiet_s", 0) <= 0:
        values["max_quiet_s"] = 3600.0
    if values.get("progress_stall_s", 0) <= 0:
        values["progress_stall_s"] = 300.0
    values["stall_kill"] = bool(values.get("stall_kill", False))
    values["session_id"] = values.get("session_id") or f"goalflight-{uuid.uuid4().hex[:8]}"
    values["read_only"] = bool(values.get("read_only", False))
    values["interactive"] = bool(values.get("interactive", False))
    values["priority"] = values.get("priority") or "normal"
    return argparse.Namespace(**values)


def acp_dispatch_exit_code(payload: dict) -> int:
    if payload.get("state") == "blocked_windows_dispatch":
        return 2
    return 0 if payload.get("state") == "complete" else 1


def write_windows_refusal_status(args: argparse.Namespace) -> tuple[dict, Path]:
    dispatch_id = args.dispatch_id or f"acp-{args.agent}-{uuid.uuid4().hex[:8]}"
    project_root = Path(args.cwd).resolve()
    status_path = _resolve_status_json_path(getattr(args, "status_json", None), dispatch_id)
    args.status_json = str(status_path)
    payload = {
        "schema": "goalflight.acp-run.v1",
        "dispatch_id": dispatch_id,
        "lease_id": None,
        "agent": args.agent,
        "session_id": args.session_id,
        "project_root": str(project_root),
        "worker_cwd": args.cwd,
        "worktree_mode": getattr(args, "worktree", "off"),
        "planned_worktree_path": None,
        "worktree_path": None,
        "state": "blocked_windows_dispatch",
        "ok": False,
        "error": goalflight_compat.windows_dispatch_refusal(),
        "next_step": "wsl --install; open an installed distro and run dispatch from the WSL checkout",
        "worker_pid": None,
        "pgid": None,
        "worker_alive": False,
        "worker_still_alive": False,
        "status_path": str(status_path),
        "updated_at": _now(),
    }
    write_status(status_path, payload)
    return payload, status_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="goal-flight ACP runner")
    parser.add_argument("--agent", required=True)
    parser.add_argument("--model", default=None,
                        help="Worker model id (grok/codex --model passthrough, e.g. "
                             "grok-composer-2.5-fast). Default = agent's own default.")
    parser.add_argument(
        "--install-slot",
        default=None,
        help="Gateway worker profile slot (~/.goal-flight/profiles/<slot>.env). "
        "Ignored for non-gateway agents.",
    )
    parser.add_argument("--cwd", required=True)
    parser.add_argument(
        "--worktree",
        choices=["off", "create"],
        default="off",
        help="Dispatch worktree mode. 'create' runs `git worktree add "
             "worktrees/<dispatch-id>/ HEAD` from the original --cwd and "
             "routes the worker --cwd to that per-dispatch worktree. The "
             "worktree is intentionally left on exit for operator inspection.",
    )
    parser.add_argument("--session-id", default=None)
    parser.add_argument("--dispatch-id")
    parser.add_argument(
        "--priority",
        choices=list(goalflight_capacity.PRIORITY_LANES),
        default="normal",
        help="Capacity lane. ACP dispatch inherits goalflight_dispatch --priority.",
    )
    parser.add_argument(
        "--capacity-wait-s",
        type=float,
        default=None,
        help="Capacity wait budget in seconds. Overrides GOALFLIGHT_CAPACITY_WAIT_S and lane default.",
    )
    parser.add_argument("--prompt-id")
    parser.add_argument("--prompt")
    parser.add_argument("--original-prompt-file", default=None, help=argparse.SUPPRESS)
    parser.add_argument("--prompt-text")
    parser.add_argument(
        "--prompt-b64",
        help="Base64-encoded prompt text for fleet SSH dispatch (avoids argv splitting).",
    )
    parser.add_argument(
        "--mode",
        choices=["one-shot", "goal"],
        default="one-shot",
        help="Dispatch mode. 'goal' raises the default idle-timeout to tolerate the "
             "long silent stretches of multi-hour goal-mode loops (a worker churning "
             "through a big test/compile may emit no events for tens of minutes). "
             "'one-shot' keeps a tight default so a wedged short dispatch is caught fast.",
    )
    parser.add_argument(
        "--idle-timeout",
        type=float,
        default=None,
        help="Seconds of zero agent events before giving up. Idle, NOT total runtime — "
             "the timer resets on every event. If unset, derived from --mode: "
             "one-shot=300 (5min), goal=36000 (10h). Pass 0 for no idle timeout "
             "(rely on PID liveness + the worker's terminal marker instead).",
    )
    parser.add_argument("--status-json")
    parser.add_argument(
        "--steer-file",
        default=None,
        help="File-IPC steer mailbox for between-turn ACP steer delivery.",
    )
    parser.add_argument(
        "--user-confirm-timeout-s",
        type=float,
        default=float(
            os.environ.get(
                "GOALFLIGHT_USER_CONFIRM_TIMEOUT_S",
                str(DEFAULT_USER_CONFIRM_TIMEOUT_S),
            )
        ),
        help=(
            "Awake-second deadline for a correlated unattended USER-CONFIRM reply "
            "(default 600). At the deadline, reconcile affirmatives whose durable "
            "awake-monotonic arrival stamp is at or before the cutoff; otherwise "
            "append one explicit 'no' continuation so safe independent work can "
            "finish. The question alone does not kill the turn, but after "
            "settlement normal silence/wedge limits apply again. A repeated "
            "question then blocks with partial output preserved."
        ),
    )
    parser.add_argument(
        "--context-mode",
        choices=["enabled", "disabled"],
        default="enabled",
        help="codex-acp only: whether the context-mode MCP server is active for "
             "this worker. 'enabled' (default) routes its elicitation through the "
             "ACP permission channel (auto-approved when in-scope); 'disabled' "
             "turns context-mode off for this dispatch entirely (no MCP "
             "elicitation surface). No effect on other adapters.",
    )
    parser.add_argument(
        "--os-sandbox",
        choices=OS_SANDBOX_ARG_CHOICES,
        default=OS_SANDBOX_OFF,
        help="Process-level OS sandbox for the worker subprocess. 'off'/'host-default' "
             "keeps current behavior. 'read-only' permits file reads plus temp writes "
             "but blocks repository writes. 'workspace-write' also permits writes under "
             "--cwd. On macOS this uses sandbox-exec; unsupported platforms fail closed "
             "with blocked_os_sandbox before capacity is acquired.",
    )
    parser.add_argument(
        "--permission-mode",
        choices=["auto", "inline"],
        default="auto",
        help="Escalation transport for boundary-crossing permission requests. "
             "'auto' (default): deny the individual request, route USER-CONFIRM "
             "to controller mail, and keep the turn alive for safe independent work. "
             "'inline': HOLD the worker open and "
             "authorize in place via the --permission-dir file IPC (an orchestrator "
             "drains the dir, optionally write_ack to defer to the user, writes a "
             "decision) -- it never re-dispatches. Two-phase awake-time timeout: if "
             "no ack/decision within --permission-inline-timeout-s, or no decision "
             "within --permission-user-timeout-s after an ack, the worker "
             "auto-declines that tool and CONTINUES. Inline across processes "
             "REQUIRES an explicit --permission-dir both sides share.",
    )
    parser.add_argument(
        "--permission-dir",
        default=None,
        help="Directory for inline permission request/decision files. Default: "
             "$GOAL_FLIGHT_PERMISSION_DIR or a PID-scoped temp dir (only "
             "discoverable in-process). Set explicitly so a separate orchestrator "
             "relay can find this worker's requests. No effect in 'auto' mode.",
    )
    parser.add_argument(
        "--permission-inline-timeout-s",
        type=float,
        default=None,
        help="Inline mode controller-responsiveness window: max awake-seconds to "
             "hold a permission waiting for the orchestrator to ack-or-decide before "
             "auto-declining (worker continues; default 180 = 3 min). No effect in "
             "'auto' mode.",
    )
    parser.add_argument(
        "--permission-user-timeout-s", type=float, default=None,
        help="Inline mode: after the orchestrator ACKs a permission (defer-to-user), "
             "max awake-seconds to wait for the user's decision before auto-declining "
             "(default 36000 = 10h). No effect in 'auto' mode.",
    )
    parser.add_argument(
        "--permission-allow-tool-title-pattern",
        action="append",
        default=[],
        metavar="REGEX",
        help="Controller-discretion auto-allow shortcut (R26). Regex pattern (Python "
             "re.search semantics) matched against the request_permission tool-call "
             "title. When the title matches any provided pattern, the runner "
             "auto-approves the request before falling through to the default "
             "scope-aware policy. Repeatable: pass multiple --permission-allow-tool-"
             "title-pattern flags to add patterns. Use to pre-authorize the worker's "
             "in-scope tool uses (e.g., a chunk authorized to run "
             "'./tests/run.sh' can pass '^./tests/run\\.sh$'). Does NOT bypass the "
             "destructive-op fail-closed checks in the base policy when patterns "
             "don't match — those still escalate to USER-CONFIRM.",
    )
    parser.add_argument(
        "--heartbeat-interval",
        type=float,
        default=float(os.environ.get("GOALFLIGHT_HEARTBEAT_INTERVAL", "15")),
        help="Seconds between runner status heartbeat samples.",
    )
    parser.add_argument(
        "--wedge-samples",
        type=int,
        default=int(os.environ.get("GOALFLIGHT_WEDGE_SAMPLES", "4")),
        help="Consecutive heartbeat dead samples required before killing a wedged ACP worker.",
    )
    parser.add_argument(
        "--max-tool-s",
        type=float,
        default=float(os.environ.get("GOALFLIGHT_MAX_TOOL_S", str(DEFAULT_MAX_TOOL_S))),
        help="Wall-clock safety net for one outstanding ACP tool call (default "
             f"{DEFAULT_MAX_TOOL_S:.0f}s, the harness clamp). Activity-naive — "
             "not the primary stuck detector. Lower values are for known-fast "
             "tasks only. Use --progress-stall-s for genuine hangs.",
    )
    # Accepted-watch SC-13 posture: numeric behavior-tuning knobs only; not
    # source, safety-disable, or command overrides, so no GOALFLIGHT_ALLOW gate.
    parser.add_argument(
        "--max-consecutive-tool-errors",
        type=int,
        default=int(os.environ.get(
            "GOALFLIGHT_MAX_CONSECUTIVE_TOOL_ERRORS",
            str(DEFAULT_MAX_CONSECUTIVE_TOOL_ERRORS),
        )),
        help="Runaway backstop: fail and kill the ACP worker after this many "
             "consecutive tool failure events without model progress or a "
             f"successful tool result (default {DEFAULT_MAX_CONSECUTIVE_TOOL_ERRORS}).",
    )
    parser.add_argument(
        "--max-acp-events",
        type=int,
        default=int(os.environ.get("GOALFLIGHT_MAX_ACP_EVENTS", str(DEFAULT_MAX_ACP_EVENTS))),
        help="Runaway backstop: fail and kill the ACP worker when total ACP "
             f"events exceed this cap (default {DEFAULT_MAX_ACP_EVENTS}). "
             "Sized for runaway loops, not normal performance limiting.",
    )
    parser.add_argument(
        "--max-quiet-s",
        type=float,
        default=float(os.environ.get("GOALFLIGHT_MAX_QUIET_S", "3600")),
        help="Absolute event-silence hard wall for CPU-busy quiet workers, independent of idle-timeout.",
    )
    parser.add_argument(
        "--progress-stall-s",
        type=float,
        default=float(os.environ.get("GOALFLIGHT_PROGRESS_STALL_S", "300")),
        help="Operative activity-based stall detector: by default, exit the "
             "runner and leave the worker alive when standard progress events "
             "are silent for N seconds (default 300). Raw vendor events do not "
             "reset it. Tune for the worker's expected quiet pattern; do not "
             "substitute a tight --max-tool-s for this.",
    )
    parser.add_argument(
        "--stall-kill",
        action="store_true",
        default=os.environ.get("GOALFLIGHT_STALL_KILL", "").lower() in {"1", "true", "yes"},
        help="Restore old progress-stall behavior: kill the worker instead of "
             "detaching it and waking the orchestrator.",
    )
    parser.add_argument(
        "--liveness-profile",
        choices=sorted(LIVENESS_PROFILES),
        default=None,
        help="Override adapter liveness profile. Defaults to adapters/<agent>.json status_contract.",
    )
    parser.add_argument(
        "--remote-turn-silence-s",
        type=float,
        default=None,
        help="Remote-API in-flight prompt-turn silence wall. Defaults to adapter override or 1200s.",
    )
    parser.add_argument(
        "--remote-turn-cancel-grace-s",
        type=float,
        default=float(os.environ.get("GOALFLIGHT_REMOTE_TURN_CANCEL_GRACE_S", str(DEFAULT_REMOTE_TURN_CANCEL_GRACE_S))),
        help="Seconds to wait after ACP cancel before killing a remote silent turn.",
    )
    parser.add_argument(
        "--cpu-epsilon",
        type=float,
        default=0.1,
        help="Process-group CPU percent above which an event-silent worker "
             "counts as running_quiet (alive) rather than wedged on the idle "
             "path. Matches goalflight_watch.py's default.",
    )
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    # Derive idle-timeout from mode when not explicitly set. Goal-mode loops
    # run multi-hour and can go silent for long stretches between events; a
    # 5-minute idle ceiling would kill a healthy worker mid-test. 10h is a
    # safe wedge-detector ceiling (10h of TOTAL silence = genuinely stuck).
    cfg = normalized_acp_dispatch_cfg(args)
    payload = asyncio.run(run_acp_dispatch(cfg))
    if cfg.json:
        print(json.dumps(payload, sort_keys=True))
    else:
        print(f"{payload['dispatch_id']}: {payload['state']} status={cfg.status_json}")
    return acp_dispatch_exit_code(payload)


if __name__ == "__main__":
    _ensure_acp_sdk_python()
    raise SystemExit(main())
