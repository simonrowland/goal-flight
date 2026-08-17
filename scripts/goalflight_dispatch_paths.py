"""Shared dispatch path helpers.

This module stays below ``goalflight_dispatch.py`` so transport runners can
resolve dispatch files without importing the dispatcher CLI.
"""

from __future__ import annotations

from pathlib import Path

import goalflight_compat


def state_dir(configured: Path | str | None = None) -> Path:
    return Path(configured).expanduser() if configured is not None else goalflight_compat.resolve_state_dir()


def dispatch_base_dir(configured_state_dir: Path | str | None = None) -> Path:
    """Status/tail/steer directory, overridable via ``$GOALFLIGHT_DISPATCH_DIR``.

    Default remains ``<state_dir>/dispatch``. Tests that exercise the launch
    path must set the override themselves so isolation does not depend on the
    invoker remembering ``$GOALFLIGHT_STATE_DIR``. Blank/whitespace falls back,
    matching the other ``GOALFLIGHT_*`` path resolvers.
    """
    return goalflight_compat.resolve_env_path(
        "GOALFLIGHT_DISPATCH_DIR",
        state_dir(configured_state_dir) / "dispatch",
    )


def dispatch_queue_dir(configured_state_dir: Path | str | None = None) -> Path:
    return state_dir(configured_state_dir) / "dispatch-queue"


def safe_dispatch_filename(dispatch_id: str) -> str:
    return goalflight_compat.safe_dispatch_filename(dispatch_id)


def queue_entry_path(
    dispatch_id: str,
    *,
    queue_dir: Path | str | None = None,
    state_dir: Path | str | None = None,
) -> Path:
    base = Path(queue_dir).expanduser() if queue_dir is not None else dispatch_queue_dir(state_dir)
    return base / f"{safe_dispatch_filename(dispatch_id)}.json"


def status_path_for(
    dispatch_id: str,
    configured: str | Path | None = None,
    state_dir: Path | str | None = None,
) -> Path:
    return Path(configured).expanduser() if configured else dispatch_base_dir(state_dir) / f"{dispatch_id}.status.json"


def steer_file(dispatch_id: str, state_dir: Path | str | None = None) -> Path:
    return dispatch_base_dir(state_dir) / f"{dispatch_id}.steer.jsonl"
