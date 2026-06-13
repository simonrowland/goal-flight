#!/usr/bin/env python3
"""Fleet ferry primitive and convergent-rsync salvage wrapper."""

from __future__ import annotations

import base64
import fnmatch
import hashlib
import json
import os
import posixpath
import re
import shlex
import shutil
import stat
import subprocess
import tempfile
import time
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterable

import goalflight_fleet_ssh as fleet_ssh


DENY_CREDENTIAL_PATTERNS: tuple[str, ...] = (
    "auth.json",
    "auth.json*",
    "*auth*.json",
    "*_token*",
    "*.pem",
    "id_rsa*",
    "id_ed25519*",
    ".ssh/",
    ".codex/",
    ".claude/",
    ".cursor/",
    ".grok/",
    "*keychain*",
    ".netrc",
    ".npmrc",
    ".env",
    ".env.*",
)

PROVIDER_AUTH_NAMES = frozenset(
    {
        "auth.json",
        "credentials.json",
        "oauth.json",
        "session.json",
        "tokens.json",
    }
)
PROVIDER_AUTH_TOKEN_JSON_KEYS = frozenset(
    {
        "access_token",
        "refresh_token",
        "id_token",
        "auth_token",
        "session_token",
        "api_key",
        "apiKey",
        "accessToken",
        "refreshToken",
        "idToken",
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "GROK_API_KEY",
        "CURSOR_API_KEY",
    }
)
CONTENT_SIGNATURE_SCAN_BYTES = 1024 * 1024
PRIVATE_KEY_HEADER_RE = re.compile(
    rb"\A(?:\xef\xbb\xbf)?[ \t\r\n]*-----BEGIN (?:[A-Z0-9]+(?: [A-Z0-9]+)* )?PRIVATE KEY-----"
)
DEFAULT_APPEND_ONLY_PATTERNS: tuple[str, ...] = (
    "*.log",
    "logs/*",
    "*/logs/*",
    "tails/*",
    "*/tails/*",
    "tail.log",
    "dispatcher.log",
    "stdout.log",
    "stderr.log",
)


class FerryError(Exception):
    pass


class FerryDenyError(FerryError):
    pass


@dataclass(frozen=True)
class FerryReceipt:
    payload: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return dict(self.payload)


def _utc_iso() -> str:
    import datetime as dt

    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def assert_live_ssh_opt_in() -> None:
    if os.environ.get("GOALFLIGHT_LIVE_SSH") == "1":
        return
    raise FerryError(
        "--exec refused: set GOALFLIGHT_LIVE_SSH=1 to allow live SSH/rsync. "
        "Ferry and salvage can move account-adjacent files, so they fail closed in tests and CI."
    )


def normalize_rel_path(value: str) -> str:
    raw_value = str(value or "")
    if "\n" in raw_value or "\r" in raw_value:
        raise FerryError("file list entries must be single-line relative paths")
    raw = raw_value.strip().replace("\\", "/")
    if not raw:
        raise FerryError("file list entries must be non-empty relative paths")
    norm = posixpath.normpath(raw)
    if norm in {"", "."}:
        raise FerryError("file list entries must name files, not the transfer root")
    path = PurePosixPath(norm)
    if path.is_absolute() or any(part == ".." for part in path.parts):
        raise FerryError(f"refusing path outside declared root: {value!r}")
    return norm


def _credential_deny_reason_for_parts(parts: list[str], lower_path: str) -> str | None:
    basename = parts[-1] if parts else ""
    if basename in PROVIDER_AUTH_NAMES:
        return basename
    for pattern in DENY_CREDENTIAL_PATTERNS:
        normalized = pattern.lower().replace("\\", "/")
        if normalized.endswith("/"):
            dirname = normalized.rstrip("/")
            if any(part == dirname for part in parts):
                return pattern
            continue
        if fnmatch.fnmatch(basename, normalized) or fnmatch.fnmatch(lower_path, normalized):
            return pattern
    return None


def _credential_deny_reason_for_path_text(path: str) -> str | None:
    lower = str(path or "").strip().replace("\\", "/").strip("/").lower()
    if not lower:
        return None
    parts = [part for part in PurePosixPath(lower).parts if part not in {"", "."}]
    return _credential_deny_reason_for_parts(parts, lower)


def credential_deny_reason(path: str) -> str | None:
    rel = normalize_rel_path(path)
    lower = rel.lower()
    parts = [part.lower() for part in PurePosixPath(lower).parts]
    return _credential_deny_reason_for_parts(parts, lower)


def _teaching_deny_error(path: str, reason: str, *, where: str) -> FerryDenyError:
    return FerryDenyError(
        f"ferry refused {where} path {path!r}: matches credential deny pattern {reason!r}. "
        "The colleague's resident account credentials must never transit between controller and node; "
        "move secrets out of the transfer set and retry."
    )


def _teaching_content_deny_error(path: str, reason: str, *, where: str) -> FerryDenyError:
    return FerryDenyError(
        f"ferry refused {where} file {path!r}: contains high-confidence credential content signature {reason!r}. "
        "The colleague's resident account credentials must never transit between controller and node; "
        "move secrets out of the transfer set and retry."
    )


def assert_no_credential_paths(paths: Iterable[str], *, where: str) -> None:
    for path in paths:
        reason = credential_deny_reason(path)
        if reason:
            raise _teaching_deny_error(path, reason, where=where)


def _looks_provider_token_value(value: Any) -> bool:
    return isinstance(value, str) and len(value.strip()) >= 8


def _provider_token_json_key(value: Any) -> str | None:
    if isinstance(value, dict):
        for key, child in value.items():
            key_text = str(key)
            if key_text in PROVIDER_AUTH_TOKEN_JSON_KEYS and _looks_provider_token_value(child):
                return key_text
        for child in value.values():
            nested = _provider_token_json_key(child)
            if nested:
                return nested
    return None


def content_credential_deny_reason(path: Path) -> str | None:
    try:
        st = path.stat()
    except OSError:
        return None
    if not stat.S_ISREG(st.st_mode):
        return None
    try:
        with path.open("rb") as handle:
            data = handle.read(CONTENT_SIGNATURE_SCAN_BYTES + 1)
    except OSError:
        return None
    if PRIVATE_KEY_HEADER_RE.search(data[:4096]):
        return "private-key header"
    if st.st_size > CONTENT_SIGNATURE_SCAN_BYTES:
        return None
    stripped = data.lstrip()
    if not stripped.startswith(b"{"):
        return None
    try:
        parsed = json.loads(data.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    key = _provider_token_json_key(parsed)
    if key:
        return f"provider auth-token JSON key {key}"
    return None


def assert_no_credential_content(root: Path, paths: Iterable[str], *, where: str) -> None:
    for rel in paths:
        reason = content_credential_deny_reason(root / rel)
        if reason:
            raise _teaching_content_deny_error(rel, reason, where=where)


def _relative_to(root: Path, target: Path) -> str:
    try:
        return target.relative_to(root).as_posix()
    except ValueError as exc:
        raise FerryError(f"refusing symlink/path escape outside declared root: {target}") from exc


def _checked_local_file(root_real: Path, path: Path) -> str:
    rel = _relative_to(root_real, path)
    assert_no_credential_paths([rel], where="expanded")
    resolved = path.resolve(strict=True)
    real_rel = _relative_to(root_real, resolved)
    assert_no_credential_paths([real_rel], where="expanded realpath")
    try:
        st = resolved.stat()
    except OSError as exc:
        raise FerryError(f"local stat failed for {rel}: {exc}") from exc
    if st.st_nlink > 1:
        _deny_same_inode_aliases(root_real, rel, dev=st.st_dev, ino=st.st_ino, nlink=st.st_nlink, where="expanded")
    return rel


def expand_local_files(root: Path, requested_paths: Iterable[str]) -> list[str]:
    root_real = root.expanduser().resolve(strict=True)
    expanded: list[str] = []
    seen: set[str] = set()
    requested = [normalize_rel_path(path) for path in requested_paths]
    assert_no_credential_paths(requested, where="requested")
    for rel in requested:
        path = root_real / rel
        if not path.exists():
            raise FerryError(f"requested local path does not exist: {rel}")
        if path.is_dir() and not path.is_symlink():
            for child in sorted(path.rglob("*")):
                if child.is_dir() and not child.is_symlink():
                    continue
                if not child.is_file() and not child.is_symlink():
                    continue
                child_rel = _checked_local_file(root_real, child)
                if child_rel not in seen:
                    seen.add(child_rel)
                    expanded.append(child_rel)
            continue
        file_rel = _checked_local_file(root_real, path)
        if file_rel not in seen:
            seen.add(file_rel)
            expanded.append(file_rel)
    return expanded


def _audit_path(fleet_dir: Path) -> Path:
    path = fleet_dir / "audit" / "ferry.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    return path


def append_ferry_audit(fleet_dir: Path, receipt: dict[str, Any]) -> None:
    payload = dict(receipt)
    payload.setdefault("schema", "goalflight.fleet.ferry.receipt.v1")
    payload.setdefault("ts", _utc_iso())
    with _audit_path(fleet_dir).open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, sort_keys=True) + "\n")


def _load_node_entry(fleet_dir: Path, node_id: str) -> dict[str, Any]:
    import goalflight_fleet as fleet

    fleet.bootstrap(fleet_dir)
    doc = fleet.read_json(fleet_dir / "fleet.json")
    node_entry = (doc.get("nodes") or {}).get(node_id)
    if not isinstance(node_entry, dict):
        raise FerryError(f"unknown node: {node_id}")
    return node_entry


def _remote_target(host: fleet_ssh.SshHostSpec) -> str:
    if host.user:
        return f"{host.user}@{host.hostname}"
    return host.hostname


def _rsync_ssh_arg(host: fleet_ssh.SshHostSpec) -> str:
    parts = ["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new"]
    if host.port:
        parts.extend(["-p", str(host.port)])
    if host.identity_file:
        parts.extend(["-i", str(Path(host.identity_file).expanduser())])
    return shlex.join(parts)


def _remote_path_arg(host: fleet_ssh.SshHostSpec, root: str) -> str:
    base = str(root or "").strip()
    if not base or "\n" in base or "\r" in base:
        raise FerryError("remote transfer root must be a non-empty single-line path")
    return f"{_remote_target(host)}:{base.rstrip('/')}/"


def _remote_norm(path: str) -> str:
    raw = str(path or "").strip().replace("\\", "/").rstrip("/")
    if not raw or "\n" in raw or "\r" in raw:
        raise FerryError("remote transfer root must be a non-empty single-line path")
    return posixpath.normpath(raw)


def _remote_under(path: str, root: str) -> bool:
    candidate = _remote_norm(path)
    base = _remote_norm(root)
    return candidate == base or candidate.startswith(base.rstrip("/") + "/")


def _remote_allowed_roots(node_entry: dict[str, Any]) -> list[str]:
    repo_root = str(node_entry.get("repo_root") or "").strip()
    state_dir = str(node_entry.get("state_dir") or "").strip()
    return [root for root in (repo_root, state_dir, f"{state_dir.rstrip('/')}/worktrees" if state_dir else "") if root]


def assert_remote_root_allowed(node_entry: dict[str, Any], remote_root: str) -> None:
    roots = _remote_allowed_roots(node_entry)
    if not roots or not any(_remote_under(remote_root, root) for root in roots):
        raise FerryError(
            "remote transfer root must resolve under a declared node root "
            f"(repo_root/state_dir/worktrees): {remote_root}"
        )


def controller_staging_root(fleet_dir: Path) -> Path:
    return fleet_dir.expanduser().resolve() / "staging"


def assert_controller_staging_root_allowed(fleet_dir: Path, local_root: Path | str) -> None:
    staging = controller_staging_root(fleet_dir)
    candidate = Path(local_root).expanduser().resolve()
    if candidate != staging and staging not in candidate.parents:
        raise FerryError(f"controller pull destination must resolve under fleet staging root {staging}: {local_root}")


def _local_root_arg(root: Path) -> str:
    return str(root.expanduser().resolve()) + "/"


def _build_rsync_argv(
    *,
    host: fleet_ssh.SshHostSpec,
    direction: str,
    src_root: str,
    dst_root: str,
    files_from: Path,
    itemize: bool,
) -> list[str]:
    argv = ["rsync", "-a", "--checksum", "--files-from", str(files_from)]
    if itemize:
        argv.append("--itemize-changes")
    argv.extend(["-e", _rsync_ssh_arg(host)])
    if direction == "pull":
        argv.extend([_remote_path_arg(host, src_root), _local_root_arg(Path(dst_root))])
    elif direction == "push":
        argv.extend([_local_root_arg(Path(src_root)), _remote_path_arg(host, dst_root)])
    else:
        raise FerryError("direction must be 'pull' or 'push'")
    return argv


def _write_files_from(path: Path, transfer_files: list[str]) -> None:
    text = "".join(f"{normalize_rel_path(rel)}\n" for rel in transfer_files)
    path.write_text(text, encoding="utf-8")
    written = path.read_text(encoding="utf-8").splitlines()
    if written != transfer_files:
        raise FerryError("files-from validation failed: serialized file list differs from intended entries")


def _cleanup_partial_pull(dst_root: str, transfer_files: Iterable[str]) -> None:
    root = Path(dst_root).expanduser()
    for rel in transfer_files:
        target = root / rel
        with suppress(FileNotFoundError):
            if target.is_dir() and not target.is_symlink():
                continue
            target.unlink()
        parent = target.parent
        while parent != root and root in parent.parents:
            try:
                parent.rmdir()
            except OSError:
                break
            parent = parent.parent


def _scan_local_files(root: Path, transfer_files: Iterable[str], *, where: str) -> None:
    root_real = root.expanduser().resolve(strict=True)
    for rel in transfer_files:
        try:
            _checked_local_file(root_real, root_real / rel)
        except FerryDenyError:
            raise
        except OSError as exc:
            raise FerryError(f"{where} scan failed for {rel}: {exc}") from exc


def _make_push_staging(fleet_dir: Path) -> Path:
    base = controller_staging_root(fleet_dir) / "push"
    base.mkdir(parents=True, exist_ok=True, mode=0o700)
    return Path(tempfile.mkdtemp(prefix=".goalflight-ferry-push-", dir=str(base)))


def _copy_checked_push_files(src_root: Path, staging_root: Path, transfer_files: Iterable[str]) -> None:
    root_real = src_root.expanduser().resolve(strict=True)
    for rel in transfer_files:
        checked_rel = _checked_local_file(root_real, root_real / rel)
        if checked_rel != rel:
            raise FerryError(f"push staging path mismatch: {rel} resolved as {checked_rel}")
        source = root_real / rel
        target = staging_root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            with source.open("rb") as source_handle:
                st = os.fstat(source_handle.fileno())
                if not stat.S_ISREG(st.st_mode):
                    raise FerryError(f"push staging source is not a regular file: {rel}")
                if st.st_nlink > 1:
                    _deny_same_inode_aliases(
                        root_real,
                        rel,
                        dev=st.st_dev,
                        ino=st.st_ino,
                        nlink=st.st_nlink,
                        where="push staging",
                    )
                with target.open("wb") as target_handle:
                    shutil.copyfileobj(source_handle, target_handle)
            os.chmod(target, stat.S_IMODE(st.st_mode) & 0o777)
        except FerryDenyError:
            raise
        except OSError as exc:
            raise FerryError(f"push staging copy failed for {rel}: {exc}") from exc


def _make_pull_quarantine(dst_root: str) -> Path:
    root = Path(dst_root).expanduser()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    return Path(tempfile.mkdtemp(prefix=".goalflight-ferry-quarantine-", dir=str(root)))


def _seed_pull_quarantine(dst_root: str, quarantine_root: Path, transfer_files: Iterable[str]) -> None:
    root = Path(dst_root).expanduser()
    for rel in transfer_files:
        source = root / rel
        if not source.exists() and not source.is_symlink():
            continue
        target = quarantine_root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        if source.is_dir() and not source.is_symlink():
            continue
        shutil.copy2(source, target, follow_symlinks=False)


def _promote_pull_quarantine(quarantine_root: Path, dst_root: str, transfer_files: Iterable[str]) -> None:
    root = Path(dst_root).expanduser()
    for rel in transfer_files:
        source = quarantine_root / rel
        if not source.exists() and not source.is_symlink():
            raise FerryError(f"received file missing from quarantine: {rel}")
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists() or target.is_symlink():
            if target.is_dir() and not target.is_symlink():
                raise FerryError(f"refusing to replace destination directory with received file: {rel}")
            target.unlink()
        source.replace(target)


def _run(argv: list[str], runner: Callable[[list[str]], tuple[int, str, str]] | None) -> tuple[int, str, str]:
    if runner is not None:
        return runner(argv)
    proc = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8", errors="replace", check=False)
    return proc.returncode, proc.stdout, proc.stderr


def _remote_preflight_error(error: Exception) -> dict[str, Any]:
    payload: dict[str, Any] = {"ok": False, "error": str(error)}
    if isinstance(error, FerryDenyError):
        payload["error_type"] = "deny"
    return payload


def _same_or_under_text(path: str, root: str) -> bool:
    candidate = os.path.realpath(path)
    base = os.path.realpath(root)
    return candidate == base or candidate.startswith(base.rstrip(os.sep) + os.sep)


def _same_or_under_norm(path: str, root: str) -> bool:
    candidate = os.path.normpath(path)
    base = os.path.normpath(root)
    return candidate == base or candidate.startswith(base.rstrip(os.sep) + os.sep)


def _iter_same_inode_aliases(root: str, dev: int, ino: int) -> Iterable[str]:
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [name for name in dirnames if not os.path.islink(os.path.join(dirpath, name))]
        for name in filenames:
            path = os.path.join(dirpath, name)
            try:
                st = os.stat(path)
            except OSError:
                continue
            if st.st_dev == dev and st.st_ino == ino:
                yield path


def _deny_same_inode_aliases(
    root: str | Path,
    rel: str,
    *,
    dev: int,
    ino: int,
    nlink: int,
    where: str,
) -> None:
    root_text = os.fspath(root)
    aliases = list(_iter_same_inode_aliases(root_text, dev, ino))
    for alias in aliases:
        alias_rel = os.path.relpath(alias, root_text).replace(os.sep, "/")
        reason = credential_deny_reason(alias_rel)
        if reason:
            raise _teaching_deny_error(rel, reason, where=f"{where} hardlink alias {alias_rel!r}")
    if len(aliases) < nlink:
        raise _teaching_deny_error(rel, "hardlink", where=f"{where} hardlink")


def _remote_preflight_payload(payload: dict[str, Any]) -> dict[str, Any]:
    root = _remote_norm(str(payload.get("root") or ""))
    files = [normalize_rel_path(path) for path in payload.get("files") or []]
    allowed_roots = [_remote_norm(str(path)) for path in payload.get("allowed_roots") or [] if str(path or "").strip()]
    if not files:
        raise FerryError("remote preflight requires at least one file")
    if not allowed_roots:
        raise FerryError("remote preflight requires allowed roots")
    assert_no_credential_paths(files, where="remote requested")

    root_real = os.path.realpath(root)
    allowed_real = [os.path.realpath(path) for path in allowed_roots]
    if not any(_remote_under(root, allowed) or _same_or_under_text(root_real, allowed) for allowed in allowed_roots):
        raise FerryError(f"remote root resolves outside declared roots: {root}")
    if not any(_same_or_under_text(root_real, allowed) for allowed in allowed_real):
        raise FerryError(f"remote root realpath resolves outside declared roots: {root_real}")

    checked: list[dict[str, Any]] = []
    for rel in files:
        candidate = os.path.normpath(os.path.join(root_real, rel.replace("/", os.sep)))
        if not _same_or_under_norm(candidate, root_real):
            raise FerryError(f"remote path escapes declared root: {rel}")
        real = os.path.realpath(candidate)
        reason = _credential_deny_reason_for_path_text(real)
        if reason:
            raise _teaching_deny_error(rel, reason, where="remote realpath")
        if not _same_or_under_text(real, root_real):
            raise FerryError(f"remote path realpath escapes declared root: {rel} -> {real}")
        try:
            st = os.stat(candidate)
        except FileNotFoundError:
            checked.append({"path": rel, "realpath": real, "exists": False})
            continue
        except OSError as exc:
            raise FerryError(f"remote stat failed for {rel}: {exc}") from exc

        if st.st_nlink > 1:
            _deny_same_inode_aliases(
                root_real,
                rel,
                dev=st.st_dev,
                ino=st.st_ino,
                nlink=st.st_nlink,
                where="remote",
            )
        checked.append({"path": rel, "realpath": real, "exists": True, "nlink": st.st_nlink})
    return {"ok": True, "checked": checked}


def _run_remote_preflight(
    fleet_dir: Path,
    *,
    node_id: str,
    node_entry: dict[str, Any],
    remote_root: str,
    files: Iterable[str],
    runner: Callable[[list[str]], tuple[int, str, str]] | None,
) -> None:
    transfer_files = [normalize_rel_path(path) for path in files]
    assert_no_credential_paths(transfer_files, where="remote requested")
    repo_root = str(node_entry.get("repo_root") or "").strip()
    if not repo_root:
        raise FerryError(f"node {node_id} has no declared repo_root")
    remote = fleet_ssh.build_remote_command(
        "ferry_preflight",
        repo_root=repo_root,
        root=remote_root,
        files=transfer_files,
        allowed_roots=_remote_allowed_roots(node_entry),
        python=str(node_entry.get("python") or "python3"),
    )
    host = fleet_ssh.host_from_node_entry(node_id, node_entry)
    ssh_argv = fleet_ssh.build_ssh_command(host, remote, command_class="ferry_preflight")
    with fleet_ssh.node_ssh_lock(node_id, fleet_dir=fleet_dir):
        code, stdout, stderr = _run(ssh_argv, runner)
    if code == 0:
        return
    try:
        payload = json.loads(stdout or stderr or "{}")
    except json.JSONDecodeError:
        payload = {}
    if payload.get("error_type") == "deny":
        raise FerryDenyError(str(payload.get("error") or "remote credential deny preflight failed"))
    raise FerryError(
        f"remote ferry preflight failed for {node_id} exit {code}: "
        f"{str(payload.get('error') or stderr).strip()}"
    )


def _file_entry(root: Path, rel: str) -> dict[str, Any]:
    path = root / rel
    entry: dict[str, Any] = {"path": rel, "exists": path.exists()}
    if path.is_file():
        data = path.read_bytes()
        entry.update({"size": len(data), "sha256": hashlib.sha256(data).hexdigest()})
    return entry


def execute_ferry(
    fleet_dir: Path,
    *,
    node_id: str,
    direction: str,
    src_root: str,
    dst_root: str,
    files: Iterable[str],
    purpose: str,
    runner: Callable[[list[str]], tuple[int, str, str]] | None = None,
    dry_run: bool = False,
    itemize: bool = False,
    expanded_files: Iterable[str] | None = None,
) -> FerryReceipt:
    if not purpose.strip():
        raise FerryError("ferry purpose label is required")
    requested = [normalize_rel_path(path) for path in files]
    assert_no_credential_paths(requested, where="requested")
    if expanded_files is not None:
        transfer_files = [normalize_rel_path(path) for path in expanded_files]
        assert_no_credential_paths(transfer_files, where="expanded")
    elif direction == "push":
        transfer_files = expand_local_files(Path(src_root), requested)
    else:
        transfer_files = requested
        assert_no_credential_paths(transfer_files, where="expanded")
    if direction == "push" and expanded_files is not None:
        _scan_local_files(Path(src_root), transfer_files, where="expanded")

    node_entry = _load_node_entry(fleet_dir, node_id)
    remote_root = dst_root if direction == "push" else src_root
    assert_remote_root_allowed(node_entry, remote_root)
    if direction == "pull":
        assert_controller_staging_root_allowed(fleet_dir, dst_root)
    host = fleet_ssh.host_from_node_entry(node_id, node_entry)
    receipt: dict[str, Any] = {
        "schema": "goalflight.fleet.ferry.receipt.v1",
        "ts": _utc_iso(),
        "node_id": node_id,
        "direction": direction,
        "purpose": purpose,
        "src": {
            "node": "controller" if direction == "push" else node_id,
            "path": src_root,
        },
        "dst": {
            "node": node_id if direction == "push" else "controller",
            "path": dst_root,
        },
        "requested_files": requested,
        "files": transfer_files,
        "file_count": len(transfer_files),
        "itemize": itemize,
        "dry_run": dry_run,
    }
    if not transfer_files:
        receipt.update({"ok": True, "skipped": "empty transfer set", "exit_code": 0, "stdout": "", "stderr": ""})
        if not dry_run:
            append_ferry_audit(fleet_dir, receipt)
        return FerryReceipt(receipt)

    pull_quarantine: Path | None = None
    push_staging_root: Path | None = None
    rsync_src_root = src_root
    rsync_dst_root = dst_root
    if not dry_run:
        assert_live_ssh_opt_in()
        if direction == "push":
            _scan_local_files(Path(src_root), transfer_files, where="pre-stage push")
            try:
                push_staging_root = _make_push_staging(fleet_dir)
                _copy_checked_push_files(Path(src_root), push_staging_root, transfer_files)
                _scan_local_files(push_staging_root, transfer_files, where="staged push")
            except Exception:
                if push_staging_root is not None:
                    shutil.rmtree(push_staging_root, ignore_errors=True)
                    push_staging_root = None
                raise
            rsync_src_root = str(push_staging_root)
            receipt["push_staging_root"] = rsync_src_root
        try:
            _run_remote_preflight(
                fleet_dir,
                node_id=node_id,
                node_entry=node_entry,
                remote_root=remote_root,
                files=transfer_files,
                runner=runner,
            )
        except Exception:
            if push_staging_root is not None:
                shutil.rmtree(push_staging_root, ignore_errors=True)
                push_staging_root = None
            raise
    if direction == "pull":
        Path(dst_root).expanduser().mkdir(parents=True, exist_ok=True, mode=0o700)
        if not dry_run:
            pull_quarantine = _make_pull_quarantine(dst_root)
            _seed_pull_quarantine(dst_root, pull_quarantine, transfer_files)
            rsync_dst_root = str(pull_quarantine)
            receipt["quarantine_root"] = rsync_dst_root

    with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False) as handle:
        files_from = Path(handle.name)
    _write_files_from(files_from, transfer_files)
    try:
        argv = _build_rsync_argv(
            host=host,
            direction=direction,
            src_root=rsync_src_root,
            dst_root=rsync_dst_root,
            files_from=files_from,
            itemize=itemize,
        )
        receipt["rsync_argv"] = argv
        if dry_run:
            receipt.update({"ok": True, "exit_code": 0, "stdout": "", "stderr": ""})
        else:
            with fleet_ssh.node_ssh_lock(node_id, fleet_dir=fleet_dir):
                code, stdout, stderr = _run(argv, runner)
            receipt.update({"ok": code == 0, "exit_code": code, "stdout": stdout, "stderr": stderr})
            if code != 0:
                if direction == "pull":
                    if pull_quarantine is not None:
                        shutil.rmtree(pull_quarantine, ignore_errors=True)
                    else:
                        _cleanup_partial_pull(dst_root, transfer_files)
                append_ferry_audit(fleet_dir, receipt)
                raise FerryError(f"rsync ferry failed for {node_id} ({direction}) exit {code}: {stderr.strip()}")
            if direction == "pull":
                if pull_quarantine is None:
                    raise FerryError("pull quarantine missing after live transfer")
                _scan_local_files(pull_quarantine, transfer_files, where="post-receive quarantine")
                assert_no_credential_content(
                    pull_quarantine,
                    transfer_files,
                    where="post-receive quarantine",
                )
                _promote_pull_quarantine(pull_quarantine, dst_root, transfer_files)
            elif direction == "push":
                _run_remote_preflight(
                    fleet_dir,
                    node_id=node_id,
                    node_entry=node_entry,
                    remote_root=remote_root,
                    files=transfer_files,
                    runner=runner,
                )
        if not dry_run:
            append_ferry_audit(fleet_dir, receipt)
        return FerryReceipt(receipt)
    finally:
        with suppress(OSError):
            files_from.unlink()
        if pull_quarantine is not None:
            shutil.rmtree(pull_quarantine, ignore_errors=True)
        if push_staging_root is not None:
            shutil.rmtree(push_staging_root, ignore_errors=True)


def parse_porcelain_paths(stdout: str) -> tuple[list[str], list[dict[str, str]]]:
    paths: list[str] = []
    skipped: list[dict[str, str]] = []
    for raw in stdout.splitlines():
        if not raw.strip() or len(raw) < 4:
            continue
        status = raw[:2]
        value = raw[3:].strip()
        if " -> " in value:
            value = value.rsplit(" -> ", 1)[1].strip()
        if "D" in status:
            skipped.append({"path": value, "reason": "deleted in git status", "action": "skipped"})
            continue
        try:
            rel = normalize_rel_path(value)
        except FerryError as exc:
            skipped.append({"path": value, "reason": str(exc), "action": "skipped"})
            continue
        if rel not in paths:
            paths.append(rel)
    return paths, skipped


def filter_salvage_denied(paths: Iterable[str]) -> tuple[list[str], list[dict[str, str]]]:
    allowed: list[str] = []
    skipped: list[dict[str, str]] = []
    for path in paths:
        reason = credential_deny_reason(path)
        if reason:
            skipped.append(
                {
                    "path": path,
                    "reason": f"credential deny pattern {reason}",
                    "action": "skipped_not_ferried",
                }
            )
            continue
        allowed.append(path)
    return allowed, skipped


def _salvage_targets_from_porcelain(stdout: str) -> tuple[list[str], list[str], list[dict[str, str]]]:
    parsed_paths, parse_skipped = parse_porcelain_paths(stdout)
    target_files, deny_skipped = filter_salvage_denied(parsed_paths)
    return parsed_paths, target_files, parse_skipped + deny_skipped


def _assert_salvage_targets_unchanged(
    *,
    current_paths: list[str],
    previous_paths: list[str],
    current_targets: list[str],
    previous_targets: list[str],
    phase: str,
) -> None:
    if current_paths != previous_paths or current_targets != previous_targets:
        raise FerryError(
            f"remote dirty file set changed before {phase}; aborting salvage to avoid stale files-from transfer"
        )


def _run_remote_git_status(
    fleet_dir: Path,
    *,
    node_id: str,
    worktree_path: str,
    runner: Callable[[list[str]], tuple[int, str, str]] | None,
) -> str:
    node_entry = _load_node_entry(fleet_dir, node_id)
    assert_remote_root_allowed(node_entry, worktree_path)
    repo_root = str(node_entry.get("repo_root") or worktree_path)
    remote = fleet_ssh.build_remote_command(
        "git_status_porcelain",
        repo_root=repo_root,
        worktree_path=worktree_path,
        allowed_roots=_remote_allowed_roots(node_entry),
    )
    host = fleet_ssh.host_from_node_entry(node_id, node_entry)
    ssh_argv = fleet_ssh.build_ssh_command(host, remote, command_class="git_status_porcelain")
    with fleet_ssh.node_ssh_lock(node_id, fleet_dir=fleet_dir):
        code, stdout, stderr = _run(ssh_argv, runner)
    if code != 0:
        raise FerryError(f"remote git status failed for {node_id} exit {code}: {stderr.strip()}")
    return stdout


def parse_rsync_itemized_paths(stdout: str) -> list[str]:
    changed: list[str] = []
    for raw in stdout.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("*deleting "):
            value = line[len("*deleting ") :].strip()
        else:
            parts = line.split(maxsplit=1)
            if len(parts) != 2:
                continue
            value = parts[1].strip()
        if value.endswith("/"):
            continue
        try:
            rel = normalize_rel_path(value)
        except FerryError:
            continue
        changed.append(rel)
    return changed


def _matches_append_only(path: str, patterns: Iterable[str]) -> bool:
    rel = normalize_rel_path(path)
    basename = PurePosixPath(rel).name
    for pattern in patterns:
        normalized = str(pattern).strip().replace("\\", "/")
        if normalized and (fnmatch.fnmatch(rel, normalized) or fnmatch.fnmatch(basename, normalized)):
            return True
    return False


def _merge_append_only_patterns(extra: Iterable[str] | None) -> tuple[str, ...]:
    merged: list[str] = []
    seen: set[str] = set()
    for pattern in (*DEFAULT_APPEND_ONLY_PATTERNS, *(extra or ())):
        normalized = str(pattern).strip().replace("\\", "/")
        if normalized and normalized not in seen:
            seen.add(normalized)
            merged.append(normalized)
    return tuple(merged)


def _salvage_lock_identity(fleet_dir: Path, dispatch_id: str | None) -> dict[str, str]:
    """Resolve dispatch + account lock fields for post-salvage release."""
    if not dispatch_id:
        return {}
    import goalflight_fleet_reconcile as fleet_reconcile
    import goalflight_fleet_status_cli as status_cli

    meta = status_cli._collect_dispatch_meta(fleet_dir).get(dispatch_id) or {}
    lock = fleet_reconcile.resolve_account_lock_for_dispatch(fleet_dir, dispatch_id, meta)
    identity: dict[str, str] = {"dispatch_id": dispatch_id}
    if not lock:
        return identity
    account_key = lock.get("account_key")
    fencing_token = lock.get("fencing_token")
    if isinstance(account_key, str) and account_key:
        identity["account_key"] = account_key
    if isinstance(fencing_token, str) and fencing_token:
        identity["fencing_token"] = fencing_token
    return identity


def salvage_worktree(
    fleet_dir: Path,
    *,
    node_id: str,
    worktree_path: str,
    out_dir: Path,
    purpose: str = "salvage",
    dispatch_id: str | None = None,
    runner: Callable[[list[str]], tuple[int, str, str]] | None = None,
    max_iterations: int = 10,
    append_only_paths: Iterable[str] | None = None,
    sleep_s: float = 1.0,
) -> dict[str, Any]:
    assert_live_ssh_opt_in()
    if max_iterations < 1:
        raise FerryError("max_iterations must be >= 1")
    out_dir = out_dir.expanduser()
    assert_controller_staging_root_allowed(fleet_dir, out_dir)
    out_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    append_only_patterns = _merge_append_only_patterns(append_only_paths)
    porcelain = _run_remote_git_status(
        fleet_dir,
        node_id=node_id,
        worktree_path=worktree_path,
        runner=runner,
    )
    parsed_paths, target_files, skipped = _salvage_targets_from_porcelain(porcelain)

    initial_receipt: dict[str, Any] | None = None
    if target_files:
        current_paths, current_targets, current_skipped = _salvage_targets_from_porcelain(
            _run_remote_git_status(
                fleet_dir,
                node_id=node_id,
                worktree_path=worktree_path,
                runner=runner,
            )
        )
        _assert_salvage_targets_unchanged(
            current_paths=current_paths,
            previous_paths=parsed_paths,
            current_targets=current_targets,
            previous_targets=target_files,
            phase="initial rsync",
        )
        skipped = current_skipped
        initial_receipt = execute_ferry(
            fleet_dir,
            node_id=node_id,
            direction="pull",
            src_root=worktree_path,
            dst_root=str(out_dir),
            files=target_files,
            expanded_files=target_files,
            purpose=f"{purpose}:initial",
            runner=runner,
        ).to_dict()

    iterations: list[dict[str, Any]] = []
    consecutive_zero = 0
    converged = not target_files
    for idx in range(1, max_iterations + 1):
        if not target_files:
            break
        current_paths, current_targets, current_skipped = _salvage_targets_from_porcelain(
            _run_remote_git_status(
                fleet_dir,
                node_id=node_id,
                worktree_path=worktree_path,
                runner=runner,
            )
        )
        _assert_salvage_targets_unchanged(
            current_paths=current_paths,
            previous_paths=parsed_paths,
            current_targets=current_targets,
            previous_targets=target_files,
            phase=f"convergence rsync {idx}",
        )
        skipped = current_skipped
        receipt = execute_ferry(
            fleet_dir,
            node_id=node_id,
            direction="pull",
            src_root=worktree_path,
            dst_root=str(out_dir),
            files=target_files,
            expanded_files=target_files,
            purpose=f"{purpose}:convergence-{idx}",
            runner=runner,
            itemize=True,
        ).to_dict()
        changed = parse_rsync_itemized_paths(str(receipt.get("stdout") or ""))
        checked_changed = [path for path in changed if not _matches_append_only(path, append_only_patterns)]
        zero_delta = not checked_changed
        consecutive_zero = consecutive_zero + 1 if zero_delta else 0
        iterations.append(
            {
                "iteration": idx,
                "changed": changed,
                "checked_changed": checked_changed,
                "zero_delta": zero_delta,
                "consecutive_zero": consecutive_zero,
            }
        )
        if consecutive_zero >= 2:
            converged = True
            break
        if idx < max_iterations:
            time.sleep(sleep_s)

    files = [_file_entry(out_dir, rel) for rel in target_files]
    lock_identity = _salvage_lock_identity(fleet_dir, dispatch_id)
    manifest: dict[str, Any] = {
        "schema": "goalflight.fleet.salvage.manifest.v1",
        "ts": _utc_iso(),
        "node_id": node_id,
        "worktree_path": worktree_path,
        "salvage_dir": str(out_dir),
        "target_files": target_files,
        "skipped": skipped,
        "files": files,
        "iterations": iterations,
        "max_iterations": max_iterations,
        "converged": converged,
        "initial_receipt": initial_receipt,
    }
    manifest.update(lock_identity)
    if lock_identity.get("account_key") and lock_identity.get("fencing_token"):
        manifest["lock_release_command"] = (
            "goalflight_fleet.py lock-release "
            f"--account-key {lock_identity['account_key']} "
            f"--fencing-token {lock_identity['fencing_token']} "
            "--reason salvage_complete"
        )
    if not converged:
        manifest["liveness_signal"] = "worktree still changing - worker may be alive"
    manifest_path = out_dir / "salvage-manifest.json"
    manifest["manifest_path"] = str(manifest_path)
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    return manifest


def cmd_ferry(args) -> int:
    try:
        receipt = execute_ferry(
            args.fleet_dir,
            node_id=args.node,
            direction=args.direction,
            src_root=args.src_root,
            dst_root=args.dst_root,
            files=args.path,
            purpose=args.purpose,
            dry_run=not args.exec,
        ).to_dict()
    except FerryError as exc:
        print(str(exc), file=__import__("sys").stderr)
        return 2
    print(json.dumps(receipt, indent=2, sort_keys=True))
    return 0


def cmd_salvage(args) -> int:
    if not args.exec:
        print(
            json.dumps(
                {
                    "dry_run": True,
                    "node_id": args.node,
                    "worktree_path": args.worktree_path,
                    "out_dir": str(args.out_dir),
                    "max_iterations": args.max_iterations,
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 0
    try:
        manifest = salvage_worktree(
            args.fleet_dir,
            node_id=args.node,
            worktree_path=args.worktree_path,
            out_dir=args.out_dir,
            purpose=args.purpose,
            dispatch_id=getattr(args, "dispatch_id", None),
            max_iterations=args.max_iterations,
            append_only_paths=args.append_only,
            sleep_s=args.sleep_s,
        )
    except FerryError as exc:
        print(str(exc), file=__import__("sys").stderr)
        return 2
    print(json.dumps(manifest, indent=2, sort_keys=True))
    return 0


def _cmd_remote_preflight(argv: list[str]) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Internal fleet ferry remote preflight")
    parser.add_argument("--payload-b64", required=True)
    args = parser.parse_args(argv)
    try:
        payload = json.loads(base64.b64decode(args.payload_b64.encode("ascii")).decode("utf-8"))
        result = _remote_preflight_payload(payload)
    except Exception as exc:  # remote helper reports errors as data for the controller.
        result = _remote_preflight_error(exc)
        print(json.dumps(result, sort_keys=True))
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


def _main(argv: list[str] | None = None) -> int:
    import sys

    args = list(sys.argv[1:] if argv is None else argv)
    if args[:1] == ["remote-preflight"]:
        return _cmd_remote_preflight(args[1:])
    print("usage: goalflight_fleet_ferry.py remote-preflight --payload-b64 <payload>", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(_main())
