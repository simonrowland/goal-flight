"""Shared dispatch path helpers.

This module stays below ``goalflight_dispatch.py`` so transport runners can
resolve dispatch files without importing the dispatcher CLI.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Mapping

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


def worker_question_outbox_dir(
    dispatch_id: str,
    *,
    dispatch_dir: Path | str | None = None,
) -> Path:
    base = Path(dispatch_dir).expanduser() if dispatch_dir is not None else dispatch_base_dir()
    return base / f"{safe_dispatch_filename(dispatch_id)}.questions"


def worker_scratch_dir(dispatch_id: str) -> Path:
    """Return the retention-managed scratch directory for one dispatch."""
    return dispatch_base_dir() / f"{safe_dispatch_filename(dispatch_id)}.scratch"


def pidfile_dir() -> Path:
    """Directory holding the worker-owned bash-tail PID files."""

    configured = os.environ.get("GOAL_FLIGHT_PIDFILE_DIR")
    if configured is not None:
        return Path(configured).expanduser()
    return goalflight_compat.temp_base() / "goal-flight-acp-pids.d"


def codex_resume_lock_path(home: Path, session_id: str) -> Path:
    key = hashlib.sha256(
        f"{home.resolve(strict=False)}\0{session_id}".encode("utf-8")
    ).hexdigest()
    return home.parent / ".resume-locks" / f"{key}.lock"


def engine_resume_lock_path(engine: str, session_id: str) -> Path:
    key = hashlib.sha256(f"{engine}\0{session_id}".encode("utf-8")).hexdigest()
    codex_state = Path(
        os.environ.get("GOALFLIGHT_CODEX_STATE_DIR", "~/.goal-flight")
    ).expanduser().resolve(strict=False)
    return codex_state / "dispatch-homes" / ".resume-locks" / f"{key}.lock"


def dispatch_queue_dir(configured_state_dir: Path | str | None = None) -> Path:
    return state_dir(configured_state_dir) / "dispatch-queue"


def remote_ci_spool_dir(configured_state_dir: Path | str | None = None) -> Path:
    """Return the shared request/result spool granted to sandboxed workers."""
    return state_dir(configured_state_dir) / "remote-ci" / "spool"


def remote_ci_worker_environment(
    project_root: Path | str, existing: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Prepare the exact spool paths shared with a sandboxed worker."""
    configured = os.environ if existing is None else existing
    explicit_config = configured.get("GOALFLIGHT_REMOTE_CI_CONFIG")
    config_path = explicit_config or str(
        Path(project_root).resolve() / ".goal-flight" / "remote-ci.json"
    )
    spool = remote_ci_spool_dir()
    try:
        raw = json.loads(Path(config_path).expanduser().read_text(encoding="utf-8"))
        paths = raw.get("paths") if isinstance(raw, Mapping) else None
        if isinstance(paths, Mapping):
            queue_dir = Path(paths["queue_dir"]).expanduser()
            result_dir = Path(paths["result_dir"]).expanduser()
            if queue_dir.is_absolute() and result_dir.is_absolute():
                queue_dir = queue_dir.resolve(strict=False)
                result_dir = result_dir.resolve(strict=False)
                if (queue_dir.name == "requests" and result_dir.name == "results"
                        and queue_dir.parent == result_dir.parent):
                    spool = queue_dir.parent
    except (OSError, ValueError, KeyError, TypeError):
        pass
    env = {
        "GOALFLIGHT_REMOTE_CI_CONFIG": config_path,
        "GOALFLIGHT_REMOTE_CI_SPOOL_DIR": "",
        "GOALFLIGHT_REMOTE_CI_SPOOL_ERROR": "",
    }
    if explicit_config or Path(config_path).expanduser().is_file():
        env["GOALFLIGHT_REMOTE_RUN"] = str(
            Path(__file__).resolve().parent.parent / "bin" / "goalflight-remote-run"
        )
    try:
        for name in ("requests", "results"):
            (spool / name).mkdir(parents=True, exist_ok=True, mode=0o700)
    except OSError:
        # Do not grant a spool that the dispatcher could not prepare. The
        # wrapper uses this typed reason to apply the configured fallback.
        env["GOALFLIGHT_REMOTE_CI_SPOOL_ERROR"] = "spool-unavailable"
    else:
        env["GOALFLIGHT_REMOTE_CI_SPOOL_DIR"] = str(spool)
    return env


def remote_ci_worker_prompt(environment: Mapping[str, str]) -> str | None:
    """Return the concise remote-run instruction for an enabled worker env."""
    if not str(environment.get("GOALFLIGHT_REMOTE_RUN") or "").strip():
        return None
    command_ids: list[str] = []
    config_path = str(environment.get("GOALFLIGHT_REMOTE_CI_CONFIG") or "").strip()
    if config_path:
        try:
            raw = json.loads(Path(config_path).expanduser().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raw = None
        runner = raw.get("runner") if isinstance(raw, Mapping) else None
        configured_ids = runner.get("heavy_command_ids") if isinstance(runner, Mapping) else None
        if isinstance(configured_ids, list):
            command_ids = [
                value.strip()
                for value in configured_ids
                if isinstance(value, str) and value.strip()
            ]
    cmd_example = ""
    pair_cmd_id = " --cmd-id <configured-command-id>"
    if command_ids:
        cmd_example = f" --cmd-id {command_ids[0]}"
        pair_cmd_id = cmd_example
        if len(command_ids) > 1:
            cmd_example += f" (other configured IDs: {', '.join(command_ids[1:])})"
    pair_example = (
        '"$GOALFLIGHT_REMOTE_RUN" --pair --base <base-sha>'
        f"{pair_cmd_id} -- <pytest command>"
    )
    return (
        'Remote CI: run test suites and other heavy CPU through '
        f'"$GOALFLIGHT_REMOTE_RUN" --kind gate{cmd_example} -- <command>, '
        'which runs them on a Studio. If it refuses or falls back, the reason '
        'names each Studio; a local run is the fallback, bounded with a timeout. '
        'For base-vs-candidate evidence, run the matched pair: '
        f'{pair_example}. The matched-pair command refuses remote failures and '
        'never falls back locally. Do not run the full suite locally by default.'
    )


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


# Removed status_path_for: raw dispatch IDs bypassed the canonical status filename sanitization.


def steer_file(dispatch_id: str, state_dir: Path | str | None = None) -> Path:
    return dispatch_base_dir(state_dir) / f"{dispatch_id}.steer.jsonl"
