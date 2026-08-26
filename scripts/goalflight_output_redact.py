#!/usr/bin/env python3
"""Redact credential-shaped material from captured worker output.

Capture sinks (tails, status JSON, watcher logs, journal/outbox, mail) are
read by other agents and pasted into briefs. A redaction that keys on a prior
lookup succeeding — a resolved seat, a billing flag, a populated env — fails
exactly when things are going wrong. This module keys on the public shape of
the secret itself, so a seatless dispatch is still scrubbed.

Fail-closed degrades the unit, not the surface: a scrub error on one line
replaces that line, and the rest of the capture still lands.

When invoked as a stdin-to-file filter, this process owns the tail flock for
its lifetime so reconciliation cannot pass a COMPLETE still buffered here.
"""

from __future__ import annotations

import argparse
import io
import os
from pathlib import Path
import re
import sys
from typing import Any

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows fallback path
    fcntl = None

# Public key shape (prefix + entropy). Short tokens such as account labels
# do not match. Length bounds follow the vendor's own published detector.
_SECRET_SHAPE_RE = re.compile(r"(?i)xai-[a-z0-9]{20,}")
REDACTED = "[redacted]"
_LINE_FAIL = b"[redacted]"
_CHUNK_SIZE = 4096
READY_FD_ENV = "GOALFLIGHT_REDACT_READY_FD"


def redact_text(text: str) -> str:
    """Replace credential-shaped substrings. Never raises."""
    try:
        return _SECRET_SHAPE_RE.sub(REDACTED, text)
    except Exception:
        return REDACTED


def redact_data(value: Any) -> Any:
    """Walk JSON-shaped data, redacting strings. Never raises."""
    try:
        if isinstance(value, str):
            return redact_text(value)
        if isinstance(value, dict):
            return {key: redact_data(item) for key, item in value.items()}
        if isinstance(value, list):
            return [redact_data(item) for item in value]
        if isinstance(value, tuple):
            return tuple(redact_data(item) for item in value)
        return value
    except Exception:
        if isinstance(value, str):
            return REDACTED
        if isinstance(value, dict):
            out: dict[Any, Any] = {}
            for key, item in value.items():
                try:
                    out[key] = redact_data(item)
                except Exception:
                    out[key] = REDACTED
            return out
        if isinstance(value, list):
            out_list: list[Any] = []
            for item in value:
                try:
                    out_list.append(redact_data(item))
                except Exception:
                    out_list.append(REDACTED)
            return out_list
        return value


def _redact_line_bytes(line: bytes) -> bytes:
    try:
        return redact_text(line.decode("utf-8", errors="replace")).encode("utf-8")
    except Exception:
        return _LINE_FAIL


def _read_chunk(src, size: int) -> bytes:
    """Return available bytes without waiting to fill ``size``.

    ``BufferedReader.read(n)`` blocks until n bytes or EOF, so a live
    worker that prints a short line would not reach the tail until it
    exited — and the flock would drop at the same moment.
    """
    try:
        fileno = src.fileno()
    except (AttributeError, io.UnsupportedOperation, OSError, ValueError):
        fileno = None
    if isinstance(fileno, int) and fileno >= 0:
        while True:
            try:
                return os.read(fileno, size)
            except InterruptedError:
                continue
            except OSError:
                raise
    try:
        return src.read(size) or b""
    except Exception:
        return b""


def filter_stream(src, dst) -> None:
    """Copy src to dst, redacting complete lines. Partial lines flush at EOF.

    Sink write failures stop emitting but keep reading until stdin EOF so
    the tail flock is not dropped while the worker can still write.
    """
    buf = b""
    sink_ok = True
    while True:
        chunk = _read_chunk(src, _CHUNK_SIZE)
        if not chunk:
            break
        buf += chunk
        while True:
            idx = buf.find(b"\n")
            if idx < 0:
                break
            line, buf = buf[:idx], buf[idx + 1 :]
            if not sink_ok:
                continue
            try:
                dst.write(_redact_line_bytes(line) + b"\n")
                dst.flush()
            except Exception:
                sink_ok = False
    if buf and sink_ok:
        try:
            dst.write(_redact_line_bytes(buf))
            dst.flush()
        except Exception:
            pass


def _signal_ready() -> None:
    raw = os.environ.get(READY_FD_ENV, "").strip()
    if not raw:
        return
    fd = int(raw)
    try:
        os.write(fd, b"x")
    finally:
        os.close(fd)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--lock", action="store_true")
    parser.add_argument("--append", action="store_true")
    parser.add_argument("path", nargs="?")
    args = parser.parse_args(argv)
    try:
        if args.path:
            path = Path(args.path)
            path.parent.mkdir(parents=True, exist_ok=True)
            mode = "ab" if args.append else "wb"
            with path.open(mode) as handle:
                if args.lock and fcntl is not None:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                _signal_ready()
                filter_stream(sys.stdin.buffer, handle)
            return 0
        _signal_ready()
        filter_stream(sys.stdin.buffer, sys.stdout.buffer)
        return 0
    except Exception:
        # Do not copy remaining unscrubbed bytes to the sink.
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
