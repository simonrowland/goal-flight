"""Keep every Python test module isolated, visible, and enforced under pytest."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from machine_isolation import AMBIENT_IDENTITY_ENV, isolated_machine_env
from support import acp_sdk_unavailable_reason, requires_acp_sdk

TEST_DIR = Path(__file__).resolve().parent
REPO_ROOT = TEST_DIR.parents[1]
ISOLATED_TEST_FILE_ENV = "GOALFLIGHT_ISOLATED_TEST_FILE"
OUTCOME_PASS = "pass"
OUTCOME_SKIP = "skip"
OUTCOME_FLAKE = "flake"
OUTCOME_FAIL = "fail"
AMBIENT_RUNTIME_ENV = AMBIENT_IDENTITY_ENV


@dataclass(frozen=True)
class IsolatedModuleRun:
    outcome: str
    initial: subprocess.CompletedProcess[str]
    retry: subprocess.CompletedProcess[str] | None


def _tail(text: str, limit: int = 4_000) -> str:
    return text if len(text) <= limit else text[-limit:]


def _one_line_diagnostic(result: subprocess.CompletedProcess[str], limit: int = 400) -> str:
    text = result.stderr.strip() or result.stdout.strip() or "<no output>"
    return " ".join(text.split())[-limit:]


def _skipped_reason(
    result: subprocess.CompletedProcess[str],
    *,
    direct_script: bool,
) -> str | None:
    if direct_script or result.returncode != 0:
        return None
    return next(
        (
            line.strip()
            for line in result.stdout.splitlines()
            if line.startswith("SKIPPED ")
        ),
        None,
    )


def _classify_module_runs(
    initial: subprocess.CompletedProcess[str],
    retry: subprocess.CompletedProcess[str] | None,
    *,
    direct_script: bool,
) -> str:
    if initial.returncode == 0:
        return OUTCOME_SKIP if _skipped_reason(initial, direct_script=direct_script) else OUTCOME_PASS
    if retry is None:
        raise AssertionError("a failed isolated module was not rerun")
    if retry.returncode == 0 and not _skipped_reason(retry, direct_script=direct_script):
        return OUTCOME_FLAKE
    return OUTCOME_FAIL


def _validate_module_outcome(
    outcome: str,
    initial: subprocess.CompletedProcess[str],
    retry: subprocess.CompletedProcess[str] | None,
    *,
    direct_script: bool,
) -> None:
    initial_skip = _skipped_reason(initial, direct_script=direct_script)
    retry_skip = (
        _skipped_reason(retry, direct_script=direct_script)
        if retry is not None
        else None
    )
    if outcome == OUTCOME_PASS:
        assert initial.returncode == 0 and not initial_skip, "invalid pass outcome"
        return
    if outcome == OUTCOME_SKIP:
        assert initial.returncode == 0 and initial_skip, "invalid skip outcome"
        return
    if outcome == OUTCOME_FLAKE:
        assert (
            initial.returncode != 0
            and retry is not None
            and retry.returncode == 0
            and not retry_skip
        ), "invalid flake outcome: a real failure would be downgraded"
        return
    if outcome == OUTCOME_FAIL:
        assert (
            initial.returncode != 0
            and retry is not None
            and (retry.returncode != 0 or retry_skip)
        ), "invalid fail outcome"
        return
    raise AssertionError(f"unknown isolated module outcome: {outcome}")


def _run_module_with_confirmation(
    command: list[str],
    *,
    initial_env: dict[str, str],
    retry_env: dict[str, str],
    direct_script: bool,
) -> IsolatedModuleRun:
    initial = subprocess.run(
        command,
        cwd=REPO_ROOT,
        env=initial_env,
        capture_output=True,
        text=True,
        check=False,
    )
    retry = None
    if initial.returncode != 0:
        retry = subprocess.run(
            command,
            cwd=REPO_ROOT,
            env=retry_env,
            capture_output=True,
            text=True,
            check=False,
        )
    outcome = _classify_module_runs(initial, retry, direct_script=direct_script)
    _validate_module_outcome(
        outcome,
        initial,
        retry,
        direct_script=direct_script,
    )
    return IsolatedModuleRun(outcome, initial, retry)


def _isolated_env(root: Path, *, test_id: str) -> dict[str, str]:
    env = os.environ.copy()
    for key in AMBIENT_RUNTIME_ENV:
        env.pop(key, None)
    env.pop("GOALFLIGHT_WAKE_LEDGER", None)
    env.update(isolated_machine_env(root))
    env[ISOLATED_TEST_FILE_ENV] = test_id
    return env


def _interpreter_for(path: Path) -> Path:
    needs_acp_sdk = requires_acp_sdk(path)
    if needs_acp_sdk and os.name != "nt":
        configured = os.environ.get("GOALFLIGHT_ACP_PYTHON")
        return (
            Path(configured).expanduser()
            if configured
            else Path.home() / ".goal-flight/venvs/acp-0.10/bin/python"
        )
    return Path(sys.executable)


def test_isolated_test_module(
    isolated_test_module: tuple[Path, bool],
    tmp_path: Path,
    request: pytest.FixtureRequest,
) -> None:
    test_path, direct_script = isolated_test_module
    interpreter = _interpreter_for(test_path)
    if requires_acp_sdk(test_path) and os.name != "nt":
        unavailable = acp_sdk_unavailable_reason(str(interpreter))
        if unavailable is not None:
            pytest.skip(
                f"{test_path.relative_to(TEST_DIR).as_posix()}: "
                f"ACP SDK requirement unsatisfied: {unavailable}"
            )

    # Journal/state isolation: without these, a module that resolves default
    # paths write-opens LIVE journals — and a schema-carrying tree migrated
    # two of them mid-development (b-150). Second-level spawns that build
    # their own env are covered by the migration allow-guard, not this.
    test_id = test_path.relative_to(TEST_DIR).as_posix()
    command = (
        [str(interpreter), str(test_path)]
        if direct_script
        else [str(interpreter), "-m", "pytest", str(test_path), "-q", "-rs"]
    )
    run = _run_module_with_confirmation(
        command,
        initial_env=_isolated_env(tmp_path / "initial", test_id=test_id),
        retry_env=_isolated_env(tmp_path / "retry", test_id=test_id),
        direct_script=direct_script,
    )
    skipped_reason = _skipped_reason(run.initial, direct_script=direct_script)
    if run.outcome == OUTCOME_SKIP:
        assert skipped_reason is not None
        pytest.skip(f"{test_id}: {skipped_reason}")
    if run.outcome == OUTCOME_FLAKE:
        assert run.retry is not None
        request.node.user_properties.extend(
            (
                ("goalflight_isolated_outcome", OUTCOME_FLAKE),
                ("goalflight_test_id", test_id),
                ("goalflight_initial_exit", str(run.initial.returncode)),
                ("goalflight_retry_exit", str(run.retry.returncode)),
                ("goalflight_initial_diagnostic", _one_line_diagnostic(run.initial)),
            )
        )
        return
    assert run.outcome == OUTCOME_PASS, (
        f"FAIL {test_id}: initial exit={run.initial.returncode}; "
        f"confirmation exit={run.retry.returncode if run.retry is not None else 'not-run'}\n"
        f"initial stdout:\n{_tail(run.initial.stdout)}\n"
        f"initial stderr:\n{_tail(run.initial.stderr)}\n"
        f"confirmation stdout:\n{_tail(run.retry.stdout) if run.retry is not None else ''}\n"
        f"confirmation stderr:\n{_tail(run.retry.stderr) if run.retry is not None else ''}"
    )


def test_deliberately_flaky_module_is_classified_as_flake(tmp_path: Path) -> None:
    counter = tmp_path / "counter"
    probe = tmp_path / "deliberately_flaky.py"
    probe.write_text(
        "import os\n"
        "from pathlib import Path\n"
        "counter = Path(os.environ['B203_COUNTER'])\n"
        "if not counter.exists():\n"
        "    counter.write_text('failed-once', encoding='utf-8')\n"
        "    raise SystemExit(7)\n"
        "print('retry passed')\n",
        encoding="utf-8",
    )
    env = {**os.environ, "B203_COUNTER": str(counter)}
    run = _run_module_with_confirmation(
        [sys.executable, str(probe)],
        initial_env=env,
        retry_env=env,
        direct_script=True,
    )
    assert run.outcome == OUTCOME_FLAKE


def test_genuinely_broken_module_is_classified_as_fail(tmp_path: Path) -> None:
    probe = tmp_path / "broken.py"
    probe.write_text("raise SystemExit(9)\n", encoding="utf-8")
    run = _run_module_with_confirmation(
        [sys.executable, str(probe)],
        initial_env=os.environ.copy(),
        retry_env=os.environ.copy(),
        direct_script=True,
    )
    assert run.outcome == OUTCOME_FAIL
    assert run.initial.returncode == 9
    assert run.retry is not None and run.retry.returncode == 9


def test_mutated_real_failure_cannot_be_downgraded_to_flake(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    probe = tmp_path / "broken-under-mutation.py"
    probe.write_text("raise SystemExit(11)\n", encoding="utf-8")
    monkeypatch.setattr(
        sys.modules[__name__],
        "_classify_module_runs",
        lambda *_args, **_kwargs: OUTCOME_FLAKE,
    )
    with pytest.raises(
        AssertionError,
        match="invalid flake outcome: a real failure would be downgraded",
    ):
        _run_module_with_confirmation(
            [sys.executable, str(probe)],
            initial_env=os.environ.copy(),
            retry_env=os.environ.copy(),
            direct_script=True,
        )


def test_isolated_env_scrubs_ambient_runtime_identity(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    for key in AMBIENT_RUNTIME_ENV:
        monkeypatch.setenv(key, f"ambient-{key.lower()}")
    env = _isolated_env(tmp_path, test_id="test_probe.py")
    assert all(key not in env for key in AMBIENT_RUNTIME_ENV)
    assert env[ISOLATED_TEST_FILE_ENV] == "test_probe.py"
    assert env["GOALFLIGHT_STATE_DIR"] == str(tmp_path / "state")
    assert env["GOALFLIGHT_DISPATCH_DIR"] == str(tmp_path / "state" / "dispatch")
    assert env["GOAL_FLIGHT_PIDFILE_DIR"] == env["GOALFLIGHT_PIDFILE_DIR"] == str(
        tmp_path / "pids"
    )


def test_flake_report_is_visible_without_becoming_a_failure() -> None:
    import conftest as suite_conftest

    report = SimpleNamespace(
        when="call",
        passed=True,
        nodeid="driver[test_probe.py]",
        user_properties=(
            ("goalflight_isolated_outcome", OUTCOME_FLAKE),
            ("goalflight_test_id", "test_probe.py"),
            ("goalflight_initial_exit", "7"),
            ("goalflight_retry_exit", "0"),
            ("goalflight_initial_diagnostic", "synthetic first-run failure"),
        ),
    )
    assert suite_conftest.pytest_report_teststatus(report, None) == (
        "flake",
        "f",
        "FLAKE",
    )
    assert report.passed

    class Reporter:
        stats = {"flake": (report,)}

        def __init__(self) -> None:
            self.lines: list[str] = []

        def write_sep(self, _separator: str, title: str) -> None:
            self.lines.append(title)

        def write_line(self, line: str) -> None:
            self.lines.append(line)

    terminal = Reporter()
    suite_conftest.pytest_terminal_summary(terminal, 0, None)
    assert terminal.lines == [
        "isolated module flakes",
        "FLAKE  test_probe.py initial_exit=7 retry_exit=0 "
        "diagnostic=synthetic first-run failure",
    ]


def test_deliberate_flake_is_reported_end_to_end(tmp_path: Path) -> None:
    suite = tmp_path / "suite"
    suite.mkdir()
    (suite / "conftest.py").write_text(
        (TEST_DIR / "conftest.py").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    (suite / "test_probe.py").write_text(
        "def test_deliberate_flake(request):\n"
        "    request.node.user_properties.extend((\n"
        "        ('goalflight_isolated_outcome', 'flake'),\n"
        "        ('goalflight_test_id', 'deliberately_flaky.py'),\n"
        "        ('goalflight_initial_exit', '7'),\n"
        "        ('goalflight_retry_exit', '0'),\n"
        "        ('goalflight_initial_diagnostic', 'failed once'),\n"
        "    ))\n",
        encoding="utf-8",
    )
    env = {**os.environ, ISOLATED_TEST_FILE_ENV: "test_probe.py"}
    result = subprocess.run(
        [sys.executable, "-m", "pytest", str(suite), "-q"],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert "1 flake" in result.stdout
    assert (
        "FLAKE  deliberately_flaky.py initial_exit=7 retry_exit=0 "
        "diagnostic=failed once"
    ) in result.stdout


def test_directory_collection_is_visible_and_clean() -> None:
    """The obvious directory-level command must never fail without output."""
    # This early-import sentinel reaches goalflight_acp_run before another test
    # can populate a fake ACP module and mask import-time interpreter changes.
    sentinel_env = os.environ.copy()
    sentinel_path = TEST_DIR / "test_dispatch_ergonomics.py"
    sentinel_env[ISOLATED_TEST_FILE_ENV] = sentinel_path.relative_to(TEST_DIR).as_posix()
    sentinel = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            str(sentinel_path),
            "--collect-only",
            "-q",
        ],
        cwd=REPO_ROOT,
        env=sentinel_env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert sentinel.returncode == 0, (
        f"sentinel collection exited {sentinel.returncode}\n"
        f"stdout:\n{_tail(sentinel.stdout)}\n"
        f"stderr:\n{_tail(sentinel.stderr)}"
    )

    result = subprocess.run(
        [sys.executable, "-m", "pytest", str(TEST_DIR), "--collect-only", "-q"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    combined = result.stdout + result.stderr
    assert result.returncode == 0, (
        f"directory collection exited {result.returncode}\n"
        f"stdout:\n{_tail(result.stdout)}\n"
        f"stderr:\n{_tail(result.stderr)}"
    )
    assert combined.strip(), "directory collection exited without any diagnostic output"

    execution = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            str(TEST_DIR),
            "-q",
            "-k",
            "test_isolated_test_module and test_dispatch_ergonomics",
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    execution_output = execution.stdout + execution.stderr
    assert execution.returncode == 0, (
        f"directory execution exited {execution.returncode}\n"
        f"stdout:\n{_tail(execution.stdout)}\n"
        f"stderr:\n{_tail(execution.stderr)}"
    )
    assert execution_output.strip(), "directory execution exited without any diagnostic output"
