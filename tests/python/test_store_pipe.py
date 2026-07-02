#!/usr/bin/env python3
"""Hermetic tests for goalflight_task.py pipe."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TASK = ROOT / "goalflight_task.py"


def assert_true(name: str, condition: bool) -> None:
    if not condition:
        raise AssertionError(name)


def run_task(
    project_root: Path,
    *args: str,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess:
    merged_env = os.environ.copy()
    if env:
        merged_env.update(env)
    return subprocess.run(
        [sys.executable, str(TASK), "--project-root", str(project_root), *args],
        cwd=str(ROOT),
        env=merged_env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=30,
        check=False,
    )


def read_items(project_root: Path) -> list[dict]:
    path = project_root / "docs-private" / "tasks.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def write_items(project_root: Path, items: list[dict]) -> None:
    docs = project_root / "docs-private"
    docs.mkdir(parents=True, exist_ok=True)
    (docs / "tasks.jsonl").write_text(
        "".join(json.dumps(item, separators=(",", ":")) + "\n" for item in items),
        encoding="utf-8",
    )
    proc = run_task(project_root, "sync")
    assert_true(f"sync exits 0: {proc.stderr}", proc.returncode == 0)


def make_stub(tmp: Path) -> tuple[Path, Path]:
    log = tmp / "dispatch-calls.jsonl"
    stub = tmp / "dispatch-stub.py"
    stub.write_text(
        textwrap.dedent(
            f"""\
            #!/usr/bin/env python3
            import json
            import sys
            from pathlib import Path

            Path({str(log)!r}).parent.mkdir(parents=True, exist_ok=True)
            with Path({str(log)!r}).open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(sys.argv[1:]) + "\\n")
            if sys.argv[1:2] == ["drain"]:
                print("DRAIN stub")
            else:
                print("DISPATCH-QUEUED stub")
            """
        ),
        encoding="utf-8",
    )
    stub.chmod(0o755)
    return stub, log


def read_calls(log: Path) -> list[list[str]]:
    if not log.exists():
        return []
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_pipe_dry_run_and_no_prompt_exclusion() -> None:
    with tempfile.TemporaryDirectory() as td:
        project = Path(td)
        prompts = project / "docs-private" / "prompts"
        prompts.mkdir(parents=True)
        (prompts / "first.md").write_text("first prompt\n", encoding="utf-8")
        first = run_task(
            project,
            "new",
            "Prompt-path task",
            "--prompt-path",
            "docs-private/prompts/first.md",
            "--by",
            "tester",
        ).stdout.strip()
        second = run_task(project, "new", "Inline prompt task", "--prompt", "inline prompt", "--by", "tester").stdout.strip()
        third = run_task(project, "new", "No prompt task", "--by", "tester").stdout.strip()
        run_task(project, "new", "Deferred prompt", "--prompt", "later", "--lane", "deferred", "--by", "tester")
        run_task(project, "new", "Held prompt", "--prompt", "blocked", "--lane", "held", "--by", "tester")

        items = read_items(project)
        for item in items:
            if item["id"] == second:
                item["agent"] = "grok-code"
        write_items(project, items)

        # Stub the dispatch entry point so a dry-run that secretly submits
        # would be CAUGHT (zero recorded calls), not just inferred from stdout.
        stub, log = make_stub(project)
        proc = run_task(project, "pipe", "--dry-run", env={"GOALFLIGHT_TASK_PIPE_DISPATCH": str(stub)})
        assert_true(f"pipe dry-run exits 0: {proc.stderr}", proc.returncode == 0)
        assert_true("prompt path task listed", f"{first} -> {(prompts / 'first.md').resolve()} -> codex" in proc.stdout)
        assert_true("inline prompt task listed with record agent", f"{second} -> <inline> -> grok-code" in proc.stdout)
        assert_true("no prompt excluded", f"{third} not piped (no prompt)" in proc.stdout)
        assert_true("deferred frontier excluded", "Deferred prompt" not in proc.stdout)
        assert_true("held frontier excluded", "Held prompt" not in proc.stdout)
        assert_true("dry-run submits NOTHING", read_calls(log) == [])


def test_pipe_submits_with_task_linkage_and_one_drain() -> None:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        project = tmp / "project"
        prompts = project / "docs-private" / "prompts"
        prompts.mkdir(parents=True)
        (prompts / "first.md").write_text("first prompt\n", encoding="utf-8")
        first = run_task(
            project,
            "new",
            "Prompt path task",
            "--prompt-path",
            "docs-private/prompts/first.md",
            "--by",
            "tester",
        ).stdout.strip()
        second = run_task(project, "new", "Inline prompt task", "--prompt", "inline prompt", "--by", "tester").stdout.strip()
        run_task(project, "new", "No prompt task", "--by", "tester")
        stub, log = make_stub(tmp)

        proc = run_task(
            project,
            "pipe",
            "--agent",
            "codex",
            env={"GOALFLIGHT_TASK_PIPE_DISPATCH": str(stub)},
        )
        assert_true(f"pipe exits 0: {proc.stderr}", proc.returncode == 0)
        calls = read_calls(log)
        submit_calls = [call for call in calls if call and call[0] == "--submit"]
        drain_calls = [call for call in calls if call and call[0] == "drain"]
        assert_true("two prompt-ready items submitted", len(submit_calls) == 2)
        assert_true("one drain pass requested", drain_calls == [["drain", "--limit", "1"]])
        for item_id in (first, second):
            call = next(call for call in submit_calls if call[call.index("--task") + 1] == item_id)
            assert_true(f"{item_id} submitted to queue", "--submit" in call)
            assert_true(f"{item_id} disables per-submit drain", "--no-drain-on-submit" in call)
            assert_true(f"{item_id} cwd linked", call[call.index("--cwd") + 1] == str(project.resolve()))
        first_call = next(call for call in submit_calls if call[call.index("--task") + 1] == first)
        second_call = next(call for call in submit_calls if call[call.index("--task") + 1] == second)
        assert_true("prompt-path submitted as absolute prompt-file", first_call[first_call.index("--prompt-file") + 1] == str((prompts / "first.md").resolve()))
        assert_true("inline prompt submitted", second_call[second_call.index("--prompt") + 1] == "inline prompt")


def main() -> None:
    test_pipe_dry_run_and_no_prompt_exclusion()
    test_pipe_submits_with_task_linkage_and_one_drain()
    print("OK: store pipe tests pass")


if __name__ == "__main__":
    main()
