# Refactor and dedup scan worker brief

**Cadence:** daily project duty. The project-duty owner reviews the report and files
accepted findings in the shared `debt-drawdown` lane.

## Task

Inspect commits landed in this repository during the last 24 hours. Look only for
duplicated helpers and missed hoists introduced or exposed by those changes. Compare
each candidate against open task-store items before reporting it.

## Constraints

- Read-only: do not edit code, create tasks, or commit.
- Keep the scan to the changed code and the smallest surrounding context needed to
  prove duplication or a missed hoist.
- Report a finding only when the same behavior has multiple owners or a concrete
  shared helper would remove current duplication.
- Do not propose a helper for one caller or general cleanup without evidence.

## Output

Return proposed task entries only. Each entry must include a short title, severity,
the duplicated behavior, exact `file:line` evidence for every site, the relevant
landed commit, and the closest open task-store item considered. If there are no
findings, say `no findings` and name the commits scanned.
