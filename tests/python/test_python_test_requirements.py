#!/usr/bin/env python3
"""Regression tests for declared Python test interpreter requirements."""

from __future__ import annotations

import contextlib
import importlib.util
import io
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from types import SimpleNamespace
from unittest.mock import patch

from support import has_main_driver, requires_acp_sdk
import test_script_style_modules as shared_driver


ROOT = Path(__file__).resolve().parents[2]
RUNNER_PATH = ROOT / "tests" / "run_python.py"
SPEC = importlib.util.spec_from_file_location("goalflight_run_python", RUNNER_PATH)
assert SPEC is not None and SPEC.loader is not None
run_python = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(run_python)


def _write_test(path: Path, *, requires_sdk: bool) -> None:
    marker = "REQUIRES_ACP_SDK = True\n" if requires_sdk else ""
    path.write_text(
        "from __future__ import annotations\n\n" + marker + "\nprint('probe')\n",
        encoding="utf-8",
    )


def _write_unsatisfied_python(path: Path) -> None:
    path.write_text(
        "#!/bin/sh\n"
        "echo \"ModuleNotFoundError: No module named 'acp'\" >&2\n"
        "exit 19\n",
        encoding="utf-8",
    )
    path.chmod(0o755)


def test_real_os_sandbox_requirement_selects_acp_interpreter() -> None:
    test = ROOT / "tests" / "python" / "test_os_sandbox.py"
    assert requires_acp_sdk(test), "real SDK-using file lost its declaration"
    with patch.object(run_python, "ACP_PY", "/declared/acp/python"):
        assert run_python._python_for(test) == "/declared/acp/python"


def test_selection_uses_declaration_not_filename() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        neutral = root / "test_neutral_name.py"
        misleading = root / "test_acp_misleading_name.py"
        _write_test(neutral, requires_sdk=True)
        _write_test(misleading, requires_sdk=False)
        with patch.object(run_python, "ACP_PY", "/declared/acp/python"):
            assert run_python._python_for(neutral) == "/declared/acp/python"
            assert run_python._python_for(misleading) == sys.executable


def test_unsatisfied_declared_requirement_is_a_clear_full_file_skip() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        test_dir = root / "tests" / "python"
        test_dir.mkdir(parents=True)
        test = test_dir / "test_neutral_name.py"
        interpreter = root / "python-without-acp"
        _write_test(test, requires_sdk=True)
        _write_unsatisfied_python(interpreter)
        stdout = io.StringIO()
        with (
            patch.object(run_python, "ROOT", root),
            patch.object(run_python, "TEST_DIR", test_dir),
            patch.object(run_python, "ACP_PY", str(interpreter)),
            patch.object(run_python, "_test_files", return_value=[test]),
            contextlib.redirect_stdout(stdout),
        ):
            exit_code = run_python.main(["--timeout", "120"])
        output = stdout.getvalue()
        assert exit_code == 0, output
        assert "SKIP  tests/python/test_neutral_name.py" in output, output
        assert "ACP SDK requirement unsatisfied" in output, output
        assert "cannot import acp and pydantic" in output, output
        assert "ModuleNotFoundError" in output, output
        assert "===== 0 passed, 1 skipped, 0 failed =====" in output, output


def test_invalid_declaration_is_rejected() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        test = Path(tmp) / "test_invalid_marker.py"
        test.write_text("REQUIRES_ACP_SDK = should_use_sdk\n", encoding="utf-8")
        try:
            requires_acp_sdk(test)
        except ValueError as exc:
            assert "must be the literal True or False" in str(exc)
        else:
            raise AssertionError("dynamic requirement declaration was accepted")


def test_previously_silent_pytest_file_executes_through_runner() -> None:
    test = ROOT / "tests" / "python" / "test_claim_id.py"
    stdout = io.StringIO()
    with (
        patch.object(run_python, "_test_files", return_value=[test]),
        contextlib.redirect_stdout(stdout),
    ):
        exit_code = run_python.main(["--timeout", "120"])
    output = stdout.getvalue()
    assert exit_code == 0, output
    assert "PASS  tests/python/test_claim_id.py (pytest:" in output, output
    assert re.search(r"pytest: [1-9][0-9]* passed", output), output


def test_unrunnable_pytest_file_is_not_reported_as_pass() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        test_dir = root / "tests" / "python"
        test_dir.mkdir(parents=True)
        test = test_dir / "test_unrunnable.py"
        test.write_text(
            "def test_needs_missing_fixture(definitely_missing_fixture):\n"
            "    assert definitely_missing_fixture\n",
            encoding="utf-8",
        )
        stdout = io.StringIO()
        with (
            patch.object(run_python, "ROOT", root),
            patch.object(run_python, "TEST_DIR", test_dir),
            patch.object(run_python, "_test_files", return_value=[test]),
            contextlib.redirect_stdout(stdout),
        ):
            exit_code = run_python.main(["--timeout", "120"])
        output = stdout.getvalue()
        assert exit_code == 1, output
        assert "FAIL  tests/python/test_unrunnable.py" in output, output
        assert "fixture 'definitely_missing_fixture' not found" in output, output
        assert "PASS  tests/python/test_unrunnable.py" not in output, output


def test_meta_driver_is_excluded_and_each_leaf_executes_once() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        test_dir = root / "tests" / "python"
        test_dir.mkdir(parents=True)
        counter = root / "executions.txt"
        leaf_names = ("test_leaf_one.py", "test_leaf_two.py")
        for name in leaf_names:
            (test_dir / name).write_text(
                "import os\n"
                "from pathlib import Path\n"
                "if __name__ == '__main__':\n"
                "    with Path(os.environ['GF_EXECUTION_COUNTER']).open(\n"
                "        'a', encoding='utf-8'\n"
                "    ) as handle:\n"
                f"        handle.write({name!r} + '\\n')\n",
                encoding="utf-8",
            )
        (test_dir / run_python.META_DRIVER_NAME).write_text(
            "import os\n"
            "from pathlib import Path\n"
            "import subprocess\n"
            "import sys\n"
            f"LEAVES = {leaf_names!r}\n"
            "def test_repeat_every_leaf():\n"
            "    root = Path(__file__).parent\n"
            "    for leaf in LEAVES:\n"
            "        subprocess.run([sys.executable, str(root / leaf)], check=True)\n",
            encoding="utf-8",
        )
        stdout = io.StringIO()
        with (
            patch.object(run_python, "ROOT", root),
            patch.object(run_python, "TEST_DIR", test_dir),
            patch.dict(os.environ, {"GF_EXECUTION_COUNTER": str(counter)}),
            contextlib.redirect_stdout(stdout),
        ):
            exit_code = run_python.main(["--timeout", "120"])
        output = stdout.getvalue()
        assert exit_code == 0, output
        assert counter.read_text(encoding="utf-8").splitlines() == list(leaf_names), output
        assert f"RUN   tests/python/{run_python.META_DRIVER_NAME}" not in output, output
        assert "===== 2 passed, 0 skipped, 0 failed =====" in output, output


def test_all_skipped_pytest_module_is_skip_with_reason() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        test_dir = root / "tests" / "python"
        test_dir.mkdir(parents=True)
        test = test_dir / "test_all_skipped.py"
        test.write_text(
            "import pytest\n"
            "pytestmark = pytest.mark.skip(reason='all-skipped poison reason')\n"
            "def test_never_runs():\n"
            "    raise AssertionError('all-skipped module executed a test')\n",
            encoding="utf-8",
        )
        stdout = io.StringIO()
        with (
            patch.object(run_python, "ROOT", root),
            patch.object(run_python, "TEST_DIR", test_dir),
            contextlib.redirect_stdout(stdout),
        ):
            exit_code = run_python.main(["--timeout", "120"])
        output = stdout.getvalue()
        assert exit_code == 0, output
        assert "SKIP  tests/python/test_all_skipped.py" in output, output
        assert "all-skipped poison reason" in output, output
        assert "PASS  tests/python/test_all_skipped.py" not in output, output
        assert "===== 0 passed, 1 skipped, 0 failed =====" in output, output


def test_guarded_module_delegating_to_pytest_is_skip_with_reason() -> None:
    test = ROOT / "tests" / "python" / "test_codex_dispatch_seams.py"
    assert has_main_driver(test), "real delegating module must remain script-routed"

    with tempfile.TemporaryDirectory() as tmp:
        plugin_dir = Path(tmp)
        (plugin_dir / "force_all_skipped.py").write_text(
            "import pytest\n"
            "def pytest_collection_modifyitems(items):\n"
            "    marker = pytest.mark.skip(\n"
            "        reason='per-dispatch worker homes are local POSIX-only'\n"
            "    )\n"
            "    for item in items:\n"
            "        item.add_marker(marker)\n",
            encoding="utf-8",
        )
        pythonpath = os.pathsep.join(
            part
            for part in (str(plugin_dir), os.environ.get("PYTHONPATH", ""))
            if part
        )
        stdout = io.StringIO()
        with (
            patch.object(run_python, "_test_files", return_value=[test]),
            patch.dict(
                os.environ,
                {
                    "PYTHONPATH": pythonpath,
                    "PYTEST_ADDOPTS": "-p force_all_skipped -rs",
                },
            ),
            contextlib.redirect_stdout(stdout),
        ):
            exit_code = run_python.main(["--timeout", "120"])
        output = stdout.getvalue()

    assert exit_code == 0, output
    assert "SKIP  tests/python/test_codex_dispatch_seams.py" in output, output
    assert "per-dispatch worker homes are local POSIX-only" in output, output
    assert "PASS  tests/python/test_codex_dispatch_seams.py" not in output, output
    assert "===== 0 passed, 1 skipped, 0 failed =====" in output, output


def test_guarded_module_with_zero_pytest_cases_is_not_pass() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        test_dir = root / "tests" / "python"
        test_dir.mkdir(parents=True)
        test = test_dir / "test_empty_delegate.py"
        test.write_text(
            "import pytest\n"
            "if __name__ == '__main__':\n"
            "    pytest.main([__file__, '-q'])\n",
            encoding="utf-8",
        )
        stdout = io.StringIO()
        with (
            patch.object(run_python, "ROOT", root),
            patch.object(run_python, "TEST_DIR", test_dir),
            patch.object(run_python, "_test_files", return_value=[test]),
            contextlib.redirect_stdout(stdout),
        ):
            exit_code = run_python.main(["--timeout", "120"])
        output = stdout.getvalue()

    assert exit_code == 1, output
    assert "FAIL  tests/python/test_empty_delegate.py" in output, output
    assert "pytest exited 0 without a nonzero executed-test count" in output, output
    assert "PASS  tests/python/test_empty_delegate.py" not in output, output
    assert "===== 0 passed, 0 skipped, 1 failed =====" in output, output


def test_script_outcome_words_are_not_a_pytest_summary() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        test_dir = root / "tests" / "python"
        test_dir.mkdir(parents=True)
        test = test_dir / "test_custom_summary.py"
        test.write_text(
            "if __name__ == '__main__':\n"
            "    print('1 skipped in custom probe output')\n",
            encoding="utf-8",
        )
        stdout = io.StringIO()
        with (
            patch.object(run_python, "ROOT", root),
            patch.object(run_python, "TEST_DIR", test_dir),
            patch.object(run_python, "_test_files", return_value=[test]),
            contextlib.redirect_stdout(stdout),
        ):
            exit_code = run_python.main(["--timeout", "120"])
        output = stdout.getvalue()

    assert exit_code == 0, output
    assert "PASS  tests/python/test_custom_summary.py" in output, output
    assert "SKIP  tests/python/test_custom_summary.py" not in output, output
    assert "===== 1 passed, 0 skipped, 0 failed =====" in output, output


def test_shared_driver_uses_declared_interpreter_for_pytest_module() -> None:
    if os.name == "nt":
        return
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        test_dir = root / "tests" / "python"
        test_dir.mkdir(parents=True)
        test = test_dir / "test_declared_pytest_module.py"
        test.write_text(
            "REQUIRES_ACP_SDK = True\n"
            "def test_runs():\n"
            "    assert True\n",
            encoding="utf-8",
        )
        assert not has_main_driver(test), "probe must exercise pytest-style routing"

        log = root / "interpreter.log"
        interpreter = root / "declared-acp-python"
        interpreter.write_text(
            f"#!{sys.executable}\n"
            "from pathlib import Path\n"
            "import os\n"
            "import subprocess\n"
            "import sys\n"
            "with Path(os.environ['GF_INTERPRETER_LOG']).open(\n"
            "    'a', encoding='utf-8'\n"
            ") as handle:\n"
            "    handle.write(repr(sys.argv[1:]) + '\\n')\n"
            "if sys.argv[1:] == ['-c', 'import acp, pydantic']:\n"
            "    raise SystemExit(0)\n"
            f"raise SystemExit(subprocess.run([{sys.executable!r}, *sys.argv[1:]]).returncode)\n",
            encoding="utf-8",
        )
        interpreter.chmod(0o755)
        request = SimpleNamespace(node=SimpleNamespace(user_properties=[]))
        with (
            patch.object(shared_driver, "REPO_ROOT", root),
            patch.object(shared_driver, "TEST_DIR", test_dir),
            patch.dict(
                os.environ,
                {
                    "GOALFLIGHT_ACP_PYTHON": str(interpreter),
                    "GF_INTERPRETER_LOG": str(log),
                },
            ),
        ):
            route = (test, has_main_driver(test))
            shared_driver.test_isolated_test_module(route, root / "isolated", request)
        invocations = log.read_text(encoding="utf-8").splitlines()
        assert "['-c', 'import acp, pydantic']" in invocations, invocations
        assert any(line.startswith("['-m', 'pytest',") for line in invocations), invocations


def main() -> None:
    test_real_os_sandbox_requirement_selects_acp_interpreter()
    test_selection_uses_declaration_not_filename()
    test_unsatisfied_declared_requirement_is_a_clear_full_file_skip()
    test_invalid_declaration_is_rejected()
    test_previously_silent_pytest_file_executes_through_runner()
    test_unrunnable_pytest_file_is_not_reported_as_pass()
    test_meta_driver_is_excluded_and_each_leaf_executes_once()
    test_all_skipped_pytest_module_is_skip_with_reason()
    test_guarded_module_delegating_to_pytest_is_skip_with_reason()
    test_guarded_module_with_zero_pytest_cases_is_not_pass()
    test_script_outcome_words_are_not_a_pytest_summary()
    test_shared_driver_uses_declared_interpreter_for_pytest_module()
    print("OK: Python test requirement tests pass")


if __name__ == "__main__":
    main()
