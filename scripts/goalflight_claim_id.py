#!/usr/bin/env python3
"""Atomically claim the next free numbered slot in a directory.

Numbered artifacts in a shared directory — catalogue entries, sweep formats,
ticket files — are usually allocated by "read the highest number, add one, write
the file". That is a read-then-write race, and documenting it away does not help:
one shared corpus carried a written rule to re-read the index at mint time, plus a
dated note that parallel controllers had already collided, and still accumulated
five colliding ids, the most recent while the rule was in force.

The failure is silent, which is what makes it expensive. Both writers succeed and
neither learns anything; the collision surfaces later, once the ids have been
cited across hundreds of unrelated documents, at which point renumbering means
rewriting historical records that were correct when written.

`O_CREAT | O_EXCL` decides the race in the kernel instead. The subtlety, and the
whole reason this needs care: **exclusive create arbitrates a PATH, and the thing
being allocated is an ID.** Two callers minting differently-named artifacts under
the same number produce different paths, so both exclusive creates succeed and
both walk away believing they own the number. A first version of this module had
exactly that hole, and its concurrency test missed it by giving every caller the
same suffix — which is not how anyone uses it.

So the reservation is a marker under `.claims/` named by prefix and number ALONE,
with no suffix. That path is identical for every caller contending for an id, so
the kernel has something to arbitrate.

    goalflight_claim_id.py <dir> --prefix SC --suffix=-my-new-class.md
    goalflight_claim_id.py <dir> --prefix SC --dry-run

Note the `=` in `--suffix=-...`: a suffix normally begins with a hyphen, and
argparse would otherwise read it as another option.

Prints the claimed path. The claimed file starts empty — write content into it
afterwards; the claim is the reservation, not the artifact.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import re
import sys

DEFAULT_LIMIT = 10000
# Reservations live here, named by prefix+number only, so contention for an id
# happens on one shared path regardless of what each caller intends to name.
CLAIMS_DIR = ".claims"


def existing_ids(directory: Path, prefix: str, width: int) -> set[int]:
    """Every id already present, however it is zero-padded.

    Padding must not create a second namespace: SC-7 and SC-007 are the same
    claim, and a scan that treats them as distinct reintroduces the collision it
    exists to prevent.
    """
    pattern = re.compile(rf"^{re.escape(prefix)}-(\d+)(?:\D.*)?$")
    found: set[int] = set()

    def scan(where: Path) -> None:
        try:
            entries = list(where.iterdir())
        except FileNotFoundError:
            return          # not yet created: genuinely empty
        except OSError as exc:
            # A directory we cannot enumerate is NOT an empty namespace. Treating
            # it as empty hands out ids that are already taken - exactly the
            # collision this module exists to prevent - so refuse instead.
            raise RuntimeError(
                f"cannot enumerate {where} to determine taken ids: {exc}"
            ) from exc
        for entry in entries:
            stem = entry.name[:-len(entry.suffix)] if entry.suffix else entry.name
            for candidate in (entry.name, stem):
                match = pattern.match(candidate)
                if match:
                    found.add(int(match.group(1)))
                    break

    scan(directory)
    # Reservations count as taken even before the artifact is written, otherwise
    # a caller that claimed an id but has not yet created its file loses it.
    scan(directory / CLAIMS_DIR)
    return found


def claim(
    directory: Path,
    *,
    prefix: str,
    suffix: str,
    width: int = 2,
    start: int | None = None,
    limit: int = DEFAULT_LIMIT,
) -> Path:
    """Create and return the first unclaimed path. Raises if none is free."""
    directory.mkdir(parents=True, exist_ok=True)
    claims = directory / CLAIMS_DIR
    claims.mkdir(parents=True, exist_ok=True)
    taken = existing_ids(directory, prefix, width)
    n = start if start is not None else (max(taken) + 1 if taken else 1)

    ceiling = n + limit
    while n < ceiling:
        if n in taken:
            n += 1
            continue
        # Arbitrate the ID, not the filename. The marker path omits the suffix,
        # so every caller contending for this number races on the SAME path and
        # the kernel can pick one winner. Racing on the artifact path instead
        # lets two differently-named files share a number.
        marker = claims / f"{prefix}-{n:0{width}d}"
        try:
            fd = os.open(marker, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            n += 1
            continue
        except OSError:
            n += 1
            continue
        os.close(fd)

        path = directory / f"{prefix}-{n:0{width}d}{suffix}"
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            # The number was ours but this exact filename already exists; the
            # marker stays so the number is not handed out again.
            return path
        os.close(fd)
        return path
    raise RuntimeError(
        f"no free id for prefix {prefix!r} in {directory} within {limit} of {n - limit}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Atomically claim the next free numbered slot in a directory.")
    parser.add_argument("directory", type=Path)
    parser.add_argument("--prefix", required=True,
                        help="Identifier prefix, e.g. SC (produces SC-07-...).")
    parser.add_argument("--suffix", default="",
                        help="Text after the number, e.g. --suffix=-my-class.md. "
                             "Use = form; a leading hyphen reads as an option otherwise.")
    parser.add_argument("--width", type=int, default=2,
                        help="Zero-pad the number to this width (default 2).")
    parser.add_argument("--start", type=int, default=None,
                        help="Begin searching at this id instead of max+1.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Report the next free id without claiming it. "
                             "Racy by nature — for inspection only.")
    args = parser.parse_args(argv)

    if args.dry_run:
        taken = existing_ids(args.directory, args.prefix, args.width)
        n = args.start if args.start is not None else (max(taken) + 1 if taken else 1)
        while n in taken:
            n += 1
        print(f"{args.directory / f'{args.prefix}-{n:0{args.width}d}{args.suffix}'}"
              "   (dry run - NOT claimed; two callers doing this both get the same id)")
        return 0

    try:
        print(claim(args.directory, prefix=args.prefix, suffix=args.suffix,
                    width=args.width, start=args.start))
    except (OSError, RuntimeError) as exc:
        print(f"claim failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
