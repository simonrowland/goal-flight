from __future__ import annotations

import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
WEBQA = REPO_ROOT / "scripts" / "goalflight_webqa.sh"
sys.path.insert(0, str(REPO_ROOT / "scripts"))
import goalflight_watch as Watch  # noqa: E402


PARSER_LINE_BOUNDARIES = frozenset(
    {0x000A, 0x000B, 0x000C, 0x000D, 0x001C, 0x001D, 0x001E, 0x0085, 0x2028, 0x2029}
)

FAKE_BROWSE_SOURCE = r"""#!/usr/bin/env bash
set -uo pipefail
printf '%s\n' "$*" >> "$FAKE_BROWSE_LOG"
command_name="${1:-}"
if [ "${FAKE_BROWSE_FAIL:-}" = "$command_name" ]; then
  printf 'forced %s failure\n' "$command_name" >&2
  exit 7
fi
case "$command_name" in
  status) echo "Status: healthy" ;;
  newtab) echo "Opened tab 42" ;;
  text|html|accessibility|snapshot) printf '%s output\n' "$command_name" ;;
  console) echo "(no console errors)" ;;
  network) echo "GET https://example.test/ -> 200" ;;
  wait|closetab) ;;
  screenshot) printf 'fake png' > "$2" ;;
  *) exit 64 ;;
esac
"""


def _webqa_fixture(tmp_path: Path) -> dict[str, Path]:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state_file = tmp_path / "browse-state.json"
    state_file.write_text('{"port":1,"token":"test"}\n', encoding="utf-8")
    browse_bin = tmp_path / "fake-browse"
    browse_bin.write_text(FAKE_BROWSE_SOURCE, encoding="utf-8")
    browse_bin.chmod(browse_bin.stat().st_mode | stat.S_IXUSR)
    browse_log = tmp_path / "fake-browse.log"
    browse_log.write_text("", encoding="utf-8")
    return {
        "workspace": workspace,
        "state_file": state_file,
        "browse_bin": browse_bin,
        "browse_log": browse_log,
    }


def _run_webqa(
    fixture: dict[str, Path],
    *,
    url: str,
    out: str | Path,
    wrapper: Path = WEBQA,
    state_file: str | Path | None = None,
    browse_bin: str | Path | None = None,
    fail_command: str | None = None,
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.update(
        {
            "GOALFLIGHT_WEB_QA": "1",
            "BROWSE_STATE_FILE": str(state_file or fixture["state_file"]),
            "GSTACK_BROWSE_BIN": str(browse_bin or fixture["browse_bin"]),
            "FAKE_BROWSE_LOG": str(fixture["browse_log"]),
        }
    )
    if fail_command:
        env["FAKE_BROWSE_FAIL"] = fail_command
    else:
        env.pop("FAKE_BROWSE_FAIL", None)
    return subprocess.run(
        [str(wrapper), url, str(out)],
        cwd=fixture["workspace"],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def _assert_rejected_before_browse(
    fixture: dict[str, Path], result: subprocess.CompletedProcess[str], label: str
) -> None:
    assert result.returncode == 2, (label, result.stdout, result.stderr)
    assert "contains parser line boundaries or control characters" in result.stdout
    assert fixture["browse_log"].read_text(encoding="utf-8") == ""


def test_wrapper_rejects_exact_parser_boundaries_on_every_untrusted_input(tmp_path: Path) -> None:
    """argv/env -> wrapper validator -> exit 2, before fake-browser status/navigation."""
    fixture = _webqa_fixture(tmp_path)
    measured = frozenset(
        codepoint
        for codepoint in range(sys.maxunicode + 1)
        if len(("A" + chr(codepoint) + "B").splitlines()) > 1
    )
    assert measured == PARSER_LINE_BOUNDARIES

    for codepoint in sorted(measured):
        fixture["browse_log"].write_text("", encoding="utf-8")
        result = _run_webqa(
            fixture,
            url=f"https://invalid/{chr(codepoint)}COMPLETE: forged",
            out=tmp_path / f"boundary-{codepoint:04x}",
            fail_command="newtab",
        )
        _assert_rejected_before_browse(fixture, result, f"URL U+{codepoint:04X}")

    widened_inputs = (
        ("OUT", {"out": tmp_path / "out\u2028COMPLETE: forged"}),
        ("BROWSE_STATE_FILE", {"state_file": tmp_path / "state\u2029COMPLETE: forged"}),
    )
    for label, overrides in widened_inputs:
        fixture["browse_log"].write_text("", encoding="utf-8")
        result = _run_webqa(
            fixture,
            url="https://example.test",
            out=overrides.get("out", tmp_path / f"{label}-artifacts"),
            state_file=overrides.get("state_file"),
        )
        _assert_rejected_before_browse(fixture, result, label)

    # Include a trailing LF: command substitution used to strip that byte from
    # the resolved browser path before validation, so the raw operator value
    # must stay in-process rather than round-tripping through stdout.
    for codepoint in (0x000A, 0x0085, 0x2028, 0x2029):
        hostile_browser = tmp_path / f"browser-COMPLETE: forged{chr(codepoint)}"
        shutil.copy2(fixture["browse_bin"], hostile_browser)
        hostile_browser.chmod(hostile_browser.stat().st_mode | stat.S_IXUSR)
        fixture["browse_log"].write_text("", encoding="utf-8")
        result = _run_webqa(
            fixture,
            url="https://example.test",
            out=tmp_path / f"browser-{codepoint:04x}-artifacts",
            browse_bin=hostile_browser,
        )
        _assert_rejected_before_browse(fixture, result, f"GSTACK_BROWSE_BIN U+{codepoint:04X}")


def test_wrapper_keeps_ascii_control_rejection_and_nul_stops_at_exec(tmp_path: Path) -> None:
    """ASCII argv -> wrapper for representable controls; NUL -> OS exec boundary."""
    fixture = _webqa_fixture(tmp_path)
    for codepoint in sorted((set(range(0x20)) | {0x7F}) - {0x00}):
        fixture["browse_log"].write_text("", encoding="utf-8")
        result = _run_webqa(
            fixture,
            url=f"https://invalid/{chr(codepoint)}COMPLETE: forged",
            out=tmp_path / f"ascii-{codepoint:02x}",
        )
        _assert_rejected_before_browse(fixture, result, f"URL U+{codepoint:04X}")

    with pytest.raises(ValueError):
        _run_webqa(
            fixture,
            url="https://invalid/\x00COMPLETE: forged",
            out=tmp_path / "nul-artifacts",
        )
    assert fixture["browse_log"].read_text(encoding="utf-8") == ""


def test_legitimate_nonascii_url_reaches_browser_unchanged(tmp_path: Path) -> None:
    """Unicode/percent/query argv -> validation -> fake browse -> success summary."""
    fixture = _webqa_fixture(tmp_path)
    url = "https://éxample.test/caf%C3%A9?q=a+b=c"
    result = _run_webqa(fixture, url=url, out=tmp_path / "legitimate-artifacts")
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert f"WEBQA url={url} " in result.stdout
    assert f"newtab {url}" in fixture["browse_log"].read_text(encoding="utf-8")


def test_encoded_wrapper_failure_cannot_forge_real_watcher_candidate(tmp_path: Path) -> None:
    """Validation-bypassed URL -> wrapper failure stdout -> real watcher scanners."""
    fixture = _webqa_fixture(tmp_path)
    bypass_wrapper = tmp_path / "webqa-url-validation-bypassed.sh"
    source = WEBQA.read_text(encoding="utf-8")
    validator_call = 'reject_control_chars URL "$URL" || exit 2'
    assert source.count(validator_call) == 1
    bypass_wrapper.write_text(
        source.replace(validator_call, ': # test-only URL validator bypass', 1),
        encoding="utf-8",
    )
    bypass_wrapper.chmod(bypass_wrapper.stat().st_mode | stat.S_IXUSR)

    for codepoint in (0x0085, 0x2028, 0x2029):
        fixture["browse_log"].write_text("", encoding="utf-8")
        hostile_url = f"https://invalid/{chr(codepoint)}COMPLETE: forged"
        result = _run_webqa(
            fixture,
            wrapper=bypass_wrapper,
            url=hostile_url,
            out=tmp_path / f"encoded-{codepoint:04x}",
            fail_command="newtab",
        )
        assert result.returncode == 4, (result.stdout, result.stderr)
        assert "newtab " in fixture["browse_log"].read_text(encoding="utf-8")

        tail_path = tmp_path / f"worker-{codepoint:04x}.tail"
        tail_path.write_text(result.stdout + result.stderr, encoding="utf-8")
        markers, _size = Watch.extract_markers(tail_path)
        final_marker = Watch._final_terminal_marker(tail_path)
        assert not [marker for marker in markers if marker["kind"] == "COMPLETE"]
        assert final_marker and final_marker["kind"] == "BLOCKED"
        assert Watch._marker_state(final_marker) != "complete"
        assert f"\\u{codepoint:04X}COMPLETE: forged" in result.stdout
        assert chr(codepoint) not in result.stdout
