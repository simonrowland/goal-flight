#!/usr/bin/env python3
"""Hermetic tests for goalflight_skill_link.py (host skill-symlink pin/live toggle).

All cases run against temp dirs -- they never touch the real ~/.claude or
~/.goal-flight. Symlink management is POSIX-only, so skip on native Windows.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile

from support import skip_posix_on_native_windows

skip_posix_on_native_windows("skill-symlink management is POSIX-only in this suite")

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import goalflight_skill_link as sl  # noqa: E402


def _skill_dir(base: Path, name: str) -> Path:
    d = base / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(f"# {name} skill\n", encoding="utf-8")
    return d


def _common(td: str):
    base = Path(td)
    pin = _skill_dir(base, "pin")
    live = _skill_dir(base, "live")
    link = base / "link"
    cfg = base / "cfg.json"
    return base, pin, live, link, cfg


def case_pin_live_restore() -> None:
    with tempfile.TemporaryDirectory() as td:
        _, pin, live, link, cfg = _common(td)
        # Start with link -> live, mimicking the current dev symlink.
        os.symlink(str(live), str(link))

        # First pin: record all three paths in config; previous becomes live.
        sl.main(["--pin", "--link", str(link), "--pin-path", str(pin),
                 "--live-path", str(live), "--config", str(cfg), "--json"])
        assert sl.classify(link, pin, live) == "PIN"
        saved = json.loads(cfg.read_text(encoding="utf-8"))
        assert saved["pin"] == str(pin) and saved["live"] == str(live), saved
        assert saved["previous"] == str(live), saved
        assert saved["current"] == str(pin), saved

        # Flip to live using only config-remembered paths (no path flags).
        sl.main(["--live", "--config", str(cfg)])
        assert sl.classify(link, pin, live) == "LIVE"
        assert json.loads(cfg.read_text(encoding="utf-8"))["previous"] == str(pin)

        # Restore goes back to the previous target (the pin).
        sl.main(["--restore", "--config", str(cfg)])
        assert sl.classify(link, pin, live) == "PIN"


def case_status_is_noop() -> None:
    with tempfile.TemporaryDirectory() as td:
        _, pin, live, link, cfg = _common(td)
        os.symlink(str(pin), str(link))
        before = os.readlink(link)
        sl.main(["--status", "--link", str(link), "--pin-path", str(pin), "--config", str(cfg)])
        assert os.readlink(link) == before
        # No mode flag also defaults to status (no change).
        sl.main(["--link", str(link), "--pin-path", str(pin), "--config", str(cfg)])
        assert os.readlink(link) == before


def case_refuse_realpath_without_force() -> None:
    with tempfile.TemporaryDirectory() as td:
        _, pin, live, link, cfg = _common(td)
        link.mkdir()  # a REAL directory sits where the symlink should be
        (link / "keep.txt").write_text("do not delete me\n", encoding="utf-8")
        try:
            sl.main(["--pin", "--link", str(link), "--pin-path", str(pin), "--config", str(cfg)])
            raise AssertionError("expected refusal on real-path link without --force")
        except SystemExit:
            pass
        # Real dir + its content untouched.
        assert link.is_dir() and not link.is_symlink()
        assert (link / "keep.txt").is_file()

        # With --force it is moved aside (never deleted) and the symlink lands.
        sl.main(["--pin", "--force", "--link", str(link), "--pin-path", str(pin),
                 "--config", str(cfg)])
        assert link.is_symlink() and sl.classify(link, pin, None) == "PIN"
        backups = list(Path(td).glob("link.bak-*"))
        assert backups and (backups[0] / "keep.txt").is_file(), backups


def case_refuse_target_without_skill() -> None:
    with tempfile.TemporaryDirectory() as td:
        base, pin, live, link, cfg = _common(td)
        empty = base / "empty"  # exists but no SKILL.md
        empty.mkdir()
        os.symlink(str(live), str(link))
        try:
            sl.main(["--pin", "--link", str(link), "--pin-path", str(empty), "--config", str(cfg)])
            raise AssertionError("expected refusal when target has no SKILL.md")
        except SystemExit:
            pass
        # Link unchanged (still pointing at live).
        assert sl.classify(link, pin, live) == "LIVE"


def case_dry_run_and_dangling() -> None:
    with tempfile.TemporaryDirectory() as td:
        _, pin, live, link, cfg = _common(td)
        os.symlink(str(live), str(link))
        sl.main(["--pin", "--dry-run", "--link", str(link), "--pin-path", str(pin),
                 "--live-path", str(live), "--config", str(cfg)])
        # Dry run changes nothing and writes no config.
        assert sl.classify(link, pin, live) == "LIVE"
        assert not cfg.exists()

        # A dangling symlink is safe to replace directly (no --force needed).
        link.unlink()
        os.symlink(str(Path(td) / "ghost"), str(link))
        assert link.is_symlink() and not link.exists()
        sl.main(["--pin", "--link", str(link), "--pin-path", str(pin), "--config", str(cfg)])
        assert sl.classify(link, pin, None) == "PIN"


def case_free_path_no_collision() -> None:
    with tempfile.TemporaryDirectory() as td:
        base = Path(td) / "b.bak"
        assert sl._free_path(base) == base
        base.write_text("x", encoding="utf-8")
        assert sl._free_path(base) == base.with_name("b.bak.1")
        base.with_name("b.bak.1").write_text("y", encoding="utf-8")
        assert sl._free_path(base) == base.with_name("b.bak.2")


def case_temp_path_collision_preserves_real_file() -> None:
    # P0: a real file occupying a candidate temp name must NOT be deleted.
    with tempfile.TemporaryDirectory() as td:
        _, pin, live, link, cfg = _common(td)
        os.symlink(str(live), str(link))
        squatter = link.with_name(f"{link.name}.newlink.{os.getpid()}.0")
        squatter.write_text("precious\n", encoding="utf-8")
        sl.main(["--pin", "--link", str(link), "--pin-path", str(pin),
                 "--live-path", str(live), "--config", str(cfg)])
        assert sl.classify(link, pin, live) == "PIN"
        assert squatter.is_file() and squatter.read_text() == "precious\n"


def case_backup_collision_preserved() -> None:
    # P0: two --force moves that collide on the timestamped backup name must
    # both be preserved (no overwrite). Pin _now() to force identical stamps.
    orig_now = sl._now
    sl._now = lambda: "2026-01-01T000000Z"
    try:
        with tempfile.TemporaryDirectory() as td:
            _, pin, live, link, cfg = _common(td)
            link.mkdir()
            (link / "a.txt").write_text("first\n", encoding="utf-8")
            sl.main(["--pin", "--force", "--link", str(link), "--pin-path", str(pin),
                     "--config", str(cfg)])
            link.unlink()
            link.mkdir()
            (link / "b.txt").write_text("second\n", encoding="utf-8")
            sl.main(["--pin", "--force", "--link", str(link), "--pin-path", str(pin),
                     "--config", str(cfg)])
            backups = sorted(Path(td).glob("link.bak-*"))
            assert len(backups) == 2, backups
            files = {x.name for b in backups for x in b.iterdir()}
            assert files == {"a.txt", "b.txt"}, backups
    finally:
        sl._now = orig_now


def case_relative_target_stored_absolute() -> None:
    # P1: a relative --pin-path must yield an ABSOLUTE stored link target, so the
    # link stays valid regardless of the directory the command ran from.
    with tempfile.TemporaryDirectory() as td:
        _, pin, live, link, cfg = _common(td)
        os.symlink(str(live), str(link))
        cwd = os.getcwd()
        os.chdir(td)
        try:
            sl.main(["--pin", "--link", str(link), "--pin-path", "pin",
                     "--live-path", "live", "--config", str(cfg)])
        finally:
            os.chdir(cwd)
        stored = os.readlink(link)
        assert os.path.isabs(stored), stored
        assert Path(stored).resolve() == pin.resolve()
        assert sl.classify(link, pin, live) == "PIN"


def case_malformed_config_preserved() -> None:
    # P1: a corrupt config must be preserved (not silently lost) when a mutating
    # action rewrites it.
    with tempfile.TemporaryDirectory() as td:
        _, pin, live, link, cfg = _common(td)
        os.symlink(str(live), str(link))
        cfg.write_text("{ this is not json", encoding="utf-8")
        sl.main(["--pin", "--link", str(link), "--pin-path", str(pin),
                 "--live-path", str(live), "--config", str(cfg)])
        fresh = json.loads(cfg.read_text(encoding="utf-8"))
        assert fresh["current"] == str(pin), fresh
        corrupt = list(Path(td).glob("cfg.json.corrupt-*"))
        assert corrupt and corrupt[0].read_text().startswith("{ this is not json"), corrupt
        assert sl.classify(link, pin, live) == "PIN"


def main() -> None:
    case_pin_live_restore()
    case_status_is_noop()
    case_refuse_realpath_without_force()
    case_refuse_target_without_skill()
    case_dry_run_and_dangling()
    case_free_path_no_collision()
    case_temp_path_collision_preserves_real_file()
    case_backup_collision_preserved()
    case_relative_target_stored_absolute()
    case_malformed_config_preserved()
    print("OK: skill-link pin/live toggle tests pass")


if __name__ == "__main__":
    main()
