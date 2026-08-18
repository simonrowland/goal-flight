#!/usr/bin/env python3
"""Read permission_mode from a grok seat home's config.toml.

A freshly provisioned grok home often has no ``[ui]`` table and therefore no
``permission_mode``. A worker launched into that home completes one turn and
then dies with no terminal marker (``worker_dead_no_terminal_marker``).

This module only reports whether a value is set. It does not judge the value
and it does not rewrite the file. Dispatch refuses an absent mode; a present
mode is passed through unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re

# Match a TOML assignment for permission_mode on a non-comment line, including
# dotted keys (ui.permission_mode) and inline tables. Values may be quoted or
# bare tokens. Do not treat a commented-out assignment as present.
_PERMISSION_MODE_ASSIGN_RE = re.compile(
    r"(?:^|[\s,{])(?:[A-Za-z0-9_-]+\.)*permission_mode\s*=\s*"
    r'(?:"([^"]*)"|\'([^\']*)\'|([A-Za-z0-9][A-Za-z0-9_-]*))'
)

_VERIFIED_FIX = (
    '[ui]\n'
    'permission_mode = "always-approve"'
)


@dataclass(frozen=True)
class GrokPermissionModeInspection:
    path: Path
    status: str
    mode: str | None = None
    detail: str | None = None


def config_path_for_home(home: Path | str) -> Path:
    return Path(home) / ".grok" / "config.toml"


def home_from_account_env(
    account_env: dict[str, str] | None,
    *,
    default_home: Path | str | None = None,
) -> Path:
    """HOME the dispatcher already resolved, else the host default.

    Callers must pass the dict ``_resolve_account_env`` returned rather than
    re-deriving ``~/.goal-flight/accounts/<seat>/grok``.
    """
    raw = (account_env or {}).get("HOME")
    if raw:
        return Path(raw)
    if default_home is not None:
        return Path(default_home)
    return Path.home()


def inspect_config(config_path: Path | str) -> GrokPermissionModeInspection:
    path = Path(config_path)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return GrokPermissionModeInspection(
            path=path,
            status="missing",
            detail="file does not exist",
        )
    except OSError as exc:
        return GrokPermissionModeInspection(
            path=path,
            status="unreadable",
            detail=f"{type(exc).__name__}: {exc}",
        )
    except UnicodeError as exc:
        return GrokPermissionModeInspection(
            path=path,
            status="unreadable",
            detail=f"{type(exc).__name__}: {exc}",
        )

    mode = _first_permission_mode(text)
    if mode:
        return GrokPermissionModeInspection(path=path, status="present", mode=mode)
    return GrokPermissionModeInspection(
        path=path,
        status="absent",
        detail="permission_mode is not set",
    )


def inspect_home(home: Path | str) -> GrokPermissionModeInspection:
    return inspect_config(config_path_for_home(home))


def refusal_message(inspection: GrokPermissionModeInspection) -> str:
    path = inspection.path
    if inspection.status == "unreadable":
        return (
            f"grok seat config {path} could not be read "
            f"({inspection.detail}); refusing to launch a grok worker that "
            "would otherwise die after one turn with no terminal marker "
            "(worker_dead_no_terminal_marker). Fix that file and retry. "
            "This dispatcher will not rewrite an operator-owned config."
        )
    if inspection.status == "missing":
        problem = f"grok seat config {path} is missing"
    else:
        problem = f"grok seat config {path} has no permission_mode"
    return (
        f"{problem}; a grok worker launched into this home dies after one "
        "turn with no terminal marker (worker_dead_no_terminal_marker). "
        f"Set the following in that file (the value verified to work):\n"
        f"{_VERIFIED_FIX}\n"
        "This dispatcher will not rewrite an operator-owned config."
    )


def as_row(inspection: GrokPermissionModeInspection) -> dict:
    return {
        "config": str(inspection.path),
        "permission_mode": inspection.mode,
        "status": inspection.status,
        "detail": inspection.detail,
    }


def inspect_configured_homes(
    accounts: list[tuple[str | None, Path]] | None = None,
) -> list[dict]:
    """Inspect config.toml next to each auth.json. Never reads auth contents.

    ``accounts`` is the ``grok_usage.accounts()`` shape: (label, auth_path).
    A row is emitted only when auth.json or the sibling config.toml exists.
    """
    if accounts is None:
        try:
            import grok_usage

            accounts = grok_usage.accounts()
        except Exception:
            accounts = [(None, Path.home() / ".grok" / "auth.json")]
    rows: list[dict] = []
    seen: set[Path] = set()
    for _label, auth_path in accounts:
        auth = Path(auth_path)
        config = auth.parent / "config.toml"
        if not auth.is_file() and not config.is_file():
            continue
        if config in seen:
            continue
        seen.add(config)
        rows.append(as_row(inspect_config(config)))
    return rows


def _first_permission_mode(text: str) -> str | None:
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        match = _PERMISSION_MODE_ASSIGN_RE.search(line)
        if not match:
            continue
        value = next((group for group in match.groups() if group), "")
        value = value.strip()
        if value:
            return value
    return None
