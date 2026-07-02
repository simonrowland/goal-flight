#!/usr/bin/env python3

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TASK = ROOT / "goalflight_task.py"


def assert_true(name: str, condition: bool) -> None:
    if not condition:
        raise AssertionError(name)


def run_task(project: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(TASK), "--project-root", str(project), *args],
        cwd=str(ROOT),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=30,
        check=False,
    )


def read_items(project: Path) -> list[dict]:
    path = project / "docs-private" / "tasks.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_migrate_previews_applies_and_leaves_sources_untouched() -> None:
    with tempfile.TemporaryDirectory() as td:
        project = Path(td)
        docs_private = project / "docs-private"
        docs_private.mkdir(parents=True)
        (docs_private / "tasks.jsonl").write_text("", encoding="utf-8")
        source = project / "docs" / "open-work.md"
        source.parent.mkdir()
        source.write_text(
            "\n".join(
                [
                    "# Open work",
                    "",
                    "- Import legacy task one",
                    "- Import legacy task two",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        before = source.read_text(encoding="utf-8")

        proc = run_task(project, "migrate", "--source", "docs/open-work.md", "--no-history")
        assert_true(f"migrate preview exits 0: {proc.stderr}", proc.returncode == 0)
        assert_true("preview reports source count", "PREVIEW: 2 source candidate(s)" in proc.stdout)
        assert_true("preview groups by source", "docs/open-work.md: 2 candidate(s)" in proc.stdout)
        assert_true("preview shows title", "Import legacy task one" in proc.stdout)
        assert_true("preview is dry", "NO CHANGES" in proc.stdout)
        assert_true("preview leaves source untouched", source.read_text(encoding="utf-8") == before)
        assert_true("preview creates no drafts", read_items(project) == [])

        proc = run_task(project, "migrate", "--source", "docs/open-work.md", "--no-history", "--apply")
        assert_true(f"migrate apply exits 0: {proc.stderr}", proc.returncode == 0)
        assert_true("apply reports created drafts", "APPLIED: harvest created 2 draft item(s)" in proc.stdout)
        assert_true("apply prints curation command", "goalflight_task.py list --tag harvest" in proc.stdout)
        assert_true("apply leaves source untouched", source.read_text(encoding="utf-8") == before)

        proc = run_task(project, "list", "--tag", "harvest", "--json")
        assert_true(f"list --tag exits 0: {proc.stderr}", proc.returncode == 0)
        drafts = json.loads(proc.stdout)
        assert_true("two harvest drafts exist", len(drafts) == 2)
        assert_true("drafts are source-linked", all(item.get("source_ref", "").startswith("docs/open-work.md:") for item in drafts))
        assert_true("drafts carry default lane", all(item.get("lane") == "deferred" for item in drafts))

        # Contract negative: double-run --apply is idempotent (dedup skips all).
        proc = run_task(project, "migrate", "--source", "docs/open-work.md", "--no-history", "--apply")
        assert_true(f"migrate re-apply exits 0: {proc.stderr}", proc.returncode == 0)
        assert_true("re-apply creates nothing", "harvest created 0 draft item(s)" in proc.stdout)
        proc = run_task(project, "list", "--tag", "harvest", "--json")
        assert_true("still exactly two drafts after re-apply", len(json.loads(proc.stdout)) == 2)
        assert_true("re-apply leaves source untouched", source.read_text(encoding="utf-8") == before)


def test_migrate_rejects_traversal_sources() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        project = root / "project"
        (project / "docs-private").mkdir(parents=True)
        (project / "docs-private" / "tasks.jsonl").write_text("", encoding="utf-8")
        (root / "outside.md").write_text("- Outside issue\n", encoding="utf-8")

        proc = run_task(project, "migrate", "--source", "../outside.md", "--no-history")
        assert_true("dot-dot source fails", proc.returncode != 0)
        assert_true("dot-dot source refused", "'..' components are not allowed" in proc.stderr)

        proc = run_task(project, "migrate", "--source", str(root / "outside.md"), "--no-history")
        assert_true("absolute source fails", proc.returncode != 0)
        assert_true("absolute source refused", "not absolute" in proc.stderr)


def main() -> None:
    test_migrate_previews_applies_and_leaves_sources_untouched()
    test_migrate_rejects_traversal_sources()
    print("OK: migrate wrapper tests pass")


if __name__ == "__main__":
    main()
