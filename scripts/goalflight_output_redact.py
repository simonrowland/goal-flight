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
Partial lines flush on a size or time bound as well as on newline/EOF, so a
newline-free worker stream cannot hold the tail silent or grow without bound.
"""

from __future__ import annotations

import argparse
import io
import os
from pathlib import Path
import re
import select
import sys
import time
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
# Partial-line flush so a newline-free stream cannot hold the tail silent or
# grow the pending buffer without bound (a size-unbounded ``buf += chunk``
# copies quadratically). Hold back a possible secret prefix so a credential
# split across the flush is still redacted.
PARTIAL_FLUSH_BYTES = 1024
PARTIAL_FLUSH_SECS = 1.0
_SECRET_PREFIX_MAX = 4 + 19  # "xai-" plus one short of the entropy bound
_SECRET_PREFIX_RE = re.compile(br"(?i)(?:x|xa|xai|xai-[a-z0-9]{0,19})\Z")
READY_FD_ENV = "GOALFLIGHT_REDACT_READY_FD"

# Archive-time redaction is deliberately aggressive. A false positive in an
# archived transcript costs nothing; a leaked token costs a lot. Named markers
# tell a reader what class of material was removed and that the file is not
# verbatim. Live capture (`redact_text`) stays on the vendor key shape so a
# worker tail is not painted over with archive markers.
_ARCHIVE_REDACT_WHY = (
    "credential-shaped material; archived tails are unreviewed worker output"
)


def archive_redaction_marker(kind: str) -> str:
    return f"[redacted {kind}: {_ARCHIVE_REDACT_WHY}]"


_ARCHIVE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("bearer token", re.compile(r"(?i)(?:authorization:\s*)?bearer\s+\S+")),
    ("authorization header", re.compile(r"(?i)authorization:\s*\S+")),
    ("jwt", re.compile(r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}")),
    ("xai api key", re.compile(r"(?i)xai-[a-z0-9]{20,}")),
    ("openai-style key", re.compile(r"(?i)sk-[a-z0-9_-]{20,}")),
    (
        "github token",
        re.compile(r"(?i)(?:ghp_|gho_|ghu_|ghs_|ghr_|github_pat_)[a-z0-9_]{20,}"),
    ),
    (
        "auth.json field",
        re.compile(
            r'(?i)("(?:api[_-]?key|access_token|refresh_token|client_secret|'
            r'password|secret|token)"\s*:\s*")([^"]+)(")'
        ),
    ),
    (
        "private key block",
        re.compile(
            r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
            re.DOTALL,
        ),
    ),
    (
        "long base64",
        re.compile(r"(?<![A-Za-z0-9+/=])[A-Za-z0-9+/]{80,}={0,2}(?![A-Za-z0-9+/=])"),
    ),
)


def redact_archive_text(text: str) -> tuple[str, int, list[str]]:
    """Replace credential-shaped archive text with named markers.

    Returns ``(redacted_text, substitution_count, kinds_applied)``. Never
    raises. Does not try to be clever: short labels such as ``xai-0`` are
    left alone because they miss the entropy bound; git SHAs and ordinary
    prose are not matched.
    """
    try:
        count = 0
        kinds: list[str] = []
        for kind, pattern in _ARCHIVE_PATTERNS:
            marker = archive_redaction_marker(kind)
            if kind == "auth.json field":

                def _keep_key(match: re.Match[str], *, _marker: str = marker) -> str:
                    return match.group(1) + _marker + match.group(3)

                text, n = pattern.subn(_keep_key, text)
            else:
                text, n = pattern.subn(marker, text)
            if n:
                count += n
                kinds.append(kind)
        return text, count, kinds
    except Exception:
        return archive_redaction_marker("unclassified"), 1, ["unclassified"]


def redact_archive_bytes(data: bytes) -> tuple[bytes, int, list[str]]:
    """Byte wrapper around ``redact_archive_text``. Never raises."""
    try:
        text = data.decode("utf-8", errors="replace")
        redacted, count, kinds = redact_archive_text(text)
        return redacted.encode("utf-8"), count, kinds
    except Exception:
        marker = archive_redaction_marker("unclassified").encode("ascii")
        return marker, 1, ["unclassified"]


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


def _src_fileno(src) -> int | None:
    try:
        fileno = src.fileno()
    except (AttributeError, io.UnsupportedOperation, OSError, ValueError):
        return None
    if isinstance(fileno, int) and fileno >= 0:
        return fileno
    return None


def _read_chunk(src, size: int) -> bytes:
    """Return available bytes without waiting to fill ``size``.

    ``BufferedReader.read(n)`` blocks until n bytes or EOF, so a live
    worker that prints a short line would not reach the tail until it
    exited — and the flock would drop at the same moment.
    """
    fileno = _src_fileno(src)
    if fileno is not None:
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


def _wait_readable(fileno: int | None, timeout: float | None) -> bool:
    """True if ``src`` should be read. False is a wait timeout with no data."""
    if fileno is None:
        return True
    try:
        ready, _, _ = select.select([fileno], [], [], timeout)
        return bool(ready)
    except (OSError, TypeError, ValueError):
        return True


def secret_holdback_len(buf: bytes) -> int:
    """Trailing bytes that could still grow into a credential-shaped token."""
    if not buf:
        return 0
    limit = min(len(buf), _SECRET_PREFIX_MAX)
    for n in range(limit, 0, -1):
        if _SECRET_PREFIX_RE.fullmatch(buf[-n:]):
            return n
    return 0


def split_partial_flush(buf: bytes) -> tuple[bytes, bytes]:
    """Redacted prefix that is safe to emit, plus the unemitted suffix.

    Complete secrets in the emit region are redacted. A possible secret
    prefix stays in the suffix so a later chunk can still match.
    """
    hold = secret_holdback_len(buf)
    if hold >= len(buf):
        return b"", buf
    emit_raw = buf[:-hold] if hold else buf
    keep = buf[-hold:] if hold else b""
    return _redact_line_bytes(emit_raw), keep


def filter_stream(
    src,
    dst,
    *,
    flush_bytes: int = PARTIAL_FLUSH_BYTES,
    flush_secs: float = PARTIAL_FLUSH_SECS,
    clock=None,
) -> None:
    """Copy src to dst, redacting complete lines and bounded partial lines.

    Partial lines also flush when they exceed ``flush_bytes`` or have been
    buffered for ``flush_secs``, so a newline-free stream cannot hold the
    tail silent or accumulate without bound. A possible secret prefix is
    held back across those flushes. Remaining bytes flush at EOF.

    Sink write failures stop emitting but keep reading until stdin EOF so
    the tail flock is not dropped while the worker can still write.
    """
    buf = b""
    sink_ok = True
    now = clock or time.monotonic
    buf_since: float | None = None
    fileno = _src_fileno(src)

    def emit(data: bytes) -> None:
        nonlocal sink_ok
        if not data or not sink_ok:
            return
        try:
            dst.write(data)
            dst.flush()
        except Exception:
            sink_ok = False

    def note_buf() -> None:
        nonlocal buf_since
        if not buf:
            buf_since = None
        elif buf_since is None:
            buf_since = now()

    def flush_partial() -> None:
        nonlocal buf, buf_since
        if not buf or not sink_ok:
            return
        out, buf = split_partial_flush(buf)
        emit(out)
        buf_since = now() if buf else None

    while True:
        timeout: float | None = None
        flushable = (
            sink_ok
            and bool(buf)
            and secret_holdback_len(buf) < len(buf)
        )
        if flushable and flush_secs > 0 and buf_since is not None:
            remaining = flush_secs - (now() - buf_since)
            timeout = remaining if remaining > 0 else 0.0
        if not _wait_readable(fileno, timeout):
            flush_partial()
            continue
        chunk = _read_chunk(src, _CHUNK_SIZE)
        if not chunk:
            break
        buf += chunk
        note_buf()
        while True:
            idx = buf.find(b"\n")
            if idx < 0:
                break
            line, buf = buf[:idx], buf[idx + 1 :]
            emit(_redact_line_bytes(line) + b"\n")
            buf_since = None
            note_buf()
        if flush_bytes > 0 and len(buf) >= flush_bytes:
            flush_partial()
    if buf and sink_ok:
        emit(_redact_line_bytes(buf))


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
