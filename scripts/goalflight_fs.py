"""Fail-closed filesystem presence and listing.

``Path.glob`` swallows ``PermissionError`` and yields ``[]``. ``Path.exists()``
returns False for a child of an unsearchable parent. Decision paths that
authorize delete, terminalize, launch, unlink, or retire must not treat those
answers as absence.

Three-state presence: ``present`` / ``absent`` / ``unknown``.
Three-state listing: ``ok`` / ``absent`` / ``unreadable``.

Verified on CPython 3.14: ``chmod 000`` on a directory makes ``os.listdir`` /
``Path.iterdir`` raise, while ``Path.glob("*.json")`` returns ``[]`` without
raising. The directory inode itself still ``lstat``s as present. A *child* of
that directory reports ``exists()`` False; ``lstat`` of the child raises
``PermissionError``, which is ``unknown``, not ``absent``.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

Presence = Literal["present", "absent", "unknown"]
ListingState = Literal["ok", "absent", "unreadable"]


def path_presence(path: Path | str) -> Presence:
    """lstat-based presence. Never ``Path.exists()`` / ``lexists`` here.

    Only ``FileNotFoundError`` is evidence of absence. Any other ``OSError``
    (including ``PermissionError`` on a child of an unsearchable parent) is
    ``unknown``.
    """
    try:
        os.lstat(path)
    except FileNotFoundError:
        return "absent"
    except OSError:
        return "unknown"
    return "present"


def list_dir(path: Path | str) -> tuple[ListingState, list[Path]]:
    """List directory entries via ``iterdir``, never ``glob``.

    ``glob`` swallowing ``PermissionError`` is the class: an unlistable
    directory reads as empty and licenses "not there" actions. ``iterdir``
    raises; this helper turns that into ``unreadable`` instead of ``[]``.

    A directory that vanishes between the lstat and the listing is ``absent``.
    """
    directory = Path(path)
    try:
        os.lstat(directory)
    except FileNotFoundError:
        return "absent", []
    except OSError:
        return "unreadable", []
    try:
        return "ok", list(directory.iterdir())
    except FileNotFoundError:
        return "absent", []
    except OSError:
        return "unreadable", []


def list_dir_suffix(path: Path | str, suffix: str) -> tuple[ListingState, list[Path]]:
    """``list_dir`` filtered by filename suffix (e.g. ``.json``)."""
    state, entries = list_dir(path)
    if state != "ok":
        return state, []
    return "ok", [entry for entry in entries if entry.name.endswith(suffix)]
