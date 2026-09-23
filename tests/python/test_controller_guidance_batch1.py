"""Living controller docs match the host monitor cap and the lease the code renews."""

from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]

ARMING_DOCS = (
    "SKILL.md",
    "commands/execute.md",
    "commands/resume.md",
    "protocols/controller-mail.md",
    "protocols/dispatch-routing.md",
    "docs/EVENT-ARCHITECTURE.md",
    "docs/controller-behaviours.md",
)

RENEWAL_DOCS = ARMING_DOCS + (
    "protocols/session-preflight.md",
    "protocols/state-handoff.md",
)


def _text(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def test_monitor_docs_state_the_host_cap_and_rearm() -> None:
    for relative in ARMING_DOCS:
        doctrine = _text(relative)
        assert "30 minutes" in doctrine, relative
        assert "renews the lease" in doctrine, relative
        assert "`persistent: true`" not in doctrine, relative
        assert "timeout_ms` inert" not in doctrine, relative
        assert "no timeout" not in doctrine.lower(), relative


def test_docs_do_not_say_a_watchdog_tick_renews() -> None:
    for relative in RENEWAL_DOCS:
        assert "watchdog tick may renew" not in _text(relative), relative
        assert "do not auto-renew" not in _text(relative), relative


def test_resume_drains_mail_before_arming_and_describes_probe() -> None:
    resume = _text("commands/resume.md")
    drain_at = resume.index("relay --drain")
    arm_at = resume.index("goalflight_messages.py supervise")
    assert drain_at < arm_at
    assert '"type":"probe"' in resume or '"type": "probe"' in resume
    assert "--controller-pid-from-ancestry" in _text("SKILL.md")


def test_probe_docs_require_coverage_evidence() -> None:
    for relative in ("commands/resume.md", "protocols/controller-mail.md"):
        text = " ".join(_text(relative).split())
        assert "proves stdout connectivity only, before migration and child spawn" in text
        assert "does not prove armed coverage" in text
        assert "`--chatty` / `--debug`" in text
        assert "default terse output has no full-coverage readiness record" in text
        assert "`live=target=4`" in text
        assert "Supervisor process presence alone is insufficient" in text
        assert "startup `stop` or child failure" in text


def test_native_claude_messaging_is_allowed_with_limits() -> None:
    mail = _text("protocols/controller-mail.md")
    assert "SendMessage" in mail
    assert "grokbot" in mail
    assert "current title" in mail
    assert "no goal-flight bus record" in mail


def test_worker_contract_forbids_forcing_ignored_paths() -> None:
    contract = _text("protocols/worker-contract.md")
    assert "git add -f" in contract
    assert "gitignored" in contract
