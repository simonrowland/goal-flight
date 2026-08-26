"""Pytest routing for the repository's mixed Python test styles."""

from __future__ import annotations

import ast
import os
from pathlib import Path

import pytest

from machine_isolation import isolate_goalflight_machine_state_impl


TEST_DIR = Path(__file__).resolve().parent
DRIVER_NAME = "test_script_style_modules.py"
ISOLATED_TEST_FILE_ENV = "GOALFLIGHT_ISOLATED_TEST_FILE"


def _is_main_guard(node: ast.stmt) -> bool:
    if not isinstance(node, ast.If) or not isinstance(node.test, ast.Compare):
        return False
    compare = node.test
    if len(compare.ops) != 1 or not isinstance(compare.ops[0], ast.Eq):
        return False
    if len(compare.comparators) != 1:
        return False
    values = (compare.left, compare.comparators[0])
    return any(isinstance(value, ast.Name) and value.id == "__name__" for value in values) and any(
        isinstance(value, ast.Constant) and value.value == "__main__" for value in values
    )


def _has_main_guard(path: Path) -> bool:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return any(_is_main_guard(node) for node in tree.body)


ISOLATED_TEST_MODULES = tuple(
    (path, _has_main_guard(path))
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
