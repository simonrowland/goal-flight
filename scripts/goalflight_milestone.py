#!/usr/bin/env python3
"""Milestone sweep due detector for goal-flight runs."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import goalflight_capacity
import goalflight_session_status

SCHEMA = "goalflight.milestone.v1"
MARKER_NAME = "milestone-marker.json"
DEFAULT_K = 5
CLEAN_VERDICT = "clean"
CLEAN_VERDICTS = {CLEAN_VERDICT, "converged"}

_CADENCE_KEYS = {
    "milestone_k",
    "milestone_cadence",
    "milestone_cadence_k",
    "milestone_review_k",
    "milestone_review_cadence",
    "review_milestone_k",
    "review_milestone_cadence",
}

_CADENCE_CONTAINER_KEYS = {"milestone", "milestone_review", "review_milestone"}
_NESTED_CADENCE_KEYS = {"k", "cadence"}


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def state_dir() -> Path:
    return goalflight_capacity.state_dir()


def marker_path(root: Path | None = None) -> Path:
    return (root or state_dir()) / MARKER_NAME


def git_root(cwd: Path | None = None) -> Path | None:
    try:
        out = _git(cwd or Path.cwd(), ["rev-parse", "--show-toplevel"])
    except RuntimeError:
        return None
    return Path(out).resolve()


def _git(repo: Path, args: list[str]) -> str:
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=str(repo),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"git {' '.join(args)} failed: {exc}") from exc
    if proc.returncode != 0:
        msg = (proc.stderr or proc.stdout or "").strip()
        raise RuntimeError(msg or f"git {' '.join(args)} failed")
    return proc.stdout.strip()


def _git_ok(repo: Path, args: list[str]) -> bool:
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=str(repo),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return proc.returncode == 0


def current_head(repo: Path) -> str:
    return _git(repo, ["rev-parse", "HEAD"])


def short_commit(value: str | None) -> str | None:
    if not value:
        return None
    return value[:7]


def read_marker(root: Path | None = None) -> dict[str, Any] | None:
    path = marker_path(root)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict) or not data.get("commit"):
        return None
    marker = {
        "commit": str(data.get("commit")),
        "ts": data.get("ts"),
        "verdict": data.get("verdict"),
    }
    if data.get("clean_commit"):
        marker["clean_commit"] = str(data.get("clean_commit"))
        marker["clean_ts"] = data.get("clean_ts")
    return marker


def clean_marker_details(marker: dict[str, Any] | None) -> dict[str, Any] | None:
    if not marker:
        return None
    verdict = marker.get("verdict")
    # Legacy marker files predate the verdict field; keep treating them as
    # clean anchors for back-compat rather than unknown/inconclusive reviews.
    if verdict is None or str(verdict).strip().lower() in CLEAN_VERDICTS:
        return {
            "commit": str(marker["commit"]),
            "ts": marker.get("ts"),
            "verdict": verdict,
        }
    clean_commit = marker.get("clean_commit")
    if clean_commit:
        return {
            "commit": str(clean_commit),
            "ts": marker.get("clean_ts"),
            "verdict": CLEAN_VERDICT,
        }
    return None


def write_marker(
    *,
    commit: str,
    verdict: str,
    root: Path | None = None,
    now: str | None = None,
) -> Path:
    path = marker_path(root)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    payload = {"commit": commit, "ts": now or utc_now(), "verdict": verdict}
    if str(verdict).strip().lower() not in CLEAN_VERDICTS:
        previous_clean = clean_marker_details(read_marker(root))
        if previous_clean:
            payload["clean_commit"] = previous_clean["commit"]
            payload["clean_ts"] = previous_clean.get("ts")
    tmp = path.with_suffix(path.suffix + f".tmp.{os.getpid()}")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)
    return path


def _coerce_positive_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _parse_inline_mapping(value: str) -> dict[str, str] | None:
    stripped = value.strip()
    if not (stripped.startswith("{") and stripped.endswith("}")):
        return None
    body = stripped[1:-1].strip()
    if not body:
        return {}
    out: dict[str, str] = {}
    for part in body.split(","):
        key, sep, raw_value = part.partition(":")
        if not sep:
            return None
        normalized_key = key.strip().strip("\"'")
        if not normalized_key:
            return None
        out[normalized_key] = raw_value.strip().strip("\"'")
    return out


def _normalize_key(value: Any) -> str:
    return str(value).strip().lower().replace("-", "_")


def cadence_from_config(config: Any, *, _in_milestone_context: bool = False) -> int | None:
    if isinstance(config, dict):
        if _in_milestone_context and any(
            _normalize_key(key) not in _NESTED_CADENCE_KEYS for key in config
        ):
            return None
        for key, value in config.items():
            normalized = _normalize_key(key)
            if _in_milestone_context and normalized not in _NESTED_CADENCE_KEYS:
                continue
            if normalized in _CADENCE_KEYS or (
                _in_milestone_context and normalized in _NESTED_CADENCE_KEYS
            ):
                found = _coerce_positive_int(value)
                if found is not None:
                    return found
            if normalized in _CADENCE_CONTAINER_KEYS:
                found = _coerce_positive_int(value)
                if found is not None:
                    return found
            if normalized in {"milestone", "milestone_review", "review", "config"}:
                found = cadence_from_config(
                    value,
                    _in_milestone_context=_in_milestone_context
                    or normalized in _CADENCE_CONTAINER_KEYS,
                )
                if found is not None:
                    return found
        for key, value in config.items():
            normalized = _normalize_key(key)
            if normalized in _CADENCE_CONTAINER_KEYS:
                continue
            if _in_milestone_context and normalized not in _NESTED_CADENCE_KEYS:
                continue
            found = cadence_from_config(value, _in_milestone_context=_in_milestone_context)
            if found is not None:
                return found
    elif isinstance(config, list):
        for item in config:
            found = cadence_from_config(item, _in_milestone_context=_in_milestone_context)
            if found is not None:
                return found
    elif isinstance(config, str):
        inline = _parse_inline_mapping(config)
        if inline is not None:
            return cadence_from_config(inline, _in_milestone_context=_in_milestone_context)
    return None


def _contains_cadence_candidate(config: Any, *, _in_milestone_context: bool = False) -> bool:
    if isinstance(config, dict):
        for key, value in config.items():
            normalized = _normalize_key(key)
            if normalized in _CADENCE_KEYS:
                return True
            if _in_milestone_context and normalized in _NESTED_CADENCE_KEYS:
                return True
            if _in_milestone_context and normalized not in _NESTED_CADENCE_KEYS:
                return True
            if normalized in _CADENCE_CONTAINER_KEYS:
                return True
            if _contains_cadence_candidate(
                value,
                _in_milestone_context=_in_milestone_context
                or normalized in _CADENCE_CONTAINER_KEYS,
            ):
                return True
    elif isinstance(config, list):
        return any(
            _contains_cadence_candidate(item, _in_milestone_context=_in_milestone_context)
            for item in config
        )
    elif isinstance(config, str):
        inline = _parse_inline_mapping(config)
        if inline is not None:
            return _contains_cadence_candidate(
                inline,
                _in_milestone_context=_in_milestone_context,
            )
    return False


def cadence_details_for_queue(queue: Path | None) -> tuple[int, list[str]]:
    frontmatter = queue_frontmatter(queue)
    configured = cadence_from_config(frontmatter)
    if configured is not None:
        return configured, []
    if _contains_cadence_candidate(frontmatter):
        return DEFAULT_K, [
            f"milestone cadence config present but unparseable; using default {DEFAULT_K}"
        ]
    return DEFAULT_K, []


def active_queue(project_root: Path) -> tuple[Path | None, dict[str, Any]]:
    status = goalflight_session_status.aggregate_status(project_root)
    queue_file = status.get("queue_file")
    if not status.get("active") or not queue_file:
        return None, status
    return project_root / str(queue_file), status


def queue_frontmatter(queue: Path | None) -> dict[str, Any]:
    if queue is None:
        return {}
    try:
        text = queue.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}
    front, _body = goalflight_session_status._parse_frontmatter(text)
    return front


def cadence_for_queue(queue: Path | None) -> int:
    cadence, _warnings = cadence_details_for_queue(queue)
    return cadence


def _landed_status(status: str) -> bool:
    upper = status.upper()
    if "TODO" in upper or "BLOCKED" in upper or "IN-FLIGHT" in upper or "IN FLIGHT" in upper:
        return False
    return any(token in upper for token in ("DONE", "COMPLETE", "LANDED", "MERGED", "REVIEWED")) or "✅" in status


def _commit_token(value: str) -> str | None:
    match = re.search(r"\b[0-9a-fA-F]{7,40}\b", value)
    return match.group(0) if match else None


def milestone_rows(queue: Path | None) -> list[dict[str, Any]]:
    if queue is None:
        return []
    try:
        lines = queue.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    rows: list[dict[str, Any]] = []
    for lineno, line in enumerate(lines, 1):
        if "[milestone]" not in line.lower() or not line.lstrip().startswith("|"):
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) < 2:
            continue
        status = cells[1]
        commit_cell = cells[2] if len(cells) > 2 else ""
        rows.append(
            {
                "line": lineno,
                "goal": cells[0],
                "status": status,
                "commit": _commit_token(commit_cell),
                "landed": _landed_status(status),
            }
        )
    return rows


def _resolve_commit(repo: Path, commit: str | None) -> str | None:
    if not commit:
        return None
    try:
        return _git(repo, ["rev-parse", f"{commit}^{{commit}}"])
    except RuntimeError:
        return None


def _commit_after(repo: Path, ancestor: str, commit: str | None) -> bool:
    resolved_ancestor = _resolve_commit(repo, ancestor)
    resolved_commit = _resolve_commit(repo, commit)
    if not resolved_ancestor or not resolved_commit or resolved_ancestor == resolved_commit:
        return False
    return _git_ok(repo, ["merge-base", "--is-ancestor", resolved_ancestor, resolved_commit])


def milestone_tag_due(
    repo: Path,
    rows: list[dict[str, Any]],
    anchor_commit: str | None,
) -> dict[str, Any] | None:
    for row in rows:
        if not row.get("landed"):
            continue
        commit = row.get("commit")
        if commit is None:
            continue
        resolved_commit = _resolve_commit(repo, commit)
        if not resolved_commit:
            continue
        if anchor_commit is None:
            continue
        if _commit_after(repo, anchor_commit, resolved_commit):
            return row
    return None


def merge_base(repo: Path, base_ref: str) -> str | None:
    try:
        return _git(repo, ["merge-base", base_ref, "HEAD"])
    except RuntimeError:
        return None


def commits_since(repo: Path, start_commit: str) -> int:
    raw = _git(repo, ["rev-list", "--count", f"{start_commit}..HEAD"])
    return int(raw or "0")


def check_status(
    *,
    repo: Path | None = None,
    project_root: Path | None = None,
    root: Path | None = None,
    queue: Path | None = None,
    base_ref: str = "main",
    require_active_queue: bool = False,
) -> dict[str, Any]:
    repo_root = (repo or project_root or git_root(Path.cwd()) or Path.cwd()).resolve()
    project = (project_root or repo_root).resolve()
    root = root or state_dir()
    queue_status: dict[str, Any] = {}
    active = True
    if queue is None:
        queue, queue_status = active_queue(project)
        active = bool(queue_status.get("active")) and queue is not None
    else:
        queue = queue.resolve()
    marker = read_marker(root)
    clean_marker = clean_marker_details(marker)
    clean_marker_commit = clean_marker.get("commit") if clean_marker else None
    repo_error = None
    if not _git_ok(repo_root, ["rev-parse", "--is-inside-work-tree"]):
        repo_error = f"not a git repository: {repo_root}"
    marker_anchor = (
        _resolve_commit(repo_root, clean_marker_commit)
        if clean_marker_commit and repo_error is None
        else None
    )
    arc_start: str | None = None
    count_error: str | None = None
    if require_active_queue and not active:
        return {
            "schema": SCHEMA,
            "active_cadence": False,
            "commits_since": None,
            "K": None,
            "last_marker": marker,
            "last_clean_marker": clean_marker,
            "due": False,
            "reason": "no active cadence",
            "warnings": [],
            "error": None,
            "queue_file": None,
            "marker_path": str(marker_path(root)),
        }

    if repo_error:
        start = None
        count_error = repo_error
    elif marker_anchor:
        start = marker_anchor
    else:
        arc_start = merge_base(repo_root, base_ref)
        start = arc_start
        if clean_marker_commit and not marker_anchor:
            count_error = "clean marker commit unreachable"
    if start is None:
        count = None
        count_error = count_error or f"no merge-base {base_ref}"
    else:
        try:
            count = commits_since(repo_root, start)
        except (RuntimeError, ValueError) as exc:
            count = None
            count_error = str(exc)

    cadence, cadence_warnings = cadence_details_for_queue(queue)
    rows = milestone_rows(queue)
    tag_anchor = marker_anchor or arc_start
    tagged = None if count_error else milestone_tag_due(repo_root, rows, tag_anchor)
    due_by_count = False if count_error else count is not None and count >= cadence
    due = None if count_error else bool(tagged) or due_by_count
    if count_error:
        reason = "milestone unavailable"
    elif tagged:
        reason = "milestone tag landed since marker" if marker_anchor else "milestone tag landed"
    elif due_by_count:
        reason = "commit cadence reached"
    else:
        reason = "ok"
    return {
        "schema": SCHEMA,
        "active_cadence": True,
        "commits_since": count,
        "K": cadence,
        "last_marker": marker,
        "last_clean_marker": clean_marker,
        "due": due,
        "reason": reason,
        "warnings": cadence_warnings,
        "error": count_error,
        "queue_file": str(queue.relative_to(project)) if queue and _is_relative_to(queue, project) else (str(queue) if queue else None),
        "marker_path": str(marker_path(root)),
        "arc_start": arc_start,
        "tagged_milestone": tagged,
        "milestone_rows": rows,
    }


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def format_line(status: dict[str, Any]) -> str:
    error = status.get("error")
    if error:
        return f"milestone: unavailable ({_one_line(error)})"
    if not status.get("active_cadence"):
        return "milestone: no active cadence"
    count = status.get("commits_since")
    count_s = "?" if count is None else str(count)
    cadence = status.get("K") or "?"
    last_marker = status.get("last_marker") or {}
    clean_marker = status.get("last_clean_marker")
    marker = clean_marker or last_marker
    if marker.get("commit"):
        anchor_label = (
            "last clean sweep"
            if clean_marker and last_marker.get("commit") and clean_marker.get("commit") != last_marker.get("commit")
            else "last sweep"
        )
        anchor = f"{anchor_label} @ {short_commit(str(marker['commit']))}"
    elif status.get("arc_start"):
        anchor = f"arc start @ {short_commit(str(status['arc_start']))}"
    else:
        anchor = "arc start"
    verdict = "DUE" if status.get("due") else "ok"
    line = f"milestone: {count_s}/{cadence} since {anchor} -> {verdict}"
    warnings = status.get("warnings") or []
    if warnings:
        line += f" (warning: {_one_line(warnings[0])})"
    return line


def _one_line(value: Any) -> str:
    return " ".join(str(value).split())


def cmd_mark(args: argparse.Namespace) -> int:
    repo = Path(args.repo).resolve() if args.repo else (git_root(Path.cwd()) or Path.cwd())
    try:
        commit = args.commit or current_head(repo)
    except RuntimeError as exc:
        print(f"goalflight_milestone: {exc}", file=sys.stderr)
        return 2
    root = Path(args.state_dir).expanduser() if args.state_dir else None
    try:
        path = write_marker(commit=commit, verdict=args.verdict, root=root)
    except OSError as exc:
        print(f"goalflight_milestone: mark failed: {exc}", file=sys.stderr)
        return 2
    payload = read_marker(root) or {"commit": commit, "ts": None, "verdict": args.verdict}
    if args.json:
        print(json.dumps({"marker_path": str(path), "marker": payload}, sort_keys=True))
    else:
        print(f"marked milestone sweep @ {short_commit(commit)} ({args.verdict}) -> {path}")
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    repo = Path(args.repo).resolve() if args.repo else None
    project = Path(args.project).resolve() if args.project else None
    root = Path(args.state_dir).expanduser() if args.state_dir else None
    queue = Path(args.queue).resolve() if args.queue else None
    try:
        payload = check_status(
            repo=repo,
            project_root=project,
            root=root,
            queue=queue,
            base_ref=args.base_ref,
            require_active_queue=args.require_active_queue,
        )
    except RuntimeError as exc:
        print(f"goalflight_milestone: {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(payload, sort_keys=True))
    else:
        print(format_line(payload))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="goal-flight milestone sweep due detector")
    sub = parser.add_subparsers(dest="command", required=True)

    mark = sub.add_parser("mark", help="record a converged milestone sweep marker")
    mark.add_argument("--commit", help="commit to record; defaults to HEAD")
    mark.add_argument("--verdict", default="clean", help="sweep verdict to record")
    mark.add_argument("--repo", help="git repository root; defaults to cwd git root")
    mark.add_argument("--state-dir", help="state dir; defaults to GOALFLIGHT_STATE_DIR or goal-flight temp state")
    mark.add_argument("--json", action="store_true", help="emit marker JSON")
    mark.set_defaults(func=cmd_mark)

    check = sub.add_parser("check", help="report whether a milestone sweep is due")
    check.add_argument("--json", action="store_true", help="emit machine JSON")
    check.add_argument("--repo", help="git repository root; defaults to cwd git root")
    check.add_argument("--project", help="project root for active queue discovery")
    check.add_argument("--queue", help="override active goal queue path")
    check.add_argument("--state-dir", help="state dir; defaults to GOALFLIGHT_STATE_DIR or goal-flight temp state")
    check.add_argument("--base-ref", default="main", help="arc-start ref when no marker exists")
    check.add_argument(
        "--require-active-queue",
        action="store_true",
        help="return no active cadence when active goal-flight queue discovery fails",
    )
    check.set_defaults(func=cmd_check)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
