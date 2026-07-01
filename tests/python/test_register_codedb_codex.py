"""Regression tests for register-codedb-codex.py.

codedb is the safe code-intelligence swap-in for the context-mode MCP server that
dispatched codex workers disable (#18). The SHARP edge this script exists to handle:
in `codex exec`, an MCP tool call with NO `approval_mode` configured is cancelled, and
for `codedb_context` that cancellation wedges the worker (an exec-mode user-input
elicitation the headless runner cannot answer) — verified live 2026-06-24 (no per-tool
entries -> wedge; with them -> clean).

Pins (incl. the three pre-landing-review findings):
- render uses the LIVE-or-fallback advertised surface minus the write tool; codedb_context
  always approved; valid TOML.
- classify_registration distinguishes absent / incomplete / complete.
- register_atomically REPAIRS an incomplete (server-only) config — the P1 where a plain
  re-run used to no-op forever on the exact wedge state.
- the flock lock file is NOT unlinked (P2 mutex-split fix).
"""

from __future__ import annotations

import importlib.util
import sys
import tempfile
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

_spec = importlib.util.spec_from_file_location(
    "register_codedb_codex", ROOT / "scripts" / "register-codedb-codex.py"
)
R = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(R)


def assert_true(name: str, cond: bool) -> None:
    if not cond:
        raise AssertionError(name)


def assert_eq(name: str, got: object, expected: object) -> None:
    if got != expected:
        raise AssertionError(f"{name}: got {got!r}, expected {expected!r}")


# A codedb path that cannot spawn -> advertised_codedb_tools() returns None ->
# target_tools() falls back to the READONLY_CODEDB_TOOLS allowlist. Hermetic (no codedb).
_NO_CODEDB = "/nonexistent/codedb-binary"

# Tools that MUST never be auto-approved (write / index-mutating / unknown-semantics).
_MUTATING_CODEDB_TOOLS = ("codedb_edit", "codedb_index", "codedb_snapshot", "codedb_remote")


def test_target_tools_fallback_excludes_edit_includes_context() -> None:
    tools = R.target_tools(_NO_CODEDB)  # query fails -> fallback
    assert_true("codedb_context approved", "codedb_context" in tools)
    assert_true("read tools present", "codedb_symbol" in tools and "codedb_search" in tools)
    assert_eq("sorted+unique", tools, sorted(set(tools)))
    # Fail-closed allowlist: NO mutating/unknown tool is ever in the approve set.
    for w in _MUTATING_CODEDB_TOOLS:
        assert_true(f"{w} NOT auto-approved (allowlist is read-only)", w not in tools)
    # Every approved tool is on the read-only allowlist (invariant by construction).
    for t in tools:
        assert_true(f"{t} is on the read-only allowlist", t in R.READONLY_CODEDB_TOOLS)


def test_advertised_writer_is_not_approved() -> None:
    # Even if codedb advertises a mutating tool (e.g. a future codedb_index/_snapshot),
    # target_tools intersects with the allowlist, so the writer is dropped. Simulate by
    # monkeypatching the live-discovery to advertise writers alongside read tools.
    saved = R.advertised_codedb_tools
    R.advertised_codedb_tools = lambda *a, **k: [  # type: ignore[assignment]
        "codedb_symbol", "codedb_search", "codedb_context",
        "codedb_index", "codedb_snapshot", "codedb_edit", "codedb_newfangled_write",
    ]
    try:
        tools = R.target_tools("/whatever")
        for w in ("codedb_index", "codedb_snapshot", "codedb_edit", "codedb_newfangled_write"):
            assert_true(f"advertised writer {w} dropped", w not in tools)
        assert_true("read tools kept", {"codedb_symbol", "codedb_search", "codedb_context"} <= set(tools))
    finally:
        R.advertised_codedb_tools = saved  # type: ignore[assignment]


def test_render_block_is_valid_toml_and_approves_context() -> None:
    tools = R.target_tools(_NO_CODEDB)
    block = R.render_block("/Users/x/bin/codedb", tools)
    data = tomllib.loads(block)
    server = data["mcp_servers"]["codedb"]
    assert_eq("command", server["command"], "/Users/x/bin/codedb")
    assert_eq("args", server["args"], ["mcp"])
    assert_eq("startup bounded", server["startup_timeout_sec"], 30)
    approved = server["tools"]
    for t in tools:
        assert_eq(f"{t} approve", approved[t]["approval_mode"], "approve")
    assert_true("codedb_context covered (wedging tool)", "codedb_context" in approved)
    assert_true("codedb_edit NOT auto-approved", "codedb_edit" not in approved)


def test_render_block_escapes_special_path() -> None:
    block = R.render_block('/tmp/we"ird/codedb', ["codedb_context"])
    data = tomllib.loads(block)
    assert_eq("escaped path", data["mcp_servers"]["codedb"]["command"], '/tmp/we"ird/codedb')


def test_classify_absent_incomplete_complete() -> None:
    with tempfile.TemporaryDirectory() as d:
        cfg = Path(d) / "config.toml"
        # absent
        cfg.write_text("[features]\nmemories = true\n", encoding="utf-8")
        assert_eq("absent", R.classify_registration(cfg)[0], "absent")
        # incomplete: server block, NO approvals (the bare-server wedge state)
        cfg.write_text(
            '[mcp_servers.codedb]\ncommand = "/bin/codedb"\nargs = ["mcp"]\n', encoding="utf-8"
        )
        assert_eq("incomplete", R.classify_registration(cfg)[0], "incomplete")
        # incomplete: approvals present but NOT for codedb_context
        cfg.write_text(
            '[mcp_servers.codedb]\ncommand = "/bin/codedb"\n'
            '[mcp_servers.codedb.tools.codedb_symbol]\napproval_mode = "approve"\n',
            encoding="utf-8",
        )
        assert_eq("incomplete (no context approval)", R.classify_registration(cfg)[0], "incomplete")
        # complete: codedb_context approved
        cfg.write_text(
            '[mcp_servers.codedb]\ncommand = "/bin/codedb"\n'
            '[mcp_servers.codedb.tools.codedb_context]\napproval_mode = "approve"\n',
            encoding="utf-8",
        )
        assert_eq("complete", R.classify_registration(cfg)[0], "complete")
        # missing file
        assert_eq("missing -> absent", R.classify_registration(Path(d) / "nope.toml")[0], "absent")


def test_register_absent_writes_full_block() -> None:
    with tempfile.TemporaryDirectory() as d:
        cfg = Path(d) / "config.toml"
        cfg.write_text("[features]\nmemories = true\n", encoding="utf-8")
        res = R.register_atomically(cfg, _NO_CODEDB)
        assert_true("not raced", res is not R.RACED)
        action, _backup = res
        assert_eq("action registered", action, "registered")
        data = tomllib.loads(cfg.read_text())
        assert_true("prior preserved", data["features"]["memories"] is True)
        assert_true("server written", "codedb" in data["mcp_servers"])
        assert_eq("now complete", R.classify_registration(cfg)[0], "complete")
        # second run -> RACED (already complete)
        assert_true("idempotent RACED", R.register_atomically(cfg, _NO_CODEDB) is R.RACED)


def test_register_repairs_incomplete_server_only() -> None:
    # The P1 the review caught: a server block WITHOUT approvals must be repaired,
    # not no-op'd. After repair, codedb_context must be approved and the original
    # command/args preserved.
    with tempfile.TemporaryDirectory() as d:
        cfg = Path(d) / "config.toml"
        cfg.write_text(
            '[mcp_servers.codedb]\ncommand = "/custom/codedb"\nargs = ["mcp"]\n', encoding="utf-8"
        )
        assert_eq("starts incomplete", R.classify_registration(cfg)[0], "incomplete")
        action, _ = R.register_atomically(cfg, _NO_CODEDB)
        assert_eq("action repaired", action, "repaired")
        data = tomllib.loads(cfg.read_text())
        assert_eq("command preserved", data["mcp_servers"]["codedb"]["command"], "/custom/codedb")
        approved = R._approved_codedb_tools(data)
        assert_true("codedb_context now approved", "codedb_context" in approved)
        assert_eq("repaired -> complete", R.classify_registration(cfg)[0], "complete")


def _expect_exit4(cfg: Path, needle: str) -> None:
    import io
    from contextlib import redirect_stderr

    before = cfg.read_text()
    err = io.StringIO()
    raised = False
    try:
        with redirect_stderr(err):
            R.register_atomically(cfg, _NO_CODEDB)
    except SystemExit as e:
        raised = True
        assert_eq("exit 4", e.code, 4)
    assert_true("SystemExit raised", raised)
    assert_true(f"message mentions {needle}", needle in err.getvalue())
    assert_eq("config untouched on error", cfg.read_text(), before)


def test_repair_clean_incomplete_no_double_define() -> None:
    # Incomplete config with a server block but NO tool tables -> repair appends every
    # target approval exactly once (no duplicate tables) and reaches complete.
    with tempfile.TemporaryDirectory() as d:
        cfg = Path(d) / "config.toml"
        cfg.write_text('[mcp_servers.codedb]\ncommand = "/bin/codedb"\nargs = ["mcp"]\n', encoding="utf-8")
        action, _ = R.register_atomically(cfg, _NO_CODEDB)
        assert_eq("repaired", action, "repaired")
        text = cfg.read_text()
        data = tomllib.loads(text)  # must still parse
        assert_eq("context table appears once", text.count("[mcp_servers.codedb.tools.codedb_context]"), 1)
        assert_true("codedb_context approved", "codedb_context" in R._approved_codedb_tools(data))
        assert_eq("now complete", R.classify_registration(cfg)[0], "complete")


def test_repair_errors_on_unapproved_existing_tool_table() -> None:
    # A target tool whose table EXISTS without approval_mode = "approve" cannot be
    # append-repaired (double-define) and must NOT be silently skipped (it would stay
    # cancel-prone forever — the re-review P2). Hard-error, listing the offending tool.
    with tempfile.TemporaryDirectory() as d:
        cfg = Path(d) / "config.toml"
        cfg.write_text(
            '[mcp_servers.codedb]\ncommand = "/bin/codedb"\n'
            '[mcp_servers.codedb.tools.codedb_query]\n# no approval_mode here\n',
            encoding="utf-8",
        )
        _expect_exit4(cfg, "codedb_query")


def test_non_approve_value_is_not_complete() -> None:
    # approval_mode = "prompt" still elicits -> NOT complete; and since the table exists
    # without "approve", repair hard-errors rather than double-define (re-review P1).
    with tempfile.TemporaryDirectory() as d:
        cfg = Path(d) / "config.toml"
        cfg.write_text(
            '[mcp_servers.codedb]\ncommand = "/bin/codedb"\n'
            '[mcp_servers.codedb.tools.codedb_context]\napproval_mode = "prompt"\n',
            encoding="utf-8",
        )
        assert_eq('"prompt" is not complete', R.classify_registration(cfg)[0], "incomplete")
        _expect_exit4(cfg, "codedb_context")


def test_malformed_tools_value_errors() -> None:
    # [mcp_servers.codedb] tools = "bad" (non-table) -> appending tool tables corrupts TOML.
    # Must hard-error, untouched (re-review P1).
    with tempfile.TemporaryDirectory() as d:
        cfg = Path(d) / "config.toml"
        cfg.write_text('[mcp_servers.codedb]\ncommand = "/bin/codedb"\ntools = "bad"\n', encoding="utf-8")
        assert_eq("malformed -> incomplete", R.classify_registration(cfg)[0], "incomplete")
        _expect_exit4(cfg, "non-table")


def test_invalid_whole_file_toml_is_not_clobbered() -> None:
    # A config that is INVALID TOML overall (e.g. an unclosed table header) must be
    # classified "malformed" and left UNTOUCHED — never classified absent and replaced
    # with appended-but-still-invalid content (convergence-review P2).
    with tempfile.TemporaryDirectory() as d:
        cfg = Path(d) / "config.toml"
        cfg.write_text("[features\nbroken = true\n", encoding="utf-8")  # unclosed [features
        assert_eq("invalid file -> malformed", R.classify_registration(cfg)[0], "malformed")
        _expect_exit4(cfg, "not valid TOML")


def test_empty_file_is_absent_not_malformed() -> None:
    # An empty / whitespace-only config parses to {} and must register cleanly (absent),
    # not be misread as malformed.
    with tempfile.TemporaryDirectory() as d:
        cfg = Path(d) / "config.toml"
        cfg.write_text("\n  \n", encoding="utf-8")
        assert_eq("empty -> absent", R.classify_registration(cfg)[0], "absent")
        action, _ = R.register_atomically(cfg, _NO_CODEDB)
        assert_eq("registered", action, "registered")
        tomllib.loads(cfg.read_text())  # result must be valid TOML
        assert_eq("now complete", R.classify_registration(cfg)[0], "complete")


def test_register_preserves_curated_complete_config() -> None:
    # A complete config that approves ONLY a curated subset must be left untouched
    # (we don't force-approve tools the user omitted).
    with tempfile.TemporaryDirectory() as d:
        cfg = Path(d) / "config.toml"
        original = (
            '[mcp_servers.codedb]\ncommand = "/bin/codedb"\n'
            '[mcp_servers.codedb.tools.codedb_context]\napproval_mode = "approve"\n'
            '[mcp_servers.codedb.tools.codedb_symbol]\napproval_mode = "approve"\n'
        )
        cfg.write_text(original, encoding="utf-8")
        assert_true("complete no-op RACED", R.register_atomically(cfg, _NO_CODEDB) is R.RACED)
        assert_eq("untouched", cfg.read_text(), original)


def test_lock_file_is_not_unlinked() -> None:
    # P2: the flock lock file must persist (unlinking can split the mutex).
    with tempfile.TemporaryDirectory() as d:
        cfg = Path(d) / "config.toml"
        cfg.write_text("[features]\nx = 1\n", encoding="utf-8")
        R.register_atomically(cfg, _NO_CODEDB)
        assert_true("lock file persists", (Path(d) / ".register-codedb.lock").exists())


def test_main_check_exit_when_no_codedb() -> None:
    import shutil

    saved_which = shutil.which
    saved_home = R.Path.home
    with tempfile.TemporaryDirectory() as d:
        cfg = Path(d) / "config.toml"
        R.Path.home = staticmethod(lambda: Path(d))  # type: ignore[assignment]
        shutil.which = lambda name: "/usr/bin/codex" if name == "codex" else None  # type: ignore[assignment]
        try:
            assert_eq("check exit 1 (absent, codedb missing)", R.main(["--check"]), 1)
            assert_eq("write exit 0 (no-op, codedb missing)", R.main([]), 0)
            assert_true("nothing written", not cfg.exists())
        finally:
            shutil.which = saved_which
            R.Path.home = saved_home  # type: ignore[assignment]


def main() -> None:
    tests = [
        test_target_tools_fallback_excludes_edit_includes_context,
        test_advertised_writer_is_not_approved,
        test_invalid_whole_file_toml_is_not_clobbered,
        test_empty_file_is_absent_not_malformed,
        test_render_block_is_valid_toml_and_approves_context,
        test_render_block_escapes_special_path,
        test_classify_absent_incomplete_complete,
        test_register_absent_writes_full_block,
        test_register_repairs_incomplete_server_only,
        test_repair_clean_incomplete_no_double_define,
        test_repair_errors_on_unapproved_existing_tool_table,
        test_non_approve_value_is_not_complete,
        test_malformed_tools_value_errors,
        test_register_preserves_curated_complete_config,
        test_lock_file_is_not_unlinked,
        test_main_check_exit_when_no_codedb,
    ]
    for t in tests:
        t()
    print(f"PASS tests/python/test_register_codedb_codex.py ({len(tests)} tests)")


if __name__ == "__main__":
    main()
