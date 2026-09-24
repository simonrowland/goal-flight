"""Hermetic coverage for the journal-host mail RPC.

The server binds an ephemeral loopback port and the messages CLI writes a
temp journal. No test contacts a live ``~/.goal-flight`` or a public address.
"""

from __future__ import annotations

import json
from pathlib import Path
import threading
import urllib.error
import urllib.request

import pytest

import goalflight_journal as journal
import goalflight_mail_rpc as rpc


LABEL = "goalflight-grokbot"
OTHER = "other-controller"
TOKEN = "mail-rpc-test-token-0123456789abcdef"


def _project(tmp_path: Path) -> Path:
    project = tmp_path / "mail-rpc-project"
    project.mkdir(exist_ok=True)
    return project


def _claim(project: Path, label: str) -> None:
    authority = journal.open_or_create_journal(project)
    active = authority.active_lease(label)
    result = authority.claim_or_renew_lease(
        label,
        principal={"principal_id": "mail-rpc-test"},
        nonce=active.nonce if active is not None else None,
    )
    assert result.committed, result.reason


@pytest.fixture
def mail_server(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv(rpc.TOKEN_ENV, TOKEN)
    monkeypatch.setenv("GOALFLIGHT_CONTROLLER_LABEL", LABEL)
    monkeypatch.delenv(rpc.BIND_ENV, raising=False)
    monkeypatch.delenv(rpc.ALLOW_PUBLIC_BIND_ENV, raising=False)
    config = rpc.load_config()
    server = rpc.MailRpcServer(("127.0.0.1", 0), config)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address[:2]
    url = f"http://{host}:{port}"
    monkeypatch.setenv("GOALFLIGHT_MAIL_RPC_URL", url)
    try:
        yield url
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_default_bind_and_public_bind_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(rpc.TOKEN_ENV, TOKEN)
    monkeypatch.delenv(rpc.BIND_ENV, raising=False)
    monkeypatch.delenv(rpc.ALLOW_PUBLIC_BIND_ENV, raising=False)
    monkeypatch.delenv("GOALFLIGHT_CONTROLLER_LABEL", raising=False)
    config = rpc.load_config()
    assert (config.bind_host, config.bind_port) == ("127.0.0.1", 8787)

    with pytest.raises(rpc.MailRpcError, match="public bind"):
        rpc.parse_bind("0.0.0.0:8787", allow_public=False)
    with pytest.raises(rpc.MailRpcError, match="public bind"):
        rpc.parse_bind("[::]:8787", allow_public=False)
    host, port = rpc.parse_bind("[::1]:8787", allow_public=False)
    assert (host, port) == ("::1", 8787)
    host, port = rpc.parse_bind("0.0.0.0:8787", allow_public=True)
    assert (host, port) == ("0.0.0.0", 8787)

    monkeypatch.setenv(rpc.TOKEN_ENV, "short")
    with pytest.raises(rpc.MailRpcError, match=rpc.TOKEN_ENV):
        rpc.load_config()


def test_health_and_auth_reject(
    mail_server: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    del mail_server
    status, health = rpc.client_call("/v1/health", method="GET")
    assert status == 200
    assert health == {"ok": True, "service": rpc.SERVICE_NAME}

    monkeypatch.setenv(rpc.TOKEN_ENV, "wrong-token-value-0123456789")
    status, denied = rpc.client_call("/v1/relay", {"drain": False})
    assert status == 401
    assert denied.get("error") == "unauthorized"
    assert "items" not in denied
    assert TOKEN not in json.dumps(denied)

    opener_status, missing = _request_without_auth(
        "/v1/post", {"type": "controller-notice"}
    )
    assert opener_status == 401
    assert missing.get("error") == "unauthorized"
    assert "detail" not in missing


def test_post_peek_and_drain(mail_server: str, tmp_path: Path) -> None:
    del mail_server
    project = _project(tmp_path)
    _claim(project, LABEL)
    status, posted = rpc.client_call(
        "/v1/post",
        {
            "dispatch_id": "mail-rpc-d1",
            "type": "controller-notice",
            "to_controller": LABEL,
            "subject": "rpc",
            "text": "hello from the mail rpc",
            "project_root": str(project),
        },
    )
    assert status == 200, posted
    assert posted["ok"] is True

    status, peeked = rpc.client_call(
        "/v1/relay",
        {"project_root": str(project)},
    )
    assert status == 200, peeked
    items = peeked["result"]["items"]
    assert any(item.get("dispatch_id") == "mail-rpc-d1" for item in items)

    status, drained = rpc.client_call(
        "/v1/relay?drain=1",
        {"project_root": str(project), "drain": True},
    )
    assert status == 200, drained
    assert drained["result"]["status"] == "drained"
    assert drained["result"]["drained"] >= 1

    status, again = rpc.client_call("/v1/relay", {"project_root": str(project)})
    assert status == 200, again
    assert again["result"]["items"] == []


def test_post_canonicalizes_project_root_alias(mail_server: str, tmp_path: Path) -> None:
    del mail_server
    project = _project(tmp_path)
    alias = tmp_path / "mail-rpc-project-alias"
    alias.symlink_to(project, target_is_directory=True)
    _claim(project, LABEL)

    status, posted = rpc.client_call(
        "/v1/post",
        {
            "dispatch_id": "mail-rpc-canonical-root",
            "type": "controller-notice",
            "to_controller": LABEL,
            "text": "canonical root",
            "project_root": str(alias),
        },
    )
    assert status == 200, posted
    envelope = posted["result"]["envelope"]
    assert envelope["addressee"]["project_root"] == str(project.resolve())


def test_advisory_and_unaddressed_match_cli(mail_server: str, tmp_path: Path) -> None:
    del mail_server
    project = _project(tmp_path)
    status, advisory = rpc.client_call(
        "/v1/post",
        {
            "dispatch_id": "mail-rpc-d1",
            "type": "advisory",
            "to_controller": LABEL,
            "text": "not deliverable",
            "project_root": str(project),
        },
    )
    assert status == 400, advisory
    assert advisory["ok"] is False
    detail = str(advisory.get("detail") or "")
    assert "controller-notice" in detail
    assert "--to-controller" in detail

    status, bare = rpc.client_call(
        "/v1/post",
        {
            "dispatch_id": "mail-rpc-d1",
            "type": "controller-notice",
            "text": "missing addressee",
            "project_root": str(project),
        },
    )
    assert status == 400, bare
    assert "no addressee" in str(bare.get("detail") or "")


def test_drain_other_controller_forbidden(mail_server: str, tmp_path: Path) -> None:
    del mail_server
    project = _project(tmp_path)
    _claim(project, LABEL)
    _claim(project, OTHER)
    status, denied = rpc.client_call(
        "/v1/relay",
        {"drain": True, "project_root": str(project)},
        label=OTHER,
    )
    assert status == 403, denied
    assert denied["error"] == "forbidden"
    assert "different controller" in str(denied.get("detail") or "")
    assert "items" not in denied


def _request_without_auth(path: str, body: dict[str, object]) -> tuple[int, dict[str, object]]:
    """POST with no Authorization header. Uses the URL already in the environment."""
    url, _token = rpc.client_endpoint()
    request = urllib.request.Request(
        url + path,
        data=json.dumps(body).encode("utf-8"),
        method="POST",
    )
    request.add_header("Content-Type", "application/json")
    opener = urllib.request.build_opener(rpc._NoRedirect)
    try:
        with opener.open(request, timeout=10) as response:
            status = int(response.status)
            raw = response.read()
    except urllib.error.HTTPError as exc:
        status = int(exc.code)
        raw = exc.read()
    payload = json.loads(raw.decode("utf-8"))
    assert isinstance(payload, dict)
    return status, payload
