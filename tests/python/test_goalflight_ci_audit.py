from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import goalflight_ci_audit as audit  # noqa: E402


NOW = datetime(2026, 10, 18, 12, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _hermetic_state_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GOALFLIGHT_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("GOALFLIGHT_TASK_STORE_DIR", str(tmp_path / "task-store"))


def _receipt(command_id: str, cpu: float, wall: float, **extra: object) -> dict[str, object]:
    return {
        "schema": "goalflight.remote-ci.receipt.v1",
        "hostname": "studio-a",
        "status": "pass",
        "command_id": command_id,
        "process_cpu_seconds": cpu,
        "wall_seconds": wall,
        **extra,
    }


def _write_result(
    directory: Path,
    request_id: str,
    command_id: str,
    cpu: float,
    wall: float,
    *,
    route: str = "remote",
    arms: bool = False,
    local: bool = False,
) -> None:
    receipt = _receipt(command_id, cpu, wall, route=route, project_id="sample/project")
    if arms:
        receipt = {
            "candidate": _receipt(command_id, cpu, wall, route=route, project_id="sample/project"),
            "base": _receipt(command_id, 3, 4, route=route, project_id="sample/project"),
        }
    suffix = ".local.json" if local else ".json"
    (directory / f"{request_id}{suffix}").write_text(json.dumps({
        "schema": "goalflight.remote-ci.result.v1",
        "request_id": request_id,
        "project_id": "sample/project",
        "status": "LOCAL_FALLBACK" if route == "local-fallback" else "pass",
        "finished_at": NOW.isoformat(),
        "receipt": receipt,
    }), encoding="utf-8")


def _fixture_spool(tmp_path: Path) -> tuple[Path, Path]:
    project = tmp_path / "project"
    results = tmp_path / "spool" / "results"
    requests = tmp_path / "spool" / "requests"
    project.mkdir()
    results.mkdir(parents=True)
    requests.mkdir(parents=True)
    (project / ".goal-flight").mkdir()
    (project / ".goal-flight" / "remote-ci.json").write_text(
        json.dumps({"repo": "sample/project"}), encoding="utf-8"
    )
    _write_result(results, "r1", "full-gate", 4, 8, arms=True)
    _write_result(
        requests, "r2", "full-gate", 6, 12,
        route="local-fallback", local=True,
    )
    _write_result(results, "r3", "integration", 3, 4)
    _write_result(results, "r4", "misc-one", 5, 7)
    _write_result(results, "r5", "misc-two", 2, 60)
    _write_result(results, "r6", "misc-three", 1, 90)
    return project, results


def _write_ledger_record(directory: Path, record: dict[str, object]) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    dispatch_id = str(record["dispatch_id"])
    (directory / f"{dispatch_id}.json").write_text(json.dumps(record), encoding="utf-8")


def _empty_project_spool(tmp_path: Path) -> tuple[Path, Path, Path]:
    project = tmp_path / "project"
    results = tmp_path / "spool" / "results"
    requests = tmp_path / "spool" / "requests"
    results.mkdir(parents=True)
    requests.mkdir()
    (project / ".goal-flight").mkdir(parents=True)
    (project / ".goal-flight" / "remote-ci.json").write_text(
        json.dumps({"repo": "sample/project"}), encoding="utf-8",
    )
    return project, results, tmp_path / "state" / "runs.d"


def test_ci_audit_aggregates_receipts_and_ranks_slowest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, results = _fixture_spool(tmp_path)
    monkeypatch.setenv("GOALFLIGHT_REMOTE_CI_SPOOL_DIR", str(results.parent))

    report = audit.analyze(project, now=NOW)

    full = next(row for row in report["commands"] if row["command_id"] == "full-gate")
    assert full == {
        "command_id": "full-gate",
        "runs": 2,
        "cpu_seconds": 13.0,
        "wall_seconds": 24.0,
        "wall_cpu_ratio": 24.0 / 13.0,
        "local_fallbacks": 1,
        "median_wall_seconds": 12.0,
        "p95_wall_seconds": 12.0,
        "controller_labels": [],
        "dispatch_ids": [],
    }
    assert [row["command_id"] for row in report["slowest"]] == [
        "full-gate", "full-gate", "misc-one", "integration", "misc-two",
    ]
    assert report["repeated_runs"] == {"available": False, "groups": []}
    assert any("repeated identical runs unavailable" in note for note in report["notes"])
    assert full["local_fallbacks"] == 1


def test_ci_audit_attributes_and_detects_repeated_runs(tmp_path, monkeypatch):
    project, results = _fixture_spool(tmp_path)
    for request_id, cpu, wall in (("repeat-1", 8, 10), ("repeat-2", 9, 11)):
        receipt = _receipt(
            "full-gate", cpu, wall, route="remote", project_id="sample/project",
            controller_label="beta", dispatch_id="dispatch-7", command_sha="abc123",
        )
        (results / f"{request_id}.json").write_text(json.dumps({
            "schema": "goalflight.remote-ci.result.v1",
            "request_id": request_id,
            "project_id": "sample/project",
            "status": "pass",
            "finished_at": NOW.isoformat(),
            "receipt": receipt,
        }), encoding="utf-8")
    monkeypatch.setenv("GOALFLIGHT_REMOTE_CI_SPOOL_DIR", str(results.parent))

    report = audit.analyze(project, now=NOW, controller_label="beta")

    assert report["attribution"] == {"controller_label": "available", "dispatch_id": "available"}
    full = next(row for row in report["commands"] if row["command_id"] == "full-gate")
    assert full["controller_labels"] == ["beta"]
    assert full["dispatch_ids"] == ["dispatch-7"]
    assert report["repeated_runs"] == {
        "available": True,
        "groups": [{
            "dispatch_id": "dispatch-7", "command_sha": "abc123",
            "command_ids": ["full-gate"], "runs": 2,
        }],
    }
    assert report["unattributed"]["runs"] == 6


def test_ci_audit_filters_other_controllers_and_reports_unattributed(
    tmp_path, monkeypatch,
) -> None:
    project, results = _fixture_spool(tmp_path)
    for request_id, label, cpu, wall in (
        ("owned", "beta", 2, 4),
        ("other", "gamma", 90, 100),
    ):
        receipt = _receipt(
            f"{label}-command", cpu, wall, project_id="sample/project",
            controller_label=label,
        )
        (results / f"{request_id}.json").write_text(json.dumps({
            "request_id": request_id, "project_id": "sample/project",
            "finished_at": NOW.isoformat(), "receipt": receipt,
        }), encoding="utf-8")
    monkeypatch.setenv("GOALFLIGHT_REMOTE_CI_SPOOL_DIR", str(results.parent))

    report = audit.analyze(project, now=NOW, controller_label="beta")

    assert [row["command_id"] for row in report["commands"]] == ["beta-command"]
    assert report["commands"][0]["cpu_seconds"] == 2
    assert report["unattributed"] == {"runs": 6, "cpu_seconds": 24.0}
    assert "unattributed: 6 runs, cpu 24.0s" in audit._text(report)


def test_ci_audit_reports_empty_controller_filter_without_attribution_warnings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, results = _fixture_spool(tmp_path)
    for path in (*results.glob("*.json"), *(results.parent / "requests").glob("*.json")):
        path.unlink()
    for request_id in ("run-one", "run-two"):
        receipt = _receipt(
            "suite", 5, 8, project_id="sample/project",
            controller_label="another-controller", dispatch_id=f"dispatch-{request_id}",
            command_sha="sha-123",
        )
        (results / f"{request_id}.json").write_text(json.dumps({
            "request_id": request_id, "project_id": "sample/project",
            "finished_at": NOW.isoformat(), "receipt": receipt,
        }), encoding="utf-8")
    monkeypatch.setenv("GOALFLIGHT_REMOTE_CI_SPOOL_DIR", str(results.parent))

    report = audit.analyze(project, now=NOW, controller_label="pm2-train")
    output = audit._text(report)

    assert report["commands"] == []
    assert report["unattributed"] == {"runs": 0, "cpu_seconds": 0}
    assert "no remote runs attributed to pm2-train in last 24h" in output
    assert "attribution unavailable" not in output
    assert "repeated identical runs unavailable" not in output


def test_ci_audit_resolves_dispatch_labels_per_receipt_in_project_spool(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = tmp_path / "pm2"
    spool = project / "remote-ci" / "spool"
    results = spool / "results"
    requests = spool / "requests"
    results.mkdir(parents=True)
    requests.mkdir()
    config = project / ".goal-flight"
    config.mkdir()
    (config / "remote-ci.json").write_text(json.dumps({
        "repo": "sample/pm2",
        "paths": {"queue_dir": str(requests), "result_dir": str(results)},
    }), encoding="utf-8")
    monkeypatch.delenv("GOALFLIGHT_REMOTE_CI_SPOOL_DIR", raising=False)
    monkeypatch.setenv("GOALFLIGHT_STATE_DIR", str(tmp_path / "state"))
    ledger_dir = tmp_path / "state" / "runs.d"
    _write_ledger_record(ledger_dir, {
        "dispatch_id": "dispatch-new", "controller_label": "pm2-train",
        "project_root": str(project),
    })
    _write_ledger_record(ledger_dir, {
        "dispatch_id": "dispatch-foreign", "controller_label": "pm2-train",
        "project_root": str(tmp_path / "another-project"),
    })
    for name, receipt in (
        ("old", _receipt("suite", 10, 12, project_id="sample/pm2")),
        ("new", _receipt(
            "suite", 20, 24, project_id="sample/pm2",
            dispatch_id="dispatch-new", command_sha="sha-new",
        )),
        ("unknown", _receipt(
            "suite", 30, 36, project_id="sample/pm2",
            dispatch_id="dispatch-missing",
        )),
        ("foreign", _receipt(
            "suite", 15, 18, project_id="sample/pm2",
            dispatch_id="dispatch-foreign",
        )),
    ):
        (results / f"{name}.json").write_text(json.dumps({
            "request_id": name, "project_id": "sample/pm2",
            "finished_at": NOW.isoformat(), "receipt": receipt,
        }), encoding="utf-8")

    report = audit.analyze(project, now=NOW, controller_label="pm2-train")

    assert report["attribution"] == {
        "controller_label": "available", "dispatch_id": "available",
    }
    assert report["commands"] == [{
        "command_id": "suite", "runs": 1, "cpu_seconds": 20.0,
        "wall_seconds": 24.0, "wall_cpu_ratio": 1.2,
        "local_fallbacks": 0, "median_wall_seconds": 24.0,
        "p95_wall_seconds": 24.0, "controller_labels": ["pm2-train"],
        "dispatch_ids": ["dispatch-new"],
    }]
    assert report["unattributed"] == {"runs": 3, "cpu_seconds": 55.0}
    assert any("foreign-project dispatch ledger row" in note for note in report["notes"])


def test_ci_audit_rejects_ambiguous_dispatch_ledger_labels(tmp_path, monkeypatch):
    project, results, ledger_dir = _empty_project_spool(tmp_path)
    receipt = _receipt(
        "suite", 4, 8, project_id="sample/project", dispatch_id="dispatch-shared",
        controller_label="alpha",
    )
    (results / "shared.json").write_text(json.dumps({
        "request_id": "shared", "project_id": "sample/project",
        "finished_at": NOW.isoformat(), "receipt": receipt,
    }), encoding="utf-8")
    record = {
        "dispatch_id": "dispatch-shared", "controller_label": "alpha",
        "project_root": str(project),
    }
    _write_ledger_record(ledger_dir, record)
    archive = tmp_path / "state" / "runs.d.archive" / "2026-10"
    _write_ledger_record(archive, {**record, "controller_label": "beta"})
    monkeypatch.setenv("GOALFLIGHT_REMOTE_CI_SPOOL_DIR", str(results.parent))
    monkeypatch.setenv("GOALFLIGHT_STATE_DIR", str(tmp_path / "state"))

    report = audit.analyze(project, now=NOW, controller_label="alpha")

    assert report["commands"] == []
    assert report["unattributed"] == {"runs": 1, "cpu_seconds": 4.0}
    assert any("ambiguous dispatch ledger labels" in note for note in report["notes"])


def test_ci_audit_rejects_receipt_label_that_disagrees_with_project_ledger(
    tmp_path, monkeypatch,
):
    project, results, ledger_dir = _empty_project_spool(tmp_path)
    receipt = _receipt(
        "suite", 6, 9, project_id="sample/project", dispatch_id="dispatch-conflict",
        controller_label="beta",
    )
    (results / "conflict.json").write_text(json.dumps({
        "request_id": "conflict", "project_id": "sample/project",
        "finished_at": NOW.isoformat(), "receipt": receipt,
    }), encoding="utf-8")
    _write_ledger_record(ledger_dir, {
        "dispatch_id": "dispatch-conflict", "controller_label": "alpha",
        "project_root": str(project),
    })
    monkeypatch.setenv("GOALFLIGHT_REMOTE_CI_SPOOL_DIR", str(results.parent))
    monkeypatch.setenv("GOALFLIGHT_STATE_DIR", str(tmp_path / "state"))

    report = audit.analyze(project, now=NOW, controller_label="beta")

    assert report["commands"] == []
    assert report["unattributed"] == {"runs": 1, "cpu_seconds": 6.0}
    assert any("receipt label disagrees with dispatch ledger" in note for note in report["notes"])


@pytest.mark.parametrize("incomplete", ["unreadable", "timeout"])
def test_ci_audit_does_not_report_no_runs_when_scan_is_incomplete(
    tmp_path, monkeypatch, incomplete,
):
    project, results, _ledger_dir = _empty_project_spool(tmp_path)
    if incomplete == "unreadable":
        monkeypatch.setenv("GOALFLIGHT_REMOTE_CI_SPOOL_DIR", str(tmp_path / "missing-spool"))
    else:
        monkeypatch.setenv("GOALFLIGHT_REMOTE_CI_SPOOL_DIR", str(results.parent))
        monkeypatch.setattr(audit, "SCAN_TIME_BUDGET_SECONDS", 0)

    report = audit.analyze(project, now=NOW, controller_label="pm2-train")
    output = audit._text(report)

    assert "scan incomplete:" in output
    assert "no remote runs attributed" not in output
    assert "attribution unavailable" not in output
    assert "repeated identical runs unavailable" not in output


def test_ci_audit_text_ranks_commands_by_cpu_and_formats_pure_wait_as_infinity(
    tmp_path, monkeypatch,
) -> None:
    project, results = _fixture_spool(tmp_path)
    monkeypatch.setenv("GOALFLIGHT_REMOTE_CI_SPOOL_DIR", str(results.parent))
    report = audit.analyze(project, now=NOW)
    rows = [row["command_id"] for row in report["commands"]]
    assert rows.index("misc-one") < rows.index("integration")
    text = audit._text(report)
    assert text.index("misc-one:") < text.index("integration:")

    wait_report = {
        "project_root": str(project), "window_seconds": 3600,
        "commands": [{
            "command_id": "pure-wait", "runs": 1, "cpu_seconds": 0.0,
            "wall_seconds": 600.0, "wall_cpu_ratio": None,
            "local_fallbacks": 0, "median_wall_seconds": 600.0,
            "p95_wall_seconds": 600.0, "controller_labels": [], "dispatch_ids": [],
        }],
        "attribution": {"controller_label": "available", "dispatch_id": "available"},
        "repeated_runs": {"groups": []}, "slowest": [], "notes": [],
    }
    assert "pure-wait:" in audit._text(wait_report)
    assert "wall/cpu=inf" in audit._text(wait_report)


def test_ci_audit_does_not_drop_future_timestamped_receipts(tmp_path, monkeypatch):
    project, results = _fixture_spool(tmp_path)
    future = _receipt("future-command", 2, 3, project_id="sample/project")
    (results / "future.json").write_text(json.dumps({
        "request_id": "future", "project_id": "sample/project",
        "finished_at": "2026-10-19T00:00:00+00:00", "receipt": future,
    }), encoding="utf-8")
    monkeypatch.setenv("GOALFLIGHT_REMOTE_CI_SPOOL_DIR", str(results.parent))

    report = audit.analyze(project, now=NOW)

    assert "future-command" in {row["command_id"] for row in report["commands"]}


def test_ci_audit_surfaces_repeated_codex_exec_wall_without_secrets(
    tmp_path, monkeypatch,
) -> None:
    assert audit._command_shape(
        "/bin/zsh -lc 'state_dir=$(mktemp -d \"$TMPDIR/audit.XXXXXX\") && python3 -m pytest'"
    ) == "python3 -m pytest"
    project, results = _fixture_spool(tmp_path)
    dispatch_dir = tmp_path / "state" / "dispatch"
    dispatch_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("GOALFLIGHT_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(
        audit.goalflight_dispatch_paths, "dispatch_base_dir", lambda: dispatch_dir,
    )
    lines = []
    for _ in range(4):
        lines.extend([
            "exec",
            '/bin/zsh -lc "TOKEN=private python3 --api-token SECRET_VALUE -m pytest tests/python/test_remote_ci.py --password hidden" in /Users/simonrowland/Repos/battery-to...',
            "hook: PostToolUse",
            "hook: PostToolUse Completed",
            "hook: PostToolUse Completed",
            " succeeded in 600000ms:",
        ])
    lines.extend([
        "exec",
        '/bin/zsh -lc "python3 -m compileall scripts" in /Users/simonrowland/Repos/battery-to...',
        "hook: PostToolUse",
        "hook: PostToolUse Completed",
        " exited -9 in 1250ms:",
        "exec",
        '/bin/zsh -lc "python3 tools/unpaired-call.py" in /Users/simonrowland/Repos/battery-to...',
    ])
    for command in (
        "curl --auth AUTH_VALUE",
        "mysql -p MYSQL_PASSWORD",
        "curl https://user:pass@host/path",
        "python3 -c 'BODY_SECRET'",
        "curl --token=EQUAL_SECRET",
        "mysql -pCOMPACT_SECRET",
        "python3 - <<PY",
        "FOO=$(echo s3cret)",
        "TOKEN=$(curl --auth s3cret ...)",
        "curl -u user:tok",
        'curl --header "Authorization: Bearer x"',
        '--header "Authorization: Bearer x"',
    ):
        lines.extend(["exec", f'/bin/zsh -lc "{command}" in /project', " succeeded in 1s:"])
    (dispatch_dir / "dispatch-repeat.tail").write_text("\n".join(lines), encoding="utf-8")
    ledger_dir = tmp_path / "ledger"
    _write_ledger_record(ledger_dir, {
        "dispatch_id": "dispatch-repeat", "controller_label": "beta",
        "project_root": str(project.resolve()), "agent": "codex", "engine": "codex",
    })
    monkeypatch.setattr(audit.goalflight_ledger, "runs_dir", lambda **_kwargs: ledger_dir)
    monkeypatch.setenv("GOALFLIGHT_REMOTE_CI_SPOOL_DIR", str(results.parent))

    report = audit.analyze(project, now=NOW, controller_label="beta")
    text = audit._text(report)

    assert report["in_worker"]["commands"][0] == {
        "command": "python3 -m pytest", "calls": 4,
        "wall_seconds": 2400.0, "max_wall_seconds": 600.0,
        "repeated_dispatch_ids": ["dispatch-repeat"],
    }
    assert "repeated in 1 jobs (e.g. dispatch-repeat)" in text
    assert all(len(line) <= 160 for line in text.splitlines())
    assert "wall=2400.0s" in text and "max-wall=600.0s" in text
    assert any(row["command"] == "python3 -m compileall" for row in report["in_worker"]["commands"])
    assert report["in_worker"]["unpaired_exec_blocks"] == 1
    assert "unpaired: 1 exec blocks" in text
    assert text.startswith("CI audit: beta;")
    assert "hook: PostToolUse" not in text
    assert "SECRET_VALUE" not in text and "private" not in text and "hidden" not in text
    for secret in (
        "AUTH_VALUE", "MYSQL_PASSWORD", "user:pass@host", "BODY_SECRET",
        "EQUAL_SECRET", "COMPACT_SECRET", "s3cret", "user:tok",
        "Authorization", "Bearer x",
    ):
        assert secret not in text


@pytest.mark.parametrize(("tail_id", "exec_count"), [
    ("example-resume-5", 90),
    ("example-resume-6", 45),
    ("example-dispatch-2", 33),
])
def test_known_codex_tail_fixture_copies_pair_every_exec(tail_id, exec_count):
    # Compact synthetic tail records retain the exec/result counts and exercise
    # multiline commands, hook lines, and quoted output.
    lines = []
    for _ in range(exec_count):
        lines.extend([
            "exec",
            '/bin/zsh -lc "python3 -m pytest in /project',
            " tests/python/test_goalflight_ci_audit.py in /project",
            'output: "succeeded in 999ms:"',
            "hook: PostToolUse Completed",
            " succeeded in 1s:",
        ])
    calls, unpaired, stop = audit._tail_execs("\n".join(lines).encode())

    assert len(calls) == exec_count, tail_id
    assert unpaired == 0, tail_id
    assert stop is None, tail_id
    assert set(command for command, _seconds in calls) == {"python3 -m pytest"}


def test_multiline_codex_header_pairs_from_last_continuation_line():
    # Synthetic multiline command block shape from example-resume-7.
    payload = b'''exec
/bin/zsh -lc "python3 - <<'PY'
import gzip, re
p='/tmp/example/remote.log.gz'
s=gzip.open(p,'rt',errors='replace').read()
blocks=re.split(r'(?m)(?="'^_{5,}.*_{5,} ?$")',s)
for b in blocks:
    if not re.search(r'(?m)"'^_{5,}.*_{5,} ?$")',b):
        continue
    head=next((x.strip() for x in b.splitlines() if x.strip().startswith('_')), '')
    errs=[]
    for line in b.splitlines():
        t=line.strip()
        if t.startswith('E '):
            t=t[2:].strip()
            if re.search(r'(Error:|AssertionError|expected|assert |Failed:|not equal|did not|got )',t):
                errs.append(t)
    print(head[:140])
    for x in errs[-2:]: print('  '+x[:200])
PY" in /workspace/example-project/worktrees/seat-13
 succeeded in 0ms:'''

    calls, unpaired, stop = audit._tail_execs(payload)

    assert calls == [("python3 <heredoc>", 0.0)]
    assert unpaired == 0
    assert stop is None


def test_ci_audit_command_shape_skips_setup_segments():
    command = (
        '/bin/zsh -lc "state_dir=$(mktemp -d \"$TMPDIR/audit.XXXXXX\") && '
        'cd /project && GOALFLIGHT_STATE_DIR=$state_dir python3 tools/check_ci.py -q"'
    )

    assert audit._command_shape(command) == "python3 tools/check_ci.py"


def test_ci_audit_command_shapes_keep_only_allowlisted_arguments():
    assert audit._command_shape('env MODE=ci timeout 30s nice -n 5 python3 -m pytest') == "python3 -m pytest"
    assert audit._command_shape('"$GOALFLIGHT_REMOTE_RUN" --kind gate --cmd-id secret') == "goalflight-remote-run"
    assert audit._command_shape('"$GOALFLIGHT_REMOTE_RUN" --kind gate -- python3 -m pytest') == "goalflight-remote-run -m pytest"
    assert audit._command_shape("python -m unittest") == "python -m unittest"
    assert audit._command_shape("python3.12 -m pytest") == "python3.12 -m pytest"
    assert audit._command_shape("git status --short") == "git status"
    assert audit._command_shape("git commit -m SYNTHETIC_SECRET") == "git commit"
    assert audit._command_shape("git tag -m SYNTHETIC_SECRET") == "git tag"
    assert audit._command_shape("curl SECRET_VALUE") == "curl"
    assert audit._command_shape("python3 - <<PY") == "python3 <heredoc>"
    assert audit._command_shape("python3 -c 'unterminated") == "python3"
    assert audit._command_shape("/bin/zsh -lc \"python3 - <<'PY'") == "python3 <heredoc>"
    assert audit._command_shape('/bin/zsh -lc "cd /project && python3 tools/audit.py \\') == "python3 tools/audit.py"
    assert audit._command_shape("FOO=$(echo s3cret)") == "<subst>"
    assert audit._command_shape("TOKEN=$(curl --auth s3cret ...)") == "<subst>"
    assert audit._command_shape("curl -u user:tok") == "curl"
    assert audit._command_shape('curl --header "Authorization: Bearer x"') == "curl"
    assert audit._command_shape('--header "Authorization: Bearer x"') == "<unparsed>"
    for keyword in ("then", "do", "else", "elif", "fi", "done", "esac", "{", "}", "!", "time"):
        assert audit._command_shape(f"{keyword} python3 -m pytest") == "python3 -m pytest"
    assert audit._command_shape("bad!program arg") == "<unparsed>"
    assert audit._command_shape("https://user:pass@host") == "<unparsed>"


def test_ci_audit_does_not_pair_heredoc_exec_as_a_real_block():
    payload = b"\n".join([
        b"exec",
        b'/bin/zsh -lc "python3 - <<PY" in /project',
        b"exec",
        b"print(s3cret)",
        b" succeeded in 1s:",
    ])

    calls, unpaired, stop = audit._tail_execs(payload)

    assert calls == []
    assert unpaired == 2
    assert stop is None


def test_ci_audit_exception_output_omits_argument_value(capsys):
    assert audit.main(["--since", "SYNTHETIC_SECRET"]) == 0
    output = capsys.readouterr().out
    assert "SYNTHETIC_SECRET" not in output
    assert "invalid-input: ValueError" in output


def test_ci_audit_argument_error_does_not_echo_arguments(capsys):
    assert audit.main(["--unknown-option", "SYNTHETIC_SECRET"]) == 0
    captured = capsys.readouterr()
    assert "SYNTHETIC_SECRET" not in captured.out + captured.err
    assert captured.out == ""
    assert captured.err.strip() == "invalid-input: argument error"


def test_ci_audit_command_shape_rejects_oversized_input_quickly():
    command = "python3 " + ("x" * 1_900_000)

    started = audit.time.perf_counter()
    result = audit._command_shape(command)
    elapsed = audit.time.perf_counter() - started

    assert result == "<unparsed>"
    assert elapsed < 1.0


def test_ci_audit_tail_parser_obeys_deadline_and_block_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, _results = _fixture_spool(tmp_path)
    dispatch_dir = tmp_path / "state" / "dispatch"
    dispatch_dir.mkdir(parents=True, exist_ok=True)
    tail = dispatch_dir / "slow.tail"
    tail.write_text(
        "exec\npython3 -m pytest in /project\n" + "output line\n" * 20000,
        encoding="utf-8",
    )
    monkeypatch.setattr(audit.goalflight_dispatch_paths, "dispatch_base_dir", lambda: dispatch_dir)
    ledger_dir = tmp_path / "ledger"
    _write_ledger_record(ledger_dir, {
        "dispatch_id": "slow", "controller_label": "beta",
        "project_root": str(project), "engine": "codex",
    })
    monkeypatch.setattr(audit.goalflight_ledger, "runs_dir", lambda **_kwargs: ledger_dir)
    ticks = iter([0.0, 0.0, 0.0, 0.0, 2.0])
    monkeypatch.setattr(audit.time, "monotonic", lambda: next(ticks, 2.0))
    started = audit.time.perf_counter()
    report, timed_out, notes = audit._in_worker_report(
        project, "beta", NOW - audit.timedelta(days=1), 86400, 1.0,
    )
    elapsed = audit.time.perf_counter() - started

    assert timed_out
    assert any("budget exhausted" in note for note in notes)
    assert elapsed < 0.5
    assert report["unpaired_exec_blocks"] == 0

    monkeypatch.setattr(audit, "MAX_TAIL_EXEC_BLOCKS", 2)
    calls, unpaired, stopped = audit._tail_execs(
        b"exec\npython3 -m pytest in /project\n succeeded in 1s:\n"
        b"exec\npython3 -m pytest in /project\n succeeded in 1s:\n"
        b"exec\npython3 -m pytest in /project\n succeeded in 1s:\n"
    )
    assert len(calls) == 2 and unpaired == 0 and stopped == "block-cap"


def test_ci_audit_ignores_old_ledger_files_and_obeys_ledger_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, _results = _fixture_spool(tmp_path)
    ledger_dir = tmp_path / "ledger"
    ledger_dir.mkdir()
    old = ledger_dir / "old.json"
    old.write_text(json.dumps({
        "dispatch_id": "old", "controller_label": "beta",
        "project_root": str(project), "engine": "codex",
    }), encoding="utf-8")
    os.utime(old, (NOW.timestamp() - 86400 * 10, NOW.timestamp() - 86400 * 10))
    monkeypatch.setattr(audit.goalflight_ledger, "runs_dir", lambda **_kwargs: ledger_dir)
    ticks = iter([0.0, 2.0])
    monkeypatch.setattr(audit.time, "monotonic", lambda: next(ticks, 2.0))

    report, timed_out, notes = audit._in_worker_report(
        project, "beta", NOW - audit.timedelta(days=1), 86400, 1.0,
    )

    assert not timed_out
    assert report["commands"] == []
    assert not notes

    fresh = ledger_dir / "fresh.json"
    fresh.write_text(old.read_text(encoding="utf-8"), encoding="utf-8")
    os.utime(fresh, (NOW.timestamp(), NOW.timestamp()))
    ticks = iter([0.0, 2.0])
    monkeypatch.setattr(audit.time, "monotonic", lambda: next(ticks, 2.0))
    report, timed_out, notes = audit._in_worker_report(
        project, "beta", NOW - audit.timedelta(days=1), 86400, 1.0,
    )
    assert timed_out
    assert any("ledger scan budget exhausted" in note for note in notes)


def test_ci_audit_unreadable_spool_is_reported_and_exits_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    not_a_spool = tmp_path / "file"
    not_a_spool.write_text("not a directory", encoding="utf-8")
    monkeypatch.setenv("GOALFLIGHT_REMOTE_CI_SPOOL_DIR", str(not_a_spool))

    report = audit.analyze(project, now=NOW)
    assert report["commands"] is None
    assert any("receipt spool unavailable" in note for note in report["notes"])

    assert audit.main(["--project-root", str(project), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["commands"] is None


def test_ci_audit_skips_oversize_and_deep_json_with_notes(tmp_path, monkeypatch):
    project, results = _fixture_spool(tmp_path)
    monkeypatch.setenv("GOALFLIGHT_REMOTE_CI_SPOOL_DIR", str(results.parent))
    monkeypatch.setattr(audit, "MAX_RESULT_BYTES", 1024)
    monkeypatch.setattr(audit, "MAX_JSON_DEPTH", 8)
    (results / "oversize.json").write_bytes(b" " * 1025)
    deep_result = {
        "schema": "goalflight.remote-ci.result.v1",
        "request_id": "deep",
        "project_id": "sample/project",
        "status": "pass",
        "finished_at": NOW.isoformat(),
        "receipt": _receipt("deep-command", 1, 2),
    }
    deep_result["nested"] = {"a": {"b": {"c": {"d": {"e": {"f": {"g": {}}}}}}}}
    (results / "deep.json").write_text(json.dumps(deep_result), encoding="utf-8")

    report = audit.analyze(project, now=NOW)

    assert any("oversize receipt file(s) omitted" in note for note in report["notes"])
    assert any("over-depth receipt file(s) omitted" in note for note in report["notes"])
    assert "deep-command" not in {row["command_id"] for row in report["commands"]}


def test_ci_audit_caps_files_newest_first_and_obeys_time_budget(tmp_path, monkeypatch):
    project, results = _fixture_spool(tmp_path)
    monkeypatch.setenv("GOALFLIGHT_REMOTE_CI_SPOOL_DIR", str(results.parent))
    monkeypatch.setattr(audit, "MAX_RESULT_FILES", 2)
    requests = results.parent / "requests"
    all_receipts = [*results.glob("*.json"), *requests.glob("*.local.json")]
    for index, path in enumerate(sorted(all_receipts), start=1):
        os.utime(path, (index, index))

    report = audit.analyze(project, now=NOW)

    assert {row["command_id"] for row in report["commands"]} == {"misc-two", "misc-three"}
    assert any("receipt file(s) omitted by newest-first limit (2)" in note for note in report["notes"])

    monkeypatch.setattr(audit, "SCAN_TIME_BUDGET_SECONDS", 0.0)
    timed = audit.analyze(project, now=NOW)
    assert timed["commands"] == []
    assert any("analysis time budget reached" in note for note in timed["notes"])


def test_ci_audit_text_is_bounded_and_includes_cpu_and_wall_ratio(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project, results = _fixture_spool(tmp_path)
    for path in results.glob("*.json"):
        outer = json.loads(path.read_text(encoding="utf-8"))
        outer["finished_at"] = datetime.now(timezone.utc).isoformat()
        path.write_text(json.dumps(outer), encoding="utf-8")
    monkeypatch.setenv("GOALFLIGHT_REMOTE_CI_SPOOL_DIR", str(results.parent))

    assert audit.main(["--project-root", str(project), "--since", "24h"]) == 0
    output = capsys.readouterr().out
    assert len(output.splitlines()) <= 15
    assert "cpu=" in output and "wall/cpu=" in output
