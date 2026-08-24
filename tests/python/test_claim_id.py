"""Two callers must never claim the same numbered slot.

The rule this replaces was written down and still failed: a shared corpus
documented "re-read the index at mint time", noted that parallel controllers had
already collided, and then accumulated five colliding ids anyway — the most recent
while the rule was in force. Read-then-write cannot be fixed by asking people to
read more carefully, because both readers see a consistent world and both writes
succeed.

So the load-bearing test here is the concurrent one. The rest guard the ways a
claim can be silently wrong: a padding difference that splits one id into two
namespaces, and a dry run being mistaken for a reservation.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import re
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import goalflight_claim_id as claim_id  # noqa: E402


def _id_of(path: Path) -> int:
    m = re.match(r"SC-(\d+)-", path.name)
    assert m, path.name
    return int(m.group(1))


def test_concurrent_claims_with_DIFFERENT_names_get_different_ids(tmp_path: Path) -> None:
    """The regression that matters, and the one the first version of this test missed.

    Every caller here mints a DIFFERENTLY-named artifact, which is how the tool is
    actually used — nobody allocates an id in order to write a file with the same
    name as everyone else. The original test gave all 40 callers the same suffix,
    so every candidate path was identical, exclusive create had something to
    arbitrate, and it passed against an implementation that raced on the artifact
    path. With distinct suffixes that implementation handed 24 callers only 6 ids.

    Assert on the ID, never on the filename: distinct filenames are guaranteed by
    the differing suffixes and prove nothing about allocation.
    """
    suffixes = [f"-class-{i:02d}.md" for i in range(24)]
    with ThreadPoolExecutor(max_workers=len(suffixes)) as pool:
        paths = list(pool.map(
            lambda sfx: claim_id.claim(tmp_path, prefix="SC", suffix=sfx), suffixes))
    ids = [_id_of(p) for p in paths]
    assert len(set(ids)) == len(suffixes), f"id collision: {sorted(ids)}"
    assert all(p.exists() for p in paths)


def test_concurrent_claims_with_identical_names_also_distinct(tmp_path: Path) -> None:
    """The weaker original case, kept so the easy path stays covered."""
    n = 20
    with ThreadPoolExecutor(max_workers=n) as pool:
        paths = list(pool.map(
            lambda _: claim_id.claim(tmp_path, prefix="SC", suffix="-x.md"), range(n)))
    assert len({_id_of(p) for p in paths}) == n


def test_sequential_claims_increment(tmp_path: Path) -> None:
    first = claim_id.claim(tmp_path, prefix="SC", suffix="-a.md")
    second = claim_id.claim(tmp_path, prefix="SC", suffix="-b.md")
    assert first.name == "SC-01-a.md"
    assert second.name == "SC-02-b.md"


def test_padding_variants_are_one_namespace(tmp_path: Path) -> None:
    """SC-7 and SC-007 are the same claim; treating them as distinct re-collides."""
    (tmp_path / "SC-7-legacy.md").write_text("")
    (tmp_path / "SC-008-legacy.md").write_text("")
    taken = claim_id.existing_ids(tmp_path, "SC", 2)
    assert taken == {7, 8}
    assert claim_id.claim(tmp_path, prefix="SC", suffix="-new.md").name == "SC-09-new.md"


def test_gaps_are_not_reused_by_default(tmp_path: Path) -> None:
    """Default is max+1: a deleted id stays retired so old citations stay unique."""
    (tmp_path / "SC-01-a.md").write_text("")
    (tmp_path / "SC-05-b.md").write_text("")
    assert claim_id.claim(tmp_path, prefix="SC", suffix="-c.md").name == "SC-06-c.md"


def test_explicit_start_can_fill_a_gap(tmp_path: Path) -> None:
    (tmp_path / "SC-01-a.md").write_text("")
    (tmp_path / "SC-05-b.md").write_text("")
    got = claim_id.claim(tmp_path, prefix="SC", suffix="-c.md", start=2)
    assert got.name == "SC-02-c.md"


def test_claim_creates_the_file_so_the_slot_is_held(tmp_path: Path) -> None:
    """The reservation IS the file. If it were not created, the race returns."""
    p = claim_id.claim(tmp_path, prefix="SC", suffix="-held.md")
    assert p.exists() and p.read_text() == ""
    other = claim_id.claim(tmp_path, prefix="SC", suffix="-next.md")
    assert other.name != p.name


def test_other_prefixes_are_untouched(tmp_path: Path) -> None:
    (tmp_path / "BUG-09-x.md").write_text("")
    assert claim_id.claim(tmp_path, prefix="SC", suffix="-a.md").name == "SC-01-a.md"


def test_dry_run_does_not_claim(tmp_path: Path) -> None:
    """A dry run must not create the file, and must say it is not a reservation."""
    done = subprocess.run(
        [sys.executable, str(SCRIPTS / "goalflight_claim_id.py"), str(tmp_path),
         "--prefix", "SC", "--suffix=-a.md", "--dry-run"],
        capture_output=True, text=True, check=True)
    assert "NOT claimed" in done.stdout
    assert not list(tmp_path.glob("SC-*")), "dry run created a file"


def test_cli_claims_and_prints_the_path(tmp_path: Path) -> None:
    done = subprocess.run(
        [sys.executable, str(SCRIPTS / "goalflight_claim_id.py"), str(tmp_path),
         "--prefix", "SC", "--suffix=-a.md"],
        capture_output=True, text=True, check=True)
    claimed = Path(done.stdout.strip())
    assert claimed.exists() and claimed.name == "SC-01-a.md"


def test_exhaustion_raises_rather_than_looping(tmp_path: Path) -> None:
    for i in range(1, 4):
        (tmp_path / f"SC-{i:02d}-x.md").write_text("")
    with pytest.raises(RuntimeError):
        claim_id.claim(tmp_path, prefix="SC", suffix="-y.md", start=1, limit=3)
