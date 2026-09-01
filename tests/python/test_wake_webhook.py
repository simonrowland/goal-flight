"""Optional outbound wake-webhook nudge on listen-visible mail harvest.

The journal is durable truth. When GOALFLIGHT_WAKE_WEBHOOK_URL is unset,
harvest must not open a socket. When it is set, one POST follows a newly
projected waking delivery. A failed POST must not drop the journal write.
"""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import urllib.request

import pytest


ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import goalflight_journal as journal  # noqa: E402
import goalflight_messages as messages  # noqa: E402
import goalflight_wake_webhook as webhook  # noqa: E402


class _WebhookServer(ThreadingHTTPServer):
    def __init__(self, status: int = 200) -> None:
        super().__init__(("127.0.0.1", 0), _WebhookHandler)
        self.status = status
        self.requests: list[dict[str, object]] = []
        self._thread = threading.Thread(target=self.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        host, port = self.server_address[:2]
        return f"http://{host}:{port}/wake"

    def __enter__(self) -> _WebhookServer:
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.shutdown()
        self.server_close()
        self._thread.join(timeout=2)


class _WebhookHandler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:  # noqa: N802 — BaseHTTPRequestHandler API
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length)
        assert isinstance(self.server, _WebhookServer)
        self.server.requests.append(
            {
                "path": self.path,
                "headers": {key.lower(): value for key, value in self.headers.items()},
                "body": body,
            }
        )
        self.send_response(self.server.status)
        self.end_headers()

    def log_message(self, fmt: str, *args: object) -> None:
        return


def _git_project(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    project.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=project, check=True)
    return project


def _claim(authority: journal.Journal, label: str = "ctl"):
    claimed = authority.claim_or_renew_lease(
        label, principal={"principal_id": f"{label}-principal"}
    )
    assert claimed.committed and claimed.value is not None, claimed.reason
    return claimed.value


def _post_mail(
    project: Path,
    messages_dir: Path,
    label: str,
    text: str = "nudge-mail",
    *,
    dispatch_id: str = "nudge-stream",
    msg_type: str = "controller-notice",
) -> dict:
    return messages.post_message(
        dispatch_id=dispatch_id,
        msg_type=msg_type,
        payload={"text": text},
        messages_dir=messages_dir,
        source={"node": "test", "adapter": "pytest", "transport": "controller"},
        addressee=messages.controller_addressee(label, project_root=project),
    )


def test_classify_nudge_kind_splits_mail_wake_complete() -> None:
    assert webhook.classify_nudge_kind("controller-notice") == "mail"
    assert webhook.classify_nudge_kind("merge-request") == "mail"
    assert webhook.classify_nudge_kind("result") == "complete"
    assert webhook.classify_nudge_kind("blocked") == "complete"
    assert webhook.classify_nudge_kind("user_need") == "wake"
    assert webhook.classify_nudge_kind("steering") == "wake"


def test_load_config_unset_means_none(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("GOALFLIGHT_WAKE_WEBHOOK_URL", raising=False)
    monkeypatch.setenv("GOALFLIGHT_WAKE_WEBHOOK_CONFIG", os.devnull)
    assert webhook.load_config() is None
    monkeypatch.setenv("GOALFLIGHT_TEST_MODE", "1")
    monkeypatch.delenv("GOALFLIGHT_WAKE_WEBHOOK_CONFIG", raising=False)
    assert webhook.default_config_path() == Path(os.devnull)
    assert webhook.load_config() is None


def test_load_config_file_and_env_auth(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    conf = tmp_path / "wake-webhook.json"
    conf.write_text(
        json.dumps(
            {
                "url": "https://example.test/from-file",
                "secret": "file-secret",
                "auth": "x-webhook-key",
                "timeout_s": 1.5,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("GOALFLIGHT_WAKE_WEBHOOK_CONFIG", str(conf))
    monkeypatch.delenv("GOALFLIGHT_WAKE_WEBHOOK_URL", raising=False)
    loaded = webhook.load_config()
    assert loaded is not None
    assert loaded.url == "https://example.test/from-file"
    assert loaded.secret == "file-secret"
    assert loaded.auth == "x-webhook-key"
    assert loaded.timeout_s == 1.5

    monkeypatch.setenv("GOALFLIGHT_WAKE_WEBHOOK_URL", "http://127.0.0.1:9/from-env")
    monkeypatch.setenv("GOALFLIGHT_WAKE_WEBHOOK_SECRET", "env-secret")
    monkeypatch.setenv("GOALFLIGHT_WAKE_WEBHOOK_AUTH", "bearer")
    overlaid = webhook.load_config()
    assert overlaid is not None
    assert overlaid.url == "http://127.0.0.1:9/from-env"
    assert overlaid.secret == "env-secret"
    assert overlaid.auth == "bearer"


def test_payload_omits_mail_body_and_attention_dispatch() -> None:
    payload = webhook.nudge_payload(
        {
            "event_type": "controller-notice",
            "recipient_label": "alice",
            "project_root": "/tmp/proj",
            "stream_id": "chunk-1",
            "payload": {"text": "SECRET BODY"},
        }
    )
    assert payload == {
        "kind": "mail",
        "controller_label": "alice",
        "project_root": "/tmp/proj",
        "event_type": "controller-notice",
        "dispatch_id": "chunk-1",
    }
    assert "SECRET BODY" not in json.dumps(payload)
    attention = webhook.nudge_payload(
        {
            "event_type": "user_need",
            "recipient_label": "alice",
            "project_root": "/tmp/proj",
            "stream_id": "attention",
        }
    )
    assert "dispatch_id" not in attention
    assert attention["kind"] == "wake"


def test_unset_url_means_zero_http_on_mail_harvest(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("GOALFLIGHT_WAKE_WEBHOOK_URL", raising=False)
    monkeypatch.setenv("GOALFLIGHT_WAKE_WEBHOOK_CONFIG", os.devnull)

    def forbid_http(*_args: object, **_kwargs: object):
        raise AssertionError("webhook must not open a socket when URL is unset")

    monkeypatch.setattr(urllib.request, "urlopen", forbid_http)
    project = _git_project(tmp_path)
    authority = journal.Journal.create(project)
    _claim(authority, "alice")
    posted = _post_mail(project, tmp_path / "messages", "alice")
    assert posted["recorded"] is True
    rows = authority.read_all(
        "SELECT event_uuid, projected_at FROM delivery_events WHERE event_uuid = ?",
        (posted["envelope"]["id"],),
    )
    assert len(rows) == 1
    assert rows[0]["projected_at"] is not None


def test_mail_harvest_posts_one_nudge(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    project = _git_project(tmp_path)
    authority = journal.Journal.create(project)
    _claim(authority, "alice")
    with _WebhookServer() as server:
        monkeypatch.setenv("GOALFLIGHT_WAKE_WEBHOOK_URL", server.url)
        monkeypatch.setenv("GOALFLIGHT_WAKE_WEBHOOK_SECRET", "sender-key")
        monkeypatch.setenv("GOALFLIGHT_WAKE_WEBHOOK_AUTH", "x-webhook-key")
        posted = _post_mail(project, tmp_path / "messages", "alice", dispatch_id="mail-1")
        assert posted["recorded"] is True
        # Idempotent replay of the same identity must not emit a second POST.
        posted_again = messages.post_message(
            dispatch_id="mail-1",
            msg_type="controller-notice",
            payload={"text": "nudge-mail"},
            messages_dir=tmp_path / "messages",
            source={"node": "test", "adapter": "pytest", "transport": "controller"},
            addressee=messages.controller_addressee("alice", project_root=project),
            event_id=posted["envelope"]["id"],
            event_ts=posted["envelope"]["ts"],
        )
        assert posted_again["recorded"] is False
        assert len(server.requests) == 1
        req = server.requests[0]
        assert req["path"] == "/wake"
        headers = req["headers"]
        assert isinstance(headers, dict)
        assert headers.get("x-webhook-key") == "sender-key"
        assert "authorization" not in headers
        body = json.loads(req["body"])
        assert body == {
            "kind": "mail",
            "controller_label": "alice",
            "project_root": str(authority.project_root),
            "event_type": "controller-notice",
            "dispatch_id": "mail-1",
        }
        assert "nudge-mail" not in json.dumps(body)


def test_complete_harvest_posts_complete_kind(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    project = _git_project(tmp_path)
    authority = journal.Journal.create(project)
    _claim(authority, "alice")
    prepared = authority.prepare_attempt("done-chunk")
    assert prepared.committed and prepared.value is not None
    committed = authority.commit_terminal(
        prepared.value.attempt_id,
        terminal_state="complete",
        observation={"state": "complete", "outcome": {}},
    )
    assert committed.committed
    with _WebhookServer() as server:
        monkeypatch.setenv("GOALFLIGHT_WAKE_WEBHOOK_URL", server.url)
        projected = authority.project_terminal_outbox(messages_dir=tmp_path / "messages")
        assert len(projected) == 1
        assert authority.project_terminal_outbox(messages_dir=tmp_path / "messages") == []
        assert len(server.requests) == 1
        body = json.loads(server.requests[0]["body"])
        assert body["kind"] == "complete"
        assert body["controller_label"] == "alice"
        assert body["dispatch_id"] == "done-chunk"
        assert body["event_type"] == "result"


def test_journal_write_survives_failed_post(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    project = _git_project(tmp_path)
    authority = journal.Journal.create(project)
    _claim(authority, "alice")
    with _WebhookServer(status=500) as server:
        monkeypatch.setenv("GOALFLIGHT_WAKE_WEBHOOK_URL", server.url)
        posted = _post_mail(project, tmp_path / "messages", "alice", dispatch_id="fail-1")
        assert posted["recorded"] is True
        assert len(server.requests) == 1
    rows = authority.read_all(
        "SELECT event_uuid, projected_at FROM delivery_events WHERE stream_id = ?",
        ("fail-1",),
    )
    assert len(rows) == 1
    assert rows[0]["projected_at"] is not None

    monkeypatch.setenv("GOALFLIGHT_WAKE_WEBHOOK_URL", "http://127.0.0.1:1/closed")
    monkeypatch.setenv("GOALFLIGHT_WAKE_WEBHOOK_TIMEOUT_S", "0.2")
    posted_closed = _post_mail(
        project, tmp_path / "messages", "alice", dispatch_id="fail-2"
    )
    assert posted_closed["recorded"] is True
    closed_rows = authority.read_all(
        "SELECT event_uuid FROM delivery_events WHERE stream_id = ?",
        ("fail-2",),
    )
    assert len(closed_rows) == 1


def test_quiet_projection_does_not_post(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GOALFLIGHT_WAKE_WEBHOOK_URL", "http://127.0.0.1:9/unused")

    def forbid_http(*_args: object, **_kwargs: object):
        raise AssertionError("quiet deliveries must not POST")

    monkeypatch.setattr(webhook, "post_nudge", forbid_http)
    assert (
        webhook.nudge_projected_delivery(
            {
                "newly_projected": True,
                "wake_class": "quiet",
                "event_type": "status",
                "recipient_label": "alice",
                "project_root": "/tmp/p",
                "stream_id": "s",
            }
        )
        is False
    )
    assert (
        webhook.nudge_projected_delivery(
            {
                "newly_projected": False,
                "wake_class": "waking",
                "event_type": "controller-notice",
                "recipient_label": "alice",
                "project_root": "/tmp/p",
                "stream_id": "s",
            }
        )
        is False
    )
