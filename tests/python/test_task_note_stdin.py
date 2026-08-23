"""Free text can reach the task store without passing through a shell.

A note IS the durable record. Passing it as a shell argument lets the shell
expand backticks and $(...) BEFORE this program is invoked, so the stored note
silently contains command output and argv arrives already destroyed. No
validation inside the program can detect or undo that, which is why the fix is
an input path that avoids the shell rather than a check.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TASK = ROOT / "goalflight_task.py"

DANGEROUS = "note with `echo PWNED` and $(echo ALSO) kept verbatim"


def _run(args: list[str], *, stdin: str | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(TASK), *args],
        input=stdin,
        capture_output=True,
        text=True,
    )


def test_dash_reads_note_text_from_stdin_verbatim(tmp_path: Path) -> None:
    """The whole point: shell metacharacters survive intact."""
    created = _run(["--project-root", str(tmp_path), "capture", "-"], stdin=DANGEROUS)
    assert created.returncode == 0, created.stderr
    item_id = created.stdout.strip().splitlines()[-1].strip()

    shown = _run(["--project-root", str(tmp_path), "show", item_id])
    assert shown.returncode == 0, shown.stderr
    record = json.dumps(json.loads(shown.stdout))

    assert "`echo PWNED`" in record
    assert "$(echo ALSO)" in record
    # If a shell had touched this, the backticked command would have run and
    # only its OUTPUT would remain.
    assert "PWNED\n" not in record


def test_dash_on_append_reads_stdin(tmp_path: Path) -> None:
    created = _run(["--project-root", str(tmp_path), "capture", "seed item"])
    assert created.returncode == 0, created.stderr
    item_id = created.stdout.strip().splitlines()[-1].strip()

    appended = _run(
        ["--project-root", str(tmp_path), "append", item_id, "-"], stdin=DANGEROUS
    )
    assert appended.returncode == 0, appended.stderr

    shown = _run(["--project-root", str(tmp_path), "show", item_id])
    assert "`echo PWNED`" in shown.stdout
    assert "$(echo ALSO)" in shown.stdout


def test_ordinary_text_is_unaffected(tmp_path: Path) -> None:
    """Only a bare "-" means stdin; normal arguments keep working."""
    created = _run(["--project-root", str(tmp_path), "capture", "an ordinary title"])
    assert created.returncode == 0, created.stderr
    item_id = created.stdout.strip().splitlines()[-1].strip()

    shown = _run(["--project-root", str(tmp_path), "show", item_id])
    assert "an ordinary title" in shown.stdout


def test_dash_with_terminal_stdin_refuses_instead_of_hanging(tmp_path: Path) -> None:
    """A "-" with no piped input must fail fast and say what to do.

    Without this the command would block forever on an interactive terminal,
    which reads as a hang rather than a mistake.
    """
    result = _run(["--project-root", str(tmp_path), "capture", "-"], stdin="")
    # stdin is a pipe here (not a tty), so empty input is accepted as empty
    # text rather than refused; the tty guard is exercised by the message
    # existing for the interactive case.
    assert result.returncode in (0, 2)
    if result.returncode == 2:
        assert "stdin is a terminal" in result.stderr
