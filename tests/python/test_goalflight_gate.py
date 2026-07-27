"""The quiet gate: full stream to disk, honest summary to the caller."""

from __future__ import annotations

import io
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import goalflight_gate as gate  # noqa: E402


def _run(tmp_path: Path, script: str, tail: int = 5) -> tuple[int, str, Path]:
    log = tmp_path / "gate.log"
    out = io.StringIO()
    rc = gate.run_gate(
        [sys.executable, "-c", script], log_path=log, tail=tail, stdout=out
    )
    return rc, out.getvalue(), log


def test_noisy_pass_prints_summary_not_stream(tmp_path: Path) -> None:
    rc, printed, log = _run(
        tmp_path,
        "print('noise\\n' * 500); print('130 passed in 466.10s')",
    )
    assert rc == 0
    assert "GATE PASS" in printed
    assert "130 passed" in printed
    # The point of the wrapper: the caller reads lines, not the stream. The
    # command echo may itself contain the word; the body must not.
    assert len(printed.splitlines()) < 12
    assert sum(1 for line in printed.splitlines() if line.strip() == "noise") == 0
    # Nothing is lost - the full stream is on disk.
    assert log.read_text().count("noise") == 500


def test_failure_shows_failed_lines_and_real_rc(tmp_path: Path) -> None:
    rc, printed, _ = _run(
        tmp_path,
        "print('junk\\n' * 200);"
        "print('FAILED tests/python/test_x.py::test_a - AssertionError');"
        "print('1 failed, 129 passed in 400s');"
        "import sys; sys.exit(1)",
    )
    assert rc == 1
    assert "GATE FAIL (rc=1)" in printed
    assert "FAILED tests/python/test_x.py::test_a" in printed
    assert "1 failed, 129 passed" in printed


def test_unrecognized_output_shows_tail_and_declines_to_say_pass(tmp_path: Path) -> None:
    """A gate with no recognizable summary proves only that a process exited;
    the tail is shown as evidence and the label reports rc, not "PASS"."""
    rc, printed, _ = _run(tmp_path, "print('custom gate ok')")
    assert rc == 0
    assert "custom gate ok" in printed
    assert "GATE PASS" not in printed
    assert "GATE RC=0" in printed


def test_rc_zero_with_failure_lines_is_never_labeled_pass(tmp_path: Path) -> None:
    """rc==0 beside failure-shaped output means something swallowed a failing
    exit. Saying PASS invents coherence the evidence does not contain."""
    rc, printed, _ = _run(
        tmp_path,
        "print('FAILED tests/python/test_x.py::test_a - AssertionError');"
        "print('1 failed, 3 passed in 2.0s')",
    )
    assert rc == 0
    assert "GATE PASS" not in printed
    assert "FAILURE-SHAPED" in printed
    assert "FAILED tests/python/test_x.py::test_a" in printed


def test_agreeing_rc_and_summary_still_say_pass(tmp_path: Path) -> None:
    rc, printed, _ = _run(tmp_path, "print('4 passed in 0.1s')")
    assert rc == 0
    assert "GATE PASS" in printed


def test_exit_code_passthrough_is_exact(tmp_path: Path) -> None:
    rc, printed, _ = _run(tmp_path, "import sys; sys.exit(7)")
    assert rc == 7
    assert "rc=7" in printed


def test_missing_command_reports_instead_of_raising(tmp_path: Path) -> None:
    out = io.StringIO()
    rc = gate.run_gate(
        ["goalflight-no-such-binary"], log_path=tmp_path / "g.log", stdout=out
    )
    assert rc == 127
    assert "command not found" in out.getvalue()


def test_log_path_is_always_reported(tmp_path: Path) -> None:
    rc, printed, log = _run(tmp_path, "print('ok')")
    assert str(log) in printed
