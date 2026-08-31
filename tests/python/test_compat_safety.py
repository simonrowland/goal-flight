"""Compatibility safety-boundary regression tests."""

from __future__ import annotations

import errno
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import goalflight_compat  # noqa: E402


def test_path_resolution_failure_is_denied(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    candidate = allowed / "link" / "secret"
    original_resolve = Path.resolve

    def fail_candidate_resolution(self: Path, *args, **kwargs):
        if self == candidate:
            raise OSError(errno.EACCES, "resolution denied")
        return original_resolve(self, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", fail_candidate_resolution)
    assert goalflight_compat.path_is_under(candidate, [allowed]) is False


def test_path_is_under_allows_missing_in_root_path(tmp_path: Path) -> None:
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    assert (
        goalflight_compat.path_is_under(allowed / "not-created-yet", [allowed])
        is True
    )


def test_path_is_under_denies_symlink_escape(tmp_path: Path) -> None:
    allowed = tmp_path / "allowed"
    outside = tmp_path / "outside"
    allowed.mkdir()
    outside.mkdir()
    (allowed / "link").symlink_to(outside, target_is_directory=True)
    assert (
        goalflight_compat.path_is_under(allowed / "link" / "secret", [allowed])
        is False
    )


def test_gstack_stat_failure_keeps_uncertain_candidate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    candidate = tmp_path / "browse"
    candidate.write_text("#!/bin/sh\n", encoding="utf-8")
    candidate.chmod(0o700)
    original_stat = os.stat

    def fail_candidate_stat(path, *args, **kwargs):
        if isinstance(path, (str, os.PathLike)) and Path(path) == candidate:
            raise OSError(errno.ESTALE, "stale browser binary")
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(
        goalflight_compat, "gstack_browse_bin_candidates", lambda: [candidate]
    )
    monkeypatch.setattr(os, "stat", fail_candidate_stat)
    assert goalflight_compat.resolve_gstack_browse_bin() == candidate


def test_gstack_missing_candidate_is_definitely_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    missing = tmp_path / "missing-browse"
    monkeypatch.setattr(
        goalflight_compat, "gstack_browse_bin_candidates", lambda: [missing]
    )
    assert goalflight_compat.resolve_gstack_browse_bin() is None
