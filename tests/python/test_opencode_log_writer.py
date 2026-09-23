"""OpenCode server log rotation tests."""

from __future__ import annotations

import io
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
import sys

sys.path.insert(0, str(ROOT / "scripts"))

import goalflight_opencode_log_writer as log_writer  # noqa: E402


def test_long_lived_writer_rotates_at_write_time(tmp_path: Path) -> None:
    path = tmp_path / "opencode-serve.log"
    log_writer.copy_stream(io.BytesIO(b"a" * 17 + b"b" * 17 + b"c" * 17), path, max_bytes=16, keep=2)
    assert sorted(p.name for p in tmp_path.iterdir() if p.name.startswith("opencode-serve.log")) == ["opencode-serve.log", "opencode-serve.log.0", "opencode-serve.log.1"]
    assert path.read_bytes() == b"c" * 3
    assert path.with_name(path.name + ".0").read_bytes() == b"b" * 2 + b"c" * 14
    assert path.with_name(path.name + ".1").read_bytes() == b"a" + b"b" * 15


def test_writer_refuses_symlinked_log_path(tmp_path: Path) -> None:
    outside = tmp_path / "outside.log"
    outside.write_bytes(b"keep")
    path = tmp_path / "opencode-serve.log"
    path.symlink_to(outside)
    with pytest.raises(OSError, match="symlinked log path"):
        log_writer.copy_stream(io.BytesIO(b"overwrite"), path, max_bytes=16, keep=2)
    assert outside.read_bytes() == b"keep"
