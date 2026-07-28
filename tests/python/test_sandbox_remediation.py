"""A misconfigured dispatch must say how to re-run it.

Eight dispatches were lost in one session to sandbox flag mistakes: six probes
with a cwd inside /tmp, two reviews with no writable TMPDIR. In every case the
worker never started, so the only useful output is the corrected invocation --
but the verdict line just said "blocked", and the reason sat in a status file
nobody reads mid-flight.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import goalflight_status as status  # noqa: E402

# Verbatim reasons emitted by real failed dispatches.
CWD_IN_TEMP = (
    "os sandbox cannot enforce workspace boundaries when cwd is inside allowed "
    "temp root '/tmp'; move the worktree or use off"
)
NO_TMPDIR = (
    "focused pytest could not start because the read-only sandbox exposes no "
    "writable temporary directory"
)
UNTRUSTED_DIR = "Not inside a trusted directory and --skip-git-repo-check was not specified."
READONLY_WRITE = "patch rejected: writing is blocked by read-only sandbox"


def test_each_known_signature_yields_a_corrective_invocation():
    for reason, expect in (
        (CWD_IN_TEMP, "--cwd"),
        (NO_TMPDIR, "cannot be dispatched read-only"),
        (UNTRUSTED_DIR, "--skip-git-repo-check"),
        (READONLY_WRITE, "--os-sandbox workspace-write"),
    ):
        remedy = status.sandbox_remediation(reason)
        assert remedy is not None, reason
        assert expect in remedy, (reason, remedy)


def test_unrelated_text_and_empty_input_yield_nothing():
    """The hint must not fire on ordinary failures, or it becomes noise that
    trains readers to ignore it."""
    assert status.sandbox_remediation("AssertionError: expected 3 got 4") is None
    assert status.sandbox_remediation("") is None
    assert status.sandbox_remediation(None) is None


def test_verdict_line_tells_the_controller_to_kill_and_retry():
    row = {
        "dispatch_id": "curcap-1",
        "state": "blocked_os_sandbox",
        "block_reason": CWD_IN_TEMP,
    }
    line = status._wait_verdict_line(row)
    assert "DISPATCH MISCONFIGURED" in line
    assert "kill and retry" in line
    assert "--cwd" in line


def test_unrecognized_sandbox_block_still_flags_misconfiguration():
    """An unknown sandbox refusal is still a config fault, not a worker fault."""
    row = {"dispatch_id": "x", "state": "blocked_os_sandbox", "block_reason": "novel refusal"}
    line = status._wait_verdict_line(row)
    assert "MISCONFIGURED" in line and "re-dispatch" in line


def test_ordinary_blocked_worker_is_untouched():
    """A real BLOCKED escalation must keep its marker rendering, not be
    relabelled as a config error."""
    row = {
        "dispatch_id": "wdg",
        "state": "blocked",
        "marker_kind": "BLOCKED",
        "marker_headline": "sandbox refused bind",
        "block_reason": "worker reported a blocker",
    }
    line = status._wait_verdict_line(row)
    assert "[BLOCKED]" in line and "MISCONFIGURED" not in line


def test_checkpoint_verdict_still_distinguishes_itself():
    row = {
        "dispatch_id": "chk",
        "state": "blocked",
        "marker_kind": "USER-NEED",
        "marker_headline": "landing checkpoint - session abc",
    }
    line = status._wait_verdict_line(row)
    assert "[USER-NEED]" in line and "MISCONFIGURED" not in line


def test_tmpdir_remedy_does_not_recommend_a_flag_that_changes_nothing():
    """--os-sandbox read-only and --read-only both resolve to
    `codex --sandbox read-only`. Recommending one as a fix for the other is
    advice that cannot work, and this hint shipped that way once."""
    remedy = status.sandbox_remediation(NO_TMPDIR)
    assert "--os-sandbox read-only" not in remedy.replace(
        "--os-sandbox read-only` blocks", ""
    ) or "resolve to" in remedy
    assert "workspace-write" in remedy or "neither flag" in remedy
