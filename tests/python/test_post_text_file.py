"""CLI ``post --text-file`` and the sendable ``--type`` working set.

Hermetic: isolated GOALFLIGHT_* dirs, no live journal, no fleet re-arm.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(ROOT / "tests" / "python"))

import goalflight_messages as messages  # noqa: E402
from support import isolated_machine_env  # noqa: E402

SCRIPT = SCRIPTS / "goalflight_messages.py"
BODY_WITH_QUOTES = "don't run `git status` or $(hostname) here"

JUNK_TYPES = ("advisory", "note", "defect-notice", "qa-bug", "controller-note")
HELP_MUST_LIST = (
    "controller-notice",
    "controller-question",
    "controller-answer",
    "controller-coordination",
    "coordination",
    "notice",
    "merge-request",
    "patch",
    "finding",
    "result",
    "blocked",
    "ack",
    "status",
    "user_need",
    "steering",
)
HELP_MUST_NOT_LIST_AS_CHOICE = (
    "advisory",
    "note",
    "defect-notice",
    "qa-bug",
    "controller-note",
    "user_confirm",
    "monitor",
)


def _isolate(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict[str, str]:
    env = os.environ.copy()
    env.update(isolated_machine_env(tmp_path))
    env["GOALFLIGHT_TEST_MODE"] = "1"
    env.pop("GOALFLIGHT_DISPATCH_ID", None)
    env.pop("GOALFLIGHT_CONTROLLER_LABEL", None)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    for key in ("GOALFLIGHT_DISPATCH_ID", "GOALFLIGHT_CONTROLLER_LABEL"):
        monkeypatch.delenv(key, raising=False)
    return env


def _post(
    env: dict[str, str],
    args: list[str],
    *,
    cwd: Path,
    stdin: str | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--messages-dir",
            env["GOALFLIGHT_MESSAGES_DIR"],
            "--fleet-dir",
            env["GOALFLIGHT_FLEET_DIR"],
            "post",
            *args,
        ],
        cwd=str(cwd),
        env=env,
        input=stdin,
        capture_output=True,
        text=True,
        check=False,
    )


def _posted_text(env: dict[str, str], dispatch_id: str) -> str:
    path = messages.inbox_path(Path(env["GOALFLIGHT_MESSAGES_DIR"]), dispatch_id)
    envelopes = messages.read_envelopes(path)
    assert envelopes, path
    return str(envelopes[-1]["payload"]["text"])


def test_text_file_round_trip_keeps_backticks_and_apostrophe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = _isolate(monkeypatch, tmp_path)
    body = tmp_path / "body.txt"
    body.write_text(BODY_WITH_QUOTES, encoding="utf-8")
    posted = _post(
        env,
        [
            "--dispatch-id",
            "file-body",
            "--type",
            "status",
            "--text-file",
            str(body),
        ],
        cwd=tmp_path,
    )
    assert posted.returncode == 0, posted.stderr
    assert _posted_text(env, "file-body") == BODY_WITH_QUOTES


@pytest.mark.parametrize("stdin_name", ["-", "/dev/stdin"])
def test_text_file_stdin_aliases_read_heredoc_body(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stdin_name: str
) -> None:
    env = _isolate(monkeypatch, tmp_path)
    dispatch_id = "stdin-dash" if stdin_name == "-" else "stdin-dev-stdin"
    posted = _post(
        env,
        [
            "--dispatch-id",
            dispatch_id,
            "--type",
            "status",
            "--text-file",
            stdin_name,
        ],
        cwd=tmp_path,
        stdin=BODY_WITH_QUOTES,
    )
    assert posted.returncode == 0, posted.stderr
    assert _posted_text(env, dispatch_id) == BODY_WITH_QUOTES


def test_text_and_text_file_are_mutually_exclusive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = _isolate(monkeypatch, tmp_path)
    body = tmp_path / "body.txt"
    body.write_text("from-file", encoding="utf-8")
    posted = _post(
        env,
        [
            "--dispatch-id",
            "both-text",
            "--type",
            "status",
            "--text",
            "from-argv",
            "--text-file",
            str(body),
        ],
        cwd=tmp_path,
    )
    assert posted.returncode == 2, posted.stdout
    assert "--text and --text-file are mutually exclusive" in posted.stderr
    assert "--text-file /dev/stdin" in posted.stderr
    assert not messages.inbox_path(Path(env["GOALFLIGHT_MESSAGES_DIR"]), "both-text").is_file()


def test_payload_and_text_file_are_mutually_exclusive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = _isolate(monkeypatch, tmp_path)
    body = tmp_path / "body.txt"
    body.write_text("from-file", encoding="utf-8")
    posted = _post(
        env,
        [
            "--dispatch-id",
            "both-payload",
            "--type",
            "status",
            "--payload",
            json.dumps({"text": "from-json"}),
            "--text-file",
            str(body),
        ],
        cwd=tmp_path,
    )
    assert posted.returncode == 2, posted.stdout
    assert "--payload and --text-file are mutually exclusive" in posted.stderr
    assert "--text-file /dev/stdin" in posted.stderr
    assert not messages.inbox_path(
        Path(env["GOALFLIGHT_MESSAGES_DIR"]), "both-payload"
    ).is_file()


@pytest.mark.parametrize("msg_type", ["ack", "result", "blocked"])
def test_worker_working_set_types_still_post(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, msg_type: str
) -> None:
    env = _isolate(monkeypatch, tmp_path)
    posted = _post(
        env,
        [
            "--dispatch-id",
            f"worker-{msg_type}",
            "--type",
            msg_type,
            "--text",
            msg_type,
        ],
        cwd=tmp_path,
    )
    assert posted.returncode == 0, posted.stderr
    path = messages.inbox_path(Path(env["GOALFLIGHT_MESSAGES_DIR"]), f"worker-{msg_type}")
    assert messages.read_envelopes(path)[0]["type"] == msg_type


def test_text_flag_still_posts_short_strings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = _isolate(monkeypatch, tmp_path)
    posted = _post(
        env,
        [
            "--dispatch-id",
            "short-text",
            "--type",
            "status",
            "--text",
            "short",
        ],
        cwd=tmp_path,
    )
    assert posted.returncode == 0, posted.stderr
    assert _posted_text(env, "short-text") == "short"


@pytest.mark.parametrize("junk", JUNK_TYPES)
def test_junk_drawer_types_bounce_with_controller_notice_syntax(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, junk: str
) -> None:
    env = _isolate(monkeypatch, tmp_path)
    posted = _post(
        env,
        [
            "--dispatch-id",
            f"junk-{junk}",
            "--type",
            junk,
            "--text",
            "should not record",
        ],
        cwd=tmp_path,
    )
    assert posted.returncode == 2, posted.stdout
    assert f"type {junk!r} is not a sendable post type" in posted.stderr
    assert "--type controller-notice" in posted.stderr
    assert "--to-controller LABEL" in posted.stderr
    assert not messages.inbox_path(
        Path(env["GOALFLIGHT_MESSAGES_DIR"]), f"junk-{junk}"
    ).is_file()


def test_post_help_lists_working_set_not_junk_drawer() -> None:
    help_proc = subprocess.run(
        [sys.executable, str(SCRIPT), "post", "--help"],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        check=False,
    )
    assert help_proc.returncode == 0, help_proc.stderr
    text = help_proc.stdout
    assert "--text-file" in text
    assert "backticks" in text
    assert "$()" in text
    metavar_start = text.find("--type {")
    assert metavar_start != -1, text
    metavar_end = text.find("}", metavar_start)
    choices = text[metavar_start + len("--type {") : metavar_end]
    listed = set(choices.split(","))
    assert listed == set(HELP_MUST_LIST)
    for name in HELP_MUST_NOT_LIST_AS_CHOICE:
        assert name not in listed
    assert set(messages.POST_CLI_TYPES) == set(HELP_MUST_LIST)
    assert "steer" in messages.POST_CLI_HIDDEN_TYPES
    assert "advisory" in messages.EVENT_TYPE_REGISTRY
    assert "note" in messages.EVENT_TYPE_COMPATIBILITY_ALIASES


def test_controller_mail_send_documents_text_file_heredoc() -> None:
    send = (ROOT / "protocols" / "controller-mail.md").read_text(encoding="utf-8")
    assert "--text-file /dev/stdin <<'EOF'" in send
    assert "body with `backticks` and apostrophes" in send
    assert "$(git format-patch" not in send
