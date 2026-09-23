"""Write-cost regressions; all stores and machine state are pytest-isolated."""
import ctypes
import json
import os
from pathlib import Path
import shutil
import sys
import subprocess
import types

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import goalflight_task as task


@pytest.fixture
def store(tmp_path, monkeypatch):
    # Existing write-cost cases exercise the opt-in mirror contract.
    monkeypatch.setenv("GOALFLIGHT_DASHBOARD_EXPORT_ENABLED", "1")
    project = tmp_path / "project"
    project.mkdir()
    return task.TaskStore(project)


def item():
    return task._make_item("t-001", kind="task", title="Initial", actor="test")


def targets(store):
    names = ["tasks.jsonl", "task-decomposition.md", "tasks-done.md", "bug-backlog.md", "bugs-done.md"]
    return ([base / name for base in (store.docs_dir, store.export_docs_dir) for name in names]
            + [store.data_js_path, store.export_dashboard_dir / "tasks-data.js"])


def test_unchanged_save_and_changed_atomic_publish(store, monkeypatch):
    # The mirror includes generated_at; byte identity requires the same clock.
    monkeypatch.setattr(task, "utc_now", lambda: "2026-09-23T00:00:00+00:00")
    items = [item()]
    store.save_items_atomic(items)
    before = {p: (p.stat().st_ino, p.stat().st_mtime_ns) for p in targets(store)}
    writes = []
    original = task._write_text_fsync
    monkeypatch.setattr(task, "_write_text_fsync", lambda p, text: (writes.append(p), original(p, text))[-1])
    store.save_items_atomic(items)
    assert not writes, writes
    assert before == {p: (p.stat().st_ino, p.stat().st_mtime_ns) for p in targets(store)}
    replacements = []
    replace = Path.replace
    monkeypatch.setattr(Path, "replace", lambda p, dst: (replacements.append((p, dst)), replace(p, dst))[-1])
    items[0]["title"] = "Changed"
    store.save_items_atomic(items)
    assert store.tasks_path.stat().st_ino != before[store.tasks_path][0]
    assert any(src.parent.name.startswith(".tasks-stage-") and dst == store.tasks_path for src, dst in replacements)
    assert any(src.name.startswith(".tmp-") and dst == store.export_docs_dir / "tasks.jsonl" for src, dst in replacements)
    assert not store.publish_marker_path.exists()


def test_comparison_size_first_and_same_size_content(tmp_path, monkeypatch):
    path = tmp_path / "content"
    path.write_bytes(b"abc")
    assert task._file_matches_bytes(path, b"abc")
    assert not task._file_matches_bytes(path, b"abd")
    monkeypatch.setattr(Path, "open", lambda *args, **kw: pytest.fail("size mismatch opened file"))
    assert not task._file_matches_bytes(path, b"longer")


def test_changed_publish_failure_restores_previous_generation(store, monkeypatch):
    monkeypatch.setattr(task, "utc_now", lambda: "2026-09-23T00:00:00+00:00")
    items = [item()]
    store.save_items_atomic(items)
    before = {p: p.read_bytes() for p in targets(store)}
    unchanged_inode = store.tasks_done_path.stat().st_ino
    replace = Path.replace

    def fail_mirror(src, dst):
        if src.parent.name.startswith(".tasks-stage-") and dst == store.data_js_path:
            raise OSError("injected publication failure")
        return replace(src, dst)

    monkeypatch.setattr(Path, "replace", fail_mirror)
    items[0]["title"] = "Changed"
    with pytest.raises(OSError, match="injected publication failure"):
        store.save_items_atomic(items)
    assert before == {p: p.read_bytes() for p in targets(store)}
    assert store.tasks_done_path.stat().st_ino == unchanged_inode
    assert not store.publish_marker_path.exists()


def test_snapshot_contents_metadata_and_pruning(store):
    store.save_items_atomic([item()])
    for _ in range(22):
        store._snapshot_last_good()
    for src, pattern in ((store.tasks_path, "tasks-*.jsonl"), (store.data_js_path, "tasks-data-*.js")):
        backups = sorted(store.log_dir.glob(pattern))
        assert len(backups) == 20
        assert all(p.read_bytes() == src.read_bytes() for p in backups)
        assert all(p.stat().st_mtime_ns == src.stat().st_mtime_ns for p in backups)


@pytest.mark.parametrize("equal_mtimes", [False, True])
def test_pruning_preserves_parent_survivors(store, equal_mtimes):
    store.log_dir.mkdir(parents=True, exist_ok=True)
    for index in range(21):
        path = store.log_dir / f"tasks-20260923T000000{index:06d}Z.jsonl"
        path.write_bytes(b"backup")
        stamp = 1000 if equal_mtimes else 1000 - index
        os.utime(path, (stamp, stamp))
    expected = set(sorted(store.log_dir.glob("tasks-*.jsonl"),
                          key=lambda p: p.stat().st_mtime, reverse=True)[:20])
    store._prune_backups()
    assert set(store.log_dir.glob("tasks-*.jsonl")) == expected


def test_export_repairs_symlink_without_touching_referent(store, tmp_path):
    store.save_items_atomic([item()])
    target = store.export_docs_dir / "tasks.jsonl"
    referent = tmp_path / "referent"
    referent.write_bytes(b"outdated export")
    target.unlink()
    target.symlink_to(referent)
    store._export_to_project_tree()
    assert not target.is_symlink()
    assert target.read_bytes() == store.tasks_path.read_bytes()
    assert referent.read_bytes() == b"outdated export"


def test_successful_identical_save_clears_existing_marker(store, monkeypatch):
    monkeypatch.setattr(task, "utc_now", lambda: "2026-09-23T00:00:00+00:00")
    items = [item()]
    store.save_items_atomic(items)
    store._write_publish_marker("interrupted")
    with store.store_lock():
        store.save_items_atomic(items)
    assert not store.publish_marker_path.exists()


def test_comparison_nonregular_and_unreadable_are_changed(tmp_path, monkeypatch):
    assert not task._file_matches_bytes(tmp_path, b"")
    path = tmp_path / "content"
    path.write_bytes(b"abc")
    def denied(*args, **kwargs):
        raise PermissionError("injected unreadable target")
    monkeypatch.setattr(Path, "open", denied)
    assert not task._file_matches_bytes(path, b"abc")


@pytest.mark.parametrize("changed", [False, True])
@pytest.mark.parametrize("recover", [False, True])
def test_dashboard_off_tombstones_existing_mirrors_once(store, monkeypatch, changed, recover):
    monkeypatch.delenv("GOALFLIGHT_DASHBOARD_EXPORT_ENABLED")
    monkeypatch.setattr(task, "CHECKER", store.project_root / "nonexistent-checker.js")
    monkeypatch.setattr(task, "_items_data_js", lambda *a: pytest.fail("generated disabled mirror"))
    store.save_items_atomic([item()])
    assert not store.data_js_path.exists()
    exported = store.export_dashboard_dir / "tasks-data.js"
    assert not exported.exists()
    # Upgrade from an enabled installation with a valid, now stale snapshot.
    fixture = task.ROOT / "tests/fixtures/tasks-mirror/tasks-data.js"
    for path in (store.data_js_path, exported):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(fixture.read_bytes() + b" " * (2 * 1024 * 1024))
    replacements = []
    replace = Path.replace
    monkeypatch.setattr(Path, "replace", lambda src, dst: (replacements.append((src, dst)), replace(src, dst))[-1])
    if recover:
        store._write_publish_marker("interrupted")
        store._recover_interrupted_publish()
    else:
        store.mutate_items(lambda rows: rows[0].update(title="Changed") if changed else None)
    for path in (store.data_js_path, exported):
        assert path.stat().st_size < 200
        assert b'"dashboard_export":"disabled"' in path.read_bytes()
        assert b'window.GF_ITEMS = [];' in path.read_bytes()
        assert any(src.name.startswith(".tmp-") and dst == path for src, dst in replacements)
    before = {p: (p.read_bytes(), p.stat().st_ino, p.stat().st_mtime_ns)
              for p in (store.data_js_path, exported)}
    replacements.clear()
    store.save_items_atomic(store.load_items())
    store._write_publish_marker("interrupted")
    store._recover_interrupted_publish()
    assert not store.publish_marker_path.exists()
    assert before == {p: (p.read_bytes(), p.stat().st_ino, p.stat().st_mtime_ns) for p in before}
    assert not any(dst in before for src, dst in replacements)
    assert not list(store.log_dir.glob("tasks-data-*.js"))


@pytest.mark.parametrize("canonical", [False, True])
def test_disabled_export_preserves_symlinked_directory(store, monkeypatch, tmp_path, canonical):
    monkeypatch.delenv("GOALFLIGHT_DASHBOARD_EXPORT_ENABLED")
    outside = tmp_path / "unrelated"
    outside.mkdir()
    victim = outside / "tasks-data.js"
    victim.write_bytes(b"unrelated data")
    before = victim.stat()
    dashboard = store.dashboard_dir if canonical else store.export_dashboard_dir
    dashboard.parent.mkdir(parents=True, exist_ok=True)
    dashboard.symlink_to(outside, target_is_directory=True)
    with pytest.raises(task.TaskError, match="escapes|symlink"):
        store.save_items_atomic([item()])
    assert victim.read_bytes() == b"unrelated data"
    assert (victim.stat().st_ino, victim.stat().st_mtime_ns) == (before.st_ino, before.st_mtime_ns)


@pytest.mark.parametrize("canonical", [False, True])
def test_disabled_mirror_replaces_file_symlink_not_referent(store, monkeypatch, tmp_path, canonical):
    monkeypatch.delenv("GOALFLIGHT_DASHBOARD_EXPORT_ENABLED")
    victim = tmp_path / "unrelated.js"
    victim.write_bytes(b"unrelated data")
    before = victim.stat()
    path = store.data_js_path if canonical else store.export_dashboard_dir / "tasks-data.js"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.symlink_to(victim)
    store.save_items_atomic([item()])
    assert not path.is_symlink()
    assert b'"dashboard_export":"disabled"' in path.read_bytes()
    assert victim.read_bytes() == b"unrelated data"
    assert (victim.stat().st_ino, victim.stat().st_mtime_ns) == (before.st_ino, before.st_mtime_ns)


def test_old_process_can_mutate_after_new_default_write(store, monkeypatch):
    # Keep the old module/store alive across the new writer's publication.
    old = types.ModuleType("taskstore_before_default_off")
    old.__file__ = str(task.ROOT / "goalflight_task.py")
    monkeypatch.setitem(sys.modules, old.__name__, old)
    source = subprocess.check_output(["git", "show", "59f803f:goalflight_task.py"], cwd=task.ROOT)
    exec(compile(source, old.__file__, "exec"), old.__dict__)
    old_store = old.TaskStore(store.project_root)
    old_store.save_items_atomic([item()])
    monkeypatch.delenv("GOALFLIGHT_DASHBOARD_EXPORT_ENABLED")
    store.mutate_items(lambda rows: rows[0].update(title="New writer"))
    old_store.mutate_items(lambda rows: rows[0].update(title="Old writer"))
    assert store.load_items()[0]["title"] == "Old writer"


@pytest.mark.parametrize("revision", ["c6062e5", "6665a21"])
def test_opted_in_old_writer_refusal_and_watcher_recovery(store, monkeypatch, tmp_path, revision):
    from test_watch_prompt_echo import _watcher_command, _wait_for_status_matching

    store.save_items_atomic([item()])
    monkeypatch.delenv("GOALFLIGHT_DASHBOARD_EXPORT_ENABLED")
    store.save_items_atomic(store.load_items())
    monkeypatch.setenv("GOALFLIGHT_DASHBOARD_EXPORT_ENABLED", "1")
    before = {p: (p.read_bytes(), p.stat().st_ino, p.stat().st_mtime_ns) for p in targets(store)}
    old = types.ModuleType("taskstore_old_opted_in")
    old.__file__ = str(task.ROOT / "goalflight_task.py")
    monkeypatch.setitem(sys.modules, old.__name__, old)
    source = subprocess.check_output(["git", "show", f"{revision}:goalflight_task.py"], cwd=task.ROOT)
    exec(compile(source, old.__file__, "exec"), old.__dict__)
    with pytest.raises(old.TaskError, match="id-sets differ"):
        old.TaskStore(store.project_root).mutate_items(lambda rows: pytest.fail("refused mutation ran"))
    assert before == {p: (p.read_bytes(), p.stat().st_ino, p.stat().st_mtime_ns) for p in before}
    assert not store.publish_marker_path.exists()
    monkeypatch.setenv("GOALFLIGHT_TEST_MODE", "1")
    monkeypatch.setenv("GOALFLIGHT_TEST_PGROUP_CPU_PCT", "0.0")
    tail, status = tmp_path / "tail.txt", tmp_path / "status.json"
    tail.write_text("Worker running\n")
    dispatch_id = "old-writer-upgrade"
    worker = subprocess.Popen([sys.executable, "-c", "import sys; sys.stdin.read()"],
                              stdin=subprocess.PIPE, start_new_session=True)
    watcher = None
    try:
        command = _watcher_command(tail=tail, status=status, worker_pid=worker.pid,
                                   dispatch_id=dispatch_id, poll_secs="1", max_idle_secs="30")
        bootstrap = """
import pathlib, subprocess, sys, types
root = pathlib.Path.cwd()
sys.path[:0] = [str(root), str(root / 'scripts')]
revision = sys.argv.pop(1)
old = types.ModuleType('goalflight_task')
old.__file__ = str(root / 'goalflight_task.py')
sys.modules['goalflight_task'] = old
exec(compile(subprocess.check_output(['git', 'show', revision + ':goalflight_task.py']), old.__file__, 'exec'), old.__dict__)
watch_path = str(root / 'scripts/goalflight_watch.py')
sys.argv[0] = watch_path
exec(compile(subprocess.check_output(['git', 'show', revision + ':scripts/goalflight_watch.py']), watch_path, 'exec'), {'__name__':'__main__', '__file__':watch_path})
"""
        watcher = subprocess.Popen([sys.executable, "-c", bootstrap, revision, *command[2:],
                                    "--project-root", str(store.project_root), "--task-ids", "t-001"],
                                   cwd=task.ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        # Old watcher breadcrumbs deliberately allow an invalid live mirror,
        # unlike ordinary mutations: they repair the pair and keep running.
        _wait_for_status_matching(status, lambda p: bool(store.load_items()[0]["dispatches"]))
        first_mtime = status.stat().st_mtime_ns
        _wait_for_status_matching(status, lambda p: status.stat().st_mtime_ns != first_mtime)
        assert watcher.poll() is None
        assert worker.poll() is None
        assert len(store.load_items()[0]["dispatches"]) == 1
        task._run_checker(store.docs_dir, store.dashboard_dir)
        with tail.open("a") as stream:
            stream.write(f"!COMPLETE: {dispatch_id} — work finished\n")
        worker.stdin.close()
        worker.wait(timeout=5)
        stdout, stderr = watcher.communicate(timeout=30)
        final = json.loads(status.read_text())
        assert watcher.returncode == 0, (stdout, stderr, final)
        assert final["state"] == "complete"
        assert final["terminal_marker"]["kind"] == "COMPLETE"
        rows = store.load_items()
        assert len(rows) == 1 and rows[0]["title"] == "Initial"
        assert [crumb["state"] for crumb in rows[0]["dispatches"]] == ["working", "worker-finished"]
        task._run_checker(store.docs_dir, store.dashboard_dir)
        assert not store.publish_marker_path.exists()
        assert not list(store.docs_dir.glob(".tasks-stage-*"))
    finally:
        for process in (watcher, worker):
            if process is not None and process.poll() is None:
                process.terminate()
                process.wait(timeout=5)


def test_disabled_stub_browser_notice_and_no_records(store, monkeypatch):
    store.save_items_atomic([item()])
    monkeypatch.delenv("GOALFLIGHT_DASHBOARD_EXPORT_ENABLED")
    store._write_publish_marker("interrupted")
    store._recover_interrupted_publish()
    assert_disabled_browser(store.export_dashboard_dir / "tasks-data.js", task.ROOT / "templates/state-skeleton/gf.js")


def assert_disabled_browser(mirror, renderer):
    browser = subprocess.run(["node", "-e", """
const fs = require('fs'), vm = require('vm'), assert = require('assert');
const notices = [];
const window = {
  document: {
    createElement: () => ({setAttribute() {}}),
    querySelector: () => ({prepend: node => notices.push(node.textContent)}),
    addEventListener() {}, removeEventListener() {}
  },
  addEventListener() {}, removeEventListener() {}
};
const context = vm.createContext({window, URL, URLSearchParams});
vm.runInContext(fs.readFileSync(process.argv[1], 'utf8'), context);
assert.strictEqual(window.GF_ITEMS.length, 0);
// Disabled metadata must also override any retained in-memory array.
window.GF_ITEMS = [{id: 't-999', kind: 'task', title: 'Stale'}];
vm.runInContext(fs.readFileSync(process.argv[2], 'utf8'), context);
const driver = window.GF.attach({});
assert.strictEqual(window.GF.store.items.length, 0);
assert(notices.some(text => text.includes('Dashboard export disabled') &&
  text.includes('GOALFLIGHT_DASHBOARD_EXPORT_ENABLED=1')));
driver.destroy();
""", str(mirror), str(renderer)], capture_output=True, text=True)
    assert browser.returncode == 0, browser.stderr


@pytest.mark.parametrize("refresh", [False, True])
def test_upgrade_tombstones_retained_mirrors_once(store, monkeypatch, refresh):
    import goalflight_setup as setup

    setup.scaffold_project_state(task.ROOT, store.project_root, apply=True)
    store.save_items_atomic([item()])
    monkeypatch.delenv("GOALFLIGHT_DASHBOARD_EXPORT_ENABLED")
    paths = (store.data_js_path, store.export_dashboard_dir / "tasks-data.js")
    before = {p: p.read_bytes() for p in paths}
    if refresh:
        setup.refresh_managed_views(task.ROOT, store.project_root, dry_run=True)
    else:
        setup.scaffold_project_state(task.ROOT, store.project_root, apply=False)
    assert before == {p: p.read_bytes() for p in paths}
    for _ in range(2):
        if refresh:
            setup.refresh_managed_views(task.ROOT, store.project_root)
        else:
            setup.scaffold_project_state(task.ROOT, store.project_root, apply=True)
        assert_disabled_browser(paths[1], store.export_dashboard_dir / "gf.js")
        after = {p: (p.read_bytes(), p.stat().st_ino, p.stat().st_mtime_ns) for p in paths}
        if _ == 0:
            stub = after
        else:
            assert after == stub


@pytest.mark.parametrize("canonical", [False, True])
def test_refresh_refuses_symlinked_dashboard(store, monkeypatch, tmp_path, canonical):
    import goalflight_setup as setup

    monkeypatch.delenv("GOALFLIGHT_DASHBOARD_EXPORT_ENABLED")
    outside = tmp_path / "unrelated"
    outside.mkdir()
    victim = outside / "tasks-data.js"
    victim.write_bytes(b"unrelated data")
    dashboard = store.dashboard_dir if canonical else store.export_dashboard_dir
    dashboard.parent.mkdir(parents=True, exist_ok=True)
    dashboard.symlink_to(outside, target_is_directory=True)
    with pytest.raises(setup.SetupError, match="escapes|symlink"):
        setup.refresh_managed_views(task.ROOT, store.project_root)
    assert victim.read_bytes() == b"unrelated data"


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
@pytest.mark.parametrize("nested", [False, True])
def test_dashboard_off_rejects_nonfinite_canonical_values(store, monkeypatch, value, nested):
    monkeypatch.delenv("GOALFLIGHT_DASHBOARD_EXPORT_ENABLED")
    store.save_items_atomic([item()])
    before = store.tasks_path.read_bytes()
    invalid = item()
    invalid["measurement"] = {"samples": [value]} if nested else value
    with pytest.raises(task.TaskError, match="JSON"):
        store.save_items_atomic([invalid])
    assert store.tasks_path.read_bytes() == before
    assert not store.publish_marker_path.exists()


def test_dashboard_opt_in_restores_exact_output(store, monkeypatch):
    monkeypatch.setattr(task, "utc_now", lambda: "2026-09-23T00:00:00+00:00")
    items = [item()]
    expected = task._items_data_js(store._mirror_items_for_script(items)).encode()
    store.save_items_atomic(items)
    monkeypatch.delenv("GOALFLIGHT_DASHBOARD_EXPORT_ENABLED")
    store.save_items_atomic(items)
    assert b'"dashboard_export":"disabled"' in store.data_js_path.read_bytes()
    monkeypatch.setenv("GOALFLIGHT_DASHBOARD_EXPORT_ENABLED", "1")
    store.mutate_items(lambda rows: None, allow_invalid_live_mirror=True)
    assert store.data_js_path.read_bytes() == expected
    assert (store.export_dashboard_dir / "tasks-data.js").read_bytes() == expected


def test_dashboard_disabled_readers(store, monkeypatch, capsys):
    import goalflight_doctor as doctor
    import goalflight_dispatch as dispatch
    import goalflight_messages as messages
    import goalflight_setup as setup

    monkeypatch.delenv("GOALFLIGHT_DASHBOARD_EXPORT_ENABLED")
    store.save_items_atomic([item()])
    hint = "GOALFLIGHT_DASHBOARD_EXPORT_ENABLED=1"
    assert hint in doctor._check_tasks_mirror(store.project_root, task.ROOT)["skipped"]
    assert "tasks-data.js" not in doctor.canonical_dashboard_files(task.ROOT)
    assert dispatch._cmd_dashboard_refresh(["--project-root", str(store.project_root)]) == 0
    assert hint in capsys.readouterr().out
    frontier = messages._follow_frontier_snapshot(store)
    assert frontier["payload"]["state"] == "unavailable"
    assert hint in frontier["payload"]["detail"]
    plan = setup.scaffold_project_state(task.ROOT, store.project_root)
    assert "dashboard/tasks-data.js" not in plan["would_create_files"]
    setup.scaffold_project_state(task.ROOT, store.project_root, apply=True)
    assert not (store.export_dashboard_dir / "tasks-data.js").exists()
    layout = doctor.check_project_state_layout(store.project_root, task.ROOT)
    assert "dashboard/tasks-data.js" not in layout["missing_files"]
    result = subprocess.run(["node", str(task.CHECKER), str(store.docs_dir)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert hint in result.stdout
    status = subprocess.run([sys.executable, str(task.ROOT / "goalflight_task.py"),
                             "--project-root", str(store.project_root), "status", "--json"],
                            capture_output=True, text=True)
    assert status.returncode == 0, status.stderr
    assert hint in status.stderr
    assert json.loads(status.stdout)["items"][0]["id"] == "t-001"


def test_dashboard_off_legacy_snapshot_recovery(store, monkeypatch):
    monkeypatch.delenv("GOALFLIGHT_DASHBOARD_EXPORT_ENABLED")
    legacy_log = store.export_docs_dir / "log"
    legacy_log.mkdir(parents=True)
    (legacy_log / "tasks-20260923.jsonl").write_text(task._items_jsonl([item()]))
    store.mutate_items(lambda rows: rows[0].update(title="Recovered"))
    assert store.load_items()[0]["title"] == "Recovered"
    assert not store.data_js_path.exists()
    assert not (store.export_dashboard_dir / "tasks-data.js").exists()


def test_dashboard_off_still_rejects_invalid_canonical_store(store, monkeypatch):
    monkeypatch.delenv("GOALFLIGHT_DASHBOARD_EXPORT_ENABLED")
    store.save_items_atomic([item()])
    store.tasks_path.write_bytes(b"invalid json\n")
    with pytest.raises(task.TaskError):
        store.mutate_items(lambda rows: pytest.fail("mutated invalid store"))
    assert store.tasks_path.read_bytes() == b"invalid json\n"
    assert not list(store.log_dir.glob("tasks-*.jsonl"))


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS clonefile")
@pytest.mark.parametrize("failure", [False, True])
def test_clonefile_path_and_fallback(tmp_path, monkeypatch, failure):
    src, dst = tmp_path / "src", tmp_path / "dst"
    src.write_bytes(b"clone me" * 1000)
    os.utime(src, ns=(1234567890000000000, 1234567890000000000))
    real = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True).clonefile
    calls = []

    def clone(a, b, flags):
        calls.append((a, b, flags))
        if failure:
            raise OSError("injected clone failure")
        return real(a, b, flags)

    class Library:
        clonefile = staticmethod(clone)

    monkeypatch.setattr(ctypes, "CDLL", lambda *a, **kw: Library())
    task._clone_or_copy(src, dst)
    assert calls == [(os.fsencode(src), os.fsencode(dst), 0)]
    assert dst.read_bytes() == src.read_bytes()
    assert dst.stat().st_mtime_ns == src.stat().st_mtime_ns


def test_unsupported_clone_falls_back(tmp_path, monkeypatch):
    src, dst = tmp_path / "src", tmp_path / "dst"
    src.write_bytes(b"backup")
    monkeypatch.setattr(sys, "platform", "unsupported")
    task._clone_or_copy(src, dst)
    assert dst.read_bytes() == b"backup"


@pytest.mark.skipif(sys.platform == "win32", reason="fcntl unavailable")
@pytest.mark.parametrize("failure", [False, True])
def test_linux_reflink_and_partial_failure(tmp_path, monkeypatch, failure):
    import fcntl

    src, dst = tmp_path / "src", tmp_path / "dst"
    src.write_bytes(b"reflink")
    calls = []

    def ioctl(target, request, source):
        calls.append(request)
        if failure:
            os.write(target, b"partial")
            raise OSError("injected reflink failure")
        os.write(target, os.read(source, 100))

    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(fcntl, "ioctl", ioctl)
    task._clone_or_copy(src, dst)
    assert calls == [0x40049409]
    assert dst.read_bytes() == src.read_bytes()


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS proc_pid_rusage")
def test_mutation_disk_write_cost(store, monkeypatch):
    # rusage_info_v2: two UUID words followed by 18 uint64 fields.
    # diskio_byteswritten is the final field (offset 152).
    lib = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)

    def written():
        info = (ctypes.c_uint64 * 20)()
        assert lib.proc_pid_rusage(os.getpid(), 2, ctypes.byref(info)) == 0
        return info[19]

    items = [item()]
    items[0]["notes"] = "x" * (20 * 1024 * 1024)
    store.save_items_atomic(items)
    clone = task._clone_or_copy
    matches = task._file_matches_bytes

    def mutate(rows):
        rows[0]["title"] += "!"

    monkeypatch.setattr(task, "_clone_or_copy", shutil.copy2)
    monkeypatch.setattr(task, "_file_matches_bytes", lambda *a: False)
    start = written()
    store.mutate_items(mutate)
    before = written() - start
    monkeypatch.setattr(task, "_clone_or_copy", clone)
    monkeypatch.setattr(task, "_file_matches_bytes", matches)
    published = []
    replace = Path.replace
    write_text = task._write_text_fsync

    def record_write(path, text):
        write_text(path, text)
        if path == store.publish_marker_path:
            published.append((path, path.stat().st_size))

    def record_replace(src, dst):
        result = replace(src, dst)
        published.append((Path(dst), Path(dst).stat().st_size))
        return result

    monkeypatch.setattr(Path, "replace", record_replace)
    monkeypatch.setattr(task, "_write_text_fsync", record_write)
    start = written()
    store.mutate_items(mutate)
    after = written() - start
    print(f"diskio_byteswritten before={before} after={after}")
    for path, size in published:
        base = store.store_dir if path.is_relative_to(store.store_dir) else store.project_root
        label = "canonical" if base == store.store_dir else "export"
        print(f"{label}/{path.relative_to(base)}: {size} bytes")
    assert before > 0
    assert after < before * 0.8
    monkeypatch.delenv("GOALFLIGHT_DASHBOARD_EXPORT_ENABLED")
    published.clear()
    start = written()
    store.mutate_items(mutate)
    disabled = written() - start
    print(f"diskio_byteswritten original={before} optimized_mirror_on={after} mirror_off={disabled}")
    assert disabled < after * 0.6
    assert all(size < 200 for path, size in published if path.name == "tasks-data.js")
