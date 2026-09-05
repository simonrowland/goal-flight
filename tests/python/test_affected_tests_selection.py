"""`select_modules` decides what the affected-tests gate runs.

It had no direct coverage, which is a poor place for a blind spot: a selector
bug does not fail loudly, it changes which evidence the gate collects. The
regression these pin is b-336 — a changed HELPER under tests/python was added
to the run set, pytest reported "no tests ran", the runner counted that as
FAILED, and a name-level gate read it as a regression in the code.
"""

from __future__ import annotations

import importlib.util
import uuid
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
SELECTOR = REPO_ROOT / "scripts" / "goalflight_affected_tests.py"


def _load():
    spec = importlib.util.spec_from_file_location("gaf_selection", SELECTOR)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def gaf():
    return _load()


HELPERS = ("machine_isolation.py", "support.py")


@pytest.mark.parametrize("helper", HELPERS)
def test_changed_helper_selects_its_users_not_itself(gaf, helper: str) -> None:
    """b-336: a helper is not a runnable module; it selects the modules using it."""
    if not (REPO_ROOT / "tests" / "python" / helper).exists():
        pytest.skip(f"{helper} absent")
    modules, _unmatched = gaf.select_modules([f"tests/python/{helper}"])
    names = {m.name for m in modules}
    assert helper not in names, f"{helper} selected itself; it has no tests to run"
    assert names, f"{helper} selected nothing; its users would go ungated"
    assert all(n.startswith("test_") for n in names), sorted(names)[:5]


def test_changed_test_module_still_selects_itself(gaf) -> None:
    """The helper carve-out must not stop a real test module selecting itself."""
    victim = next(
        (p for p in sorted((REPO_ROOT / "tests" / "python").glob("test_*.py"))),
        None,
    )
    assert victim is not None, "no test modules found"
    rel = victim.relative_to(REPO_ROOT).as_posix()
    modules, _unmatched = gaf.select_modules([rel])
    assert victim.name in {m.name for m in modules}


def test_every_selected_module_is_a_test_module(gaf) -> None:
    """Whatever the input, the run set never contains a non-test file."""
    modules, _unmatched = gaf.select_modules(
        [
            "tests/python/machine_isolation.py",
            "tests/python/support.py",
            "scripts/goalflight_dispatch.py",
        ]
    )
    offenders = [m.name for m in modules if not m.name.startswith("test_")]
    assert not offenders, offenders


def test_unmatched_paths_are_reported_not_silently_dropped(gaf) -> None:
    """A changed file that selects nothing is a coverage gap worth seeing.

    The stem is generated, not written literally: selection is `stem in src`
    over every test source, so a literal placeholder in THIS file would match
    itself and the test would silently stop testing anything.
    """
    stem = f"absent-{uuid.uuid4().hex}"
    rel = f"docs/{stem}.md"
    modules, unmatched = gaf.select_modules([rel])
    assert modules == [], modules
    assert rel in unmatched
