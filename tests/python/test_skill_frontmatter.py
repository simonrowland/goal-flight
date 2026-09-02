"""Skill and command YAML is when-to-use, version-pinned, and host-legal."""

from __future__ import annotations

from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[2]

FORBIDDEN_SKILL_KEYS = ("tags", "triggers", "paths", "allowed-tools")
FORBIDDEN_PHRASES = (
    "check mail",
    "start a long refactor",
    "start a refactor",
)

ROOT_SKILL_KEYS = {
    "name",
    "version",
    "description",
    "when_to_use",
    "disable-model-invocation",
}

CURSOR_SKILL_KEYS = {"name", "description", "disable-model-invocation"}
HOST_SKILL_KEYS = {"name", "description"}

COMMANDS_WITH_ARGS = {
    "ask-questions.md",
    "bug-sweep.md",
    "build-corpus.md",
    "controller-behavior-test.md",
    "decompose-plan.md",
    "execute.md",
    "goal.md",
    "init.md",
    "migrate.md",
    "register-codex.md",
    "validate-dispatch.md",
    "validate-queue.md",
}

SIDE_EFFECT_COMMANDS = {
    "ask-questions.md",
    "bug-sweep.md",
    "build-corpus.md",
    "controller-behavior-test.md",
    "decompose-plan.md",
    "execute.md",
    "goal.md",
    "init.md",
    "migrate.md",
    "register-codex.md",
    "resume.md",
    "self-dispatch-test.md",
    "update.md",
}

SKILL_PAGES = (
    Path("SKILL.md"),
    Path("configs/cursor/skills/goal-flight/SKILL.md"),
    Path("configs/grok/skills/goal-flight/SKILL.md"),
    Path("configs/opencode/skills/goal-flight/SKILL.md"),
    Path("plugins/goal-flight/skills/goal-flight/SKILL.md"),
    Path("plugins/goal-flight/skills/goal-flight-init/SKILL.md"),
    Path("plugins/goal-flight/skills/goal-flight-doctor/SKILL.md"),
)


def _expected_version() -> str:
    return (ROOT / "VERSION").read_text().strip()


def _split_frontmatter(text: str) -> tuple[str, str]:
    assert text.startswith("---\n"), "missing opening frontmatter"
    close = text.find("\n---\n", 4)
    assert close != -1, "missing closing frontmatter"
    return text[4:close], text[close + 5 :]


def _parse_frontmatter(block: str) -> dict[str, str]:
    data: dict[str, str] = {}
    for raw in block.splitlines():
        line = raw.rstrip()
        if not line or line.startswith(" ") or line.startswith("-"):
            raise AssertionError(f"nested or list YAML is not allowed in skill/command frontmatter: {line!r}")
        if ":" not in line:
            raise AssertionError(f"frontmatter line has no key: {line!r}")
        key, value = line.split(":", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        assert key, f"empty frontmatter key in {line!r}"
        data[key] = value
    return data


def _load(rel: Path) -> tuple[dict[str, str], str]:
    text = (ROOT / rel).read_text()
    block, body = _split_frontmatter(text)
    return _parse_frontmatter(block), body


def _assert_named_goal_flight(rel: Path, text: str) -> None:
    lowered = text.lower()
    for phrase in FORBIDDEN_PHRASES:
        assert phrase not in lowered, f"{rel}: frontmatter must not include {phrase!r}"
    assert "goal flight" in lowered or "/goal-flight" in lowered, (
        f"{rel}: text must name Goal Flight or /goal-flight"
    )


def _assert_when_to_use(rel: Path, description: str) -> None:
    _assert_named_goal_flight(rel, description)
    assert "use when" in description.lower(), f"{rel}: description must be when-to-use, not a product blurb"


def test_root_skill_frontmatter_is_when_to_use_and_version_pinned() -> None:
    data, body = _load(Path("SKILL.md"))
    assert set(data) == ROOT_SKILL_KEYS
    assert data["name"] == "goal-flight"
    assert data["version"] == _expected_version()
    assert data["disable-model-invocation"] == "true"
    _assert_when_to_use(Path("SKILL.md"), data["description"])
    _assert_named_goal_flight(Path("SKILL.md"), data["when_to_use"])
    assert body.lstrip().startswith("> ⚠️"), "root SKILL.md body must stay intact after frontmatter"


def test_host_and_plugin_skill_pages_are_when_to_use() -> None:
    for rel in SKILL_PAGES:
        if not (ROOT / rel).is_file():
            continue
        data, _body = _load(rel)
        for key in FORBIDDEN_SKILL_KEYS:
            assert key not in data, f"{rel}: drop unhonored key {key}"
        _assert_when_to_use(rel, data["description"])
        assert data["name"]
        if rel == Path("SKILL.md"):
            continue
        if rel == Path("configs/cursor/skills/goal-flight/SKILL.md"):
            assert set(data) == CURSOR_SKILL_KEYS
            assert data["disable-model-invocation"] == "true"
        else:
            assert set(data) == HOST_SKILL_KEYS


def test_optional_grok_bot_wrapper_uses_same_frontmatter_contract() -> None:
    rel = Path("configs/grok-bot/skills/goal-flight/SKILL.md")
    if not (ROOT / rel).is_file():
        return
    data, _body = _load(rel)
    for key in FORBIDDEN_SKILL_KEYS:
        assert key not in data, f"{rel}: drop unhonored key {key}"
    _assert_when_to_use(rel, data["description"])
    assert data["name"] == "goal-flight"


def test_command_pages_have_when_to_use_frontmatter() -> None:
    commands = sorted((ROOT / "commands").glob("*.md"))
    assert commands, "commands/ is empty"
    for path in commands:
        rel = path.relative_to(ROOT)
        data, _body = _load(rel)
        assert "description" in data, f"{rel}: missing description"
        _assert_when_to_use(rel, data["description"])
        if path.name in COMMANDS_WITH_ARGS:
            assert data.get("argument-hint"), f"{rel}: argument-hint required"
        if path.name in SIDE_EFFECT_COMMANDS:
            assert data.get("disable-model-invocation") == "true", (
                f"{rel}: side-effect command must not auto-invoke"
            )
        if path.name == "execute.md":
            assert data.get("disable-model-invocation") == "true"


def test_doctor_pin_reads_host_wrapper_not_root_bible() -> None:
    text = (ROOT / "scripts/goalflight_doctor.py").read_text()
    assert 'cursor_source = skill_root / "configs/cursor/skills/goal-flight/SKILL.md"' in text
    assert 'grok_source = skill_root / "configs/grok/skills/goal-flight/SKILL.md"' in text
    assert 'opencode_source = skill_root / "configs/opencode/skills/goal-flight/SKILL.md"' in text
    grok_bot = ROOT / "configs/grok-bot/skills/goal-flight/SKILL.md"
    if grok_bot.is_file():
        assert (
            'grok_bot_source = skill_root / "configs/grok-bot/skills/goal-flight/SKILL.md"'
            in text
        ), "grok-bot doctor pin must hash the in-repo wrapper"
