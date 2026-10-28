#!/usr/bin/env python3
"""Select the dispatch/watcher A/B test family from review module patterns.

With the checked-in policy, a green selector output establishes that
discovered modules match the Git index plus the declared local-only modules
and reports the review-guided selection (or the full suite when review input is
unresolved). The local-only paths are a maintained declaration: green requires
every declared file to be present in this checkout, but does not prove the list
is complete or current. A green test run using that selection establishes only
that those selected modules passed; it does not prove the review patterns cover
every affected module.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence


SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
UNMAPPED_REVIEW_MARKER = "UNMAPPED"


class GatePolicyError(ValueError):
    """The checked-in deploy-gate policy is invalid or incomplete."""


@dataclass(frozen=True)
class GateSelection:
    modules: tuple[str, ...]
    revisions: tuple[str, ...]
    fallback_reason: str | None


def load_policy(repo_root: Path = REPO_ROOT) -> dict[str, Any]:
    policy_path = repo_root / "scripts" / "dispatch_watcher_gate_policy.json"
    try:
        with policy_path.open(encoding="utf-8") as stream:
            policy = json.load(stream)
    except OSError as exc:
        raise GatePolicyError(
            f"executable gate policy is missing: {policy_path}"
        ) from exc
    if not isinstance(policy, dict):
        raise GatePolicyError("gate policy must be a JSON object")
    return policy


def _module_paths(repo_root: Path, policy: dict[str, Any]) -> list[Path]:
    test_root = repo_root / str(policy["test_root"])
    pattern = str(policy["module_pattern"])
    if not test_root.is_dir():
        raise GatePolicyError(f"test root is missing: {test_root}")
    discovered = sorted(path for path in test_root.rglob(pattern) if path.is_file())

    try:
        tracked = subprocess.check_output(
            [
                "git",
                "-C",
                str(repo_root),
                "ls-files",
                "-z",
                "--",
                test_root.relative_to(repo_root).as_posix(),
            ],
            timeout=30,
        ).decode("utf-8").split("\0")
    except subprocess.TimeoutExpired as exc:
        raise GatePolicyError(
            "cannot read tracked test modules from Git index: "
            f"{repo_root} (git ls-files timed out after 30 seconds)"
        ) from exc
    except (OSError, subprocess.CalledProcessError) as exc:
        raise GatePolicyError(
            f"cannot read tracked test modules from Git index: {repo_root}"
        ) from exc

    expected = {
        path
        for path in tracked
        if path and fnmatch.fnmatchcase(Path(path).name, pattern)
    }
    required_untracked_modules = policy["required_untracked_modules"]
    if not isinstance(required_untracked_modules, list) or any(
        not isinstance(module, str) for module in required_untracked_modules
    ):
        raise GatePolicyError(
            "required_untracked_modules must be a list of module paths"
        )
    expected.update(required_untracked_modules)

    discovered_names = {
        path.relative_to(repo_root).as_posix() for path in discovered
    }
    missing = sorted(expected - discovered_names)
    unexpected = sorted(discovered_names - expected)
    if missing or unexpected:
        details = [
            f"expected {len(expected)} modules, discovered {len(discovered_names)}"
        ]
        if missing:
            details.append("missing: " + ", ".join(missing))
        if unexpected:
            details.append("unexpected: " + ", ".join(unexpected))
        if missing:
            details.append(
                "restore the listed files in this gate worktree; for local-only "
                "tests, copy tests/python/ext/ from the main checkout, or run "
                "selection in a checkout where the paths are present. Remove "
                "the copied tests/python/ext/ directory after the gate run: "
                "gitignored copies survive ordinary pool cleanup and can "
                "silently reuse stale tests."
            )
        raise GatePolicyError("module universe mismatch: " + "; ".join(details))

    return discovered


def _is_basename_pattern(pattern: str) -> bool:
    return (
        bool(pattern)
        and Path(pattern).name == pattern
        and "/" not in pattern
        and "\\" not in pattern
    )


def _fallback_reason(
    review_patterns: Sequence[str] | None,
    module_paths: list[Path],
) -> str | None:
    if review_patterns is None:
        return "missing"
    if not review_patterns:
        return "empty"
    if all(isinstance(item, str) and not item.strip() for item in review_patterns):
        return "empty"
    if UNMAPPED_REVIEW_MARKER in review_patterns:
        return "unmapped"

    module_names = [path.name for path in module_paths]
    for pattern in review_patterns:
        if not isinstance(pattern, str) or not _is_basename_pattern(pattern):
            return "ambiguous"
        if not any(fnmatch.fnmatchcase(name, pattern) for name in module_names):
            return "ambiguous"
    return None


def select_gate(
    repo_root: Path = REPO_ROOT,
    review_patterns: Sequence[str] | None = None,
    *,
    policy: dict[str, Any] | None = None,
) -> GateSelection:
    """Select the policy union or widen unresolved review input to all modules.

    A review pattern is ambiguous when it is not a basename-only glob or when
    it matches no discovered ``test_*.py`` module. The explicit ``UNMAPPED``
    review marker also widens the whole selection when a group could not be
    mapped to a test module.
    """
    active_policy = load_policy(repo_root) if policy is None else policy
    module_paths = _module_paths(repo_root, active_policy)
    modules = {
        path.relative_to(repo_root).as_posix(): path.name for path in module_paths
    }
    all_modules = set(modules)
    reason = _fallback_reason(review_patterns, module_paths)

    fallback = active_policy["fallback"]
    if reason is not None:
        if reason not in fallback["on"]:
            raise GatePolicyError(
                f"fallback policy does not cover {reason!r} review input"
            )
        if fallback["module_set"] != "all":
            raise GatePolicyError("fallback module_set must be 'all'")
        selected = all_modules
    else:
        if active_policy["selection_operator"] != "union":
            raise GatePolicyError("selection_operator must be 'union'")
        prefix_patterns = active_policy["prefix_patterns"]
        selected = {
            path
            for path, name in modules.items()
            if any(fnmatch.fnmatchcase(name, pattern) for pattern in prefix_patterns)
        }
        for review_pattern in review_patterns or ():
            selected.update(
                path
                for path, name in modules.items()
                if fnmatch.fnmatchcase(name, review_pattern)
            )

    revisions = active_policy["revision_targets"]
    if not isinstance(revisions, list) or any(
        not isinstance(item, str) for item in revisions
    ):
        raise GatePolicyError("revision_targets must be a list of revision names")
    return GateSelection(
        modules=tuple(sorted(selected)),
        revisions=tuple(revisions),
        fallback_reason=reason,
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--review-pattern",
        action="append",
        help="basename glob for affected tests/python test modules; repeat per pattern",
    )
    parser.add_argument("--repo-root", type=Path, default=REPO_ROOT)
    args = parser.parse_args(argv)
    try:
        selection = select_gate(
            args.repo_root,
            args.review_pattern,
        )
    except (OSError, KeyError, TypeError, json.JSONDecodeError, GatePolicyError) as exc:
        parser.error(str(exc))
    print(
        json.dumps(
            {
                "modules": selection.modules,
                "revisions": selection.revisions,
                "fallback_reason": selection.fallback_reason,
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
