"""Hermetic tests for Goal Flight's minimal gstack subset installer."""

from __future__ import annotations

from contextlib import contextmanager, redirect_stderr, redirect_stdout
import io
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import goalflight_doctor  # noqa: E402
import goalflight_setup  # noqa: E402


MINIMAL = ("review", "plan-eng-review", "office-hours")
EXTERNAL = ("grill-me", "thermo-nuclear-code-quality-review")
REQUIRED = MINIMAL + EXTERNAL
UNWANTED = ("design-review", "browse", "qa")
FIXTURE = ROOT / "tests/fixtures/gstack-subset/fake-gstack"
EXTERNAL_FIXTURE = ROOT / "tests/fixtures/gstack-subset/external"


@contextmanager
def patched_env(**values: str):
    old = {key: os.environ.get(key) for key in values}
    try:
        for key, value in values.items():
            os.environ[key] = value
        yield
    finally:
        for key, value in old.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def make_source(root: Path, *, with_setup: bool = False) -> Path:
    source = root / "gstack"
    shutil.copytree(FIXTURE, source)
    setup = source / "setup"
    if with_setup:
        setup.chmod(setup.stat().st_mode | stat.S_IXUSR)
    else:
        setup.unlink()
    return source


def target_names(path: Path) -> set[str]:
    if not path.exists():
        return set()
    return {item.name for item in path.iterdir()}


def external_env(
    *,
    grill: Path | None = EXTERNAL_FIXTURE / "grill-me.md",
    thermo: Path | None = EXTERNAL_FIXTURE / "thermo-nuclear-code-quality-review.md",
    allow_override: bool = True,
) -> dict[str, str]:
    values: dict[str, str] = {}
    if allow_override:
        values["GOALFLIGHT_ALLOW_EXTERNAL_SOURCE_OVERRIDE"] = "1"
    if grill is not None:
        values["GOALFLIGHT_GSTACK_EXTERNAL_SOURCE_GRILL_ME"] = grill.as_uri()
    if thermo is not None:
        values["GOALFLIGHT_GSTACK_EXTERNAL_SOURCE_THERMO_NUCLEAR_CODE_QUALITY_REVIEW"] = thermo.as_uri()
    return values


def gstack_override_env(source: Path, skills: Path, *, install: str = "minimal") -> dict[str, str]:
    return {
        "GOALFLIGHT_ALLOW_GSTACK_SOURCE_OVERRIDE": "1",
        "GOALFLIGHT_ALLOW_GSTACK_SKILLS_DIR_OVERRIDE": "1",
        "GOALFLIGHT_GSTACK_SOURCE": str(source),
        "GOALFLIGHT_GSTACK_SKILLS_DIR": str(skills),
        "GOALFLIGHT_GSTACK_INSTALL": install,
    }


def capture_stdout(func):
    buffer = io.StringIO()
    with redirect_stdout(buffer):
        result = func()
    return result, buffer.getvalue()


def case_minimal_installs_only_subset_and_license() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        source = make_source(root)
        skills = root / "skills"
        backups = root / "backups"
        with patched_env(
            HOME=str(root / "home"),
            **gstack_override_env(source, skills),
            **external_env(),
        ):
            records = goalflight_setup._run_gstack_addon("codex", dry_run=False, backups_root=backups)
        expected = {f"gstack-{name}" for name in REQUIRED} | {"gstack-LICENSE"}
        assert target_names(skills) == expected
        assert all((skills / f"gstack-{name}" / "SKILL.md").is_file() for name in MINIMAL)
        assert not any((skills / f"gstack-{name}").exists() for name in UNWANTED)
        assert (skills / "gstack-LICENSE").read_text(encoding="utf-8") == "fake license\n"
        grill_text = (skills / "gstack-grill-me/SKILL.md").read_text(encoding="utf-8")
        thermo_text = (skills / "gstack-thermo-nuclear-code-quality-review/SKILL.md").read_text(encoding="utf-8")
        assert "Fixture grill body." in grill_text
        assert "source_url:" in grill_text
        assert 'name: "thermo-nuclear-code-quality-review"' in thermo_text
        assert 'description: "Fixture Thermo Nuclear Review"' in thermo_text
        assert "Fixture thermo body." in thermo_text
        assert "source_url:" in thermo_text
        assert len(records) == 6


def case_minimal_installs_claude_flat_names() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        source = make_source(root)
        skills = root / "skills"
        backups = root / "backups"
        with patched_env(
            HOME=str(root / "home"),
            **gstack_override_env(source, skills),
            **external_env(),
        ):
            records = goalflight_setup._run_gstack_addon("claude-code", dry_run=False, backups_root=backups)
        assert target_names(skills) == set(REQUIRED) | {"gstack-LICENSE"}
        assert all((skills / name / "SKILL.md").is_file() for name in MINIMAL)
        assert not any((skills / f"gstack-{name}").exists() for name in MINIMAL)
        assert len(records) == 6


def case_prefixed_claude_subset_counts_as_installed_without_refetch() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        skills = root / "skills"
        for name in REQUIRED:
            target = skills / f"gstack-{name}"
            target.mkdir(parents=True)
            (target / "SKILL.md").write_text(f"---\nname: gstack-{name}\n---\n", encoding="utf-8")
        with patched_env(
            HOME=str(root / "home"),
            **gstack_override_env(make_source(root), skills),
            **external_env(),
        ), patch("goalflight_setup._fetch_external_skill_text", side_effect=AssertionError("refetch")):
            records, output = capture_stdout(
                lambda: goalflight_setup._run_gstack_addon("claude-code", dry_run=False, backups_root=root / "backups")
            )
        assert records == []
        assert "status=ok detail=minimal_subset" in output
        assert "choices=minimal,full,skip" not in output


def case_skip_copies_nothing() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        source = make_source(root)
        skills = root / "skills"
        with patched_env(
            HOME=str(root / "home"),
            **gstack_override_env(source, skills, install="skip"),
            **external_env(),
        ):
            records = goalflight_setup._run_gstack_addon("codex", dry_run=False, backups_root=root / "backups")
        assert records == []
        assert target_names(skills) == set()


def case_source_missing_still_downloads_external_skills() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        skills = root / "skills"
        with patched_env(
            HOME=str(root / "home"),
            **gstack_override_env(root / "missing-gstack", skills),
            **external_env(),
        ):
            records, output = capture_stdout(
                lambda: goalflight_setup._run_gstack_addon("codex", dry_run=False, backups_root=root / "backups")
            )
        assert len(records) == 2
        assert "ADDON_GSTACK install=blocked reason=source_missing" in output
        assert target_names(skills) == {f"gstack-{name}" for name in EXTERNAL}


def case_external_fetch_failure_blocks_one_skill_without_undoing_subset() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        source = make_source(root)
        skills = root / "skills"
        missing = root / "missing-thermo.md"
        with patched_env(
            HOME=str(root / "home"),
            **gstack_override_env(source, skills),
            **external_env(thermo=missing),
        ):
            records, output = capture_stdout(
                lambda: goalflight_setup._run_gstack_addon("codex", dry_run=False, backups_root=root / "backups")
            )
        expected = {f"gstack-{name}" for name in (*MINIMAL, "grill-me")} | {"gstack-LICENSE"}
        assert target_names(skills) == expected
        assert len(records) == 5
        assert "skill=thermo-nuclear-code-quality-review install=blocked reason=network/source" in output


def case_external_override_without_gate_uses_pinned_source_and_warns() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        override = Path(tmp) / "grill-me.md"
        override.write_text("# ignored\n", encoding="utf-8")
        with patched_env(
            GOALFLIGHT_ALLOW_EXTERNAL_SOURCE_OVERRIDE="0",
            GOALFLIGHT_GSTACK_EXTERNAL_SOURCE_GRILL_ME=override.as_uri(),
        ):
            source, output = capture_stdout(lambda: goalflight_setup._gstack_external_source_url("grill-me"))
        assert source == goalflight_setup.GSTACK_EXTERNAL_SKILL_SOURCES["grill-me"]
        assert "warning=external_source_override_ignored" in output
        assert "GOALFLIGHT_ALLOW_EXTERNAL_SOURCE_OVERRIDE_not_1" in output


def case_gstack_source_env_requires_allow_gate() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        source = make_source(root)
        with patched_env(
            HOME=str(root / "home"),
            GOALFLIGHT_GSTACK_SOURCE=str(source),
            GOALFLIGHT_ALLOW_GSTACK_SOURCE_OVERRIDE="0",
        ):
            err = io.StringIO()
            with redirect_stderr(err):
                candidates = goalflight_setup._gstack_source_candidates()
        assert source not in candidates
        assert "env=GOALFLIGHT_GSTACK_SOURCE action=ignored" in err.getvalue()


def case_gstack_skills_dir_env_requires_allow_gate() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        home = root / "home"
        override = root / "override-skills"
        with patched_env(
            HOME=str(home),
            GOALFLIGHT_GSTACK_SKILLS_DIR=str(override),
            GOALFLIGHT_ALLOW_GSTACK_SKILLS_DIR_OVERRIDE="0",
        ):
            err = io.StringIO()
            with redirect_stderr(err):
                skills_dir = goalflight_setup._gstack_host_skills_dir("codex")
        assert skills_dir == home / ".codex/skills"
        assert "env=GOALFLIGHT_GSTACK_SKILLS_DIR action=ignored" in err.getvalue()


def case_setup_fake_logs_require_test_mode() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        codex_log = root / "fake-codex.log"
        context_log = root / "fake-context.log"

        def fake_run(argv, **_kwargs):
            text = "goal-flight@goal-flight installed enabled" if argv[:3] == ["codex", "plugin", "list"] else ""
            return subprocess.CompletedProcess(argv, 0, stdout=text, stderr="")

        with patched_env(
            GOALFLIGHT_SETUP_FAKE_CODEX_LOG=str(codex_log),
            GOALFLIGHT_SETUP_FAKE_CONTEXT_MODE_LOG=str(context_log),
            GOALFLIGHT_TEST_MODE="0",
        ), patch("goalflight_setup.subprocess.run", side_effect=fake_run):
            err = io.StringIO()
            with redirect_stderr(err):
                goalflight_setup._run_codex_plugin_registration(ROOT)
                goalflight_setup._run_codex_context_mode_registration(ROOT, dry_run=False)
        assert not codex_log.exists()
        assert not context_log.exists()
        assert "env=GOALFLIGHT_SETUP_FAKE_CODEX_LOG action=ignored" in err.getvalue()
        assert "env=GOALFLIGHT_SETUP_FAKE_CONTEXT_MODE_LOG action=ignored" in err.getvalue()

        with patched_env(
            GOALFLIGHT_SETUP_FAKE_CODEX_LOG=str(codex_log),
            GOALFLIGHT_SETUP_FAKE_CONTEXT_MODE_LOG=str(context_log),
            GOALFLIGHT_TEST_MODE="1",
        ):
            err = io.StringIO()
            with redirect_stderr(err), redirect_stdout(io.StringIO()):
                goalflight_setup._run_codex_plugin_registration(ROOT)
                goalflight_setup._run_codex_context_mode_registration(ROOT, dry_run=False)
        assert codex_log.exists()
        assert context_log.exists()
        assert "env=GOALFLIGHT_SETUP_FAKE_CODEX_LOG action=active" in err.getvalue()
        assert "env=GOALFLIGHT_SETUP_FAKE_CONTEXT_MODE_LOG action=active" in err.getvalue()


def case_default_non_https_external_source_blocks_without_undoing_subset() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        source = make_source(root)
        skills = root / "skills"
        non_https_sources = {
            "grill-me": (EXTERNAL_FIXTURE / "grill-me.md").as_uri(),
            "thermo-nuclear-code-quality-review": (
                EXTERNAL_FIXTURE / "thermo-nuclear-code-quality-review.md"
            ).as_uri(),
        }
        with patched_env(
            HOME=str(root / "home"),
            **gstack_override_env(source, skills),
        ), patch.dict(goalflight_setup.GSTACK_EXTERNAL_SKILL_SOURCES, non_https_sources, clear=True):
            records, output = capture_stdout(
                lambda: goalflight_setup._run_gstack_addon("codex", dry_run=False, backups_root=root / "backups")
            )
        expected = {f"gstack-{name}" for name in MINIMAL} | {"gstack-LICENSE"}
        assert target_names(skills) == expected
        assert len(records) == 4
        assert output.count("install=blocked reason=network/source") == 2
        assert "unsupported_scheme=file" in output


def case_dry_run_previews_minimal_before_skip() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        source = make_source(root)
        skills = root / "skills"
        with patched_env(
            HOME=str(root / "home"),
            GOALFLIGHT_ALLOW_GSTACK_SOURCE_OVERRIDE="1",
            GOALFLIGHT_ALLOW_GSTACK_SKILLS_DIR_OVERRIDE="1",
            GOALFLIGHT_GSTACK_SOURCE=str(source),
            GOALFLIGHT_GSTACK_SKILLS_DIR=str(skills),
            **external_env(),
        ):
            records, output = capture_stdout(
                lambda: goalflight_setup._run_gstack_addon("codex", dry_run=True, backups_root=None)
            )
        assert records == []
        preview_at = output.index("ADDON_GSTACK install=minimal")
        skip_at = output.index("ADDON_GSTACK install=skip")
        assert preview_at < skip_at
        assert not skills.exists()


def case_full_delegates_to_setup_and_copies_everything() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        source = make_source(root, with_setup=True)
        skills = root / "skills"
        with patched_env(
            HOME=str(root / "home"),
            **gstack_override_env(source, skills, install="full"),
            **external_env(),
        ):
            records = goalflight_setup._run_gstack_addon("codex", dry_run=False, backups_root=root / "backups")
        expected = {f"gstack-{name}" for name in (*MINIMAL, *UNWANTED, *EXTERNAL)} | {"gstack-LICENSE"}
        assert len(records) == 2
        assert target_names(skills) == expected


def case_full_unsupported_host_falls_back_to_minimal() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        source = make_source(root, with_setup=True)
        skills = root / "skills"
        with patched_env(
            HOME=str(root / "home"),
            **gstack_override_env(source, skills, install="full"),
            **external_env(),
        ):
            records, output = capture_stdout(
                lambda: goalflight_setup._run_gstack_addon("cursor", dry_run=False, backups_root=root / "backups")
            )
        expected = {f"gstack-{name}" for name in REQUIRED} | {"gstack-LICENSE"}
        assert target_names(skills) == expected
        assert not any((skills / f"gstack-{name}").exists() for name in UNWANTED)
        assert "install=full status=unsupported agent=cursor" in output
        assert "fallback=minimal" in output
        assert len(records) == 6


def case_doctor_warns_for_subset_only() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp) / "home"
        skills = home / ".codex/skills"
        for name in REQUIRED:
            target = skills / f"gstack-{name}"
            target.mkdir(parents=True)
            (target / "SKILL.md").write_text(f"---\nname: {name}\n---\n", encoding="utf-8")
        with patched_env(HOME=str(home)), patch("goalflight_doctor.version", return_value={"present": False}):
            payload = goalflight_doctor.check_gstack()
        assert payload["present"] is True
        assert payload["ok"] is True
        assert payload["level"] == "warning"
        assert payload["kind"] == "minimal_subset"
        assert payload["minimal_subset_hosts"] == ["codex"]
        assert payload["host_skill_roots"]["codex"]["missing"] == []


def case_doctor_reports_zero_gstack_json() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp) / "home"
        with patched_env(HOME=str(home)), patch("goalflight_doctor.version", return_value={"present": False}):
            payload = goalflight_doctor.check_gstack()
        assert payload["present"] is False
        assert payload["ok"] is False
        assert payload["level"] == "warning"
        assert payload["minimal_subset_hosts"] == []
        assert payload["host_skill_roots"]["codex"]["missing"] == list(REQUIRED)


def case_doctor_reports_cli_present_json() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp) / "home"
        with patched_env(HOME=str(home)), patch(
            "goalflight_doctor.version",
            return_value={"present": True, "path": "/fake/bin/gstack", "version": "gstack 1.2.3", "ok": True},
        ):
            payload = goalflight_doctor.check_gstack()
        assert payload["present"] is True
        assert payload["ok"] is True
        assert payload["level"] == "ok"
        assert payload["kind"] == "cli"
        assert payload["version"] == "gstack 1.2.3"
        assert payload["minimal_required_skills"] == list(REQUIRED)


def main() -> None:
    case_minimal_installs_only_subset_and_license()
    case_minimal_installs_claude_flat_names()
    case_prefixed_claude_subset_counts_as_installed_without_refetch()
    case_skip_copies_nothing()
    case_source_missing_still_downloads_external_skills()
    case_external_fetch_failure_blocks_one_skill_without_undoing_subset()
    case_external_override_without_gate_uses_pinned_source_and_warns()
    case_gstack_source_env_requires_allow_gate()
    case_gstack_skills_dir_env_requires_allow_gate()
    case_setup_fake_logs_require_test_mode()
    case_default_non_https_external_source_blocks_without_undoing_subset()
    case_dry_run_previews_minimal_before_skip()
    case_full_delegates_to_setup_and_copies_everything()
    case_full_unsupported_host_falls_back_to_minimal()
    case_doctor_warns_for_subset_only()
    case_doctor_reports_zero_gstack_json()
    case_doctor_reports_cli_present_json()
    print("OK: gstack subset tests pass (17 cases)")


if __name__ == "__main__":
    main()
