#!/usr/bin/env python3
"""Ordered end-to-end sanity probe of the wake/events chain.

Runs the main events in the order they occur in a controller session's life,
each against a HERMETIC temp project (isolated env dirs; the live journal,
ledger, and registry are never touched). Prints one PASS/FAIL line per stage
with measured latency where the stage has one.

The chain stops at "listener process exits on the event" — that is the
harness boundary: the final hop (a host re-invoking its controller on the
tracked task's exit) can only be tested from inside a live session.

Usage: python3 scripts/goalflight_events_smoke.py [--keep]
Exit: 0 all stages pass, 1 otherwise.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))

RESULTS: list[tuple[str, bool, str]] = []


def stage(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  [{detail}]" if detail else ""))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--keep", action="store_true", help="keep the temp root")
    args = parser.parse_args()

    temp_root = Path(tempfile.mkdtemp(prefix="gf-events-smoke-"))
    project_root = temp_root / "project"
    project_root.mkdir()
    env = {
        **os.environ,
        "GOALFLIGHT_JOURNAL_DIR": str(temp_root / "journals"),
        "GOALFLIGHT_STATE_DIR": str(temp_root / "state"),
        "GOALFLIGHT_WAKE_LEDGER_DIR": str(temp_root / "wake-ledger"),
        "GOALFLIGHT_MESSAGES_DIR": str(temp_root / "messages"),
        "GOALFLIGHT_TASK_STORE_DIR": str(temp_root / "task-store"),
        "GOAL_FLIGHT_PIDFILE_DIR": str(temp_root / "pids"),
        "GOALFLIGHT_CAPACITY_CONF": os.devnull,
    }
    for key in ("GOALFLIGHT_JOURNAL_DIR", "GOALFLIGHT_STATE_DIR",
                "GOALFLIGHT_WAKE_LEDGER_DIR", "GOALFLIGHT_MESSAGES_DIR",
                "GOALFLIGHT_TASK_STORE_DIR", "GOAL_FLIGHT_PIDFILE_DIR"):
        Path(env[key]).mkdir(parents=True, exist_ok=True)
    # A live controller's ambient identity must never cross-talk into the
    # hermetic project — relay/advance resolve their controller from these.
    env.pop("GOALFLIGHT_CONTROLLER_LABEL", None)
    env.pop("GOALFLIGHT_CONTROLLER_LEASE_NONCE", None)
    os.environ.update(env)  # module-level probes below use the same isolation
    os.environ.pop("GOALFLIGHT_CONTROLLER_LABEL", None)
    os.environ.pop("GOALFLIGHT_CONTROLLER_LEASE_NONCE", None)

    import goalflight_journal as journal
    import goalflight_wake as wake
    import goalflight_messages as msgs
    import goalflight_fleet_console as console

    label = "smoke"
    # listen-auto is what production controllers arm; the bare `listen`
    # blocks on the journal cursor alone and misses carrier-projected
    # worker terminals until an ingesting entry runs.
    listener_cmd = [
        sys.executable, str(SCRIPTS / "goalflight_messages.py"), "listen-auto",
        "--project-root", str(project_root), "--controller-label", label,
    ]

    def spawn_listener(nonce: str) -> subprocess.Popen:
        listener_env = {**env,
                        "GOALFLIGHT_CONTROLLER_LABEL": label,
                        "GOALFLIGHT_CONTROLLER_LEASE_NONCE": nonce}
        return subprocess.Popen(
            listener_cmd,
            env=listener_env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )

    def wait_exit(proc: subprocess.Popen, timeout_s: float) -> float | None:
        t0 = time.monotonic()
        try:
            proc.wait(timeout=timeout_s)
            return time.monotonic() - t0
        except subprocess.TimeoutExpired:
            # A leaked blocking listener holds a live waiter flock and would
            # falsify every later coverage-sensitive stage.
            proc.kill()
            proc.wait()
            return None

    # S1 — session start: claim a lease, hold the kernel lock.
    authority = journal.open_or_create_journal(project_root)
    claimed = authority.claim_or_renew_lease(label, principal={"principal_id": "smoke"})
    lease = claimed.value
    holder = wake.register_lease_holder(
        project_root, controller_label=label, lease_nonce=lease.nonce
    )
    alive = wake.lease_holder_alive(
        project_root, controller_label=label, lease_nonce=lease.nonce
    )
    stage("S1 claim: lease committed + kernel lock held", bool(claimed.committed) and alive is True)

    # S2 — arm: a listener registers in the flock ledger; coverage sees it.
    with wake.register_waiter(project_root, controller_label=label, kind="listener"):
        cov = wake.coverage_status(project_root, controller_label=label)
        stage("S2 arm: waiter flock counted as coverage", cov.get("covered") is True,
              cov.get("reason", ""))
    cov = wake.coverage_status(project_root, controller_label=label)
    stage("S3 release: dropped flock ends coverage", cov.get("covered") is False,
          cov.get("reason", ""))

    # S4 — self-mail wakes a blocking listener (the dogfood ring).
    proc = spawn_listener(lease.nonce)
    time.sleep(1.0)  # listener reaches its blocking wait
    t_post = time.monotonic()
    subprocess.run(
        [sys.executable, str(SCRIPTS / "goalflight_messages.py"), "post",
         "--to-controller", label, "--dispatch-id", "smoke-self",
         "--type", "controller-notice", "--text", "ping"],
        env=env, cwd=project_root, capture_output=True,
    )
    latency = wait_exit(proc, 30)
    stage("S4 self-mail rings the doorbell",
          latency is not None and proc.returncode == 0,
          f"{(time.monotonic() - t_post):.2f}s post->exit" if latency is not None else "no exit")

    # S5 — peek shows the item; CAS advance drains it; re-peek is empty.
    # relay has no label flag: identity arrives ambiently, the way a live
    # controller session provides it.
    peek_env = {**env,
                "GOALFLIGHT_CONTROLLER_LABEL": label,
                "GOALFLIGHT_CONTROLLER_LEASE_NONCE": lease.nonce}
    env = peek_env  # every later stage keeps the smoke identity
    peek = subprocess.run(
        [sys.executable, str(SCRIPTS / "goalflight_messages.py"), "relay",
         "--new", "--json"], env=env, cwd=project_root, capture_output=True, text=True,
    )
    d = json.loads(peek.stdout or "{}")
    items = d.get("items", [])
    adv_ok = False
    if d.get("advance_command") and items:
        import shlex
        adv = subprocess.run(shlex.split(d["advance_command"]), env=env,
                             capture_output=True, text=True)
        adv_ok = adv.returncode == 0
    peek2 = subprocess.run(peek.args, env=env, cwd=project_root,
                           capture_output=True, text=True)
    d2 = json.loads(peek2.stdout or "{}")
    stage("S5 peek -> CAS advance -> drained",
          len(items) == 1 and adv_ok and not d2.get("items"),
          f"items={len(items)} redrain={len(d2.get('items', []))}")

    # S6 — a worker terminal (journal-level, as a watcher records it) rings
    # a re-armed listener: the worker-finish wake without the process zoo.
    proc = spawn_listener(lease.nonce)
    time.sleep(1.0)
    prepared = authority.prepare_attempt("smoke-worker")
    attempt = prepared.value
    t_term = time.monotonic()
    authority.commit_terminal(
        attempt.attempt_id, terminal_state="complete",
        observation={"state": "complete", "outcome": {"reason": "marker:COMPLETE"}},
    )
    # commit_terminal writes the outbox row; the watcher's pipeline then
    # projects outbox rows onto the carrier, which is what rings doorbells.
    authority.project_terminal_outbox(messages_dir=Path(env["GOALFLIGHT_MESSAGES_DIR"]))
    latency = wait_exit(proc, 30)
    stage("S6 worker terminal rings the doorbell",
          latency is not None and proc.returncode == 0,
          f"{(time.monotonic() - t_term):.2f}s terminal->exit" if latency is not None else "no exit")

    # S7 — entry hint: exposure with zero coverage produces the actionable
    # one-line reminder; zero exposure stays silent.
    import io
    quiet = io.StringIO()
    silent = msgs.emit_listener_reminder(
        project_root=project_root, controller_label=label, exposure=0, stream=quiet)
    loud = msgs.emit_listener_reminder(
        project_root=project_root, controller_label=label, exposure=1, stream=io.StringIO())
    stage("S7 entry hint: silent at exposure=0, actionable at exposure>0",
          silent is None and loud is not None and "listen" in (loud or ""),
          (loud or "")[:60])

    # S8 — console classification tracks the session's own lifecycle.
    n_alive = console.classify_controller(True, 1, 0)
    n_wait = console.classify_controller(True, 0, 0)
    holder.close()
    released = wake.lease_holder_alive(
        project_root, controller_label=label, lease_nonce=lease.nonce)
    n_dead = console.classify_controller(released, 0, 0)
    stage("S8 classify: ALIVE -> WAITING-ON-USER -> DEAD across real lock lifecycle",
          (n_alive, n_wait, n_dead) == ("ALIVE", "WAITING-ON-USER", "DEAD"),
          f"{n_alive}/{n_wait}/{n_dead}")

    if not args.keep:
        shutil.rmtree(temp_root, ignore_errors=True)
    failed = [name for name, ok, _ in RESULTS if not ok]
    print(f"\n{'ALL PASS' if not failed else 'FAILED: ' + ', '.join(failed)}"
          f"  ({len(RESULTS) - len(failed)}/{len(RESULTS)})")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
