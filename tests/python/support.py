"""Shared helpers for the file-runner Python tests."""

from __future__ import annotations

import ast
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from functools import lru_cache
import os
import shutil
import subprocess
import sys
import uuid
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))


def sandbox_exec_unavailable_reason() -> str | None:
    """Return why sandbox-exec cannot run here; raise on other probe failures."""
    if sys.platform != "darwin":
        return "requires macOS sandbox-exec"
    executable = shutil.which("sandbox-exec")
    if executable is None:
        return "sandbox-exec is not installed on this macOS host"
    try:
        result = subprocess.run(
            [executable, "-p", "(version 1)\n(allow default)", "/usr/bin/true"],
            text=True,
            capture_output=True,
            timeout=5,
            check=False,
        )
    except PermissionError:
        return "sandbox denies exec of sandbox-exec"
    except FileNotFoundError:
        raise AssertionError("sandbox-exec disappeared after PATH resolution")
    except subprocess.TimeoutExpired as exc:
        raise AssertionError("sandbox-exec capability probe timed out") from exc
    except OSError as exc:
        raise AssertionError(f"sandbox-exec capability probe failed: {exc}") from exc
    if result.returncode == 0:
        return None
    output = f"{result.stdout or ''}\n{result.stderr or ''}".lower()
    if "operation not permitted" in output or "permission denied" in output:
        return "sandbox denies exec of sandbox-exec"
    raise AssertionError(
        "sandbox-exec capability probe failed without a sandbox refusal: "
        f"exit={result.returncode}, stdout={result.stdout!r}, stderr={result.stderr!r}"
    )


def launchctl_unavailable_reason() -> str | None:
    """Skip launchctl callers only when its recorded sandbox is enforced."""
    # pytest_configure captures the live worker's ledger before test fixtures
    # clear its identity. Per-test environment values are not authoritative:
    # tests often set GOALFLIGHT_DISPATCH_ID to exercise record handling.
    dispatch_id = _RECORDED_DISPATCH_ID
    if dispatch_id:
        if _RECORDED_SANDBOX_ERROR:
            raise AssertionError(_RECORDED_SANDBOX_ERROR)
        if _RECORDED_ENFORCED_PROFILE:
            return (
                f"enforced {_RECORDED_ENFORCED_PROFILE} sandbox "
                "prevents launchctl submit"
            )

    label = f"com.goalflight.capability-probe.{os.getpid()}.{uuid.uuid4().hex}"
    try:
        result = subprocess.run(
            [
                "launchctl", "submit", "-l", label,
                "-o", "/dev/null", "-e", "/dev/null",
                "--", "/usr/bin/true",
            ],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
    except PermissionError:
        return "sandbox denies exec of launchctl"
    except FileNotFoundError:
        return "launchctl is not installed on this host"
    except subprocess.TimeoutExpired as exc:
        raise AssertionError("launchctl capability probe timed out") from exc
    except OSError as exc:
        raise AssertionError(f"launchctl capability probe failed: {exc}") from exc
    if result.returncode == 0:
        return None
    output = f"{result.stdout or ''}\n{result.stderr or ''}".strip()
    lowered = output.lower()
    if "operation not permitted" in lowered or "permission denied" in lowered:
        return "sandbox denies launchctl submit"
    raise AssertionError(
        "launchctl capability probe failed without a sandbox refusal: "
        f"exit={result.returncode}, stdout={result.stdout!r}, stderr={result.stderr!r}"
    )


def node_unavailable_reason() -> str | None:
    """Probe the Node executable used by tests; unknown failures stay visible."""
    executable = shutil.which("node")
    if executable is None:
        return "Node is not installed on this host"
    try:
        result = subprocess.run(
            [executable, "--version"],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
    except PermissionError:
        return "sandbox denies exec of Node"
    except FileNotFoundError:
        raise AssertionError("Node disappeared after PATH resolution")
    except subprocess.TimeoutExpired as exc:
        raise AssertionError("Node capability probe timed out") from exc
    except OSError as exc:
        raise AssertionError(f"Node capability probe failed: {exc}") from exc
    if result.returncode == 0:
        return None
    output = f"{result.stdout or ''}\n{result.stderr or ''}".lower()
    if "operation not permitted" in output or "permission denied" in output:
        return "sandbox denies exec of Node"
    raise AssertionError(
        "Node capability probe failed without a sandbox refusal: "
        f"exit={result.returncode}, stdout={result.stdout!r}, stderr={result.stderr!r}"
    )


AMBIENT_IDENTITY_ENV = (
    "GOALFLIGHT_DISPATCH_ID",
    "GOALFLIGHT_DISPATCH_SCRIPT",
    "GOALFLIGHT_ISOLATED_TEST_FILE",
    "GOALFLIGHT_PROJECT_ROOT",
    "GOALFLIGHT_PROMPT_FILE",
    "GOALFLIGHT_STEER_FILE",
    "GOALFLIGHT_ALLOW_EXTERNAL_STEER_FILE",
    "GOALFLIGHT_CONTROLLER_SESSION_ID",
    "GOALFLIGHT_CONTROLLER_LEASE_NONCE",
    "GOALFLIGHT_CONTROLLER_PID",
    "GOALFLIGHT_CONTROLLER_LABEL",
    "GOALFLIGHT_PROCESS_ROLE",
    "GOALFLIGHT_LISTENER_SLOTS",
    "GOALFLIGHT_LISTENER_LOW_WATER",
    "GOALFLIGHT_PERSISTENT_BACKUP_SLOTS",
    "GOALFLIGHT_WORKTREE_LOCK_FD",
    "GOALFLIGHT_OCCUPANCY_LOCK_FD",
)
AMBIENT_WEBHOOK_ENV = (
    "GOALFLIGHT_WAKE_WEBHOOK_URL",
    "GOALFLIGHT_WAKE_WEBHOOK_SECRET",
    "GOALFLIGHT_WAKE_WEBHOOK_AUTH",
    "GOALFLIGHT_WAKE_WEBHOOK_TIMEOUT_S",
    "GOALFLIGHT_WAKE_WEBHOOK_CONFIG",
)
MACHINE_PATH_ENV = (
    "GOALFLIGHT_DISPATCH_DIR",
    "GOALFLIGHT_STATE_DIR",
    "GOALFLIGHT_CODEX_STATE_DIR",
    "GOALFLIGHT_JOURNAL_DIR",
    "GOALFLIGHT_MESSAGES_DIR",
    "GOALFLIGHT_TASK_STORE_DIR",
    "GOALFLIGHT_WAKE_LEDGER_DIR",
    "GOALFLIGHT_WAKE_LEDGER",
    "GOAL_FLIGHT_PIDFILE_DIR",
    "GOALFLIGHT_PIDFILE_DIR",
    "GOALFLIGHT_FLEET_DIR",
    "XDG_STATE_HOME",
)

_RECORDED_DISPATCH_ID: str | None = None
_RECORDED_ENFORCED_PROFILE: str | None = None
_RECORDED_SANDBOX_ERROR: str | None = None


def enforced_sandbox_profile_from_record(record: object) -> str | None:
    """Normalize the launcher's recorded OS sandbox posture across agent shapes."""
    from goalflight_os_sandbox import OS_SANDBOX_OFF, OS_SANDBOX_PROFILES

    if not isinstance(record, dict) or record.get("state") == "unreadable":
        raise AssertionError("dispatch sandbox posture record is absent or unreadable")
    posture = record.get("os_sandbox")
    if not isinstance(posture, dict):
        raise AssertionError("dispatch record has no OS sandbox posture")

    inactive_profiles = (None, "", OS_SANDBOX_OFF, "none", "host-default")
    active_profiles = set(OS_SANDBOX_PROFILES) - {OS_SANDBOX_OFF}
    shape = str(record.get("shape") or posture.get("shape") or "bash")
    agent = str(record.get("agent") or "")

    # ACP stores its effective profile and whether the OS sandbox was enabled;
    # it does not use the dispatcher's enforced_profile field.
    if shape == "acp" or "enabled" in posture:
        profile = posture.get("profile")
        enabled = posture.get("enabled")
        if (
            enabled is True
            and isinstance(profile, str)
            and profile in active_profiles
        ):
            return profile
        if enabled is False:
            requested = posture.get("requested")
            if profile in ("", "off", "none", "host-default"):
                return None
            if profile is None and requested in inactive_profiles:
                return None
        raise AssertionError("ACP record has an unknown OS sandbox posture")

    if "enforced_profile" not in posture:
        raise AssertionError("dispatch record has no enforced OS sandbox profile")
    profile = posture.get("enforced_profile")
    if isinstance(profile, str) and profile in active_profiles:
        return profile
    if profile not in inactive_profiles:
        raise AssertionError(
            "dispatch record has an invalid enforced OS sandbox profile"
        )
    if profile is not None:
        return None

    # The generic dispatcher records null for Grok even after its macOS wrapper
    # selected Seatbelt. requested + supported distinguish that case from the
    # known no-sandbox posture.
    requested = posture.get("requested_profile")
    supported = posture.get("supported_profile")
    if agent in {"grok-code", "grok-research"}:
        if requested == supported == "read-only":
            return "read-only"
        if requested in inactive_profiles and supported in inactive_profiles:
            return None
        raise AssertionError("Grok record has an unknown OS sandbox posture")

    # Codex, Moonshot, and Cursor use enforced_profile directly. A null value
    # is only conclusive for Cursor when no profile was requested or supported.
    if agent in {"codex", "moonshot", "cursor", "cursor-agent"}:
        if requested in inactive_profiles and supported in inactive_profiles:
            return None
    raise AssertionError("dispatch record has an unknown OS sandbox posture")


def capture_dispatch_sandbox_posture() -> None:
    """Snapshot dispatcher enforcement before pytest isolates worker identity."""
    global _RECORDED_DISPATCH_ID, _RECORDED_ENFORCED_PROFILE, _RECORDED_SANDBOX_ERROR
    dispatch_id = os.environ.get("GOALFLIGHT_DISPATCH_ID")
    _RECORDED_DISPATCH_ID = dispatch_id
    _RECORDED_ENFORCED_PROFILE = None
    _RECORDED_SANDBOX_ERROR = None
    if not dispatch_id:
        return
    from goalflight_ledger import read_record

    try:
        record = read_record(dispatch_id)
    except OSError as exc:
        _RECORDED_SANDBOX_ERROR = (
            f"could not read sandbox posture for dispatch {dispatch_id}: {exc}"
        )
        return
    if record is None:
        _RECORDED_SANDBOX_ERROR = (
            f"sandbox posture record for dispatch {dispatch_id} is absent"
        )
        return
    try:
        _RECORDED_ENFORCED_PROFILE = enforced_sandbox_profile_from_record(record)
    except AssertionError as exc:
        _RECORDED_SANDBOX_ERROR = (
            f"sandbox posture for dispatch {dispatch_id} is unknown: {exc}"
        )


def isolated_machine_env(root: Path) -> dict[str, str]:
    """Return env assignments that pin every machine-global writable default."""
    state = root / "state"
    pids = root / "pids"
    mapping = {
        "GOALFLIGHT_MESSAGES_DIR": str(root / "messages"),
        "GOALFLIGHT_FLEET_DIR": str(root / "fleet"),
        "GOALFLIGHT_JOURNAL_DIR": str(root / "journals"),
        "GOALFLIGHT_TASK_STORE_DIR": str(root / "task-store"),
        "GOALFLIGHT_STATE_DIR": str(state),
        "GOALFLIGHT_CODEX_STATE_DIR": str(root / "codex-state"),
        "GOALFLIGHT_DISPATCH_DIR": str(state / "dispatch"),
        "GOALFLIGHT_WAKE_LEDGER_DIR": str(root / "wake-ledger"),
        "GOAL_FLIGHT_PIDFILE_DIR": str(pids),
        "GOALFLIGHT_PIDFILE_DIR": str(pids),
        # JOURNAL_DIR does not cover the XDG fallback. A test that pops
        # JOURNAL_DIR (and TASK_STORE_DIR) writes ~/.local/state/goal-flight
        # unless XDG_STATE_HOME is redirected too.
        "XDG_STATE_HOME": str(root / "xdg"),
        "GOALFLIGHT_CAPACITY_CONF": os.devnull,
        # Same reason as capacity: a live ~/.goal-flight/wake-webhook.json
        # must not fire HTTP from the suite. /dev/null reads empty.
        "GOALFLIGHT_WAKE_WEBHOOK_CONFIG": os.devnull,
        "PYTHONUNBUFFERED": "1",
    }
    for key, value in mapping.items():
        if key in MACHINE_PATH_ENV and value != os.devnull:
            Path(value).mkdir(parents=True, exist_ok=True)
    return mapping


def make_test_git_repo(root: Path, name: str = "project") -> Path:
    """Create a committed temporary checkout for isolated worktree tests."""
    repo = root / name
    repo.mkdir(parents=True, exist_ok=True)
    if not (repo / ".git").exists():
        for args in (
            ("init", "-b", "main"),
            ("config", "user.email", "goalflight-test@example.invalid"),
            ("config", "user.name", "Goal Flight Test"),
        ):
            subprocess.run(
                ["git", *args],
                cwd=repo,
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
        (repo / "tracked.txt").write_text("base\n", encoding="utf-8")
        subprocess.run(
            ["git", "add", "tracked.txt"],
            cwd=repo,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        subprocess.run(
            ["git", "commit", "-m", "base"],
            cwd=repo,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    return repo


@contextmanager
def registered_child_environment(
    project_root: Path | str,
    *,
    env: Mapping[str, str] | None = None,
    controller_label: str | None = None,
) -> Iterator[dict[str, str]]:
    """Run an ACP/dispatch child with a live checkout-local controller."""
    import goalflight_journal
    import goalflight_ledger
    import goalflight_task
    import goalflight_wake

    project = goalflight_task.resolve_project_root(str(project_root))
    child_env = dict(os.environ if env is None else env)
    child_env.update(
        {
            "GOALFLIGHT_ROOT": str(ROOT),
            "GOALFLIGHT_DISPATCH_SCRIPT": str(SCRIPTS / "goalflight_dispatch.py"),
            "GOALFLIGHT_PROJECT_ROOT": str(project),
        }
    )
    child_env.pop("GOALFLIGHT_CONTROLLER_SESSION_ID", None)
    label = controller_label or f"isolated-test-{os.getpid()}"

    with patch.dict(os.environ, child_env, clear=True):
        authority = goalflight_journal.open_or_create_journal(project)
        principal = goalflight_ledger.process_identity(os.getpid())
        if principal is None:
            raise RuntimeError("isolated child controller has no process identity")
        claimed = authority.claim_or_renew_lease(label, principal=principal)
        if not claimed.committed or claimed.value is None:
            raise AssertionError(
                f"isolated child controller lease failed: {claimed.reason}"
            )
        holder = goalflight_wake.register_lease_holder(
            project,
            controller_label=label,
            lease_nonce=claimed.value.nonce,
        )
        child_env.update(
            {
                "GOALFLIGHT_CONTROLLER_LABEL": label,
                "GOALFLIGHT_CONTROLLER_PID": str(os.getpid()),
                "GOALFLIGHT_CONTROLLER_LEASE_NONCE": claimed.value.nonce,
            }
        )
        os.environ.update(child_env)
        try:
            yield child_env
        finally:
            holder.close()


@lru_cache(maxsize=1)
def _module_tree(test: Path) -> ast.Module:
    return ast.parse(test.read_text(encoding="utf-8"), filename=str(test))


def _is_main_guard(node: ast.stmt) -> bool:
    if not isinstance(node, ast.If) or not isinstance(node.test, ast.Compare):
        return False
    compare = node.test
    if len(compare.ops) != 1 or not isinstance(compare.ops[0], ast.Eq):
        return False
    if len(compare.comparators) != 1:
        return False
    values = (compare.left, compare.comparators[0])
    return any(isinstance(value, ast.Name) and value.id == "__name__" for value in values) and any(
        isinstance(value, ast.Constant) and value.value == "__main__" for value in values
    )


def has_main_driver(test: Path) -> bool:
    """Return whether a module declares the script-style test driver contract."""
    return any(_is_main_guard(node) for node in _module_tree(test).body)


def requires_acp_sdk(test: Path) -> bool:
    """Read the explicit, module-level ACP SDK requirement from a test file."""
    tree = _module_tree(test)
    for node in tree.body:
        targets: list[ast.expr] = []
        value: ast.expr | None = None
        if isinstance(node, ast.Assign):
            targets = node.targets
            value = node.value
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
            value = node.value
        if not any(
            isinstance(target, ast.Name) and target.id == "REQUIRES_ACP_SDK"
            for target in targets
        ):
            continue
        if not isinstance(value, ast.Constant) or not isinstance(value.value, bool):
            raise ValueError(f"{test}: REQUIRES_ACP_SDK must be the literal True or False")
        return value.value
    return False


@lru_cache(maxsize=None)
def acp_sdk_unavailable_reason(interpreter: str) -> str | None:
    """Return why an interpreter cannot meet the declared ACP SDK requirement."""
    path = Path(interpreter).expanduser()
    if not path.is_file():
        return f"configured interpreter does not exist or is not a file: {path}"
    if not os.access(path, os.X_OK):
        return f"configured interpreter is not executable: {path}"
    try:
        probe = subprocess.run(
            [str(path), "-c", "import acp, pydantic"],
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"could not probe configured interpreter {path}: {type(exc).__name__}: {exc}"
    if probe.returncode == 0:
        return None
    detail = next(
        (
            line.strip()
            for line in reversed((probe.stderr + "\n" + probe.stdout).splitlines())
            if line.strip()
        ),
        f"exit {probe.returncode}",
    )
    return f"interpreter {path} cannot import acp and pydantic: {detail}"


def ensure_acp_test_interpreter(test_name: str) -> None:
    """Run an ACP SDK test with the configured interpreter when needed."""
    import goalflight_acp_client

    resolution = goalflight_acp_client.acp_sdk_resolution()
    if resolution.state == goalflight_acp_client.ACP_SDK_IMPORTABLE:
        return
    if (
        resolution.state == goalflight_acp_client.ACP_SDK_REEXEC
        and resolution.target_python
    ):
        os.execv(resolution.target_python, [resolution.target_python, *sys.argv])
    print(
        f"SKIP-FILE: {test_name}: ACP SDK requirement unsatisfied: "
        f"{resolution.reason}"
    )
    raise SystemExit(0)


def _current_test_name() -> str:
    return Path(sys.argv[0]).name or "test"


def _posix_on_windows_reason(reason: str) -> str:
    import goalflight_compat

    try:
        probe = goalflight_compat.probe_wsl(ROOT)
    except Exception as exc:  # pragma: no cover - defensive visibility path
        probe = {"state": f"probe_failed:{type(exc).__name__}", "usable": False}

    if probe.get("usable"):
        reminder = "run this suite under WSL (where these POSIX tests execute)"
    else:
        reminder = (
            "needs the POSIX-for-Windows package - install WSL (`wsl --install`) "
            "and run this suite under WSL"
        )
    state = probe.get("state") or "unknown"
    return f"{reason}; native Windows cannot run POSIX primitives; WSL state={state}; {reminder}"


def skip_posix_on_native_windows(reason: str) -> None:
    """Exit cleanly on native Windows for tests that require POSIX semantics."""
    if os.name != "nt":
        return
    print(f"SKIP-FILE: {_current_test_name()}: {_posix_on_windows_reason(reason)}")
    raise SystemExit(0)


def skip_case_posix_on_native_windows(case_name: str, reason: str) -> bool:
    """Return True after printing a visible native-Windows skip for one case."""
    if os.name != "nt":
        return False
    print(f"SKIP: {case_name}: {_posix_on_windows_reason(reason)}")
    return True


def skip_unless_native_windows(reason: str) -> bool:
    """Skip under pytest, or report a skip and return false in script mode."""
    if os.name == "nt":
        return True
    if "pytest" in sys.modules:
        import pytest

        pytest.skip(reason, allow_module_level=True)
    print(f"SKIP-FILE: {_current_test_name()}: {reason}")
    return False


def note_skip(case_name: str, reason: str) -> None:
    print(f"SKIP: {case_name}: {reason}")


def skip_if_node_unavailable(case_name: str, *, script_mode: bool) -> bool:
    """Skip one Node-dependent case while keeping script drivers running."""
    reason = node_unavailable_reason()
    if reason is None:
        return False
    if script_mode:
        note_skip(case_name, reason)
        return True
    import pytest

    pytest.skip(reason)
