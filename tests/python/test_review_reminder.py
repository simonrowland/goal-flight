"""Tests for the OPTIONAL pre-commit review reminder (scripts/goalflight_review_reminder.py).

Hermetic: runs the script in a tmp cwd with the goalflight.* env knobs explicitly cleared,
so neither the repo's git config nor the developer's environment leaks into the assertions.
"""
import os
import subprocess
import sys
import tempfile
from pathlib import Path

SCRIPT = str(Path(__file__).resolve().parents[2] / "scripts" / "goalflight_review_reminder.py")
_KNOBS = ("GOALFLIGHT_REVIEW_REMINDER", "GOALFLIGHT_REVIEW_STRICT", "GOALFLIGHT_REVIEW_OK")


def _isolated_env(base_extra: dict, home: str) -> dict:
    # Isolate ALL git config + repo-identity sources so only the explicit env knobs /
    # repo-local config under test can matter: drop EVERY inherited GIT_* var
    # (GIT_CONFIG_COUNT/KEY_*/VALUE_* env-injection AND GIT_DIR/GIT_WORK_TREE repo
    # identity), then pin global -> /dev/null and disable system config. Stripping the
    # whole GIT_* class — not each var as a review finds it — is the convergent fix:
    # review rounds 1-3 were all instances of one class (inherited git env leaking
    # into the git subprocess this script shells out to).
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in _KNOBS and not k.startswith("GIT_")
    }
    env["HOME"] = home
    env["XDG_CONFIG_HOME"] = home
    env["GIT_CONFIG_GLOBAL"] = os.devnull
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    env.update(base_extra)
    return env


def _run(env_extra: dict) -> tuple[int, str]:
    with tempfile.TemporaryDirectory() as cwd:  # no repo here -> git config --get is empty
        env = _isolated_env(env_extra, cwd)
        p = subprocess.run(
            [sys.executable, SCRIPT], capture_output=True, text=True, env=env, cwd=cwd
        )
    return p.returncode, p.stderr


def _run_in_git_repo(config: dict, env_extra: dict) -> tuple[int, str]:
    with tempfile.TemporaryDirectory() as repo:
        env = _isolated_env({}, repo)
        subprocess.run(["git", "init", "-q"], cwd=repo, env=env, check=True)
        for key, val in config.items():
            subprocess.run(["git", "config", key, val], cwd=repo, env=env, check=True)
        env.update(env_extra)
        p = subprocess.run(
            [sys.executable, SCRIPT], capture_output=True, text=True, env=env, cwd=repo
        )
    return p.returncode, p.stderr


def test_disabled_is_silent_noop():
    rc, err = _run({})
    assert rc == 0 and err.strip() == ""


def test_enabled_reminder_warns_but_does_not_block():
    rc, err = _run({"GOALFLIGHT_REVIEW_REMINDER": "1"})
    assert rc == 0
    assert "remember to review" in err.lower()


def test_acknowledge_silences_reminder():
    rc, err = _run({"GOALFLIGHT_REVIEW_REMINDER": "1", "GOALFLIGHT_REVIEW_OK": "1"})
    assert rc == 0 and err.strip() == ""


def test_strict_blocks_without_ack():
    rc, err = _run({"GOALFLIGHT_REVIEW_REMINDER": "1", "GOALFLIGHT_REVIEW_STRICT": "1"})
    assert rc == 1
    assert "strict" in err.lower()


def test_strict_override_with_ack_passes():
    rc, err = _run(
        {"GOALFLIGHT_REVIEW_REMINDER": "1", "GOALFLIGHT_REVIEW_STRICT": "1", "GOALFLIGHT_REVIEW_OK": "1"}
    )
    assert rc == 0


def test_strict_is_noop_when_not_enabled():
    # strict alone must not act unless the reminder is enabled (double opt-in)
    rc, err = _run({"GOALFLIGHT_REVIEW_STRICT": "1"})
    assert rc == 0 and err.strip() == ""


# --- git-config path (mirror the env tests + the false-overrides-env fix) ---

def test_config_enables_reminder():
    rc, err = _run_in_git_repo({"goalflight.reviewReminder": "true"}, {})
    assert rc == 0
    assert "remember to review" in err.lower()


def test_config_strict_blocks():
    rc, err = _run_in_git_repo(
        {"goalflight.reviewReminder": "true", "goalflight.reviewStrict": "true"}, {}
    )
    assert rc == 1


def test_config_false_overrides_env_enable():
    # the override fix: explicit `git config ... false` disables even when env enables
    rc, err = _run_in_git_repo(
        {"goalflight.reviewReminder": "false"}, {"GOALFLIGHT_REVIEW_REMINDER": "1"}
    )
    assert rc == 0 and err.strip() == ""


def test_hermetic_against_git_config_env_injection(monkeypatch):
    # git's GIT_CONFIG_COUNT / GIT_CONFIG_KEY_* / GIT_CONFIG_VALUE_* env-injection
    # must NOT leak into the script under test (_isolated_env strips GIT_CONFIG*).
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "goalflight.reviewReminder")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "true")
    rc, err = _run({})  # not enabled via the env knobs -> must remain a silent no-op
    assert rc == 0 and err.strip() == ""


def test_hermetic_against_inherited_git_dir(monkeypatch):
    # an inherited GIT_DIR pointing at a repo with the knob enabled must NOT leak
    # (the other half of the inherited-git-env class: repo identity, not config env).
    with tempfile.TemporaryDirectory() as repo:
        setup_env = _isolated_env({}, repo)
        subprocess.run(["git", "init", "-q"], cwd=repo, env=setup_env, check=True)
        subprocess.run(
            ["git", "config", "goalflight.reviewReminder", "true"],
            cwd=repo, env=setup_env, check=True,
        )
        monkeypatch.setenv("GIT_DIR", os.path.join(repo, ".git"))
        rc, err = _run({})  # disabled by knobs -> stays a no-op despite ambient GIT_DIR
        assert rc == 0 and err.strip() == ""
