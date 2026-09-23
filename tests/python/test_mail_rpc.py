"""Hermetic coverage for the journal-host mail RPC.

The server binds an ephemeral loopback port and the messages CLI writes a
temp journal. No test contacts a live ``~/.goal-flight`` or a public address.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
from types import SimpleNamespace
import threading
import urllib.error
import urllib.request

import pytest

import goalflight_journal as journal
import goalflight_mail_rpc as rpc
import goalflight_ledger as ledger


LABEL = "goalflight-grokbot"
OTHER = "other-controller"
TOKEN = "mail-rpc-test-token-0123456789abcdef"


@pytest.fixture(autouse=True)
def isolated_rpc_child(monkeypatch, isolate_goalflight_machine_state):
    """After production scrubbing, relocate stores into the test sandbox.

    The scrub itself is asserted separately. Never let a real messages child
    fall through to the host's default stores while testing that boundary.
    """
    isolated = dict(isolate_goalflight_machine_state)
    run = subprocess.run

    def isolated_run(command, **kwargs):
        if "env" in kwargs and (
            str(rpc.MESSAGES_SCRIPT) in command
            or any("_confined_messages_main" in str(arg) for arg in command)
        ):
            kwargs["env"] = {**kwargs["env"], **isolated}
        return run(command, **kwargs)

    monkeypatch.setattr(subprocess, "run", isolated_run)


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
    monkeypatch.delenv(rpc.USERS_FILE_ENV, raising=False)
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
    monkeypatch.delenv(rpc.USERS_FILE_ENV, raising=False)
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


TOKEN_A = "mail-rpc-user-a-token-0123456789abcdef"
TOKEN_B = "mail-rpc-user-b-token-0123456789abcdef"
LEGACY_TOKEN = "mail-rpc-legacy-token-0123456789abcd"


@pytest.mark.parametrize("file_mode", [False, True])
@pytest.mark.parametrize("capability", [
    "GOALFLIGHT_CONTROLLER_LEASE_NONCE", "GOALFLIGHT_CONTROLLER_SESSION_ID",
])
def test_parent_capability_cannot_mint_rpc_authorship(monkeypatch, tmp_path, capability, file_mode):
    project = _project(tmp_path)
    _claim(project, LABEL)
    _clear_daemon_env(monkeypatch, tmp_path)
    monkeypatch.setenv(rpc.TOKEN_ENV, TOKEN)
    monkeypatch.setenv("GOALFLIGHT_CONTROLLER_LABEL", LABEL)
    if file_mode:
        users = _users_file(tmp_path, [{
            "token": TOKEN_A, "controller_label": LABEL, "project_root": str(project),
        }])
        monkeypatch.setenv(rpc.USERS_FILE_ENV, str(users))
    monkeypatch.setenv(capability, "parent-private-capability")
    config = rpc.load_config()
    status, result = rpc.handle_post(config, config.users[0], header_label=None, body={
        "dispatch_id": "authorship-probe", "type": "controller-notice",
        "to_controller": LABEL, "project_root": str(project), "text": "hello",
    })
    assert status == 200, result
    envelope = result["result"]["envelope"]
    assert "author_digest" not in envelope
    assert "parent-private-capability" not in json.dumps(envelope)


def test_child_environment_drops_store_routing(monkeypatch):
    # Inventory: messages defaults, task root/store, journal and dispatch ledger.
    names = (
        "GOALFLIGHT_DISPATCH_ID", "GOALFLIGHT_CONTROLLER_LEASE_NONCE",
        "GOALFLIGHT_CONTROLLER_SESSION_ID", "GOALFLIGHT_PROJECT_ROOT",
        "GOALFLIGHT_MESSAGES_DIR", "GOALFLIGHT_FLEET_DIR",
        "GOALFLIGHT_TASK_STORE_DIR", "GOALFLIGHT_JOURNAL_DIR",
        "GOALFLIGHT_STATE_DIR", "GOALFLIGHT_DISPATCH_DIR", "XDG_STATE_HOME",
    )
    for name in names:
        monkeypatch.setenv(name, "parent-routing")
    child = rpc._child_env(LABEL)
    assert not set(names).intersection(child)
    assert child["GOALFLIGHT_CONTROLLER_LABEL"] == LABEL


@pytest.mark.parametrize("route", ["explicit", "payload", "dispatch"])
@pytest.mark.parametrize("msg_type", ["status", "result", "blocked", "user_need"])
@pytest.mark.parametrize("legacy", [False, True])
def test_actual_delivery_project_confinement(monkeypatch, tmp_path, route, msg_type, legacy):
    project = _project(tmp_path)
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    _claim(foreign, OTHER)
    _clear_daemon_env(monkeypatch, tmp_path)
    monkeypatch.setenv("GOALFLIGHT_CONTROLLER_LABEL", LABEL)
    monkeypatch.setenv("GOALFLIGHT_PROJECT_ROOT", str(project))
    if legacy:
        monkeypatch.setenv(rpc.TOKEN_ENV, TOKEN)
    else:
        users = _users_file(tmp_path, [{
            "token": TOKEN_A, "controller_label": LABEL, "project_root": str(project),
        }])
        monkeypatch.setenv(rpc.USERS_FILE_ENV, str(users))
    dispatch_id = "foreign-route-probe"
    body = {"dispatch_id": dispatch_id, "type": msg_type, "text": "probe"}
    if route == "explicit":
        body.update(type="controller-notice", to_controller=OTHER,
                    controller_project_root=str(foreign))
    elif route == "payload":
        body.pop("text")
        body["payload"] = {"text": "probe", "project_root": str(foreign)}
    else:
        ledger.record_path(dispatch_id).write_text(json.dumps({
            "dispatch_id": dispatch_id, "project_root": str(foreign),
            "controller_label": OTHER,
        }))
    config = rpc.load_config()
    server, thread, url = _serve(config)
    monkeypatch.setenv("GOALFLIGHT_MAIL_RPC_URL", url)
    monkeypatch.setenv(rpc.TOKEN_ENV, TOKEN if legacy else TOKEN_A)
    # Compare durable journal and carrier bytes, including newly created files.
    roots = [Path(os.environ[name]) for name in (
        "GOALFLIGHT_MESSAGES_DIR", "GOALFLIGHT_JOURNAL_DIR",
    )]
    def snapshot():
        return {str(p): p.read_bytes() for root in roots for p in root.rglob("*")
                if p.is_file() and not p.name.endswith("-shm")}
    before = snapshot()
    try:
        status, result = rpc.client_call("/v1/post", body, label="")
        assert status == (200 if legacy else 403), result
        if not legacy:
            assert snapshot() == before
    finally:
        _stop(server, thread)


@pytest.mark.parametrize("layout", ["managed", "gitdir"])
def test_users_pin_canonical_journal_root(monkeypatch, tmp_path, layout):
    root = _project(tmp_path)
    if layout == "managed":
        checkout = root / ".claude" / "worktrees" / "worker"
        checkout.mkdir(parents=True)
    else:
        subprocess.run(["git", "init", "-q", str(root)], check=True)
        checkout = tmp_path / "linked-checkout"
        subprocess.run(["git", "-C", str(root), "worktree", "add", "--orphan",
                        "-b", "rpc-pin-test", str(checkout)], check=True, capture_output=True)
    users = _users_file(tmp_path, [{
        "token": TOKEN_A, "controller_label": LABEL, "project_root": str(checkout),
    }])
    _clear_daemon_env(monkeypatch, tmp_path)
    monkeypatch.setenv(rpc.USERS_FILE_ENV, str(users))
    config = rpc.load_config()
    assert config.users[0].project_root == str(root.resolve())
    assert rpc.resolve_project_root(str(checkout), config, config.users[0]) == root.resolve()


def test_users_pin_refuses_canonical_root_move(monkeypatch, tmp_path):
    root = _project(tmp_path)
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    link = tmp_path / "pinned-link"
    link.symlink_to(root, target_is_directory=True)
    users = _users_file(tmp_path, [{
        "token": TOKEN_A, "controller_label": LABEL, "project_root": str(link),
    }])
    _clear_daemon_env(monkeypatch, tmp_path)
    monkeypatch.setenv(rpc.USERS_FILE_ENV, str(users))
    config = rpc.load_config()
    link.unlink()
    link.symlink_to(foreign, target_is_directory=True)
    with pytest.raises(rpc.Forbidden, match="changed"):
        rpc.resolve_project_root(None, config, config.users[0])
    monkeypatch.setattr(rpc, "MailRpcServer", lambda *_: pytest.fail("must refuse before bind"))
    with pytest.raises(rpc.Forbidden, match="changed"):
        rpc.serve(config)


def test_users_file_rejects_symlink(tmp_path):
    users = _users_file(tmp_path, [{"token": TOKEN_A, "controller_label": LABEL}])
    link = tmp_path / "linked-users.json"
    link.symlink_to(users)
    with pytest.raises(rpc.MailRpcError):
        rpc.load_users_file(link, global_project_root="")


def test_users_file_rejects_foreign_owner(monkeypatch, tmp_path):
    users = _users_file(tmp_path, [{"token": TOKEN_A, "controller_label": LABEL}])
    original = os.fstat
    def foreign_owner(fd):
        info = original(fd)
        return SimpleNamespace(st_mode=info.st_mode, st_uid=os.geteuid() + 1)
    monkeypatch.setattr(os, "fstat", foreign_owner)
    with pytest.raises(rpc.MailRpcError, match="owner"):
        rpc.load_users_file(users, global_project_root="")


def test_users_file_reads_validated_descriptor(monkeypatch, tmp_path):
    users = _users_file(tmp_path, [{"token": TOKEN_A, "controller_label": LABEL}])
    replacement = tmp_path / "replacement.json"
    replacement.write_text(json.dumps({"users": [{"token": TOKEN_B, "controller_label": OTHER}]}))
    original_fstat, original_stat = os.fstat, Path.stat
    replaced = False
    def replace():
        nonlocal replaced
        if not replaced:
            replacement.replace(users)
            replaced = True
    def fstat(fd):
        info = original_fstat(fd)
        replace()
        return info
    def path_stat(path, *args, **kwargs):
        info = original_stat(path, *args, **kwargs)
        if path == users:
            replace()
        return info
    monkeypatch.setattr(os, "fstat", fstat)
    monkeypatch.setattr(Path, "stat", path_stat)
    loaded = rpc.load_users_file(users, global_project_root="")
    assert replaced
    assert loaded[0].controller_label == LABEL


@pytest.mark.parametrize("blank", ["", "  \t "])
@pytest.mark.parametrize("legacy", [False, True])
def test_explicit_blank_users_file_never_falls_back(monkeypatch, tmp_path, blank, legacy):
    users = _users_file(tmp_path, [{"token": TOKEN_A, "controller_label": LABEL}])
    _clear_daemon_env(monkeypatch, tmp_path)
    monkeypatch.setattr(rpc, "default_users_file", lambda: users)
    monkeypatch.setenv(rpc.USERS_FILE_ENV, blank)
    if legacy:
        monkeypatch.setenv(rpc.TOKEN_ENV, TOKEN)
    with pytest.raises(rpc.MailRpcError, match="empty"):
        rpc.load_config()


def test_implicit_users_file_logs_path(monkeypatch, tmp_path, capsys):
    users = _users_file(tmp_path, [{"token": TOKEN_A, "controller_label": LABEL}])
    _clear_daemon_env(monkeypatch, tmp_path)
    monkeypatch.setattr(rpc, "default_users_file", lambda: users)
    rpc.load_config()
    assert str(users) in capsys.readouterr().err


@pytest.mark.parametrize("duplicate", ["controller_label", "token"])
def test_users_file_rejects_duplicate_identity(tmp_path, duplicate):
    second = {"token": TOKEN_B, "controller_label": OTHER}
    second[duplicate] = LABEL if duplicate == "controller_label" else TOKEN_A
    users = _users_file(tmp_path, [{"token": TOKEN_A, "controller_label": LABEL}, second])
    with pytest.raises(rpc.MailRpcError, match="duplicate") as caught:
        rpc.load_users_file(users, global_project_root="")
    assert TOKEN_A not in str(caught.value)
    assert TOKEN_B not in str(caught.value)
    if duplicate == "controller_label":
        assert LABEL in str(caught.value)


def _users_file(tmp_path: Path, users: list[dict[str, str]]) -> Path:
    path = tmp_path / "mail-rpc.users.json"
    path.write_text(json.dumps({"users": users}), encoding="utf-8")
    path.chmod(0o600)
    return path


def _clear_daemon_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    missing = tmp_path / "no-default-users.json"
    monkeypatch.delenv(rpc.TOKEN_ENV, raising=False)
    monkeypatch.delenv(rpc.USERS_FILE_ENV, raising=False)
    monkeypatch.delenv(rpc.BIND_ENV, raising=False)
    monkeypatch.delenv(rpc.ALLOW_PUBLIC_BIND_ENV, raising=False)
    monkeypatch.delenv("GOALFLIGHT_CONTROLLER_LABEL", raising=False)
    monkeypatch.delenv("GOALFLIGHT_PROJECT_ROOT", raising=False)
    monkeypatch.setattr(rpc, "default_users_file", lambda: missing)


def _serve(config: rpc.MailRpcConfig):
    server = rpc.MailRpcServer(("127.0.0.1", 0), config)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address[:2]
    return server, thread, f"http://{host}:{port}"


def _stop(server: rpc.MailRpcServer, thread: threading.Thread) -> None:
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)


def test_users_file_confines_each_token(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    project = _project(tmp_path)
    other_root = tmp_path / "other-checkout"
    other_root.mkdir()
    _claim(project, LABEL)
    _claim(project, OTHER)
    users = _users_file(
        tmp_path,
        [
            {
                "token": TOKEN_A,
                "controller_label": LABEL,
                "project_root": str(project),
            },
            {
                "token": TOKEN_B,
                "controller_label": OTHER,
                "project_root": str(project),
            },
        ],
    )
    _clear_daemon_env(monkeypatch, tmp_path)
    monkeypatch.setenv(rpc.USERS_FILE_ENV, str(users))
    # A leftover single-token pin must not apply to every user, and must not
    # be accepted as a bearer once the users file is selected.
    monkeypatch.setenv(rpc.TOKEN_ENV, LEGACY_TOKEN)
    monkeypatch.setenv("GOALFLIGHT_CONTROLLER_LABEL", "not a valid label")
    config = rpc.load_config()
    assert config.users_from_file is True
    assert [user.controller_label for user in config.users] == [LABEL, OTHER]
    assert LEGACY_TOKEN not in repr(config)
    assert TOKEN_A not in repr(config)
    server, thread, url = _serve(config)
    monkeypatch.setenv("GOALFLIGHT_MAIL_RPC_URL", url)
    monkeypatch.delenv("GOALFLIGHT_CONTROLLER_LABEL", raising=False)
    try:
        status, health = rpc.client_call("/v1/health", method="GET")
        assert status == 200
        assert health == {"ok": True, "service": rpc.SERVICE_NAME}
        assert "users" not in health

        monkeypatch.setenv(rpc.TOKEN_ENV, LEGACY_TOKEN)
        status, legacy = rpc.client_call("/v1/relay", {}, label="")
        assert status == 401
        assert legacy.get("error") == "unauthorized"
        assert "detail" not in legacy

        monkeypatch.setenv(rpc.TOKEN_ENV, "wrong-token-value-0123456789")
        status, denied = rpc.client_call("/v1/post", {"type": "controller-notice"}, label="")
        assert status == 401
        assert denied.get("error") == "unauthorized"
        assert TOKEN_A not in json.dumps(denied)
        assert TOKEN_B not in json.dumps(denied)

        monkeypatch.setenv(rpc.TOKEN_ENV, TOKEN_A)
        status, posted = rpc.client_call(
            "/v1/post",
            {
                "dispatch_id": "mail-rpc-a",
                "type": "controller-notice",
                "to_controller": LABEL,
                "text": "for label a",
                "project_root": str(project),
                "controller_project_root": str(project),
            },
            label="",
        )
        assert status == 200, posted

        status, forged = rpc.client_call(
            "/v1/post",
            {
                "dispatch_id": "mail-rpc-forged",
                "type": "controller-notice",
                "to_controller": OTHER,
                "text": "not from a",
                "project_root": str(project),
                "controller_label": OTHER,
            },
            label="",
        )
        assert status == 403, forged
        assert "items" not in forged

        status, other_checkout = rpc.client_call(
            "/v1/post",
            {
                "dispatch_id": "mail-rpc-other-root",
                "type": "controller-notice",
                "to_controller": OTHER,
                "text": "wrong checkout",
                "project_root": str(project),
                "controller_project_root": str(other_root),
            },
            label="",
        )
        assert status == 403, other_checkout
        assert "different checkout" in str(other_checkout.get("detail") or "")

        status, crossed = rpc.client_call(
            "/v1/relay",
            {"project_root": str(project), "controller_label": OTHER},
            label=OTHER,
        )
        assert status == 403, crossed
        assert crossed["error"] == "forbidden"
        assert "different controller" in str(crossed.get("detail") or "")
        assert "items" not in crossed

        status, wrong_root = rpc.client_call(
            "/v1/relay",
            {"project_root": str(other_root)},
            label="",
        )
        assert status == 403, wrong_root
        assert "different checkout" in str(wrong_root.get("detail") or "")

        monkeypatch.setenv(rpc.TOKEN_ENV, TOKEN_B)
        status, peeked = rpc.client_call(
            "/v1/relay",
            {"project_root": str(project)},
            label="",
        )
        assert status == 200, peeked
        assert peeked["result"]["items"] == []

        monkeypatch.setenv(rpc.TOKEN_ENV, TOKEN_A)
        status, own = rpc.client_call(
            "/v1/relay",
            {"project_root": str(project)},
            label=LABEL,
        )
        assert status == 200, own
        assert any(item.get("dispatch_id") == "mail-rpc-a" for item in own["result"]["items"])
    finally:
        _stop(server, thread)


def test_legacy_token_ignores_default_users_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    project = _project(tmp_path)
    other = tmp_path / "elsewhere"
    other.mkdir()
    users = _users_file(
        tmp_path,
        [{"token": TOKEN_A, "controller_label": OTHER, "project_root": str(other)}],
    )
    _clear_daemon_env(monkeypatch, tmp_path)
    monkeypatch.setattr(rpc, "default_users_file", lambda: users)
    monkeypatch.setenv(rpc.TOKEN_ENV, TOKEN)
    monkeypatch.setenv("GOALFLIGHT_CONTROLLER_LABEL", LABEL)
    monkeypatch.setenv("GOALFLIGHT_PROJECT_ROOT", str(project))
    config = rpc.load_config()
    assert config.users_from_file is False
    assert len(config.users) == 1
    assert config.users[0].controller_label == LABEL
    assert config.users[0].pin_project_root is False
    assert rpc.token_matches(f"Bearer {TOKEN}", TOKEN)
    assert rpc.authenticate(f"Bearer {TOKEN}", config).controller_label == LABEL
    with pytest.raises(rpc.Unauthorized):
        rpc.authenticate(f"Bearer {TOKEN_A}", config)
    # Request path still wins in single-token mode.
    assert rpc.resolve_project_root(str(other), config, config.users[0]) == other
    assert rpc.resolve_project_root(None, config, config.users[0]) == project


def test_default_users_file_when_token_unset(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    project = _project(tmp_path)
    global_root = tmp_path / "global-checkout"
    global_root.mkdir()
    users = _users_file(
        tmp_path,
        [
            {"token": TOKEN_A, "controller_label": LABEL, "project_root": str(project)},
            {"token": TOKEN_B, "controller_label": OTHER},
        ],
    )
    _clear_daemon_env(monkeypatch, tmp_path)
    monkeypatch.setattr(rpc, "default_users_file", lambda: users)
    monkeypatch.setenv("GOALFLIGHT_PROJECT_ROOT", str(global_root))
    config = rpc.load_config()
    assert config.users_from_file is True
    pinned, fallback = config.users
    assert pinned.project_root == str(project)
    assert pinned.pin_project_root is True
    assert fallback.project_root == str(global_root.resolve())
    assert fallback.pin_project_root is True
    assert rpc.resolve_project_root(None, config, pinned) == project.resolve()
    assert rpc.resolve_project_root(str(project), config, pinned) == project.resolve()
    with pytest.raises(rpc.Forbidden, match="different checkout"):
        rpc.resolve_project_root(str(global_root), config, pinned)
    with pytest.raises(rpc.Forbidden, match="different checkout"):
        rpc.resolve_project_root(str(tmp_path / "missing-checkout"), config, pinned)
    assert rpc.resolve_project_root(None, config, fallback) == global_root.resolve()
    with pytest.raises(rpc.Forbidden, match="different checkout"):
        rpc.resolve_project_root(str(project), config, fallback)


def test_users_file_rejects_bad_entries(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _clear_daemon_env(monkeypatch, tmp_path)
    cases: list[tuple[object, str]] = [
        ({"users": []}, "non-empty"),
        ({"users": [{"token": "short", "controller_label": LABEL}]}, "at least"),
        ({"users": [{"token": TOKEN_A}]}, "controller_label is required"),
        (
            {"users": [{"token": TOKEN_A, "controller_label": "bad label"}]},
            "bounded identity",
        ),
        (
            {
                "users": [
                    {"token": TOKEN_A, "controller_label": LABEL},
                    {"token": TOKEN_A, "controller_label": OTHER},
                ]
            },
            "duplicates",
        ),
        (
            {"users": [{"token": TOKEN_A, "controller_label": LABEL, "extra": "no"}]},
            "unknown keys",
        ),
        ([], "users array"),
    ]
    for payload, match in cases:
        path = tmp_path / "users.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        path.chmod(0o600)
        monkeypatch.setenv(rpc.USERS_FILE_ENV, str(path))
        with pytest.raises(rpc.MailRpcError, match=match) as caught:
            rpc.load_config()
        assert TOKEN_A not in str(caught.value)

    loose = _users_file(
        tmp_path, [{"token": TOKEN_A, "controller_label": LABEL}]
    )
    loose.chmod(0o644)
    monkeypatch.setenv(rpc.USERS_FILE_ENV, str(loose))
    with pytest.raises(rpc.MailRpcError, match="chmod 600"):
        rpc.load_config()

    monkeypatch.setenv(rpc.USERS_FILE_ENV, str(tmp_path / "missing-users.json"))
    with pytest.raises(rpc.MailRpcError, match="not a file"):
        rpc.load_config()


def test_file_user_without_roots_still_accepts_the_request_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    project = _project(tmp_path)
    other = tmp_path / "other-checkout"
    other.mkdir()
    users = _users_file(tmp_path, [{"token": TOKEN_A, "controller_label": LABEL}])
    _clear_daemon_env(monkeypatch, tmp_path)
    monkeypatch.setenv(rpc.USERS_FILE_ENV, str(users))
    config = rpc.load_config()
    user = config.users[0]
    assert user.pin_project_root is False
    assert rpc.resolve_project_root(str(project), config, user) == project
    assert rpc.resolve_project_root(str(other), config, user) == other


def test_legacy_load_config_without_label(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _clear_daemon_env(monkeypatch, tmp_path)
    monkeypatch.setenv(rpc.TOKEN_ENV, TOKEN)
    config = rpc.load_config()
    assert config.users_from_file is False
    assert config.users[0].controller_label == ""
    assert rpc.resolve_identity(config.users[0], header_label=LABEL) == LABEL


def test_users_file_rejects_relative_project_root(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    checkout = tmp_path / "proj"
    nested = checkout / "proj"
    checkout.mkdir()
    nested.mkdir()
    monkeypatch.chdir(tmp_path)
    users = _users_file(tmp_path, [
        {"token": TOKEN_A, "controller_label": LABEL, "project_root": "proj"},
    ])
    with pytest.raises(rpc.MailRpcError, match="absolute"):
        rpc.load_users_file(users, global_project_root="")


def test_relative_controller_project_root_stays_on_the_pin(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    checkout = tmp_path / "proj-a"
    nested = checkout / "proj-a"
    checkout.mkdir()
    nested.mkdir()
    _claim(checkout, LABEL)
    monkeypatch.chdir(tmp_path)
    users = _users_file(
        tmp_path,
        [
            {
                "token": TOKEN_A,
                "controller_label": LABEL,
                "project_root": str(checkout),
            }
        ],
    )
    _clear_daemon_env(monkeypatch, tmp_path)
    monkeypatch.setenv(rpc.USERS_FILE_ENV, str(users))
    config = rpc.load_config()
    server, thread, url = _serve(config)
    monkeypatch.setenv("GOALFLIGHT_MAIL_RPC_URL", url)
    monkeypatch.setenv(rpc.TOKEN_ENV, TOKEN_A)
    try:
        status, posted = rpc.client_call(
            "/v1/post",
            {
                "dispatch_id": "mail-rpc-rel",
                "type": "controller-notice",
                "to_controller": LABEL,
                "text": "stay on the pin",
                "controller_project_root": "proj-a",
            },
            label="",
        )
        assert status == 200, posted
        status, peeked = rpc.client_call(
            "/v1/relay",
            {"project_root": str(checkout)},
            label="",
        )
        assert status == 200, peeked
        assert any(
            item.get("dispatch_id") == "mail-rpc-rel" for item in peeked["result"]["items"]
        )
        code, stdout, stderr = rpc.run_messages(
            [
                "relay",
                "--json",
                "--new",
                "--project-root",
                str(nested),
                "--controller-label",
                LABEL,
            ],
            identity=LABEL,
            cwd=nested,
            timeout_s=30,
        )
        assert "mail-rpc-rel" not in stdout
        assert "mail-rpc-rel" not in stderr
        if code == 0:
            assert json.loads(stdout).get("items") == []
    finally:
        _stop(server, thread)


def test_legacy_unpinned_label_still_comes_from_the_request() -> None:
    user = rpc.MailRpcUser(
        token_digest=b"\x00" * 32,
        controller_label="",
        project_root="",
        pin_project_root=False,
    )
    assert rpc.resolve_identity(user, header_label=LABEL) == LABEL
    with pytest.raises(rpc.MailRpcError, match="controller label required"):
        rpc.resolve_identity(user, header_label=None)
    pinned = rpc.MailRpcUser(
        token_digest=b"\x01" * 32,
        controller_label=LABEL,
        project_root="",
        pin_project_root=False,
    )
    assert rpc.resolve_identity(pinned, header_label=None) == LABEL
    with pytest.raises(rpc.Forbidden, match="different controller"):
        rpc.resolve_identity(pinned, header_label=OTHER)


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
