"""Hermetic tests for Goal Flight's minimal gstack subset installer."""

from __future__ import annotations

from contextlib import contextmanager, redirect_stdout
import io
import os
from pathlib import Path
import shutil
import stat
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
            GOALFLIGHT_GSTACK_SOURCE=str(source),
            GOALFLIGHT_GSTACK_SKILLS_DIR=str(skills),
            GOALFLIGHT_GSTACK_INSTALL="minimal",
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
            GOALFLIGHT_GSTACK_SOURCE=str(source),
            GOALFLIGHT_GSTACK_SKILLS_DIR=str(skills),
            GOALFLIGHT_GSTACK_INSTALL="minimal",
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
            GOALFLIGHT_GSTACK_SOURCE=str(make_source(root)),
            GOALFLIGHT_GSTACK_SKILLS_DIR=str(skills),
            GOALFLIGHT_GSTACK_INSTALL="minimal",
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
            GOALFLIGHT_GSTACK_SOURCE=str(source),
            GOALFLIGHT_GSTACK_SKILLS_DIR=str(skills),
            GOALFLIGHT_GSTACK_INSTALL="skip",
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
            GOALFLIGHT_GSTACK_SOURCE=str(root / "missing-gstack"),
            GOALFLIGHT_GSTACK_SKILLS_DIR=str(skills),
            GOALFLIGHT_GSTACK_INSTALL="minimal",
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
            GOALFLIGHT_GSTACK_SOURCE=str(source),
            GOALFLIGHT_GSTACK_SKILLS_DIR=str(skills),
            GOALFLIGHT_GSTACK_INSTALL="minimal",
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
            GOALFLIGHT_GSTACK_SOURCE=str(source),
            GOALFLIGHT_GSTACK_SKILLS_DIR=str(skills),
            GOALFLIGHT_GSTACK_INSTALL="minimal",
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
            GOALFLIGHT_GSTACK_SOURCE=str(source),
            GOALFLIGHT_GSTACK_SKILLS_DIR=str(skills),
            GOALFLIGHT_GSTACK_INSTALL="full",
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
            GOALFLIGHT_GSTACK_SOURCE=str(source),
            GOALFLIGHT_GSTACK_SKILLS_DIR=str(skills),
            GOALFLIGHT_GSTACK_INSTALL="full",
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
    case_default_non_https_external_source_blocks_without_undoing_subset()
    case_dry_run_previews_minimal_before_skip()
    case_full_delegates_to_setup_and_copies_everything()
    case_full_unsupported_host_falls_back_to_minimal()
    case_doctor_warns_for_subset_only()
    case_doctor_reports_zero_gstack_json()
    case_doctor_reports_cli_present_json()
    print("OK: gstack subset tests pass (14 cases)")


if __name__ == "__main__":
    main()
