#!/usr/bin/env python3
"""Manage the host global goal-flight skill symlink: pin (stable) vs live (dev tree).

The native Claude host resolves the goal-flight skill from a single user-level
location, the Claude Code skills dir (``.claude/skills/goal-flight`` under the
user's home). Claude Code's documented same-name precedence is personal >
project, so a project-level ``.claude/skills`` override is NOT a reliable way to
give one project a different skill version than the rest. The single global
symlink is therefore the one reliable lever, and the robust model is:

  * default the global symlink at the STABLE PIN (a detached ``git worktree`` at
    a tag, default ``~/.goal-flight/skill``) so every project -- sibling
    projects especially -- gets a stable, reviewed goal-flight by default; and
  * flip it to the LIVE dev tree only while actively dogfooding goal-flight
    itself, then flip back.

Paths are remembered in a small JSON config (default
``~/.goal-flight/.skill-link.json``) so ``--pin`` / ``--live`` / ``--restore``
work from anywhere -- including from inside the pin, where the script's own
location cannot tell you where the live tree is.

Safety (this utility never destroys data):
  * builds the replacement symlink under a unique temp name first -- ``os.symlink``
    refuses to clobber, so an unrelated file at the temp path is never deleted;
  * refuses to clobber a real (non-symlink) install unless ``--force``, and even
    then moves it aside to a collision-free backup rather than deleting it;
  * stores ABSOLUTE targets so a link is never broken by CWD-relative resolution;
  * verifies a target looks like a skill (has ``SKILL.md``) before pointing at it;
  * swaps the symlink atomically (temp symlink + ``os.replace``);
  * preserves a malformed config (moved to a ``.corrupt-*`` sidecar) before
    writing a fresh one, and records the previous target for ``--restore``.

This utility only manages a symlink; it never edits skill content.
"""
from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timezone
from pathlib import Path

# Host-neutrality guard (test_instruction_split_contract) flags the tilde-form
# literal of the Claude skills path in portable files; build it from Path.home()
# instead, matching the convention in goalflight_doctor.py.
DEFAULT_LINK = Path.home() / ".claude" / "skills" / "goal-flight"
DEFAULT_PIN = Path.home() / ".goal-flight" / "skill"
DEFAULT_CONFIG = Path.home() / ".goal-flight" / ".skill-link.json"


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _abs(p: Path) -> Path:
    """Expand ``~`` and resolve to an absolute (lexical) path; do NOT follow the
    final symlink so a link's own location is preserved."""
    return Path(os.path.abspath(os.path.expanduser(str(p))))


def load_config(path: Path) -> tuple[dict, bool]:
    """Return (config, malformed). malformed=True iff the file exists but is not
    parseable as a JSON object -- the caller preserves it before overwriting."""
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}, False
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {}, True
    if not isinstance(data, dict):
        return {}, True
    return data, False


def save_config(path: Path, cfg: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(cfg, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def looks_like_skill(target: Path) -> bool:
    return (target / "SKILL.md").is_file()


def current_target(link: Path) -> str | None:
    """Return the raw symlink target (one level), or None if link is not a symlink."""
    if link.is_symlink():
        return os.readlink(link)
    return None


def _resolve(p: Path) -> Path:
    try:
        return p.resolve()
    except OSError:
        return p


def classify(link: Path, pin: Path, live: Path | None) -> str:
    tgt = current_target(link)
    if tgt is None:
        if link.exists():
            return "REALPATH"  # a real dir/file is installed, not a symlink
        return "MISSING"
    # Stored targets are absolute, but tolerate a legacy relative link by
    # resolving it against the link's own directory (not the CWD).
    tgt_path = Path(tgt)
    if not tgt_path.is_absolute():
        tgt_path = link.parent / tgt_path
    tgt_real = _resolve(tgt_path.expanduser())
    if tgt_real == _resolve(pin):
        return "PIN"
    if live is not None and tgt_real == _resolve(live):
        return "LIVE"
    return "OTHER"


def _free_path(base: Path) -> Path:
    """Return base, then base.1, base.2, ... -- the first that does not exist
    (as a real path or a symlink), so a move never overwrites a prior backup."""
    i = 0
    while True:
        cand = base if i == 0 else base.with_name(f"{base.name}.{i}")
        if not cand.exists() and not cand.is_symlink():
            return cand
        i += 1


def _make_symlink_unique(link: Path, target: Path) -> Path:
    """Create a symlink to ``target`` under a unique temp name beside ``link``.

    Uses ``os.symlink`` which raises ``FileExistsError`` rather than clobbering,
    so an unrelated file already at a candidate temp path is never destroyed."""
    for i in range(1000):
        cand = link.with_name(f"{link.name}.newlink.{os.getpid()}.{i}")
        try:
            os.symlink(str(target), str(cand))
            return cand
        except FileExistsError:
            continue
    raise SystemExit("could not allocate a temp symlink path beside the link")


def atomic_symlink(link: Path, target: Path, *, force: bool) -> list[str]:
    """Point ``link`` at ``target`` atomically, never deleting real data."""
    notes: list[str] = []
    link.parent.mkdir(parents=True, exist_ok=True)
    # 1. Build the replacement symlink under a unique name (no clobber possible).
    tmp = _make_symlink_unique(link, target)
    try:
        # 2. A real (non-symlink) path in the way must be moved aside (force
        #    only) -- never deleted. A dangling symlink (exists()==False,
        #    is_symlink()==True) is safe to replace directly.
        if link.exists() and not link.is_symlink():
            if not force:
                raise SystemExit(
                    f"refuse: {link} is a real path, not a symlink. "
                    f"Re-run with --force to move it aside before linking."
                )
            moved = _free_path(link.with_name(link.name + ".bak-" + _now().replace(":", "")))
            os.replace(link, moved)
            notes.append(f"moved real path aside: {link} -> {moved}")
        # 3. Atomically put the new symlink in place (replaces an existing
        #    symlink, or fills the freed/empty slot).
        os.replace(tmp, link)
    except BaseException:
        # Clean up our own temp symlink on any failure -- it is ours, safe to drop.
        if tmp.is_symlink():
            tmp.unlink()
        raise
    notes.append(f"linked: {link} -> {target}")
    return notes


def _pick(flag: Path | None, cfg: dict, key: str, default: Path | None) -> Path | None:
    if flag is not None:
        return flag
    if cfg.get(key):
        return Path(cfg[key]).expanduser()
    return default


def build_report(link: Path, pin: Path, live: Path | None, cfg: dict, notes: list[str]) -> dict:
    return {
        "link": str(link),
        "target": current_target(link),
        "state": classify(link, pin, live),
        "pin": str(pin),
        "live": str(live) if live else None,
        "previous": cfg.get("previous"),
        "notes": notes,
    }


def print_report(report: dict, as_json: bool) -> None:
    if as_json:
        print(json.dumps(report, indent=2, sort_keys=True))
        return
    print(f"link:     {report['link']}")
    print(f"target:   {report['target']}")
    print(f"state:    {report['state']}")
    print(f"pin:      {report['pin']}")
    print(f"live:     {report['live']}")
    if report["previous"]:
        print(f"previous: {report['previous']}")
    for note in report["notes"]:
        print(f"  - {note}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Pin/flip the global goal-flight skill symlink (stable pin vs live dev tree)."
    )
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--status", action="store_true",
                      help="show current link target + classification (default)")
    mode.add_argument("--pin", action="store_true",
                      help="point the link at the stable pin")
    mode.add_argument("--live", action="store_true",
                      help="point the link at the live dev tree (for dogfooding)")
    mode.add_argument("--restore", action="store_true",
                      help="point the link back at the previous target")
    ap.add_argument("--link", type=lambda s: Path(s).expanduser(), default=None,
                    help=f"the skill symlink to manage (default {DEFAULT_LINK})")
    ap.add_argument("--pin-path", type=lambda s: Path(s).expanduser(), default=None,
                    help=f"the stable pin worktree (default {DEFAULT_PIN})")
    ap.add_argument("--live-path", type=lambda s: Path(s).expanduser(), default=None,
                    help="the live dev tree (recorded in config on first use)")
    ap.add_argument("--config", type=lambda s: Path(s).expanduser(), default=DEFAULT_CONFIG,
                    help=f"path state file (default {DEFAULT_CONFIG})")
    ap.add_argument("--dry-run", action="store_true", help="show the change without making it")
    ap.add_argument("--force", action="store_true",
                    help="move a real (non-symlink) install aside instead of refusing")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    args = ap.parse_args(argv)

    cfg, cfg_malformed = load_config(args.config)
    # Normalize every path to absolute so stored symlink targets never depend on
    # the directory the command happened to run from (P1: relative-target trap).
    link = _abs(_pick(args.link, cfg, "link", DEFAULT_LINK))
    pin = _abs(_pick(args.pin_path, cfg, "pin", DEFAULT_PIN))
    live_raw = _pick(args.live_path, cfg, "live", None)
    live = _abs(live_raw) if live_raw is not None else None

    action = "status"
    if args.pin:
        action = "pin"
    elif args.live:
        action = "live"
    elif args.restore:
        action = "restore"

    notes: list[str] = []
    if action != "status":
        if action == "pin":
            target: Path | None = pin
        elif action == "live":
            if live is None:
                ap.error("--live needs a live path: pass --live-path once (it is then "
                         "remembered in the config) or set it via an earlier --pin --live-path.")
            target = live
        else:  # restore
            prev = cfg.get("previous")
            if not prev:
                ap.error("--restore: no previous target recorded in config.")
            target = _abs(Path(prev))
        assert target is not None
        if not looks_like_skill(target):
            raise SystemExit(f"refuse: {target} does not look like a skill (no SKILL.md).")
        prev_target = current_target(link)
        if args.dry_run:
            notes.append(f"[dry-run] would link {link} -> {target} (was {prev_target})")
        else:
            # Preserve a malformed config before we overwrite it, so a corrupt
            # file never silently loses 'previous'/'live' (P1: --restore trap).
            if cfg_malformed and args.config.exists():
                corrupt = _free_path(
                    args.config.with_name(args.config.name + ".corrupt-" + _now().replace(":", ""))
                )
                os.replace(args.config, corrupt)
                notes.append(f"preserved malformed config: {args.config} -> {corrupt}")
            notes.extend(atomic_symlink(link, target, force=args.force))
            cfg["link"] = str(link)
            cfg["pin"] = str(pin)
            if live is not None:
                cfg["live"] = str(live)
            if prev_target and prev_target != str(target):
                cfg["previous"] = prev_target
            cfg["current"] = str(target)
            cfg["updated_at"] = _now()
            save_config(args.config, cfg)

    print_report(build_report(link, pin, live, cfg, notes), args.json)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
