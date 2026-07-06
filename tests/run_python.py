#!/usr/bin/env python3
"""Python-only test runner for native Windows and non-bash hosts."""

from __future__ import annotations

import argparse
import atexit
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TEST_DIR = ROOT / "tests" / "python"
ACP_PY = os.environ.get(
    "GOALFLIGHT_ACP_PYTHON",
    str(Path.home() / ".goal-flight" / "venvs" / "acp-0.10" / "bin" / "python"),
)


def _test_files() -> list[Path]:
    return sorted(TEST_DIR.glob("test_*.py"))


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
    if os.name != "nt" and test.name.startswith("test_acp_"):
        return ACP_PY
    return sys.executable


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

    # Isolate the durable canonical task-store base so no test writes to the real
    # ~/.local/state/goal-flight. Each test's unique tmp project path hashes to
    # its own store beneath this base; per-test GOALFLIGHT_TASK_STORE_DIR still
    # wins. Cleaned at interpreter exit.
    if "GOALFLIGHT_TASK_STORE_DIR" not in os.environ:
        _ts_base = tempfile.mkdtemp(prefix="gf-test-taskstore-")
        os.environ["GOALFLIGHT_TASK_STORE_DIR"] = _ts_base
        atexit.register(shutil.rmtree, _ts_base, ignore_errors=True)

    if args.list:
        for test in _test_files():
            print(test.relative_to(ROOT).as_posix())
        return 0

    passed = 0
    skipped = 0
    partial_skipped = 0
    failed: list[str] = []
    for test in _test_files():
        label = test.relative_to(ROOT).as_posix()
        py = _python_for(test)
        if os.name != "nt" and test.name.startswith("test_acp_") and not Path(py).is_file():
            failed.append(label)
            print(f"FAIL  {label}")
            print(f"      SDK missing -- run install: {py}")
            continue
        print(f"RUN   {label}", flush=True)
        try:
            proc = subprocess.run(
                [py, str(test)],
                cwd=str(ROOT),
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
