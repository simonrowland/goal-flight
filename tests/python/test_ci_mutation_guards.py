"""Poison pairs for load-bearing branches that survived targeted mutations."""

from __future__ import annotations

import argparse
import ast
import asyncio
import fnmatch
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import goalflight_acp_run as acp_run  # noqa: E402
import goalflight_capacity as capacity  # noqa: E402
import goalflight_liveness as liveness  # noqa: E402
import goalflight_journal as journal  # noqa: E402
import goalflight_ledger as ledger  # noqa: E402
import goalflight_messages as messages  # noqa: E402
import goalflight_status as status  # noqa: E402
import goalflight_terminal as terminal  # noqa: E402
import dispatch_watcher_gate as dispatch_gate  # noqa: E402


def test_attention_state_wakes_without_a_scraped_marker() -> None:
    for field, state_name in (
        ("state", "awaiting_user_confirm"),
        ("state", "awaiting_permission"),
        ("classification", "running_user_confirm"),
    ):
        record = {
            "dispatch_id": "attention-state-only",
            "state": "running",
            "classification": "expected_live",
        }
        record[field] = state_name
        assert status.done_code(record, worker_alive=True) == 0, record

    assert status.done_code(
        {"state": "running", "classification": "expected_live"},
        worker_alive=True,
    ) == 1


def test_async_capacity_wait_maps_confirmed_cancellation() -> None:
    clock = iter((10.0, 10.0, 11.0))

    def acquire_wait(_args: argparse.Namespace) -> int:
        print(json.dumps({"decision": "wait", "reason": "machine_worker_cap"}))
        return 1

    async def cancelled_sleep(_seconds: float) -> None:
        raise asyncio.CancelledError

    async def exercise() -> None:
        with pytest.raises(capacity.CapacityWaitInterrupted) as raised:
            await capacity.acquire_with_wait_async(
                argparse.Namespace(),
                lane="normal",
                wait_s=30.0,
                poll_s=1.0,
                jitter=0.0,
                interrupted=lambda: True,
                interrupted_signum=lambda: 15,
                monotonic_fn=lambda: next(clock),
                sleep_fn=cancelled_sleep,
                acquire_func=acquire_wait,
            )
        error = raised.value
        assert error.exit_code == 143
        assert error.signum == 15
        assert error.payload == {
            "decision": "wait",
            "reason": "wait_interrupted",
            "waited_s": 1.0,
            "attempts": 1,
        }

    asyncio.run(exercise())


def test_acp_permission_read_only_capability_is_fail_closed() -> None:
    assert acp_run.acp_permission_read_only_supported("claude")
    assert acp_run.acp_permission_read_only_supported("claude-acp")
    for adapter in (None, "codex", "cursor", "grok", "kimi"):
        assert not acp_run.acp_permission_read_only_supported(adapter), adapter


def test_acp_success_marker_helper_matches_terminal_contract() -> None:
    for kind in ("COMPLETE", "READY", "RESULT"):
        assert acp_run._successful_terminal_marker({kind: ["evidence"]}), kind
    for kind in ("FAILED", "BLOCKED", "USER-NEED", "USER-CONFIRM"):
        assert not acp_run._successful_terminal_marker({kind: ["evidence"]}), kind
    assert not acp_run._successful_terminal_marker({"COMPLETE": []})


def test_terminal_rate_limit_numbers_require_token_boundaries() -> None:
    assert terminal.rate_limit_signature_in_text("provider returned HTTP 429") == "429"
    assert terminal.rate_limit_signature_in_text("provider returned HTTP 529") == "529"
    assert terminal.rate_limit_signature_in_text("ordinary log line 1429") is None
    assert terminal.rate_limit_signature_in_text("ordinary log line 5290") is None


def test_blocked_provisional_uses_attention_and_fresh_probe() -> None:
    assert terminal.worker_liveness_from_probe(False, "identity_indeterminate") == "unknown"
    for evidence in (
        {"state": "blocked"},
        {"reason": "marker:BLOCKED"},
        {"reason": {"marker_kind": "BLOCKED"}},
        {"terminal_marker": {"kind": "BLOCKED"}},
    ):
        assert terminal.blocked_outcome_is_provisional(evidence, "live"), evidence
        assert terminal.blocked_outcome_is_provisional(evidence, "unknown"), evidence
    for kind in ("FAILED", "USER-NEED", "USER-CONFIRM", "COMPLETE"):
        evidence = {
            "state": "blocked",
            "terminal_marker": {"kind": kind},
            "reason": {"marker_kind": kind},
        }
        assert not terminal.blocked_outcome_is_provisional(evidence, "live"), evidence
    assert not terminal.blocked_outcome_is_provisional(
        {"state": "watcher_stopped", "terminal_pending_state": "blocked"},
        "live",
    )
    assert ledger._terminal_sidecar_observation(
        {"dispatch_id": "pending-only"},
        {
            "dispatch_id": "pending-only",
            "state": "watcher_stopped",
            "terminal_pending_state": "blocked",
        },
    ) is None
    assert terminal.blocked_outcome_is_provisional(
        {"attention_marker": {"kind": "BLOCKED"}}, "live"
    )
    assert terminal.blocked_outcome_is_provisional(
        {"attention_marker": {"kind": "BLOCKED"}}, "unknown"
    )
    assert not terminal.blocked_outcome_is_provisional(
        {
            "attention_marker": {"kind": "BLOCKED"},
            "terminal_marker": {"kind": "COMPLETE"},
        },
        "unknown",
    )
    assert not terminal.blocked_outcome_is_provisional(
        {"state": "complete", "attention_marker": {"kind": "BLOCKED"}},
        "live",
    )
    assert not terminal.blocked_outcome_is_provisional(
        {"attention_marker": {"kind": "BLOCKED"}}, "dead"
    )


def test_attention_marker_helper_is_shared_by_status(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # t-367: the foreign-id rule is a ledger lookup, not a shape guess —
    # record the foreign id FOR REAL so its marker names a known foreign
    # dispatch and must not bind.
    monkeypatch.setenv("GOALFLIGHT_STATE_DIR", str(tmp_path / "state"))
    ledger.write_record(
        {
            "schema": ledger.SCHEMA,
            "dispatch_id": "foreign-attention-marker",
            "state": "running",
        }
    )
    for kind in terminal.ATTENTION_MARKERS:
        marker = {"kind": kind, "text": "attention-marker — needs controller"}
        assert terminal.attention_marker_present(marker), kind
        assert status._record_has_attention_marker(
            {"dispatch_id": "attention-marker", "terminal_marker": marker}
        ), kind
        assert not status._record_has_attention_marker(
            {
                "dispatch_id": "attention-marker",
                "terminal_marker": {
                    "kind": kind,
                    "text": "foreign-attention-marker — needs controller",
                },
            }
        ), kind
    for kind in terminal.SUCCESS_TERMINAL_MARKERS:
        marker = {"kind": kind, "text": "finished"}
        assert not terminal.attention_marker_present(marker), kind


def test_attention_marker_ceremony_free_binds_named_foreign_does_not(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Bare and prose attention still bind; a named foreign dispatch does not.

    The identity contract emits ``<KIND>: <dispatch-id> — <summary>``. A
    ceremony-free ``BLOCKED:`` (empty payload) is the property the attention
    exemption exists to protect. A ledger-known live id followed by an em
    dash is the instructed foreign form and must not bind. Ordinary
    first-word prose, including the brief's ``foreign package unavailable``
    case, must still bind.

    Since t-367 the foreign rule is a ledger lookup, not a shape guess:
    ``other-live-id`` only counts as a foreign dispatch because the test
    writes a real ledger record for it; an unknown hyphenated token binds.
    """
    monkeypatch.setenv("GOALFLIGHT_STATE_DIR", str(tmp_path / "state"))
    own_id = "attention-marker"
    live_foreign = "other-live-id"
    ledger.write_record(
        {"schema": ledger.SCHEMA, "dispatch_id": live_foreign, "state": "running"}
    )
    for kind in terminal.ATTENTION_MARKERS:
        assert status._record_has_attention_marker(
            {"dispatch_id": own_id, "terminal_marker": {"kind": kind, "text": ""}}
        ), kind
        assert not status._record_has_attention_marker(
            {
                "dispatch_id": own_id,
                "terminal_marker": {
                    "kind": kind,
                    "text": f"{live_foreign} — needs controller",
                },
            }
        ), kind
        assert status._record_has_attention_marker(
            {
                "dispatch_id": own_id,
                "terminal_marker": {
                    "kind": kind,
                    "text": "foreign package unavailable",
                },
            }
        ), kind


def test_stale_cpu_sample_forces_a_fresh_pair(monkeypatch: pytest.MonkeyPatch) -> None:
    pgid = 4242
    snapshots = iter(({7: 10.0}, {7: 10.6}))
    clock = iter((100.0, 100.6))
    sleeps: list[float] = []

    liveness._cpu_samples.clear()
    liveness._cpu_samples[pgid] = (0.0, {7: 1.0})
    monkeypatch.setattr(liveness, "process_group_id", lambda _pid: pgid)
    monkeypatch.setattr(liveness, "_pgroup_cputime_snapshot", lambda _pgid: next(snapshots))
    monkeypatch.setattr(liveness.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(liveness.time, "sleep", lambda seconds: sleeps.append(seconds))

    measured = liveness.pgroup_cpu_pct(pgid)

    assert measured == pytest.approx(100.0)
    assert sleeps == [liveness._CPU_SAMPLE_COLD_WINDOW_S]
    assert liveness._cpu_samples[pgid] == (100.6, {7: 10.6})


def test_missing_narrow_journal_api_is_not_silently_skipped(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setattr(status.goalflight_task, "resolve_project_root", lambda _value: project)

    def expected_unavailable(_cls: object, _root: object) -> object:
        raise journal.JournalBusy("expected read outage")

    monkeypatch.setattr(journal.Journal, "open_reader", classmethod(expected_unavailable))
    assert status._mail_watermark(str(project), ["shape-check"]) == set()

    monkeypatch.setattr(journal.Journal, "open_reader", None)
    with pytest.raises(AttributeError):
        status._mail_watermark(str(project), ["shape-check"])
    monkeypatch.delattr(journal.Journal, "open_reader")
    with pytest.raises(AttributeError):
        status._mail_watermark(str(project), ["shape-check"])


def _source_function(filename: str, name: str) -> str:
    path = REPO_ROOT / "scripts" / filename
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    function = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == name
    )
    return ast.get_source_segment(source, function) or ""


def test_terminal_withdrawal_and_expiry_guards_remain() -> None:
    assert "_withdraw_preflight(" in _source_function(
        "goalflight_dispatch.py", "_settle_final_dispatch"
    )
    assert "_withdraw_preflight(" in _source_function(
        "goalflight_dispatch.py", "_cmd_withdraw"
    )
    expired_attempt = _source_function("goalflight_journal.py", "commit_expired_attempt")
    assert "_deadline_at_or_before=observed_at" in expired_attempt


def test_live_blocked_is_provisional_at_terminal_sinks(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    result = ledger.commit_terminal_authority(
        {"dispatch_id": "blocked-string-reason", "project_root": str(project)},
        state="blocked",
        reason="marker:BLOCKED",
        worker_still_alive=True,
    )
    status_results = []
    for worker_alive in (True, None):
        payload = {
            "dispatch_id": f"direct-blocked-{worker_alive}",
            "state": "blocked",
            "worker_alive": worker_alive,
            "terminal_marker": {"kind": "BLOCKED"},
        }
        status_path = tmp_path / f"{payload['dispatch_id']}.status.json"
        liveness.write_status(status_path, payload)
        persisted = json.loads(status_path.read_text(encoding="utf-8"))
        status_results.append(
            {
                "converted": (
                    persisted.get("state") == "watcher_stopped"
                    and persisted.get("terminal_pending_state") == "blocked"
                    and persisted.get("terminal_marker") is None
                    and persisted.get("attention_marker", {}).get("kind") == "BLOCKED"
                ),
                "payload": persisted,
            }
        )
    assert (
        not result.committed
        and result.disposition is journal.WriteDisposition.RETRYABLE
        and all(item["converted"] for item in status_results)
    ), {"ledger_result": result, "status_sink_results": status_results}


def test_dispatch_watcher_gate_selector_returns_policy_union(tmp_path: Path) -> None:
    test_dir = tmp_path / "tests" / "python"
    test_dir.mkdir(parents=True)
    for name in (
        "test_dispatch_core.py",
        "test_watch_rules.py",
        "test_drain_worker.py",
        "test_review_job.py",
        "test_unrelated.py",
    ):
        (test_dir / name).write_text("# selector fixture\n", encoding="utf-8")

    policy = _prepare_dispatch_watcher_gate_fixture(tmp_path)
    review_patterns = ("test_drain_*.py", "test_review_job.py")
    selection = dispatch_gate.select_gate(tmp_path, review_patterns, policy=policy)
    all_modules = {
        path.relative_to(tmp_path).as_posix(): path.name
        for path in test_dir.rglob(policy["module_pattern"])
    }
    prefix_family = {
        module
        for module, name in all_modules.items()
        if any(
            fnmatch.fnmatchcase(name, pattern)
            for pattern in policy["prefix_patterns"]
        )
    }
    review_family = {
        module
        for module, name in all_modules.items()
        if any(
            fnmatch.fnmatchcase(name, pattern)
            for pattern in review_patterns
        )
    }

    assert set(selection.modules) == prefix_family | review_family
    assert "tests/python/test_unrelated.py" not in selection.modules
    assert selection.revisions == tuple(policy["revision_targets"])
    assert selection.fallback_reason is None


def test_dispatch_watcher_gate_selects_all_drain_modules_without_count_pins(
    tmp_path: Path,
) -> None:
    tracked = subprocess.check_output(
        ["git", "-C", str(REPO_ROOT), "ls-files", "-z", "--", "tests/python"]
    ).decode("utf-8").split("\0")
    policy_template = dispatch_gate.load_policy()
    tracked_modules = sorted(
        path
        for path in tracked
        if path
        and fnmatch.fnmatchcase(Path(path).name, policy_template["module_pattern"])
    )
    selected_tracked = [
        path
        for path in tracked_modules
        if Path(path).name.startswith("test_drain_")
        or any(
            fnmatch.fnmatchcase(Path(path).name, pattern)
            for pattern in policy_template["prefix_patterns"]
        )
    ]
    required_untracked = policy_template["required_untracked_modules"]

    for label, local_only_modules in (
        ("worktree", []),
        ("main-checkout", required_untracked),
    ):
        repo_root = tmp_path / label
        for relative_path in (*selected_tracked, *local_only_modules):
            module_path = repo_root / relative_path
            module_path.parent.mkdir(parents=True, exist_ok=True)
            module_path.write_text("# selector fixture\n", encoding="utf-8")
        if local_only_modules:
            (repo_root / ".gitignore").write_text(
                "tests/python/ext/\n", encoding="utf-8"
            )

        policy = _prepare_dispatch_watcher_gate_fixture(repo_root)
        policy["required_untracked_modules"] = list(local_only_modules)
        test_dir = repo_root / "tests" / "python"
        all_modules = {
            path.relative_to(repo_root).as_posix(): path.name
            for path in test_dir.rglob(policy["module_pattern"])
        }
        drains = {
            module
            for module, name in all_modules.items()
            if name.startswith("test_drain_")
        }
        prefix_family = {
            module
            for module, name in all_modules.items()
            if any(
                fnmatch.fnmatchcase(name, pattern)
                for pattern in policy["prefix_patterns"]
            )
        }
        selection = dispatch_gate.select_gate(
            repo_root,
            ("test_drain_*",),
            policy=policy,
        )

        assert drains, label
        assert not prefix_family.intersection(drains), label
        assert drains <= set(selection.modules), label
        assert prefix_family <= set(selection.modules), label


def test_dispatch_watcher_gate_policy_pins_the_required_local_only_modules() -> None:
    assert set(dispatch_gate.load_policy()["required_untracked_modules"]) == {
        "tests/python/ext/test_claude_desktop_backfill.py",
        "tests/python/ext/test_claude_desktop_sidebar_save.py",
        "tests/python/ext/test_claude_usage.py",
        "tests/python/ext/test_codex_fence.py",
        "tests/python/ext/test_codex_parity_probe.py",
        "tests/python/ext/test_codex_rotate.py",
        "tests/python/ext/test_codex_seat_integration.py",
        "tests/python/ext/test_codex_seat_lib.py",
        "tests/python/ext/test_codex_seatd.py",
        "tests/python/ext/test_cursor_usage.py",
        "tests/python/ext/test_grok_rotate.py",
        "tests/python/ext/test_kimi_usage.py",
    }


def _run_dispatch_watcher_gate_driver(
    tmp_path: Path,
    selection: dict[str, object],
    label: str,
) -> tuple[subprocess.CompletedProcess[str], Path]:
    policy_text = (REPO_ROOT / "AGENTS.md").read_text(encoding="utf-8")
    gate_text = policy_text.split("## Dispatch/watcher family A/B deploy gate", 1)[1]
    gate_text = gate_text.split("## Git workflow", 1)[0]
    driver_start = 'python3 - "$selection_json" "$DEPLOYED_SHA" <<\'PY\'\n'
    assert driver_start in gate_text
    driver = gate_text.split(driver_start, 1)[1].split("\nPY\n", 1)[0]
    fake_runner = tmp_path / "fake-remote-runner"
    fake_runner.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        "from pathlib import Path\n"
        "Path(os.environ['REMOTE_ARGS_PATH']).write_text(json.dumps(sys.argv[1:]))\n",
        encoding="utf-8",
    )
    fake_runner.chmod(0o755)
    args_path = tmp_path / f"remote-args-{label}.json"
    result = subprocess.run(
        [sys.executable, "-c", driver, json.dumps(selection), "deployed-sha"],
        check=False,
        capture_output=True,
        env={
            **os.environ,
            "GOALFLIGHT_REMOTE_RUN": str(fake_runner),
            "REMOTE_ARGS_PATH": str(args_path),
        },
        text=True,
    )
    return result, args_path


def _prepare_dispatch_watcher_gate_fixture(repo_root: Path) -> dict[str, object]:
    policy = dispatch_gate.load_policy()
    policy["required_untracked_modules"] = []
    scripts = repo_root / "scripts"
    scripts.mkdir(parents=True, exist_ok=True)
    (scripts / "dispatch_watcher_gate_policy.json").write_text(
        json.dumps(policy), encoding="utf-8"
    )
    subprocess.run(
        ["git", "init", "--quiet", str(repo_root)],
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "-C", str(repo_root), "add", "--", "tests/python"],
        check=True,
        capture_output=True,
        text=True,
    )
    return policy


def test_dispatch_watcher_gate_refuses_a_missing_tracked_module(
    tmp_path: Path,
) -> None:
    test_dir = tmp_path / "tests" / "python"
    test_dir.mkdir(parents=True)
    (test_dir / "test_present.py").write_text("# present\n", encoding="utf-8")
    missing = test_dir / "test_missing.py"
    missing.write_text("# staged, then made invisible\n", encoding="utf-8")
    policy = _prepare_dispatch_watcher_gate_fixture(tmp_path)
    policy["required_untracked_modules"] = [
        "tests/python/ext/test_local_only.py"
    ]
    missing.unlink()

    with pytest.raises(
        dispatch_gate.GatePolicyError, match="module universe mismatch"
    ) as exc:
        dispatch_gate.select_gate(tmp_path, ("test_present.py",), policy=policy)

    assert "tests/python/test_missing.py" in str(exc.value)
    assert "tests/python/ext/test_local_only.py" in str(exc.value)
    assert "copy tests/python/ext/" in str(exc.value)
    assert "Remove the copied tests/python/ext/ directory after the gate run." in str(
        exc.value
    )


def test_dispatch_watcher_gate_refuses_an_untracked_stray_module(
    tmp_path: Path,
) -> None:
    test_dir = tmp_path / "tests" / "python"
    test_dir.mkdir(parents=True)
    (test_dir / "test_present.py").write_text("# present\n", encoding="utf-8")
    policy = _prepare_dispatch_watcher_gate_fixture(tmp_path)
    stray = test_dir / "test_stray.py"
    stray.write_text("# generated under the test root\n", encoding="utf-8")

    with pytest.raises(
        dispatch_gate.GatePolicyError, match="module universe mismatch"
    ) as exc:
        dispatch_gate.select_gate(tmp_path, ("test_present.py",), policy=policy)

    assert "tests/python/test_stray.py" in str(exc.value)


def test_dispatch_watcher_gate_refuses_when_git_index_cannot_be_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    test_dir = tmp_path / "tests" / "python"
    test_dir.mkdir(parents=True)
    (test_dir / "test_present.py").write_text("# present\n", encoding="utf-8")
    policy = _prepare_dispatch_watcher_gate_fixture(tmp_path)
    empty_path = tmp_path / "empty-path"
    empty_path.mkdir()
    assert dispatch_gate.select_gate(
        tmp_path, ("test_present.py",), policy=policy
    ).fallback_reason is None
    monkeypatch.setenv("PATH", str(empty_path))

    with pytest.raises(
        dispatch_gate.GatePolicyError,
        match="cannot read tracked test modules from Git index",
    ):
        dispatch_gate.select_gate(tmp_path, ("test_present.py",), policy=policy)


def test_dispatch_watcher_gate_refuses_when_git_index_read_times_out(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    test_dir = tmp_path / "tests" / "python"
    test_dir.mkdir(parents=True)
    (test_dir / "test_present.py").write_text("# present\n", encoding="utf-8")
    policy = _prepare_dispatch_watcher_gate_fixture(tmp_path)

    def timed_out(*_args: object, **kwargs: object) -> bytes:
        assert kwargs.get("timeout") == 30
        raise subprocess.TimeoutExpired(cmd=["git", "ls-files"], timeout=30)

    monkeypatch.setattr(dispatch_gate.subprocess, "check_output", timed_out)

    with pytest.raises(
        dispatch_gate.GatePolicyError,
        match="cannot read tracked test modules from Git index",
    ) as raised:
        dispatch_gate.select_gate(tmp_path, None, policy=policy)

    assert "timed out after 30 seconds" in str(raised.value)
    assert isinstance(raised.value.__cause__, subprocess.TimeoutExpired)


def test_dispatch_watcher_gate_combines_patterns_and_falls_back_on_unknown_input(
    tmp_path: Path,
) -> None:
    test_dir = tmp_path / "tests" / "python"
    test_dir.mkdir(parents=True)
    for name in (
        "test_dispatch_core.py",
        "test_review_job.py",
        "test_unrelated.py",
    ):
        (test_dir / name).write_text("# selector fixture\n", encoding="utf-8")
    policy = _prepare_dispatch_watcher_gate_fixture(tmp_path)
    all_modules = {
        path.relative_to(tmp_path).as_posix()
        for path in test_dir.rglob(policy["module_pattern"])
    }
    review_patterns = ("test_review_job.py", "test_unknown_review_group.py")

    separate_results = [
        dispatch_gate.select_gate(tmp_path, (pattern,), policy=policy)
        for pattern in review_patterns
    ]
    kept_result = next(
        selection
        for selection in separate_results
        if selection.fallback_reason is None
    )

    combined_result = dispatch_gate.select_gate(
        tmp_path,
        review_patterns,
        policy=policy,
    )

    assert set(kept_result.modules) < all_modules
    assert combined_result.fallback_reason == "ambiguous"
    assert set(combined_result.modules) == all_modules


@pytest.mark.parametrize(
    ("review_patterns", "reason"),
    (
        (None, "missing"),
        ((), "empty"),
        (("test_missing_review_group_*.py",), "ambiguous"),
        (("nested/test_review_job.py",), "ambiguous"),
        (("test_review_job.py", "UNMAPPED"), "unmapped"),
    ),
)
def test_dispatch_watcher_gate_fallback_is_full_suite_for_policy_targets(
    tmp_path: Path,
    review_patterns: tuple[str, ...] | None,
    reason: str,
) -> None:
    test_dir = tmp_path / "tests" / "python"
    test_dir.mkdir(parents=True)
    for name in ("test_dispatch_core.py", "test_review_job.py", "test_unrelated.py"):
        (test_dir / name).write_text("# selector fixture\n", encoding="utf-8")
    policy = _prepare_dispatch_watcher_gate_fixture(tmp_path)

    selection = dispatch_gate.select_gate(tmp_path, review_patterns, policy=policy)

    expected_modules = {
        path.relative_to(tmp_path).as_posix()
        for path in test_dir.rglob(policy["module_pattern"])
    }
    assert set(selection.modules) == expected_modules
    assert selection.revisions == tuple(policy["revision_targets"])
    assert selection.fallback_reason == reason


def test_dispatch_watcher_gate_cli_reports_selection_without_completeness_claim(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    test_dir = tmp_path / "tests" / "python"
    test_dir.mkdir(parents=True)
    (test_dir / "test_review.py").write_text("# selected\n", encoding="utf-8")
    _prepare_dispatch_watcher_gate_fixture(tmp_path)

    assert dispatch_gate.main(
        ["--repo-root", str(tmp_path), "--review-pattern", "test_review.py"]
    ) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["modules"] == ["tests/python/test_review.py"]
    assert "patterns_complete" not in result


def test_dispatch_watcher_gate_documentation_uses_one_policy_driven_invocation(
    tmp_path: Path,
) -> None:
    policy_text = (REPO_ROOT / "AGENTS.md").read_text(encoding="utf-8")
    gate_text = policy_text.split("## Dispatch/watcher family A/B deploy gate", 1)[1]
    gate_text = gate_text.split("## Git workflow", 1)[0]

    assert (
        "python3 scripts/dispatch_watcher_gate.py "
        "--review-pattern '<pattern-1>' "
        "--review-pattern '<pattern-2>'"
    ) in gate_text
    assert "does not attest that" in gate_text
    assert "review patterns cover every affected module" in gate_text
    assert "patterns_complete" not in gate_text
    assert "once per pattern" not in gate_text
    assert "--review-pattern UNMAPPED" in gate_text
    normalized_gate_text = " ".join(gate_text.split())
    assert (
        "Prefer copying `tests/python/ext/` from the main checkout"
        in normalized_gate_text
    )
    assert "fallback_reason` widens to the full universe" in normalized_gate_text
    assert "Remote CI does not ship gitignored files" in normalized_gate_text
    assert "After the gate run, remove the copied directory" in normalized_gate_text
    assert "throwaway gate worktree" in normalized_gate_text
    normalized_docstring = " ".join((dispatch_gate.__doc__ or "").split())
    assert "every declared file to be present" in normalized_docstring
    assert "complete or current" in normalized_docstring

    selections = (
        (
            {
                "revisions": ["deployed-baseline", "candidate"],
                "modules": ["tests/python/test_dispatch_core.py"],
            },
            ["--pair", "--base", "deployed-sha"],
        ),
        (
            {
                "revisions": ["candidate"],
                "modules": ["tests/python/test_dispatch_core.py"],
            },
            [],
        ),
    )
    for index, (selection, revision_args) in enumerate(selections):
        result, args_path = _run_dispatch_watcher_gate_driver(
            tmp_path, selection, str(index)
        )

        assert result.returncode == 0, result.stderr
        args = json.loads(args_path.read_text(encoding="utf-8"))
        assert args == [
            "--kind",
            "gate",
            *revision_args,
            "--cmd-id",
            "goalflight-full-gate",
            "--",
            "python3",
            "-m",
            "pytest",
            "-q",
            *selection["modules"],
        ]


def test_dispatch_watcher_gate_fails_closed_when_repository_has_no_rule(
    tmp_path: Path,
) -> None:
    test_dir = tmp_path / "tests" / "python"
    test_dir.mkdir(parents=True)
    (test_dir / "test_dispatch_core.py").write_text("# baseline fixture\n", encoding="utf-8")

    with pytest.raises(dispatch_gate.GatePolicyError, match="policy is missing"):
        dispatch_gate.select_gate(tmp_path, ("test_dispatch*",))


def test_string_attention_blocked_marker_is_normalized_on_status_write(
    tmp_path: Path,
) -> None:
    status_path = tmp_path / "string-attention.status.json"
    liveness.write_status(
        status_path,
        {
            "dispatch_id": "string-attention",
            "state": "blocked",
            "worker_alive": True,
            "attention_marker": "BLOCKED",
        },
    )
    persisted = json.loads(status_path.read_text(encoding="utf-8"))
    assert persisted["state"] == "watcher_stopped", persisted
    assert persisted["terminal_pending_state"] == "blocked", persisted
    assert persisted["attention_marker"] == {"kind": "BLOCKED"}, persisted
    assert persisted.get("terminal_marker") is None, persisted


def test_live_nonblocked_markers_keep_their_terminal_kind(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    outcomes = []
    publications = []
    for kind in ("FAILED", "USER-NEED", "USER-CONFIRM"):
        dispatch_id = f"live-{kind.lower()}"
        result = ledger.commit_terminal_authority(
            {"dispatch_id": dispatch_id, "project_root": str(project)},
            state="blocked",
            reason={"marker_kind": kind, "text": f"{dispatch_id} — outcome"},
            worker_still_alive=True,
        )
        outcomes.append((kind, result.committed, result.value.event_type if result.value else None))

        payload = {
            "dispatch_id": dispatch_id,
            "state": "blocked",
            "worker_alive": True,
            "terminal_marker": {"kind": kind, "text": "worker outcome"},
        }
        status_path = tmp_path / f"{dispatch_id}.status.json"
        liveness.write_status(status_path, payload)
        persisted = json.loads(status_path.read_text(encoding="utf-8"))
        publications.append(
            (
                kind,
                persisted.get("state") == "blocked"
                and persisted.get("terminal_marker", {}).get("kind") == kind
                and "attention_marker" not in persisted,
            )
        )

    expected_events = {
        "FAILED": "blocked",
        "USER-NEED": "user_need",
        "USER-CONFIRM": "user_confirm",
    }
    assert all(
        committed and event_type == expected_events[kind]
        for kind, committed, event_type in outcomes
    ), outcomes
    assert all(kept for _kind, kept in publications), publications


def test_live_ledger_guard_uses_effective_terminal_outcome(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    cases = (
        (
            "terminal-state-override",
            {"dispatch_id": "terminal-state-override", "project_root": str(project)},
            "watcher_stopped",
            "explicit terminal override",
            "blocked",
        ),
        (
            "retained-complete-new-blocked-reason",
            {
                "dispatch_id": "retained-complete-new-blocked-reason",
                "project_root": str(project),
                "terminal_marker": {"kind": "COMPLETE"},
            },
            "watcher_stopped",
            "marker:BLOCKED:new verdict",
            "blocked",
        ),
        (
            "retained-complete-new-blocked-state",
            {
                "dispatch_id": "retained-complete-new-blocked-state",
                "project_root": str(project),
                "terminal_marker": {"kind": "COMPLETE"},
            },
            "blocked",
            "new blocked verdict",
            None,
        ),
    )
    results = []
    for dispatch_id, record, state, reason, terminal_state in cases:
        result = ledger.commit_terminal_authority(
            record,
            state=state,
            reason=reason,
            terminal_state=terminal_state,
            worker_still_alive=True,
        )
        results.append((dispatch_id, result.committed, result.disposition))
    assert all(
        not committed and disposition is journal.WriteDisposition.RETRYABLE
        for _dispatch_id, committed, disposition in results
    ), results


@pytest.mark.parametrize(
    ("marker_field", "marker"),
    (
        ("last_marker", {"kind": "COMPLETE"}),
        ("terminal_marker", {"kind": "FAILED"}),
        ("attention_marker", {"kind": "USER-NEED"}),
    ),
)
def test_live_terminal_override_ignores_retained_markers(
    tmp_path: Path, marker_field: str, marker: dict[str, str]
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    record = {
        "dispatch_id": f"terminal-override-retained-{marker_field}",
        "project_root": str(project),
        marker_field: marker,
    }
    result = ledger.commit_terminal_authority(
        record,
        state="watcher_stopped",
        reason="explicit terminal override",
        terminal_state="blocked",
        worker_still_alive=True,
    )
    assert not result.committed and result.disposition is journal.WriteDisposition.RETRYABLE, (
        marker_field,
        result,
    )


@pytest.mark.parametrize("worker_still_alive", (None, True))
@pytest.mark.parametrize(
    "state", sorted(ledger.goalflight_dispatch_states.LAUNCHER_BLOCKED_STATES)
)
def test_launcher_blocked_outcomes_commit_without_worker_wait(
    tmp_path: Path, state: str, worker_still_alive: bool | None
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    result = ledger.commit_terminal_authority(
        {
            "dispatch_id": f"launcher-{state}-{worker_still_alive}",
            "project_root": str(project),
        },
        state=state,
        reason="launcher refused before worker start",
        worker_still_alive=worker_still_alive,
    )
    assert (
        result.committed
        and result.value is not None
        and result.value.terminal_state == state
        and result.value.observation.get("state") == state
    ), (state, worker_still_alive, result)


def test_dispatch_projection_lookup_is_exact_id_not_history_scan(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    dispatch_id = "exact-projection"
    record_path = tmp_path / f"{dispatch_id}.json"
    record_path.write_text(
        json.dumps({"dispatch_id": dispatch_id, "project_root": str(tmp_path)}),
        encoding="utf-8",
    )
    monkeypatch.setattr(ledger, "record_path", lambda _dispatch_id, create=False: record_path)
    monkeypatch.setattr(
        ledger,
        "read_records",
        lambda: (_ for _ in ()).throw(AssertionError("history scan reached")),
    )

    record, error = messages._dispatch_record(dispatch_id)
    assert error is None
    assert record is not None and record["dispatch_id"] == dispatch_id

    record_path.write_text(
        json.dumps({"dispatch_id": "foreign-projection"}),
        encoding="utf-8",
    )
    record, error = messages._dispatch_record(dispatch_id)
    assert record is None
    assert error == "dispatch record is malformed or bound to a different dispatch id"


def test_cursor_position_command_round_trips_grouped_and_delimiter_bearing_streams() -> None:
    command = messages._cursor_advance_command(
        project_root=Path("/tmp/project with spaces"),
        controller_label="controller:primary",
        lease_nonce="lease-token",
        cursor_version=7,
        positions={
            "task-store:goal-flight-alpha": 13,
            "stream=with=equals": 17,
        },
        stream_snapshots={
            "task-store:goal-flight-alpha": "a" * 64,
            "stream=with=equals": "b" * 64,
        },
    )
    assert command is not None
    argv = shlex.split(command)
    snapshot_index = argv.index("--stream-snapshot")
    position_index = argv.index("--position")
    assert argv[snapshot_index + 1 : position_index] == [
        f"stream=with=equals={'b' * 64}",
        f"task-store:goal-flight-alpha={'a' * 64}",
    ]
    assert argv[position_index:] == [
        "--position",
        "stream=with=equals=17",
        "task-store:goal-flight-alpha=13",
    ]
    assert messages._parse_cursor_stream_snapshots(
        [argv[snapshot_index + 1 : position_index]]
    ) == {
        "stream=with=equals": "b" * 64,
        "task-store:goal-flight-alpha": "a" * 64,
    }
    assert messages._parse_cursor_positions([argv[position_index + 1 :]]) == {
        "stream=with=equals": 17,
        "task-store:goal-flight-alpha": 13,
    }

    assert messages._parse_cursor_positions(
        [["task-store:goal-flight-alpha=11"], ["task-store:goal-flight-alpha=13"]]
    ) == {"task-store:goal-flight-alpha": 13}
