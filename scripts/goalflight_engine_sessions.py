"""Engine-native session handles for tracked dispatch resume.

Ledger ``session_id`` / ``logical_session_id`` is the Goal Flight dispatch id.
That is not the engine's own conversation handle. Only Codex historically
recorded its handle (``codex_session_id``, harvested from the per-dispatch
rollout). Grok, cursor-agent, Claude, and Kimi can all resume; they were not
wired.

Capture policy (checked against each CLI ``--help`` on this box, 2026-08-19):

- **codex** — harvest the rollout UUID (existing). ``codex exec resume <id>``.
- **grok** — assign a UUID at launch via ``--session-id``; resume with
  ``--resume <id>``.
- **claude** — assign a UUID at launch via ``--session-id``; resume with
  ``--resume <id>``.
- **cursor** — cannot assign a new chat id from flags alone; resume with
  ``--resume <chatId>`` only when a handle was recorded. Harvest is best-effort.
- **moonshot / kimi** — ``-S/--session`` resumes an existing id; it does not
  create one. Harvest from ``$HOME/.kimi-code/session_index.jsonl`` or the
  CLI resume footer. A pre-harvest dispatch cannot be resumed.

Fork policy
-----------
Grok and Claude expose ``--fork-session``. Resume **reuses** the recorded
session id and never passes ``--fork-session``.

Operator decision (t-288 steer seq 1): resume exists so a worker can fix
bugs, take a nudge, or survive a reboot — the SAME session continues. Fork
would mint a sibling and look like a silent fresh start. The live-source
guard is what prevents attaching to a running worker; fork is not the
safety mechanism.

If an engine could only fork, refuse rather than silently forking.
"""

from __future__ import annotations

import json
import re
import uuid
from pathlib import Path

import goalflight_codex_sessions
import goalflight_ledger


# Explicit: reuse the recorded handle. Do not pass --fork-session.
RESUME_FORK_POLICY = "reuse"

_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
_KIMI_SESSION_RE = re.compile(
    r"^session_[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
# cursor-agent create-chat / --resume chatId: UUID or 32-hex hash observed.
_CURSOR_SESSION_RE = re.compile(
    r"^(?:[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}|[0-9a-fA-F]{32})$"
)
# Footer some CLIs print after the worker's last line, e.g.
# "To resume this session: kimi -S session_<uuid>".
RESUME_FOOTER_HANDLE_RE = re.compile(
    r"(?:to\s+)?(?:resume|continue)\s+(?:this\s+)?session\s*:\s*\S+\s+"
    r"(?:-S|--session|-r|--resume)\s+(\S+)",
    re.IGNORECASE,
)

# Canonical resume engine -> worker-cli labels that share that store.
ENGINE_LABELS = {
    "codex": frozenset({"codex", "codex-acp"}),
    "grok": frozenset({"grok", "grok-code", "grok-research", "grok-acp"}),
    "cursor": frozenset({"cursor", "cursor-agent"}),
    "claude": frozenset({"claude", "claude-acp", "claude-code", "claude-code-cli-acp"}),
    "moonshot": frozenset({"moonshot", "kimi"}),
}

# Engines whose CLI can name a new session before the first turn.
ASSIGN_AT_LAUNCH = frozenset({"grok", "claude"})

# Engines that expose a fork flag. Resume still reuses; see RESUME_FORK_POLICY.
FORK_CAPABLE = frozenset({"grok", "claude"})


def resume_engine(agent_or_engine: object) -> str | None:
    """Map a ledger agent/engine label to a resume engine, or None."""
    raw = goalflight_ledger.infer_engine(agent_or_engine)
    if not raw or raw == "unknown":
        if not isinstance(agent_or_engine, str) or not agent_or_engine:
            return None
        raw = agent_or_engine.strip().lower()
    else:
        raw = str(raw).strip().lower()
    if raw == "kimi":
        return "moonshot"
    for engine, labels in ENGINE_LABELS.items():
        if raw == engine or raw in labels:
            return engine
    return None


def valid_session_id(engine: object, value: object) -> str | None:
    """Return a normalized handle or None if it is not this engine's shape."""
    if not isinstance(value, str):
        return None
    handle = value.strip()
    if not handle:
        return None
    resolved = resume_engine(engine) or (
        str(engine).strip().lower() if isinstance(engine, str) else ""
    )
    if resolved == "codex":
        return goalflight_codex_sessions.valid_session_id(handle)
    if resolved in {"grok", "claude"}:
        if _UUID_RE.fullmatch(handle):
            return handle.lower()
        return None
    if resolved == "moonshot":
        if _KIMI_SESSION_RE.fullmatch(handle):
            return f"session_{handle[8:].lower()}"
        if _UUID_RE.fullmatch(handle):
            return f"session_{handle.lower()}"
        return None
    if resolved == "cursor":
        if _CURSOR_SESSION_RE.fullmatch(handle):
            return handle.lower()
        return None
    return None


def new_session_id(engine: object) -> str:
    resolved = resume_engine(engine)
    if resolved not in ASSIGN_AT_LAUNCH:
        raise ValueError(f"{engine!r} cannot assign a session id at launch")
    return str(uuid.uuid4())


def can_assign_at_launch(engine: object) -> bool:
    return resume_engine(engine) in ASSIGN_AT_LAUNCH


def session_argv(
    engine: object,
    session_id: str,
    *,
    resume: bool,
) -> list[str]:
    """CLI flags that name or resume an engine session.

    Never includes ``--fork-session`` or ``--continue``. ``--continue`` attaches
    to whichever session last touched the cwd; that is the wrong handle when
    more than one worker shares a tree.
    """
    resolved = resume_engine(engine)
    handle = valid_session_id(resolved, session_id)
    if resolved is None or handle is None:
        return []
    if resolved == "grok":
        if resume:
            return ["--resume", handle]
        return ["--session-id", handle]
    if resolved == "claude":
        if resume:
            return ["--resume", handle]
        return ["--session-id", handle]
    if resolved == "cursor":
        # cursor-agent has no "name a new chat" flag; --resume is the only
        # handle-bearing option. Callers must not pass a handle for a fresh
        # cursor launch unless they already created that chat.
        if resume:
            return ["--resume", handle]
        return []
    if resolved == "moonshot":
        if resume:
            return ["-S", handle]
        return []
    return []


def parse_resume_footer_handle(line: object) -> str | None:
    if not isinstance(line, str):
        return None
    match = RESUME_FOOTER_HANDLE_RE.search(line.strip())
    if match is None:
        return None
    return match.group(1).strip() or None


def harvest_kimi_session_id(
    home: Path,
    work_dir: Path,
    *,
    after_mtime: float | None = None,
) -> str | None:
    """Return the sole Kimi handle for ``work_dir``; never guess among many."""
    index = Path(home).expanduser() / ".kimi-code" / "session_index.jsonl"
    if not index.is_file():
        return None
    try:
        wanted = Path(work_dir).expanduser().resolve(strict=False)
    except OSError:
        return None
    found: set[str] = set()
    try:
        lines = index.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for line in lines:
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(record, dict):
            continue
        raw_wd = record.get("workDir")
        if not isinstance(raw_wd, str) or not raw_wd:
            continue
        try:
            if Path(raw_wd).expanduser().resolve(strict=False) != wanted:
                continue
        except OSError:
            continue
        handle = valid_session_id("moonshot", record.get("sessionId"))
        if handle is None:
            continue
        if after_mtime is not None:
            session_dir = record.get("sessionDir")
            stamp_path = (
                Path(session_dir).expanduser()
                if isinstance(session_dir, str) and session_dir
                else None
            )
            try:
                mtime = (
                    stamp_path.stat().st_mtime
                    if stamp_path is not None and stamp_path.exists()
                    else index.stat().st_mtime
                )
            except OSError:
                continue
            if mtime < after_mtime:
                continue
        found.add(handle)
        if len(found) > 1:
            return None
    return next(iter(found), None)


def harvest_grok_session_id(home: Path, work_dir: Path) -> str | None:
    """Return the sole grok session dir for ``work_dir``; never guess."""
    sessions = Path(home).expanduser() / ".grok" / "sessions"
    if not sessions.is_dir():
        return None
    try:
        wanted = Path(work_dir).expanduser().resolve(strict=False)
    except OSError:
        return None
    encoded = _grok_session_group_name(wanted)
    group = sessions / encoded
    if not group.is_dir():
        return None
    found: set[str] = set()
    try:
        children = list(group.iterdir())
    except OSError:
        return None
    for child in children:
        if not child.is_dir():
            continue
        handle = valid_session_id("grok", child.name)
        if handle is None:
            continue
        found.add(handle)
        if len(found) > 1:
            return None
    return next(iter(found), None)


def _grok_session_group_name(work_dir: Path) -> str:
    # Grok URL-encodes the cwd; when that exceeds 255 bytes it uses a slug+hash
    # and a .cwd file. Dispatch homes stay well under that.
    from urllib.parse import quote

    return quote(str(work_dir), safe="")
