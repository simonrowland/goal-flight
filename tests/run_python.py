#!/usr/bin/env python3
"""Python-only test runner for native Windows and non-bash hosts."""

from __future__ import annotations

import argparse
import atexit
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import NamedTuple


ROOT = Path(__file__).resolve().parents[1]
TEST_DIR = ROOT / "tests" / "python"
META_DRIVER_NAME = "test_script_style_modules.py"
sys.path.insert(0, str(TEST_DIR))
from support import (  # noqa: E402
    AMBIENT_IDENTITY_ENV,
    acp_sdk_unavailable_reason,
    has_main_driver,
    isolated_machine_env,
    requires_acp_sdk,
)

ACP_PY = os.environ.get(
    "GOALFLIGHT_ACP_PYTHON",
    str(Path.home() / ".goal-flight" / "venvs" / "acp-0.10" / "bin" / "python"),
)


def _test_files() -> list[Path]:
    return sorted(
        test for test in TEST_DIR.glob("test_*.py") if test.name != META_DRIVER_NAME
    )


def _skip_lines(stdout: str, stderr: str) -> list[str]:
    lines = []
    for line in (stdout + "\n" + stderr).splitlines():
        if line.startswith("SKIP:"):
            lines.append(line)
    return lines


def _is_full_file_skip(test: Path, skips: list[str]) -> bool:
    if not skips:
        return False
    label = test.relative_to(ROOT).as_posix()
    prefixes = (f"SKIP: {test.name}:", f"SKIP: {label}:")
    return all(line.startswith(prefixes) for line in skips)


def _python_for(test: Path) -> str:
    if os.name != "nt" and requires_acp_sdk(test):
        return ACP_PY
    return sys.executable


def _requirement_skip(test: Path, interpreter: str) -> subprocess.CompletedProcess[str] | None:
    if os.name == "nt" or not requires_acp_sdk(test):
        return None
    reason = acp_sdk_unavailable_reason(interpreter)
    if reason is None:
        return None
    label = test.relative_to(ROOT).as_posix()
    return subprocess.CompletedProcess(
        args=[interpreter, str(test)],
        returncode=0,
        stdout=f"SKIP: {label}: ACP SDK requirement unsatisfied: {reason}\n",
        stderr="",
    )


_PYTEST_COUNT_RE = re.compile(
    r"(?<![\w.])(?P<count>\d+) "
    r"(?P<outcome>passed|skipped|xfailed|xpassed)(?![\w.])"
)


class PytestExecutionSummary(NamedTuple):
    executed: int
    skipped: int
    description: str

    @property
    def all_skipped(self) -> bool:
        return self.executed > 0 and self.executed == self.skipped


def _test_command(test: Path, interpreter: str) -> tuple[list[str], bool]:
    if has_main_driver(test):
        return [interpreter, str(test)], False
    return [interpreter, "-m", "pytest", str(test), "-q", "-rs"], True


def _pytest_execution_summary(stdout: str, stderr: str) -> PytestExecutionSummary | None:
    for line in reversed((stdout + "\n" + stderr).splitlines()):
        matches = list(_PYTEST_COUNT_RE.finditer(line))
        executed = sum(int(match.group("count")) for match in matches)
        if executed > 0:
            skipped = sum(
                int(match.group("count"))
                for match in matches
                if match.group("outcome") == "skipped"
            )
            return PytestExecutionSummary(executed, skipped, line.strip("= "))
    return None


def _pytest_skip_reasons(stdout: str, stderr: str) -> list[str]:
    return [
        line.strip()
        for line in (stdout + "\n" + stderr).splitlines()
        if line.startswith("SKIPPED ")
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run goal-flight Python tests")
    parser.add_argument("--list", action="store_true", help="List tests without running")
    parser.add_argument(
        "--timeout",
        type=float,
        default=180.0,
        help="Per-file timeout in seconds (default: 180)",
    )
    args = parser.parse_args(argv)

    # Force the committed baseline capacity caps: /dev/null reads empty so the
    # per-operator capacity.local.json loader falls back, keeping suite
    # assertions machine-independent (the bash harness does the same). Child
    # subprocesses inherit this via os.environ.
    os.environ.setdefault("GOALFLIGHT_CAPACITY_CONF", os.devnull)

    if args.list:
        for test in _test_files():
            print(test.relative_to(ROOT).as_posix())
        return 0

    machine_base = Path(tempfile.mkdtemp(prefix="gf-run-python-"))
    atexit.register(shutil.rmtree, machine_base, ignore_errors=True)
    passed = 0
    skipped = 0
    partial_skipped = 0
    failed: list[str] = []
    for test_index, test in enumerate(_test_files()):
        label = test.relative_to(ROOT).as_posix()
        try:
            py = _python_for(test)
            proc = _requirement_skip(test, py)
        except (OSError, SyntaxError, ValueError) as exc:
            failed.append(label)
            print(f"FAIL  {label}")
            print(f"      invalid test requirement declaration: {type(exc).__name__}: {exc}")
            continue
        print(f"RUN   {label}", flush=True)
        used_pytest = False
        if proc is None:
            command, used_pytest = _test_command(test, py)
            child_env = os.environ.copy()
            for key in AMBIENT_IDENTITY_ENV:
                child_env.pop(key, None)
            child_env.pop("GOALFLIGHT_WAKE_LEDGER", None)
            child_env.update(isolated_machine_env(machine_base / f"test-{test_index}"))
            child_env["GOALFLIGHT_ISOLATED_TEST_FILE"] = test.relative_to(TEST_DIR).as_posix()
            try:
                proc = subprocess.run(
                    command,
                    cwd=str(ROOT),
                    env=child_env,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    timeout=args.timeout,
                    check=False,
                )
            except subprocess.TimeoutExpired as exc:
                failed.append(label)
                print(f"FAIL  {label}", flush=True)
                print(f"      timed out after {args.timeout:g}s")
                for stream in (exc.stdout, exc.stderr):
                    if not stream:
                        continue
                    if isinstance(stream, bytes):
                        stream = stream.decode("utf-8", errors="replace")
                    for line in str(stream).splitlines():
                        print(f"      {line}")
                continue
        pytest_summary = (
            _pytest_execution_summary(proc.stdout, proc.stderr)
            if used_pytest and proc.returncode == 0
            else None
        )
        if used_pytest and proc.returncode == 0 and pytest_summary is None:
            proc = subprocess.CompletedProcess(
                args=proc.args,
                returncode=1,
                stdout=proc.stdout,
                stderr=(
                    proc.stderr
                    + "\nRUNNER ERROR: pytest exited 0 without a nonzero executed-test count\n"
                ),
            )
        if (
            used_pytest
            and proc.returncode == 0
            and pytest_summary is not None
            and pytest_summary.all_skipped
        ):
            reasons = _pytest_skip_reasons(proc.stdout, proc.stderr)
            if reasons:
                skipped += 1
                print(f"SKIP  {label}", flush=True)
                for line in reasons:
                    print(f"      {line}")
                continue
            proc = subprocess.CompletedProcess(
                args=proc.args,
                returncode=1,
                stdout=proc.stdout,
                stderr=(
                    proc.stderr
                    + "\nRUNNER ERROR: pytest skipped every test without a visible reason\n"
                ),
            )
        skips = _skip_lines(proc.stdout, proc.stderr)
        if proc.returncode == 0 and _is_full_file_skip(test, skips):
            skipped += 1
            print(f"SKIP  {label}", flush=True)
            for line in skips:
                print(f"      {line}")
            continue
        if proc.returncode == 0:
            passed += 1
            if skips:
                partial_skipped += 1
                print(f"PASS  {label} (some skips)", flush=True)
                for line in skips:
                    print(f"      {line}")
            elif pytest_summary is not None:
                print(f"PASS  {label} (pytest: {pytest_summary.description})", flush=True)
            else:
                print(f"PASS  {label}", flush=True)
            continue

        failed.append(label)
        print(f"FAIL  {label}", flush=True)
        for stream in (proc.stdout, proc.stderr):
            if not stream:
                continue
            for line in stream.splitlines():
                print(f"      {line}")

    print()
    print(f"===== {passed} passed, {skipped} skipped, {len(failed)} failed =====")
    if partial_skipped:
        print(f"      {partial_skipped} passed files had case-level skips")
    if failed:
        print("failed:")
        for label in failed:
            print(f"  {label}")
    return len(failed)


if __name__ == "__main__":
    raise SystemExit(main())
