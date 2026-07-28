"""Production-shaped coverage for scannable ``relay --new`` headlines."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "goalflight_messages.py"
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import goalflight_messages as msg  # noqa: E402


def _post_env(
    tmp_path: Path,
    text: str,
    *,
    subject: str | None = None,
    dispatch_id: str = "d1",
    msg_type: str = "status",
    adapter: str = "codex",
) -> dict:
    argv = [
        sys.executable,
        str(SCRIPT),
        "--messages-dir",
        str(tmp_path / "messages"),
        "--fleet-dir",
        str(tmp_path / "fleet"),
        "post",
        "--dispatch-id",
        dispatch_id,
        "--type",
        msg_type,
        "--text",
        text,
        "--node",
        "local",
        "--adapter",
        adapter,
        "--transport",
        "controller",
        "--json",
    ]
    if subject is not None:
        argv.extend(["--subject", subject])
    posted = subprocess.run(
        argv,
        text=True,
        capture_output=True,
        check=False,
    )
    assert posted.returncode == 0, posted.stderr
    return json.loads(posted.stdout)["envelope"]


def _run_relay(tmp_path: Path, *, bodies: bool = False) -> subprocess.CompletedProcess[str]:
    fleet_dir = tmp_path / "fleet"
    fleet_dir.mkdir(exist_ok=True)
    argv = [
        sys.executable,
        str(SCRIPT),
        "--messages-dir",
        str(tmp_path / "messages"),
        "--fleet-dir",
        str(fleet_dir),
        "relay",
        "--new",
        "--all-projects",
    ]
    if bodies:
        argv.append("--bodies")
    return subprocess.run(argv, text=True, capture_output=True, check=False)


def test_explicit_subject_wins_over_body(tmp_path: Path) -> None:
    envelope = _post_env(
        tmp_path,
        "line one\nline two",
        subject="Gate green",
    )
    assert msg.envelope_headline(envelope) == "Gate green"


def test_headline_falls_back_to_first_two_meaningful_lines(tmp_path: Path) -> None:
    envelope = _post_env(
        tmp_path,
        "\n\n  RESOLVED — root cause found  \nseat daemon PATH\nthird line",
    )
    assert (
        msg.envelope_headline(envelope)
        == "RESOLVED — root cause found / seat daemon PATH"
    )


def test_headline_is_bounded_and_single_line(tmp_path: Path) -> None:
    head = msg.envelope_headline(_post_env(tmp_path, "x" * 500))
    assert len(head) <= msg.HEADLINE_MAX
    assert "\n" not in head

    multi = msg.envelope_headline(
        _post_env(tmp_path, "a\nb\nc", dispatch_id="multi")
    )
    assert multi == "a / b"


def test_empty_body_is_labelled_not_blank(tmp_path: Path) -> None:
    assert msg.envelope_headline(_post_env(tmp_path, "")) == "(no text)"
    assert msg.envelope_headline({"payload": None}) == "(no text)"


def test_listing_carries_size_without_dumping_body(tmp_path: Path) -> None:
    fragment = "DISTINCTIVE-BODY-FRAGMENT:"
    body = fragment + ("y" * (2048 - len(fragment)))
    envelope = _post_env(
        tmp_path,
        body,
        subject="Big one",
        dispatch_id="proj",
    )
    out = msg.format_envelope_headlines([envelope])
    assert out == "proj #1 [status] from codex: Big one  (2048c)"
    assert fragment not in out


def test_malformed_entries_do_not_break_the_listing(tmp_path: Path) -> None:
    envelope = _post_env(tmp_path, "ok", dispatch_id="d")
    out = msg.format_envelope_headlines(["not-a-dict", None, envelope])
    assert out.count("\n") == 0 and "d #1" in out


def test_from_prefers_recorded_source_over_inbox_id(tmp_path: Path) -> None:
    envelope = _post_env(tmp_path, "body", dispatch_id="proj-inbox")
    assert msg.envelope_from(envelope) == "codex"

    envelope["source"]["adapter"] = "unknown"
    assert msg.envelope_from(envelope) == "local"
    envelope["source"] = {}
    assert msg.envelope_from(envelope) == "proj-inbox"


def test_default_and_bodies_cli_paths_round_trip_subject(tmp_path: Path) -> None:
    fragment = "CLI-BODY-LEAK-SENTINEL:"
    body = fragment + ("z" * (2048 - len(fragment)))
    envelope = _post_env(
        tmp_path,
        body,
        subject="Gate green",
        dispatch_id="kiln",
    )

    headlines = _run_relay(tmp_path)
    assert headlines.returncode == 0, headlines.stderr
    assert headlines.stdout.splitlines() == [
        "kiln #1 [status] from codex: Gate green  (2048c)",
        "bodies: re-run with --bodies, or read one inbox with `read`",
        "unseen counts: kiln=1",
    ]
    assert fragment not in headlines.stdout

    bodies = _run_relay(tmp_path, bodies=True)
    assert bodies.returncode == 0, bodies.stderr
    relayed = json.loads(bodies.stdout.splitlines()[0])
    assert relayed == [envelope]
    assert relayed[0]["payload"]["subject"] == "Gate green"


def test_default_cli_sanitizes_source_and_body_controls(tmp_path: Path) -> None:
    _post_env(
        tmp_path,
        "worker said \x1b[2Jclear",
        dispatch_id="mail",
        adapter="codex\nFORGED",
    )

    result = _run_relay(tmp_path)

    assert result.returncode == 0, result.stderr
    assert "\x1b" not in result.stdout
    assert "from codex FORGED:" in result.stdout
    assert r"\x1b[2J" in result.stdout
    assert "FORGED\n" not in result.stdout


def test_relay_new_sanitizes_full_stdout_structure(tmp_path: Path) -> None:
    body = "first line\nFORGED-BODY\x1b[2J\x9b31m"
    _post_env(
        tmp_path,
        body,
        dispatch_id="safe\nFORGED-COUNT",
    )

    result = _run_relay(tmp_path)

    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == [
        (
            r"safe FORGED-COUNT #1 [status] from codex: "
            rf"first line / FORGED-BODY\x1b[2J\x9b31m  ({len(body)}c)"
        ),
        "bodies: re-run with --bodies, or read one inbox with `read`",
        "unseen counts: safe FORGED-COUNT=1",
    ]
    assert "\x1b" not in result.stdout
    assert "\x9b" not in result.stdout


def test_one_sanitizer_covers_dispatch_type_and_subject(tmp_path: Path) -> None:
    envelope = _post_env(
        tmp_path,
        "body",
        subject="Gate\n\x9b2Jgreen",
        dispatch_id="safe\nFORGED",
        msg_type="status\rINJECTED",
    )

    out = msg.format_envelope_headlines([envelope])

    assert out.count("\n") == 0
    assert "\x9b" not in out
    assert "safe FORGED" in out
    assert "[status INJECTED]" in out
    assert r"Gate \x9b2Jgreen" in out


def main() -> None:
    tests = (
        test_explicit_subject_wins_over_body,
        test_headline_falls_back_to_first_two_meaningful_lines,
        test_headline_is_bounded_and_single_line,
        test_empty_body_is_labelled_not_blank,
        test_listing_carries_size_without_dumping_body,
        test_malformed_entries_do_not_break_the_listing,
        test_from_prefers_recorded_source_over_inbox_id,
        test_default_and_bodies_cli_paths_round_trip_subject,
        test_default_cli_sanitizes_source_and_body_controls,
        test_relay_new_sanitizes_full_stdout_structure,
        test_one_sanitizer_covers_dispatch_type_and_subject,
    )
    for test in tests:
        with tempfile.TemporaryDirectory() as td:
            test(Path(td))
        print(f"PASS {test.__name__}")


if __name__ == "__main__":
    main()
