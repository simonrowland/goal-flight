#!/usr/bin/env python3
"""Tests for fleet ferry primitive and convergent salvage."""

from __future__ import annotations

from support import skip_posix_on_native_windows

skip_posix_on_native_windows("fleet ferry fixtures use POSIX paths and symlinks")

import json
import base64
import os
import re
import sys
import tempfile
import io
import subprocess
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import goalflight_fleet as fleet
import goalflight_fleet_ferry as ferry
import goalflight_fleet_ferry_preflight as ferry_preflight
import goalflight_fleet_ssh as fleet_ssh


def assert_true(name: str, condition: bool) -> None:
    if not condition:
        raise AssertionError(name)


def _fixture_fleet(fleet_dir: Path) -> None:
    fleet.bootstrap(fleet_dir)
    fleet_doc = fleet.read_json(fleet_dir / "fleet.json")
    fleet_doc["nodes"] = {
        "localhost": {
            "node_id": "localhost",
            "status": "active",
            "ssh": {"alias": "localhost", "hostname": "localhost"},
            "repo_root": str(ROOT),
            "state_dir": "/remote",
            "billing_accounts": [],
            "added_at": "2026-06-12T12:00:00+00:00",
        }
    }
    fleet._atomic_write_json(fleet_dir / "fleet.json", fleet_doc)


@contextmanager
def live_ssh_env(value: str | None):
    old = os.environ.get("GOALFLIGHT_LIVE_SSH")
    if value is None:
        os.environ.pop("GOALFLIGHT_LIVE_SSH", None)
    else:
        os.environ["GOALFLIGHT_LIVE_SSH"] = value
    try:
        yield
    finally:
        if old is None:
            os.environ.pop("GOALFLIGHT_LIVE_SSH", None)
        else:
            os.environ["GOALFLIGHT_LIVE_SSH"] = old


def _files_from(argv: list[str]) -> list[str]:
    path = Path(argv[argv.index("--files-from") + 1])
    return path.read_text().splitlines()


def _remote_preflight_reply(
    argv: list[str],
    *,
    sizes: dict[str, int] | None = None,
    ctimes: dict[str, int] | None = None,
    symlink_paths: set[str] | None = None,
    expanded_paths: dict[str, list[str]] | None = None,
) -> tuple[int, str, str]:
    joined = " ".join(argv)
    match = re.search(r"--payload-b64\s+'?([A-Za-z0-9+/=]+)'?", joined)
    assert_true("remote preflight payload present", match is not None)
    payload = json.loads(base64.b64decode(match.group(1)).decode("utf-8"))
    root = payload["root"]
    sizes = sizes or {}
    ctimes = ctimes or {}
    symlink_paths = symlink_paths or set()
    expanded_paths = expanded_paths or {}
    checked = []
    for requested in payload["files"]:
        for rel in expanded_paths.get(requested, [requested]):
            checked.append(
                {
                    "path": rel,
                    "realpath": str(Path(root) / rel),
                    "exists": True,
                    "nlink": 1,
                    "size": sizes.get(rel, 1),
                    "ctime_ns": ctimes.get(rel, 1),
                    "is_regular_file": rel not in symlink_paths,
                }
            )
    return 0, json.dumps({"ok": True, "checked": checked}), ""


def _write_dest_files(argv: list[str], files: list[str], prefix: str = "data") -> None:
    dest = Path(argv[-1])
    for rel in files:
        path = dest / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"{prefix}:{rel}\n")


def _staging(fleet_dir: Path, *parts: str) -> Path:
    return ferry.controller_staging_root(fleet_dir).joinpath(*parts)


def test_ferry_happy_path_both_directions_and_receipt() -> None:
    with live_ssh_env("1"):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            fleet_dir = base / "fleet"
            src = base / "src"
            dst = _staging(fleet_dir, "dst")
            src.mkdir()
            (src / "safe.txt").write_text("safe\n")
            _fixture_fleet(fleet_dir)
            captured: list[list[str]] = []

            def runner(argv: list[str]) -> tuple[int, str, str]:
                captured.append(list(argv))
                if argv[0] == "ssh" and "remote-preflight" in " ".join(argv):
                    return _remote_preflight_reply(argv)
                if argv[0] == "rsync" and argv[-1].endswith("/"):
                    files = _files_from(argv)
                    if argv[-1].startswith(str(dst)):
                        _write_dest_files(argv, files, prefix="pull")
                return 0, "", ""

            push = ferry.execute_ferry(
                fleet_dir,
                node_id="localhost",
                direction="push",
                src_root=str(src),
                dst_root="/remote/worktree",
                files=["safe.txt"],
                purpose="unit-push",
                runner=runner,
            ).to_dict()
            pull = ferry.execute_ferry(
                fleet_dir,
                node_id="localhost",
                direction="pull",
                src_root="/remote/worktree",
                dst_root=str(dst),
                files=["safe.txt"],
                purpose="unit-pull",
                runner=runner,
            ).to_dict()
            assert_true("push ok", push["ok"] is True)
            assert_true("pull ok", pull["ok"] is True)
            assert_true("purpose", pull["purpose"] == "unit-pull")
            assert_true("receipt schema bumped", pull["schema"] == "goalflight.fleet.ferry.receipt.v2")
            assert_true("complete receipt marked", pull["incomplete"] is False)
            assert_true("explicit src node", pull["src"]["node"] == "localhost")
            assert_true("explicit dst node", pull["dst"]["node"] == "controller")
            assert_true("two rsyncs", len([argv for argv in captured if argv[0] == "rsync"]) == 2)
            rsync_argv = next(argv for argv in captured if argv[0] == "rsync")
            assert_true("rsync compression enabled", "-z" in rsync_argv)
            assert_true("rsync checksum retained for convergence correctness", "--checksum" in rsync_argv)
            assert_true("configured rsync size cap", f"--max-size={ferry.DEFAULT_MAX_FILE_BYTES}" in rsync_argv)
            audit = (fleet_dir / "audit" / "ferry.jsonl").read_text().splitlines()
            assert_true("audit rows", len(audit) == 2)
            assert_true("audit purpose", json.loads(audit[-1])["purpose"] == "unit-pull")


def test_ferry_deny_requested_and_expanded_paths() -> None:
    with live_ssh_env("1"):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            fleet_dir = base / "fleet"
            src = base / "src"
            src.mkdir()
            _fixture_fleet(fleet_dir)
            called = False

            def runner(_argv: list[str]) -> tuple[int, str, str]:
                nonlocal called
                called = True
                return 0, "", ""

            try:
                ferry.execute_ferry(
                    fleet_dir,
                    node_id="localhost",
                    direction="pull",
                    src_root="/remote/worktree",
                    dst_root=str(_staging(fleet_dir, "deny-requested")),
                    files=["auth.json"],
                    purpose="deny-requested",
                    runner=runner,
                )
                assert_true("requested deny should raise", False)
            except ferry.FerryDenyError as exc:
                assert_true("teaching message", "resident account credentials" in str(exc))
            (src / "bundle").mkdir()
            (src / "bundle" / "safe.txt").write_text("safe\n")
            (src / "bundle" / "auth.json").write_text("{}\n")
            try:
                ferry.execute_ferry(
                    fleet_dir,
                    node_id="localhost",
                    direction="push",
                    src_root=str(src),
                    dst_root="/remote/worktree",
                    files=["bundle"],
                    purpose="deny-expanded",
                    runner=runner,
                )
                assert_true("expanded deny should raise", False)
            except ferry.FerryDenyError as exc:
                assert_true("expanded path named", "bundle/auth.json" in str(exc))
            assert_true("runner never called", called is False)


def test_direct_ferry_transfers_explicit_scratch_named_paths() -> None:
    paths = ["tests/.pytest_cache/fixture.txt", "pkg/__pycache__/m.pyc"]
    with live_ssh_env("1"):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            fleet_dir = base / "fleet"
            dst = _staging(fleet_dir, "scratch-named-direct-pull")
            _fixture_fleet(fleet_dir)

            def runner(argv: list[str]) -> tuple[int, str, str]:
                if argv[0] == "ssh" and "remote-preflight" in " ".join(argv):
                    return _remote_preflight_reply(argv)
                if argv[0] == "rsync":
                    _write_dest_files(argv, _files_from(argv), prefix="direct")
                    return 0, "", ""
                return 1, "", f"unexpected argv: {' '.join(argv)}"

            receipt = ferry.execute_ferry(
                fleet_dir,
                node_id="localhost",
                direction="pull",
                src_root="/remote/worktree",
                dst_root=str(dst),
                files=paths,
                purpose="scratch-named-direct-pull",
                runner=runner,
            ).to_dict()

            assert_true("direct ferry transfers requested scratch-shaped paths", receipt["files"] == paths)
            assert_true("direct ferry has no scratch exclusions", receipt["excluded_files"] == [])
            assert_true("direct ferry remains complete", receipt["incomplete"] is False)


def test_ferry_rejects_path_tricks_and_symlink_escape() -> None:
    with live_ssh_env("1"):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            fleet_dir = base / "fleet"
            src = base / "src"
            src.mkdir()
            outside = base / "outside.txt"
            outside.write_text("outside\n")
            (src / "link.txt").symlink_to(outside)
            (src / "auth.json").write_text("{}\n")
            (src / "safe-link.txt").symlink_to(src / "auth.json")
            _fixture_fleet(fleet_dir)
            try:
                ferry.execute_ferry(
                    fleet_dir,
                    node_id="localhost",
                    direction="push",
                    src_root=str(src),
                    dst_root="/remote/worktree",
                    files=["../outside.txt"],
                    purpose="path-trick",
                    runner=lambda _a: (0, "", ""),
                )
                assert_true("parent traversal should raise", False)
            except ferry.FerryError:
                pass
            try:
                ferry.execute_ferry(
                    fleet_dir,
                    node_id="localhost",
                    direction="push",
                    src_root=str(src),
                    dst_root="/remote/worktree",
                    files=["link.txt"],
                    purpose="symlink-escape",
                    runner=lambda _a: (0, "", ""),
                )
                assert_true("symlink escape should raise", False)
            except ferry.FerryError as exc:
                assert_true("escape named", "escape" in str(exc) or "outside declared root" in str(exc))
            try:
                ferry.execute_ferry(
                    fleet_dir,
                    node_id="localhost",
                    direction="push",
                    src_root=str(src),
                    dst_root="/remote/worktree",
                    files=["safe-link.txt"],
                    purpose="symlink-deny",
                    runner=lambda _a: (0, "", ""),
                )
                assert_true("symlink to denied target should raise", False)
            except ferry.FerryDenyError as exc:
                assert_true("realpath deny", "auth.json" in str(exc))


def test_credential_deny_patterns_cover_case_variants_and_common_secret_names() -> None:
    cases = {
        "AUTH.JSON": "auth.json",
        "auth.json.bak": "auth.json*",
        "auth.json~": "auth.json*",
        "auth-backup.json": "*auth*.json",
        "id_ed25519": "id_ed25519*",
        "credentials.json": "credentials.json",
        "oauth.json": "oauth.json",
        ".netrc": ".netrc",
        ".env": ".env",
        ".env.local": ".env.*",
    }
    for rel, expected in cases.items():
        reason = ferry.credential_deny_reason(rel)
        assert_true(f"{rel} denied", reason == expected)


def test_ferry_rejects_newline_split_and_pull_key_before_runner() -> None:
    with live_ssh_env("1"):
        with tempfile.TemporaryDirectory() as td:
            fleet_dir = Path(td) / "fleet"
            _fixture_fleet(fleet_dir)
            called = False

            def runner(_argv: list[str]) -> tuple[int, str, str]:
                nonlocal called
                called = True
                return 0, "", ""

            try:
                ferry.execute_ferry(
                    fleet_dir,
                    node_id="localhost",
                    direction="pull",
                    src_root="/remote/worktree",
                    dst_root=str(_staging(fleet_dir, "newline")),
                    files=["safe.txt\nauth.json"],
                    purpose="newline",
                    runner=runner,
                )
                assert_true("newline path should raise", False)
            except ferry.FerryError as exc:
                assert_true("single-line named", "single-line" in str(exc))
            try:
                ferry.execute_ferry(
                    fleet_dir,
                    node_id="localhost",
                    direction="pull",
                    src_root="/remote/worktree",
                    dst_root=str(_staging(fleet_dir, "key")),
                    files=["id_ed25519"],
                    purpose="pull-key",
                    runner=runner,
                )
                assert_true("pull key should raise", False)
            except ferry.FerryDenyError as exc:
                assert_true("pattern named", "id_ed25519*" in str(exc))
            assert_true("runner never called", called is False)


def test_ferry_live_ssh_gate_fails_closed() -> None:
    with live_ssh_env(None):
        with tempfile.TemporaryDirectory() as td:
            fleet_dir = Path(td) / "fleet"
            _fixture_fleet(fleet_dir)
            called = False

            def runner(_argv: list[str]) -> tuple[int, str, str]:
                nonlocal called
                called = True
                return 0, "", ""

            try:
                ferry.execute_ferry(
                    fleet_dir,
                    node_id="localhost",
                    direction="pull",
                    src_root="/remote/worktree",
                    dst_root=str(_staging(fleet_dir, "gate")),
                    files=["safe.txt"],
                    purpose="gate",
                    runner=runner,
                )
                assert_true("gate should raise", False)
            except ferry.FerryError as exc:
                assert_true("live ssh named", "GOALFLIGHT_LIVE_SSH=1" in str(exc))
            assert_true("runner not called", called is False)


def test_remote_preflight_denies_symlink_and_hardlink_credentials() -> None:
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        remote = base / "remote"
        remote.mkdir()
        home_auth = base / "home" / ".codex" / "auth.json"
        home_auth.parent.mkdir(parents=True)
        home_auth.write_text("{}\n")
        (remote / "safe-link.txt").symlink_to(home_auth)
        try:
            ferry._remote_preflight_payload(
                {"root": str(remote), "allowed_roots": [str(remote)], "files": ["safe-link.txt"]}
            )
            assert_true("symlink credential should raise", False)
        except ferry.FerryDenyError as exc:
            assert_true("symlink pattern", ".codex/" in str(exc) or "auth.json" in str(exc))

        credential_dir = remote / ".codex"
        credential_dir.mkdir()
        credential = credential_dir / "auth.json"
        credential.write_text("{}\n")
        hardlink = remote / "innocent-name.txt"
        os.link(credential, hardlink)
        try:
            ferry._remote_preflight_payload(
                {"root": str(remote), "allowed_roots": [str(remote)], "files": ["innocent-name.txt"]}
            )
            assert_true("hardlink credential should raise", False)
        except ferry.FerryDenyError as exc:
            assert_true("hardlink alias pattern", ".codex/" in str(exc) or "auth.json" in str(exc))


def test_push_hardlink_alias_denied_before_rsync() -> None:
    with live_ssh_env("1"):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            fleet_dir = base / "fleet"
            src = base / "src"
            src.mkdir()
            credential = src / ".codex" / "auth.json"
            credential.parent.mkdir()
            credential.write_text("{}\n")
            os.link(credential, src / "notes.txt")
            _fixture_fleet(fleet_dir)
            calls: list[str] = []

            def runner(argv: list[str]) -> tuple[int, str, str]:
                calls.append(argv[0])
                return 0, "", ""

            try:
                ferry.execute_ferry(
                    fleet_dir,
                    node_id="localhost",
                    direction="push",
                    src_root=str(src),
                    dst_root="/remote/worktree",
                    files=["notes.txt"],
                    purpose="push-hardlink-poison",
                    runner=runner,
                )
                assert_true("push hardlink alias should raise", False)
            except ferry.FerryDenyError as exc:
                assert_true("production hardlink alias deny", ".codex/" in str(exc) or "auth.json" in str(exc))
            assert_true("runner never called", calls == [])


def test_push_stages_scanned_bytes_before_rsync_source_swap() -> None:
    with live_ssh_env("1"):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            fleet_dir = base / "fleet"
            src = base / "src"
            src.mkdir()
            live_safe = src / "safe.txt"
            live_safe.write_text("public\n")
            credential = src / ".codex" / "auth.json"
            credential.parent.mkdir()
            credential.write_text('{"access_token":"secret-token-value"}\n')
            _fixture_fleet(fleet_dir)
            rsync_sources: list[Path] = []
            sent_payloads: list[str] = []

            def runner(argv: list[str]) -> tuple[int, str, str]:
                if argv[0] == "ssh":
                    return _remote_preflight_reply(argv)
                if argv[0] == "rsync":
                    assert_true("one safe transfer", _files_from(argv) == ["safe.txt"])
                    live_safe.unlink()
                    os.link(credential, live_safe)
                    rsync_source = Path(argv[-2])
                    rsync_sources.append(rsync_source)
                    sent_payloads.append((rsync_source / "safe.txt").read_text())
                    return 0, "", ""
                return 1, "", f"unexpected argv: {' '.join(argv)}"

            receipt = ferry.execute_ferry(
                fleet_dir,
                node_id="localhost",
                direction="push",
                src_root=str(src),
                dst_root="/remote/worktree",
                files=["safe.txt"],
                purpose="push-staged-source-swap",
                runner=runner,
            ).to_dict()
            assert_true("push ok", receipt["ok"] is True)
            assert_true("rsync did not read live source", rsync_sources and rsync_sources[0] != src)
            assert_true("staged public bytes sent", sent_payloads == ["public\n"])
            assert_true("live source poisoned after staging", live_safe.stat().st_ino == credential.stat().st_ino)
            assert_true("push staging cleaned", not Path(receipt["push_staging_root"]).exists())


def test_root_confinement_rejects_pull_dst_and_remote_worktree_before_runner() -> None:
    with live_ssh_env("1"):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            fleet_dir = base / "fleet"
            _fixture_fleet(fleet_dir)
            called = False

            def runner(_argv: list[str]) -> tuple[int, str, str]:
                nonlocal called
                called = True
                return 0, "", ""

            try:
                ferry.execute_ferry(
                    fleet_dir,
                    node_id="localhost",
                    direction="pull",
                    src_root="/remote/worktree",
                    dst_root=str(base / "outside"),
                    files=["safe.txt"],
                    purpose="bad-dst",
                    runner=runner,
                )
                assert_true("outside pull destination should raise", False)
            except ferry.FerryError as exc:
                assert_true("staging named", "fleet staging root" in str(exc))
            try:
                ferry.salvage_worktree(
                    fleet_dir,
                    node_id="localhost",
                    worktree_path="/tmp/outside-worktree",
                    out_dir=_staging(fleet_dir, "salvage"),
                    runner=runner,
                    sleep_s=0,
                )
                assert_true("outside worktree should raise", False)
            except ferry.FerryError as exc:
                assert_true("declared root named", "declared node root" in str(exc))
            assert_true("no ssh or rsync before root rejection", called is False)


def test_pull_remote_preflight_deny_fires_before_rsync() -> None:
    with live_ssh_env("1"):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            fleet_dir = base / "fleet"
            remote_root = base / "remote" / "worktree"
            remote_root.mkdir(parents=True)
            credential = remote_root / ".codex" / "auth.json"
            credential.parent.mkdir()
            credential.write_text("{}\n")
            os.link(credential, remote_root / "safe.txt")
            _fixture_fleet(fleet_dir)
            fleet_doc = fleet.read_json(fleet_dir / "fleet.json")
            fleet_doc["nodes"]["localhost"]["repo_root"] = str(remote_root)
            fleet_doc["nodes"]["localhost"]["state_dir"] = str(base / "remote")
            fleet._atomic_write_json(fleet_dir / "fleet.json", fleet_doc)
            calls: list[str] = []

            def runner(argv: list[str]) -> tuple[int, str, str]:
                calls.append(argv[0])
                if argv[0] == "ssh" and "remote-preflight" in " ".join(argv):
                    try:
                        payload = ferry._remote_preflight_payload(
                            {
                                "root": str(remote_root),
                                "allowed_roots": [str(base / "remote")],
                                "files": ["safe.txt"],
                            }
                        )
                    except Exception as exc:
                        return 2, json.dumps(ferry._remote_preflight_error(exc)), ""
                    return 0, json.dumps(payload), ""
                if argv[0] == "rsync":
                    assert_true("rsync must not run after deny", False)
                return 0, "", ""

            try:
                ferry.execute_ferry(
                    fleet_dir,
                    node_id="localhost",
                    direction="pull",
                    src_root=str(remote_root),
                    dst_root=str(_staging(fleet_dir, "preflight-deny")),
                    files=["safe.txt"],
                    purpose="preflight-deny",
                    runner=runner,
                )
                assert_true("remote preflight deny should raise", False)
            except ferry.FerryDenyError as exc:
                assert_true("matched pattern named", "auth.json" in str(exc))
            assert_true("preflight called", calls == ["ssh"])


def test_partial_pull_cleanup_on_rsync_failure() -> None:
    with live_ssh_env("1"):
        with tempfile.TemporaryDirectory() as td:
            fleet_dir = Path(td) / "fleet"
            _fixture_fleet(fleet_dir)
            dst = _staging(fleet_dir, "partial")

            def runner(argv: list[str]) -> tuple[int, str, str]:
                if argv[0] == "ssh" and "remote-preflight" in " ".join(argv):
                    return _remote_preflight_reply(argv)
                if argv[0] == "rsync":
                    _write_dest_files(argv, _files_from(argv), prefix="partial")
                    return 23, "", "mid-transfer failure"
                return 1, "", "unexpected"

            try:
                ferry.execute_ferry(
                    fleet_dir,
                    node_id="localhost",
                    direction="pull",
                    src_root="/remote/worktree",
                    dst_root=str(dst),
                    files=["secret-ish.txt"],
                    purpose="partial-cleanup",
                    runner=runner,
                )
                assert_true("rsync failure should raise", False)
            except ferry.FerryError as exc:
                assert_true("rsync failure named", "rsync ferry failed" in str(exc))
            assert_true("partial file removed", not (dst / "secret-ish.txt").exists())


def test_pull_quarantine_denies_private_key_content_before_promotion() -> None:
    with live_ssh_env("1"):
        with tempfile.TemporaryDirectory() as td:
            fleet_dir = Path(td) / "fleet"
            _fixture_fleet(fleet_dir)
            dst = _staging(fleet_dir, "private-key-content")

            def runner(argv: list[str]) -> tuple[int, str, str]:
                if argv[0] == "ssh" and "remote-preflight" in " ".join(argv):
                    return _remote_preflight_reply(argv)
                if argv[0] == "rsync":
                    path = Path(argv[-1]) / "safe.txt"
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text("-----BEGIN OPENSSH PRIVATE KEY-----\nnot-a-real-key\n")
                    return 0, "", ""
                return 1, "", f"unexpected argv: {' '.join(argv)}"

            try:
                ferry.execute_ferry(
                    fleet_dir,
                    node_id="localhost",
                    direction="pull",
                    src_root="/remote/worktree",
                    dst_root=str(dst),
                    files=["safe.txt"],
                    purpose="pull-private-key-content",
                    runner=runner,
                )
                assert_true("private key content should raise", False)
            except ferry.FerryDenyError as exc:
                assert_true("private key signature named", "private-key header" in str(exc))
            assert_true("private key not promoted", not (dst / "safe.txt").exists())


def test_pull_quarantine_denies_provider_token_json_before_promotion() -> None:
    with live_ssh_env("1"):
        with tempfile.TemporaryDirectory() as td:
            fleet_dir = Path(td) / "fleet"
            _fixture_fleet(fleet_dir)
            dst = _staging(fleet_dir, "provider-token-content")

            def runner(argv: list[str]) -> tuple[int, str, str]:
                if argv[0] == "ssh" and "remote-preflight" in " ".join(argv):
                    return _remote_preflight_reply(argv)
                if argv[0] == "rsync":
                    path = Path(argv[-1]) / "safe.txt"
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(json.dumps({"access_token": "secret-token-value"}) + "\n")
                    return 0, "", ""
                return 1, "", f"unexpected argv: {' '.join(argv)}"

            try:
                ferry.execute_ferry(
                    fleet_dir,
                    node_id="localhost",
                    direction="pull",
                    src_root="/remote/worktree",
                    dst_root=str(dst),
                    files=["safe.txt"],
                    purpose="pull-provider-token-content",
                    runner=runner,
                )
                assert_true("provider token content should raise", False)
            except ferry.FerryDenyError as exc:
                assert_true("provider token key named", "access_token" in str(exc))
            assert_true("provider token not promoted", not (dst / "safe.txt").exists())


def test_pull_quarantine_allows_non_provider_token_json() -> None:
    with live_ssh_env("1"):
        with tempfile.TemporaryDirectory() as td:
            fleet_dir = Path(td) / "fleet"
            _fixture_fleet(fleet_dir)
            dst = _staging(fleet_dir, "non-provider-token-content")

            def runner(argv: list[str]) -> tuple[int, str, str]:
                if argv[0] == "ssh" and "remote-preflight" in " ".join(argv):
                    return _remote_preflight_reply(argv)
                if argv[0] == "rsync":
                    path = Path(argv[-1]) / "safe.txt"
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(json.dumps({"fencing_token": "not-provider-token"}) + "\n")
                    return 0, "", ""
                return 1, "", f"unexpected argv: {' '.join(argv)}"

            receipt = ferry.execute_ferry(
                fleet_dir,
                node_id="localhost",
                direction="pull",
                src_root="/remote/worktree",
                dst_root=str(dst),
                files=["safe.txt"],
                purpose="pull-non-provider-token-content",
                runner=runner,
            ).to_dict()
            assert_true("pull ok", receipt["ok"] is True)
            assert_true("non-provider token promoted", (dst / "safe.txt").exists())


def test_pull_refuses_source_changed_during_rsync() -> None:
    with live_ssh_env("1"):
        with tempfile.TemporaryDirectory() as td:
            fleet_dir = Path(td) / "fleet"
            _fixture_fleet(fleet_dir)
            dst = _staging(fleet_dir, "pull-source-race")
            dst.mkdir(parents=True)
            (dst / "safe.txt").write_text("previous local bytes\n")
            preflight_calls = 0

            def runner(argv: list[str]) -> tuple[int, str, str]:
                nonlocal preflight_calls
                joined = " ".join(argv)
                if argv[0] == "ssh" and "remote-preflight" in joined:
                    preflight_calls += 1
                    return _remote_preflight_reply(
                        argv,
                        sizes={"safe.txt": 10},
                        ctimes={"safe.txt": preflight_calls},
                    )
                if argv[0] == "rsync":
                    assert_true("rsync uses cap refusal", "--max-size=20" in argv)
                    return 0, "", ""
                return 1, "", f"unexpected argv: {joined}"

            receipt = ferry.execute_ferry(
                fleet_dir,
                node_id="localhost",
                direction="pull",
                src_root="/remote/worktree",
                dst_root=str(dst),
                files=["safe.txt"],
                purpose="source-race-test",
                runner=runner,
                max_file_bytes=20,
            ).to_dict()

            assert_true("postflight ran", preflight_calls == 2)
            assert_true("source race marks receipt incomplete", receipt["incomplete"] is True)
            assert_true("stale seeded copy is not reported as received", receipt["files"] == [])
            refusal = next(entry for entry in receipt["excluded_files"] if entry["path"] == "safe.txt")
            assert_true("source race rule reported", refusal["rule"] == "source-changed-during-rsync")
            assert_true("previous destination bytes remain", (dst / "safe.txt").read_text() == "previous local bytes\n")


def test_pull_refuses_symlink_changed_during_rsync() -> None:
    with live_ssh_env("1"):
        with tempfile.TemporaryDirectory() as td:
            fleet_dir = Path(td) / "fleet"
            _fixture_fleet(fleet_dir)
            dst = _staging(fleet_dir, "pull-symlink-race")
            dst.mkdir(parents=True)
            (dst / "safe-link").symlink_to("old-target")
            preflight_calls = 0

            def runner(argv: list[str]) -> tuple[int, str, str]:
                nonlocal preflight_calls
                joined = " ".join(argv)
                if argv[0] == "ssh" and "remote-preflight" in joined:
                    preflight_calls += 1
                    return _remote_preflight_reply(
                        argv,
                        sizes={"safe-link": 10},
                        ctimes={"safe-link": preflight_calls},
                        symlink_paths={"safe-link"},
                    )
                if argv[0] == "rsync":
                    return 0, "", ""
                return 1, "", f"unexpected argv: {joined}"

            receipt = ferry.execute_ferry(
                fleet_dir,
                node_id="localhost",
                direction="pull",
                src_root="/remote/worktree",
                dst_root=str(dst),
                files=["safe-link"],
                purpose="symlink-source-race-test",
                runner=runner,
                max_file_bytes=20,
            ).to_dict()

            assert_true("symlink postflight ran", preflight_calls == 2)
            assert_true("symlink race marks receipt incomplete", receipt["incomplete"] is True)
            assert_true("stale seeded symlink is not reported as received", receipt["files"] == [])
            refusal = next(entry for entry in receipt["excluded_files"] if entry["path"] == "safe-link")
            assert_true("symlink race rule reported", refusal["rule"] == "source-changed-during-rsync")
            assert_true("previous destination symlink remains", (dst / "safe-link").is_symlink())


class SalvageRunner:
    def __init__(
        self,
        porcelain: str | list[str],
        diffs: list[str],
        *,
        remote_sizes: dict[str, int | list[int]] | None = None,
        expanded_paths: dict[str, list[str]] | None = None,
    ) -> None:
        self.porcelains = [porcelain] if isinstance(porcelain, str) else list(porcelain)
        self.diffs = list(diffs)
        self.remote_sizes = remote_sizes or {}
        self.expanded_paths = expanded_paths or {}
        self.rsync_files: list[list[str]] = []
        self.status_calls = 0
        self.preflight_calls = 0

    def __call__(self, argv: list[str]) -> tuple[int, str, str]:
        joined = " ".join(argv)
        if argv[0] == "ssh" and "remote-preflight" in joined:
            self.preflight_calls += 1
            sizes: dict[str, int] = {}
            for path, values in self.remote_sizes.items():
                if isinstance(values, list):
                    sizes[path] = values[min(self.preflight_calls - 1, len(values) - 1)]
                else:
                    sizes[path] = values
            return _remote_preflight_reply(argv, sizes=sizes, expanded_paths=self.expanded_paths)
        if argv[0] == "ssh" and " status " in f" {joined} ":
            idx = min(self.status_calls, len(self.porcelains) - 1)
            self.status_calls += 1
            return 0, self.porcelains[idx], ""
        if argv[0] == "rsync":
            files = _files_from(argv)
            self.rsync_files.append(files)
            _write_dest_files(argv, files, prefix=f"pass{len(self.rsync_files)}")
            if "--itemize-changes" in argv:
                return 0, self.diffs.pop(0) if self.diffs else "", ""
            return 0, "", ""
        return 1, "", f"unexpected argv: {joined}"


def test_salvage_porcelain_file_list_transfer_and_convergence() -> None:
    with live_ssh_env("1"):
        with tempfile.TemporaryDirectory() as td:
            fleet_dir = Path(td) / "fleet"
            _fixture_fleet(fleet_dir)
            out_dir = _staging(fleet_dir, "salvage")
            runner = SalvageRunner(
                " M src/app.py\n?? notes.txt\n",
                [">fcs....... src/app.py\n", "", ""],
            )
            manifest = ferry.salvage_worktree(
                fleet_dir,
                node_id="localhost",
                worktree_path="/remote/worktree",
                out_dir=out_dir,
                runner=runner,
                sleep_s=0,
            )
            assert_true("targets parsed", manifest["target_files"] == ["src/app.py", "notes.txt"])
            assert_true("initial rsync file list", runner.rsync_files[0] == ["src/app.py", "notes.txt"])
            assert_true("converged", manifest["converged"] is True)
            assert_true("converged after three checks", len(manifest["iterations"]) == 3)
            assert_true("manifest exists", Path(manifest["manifest_path"]).exists())
            assert_true("hash present", bool(manifest["files"][0].get("sha256")))


def test_salvage_bounds_at_ten_with_liveness_signal() -> None:
    with live_ssh_env("1"):
        with tempfile.TemporaryDirectory() as td:
            fleet_dir = Path(td) / "fleet"
            _fixture_fleet(fleet_dir)
            runner = SalvageRunner(" M src/app.py\n", [">fcs....... src/app.py\n"] * 10)
            manifest = ferry.salvage_worktree(
                fleet_dir,
                node_id="localhost",
                worktree_path="/remote/worktree",
                out_dir=_staging(fleet_dir, "salvage"),
                runner=runner,
                sleep_s=0,
            )
            assert_true("not converged", manifest["converged"] is False)
            assert_true("ten iterations", len(manifest["iterations"]) == 10)
            assert_true("liveness", "worker may be alive" in manifest["liveness_signal"])


def test_salvage_append_only_exclusion_converges() -> None:
    with live_ssh_env("1"):
        with tempfile.TemporaryDirectory() as td:
            fleet_dir = Path(td) / "fleet"
            _fixture_fleet(fleet_dir)
            runner = SalvageRunner(
                [
                    " M dispatcher.log\n",
                    " M dispatcher.log\n",
                    " M dispatcher.log\n?? tails/active-tail\n",
                    " M dispatcher.log\n",
                ],
                [">fcs....... dispatcher.log\n"] * 10,
                remote_sizes={"dispatcher.log": [1, 100, 10000], "tails/active-tail": 17},
            )
            manifest = ferry.salvage_worktree(
                fleet_dir,
                node_id="localhost",
                worktree_path="/remote/worktree",
                out_dir=_staging(fleet_dir, "salvage"),
                runner=runner,
                append_only_paths=("dispatcher.log",),
                sleep_s=0,
            )
            assert_true("converged", manifest["converged"] is True)
            assert_true("two zero passes", len(manifest["iterations"]) == 2)
            assert_true("append-only log ferried", all("dispatcher.log" in files for files in runner.rsync_files))
            assert_true("append-only logs are not exclusions", manifest["excluded_files"] == [])
            assert_true("new tail ferried", any("tails/active-tail" in files for files in runner.rsync_files))


def test_salvage_default_append_only_exclusion_converges() -> None:
    with live_ssh_env("1"):
        with tempfile.TemporaryDirectory() as td:
            fleet_dir = Path(td) / "fleet"
            _fixture_fleet(fleet_dir)
            runner = SalvageRunner(
                " M dispatcher.log\n M tails/stdout.log\n",
                [">fcs....... dispatcher.log\n>fcs....... tails/stdout.log\n"] * 10,
            )
            manifest = ferry.salvage_worktree(
                fleet_dir,
                node_id="localhost",
                worktree_path="/remote/worktree",
                out_dir=_staging(fleet_dir, "salvage"),
                runner=runner,
                sleep_s=0,
            )
            assert_true("converged via defaults", manifest["converged"] is True)
            assert_true("two default zero passes", len(manifest["iterations"]) == 2)
            assert_true("default logs transferred", all(
                {"dispatcher.log", "tails/stdout.log"} <= set(files) for files in runner.rsync_files
            ))
            assert_true("default append-only patterns only affect convergence", manifest["excluded_files"] == [])


def test_salvage_excludes_regenerable_scratch_but_keeps_scratch_named_source() -> None:
    with live_ssh_env("1"):
        with tempfile.TemporaryDirectory() as td:
            fleet_dir = Path(td) / "fleet"
            _fixture_fleet(fleet_dir)
            scratch_paths = [
                ".pytest_cache/x",
                "pkg/__pycache__/m.cpython-314.pyc",
                "pytest-of-u/pytest-0/t",
                "tmp/custom-test-run/test_output.bin",
                "pkg/module.pyc",
                "vendor/node_modules/library/index.js",
                "dispatcher.log",
                "tests/fixtures/pytest-of-alice/pytest-0/input.txt",
            ]
            porcelain = "".join(f"?? {path}\n" for path in scratch_paths)
            porcelain += " M pkg/__pycache__/helper.py\n M src/pytest_fixture.py\n"
            runner = SalvageRunner(porcelain, ["", ""])
            manifest = ferry.salvage_worktree(
                fleet_dir,
                node_id="localhost",
                worktree_path="/remote/worktree",
                out_dir=_staging(fleet_dir, "scratch-salvage"),
                runner=runner,
                pytest_basetemp_paths=("tmp/custom-test-run",),
                sleep_s=0,
            )

            first_transfer = runner.rsync_files[0]
            assert_true("pytest cache excluded", ".pytest_cache/x" not in first_transfer)
            assert_true("pytest basetemp excluded", "pytest-of-u/pytest-0/t" not in first_transfer)
            assert_true("explicit --basetemp target excluded", "tmp/custom-test-run/test_output.bin" not in first_transfer)
            assert_true("bytecode cache excluded", "pkg/__pycache__/m.cpython-314.pyc" not in first_transfer)
            assert_true("standalone bytecode transferred", "pkg/module.pyc" in first_transfer)
            assert_true("node_modules content transferred", "vendor/node_modules/library/index.js" in first_transfer)
            assert_true("append-only log transferred", "dispatcher.log" in first_transfer)
            assert_true("nested pytest-looking fixture transferred", "tests/fixtures/pytest-of-alice/pytest-0/input.txt" in first_transfer)
            assert_true("tracked Python file in pycache named directory transferred", "pkg/__pycache__/helper.py" in first_transfer)
            assert_true("scratch-like product source retained", "src/pytest_fixture.py" in first_transfer)
            excluded_by_path = {entry["path"]: entry for entry in manifest["excluded_files"]}
            reported = set(excluded_by_path)
            excluded_paths = {
                ".pytest_cache/x",
                "pkg/__pycache__/m.cpython-314.pyc",
                "pytest-of-u/pytest-0/t",
                "tmp/custom-test-run/test_output.bin",
            }
            assert_true("only recognized scratch paths reported", reported == excluded_paths)
            for path in excluded_paths:
                entry = excluded_by_path[path]
                assert_true(
                    f"exclusion report has path, size, and rule for {path}",
                    entry.get("path") == path and isinstance(entry.get("size"), int) and bool(entry.get("rule")),
                )


def test_salvage_transfers_every_dirty_product_path_with_scratch_names() -> None:
    product_paths = [
        "src/logs/handler.py",
        "pkg/logs/sub/handler.py",
        "a/b/logs/c/d/e.py",
        "src/tails/notes.md",
        "data/events.log",
        "tests/fixtures/server.log",
        "pkg/__pycache__/helper.py",
        "vendor/node_modules/leftpad/index.js",
        "tests/fixtures/pytest-of-alice/pytest-0/input.txt",
    ]
    porcelain = "".join(f" M {path}\n" for path in product_paths)
    with live_ssh_env("1"):
        with tempfile.TemporaryDirectory() as td:
            fleet_dir = Path(td) / "fleet"
            _fixture_fleet(fleet_dir)
            runner = SalvageRunner(porcelain, ["", ""])
            manifest = ferry.salvage_worktree(
                fleet_dir,
                node_id="localhost",
                worktree_path="/remote/worktree",
                out_dir=_staging(fleet_dir, "tracked-product"),
                runner=runner,
                sleep_s=0,
            )

            transferred = set(runner.rsync_files[0]) if runner.rsync_files else set()
            assert_true("every dirty product path transferred", set(product_paths) <= transferred)
            assert_true("tracked product paths are not excluded", not manifest["excluded_files"])


def test_salvage_include_rescues_excluded_path() -> None:
    with live_ssh_env("1"):
        with tempfile.TemporaryDirectory() as td:
            fleet_dir = Path(td) / "fleet"
            _fixture_fleet(fleet_dir)
            first_runner = SalvageRunner("?? .pytest_cache/x\n", ["", ""])
            first_manifest = ferry.salvage_worktree(
                fleet_dir,
                node_id="localhost",
                worktree_path="/remote/worktree",
                out_dir=_staging(fleet_dir, "first-cache-salvage"),
                runner=first_runner,
                sleep_s=0,
            )
            assert_true(
                "first run reports scratch path",
                any(entry["path"] == ".pytest_cache/x" for entry in first_manifest["excluded_files"]),
            )
            runner = SalvageRunner("?? .pytest_cache/x\n", ["", ""])
            manifest = ferry.salvage_worktree(
                fleet_dir,
                node_id="localhost",
                worktree_path="/remote/worktree",
                out_dir=_staging(fleet_dir, "include-scratch"),
                runner=runner,
                include_paths=[".pytest_cache/x"],
                sleep_s=0,
            )

            assert_true("include path transferred", ".pytest_cache/x" in runner.rsync_files[0])
            assert_true("included path not reported excluded", not manifest["excluded_files"])


def test_changing_excluded_cache_does_not_block_salvage_convergence() -> None:
    with live_ssh_env("1"):
        with tempfile.TemporaryDirectory() as td:
            fleet_dir = Path(td) / "fleet"
            _fixture_fleet(fleet_dir)
            runner = SalvageRunner(
                ["?? .pytest_cache/x\n"] * 4,
                [],
                remote_sizes={".pytest_cache/x": [1, 100, 1000, 10000]},
            )
            manifest = ferry.salvage_worktree(
                fleet_dir,
                node_id="localhost",
                worktree_path="/remote/worktree",
                out_dir=_staging(fleet_dir, "changing-cache"),
                runner=runner,
                sleep_s=0,
            )

            assert_true("changing excluded cache converges", manifest["converged"] is True)
            assert_true("two quiet rounds recorded", len(manifest["iterations"]) == 2)
            assert_true("cache never reaches rsync", runner.rsync_files == [])
            excluded = next(entry for entry in manifest["excluded_files"] if entry["path"] == ".pytest_cache/x")
            assert_true("latest excluded size remains visible", excluded["size"] == 10000)


def test_salvage_refuses_over_cap_file_and_marks_manifest_incomplete() -> None:
    with live_ssh_env("1"):
        with tempfile.TemporaryDirectory() as td:
            fleet_dir = Path(td) / "fleet"
            _fixture_fleet(fleet_dir)
            runner = SalvageRunner(
                "?? artifacts/large.bin\n",
                ["", ""],
                remote_sizes={"artifacts/large.bin": 65},
            )
            manifest = ferry.salvage_worktree(
                fleet_dir,
                node_id="localhost",
                worktree_path="/remote/worktree",
                out_dir=_staging(fleet_dir, "over-cap-salvage"),
                runner=runner,
                include_paths=["artifacts/large.bin"],
                max_file_bytes=64,
                sleep_s=0,
            )

            assert_true("over-cap file never sent", runner.rsync_files == [])
            assert_true("quiet rounds still converge", manifest["converged"] is True)
            assert_true("over-cap marks result incomplete", manifest["incomplete"] is True)
            refused = next(entry for entry in manifest["excluded_files"] if entry["path"] == "artifacts/large.bin")
            assert_true("exact capped size reported", refused["size"] == 65)
            assert_true("cap rule reported", refused["rule"] == "max-file-bytes")
            assert_true("configured cap reported", refused["limit_bytes"] == 64)


def test_salvage_directory_and_expanded_cap_leaf_converge() -> None:
    with live_ssh_env("1"):
        with tempfile.TemporaryDirectory() as td:
            fleet_dir = Path(td) / "fleet"
            _fixture_fleet(fleet_dir)
            runner = SalvageRunner(
                [
                    "?? artifacts/\n",
                    "?? artifacts/large.bin\n",
                    "?? artifacts/large.bin\n",
                    "?? artifacts/large.bin\n",
                ],
                [],
                remote_sizes={"artifacts/large.bin": 65},
                expanded_paths={"artifacts": ["artifacts/large.bin"]},
            )
            manifest = ferry.salvage_worktree(
                fleet_dir,
                node_id="localhost",
                worktree_path="/remote/worktree",
                out_dir=_staging(fleet_dir, "directory-cap-salvage"),
                runner=runner,
                max_file_bytes=64,
                sleep_s=0,
            )

            assert_true("directory expansion does not abort salvage", manifest["converged"] is True)
            assert_true("two quiet rounds after cap classification", len(manifest["iterations"]) == 2)
            assert_true("capped leaf never reaches rsync", runner.rsync_files == [])
            refusal = next(entry for entry in manifest["excluded_files"] if entry["path"] == "artifacts/large.bin")
            assert_true("expanded capped leaf reported", refusal["size"] == 65 and refusal["rule"] == "max-file-bytes")


def test_salvage_capped_path_churn_does_not_block_convergence() -> None:
    with live_ssh_env("1"):
        with tempfile.TemporaryDirectory() as td:
            fleet_dir = Path(td) / "fleet"
            _fixture_fleet(fleet_dir)
            porcelain = [
                " M src/app.py\n?? artifacts/large.bin\n",
                " M src/app.py\n?? artifacts/large.bin\n",
                " M src/app.py\n",
                " M src/app.py\n?? artifacts/large.bin\n",
            ]
            runner = SalvageRunner(
                porcelain,
                ["", ""],
                remote_sizes={"artifacts/large.bin": 65},
            )
            manifest = ferry.salvage_worktree(
                fleet_dir,
                node_id="localhost",
                worktree_path="/remote/worktree",
                out_dir=_staging(fleet_dir, "capped-churn-salvage"),
                runner=runner,
                max_file_bytes=64,
                sleep_s=0,
            )

            assert_true("changing capped path does not abort", manifest["converged"] is True)
            assert_true(
                "two quiet rounds recorded",
                len(manifest["iterations"]) == 2 and manifest["iterations"][-1]["consecutive_zero"] == 2,
            )
            assert_true("capped path never reaches rsync", all("artifacts/large.bin" not in files for files in runner.rsync_files))
            refusal = next(entry for entry in manifest["excluded_files"] if entry["path"] == "artifacts/large.bin")
            assert_true("churning capped path remains reported", refusal["size"] == 65 and refusal["rule"] == "max-file-bytes")


def test_salvage_refused_transfer_pass_is_not_quiet() -> None:
    with live_ssh_env("1"):
        with tempfile.TemporaryDirectory() as td:
            fleet_dir = Path(td) / "fleet"
            _fixture_fleet(fleet_dir)
            runner = SalvageRunner(" M src/app.py\n", ["", ""], remote_sizes={"src/app.py": 12})
            original_execute = ferry.execute_ferry

            def execute_with_disappearing_source(*args, **kwargs):
                files = list(kwargs["files"])
                if kwargs["purpose"].endswith(":initial"):
                    destination = Path(kwargs["dst_root"])
                    for rel in files:
                        path = destination / rel
                        path.parent.mkdir(parents=True, exist_ok=True)
                        path.write_bytes(b"product bytes")
                    return ferry.FerryReceipt(
                        {"ok": True, "files": files, "excluded_files": [], "incomplete": False, "stdout": ""}
                    )
                return ferry.FerryReceipt(
                    {
                        "ok": True,
                        "files": [],
                        "excluded_files": [
                            {"path": "src/app.py", "size": 12, "rule": "source-disappeared-after-rsync"}
                        ],
                        "incomplete": True,
                        "stdout": "",
                    }
                )

            ferry.execute_ferry = execute_with_disappearing_source
            try:
                manifest = ferry.salvage_worktree(
                    fleet_dir,
                    node_id="localhost",
                    worktree_path="/remote/worktree",
                    out_dir=_staging(fleet_dir, "missing-transfer-pass"),
                    runner=runner,
                    max_iterations=2,
                    sleep_s=0,
                )
            finally:
                ferry.execute_ferry = original_execute

            assert_true("incomplete transfer pass cannot converge", manifest["converged"] is False)
            assert_true("refused transfer passes are not quiet", all(not row["zero_delta"] for row in manifest["iterations"]))
            assert_true(
                "missing leaf is reported",
                any(row["rule"] == "source-disappeared-after-rsync" for row in manifest["excluded_files"]),
            )


def test_push_refuses_over_cap_file_in_ferry_receipt() -> None:
    with live_ssh_env("1"):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            fleet_dir = base / "fleet"
            src = base / "src"
            src.mkdir()
            (src / "large.bin").write_bytes(b"0123456789")
            _fixture_fleet(fleet_dir)
            called = False

            def runner(_argv: list[str]) -> tuple[int, str, str]:
                nonlocal called
                called = True
                return 0, "", ""

            receipt = ferry.execute_ferry(
                fleet_dir,
                node_id="localhost",
                direction="push",
                src_root=str(src),
                dst_root="/remote/worktree",
                files=["large.bin"],
                purpose="size-cap-test",
                runner=runner,
                max_file_bytes=9,
            ).to_dict()

            assert_true("over-cap push never reaches transport", called is False)
            assert_true("receipt schema bumped", receipt["schema"] == "goalflight.fleet.ferry.receipt.v2")
            assert_true("receipt incomplete", receipt["incomplete"] is True)
            assert_true("receipt has no transferred file", receipt["files"] == [])
            refused = receipt["excluded_files"][0]
            assert_true("receipt reports path and size", refused["path"] == "large.bin" and refused["size"] == 10)


def test_synthetic_basetemp_exclusion_byte_reduction() -> None:
    with live_ssh_env("1"):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            fleet_dir = base / "fleet"
            src = base / "source"
            product = src / "src" / "app.py"
            basetemp = src / "pytest-of-worker" / "pytest-4" / "test_big0" / "output.bin"
            product.parent.mkdir(parents=True)
            basetemp.parent.mkdir(parents=True)
            product_payload = b"product diff\n"
            product.write_bytes(product_payload)
            with basetemp.open("wb") as handle:
                handle.truncate(10_000_000_000)
            _fixture_fleet(fleet_dir)
            captured: list[list[str]] = []

            def runner(argv: list[str]) -> tuple[int, str, str]:
                if argv[0] == "ssh" and "remote-preflight" in " ".join(argv):
                    return _remote_preflight_reply(argv)
                if argv[0] == "rsync":
                    captured.append(_files_from(argv))
                    return 0, "", ""
                return 1, "", f"unexpected argv: {' '.join(argv)}"

            receipt = ferry.execute_ferry(
                fleet_dir,
                node_id="localhost",
                direction="push",
                src_root=str(src),
                dst_root="/remote/worktree",
                files=["src", "pytest-of-worker"],
                purpose="synthetic-byte-reduction",
                runner=runner,
            ).to_dict()
            before_bytes = product.stat().st_size + basetemp.stat().st_size
            selected_bytes = sum((src / rel).stat().st_size for rel in receipt["files"])
            removed_bytes = before_bytes - selected_bytes
            percent = 100 * removed_bytes / before_bytes

            assert_true("only small product change selected", receipt["files"] == ["src/app.py"])
            assert_true("rsync list matches receipt", captured == [["src/app.py"]])
            assert_true("synthetic byte reduction is basetemp size", removed_bytes == 10_000_000_000)
            assert_true("product bytes retained", selected_bytes == len(product_payload))
            print(
                "SYNTHETIC byte reduction: "
                f"before={before_bytes} selected={selected_bytes} removed={removed_bytes} ({percent:.9f}%)"
            )


def test_real_git_salvage_selects_product_and_reports_sparse_pytest_tree() -> None:
    product_rel = "src/product.py"
    scratch_rel = "pytest-of-x/pytest-0/large.bin"
    scratch_bytes = 10_000_000_000
    with live_ssh_env("1"):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            repo = base / "worker"
            repo.mkdir()

            def git(*args: str) -> subprocess.CompletedProcess[str]:
                return subprocess.run(
                    ["git", "-C", str(repo), "-c", "core.excludesFile=/dev/null", *args],
                    check=True,
                    capture_output=True,
                    text=True,
                )

            git("init", "--template=/dev/null")
            git("config", "user.name", "Ferry Test")
            git("config", "user.email", "ferry-test@example.invalid")
            product = repo / product_rel
            product.parent.mkdir(parents=True)
            product.write_bytes(b"tracked product change\n")
            git("add", product_rel)
            git(
                "-c",
                "core.hooksPath=/dev/null",
                "-c",
                "commit.gpgsign=false",
                "commit",
                "-m",
                "baseline",
            )
            product.write_bytes(b"worker product edit\n")
            scratch = repo / scratch_rel
            scratch.parent.mkdir(parents=True)
            with scratch.open("wb") as handle:
                handle.truncate(scratch_bytes)

            fleet_dir = base / "fleet"
            _fixture_fleet(fleet_dir)
            fleet_doc = fleet.read_json(fleet_dir / "fleet.json")
            fleet_doc["nodes"]["localhost"]["repo_root"] = str(repo)
            fleet_doc["nodes"]["localhost"]["state_dir"] = str(repo)
            fleet._atomic_write_json(fleet_dir / "fleet.json", fleet_doc)

            dispatch_id = "real-filesystem-salvage-measurement"
            lock = fleet.acquire_account_lock(
                fleet_dir,
                account_key="openai/default",
                owner_dispatch_id=dispatch_id,
            )
            dispatch_dir = fleet_dir / "register" / "dispatches" / dispatch_id
            dispatch_dir.mkdir(parents=True, exist_ok=True)
            fleet._atomic_write_json(
                dispatch_dir / "meta.json",
                {
                    "dispatch_id": dispatch_id,
                    "node_id": "localhost",
                    "lease_active": True,
                    "row_state": "salvage_needed",
                },
            )

            class FilesystemRunner:
                def __init__(self) -> None:
                    self.rsync_files: list[list[str]] = []
                    self.status_outputs: list[str] = []

                def __call__(self, argv: list[str]) -> tuple[int, str, str]:
                    joined = " ".join(argv)
                    if argv[0] == "ssh" and "remote-preflight" in joined:
                        match = re.search(r"--payload-b64\s+'?([A-Za-z0-9+/=]+)'?", joined)
                        assert_true("preflight payload present", match is not None)
                        payload = json.loads(base64.b64decode(match.group(1)).decode("utf-8"))
                        try:
                            reply = ferry._remote_preflight_payload(payload)
                        except Exception as exc:
                            return 2, json.dumps(ferry._remote_preflight_error(exc)), ""
                        return 0, json.dumps(reply), ""
                    if argv[0] == "ssh" and " status " in f" {joined} ":
                        result = git("status", "--porcelain", "--untracked-files=all")
                        self.status_outputs.append(result.stdout)
                        return 0, result.stdout, ""
                    if argv[0] == "rsync":
                        files = _files_from(argv)
                        self.rsync_files.append(files)
                        if "--itemize-changes" not in argv:
                            destination = Path(argv[-1])
                            for rel in files:
                                target = destination / rel
                                target.parent.mkdir(parents=True, exist_ok=True)
                                target.write_bytes((repo / rel).read_bytes())
                        return 0, "", ""
                    return 1, "", f"unexpected argv: {joined}"

            runner = FilesystemRunner()
            manifest = ferry.salvage_worktree(
                fleet_dir,
                node_id="localhost",
                worktree_path=str(repo),
                out_dir=_staging(fleet_dir, "real-filesystem-salvage"),
                dispatch_id=dispatch_id,
                runner=runner,
                sleep_s=0,
            )

            assert_true("git status saw tracked product edit", f" M {product_rel}" in runner.status_outputs[0])
            assert_true("git status saw untracked pytest leaf", f"?? {scratch_rel}" in runner.status_outputs[0])
            assert_true(
                "only product path selected",
                bool(runner.rsync_files) and all(files == [product_rel] for files in runner.rsync_files),
            )
            assert_true(
                "manifest lists only product file",
                [entry["path"] for entry in manifest["files"]] == [product_rel],
            )
            selected_paths = {rel for files in runner.rsync_files for rel in files}
            selected_bytes = sum((repo / rel).stat().st_size for rel in selected_paths)
            excluded = {entry["path"]: entry for entry in manifest["excluded_files"]}
            assert_true("pytest leaf excluded and reported", scratch_rel in excluded)
            assert_true("reported sparse leaf size", excluded[scratch_rel]["size"] == scratch_bytes)
            assert_true("salvage result is incomplete", manifest["incomplete"] is True)
            assert_true("incomplete salvage has no release command", "lock_release_command" not in manifest)
            assert_true("salvage records lock token", manifest.get("fencing_token") == lock.get("fencing_token"))
            active_lock = fleet.load_account_lock(fleet.account_lock_path(fleet_dir, "openai/default"))
            assert_true("incomplete salvage leaves lock active", active_lock is not None and active_lock.get("state") == "active")
            omitted_bytes = sum(entry["size"] for entry in excluded.values())
            print(
                "REAL filesystem salvage byte selection: "
                f"selected={selected_bytes} omitted={omitted_bytes}"
            )


def test_salvage_skips_denied_dirty_file_visible_note() -> None:
    with live_ssh_env("1"):
        with tempfile.TemporaryDirectory() as td:
            fleet_dir = Path(td) / "fleet"
            _fixture_fleet(fleet_dir)
            runner = SalvageRunner(" M safe.txt\n?? auth.json\n", ["", ""])
            manifest = ferry.salvage_worktree(
                fleet_dir,
                node_id="localhost",
                worktree_path="/remote/worktree",
                out_dir=_staging(fleet_dir, "salvage"),
                runner=runner,
                sleep_s=0,
            )
            assert_true("safe target only", manifest["target_files"] == ["safe.txt"])
            assert_true("auth skipped", any(item["path"] == "auth.json" for item in manifest["skipped"]))
            assert_true("not ferried", all("auth.json" not in files for files in runner.rsync_files))


def test_salvage_relists_before_rsync_and_aborts_on_changed_set() -> None:
    with live_ssh_env("1"):
        with tempfile.TemporaryDirectory() as td:
            fleet_dir = Path(td) / "fleet"
            _fixture_fleet(fleet_dir)
            runner = SalvageRunner([" M safe.txt\n", " M safe.txt\n?? auth.json\n"], ["", ""])
            try:
                ferry.salvage_worktree(
                    fleet_dir,
                    node_id="localhost",
                    worktree_path="/remote/worktree",
                    out_dir=_staging(fleet_dir, "salvage"),
                    runner=runner,
                    sleep_s=0,
                )
                assert_true("changed set should abort", False)
            except ferry.FerryError as exc:
                assert_true("changed set named", "dirty file set changed" in str(exc))
            assert_true("no stale rsync", runner.rsync_files == [])


def test_cli_salvage_default_append_only_patterns_merge() -> None:
    with tempfile.TemporaryDirectory() as td:
        fleet_dir = Path(td) / "fleet"
        _fixture_fleet(fleet_dir)
        captured: dict[str, tuple[str, ...]] = {}
        original = ferry.salvage_worktree

        def fake_salvage(*_args, append_only_paths=None, **_kwargs):
            merged = ferry._merge_append_only_patterns(append_only_paths)
            captured["append_only"] = merged
            captured["max_file_bytes"] = _kwargs.get("max_file_bytes")
            captured["pytest_basetemp_paths"] = _kwargs.get("pytest_basetemp_paths")
            return {
                "schema": "goalflight.fleet.salvage.manifest.v2",
                "target_files": [],
                "skipped": [],
                "excluded_files": [],
                "files": [],
                "iterations": [],
                "converged": True,
                "incomplete": False,
            }

        ferry.salvage_worktree = fake_salvage
        try:
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                rc = fleet.main(
                    [
                        "--fleet-dir",
                        str(fleet_dir),
                        "salvage",
                        "--node",
                        "localhost",
                        "--worktree-path",
                        "/remote/worktree",
                        "--out-dir",
                        str(_staging(fleet_dir, "cli-default")),
                        "--max-file-bytes",
                        "1234",
                        "--pytest-basetemp",
                        "tmp/worker-test-output",
                        "--exec",
                    ]
                )
        finally:
            ferry.salvage_worktree = original
        assert_true("cli rc", rc == 0)
        assert_true("default append-only present", "dispatcher.log" in captured["append_only"])
        assert_true("CLI cap override passed", captured["max_file_bytes"] == 1234)
        assert_true("explicit basetemp passed", captured["pytest_basetemp_paths"] == ["tmp/worker-test-output"])
        assert_true("json printed", "converged" in stdout.getvalue())


def test_cli_ferry_returns_incomplete_status() -> None:
    with tempfile.TemporaryDirectory() as td:
        fleet_dir = Path(td) / "fleet"
        _fixture_fleet(fleet_dir)
        original = ferry.execute_ferry
        captured: dict[str, object] = {}

        def fake_ferry(*_args, max_file_bytes=None, **_kwargs):
            captured["max_file_bytes"] = max_file_bytes
            return ferry.FerryReceipt({"schema": "goalflight.fleet.ferry.receipt.v2", "incomplete": True})

        ferry.execute_ferry = fake_ferry
        try:
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                rc = fleet.main(
                    [
                        "--fleet-dir",
                        str(fleet_dir),
                        "ferry",
                        "--node",
                        "localhost",
                        "--direction",
                        "push",
                        "--src-root",
                        str(Path(td) / "src"),
                        "--dst-root",
                        "/remote/worktree",
                        "--path",
                        "large.bin",
                        "--purpose",
                        "incomplete-test",
                    ]
                )
        finally:
            ferry.execute_ferry = original
        assert_true("ferry CLI rejects incomplete result", rc == 3)
        assert_true("CLI uses default cap", captured["max_file_bytes"] == ferry.DEFAULT_MAX_FILE_BYTES)
        assert_true("incomplete JSON printed", '"incomplete": true' in stdout.getvalue())


def test_cli_salvage_returns_incomplete_status() -> None:
    with tempfile.TemporaryDirectory() as td:
        fleet_dir = Path(td) / "fleet"
        _fixture_fleet(fleet_dir)
        original = ferry.salvage_worktree
        ferry.salvage_worktree = lambda *_args, **_kwargs: {
            "schema": "goalflight.fleet.salvage.manifest.v2",
            "incomplete": True,
            "converged": True,
            "excluded_files": [{"path": "large.bin", "size": 1000, "rule": "max-file-bytes"}],
        }
        try:
            with redirect_stdout(io.StringIO()):
                rc = fleet.main(
                    [
                        "--fleet-dir",
                        str(fleet_dir),
                        "salvage",
                        "--node",
                        "localhost",
                        "--worktree-path",
                        "/remote/worktree",
                        "--out-dir",
                        str(_staging(fleet_dir, "incomplete-cli")),
                        "--exec",
                    ]
                )
        finally:
            ferry.salvage_worktree = original
        assert_true("salvage CLI rejects incomplete result", rc == 3)


def test_cli_salvage_include_is_repeatable() -> None:
    with tempfile.TemporaryDirectory() as td:
        fleet_dir = Path(td) / "fleet"
        _fixture_fleet(fleet_dir)
        original = ferry.salvage_worktree
        captured: dict[str, object] = {}

        def fake_salvage(*_args, include_paths=None, **_kwargs):
            captured["include_paths"] = include_paths
            return {"schema": "goalflight.fleet.salvage.manifest.v2", "incomplete": False}

        ferry.salvage_worktree = fake_salvage
        try:
            with redirect_stdout(io.StringIO()):
                rc = fleet.main(
                    [
                        "--fleet-dir",
                        str(fleet_dir),
                        "salvage",
                        "--node",
                        "localhost",
                        "--worktree-path",
                        "/remote/worktree",
                        "--out-dir",
                        str(_staging(fleet_dir, "include-cli")),
                        "--include",
                        ".pytest_cache/x",
                        "--include",
                        "pkg/__pycache__/helper.py",
                        "--exec",
                    ]
                )
        except SystemExit as exc:
            rc = int(exc.code)
        finally:
            ferry.salvage_worktree = original
        assert_true("salvage include CLI accepted", rc == 0)
        assert_true(
            "salvage include values passed in order",
            captured.get("include_paths") == [".pytest_cache/x", "pkg/__pycache__/helper.py"],
        )


def test_ferry_preflight_allowlist_shape_is_narrow() -> None:
    argv = fleet_ssh.build_remote_command(
        "ferry_preflight",
        repo_root="/srv/goal-flight",
        root="/remote/worktree",
        files=["safe.txt"],
        allowed_roots=["/remote"],
    )
    assert_true("helper from stdin", argv[:2] == ["python", "-"])
    assert_true("remote preflight subcommand", "remote-preflight" in argv)
    assert_true("payload flag", "--payload-b64" in argv)


def test_shipped_ferry_preflight_bytes_match_imported_module() -> None:
    module_path = ROOT / "scripts" / "goalflight_fleet_ferry_preflight.py"
    source = ferry._remote_preflight_source()
    module_bytes = module_path.read_bytes()
    assert_true("laptop imports the shipped module", Path(ferry_preflight.__file__).resolve() == module_path.resolve())
    assert_true(
        "shipped source matches module bytes and size",
        source == module_bytes and len(source) == module_path.stat().st_size,
    )


def test_remote_preflight_passes_shipped_source_to_runner() -> None:
    with tempfile.TemporaryDirectory() as td:
        fleet_dir = Path(td) / "fleet"
        _fixture_fleet(fleet_dir)
        node_entry = fleet.read_json(fleet_dir / "fleet.json")["nodes"]["localhost"]
        captured: dict[str, object] = {}
        original_subprocess_run = ferry.subprocess.run

        def fake_runner(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
            captured["argv"] = argv
            captured["stdin_bytes"] = kwargs.get("input")
            code, stdout, stderr = _remote_preflight_reply(argv)
            return subprocess.CompletedProcess(argv, code, stdout.encode(), stderr.encode())

        ferry.subprocess.run = fake_runner  # type: ignore[assignment]
        try:
            checked = ferry._run_remote_preflight(
                fleet_dir,
                node_id="localhost",
                node_entry=node_entry,
                remote_root="/remote/worktree",
                files=["safe.txt"],
                runner=None,
            )
        finally:
            ferry.subprocess.run = original_subprocess_run

        assert_true("production preflight runner returned checked file", "safe.txt" in checked)
        assert_true("production runner received ssh argv", isinstance(captured.get("argv"), list))
        assert_true(
            "production runner receives exact shipped source on stdin",
            captured.get("stdin_bytes") == (ROOT / "scripts" / "goalflight_fleet_ferry_preflight.py").read_bytes(),
        )


def test_shipped_ferry_preflight_runs_from_stdin() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "remote"
        root.mkdir()
        (root / "safe.txt").write_text("ferry preflight\n")
        remote_argv = fleet_ssh.build_remote_command(
            "ferry_preflight",
            repo_root=str(ROOT),
            root=str(root),
            files=["safe.txt"],
            allowed_roots=[str(root)],
            python="python3",
        )
        source = ferry._remote_preflight_source()
        code, stdout, stderr = ferry._run(
            [sys.executable, *remote_argv[1:]],
            None,
            stdin_bytes=source,
        )
        assert_true("stdin helper argv", remote_argv[1] == "-")
        assert_true("node-style helper exits cleanly", code == 0)
        assert_true("node-style helper has no stderr", not stderr)
        reply = json.loads(stdout)
        assert_true("node-style preflight accepted", reply.get("ok") is True)
        checked = next(item for item in reply["checked"] if item["path"] == "safe.txt")
        assert_true("node-style path exists", checked.get("exists") is True)
        size = checked.get("size")
        ctime_ns = checked.get("ctime_ns")
        assert_true("node-style metadata size", isinstance(size, int) and not isinstance(size, bool) and size >= 0)
        assert_true(
            "node-style metadata ctime",
            isinstance(ctime_ns, int) and not isinstance(ctime_ns, bool),
        )
        assert_true("node-style metadata file kind", checked.get("is_regular_file") is True)


def test_salvage_manifest_records_lock_identity() -> None:
    dispatch_id = "acp-salvage-lock-identity"
    with live_ssh_env("1"):
        with tempfile.TemporaryDirectory() as td:
            fleet_dir = Path(td) / "fleet"
            _fixture_fleet(fleet_dir)
            lock = fleet.acquire_account_lock(
                fleet_dir,
                account_key="openai/default",
                owner_dispatch_id=dispatch_id,
            )
            dispatch_dir = fleet_dir / "register" / "dispatches" / dispatch_id
            dispatch_dir.mkdir(parents=True, exist_ok=True)
            fleet._atomic_write_json(
                dispatch_dir / "meta.json",
                {
                    "dispatch_id": dispatch_id,
                    "node_id": "localhost",
                    "lease_active": True,
                    "row_state": "salvage_needed",
                },
            )
            runner = SalvageRunner(" M src/app.py\n", [">fcs....... src/app.py\n", "", ""])
            manifest = ferry.salvage_worktree(
                fleet_dir,
                node_id="localhost",
                worktree_path="/remote/worktree",
                out_dir=_staging(fleet_dir, "salvage"),
                dispatch_id=dispatch_id,
                runner=runner,
                sleep_s=0,
            )
            assert_true("dispatch id", manifest.get("dispatch_id") == dispatch_id)
            assert_true("account key", manifest.get("account_key") == "openai/default")
            assert_true("fencing token", manifest.get("fencing_token") == lock.get("fencing_token"))
            assert_true("release command", "lock-release" in str(manifest.get("lock_release_command")))
            assert_true("release command is bound to manifest", "--manifest" in str(manifest.get("lock_release_command")))
            rc = fleet.main(
                [
                    "--fleet-dir",
                    str(fleet_dir),
                    "lock-release",
                    "--account-key",
                    "openai/default",
                    "--fencing-token",
                    str(lock.get("fencing_token")),
                    "--reason",
                    "salvage_complete",
                    "--manifest",
                    str(manifest["manifest_path"]),
                ]
            )
            assert_true("complete manifest permits exact lock release", rc == 0)


def test_incomplete_salvage_manifest_omits_lock_release_command() -> None:
    dispatch_id = "incomplete-salvage-no-release-command"
    with live_ssh_env("1"):
        with tempfile.TemporaryDirectory() as td:
            fleet_dir = Path(td) / "fleet"
            _fixture_fleet(fleet_dir)
            fleet.acquire_account_lock(
                fleet_dir,
                account_key="openai/default",
                owner_dispatch_id=dispatch_id,
            )
            dispatch_dir = fleet_dir / "register" / "dispatches" / dispatch_id
            dispatch_dir.mkdir(parents=True, exist_ok=True)
            fleet._atomic_write_json(
                dispatch_dir / "meta.json",
                {
                    "dispatch_id": dispatch_id,
                    "node_id": "localhost",
                    "lease_active": True,
                    "row_state": "salvage_needed",
                },
            )
            runner = SalvageRunner("?? large.bin\n", ["", ""], remote_sizes={"large.bin": 9})
            manifest = ferry.salvage_worktree(
                fleet_dir,
                node_id="localhost",
                worktree_path="/remote/worktree",
                out_dir=_staging(fleet_dir, "incomplete-no-release"),
                dispatch_id=dispatch_id,
                runner=runner,
                max_file_bytes=8,
                sleep_s=0,
            )
            assert_true("salvage result incomplete", manifest["incomplete"] is True)
            assert_true("incomplete manifest has no direct release command", "lock_release_command" not in manifest)


def test_salvage_complete_releases_exact_lock() -> None:
    dispatch_id = "acp-salvage-complete"
    with tempfile.TemporaryDirectory() as td:
        fleet_dir = Path(td) / "fleet"
        _fixture_fleet(fleet_dir)
        lock = fleet.acquire_account_lock(
            fleet_dir,
            account_key="openai/default",
            owner_dispatch_id=dispatch_id,
        )
        other = fleet.acquire_account_lock(
            fleet_dir,
            account_key="openai/other",
            owner_dispatch_id="other-dispatch",
        )
        manifest_path = _staging(fleet_dir, "salvage", "salvage-manifest.json")
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text(
            json.dumps(
                {
                    "schema": "goalflight.fleet.salvage.manifest.v2",
                    "incomplete": False,
                    "dispatch_id": dispatch_id,
                    "account_key": "openai/default",
                    "fencing_token": lock.get("fencing_token"),
                }
            )
            + "\n"
        )
        rc = fleet.main(
            [
                "--fleet-dir",
                str(fleet_dir),
                "salvage-complete",
                "--manifest",
                str(manifest_path),
            ]
        )
        assert_true("cli rc", rc == 0)
        released = fleet.load_account_lock(fleet.account_lock_path(fleet_dir, "openai/default"))
        assert_true("target released", released is None or released.get("state") == "released")
        held = fleet.load_account_lock(fleet.account_lock_path(fleet_dir, "openai/other"))
        assert_true("other still active", held is not None and held.get("state") == "active")
        assert_true("other fencing", held.get("fencing_token") == other.get("fencing_token"))


def test_salvage_complete_refuses_incomplete_manifest() -> None:
    with tempfile.TemporaryDirectory() as td:
        fleet_dir = Path(td) / "fleet"
        _fixture_fleet(fleet_dir)
        lock = fleet.acquire_account_lock(
            fleet_dir,
            account_key="openai/default",
            owner_dispatch_id="incomplete-salvage",
        )
        manifest_path = _staging(fleet_dir, "incomplete-salvage", "salvage-manifest.json")
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text(
            json.dumps(
                {
                    "schema": "goalflight.fleet.salvage.manifest.v2",
                    "incomplete": True,
                    "excluded_files": [{"path": "large.bin", "size": 1000, "rule": "max-file-bytes"}],
                    "account_key": "openai/default",
                    "fencing_token": lock.get("fencing_token"),
                }
            )
            + "\n"
        )
        error = io.StringIO()
        with redirect_stderr(error):
            rc = fleet.main(
                [
                    "--fleet-dir",
                    str(fleet_dir),
                    "salvage-complete",
                    "--manifest",
                    str(manifest_path),
                ]
            )
        held = fleet.load_account_lock(fleet.account_lock_path(fleet_dir, "openai/default"))
        assert_true("incomplete manifest rejected", rc == 1)
        assert_true("reason printed", "manifest is incomplete" in error.getvalue())
        assert_true("account lock remains active", held is not None and held.get("state") == "active")


def test_lock_release_refuses_incomplete_salvage_manifest() -> None:
    dispatch_id = "incomplete-direct-lock-release"
    with tempfile.TemporaryDirectory() as td:
        fleet_dir = Path(td) / "fleet"
        _fixture_fleet(fleet_dir)
        lock = fleet.acquire_account_lock(
            fleet_dir,
            account_key="openai/default",
            owner_dispatch_id=dispatch_id,
        )
        manifest_path = _staging(fleet_dir, "incomplete-direct", "salvage-manifest.json")
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text(
            json.dumps(
                {
                    "schema": "goalflight.fleet.salvage.manifest.v2",
                    "incomplete": True,
                    "account_key": "openai/default",
                    "fencing_token": lock.get("fencing_token"),
                }
            )
            + "\n"
        )
        error = io.StringIO()
        try:
            with redirect_stderr(error):
                rc = fleet.main(
                    [
                        "--fleet-dir",
                        str(fleet_dir),
                        "lock-release",
                        "--account-key",
                        "openai/default",
                        "--fencing-token",
                        str(lock.get("fencing_token")),
                        "--reason",
                        "salvage_complete",
                        "--manifest",
                        str(manifest_path),
                    ]
                )
        except SystemExit as exc:
            rc = int(exc.code)
        held = fleet.load_account_lock(fleet.account_lock_path(fleet_dir, "openai/default"))
        assert_true("incomplete direct lock release rejected", rc == 1)
        assert_true("direct release explains refusal", "manifest is incomplete" in error.getvalue())
        assert_true("direct release leaves account lock active", held is not None and held.get("state") == "active")


def test_salvage_complete_refuses_manifest_without_complete_state() -> None:
    with tempfile.TemporaryDirectory() as td:
        fleet_dir = Path(td) / "fleet"
        _fixture_fleet(fleet_dir)
        lock = fleet.acquire_account_lock(
            fleet_dir,
            account_key="openai/default",
            owner_dispatch_id="missing-complete-state",
        )
        manifest_path = _staging(fleet_dir, "missing-complete-state", "salvage-manifest.json")
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text(
            json.dumps(
                {
                    "schema": "goalflight.fleet.salvage.manifest.v2",
                    "account_key": "openai/default",
                    "fencing_token": lock.get("fencing_token"),
                }
            )
            + "\n"
        )
        error = io.StringIO()
        with redirect_stderr(error):
            rc = fleet.main(
                [
                    "--fleet-dir",
                    str(fleet_dir),
                    "salvage-complete",
                    "--manifest",
                    str(manifest_path),
                ]
            )
        held = fleet.load_account_lock(fleet.account_lock_path(fleet_dir, "openai/default"))
        assert_true("missing completion state rejected", rc == 1)
        assert_true("completion state refusal explained", "incomplete" in error.getvalue())
        assert_true("missing state leaves lock active", held is not None and held.get("state") == "active")


def test_lock_release_refuses_manifest_without_complete_state() -> None:
    with tempfile.TemporaryDirectory() as td:
        fleet_dir = Path(td) / "fleet"
        _fixture_fleet(fleet_dir)
        lock = fleet.acquire_account_lock(
            fleet_dir,
            account_key="openai/default",
            owner_dispatch_id="missing-direct-complete-state",
        )
        manifest_path = _staging(fleet_dir, "missing-direct-complete-state", "salvage-manifest.json")
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text(
            json.dumps(
                {
                    "schema": "goalflight.fleet.salvage.manifest.v2",
                    "account_key": "openai/default",
                    "fencing_token": lock.get("fencing_token"),
                }
            )
            + "\n"
        )
        error = io.StringIO()
        with redirect_stderr(error):
            rc = fleet.main(
                [
                    "--fleet-dir",
                    str(fleet_dir),
                    "lock-release",
                    "--account-key",
                    "openai/default",
                    "--fencing-token",
                    str(lock.get("fencing_token")),
                    "--reason",
                    "salvage_complete",
                    "--manifest",
                    str(manifest_path),
                ]
            )
        held = fleet.load_account_lock(fleet.account_lock_path(fleet_dir, "openai/default"))
        assert_true("direct release without complete state rejected", rc == 1)
        assert_true("direct completeness refusal explained", "incomplete" in error.getvalue())
        assert_true("missing state leaves direct lock active", held is not None and held.get("state") == "active")


def test_pytest_basetemp_rejects_traversal_and_absolute_paths() -> None:
    for invalid in ("tmp/../src", "../src", "/tmp/pytest-data"):
        try:
            ferry._normalize_pytest_basetemp_paths([invalid])
        except ferry.FerryError:
            continue
        raise AssertionError(f"unsafe pytest basetemp accepted: {invalid}")


def test_git_status_porcelain_allowlist_shape_is_narrow() -> None:
    argv = fleet_ssh.build_remote_command(
        "git_status_porcelain",
        repo_root="/srv/goal-flight",
        worktree_path="/srv/goal-flight/worktrees/chunk",
        allowed_roots=["/srv/goal-flight"],
    )
    assert_true(
        "fixed git status argv",
        argv == ["git", "-C", "/srv/goal-flight/worktrees/chunk", "status", "--porcelain", "--untracked-files=all"],
    )
    try:
        fleet_ssh.build_remote_command(
            "git_status_porcelain",
            repo_root="/srv/goal-flight",
            worktree_path="/tmp/outside",
            allowed_roots=["/srv/goal-flight"],
        )
        assert_true("outside worktree should raise", False)
    except fleet_ssh.SshAllowlistError as exc:
        assert_true("declared root message", "declared remote root" in str(exc))
    try:
        fleet_ssh.build_remote_command(
            "git_status_porcelain",
            repo_root="/srv/goal-flight",
            worktree_path="/srv/goal-flight/../outside",
            allowed_roots=["/srv/goal-flight"],
        )
        assert_true("traversal worktree should raise", False)
    except fleet_ssh.SshAllowlistError:
        pass


def main() -> None:
    tests = (
        test_ferry_happy_path_both_directions_and_receipt,
        test_ferry_deny_requested_and_expanded_paths,
        test_direct_ferry_transfers_explicit_scratch_named_paths,
        test_ferry_rejects_path_tricks_and_symlink_escape,
        test_credential_deny_patterns_cover_case_variants_and_common_secret_names,
        test_ferry_rejects_newline_split_and_pull_key_before_runner,
        test_ferry_live_ssh_gate_fails_closed,
        test_remote_preflight_denies_symlink_and_hardlink_credentials,
        test_push_hardlink_alias_denied_before_rsync,
        test_push_stages_scanned_bytes_before_rsync_source_swap,
        test_root_confinement_rejects_pull_dst_and_remote_worktree_before_runner,
        test_pull_remote_preflight_deny_fires_before_rsync,
        test_partial_pull_cleanup_on_rsync_failure,
        test_pull_quarantine_denies_private_key_content_before_promotion,
        test_pull_quarantine_denies_provider_token_json_before_promotion,
        test_pull_quarantine_allows_non_provider_token_json,
        test_pull_refuses_source_changed_during_rsync,
        test_pull_refuses_symlink_changed_during_rsync,
        test_salvage_porcelain_file_list_transfer_and_convergence,
        test_salvage_bounds_at_ten_with_liveness_signal,
        test_salvage_append_only_exclusion_converges,
        test_salvage_default_append_only_exclusion_converges,
        test_salvage_excludes_regenerable_scratch_but_keeps_scratch_named_source,
        test_salvage_transfers_every_dirty_product_path_with_scratch_names,
        test_salvage_include_rescues_excluded_path,
        test_changing_excluded_cache_does_not_block_salvage_convergence,
        test_incomplete_salvage_manifest_omits_lock_release_command,
        test_salvage_refuses_over_cap_file_and_marks_manifest_incomplete,
        test_salvage_directory_and_expanded_cap_leaf_converge,
        test_salvage_capped_path_churn_does_not_block_convergence,
        test_salvage_refused_transfer_pass_is_not_quiet,
        test_push_refuses_over_cap_file_in_ferry_receipt,
        test_synthetic_basetemp_exclusion_byte_reduction,
        test_real_git_salvage_selects_product_and_reports_sparse_pytest_tree,
        test_salvage_skips_denied_dirty_file_visible_note,
        test_salvage_relists_before_rsync_and_aborts_on_changed_set,
        test_cli_salvage_default_append_only_patterns_merge,
        test_cli_ferry_returns_incomplete_status,
        test_cli_salvage_returns_incomplete_status,
        test_cli_salvage_include_is_repeatable,
        test_salvage_manifest_records_lock_identity,
        test_salvage_complete_releases_exact_lock,
        test_salvage_complete_refuses_incomplete_manifest,
        test_lock_release_refuses_incomplete_salvage_manifest,
        test_salvage_complete_refuses_manifest_without_complete_state,
        test_lock_release_refuses_manifest_without_complete_state,
        test_pytest_basetemp_rejects_traversal_and_absolute_paths,
        test_ferry_preflight_allowlist_shape_is_narrow,
        test_shipped_ferry_preflight_bytes_match_imported_module,
        test_remote_preflight_passes_shipped_source_to_runner,
        test_shipped_ferry_preflight_runs_from_stdin,
        test_git_status_porcelain_allowlist_shape_is_narrow,
    )
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
    print(f"OK: {len(tests)} fleet ferry/salvage tests pass")


if __name__ == "__main__":
    main()
