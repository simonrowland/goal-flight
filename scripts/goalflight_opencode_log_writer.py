#!/usr/bin/env python3
"""Copy OpenCode server output through a bounded rotating file writer."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys


def _open_append(path: Path):
    flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    return os.fdopen(os.open(path, flags, 0o600), "ab")


def _rotate_owned(path: Path, *, keep: int) -> None:
    if not path.exists() or path.is_symlink():
        return
    for index in range(keep - 1, 0, -1):
        older = path.with_name(path.name + f".{index - 1}")
        newer = path.with_name(path.name + f".{index}")
        if older.exists() and not older.is_symlink():
            older.replace(newer)
    archive = path.with_name(path.name + ".0")
    path.replace(archive)


def copy_stream(stream, path: Path, *, max_bytes: int, keep: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise OSError(f"refusing symlinked log path: {path}")
    if path.exists() and path.stat().st_size >= max_bytes:
        _rotate_owned(path, keep=keep)
    output = _open_append(path)
    size = path.stat().st_size
    try:
        while True:
            chunk = stream.read(64 * 1024)
            if not chunk:
                break
            if size >= max_bytes:
                output.close()
                _rotate_owned(path, keep=keep)
                output = _open_append(path)
                size = 0
            offset = 0
            while offset < len(chunk):
                available = max_bytes - size
                take = min(available, len(chunk) - offset)
                output.write(chunk[offset:offset + take])
                output.flush()
                offset += take
                size += take
                if size >= max_bytes and offset < len(chunk):
                    output.close()
                    _rotate_owned(path, keep=keep)
                    output = _open_append(path)
                    size = 0
    finally:
        output.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", type=Path)
    parser.add_argument("--max-bytes", type=int, default=16 * 1024 * 1024)
    parser.add_argument("--keep", type=int, default=2)
    args = parser.parse_args(argv)
    if args.max_bytes <= 0 or args.keep <= 0:
        parser.error("--max-bytes and --keep must be positive")
    copy_stream(sys.stdin.buffer, args.path, max_bytes=args.max_bytes, keep=args.keep)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
