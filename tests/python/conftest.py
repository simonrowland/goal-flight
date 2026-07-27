"""Pytest routing for the repository's mixed Python test styles."""

from __future__ import annotations

import ast
import os
from pathlib import Path

import pytest


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


@pytest.fixture(
    params=ISOLATED_TEST_MODULES,
    ids=lambda route: route[0].relative_to(TEST_DIR).as_posix(),
)
def isolated_test_module(request: pytest.FixtureRequest) -> tuple[Path, bool]:
    return request.param
