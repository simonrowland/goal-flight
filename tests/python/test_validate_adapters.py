"""Adapter validator is silent when every manifest passes."""

from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import io
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import goalflight_validate_adapters as V  # noqa: E402


def case_format_silent_when_all_pass() -> None:
    code, out, err = V.format_validation_result(17, 17, [], verbose=False)
    assert code == 0
    assert out == ""
    assert err == ""


def case_format_verbose_recovers_count() -> None:
    code, out, err = V.format_validation_result(17, 17, [], verbose=True)
    assert code == 0
    assert out == "schema_validates=17/17\n"
    assert err == ""


def case_format_errors_still_report() -> None:
    errors = ["adapters/codex.json: missing required adapter manifest"]
    code, out, err = V.format_validation_result(16, 17, errors, verbose=False)
    assert code == 1
    assert out == ""
    assert "adapters/codex.json: missing required adapter manifest" in err
    assert err.endswith("schema_validates=16/17\n")
    verbose_code, verbose_out, verbose_err = V.format_validation_result(
        16, 17, errors, verbose=True
    )
    assert verbose_code == 1
    assert verbose_out == ""
    assert verbose_err == err


def case_cli_real_repo_silent_and_verbose() -> None:
    out = io.StringIO()
    err = io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        rc = V.main(["--repo", str(ROOT)])
    assert rc == 0
    assert out.getvalue() == ""
    assert err.getvalue() == ""

    out = io.StringIO()
    err = io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        rc = V.main(["--repo", str(ROOT), "--verbose"])
    assert rc == 0
    assert out.getvalue() == "schema_validates=18/18\n"
    assert err.getvalue() == ""


def case_cli_unhealthy_repo_reports_and_exits_1() -> None:
    with tempfile.TemporaryDirectory(prefix="gf-adapters-") as tmp:
        repo = Path(tmp)
        adapters = repo / "adapters"
        adapters.mkdir()
        (adapters / "agent-adapter.schema.json").write_text("{}\n", encoding="utf-8")
        out = io.StringIO()
        err = io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = V.main(["--repo", str(repo)])
        assert rc == 1
        assert out.getvalue() == ""
        text = err.getvalue()
        assert "missing required adapter manifest" in text
        assert f"schema_validates=0/{len(V.EXPECTED_ADAPTERS)}" in text


def case_cli_exit_codes_unchanged() -> None:
    out = io.StringIO()
    err = io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        assert V.main(["--repo", str(ROOT)]) == 0
        assert V.main(["--repo", str(ROOT), "--verbose"]) == 0
    with tempfile.TemporaryDirectory(prefix="gf-adapters-bad-") as tmp:
        repo = Path(tmp)
        adapters = repo / "adapters"
        adapters.mkdir()
        (adapters / "agent-adapter.schema.json").write_text("{}\n", encoding="utf-8")
        out = io.StringIO()
        err = io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            assert V.main(["--repo", str(repo)]) == 1
            assert V.main(["--repo", str(repo), "--verbose"]) == 1


def main() -> None:
    case_format_silent_when_all_pass()
    case_format_verbose_recovers_count()
    case_format_errors_still_report()
    case_cli_real_repo_silent_and_verbose()
    case_cli_unhealthy_repo_reports_and_exits_1()
    case_cli_exit_codes_unchanged()
    print("OK: validate_adapters terse tests pass")


if __name__ == "__main__":
    main()
