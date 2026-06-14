#!/usr/bin/env python3
"""Tests for fleet billing auth probes and dispatch gate."""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import goalflight_fleet as fleet
import goalflight_fleet_billing as billing


def assert_true(name: str, condition: bool) -> None:
    if not condition:
        raise AssertionError(name)


def green_runner(_argv: list[str]) -> tuple[int, str, str]:
    return 0, '{"models":[{"id":"gpt-5"}]}\n', ""


def red_runner(_argv: list[str]) -> tuple[int, str, str]:
    return 1, "", "not logged in"


def tooling_runner(_argv: list[str]) -> tuple[int, str, str]:
    return 127, "", "python: command not found"


def codex_models_runner(argv: list[str]) -> tuple[int, str, str]:
    assert_true("openai auth argv", argv == ["codex", "debug", "models"])
    return 0, '{"models":[{"id":"gpt-5"}]}\n', ""


def empty_codex_models_runner(argv: list[str]) -> tuple[int, str, str]:
    assert_true("openai auth argv", argv == ["codex", "debug", "models"])
    return 0, '{"models":[]}\n', ""


def bare_exit_zero_runner(argv: list[str]) -> tuple[int, str, str]:
    assert_true("openai auth argv", argv == ["codex", "debug", "models"])
    return 0, "Logged in using ChatGPT\n", ""


def claude_auth_status_runner(argv: list[str]) -> tuple[int, str, str]:
    assert_true("anthropic auth argv", argv == ["claude", "auth", "status", "--json"])
    return 0, '{"loggedIn":true,"authMethod":"oauth","apiProvider":"anthropic"}\n', ""


def claude_logged_out_runner(argv: list[str]) -> tuple[int, str, str]:
    assert_true("anthropic auth argv", argv == ["claude", "auth", "status", "--json"])
    return 1, '{"loggedIn":false,"authMethod":"none","apiProvider":"anthropic"}\n', ""


REVOKED_CODEX_STDERR = (
    "Your access token could not be refreshed because your refresh token was revoked.\n"
    "Please log out and sign in again.\n"
    "refresh_token_invalidated\n"
    "repeated 401 on chatgpt.com/backend-api/codex/*\n"
    'codex_acp::thread "Unhandled error during turn: ... Unauthorized"\n'
)


def revoked_codex_runner(argv: list[str]) -> tuple[int, str, str]:
    assert_true("openai auth argv", argv == ["codex", "debug", "models"])
    return 0, "Logged in using ChatGPT\n", REVOKED_CODEX_STDERR


def _fixture_fleet(fleet_dir: Path, *, node_id: str = "localhost") -> None:
    fleet.bootstrap(fleet_dir)
    fleet_doc = fleet.read_json(fleet_dir / "fleet.json")
    fleet_doc["nodes"] = {
        node_id: {
            "node_id": node_id,
            "status": "active",
            "ssh": {"alias": "localhost", "hostname": "localhost"},
            "repo_root": str(ROOT),
            "state_dir": "~/.goal-flight",
            "billing_accounts": [],
            "added_at": "2026-05-24T12:00:00+00:00",
        }
    }
    fleet._atomic_write_json(fleet_dir / "fleet.json", fleet_doc)


def _write_probe(fleet_dir: Path, node_id: str, account_key: str, status: str) -> None:
    billing.write_probe_artifact(
        fleet_dir,
        node_id,
        {
            "schema": billing.AUTH_PROBE_SCHEMA,
            "account_key": account_key,
            "provider": account_key.split("/", 1)[0],
            "status": status,
            "probed_at": "2026-05-24T12:00:00+00:00",
        },
    )


def test_account_link_runs_probe_and_writes_artifact() -> None:
    with tempfile.TemporaryDirectory() as td:
        fleet_dir = Path(td) / "fleet"
        _fixture_fleet(fleet_dir)
        result = billing.link_account_to_node(
            fleet_dir,
            "openai/default",
            "localhost",
            runner=codex_models_runner,
        )
        assert_true("link ok", result["ok"] is True)
        assert_true("probe green", result["auth_probe"]["status"] == "green")
        artifact = billing.read_probe_artifact(fleet_dir, "localhost", "openai/default")
        assert_true("artifact saved", artifact is not None and artifact["status"] == "green")


def test_dispatch_auth_refuses_stale_green_probe_without_membership() -> None:
    with tempfile.TemporaryDirectory() as td:
        fleet_dir = Path(td) / "fleet"
        _fixture_fleet(fleet_dir)
        _write_probe(fleet_dir, "localhost", "openai/default", "green")
        try:
            billing.assert_dispatch_auth(fleet_dir, "localhost", "openai/default")
            assert_true("should block", False)
        except billing.DispatchAuthError as exc:
            message = str(exc)
            assert_true("hard red", exc.auth_probe == "red")
            assert_true("membership message", "not a current linked member" in message)
            assert_true("remedy", "account link" in message)
            assert_true("stale probe", "stale green probe" in message)


def test_dispatch_auth_allows_linked_member_with_green_probe() -> None:
    with tempfile.TemporaryDirectory() as td:
        fleet_dir = Path(td) / "fleet"
        _fixture_fleet(fleet_dir)
        billing.link_account_to_node(
            fleet_dir,
            "openai/default",
            "localhost",
            runner=green_runner,
        )
        billing.assert_dispatch_auth(fleet_dir, "localhost", "openai/default")


def test_openai_probe_uses_authenticated_models_check() -> None:
    assert_true("openai probe argv", billing.probe_argv("openai") == ["codex", "debug", "models"])


def test_openai_bare_exit_zero_probe_is_inconclusive() -> None:
    payload = billing.run_local_auth_probe(
        "openai/default",
        {"accounts": [{"account_key": "openai/default", "provider": "openai"}]},
        runner=bare_exit_zero_runner,
    )
    assert_true("bare exit-zero inconclusive", payload["status"] == "inconclusive")


def test_openai_empty_models_probe_is_inconclusive() -> None:
    payload = billing.run_local_auth_probe(
        "openai/default",
        {"accounts": [{"account_key": "openai/default", "provider": "openai"}]},
        runner=empty_codex_models_runner,
    )
    assert_true("empty models inconclusive", payload["status"] == "inconclusive")


def test_openai_working_token_probe_is_green_and_dispatch_allowed() -> None:
    import goalflight_fleet_dispatch as fleet_dispatch

    with tempfile.TemporaryDirectory() as td:
        fleet_dir = Path(td) / "fleet"
        _fixture_fleet(fleet_dir)
        result = billing.link_account_to_node(
            fleet_dir,
            "openai/default",
            "localhost",
            runner=codex_models_runner,
        )
        assert_true("working token green", result["auth_probe"]["status"] == "green")
        fleet_dispatch.assert_dispatch_gates(
            fleet_dir,
            node_id="localhost",
            billing_account="openai/default",
        )


def test_openai_revoked_token_probe_is_red_and_dispatch_gate_blocks() -> None:
    import goalflight_fleet_dispatch as fleet_dispatch

    with tempfile.TemporaryDirectory() as td:
        fleet_dir = Path(td) / "fleet"
        _fixture_fleet(fleet_dir)
        result = billing.link_account_to_node(
            fleet_dir,
            "openai/default",
            "localhost",
            runner=revoked_codex_runner,
        )
        assert_true("revoked token red", result["auth_probe"]["status"] == "red")
        artifact = billing.read_probe_artifact(fleet_dir, "localhost", "openai/default")
        assert_true("red artifact saved", artifact is not None and artifact["status"] == "red")
        try:
            fleet_dispatch.assert_dispatch_gates(
                fleet_dir,
                node_id="localhost",
                billing_account="openai/default",
            )
            assert_true("should block", False)
        except fleet_dispatch.DispatchGateError as exc:
            assert_true("auth gate code", exc.code == "auth")
            message = str(exc)
            assert_true("invalid token message", "invalid/revoked token" in message)
            assert_true("re-login message", "re-login required" in message)


def test_anthropic_probe_uses_auth_status_not_version() -> None:
    assert_true(
        "anthropic probe argv",
        billing.probe_argv("anthropic-session") == ["claude", "auth", "status", "--json"],
    )


def test_anthropic_auth_status_json_is_validated() -> None:
    green = billing.run_local_auth_probe(
        "anthropic/session-local",
        {"accounts": [{"account_key": "anthropic/session-local", "provider": "anthropic-session"}]},
        runner=claude_auth_status_runner,
    )
    assert_true("anthropic logged-in green", green["status"] == "green")
    red = billing.run_local_auth_probe(
        "anthropic/session-local",
        {"accounts": [{"account_key": "anthropic/session-local", "provider": "anthropic-session"}]},
        runner=claude_logged_out_runner,
    )
    assert_true("anthropic logged-out red", red["status"] == "red")
    inconclusive = billing.run_local_auth_probe(
        "anthropic/session-local",
        {"accounts": [{"account_key": "anthropic/session-local", "provider": "anthropic-session"}]},
        runner=lambda _a: (0, '{"authMethod":"oauth"}\n', ""),
    )
    assert_true("anthropic missing loggedIn inconclusive", inconclusive["status"] == "inconclusive")


def test_anthropic_logged_in_identity_denial_words_stays_green() -> None:
    stdout = json.dumps(
        {
            "loggedIn": True,
            "authMethod": "claude.ai",
            "apiProvider": "firstParty",
            "email": "user@401-unauthorized.example",
            "orgName": "Revoked Records LLC",
            "subscriptionType": "max",
        }
    )
    payload = billing.run_local_auth_probe(
        "anthropic/session-local",
        {"accounts": [{"account_key": "anthropic/session-local", "provider": "anthropic-session"}]},
        runner=lambda _a: (0, stdout, ""),
    )
    assert_true("anthropic logged-in identity green", payload["status"] == "green")


def test_anthropic_logged_in_stderr_denial_is_red() -> None:
    stdout = '{"loggedIn":true,"authMethod":"claude.ai","apiProvider":"firstParty"}\n'
    payload = billing.run_local_auth_probe(
        "anthropic/session-local",
        {"accounts": [{"account_key": "anthropic/session-local", "provider": "anthropic-session"}]},
        runner=lambda _a: (1, stdout, "401 unauthorized\n"),
    )
    assert_true("anthropic stderr denial red", payload["status"] == "red")


def test_account_link_cli_fails_closed_on_red_probe() -> None:
    old_runner = billing.default_runner
    try:
        billing.default_runner = revoked_codex_runner
        with tempfile.TemporaryDirectory() as td:
            fleet_dir = Path(td) / "fleet"
            _fixture_fleet(fleet_dir)
            code = billing.main(
                [
                    "--fleet-dir",
                    str(fleet_dir),
                    "link",
                    "--account-key",
                    "openai/default",
                    "--node",
                    "localhost",
                ]
            )
            assert_true("link red exit", code == 1)
    finally:
        billing.default_runner = old_runner


def test_doctor_fleet_shape() -> None:
    with tempfile.TemporaryDirectory() as td:
        fleet_dir = Path(td) / "fleet"
        _fixture_fleet(fleet_dir)
        billing.link_account_to_node(
            fleet_dir,
            "openai/default",
            "localhost",
            runner=green_runner,
        )
        billing.link_account_to_node(
            fleet_dir,
            "anthropic/session-local",
            "localhost",
            runner=claude_auth_status_runner,
        )
        summary = billing.fleet_auth_doctor(fleet_dir, refresh=False)
        assert_true("available", summary["available"] is True)
        nodes = summary["nodes"]
        assert_true("one node", len(nodes) == 1)
        accounts = nodes[0]["accounts"]
        assert_true("two accounts", len(accounts) == 2)
        for entry in accounts:
            assert_true("auth_probe field", "auth_probe" in entry)


def test_dispatch_gate_blocks_red_auth() -> None:
    with tempfile.TemporaryDirectory() as td:
        fleet_dir = Path(td) / "fleet"
        _fixture_fleet(fleet_dir)
        billing.link_account_to_node(
            fleet_dir,
            "openai/default",
            "localhost",
            runner=red_runner,
        )
        try:
            billing.assert_dispatch_auth(fleet_dir, "localhost", "openai/default")
            assert_true("should block", False)
        except billing.DispatchAuthError as exc:
            assert_true("red status", exc.auth_probe == "red")


def test_dispatch_auth_refuses_recorded_controller_owner_mismatch() -> None:
    with tempfile.TemporaryDirectory() as td:
        fleet_dir = Path(td) / "fleet"
        _fixture_fleet(fleet_dir)
        billing.link_account_to_node(
            fleet_dir,
            "openai/default",
            "localhost",
            runner=green_runner,
        )
        fleet_doc = fleet.read_json(fleet_dir / "fleet.json")
        fleet_doc["nodes"]["localhost"]["billing_account_owners"]["openai/default"] = "other-controller"
        fleet._atomic_write_json(fleet_dir / "fleet.json", fleet_doc)
        try:
            billing.assert_dispatch_auth(fleet_dir, "localhost", "openai/default")
            assert_true("should block", False)
        except billing.DispatchAuthError as exc:
            message = str(exc)
            assert_true("hard red", exc.auth_probe == "red")
            assert_true("owner mismatch", "not fleet controller" in message)
            assert_true("owner remedy", "account link" in message)


def test_dispatch_auth_allows_legacy_usable_fleet_json() -> None:
    with tempfile.TemporaryDirectory() as td:
        fleet_dir = Path(td) / "fleet"
        _fixture_fleet(fleet_dir)
        fleet_doc = fleet.read_json(fleet_dir / "fleet.json")
        fleet_doc.pop("schema", None)
        fleet_doc.pop("schema_version", None)
        fleet_doc.pop("min_reader_version", None)
        fleet_doc["nodes"]["localhost"]["billing_accounts"] = ["openai/default"]
        fleet._atomic_write_json(fleet_dir / "fleet.json", fleet_doc)
        _write_probe(fleet_dir, "localhost", "openai/default", "green")
        billing.assert_dispatch_auth(fleet_dir, "localhost", "openai/default")


def test_dispatch_auth_legacy_fleet_missing_membership_field_fails_closed() -> None:
    with tempfile.TemporaryDirectory() as td:
        fleet_dir = Path(td) / "fleet"
        _fixture_fleet(fleet_dir)
        fleet_doc = fleet.read_json(fleet_dir / "fleet.json")
        fleet_doc.pop("schema", None)
        fleet_doc.pop("schema_version", None)
        fleet_doc.pop("min_reader_version", None)
        fleet_doc["nodes"]["localhost"].pop("billing_accounts", None)
        fleet._atomic_write_json(fleet_dir / "fleet.json", fleet_doc)
        _write_probe(fleet_dir, "localhost", "openai/default", "green")
        try:
            billing.assert_dispatch_auth(fleet_dir, "localhost", "openai/default")
            assert_true("should block", False)
        except billing.DispatchAuthError as exc:
            message = str(exc)
            assert_true("hard red", exc.auth_probe == "red")
            assert_true("membership field message", "billing_accounts list" in message)
            assert_true("membership remedy", "account link" in message)


def test_account_unlink_removes_link_and_artifact() -> None:
    with tempfile.TemporaryDirectory() as td:
        fleet_dir = Path(td) / "fleet"
        _fixture_fleet(fleet_dir)
        billing.link_account_to_node(
            fleet_dir,
            "openai/default",
            "localhost",
            runner=green_runner,
        )
        billing.unlink_account_from_node(fleet_dir, "openai/default", "localhost")
        doc = fleet.read_json(fleet_dir / "fleet.json")
        assert_true("unlinked", "openai/default" not in doc["nodes"]["localhost"]["billing_accounts"])
        assert_true(
            "artifact removed",
            billing.read_probe_artifact(fleet_dir, "localhost", "openai/default") is None,
        )


def test_doctor_cli_fleet_json() -> None:
    with tempfile.TemporaryDirectory() as td:
        fleet_dir = Path(td) / "fleet"
        _fixture_fleet(fleet_dir)
        billing.link_account_to_node(
            fleet_dir,
            "openai/default",
            "localhost",
            runner=green_runner,
        )
        import goalflight_doctor as doctor

        payload = doctor.doctor(ROOT, fleet=True, fleet_dir=fleet_dir, fleet_probe=False)
        assert_true("fleet key", "fleet" in payload)
        raw = json.dumps(payload)
        assert_true("auth_probe in json", "auth_probe" in raw)


def test_remote_auth_probe_uses_node_venv_python() -> None:
    captured: list[list[str]] = []

    def ssh_runner(argv: list[str]) -> tuple[int, str, str]:
        captured.append(list(argv))
        return (
            0,
            json.dumps(
                {
                    "schema": billing.AUTH_PROBE_SCHEMA,
                    "account_key": "openai/default",
                    "provider": "openai",
                    "status": "green",
                }
            ),
            "",
        )

    with tempfile.TemporaryDirectory() as td:
        fleet_dir = Path(td) / "fleet"
        _fixture_fleet(fleet_dir, node_id="remote-node")
        fleet_doc = fleet.read_json(fleet_dir / "fleet.json")
        node = fleet_doc["nodes"]["remote-node"]
        node["ssh"] = {"alias": "mac-studio-test", "hostname": "10.0.0.10"}
        node["state_dir"] = "/Users/dev/.goal-flight"
        fleet._atomic_write_json(fleet_dir / "fleet.json", fleet_doc)

        result = billing.link_account_to_node(
            fleet_dir,
            "openai/default",
            "remote-node",
            ssh_runner=ssh_runner,
        )
        assert_true("probe green", result["auth_probe"]["status"] == "green")
        assert_true("ssh captured", bool(captured))
        assert_true(
            "node venv python",
            "/Users/dev/.goal-flight/venvs/acp-0.10/bin/python" in " ".join(captured[0]),
        )


def test_tooling_auth_probe_is_inconclusive_not_red() -> None:
    payload = billing.run_local_auth_probe(
        "openai/default",
        {"accounts": [{"account_key": "openai/default", "provider": "openai"}]},
        runner=tooling_runner,
    )
    assert_true("local 127 inconclusive", payload["status"] == "inconclusive")


def test_remote_tooling_probe_reprobes_instead_of_cached_red() -> None:
    def setup_remote(fleet_dir: Path) -> None:
        _fixture_fleet(fleet_dir, node_id="remote-node")
        fleet_doc = fleet.read_json(fleet_dir / "fleet.json")
        node = fleet_doc["nodes"]["remote-node"]
        node["ssh"] = {"alias": "mac-studio-test", "hostname": "10.0.0.10"}
        node["state_dir"] = "/Users/dev/.goal-flight"
        fleet._atomic_write_json(fleet_dir / "fleet.json", fleet_doc)

    with tempfile.TemporaryDirectory() as td:
        fleet_dir = Path(td) / "fleet"
        setup_remote(fleet_dir)
        result = billing.link_account_to_node(
            fleet_dir,
            "openai/default",
            "remote-node",
            ssh_runner=lambda _a: (127, "", "bare-python-not-found"),
        )
        assert_true("remote 127 inconclusive", result["auth_probe"]["status"] == "inconclusive")
        artifact = billing.read_probe_artifact(fleet_dir, "remote-node", "openai/default")
        assert_true("artifact is not red", artifact is not None and artifact["status"] == "inconclusive")
        try:
            billing.assert_dispatch_auth(fleet_dir, "remote-node", "openai/default")
            assert_true("should block", False)
        except billing.DispatchAuthError as exc:
            assert_true("gate says inconclusive", exc.auth_probe == "inconclusive")

        calls: list[list[str]] = []

        def green_remote(argv: list[str]) -> tuple[int, str, str]:
            calls.append(list(argv))
            return (
                0,
                json.dumps(
                    {
                        "schema": billing.AUTH_PROBE_SCHEMA,
                        "account_key": "openai/default",
                        "provider": "openai",
                        "status": "green",
                    }
                ),
                "",
            )

        summary = billing.fleet_auth_doctor(fleet_dir, refresh=False, ssh_runner=green_remote)
        assert_true("doctor re-probed inconclusive", len(calls) == 1)
        account = summary["nodes"][0]["accounts"][0]
        assert_true("doctor refreshed green", account["auth_probe"] == "green")


def test_remote_auth_denied_json_can_cache_red() -> None:
    with tempfile.TemporaryDirectory() as td:
        fleet_dir = Path(td) / "fleet"
        _fixture_fleet(fleet_dir, node_id="remote-node")
        fleet_doc = fleet.read_json(fleet_dir / "fleet.json")
        node = fleet_doc["nodes"]["remote-node"]
        node["ssh"] = {"alias": "mac-studio-test", "hostname": "10.0.0.10"}
        fleet._atomic_write_json(fleet_dir / "fleet.json", fleet_doc)

        result = billing.link_account_to_node(
            fleet_dir,
            "openai/default",
            "remote-node",
            ssh_runner=lambda _a: (
                1,
                json.dumps(
                    {
                        "schema": billing.AUTH_PROBE_SCHEMA,
                        "account_key": "openai/default",
                        "provider": "openai",
                        "status": "red",
                    }
                ),
                "",
            ),
        )
        assert_true("remote auth denied stays red", result["auth_probe"]["status"] == "red")
        artifact = billing.read_probe_artifact(fleet_dir, "remote-node", "openai/default")
        assert_true("red artifact saved", artifact is not None and artifact["status"] == "red")


def test_grok_auth_probe_green() -> None:
    payload = billing.run_local_auth_probe(
        "grok/shared",
        {"accounts": [{"account_key": "grok/shared", "provider": "grok"}]},
        runner=lambda _a: (0, "logged_in\n", ""),
    )
    assert_true("grok green", payload["status"] == "green")


def test_cursor_auth_probe_uses_status_not_version() -> None:
    assert_true(
        "cursor status argv",
        billing.probe_argv("cursor") == ["cursor-agent", "status"],
    )
    green = billing.run_local_auth_probe(
        "cursor/shared",
        {"accounts": [{"account_key": "cursor/shared", "provider": "cursor"}]},
        runner=lambda _a: (0, "✓ Logged in as simon@example.com\n", ""),
    )
    assert_true("cursor green when logged in", green["status"] == "green")
    login_ok = billing.run_local_auth_probe(
        "cursor/shared",
        {"accounts": [{"account_key": "cursor/shared", "provider": "cursor"}]},
        runner=lambda _a: (0, "Login successful!\n", ""),
    )
    assert_true("cursor green on login successful", login_ok["status"] == "green")
    red = billing.run_local_auth_probe(
        "cursor/shared",
        {"accounts": [{"account_key": "cursor/shared", "provider": "cursor"}]},
        runner=lambda _a: (0, "Not logged in\n", ""),
    )
    assert_true("cursor red when not logged in", red["status"] == "red")
    version_only = billing.run_local_auth_probe(
        "cursor/shared",
        {"accounts": [{"account_key": "cursor/shared", "provider": "cursor"}]},
        runner=lambda _a: (0, "cursor-agent 2026.05.20\n", ""),
    )
    assert_true("cursor version-only inconclusive", version_only["status"] == "inconclusive")
    keychain_locked = billing.run_local_auth_probe(
        "cursor/shared",
        {"accounts": [{"account_key": "cursor/shared", "provider": "cursor"}]},
        runner=lambda _a: (1, "", "login keychain is locked\n"),
    )
    assert_true("cursor keychain locked unchanged", keychain_locked["status"] == "yellow")


def test_dispatch_auth_reprobes_stale_red_and_allows_now_valid() -> None:
    # Seed bug: a STALE red probe (e.g. written before a token was persisted) must NOT block a
    # now-valid token. The gate must re-probe live past the freshness window, not trust the cache.
    with tempfile.TemporaryDirectory() as td:
        fleet_dir = Path(td) / "fleet"
        _fixture_fleet(fleet_dir)
        billing.link_account_to_node(fleet_dir, "openai/default", "localhost", runner=green_runner)
        _write_probe(fleet_dir, "localhost", "openai/default", "red")  # stale (2026-05-24) red
        calls: list[int] = []

        def reprobe_green(fleet_dir, node_id, account_key, **_kw):
            calls.append(1)
            return {
                "schema": billing.AUTH_PROBE_SCHEMA,
                "account_key": account_key,
                "status": "green",
                "probed_at": fleet.iso(),
            }

        billing.assert_dispatch_auth(fleet_dir, "localhost", "openai/default", reprobe=reprobe_green)
        assert_true("stale red triggered a live re-probe", len(calls) == 1)


def test_dispatch_auth_reprobes_stale_green_and_blocks_revoked() -> None:
    # Security hole: a STALE green probe must NOT pass a since-revoked token. Re-probe -> red -> block.
    with tempfile.TemporaryDirectory() as td:
        fleet_dir = Path(td) / "fleet"
        _fixture_fleet(fleet_dir)
        billing.link_account_to_node(fleet_dir, "openai/default", "localhost", runner=green_runner)
        _write_probe(fleet_dir, "localhost", "openai/default", "green")  # stale (2026-05-24) green
        calls: list[int] = []

        def reprobe_red(fleet_dir, node_id, account_key, **_kw):
            calls.append(1)
            return {
                "schema": billing.AUTH_PROBE_SCHEMA,
                "account_key": account_key,
                "status": "red",
                "probed_at": fleet.iso(),
            }

        try:
            billing.assert_dispatch_auth(fleet_dir, "localhost", "openai/default", reprobe=reprobe_red)
            assert_true("should block revoked-on-reprobe", False)
        except billing.DispatchAuthError as exc:
            assert_true("stale green triggered a live re-probe", len(calls) == 1)
            assert_true("revoked token blocked red", exc.auth_probe == "red")


def test_dispatch_auth_trusts_fresh_probe_without_reprobe() -> None:
    # A FRESH cached green must be trusted as-is — no needless live re-probe per dispatch.
    with tempfile.TemporaryDirectory() as td:
        fleet_dir = Path(td) / "fleet"
        _fixture_fleet(fleet_dir)
        billing.link_account_to_node(fleet_dir, "openai/default", "localhost", runner=green_runner)
        calls: list[int] = []

        def reprobe_should_not_run(fleet_dir, node_id, account_key, **_kw):
            calls.append(1)
            return {"status": "red", "probed_at": fleet.iso()}

        billing.assert_dispatch_auth(
            fleet_dir, "localhost", "openai/default", reprobe=reprobe_should_not_run
        )
        assert_true("fresh probe was trusted without re-probe", len(calls) == 0)


def test_dispatch_auth_default_reprobe_gated_on_live_ssh() -> None:
    # The default (real-SSH) re-probe must run ONLY under GOALFLIGHT_LIVE_SSH=1, so a non-live
    # --exec never SSHes before assert_live_ssh_opt_in refuses it. Covers the production
    # default-reprobe path that the injected-reprobe poison-pairs don't exercise.
    with tempfile.TemporaryDirectory() as td:
        fleet_dir = Path(td) / "fleet"
        _fixture_fleet(fleet_dir)
        billing.link_account_to_node(fleet_dir, "openai/default", "localhost", runner=green_runner)
        _write_probe(fleet_dir, "localhost", "openai/default", "green")  # stale green
        calls: list[int] = []
        orig = billing.run_node_auth_probe
        billing.run_node_auth_probe = lambda *a, **k: (
            calls.append(1) or {"status": "green", "probed_at": fleet.iso()}
        )
        old_env = os.environ.get("GOALFLIGHT_LIVE_SSH")
        try:
            os.environ.pop("GOALFLIGHT_LIVE_SSH", None)
            billing.assert_dispatch_auth(fleet_dir, "localhost", "openai/default")
            assert_true("no default re-probe without LIVE_SSH", len(calls) == 0)
            os.environ["GOALFLIGHT_LIVE_SSH"] = "1"
            billing.assert_dispatch_auth(fleet_dir, "localhost", "openai/default")
            assert_true("default re-probe runs with LIVE_SSH=1", len(calls) == 1)
        finally:
            billing.run_node_auth_probe = orig
            if old_env is None:
                os.environ.pop("GOALFLIGHT_LIVE_SSH", None)
            else:
                os.environ["GOALFLIGHT_LIVE_SSH"] = old_env


def main() -> None:
    for test in (
        test_account_link_runs_probe_and_writes_artifact,
        test_dispatch_auth_refuses_stale_green_probe_without_membership,
        test_dispatch_auth_allows_linked_member_with_green_probe,
        test_openai_probe_uses_authenticated_models_check,
        test_openai_bare_exit_zero_probe_is_inconclusive,
        test_openai_empty_models_probe_is_inconclusive,
        test_openai_working_token_probe_is_green_and_dispatch_allowed,
        test_openai_revoked_token_probe_is_red_and_dispatch_gate_blocks,
        test_anthropic_probe_uses_auth_status_not_version,
        test_anthropic_auth_status_json_is_validated,
        test_anthropic_logged_in_identity_denial_words_stays_green,
        test_anthropic_logged_in_stderr_denial_is_red,
        test_account_link_cli_fails_closed_on_red_probe,
        test_doctor_fleet_shape,
        test_dispatch_gate_blocks_red_auth,
        test_dispatch_auth_reprobes_stale_red_and_allows_now_valid,
        test_dispatch_auth_reprobes_stale_green_and_blocks_revoked,
        test_dispatch_auth_trusts_fresh_probe_without_reprobe,
        test_dispatch_auth_default_reprobe_gated_on_live_ssh,
        test_dispatch_auth_refuses_recorded_controller_owner_mismatch,
        test_dispatch_auth_allows_legacy_usable_fleet_json,
        test_dispatch_auth_legacy_fleet_missing_membership_field_fails_closed,
        test_account_unlink_removes_link_and_artifact,
        test_doctor_cli_fleet_json,
        test_remote_auth_probe_uses_node_venv_python,
        test_tooling_auth_probe_is_inconclusive_not_red,
        test_remote_tooling_probe_reprobes_instead_of_cached_red,
        test_remote_auth_denied_json_can_cache_red,
        test_grok_auth_probe_green,
        test_cursor_auth_probe_uses_status_not_version,
    ):
        test()
        print(f"PASS {test.__name__}")


if __name__ == "__main__":
    main()
