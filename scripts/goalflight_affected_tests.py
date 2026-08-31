#!/usr/bin/env python3
"""Run only the Python test modules a change could plausibly affect.

`tests/run.sh` is the authority and stays the pre-push gate. It is not usable
as a pre-commit gate: it forks a fresh process for each of ~249 modules, and on
a busy machine that has taken over 40 minutes, so it gets skipped or killed
part-way. A killed run is the dangerous case -- the bash phase has already
printed its PASS lines by then, so a truncated log reads exactly like a green
one.

This selects the modules that mention the changed code, runs them under the
same environment isolation `run.sh` uses, and reports three states rather than
two: passed, failed, and did-not-complete. Anything short of "every selected
module completed and passed" exits non-zero, and the summary always names how
many modules were NOT selected, so its green can never be mistaken for the
full suite's.

Selection is deliberately over-inclusive (substring mention, not import
analysis): a false positive costs 0.2s, a false negative costs a missed
regression.

Use this to check a single module too, rather than running it by hand:

    python3 scripts/goalflight_affected_tests.py tests/python/test_foo.py

`python3 tests/python/test_foo.py` is NOT a general substitute. On a module
with no `__main__` guard it executes zero tests, prints nothing, and exits 0 --
a pass that cannot fail. Measured: test_goalflight_p3.py exits 0 as a script
while pytest reports 4 failed, 41 passed. Running a module by hand also leaves
every GOALFLIGHT_* isolation variable unset, so it reads the live journal,
task store, and capacity conf instead of a sandbox. This routes on the suite's
own has_main_driver predicate and sets the isolation, so neither trap applies.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
TEST_DIR = REPO_ROOT / "tests" / "python"

# The suite holds two test styles and routes them differently: modules with a
# __main__ driver run as scripts, pytest-native modules run under pytest.
# Reuse the driver's own predicate rather than guessing -- running a
# pytest-native module as a script calls its test functions with no fixtures
# and reports a false failure.
sys.path.insert(0, str(TEST_DIR))
from support import has_main_driver  # noqa: E402

PASSED, FAILED, INCOMPLETE = "passed", "failed", "did-not-complete"


def _git(*args: str) -> list[str]:
    proc = subprocess.run(
        ["git", "-C", str(REPO_ROOT), *args],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        return []
    return [line for line in proc.stdout.splitlines() if line.strip()]


def changed_paths(since: str | None) -> list[str]:
    """Union of committed-since, staged, unstaged, and untracked paths."""
    paths: set[str] = set()
    if since:
        paths.update(_git("diff", "--name-only", f"{since}...HEAD"))
    paths.update(_git("diff", "--name-only"))
    paths.update(_git("diff", "--name-only", "--cached"))
    paths.update(_git("ls-files", "--others", "--exclude-standard"))
    return sorted(paths)


def select_modules(paths: list[str]) -> tuple[list[Path], list[str]]:
    """Map changed paths to test modules that mention them.

    Returns (modules, unmatched) so the caller can report changed files that
    selected nothing -- silence there is a coverage gap worth seeing, not a
    pass.
    """
    all_tests = sorted(TEST_DIR.rglob("test_*.py"))
    sources = {p: p.read_text(encoding="utf-8", errors="replace") for p in all_tests}

    selected: set[Path] = set()
    unmatched: list[str] = []
    for rel in paths:
        p = Path(rel)
        if p.suffix == ".py" and p.parts[:2] == ("tests", "python"):
            candidate = REPO_ROOT / rel
            if candidate.exists():
                selected.add(candidate)
            continue
        stem = p.stem
        if not stem:
            continue
        hits = {t for t, src in sources.items() if stem in src}
        if hits:
            selected |= hits
        else:
            unmatched.append(rel)
    return sorted(selected), unmatched


def isolated_env(base: Path) -> dict[str, str]:
    """Mirror run_isolated_test_env() in tests/run.sh.

    This is load-bearing, not hygiene: ~20 live controllers share the real
    journal, ledger, and task store, and an unisolated module writes to them.
    Kept deliberately in the same shape as the shell function so the two can be
    diffed by eye when either changes.
    """
    env = dict(os.environ)
    for unset in (
        "GOALFLIGHT_STEER_FILE",
        "GOALFLIGHT_ALLOW_EXTERNAL_STEER_FILE",
        "GOALFLIGHT_ISOLATED_TEST_FILE",
    ):
        env.pop(unset, None)
    env["GOALFLIGHT_CAPACITY_CONF"] = env.get("GOALFLIGHT_CAPACITY_CONF") or "/dev/null"
    env["GOALFLIGHT_MESSAGES_DIR"] = env.get("GOALFLIGHT_MESSAGES_DIR") or str(base / "messages")
    env["GOALFLIGHT_JOURNAL_DIR"] = env.get("GOALFLIGHT_JOURNAL_DIR") or str(base / "journal")
    env["GOALFLIGHT_TASK_STORE_DIR"] = env.get("GOALFLIGHT_TASK_STORE_DIR") or str(base / "task-store")
    pidfiles = env.get("GOAL_FLIGHT_PIDFILE_DIR") or env.get("GOALFLIGHT_PIDFILE_DIR") or str(base / "pids")
    env["GOAL_FLIGHT_PIDFILE_DIR"] = pidfiles
    env["GOALFLIGHT_PIDFILE_DIR"] = pidfiles
    env["XDG_STATE_HOME"] = str(base / "xdg")
    return env


def run_module(module: Path, timeout: float) -> tuple[Path, str, str]:
    # Each module gets its own isolation base. tests/run.sh can share one
    # because it does not run modules concurrently; here they would collide on
    # dispatch ids and ledger records inside a shared state dir.
    base = Path(tempfile.mkdtemp(prefix="gf-affected-mod-"))
    if has_main_driver(module):
        argv = [sys.executable, str(module)]
    else:
        argv = [sys.executable, "-m", "pytest", str(module), "-q", "-p", "no:cacheprovider"]
    try:
        proc = subprocess.run(
            argv,
            cwd=str(REPO_ROOT),
            env=isolated_env(base),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return module, INCOMPLETE, f"exceeded {timeout:g}s"
    except OSError as exc:
        return module, INCOMPLETE, f"could not run: {exc}"
    finally:
        shutil.rmtree(base, ignore_errors=True)
    if proc.returncode == 0:
        return module, PASSED, ""
    # A negative return code is a signal, not a test verdict: the module never
    # reached its own pass/fail decision, so it cannot be reported as failed.
    if proc.returncode < 0:
        return module, INCOMPLETE, f"killed by signal {-proc.returncode}"
    tail = (proc.stdout + proc.stderr).strip().splitlines()
    return module, FAILED, tail[-1] if tail else f"exit {proc.returncode}"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--since", default="main", help="compare against this ref (default: main)")
    # Explicit module list makes a baseline comparison possible: the same set
    # can be run in a checkout of the pre-change commit, where --since would
    # select nothing. Without it the gate reports failures; with it, the
    # difference between two runs reports regressions.
    ap.add_argument("modules", nargs="*", help="explicit module paths (skips selection)")
    ap.add_argument("--list", action="store_true", help="print the selected modules and exit")
    ap.add_argument("--timeout", type=float, default=300.0, help="per-module timeout in seconds")
    ap.add_argument("--jobs", type=int, default=0, help="parallel modules (default: cpus-2, max 4)")
    ap.add_argument("--no-rerun", action="store_true", help="do not re-run non-passing modules serially")
    args = ap.parse_args()

    since = None if args.since in ("", "none") else args.since
    if since and not _git("rev-parse", "--verify", since):
        print(f"goalflight_affected_tests: unknown ref {since!r}", file=sys.stderr)
        return 2

    if args.modules:
        modules = [Path(m) if Path(m).is_absolute() else REPO_ROOT / m for m in args.modules]
        missing = [m for m in modules if not m.is_file()]
        if missing:
            for m in missing:
                print(f"no such module: {m}", file=sys.stderr)
            return 2
        paths, unmatched = [], []
    else:
        paths = changed_paths(since)
        if not paths:
            print("no changed files; nothing to run")
            return 0
        modules, unmatched = select_modules(paths)
    total_modules = len(list(TEST_DIR.rglob("test_*.py")))

    if args.list:
        # An empty selection must never print as silence: "no output" reads as
        # "all clear" when it actually means "nothing would be exercised".
        if not modules:
            print(f"NO MODULE SELECTED for {len(paths)} changed file(s): {', '.join(paths)}")
            return 1
        for m in modules:
            print(m.relative_to(REPO_ROOT))
        return 0

    if not modules:
        print(f"{len(paths)} changed file(s) selected no test module:")
        for rel in unmatched:
            print(f"  {rel}")
        print("NOT A PASS -- no coverage was exercised. Run ./tests/run.sh.")
        return 1

    jobs = args.jobs or max(1, min(4, (os.cpu_count() or 4) - 2))
    results: list[tuple[Path, str, str]] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=jobs) as pool:
        futures = [pool.submit(run_module, m, args.timeout) for m in modules]
        for fut in concurrent.futures.as_completed(futures):
            results.append(fut.result())

    # Two parallel passes over identical code disagreed by nine modules on a
    # box that was also running nine workers: several of these modules spawn
    # subprocesses and time out under load rather than failing on their merits.
    # A gate that answers differently each run is not a gate, so anything that
    # did not pass is re-run once, alone, and only a second non-pass counts.
    suspects = [r for r in results if r[1] != PASSED]
    if suspects and not args.no_rerun:
        print(f"re-running {len(suspects)} non-passing module(s) serially...")
        confirmed: list[tuple[Path, str, str]] = []
        for module, _state, _detail in suspects:
            again = run_module(module, args.timeout)
            confirmed.append(again)
            label = "CLEARED ON RERUN" if again[1] == PASSED else again[1].upper()
            first = again[2].strip().splitlines()[0] if again[2].strip() else ""
            print(f"{label:24} {module.relative_to(REPO_ROOT)}  {first[:150]}")
        by_module = {m: (m, s, d) for m, s, d in confirmed}
        results = [by_module.get(r[0], r) for r in results]
        cleared = [m for m, s, _ in confirmed if s == PASSED]
        if cleared:
            print(
                f"\n{len(cleared)} module(s) failed the parallel sweep and passed the "
                "shared-base rerun: an isolation-model artefact or load flake, not a "
                "defect. Counted as passing."
            )

    passed = [r for r in results if r[1] == PASSED]
    failed = [r for r in results if r[1] == FAILED]
    incomplete = [r for r in results if r[1] == INCOMPLETE]

    print(
        f"\n{len(passed)} passed, {len(failed)} failed, {len(incomplete)} did-not-complete "
        f"({len(modules)} selected of {total_modules} modules)"
    )
    if unmatched:
        print(f"changed files that selected no module: {', '.join(unmatched)}")
    print(
        f"NOT the full suite: {total_modules - len(modules)} module(s) and every bash/js "
        "test were not run. ./tests/run.sh remains the pre-push gate."
    )
    return 0 if not failed and not incomplete else 1


if __name__ == "__main__":
    raise SystemExit(main())
