"""Shared helpers for the file-runner Python tests."""

from __future__ import annotations

import ast
from functools import lru_cache
import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))


AMBIENT_IDENTITY_ENV = (
    "GOALFLIGHT_DISPATCH_ID",
    "GOALFLIGHT_DISPATCH_SCRIPT",
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


@lru_cache(maxsize=None)
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
    print(f"SKIP: {_current_test_name()}: {_posix_on_windows_reason(reason)}")
    raise SystemExit(0)


def skip_case_posix_on_native_windows(case_name: str, reason: str) -> bool:
    """Return True after printing a visible native-Windows skip for one case."""
    if os.name != "nt":
        return False
    print(f"SKIP: {case_name}: {_posix_on_windows_reason(reason)}")
    return True


def skip_unless_native_windows(reason: str) -> None:
    """Exit cleanly unless this is a real native-Windows Python process."""
    if os.name == "nt":
        return
    print(f"SKIP: {_current_test_name()}: {reason}")
    raise SystemExit(0)


def note_skip(case_name: str, reason: str) -> None:
    print(f"SKIP: {case_name}: {reason}")
