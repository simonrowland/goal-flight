#!/usr/bin/env python3
"""A dispatched worker must be able to SEND mail, not only be scraped.

Before this, a worker's only route to a controller was a marker in its console
log that the watcher happened to recognise. That makes every message a side
effect of stdout: it cannot be addressed to anyone, cannot carry a type, and
arrives only if the scrape matches. Controllers responded the way people always
do when the real channel does not work -- they invented disposable side-channel
files and then forgot to read them.

Two things make the sideband real: the worker is told its own dispatch id, and
the messages tree is writable from inside the sandbox. Either one missing and
the channel does not exist.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT))

import goalflight_codex_sandbox as codex_sandbox  # noqa: E402
import goalflight_messages as gm  # noqa: E402
import goalflight_os_sandbox as sandbox  # noqa: E402


def assert_true(name: str, condition: bool) -> None:
    if not condition:
        raise AssertionError(name)


def _writable(roots: list[str], probe: str) -> bool:
    return any(probe == r or probe.startswith(r.rstrip("/") + "/") for r in roots)


def case_messages_tree_is_writable_from_inside_the_sandbox() -> None:
    if sandbox.os_sandbox_platform_key() != "darwin":
        print("SKIP: macOS sandbox profiles only")
        return
    roots = sandbox.macos_write_roots(
        str(ROOT), sandbox.OS_SANDBOX_WORKSPACE_WRITE, agent="grok-code", command="grok"
    )
    assert_true(
        "a worker can write the messages tree",
        _writable(roots, str(gm.default_messages_dir())),
    )


def case_worker_still_cannot_author_fleet_state() -> None:
    """The grant is a channel, not an opening.

    The fleet directory holds the registry and the derived aggregate: state a
    worker consumes but must never author. Granting the whole goal-flight root
    would have been one character shorter and would have let a worker rewrite
    the very records the controller uses to judge it.
    """
    if sandbox.os_sandbox_platform_key() != "darwin":
        print("SKIP: macOS sandbox profiles only")
        return
    roots = sandbox.macos_write_roots(
        str(ROOT), sandbox.OS_SANDBOX_WORKSPACE_WRITE, agent="grok-code", command="grok"
    )
    assert_true(
        "fleet state stays denied",
        not _writable(roots, str(gm.default_fleet_dir())),
    )
    assert_true(
        "the rest of HOME stays denied",
        not _writable(roots, str(Path.home() / "Documents")),
    )


def case_worker_can_write_the_task_store_it_is_told_to_capture_into() -> None:
    """The worker contract says capture out-of-scope findings; the sandbox must allow it.

    The store moved out of the repo to the durable state home and the grant did
    not follow, so `goalflight_task.py capture` failed with a bare "Operation
    not permitted" -- a documented capability the sandbox made impossible, the
    same shape as the review that could never run.

    Asserted against the resolver the store actually writes through, not a
    literal path: a grant naming a directory nobody writes is how this broke.
    """
    import goalflight_task as gt  # noqa: PLC0415  (repo root, not the scripts path above)

    store = gt.resolve_task_store_dir(ROOT)
    for roots, label in (
        (codex_sandbox.worker_channel_roots(), "codex --sandbox"),
        (sandbox.goalflight_worker_channel_roots(), "seatbelt"),
    ):
        assert_true(f"{label} reaches the project's task store", _writable(roots, str(store)))


def case_the_grant_stops_at_the_task_stores_parent() -> None:
    """Granting the whole state base would hand a worker every other project.

    That base also holds `projects.json`, the cross-project index, and
    `setup-backups/`, which setup owns. A worker captures a task; it does not
    rewrite the registry or another repo's backlog, and a grant one directory
    too high quietly permits both.
    """
    import goalflight_task as gt  # noqa: PLC0415

    base = gt.resolve_state_base_dir().resolve()
    for roots, label in (
        (codex_sandbox.worker_channel_roots(), "codex --sandbox"),
        (sandbox.goalflight_worker_channel_roots(), "seatbelt"),
    ):
        resolved = {Path(r).resolve() for r in roots}
        assert_true(f"{label} does not grant the state base", base not in resolved)
        assert_true(
            f"{label} does not grant the cross-project index directory",
            not _writable(roots, str(base / "projects.json")),
        )
        assert_true(
            f"{label} does not grant setup backups",
            not _writable(roots, str(base / "setup-backups")),
        )


def case_grant_follows_the_store_override_not_just_xdg() -> None:
    """`$GOALFLIGHT_TASK_STORE_DIR` moves the store; the grant must move with it.

    `goalflight_task.resolve_state_base_dir()` checks the override FIRST and
    only then falls back to `$XDG_STATE_HOME`. A grant that mirrored the
    fallback alone would name a directory nobody writes whenever the override is
    set -- a value asserted to match a write path it never measured, which is
    the class this whole fix belongs to.
    """
    import goalflight_task as gt  # noqa: PLC0415

    # Both variables are exercised, but they are not peers: the override wins,
    # so the XDG case only means anything with the override cleared. The gate
    # harness sets GOALFLIGHT_TASK_STORE_DIR to isolate tests, and leaving it in
    # place made the XDG case assert a redirect that correctly never happened.
    for var in ("GOALFLIGHT_TASK_STORE_DIR", "XDG_STATE_HOME"):
        with tempfile.TemporaryDirectory() as td:
            saved = {k: os.environ.get(k) for k in ("GOALFLIGHT_TASK_STORE_DIR", "XDG_STATE_HOME")}
            os.environ.pop("GOALFLIGHT_TASK_STORE_DIR", None)
            os.environ[var] = td
            try:
                granted = codex_sandbox.worker_channel_roots()
                store = gt.resolve_task_store_dir(ROOT)
            finally:
                for k, v in saved.items():
                    if v is None:
                        os.environ.pop(k, None)
                    else:
                        os.environ[k] = v
            # Compare on the canonical form: resolve_task_store_dir() resolves,
            # and macOS reports the temp dir as /var/... while canonicalising it
            # to /private/var/...
            canonical = str(Path(td).resolve())
            assert_true(f"{var}: the store follows the redirect", str(store).startswith(canonical))
            assert_true(f"{var}: and so does the grant", _writable(granted, str(store)))
            assert_true(
                f"{var}: the operator's real store is not granted to a redirected run",
                not any(
                    Path(r).resolve() == (Path.home() / ".local" / "state" / "goal-flight" / "task-stores").resolve()
                    for r in granted
                ),
            )


def case_dispatch_tells_the_worker_its_own_id() -> None:
    """Without this the worker has nothing to address a message FROM.

    Checked against the dispatch source rather than a live launch: the launch
    path needs capacity, a real agent binary and a lease, none of which belong
    in a hermetic test. What must not regress is that the variable is set at
    all -- a worker that does not know its own id silently falls back to
    scraped markers, which is the failure this replaced.
    """
    source = (ROOT / "scripts" / "goalflight_dispatch.py").read_text(encoding="utf-8")
    assert_true(
        "worker env carries GOALFLIGHT_DISPATCH_ID",
        'env["GOALFLIGHT_DISPATCH_ID"]' in source,
    )


def case_a_worker_posting_mail_reaches_the_controller_summary() -> None:
    """End to end: a worker posts, a controller sees it.

    Uses the real post/read path against an isolated messages dir, so a change
    that keeps the envelope writable but drops it from the controller's view
    still fails here.
    """
    with tempfile.TemporaryDirectory() as td:
        messages_dir = Path(td) / "messages"
        messages_dir.mkdir(parents=True)
        env = dict(os.environ)
        env["GOALFLIGHT_DISPATCH_ID"] = "worker-under-test"
        proc = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts" / "goalflight_messages.py"),
                "--messages-dir", str(messages_dir),
                "post",
                "--dispatch-id", "worker-under-test",
                "--type", "controller-question",
                "--text", "the fixture disagrees with the spec; which is authoritative?",
            ],
            capture_output=True,
            text=True,
            env=env,
        )
        assert_true(f"worker post succeeds (rc={proc.returncode}) {proc.stderr[:200]}", proc.returncode == 0)

        read = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts" / "goalflight_messages.py"),
                "--messages-dir", str(messages_dir),
                "read", "--dispatch-id", "worker-under-test",
            ],
            capture_output=True,
            text=True,
        )
        assert_true("the envelope reads back", read.returncode == 0)
        envelopes = json.loads(read.stdout)
        assert_true("exactly one envelope", len(envelopes) == 1)
        assert_true(
            "the worker's words survive verbatim",
            "which is authoritative?" in envelopes[0]["payload"]["text"],
        )
        assert_true(
            "and it is typed, not an untyped log line",
            envelopes[0]["type"] == "controller-question",
        )


def main() -> None:
    case_messages_tree_is_writable_from_inside_the_sandbox()
    case_worker_still_cannot_author_fleet_state()
    case_worker_can_write_the_task_store_it_is_told_to_capture_into()
    case_the_grant_stops_at_the_task_stores_parent()
    case_grant_follows_the_store_override_not_just_xdg()
    case_dispatch_tells_the_worker_its_own_id()
    case_a_worker_posting_mail_reaches_the_controller_summary()
    print("OK: worker mail sideband tests pass")


if __name__ == "__main__":
    main()
