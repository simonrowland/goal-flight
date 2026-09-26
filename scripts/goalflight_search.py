#!/usr/bin/env python3
"""Bounded search across a checkout, refs, or Git history.

The refs mode deliberately searches each distinct tree and blob once.  It is
intended for workers that need answers from many refs without asking Git to
materialize every ref's complete tree independently.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import mmap
import os
import re
import selectors
import signal
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO, Iterable, Iterator, Sequence


DEFAULT_MAX_RESULTS = 100
MAX_UNSCOPED_REFS = 100
DEFAULT_WALL_CLOCK_SECONDS = 120.0
MAX_BLOB_BYTES = 1024 * 1024
STREAM_CHUNK_BYTES = 64 * 1024
TREE_READ_BYTES = 4 * 1024 * 1024
MAX_PATH_EXAMPLES = 20
MAX_REF_EXAMPLES = 5
COMMIT_BATCH_SIZE = 512
BLOB_BATCH_SIZE = 512
BLOB_WORKERS = 2
MAX_PENDING_OCCURRENCES = 4096
BULK_DIRECTORIES = frozenset({"build", "vendor", "data", "node_modules"})


class SearchError(RuntimeError):
    """A user-actionable search failure."""


class SearchTimeout(SearchError):
    """The bounded search budget expired."""


class SearchBudget:
    def __init__(self, seconds: float = DEFAULT_WALL_CLOCK_SECONDS) -> None:
        self.seconds = seconds
        self.started = time.monotonic()

    def check(self) -> None:
        if time.monotonic() - self.started >= self.seconds:
            raise SearchTimeout(
                f"search truncated: wall-clock budget exceeded ({self.seconds:.1f}s); "
                "narrow --refs or add PATH filters"
            )

    def remaining(self) -> float:
        return max(0.0, self.seconds - (time.monotonic() - self.started))


@dataclass
class TreeGroup:
    ref_count: int = 0
    ref_examples: list[str] = field(default_factory=list)

    def add_refs(self, refs: Iterable[str]) -> None:
        for ref in refs:
            self.ref_count += 1
            if len(self.ref_examples) < MAX_REF_EXAMPLES:
                self.ref_examples.append(ref)

    def merge(self, other: "TreeGroup") -> None:
        self.ref_count += other.ref_count
        for ref in other.ref_examples:
            if len(self.ref_examples) >= MAX_REF_EXAMPLES:
                break
            if ref not in self.ref_examples:
                self.ref_examples.append(ref)


@dataclass
class RefSelection:
    commits: dict[str, TreeGroup]
    ref_count: int


@dataclass
class RefHit:
    blob: str
    paths: list[str] = field(default_factory=list)
    refs: list[str] = field(default_factory=list)
    ref_count: int = 0
    counted_trees: set[str] = field(default_factory=set)

    def add_occurrence(self, tree: str, path: str, group: TreeGroup) -> None:
        if path not in self.paths and len(self.paths) < MAX_PATH_EXAMPLES:
            self.paths.append(path)
        if tree in self.counted_trees:
            return
        self.counted_trees.add(tree)
        self.ref_count += group.ref_count
        for ref in group.ref_examples:
            if ref not in self.refs and len(self.refs) < MAX_REF_EXAMPLES:
                self.refs.append(ref)

    def as_dict(self) -> dict[str, object]:
        return {
            "blob": self.blob,
            "paths": self.paths,
            "refs": self.refs,
            "ref_count": self.ref_count,
        }


@dataclass(frozen=True)
class TreeEntry:
    blob: str
    path: str


@dataclass(frozen=True)
class RawTreeEntry:
    mode: bytes
    object_id: str
    name: bytes


class GitCommandError(SearchError):
    def __init__(self, argv: Sequence[str], completed: subprocess.CompletedProcess[str]) -> None:
        detail = (completed.stderr or completed.stdout or "").strip()
        command = " ".join(argv)
        super().__init__(f"{command} failed ({completed.returncode})" + (f": {detail}" if detail else ""))


def _run_git(repo: Path, args: Sequence[str], budget: SearchBudget) -> subprocess.CompletedProcess[str]:
    budget.check()
    argv = ["git", *args]
    remaining = max(0.1, budget.seconds - (time.monotonic() - budget.started))
    try:
        completed = subprocess.run(
            argv,
            cwd=repo,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            check=False,
            timeout=remaining,
        )
    except subprocess.TimeoutExpired as exc:
        raise SearchTimeout("search truncated: wall-clock budget exceeded while Git was running") from exc
    if completed.returncode != 0:
        raise GitCommandError(argv, completed)
    return completed


def _repo_root(cwd: Path, budget: SearchBudget) -> Path:
    result = _run_git(cwd, ["rev-parse", "--show-toplevel"], budget)
    root = result.stdout.strip()
    if not root:
        raise SearchError("current directory is not inside a Git checkout")
    return Path(root)


def _decode(value: bytes) -> str:
    return value.decode("utf-8", errors="surrogateescape")


class PipeReader:
    """Read a child pipe without allowing a blocked read to defeat the budget."""

    def __init__(self, stream: BinaryIO) -> None:
        self.fd = stream.fileno()
        self.selector = selectors.DefaultSelector()
        self.selector.register(self.fd, selectors.EVENT_READ)
        self.pending = bytearray()
        self.eof = False

    def read_some(self, size: int, budget: SearchBudget) -> bytes:
        while True:
            budget.check()
            events = self.selector.select(budget.remaining())
            if not events:
                raise SearchTimeout(
                    f"search truncated: wall-clock budget exceeded ({budget.seconds:.1f}s) while reading Git output"
                )
            try:
                chunk = os.read(self.fd, size)
            except BlockingIOError:
                continue
            if not chunk:
                self.eof = True
            return chunk

    def read(self, size: int, budget: SearchBudget) -> bytes:
        chunks: list[bytes] = []
        remaining = size
        if self.pending:
            prefix = bytes(self.pending[:remaining])
            del self.pending[: len(prefix)]
            chunks.append(prefix)
            remaining -= len(prefix)
        while remaining:
            chunk = self.read_some(remaining, budget)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)

    def readline(self, budget: SearchBudget) -> bytes:
        while True:
            end = self.pending.find(b"\n")
            if end >= 0:
                line = bytes(self.pending[: end + 1])
                del self.pending[: end + 1]
                return line
            chunk = self.read_some(STREAM_CHUNK_BYTES, budget)
            if not chunk:
                if not self.pending:
                    return b""
                line = bytes(self.pending)
                self.pending.clear()
                return line
            self.pending.extend(chunk)

    def close(self) -> None:
        self.selector.close()


def _iter_nul_records(
    stream: BinaryIO,
    budget: SearchBudget,
    reader: PipeReader | None = None,
) -> Iterator[bytes]:
    pending = bytearray()
    while True:
        budget.check()
        chunk = reader.read_some(STREAM_CHUNK_BYTES, budget) if reader else stream.read(STREAM_CHUNK_BYTES)
        if not chunk:
            break
        pending.extend(chunk)
        while True:
            try:
                end = pending.index(0)
            except ValueError:
                break
            yield bytes(pending[:end])
            del pending[: end + 1]
    if pending:
        yield bytes(pending)


def _normalise_path(path: str) -> str:
    while path.startswith("./"):
        path = path[2:]
    return path or "."


def _explicit_bulk_directories(paths: Sequence[str]) -> set[str]:
    result: set[str] = set()
    for raw_path in paths:
        path = _normalise_path(raw_path)
        if path.startswith(":("):
            closing = path.find(")")
            if closing < 0:
                continue
            magic = path[2:closing]
            if "exclude" in magic or "!" in magic:
                continue
            path = path[closing + 1 :]
        first = path.split("/", 1)[0]
        if first in BULK_DIRECTORIES:
            result.add(first)
    return result


def _is_default_excluded(path: str, explicit_bulk: set[str]) -> bool:
    first = path.split("/", 1)[0]
    return first in BULK_DIRECTORIES and first not in explicit_bulk


def _resolve_refs(
    repo: Path,
    patterns: Sequence[str],
    budget: SearchBudget,
    refuse_without_paths: bool,
) -> RefSelection:
    format_string = "%(refname)%00%(objectname)%00%(objecttype)"
    proc = subprocess.Popen(
        ["git", "for-each-ref", f"--format={format_string}", *patterns],
        cwd=repo,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=False,
    )
    assert proc.stdout is not None
    reader = PipeReader(proc.stdout)
    commits: dict[str, TreeGroup] = {}
    seen: set[str] = set()
    ref_count = 0
    try:
        while True:
            raw_line = reader.readline(budget)
            if not raw_line:
                break
            fields = _decode(raw_line.rstrip(b"\n")).split("\0")
            if len(fields) != 3:
                continue
            ref, object_id, object_type = fields
            if ref in seen:
                continue
            seen.add(ref)
            commit = object_id if object_type == "commit" else ""
            # Annotated tags are valid user-facing refs.  Resolve only those
            # uncommon non-commit refs rather than spawning one command per branch.
            if object_type == "tag":
                resolved = _run_git(repo, ["rev-parse", "--verify", f"{ref}^{{commit}}"], budget)
                commit = resolved.stdout.strip()
            if not commit:
                continue
            ref_count += 1
            commits.setdefault(commit, TreeGroup()).add_refs([ref])
            if refuse_without_paths and ref_count > MAX_UNSCOPED_REFS:
                proc.terminate()
                break
        return_code = proc.wait(timeout=max(0.1, min(1.0, budget.remaining())))
        stderr = proc.stderr.read().decode("utf-8", errors="replace") if proc.stderr is not None else ""
    except SearchTimeout:
        proc.kill()
        proc.wait()
        raise
    except subprocess.TimeoutExpired as exc:
        proc.kill()
        proc.wait()
        raise SearchTimeout("search truncated: wall-clock budget exceeded while resolving refs") from exc
    finally:
        reader.close()
        if proc.stdout is not None:
            proc.stdout.close()
        if proc.stderr is not None:
            proc.stderr.close()
    if return_code not in (0, -15):
        raise SearchError(f"git for-each-ref failed ({return_code}): {stderr.strip()}")
    if not commits:
        patterns_text = " ".join(patterns)
        raise SearchError(f"no commit refs matched --refs {patterns_text!r}")
    return RefSelection(commits=commits, ref_count=ref_count)


def _resolve_trees(
    repo: Path,
    commits: set[str],
    budget: SearchBudget,
) -> dict[str, str]:
    """Resolve selected commits to root trees without walking unrelated history."""

    trees: dict[str, str] = {}
    if not commits:
        return trees
    selected = sorted(commits)
    for offset in range(0, len(selected), COMMIT_BATCH_SIZE):
        batch = selected[offset : offset + COMMIT_BATCH_SIZE]
        completed = _run_git(
            repo,
            ["rev-list", "--no-walk", "--format=%H%x00%T", *batch],
            budget,
        )
        for line in completed.stdout.splitlines():
            fields = line.split("\0", 1)
            if len(fields) == 2 and fields[0] in commits:
                trees[fields[0]] = fields[1]
    missing = commits - trees.keys()
    if missing:
        sample = ", ".join(sorted(missing)[:3])
        raise SearchError(f"could not resolve {len(missing)} selected commit(s) to trees: {sample}")
    return trees


def _parse_tree_record(record: bytes) -> TreeEntry | None:
    try:
        metadata, raw_path = record.split(b"\t", 1)
        fields = metadata.split()
        if len(fields) != 3 or fields[1] != b"blob":
            return None
        blob = _decode(fields[2])
        path = _decode(raw_path)
    except (ValueError, UnicodeError):
        return None
    return TreeEntry(blob=blob, path=path)


class TreeListingCache:
    """Disk-backed per-invocation cache; never retains a tree's full listing."""

    def __init__(
        self,
        repo: Path,
        paths: Sequence[str],
        explicit_bulk: set[str],
        budget: SearchBudget,
    ) -> None:
        self.repo = repo
        self.paths = tuple(_normalise_path(path) for path in paths)
        self.explicit_bulk = explicit_bulk
        self.budget = budget
        self.directory = tempfile.TemporaryDirectory(prefix="goalflight-search-")
        self.files: dict[str, Path] = {}

    def close(self) -> None:
        self.directory.cleanup()

    def _cache_path(self, tree: str) -> Path:
        return Path(self.directory.name) / tree

    def _write_tree(self, tree: str, cache_path: Path) -> None:
        args = ["ls-tree", "-r", "-z", tree]
        if self.paths:
            args.extend(["--", *self.paths])
        self.budget.check()
        proc = subprocess.Popen(
            ["git", *args],
            cwd=self.repo,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=False,
        )
        assert proc.stdout is not None
        reader = PipeReader(proc.stdout)
        try:
            with cache_path.open("wb") as output:
                for raw_record in _iter_nul_records(proc.stdout, self.budget, reader):
                    entry = _parse_tree_record(raw_record)
                    if entry is None:
                        continue
                    if _is_default_excluded(entry.path, self.explicit_bulk):
                        continue
                    output.write(entry.blob.encode("ascii") + b"\0")
                    output.write(entry.path.encode("utf-8", errors="surrogateescape") + b"\0")
            return_code = proc.wait(timeout=max(0.1, min(1.0, self.budget.remaining())))
        except SearchTimeout:
            proc.kill()
            proc.wait()
            raise
        except subprocess.TimeoutExpired as exc:
            proc.kill()
            proc.wait()
            raise SearchTimeout("search truncated: wall-clock budget exceeded while listing trees") from exc
        finally:
            reader.close()
            if proc.stdout is not None:
                proc.stdout.close()
            if proc.stderr is not None:
                proc.stderr.close()
        if return_code != 0:
            raise SearchError(f"git ls-tree failed for tree {tree}")

    def entries(self, tree: str) -> Iterator[TreeEntry]:
        cache_path = self.files.get(tree)
        if cache_path is None:
            cache_path = self._cache_path(tree)
            self._write_tree(tree, cache_path)
            self.files[tree] = cache_path
        with cache_path.open("rb") as source:
            records = _iter_nul_records(source, self.budget)
            while True:
                try:
                    blob = next(records)
                    path = next(records)
                except StopIteration:
                    return
                yield TreeEntry(blob=_decode(blob), path=_decode(path))


def _fast_pathspecs(paths: Sequence[str]) -> tuple[str, ...] | None:
    """Return literal path prefixes safe for the batched tree walker."""

    normalised = tuple(_normalise_path(path) for path in paths)
    if not normalised:
        return None
    for path in normalised:
        if path == ".":
            continue
        if "/" in path or path.endswith("/") or path.startswith(("!", "^", ":")):
            return None
        if any(token in path for token in ("*", "?", "[", "]", "\\")):
            return None
    return normalised


def _fast_path_matches(path: str, paths: Sequence[str]) -> bool:
    return any(candidate == "." or path == candidate or path.startswith(f"{candidate}/") for candidate in paths)


class TreeObjectIndex:
    """Current selected-tree graph loaded through bounded Git batches."""

    def __init__(
        self,
        repo: Path,
        roots: Sequence[str],
        paths: Sequence[str],
        explicit_bulk: set[str],
        budget: SearchBudget,
    ) -> None:
        self.budget = budget
        self.paths = paths
        self.explicit_bulk = explicit_bulk
        self.store = tempfile.TemporaryFile(prefix="goalflight-search-trees-")
        self.index: dict[str, tuple[int, int]] = {}
        self.object_id_bytes = 20
        self._candidate_order: list[str] = []
        self._load_graph(repo, roots)

    def _load_graph(self, repo: Path, roots: Sequence[str]) -> None:
        if not roots:
            return
        records = self._rev_list_objects(repo, roots)
        object_ids = list(dict.fromkeys(object_id for object_id, _ in records))
        object_types = self._object_types(repo, object_ids)
        self._load_batch(repo, [object_id for object_id in object_ids if object_types.get(object_id) == "tree"])
        seen_blobs: set[str] = set()
        for object_id, path in records:
            if (
                path is not None
                and object_types.get(object_id) == "blob"
                and not _is_default_excluded(path, self.explicit_bulk)
                and _fast_path_matches(path, self.paths)
                and object_id not in seen_blobs
            ):
                seen_blobs.add(object_id)
                self._candidate_order.append(object_id)

    def _rev_list_objects(self, repo: Path, roots: Sequence[str]) -> list[tuple[str, str | None]]:
        pathspecs = list(self.paths)
        if "." in self.paths:
            pathspecs.extend(
                f":(exclude){directory}"
                for directory in sorted(BULK_DIRECTORIES - self.explicit_bulk)
            )
        self.budget.check()
        proc = subprocess.Popen(
            ["git", "rev-list", "--objects", "--stdin", "--no-walk", "-z", "--", *pathspecs],
            cwd=repo,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        payload = b"\n".join(root.encode("ascii") for root in dict.fromkeys(roots)) + b"\n"
        try:
            output, stderr = proc.communicate(input=payload, timeout=max(0.1, self.budget.remaining()))
        except subprocess.TimeoutExpired as exc:
            proc.kill()
            proc.wait()
            raise SearchTimeout("search truncated: wall-clock budget exceeded while walking Git trees") from exc
        if proc.returncode != 0:
            detail = stderr.decode("utf-8", errors="replace").strip()
            raise SearchError(f"git rev-list --objects failed ({proc.returncode})" + (f": {detail}" if detail else ""))
        fields = output.split(b"\0")
        records: list[tuple[str, str | None]] = []
        offset = 0
        while offset < len(fields):
            raw_object_id = fields[offset]
            offset += 1
            if not raw_object_id:
                continue
            path: str | None = None
            if offset < len(fields) and fields[offset].startswith(b"path="):
                path = _decode(fields[offset][5:])
                offset += 1
            records.append((_decode(raw_object_id), path))
        return records

    def _object_types(self, repo: Path, object_ids: Sequence[str]) -> dict[str, str]:
        if not object_ids:
            return {}
        self.budget.check()
        proc = subprocess.Popen(
            ["git", "cat-file", "--batch-check=%(objectname) %(objecttype) %(objectsize)", "--unordered"],
            cwd=repo,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        payload = b"\n".join(object_id.encode("ascii") for object_id in object_ids) + b"\n"
        try:
            output, stderr = proc.communicate(input=payload, timeout=max(0.1, self.budget.remaining()))
        except subprocess.TimeoutExpired as exc:
            proc.kill()
            proc.wait()
            raise SearchTimeout("search truncated: wall-clock budget exceeded while classifying Git objects") from exc
        if proc.returncode != 0:
            detail = stderr.decode("utf-8", errors="replace").strip()
            raise SearchError(f"git cat-file --batch-check failed ({proc.returncode})" + (f": {detail}" if detail else ""))
        object_types: dict[str, str] = {}
        for line in output.splitlines():
            fields = line.split()
            if len(fields) == 3:
                object_types[_decode(fields[0])] = _decode(fields[1])
        return object_types

    def _load_batch(self, repo: Path, tree_ids: Sequence[str]) -> None:
        tree_ids = list(dict.fromkeys(tree for tree in tree_ids if tree not in self.index))
        if not tree_ids:
            return
        self.budget.check()
        self.store.seek(0, os.SEEK_END)
        start = self.store.tell()
        proc = subprocess.Popen(
            ["git", "cat-file", "--batch=%(objectname) %(objecttype) %(objectsize)", "--unordered"],
            cwd=repo,
            stdin=subprocess.PIPE,
            stdout=self.store,
            stderr=subprocess.PIPE,
        )
        payload = b"\n".join(tree.encode("ascii") for tree in tree_ids) + b"\n"
        try:
            _, stderr = proc.communicate(input=payload, timeout=max(0.1, self.budget.remaining()))
        except subprocess.TimeoutExpired as exc:
            proc.kill()
            proc.wait()
            raise SearchTimeout("search truncated: wall-clock budget exceeded while loading Git trees") from exc
        if proc.returncode != 0:
            detail = stderr.decode("utf-8", errors="replace").strip()
            raise SearchError(f"git cat-file --batch failed ({proc.returncode})" + (f": {detail}" if detail else ""))
        self._index_store(start)

    def _index_store(self, start: int) -> None:
        self.store.flush()
        self.store.seek(0, os.SEEK_END)
        end = self.store.tell()
        self.store.seek(start)
        while self.store.tell() < end:
            self.budget.check()
            header = self.store.readline()
            if not header:
                raise SearchError("Git tree batch ended before its declared output")
            fields = header.rstrip(b"\n").split()
            if len(fields) == 2 and fields[1] == b"missing":
                continue
            if len(fields) != 3 or fields[1] != b"tree":
                raise SearchError("unexpected git cat-file tree response")
            try:
                size = int(fields[2])
            except ValueError as exc:
                raise SearchError("invalid Git tree object size") from exc
            if size < 0:
                raise SearchError("invalid negative Git tree object size")
            object_id = _decode(fields[0])
            if not self.index:
                self.object_id_bytes = len(fields[0]) // 2
            if len(fields[0]) not in (40, 64):
                raise SearchError(f"invalid Git tree object id {object_id}")
            offset = self.store.tell()
            self.index[object_id] = (offset, size)
            self.store.seek(size, os.SEEK_CUR)
            if self.store.read(1) != b"\n":
                raise SearchError(f"incomplete Git tree object {object_id}")

    def entries(self, tree: str) -> Iterator[RawTreeEntry]:
        location = self.index.get(tree)
        if location is None:
            raise SearchError(f"Git tree {tree} was not included in the object walk")
        offset, size = location
        self.store.seek(offset)
        if size <= TREE_READ_BYTES:
            data = self.store.read(size)
            if len(data) != size:
                raise SearchError(f"Git tree {tree} ended before its declared size")
            yield from self._entries_from_body(tree, data)
            return
        remaining = size
        pending = bytearray()
        while remaining:
            self.budget.check()
            chunk = self.store.read(min(STREAM_CHUNK_BYTES, remaining))
            if not chunk:
                raise SearchError(f"Git tree {tree} ended before its declared size")
            remaining -= len(chunk)
            pending.extend(chunk)
            while True:
                space = pending.find(b" ")
                if space < 0:
                    break
                nul = pending.find(0, space + 1)
                if nul < 0 or len(pending) < nul + 1 + self.object_id_bytes:
                    break
                mode = bytes(pending[:space])
                name = bytes(pending[space + 1 : nul])
                end = nul + 1 + self.object_id_bytes
                object_id = pending[nul + 1 : end].hex()
                del pending[:end]
                yield RawTreeEntry(mode=mode, object_id=object_id, name=name)
        if pending:
            raise SearchError(f"malformed Git tree object {tree}")

    def _entries_from_body(self, tree: str, data: bytes) -> Iterator[RawTreeEntry]:
        cursor = 0
        entry_count = 0
        while cursor < len(data):
            if entry_count % 1024 == 0:
                self.budget.check()
            space = data.find(b" ", cursor)
            if space < 0:
                raise SearchError(f"malformed Git tree object {tree}")
            nul = data.find(0, space + 1)
            if nul < 0:
                raise SearchError(f"malformed Git tree object {tree}")
            end = nul + 1 + self.object_id_bytes
            if end > len(data):
                raise SearchError(f"malformed Git tree object {tree}")
            yield RawTreeEntry(
                mode=data[cursor:space],
                object_id=data[nul + 1 : end].hex(),
                name=data[space + 1 : nul],
            )
            cursor = end
            entry_count += 1

    def _small_body(self, tree: str) -> bytes | None:
        location = self.index.get(tree)
        if location is None:
            raise SearchError(f"Git tree {tree} was not included in the object walk")
        offset, size = location
        if size > TREE_READ_BYTES:
            return None
        self.store.seek(offset)
        data = self.store.read(size)
        if len(data) != size:
            raise SearchError(f"Git tree {tree} ended before its declared size")
        return data

    def candidate_blobs(self, roots: Sequence[str]) -> list[str]:
        """Return current-path blob IDs once, in Git's traversal order."""

        return self._candidate_order

    def matching_summary(self, matched_blobs: set[str]):
        """Build memoized relative paths for matched blobs in each subtree."""

        if not matched_blobs:
            return lambda tree: {}

        memo: dict[str, dict[str, list[str]]] = {}
        blob_alternatives = b"|".join(
            re.escape(bytes.fromhex(blob)) for blob in sorted(matched_blobs)
        )
        entry_pattern = re.compile(
            rb"(?:\A|(?<=\0.{" + str(self.object_id_bytes).encode("ascii") + rb"}))(?:"
            + rb"(?:40000|040000) ([^\0]*)\0(.{" + str(self.object_id_bytes).encode("ascii") + rb"})"
            + rb"|[0-9]+ ([^\0]*)\0(" + blob_alternatives + rb"))",
            re.DOTALL,
        )

        def summarize(tree: str) -> dict[str, list[str]]:
            cached = memo.get(tree)
            if cached is not None:
                return cached
            summary: dict[str, list[str]] = {}
            body = self._small_body(tree)
            if body is not None:
                for match in entry_pattern.finditer(body):
                    child_name = match.group(1)
                    if child_name is not None:
                        child = summarize(match.group(2).hex())
                        for blob, child_paths in child.items():
                            paths = summary.setdefault(blob, [])
                            for child_path in child_paths:
                                if len(paths) >= MAX_PATH_EXAMPLES:
                                    break
                                paths.append(f"{_decode(child_name)}/{child_path}")
                        continue
                    blob = match.group(4).hex()
                    paths = summary.setdefault(blob, [])
                    if len(paths) < MAX_PATH_EXAMPLES:
                        paths.append(_decode(match.group(3)))
                memo[tree] = summary
                return summary
            for entry in self.entries(tree):
                self.budget.check()
                name = _decode(entry.name)
                if entry.mode.startswith(b"10") or entry.mode == b"120000":
                    if entry.object_id in matched_blobs:
                        summary.setdefault(entry.object_id, []).append(name)
                    continue
                if entry.mode not in (b"40000", b"040000"):
                    continue
                child = summarize(entry.object_id)
                for blob, child_paths in child.items():
                    paths = summary.setdefault(blob, [])
                    for child_path in child_paths:
                        if len(paths) >= MAX_PATH_EXAMPLES:
                            break
                        paths.append(f"{name}/{child_path}")
            memo[tree] = summary
            return summary

        return summarize

    def close(self) -> None:
        self.store.close()


class BlobBatch:
    """One long-lived git cat-file process, with chunked blob scanning."""

    def __init__(self, repo: Path, budget: SearchBudget) -> None:
        self.budget = budget
        self.proc = subprocess.Popen(
            ["git", "cat-file", "--batch"],
            cwd=repo,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=False,
        )
        assert self.proc.stdin is not None
        assert self.proc.stdout is not None
        self.reader = PipeReader(self.proc.stdout)

    def _read_exact(self, size: int) -> bytes:
        remaining = size
        chunks: list[bytes] = []
        while remaining:
            chunk = self.reader.read(min(STREAM_CHUNK_BYTES, remaining), self.budget)
            if not chunk:
                raise SearchError("git cat-file ended before returning a complete object")
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)

    def _discard(self, size: int) -> None:
        remaining = size
        while remaining:
            chunk = self.reader.read(min(STREAM_CHUNK_BYTES, remaining), self.budget)
            if not chunk:
                raise SearchError("git cat-file ended while skipping a large blob")
            remaining -= len(chunk)

    def match(self, blob: str, matcher: "BlobMatcher") -> tuple[bool, str]:
        self.budget.check()
        assert self.proc.stdin is not None
        self.proc.stdin.write(blob.encode("ascii") + b"\n")
        self.proc.stdin.flush()
        return self._match_response(blob, matcher)

    def match_many(self, blobs: Sequence[str], matcher: "BlobMatcher") -> list[tuple[bool, str]]:
        self.budget.check()
        assert self.proc.stdin is not None
        for blob in blobs:
            self.proc.stdin.write(blob.encode("ascii") + b"\n")
        self.proc.stdin.flush()
        return [self._match_response(blob, matcher) for blob in blobs]

    def _match_response(self, blob: str, matcher: "BlobMatcher") -> tuple[bool, str]:
        self.budget.check()
        assert self.proc.stdout is not None
        header = self.reader.readline(self.budget)
        if not header:
            raise SearchError("git cat-file exited before returning a blob")
        parts = header.rstrip(b"\n").split()
        if len(parts) == 2 and parts[1] == b"missing":
            raise SearchError(f"blob {blob} disappeared while searching")
        if len(parts) != 3:
            raise SearchError("unexpected git cat-file response")
        object_type = parts[1]
        try:
            size = int(parts[2])
        except ValueError as exc:
            raise SearchError("invalid git cat-file object size") from exc
        if object_type != b"blob":
            self._discard(size)
            self._read_exact(1)
            return False, "non-blob"
        if size > MAX_BLOB_BYTES:
            self._discard(size)
            self._read_exact(1)
            return False, "large"

        matched = False
        binary = False
        carry = b""
        spool = tempfile.TemporaryFile() if matcher.needs_full_blob else None
        try:
            while size:
                self.budget.check()
                chunk_size = min(STREAM_CHUNK_BYTES, size)
                chunk = self.reader.read(chunk_size, self.budget)
                if not chunk:
                    raise SearchError("git cat-file ended inside a blob")
                size -= len(chunk)
                if spool is not None:
                    spool.write(chunk)
                if b"\0" in chunk:
                    binary = True
                if spool is None and not binary and not matched:
                    haystack = carry + chunk
                    if matcher.search(haystack, self.budget):
                        matched = True
                    carry = haystack[-matcher.overlap :] if matcher.overlap else b""
            if spool is not None and not binary:
                matched = matcher.search_file(spool, self.budget)
        finally:
            if spool is not None:
                spool.close()
        self._read_exact(1)
        if binary:
            return False, "binary"
        return matched, "matched" if matched else "text"

    def close(self) -> None:
        if self.proc.stdin is not None:
            self.proc.stdin.close()
        try:
            self.proc.wait(timeout=1)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait()
        if self.proc.stdout is not None:
            self.proc.stdout.close()
        if self.proc.stderr is not None:
            self.proc.stderr.close()
        self.reader.close()


class BlobMatcher:
    def __init__(self, pattern: str, fixed: bool, ignore_case: bool) -> None:
        raw = pattern.encode("utf-8")
        self.fixed = fixed
        if fixed:
            self.needle = raw.lower() if ignore_case else raw
            self.regex = None
            self.needs_full_blob = False
            self.overlap = max(0, len(raw) - 1)
        else:
            flags = re.IGNORECASE if ignore_case else 0
            try:
                self.regex = re.compile(raw, flags)
            except re.error as exc:
                raise SearchError(f"invalid regular expression: {exc}") from exc
            self.needle = None
            self.needs_full_blob = any(token in raw for token in (b"*", b"+", b"{", b"(?", b"\\1"))
            # Code searches normally use short tokens/alternations.  Keep a
            # bounded overlap for the common no-quantifier case.
            self.overlap = min(64 * 1024, max(4096, len(raw) * 4))
        self.ignore_case = ignore_case

    def _regex_search(self, target: object, budget: SearchBudget) -> bool:
        assert self.regex is not None
        budget.check()
        if not self.needs_full_blob or not hasattr(signal, "SIGALRM"):
            return self.regex.search(target) is not None  # type: ignore[arg-type]

        def interrupt(_signum: int, _frame: object) -> None:
            raise SearchTimeout("search truncated: regex evaluation exceeded the wall-clock budget")

        previous = signal.signal(signal.SIGALRM, interrupt)
        signal.setitimer(signal.ITIMER_REAL, max(0.001, budget.remaining()))
        try:
            return self.regex.search(target) is not None  # type: ignore[arg-type]
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, previous)

    def search_file(self, source: BinaryIO, budget: SearchBudget) -> bool:
        source.flush()
        source.seek(0)
        size = os.fstat(source.fileno()).st_size
        if not size:
            return self._regex_search(b"", budget)
        with mmap.mmap(source.fileno(), length=0, access=mmap.ACCESS_READ) as mapped:
            return self._regex_search(mapped, budget)

    def search(self, data: bytes, budget: SearchBudget) -> bool:
        if self.fixed:
            haystack = data.lower() if self.ignore_case else data
            assert self.needle is not None
            return self.needle in haystack
        return self._regex_search(data, budget)


def _result_document(
    *,
    mode: str,
    pattern: str,
    results: list[dict[str, object]],
    stats: dict[str, object],
    truncated: bool = False,
    truncation_reason: str | None = None,
) -> dict[str, object]:
    return {
        "mode": mode,
        "pattern": pattern,
        "results": results,
        "truncated": truncated,
        "truncation_reason": truncation_reason,
        "stats": stats,
    }


def _search_refs(
    repo: Path,
    pattern: str,
    ref_patterns: Sequence[str],
    paths: Sequence[str],
    fixed: bool,
    ignore_case: bool,
    max_results: int,
    budget: SearchBudget,
) -> dict[str, object]:
    selection = _resolve_refs(repo, ref_patterns, budget, refuse_without_paths=not paths)
    if selection.ref_count > MAX_UNSCOPED_REFS and not paths:
        raise SearchError(
            f"ref search refused: more than {MAX_UNSCOPED_REFS} refs matched without PATH filters "
            f"(limit {MAX_UNSCOPED_REFS}). Add paths after --, for example "
            "`-- core application`, or narrow --refs patterns."
        )

    commit_groups = selection.commits
    trees_by_commit = _resolve_trees(repo, set(commit_groups), budget)
    tree_groups: dict[str, TreeGroup] = {}
    for commit, commit_group in commit_groups.items():
        tree = trees_by_commit[commit]
        tree_groups.setdefault(tree, TreeGroup()).merge(commit_group)

    matcher = BlobMatcher(pattern, fixed=fixed, ignore_case=ignore_case)
    results: dict[str, RefHit] = {}
    seen_blobs: set[str] = set()
    stats: dict[str, object] = {
        "refs": selection.ref_count,
        "commits": len(commit_groups),
        "trees": len(tree_groups),
        "blobs_seen": 0,
        "blobs_matched": 0,
        "blobs_skipped_binary": 0,
        "blobs_skipped_large": 0,
    }
    truncated = False
    truncation_reason: str | None = None
    matching_stopped = False
    explicit_bulk = _explicit_bulk_directories(paths)
    fast_paths = _fast_pathspecs(paths)
    tree_cache: TreeListingCache | None = None
    tree_index: TreeObjectIndex | None = None
    batch: BlobBatch | None = None
    pending_occurrences: dict[str, list[tuple[str, str, TreeGroup]]] = {}
    pending_order: list[str] = []
    pending_count = 0

    def flush_pending() -> None:
        nonlocal matching_stopped, pending_count, truncated, truncation_reason
        if not pending_order:
            return
        assert batch is not None
        blobs = list(pending_order)
        outcomes = batch.match_many(blobs, matcher)
        pending_order.clear()
        pending_count = 0
        for blob, (matched, reason) in zip(blobs, outcomes):
            occurrences = pending_occurrences.pop(blob)
            if matching_stopped:
                continue
            if reason == "binary":
                stats["blobs_skipped_binary"] = int(stats["blobs_skipped_binary"]) + 1
            elif reason == "large":
                stats["blobs_skipped_large"] = int(stats["blobs_skipped_large"]) + 1
            if not matched:
                continue
            if len(results) >= max_results:
                truncated = True
                truncation_reason = f"max-results {max_results} reached"
                matching_stopped = True
                continue
            hit = RefHit(blob=blob)
            results[blob] = hit
            stats["blobs_matched"] = len(results)
            for occurrence_tree, occurrence_path, occurrence_group in occurrences:
                hit.add_occurrence(occurrence_tree, occurrence_path, occurrence_group)

    try:
        if fast_paths is not None:
            roots = tuple(tree_groups)
            tree_index = TreeObjectIndex(repo, roots, fast_paths, explicit_bulk, budget)
            candidate_blobs = tree_index.candidate_blobs(roots)
            stats["blobs_seen"] = len(candidate_blobs)
            matched_order: list[str] = []
            matched_blobs: set[str] = set()
            worker_count = (
                min(BLOB_WORKERS, len(candidate_blobs))
                if candidate_blobs and max_results >= 32 and matcher.fixed and not matcher.needs_full_blob
                else 1
            )

            def scan_blob_partition(blob_partition: Sequence[str]) -> list[tuple[bool, str]]:
                local_batch = BlobBatch(repo, budget)
                try:
                    outcomes: list[tuple[bool, str]] = []
                    for offset in range(0, len(blob_partition), BLOB_BATCH_SIZE):
                        budget.check()
                        blob_batch = blob_partition[offset : offset + BLOB_BATCH_SIZE]
                        outcomes.extend(local_batch.match_many(blob_batch, matcher))
                    return outcomes
                finally:
                    local_batch.close()

            def record_blob_outcomes(
                blobs: Sequence[str], outcomes: Sequence[tuple[bool, str]]
            ) -> None:
                nonlocal matching_stopped, truncated, truncation_reason
                for blob, (matched, reason) in zip(blobs, outcomes):
                    if matching_stopped:
                        break
                    if reason == "binary":
                        stats["blobs_skipped_binary"] = int(stats["blobs_skipped_binary"]) + 1
                    elif reason == "large":
                        stats["blobs_skipped_large"] = int(stats["blobs_skipped_large"]) + 1
                    if not matched:
                        continue
                    if len(matched_order) >= max_results:
                        truncated = True
                        truncation_reason = f"max-results {max_results} reached"
                        matching_stopped = True
                        break
                    matched_blobs.add(blob)
                    matched_order.append(blob)
                    results[blob] = RefHit(blob=blob)
                    stats["blobs_matched"] = len(results)

            if worker_count == 1:
                batch = BlobBatch(repo, budget)
                try:
                    scan_batch_size = min(BLOB_BATCH_SIZE, max_results + 1)
                    for offset in range(0, len(candidate_blobs), scan_batch_size):
                        budget.check()
                        if matching_stopped:
                            break
                        blob_batch = candidate_blobs[offset : offset + scan_batch_size]
                        record_blob_outcomes(blob_batch, batch.match_many(blob_batch, matcher))
                finally:
                    batch.close()
            else:
                round_size = BLOB_BATCH_SIZE * worker_count * 8
                with ThreadPoolExecutor(max_workers=worker_count) as pool:
                    for offset in range(0, len(candidate_blobs), round_size):
                        budget.check()
                        if matching_stopped:
                            break
                        blob_round = candidate_blobs[offset : offset + round_size]
                        partitions = [blob_round[index::worker_count] for index in range(worker_count)]
                        partition_outcomes = list(pool.map(scan_blob_partition, partitions))
                        outcome_by_blob = {
                            blob: outcome
                            for partition, outcomes in zip(partitions, partition_outcomes)
                            for blob, outcome in zip(partition, outcomes)
                        }
                        record_blob_outcomes(
                            blob_round,
                            [outcome_by_blob[blob] for blob in blob_round],
                        )
            summary = tree_index.matching_summary(matched_blobs)
            for tree, group in tree_groups.items():
                if tree not in tree_index.index:
                    continue
                for raw_entry in tree_index.entries(tree):
                    budget.check()
                    name = _decode(raw_entry.name)
                    path = name
                    if raw_entry.mode in (b"40000", b"040000"):
                        if _is_default_excluded(path, explicit_bulk) or not _fast_path_matches(path, fast_paths):
                            continue
                        child_summary = summary(raw_entry.object_id)
                        for blob in matched_order:
                            hit = results[blob]
                            for relative_path in child_summary.get(blob, []):
                                full_path = f"{path}/{relative_path}"
                                if not _is_default_excluded(full_path, explicit_bulk):
                                    hit.add_occurrence(tree, full_path, group)
                        continue
                    if (
                        (raw_entry.mode.startswith(b"10") or raw_entry.mode == b"120000")
                        and not _is_default_excluded(path, explicit_bulk)
                        and _fast_path_matches(path, fast_paths)
                    ):
                        hit = results.get(raw_entry.object_id)
                        if hit is not None:
                            hit.add_occurrence(tree, path, group)
        else:
            tree_cache = TreeListingCache(repo, paths, explicit_bulk, budget)
            batch = BlobBatch(repo, budget)
            for tree, group in tree_groups.items():
                budget.check()
                for entry in tree_cache.entries(tree):
                    budget.check()
                    if _is_default_excluded(entry.path, explicit_bulk):
                        continue
                    first_in_tree = entry.blob not in seen_blobs
                    if first_in_tree:
                        seen_blobs.add(entry.blob)
                        stats["blobs_seen"] = len(seen_blobs)
                        if matching_stopped:
                            continue
                        pending_occurrences[entry.blob] = [(tree, entry.path, group)]
                        pending_order.append(entry.blob)
                        pending_count += 1
                        if len(pending_order) >= BLOB_BATCH_SIZE or pending_count >= MAX_PENDING_OCCURRENCES:
                            flush_pending()
                        continue
                    if entry.blob in pending_occurrences:
                        pending_occurrences[entry.blob].append((tree, entry.path, group))
                        pending_count += 1
                        if pending_count >= MAX_PENDING_OCCURRENCES:
                            flush_pending()
                        continue
                    hit = results.get(entry.blob)
                    if hit is not None:
                        hit.add_occurrence(tree, entry.path, group)
            flush_pending()
        result_list = [hit.as_dict() for hit in results.values()]
        return _result_document(
            mode="refs",
            pattern=pattern,
            results=result_list,
            stats=stats,
            truncated=truncated,
            truncation_reason=truncation_reason,
        )
    except SearchTimeout as exc:
        return _result_document(
            mode="refs",
            pattern=pattern,
            results=[hit.as_dict() for hit in results.values()],
            stats=stats,
            truncated=True,
            truncation_reason=str(exc),
        )
    finally:
        if batch is not None:
            batch.close()
        if tree_cache is not None:
            tree_cache.close()
        if tree_index is not None:
            tree_index.close()


def _parse_rg_json_line(raw_line: str) -> dict[str, object] | None:
    try:
        event = json.loads(raw_line)
    except json.JSONDecodeError:
        return None
    if event.get("type") != "match":
        return None
    data = event.get("data")
    if not isinstance(data, dict):
        return None
    path = data.get("path")
    lines = data.get("lines")
    line_number = data.get("line_number")
    if not isinstance(path, dict) or not isinstance(lines, dict):
        return None
    path_text = path.get("text")
    line_text = lines.get("text")
    if not isinstance(path_text, str) or not isinstance(line_text, str):
        return None
    return {"path": path_text, "line": line_number, "text": line_text.rstrip("\n")}


def _iter_rg_matches(reader: PipeReader, budget: SearchBudget) -> Iterator[dict[str, object]]:
    while True:
        raw_line = reader.readline(budget)
        if not raw_line:
            return
        match = _parse_rg_json_line(raw_line.decode("utf-8", errors="replace"))
        if match is not None:
            yield match


def _iter_git_grep_matches(reader: PipeReader, budget: SearchBudget) -> Iterator[dict[str, object]]:
    """Parse `git grep -z` without treating colons in paths as separators."""

    pending = bytearray()
    while True:
        chunk = reader.read_some(STREAM_CHUNK_BYTES, budget)
        if not chunk:
            break
        pending.extend(chunk)
        while True:
            path_end = pending.find(0)
            if path_end < 0:
                break
            line_end = pending.find(0, path_end + 1)
            if line_end < 0:
                break
            text_end = pending.find(b"\n", line_end + 1)
            if text_end < 0:
                break
            path = _decode(bytes(pending[:path_end]))
            line = _decode(bytes(pending[path_end + 1 : line_end]))
            text = _decode(bytes(pending[line_end + 1 : text_end]))
            del pending[: text_end + 1]
            yield {"path": path, "line": int(line) if line.isdigit() else "?", "text": text}
    if pending:
        fields = pending.split(b"\0", 2)
        if len(fields) == 3:
            path, line, text = map(_decode, fields)
            yield {"path": path, "line": int(line) if line.isdigit() else "?", "text": text.rstrip("\n")}


def _search_tree(
    repo: Path,
    pattern: str,
    paths: Sequence[str],
    fixed: bool,
    ignore_case: bool,
    max_results: int,
    budget: SearchBudget,
) -> dict[str, object]:
    rg = shutil.which("rg")
    if rg:
        args = [rg, "--json", "--color", "never"]
        if fixed:
            args.append("--fixed-strings")
        if ignore_case:
            args.append("--ignore-case")
        args.extend(["-e", pattern])
        if paths:
            args.extend(["--", *paths])
        tool_name = "rg"
    else:
        args = ["git", "grep", "--no-color", "--line-number", "--null"]
        args.append("--fixed-strings" if fixed else "--extended-regexp")
        if ignore_case:
            args.append("--ignore-case")
        args.extend(["-e", pattern, "--", *paths])
        tool_name = "git grep"

    budget.check()
    proc = subprocess.Popen(
        args,
        cwd=repo,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=False,
    )
    assert proc.stdout is not None
    reader = PipeReader(proc.stdout)
    matches: list[dict[str, object]] = []
    truncated = False
    try:
        match_iterator = _iter_rg_matches(reader, budget) if rg else _iter_git_grep_matches(reader, budget)
        for match in match_iterator:
            budget.check()
            if len(matches) >= max_results:
                truncated = True
                proc.terminate()
                break
            matches.append(match)
        return_code = proc.wait(timeout=max(0.1, min(1.0, budget.remaining())))
        stderr_bytes = proc.stderr.read() if proc.stderr is not None else b""
        stderr = stderr_bytes.decode("utf-8", errors="replace")
    except SearchTimeout:
        proc.kill()
        proc.wait()
        return _result_document(
            mode="tree",
            pattern=pattern,
            results=matches,
            stats={"matches": len(matches)},
            truncated=True,
            truncation_reason="search truncated: wall-clock budget exceeded while searching the checkout",
        )
    except subprocess.TimeoutExpired as exc:
        proc.kill()
        proc.wait()
        return _result_document(
            mode="tree",
            pattern=pattern,
            results=matches,
            stats={"matches": len(matches)},
            truncated=True,
            truncation_reason="search truncated: wall-clock budget exceeded while searching the checkout",
        )
    finally:
        reader.close()
        if proc.stdout is not None:
            proc.stdout.close()
        if proc.stderr is not None:
            proc.stderr.close()
    if return_code not in (0, 1, -15):
        detail = stderr.strip()
        raise SearchError(f"{tool_name} failed ({return_code})" + (f": {detail}" if detail else ""))
    return _result_document(
        mode="tree",
        pattern=pattern,
        results=matches,
        stats={"matches": len(matches)},
        truncated=truncated,
        truncation_reason=f"max-results {max_results} reached" if truncated else None,
    )


def _parse_history(data: bytes) -> list[dict[str, object]]:
    results: list[dict[str, object]] = []
    for raw_record in data.split(b"\x1e"):
        if not raw_record:
            continue
        fields = raw_record.split(b"\0", 4)
        if len(fields) != 5:
            continue
        commit, date, subject, _separator, raw_paths = fields
        paths = []
        for raw_path in raw_paths.split(b"\0"):
            raw_path = raw_path.lstrip(b"\n")
            if raw_path:
                paths.append(_decode(raw_path))
        results.append(
            {
                "commit": _decode(commit),
                "date": _decode(date),
                "subject": _decode(subject),
                "paths": paths,
            }
        )
    return results


def _search_history(
    repo: Path,
    pattern: str,
    paths: Sequence[str],
    fixed: bool,
    ignore_case: bool,
    max_results: int,
    budget: SearchBudget,
) -> dict[str, object]:
    if not paths:
        raise SearchError(
            "history search requires PATH filters; add `-- <PATH>` to keep the log walk bounded"
        )
    if fixed and not ignore_case:
        pickaxe = f"-S{pattern}"
        extra: list[str] = []
    elif fixed:
        pickaxe = f"-G{re.escape(pattern)}"
        extra = ["--regexp-ignore-case"]
    else:
        pickaxe = f"-G{pattern}"
        extra = ["--regexp-ignore-case"] if ignore_case else []
    args = [
        "log",
        "--all",
        f"--max-count={max_results + 1}",
        "--date=iso-strict",
        f"--format=%x1e%H%x00%aI%x00%s%x00",
        *extra,
        pickaxe,
        "--name-only",
        "-z",
    ]
    args.extend(["--", *paths])
    completed = _run_git(repo, args, budget)
    parsed = _parse_history(completed.stdout.encode("utf-8", errors="surrogateescape"))
    truncated = len(parsed) > max_results
    return _result_document(
        mode="history",
        pattern=pattern,
        results=parsed[:max_results],
        stats={"commits": min(len(parsed), max_results)},
        truncated=truncated,
        truncation_reason=f"max-results {max_results} reached" if truncated else None,
    )


def _print_human(document: dict[str, object]) -> None:
    mode = document["mode"]
    results = document["results"]
    assert isinstance(results, list)
    if mode == "tree":
        for result in results:
            assert isinstance(result, dict)
            print(f"{result.get('path', '?')}:{result.get('line', '?')}:{result.get('text', '')}")
    elif mode == "history":
        for result in results:
            assert isinstance(result, dict)
            print(f"commit {result.get('commit', '?')}")
            print(f"date {result.get('date', '?')}")
            print(f"subject {result.get('subject', '')}")
            for path in result.get("paths", []):
                print(f"path {path}")
            print()
    else:
        for result in results:
            assert isinstance(result, dict)
            refs = ", ".join(str(ref) for ref in result.get("refs", [])) or "(none)"
            paths = ", ".join(str(path) for path in result.get("paths", [])) or "(none)"
            print(f"blob {result.get('blob', '?')}")
            print(f"  paths: {paths}")
            print(f"  refs ({result.get('ref_count', 0)}): {refs}")
    stats = document.get("stats")
    if isinstance(stats, dict):
        compact_stats = " ".join(f"{key}={value}" for key, value in stats.items())
        print(f"summary results={len(results)} {compact_stats}", file=sys.stderr)
    if document.get("truncated"):
        print(f"truncated: {document.get('truncation_reason')}", file=sys.stderr)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Search the checkout, distinct Git ref trees/blobs, or bounded history."
    )
    parser.add_argument("pattern", help="regular expression, or literal text with --fixed")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--tree", action="store_true", help="search the current checkout (default)")
    modes.add_argument("--refs", nargs="+", metavar="GLOB", help="search refs matching one or more Git ref globs")
    modes.add_argument("--history", action="store_true", help="search bounded history across refs")
    parser.add_argument("--fixed", action="store_true", help="treat PATTERN as fixed text")
    parser.add_argument("-i", "--ignore-case", action="store_true", help="case-insensitive matching")
    parser.add_argument(
        "--max-results",
        type=int,
        default=DEFAULT_MAX_RESULTS,
        metavar="N",
        help=f"cap results (default: {DEFAULT_MAX_RESULTS})",
    )
    parser.add_argument("--json", action="store_true", help="emit one JSON document")
    parser.add_argument("paths", nargs="*", metavar="PATH")
    return parser


def _parse_args(parser: argparse.ArgumentParser, argv: Sequence[str] | None) -> argparse.Namespace:
    """Parse the documented `-- PATH...` form on Python 3.9 as well."""

    raw = list(sys.argv[1:] if argv is None else argv)
    if "--" not in raw:
        return parser.parse_args(raw)
    separator = raw.index("--")
    option_args = raw[:separator]
    path_args = raw[separator + 1 :]
    parsed = parser.parse_args(option_args)
    if parsed.paths:
        parser.error("PATH arguments must appear after the `--` separator")
    parsed.paths = path_args
    return parsed


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = _parse_args(parser, argv)
    if args.max_results <= 0:
        parser.error("--max-results must be greater than zero")
    if args.refs is not None and args.history:
        parser.error("choose one of --tree, --refs, or --history")
    budget = SearchBudget()
    try:
        cwd = Path.cwd()
        repo = _repo_root(cwd, budget)
        if args.refs is not None:
            document = _search_refs(
                repo,
                args.pattern,
                args.refs,
                args.paths,
                args.fixed,
                args.ignore_case,
                args.max_results,
                budget,
            )
        elif args.history:
            document = _search_history(
                repo,
                args.pattern,
                args.paths,
                args.fixed,
                args.ignore_case,
                args.max_results,
                budget,
            )
        else:
            document = _search_tree(
                repo,
                args.pattern,
                args.paths,
                args.fixed,
                args.ignore_case,
                args.max_results,
                budget,
            )
    except SearchError as exc:
        if args.json and isinstance(exc, SearchTimeout):
            mode = "refs" if args.refs is not None else "history" if args.history else "tree"
            print(
                json.dumps(
                    _result_document(
                        mode=mode,
                        pattern=args.pattern,
                        results=[],
                        stats={},
                        truncated=True,
                        truncation_reason=str(exc),
                    ),
                    ensure_ascii=False,
                    sort_keys=True,
                )
            )
            return 2
        print(f"goalflight_search: {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(document, ensure_ascii=False, sort_keys=True))
    else:
        _print_human(document)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
