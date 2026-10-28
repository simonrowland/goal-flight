"""Pytest routing for the repository's mixed Python test styles."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from machine_isolation import isolate_goalflight_machine_state_impl
from support import capture_dispatch_sandbox_posture, has_main_driver


TEST_DIR = Path(__file__).resolve().parent
DRIVER_NAME = "test_script_style_modules.py"
ISOLATED_TEST_FILE_ENV = "GOALFLIGHT_ISOLATED_TEST_FILE"


ISOLATED_TEST_MODULES = tuple(
    (path, has_main_driver(path))
    for path in sorted(TEST_DIR.rglob("test_*.py"))
    if path.name != DRIVER_NAME
)


# The canonical suite executes each Python test file in a fresh process.
# Importing the whole directory into one pytest process leaks module globals,
# monkeypatches, and environment between files. The driver preserves that
# isolation and keeps guarded case_* scripts from becoming zero-test greens.
_isolated_child = os.environ.get(ISOLATED_TEST_FILE_ENV)
collect_ignore = [
    path.relative_to(TEST_DIR).as_posix()
    for path, _direct_script in ISOLATED_TEST_MODULES
    if path.relative_to(TEST_DIR).as_posix() != _isolated_child
]
if _isolated_child:
    collect_ignore.append(DRIVER_NAME)


def pytest_configure(config: pytest.Config) -> None:
    capture_dispatch_sandbox_posture()
    config.addinivalue_line(
        "markers",
        "live_machine_state: do not isolate GOALFLIGHT_* machine paths "
        "(the test's subject is the live default)",
    )


@pytest.fixture(autouse=True)
def isolate_goalflight_machine_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
) -> dict[str, str] | None:
    return isolate_goalflight_machine_state_impl(tmp_path, monkeypatch, request)


@pytest.fixture(autouse=True)
def ample_disk_space_for_launch_tests(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    disk_usage = shutil.disk_usage

    def report_ample_space(path):
        usage = disk_usage(path)
        return usage._replace(free=max(usage.free, 50_000_000_000))

    monkeypatch.setattr(shutil, "disk_usage", report_ample_space)
    child_python = tmp_path / "goalflight-test-python"
    child_python.mkdir()
    (child_python / "usercustomize.py").write_text(
        "import shutil\n"
        "_disk_usage = shutil.disk_usage\n"
        "def _ample_disk_space(path):\n"
        "    usage = _disk_usage(path)\n"
        "    return usage._replace(free=max(usage.free, 50_000_000_000))\n"
        "shutil.disk_usage = _ample_disk_space\n",
        encoding="utf-8",
    )
    python_path = [str(child_python), os.environ.get("PYTHONPATH", "")]
    monkeypatch.setenv(
        "PYTHONPATH", os.pathsep.join(part for part in python_path if part)
    )


@pytest.fixture(
    params=ISOLATED_TEST_MODULES,
    ids=lambda route: route[0].relative_to(TEST_DIR).as_posix(),
)
def isolated_test_module(request: pytest.FixtureRequest) -> tuple[Path, bool]:
    return request.param


def pytest_report_teststatus(report, config):
    del config
    properties = dict(getattr(report, "user_properties", ()))
    if (
        report.when == "call"
        and report.passed
        and properties.get("goalflight_isolated_outcome") == "flake"
    ):
        return "flake", "f", "FLAKE"
    return None


def pytest_terminal_summary(terminalreporter, exitstatus, config) -> None:
    del exitstatus, config
    flakes = terminalreporter.stats.get("flake", ())
    if not flakes:
        return
    terminalreporter.write_sep("=", "isolated module flakes")
    for report in flakes:
        properties = dict(getattr(report, "user_properties", ()))
        terminalreporter.write_line(
            "FLAKE  "
            f"{properties.get('goalflight_test_id', report.nodeid)} "
            f"initial_exit={properties.get('goalflight_initial_exit', '?')} "
            f"retry_exit={properties.get('goalflight_retry_exit', '?')} "
            f"diagnostic={properties.get('goalflight_initial_diagnostic', '<none>')}"
        )
