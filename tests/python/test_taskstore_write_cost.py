"""Write-cost regressions; all stores and machine state are pytest-isolated."""
import ctypes
import json
import os
from pathlib import Path
import shutil
import sys
import subprocess

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
def test_dashboard_off_by_default_removes_existing_mirrors(store, monkeypatch, changed):
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
        path.write_bytes(fixture.read_bytes())
    store.mutate_items(lambda rows: rows[0].update(title="Changed") if changed else None)
    assert not store.data_js_path.exists()
    assert not exported.exists()
    # Browser boot after publication must render no stale rows and explain why.
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
if (fs.existsSync(process.argv[1])) vm.runInContext(fs.readFileSync(process.argv[1], 'utf8'), context);
vm.runInContext(fs.readFileSync(process.argv[2], 'utf8'), context);
const driver = window.GF.attach({});
assert.strictEqual(window.GF.store.items.length, 0);
assert(notices.some(text => text.includes('GOALFLIGHT_DASHBOARD_EXPORT_ENABLED=1')));
driver.destroy();
""", str(exported), str(task.ROOT / "templates/state-skeleton/gf.js")], capture_output=True, text=True)
    assert browser.returncode == 0, browser.stderr
    # Interrupted-publish recovery also invalidates the canonical mirror.
    store.data_js_path.write_bytes(fixture.read_bytes())
    store._write_publish_marker("interrupted")
    assert "dashboard/tasks-data.js" not in json.loads(store.publish_marker_path.read_text())["artifacts"]
    store._recover_interrupted_publish()
    assert not store.publish_marker_path.exists()
    assert not store.data_js_path.exists()
    assert not exported.exists()
    assert len(list(store.log_dir.glob("tasks-*.jsonl"))) == 1
    assert not list(store.log_dir.glob("tasks-data-*.js"))


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
    monkeypatch.delenv("GOALFLIGHT_DASHBOARD_EXPORT_ENABLED")
    store.save_items_atomic(items)
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
    assert all(path.name != "tasks-data.js" for path, _size in published)
