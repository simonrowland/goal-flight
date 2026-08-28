"""Absence of the gitignored local-only python test zone is not-applicable.

`tests/python/ext` is untracked by design and is not materialised in git
worktrees. A gate that FAILs because that zone is missing turns "not
applicable" into a determination. When the zone *is* present, nested
collection must still list the local-only module or the honesty check is
lying.
"""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import stat
import subprocess
import textwrap

REPO_ROOT = Path(__file__).resolve().parents[2]
HONESTY = REPO_ROOT / "tests" / "bash" / "test-ci-gate-honesty.sh"
RUN_SH = REPO_ROOT / "tests" / "run.sh"
EXT_ZONE_REL = Path("tests") / "python" / "ext"
EXT_MODULE_REL = EXT_ZONE_REL / "test_claude_usage.py"
SKIP_REASON_NEEDLE = "gitignored local-only zone, absent from worktrees by construction"

TRACKED_LIST_PATHS = (
    "tests/python/test_ci_mutation_guards.py",
    "tests/python/test_goalflight_gate.py",
    "tests/python/test_script_style_modules.py",
    "tests/js/test_gf_escape.js",
)
EXT_LIST_PATH = EXT_MODULE_REL.as_posix()

_RUNNER_FRAGMENTS = textwrap.dedent(
    """\
    # python3 -m pytest tests/python -q
    # find tests/python -type f -name 'test_*.py'
    # $skip skipped
    # skill_structure_collected
    # -u GOALFLIGHT_ISOLATED_TEST_FILE
    # GOALFLIGHT_MESSAGES_DIR="$_GF_MESSAGES_BASE"
    # GOALFLIGHT_JOURNAL_DIR="$_GF_JOURNAL_BASE"
    # project-<10-hex>
    """
)


def _isolated_env(tmp_path: Path) -> dict[str, str]:
    env = os.environ.copy()
    root = tmp_path / "gf-env"
    mapping = {
        "GOALFLIGHT_JOURNAL_DIR": str(root / "journal"),
        "GOALFLIGHT_STATE_DIR": str(root / "state"),
        "GOALFLIGHT_WAKE_LEDGER": str(root / "wake.ledger"),
        "GOALFLIGHT_MESSAGES_DIR": str(root / "messages"),
        "GOALFLIGHT_TASK_STORE": str(root / "tasks"),
        "GOALFLIGHT_TASK_STORE_DIR": str(root / "task-store"),
        "GOALFLIGHT_PIDFILE_DIR": str(root / "pids"),
        "GOAL_FLIGHT_PIDFILE_DIR": str(root / "pids"),
        "GOALFLIGHT_CAPACITY_CONF": os.devnull,
    }
    for key, value in mapping.items():
        if value != os.devnull:
            Path(value).mkdir(parents=True, exist_ok=True)
        env[key] = value
    return env


def _write_gate_stub(root: Path, listed: tuple[str, ...]) -> None:
    (root / "tests").mkdir(parents=True, exist_ok=True)
    (root / "scripts").mkdir(parents=True, exist_ok=True)
    listed_body = "".join(f"{path}\n" for path in listed)
    (root / "tests" / "run.sh").write_text(
        "#!/usr/bin/env bash\nset -euo pipefail\n"
        + _RUNNER_FRAGMENTS
        + 'if [ "${1:-}" = "--list" ]; then\n'
        + "  cat <<'EOF'\n"
        + listed_body
        + "EOF\n"
        + "  exit 0\n"
        + "fi\n"
        + "exit 0\n",
        encoding="utf-8",
    )
    (root / "tests" / "run.sh").chmod(
        (root / "tests" / "run.sh").stat().st_mode | stat.S_IXUSR
    )
    (root / "scripts" / "goalflight_gate.py").write_text(
        "from pathlib import Path\n"
        "DEFAULT_CMD = [str(Path(__file__).resolve().parents[1] / 'tests' / 'run.sh')]\n",
        encoding="utf-8",
    )


def _write_honesty_copy(root: Path) -> Path:
    dest = root / "tests" / "bash" / "test-ci-gate-honesty.sh"
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(HONESTY, dest)
    return dest


def _make_ext_zone(root: Path) -> None:
    zone = root / EXT_ZONE_REL
    zone.mkdir(parents=True, exist_ok=True)
    (zone / "test_claude_usage.py").write_text(
        "# fixture local-only zone; not the real checkout zone\n",
        encoding="utf-8",
    )


def _run_honesty(root: Path, tmp_path: Path) -> subprocess.CompletedProcess[str]:
    script = _write_honesty_copy(root)
    return subprocess.run(
        ["bash", str(script)],
        cwd=root,
        text=True,
        capture_output=True,
        check=False,
        env=_isolated_env(tmp_path),
    )


def _run_copied_gate(root: Path, tmp_path: Path) -> subprocess.CompletedProcess[str]:
    shutil.copy2(RUN_SH, root / "tests" / "run.sh")
    (root / "tests" / "run.sh").chmod(
        (root / "tests" / "run.sh").stat().st_mode | stat.S_IXUSR
    )
    return subprocess.run(
        ["bash", str(root / "tests" / "run.sh")],
        cwd=root,
        text=True,
        capture_output=True,
        check=False,
        env=_isolated_env(tmp_path),
    )


def test_honesty_skips_when_local_only_zone_is_absent(tmp_path: Path) -> None:
    root = tmp_path / "absent"
    _write_gate_stub(root, TRACKED_LIST_PATHS)
    result = _run_honesty(root, tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert SKIP_REASON_NEEDLE in result.stdout
    assert "omitted tests/python/ext/test_claude_usage.py" not in result.stdout
    assert "CI gate honesty tests passed" in result.stdout


def test_honesty_still_requires_tracked_paths_when_zone_absent(
    tmp_path: Path,
) -> None:
    """Not-applicable for the local-only zone must not skip tracked collection."""
    root = tmp_path / "absent-missing-tracked"
    listed = tuple(path for path in TRACKED_LIST_PATHS if not path.endswith("test_gf_escape.js"))
    _write_gate_stub(root, listed)
    result = _run_honesty(root, tmp_path)
    combined = result.stdout + result.stderr
    assert result.returncode != 0, combined
    assert "omitted tests/js/test_gf_escape.js" in combined
    assert SKIP_REASON_NEEDLE not in combined


def test_honesty_fails_when_zone_present_but_unlistable(tmp_path: Path) -> None:
    root = tmp_path / "present-unlisted"
    _write_gate_stub(root, TRACKED_LIST_PATHS)
    _make_ext_zone(root)
    result = _run_honesty(root, tmp_path)
    assert result.returncode != 0, result.stdout + result.stderr
    combined = result.stdout + result.stderr
    assert "omitted tests/python/ext/test_claude_usage.py" in combined
    assert SKIP_REASON_NEEDLE not in combined


def test_honesty_still_passes_when_zone_present_and_listed(tmp_path: Path) -> None:
    root = tmp_path / "present-listed"
    _write_gate_stub(root, TRACKED_LIST_PATHS + (EXT_LIST_PATH,))
    _make_ext_zone(root)
    result = _run_honesty(root, tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "CI gate honesty tests passed" in result.stdout
    assert SKIP_REASON_NEEDLE not in result.stdout


def test_run_sh_still_runs_honesty_when_local_only_zone_is_absent(
    tmp_path: Path,
) -> None:
    """Absence of tests/python/ext is N/A inside honesty, not a gate skip.

    Replaces test_run_sh_skips_honesty_when_local_only_zone_is_absent, which
    pinned the over-broad run.sh continue that swallowed tracked-path checks.
    """
    root = tmp_path / "gate-absent"
    bash_dir = root / "tests" / "bash"
    bash_dir.mkdir(parents=True)
    (bash_dir / "test-ci-gate-honesty.sh").write_text(
        "#!/usr/bin/env bash\necho HONESTY-RAN\nexit 1\n",
        encoding="utf-8",
    )
    result = _run_copied_gate(root, tmp_path)
    combined = result.stdout + result.stderr
    assert result.returncode != 0, combined
    assert "HONESTY-RAN" in combined
    assert "FAIL  tests/bash/test-ci-gate-honesty.sh" in combined
    assert "SKIP  tests/bash/test-ci-gate-honesty.sh" not in combined


def test_run_sh_still_runs_honesty_when_local_only_zone_is_present(
    tmp_path: Path,
) -> None:
    root = tmp_path / "gate-present"
    bash_dir = root / "tests" / "bash"
    bash_dir.mkdir(parents=True)
    (bash_dir / "test-ci-gate-honesty.sh").write_text(
        "#!/usr/bin/env bash\necho HONESTY-RAN\nexit 1\n",
        encoding="utf-8",
    )
    _make_ext_zone(root)
    result = _run_copied_gate(root, tmp_path)
    combined = result.stdout + result.stderr
    assert result.returncode != 0, combined
    assert "HONESTY-RAN" in combined
    assert "FAIL  tests/bash/test-ci-gate-honesty.sh" in combined
    assert "SKIP  tests/bash/test-ci-gate-honesty.sh" not in combined
