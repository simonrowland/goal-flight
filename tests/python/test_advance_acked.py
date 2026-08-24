"""Advancing the cursor should not require transcribing what a peek already knows.

Clearing nine read items previously meant supplying three CAS inputs by hand —
`--cursor-version`, a `--stream-snapshot STREAM=TOKEN` per stream, and a
`--position STREAM=SEQ` per stream. All three come from the same peek, and the
tokens are opaque hashes printed only by the doorbell, so a controller that had
already read its mail had to re-derive them: read the journal for the version,
import the module for the tokens, then assemble a nine-pair command line. That is
friction without safety — the compare-and-swap is what makes the write safe, and
it still runs either way.

`--acked` does the peek itself. These tests cover the position arithmetic and the
refusals, since a convenience flag that silently advanced past unread mail would
be much worse than the friction it removes.
"""

from __future__ import annotations

from pathlib import Path
import subprocess
import sys
import types

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import goalflight_messages as gm  # noqa: E402


def _item(stream_id: str, stream_seq: int):
    return types.SimpleNamespace(stream_id=stream_id, stream_seq=stream_seq)


def _peek(items, *, cursor_version=1, snapshots=None):
    return types.SimpleNamespace(
        items=items,
        cursor_version=cursor_version,
        stream_snapshots=snapshots or {},
    )


def test_highest_seq_per_stream_is_the_target() -> None:
    """Advancing means 'seen up to here', so the target is the largest seq."""
    got = gm._acked_positions(_peek([
        _item("alpha", 1), _item("alpha", 4), _item("alpha", 2),
        _item("beta", 7),
    ]))
    assert got == {"alpha": 4, "beta": 7}


def test_out_of_order_items_do_not_lower_the_target() -> None:
    """A later-listed lower seq must not walk the cursor backwards."""
    got = gm._acked_positions(_peek([_item("alpha", 9), _item("alpha", 3)]))
    assert got == {"alpha": 9}


def test_empty_peek_yields_no_advance() -> None:
    assert gm._acked_positions(_peek([])) == {}
    assert gm._acked_positions(_peek(None)) == {}


def test_dict_shaped_items_are_accepted() -> None:
    """The peek may hand back mappings rather than objects."""
    got = gm._acked_positions(_peek([
        {"stream_id": "alpha", "stream_seq": 2},
        {"stream_id": "alpha", "stream_seq": 5},
    ]))
    assert got == {"alpha": 5}


def test_malformed_items_are_skipped_not_guessed() -> None:
    """An item without a stream or seq is skipped; inventing one would advance
    past something never actually seen."""
    got = gm._acked_positions(_peek([
        _item("alpha", 3),
        types.SimpleNamespace(stream_id=None, stream_seq=9),
        types.SimpleNamespace(stream_id="beta", stream_seq=None),
    ]))
    assert got == {"alpha": 3}


def _run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPTS / "goalflight_messages.py"), "advance",
         "--controller-label", "probe", "--lease-nonce", "nonce", *args],
        capture_output=True, text=True,
    )


def test_acked_refuses_to_mix_with_hand_supplied_cas() -> None:
    """Half-derived, half-supplied CAS inputs are the one way to get this wrong."""
    done = _run("--acked", "--cursor-version", "5")
    assert done.returncode != 0
    assert "pass it alone" in done.stderr


def test_cursor_version_still_required_without_acked() -> None:
    """The old contract is unchanged for callers that do supply the inputs."""
    done = _run("--position", "alpha=1")
    assert done.returncode != 0
    assert "--cursor-version is required" in done.stderr
    assert "--acked" in done.stderr, "the error should name the easier route"
