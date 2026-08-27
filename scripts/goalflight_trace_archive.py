#!/usr/bin/env python3
"""Archive selected finished-dispatch traces out of volatile dispatch state.

Why this exists
---------------

Worker transcripts already live *outside* the worktree: the dispatcher writes
``<dispatch-dir>/<id>.tail`` (and siblings) under machine state, typically
``/tmp/goal-flight-<uid>/dispatch/``. The worktree holds none of that, so
rebuilding a tree does not destroy the trace. ``/tmp`` *does*: macOS purges it,
and the measured backlog was ~7.1 GB across ~1955 tails.

This tool copies a *subset* of those files into the project's gitignored
``docs-private/traces/<YYYY-MM-DD>/<dispatch-id>/`` so a later operator can
grep them. It never ``git add``s — there is no git-add path, and
``refuse_git_add`` raises if one is attempted. Tail text is untrusted
worker output, unreviewed, and possibly sensitive (file contents, paths,
error text, credential-shaped material). Credential-shaped spans are
redacted at copy time; the archived file is not verbatim. Committing an
archive is an operator decision, not part of this command.

What is KEPT
------------

A run is archived only when it produced a worker marker
(``COMPLETE`` / ``RESULT`` / ``FAILED`` / ``BLOCKED`` / ``READY`` /
``USER-NEED`` / ``USER-CONFIRM``) or names a findings/review path. Then:

- the tail, capped (first 64 KiB + last 192 KiB; dropped middle recorded),
  with credential-shaped material replaced by a named marker
- ``status.json`` if it is at most 256 KiB
- a ``MANIFEST.json`` naming every kept, capped, dropped, and redacted input

What is DROPPED
---------------

A silent drop reads as "we kept everything". This command does **not**.
It drops:

- unmarked runs: no worker marker and no findings path (capacity-blocked,
  never spawned, empty tails)
- steer mailboxes (operator text, possibly sensitive)
- watcher logs
- caffeinate logs
- pidfiles
- prompt copies
- middle bytes of an oversized tail (64 KiB head + 192 KiB tail; count in
  the manifest and a marker in ``tail.log``)
- the historical ~7.1 GB ``/tmp`` backlog, unless an operator passes
  ``--source-dir --apply`` (CLI without ``--source-dir`` exits 64 and says so)

Nothing is force-added to git.

Going-forward: ``goalflight_ledger.cmd_finish`` calls
``archive_finished_dispatch``. Failure there is swallowed; a dispatch must
not fail because the archive disk is full.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
from pathlib import Path
import re
import sys
from typing import Any

_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

import goalflight_output_redact  # noqa: E402


SCHEMA = "goalflight.trace-archive.v1"
DROP_LIST = (
    "unmarked / capacity-blocked / never-spawned / empty tails",
    "steer mailbox",
    "watcher log",
    "caffeinate log",
    "pidfile",
    "prompt copy",
    "tail middle bytes",
    "historical /tmp backlog (unless --source-dir --apply)",
)
HEAD_BYTES = 64 * 1024
TAIL_BYTES = 192 * 1024
STATUS_MAX_BYTES = 256 * 1024
TRACES_DIRNAME = "docs-private/traces"

# Worker markers, including the `!` prefix used by newer identity contracts.
_MARKER_RE = re.compile(
    rb"(?m)^(?:!)?(?:COMPLETE|RESULT|FAILED|BLOCKED|READY|USER-NEED|USER-CONFIRM|STEER-ACK)\b"
)
_FINDINGS_RE = re.compile(
    rb"findings\.md|docs-private/reviews/",
    re.IGNORECASE,
)

_SKIP_STATES = frozenset(
    {
        "queued",
        "submitted",
        "waiting_capacity",
        "blocked_capacity",
        "claimed",
    }
)


def refuse_git_add(path: Path | str | None = None) -> None:
    """Hard refuse. Archived tails are unreviewed worker output."""
    detail = f" {path}" if path is not None else ""
    raise RuntimeError(
        "archived tails are unreviewed worker output; this tool never git-adds"
        + detail
    )


def git_add_is_forbidden(argv: list[str] | tuple[str, ...]) -> bool:
    """True when ``argv`` is a ``git add`` (including ``git -C … add``)."""
    words = [str(part) for part in argv]
    if not words:
        return False
    head = Path(words[0]).name
    if head != "git":
        return False
    i = 1
    while i < len(words):
        token = words[i]
        if token in {"-C", "--git-dir", "--work-tree"}:
            i += 2
            continue
        if token.startswith("-"):
            i += 1
            continue
        return token == "add"
    return False


def _archive_root(project_root: Path) -> Path:
    return project_root.resolve() / "docs-private" / "traces"


def _record_day(record: dict[str, Any]) -> str:
    for key in ("ended_at", "finished_at", "updated_at", "started_at"):
        raw = record.get(key)
        if isinstance(raw, str) and len(raw) >= 10 and raw[4] == "-":
            return raw[:10]
        if isinstance(raw, (int, float)) and raw > 0:
            return dt.datetime.fromtimestamp(raw, dt.timezone.utc).strftime("%Y-%m-%d")
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")


def _read_prefix_suffix(path: Path, *, head: int, tail: int) -> tuple[bytes, int, int]:
    """Return (payload, total_bytes, dropped_bytes)."""
    size = path.stat().st_size
    if size <= head + tail:
        data = path.read_bytes()
        return data, size, 0
    with path.open("rb") as fh:
        prefix = fh.read(head)
        fh.seek(max(0, size - tail))
        suffix = fh.read(tail)
    dropped = size - len(prefix) - len(suffix)
    marker = (
        b"\n\n[goalflight-trace-archive: dropped "
        + str(dropped).encode("ascii")
        + b" bytes from the middle of this tail]\n\n"
    )
    return prefix + marker + suffix, size, dropped


def _tail_is_worth_keeping(data: bytes) -> bool:
    return bool(_MARKER_RE.search(data) or _FINDINGS_RE.search(data))


def decide_archive(record: dict[str, Any], *, tail_path: Path | None) -> dict[str, Any]:
    """Return a decision dict: keep/skip plus reasons. Never raises on IO."""
    dispatch_id = str(record.get("dispatch_id") or "")
    state = str(record.get("state") or record.get("terminal_state") or "")
    worker_pid = record.get("worker_pid")
    spawned = isinstance(worker_pid, int) and not isinstance(worker_pid, bool) and worker_pid > 0

    if not dispatch_id:
        return {"keep": False, "reason": "missing dispatch_id"}
    if tail_path is None:
        return {"keep": False, "reason": "no tail path on the record"}
    try:
        if tail_path.is_symlink():
            return {"keep": False, "reason": "tail path is a symlink; refusing"}
        st = tail_path.stat()
    except FileNotFoundError:
        return {"keep": False, "reason": "tail missing"}
    except OSError as exc:
        return {"keep": False, "reason": f"tail unreadable ({exc.__class__.__name__})"}
    if st.st_size <= 0:
        return {"keep": False, "reason": "empty tail"}

    try:
        sample, total, dropped = _read_prefix_suffix(
            tail_path, head=HEAD_BYTES, tail=TAIL_BYTES
        )
    except OSError as exc:
        return {"keep": False, "reason": f"tail read failed ({exc.__class__.__name__})"}

    worth = _tail_is_worth_keeping(sample)
    if not worth:
        if state in _SKIP_STATES or not spawned:
            return {
                "keep": False,
                "reason": (
                    "no worker marker and no findings path "
                    f"(state={state or '<none>'} spawned={spawned})"
                ),
                "tail_bytes": total,
            }
        return {
            "keep": False,
            "reason": "tail has no worker marker and no findings path",
            "tail_bytes": total,
        }
    return {
        "keep": True,
        "reason": "worker marker or findings path present",
        "tail_bytes": total,
        "dropped_bytes": dropped,
        "archived_bytes": len(sample),
        "payload": sample,
    }


def _dest_dir(project_root: Path, record: dict[str, Any]) -> Path:
    dispatch_id = str(record.get("dispatch_id"))
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", dispatch_id).strip("._-") or "invalid-dispatch"
    return _archive_root(project_root) / _record_day(record) / safe


def archive_finished_dispatch(
    record: dict[str, Any],
    *,
    apply: bool = True,
    project_root: Path | None = None,
) -> dict[str, Any]:
    """Archive one finished dispatch. Never raises for policy/IO skip paths."""
    root_raw = project_root or record.get("project_root")
    if not isinstance(root_raw, (str, Path)) or not str(root_raw).strip():
        return {"ok": False, "keep": False, "reason": "missing project_root"}
    root = Path(str(root_raw)).expanduser()
    try:
        root = root.resolve()
    except OSError:
        return {"ok": False, "keep": False, "reason": "project_root unresolvable"}

    stdout = record.get("stdout_path") or record.get("tail_path")
    tail_path = Path(str(stdout)).expanduser() if isinstance(stdout, str) and stdout else None
    decision = decide_archive(record, tail_path=tail_path)
    result: dict[str, Any] = {
        "schema": SCHEMA,
        "dispatch_id": record.get("dispatch_id"),
        "project_root": str(root),
        "keep": bool(decision.get("keep")),
        "reason": decision.get("reason"),
        "apply": bool(apply),
    }
    if not decision.get("keep"):
        result["ok"] = True
        return result

    dest = _dest_dir(root, record)
    result["dest"] = str(dest)
    result["tail_bytes"] = decision.get("tail_bytes")
    result["dropped_bytes"] = decision.get("dropped_bytes")
    result["archived_bytes"] = decision.get("archived_bytes")
    result["dropped"] = [
        "steer mailbox",
        "watcher log",
        "caffeinate log",
        "pidfile",
        "prompt copy",
        "tail middle bytes" if decision.get("dropped_bytes") else None,
    ]
    result["dropped"] = [item for item in result["dropped"] if item]
    payload = decision["payload"]
    redacted, redaction_count, redaction_kinds = (
        goalflight_output_redact.redact_archive_bytes(payload)
    )
    if redaction_count:
        header = (
            f"[goalflight-trace-archive: {redaction_count} redaction(s) applied; "
            "this file is not verbatim]\n"
        ).encode("ascii")
        redacted = header + redacted
    result["redactions"] = redaction_count
    result["redaction_kinds"] = redaction_kinds
    if not apply:
        result["ok"] = True
        return result

    try:
        dest.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(dest, 0o700)
        (dest / "tail.log").write_bytes(redacted)
        os.chmod(dest / "tail.log", 0o600)
        status_copied = False
        status_raw = record.get("status_path")
        if isinstance(status_raw, str) and status_raw:
            status_path = Path(status_raw).expanduser()
            try:
                if (
                    not status_path.is_symlink()
                    and status_path.is_file()
                    and status_path.stat().st_size <= STATUS_MAX_BYTES
                ):
                    (dest / "status.json").write_bytes(status_path.read_bytes())
                    os.chmod(dest / "status.json", 0o600)
                    status_copied = True
            except OSError:
                status_copied = False
        result["status_copied"] = status_copied
        manifest = {k: v for k, v in result.items() if k != "payload"}
        manifest["kept"] = ["tail.log", "MANIFEST.json"] + (
            ["status.json"] if status_copied else []
        )
        manifest["git"] = (
            "docs-private/traces is gitignored. This tool never git-adds. "
            "Archived tails are unreviewed worker output, untrusted, and "
            "possibly sensitive. Credential-shaped material is redacted; "
            "the file is not verbatim."
        )
        manifest["redactions"] = redaction_count
        manifest["redaction_kinds"] = redaction_kinds
        manifest["drop_list"] = list(DROP_LIST)
        (dest / "MANIFEST.json").write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.chmod(dest / "MANIFEST.json", 0o600)
        result["ok"] = True
        result["written"] = True
        return result
    except OSError as exc:
        return {
            "ok": False,
            "keep": True,
            "reason": f"archive write failed ({exc.__class__.__name__}: {exc})",
            "dest": str(dest),
        }


def _iter_source_records(source_dir: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for status in sorted(source_dir.glob("*.status.json")):
        try:
            payload = json.loads(status.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict):
            continue
        dispatch_id = payload.get("dispatch_id") or status.name[: -len(".status.json")]
        tail = source_dir / f"{dispatch_id}.tail"
        record = dict(payload)
        record.setdefault("dispatch_id", dispatch_id)
        record.setdefault("status_path", str(status))
        record.setdefault("stdout_path", str(tail if tail.exists() else payload.get("tail_path") or ""))
        records.append(record)
    return records


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=(
            "Selectively archive finished dispatch tails from volatile dispatch "
            "state into gitignored docs-private/traces. Never git-adds. "
            "Archived tails are unreviewed worker output.\n\n"
            "DROPPED: unmarked/capacity-blocked/empty tails, steer mailboxes, "
            "watcher logs, caffeinate logs, pidfiles, prompt copies, "
            "oversized-tail middle bytes, and the historical /tmp backlog "
            "unless --source-dir --apply."
        )
    )
    parser.add_argument(
        "--project-root",
        type=Path,
        default=Path.cwd(),
        help="Repository whose docs-private/traces receives the archive.",
    )
    parser.add_argument(
        "--source-dir",
        type=Path,
        default=None,
        help="Dispatch dir to scan (default: none; use with --apply to sweep a backlog).",
    )
    parser.add_argument(
        "--dispatch-id",
        action="append",
        default=[],
        help="Archive one id (repeatable). Looks up stdout_path from --source-dir.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write files. Default is report-only.",
    )
    parser.add_argument("--json", action="store_true", help="Emit JSON.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    project_root = args.project_root.resolve()
    results: list[dict[str, Any]] = []
    if args.source_dir is None and not args.dispatch_id:
        print(
            "goalflight_trace_archive: pass --source-dir to scan a dispatch "
            "directory, or --dispatch-id with --source-dir. Report-only unless "
            "--apply. Historical /tmp backlogs are not copied automatically.",
            file=sys.stderr,
        )
        return 64
    source = args.source_dir.resolve() if args.source_dir else None
    if source is None:
        print("goalflight_trace_archive: --source-dir is required", file=sys.stderr)
        return 64
    wanted = set(args.dispatch_id)
    for record in _iter_source_records(source):
        if wanted and str(record.get("dispatch_id")) not in wanted:
            continue
        record.setdefault("project_root", str(project_root))
        results.append(
            archive_finished_dispatch(
                record, apply=bool(args.apply), project_root=project_root
            )
        )
    kept = sum(1 for row in results if row.get("keep") and row.get("written"))
    eligible = sum(1 for row in results if row.get("keep"))
    skipped = sum(1 for row in results if not row.get("keep"))
    report = {
        "schema": SCHEMA,
        "project_root": str(project_root),
        "source_dir": str(source),
        "mode": "apply" if args.apply else "report",
        "scanned": len(results),
        "eligible": eligible,
        "written": kept,
        "skipped": skipped,
        "results": results,
    }
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(
            f"trace archive ({report['mode']}): scanned={len(results)} "
            f"eligible={eligible} written={kept} skipped={skipped}"
        )
        for row in results:
            flag = "KEEP" if row.get("keep") else "SKIP"
            dest = row.get("dest") or ""
            print(f"  {flag} {row.get('dispatch_id')}  {row.get('reason')}  {dest}")
        if not args.apply and eligible:
            print("report only — nothing written. Re-run with --apply to copy.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
