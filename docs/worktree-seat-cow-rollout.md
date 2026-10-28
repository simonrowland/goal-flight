# CoW worktree seat rollout

Enable one repository at a time with one entry in the laptop's
`~/.goal-flight/capacity.local.json`:

```json
"seat_cow_templates": {"/absolute/path/to/repo": true}
```

Start with `project-a`, then `example-sim`, `battery-tool-v2`,
`pm2`, and `kiln`. Keep the existing sparse profile for each repository; when
none is configured, the template covers the full tracked tree.

## Laptop verification

For each repository, create a new seat, then release it cleanly and reuse it at
the **same base commit**. Inspect the seat's
`.git/worktrees/<seat>/goalflight-cow.json` report, or read it with
`goalflight_seat_cow.read_seat_cow_report(Path(seat_path))`. For the new seat,
confirm `population: cow`, `clone: clonefile`, `filesystem: apfs`, and
`operation: new`. For the clean same-base reuse, confirm the same values with
`operation: reuse`.

A reuse that changes the base commit intentionally skips CoW priming so Git's
existing index can remove outgoing tracked paths. Its report is
`population: plain`, `cow_skipped: true`, and `reason: base-changed`; tracked
changes also skip priming and report `reason: tracked-changes`. These are
expected skips, not filesystem or conversion fallbacks. A fallback instead
reports `population: plain` without `cow_skipped` and with its fallback
`reason`; keep that seat and investigate the reason before enabling another
repository.

Enable the next repository only after one new-seat report and one clean
same-base reuse report both show the expected CoW path. This confirms those
operations for the observed repository, filesystem, Git conversion settings,
sparse profile, and template generation. It does not validate other
repositories or configurations, or prove that changed-base and tracked-dirty
reuse used CoW; those cases are expected to skip it.

Verify the write comparison in a temporary repo with:

```sh
python3 -m pytest -q -s tests/python/test_worktree_seat_cow.py::test_measured_write_bytes_for_three_thousand_file_seat
```

It prints APFS private payload bytes for a full seat, the template before its
first clone, the first seat after cloning, and a warm seat. The first value is
the one-time cost per template generation; the warm value is per seat. It also
prints the cumulative payload allocation for the cold and warm preparations.
The report excludes Git administrative files and filesystem metadata. Run the
existing idle-seat dedupe once and confirm it reports little or no sharing work
for these seats.

## Rollback

Set the repository's value to `false` or remove its entry. New and reused seats
then follow the existing checkout path immediately. In-flight seats remain
valid, and `goalflight_seat_dedupe.py` remains available for idle seats.
