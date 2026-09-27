"""Operator-configured model refusals leave launch state untouched."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
DISPATCH = ROOT / "scripts" / "goalflight_dispatch.py"
sys.path.insert(0, str(ROOT / "scripts"))

import goalflight_dispatch as D  # noqa: E402


REFUSED_MODEL = "gpt-5.6-luna"
REPLACEMENT_MODEL = "gpt-6-luna"
CURSOR_ALLOW_PATTERNS = ("grok-*", "cursor-grok-*", "kimi-k3-*")


def _runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    refused_models: dict | None = None,
    agent_model_allow: dict | None = None,
) -> tuple[Path, Path, dict[str, str]]:
    project = tmp_path / "project"
    project.mkdir()
    subprocess.run(
        ["git", "init", "--quiet", "--initial-branch=main", str(project)],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    home = tmp_path / "home"
    home.mkdir()
    state = tmp_path / "state"
    config = tmp_path / "capacity.local.json"
    policy = {}
    if refused_models is not None:
        policy["refused_models"] = refused_models
    if agent_model_allow is not None:
        policy["agent_model_allow"] = agent_model_allow
    config.write_text(json.dumps(policy), encoding="utf-8")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    codex_marker = tmp_path / "codex-spawned"
    fake_codex = bin_dir / "codex"
    fake_codex.write_text(
        "#!/usr/bin/env python3\n"
        "import os\n"
        "from pathlib import Path\n"
        "Path(os.environ['MODEL_REFUSAL_CODEX_MARKER']).write_text('spawned')\n",
        encoding="utf-8",
    )
    fake_codex.chmod(0o755)
    cursor_marker = tmp_path / "cursor-spawned"
    fake_cursor = bin_dir / "cursor-agent"
    fake_cursor.write_text(
        "#!/usr/bin/env python3\n"
        "import os\n"
        "from pathlib import Path\n"
        "Path(os.environ['MODEL_REFUSAL_CURSOR_MARKER']).write_text('spawned')\n",
        encoding="utf-8",
    )
    fake_cursor.chmod(0o755)

    env_values = {
        "HOME": str(home),
        "GOALFLIGHT_STATE_DIR": str(state),
        "GOALFLIGHT_CODEX_STATE_DIR": str(state),
        "GOALFLIGHT_DISPATCH_DIR": str(state / "dispatch"),
        "GOALFLIGHT_CAPACITY_CONF": str(config),
        "GOALFLIGHT_CAPACITY_WAIT_S": "0",
        "GOALFLIGHT_MESSAGES_DIR": str(tmp_path / "messages"),
        "GOALFLIGHT_JOURNAL_DIR": str(tmp_path / "journal"),
        "GOALFLIGHT_TASK_STORE_DIR": str(tmp_path / "task-store"),
        "GOALFLIGHT_WAKE_LEDGER_DIR": str(tmp_path / "wake-ledger"),
        "GOALFLIGHT_PIDFILE_DIR": str(tmp_path / "pidfiles"),
        "GOAL_FLIGHT_PIDFILE_DIR": str(tmp_path / "pidfiles"),
        "GOALFLIGHT_DISABLE_NUDGES": "1",
        "MODEL_REFUSAL_CODEX_MARKER": str(codex_marker),
        "MODEL_REFUSAL_CURSOR_MARKER": str(cursor_marker),
        "PATH": str(bin_dir) + os.pathsep + os.environ.get("PATH", ""),
    }
    for key, value in env_values.items():
        monkeypatch.setenv(key, value)
    for key in (
        "CODEX_HOME",
        "GOALFLIGHT_CONTROLLER_LABEL",
        "GOALFLIGHT_CONTROLLER_PID",
        "GOALFLIGHT_CONTROLLER_SESSION_ID",
        "GOALFLIGHT_CONTROLLER_LEASE_NONCE",
        "GOALFLIGHT_DISPATCH_ID",
        "GOALFLIGHT_DISPATCH_SCRIPT",
        "GOALFLIGHT_PROMPT_FILE",
        "GOALFLIGHT_STEER_FILE",
        "GOALFLIGHT_WORKTREE_LOCK_FD",
        "GOALFLIGHT_OCCUPANCY_LOCK_FD",
    ):
        monkeypatch.delenv(key, raising=False)
    return project, state, os.environ.copy()


def _raw_launch_argv(
    *,
    project: Path,
    dispatch_id: str,
    model: str | None,
    marker: Path,
    agent: str = "test-dispatch",
) -> list[str]:
    worker = (
        "from pathlib import Path; "
        f"Path({str(marker)!r}).write_text('spawned'); "
        f"print('COMPLETE: {dispatch_id} — ok', flush=True)"
    )
    argv = [
        "--agent",
        agent,
        "--shape",
        "bash",
        "--cwd",
        str(project),
        "--tail",
        str(marker.with_suffix(".tail")),
        "--poll-secs",
        "0.1",
        "--max-idle-secs",
        "5",
    ]
    if model is not None:
        argv += ["--model", model]
    argv += [
        "--dispatch-id",
        dispatch_id,
        "--unregistered-forced",
        "--foreground",
        "--",
        sys.executable,
        "-c",
        worker,
    ]
    return argv


def _run_dispatch(
    project: Path, env: dict[str, str], argv: list[str]
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(DISPATCH), *argv],
        cwd=project,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=30,
    )


def _assert_refusal(result: subprocess.CompletedProcess[str]) -> None:
    assert result.returncode == 64, (result.returncode, result.stdout, result.stderr)
    assert (
        "goalflight_dispatch: model gpt-5.6-luna is refused by operator policy "
        "(capacity config refused_models); use --model gpt-6-luna"
    ) in result.stderr


def _assert_cursor_allowlist_refusal(
    result: subprocess.CompletedProcess[str], model: str | None
) -> None:
    assert result.returncode == 64, (result.returncode, result.stdout, result.stderr)
    model_name = model if model is not None else "<missing>"
    assert (
        f"goalflight_dispatch: agent cursor model {model_name} is refused by operator "
        "policy (capacity config agent_model_allow); allowed patterns: grok-*, "
        "cursor-grok-*, kimi-k3-*; pass an explicit --model matching an allowed pattern"
    ) in result.stderr


def _assert_no_launch_effects(
    state: Path,
    env: dict[str, str],
    dispatch_id: str,
    worker_marker: Path,
    *,
    existing_record: bool = False,
) -> None:
    assert (
        D.goalflight_ledger.record_path(dispatch_id, create=False).exists()
        is existing_record
    )
    assert not (state / "capacity.json").exists()
    assert not (state / "dispatch-homes" / dispatch_id).exists()
    assert not Path(env["GOALFLIGHT_JOURNAL_DIR"]).exists()
    assert not worker_marker.exists()


def test_refused_fresh_launch_has_no_side_effects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, state, env = _runtime(
        tmp_path, monkeypatch, refused_models={REFUSED_MODEL: REPLACEMENT_MODEL}
    )
    dispatch_id = "refused-fresh"
    marker = tmp_path / "fresh-worker-spawned"

    result = _run_dispatch(
        project,
        env,
        _raw_launch_argv(
            project=project,
            dispatch_id=dispatch_id,
            model=REFUSED_MODEL,
            marker=marker,
        ),
    )

    _assert_refusal(result)
    _assert_no_launch_effects(state, env, dispatch_id, marker)


def test_refused_queue_replay_has_no_launch_side_effects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, state, env = _runtime(
        tmp_path, monkeypatch, refused_models={REFUSED_MODEL: REPLACEMENT_MODEL}
    )
    dispatch_id = "refused-queue"
    token = "queue-launch-token"
    marker = tmp_path / "queue-worker-spawned"
    claim = state / "dispatch-queue" / f"{dispatch_id}.claim.json"
    claim.parent.mkdir(parents=True)
    claim_bytes = json.dumps(
        {
            "dispatch_id": dispatch_id,
            "queue_launch_token": token,
            "state": "claimed",
        },
        sort_keys=True,
    ).encode()
    claim.write_bytes(claim_bytes)
    queued_record = {
        "schema": D.goalflight_ledger.SCHEMA,
        "dispatch_id": dispatch_id,
        "agent": "test-dispatch",
        "model": REFUSED_MODEL,
        "state": "queued",
        "reason": "dispatch_queue",
        "project_root": str(project),
        "worker_cwd": str(project),
        "task_ids": [],
    }
    D.goalflight_ledger.write_record(queued_record)
    record_path = D.goalflight_ledger.record_path(dispatch_id, create=False)
    record_bytes = record_path.read_bytes()

    dispatch_argv = _raw_launch_argv(
        project=project,
        dispatch_id=dispatch_id,
        model=REFUSED_MODEL,
        marker=marker,
    )
    replay_argv = D._drain_launch_argv(
        dispatch_argv,
        capacity_wait_s=0,
        queue_launch_token=token,
        queue_claim_path=claim,
    )
    result = _run_dispatch(project, env, replay_argv)

    _assert_refusal(result)
    _assert_no_launch_effects(
        state, env, dispatch_id, marker, existing_record=True
    )
    assert claim.read_bytes() == claim_bytes
    assert record_path.read_bytes() == record_bytes
    assert [path.name for path in record_path.parent.iterdir()] == [record_path.name]


@pytest.mark.parametrize(
    "model",
    ["gpt-6-luna", "gpt-5.6-luna-high", "kimi-k2.7-code", None],
    ids=["other-family", "luna-variant", "kimi-k2", "missing"],
)
def test_cursor_model_allowlist_refuses_before_launch_side_effects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    model: str | None,
) -> None:
    project, state, env = _runtime(
        tmp_path,
        monkeypatch,
        agent_model_allow={"cursor": list(CURSOR_ALLOW_PATTERNS)},
    )
    dispatch_id = "cursor-refused-fresh"
    marker = tmp_path / "cursor-worker-spawned"

    result = _run_dispatch(
        project,
        env,
        _raw_launch_argv(
            project=project,
            dispatch_id=dispatch_id,
            model=model,
            marker=marker,
            agent="cursor",
        ),
    )

    _assert_cursor_allowlist_refusal(result, model)
    _assert_no_launch_effects(state, env, dispatch_id, marker)
    assert not (tmp_path / "cursor-spawned").exists()


def _write_cursor_parent(
    project: Path, *, dispatch_id: str, model: str
) -> None:
    D.goalflight_ledger.write_record(
        {
            "schema": D.goalflight_ledger.SCHEMA,
            "dispatch_id": dispatch_id,
            "agent": "cursor",
            "engine": "cursor",
            "state": "blocked",
            "terminal_state": "blocked",
            "started_at": D.goalflight_ledger.utc_now(),
            "project_root": str(project),
            "worker_cwd": str(project),
            "task_ids": [],
            "model": model,
        }
    )


@pytest.mark.parametrize("explicit", [False, True], ids=["inherited", "explicit"])
def test_cursor_model_allowlist_refuses_resume_before_side_effects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    explicit: bool,
) -> None:
    project, state, env = _runtime(
        tmp_path,
        monkeypatch,
        agent_model_allow={"cursor": list(CURSOR_ALLOW_PATTERNS)},
    )
    parent_id = "cursor-refused-resume-parent"
    child_prompt = tmp_path / "resume-prompt.md"
    child_prompt.write_text("resume\n", encoding="utf-8")
    _write_cursor_parent(
        project,
        dispatch_id=parent_id,
        model="grok-4.7-high" if explicit else "gpt-6-luna",
    )
    parent_record = D.goalflight_ledger.record_path(parent_id, create=False)
    parent_bytes = parent_record.read_bytes()
    before_records = sorted(path.name for path in parent_record.parent.iterdir())
    argv = [
        "resume",
        parent_id,
        "--prompt-file",
        str(child_prompt),
        "--unregistered-forced",
    ]
    if explicit:
        argv += ["--model", "gpt-6-luna"]

    result = _run_dispatch(project, env, argv)

    _assert_cursor_allowlist_refusal(result, "gpt-6-luna")
    _assert_no_launch_effects(
        state,
        env,
        parent_id,
        tmp_path / "cursor-worker-spawned",
        existing_record=True,
    )
    assert parent_record.read_bytes() == parent_bytes
    assert sorted(path.name for path in parent_record.parent.iterdir()) == before_records
    assert not (tmp_path / "cursor-spawned").exists()


def test_cursor_model_allowlist_refuses_queue_replay_before_side_effects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, state, env = _runtime(
        tmp_path,
        monkeypatch,
        agent_model_allow={"cursor": list(CURSOR_ALLOW_PATTERNS)},
    )
    dispatch_id = "cursor-refused-queue"
    token = "cursor-queue-launch-token"
    model = "kimi-k2.7-code"
    marker = tmp_path / "cursor-queue-worker-spawned"
    claim = state / "dispatch-queue" / f"{dispatch_id}.claim.json"
    claim.parent.mkdir(parents=True)
    claim_bytes = json.dumps(
        {
            "dispatch_id": dispatch_id,
            "queue_launch_token": token,
            "state": "claimed",
        },
        sort_keys=True,
    ).encode()
    claim.write_bytes(claim_bytes)
    D.goalflight_ledger.write_record(
        {
            "schema": D.goalflight_ledger.SCHEMA,
            "dispatch_id": dispatch_id,
            "agent": "cursor",
            "model": model,
            "state": "queued",
            "reason": "dispatch_queue",
            "project_root": str(project),
            "worker_cwd": str(project),
            "task_ids": [],
        }
    )
    record_path = D.goalflight_ledger.record_path(dispatch_id, create=False)
    record_bytes = record_path.read_bytes()
    before_records = sorted(path.name for path in record_path.parent.iterdir())
    dispatch_argv = _raw_launch_argv(
        project=project,
        dispatch_id=dispatch_id,
        model=model,
        marker=marker,
        agent="cursor",
    )
    replay_argv = D._drain_launch_argv(
        dispatch_argv,
        capacity_wait_s=0,
        queue_launch_token=token,
        queue_claim_path=claim,
    )

    result = _run_dispatch(project, env, replay_argv)

    _assert_cursor_allowlist_refusal(result, model)
    _assert_no_launch_effects(
        state, env, dispatch_id, marker, existing_record=True
    )
    assert claim.read_bytes() == claim_bytes
    assert record_path.read_bytes() == record_bytes
    assert sorted(path.name for path in record_path.parent.iterdir()) == before_records
    assert not (tmp_path / "cursor-spawned").exists()


def test_cursor_model_allowlist_globs_are_case_insensitive_for_family_aliases(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(D, "model_refusal", lambda _model: None)
    monkeypatch.setattr(
        D,
        "agent_model_allowlist",
        lambda agent: CURSOR_ALLOW_PATTERNS if agent == "cursor" else None,
    )
    for agent in ("cursor", "cursor-agent"):
        for model in ("GROK-4.7-HIGH", "CURSOR-GROK-4.7-XHIGH", "KIMI-K3-HIGH"):
            D._refuse_configured_model(model, agent)
        for model in (
            "gpt-5.6-luna-high",
            "kimi-k2.7-code",
            "claude-sonnet",
            "gemini-pro",
            "composer-1",
        ):
            with pytest.raises(D.DispatchUsageError):
                D._refuse_configured_model(model, agent)


def _write_codex_parent(
    project: Path, state: Path, *, dispatch_id: str, model: str
) -> Path:
    session_id = "12345678-1234-4abc-8def-1234567890ab"
    home = state / "dispatch-homes" / dispatch_id
    rollout = (
        home
        / "sessions"
        / "2026"
        / "07"
        / "28"
        / f"rollout-2026-07-28T12-00-00-{session_id}.jsonl"
    )
    rollout.parent.mkdir(parents=True)
    rollout.write_text('{"type":"session_meta"}\n', encoding="utf-8")
    D.goalflight_ledger.write_record(
        {
            "schema": D.goalflight_ledger.SCHEMA,
            "dispatch_id": dispatch_id,
            "agent": "codex",
            "engine": "codex",
            "shape": "bash",
            "transport": "dispatch",
            "project_root": str(project),
            "worker_cwd": str(project),
            "status_path": str(state / "dispatch" / f"{dispatch_id}.status.json"),
            "state": "blocked",
            "terminal_state": "blocked",
            "started_at": D.goalflight_ledger.utc_now(),
            "task_ids": [],
            "model": model,
            "codex_session_id": session_id,
            "codex_home": str(home),
            "codex_home_owner_dispatch_id": dispatch_id,
        }
    )
    return home


@pytest.mark.parametrize("explicit", [False, True], ids=["inherited", "explicit"])
def test_refused_codex_resume_has_no_side_effects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    explicit: bool,
) -> None:
    project, state, env = _runtime(
        tmp_path, monkeypatch, refused_models={REFUSED_MODEL: REPLACEMENT_MODEL}
    )
    parent_id = "refused-resume-parent"
    child_prompt = tmp_path / "resume-prompt.md"
    child_prompt.write_text("resume\n", encoding="utf-8")
    _write_codex_parent(
        project,
        state,
        dispatch_id=parent_id,
        model=REPLACEMENT_MODEL if explicit else REFUSED_MODEL,
    )
    before_records = sorted(path.name for path in (state / "runs.d").iterdir())
    before_homes = sorted(path.name for path in (state / "dispatch-homes").iterdir())
    argv = [
        "resume",
        parent_id,
        "--prompt-file",
        str(child_prompt),
        "--unregistered-forced",
    ]
    if explicit:
        argv += ["--model", REFUSED_MODEL]

    result = _run_dispatch(project, env, argv)

    _assert_refusal(result)
    assert sorted(path.name for path in (state / "runs.d").iterdir()) == before_records
    assert sorted(path.name for path in (state / "dispatch-homes").iterdir()) == before_homes
    assert not (state / "capacity.json").exists()
    assert not Path(env["GOALFLIGHT_JOURNAL_DIR"]).exists()
    assert not (tmp_path / "codex-spawned").exists()


def test_non_refused_model_passes_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, state, env = _runtime(
        tmp_path, monkeypatch, refused_models={REFUSED_MODEL: REPLACEMENT_MODEL}
    )
    dispatch_id = "allowed-model"
    marker = tmp_path / "allowed-worker-spawned"

    result = _run_dispatch(
        project,
        env,
        _raw_launch_argv(
            project=project,
            dispatch_id=dispatch_id,
            model=REPLACEMENT_MODEL,
            marker=marker,
        ),
    )

    assert result.returncode == 0, (result.stdout, result.stderr)
    assert marker.read_text(encoding="utf-8") == "spawned"
    record = json.loads(
        D.goalflight_ledger.record_path(dispatch_id, create=False).read_text(
            encoding="utf-8"
        )
    )
    assert record["model"] == REPLACEMENT_MODEL


def test_absent_refused_models_key_keeps_model_allowed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, _state, env = _runtime(tmp_path, monkeypatch)
    dispatch_id = "absent-policy-model"
    marker = tmp_path / "absent-policy-worker-spawned"

    result = _run_dispatch(
        project,
        env,
        _raw_launch_argv(
            project=project,
            dispatch_id=dispatch_id,
            model=REFUSED_MODEL,
            marker=marker,
        ),
    )

    assert result.returncode == 0, (result.stdout, result.stderr)
    assert marker.read_text(encoding="utf-8") == "spawned"
