#!/usr/bin/env python3
"""Tests for fleet ferry primitive and convergent salvage."""

from __future__ import annotations

from support import skip_posix_on_native_windows

skip_posix_on_native_windows("fleet ferry fixtures use POSIX paths and symlinks")

import json
import os
import sys
import tempfile
import io
from contextlib import contextmanager, redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import goalflight_fleet as fleet
import goalflight_fleet_ferry as ferry
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
            assert_true("explicit src node", pull["src"]["node"] == "localhost")
            assert_true("explicit dst node", pull["dst"]["node"] == "controller")
            assert_true("two rsyncs", len([argv for argv in captured if argv[0] == "rsync"]) == 2)
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
                    return 0, '{"ok":true}\n', ""
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
                    return 0, '{"ok":true}\n', ""
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
                    return 0, '{"ok":true}\n', ""
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
                    return 0, '{"ok":true}\n', ""
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
                    return 0, '{"ok":true}\n', ""
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


class SalvageRunner:
    def __init__(self, porcelain: str | list[str], diffs: list[str]) -> None:
        self.porcelains = [porcelain] if isinstance(porcelain, str) else list(porcelain)
        self.diffs = list(diffs)
        self.rsync_files: list[list[str]] = []
        self.status_calls = 0
        self.preflight_calls = 0

    def __call__(self, argv: list[str]) -> tuple[int, str, str]:
        joined = " ".join(argv)
        if argv[0] == "ssh" and "remote-preflight" in joined:
            self.preflight_calls += 1
            return 0, '{"ok":true}\n', ""
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
            runner = SalvageRunner(" M dispatcher.log\n", [">fcs....... dispatcher.log\n"] * 10)
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
            assert_true("append only excluded", manifest["iterations"][0]["checked_changed"] == [])


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
            assert_true("default append-only excluded", manifest["iterations"][0]["checked_changed"] == [])
            assert_true("dispatcher observed", "dispatcher.log" in manifest["iterations"][0]["changed"])
            assert_true("tail observed", "tails/stdout.log" in manifest["iterations"][0]["changed"])


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
            return {
                "schema": "goalflight.fleet.salvage.manifest.v1",
                "target_files": [],
                "skipped": [],
                "files": [],
                "iterations": [],
                "converged": True,
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
                        "--exec",
                    ]
                )
        finally:
            ferry.salvage_worktree = original
        assert_true("cli rc", rc == 0)
        assert_true("default append-only present", "dispatcher.log" in captured["append_only"])
        assert_true("json printed", "converged" in stdout.getvalue())


def test_ferry_preflight_allowlist_shape_is_narrow() -> None:
    argv = fleet_ssh.build_remote_command(
        "ferry_preflight",
        repo_root="/srv/goal-flight",
        root="/remote/worktree",
        files=["safe.txt"],
        allowed_roots=["/remote"],
    )
    assert_true("helper script", argv[1].endswith("goalflight_fleet_ferry.py"))
    assert_true("remote preflight subcommand", "remote-preflight" in argv)
    assert_true("payload flag", "--payload-b64" in argv)


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
                    "schema": "goalflight.fleet.salvage.manifest.v1",
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
        test_salvage_porcelain_file_list_transfer_and_convergence,
        test_salvage_bounds_at_ten_with_liveness_signal,
        test_salvage_append_only_exclusion_converges,
        test_salvage_default_append_only_exclusion_converges,
        test_salvage_skips_denied_dirty_file_visible_note,
        test_salvage_relists_before_rsync_and_aborts_on_changed_set,
        test_cli_salvage_default_append_only_patterns_merge,
        test_salvage_manifest_records_lock_identity,
        test_salvage_complete_releases_exact_lock,
        test_ferry_preflight_allowlist_shape_is_narrow,
        test_git_status_porcelain_allowlist_shape_is_narrow,
    )
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
    print(f"OK: {len(tests)} fleet ferry/salvage tests pass")


if __name__ == "__main__":
    main()
