#!/usr/bin/env python3
"""Capture is idempotent by content hash: an ambiguous retry cannot double-mint.

t-376: a store mutation that hangs is indistinguishable from one that
succeeded-then-died, so a `capture` retry after an ambiguous timeout must
resolve to the EXISTING item, not mint a second one. The dedupe key covers
normalized title + kind + lane + severity + project root (never a timestamp);
`--allow-duplicate` is the deliberate-duplicate escape; a done item no longer
matches so a recurrence of the same finding re-mints.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TASK = ROOT / "goalflight_task.py"


def assert_true(name: str, condition: bool) -> None:
    if not condition:
        raise AssertionError(name)


def run_task(project_root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(TASK), "--project-root", str(project_root), *args],
        cwd=str(ROOT),
        env=os.environ.copy(),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=30,
        check=False,
    )


def _new_project(td: str) -> Path:
    project = Path(td)
    (project / "docs-private").mkdir(parents=True)
    return project


def _items(project: Path) -> list[dict]:
    proc = run_task(project, "list", "--json")
    assert_true(f"list ok: {proc.stderr}", proc.returncode == 0)
    return json.loads(proc.stdout)


def test_same_text_twice_returns_same_id_and_reports_existing() -> None:
    with tempfile.TemporaryDirectory() as td:
        project = _new_project(td)
        first = run_task(project, "capture", "Race in retry path")
        assert_true(f"first capture ok: {first.stderr}", first.returncode == 0)
        first_id = first.stdout.strip()
        assert_true("first capture minted hint", f"captured {first_id}" in first.stderr)

        second = run_task(project, "capture", "Race in retry path")
        assert_true(f"retry exits 0: {second.stderr}", second.returncode == 0)
        assert_true(
            "retry returns the SAME id",
            second.stdout.strip() == first_id,
        )
        assert_true(
            "retry says already-captured, not minted",
            f"already captured as {first_id}" in second.stderr,
        )
        assert_true("retry prints no minted hint", f"captured {first_id} (" not in second.stderr)
        items = _items(project)
        assert_true("store holds exactly one item", len(items) == 1)
        assert_true("the one item is the first mint", items[0]["id"] == first_id)


def test_json_retry_marks_already_captured() -> None:
    with tempfile.TemporaryDirectory() as td:
        project = _new_project(td)
        first = run_task(project, "capture", "Queue never says why", "--json")
        assert_true("first --json mints", json.loads(first.stdout)["already_captured"] is False)
        second = run_task(project, "capture", "Queue never says why", "--json")
        assert_true(f"second --json ok: {second.stderr}", second.returncode == 0)
        payload = json.loads(second.stdout)
        assert_true("second --json flags already_captured", payload["already_captured"] is True)
        assert_true("second --json carries the existing id", payload["id"] == json.loads(first.stdout)["id"])


def test_allow_duplicate_mints_a_second_item() -> None:
    with tempfile.TemporaryDirectory() as td:
        project = _new_project(td)
        first = run_task(project, "capture", "Same finding, filed twice on purpose")
        assert_true(f"first ok: {first.stderr}", first.returncode == 0)
        second = run_task(
            project, "capture", "Same finding, filed twice on purpose", "--allow-duplicate"
        )
        assert_true(f"--allow-duplicate ok: {second.stderr}", second.returncode == 0)
        second_id = second.stdout.strip()
        assert_true(
            "--allow-duplicate mints a NEW id",
            second_id != first.stdout.strip(),
        )
        assert_true("duplicate mint says captured", f"captured {second_id}" in second.stderr)
        items = _items(project)
        assert_true("both deliberate duplicates exist", len(items) == 2)

        third = run_task(project, "capture", "Same finding, filed twice on purpose")
        assert_true(f"plain retry ok: {third.stderr}", third.returncode == 0)
        assert_true(
            "plain retry still dedupes against the first live mint",
            third.stdout.strip() == first.stdout.strip(),
        )
        assert_true("still two items", len(_items(project)) == 2)


def test_two_genuinely_different_captures_both_mint() -> None:
    with tempfile.TemporaryDirectory() as td:
        project = _new_project(td)
        one = run_task(project, "capture", "Investigate flaky test")
        two = run_task(project, "capture", "Fix the login redirect")
        assert_true("different texts mint distinct ids", one.stdout.strip() != two.stdout.strip())
        # Same text, different severity: a different statement about the finding.
        plain = run_task(project, "capture", "Ambiguous mint")
        sev = run_task(project, "capture", "Ambiguous mint", "--severity", "P2")
        assert_true(f"severity capture ok: {sev.stderr}", sev.returncode == 0)
        assert_true(
            "same text at a different severity is not a retry",
            sev.stdout.strip() != plain.stdout.strip(),
        )
        assert_true("severity capture is a bug", sev.stdout.strip().startswith("b-"))
        assert_true("four items total", len(_items(project)) == 4)


def test_done_item_does_not_block_recurrence() -> None:
    with tempfile.TemporaryDirectory() as td:
        project = _new_project(td)
        first = run_task(project, "capture", "Intermittent cache flush")
        first_id = first.stdout.strip()
        done = run_task(project, "done", first_id)
        assert_true(f"done ok: {done.stderr}", done.returncode == 0)
        recur = run_task(project, "capture", "Intermittent cache flush")
        assert_true(f"recurrence ok: {recur.stderr}", recur.returncode == 0)
        recur_id = recur.stdout.strip()
        assert_true("recurrence after done re-mints a fresh id", recur_id != first_id)
        assert_true("recurrence says captured", f"captured {recur_id}" in recur.stderr)
        # ...and the fresh mint is itself protected from double-minting.
        retry = run_task(project, "capture", "Intermittent cache flush")
        assert_true("recurrence retry dedupes", retry.stdout.strip() == recur_id)
        assert_true("recurrence retry reports existing", f"already captured as {recur_id}" in retry.stderr)


def test_retry_with_new_annotations_preserves_the_change() -> None:
    """Re-capturing with updated detail must not drop the new fields.

    Same title/kind/lane is a retry (same id, no duplicate), but blockers,
    tags, acceptance, links, and prompt are requirement content. Returning
    success while dropping them is how re-explained requirements vanished.
    """
    with tempfile.TemporaryDirectory() as td:
        project = _new_project(td)
        first = run_task(project, "capture", "Same title, more detail later")
        assert_true(f"first capture ok: {first.stderr}", first.returncode == 0)
        item_id = first.stdout.strip()

        second = run_task(
            project,
            "capture",
            "Same title, more detail later",
            "--blocked-by",
            "q-999",
            "--tag",
            "new-tag",
            "--link",
            "https://example.test/req",
            "--acceptance",
            "must preserve",
            "--prompt",
            "do the thing",
            "--pattern",
            "src/**/*.py",
        )
        assert_true(f"annotated retry exits 0: {second.stderr}", second.returncode == 0)
        assert_true("annotated retry returns the SAME id", second.stdout.strip() == item_id)
        assert_true(
            "annotated retry says updated, not silently dropped",
            f"already captured as {item_id}" in second.stderr
            and "updated" in second.stderr,
        )
        items = _items(project)
        assert_true("store still holds exactly one item", len(items) == 1)
        item = items[0]
        assert_true("the one item is the first mint", item["id"] == item_id)
        assert_true("blocked_by preserved", "q-999" in item.get("blocked_by", []))
        assert_true("tags preserved", "new-tag" in item.get("tags", []))
        assert_true("links preserved", "https://example.test/req" in item.get("links", []))
        assert_true("acceptance preserved", item.get("acceptance") == "must preserve")
        assert_true("prompt preserved", item.get("prompt") == "do the thing")
        assert_true("pattern preserved", item.get("pattern") == "src/**/*.py")

        third = run_task(
            project,
            "capture",
            "Same title, more detail later",
            "--blocked-by",
            "q-999",
            "--tag",
            "new-tag",
            "--acceptance",
            "must preserve",
            "--json",
        )
        assert_true(f"identical annotated retry ok: {third.stderr}", third.returncode == 0)
        payload = json.loads(third.stdout)
        assert_true("identical retry already_captured", payload["already_captured"] is True)
        assert_true("identical retry same id", payload["id"] == item_id)
        assert_true("identical retry did not rewrite", "updated_fields" not in payload)
        assert_true("still one item", len(_items(project)) == 1)


def test_dedupe_retry_does_not_burn_id_sequence() -> None:
    with tempfile.TemporaryDirectory() as td:
        project = _new_project(td)
        first = run_task(project, "capture", "Sequence adjacency probe")
        first_id = first.stdout.strip()
        run_task(project, "capture", "Sequence adjacency probe")
        run_task(project, "capture", "Sequence adjacency probe")
        nxt = run_task(project, "capture", "Sequence adjacency probe (next)")
        expected = f"t-{int(first_id.split('-')[1]) + 1:03d}"
        assert_true(
            f"dedupe hits reserved no ids (next is {expected}, got {nxt.stdout.strip()})",
            nxt.stdout.strip() == expected,
        )


def main() -> None:
    test_same_text_twice_returns_same_id_and_reports_existing()
    test_json_retry_marks_already_captured()
    test_allow_duplicate_mints_a_second_item()
    test_two_genuinely_different_captures_both_mint()
    test_done_item_does_not_block_recurrence()
    test_retry_with_new_annotations_preserves_the_change()
    test_dedupe_retry_does_not_burn_id_sequence()
    print("OK: capture idempotence tests pass")


if __name__ == "__main__":
    main()
