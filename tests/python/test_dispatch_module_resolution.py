from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import goalflight_dispatch as D  # noqa: E402


def test_prelaunch_failure_status_records_runtime_module_origins(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    status_path = tmp_path / "dispatch.status.json"
    stale_module_path = tmp_path / "other-scripts" / "goalflight_worktree_gc.py"
    monkeypatch.setitem(
        sys.modules,
        "goalflight_worktree_gc",
        SimpleNamespace(__file__=str(stale_module_path)),
    )
    monkeypatch.setattr(D, "_project_root", lambda _args: tmp_path)
    monkeypatch.setattr(D, "_dispatch_base_dir", lambda: tmp_path)
    monkeypatch.setattr(D, "_stamp_prelaunch_controller_identity", lambda *_args: None)
    monkeypatch.setattr(D, "_prelaunch_status_metadata", lambda _args: {})
    monkeypatch.setattr(
        D.goalflight_ledger,
        "record_path",
        lambda *_args, **_kwargs: tmp_path / "absent-ledger-record.json",
    )
    monkeypatch.setattr(D, "_record_ledger", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(D, "_finish_ledger", lambda *_args, **_kwargs: None)

    D._record_prelaunch_failure(
        SimpleNamespace(
            dispatch_id="runtime-origin-test",
            status_json=str(status_path),
            tail=str(tmp_path / "dispatch.tail"),
            prompt_file=None,
        ),
        error="reproduced import failure",
    )

    payload = json.loads(status_path.read_text(encoding="utf-8"))
    assert payload["python_executable"] == sys.executable
    assert payload["dispatcher_file"] == str(Path(D.__file__).resolve())
    assert payload["goalflight_worktree_gc_file"] == str(stale_module_path.resolve())
