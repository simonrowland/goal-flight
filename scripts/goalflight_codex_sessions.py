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
_BANNER_SESSION_RE = re.compile(
    r"^session id:\s*"
    r"([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12})\s*$",
    re.IGNORECASE,
)
_BANNER_READ_LIMIT = 64 * 1024


def valid_session_id(value: object) -> str | None:
    if not isinstance(value, str) or not _SESSION_ID_RE.fullmatch(value):
        return None
    return value.lower()


def canonical_account_home(account: object) -> Path | None:
    """Return the one canonical Codex home for a safe account directory name."""
    if not isinstance(account, str) or not account:
        return None
    account_path = Path(account)
    if account in {".", ".."} or account_path.parts != (account,):
        return None
    return Path.home() / ".goal-flight" / "accounts" / account / "codex"


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


def session_id_from_tail(home: Path, tail: Path) -> str | None:
    """Return this dispatch's banner UUID after its rollout exists in ``home``.

    Canonical account homes are shared by concurrent dispatches, so their
    rollout directory cannot identify which session belongs to this worker.
    Codex prints the authoritative session id in the dispatch's own banner.
    Only the first banner field is considered, and it is accepted only after a
    matching rollout file exists.
    """
    try:
        with Path(tail).open("rb") as stream:
            prefix = stream.read(_BANNER_READ_LIMIT).decode(
                "utf-8", errors="replace"
            )
    except OSError:
        return None
    for line in prefix.splitlines():
        if line.strip() == "user":
            return None
        match = _BANNER_SESSION_RE.fullmatch(line.strip())
        if match is None:
            continue
        session_id = valid_session_id(match.group(1))
        if session_id is None:
            return None
        return session_id if rollout_path(Path(home), session_id) else None
    return None
