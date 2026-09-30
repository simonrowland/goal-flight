#!/usr/bin/env python3
"""Stdlib-only ferry preflight, shared by the controller and shipped to nodes."""

from __future__ import annotations

import argparse
import base64
import fnmatch
import json
import os
import posixpath
import stat
import sys
from pathlib import Path, PurePosixPath
from typing import Any, Iterable

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


class FerryError(Exception):
    pass


class FerryDenyError(FerryError):
    pass


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


def assert_no_credential_paths(paths: Iterable[str], *, where: str) -> None:
    for path in paths:
        reason = credential_deny_reason(path)
        if reason:
            raise _teaching_deny_error(path, reason, where=where)


def _remote_norm(path: str) -> str:
    raw = str(path or "").strip().replace("\\", "/").rstrip("/")
    if not raw or "\n" in raw or "\r" in raw:
        raise FerryError("remote transfer root must be a non-empty single-line path")
    return posixpath.normpath(raw)


def _remote_under(path: str, root: str) -> bool:
    candidate = _remote_norm(path)
    base = _remote_norm(root)
    return candidate == base or candidate.startswith(base.rstrip("/") + "/")


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
    allowed_roots = [
        _remote_norm(str(path)) for path in payload.get("allowed_roots") or [] if str(path or "").strip()
    ]
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


def _remote_preflight_error(error: Exception) -> dict[str, Any]:
    payload: dict[str, Any] = {"ok": False, "error": str(error)}
    if isinstance(error, FerryDenyError):
        payload["error_type"] = "deny"
    return payload


def _cmd_remote_preflight(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Internal fleet ferry remote preflight")
    parser.add_argument("--payload-b64", required=True)
    args = parser.parse_args(argv)
    try:
        payload = json.loads(base64.b64decode(args.payload_b64.encode("ascii"), validate=True).decode("utf-8"))
        result = _remote_preflight_payload(payload)
    except Exception as exc:  # Remote helper reports failures as data for the controller.
        result = _remote_preflight_error(exc)
        print(json.dumps(result, sort_keys=True))
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


def _main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args[:1] == ["remote-preflight"]:
        return _cmd_remote_preflight(args[1:])
    print("usage: python3 - remote-preflight --payload-b64 <payload>", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(_main())
