"""t-260: LIST_TYPE must stay the builtin list across importlib.reload()."""

from __future__ import annotations

import builtins
import importlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import goalflight_task as task  # noqa: E402


def test_list_type_is_builtin_list_after_reload() -> None:
    assert task.LIST_TYPE is builtins.list
    assert task.LIST_TYPE is not task.list
    reloaded = importlib.reload(task)
    assert reloaded.LIST_TYPE is builtins.list
    assert reloaded.LIST_TYPE is not reloaded.list
    assert isinstance([], reloaded.LIST_TYPE)
