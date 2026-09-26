#!/usr/bin/env python3
"""Report (or remove) git worktrees whose removal is provably safe.

Report-only by default. ``--apply`` is required to remove anything, and every
survivor is printed with the reason it was retained — the reason is how an
operator confirms the tool understood the tree rather than guessed.

Routine merge-down command (run from the repo after integrating a worker branch)::

    python3 scripts/goalflight_worktree_gc.py --into main
    python3 scripts/goalflight_worktree_gc.py --into main --apply

Managed pool worktrees (new ``<repo>/worktrees/s-N`` and migrated legacy
``<repo>/worktrees/<label>/s-N`` / ``wt-N`` paths) are evaluated by the same
four-part predicate as other registered worktrees. A directory merely *named*
``s-N`` or ``wt-N`` is ordinary litter: registration is evidence, not a
deletion exemption. If registration cannot be determined, the verdict is
UNKNOWN and the tree is retained.

Shared read-only worktrees under ``<repo>/worktrees/.goalflight-readonly`` are
also listed. They are detached by design, so merge state is not a condition;
cleanliness, dispatch ownership, and current-checkout protection still apply.

Removal requires the CONJUNCTION of all four conditions:

  1. the worktree's branch is merged into the integration branch; AND
  2. the worktree is clean (``git status --porcelain`` empty); AND
  3. no non-terminal dispatch records that path as its ``worker_cwd``, and no
     identity-live worker whose ledger row carries a liveness verdict; AND
  4. it is not the currently-checked-out path (nor the main worktree).

Why the conjunction, and why "merged" alone is not a predicate
--------------------------------------------------------------

Audit 2026-08-27: 37 worktrees had accumulated under ``worktrees/`` from prior
sessions. The obvious sweep — "branch is an ancestor of main, therefore safe to
delete" — returns TRUE for a worktree whose branch simply EQUALS main because
its worker has not committed yet. A merged-only sweep would have deleted four
ACTIVE workers' in-progress trees; all four were live at audit time. The
predicate answered a different question than the one being asked, and it looked
authoritative. Condition (3) is the one that saves live work.

Condition (3) reads the dispatch ledger — never ``ps``/``pgrep``. ``pgrep``
matches the searcher itself, and a process probe cannot see a worker that has
been dispatched but has not spawned yet; the ledger records the claim at
dispatch time, before any process exists. A future maintainer will be tempted
to drop this check as slow: the four live trees above are what it costs.

Three-state discipline (load-bearing)
-------------------------------------

Every condition distinguishes "I know this is false" from "I could not find
out". UNKNOWN always retains, with a reason naming the check that could not be
performed. If the ledger is unreadable, condition (3) is UNKNOWN for every
worktree — an unreadable record may be exactly the live dispatch that owns the
path. A tool that treats "could not read the ledger" as "no dispatch owns it"
deletes live workers' trees, which is precisely the failure this predicate
exists to prevent. Absence of proof is never proof of absence.

Removal uses ``git worktree remove`` semantics. A worktree whose directory is
already gone but whose administrative entry remains is reclaimed with
``git worktree prune`` and reported as ``pruned`` — a distinct outcome, not an
error and not a removal. Paths come exclusively from the repo's own
``git worktree list --porcelain`` output; nothing outside that list is touched.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import time
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import goalflight_compat  # noqa: E402
import goalflight_dispatch_states  # noqa: E402
import goalflight_fs  # noqa: E402
import goalflight_ledger  # noqa: E402
import goalflight_worktree_pool  # noqa: E402

SCHEMA = "goalflight.worktree-gc.v1"

YES = "yes"
NO = "no"
UNKNOWN = "unknown"

_GIT_TIMEOUT = 30

# Terminal rows are kept for the same seven-day window as the dispatch ledger
# itself.  The resume path accepts terminal states such as quota_exhausted;
# retaining their checkout for the ledger retention horizon makes that promise
# true without inventing a second cleanup policy.
RESUMABLE_TERMINAL_HORIZON_S = (
    goalflight_ledger.TERMINAL_RECORD_RETENTION_DAYS * 24.0 * 60.0 * 60.0
)

# Ledger rows whose ``state`` / ``terminal_state`` looks settled but may still
# name a live process. ``idle_timeout`` in particular has been observed on a
# worker that stayed identity-live and mid-gate for tens of minutes.
LIVENESS_VERDICTS = frozenset(
    {
        "idle_timeout",
        "worker_dead",
        "blocked",
        "wedged",
        "liveness_indeterminate",
        "inconclusive_timeout",
        "watcher_stopped",
    }
)


def _remaining_timeout(deadline: float | None) -> float | None:
    if deadline is None:
        return None
    return max(0.0, deadline - time.monotonic())


def _deadline_expired(deadline: float | None) -> bool:
    return deadline is not None and time.monotonic() >= deadline


def _git(
    repo: Path, *args: str, timeout: float | None = None
) -> subprocess.CompletedProcess[str]:
    effective_timeout = _GIT_TIMEOUT if timeout is None else max(0.0, timeout)
    guard_error = goalflight_worktree_pool.guard_worktree_mutation(
        repo, *args, timeout=effective_timeout
    )
    if guard_error is not None:
        return subprocess.CompletedProcess(
            ["git", "-C", str(repo), *args], 128, "", guard_error
        )
    try:
        return subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=effective_timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(
            ["git", "-C", str(repo), *args],
            124,
            "",
            "git command timed out",
        )


def _presence(path: Path) -> str:
    """Return present / absent / unknown. Never use Path.exists() here.

    ``exists()`` answers False for both "not there" and "I could not look".
    Only FileNotFoundError is evidence of absence.
    """
    return goalflight_fs.path_presence(path)


def _resolve(path: str) -> str:
    return os.path.realpath(path)


def _same_path(left: str, right: str) -> bool:
    """Compare paths without aliasing distinct case-sensitive paths."""
    left_real = _resolve(left)
    right_real = _resolve(right)
    if left_real == right_real:
        return True
    try:
        return os.path.samefile(left_real, right_real)
    except (FileNotFoundError, OSError):
        return False


def _condition(verdict: str, reason: str) -> dict[str, str]:
    return {"verdict": verdict, "reason": reason}


# --------------------------------------------------------------------------
# Worktree listing


def list_worktrees(
    repo: Path, *, deadline: float | None = None
) -> tuple[list[dict[str, Any]], str | None]:
    """Parse ``git worktree list --porcelain``. (entries, error)."""
    try:
        proc = _git(
            repo,
            "worktree",
            "list",
            "--porcelain",
            timeout=_remaining_timeout(deadline),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return [], f"git worktree list failed ({exc.__class__.__name__})"
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip() or "not a git repository"
        return [], detail
    entries: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for line in proc.stdout.splitlines():
        if line.startswith("worktree "):
            current = {"path": line[len("worktree "):].strip(), "branch": None,
                       "detached": False, "prunable": False, "bare": False}
            entries.append(current)
        elif current is None:
            continue
        elif line.startswith("branch "):
            ref = line.split(" ", 1)[1].strip()
            current["branch"] = ref.removeprefix("refs/heads/")
        elif line == "detached":
            current["detached"] = True
        elif line.startswith("prunable"):
            current["prunable"] = True
        elif line == "bare":
            current["bare"] = True
    return entries, None


def main_worktree_path(repo: Path, *, deadline: float | None = None) -> str | None:
    """Absolute path of the main worktree, via the common git dir's parent."""
    try:
        proc = _git(
            repo,
            "rev-parse",
            "--path-format=absolute",
            "--git-common-dir",
            timeout=_remaining_timeout(deadline),
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    common = proc.stdout.strip()
    if not common:
        return None
    return _resolve(str(Path(common).parent))


def current_checkout_path(
    repo: Path, *, deadline: float | None = None
) -> tuple[str | None, str | None]:
    """The checked-out path for the repo argument. (path, error)."""
    try:
        proc = _git(
            repo,
            "rev-parse",
            "--path-format=absolute",
            "--show-toplevel",
            timeout=_remaining_timeout(deadline),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return None, f"git rev-parse --show-toplevel failed ({exc.__class__.__name__})"
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip() or "cannot resolve checkout"
        return None, detail
    return _resolve(proc.stdout.strip()), None


# --------------------------------------------------------------------------
# The four conditions. Each returns _condition(YES|NO|UNKNOWN, reason).


def check_merged(
    repo: Path,
    branch: str | None,
    detached: bool,
    into: str,
    *,
    deadline: float | None = None,
) -> dict[str, str]:
    """Condition 1: the branch is merged into the integration branch."""
    if detached or not branch:
        return _condition(
            UNKNOWN,
            "detached HEAD: no branch to test for merge state",
        )
    if _deadline_expired(deadline):
        return _condition(UNKNOWN, "merge check deadline expired; retaining checkout")
    try:
        base = _git(
            repo,
            "rev-parse",
            "--verify",
            "--quiet",
            f"{into}^{{commit}}",
            timeout=_remaining_timeout(deadline),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return _condition(
            UNKNOWN,
            f"cannot resolve integration branch {into!r} ({exc.__class__.__name__})",
        )
    if base.returncode != 0:
        return _condition(
            UNKNOWN,
            f"integration branch {into!r} does not exist, so merge state "
            "cannot be evaluated",
        )
    if _deadline_expired(deadline):
        return _condition(UNKNOWN, "merge check deadline expired; retaining checkout")
    try:
        proc = _git(
            repo,
            "merge-base",
            "--is-ancestor",
            branch,
            into,
            timeout=_remaining_timeout(deadline),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return _condition(
            UNKNOWN,
            f"ancestry of branch {branch!r} could not be evaluated "
            f"({exc.__class__.__name__})",
        )
    if proc.returncode == 0:
        return _condition(YES, f"branch {branch!r} is merged into {into!r}")
    if proc.returncode == 1:
        # Ancestry answers "is this branch an ancestor of main?", but the
        # question is "is this branch's work in main?". Every branch whose
        # commits landed by rebase, squash, or cherry-pick fails the ancestry
        # test forever, so its worktree can never be reclaimed -- which is how
        # a checkout accumulates dozens of immortal trees holding work that is
        # already shipped.
        #
        # git cherry compares by patch-id: "+ <sha>" is a commit with no
        # equivalent upstream, "- <sha>" is one already applied. No "+" lines
        # means nothing unique is left to protect.
        if _deadline_expired(deadline):
            return _condition(UNKNOWN, "merge check deadline expired; retaining checkout")
        try:
            cherry = _git(
                repo,
                "cherry",
                into,
                branch,
                timeout=_remaining_timeout(deadline),
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return _condition(
                UNKNOWN,
                f"branch {branch!r} is not an ancestor of {into!r} and patch "
                f"equivalence could not be evaluated ({exc.__class__.__name__})",
            )
        if cherry.returncode != 0:
            detail = (cherry.stderr or cherry.stdout).strip() or f"exit {cherry.returncode}"
            return _condition(
                UNKNOWN,
                f"branch {branch!r} is not an ancestor of {into!r} and patch "
                f"equivalence could not be evaluated ({detail})",
            )
        unique = [
            line for line in cherry.stdout.splitlines() if line.startswith("+")
        ]
        if not unique:
            return _condition(
                YES,
                f"every commit on branch {branch!r} is already applied in "
                f"{into!r} (patch-id equivalent, not an ancestor)",
            )
        return _condition(
            NO,
            f"branch {branch!r} has {len(unique)} commit(s) not in {into!r}",
        )
    detail = (proc.stderr or proc.stdout).strip() or f"exit {proc.returncode}"
    return _condition(
        UNKNOWN,
        f"ancestry of branch {branch!r} could not be evaluated ({detail})",
    )


def check_clean(
    path: str,
    *,
    directory_state: str,
    deadline: float | None = None,
) -> dict[str, str]:
    """Condition 2: the worktree is clean.

    A missing directory is vacuously clean: there is no on-disk work left to
    protect, and reclamation of the administrative entry is the prune path.
    An unverifiable directory is never assumed clean.
    """
    if directory_state == "absent":
        return _condition(
            YES,
            "worktree directory absent; no on-disk work to protect",
        )
    if directory_state == "unknown":
        return _condition(
            UNKNOWN,
            "worktree directory presence unverifiable, so cleanliness is unknown",
        )
    if _deadline_expired(deadline):
        return _condition(UNKNOWN, "cleanliness check deadline expired; retaining checkout")
    try:
        proc = _git(
            Path(path),
            "status",
            "--porcelain",
            timeout=_remaining_timeout(deadline),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return _condition(
            UNKNOWN,
            f"git status could not run ({exc.__class__.__name__}), "
            "so cleanliness is unknown",
        )
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip() or f"exit {proc.returncode}"
        return _condition(
            UNKNOWN,
            f"git status failed ({detail}), so cleanliness is unknown",
        )
    dirty = [line for line in proc.stdout.splitlines() if line.strip()]
    if dirty:
        return _condition(
            NO,
            f"worktree has uncommitted or untracked files ({len(dirty)} entries)",
        )
    return _condition(YES, "worktree is clean")


def check_read_only_grace(path: str, *, directory_state: str) -> dict[str, str]:
    """Keep detached checkouts inside the allocator's grace window."""
    if directory_state == "absent":
        return _condition(YES, "checkout directory absent; no grace window applies")
    if directory_state == "unknown":
        return _condition(UNKNOWN, "checkout age could not be evaluated")
    try:
        age_s = max(0.0, time.time() - Path(path).stat().st_mtime)
    except OSError as exc:
        return _condition(UNKNOWN, f"checkout age could not be evaluated ({exc})")
    grace_s = float(goalflight_worktree_pool.READ_ONLY_WORKTREE_GRACE_S)
    if age_s < grace_s:
        return _condition(
            NO,
            f"checkout is inside the {grace_s:g}s read-only grace window",
        )
    return _condition(YES, "read-only grace window elapsed")


def read_ledger_records(
    ledger_dir: Path, *, deadline: float | None = None
) -> tuple[list[dict[str, Any]], list[str]]:
    """Return (records, unreadable_files) from the dispatch runs directory.

    ``goalflight_ledger.read_records`` collapses a corrupt file into an
    ``unreadable`` placeholder so production never raises; this sweep needs the
    distinction preserved, because an unreadable record may be exactly the live
    dispatch that owns the path under evaluation.
    """
    state = _presence(ledger_dir)
    if state == "absent":
        # No runs directory is not proof that no dispatch owns the path: a
        # live row may be temporarily hidden while the ledger is replaced.
        return [], [f"{ledger_dir} (absent)"]
    if state == "unknown":
        return [], [str(ledger_dir)]
    listing, children = goalflight_fs.list_dir_suffix(ledger_dir, ".json")
    if listing == "unreadable":
        # glob swallows PermissionError and yields []; iterdir raises.
        # An unlistable runs dir is not "no owner".
        return [], [str(ledger_dir)]
    if listing == "absent" or not children:
        # A readable-but-empty ledger is the same fail-closed condition as a
        # missing ledger. It does not positively prove that this path is free.
        return [], [f"{ledger_dir} (empty)"]
    records: list[dict[str, Any]] = []
    unreadable: list[str] = []
    for child in sorted(children):
        if deadline is not None and time.monotonic() >= deadline:
            unreadable.append(f"{ledger_dir} (snapshot deadline expired)")
            break
        try:
            payload = json.loads(child.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            unreadable.append(child.name)
            continue
        if deadline is not None and time.monotonic() >= deadline:
            unreadable.append(f"{ledger_dir} (snapshot deadline expired)")
            break
        if not isinstance(payload, dict):
            unreadable.append(child.name)
            continue
        records.append(payload)
    return records, unreadable


def _argv_lists_from_record(record: dict[str, Any]) -> list[list[str]]:
    """Return every recorded dispatch argv in every ledger envelope shape."""
    envelope = (
        record.get("request_envelope")
        if isinstance(record.get("request_envelope"), dict)
        else {}
    )
    request = record.get("request") if isinstance(record.get("request"), dict) else {}
    env_request = (
        envelope.get("request") if isinstance(envelope.get("request"), dict) else {}
    )
    argv_lists: list[list[str]] = []
    for blob in (
        record.get("dispatch_argv"),
        envelope.get("dispatch_argv"),
        request.get("dispatch_argv"),
        env_request.get("dispatch_argv"),
    ):
        if isinstance(blob, list) and blob:
            argv_lists.append([str(part) for part in blob])
        elif isinstance(blob, str) and blob.strip():
            try:
                argv_lists.append(shlex.split(blob))
            except ValueError:
                argv_lists.append(blob.split())
    return argv_lists


def _argv_option_values(argv: list[str], flag: str) -> list[str]:
    """Read every ``--flag value`` and ``--flag=value`` from recorded argv."""
    prefix = flag + "="
    values: list[str] = []
    index = 0
    while index < len(argv):
        token = argv[index]
        if token == flag:
            if index + 1 < len(argv):
                value = argv[index + 1]
                if value and not value.startswith("-"):
                    values.append(value)
                    index += 2
                    continue
            index += 1
            continue
        if token.startswith(prefix):
            values.append(token[len(prefix) :])
        index += 1
    return values


def _looks_like_path(value: str) -> bool:
    """Recognize path-shaped --worktree/--at values without treating refs as paths."""
    return value.startswith((os.sep, ".", "~")) or Path(value).is_absolute()


def _record_cwd_raw_values(record: dict[str, Any]) -> list[str]:
    """Return every recorded source that can identify a worker checkout."""
    values: list[str] = []
    seen: set[str] = set()

    def add(raw: object) -> None:
        if raw is None:
            return
        text = str(raw).strip()
        if not text or text in seen:
            return
        seen.add(text)
        values.append(text)

    add(record.get("worker_cwd"))
    add(record.get("worktree_path"))
    for argv in _argv_lists_from_record(record):
        for value in _argv_option_values(argv, "--cwd"):
            add(value)
        for flag in ("--worktree", "--at"):
            for value in _argv_option_values(argv, flag):
                if _looks_like_path(value):
                    # These options are normally Git refs. A path-shaped value
                    # is still ownership evidence when a caller recorded a
                    # derived checkout path instead of the post-admission fields.
                    add(value)
    envelope = (
        record.get("request_envelope")
        if isinstance(record.get("request_envelope"), dict)
        else {}
    )
    env_request = (
        envelope.get("request") if isinstance(envelope.get("request"), dict) else {}
    )
    request = record.get("request") if isinstance(record.get("request"), dict) else {}
    for blob in (envelope, request, env_request):
        add(blob.get("worker_cwd"))
        add(blob.get("worktree_path"))
        add(blob.get("cwd"))
    return values


def _record_checkout_raw_values(record: dict[str, Any]) -> list[str]:
    """Return sources that admission records as the worker's checkout cwd."""
    values: list[str] = []
    seen: set[str] = set()

    def add(raw: object) -> None:
        if raw is None:
            return
        text = str(raw).strip()
        if not text or text in seen:
            return
        seen.add(text)
        values.append(text)

    add(record.get("worker_cwd"))
    add(record.get("worktree_path"))
    for argv in _argv_lists_from_record(record):
        for value in _argv_option_values(argv, "--cwd"):
            add(value)
    envelope = (
        record.get("request_envelope")
        if isinstance(record.get("request_envelope"), dict)
        else {}
    )
    env_request = (
        envelope.get("request") if isinstance(envelope.get("request"), dict) else {}
    )
    request = record.get("request") if isinstance(record.get("request"), dict) else {}
    for blob in (envelope, request, env_request):
        add(blob.get("worker_cwd"))
        add(blob.get("worktree_path"))
    return values


def _record_cwd_paths(record: dict[str, Any]) -> tuple[str, ...]:
    """Resolve usable recorded cwd sources using the row's project root."""
    raw_root = record.get("project_root")
    root: Path | None = None
    if isinstance(raw_root, str) and raw_root.strip():
        try:
            root = Path(raw_root.strip()).expanduser()
        except (OSError, RuntimeError, TypeError, ValueError):
            root = None
    paths: list[str] = []
    for raw in _record_cwd_raw_values(record):
        try:
            candidate = Path(raw).expanduser()
            if not candidate.is_absolute():
                if root is None or not root.is_absolute():
                    continue
                candidate = root / candidate
            paths.append(_resolve(str(candidate)))
        except (OSError, RuntimeError, ValueError):
            continue
    return tuple(dict.fromkeys(paths))


class LedgerIndex:
    """One sweep's ledger snapshot plus reusable path and identity indexes."""

    def __init__(
        self,
        records: list[dict[str, Any]],
        unreadable: list[str],
        *,
        deadline: float | None = None,
    ) -> None:
        self.records = tuple(records)
        indexed_unreadable = list(unreadable)
        self._cwd_paths: dict[int, tuple[str, ...]] = {}
        for record in self.records:
            if deadline is not None and time.monotonic() >= deadline:
                indexed_unreadable.append("ledger path index deadline expired")
                break
            self._cwd_paths[id(record)] = _record_cwd_paths(record)
        self.unreadable = tuple(indexed_unreadable)
        self.repository_identities: dict[str, tuple[str, int, int] | None] = {}

    def cwd_paths(self, record: dict[str, Any]) -> tuple[str, ...]:
        return self._cwd_paths.get(id(record), ())


def ledger_index_for_dir(
    ledger_dir: Path, *, deadline: float | None = None
) -> LedgerIndex:
    """Read and index the dispatch ledger once for one reclamation sweep."""
    if deadline is not None and time.monotonic() >= deadline:
        return LedgerIndex([], [f"{ledger_dir} (snapshot deadline expired)"])
    records, unreadable = read_ledger_records(ledger_dir, deadline=deadline)
    if deadline is not None and time.monotonic() >= deadline:
        return LedgerIndex(
            [], [f"{ledger_dir} (snapshot deadline expired)"]
        )
    return LedgerIndex(records, unreadable, deadline=deadline)


def _record_cwd_matches(
    record: dict[str, Any], path: str, *, ledger_index: LedgerIndex | None = None
) -> bool:
    target = _resolve(path)
    candidates = (
        ledger_index.cwd_paths(record)
        if ledger_index is not None
        else _record_cwd_paths(record)
    )
    return any(
        _same_path(candidate, target)
        or candidate == target
        or candidate.startswith(target + os.sep)
        for candidate in candidates
    )


def _record_has_usable_path(record: dict[str, Any]) -> bool:
    """True when every recorded checkout path resolves to an existing directory."""
    saw_path = False
    raw_root = record.get("project_root")
    root: Path | None = None
    if isinstance(raw_root, str) and raw_root.strip():
        try:
            root = Path(raw_root.strip()).expanduser()
        except (OSError, RuntimeError, ValueError):
            root = None
    for raw_path in _record_checkout_raw_values(record):
        saw_path = True
        candidate = raw_path.strip()
        try:
            candidate_path = Path(candidate).expanduser()
            if not candidate_path.is_absolute():
                if root is None or not root.is_absolute():
                    return False
                candidate_path = root / candidate_path
            os.path.realpath(str(candidate_path))
            if not candidate_path.is_dir():
                return False
        except (OSError, RuntimeError, ValueError):
            return False
    return saw_path


def _record_project_root_matches(
    record: dict[str, Any],
    project_root: Path | None,
    *,
    identity_cache: dict[str, tuple[str, int, int] | None] | None = None,
    deadline: float | None = None,
) -> bool | None:
    """Return whether a pathless row can concern ``project_root``.

    ``False`` is reserved for a proven different Git repository.  A missing,
    relative, or otherwise unresolvable root stays ``None`` so ownership
    remains fail-closed.  Comparing Git common directories also treats two
    worktree spellings of one repository as the same project after the
    realpath comparison has ruled out a literal spelling match.
    """
    if project_root is None:
        return None
    raw = record.get("project_root")
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        recorded_path = Path(raw.strip()).expanduser()
        target_path = Path(project_root).expanduser()
        if not recorded_path.is_absolute() or not target_path.is_absolute():
            return None
        recorded_real = Path(os.path.realpath(str(recorded_path)))
        target_real = Path(os.path.realpath(str(target_path)))
    except (OSError, RuntimeError, ValueError):
        return None
    if recorded_real == target_real:
        return True

    if identity_cache is None:
        identity_cache = {}

    def cached_common_identity(path: Path) -> tuple[str, int, int] | None:
        try:
            # Case-folding is unsafe on case-sensitive filesystems: two
            # repositories can have the same folded spelling.
            key = str(Path(os.path.realpath(str(path))))
        except (OSError, ValueError):
            return None
        if key in identity_cache:
            return identity_cache[key]
        timeout = goalflight_worktree_pool.READ_ONLY_GIT_TIMEOUT_S
        if deadline is not None:
            timeout = min(timeout, max(0.0, deadline - time.monotonic()))
            if timeout <= 0:
                identity_cache[key] = None
                return None
        try:
            common = goalflight_worktree_pool._git_common_dir(
                Path(os.path.realpath(str(path))), timeout=timeout
            )
            common_real = os.path.realpath(str(common))
            common_stat = os.stat(common_real)
            identity = (
                common_real,
                int(common_stat.st_dev),
                int(common_stat.st_ino),
            )
        except (
            OSError,
            RuntimeError,
            TypeError,
            ValueError,
            subprocess.SubprocessError,
            goalflight_worktree_pool.WorktreeSeatError,
        ):
            identity = None
        identity_cache[key] = identity
        return identity

    recorded_common = cached_common_identity(recorded_real)
    target_common = cached_common_identity(target_real)
    if recorded_common is None or target_common is None:
        return None
    return recorded_common == target_common


def _dispatch_id_summary(dispatch_ids: list[str]) -> str:
    """Compactly name the first few blocking rows and their total count."""
    ordered = sorted(dispatch_ids)
    preview = ", ".join(ordered[:3])
    remainder = len(ordered) - len(ordered[:3])
    suffix = f" (+{remainder} more)" if remainder else ""
    return f"blocking dispatch rows (count={len(ordered)}): {preview}{suffix}"


def _record_states(record: dict[str, Any]) -> list[str]:
    states: list[str] = []
    for key in ("state", "terminal_state"):
        value = record.get(key)
        if isinstance(value, str) and value:
            states.append(value)
    return states


def _record_is_nonterminal(record: dict[str, Any]) -> bool:
    states = _record_states(record)
    return not states or any(
        not goalflight_dispatch_states.is_terminal_state(state) for state in states
    )


def _resumable_terminal_hold_reason(record: dict[str, Any]) -> str | None:
    """Return a retention reason while a terminal row remains resumeable.

    Resume accepts every structurally terminal dispatch state after proving the
    source worker is no longer live. Only rows carrying the resume metadata the
    resume path needs (or an explicit limit-terminal state such as
    ``quota_exhausted``) claim the checkout; synthetic legacy terminal rows
    without a resumable source remain ordinary cleanup candidates. Keep an
    eligible checkout for the ledger's seven-day retention horizon.
    """
    states = _record_states(record)
    if not states or not any(
        goalflight_dispatch_states.is_terminal_state(state) for state in states
    ):
        return None
    resumable_marker = any(
        state in goalflight_dispatch_states.LIMIT_TERMINAL_STATES for state in states
    ) or any(
        record.get(key)
        for key in (
            "parent_dispatch_id",
            "resume_mode",
            "engine_session_id",
            "codex_session_id",
            "acp_session_id",
            "worktree_base",
            "worktree_head",
        )
    )
    if not resumable_marker:
        return None
    terminal_at = goalflight_ledger.parse_utc(
        record.get("ended_at") or record.get("updated_at")
    )
    if terminal_at is None:
        # Legacy hand-written terminal rows predate the resume retention
        # contract and have no bounded age.  They remain subject to the
        # historical terminal-ownership predicate; only a real lifecycle row
        # with a timestamp can claim the resumable horizon.
        return None
    age_s = time.time() - terminal_at.timestamp()
    if age_s <= RESUMABLE_TERMINAL_HORIZON_S:
        return (
            "resumable terminal dispatch retains checkout for "
            f"{RESUMABLE_TERMINAL_HORIZON_S / 86400.0:g} days"
        )
    return None


def _is_liveness_verdict(record: dict[str, Any]) -> bool:
    for state in _record_states(record):
        if state in LIVENESS_VERDICTS or state.startswith("blocked"):
            return True
    return False


def _identity_live(record: dict[str, Any]) -> bool | None:
    """pid + start_token liveness. Never pgrep, never pid alone.

    True: the recorded generation is still that process.
    False: no pid was recorded, or the generation is proven gone/replaced.
    None: a pid exists but the check could not complete — fail closed.
    """
    identity = record.get("worker_identity")
    pid = None
    start_token = ""
    if isinstance(identity, dict):
        raw_pid = identity.get("pid")
        if isinstance(raw_pid, int) and not isinstance(raw_pid, bool) and raw_pid > 0:
            pid = raw_pid
        token = identity.get("start_token")
        if isinstance(token, str) and token:
            start_token = token
    if pid is None:
        raw_pid = record.get("worker_pid")
        if isinstance(raw_pid, int) and not isinstance(raw_pid, bool) and raw_pid > 0:
            pid = raw_pid
        else:
            return False
    if not start_token:
        return None
    return goalflight_compat.process_identity_matches(pid, start_token)


def _record_owns_path(
    record: dict[str, Any], path: str, *, ledger_index: LedgerIndex | None = None
) -> bool:
    """True when a dispatch still owns this path as its worker cwd.

    A missing state is treated as non-terminal: we did not observe the record
    settle, so we do not get to assume it did. A cwd recorded inside the
    worktree (a worker that cd'd deeper) still means the tree is in use.

    A liveness verdict (``idle_timeout``, ``worker_dead``, ``blocked``, …) is
    not proof the process is gone. Observed: a worker sat in ``idle_timeout``
    for 35 minutes while identity-live and mid-gate. If the recorded pid +
    start_token still match, the row owns the path. If the identity probe is
    indeterminate, the row also owns the path: unknown liveness is live for GC.
    """
    if not _record_cwd_matches(record, path, ledger_index=ledger_index):
        return False
    live = _identity_live(record)
    if live is True:
        return True
    if live is None:
        return True
    if _resumable_terminal_hold_reason(record) is not None:
        return True
    return _record_is_nonterminal(record)


def check_unowned(
    path: str,
    ledger_dir: Path,
    *,
    project_root: Path | None = None,
    ledger_index: LedgerIndex | None = None,
    identity_deadline: float | None = None,
) -> dict[str, str]:
    """Condition 3: no non-terminal dispatch has this path as its cwd.

    This is the condition that saves live work — see the module docstring. On
    2026-08-27 a merged-only sweep scored four ACTIVE workers' trees as
    deletable because each uncommitted branch still EQUALED main; only the
    ledger claim distinguished "merged" from "not started". The ledger is
    consulted rather than ps/pgrep because a process probe cannot see a worker
    that has been dispatched but has not spawned yet (and pgrep matches the
    searcher itself). Do not drop this check as slow, and do not let an
    unreadable ledger collapse into a green light: an unreadable record may be
    exactly the live dispatch that owns this path, so UNKNOWN retains.
    """
    if ledger_index is None:
        ledger_index = ledger_index_for_dir(ledger_dir)
    if _deadline_expired(identity_deadline):
        return _condition(
            UNKNOWN,
            "dispatch ownership identity deadline expired; retaining checkout",
        )
    records = ledger_index.records
    unreadable = ledger_index.unreadable
    if unreadable:
        return _condition(
            UNKNOWN,
            "dispatch ledger unreadable or empty ("
            + ", ".join(unreadable)
            + "); cannot prove no live dispatch owns this path",
        )
    state_unknown: list[str] = []
    for record in records:
        if _deadline_expired(identity_deadline):
            return _condition(
                UNKNOWN,
                "dispatch ownership identity deadline expired; retaining checkout",
            )
        states = _record_states(record)
        if states and not any(state == "unreadable" for state in states):
            continue
        project_match = _record_project_root_matches(
            record,
            project_root,
            identity_cache=ledger_index.repository_identities,
            deadline=identity_deadline,
        )
        if project_match is False and not _record_cwd_matches(
            record, path, ledger_index=ledger_index
        ):
            continue
        state_unknown.append(str(record.get("dispatch_id") or "<unknown>"))
    if state_unknown:
        return _condition(
            UNKNOWN,
            "dispatch ledger state unknown; "
            + _dispatch_id_summary(state_unknown)
            + "; cannot prove no live dispatch owns this path",
        )
    incomplete: list[str] = []
    for record in records:
        if _deadline_expired(identity_deadline):
            return _condition(
                UNKNOWN,
                "dispatch ownership identity deadline expired; retaining checkout",
            )
        if (
            not _record_is_nonterminal(record)
            or _record_has_usable_path(record)
            or _record_cwd_matches(record, path, ledger_index=ledger_index)
        ):
            continue
        project_match = _record_project_root_matches(
            record,
            project_root,
            identity_cache=ledger_index.repository_identities,
            deadline=identity_deadline,
        )
        if project_match is False and not _record_cwd_matches(
            record, path, ledger_index=ledger_index
        ):
            continue
        incomplete.append(str(record.get("dispatch_id") or "<unknown>"))
    if incomplete:
        return _condition(
            UNKNOWN,
            "non-terminal dispatch ledger row has no usable worker_cwd or "
            "worktree_path; "
            + _dispatch_id_summary(incomplete)
            + "; cannot prove no live dispatch owns this path",
        )
    owned = [
        record
        for record in records
        if _record_owns_path(record, path, ledger_index=ledger_index)
    ]
    if owned:
        running: list[str] = []
        identity_live: list[str] = []
        resumable: list[str] = []
        for record in owned:
            dispatch_id = str(record.get("dispatch_id") or "<unknown>")
            state = str(record.get("state") or "<none>")
            terminal = not _record_is_nonterminal(record)
            label = f"{dispatch_id} (state={state})"
            if terminal:
                hold_reason = _resumable_terminal_hold_reason(record)
                if hold_reason is not None:
                    resumable.append(f"{label}: {hold_reason}")
                else:
                    identity_live.append(label)
            else:
                running.append(label)
        parts: list[str] = []
        if running:
            parts.append(
                "non-terminal dispatch "
                + ", ".join(sorted(running))
                + " records this path as worker_cwd"
            )
        if identity_live:
            parts.append(
                "identity-live dispatch "
                + ", ".join(sorted(identity_live))
                + " still owns this path"
            )
        if resumable:
            parts.append("; ".join(sorted(resumable)))
        return _condition(NO, "; ".join(parts))
    return _condition(YES, "no non-terminal dispatch records this path")


def check_pool_unlocked(
    repo: Path,
    path: str,
    *,
    held_lock=None,
    deadline: float | None = None,
) -> dict[str, str]:
    """Include the kernel worktree lease in the ownership conjunction."""
    verdict, reason, lock_path, lock_stat = (
        goalflight_worktree_pool._registered_pool_seat_lock_info(
            path, project_root=repo, deadline=deadline
        )
    )
    if verdict == NO:
        if held_lock is not None:
            return _condition(
                UNKNOWN,
                "registered pool worktree lock disappeared while action lock was held",
            )
        return _condition(YES, "path is not a registered pool worktree")
    if verdict == UNKNOWN:
        return _condition(UNKNOWN, reason)
    if held_lock is not None:
        try:
            held_matches = goalflight_worktree_pool._lock_fd_matches_identity(
                held_lock.fileno(), lock_stat
            )
        except (OSError, ValueError):
            held_matches = False
        if not held_matches:
            return _condition(
                UNKNOWN,
                "registered pool worktree lock changed while action lock was held",
            )
        return _condition(YES, "registered pool worktree lock held for action")
    handle, error = _open_validated_pool_lock(lock_path, lock_stat)
    if error is not None:
        if error == "registered pool worktree is held by a live lease":
            return _condition(NO, error)
        return _condition(UNKNOWN, error)
    assert handle is not None
    handle.close()
    return _condition(YES, "registered pool worktree has no live kernel lease")


def _open_validated_pool_lock(
    lock_path: Path | None,
    expected_stat: os.stat_result | None,
) -> tuple[object | None, str | None]:
    """Open and hold the exact lock file used by the registration verdict."""
    if lock_path is None or expected_stat is None:
        return None, "pool worktree lock identity is unavailable"
    flags = os.O_RDWR
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd: int | None = None
    try:
        fd = goalflight_worktree_pool._open_lock_path_safely(
            lock_path,
            flags,
            expected_stat=expected_stat,
        )
    except OSError as exc:
        if fd is not None:
            os.close(fd)
        return None, f"pool worktree lock could not be opened ({exc})"
    try:
        handle = os.fdopen(fd, "r+", encoding="utf-8")
    except OSError as exc:
        os.close(fd)
        return None, f"pool worktree lock could not be opened ({exc})"
    try:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        return None, "registered pool worktree is held by a live lease"
    except OSError as exc:
        handle.close()
        return None, f"pool worktree lease could not be evaluated ({exc})"
    return handle, None


def _acquire_pool_action_lock(
    repo: Path, path: str, *, deadline: float | None = None
) -> tuple[object | None, str | None]:
    """Hold a registered pool lock across recheck, pin, and removal."""
    verdict, reason, lock_path, lock_stat = (
        goalflight_worktree_pool._registered_pool_seat_lock_info(
            path, project_root=repo, deadline=deadline
        )
    )
    if verdict == NO:
        return None, None
    if verdict != YES:
        return None, f"pool worktree action lock unavailable: {reason}"
    handle, error = _open_validated_pool_lock(lock_path, lock_stat)
    if error == "registered pool worktree is held by a live lease":
        return None, "registered pool worktree became held before action"
    return handle, error


def _acquire_read_only_action_lock(
    repo: Path,
    *,
    deadline: float | None = None,
) -> tuple[object | None, str | None]:
    """Hold the allocator's detached-checkout lock across GC recheck/removal."""
    if deadline is None:
        deadline = time.monotonic() + goalflight_worktree_pool.READ_ONLY_REAP_TIMEOUT_S
    timeout = _remaining_timeout(deadline)
    if timeout is not None and timeout <= 0:
        return None, "read-only allocation lock deadline expired; retaining checkout"
    try:
        registry_root = goalflight_worktree_pool._git_common_dir(
            repo, timeout=timeout
        )
        lock_path = registry_root / "goalflight-worktree-seat-locks" / "readonly-allocation.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        flags = os.O_RDWR | os.O_CREAT
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = goalflight_worktree_pool._open_registered_lock(
            lock_path,
            flags,
            registry_root=registry_root,
            allow_create=True,
            allow_unregistered=True,
            registration_deadline=deadline,
        )
    except goalflight_worktree_pool.WorktreeReadOnlyLockTimeout as exc:
        return None, f"read-only allocation lock deadline expired; retaining checkout ({exc})"
    except goalflight_worktree_pool.WorktreeSeatError as exc:
        if "timed out" in str(exc).lower() or _deadline_expired(deadline):
            return None, (
                "read-only allocation lock deadline expired; retaining checkout "
                f"({exc})"
            )
        return None, f"read-only allocation lock could not be evaluated ({exc})"
    except OSError as exc:
        return None, f"read-only allocation lock could not be opened ({exc})"
    handle = os.fdopen(fd, "r+", encoding="utf-8")
    try:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        goalflight_worktree_pool._adopt_exclusive_lock(
            lock_path,
            handle.fileno(),
            registry_root=registry_root,
            deadline=deadline,
        )
    except BlockingIOError:
        handle.close()
        return None, "read-only allocation is active; retry after it completes"
    except OSError as exc:
        handle.close()
        return None, f"read-only allocation lock could not be evaluated ({exc})"
    return handle, None


def check_not_current(
    path: str,
    *,
    current_checkout: str | None,
    current_error: str | None,
) -> dict[str, str]:
    """Condition 4: this is not the currently-checked-out path."""
    if current_error is not None:
        return _condition(
            UNKNOWN,
            f"current checkout could not be determined ({current_error})",
        )
    if current_checkout is not None and _same_path(path, current_checkout):
        return _condition(NO, "this path is the currently-checked-out worktree")
    return _condition(YES, "not the currently-checked-out worktree")


# --------------------------------------------------------------------------
# Classification


def classify(
    repo: Path,
    entry: dict[str, Any],
    *,
    into: str,
    ledger_dir: Path,
    main_path: str | None,
    current_checkout: str | None,
    current_error: str | None,
    pool_lock=None,
    ledger_index: LedgerIndex | None = None,
    identity_deadline: float | None = None,
    deadline: float | None = None,
) -> dict[str, Any]:
    """Evaluate one listed worktree against the full conjunction."""
    path = entry["path"]
    if identity_deadline is None:
        identity_deadline = deadline
    if deadline is not None and _deadline_expired(deadline):
        return {
            "path": path,
            "branch": entry.get("branch"),
            "detached": bool(entry.get("detached")),
            "missing_on_disk": False,
            "decision": "retain",
            "reason": "classification deadline expired; retaining checkout",
            "conditions": {},
        }
    directory_state = _presence(Path(path))
    result: dict[str, Any] = {
        "path": path,
        "branch": entry.get("branch"),
        "detached": bool(entry.get("detached")),
        "missing_on_disk": directory_state == "absent",
    }

    if main_path is not None and _same_path(path, main_path):
        result["decision"] = "retain"
        result["reason"] = "main worktree is never a removal candidate"
        result["conditions"] = {}
        return result

    project_root = Path(main_path) if main_path is not None else repo
    read_only_verdict, read_only_reason = (
        goalflight_worktree_pool.read_only_worktree_path_verdict(
            path, project_root=project_root
        )
    )
    if read_only_verdict == UNKNOWN:
        result["decision"] = "retain"
        result["reason"] = (
            "read-only path classification unknown ("
            f"{read_only_reason}); refusing removal"
        )
        result["conditions"] = {}
        return result
    if read_only_verdict == YES:
        pool_verdict, pool_reason = goalflight_worktree_pool.registered_pool_seat_verdict(
            path, project_root=project_root, deadline=deadline
        )
        if pool_verdict != NO:
            result["decision"] = "retain"
            result["reason"] = (
                "read-only checkout path is also a registered pool worktree "
                f"({pool_reason})"
            )
            result["conditions"] = {}
            return result
        usage = goalflight_worktree_pool.read_only_worktree_usage(
            path,
            ledger_dir=ledger_dir,
            project_root=project_root,
            ledger_index=ledger_index,
            identity_deadline=identity_deadline,
        )
        conditions = {
            "clean": check_clean(
                path, directory_state=directory_state, deadline=deadline
            ),
            "grace": check_read_only_grace(path, directory_state=directory_state),
            "unowned": usage,
            "not_current": check_not_current(
                path, current_checkout=current_checkout, current_error=current_error
            ),
        }
        result["read_only"] = True
        result["conditions"] = conditions
        blockers = [
            f"{name}: {cond['reason']}"
            for name, cond in conditions.items()
            if cond["verdict"] != YES
        ]
        if blockers:
            result["decision"] = "retain"
            result["reason"] = "; ".join(blockers)
            return result
        result["decision"] = "prune" if directory_state == "absent" else "remove"
        result["reason"] = (
            "read-only checkout is clean, has no live dispatch owner, and is "
            "not the current checkout"
        )
        return result

    seat_verdict, seat_reason = goalflight_worktree_pool.registered_pool_seat_verdict(
        path, project_root=repo, deadline=deadline
    )
    if seat_verdict == UNKNOWN:
        result["decision"] = "retain"
        result["reason"] = (
            "pool-worktree registration unknown ("
            f"{seat_reason}); cannot prove this path is not a maintained worktree"
        )
        result["conditions"] = {}
        pool = {"verdict": seat_verdict, "reason": seat_reason}
        result["pool_worktree"] = pool
        result["pool_seat"] = pool
        return result

    conditions = {
        "merged": check_merged(
            repo,
            entry.get("branch"),
            bool(entry.get("detached")),
            into,
            deadline=deadline,
        ),
        "clean": check_clean(
            path, directory_state=directory_state, deadline=deadline
        ),
        "unowned": check_unowned(
            path,
            ledger_dir,
            project_root=project_root,
            ledger_index=ledger_index,
            identity_deadline=identity_deadline,
        ),
        "not_current": check_not_current(
            path, current_checkout=current_checkout, current_error=current_error
        ),
    }
    if seat_verdict == YES:
        lease = check_pool_unlocked(
            repo, path, held_lock=pool_lock, deadline=deadline
        )
        if lease["verdict"] != YES:
            conditions["unowned"] = lease
        pool = {"verdict": seat_verdict, "reason": seat_reason}
        result["pool_worktree"] = pool
        result["pool_seat"] = pool
    result["conditions"] = conditions

    blockers = [
        f"{name}: {cond['reason']}"
        for name, cond in conditions.items()
        if cond["verdict"] != YES
    ]
    if blockers:
        result["decision"] = "retain"
        result["reason"] = "; ".join(blockers)
        return result

    result["decision"] = "prune" if directory_state == "absent" else "remove"
    result["reason"] = (
        "all four conditions hold: branch merged into "
        f"{into!r}, worktree clean, no non-terminal dispatch owns the path, "
        "not the current checkout"
    )
    return result


# --------------------------------------------------------------------------
# Removal


def _remove_worktree(
    repo: Path, path: str, *, deadline: float | None = None
) -> tuple[bool, str]:
    proc = _git(
        repo,
        "worktree",
        "remove",
        path,
        timeout=_remaining_timeout(deadline),
    )
    if proc.returncode == 0:
        return True, ""
    if proc.returncode == 124:
        return False, "git worktree remove deadline expired; retaining checkout"
    return False, (proc.stderr or proc.stdout).strip() or f"exit {proc.returncode}"


def _prune_worktrees(
    repo: Path, *, deadline: float | None = None
) -> tuple[bool, str]:
    proc = _git(
        repo,
        "worktree",
        "prune",
        timeout=_remaining_timeout(deadline),
    )
    if proc.returncode == 0:
        return True, ""
    if proc.returncode == 124:
        return False, "git worktree prune deadline expired; retaining checkout"
    return False, (proc.stderr or proc.stdout).strip() or f"exit {proc.returncode}"


def _pin_before_remove(
    repo: Path, path: str, *, deadline: float | None = None
) -> tuple[str | None, str | None]:
    """Keep the candidate's current commit durable before destructive removal."""
    return goalflight_worktree_pool.pin_worktree_head_before_remove(
        repo, path, deadline=deadline
    )


def apply_removals(
    repo: Path,
    entries: list[dict[str, Any]],
    *,
    into: str,
    ledger_dir: Path,
    main_path: str | None,
    current_checkout: str | None,
    current_error: str | None,
) -> None:
    """Act on decided entries, re-verifying each immediately beforehand.

    The scan is a snapshot; a branch can acquire commits, a tree can acquire
    files, and a dispatch can claim a path between listing and acting. A stale
    decision is never executed: anything no longer removable is reported as
    ``changed_before_remove`` instead.
    """
    targets = [e for e in entries if e["decision"] in {"remove", "prune"}]
    for entry in targets:
        path = entry["path"]
        deadline = time.monotonic() + goalflight_worktree_pool.READ_ONLY_REAP_TIMEOUT_S

        # Re-list: the fresh listing is the only authority on what exists NOW.
        listed, list_error = list_worktrees(repo, deadline=deadline)
        fresh = next(
            (item for item in listed if _same_path(item["path"], path)),
            None,
        )
        if list_error is not None or fresh is None:
            entry["outcome"] = "retained"
            entry["reason"] = (
                "changed_before_remove: worktree listing changed "
                f"({list_error or 'path no longer listed'})"
            )
            continue

        read_only_project_root = Path(main_path) if main_path is not None else repo
        read_only_verdict, _read_only_reason = (
            goalflight_worktree_pool.read_only_worktree_path_verdict(
                path, project_root=read_only_project_root
            )
            )
        if _deadline_expired(deadline):
            entry["outcome"] = "retained"
            entry["reason"] = (
                "changed_before_remove: removal deadline expired; retaining checkout"
            )
            continue
        if read_only_verdict in {YES, UNKNOWN}:
            pool_lock, lock_error = _acquire_read_only_action_lock(
                repo, deadline=deadline
            )
        else:
            pool_lock, lock_error = _acquire_pool_action_lock(
                repo, path, deadline=deadline
            )
        if lock_error is not None:
            entry["outcome"] = "retained"
            entry["reason"] = f"changed_before_remove: {lock_error}"
            continue
        try:
            # Pinning is idempotent and must happen before StateLock. A stalled
            # Git process therefore cannot block unrelated ledger writers.
            if fresh.get("path") and not _deadline_expired(deadline):
                if entry["decision"] == "remove":
                    keep_ref, pin_error = _pin_before_remove(
                        repo, path, deadline=deadline
                    )
                    if pin_error is not None:
                        entry["outcome"] = "retained"
                        entry["reason"] = (
                            f"changed_before_remove: keep pin retained checkout: {pin_error}"
                        )
                        continue
                    entry["keep_ref"] = keep_ref

            if _deadline_expired(deadline):
                entry["outcome"] = "retained"
                entry["reason"] = (
                    "changed_before_remove: removal deadline expired after pinning; "
                    "retaining checkout"
                )
                continue

            # The ledger lock is deliberately taken only after pinning and is
            # held for the final re-check plus the destructive Git operation.
            ledger_lock = goalflight_ledger.StateLock.try_acquire(deadline)
            if ledger_lock is None:
                entry["outcome"] = "retained"
                entry["reason"] = (
                    "changed_before_remove: ledger lock deadline expired; retaining checkout"
                )
                continue
            with ledger_lock:
                if _deadline_expired(deadline):
                    entry["outcome"] = "retained"
                    entry["reason"] = (
                        "changed_before_remove: removal deadline expired before final re-check; "
                        "retaining checkout"
                    )
                    continue
                final_ledger_index = ledger_index_for_dir(
                    ledger_dir, deadline=deadline
                )
                final_current_checkout, final_current_error = current_checkout_path(
                    repo, deadline=deadline
                )
                current = classify(
                    repo,
                    fresh,
                    into=into,
                    ledger_dir=ledger_dir,
                    main_path=main_path,
                    current_checkout=final_current_checkout,
                    current_error=final_current_error,
                    pool_lock=pool_lock,
                    ledger_index=final_ledger_index,
                    identity_deadline=deadline,
                    deadline=deadline,
                )
                if current["decision"] not in {"remove", "prune"}:
                    entry["outcome"] = "retained"
                    entry["reason"] = f"changed_before_remove: {current['reason']}"
                    continue

                # ``git worktree prune`` clears every stale administrative entry,
                # so only allow it when this is the sole stale path.
                stale: set[str] = set()
                scan_expired = False
                for item in listed:
                    if _deadline_expired(deadline):
                        scan_expired = True
                        break
                    if _presence(Path(item["path"])) == "absent":
                        stale.add(item["path"])
                if scan_expired:
                    entry["outcome"] = "retained"
                    entry["reason"] = (
                        "changed_before_remove: removal deadline expired while checking "
                        "stale entries; retaining checkout"
                    )
                    continue
                prune_allowed = stale <= {path}

                if current["decision"] == "prune":
                    if not prune_allowed:
                        entry["outcome"] = "failed"
                        entry["error"] = (
                            "git worktree prune would also clear administrative entries "
                            "that did not pass the conjunction; skipped"
                        )
                        continue
                    ok, detail = _prune_worktrees(repo, deadline=deadline)
                    if ok:
                        entry["outcome"] = "pruned"
                    elif "deadline expired" in detail:
                        entry["outcome"] = "retained"
                        entry["reason"] = detail
                    else:
                        entry["outcome"] = "failed"
                        entry["error"] = detail
                    continue

                ok, detail = _remove_worktree(repo, path, deadline=deadline)
                if ok:
                    entry["outcome"] = "removed"
                elif "deadline expired" in detail:
                    entry["outcome"] = "retained"
                    entry["reason"] = detail
                elif _presence(Path(path)) == "absent" and prune_allowed:
                    # The directory disappeared between scan and removal; reclaim
                    # the administrative entry instead of reporting an error.
                    ok, detail = _prune_worktrees(repo, deadline=deadline)
                    if ok:
                        entry["outcome"] = "pruned"
                    elif "deadline expired" in detail:
                        entry["outcome"] = "retained"
                        entry["reason"] = detail
                    else:
                        entry["outcome"] = "failed"
                        entry["error"] = detail
                else:
                    entry["outcome"] = "failed"
                    entry["error"] = detail
        finally:
            if pool_lock is not None:
                pool_lock.close()


# --------------------------------------------------------------------------
# Reporting


def _counts(entries: list[dict[str, Any]]) -> dict[str, int]:
    counts = {
        "worktrees": len(entries),
        "removable": 0,
        "retained": 0,
        "removed": 0,
        "pruned": 0,
        "failed": 0,
    }
    for entry in entries:
        if entry["decision"] == "retain":
            counts["retained"] += 1
        else:
            counts["removable"] += 1
        outcome = entry.get("outcome")
        if outcome == "removed":
            counts["removed"] += 1
        elif outcome == "pruned":
            counts["pruned"] += 1
        elif outcome == "failed":
            counts["failed"] += 1
    return counts


def format_human(repo: Path, into: str, entries: list[dict[str, Any]], *, applied: bool) -> str:
    counts = _counts(entries)
    mode = "apply" if applied else "report"
    lines = [
        f"worktree gc ({mode}): {repo}  into={into}",
        f"  worktrees : {counts['worktrees']}",
        f"  removable : {counts['removable']}",
        f"  retained  : {counts['retained']}",
    ]
    if applied:
        lines.append(
            f"  removed {counts['removed']}, pruned {counts['pruned']}, "
            f"failed {counts['failed']}"
        )
    removable = [e for e in entries if e["decision"] != "retain"]
    retained = [e for e in entries if e["decision"] == "retain"]
    if removable:
        lines.append("\n  removable worktrees:")
        for entry in removable:
            verb = entry.get("outcome") or f"would_{entry['decision']}"
            branch = entry["branch"] or "(detached)"
            lines.append(f"    {verb:<14} {entry['path']}  branch={branch}")
            if entry.get("error"):
                lines.append(f"                   error={entry['error']}")
    if retained:
        lines.append("\n  retained:")
        for entry in retained:
            branch = entry["branch"] or "(detached)"
            lines.append(f"    {entry['path']}  branch={branch}")
            lines.append(f"        why={entry['reason']}")
    if not applied and counts["removable"]:
        lines.append("\n  report only - nothing removed. Re-run with --apply to remove.")
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Report (or with --apply, remove) git worktrees that are merged, "
            "clean, unowned by a live dispatch, and not checked out. "
            "Registered pool worktrees are evaluated by the full predicate; a directory "
            "merely named wt-N is ordinary litter. Shared read-only checkouts are "
            "evaluated without a merge condition. Run after merging a worker branch "
            "into the integration branch."
        )
    )
    parser.add_argument(
        "repo",
        nargs="?",
        type=Path,
        default=Path.cwd(),
        help="Repository (or any of its worktrees) to sweep. Default: cwd.",
    )
    parser.add_argument(
        "--into",
        default="main",
        help="Integration branch for the merged check (default: main).",
    )
    parser.add_argument(
        "--ledger-dir",
        type=Path,
        default=None,
        help="Dispatch ledger runs directory "
        "(default: Goal Flight machine-state runs directory).",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually remove reclaimable worktrees. Default is report-only.",
    )
    parser.add_argument("--json", action="store_true", help="Emit JSON.")
    return parser


def terminal_dry_run(repo: Path, *, into: str = "main", ledger_dir: Path | None = None) -> dict[str, Any]:
    """Return the same report used by the CLI, without mutating the repository."""
    repo = Path(repo).resolve()
    ledger_dir = ledger_dir or goalflight_ledger.runs_dir(create=False)
    listed, list_error = list_worktrees(repo)
    if list_error is not None:
        return {"schema": SCHEMA, "repo": str(repo), "mode": "report", "error": list_error}
    main_path = main_worktree_path(repo)
    current_checkout, current_error = current_checkout_path(repo)
    ledger_index = ledger_index_for_dir(ledger_dir)
    entries = [
        classify(
            repo,
            entry,
            into=into,
            ledger_dir=ledger_dir,
            main_path=main_path,
            current_checkout=current_checkout,
            current_error=current_error,
            ledger_index=ledger_index,
        )
        for entry in listed
    ]
    return {
        "schema": SCHEMA,
        "repo": str(repo),
        "into": into,
        "ledger_dir": str(ledger_dir),
        "mode": "report",
        **_counts(entries),
        "entries": entries,
    }


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    repo = args.repo.resolve()
    ledger_dir = args.ledger_dir or goalflight_ledger.runs_dir(create=False)

    listed, list_error = list_worktrees(repo)
    if list_error is not None:
        print(f"cannot list worktrees for {repo}: {list_error}", file=sys.stderr)
        return 1
    main_path = main_worktree_path(repo)
    current_checkout, current_error = current_checkout_path(repo)
    ledger_index = ledger_index_for_dir(ledger_dir)

    entries = [
        classify(
            repo,
            entry,
            into=args.into,
            ledger_dir=ledger_dir,
            main_path=main_path,
            current_checkout=current_checkout,
            current_error=current_error,
            ledger_index=ledger_index,
        )
        for entry in listed
    ]

    if args.apply:
        apply_removals(
            repo,
            entries,
            into=args.into,
            ledger_dir=ledger_dir,
            main_path=main_path,
            current_checkout=current_checkout,
            current_error=current_error,
        )

    report = {
        "schema": SCHEMA,
        "repo": str(repo),
        "into": args.into,
        "ledger_dir": str(ledger_dir),
        "mode": "apply" if args.apply else "report",
        **_counts(entries),
        "entries": entries,
    }
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(format_human(repo, args.into, entries, applied=bool(args.apply)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
