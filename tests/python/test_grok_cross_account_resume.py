"""Cross-account Grok resume keeps context or reconstructs it locally."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

import goalflight_dispatch as D
import goalflight_ledger as L


SESSION = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
CWD_NAME = "worker-tree"


@pytest.fixture(autouse=True)
def isolated(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
    monkeypatch.setenv("GOALFLIGHT_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("GOALFLIGHT_DISPATCH_DIR", str(tmp_path / "dispatch"))
    monkeypatch.setenv("GOALFLIGHT_CAPACITY_CONF", "/dev/null")


def _accounts(tmp_path: Path, *names: str) -> None:
    for name in names:
        home = Path.home() / ".goal-flight" / "accounts" / name / "grok"
        home.mkdir(parents=True)


def _session(account: str, cwd: Path, *, with_files: bool = True) -> Path:
    path = D._seat_session_dir(account, "grok", str(cwd), SESSION)
    assert path is not None
    path.mkdir(parents=True)
    if with_files:
        (path / "summary.json").write_text(
            json.dumps({"last_action": "edited worker.py"}), encoding="utf-8"
        )
        (path / "chat_history.jsonl").write_text(
            '{"role":"assistant","content":"edited worker.py"}\n',
            encoding="utf-8",
        )
    return path


def _record(tmp_path: Path, *, account: str = "old", cwd: Path | None = None) -> dict:
    worktree = cwd or tmp_path / CWD_NAME
    worktree.mkdir(exist_ok=True)
    brief = tmp_path / "original-brief.md"
    brief.write_text("Original task: preserve the partial implementation.\n", encoding="utf-8")
    tail = tmp_path / "parent.tail"
    tail.write_text("last worker turn: changed worker.py\n", encoding="utf-8")
    return {
        "dispatch_id": "grok-parent",
        "agent": "grok-code",
        "engine": "grok",
        "shape": "bash",
        "account": account,
        "effective_account": account,
        "worker_cwd": str(worktree),
        "prompt_path": str(brief),
        "stdout_path": str(tail),
        "dispatch_argv": [
            "--agent", "grok-code", "--cwd", str(worktree),
            "--account", account, "--prompt-file", str(brief),
        ],
    }


def _resume_args(**overrides):
    values = dict(
        dispatch_id="grok-parent",
        account=None,
        cwd=None,
        os_sandbox=None,
        unregistered_forced=True,
        controller_label=None,
        controller_pid=None,
        controller_session_id=None,
    )
    values.update(overrides)
    return SimpleNamespace(**values)


def _source(record: dict) -> dict:
    return {
        "record": record,
        "engine": "grok",
        "agent": "grok-code",
        "shape": "bash",
        "session_id": SESSION,
        "codex_home": None,
        "codex_home_owner_dispatch_id": None,
    }


def _option(argv: list[str], flag: str) -> str:
    return argv[argv.index(flag) + 1]


def test_same_account_resume_is_preferred(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _accounts(tmp_path, "old", "new")
    record = _record(tmp_path)
    monkeypatch.setattr(D, "_account_quota_blocked", lambda *args, **kwargs: False)
    monkeypatch.setattr(D, "_grok_account_admission_reason", lambda *args, **kwargs: None)
    monkeypatch.setattr(D, "migrate_seat_session", lambda **kwargs: pytest.fail("same account must not carry"))

    argv = D._resume_launch_argv(
        _source(record), child_dispatch_id="grok-child", prompt_path=Path(record["prompt_path"]),
        resume_args=_resume_args(),
    )

    assert _option(argv, "--account") == "old"
    assert _option(argv, "--resume-mode") == "same-account"
    assert _option(argv, "--engine-session-id") == SESSION
    assert "--resume-reconstruction" not in argv


def test_walled_account_carries_session_to_healthy_account(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _accounts(tmp_path, "old", "new")
    record = _record(tmp_path)
    _session("old", Path(record["worker_cwd"]))
    monkeypatch.setattr(D, "_account_quota_blocked", lambda account, **kwargs: account == "old")
    monkeypatch.setattr(
        D,
        "_select_healthy_grok_account",
        lambda **kwargs: "new",
    )

    argv = D._resume_launch_argv(
        _source(record), child_dispatch_id="grok-child", prompt_path=Path(record["prompt_path"]),
        resume_args=_resume_args(),
    )

    assert _option(argv, "--account") == "new"
    assert _option(argv, "--resume-mode") == "carried"
    assert _option(argv, "--engine-session-id") == SESSION


def test_failed_carry_reconstructs_prompt_and_starts_fresh(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _accounts(tmp_path, "old", "new")
    record = _record(tmp_path)
    worktree = Path(record["worker_cwd"])
    _session("old", worktree)
    subprocess.run(["git", "init", "-q"], cwd=worktree, check=True)
    (worktree / "partial.py").write_text("dirty = True\n", encoding="utf-8")
    monkeypatch.setattr(D, "_account_quota_blocked", lambda account, **kwargs: account == "old")
    monkeypatch.setattr(
        D,
        "_select_healthy_grok_account",
        lambda **kwargs: "new",
    )
    monkeypatch.setattr(D, "migrate_seat_session", lambda **kwargs: (False, "fixture carry miss"))

    controller_prompt = tmp_path / "controller.md"
    controller_prompt.write_text("Controller says: continue from the dirty tree.\n", encoding="utf-8")
    argv = D._resume_launch_argv(
        _source(record), child_dispatch_id="grok-child", prompt_path=controller_prompt,
        resume_args=_resume_args(),
    )

    reconstructed = Path(_option(argv, "--prompt-file"))
    text = reconstructed.read_text(encoding="utf-8")
    assert _option(argv, "--account") == "new"
    assert _option(argv, "--resume-mode") == "reconstructed"
    assert "--resume-reconstruction" in argv
    assert "--resume" not in argv
    assert _option(argv, "--engine-session-id") != SESSION
    for expected in (
        "RESUMED-BY-RECONSTRUCTION",
        "Original task: preserve the partial implementation.",
        "last_action",
        "edited worker.py",
        "git status --short",
        "git diff --stat",
        "Controller says: continue from the dirty tree.",
    ):
        assert expected in text


def test_reconstruction_does_not_refuse_with_healthy_account(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _accounts(tmp_path, "old", "new")
    record = _record(tmp_path)
    monkeypatch.setattr(D, "_account_quota_blocked", lambda account, **kwargs: account == "old")
    monkeypatch.setattr(
        D,
        "_select_healthy_grok_account",
        lambda **kwargs: "new",
    )
    monkeypatch.setattr(D, "migrate_seat_session", lambda **kwargs: (False, "missing artifact"))

    argv = D._resume_launch_argv(
        _source(record), child_dispatch_id="grok-child", prompt_path=Path(record["prompt_path"]),
        resume_args=_resume_args(),
    )

    assert _option(argv, "--account") == "new"
    assert _option(argv, "--resume-mode") == "reconstructed"


def test_fallback_uses_measured_selector_not_configured_order(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _accounts(tmp_path, "old", "bad", "good")
    record = _record(tmp_path)
    calls: list[dict] = []

    def measured_selector(**kwargs):
        calls.append(kwargs)
        return "good"

    monkeypatch.setattr(D, "_account_quota_blocked", lambda account, **kwargs: account == "old")
    monkeypatch.setattr(D, "_select_healthy_grok_account", measured_selector)

    argv = D._resume_launch_argv(
        _source(record), child_dispatch_id="grok-child", prompt_path=Path(record["prompt_path"]),
        resume_args=_resume_args(),
    )

    assert _option(argv, "--account") == "good"
    assert calls == [{"model": None, "exclude": {"old"}, "named_only": True}]


def test_measured_unhealthy_owner_falls_back_without_ledger_wall(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _accounts(tmp_path, "old", "good")
    record = _record(tmp_path)
    monkeypatch.setattr(D, "_seat_probe_says_usable", lambda *args, **kwargs: False)
    monkeypatch.setattr(D, "_account_quota_blocked", lambda *args, **kwargs: False)
    monkeypatch.setattr(D, "_select_healthy_grok_account", lambda **kwargs: "good")

    argv = D._resume_launch_argv(
        _source(record), child_dispatch_id="grok-child", prompt_path=Path(record["prompt_path"]),
        resume_args=_resume_args(),
    )

    assert _option(argv, "--account") == "good"


def test_resume_mode_is_carried_into_status_metadata_and_watcher(
    tmp_path: Path,
) -> None:
    args = SimpleNamespace(
        dispatch_id="grok-child",
        agent="grok-code",
        model="gpt-5.6-sol",
        shape="bash",
        controller_session_id=None,
        controller_pid=None,
        controller_label=None,
        task_ids=[],
        parent_dispatch_id="grok-parent",
        resume_mode="reconstructed",
        _worktree_id=None,
        _worktree_path=None,
        _worktree_base_commit=None,
        codex_session_id=None,
        codex_resume_home=None,
        codex_home_owner_dispatch_id=None,
    )
    metadata = D._prelaunch_status_metadata(args)
    assert metadata["model"] == "gpt-5.6-sol"
    assert metadata["resume_mode"] == "reconstructed"
    watcher = D._watcher_spawn_argv(
        worker_pid=123,
        tail=tmp_path / "tail",
        status_json=tmp_path / "status.json",
        agent="grok-code",
        poll_secs=1,
        max_idle_secs=10,
        dispatch_id="grok-child",
        pgid=123,
        parent_dispatch_id="grok-parent",
        resume_mode="reconstructed",
    )
    assert watcher[watcher.index("--resume-mode") + 1] == "reconstructed"
