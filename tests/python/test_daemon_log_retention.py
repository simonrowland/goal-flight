"""Exercise retention through the rendered launchd command, with real open FDs."""

from __future__ import annotations

import json
import os
import plistlib
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
CAP = 16 * 1024 * 1024
DAY_NS = 24 * 60 * 60 * 1_000_000_000
LOG_NAMES = (
    "drain-launchd.log",
    "fleet-console-fleet-launchd.log",
    "fleet-console-attention-launchd.log",
    "codex-seatd.log",
    "grok-seatd.log",
    "codex-rotate.log",
)
@pytest.fixture
def launch_tick(tmp_path, request):
    if sys.platform != "darwin":
        pytest.skip("macOS newsyslog")
    # Render the production plist, replacing only the drain payload. No launchctl,
    # installed plists, real HOME, dispatches or shared-state writes are involved.
    env = dict(os.environ, HOME=str(tmp_path), SKILL_ROOT=str(ROOT))
    env.update(
        {
            "GOALFLIGHT_STATE_DIR": str(tmp_path / "machine-state"),
            "GOALFLIGHT_CODEX_STATE_DIR": str(tmp_path / ".goal-flight"),
            "GOALFLIGHT_TASK_STORE_DIR": str(tmp_path / "task-state"),
            "GOAL_FLIGHT_PIDFILE_DIR": str(tmp_path / "pidfiles"),
            "GOALFLIGHT_CAPACITY_CONF": "/dev/null",
            "GOALFLIGHT_JOURNAL_DIR": str(tmp_path / "journals"),
            "GOALFLIGHT_MESSAGES_DIR": str(tmp_path / "messages"),
            "GOALFLIGHT_WAKE_LEDGER": str(tmp_path / "wake-ledger"),
        }
    )
    env.pop("GOALFLIGHT_DRAIN_LOG", None)
    if hasattr(request, "param"):
        env["GOALFLIGHT_DRAIN_LOG"] = str(tmp_path / ".goal-flight" / request.param)
    result = subprocess.run(
        ["bash", str(ROOT / "scripts/install-drainer.sh"), "--dry-run"],
        env=env, capture_output=True, text=True, check=True,
    )
    argv = plistlib.loads(result.stdout.encode())["ProgramArguments"]
    payload = argv.index(str(ROOT / "scripts/goalflight_dispatch.py"))
    command = argv[:payload] + ["-c", "import sys; print('tick-finished'); sys.exit(7)"]
    log_dir = tmp_path / ".goal-flight"
    log_dir.mkdir()

    def tick():
        proc = subprocess.run(command, env=env, capture_output=True, text=True, timeout=15)
        assert proc.returncode == 7, proc.stderr
        assert proc.stdout == "tick-finished\n", proc.stdout
        assert not proc.stderr, proc.stderr

    return log_dir, tick


@pytest.mark.parametrize("name", LOG_NAMES)
def test_past_cap_rotates_without_dropping_recent_bytes(launch_tick, name):
    log_dir, tick = launch_tick
    path = log_dir / name
    content = b"x" * CAP + b"\nlatest-before-rotation\n"
    path.write_bytes(content)
    inode = path.stat().st_ino
    tick()
    archive = path.with_name(path.name + ".0")
    assert archive.is_file(), "past-cap log was not rotated"
    assert archive.stat().st_ino == inode, "rotation must preserve the live inode"
    assert archive.read_bytes() == content, "rotation lost the newest content"
    assert path.stat().st_size == 0


def test_under_cap_is_untouched(launch_tick):
    log_dir, tick = launch_tick
    path = log_dir / LOG_NAMES[0]
    # newsyslog measures allocated KiB, so use a file well below the threshold
    # rather than depending on the filesystem's allocation rounding at 16 MiB.
    content = b"x" * (4 * 1024 * 1024)
    path.write_bytes(content)
    before = path.stat()
    tick()
    assert path.read_bytes() == content
    after = path.stat()
    assert (after.st_ino, after.st_mtime_ns) == (before.st_ino, before.st_mtime_ns)
    assert set(log_dir.iterdir()) == {
        path, log_dir / "dispatch-retention.log",
    }, "under-cap logs must not rotate"


@pytest.mark.parametrize("append", [True, False])
def test_open_writer_and_tail_keep_receiving_lines(launch_tick, append):
    log_dir, tick = launch_tick
    path = log_dir / LOG_NAMES[0]
    path.write_bytes(b"x" * CAP + b"\nbefore\n")
    flags = os.O_WRONLY | (os.O_APPEND if append else 0)
    fd = os.open(path, flags)
    try:
        # Include a non-O_APPEND descriptor: rename must not depend on resetting
        # a writer's offset after truncation. The real launchd writer uses append.
        os.lseek(fd, 0, os.SEEK_END)
        with path.open("rb") as tail:
            tail.seek(0, os.SEEK_END)
            tick()
            os.write(fd, b"after-rotation\n")
            assert tail.read() == b"after-rotation\n", "open tail lost subsequent lines"
            archive = path.with_name(path.name + ".0")
            assert archive.is_file(), "open writer was not retained in an archive"
            assert archive.read_bytes().endswith(b"\nbefore\nafter-rotation\n")
            assert archive.stat().st_size == CAP + len(b"\nbefore\nafter-rotation\n")
            with path.open("ab") as next_tick:
                next_tick.write(b"next-invocation\n")
            assert path.read_bytes() == b"next-invocation\n"
    finally:
        os.close(fd)


def test_archive_count_is_bounded(launch_tick):
    log_dir, tick = launch_tick
    path = log_dir / LOG_NAMES[0]
    for generation in range(4):
        path.write_bytes(b"x" * CAP + f"\ngeneration-{generation}\n".encode())
        tick()
    assert sorted(p.name for p in log_dir.iterdir()) == [
        "dispatch-retention.log", path.name, path.name + ".0", path.name + ".1",
    ]
    assert path.with_name(path.name + ".0").read_bytes().endswith(b"generation-3\n")
    assert path.with_name(path.name + ".1").read_bytes().endswith(b"generation-2\n")


@pytest.mark.parametrize("launch_tick", ["custom drain.log"], indirect=True)
def test_existing_drain_log_override_is_preserved(launch_tick):
    log_dir, tick = launch_tick
    path = log_dir / "custom drain.log"
    path.write_bytes(b"x" * CAP + b"\nlatest\n")
    tick()
    assert path.with_name(path.name + ".0").read_bytes().endswith(b"\nlatest\n")
    assert not (log_dir / LOG_NAMES[0]).exists()


def test_retention_failure_reports_error_and_still_executes_tick(tmp_path, monkeypatch, capsys):
    sys.path.insert(0, str(ROOT / "scripts"))
    import goalflight_daemon_logs as logs

    monkeypatch.setenv("GOALFLIGHT_STATE_DIR", str(tmp_path / "machine-state"))
    keepalive_calls = []

    def fail_keepalive():
        keepalive_calls.append(True)
        raise OSError("injected keep-alive failure")

    monkeypatch.setattr(logs, "_keepalive_state_atime", fail_keepalive)

    def fail(*_args, **_kwargs):
        raise subprocess.CalledProcessError(1, "/usr/sbin/newsyslog")

    executed = []
    calls = []

    def record_spawn(command, **kwargs):
        calls.append(("sweep", command, kwargs))

    def record_failure(command, **kwargs):
        calls.append(("rotate", command, kwargs))
        return fail(command, **kwargs)

    monkeypatch.setattr(logs.subprocess, "run", record_failure)
    monkeypatch.setattr(logs.subprocess, "Popen", record_spawn)
    monkeypatch.setattr(logs.os, "execv", lambda *args: executed.append(args))
    drain_log = tmp_path / "drain.log"
    drain_log.touch()
    logs.main([str(drain_log), "/usr/bin/python3", "drain.py", "--cross-project", "--json"])
    assert keepalive_calls == [True]
    assert executed == [("/usr/bin/python3", ["/usr/bin/python3", "drain.py", "--cross-project", "--json"])]
    assert [call[0] for call in calls] == ["sweep", "rotate"]
    assert calls[0][1][-2:] == [str(ROOT / "scripts/goalflight_reap_dispatch_homes.py"), "--delete"]
    assert calls[0][2]["start_new_session"] is True
    assert calls[0][2]["stdin"] is subprocess.DEVNULL
    assert calls[1][1][0] == "/usr/sbin/newsyslog"
    error_output = capsys.readouterr().err
    assert "state atime keep-alive failed: injected keep-alive failure" in error_output
    assert "daemon log retention failed" in error_output


def test_symlinked_drain_log_does_not_skip_sweep(tmp_path, monkeypatch):
    sys.path.insert(0, str(ROOT / "scripts"))
    import goalflight_daemon_logs as logs

    monkeypatch.setenv("GOALFLIGHT_STATE_DIR", str(tmp_path / "machine-state"))

    target = tmp_path / "shared.log"
    target.touch()
    drain_log = tmp_path / "drain.log"
    drain_log.symlink_to(target)
    calls = []
    monkeypatch.setattr(logs.subprocess, "Popen", lambda command, **kw: calls.append((command, kw)))
    monkeypatch.setattr(logs.subprocess, "run", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(logs.os, "execv", lambda *_args: None)

    logs.main([str(drain_log), "/usr/bin/python3", "drain.py"])

    assert len(calls) == 1
    assert Path(calls[0][1]["stdout"].name) == tmp_path / "dispatch-retention.log"
    assert calls[0][1]["stderr"] is calls[0][1]["stdout"]
    assert calls[0][1]["start_new_session"] is True


def test_state_atime_keepalive_preserves_mtime_and_walks_once_per_day(tmp_path, monkeypatch):
    sys.path.insert(0, str(ROOT / "scripts"))
    import goalflight_daemon_logs as logs

    state = tmp_path / "machine-state"
    files = [
        state / "runs.d" / "run.json",
        state / "remote-ci" / "projects" / "pm2" / "state" / "daemon.pid",
        state / "remote-ci" / "projects" / "pm2" / "state" / "daemon.lock",
    ]
    for path in files:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("state", encoding="utf-8")
    monkeypatch.setenv("GOALFLIGHT_STATE_DIR", str(state))

    now_ns = time.time_ns()
    old_ns = now_ns - 5 * DAY_NS
    for path in files:
        os.utime(path, ns=(old_ns, old_ns))
    original_mtimes = {path: path.lstat().st_mtime_ns for path in files}

    logs._keepalive_state_atime()

    for path in files:
        after = path.lstat()
        assert after.st_atime_ns >= now_ns - 1_000_000_000
        assert after.st_mtime_ns == original_mtimes[path]
    marker = state / logs._STATE_ATIME_MARKER
    assert marker.is_file()

    scans = []
    real_scandir = logs.os.scandir

    def count_scandir(path):
        scans.append(path)
        return real_scandir(path)

    monkeypatch.setattr(logs.os, "scandir", count_scandir)
    os.utime(files[0], ns=(old_ns, original_mtimes[files[0]]))
    logs._keepalive_state_atime()

    assert scans == [], "a same-day call must use the marker instead of walking state"
    assert files[0].lstat().st_atime_ns == old_ns
    assert files[0].lstat().st_mtime_ns == original_mtimes[files[0]]
    assert marker.read_bytes() == b"", "the marker stores its sweep time only in mtime"


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores mode 000, so the directory stays readable")
def test_state_atime_keepalive_records_unreadable_directory_and_throttles(
    tmp_path, monkeypatch, capsys,
):
    sys.path.insert(0, str(ROOT / "scripts"))
    import goalflight_daemon_logs as logs

    state = tmp_path / "machine-state"
    state.mkdir()
    retained = state / "record.json"
    retained.write_text("state", encoding="utf-8")
    unreadable = state / "unreadable"
    unreadable.mkdir()
    hidden = unreadable / "hidden.json"
    hidden.write_text("state", encoding="utf-8")
    old_ns = time.time_ns() - 5 * DAY_NS
    for path in (retained, hidden):
        os.utime(path, ns=(old_ns, old_ns))
    os.chmod(unreadable, 0)
    monkeypatch.setenv("GOALFLIGHT_STATE_DIR", str(state))

    calls = []
    real_set_atime = logs._set_atime

    def count_set_atime(path, now_ns, *, dir_fd=-2):
        calls.append(path)
        return real_set_atime(path, now_ns, dir_fd=dir_fd)

    monkeypatch.setattr(logs, "_set_atime", count_set_atime)
    try:
        logs._keepalive_state_atime()
        first_error = capsys.readouterr().err
        os.chmod(unreadable, 0o700)

        assert (state / logs._STATE_ATIME_MARKER).is_file()
        assert retained.lstat().st_atime_ns > old_ns
        assert hidden.lstat().st_atime_ns == old_ns
        assert len(calls) == 1
        assert first_error.count("state atime keep-alive could not refresh or descend 1 entries") == 1
        assert "unreadable (EACCES)" in first_error

        logs._keepalive_state_atime()
        assert capsys.readouterr().err == ""
        assert len(calls) == 1, "a same-day call must honor the marker after partial failures"
    finally:
        os.chmod(unreadable, 0o700)


def _simulate_tmp_cleaner(root: Path, now_ns: int) -> list[Path]:
    """Apply tmp_cleaner's file and empty-directory predicates to a test tree."""
    removed: list[Path] = []

    def older_than_four_days(timestamp_ns: int) -> bool:
        # The installed-find measurement below shows +3 matches at four full days.
        return now_ns - timestamp_ns >= 4 * DAY_NS

    for directory_name, directory_names, filenames in os.walk(
        root, topdown=False, followlinks=False,
    ):
        directory = Path(directory_name)
        for filename in filenames:
            path = directory / filename
            try:
                info = path.lstat()
            except FileNotFoundError:
                continue
            if (stat.S_ISREG(info.st_mode)
                    and older_than_four_days(info.st_atime_ns)
                    and older_than_four_days(info.st_mtime_ns)
                    and older_than_four_days(info.st_ctime_ns)):
                path.unlink()
                removed.append(path)
        if directory == root:
            continue
        for dirname in directory_names:
            path = directory / dirname
            try:
                info = path.lstat()
            except FileNotFoundError:
                continue
            if (stat.S_ISDIR(info.st_mode)
                    and not stat.S_ISLNK(info.st_mode)
                    and older_than_four_days(info.st_mtime_ns)):
                try:
                    path.rmdir()
                except OSError:
                    continue
                removed.append(path)
    return removed


def test_tmp_cleaner_keeps_state_and_spool_directories_recreate_on_use(
    tmp_path, monkeypatch,
):
    sys.path.insert(0, str(ROOT / "scripts"))
    import goalflight_daemon_logs as logs
    import goalflight_dispatch_paths

    state = tmp_path / "machine-state"
    retained_files = [
        state / "runs.d" / "run.json",
        state / "remote-ci" / "projects" / "pm2" / "state" / "daemon.pid",
        state / "remote-ci" / "projects" / "pm2" / "state" / "daemon.lock",
    ]
    for path in retained_files:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("state", encoding="utf-8")
    spool = state / "remote-ci" / "spool"
    requests = spool / "requests"
    results = spool / "results"
    requests.mkdir(parents=True)
    results.mkdir()
    monkeypatch.setenv("GOALFLIGHT_STATE_DIR", str(state))

    now_ns = time.time_ns() + 10 * DAY_NS
    old_ns = now_ns - 5 * DAY_NS
    monkeypatch.setattr(logs.time, "time_ns", lambda: now_ns)
    for path in retained_files:
        os.utime(path, ns=(old_ns, old_ns))
    for path in (state, state / "remote-ci", spool, requests, results):
        os.utime(path, ns=(old_ns, old_ns))

    logs._keepalive_state_atime()
    initial_files = {
        path for path in state.rglob("*")
        if path.is_file() and not path.is_symlink()
    }
    for path in retained_files:
        after = path.lstat()
        assert after.st_atime_ns == now_ns
        assert after.st_mtime_ns == old_ns

    removed = _simulate_tmp_cleaner(state, now_ns)

    assert not [path for path in removed if path in initial_files]
    assert all(path.exists() for path in initial_files)
    assert not requests.exists() and not results.exists()

    project_root = tmp_path / "project"
    project_root.mkdir()
    config_path = project_root / ".goal-flight" / "remote-ci.json"
    config_path.parent.mkdir()
    config_path.write_text(json.dumps({"paths": {
        "queue_dir": str(requests),
        "result_dir": str(results),
    }}), encoding="utf-8")
    environment = goalflight_dispatch_paths.remote_ci_worker_environment(
        project_root, {"GOALFLIGHT_REMOTE_CI_CONFIG": str(config_path)},
    )

    assert environment["GOALFLIGHT_REMOTE_CI_SPOOL_DIR"] == str(spool)
    assert requests.is_dir() and results.is_dir()


def test_installed_find_atime_plus_three_boundary(tmp_path):
    """Remeasure /usr/bin/find: +3 matches 4 days, not 3d23h or 4d-1s.

    The local derivation sets three atimes relative to one captured clock value,
    then asks the installed binary directly. This pins the simulator to this
    machine's behavior instead of relying on the man page's rounding wording.
    """
    find = Path("/usr/bin/find")
    if not find.exists():
        pytest.skip("installed /usr/bin/find is unavailable")
    now_ns = time.time_ns()
    probes = {
        "exactly-four-days": now_ns - 4 * DAY_NS,
        "three-days-twenty-three-hours": now_ns - (3 * DAY_NS + 23 * 60 * 60 * 1_000_000_000),
        "four-days-minus-one-second": now_ns - (4 * DAY_NS - 1_000_000_000),
    }
    for name, atime_ns in probes.items():
        path = tmp_path / name
        path.write_text("probe", encoding="utf-8")
        os.utime(path, ns=(atime_ns, atime_ns))

    result = subprocess.run(
        [str(find), str(tmp_path), "-type", "f", "-atime", "+3", "-print0"],
        capture_output=True,
        check=True,
    )
    matched = {
        Path(raw.decode()).name for raw in result.stdout.split(b"\0") if raw
    }
    assert matched == {"exactly-four-days"}


def test_tmp_cleaner_boundary_and_keepalive_disabled_control(tmp_path, monkeypatch):
    sys.path.insert(0, str(ROOT / "scripts"))
    import goalflight_daemon_logs as logs

    now_ns = time.time_ns() + 10 * DAY_NS
    old_ns = now_ns - 4 * DAY_NS
    state = tmp_path / "machine-state"
    state.mkdir()
    protected = state / "protected.json"
    protected.write_text("state", encoding="utf-8")
    os.utime(protected, ns=(old_ns, old_ns))
    monkeypatch.setenv("GOALFLIGHT_STATE_DIR", str(state))
    monkeypatch.setattr(logs.time, "time_ns", lambda: now_ns)

    assert _simulate_tmp_cleaner(state, now_ns) == [protected]

    protected.write_text("state", encoding="utf-8")
    os.utime(protected, ns=(old_ns, old_ns))
    logs._keepalive_state_atime()
    assert _simulate_tmp_cleaner(state, now_ns) == []


def test_keepalive_prevents_one_missed_sweep_from_crossing_cleaner_limit(
    tmp_path, monkeypatch,
):
    sys.path.insert(0, str(ROOT / "scripts"))
    import goalflight_daemon_logs as logs

    first_sweep_ns = time.time_ns() + 20 * DAY_NS
    second_sweep_ns = first_sweep_ns + 2 * DAY_NS + 2
    nearly_two_days_old_ns = first_sweep_ns - 2 * DAY_NS + 1
    protected_root = tmp_path / "protected"
    disabled_root = tmp_path / "disabled"
    protected_root.mkdir()
    disabled_root.mkdir()
    protected = protected_root / "state.json"
    disabled = disabled_root / "state.json"
    for path in (protected, disabled):
        path.write_text("state", encoding="utf-8")
        os.utime(path, ns=(nearly_two_days_old_ns, nearly_two_days_old_ns))

    monkeypatch.setenv("GOALFLIGHT_STATE_DIR", str(protected_root))
    monkeypatch.setattr(logs.time, "time_ns", lambda: first_sweep_ns)
    logs._keepalive_state_atime()
    monkeypatch.setattr(logs.time, "time_ns", lambda: second_sweep_ns)

    assert _simulate_tmp_cleaner(disabled_root, second_sweep_ns) == [disabled]
    assert _simulate_tmp_cleaner(protected_root, second_sweep_ns) == []


def test_state_atime_keepalive_skips_scratch_directories(tmp_path, monkeypatch):
    sys.path.insert(0, str(ROOT / "scripts"))
    import goalflight_daemon_logs as logs

    now_ns = time.time_ns() + 10 * DAY_NS
    old_ns = now_ns - 5 * DAY_NS
    state = tmp_path / "machine-state"
    scratch_file = state / "dispatch" / "worker.scratch" / "payload.json"
    retained_file = state / "runs.d" / "run.json"
    for path in (scratch_file, retained_file):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("state", encoding="utf-8")
        os.utime(path, ns=(old_ns, old_ns))
    monkeypatch.setenv("GOALFLIGHT_STATE_DIR", str(state))
    monkeypatch.setattr(logs.time, "time_ns", lambda: now_ns)

    logs._keepalive_state_atime()

    assert scratch_file.lstat().st_atime_ns == old_ns
    assert retained_file.lstat().st_atime_ns == now_ns


def test_state_atime_keepalive_skips_symlink_marker(tmp_path, monkeypatch, capsys):
    sys.path.insert(0, str(ROOT / "scripts"))
    import goalflight_daemon_logs as logs

    state = tmp_path / "machine-state"
    state.mkdir()
    retained = state / "record.json"
    retained.write_text("state", encoding="utf-8")
    outside = tmp_path / "outside-marker-target"
    outside.write_text("do not truncate", encoding="utf-8")
    old_ns = time.time_ns() - 5 * DAY_NS
    os.utime(retained, ns=(old_ns, old_ns))
    os.utime(outside, ns=(old_ns, old_ns))
    marker = state / logs._STATE_ATIME_MARKER
    marker.symlink_to(outside)
    monkeypatch.setenv("GOALFLIGHT_STATE_DIR", str(state))

    logs._keepalive_state_atime()

    assert retained.lstat().st_atime_ns == old_ns, "symlink marker must fail closed"
    assert outside.read_text(encoding="utf-8") == "do not truncate"
    assert outside.lstat().st_mtime_ns == old_ns
    assert "skipped symlink marker" in capsys.readouterr().err


def test_state_keepalive_does_not_overwrite_concurrent_mtime(tmp_path, monkeypatch):
    sys.path.insert(0, str(ROOT / "scripts"))
    import goalflight_daemon_logs as logs

    state = tmp_path / "machine-state"
    state.mkdir()
    path = state / "record.json"
    path.write_text("old", encoding="utf-8")
    now_ns = time.time_ns()
    old_ns = now_ns - 5 * DAY_NS
    concurrent_mtime_ns = now_ns + 2_000_000_000
    os.utime(path, ns=(old_ns, old_ns))
    monkeypatch.setenv("GOALFLIGHT_STATE_DIR", str(state))
    monkeypatch.setattr(logs.time, "time_ns", lambda: now_ns)
    real_scandir = logs.os.scandir
    injected = False

    class RaceEntry:
        def __init__(self, entry):
            self.entry = entry

        def __getattr__(self, name):
            return getattr(self.entry, name)

        def stat(self, *, follow_symlinks=True):
            nonlocal injected
            stale = self.entry.stat(follow_symlinks=follow_symlinks)
            if self.entry.name == path.name and not injected:
                os.utime(path, ns=(old_ns, concurrent_mtime_ns))
                injected = True
            return stale

    from contextlib import contextmanager

    @contextmanager
    def racing_scandir(directory):
        with real_scandir(directory) as entries:
            yield (RaceEntry(entry) for entry in entries)

    monkeypatch.setattr(logs.os, "scandir", racing_scandir)
    logs._keepalive_state_atime()

    assert injected, "the concurrent mtime write must happen after enumeration stat"
    assert path.lstat().st_atime_ns == now_ns
    assert path.lstat().st_mtime_ns == concurrent_mtime_ns


def test_state_atime_keepalive_skips_symlinks_and_missing_roots(tmp_path, monkeypatch):
    sys.path.insert(0, str(ROOT / "scripts"))
    import goalflight_daemon_logs as logs

    state = tmp_path / "machine-state"
    state.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    target = outside / "retained.json"
    target.write_text("external", encoding="utf-8")
    old_ns = time.time_ns() - 5 * DAY_NS
    os.utime(target, ns=(old_ns, old_ns))
    (state / "file-link").symlink_to(target)
    (state / "directory-link").symlink_to(outside, target_is_directory=True)
    monkeypatch.setenv("GOALFLIGHT_STATE_DIR", str(state))

    logs._keepalive_state_atime()

    assert target.lstat().st_atime_ns == old_ns
    assert target.lstat().st_mtime_ns == old_ns

    missing = tmp_path / "missing-state"
    monkeypatch.setenv("GOALFLIGHT_STATE_DIR", str(missing))
    logs._keepalive_state_atime()
    assert not missing.exists(), "the keep-alive must not recreate a removed state tree"


def test_installed_drain_keeps_wrapper_and_cross_project(tmp_path):
    env = dict(os.environ, HOME=str(tmp_path), SKILL_ROOT=str(ROOT))
    result = subprocess.run(
        ["bash", str(ROOT / "scripts/install-drainer.sh"), "--dry-run"],
        env=env, capture_output=True, text=True, check=True,
    )
    argv = plistlib.loads(result.stdout.encode())["ProgramArguments"]
    payload = argv.index(str(ROOT / "scripts/goalflight_dispatch.py"))
    assert argv[payload - 3] == str(ROOT / "scripts/goalflight_daemon_logs.py")
    assert argv[payload + 1:] == ["drain", "--cross-project", "--json"]
