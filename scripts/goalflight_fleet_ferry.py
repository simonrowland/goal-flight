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
DEFAULT_MAX_FILE_BYTES = 100 * 1024 * 1024
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
    payload.setdefault("schema", "goalflight.fleet.ferry.receipt.v2")
    payload.setdefault("ts", _utc_iso())
    with _audit_path(fleet_dir).open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, sort_keys=True) + "\n")


def _load_node_entry(fleet_dir: Path, node_id: str) -> dict[str, Any]:
    import goalflight_fleet_store as fleet

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
    max_file_bytes: int,
) -> list[str]:
    # Salvage reuses its quarantine across passes; checksum detects same-size,
    # same-mtime rewrites that would otherwise look quiet to convergence.
    argv = [
        "rsync",
        "-a",
        "-z",
        "--checksum",
        f"--max-size={max_file_bytes}",
        "--files-from",
        str(files_from),
    ]
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


def _copy_checked_push_files(
    src_root: Path,
    staging_root: Path,
    transfer_files: Iterable[str],
    *,
    max_file_bytes: int,
) -> list[dict[str, Any]]:
    root_real = src_root.expanduser().resolve(strict=True)
    excluded: list[dict[str, Any]] = []
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
                if st.st_size > max_file_bytes:
                    excluded.append(
                        {
                            "path": rel,
                            "size": st.st_size,
                            "rule": "max-file-bytes",
                            "limit_bytes": max_file_bytes,
                        }
                    )
                    continue
                with target.open("wb") as target_handle:
                    copied = 0
                    observed_size: int | None = None
                    while True:
                        block = source_handle.read(1024 * 1024)
                        if not block:
                            break
                        if copied + len(block) > max_file_bytes:
                            observed_size = max(os.fstat(source_handle.fileno()).st_size, copied + len(block))
                            break
                        target_handle.write(block)
                        copied += len(block)
                if observed_size is not None:
                    with suppress(FileNotFoundError):
                        target.unlink()
                    excluded.append(
                        {
                            "path": rel,
                            "size": observed_size,
                            "rule": "max-file-bytes",
                            "limit_bytes": max_file_bytes,
                        }
                    )
                    continue
            os.chmod(target, stat.S_IMODE(st.st_mode) & 0o777)
        except FerryDenyError:
            raise
        except OSError as exc:
            raise FerryError(f"push staging copy failed for {rel}: {exc}") from exc
    return excluded


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
    seen: set[str] = set()

    def check_path(rel: str) -> None:
        if rel in seen:
            return
        seen.add(rel)
        assert_no_credential_paths([rel], where="remote requested")
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
            st = os.lstat(candidate)
        except FileNotFoundError:
            checked.append({"path": rel, "realpath": real, "exists": False})
            return
        except OSError as exc:
            raise FerryError(f"remote stat failed for {rel}: {exc}") from exc

        if stat.S_ISDIR(st.st_mode):
            try:
                children = sorted(os.scandir(candidate), key=lambda child: child.name)
            except OSError as exc:
                raise FerryError(f"remote directory scan failed for {rel}: {exc}") from exc
            if not children:
                checked.append(
                    {
                        "path": rel,
                        "realpath": real,
                        "exists": True,
                        "size": st.st_size,
                        "ctime_ns": st.st_ctime_ns,
                        "is_regular_file": False,
                    }
                )
                return
            for child in children:
                child_rel = normalize_rel_path(f"{rel}/{child.name}")
                check_path(child_rel)
            return

        try:
            target_st = os.stat(candidate)
        except OSError as exc:
            raise FerryError(f"remote stat failed for {rel}: {exc}") from exc
        if target_st.st_nlink > 1:
            _deny_same_inode_aliases(
                root_real,
                rel,
                dev=target_st.st_dev,
                ino=target_st.st_ino,
                nlink=target_st.st_nlink,
                where="remote",
            )
        checked.append(
            {
                "path": rel,
                "realpath": real,
                "exists": True,
                "nlink": st.st_nlink,
                "size": st.st_size,
                "ctime_ns": st.st_ctime_ns,
                "is_regular_file": stat.S_ISREG(st.st_mode),
            }
        )

    for rel in files:
        check_path(rel)
    return {"ok": True, "checked": checked}


def _run_remote_preflight(
    fleet_dir: Path,
    *,
    node_id: str,
    node_entry: dict[str, Any],
    remote_root: str,
    files: Iterable[str],
    runner: Callable[[list[str]], tuple[int, str, str]] | None,
) -> dict[str, dict[str, Any]]:
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
    try:
        payload = json.loads(stdout or stderr or "{}")
    except json.JSONDecodeError:
        payload = {}
    if code == 0 and payload.get("ok") is True and isinstance(payload.get("checked"), list):
        checked: dict[str, dict[str, Any]] = {}
        for item in payload["checked"]:
            if not isinstance(item, dict) or not isinstance(item.get("path"), str):
                raise FerryError("remote ferry preflight returned a malformed checked path")
            rel = normalize_rel_path(item["path"])
            if rel != item["path"] or rel in checked or not isinstance(item.get("exists"), bool):
                raise FerryError(f"remote ferry preflight returned an invalid checked path: {item['path']!r}")
            if item["exists"]:
                size = item.get("size")
                if (
                    not isinstance(item.get("is_regular_file"), bool)
                    or not isinstance(size, int)
                    or isinstance(size, bool)
                    or size < 0
                    or not isinstance(item.get("ctime_ns"), int)
                    or isinstance(item.get("ctime_ns"), bool)
                ):
                    raise FerryError(f"remote ferry preflight returned invalid file metadata for {rel}")
            checked[rel] = item
        for requested_path in transfer_files:
            if not any(
                checked_path == requested_path or checked_path.startswith(requested_path.rstrip("/") + "/")
                for checked_path in checked
            ):
                raise FerryError(f"remote ferry preflight omitted requested path: {requested_path}")
        for checked_path in checked:
            if not any(
                checked_path == requested_path or checked_path.startswith(requested_path.rstrip("/") + "/")
                for requested_path in transfer_files
            ):
                raise FerryError(f"remote ferry preflight returned an unrequested path: {checked_path}")
        return checked
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
    max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
    _preflight_metadata: dict[str, dict[str, Any]] | None = None,
) -> FerryReceipt:
    if not purpose.strip():
        raise FerryError("ferry purpose label is required")
    if not isinstance(max_file_bytes, int) or isinstance(max_file_bytes, bool) or max_file_bytes < 1:
        raise FerryError("max_file_bytes must be a positive integer")
    if direction not in {"pull", "push"}:
        raise FerryError("direction must be 'pull' or 'push'")
    requested = [normalize_rel_path(path) for path in files]
    assert_no_credential_paths(requested, where="requested")
    if expanded_files is not None:
        candidate_files = [normalize_rel_path(path) for path in expanded_files]
        assert_no_credential_paths(candidate_files, where="expanded")
    elif direction == "push":
        candidate_files = expand_local_files(Path(src_root), requested)
    else:
        candidate_files = requested
    if direction == "push" and expanded_files is not None:
        _scan_local_files(Path(src_root), candidate_files, where="expanded")

    node_entry = _load_node_entry(fleet_dir, node_id)
    remote_root = dst_root if direction == "push" else src_root
    assert_remote_root_allowed(node_entry, remote_root)
    if direction == "pull":
        assert_controller_staging_root_allowed(fleet_dir, dst_root)
    host = fleet_ssh.host_from_node_entry(node_id, node_entry)
    if direction == "push":
        source_metadata = _source_file_metadata(Path(src_root), candidate_files)
    elif not dry_run:
        assert_live_ssh_opt_in()
        # Salvage reuses its cap-aware convergence scan instead of walking the remote tree twice.
        source_metadata = _preflight_metadata
        if source_metadata is None:
            source_metadata = _run_remote_preflight(
                fleet_dir,
                node_id=node_id,
                node_entry=node_entry,
                remote_root=src_root,
                files=candidate_files,
                runner=runner,
            )
        candidate_files = list(source_metadata)
    else:
        source_metadata = {}
    transfer_files, excluded_files = _classify_transfer_files(
        candidate_files,
        metadata=source_metadata,
        max_file_bytes=max_file_bytes,
        require_sizes=not dry_run,
    )
    receipt: dict[str, Any] = {
        "schema": "goalflight.fleet.ferry.receipt.v2",
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
        "max_file_bytes": max_file_bytes,
        "excluded_files": excluded_files,
        "incomplete": bool(excluded_files),
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
            _scan_local_files(Path(src_root), candidate_files, where="pre-stage push")
            try:
                push_staging_root = _make_push_staging(fleet_dir)
                stage_excluded = _copy_checked_push_files(
                    Path(src_root),
                    push_staging_root,
                    transfer_files,
                    max_file_bytes=max_file_bytes,
                )
                if stage_excluded:
                    excluded_files.extend(stage_excluded)
                    excluded_paths = {entry["path"] for entry in stage_excluded}
                    transfer_files = [path for path in transfer_files if path not in excluded_paths]
                    receipt["files"] = transfer_files
                    receipt["file_count"] = len(transfer_files)
                    receipt["excluded_files"] = excluded_files
                    receipt["incomplete"] = True
                    if not transfer_files:
                        receipt.update(
                            {
                                "ok": True,
                                "skipped": "empty transfer set",
                                "exit_code": 0,
                                "stdout": "",
                                "stderr": "",
                            }
                        )
                        append_ferry_audit(fleet_dir, receipt)
                        shutil.rmtree(push_staging_root, ignore_errors=True)
                        push_staging_root = None
                        return FerryReceipt(receipt)
                _scan_local_files(push_staging_root, transfer_files, where="staged push")
            except Exception:
                if push_staging_root is not None:
                    shutil.rmtree(push_staging_root, ignore_errors=True)
                    push_staging_root = None
                raise
            rsync_src_root = str(push_staging_root)
            receipt["push_staging_root"] = rsync_src_root
        if direction == "push":
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
            max_file_bytes=max_file_bytes,
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
                postflight = _run_remote_preflight(
                    fleet_dir,
                    node_id=node_id,
                    node_entry=node_entry,
                    remote_root=src_root,
                    files=transfer_files,
                    runner=runner,
                )
                refused_after_read: list[dict[str, Any]] = []
                received_files: list[str] = []
                for rel in transfer_files:
                    info = postflight.get(rel) or {}
                    size = info.get("size") if isinstance(info.get("size"), int) else None
                    if info.get("exists") is not True:
                        refused_after_read.append(
                            {
                                "path": rel,
                                "size": None,
                                "rule": "source-disappeared-after-rsync",
                            }
                        )
                        continue
                    if info.get("exists") is True and info.get("is_regular_file") is True and size is None:
                        raise FerryError(f"file size missing from ferry postflight for {rel}; refusing promotion")
                    if info.get("is_regular_file") is True and size is not None and size > max_file_bytes:
                        refused_after_read.append(
                            {
                                "path": rel,
                                "size": size,
                                "rule": "max-file-bytes",
                                "limit_bytes": max_file_bytes,
                            }
                        )
                        continue
                    before = source_metadata.get(rel) or {}
                    # Also check symlinks: ctime and size describe the link itself.
                    if (
                        before.get("size") != size
                        or before.get("ctime_ns") != info.get("ctime_ns")
                        or before.get("is_regular_file") != info.get("is_regular_file")
                    ):
                        refused_after_read.append(
                            {
                                "path": rel,
                                "size": size,
                                "rule": "source-changed-during-rsync",
                            }
                        )
                        continue
                    received_files.append(rel)
                if refused_after_read:
                    excluded_files.extend(refused_after_read)
                    receipt["files"] = received_files
                    receipt["file_count"] = len(received_files)
                    receipt["excluded_files"] = excluded_files
                    receipt["incomplete"] = True
                _scan_local_files(pull_quarantine, received_files, where="post-receive quarantine")
                assert_no_credential_content(
                    pull_quarantine,
                    received_files,
                    where="post-receive quarantine",
                )
                _promote_pull_quarantine(pull_quarantine, dst_root, received_files)
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


def parse_porcelain_entries(stdout: str) -> tuple[list[tuple[str, str]], list[dict[str, str]]]:
    entries: list[tuple[str, str]] = []
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
        entry = (status, rel)
        if entry not in entries:
            entries.append(entry)
    return entries, skipped


def filter_salvage_denied(
    entries: Iterable[tuple[str, str]],
) -> tuple[list[tuple[str, str]], list[dict[str, str]]]:
    allowed: list[tuple[str, str]] = []
    skipped: list[dict[str, str]] = []
    for status, path in entries:
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
        allowed.append((status, path))
    return allowed, skipped


def _salvage_entries_from_porcelain(
    stdout: str,
) -> tuple[list[tuple[str, str]], list[tuple[str, str]], list[dict[str, str]]]:
    entries, parse_skipped = parse_porcelain_entries(stdout)
    allowed_entries, deny_skipped = filter_salvage_denied(entries)
    return entries, allowed_entries, parse_skipped + deny_skipped


def _assert_salvage_targets_unchanged(
    *,
    current_paths: list[str],
    previous_paths: list[str],
    phase: str,
) -> None:
    if current_paths != previous_paths:
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


def _normalize_pytest_basetemp_paths(paths: Iterable[str] | None) -> tuple[str, ...]:
    normalized: list[str] = []
    seen: set[str] = set()
    for path in paths or ():
        raw = str(path or "").strip().replace("\\", "/")
        if ".." in raw.split("/"):
            raise FerryError(f"refusing pytest --basetemp parent traversal: {path!r}")
        rel = normalize_rel_path(raw)
        if rel not in seen:
            seen.add(rel)
            normalized.append(rel)
    return tuple(normalized)


def _scratch_exclusion_rule(path: str, pytest_basetemp_paths: Iterable[str]) -> str | None:
    rel = normalize_rel_path(path)
    parts = PurePosixPath(rel).parts
    for root in pytest_basetemp_paths:
        if rel == root or rel.startswith(root.rstrip("/") + "/"):
            return f"explicit pytest --basetemp target {root} (pytest recreates its contents)"
    if any(part == ".pytest_cache" for part in parts):
        return ".pytest_cache directory (pytest regenerates cache metadata)"
    if "__pycache__" in parts and PurePosixPath(rel).name.endswith(".pyc"):
        return "Python bytecode file inside __pycache__ (regenerated from source)"
    if (
        len(parts) >= 3
        and parts[0].startswith("pytest-of-")
        and re.fullmatch(r"pytest-[0-9]+", parts[1])
    ):
        return "worktree-root pytest temp tree (pytest recreates its test data)"
    return None


def _source_file_metadata(root: Path, files: Iterable[str]) -> dict[str, dict[str, Any]]:
    root_real = root.expanduser().resolve(strict=True)
    result: dict[str, dict[str, Any]] = {}
    for rel in files:
        source = root_real / rel
        try:
            st = os.stat(source)
        except OSError as exc:
            raise FerryError(f"local stat failed for {rel}: {exc}") from exc
        result[rel] = {
            "path": rel,
            "exists": True,
            "size": st.st_size,
            "is_regular_file": stat.S_ISREG(st.st_mode),
        }
    return result


def _normalize_include_paths(paths: Iterable[str] | None) -> tuple[str, ...]:
    normalized: list[str] = []
    seen: set[str] = set()
    for path in paths or ():
        rel = normalize_rel_path(path)
        if rel not in seen:
            seen.add(rel)
            normalized.append(rel)
    return tuple(normalized)


def _path_is_included(path: str, include_paths: Iterable[str]) -> bool:
    rel = normalize_rel_path(path)
    return any(rel == root or rel.startswith(root.rstrip("/") + "/") for root in include_paths)


def _classify_transfer_files(
    files: Iterable[str],
    *,
    metadata: dict[str, dict[str, Any]],
    max_file_bytes: int,
    require_sizes: bool,
) -> tuple[list[str], list[dict[str, Any]]]:
    transfer_files: list[str] = []
    excluded: list[dict[str, Any]] = []
    for rel in files:
        info = metadata.get(rel) or {}
        size = info.get("size") if isinstance(info.get("size"), int) else None
        if require_sizes and info.get("exists") is True and info.get("is_regular_file") is True and size is None:
            raise FerryError(f"file size missing from ferry preflight for {rel}; refusing uncapped transfer")
        if info.get("is_regular_file") is True and size is not None and size > max_file_bytes:
            excluded.append(
                {
                    "path": rel,
                    "size": size,
                    "rule": "max-file-bytes",
                    "limit_bytes": max_file_bytes,
                }
            )
            continue
        transfer_files.append(rel)
    return transfer_files, excluded


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
    pytest_basetemp_paths: Iterable[str] | None = None,
    include_paths: Iterable[str] | None = None,
    max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
    sleep_s: float = 1.0,
) -> dict[str, Any]:
    assert_live_ssh_opt_in()
    if max_iterations < 1:
        raise FerryError("max_iterations must be >= 1")
    if not isinstance(max_file_bytes, int) or isinstance(max_file_bytes, bool) or max_file_bytes < 1:
        raise FerryError("max_file_bytes must be a positive integer")
    out_dir = out_dir.expanduser()
    assert_controller_staging_root_allowed(fleet_dir, out_dir)
    out_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    append_only_patterns = _merge_append_only_patterns(append_only_paths)
    basetemp_paths = _normalize_pytest_basetemp_paths(pytest_basetemp_paths)
    included_paths = _normalize_include_paths(include_paths)

    initial_receipt: dict[str, Any] | None = None
    excluded_by_key: dict[tuple[str, str], dict[str, Any]] = {}
    transferred_paths: set[str] = set()
    node_entry = _load_node_entry(fleet_dir, node_id)

    def preflight_candidates(paths: Iterable[str]) -> dict[str, dict[str, Any]]:
        candidates = list(paths)
        if not candidates:
            return {}
        return _run_remote_preflight(
            fleet_dir,
            node_id=node_id,
            node_entry=node_entry,
            remote_root=worktree_path,
            files=candidates,
            runner=runner,
        )

    def snapshot(stdout: str) -> dict[str, Any]:
        entries, allowed_entries, skipped = _salvage_entries_from_porcelain(stdout)
        requested_paths = list(dict.fromkeys(path for _, path in allowed_entries))
        metadata = preflight_candidates(requested_paths)
        status_by_path = {path: status for status, path in allowed_entries}
        transfer_files: list[str] = []
        convergence_files: list[str] = []
        excluded: list[dict[str, Any]] = []

        denied_paths = set(path for _, path in entries) - set(requested_paths)
        for _, path in entries:
            if path in denied_paths and not _matches_append_only(path, append_only_patterns):
                convergence_files.append(path)

        for path, info in metadata.items():
            owners = [
                requested
                for requested in requested_paths
                if path == requested or path.startswith(requested.rstrip("/") + "/")
            ]
            if not owners:
                raise FerryError(f"remote ferry preflight path has no dirty status entry: {path}")
            owner = max(owners, key=len)
            status = status_by_path[owner]
            size = info.get("size") if isinstance(info.get("size"), int) and not isinstance(info.get("size"), bool) else None
            rule = None
            if status == "??" and not _path_is_included(path, included_paths):
                rule = _scratch_exclusion_rule(path, basetemp_paths)
            if rule is not None:
                excluded.append({"path": path, "size": size, "rule": rule})
                continue
            if info.get("exists") is True and info.get("is_regular_file") is True and size is not None and size > max_file_bytes:
                excluded.append(
                    {
                        "path": path,
                        "size": size,
                        "rule": "max-file-bytes",
                        "limit_bytes": max_file_bytes,
                    }
                )
                continue
            transfer_files.append(path)
            if not _matches_append_only(path, append_only_patterns):
                convergence_files.append(path)

        return {
            "requested_paths": requested_paths,
            "metadata": metadata,
            "transfer_files": list(dict.fromkeys(transfer_files)),
            "convergence_files": sorted(set(convergence_files)),
            "excluded_files": excluded,
            "skipped": skipped,
        }

    skipped_by_key: dict[tuple[str, str], dict[str, str]] = {}

    def record_snapshot(current: dict[str, Any]) -> None:
        for path in current["transfer_files"]:
            for key in [key for key in excluded_by_key if key[0] == path]:
                excluded_by_key.pop(key, None)
        for entry in current["excluded_files"]:
            excluded_by_key[(entry["path"], entry["rule"])] = entry
        for item in current["skipped"]:
            skipped_by_key[(item["path"], item["reason"])] = item

    def transfer(
        files: list[str],
        metadata: dict[str, dict[str, Any]],
        *,
        label: str,
        itemize: bool = False,
    ) -> dict[str, Any]:
        if not files:
            return {"ok": True, "files": [], "excluded_files": [], "incomplete": False, "stdout": ""}
        return execute_ferry(
            fleet_dir,
            node_id=node_id,
            direction="pull",
            src_root=worktree_path,
            dst_root=str(out_dir),
            files=files,
            expanded_files=files,
            purpose=f"{purpose}:{label}",
            runner=runner,
            itemize=itemize,
            max_file_bytes=max_file_bytes,
            _preflight_metadata={path: metadata[path] for path in files},
        ).to_dict()

    baseline = snapshot(
        _run_remote_git_status(
            fleet_dir,
            node_id=node_id,
            worktree_path=worktree_path,
            runner=runner,
        )
    )
    target_files = baseline["requested_paths"]
    record_snapshot(baseline)

    if target_files:
        current = snapshot(
            _run_remote_git_status(
                fleet_dir,
                node_id=node_id,
                worktree_path=worktree_path,
                runner=runner,
            )
        )
        record_snapshot(current)
        _assert_salvage_targets_unchanged(
            current_paths=current["convergence_files"],
            previous_paths=baseline["convergence_files"],
            phase="initial rsync",
        )
        initial_candidates = list(dict.fromkeys([*baseline["transfer_files"], *current["transfer_files"]]))
        initial_metadata = dict(baseline["metadata"])
        initial_metadata.update(current["metadata"])
        initial_receipt = transfer(initial_candidates, initial_metadata, label="initial")
        transferred_paths.update(str(path) for path in initial_receipt.get("files") or [])
        for path in initial_receipt.get("files") or []:
            for key in [key for key in excluded_by_key if key[0] == path]:
                excluded_by_key.pop(key, None)
        for entry in initial_receipt.get("excluded_files") or []:
            key = (str(entry.get("path") or ""), str(entry.get("rule") or ""))
            excluded_by_key[key] = entry

    iterations: list[dict[str, Any]] = []
    consecutive_zero = 0
    converged = not target_files
    for idx in range(1, max_iterations + 1):
        if not target_files:
            break
        current = snapshot(
            _run_remote_git_status(
                fleet_dir,
                node_id=node_id,
                worktree_path=worktree_path,
                runner=runner,
            )
        )
        record_snapshot(current)
        _assert_salvage_targets_unchanged(
            current_paths=current["convergence_files"],
            previous_paths=baseline["convergence_files"],
            phase=f"convergence rsync {idx}",
        )
        receipt = transfer(
            current["transfer_files"],
            current["metadata"],
            label=f"convergence-{idx}",
            itemize=True,
        )
        transferred_paths.update(str(path) for path in receipt.get("files") or [])
        for path in receipt.get("files") or []:
            for key in [key for key in excluded_by_key if key[0] == path]:
                excluded_by_key.pop(key, None)
        for entry in receipt.get("excluded_files") or []:
            key = (str(entry.get("path") or ""), str(entry.get("rule") or ""))
            excluded_by_key[key] = entry
        changed = parse_rsync_itemized_paths(str(receipt.get("stdout") or ""))
        checked_changed = [path for path in changed if not _matches_append_only(path, append_only_patterns)]
        received_files = {str(path) for path in receipt.get("files") or []}
        expected_files = set(current["transfer_files"])
        transfer_complete = (
            receipt.get("ok") is True
            and not receipt.get("incomplete")
            and not receipt.get("excluded_files")
            and expected_files.issubset(received_files)
        )
        zero_delta = not checked_changed and transfer_complete
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

    excluded_files = sorted(excluded_by_key.values(), key=lambda entry: (entry["path"], entry["rule"]))
    skipped = sorted(skipped_by_key.values(), key=lambda item: (item["path"], item["reason"]))
    files = [_file_entry(out_dir, rel) for rel in sorted(transferred_paths)]
    incomplete = bool(skipped or excluded_files or not converged)
    lock_identity = _salvage_lock_identity(fleet_dir, dispatch_id)
    manifest: dict[str, Any] = {
        "schema": "goalflight.fleet.salvage.manifest.v2",
        "ts": _utc_iso(),
        "node_id": node_id,
        "worktree_path": worktree_path,
        "salvage_dir": str(out_dir),
        "target_files": target_files,
        "max_file_bytes": max_file_bytes,
        "excluded_files": excluded_files,
        "incomplete": incomplete,
        "skipped": skipped,
        "files": files,
        "iterations": iterations,
        "max_iterations": max_iterations,
        "converged": converged,
        "initial_receipt": initial_receipt,
    }
    manifest.update(lock_identity)
    manifest_path = out_dir / "salvage-manifest.json"
    manifest["manifest_path"] = str(manifest_path)
    if not incomplete and lock_identity.get("account_key") and lock_identity.get("fencing_token"):
        manifest["lock_release_command"] = (
            "goalflight_fleet.py lock-release "
            f"--account-key {lock_identity['account_key']} "
            f"--fencing-token {lock_identity['fencing_token']} "
            f"--reason salvage_complete --manifest {shlex.quote(str(manifest_path))}"
        )
    if not converged:
        manifest["liveness_signal"] = "worktree still changing - worker may be alive"
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
            max_file_bytes=args.max_file_bytes,
        ).to_dict()
    except FerryError as exc:
        print(str(exc), file=__import__("sys").stderr)
        return 2
    print(json.dumps(receipt, indent=2, sort_keys=True))
    return 3 if receipt.get("incomplete") else 0


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
                    "max_file_bytes": args.max_file_bytes,
                    "include_paths": args.include,
                    "schema": "goalflight.fleet.salvage.manifest.v2",
                    "incomplete": False,
                    "excluded_files": [],
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
            pytest_basetemp_paths=args.pytest_basetemp,
            include_paths=args.include,
            max_file_bytes=args.max_file_bytes,
            sleep_s=args.sleep_s,
        )
    except FerryError as exc:
        print(str(exc), file=__import__("sys").stderr)
        return 2
    print(json.dumps(manifest, indent=2, sort_keys=True))
    return 3 if manifest.get("incomplete") else 0


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
