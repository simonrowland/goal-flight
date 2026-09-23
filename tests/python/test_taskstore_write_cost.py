"""Write-cost regressions; all stores and machine state are pytest-isolated."""
import ctypes
import os
from pathlib import Path
import shutil
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import goalflight_task as task


@pytest.fixture
def store(tmp_path):
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
        oldest = backups[0]
        store._snapshot_last_good()
        assert not oldest.exists()


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
