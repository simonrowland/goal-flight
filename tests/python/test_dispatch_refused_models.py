"""Operator-configured model refusals leave launch state untouched."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from argparse import Namespace
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
    refused_models: object = None,
    agent_model_allow: object = None,
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
    worker_argv: list[str] | None = None,
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
    argv += ["--dispatch-id", dispatch_id, "--unregistered-forced", "--foreground", "--"]
    argv += worker_argv if worker_argv is not None else [sys.executable, "-c", worker]
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


def _assert_permanent_refusal(
    result: subprocess.CompletedProcess[str], expected_reason: str | None = None
) -> None:
    markers = [
        line.removeprefix(D.DISPATCH_REFUSED_PREFIX)
        for line in result.stdout.splitlines()
        if line.startswith(D.DISPATCH_REFUSED_PREFIX)
    ]
    assert len(markers) == 1, result.stdout
    payload = json.loads(markers[0])
    assert payload["permanent"] is True
    assert isinstance(payload.get("reason"), str) and payload["reason"]
    if expected_reason is not None:
        assert expected_reason in payload["reason"]
    assert D._permanent_pre_spawn_refusal_reason(result) == payload["reason"]


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
    _assert_permanent_refusal(result, REFUSED_MODEL)
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
    _assert_permanent_refusal(result, REFUSED_MODEL)
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
    project: Path,
    state: Path,
    *,
    dispatch_id: str,
    model: str | None,
    agent: str = "codex",
    dispatch_argv: list[str] | None = None,
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
            "agent": agent,
            "engine": agent,
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
            **({"dispatch_argv": dispatch_argv} if dispatch_argv is not None else {}),
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
    resume_lock_dir = state / "dispatch-homes" / ".resume-locks"
    before_resume_locks = (
        sorted(path.name for path in resume_lock_dir.iterdir())
        if resume_lock_dir.exists()
        else []
    )
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
    assert (
        sorted(path.name for path in resume_lock_dir.iterdir())
        if resume_lock_dir.exists()
        else []
    ) == before_resume_locks
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


def test_stale_dispatch_model_before_effective_model_does_not_refuse(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, _state, env = _runtime(
        tmp_path, monkeypatch, refused_models={REFUSED_MODEL: REPLACEMENT_MODEL}
    )
    dispatch_id = "effective-dispatch-model"
    marker = tmp_path / "effective-dispatch-worker-spawned"
    argv = _raw_launch_argv(
        project=project,
        dispatch_id=dispatch_id,
        model=REFUSED_MODEL,
        marker=marker,
    )
    model_index = argv.index("--model")
    argv[model_index + 2 : model_index + 2] = ["--model", REPLACEMENT_MODEL]

    result = _run_dispatch(project, env, argv)

    assert result.returncode == 0, (result.stdout, result.stderr)
    assert marker.read_text(encoding="utf-8") == "spawned"


@pytest.mark.parametrize(
    "dispatch_argv",
    [
        lambda project: [
            "--agent", "codex", "--shape", "bash", "--cwd", str(project),
            "--model", REPLACEMENT_MODEL, f"--model={REFUSED_MODEL}",
        ],
        lambda project: [
            "--agent", "cursor", "--shape", "bash", "--cwd", str(project),
        ],
    ],
    ids=["last-duplicate-model-equals-form", "child-agent-cursor-with-missing-model"],
)
def test_resume_policy_uses_child_launch_argv_before_preflight(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    dispatch_argv,
) -> None:
    project, state, env = _runtime(
        tmp_path,
        monkeypatch,
        refused_models={REFUSED_MODEL: REPLACEMENT_MODEL},
        agent_model_allow={"cursor": list(CURSOR_ALLOW_PATTERNS)},
    )
    parent_id = "resume-child-policy-parent"
    child_prompt = tmp_path / "resume-prompt.md"
    child_prompt.write_text("resume\n", encoding="utf-8")
    subprocess.run(
        [
            "git", "-C", str(project), "-c", "user.name=Test",
            "-c", "user.email=test@example.invalid", "commit", "--allow-empty",
            "--quiet", "-m", "initial",
        ],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    _write_codex_parent(
        project,
        state,
        dispatch_id=parent_id,
        model=None,
        dispatch_argv=dispatch_argv(project),
    )
    before_records = sorted(path.name for path in (state / "runs.d").iterdir())
    before_homes = sorted(path.name for path in (state / "dispatch-homes").iterdir())
    resume_lock_dir = state / "dispatch-homes" / ".resume-locks"
    before_resume_locks = (
        sorted(path.name for path in resume_lock_dir.iterdir())
        if resume_lock_dir.exists()
        else []
    )

    result = _run_dispatch(
        project,
        env,
        [
            "resume", parent_id, "--prompt-file", str(child_prompt),
            "--unregistered-forced",
        ],
    )

    assert result.returncode == 64, (result.returncode, result.stdout, result.stderr)
    assert "refused by operator policy" in result.stderr
    if "cursor" in dispatch_argv(project):
        assert "agent cursor model <missing>" in result.stderr
    else:
        assert f"model {REFUSED_MODEL} is refused" in result.stderr
    assert sorted(path.name for path in (state / "runs.d").iterdir()) == before_records
    assert sorted(path.name for path in (state / "dispatch-homes").iterdir()) == before_homes
    assert (
        sorted(path.name for path in resume_lock_dir.iterdir())
        if resume_lock_dir.exists()
        else []
    ) == before_resume_locks
    assert not (state / "capacity.json").exists()
    assert not Path(env["GOALFLIGHT_JOURNAL_DIR"]).exists()
    assert not (tmp_path / "codex-spawned").exists()
    _assert_permanent_refusal(result)


def test_resume_policy_refuses_before_preflight_is_called(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    project, _state, _env = _runtime(tmp_path, monkeypatch)
    prompt = tmp_path / "resume-preflight-prompt.md"
    prompt.write_text("resume\n", encoding="utf-8")
    parent_id = "resume-preflight-policy-parent"
    record = {
        "dispatch_id": parent_id,
        "agent": "codex",
        "engine": "codex",
        "model": None,
        "project_root": str(project),
        "worker_cwd": str(project),
        "dispatch_argv": [
            "--agent", "codex", "--shape", "bash", "--cwd", str(project),
            "--model", REPLACEMENT_MODEL, f"--model={REFUSED_MODEL}",
        ],
    }
    source = {
        "record": record,
        "engine": "codex",
        "agent": "codex",
        "shape": "bash",
        "session_id": "resume-session",
        "codex_home": project / "codex-home",
        "codex_home_owner_dispatch_id": parent_id,
    }
    monkeypatch.setattr(D, "_find_dispatch_record", lambda _dispatch_id: record)
    monkeypatch.setattr(
        D,
        "model_refusal",
        lambda model: (REFUSED_MODEL, REPLACEMENT_MODEL)
        if isinstance(model, str) and model.strip().casefold() == REFUSED_MODEL
        else None,
    )
    monkeypatch.setattr(D, "agent_model_allowlist", lambda _agent: None)
    monkeypatch.setattr(D, "_validate_resume_source", lambda *_a, **_k: source)
    monkeypatch.setattr(D, "_validate_resume_worktree_source", lambda *_a, **_k: None)
    monkeypatch.setattr(D, "_resume_worker_cwd", lambda *_a, **_k: project)
    monkeypatch.setattr(D, "_default_dispatch_id", lambda *_a, **_k: "resume-child")
    monkeypatch.setattr(D, "_refuse_existing_dispatch_id_for_resume", lambda *_a, **_k: None)
    preflight_calls: list[bool] = []

    def fail_if_preflight_called(*_args, **_kwargs):
        preflight_calls.append(True)
        raise AssertionError("resume preflight ran before the refusal")

    monkeypatch.setattr(D, "_preflight_resume_dispatch", fail_if_preflight_called)

    result = D._cmd_resume(
        [parent_id, "--prompt-file", str(prompt), "--unregistered-forced"]
    )

    assert result == 64
    assert preflight_calls == []
    assert f"model {REFUSED_MODEL} is refused" in capsys.readouterr().err


@pytest.mark.parametrize(
    "raw_worker",
    [
        ["codex", "exec", "--model", REPLACEMENT_MODEL, "--model", REFUSED_MODEL],
        ["codex", "exec", f"--model={REFUSED_MODEL}"],
        ["codex", "exec", "-m", REFUSED_MODEL],
        ["codex", "exec", "-c", f"model={REFUSED_MODEL}"],
        ["codex", "exec", "-c", f'model="{REFUSED_MODEL}"'],
        ["codex", "exec", "-c", f"model='{REFUSED_MODEL}'"],
        ["codex", "exec", "--config", f"model={REFUSED_MODEL}"],
        ["codex", "exec", f"--config=model={REFUSED_MODEL}"],
    ],
    ids=[
        "every-occurrence",
        "equals-form",
        "short-form",
        "codex-config-form",
        "double-quoted-config",
        "single-quoted-config",
        "long-config",
        "long-config-equals-form",
    ],
)
def test_raw_worker_model_occurrences_are_checked(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    raw_worker: list[str],
) -> None:
    project, state, env = _runtime(
        tmp_path, monkeypatch, refused_models={REFUSED_MODEL: REPLACEMENT_MODEL}
    )
    dispatch_id = "refused-raw-worker-model"
    marker = tmp_path / "raw-worker-spawned"
    argv = _raw_launch_argv(
        project=project,
        dispatch_id=dispatch_id,
        model=REPLACEMENT_MODEL,
        marker=marker,
        worker_argv=raw_worker,
    )

    result = _run_dispatch(project, env, argv)

    _assert_refusal(result)
    _assert_no_launch_effects(state, env, dispatch_id, marker)
    assert not (tmp_path / "codex-spawned").exists()


def test_refused_dispatch_model_is_checked_even_with_raw_allowed_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, state, env = _runtime(
        tmp_path, monkeypatch, refused_models={REFUSED_MODEL: REPLACEMENT_MODEL}
    )
    dispatch_id = "refused-dispatch-model-with-raw"
    marker = tmp_path / "raw-allowed-worker-spawned"
    argv = _raw_launch_argv(
        project=project,
        dispatch_id=dispatch_id,
        model=REFUSED_MODEL,
        marker=marker,
        worker_argv=["codex", "exec", "--model", REPLACEMENT_MODEL],
    )

    result = _run_dispatch(project, env, argv)

    _assert_refusal(result)
    _assert_no_launch_effects(state, env, dispatch_id, marker)


def test_cursor_raw_command_without_its_own_model_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, state, env = _runtime(
        tmp_path,
        monkeypatch,
        agent_model_allow={"cursor": list(CURSOR_ALLOW_PATTERNS)},
    )
    dispatch_id = "cursor-raw-missing-model"
    marker = tmp_path / "cursor-raw-worker-spawned"
    argv = _raw_launch_argv(
        project=project,
        dispatch_id=dispatch_id,
        model="grok-4.7-high",
        marker=marker,
        agent="cursor",
        worker_argv=["cursor-agent", "-p", "--force", "--trust"],
    )

    result = _run_dispatch(project, env, argv)

    _assert_cursor_allowlist_refusal(result, None)
    _assert_no_launch_effects(state, env, dispatch_id, marker)
    assert not (tmp_path / "cursor-spawned").exists()


@pytest.mark.parametrize(
    "worker_argv",
    [
        ["cursor-agent", "-p", "--force", "--trust"],
        ["bash", "-lc", "cursor-agent -p --force --trust"],
    ],
    ids=["raw-binary", "shell-command"],
)
def test_cursor_allowlist_follows_effective_raw_binary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    worker_argv: list[str],
) -> None:
    project, state, env = _runtime(
        tmp_path,
        monkeypatch,
        agent_model_allow={"cursor": list(CURSOR_ALLOW_PATTERNS)},
    )
    dispatch_id = "cursor-binary-under-codex-label"
    marker = tmp_path / "cursor-binary-under-other-agent-spawned"

    result = _run_dispatch(
        project,
        env,
        _raw_launch_argv(
            project=project,
            dispatch_id=dispatch_id,
            model=None,
            marker=marker,
            agent="codex",
            worker_argv=worker_argv,
        ),
    )

    _assert_cursor_allowlist_refusal(result, None)
    _assert_no_launch_effects(state, env, dispatch_id, marker)
    assert not (tmp_path / "cursor-spawned").exists()


def test_refused_model_inside_shell_command_is_checked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, state, env = _runtime(
        tmp_path, monkeypatch, refused_models={REFUSED_MODEL: REPLACEMENT_MODEL}
    )
    dispatch_id = "refused-model-shell-command"
    marker = tmp_path / "shell-command-worker-spawned"

    result = _run_dispatch(
        project,
        env,
        _raw_launch_argv(
            project=project,
            dispatch_id=dispatch_id,
            model=REPLACEMENT_MODEL,
            marker=marker,
            agent="codex",
            worker_argv=[
                "bash",
                "-lc",
                f"codex exec --model {REFUSED_MODEL}",
            ],
        ),
    )

    _assert_refusal(result)
    _assert_no_launch_effects(state, env, dispatch_id, marker)
    assert not (tmp_path / "codex-spawned").exists()


def test_unparseable_capacity_config_fails_closed_for_model_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, state, env = _runtime(tmp_path, monkeypatch)
    config = Path(env["GOALFLIGHT_CAPACITY_CONF"])
    config.write_text(
        f'{{"refused_models":{{"{REFUSED_MODEL}":"{REPLACEMENT_MODEL}",}}}}',
        encoding="utf-8",
    )
    dispatch_id = "unparseable-capacity-policy"
    marker = tmp_path / "unparseable-policy-worker-spawned"

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

    assert result.returncode == 64, (result.returncode, result.stdout, result.stderr)
    assert result.stderr.count("capacity config error:") == 1, result.stderr
    _assert_permanent_refusal(result, "capacity config error:")
    _assert_no_launch_effects(state, env, dispatch_id, marker)


def test_unparseable_capacity_config_does_not_block_model_free_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, _state, env = _runtime(tmp_path, monkeypatch)
    Path(env["GOALFLIGHT_CAPACITY_CONF"]).write_text("{broken", encoding="utf-8")
    dispatch_id = "unparseable-capacity-model-free"
    marker = tmp_path / "model-free-policy-worker-spawned"

    result = _run_dispatch(
        project,
        env,
        _raw_launch_argv(
            project=project,
            dispatch_id=dispatch_id,
            model=None,
            marker=marker,
        ),
    )

    assert result.returncode == 0, (result.stdout, result.stderr)
    assert marker.read_text(encoding="utf-8") == "spawned"


@pytest.mark.parametrize(
    "model",
    ["gpt-5.6-luna ", " GpT-5.6-LuNa"],
    ids=["trailing-live-whitespace", "leading-live-whitespace-and-case"],
)
def test_refused_model_matching_strips_and_casefolds_ids(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, model: str
) -> None:
    project, state, env = _runtime(
        tmp_path,
        monkeypatch,
        refused_models={" GpT-5.6-LuNa ": REPLACEMENT_MODEL},
    )
    dispatch_id = "refused-normalized-model"
    marker = tmp_path / "normalized-model-worker-spawned"

    result = _run_dispatch(
        project,
        env,
        _raw_launch_argv(
            project=project,
            dispatch_id=dispatch_id,
            model=model,
            marker=marker,
        ),
    )

    assert result.returncode == 64, (result.returncode, result.stdout, result.stderr)
    assert "model gpt-5.6-luna is refused" in result.stderr.casefold()
    _assert_no_launch_effects(state, env, dispatch_id, marker)


def test_agent_allowlist_normalizes_agent_keys_and_patterns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import goalflight_agent_limits as limits

    monkeypatch.setattr(
        limits,
        "LOCAL_OVERRIDES",
        {
            "refused_models": {" GpT-5.6-LuNa ": " gpt-6-luna "},
            "agent_model_allow": {
                " Cursor ": [" grok-* "],
                " Straße ": [" gpt-* "],
            },
        },
    )

    assert D.model_refusal(" gPt-5.6-lUnA ") == ("gpt-5.6-luna", "gpt-6-luna")
    assert D.agent_model_allowlist("cursor") == ("grok-*",)
    assert D.agent_model_allowlist("STRASSE") == ("gpt-*",)
    D._refuse_configured_model(" GROK-4.7-HIGH ", "cursor-agent")


@pytest.mark.parametrize(
    ("refused_models", "agent_model_allow", "agent", "model", "detail"),
    [
        ([], None, "test-dispatch", REPLACEMENT_MODEL, "refused_models must be an object"),
        ({REFUSED_MODEL: []}, None, "test-dispatch", REFUSED_MODEL, "replacement must be a string or null"),
        (None, {"cursor": "grok-*"}, "cursor", "grok-4.7-high", "agent_model_allow[cursor] must be a list"),
    ],
    ids=["refused-root-type", "refused-replacement-type", "allowlist-type"],
)
def test_mistyped_present_policy_fails_closed_with_config_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    refused_models: object,
    agent_model_allow: object,
    agent: str,
    model: str,
    detail: str,
) -> None:
    project, state, env = _runtime(
        tmp_path,
        monkeypatch,
        refused_models=refused_models,
        agent_model_allow=agent_model_allow,
    )
    dispatch_id = "mistyped-policy"
    marker = tmp_path / "mistyped-policy-worker-spawned"

    result = _run_dispatch(
        project,
        env,
        _raw_launch_argv(
            project=project,
            dispatch_id=dispatch_id,
            model=model,
            marker=marker,
            agent=agent,
        ),
    )

    assert result.returncode == 64, (result.returncode, result.stdout, result.stderr)
    assert f"capacity config error: {detail}" in result.stderr
    assert "(none)" not in result.stderr
    assert result.stderr.count("goalflight_dispatch:") == 1
    _assert_no_launch_effects(state, env, dispatch_id, marker)


def test_cursor_allowlist_takes_precedence_over_disallowed_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, state, env = _runtime(
        tmp_path,
        monkeypatch,
        refused_models={REFUSED_MODEL: REPLACEMENT_MODEL},
        agent_model_allow={"cursor": list(CURSOR_ALLOW_PATTERNS)},
    )
    dispatch_id = "cursor-refused-model-replacement"
    marker = tmp_path / "cursor-refused-replacement-worker-spawned"

    result = _run_dispatch(
        project,
        env,
        _raw_launch_argv(
            project=project,
            dispatch_id=dispatch_id,
            model=REFUSED_MODEL,
            marker=marker,
            agent="cursor",
        ),
    )

    _assert_cursor_allowlist_refusal(result, REFUSED_MODEL)
    assert REPLACEMENT_MODEL not in result.stderr
    _assert_no_launch_effects(state, env, dispatch_id, marker)


def test_acp_runner_checks_model_policy_before_status_or_spawn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, _state, env = _runtime(
        tmp_path,
        monkeypatch,
        agent_model_allow={"cursor": list(CURSOR_ALLOW_PATTERNS)},
    )
    status_path = tmp_path / "acp-status.json"
    dispatch_id = "acp-policy-refusal"

    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "goalflight_acp_run.py"),
            "--agent", "cursor",
            "--model", "gpt-6-luna",
            "--cwd", str(project),
            "--dispatch-id", dispatch_id,
            "--status-json", str(status_path),
            "--prompt-text", "test prompt",
            "--unregistered-forced",
        ],
        cwd=project,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=30,
    )

    assert result.returncode == 64, (result.returncode, result.stdout, result.stderr)
    assert "agent cursor model gpt-6-luna is refused" in result.stderr
    assert not status_path.exists()
    assert not (tmp_path / "cursor-spawned").exists()


def test_reasoning_effort_error_has_one_dispatch_prefix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, _state, env = _runtime(tmp_path, monkeypatch)
    argv = _raw_launch_argv(
        project=project,
        dispatch_id="reasoning-effort-error-prefix",
        model="grok-4.7-high",
        marker=tmp_path / "reasoning-effort-worker-spawned",
        agent="cursor",
    )
    argv[argv.index("--"):argv.index("--")] = ["--reasoning-effort", "high"]

    result = _run_dispatch(project, env, argv)

    assert result.returncode == 64, (result.returncode, result.stdout, result.stderr)
    assert "reasoning-effort" in result.stderr
    assert "goalflight_dispatch: goalflight_dispatch:" not in result.stderr
    assert result.stderr.count("goalflight_dispatch:") == 1


def test_refused_queue_drain_terminalizes_permanent_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, state, env = _runtime(
        tmp_path, monkeypatch, refused_models={REFUSED_MODEL: REPLACEMENT_MODEL}
    )
    dispatch_id = "refused-real-drain"
    queue_dir = state / "dispatch-queue"
    queue_dir.mkdir(parents=True)
    marker = tmp_path / "drained-worker-spawned"
    dispatch_argv = _raw_launch_argv(
        project=project,
        dispatch_id=dispatch_id,
        model=REFUSED_MODEL,
        marker=marker,
    )
    request = {
        "dispatch_id": dispatch_id,
        "cwd": str(project),
        "tail": str(tmp_path / "drain.tail"),
        "status_json": str(tmp_path / "drain.status.json"),
    }
    entry = {
        "schema": D.DISPATCH_QUEUE_SCHEMA,
        "state": "queued",
        "dispatch_id": dispatch_id,
        "created_at": D.goalflight_ledger.utc_now(),
        "updated_at": D.goalflight_ledger.utc_now(),
        "agent": "test-dispatch",
        "shape": "bash",
        "project_root": str(project),
        "process_cwd": str(project),
        "transport": "dispatch",
        "dispatch_argv": dispatch_argv,
        "request": request,
    }
    queue_entry = queue_dir / f"{dispatch_id}.json"
    queue_entry.write_text(json.dumps(entry), encoding="utf-8")
    D.goalflight_ledger.write_record(
        {
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
    )

    result = _run_dispatch(
        project,
        env,
        ["drain", "--cross-project", "--dispatch-id", dispatch_id, "--json"],
    )

    assert result.returncode == 1, (result.returncode, result.stdout, result.stderr)
    payload = json.loads(result.stdout)
    assert payload["failed"] == 1, {
        key: payload.get(key)
        for key in ("failed", "launched", "left_queued", "remaining", "details", "recovered_claims", "holds")
    }
    assert payload["remaining"] == 0, payload
    assert not queue_entry.exists()
    failed_claims = list(queue_dir.glob(f"{dispatch_id}.json.claimed-*.failed"))
    assert len(failed_claims) == 1
    failed_payload = json.loads(failed_claims[0].read_text(encoding="utf-8"))
    assert failed_payload["state"] == "failed"
    assert "refused by operator policy" in failed_payload["reason"]
    capacity = json.loads((state / "capacity.json").read_text(encoding="utf-8"))
    assert not any(
        lease.get("dispatch_id") == dispatch_id
        for lease in capacity.get("leases", {}).values()
    )
    assert not Path(env["GOALFLIGHT_JOURNAL_DIR"]).exists()
    assert not marker.exists()


def test_remote_queue_drain_refuses_before_fleet_preview(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project, state, _env = _runtime(
        tmp_path, monkeypatch, refused_models={REFUSED_MODEL: REPLACEMENT_MODEL}
    )
    import goalflight_agent_limits as limits
    import goalflight_fleet_dispatch as fleet_dispatch

    monkeypatch.setattr(
        limits,
        "LOCAL_OVERRIDES",
        {"refused_models": {REFUSED_MODEL: REPLACEMENT_MODEL}},
    )
    monkeypatch.setattr(limits, "LOCAL_OVERRIDES_LOAD_ERROR", None)
    monkeypatch.setenv("GOALFLIGHT_LIVE_SSH", "1")
    dispatch_id = "remote-drain-refused-model"
    queue_dir = state / "dispatch-queue"
    queue_dir.mkdir(parents=True)
    marker = tmp_path / "remote-drain-worker-spawned"
    dispatch_argv = _raw_launch_argv(
        project=project,
        dispatch_id=dispatch_id,
        model=REFUSED_MODEL,
        marker=marker,
        agent="codex",
    )
    request = {
        "dispatch_id": dispatch_id,
        "cwd": str(project),
        "prompt": "queued remote prompt",
        "base_sha": "a" * 40,
    }
    entry = {
        "schema": D.DISPATCH_QUEUE_SCHEMA,
        "state": "queued",
        "dispatch_id": dispatch_id,
        "created_at": D.goalflight_ledger.utc_now(),
        "updated_at": D.goalflight_ledger.utc_now(),
        "agent": "codex",
        "shape": "acp",
        "project_root": str(project),
        "process_cwd": str(project),
        "transport": "dispatch",
        "dispatch_argv": dispatch_argv,
        "request": request,
    }
    queue_entry = queue_dir / f"{dispatch_id}.json"
    queue_entry.write_text(json.dumps(entry), encoding="utf-8")
    D.goalflight_ledger.write_record(
        {
            "schema": D.goalflight_ledger.SCHEMA,
            "dispatch_id": dispatch_id,
            "agent": "codex",
            "model": REFUSED_MODEL,
            "state": "queued",
            "reason": "dispatch_queue",
            "project_root": str(project),
            "worker_cwd": str(project),
            "task_ids": [],
        }
    )

    fleet_dir = tmp_path / "fleet"
    fleet_dir.mkdir(exist_ok=True)
    preview_calls: list[dict[str, object]] = []
    monkeypatch.setattr(D, "_validate_remote_drain_node", lambda _args: fleet_dir)
    monkeypatch.setattr(
        fleet_dispatch,
        "preview_dispatch",
        lambda *_args, **kwargs: preview_calls.append(kwargs) or Namespace(),
    )
    monkeypatch.setattr(fleet_dispatch, "assert_live_ssh_opt_in", lambda: None)
    monkeypatch.setattr(
        fleet_dispatch,
        "execute_dispatch",
        lambda *_args, **_kwargs: {"launch_unconfirmed": False},
    )

    result = D._cmd_drain(
        [
            "--queue-dir",
            str(queue_dir),
            "--cross-project",
            "--dispatch-id",
            dispatch_id,
            "--remote-node",
            "fixture-node",
            "--json",
        ]
    )

    assert result == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["failed"] == 1, payload
    assert preview_calls == []
    assert not queue_entry.exists()
    failed_claims = list(queue_dir.glob(f"{dispatch_id}.json.claimed-*.failed"))
    assert len(failed_claims) == 1
    failed_payload = json.loads(failed_claims[0].read_text(encoding="utf-8"))
    assert "refused by operator policy" in failed_payload["reason"]
    assert not (state / "capacity.json").exists()
    assert not marker.exists()
