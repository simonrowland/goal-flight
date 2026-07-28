"""Codex rollout/session helpers shared by dispatch and watcher paths."""

from __future__ import annotations

import re
from pathlib import Path


_SESSION_ID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
_ROLLOUT_NAME_RE = re.compile(
    r"^rollout-.*-"
    r"([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12})\.jsonl$"
)


def valid_session_id(value: object) -> str | None:
    if not isinstance(value, str) or not _SESSION_ID_RE.fullmatch(value):
        return None
    return value.lower()


def rollout_path(home: Path, session_id: str) -> Path | None:
    expected = valid_session_id(session_id)
    sessions = home / "sessions"
    if expected is None or not sessions.is_dir():
        return None
    for path in sessions.rglob("rollout-*.jsonl"):
        match = _ROLLOUT_NAME_RE.fullmatch(path.name)
        if match and match.group(1).lower() == expected and path.is_file():
            return path
    return None


def discover_session_id(home: Path) -> str | None:
    """Return the sole recorded rollout UUID; never guess among multiple sessions."""
    sessions = home / "sessions"
    if not sessions.is_dir():
        return None
    found: set[str] = set()
    for path in sessions.rglob("rollout-*.jsonl"):
        match = _ROLLOUT_NAME_RE.fullmatch(path.name)
        if match and path.is_file():
            found.add(match.group(1).lower())
            if len(found) > 1:
                return None
    return next(iter(found), None)
