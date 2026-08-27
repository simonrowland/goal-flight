#!/usr/bin/env python3
"""Emit INDEX.md and per-project TLDR files from the already-pinned catalog."""
from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path

AUDIT = Path(__file__).resolve().parent
CAT = json.loads((AUDIT / "_raw-snapshot" / "catalog.json").read_text())
ENTRIES = {e["dispatch_id"]: e for e in CAT["entries"]}

BANNER = """\
> **Nothing in the shared queue has been deleted, moved, renamed, or re-fired.**
> This file is an inventory so *you* can **claim or abandon** each entry.
> If the work is still wanted, re-submit it as a **fresh dispatch under a new id
> against current HEAD**. Re-firing a stale queue record is what we are avoiding:
> a queued dispatch carries a premise, and premises go stale (wrong tree, wrong
> branch, vanished prompt, already-finished work).
>
> Ledger `state` values below are the raw on-disk field. They are **not** a
> `reconcile-abandoned` verdict (that gate is under repair in t-371). Claim-pid
> liveness is `kill(pid, 0)` on the filename pid only — a live pid can still be
> a reuse; a dead pid is not a license to drain the same record.
"""

TLDR = {
    "acp-async-launch": "Leftover ACP launch-path probe (ledger-only). Prompt path is relative `chunk.md` and is gone. Target: goal-flight repo. Honest default: abandon.",
    "acp-launch-retry": "Leftover ACP launch-retry probe (ledger-only). Prompt path `chunk.md` is gone. Target: goal-flight repo. Honest default: abandon.",
    "acp-launch-unconfirmed": "Leftover ACP launch_unconfirmed probe (ledger-only). Prompt path `chunk.md` is gone. Target: goal-flight repo. Honest default: abandon.",
    "acp-ledger-lease": "Leftover ACP ledger-lease probe (ledger-only). Prompt path `chunk.md` is gone. Target: goal-flight repo. Honest default: abandon.",
    "acp-live-runner": "Leftover ACP live-runner probe (ledger-only). Prompt path `chunk.md` is gone. Target: goal-flight repo. Honest default: abandon.",
    "acp-node-ssh": "Leftover ACP node-SSH probe (ledger-only). Prompt path `chunk.md` is gone. Target: goal-flight repo. Honest default: abandon.",
    "t372-w1": "THIS inventory worker (t-372). Target: worktree `t372-queue` @ 27fb24f. In-flight; not a stuck queue entry.",
    "rev-t371": "Review of t-371 reconcile-abandoned identity probe (55bcb7f / 76942df). Target: worktree t371-reconcile; findings-file only. Ledger worker pid was alive at inventory.",
    "b13-1-reverse-mass-drivers": "Implement B13 chunk 1 — reverse mass-driver family (t-261 / BATCH-PLAN B13). Target: pm2 main repo. Ledger running with a live worker pid at inventory — may still be in flight.",
    "b264probe-1": "Duplicate of layermap-harvest (t-763): collect LAYER-MAP declaration-needs; do not edit docs/LAYER-MAP.md. Target: pm2 HEAD 590b1ae. Ledger already `complete`.",
    "b264probe-2": "Duplicate of layermap-harvest (t-763) against pm2 HEAD 590b1ae. Same prompt as b264probe-1. Ledger already `complete`.",
    "b264probe-4": "Duplicate of layermap-harvest (t-763) against pm2 HEAD 590b1ae. Same prompt as b264probe-1. Ledger already `complete`.",
    "b285-ring-coherence-adjudication": "Adjudicate ring coherence (b-285). Target: `/private/tmp/pm2-b285/pm2`. Prompt file MISSING. Bare json was in the first snapshot then vanished from the live dir; ledger running with a live worker pid.",
    "bugs-b277a": "Fix b-277: eleven copies of a refusal idiom make a false claim (bugs-lane fixer). Target: pm2. Ledger-only; worker pid dead.",
    "fr-d1-r3-retry-b8fa0aba": "Retry of force-rail carrier D1 (device-plasma, t-742). Target: pm2 main repo at the queued HEAD. No claim marker.",
    "fuzzspoke": "Teach the fuzzer generator the SPOKE aggregate ceiling. Target: pm2 main repo. Already in retired-by-main (bare + claim marker).",
    "layermap-harvest": "Harvest every owed LAYER-MAP declaration-need (t-763); do not edit docs/LAYER-MAP.md. Target: pm2 HEAD 590b1ae.",
    "layermap-harvest-d": "Same layermap-harvest (t-763) prompt, dispatch id -d. Claim-only (no surviving bare json) — unrecoverable by drain. Target: pm2 HEAD 590b1ae.",
    "layermap-harvest-g": "Same layermap-harvest (t-763) prompt, dispatch id -g. Target: pm2 HEAD 590b1ae.",
    "t292-relativistic-gathered-mass": "Adjudicate relativistic gathered-mass energization (t-292; extends t-287). Target: pm2 main repo. Ledger running with a live worker pid at inventory.",
    "t702-rev-seam": "Review-seam work (t-702). Prompt file `docs-private/task-prompts/2026-08-26-engine/t702-rev-seam.md` is MISSING. Target: pm2 repo. Honest default: abandon unless the brief is reconstructed.",
    "t746-r2-retry-15cc828e": "t-746 — the lossy numeric environment vector (retry). Target: pm2 HEAD 06e62d2 (main tree, not a worktree). No claim marker.",
    "t800-pulse": "t-800 pulse/reactive adequacy (store can be joule-rich and still miss the chirp edge). Target: `/private/tmp/pm2-engine-t800/pm2` branch `engine-t800-pulse`. Claim-only; ledger `failed`.",
    "t801-fix1": "t-801 fix round on physics+honesty review FAILs (commit b1b0a9c). Target: `/private/tmp/pm2-engine-t801/pm2`. Ledger running with a live worker pid at inventory.",
    "b234-mre-scope": "Scope whether the MRE charge-accounting fail-open (b-234, `simulator/extraction.py` ~2113) fires on any golden feedstock. Target: `/Users/simonrowland/Repos/rps-b234` branch `work-b234` — NOT the Dropbox main tree. Stale-target risk is exactly the regolith worked example.",
    "t481-registry-decide": "Decide the fate of a fully-built, never-read phase-aware volatile-property registry (t-481). Target: `/Users/simonrowland/Repos/rps-t481` branch `work-t481` (HEAD 236553f9), not the Dropbox main tree.",
    "t697-dunite": "Diagnose an unexplained residual in the dunite melt-activity benchmark. Target: Dropbox `regolith-pyrolysis-simulator` working tree. Also copied under retired-by-engine.",
    "t743-exclusion-audit": "Exclusion audit: does the validation battery only discard data that flatters the model? (task_ids t-754 / KEMS). Target: Dropbox `regolith-pyrolysis-simulator` tree.",
    "t748-stage-pressures": "Derive per-species STAGE partial pressure and whether each condenser captures its species (t-748). Target: `/Users/simonrowland/Repos/rps-b236` branch `work-b236`. Ledger `worker_dead`.",
    "t750-r2-notfixed": "Adversarial NOT-FIXED review of an uncommitted changeset (`docs-private/review/t750/changeset.diff`). Target: the Dropbox working tree as it stood when queued — firing later reviews whatever is dirty now.",
    "magemin-status-review": "Review an uncommitted MagmaMin bugfix (`engines/magemin/`) on branch `engine-2026-08-16`. Target: Dropbox working-tree diff. Also copied under retired-by-engine.",
    "sr2-closure": "Closure review of a round-2 bugfix on branch `engine-2026-08-16` (uncommitted tree + untracked test). Claim-only, no surviving bare json.",
    "status-r2-closure": "Same round-2 closure review as sr2-closure. Quarantined claim marker (pid dead). Ledger `failed`.",
    "status-r2-notfixed": "Adversarial NOT-FIXED review, round 2, of the uncommitted `engine-2026-08-16` tree. Also copied under retired-by-engine.",
    "webqa-D-leaderboard-0827": "Web QA surface D: optimizer leaderboard (b-089); regenerate findings that were never written to disk. Quarantined. Target: Dropbox regolith tree.",
    "codex-20873-1787795343": "b-2710 — write the end-to-end seam test that three 'verified' fixes slipped through; do not fix the bug. Target: worktree `bt-b2080`. Claim-only.",
    "codex-21996-1787791668": "b-2690 — split the CLEAN half of a contaminated commit onto its own branch. Target: worktree `bt-warm`.",
    "codex-320-1787529559": "b-2043 — why the current tree tops out at 2–3 MP on BBL 4014760001 (do not bisect history). Target: battery-tool-v2 main. Retired-by-main.",
    "codex-35329-1787794960": "b-2710 seam test (same brief as codex-20873) but cwd is battery-tool-v2 **main**, not `bt-b2080`. Claim-only. Same premise, different tree — do not re-fire blindly.",
    "codex-356-1787583341-retry-03e74ab8": "Retry of b-2181 / t-556: bearing-topology mechanism on the FLAGSHIP. Target: battery-tool-v2 main. Prompt file `B-b2181.md`. No ledger record.",
    "codex-36951-1787795932": "b-2710 seam test against battery-tool-v2 **main** (third copy of the same brief). Target: main tree, not a worktree.",
    "codex-38168-1787588171-retry-ae50e9f1": "Retry of t-558: layout_count ~ n_cpu / per-(layout x family) seed prep on parallel CPUs. Target: battery-tool-v2 main. No ledger record.",
    "codex-38318-1787793995": "b-2702 gap 2 — IFC export silently discards the BESS equipment. Target: worktree `bt-b2702` which was MISSING on disk at inventory. Claim-only.",
    "codex-43969-1787541709": "b-2130 audit slice 0001 (retired-by-bugs). Target: `/private/tmp/goal-flight-501/audit-0001` — a tmp sandbox, not battery-tool-v2 main.",
    "codex-44655-1787541714": "b-2130 audit slice 0401 (retired-by-bugs). Target: `/private/tmp/goal-flight-501/audit-0401`.",
    "codex-45162-1787541720": "b-2130 audit slice 0801 (retired-by-bugs). Target: `/private/tmp/goal-flight-501/audit-0801`.",
    "codex-45427-1787794032": "Put test coverage on MEMBER_TRUTH_CONNECTION_ID_REQUIRED (export-mission guard). Target: worktree `bt-warm`. Claim-only.",
    "codex-45667-1787541726": "b-2130 audit slice 1201 (retired-by-bugs). Target: `/private/tmp/goal-flight-501/audit-1201`.",
    "codex-46279-1787541732": "b-2130 audit slice 1501 (retired-by-bugs). Target: `/private/tmp/goal-flight-501/audit-1501`.",
    "codex-46804-1787541738": "b-2130 audit slice 1801 (retired-by-bugs). Target: `/private/tmp/goal-flight-501/audit-1801`.",
    "codex-48669-1787557501": "b-2130 audit slice 0001, second wave (retired-by-bugs). Target: `/private/tmp/goal-flight-501/audit-0001`.",
    "codex-49282-1787557506": "b-2130 audit slice 0401, second wave (retired-by-bugs). Target: `/private/tmp/goal-flight-501/audit-0401`.",
    "codex-50159-1787557511": "b-2130 audit slice 0801, second wave (retired-by-bugs). Target: `/private/tmp/goal-flight-501/audit-0801`.",
    "codex-52033-1787557524": "b-2130 audit slice 1201, second wave (retired-by-bugs). Target: `/private/tmp/goal-flight-501/audit-1201`.",
    "codex-53248-1787557530": "b-2130 audit slice 1501, second wave (retired-by-bugs). Target: `/private/tmp/goal-flight-501/audit-1501`.",
    "codex-53956-1787557536": "b-2130 audit slice 1801, second wave (retired-by-bugs). Target: `/private/tmp/goal-flight-501/audit-1801`.",
    "codex-55395-1787799489": "b-2714 — the lint that enforces infeasible-is-an-owner-verdict is itself red. Ledger-only; worker pid dead. Target: battery-tool-v2.",
    "codex-56849-1787443719-retry-63523187": "Retry of b-1977. Prompt file `b-1977.md` is MISSING. Present both in the live queue and in retired-by-main. Honest default: abandon.",
    "codex-60942-1787841540": "SC-150 class sweep — read-only diagnosis, do not fix. Ledger running with a live worker pid at inventory. Target: battery-tool-v2.",
    "codex-62503-1787807410": "b-2696 — IFC provenance guards are self-referential. Target: worktree `bt-b2696` which was MISSING on disk. Claim-only.",
    "codex-62720-1787530305": "b-2101 — publish SEED cards while until-dry is still running (plan-first). Retired-by-webui. Target: battery-tool-v2 main.",
    "codex-63597-1787530311": "t-554 / b-2139 follow-through: make e2e capacity runs site on the DECLARED roof. Retired-by-webui. Target: battery-tool-v2 main.",
    "codex-64518-1787530317": "t-555 backwards sweep (read-only): capacity/coverage comparisons lacking site-model identity. Retired-by-webui. Target: battery-tool-v2 main.",
    "codex-69648-1787841595": "SC-150 class sweep — read-only diagnosis, do not fix. Ledger running with a live worker pid at inventory. Target: battery-tool-v2.",
    "codex-72685-1787777565": "Review-and-verify the UNVERIFIED q-042 salvage branch (read-only; do not modify the branch). Ledger-only; worker pid dead.",
    "codex-73102-1787700838-retry-c2f707cf": "Retry of b-2484 (brief `b2482-brief.md`). Target: battery-tool-v2 main. No ledger record.",
    "codex-73527-1787842274": "b-2759 — diagnose-then-fix: rail crossing called ORPHAN at 0.0057 inch from its top chord. Appeared mid-inventory with a live claim pid; ledger running.",
    "codex-74265-1787791956": "b-2702 gaps 2 and 3 — put BESS equipment and keep-clear zones into the IFC. Target: worktree `bt-cat`. Claim-only.",
    "codex-75378-1787688360": "b-2426 (brief `b2426-brief.md`). Target: worktree `bt-b2154` — both cwd and project_root were MISSING on disk at inventory.",
    "codex-80486-1787414845-retry-d69b4bc9": "Retry of b-1940 (brief-sfring3). Prompt file MISSING. Present both in the live queue and in retired-by-main. Honest default: abandon.",
    "codex-86585-1787546556": "b-2148 — two defects in the Playwright acceptance bar itself (fix the bar, not the product). Retired-by-main. Target: battery-tool-v2 main.",
    "grok-code-21722-1787778673": "b-2630 / b-2619 — reimport must invalidate/bypass the stale acquisition cache. Target: worktree `bt-b2630`.",
    "grok-code-28489-1787781619": "t-602 scout A (read-only): ground-site mode — acquisition/lot-polygon + import seam. Target: battery-tool-v2 main. Claim-only.",
    "grok-code-34851-1787725369": "Offer-review (`offer-review-brief.md`). Target: worktree `bt-d013` which was MISSING on disk.",
    "grok-code-58307-1787790827": "t-605 (G1 of ground-site mode t-602): get the lot polygon into the pipeline. Target: worktree `bt-t605`. Claim-only.",
    "grok-code-70915-1787842258": "SC-150 class sweep (STORED-VERDICT.md) — read-only diagnosis. Target: worktree `bt-sc150-3`. Appeared mid-inventory with a live claim pid.",
    "grok-code-77178-1787729728": "Offer-review (`offer-review-brief.md`), second copy. Target: worktree `bt-d013` which was MISSING on disk.",
    "grok-code-84519-1787841683": "SC-150 class sweep — read-only diagnosis, do not fix. Ledger-only; worker pid dead. Target: battery-tool-v2.",
    "raw-b2753-studio-e02ec9816-r2": "Premise missing (no inline prompt, no prompt-file). Target cwd: worktree `bt-b2753`. Honest default: abandon.",
    "zw-aw31": "Awaiting-review batch 31: verify 14 already-closed rows against current release tip. Target: battery-tool-v2. Ledger `running` but worker pid was dead at inventory.",
    "zw-fix_other_cross-layer__b1": "ZERO campaign fix wave 2, batch `fix_other_cross-layer__b1` (adapters/exporters). Target: worktree `bt-zw-fix_other_cross-layer__b1` DETACHED at origin/main 0bcbebe2a. Ledger `waiting_capacity`, age ~2d 18h.",
}


def project_group(e: dict) -> str:
    root = str(e.get("project_root") or "")
    cwd = str(e.get("worker_cwd") or "")
    slug = e.get("project_slug") or ""
    blob = " ".join([root, cwd, slug, e.get("project_name") or ""])
    if "battery-tool" in blob or slug.startswith("audit") or "/audit-" in blob:
        return "battery-tool-v2"
    if "regolith" in blob.lower() or "Regolith Processing" in blob:
        return "regolith"
    if "/pm2" in blob or slug == "pm2":
        return "pm2"
    if "goal-flight" in blob or slug == "goal-flight":
        return "goal-flight"
    return slug or "unknown"


PROJECT_TITLES = {
    "battery-tool-v2": "battery-tool-v2",
    "pm2": "pm2",
    "regolith": "regolith-pyrolysis-simulator",
    "goal-flight": "goal-flight",
}

PROJECT_INTRO = {
    "battery-tool-v2": "This list is for the battery-tool-v2 controllers (main, engine, bugs, webui). Several entries target `.cache/worktrees/*` or `/tmp/goal-flight-501/audit-*` sandboxes — those are not the main tree. The twelve `retired-by-bugs` rows are the b-2130 audit slices; the three `retired-by-webui` rows were already set aside by that controller.",
    "pm2": "This list is for the pm2 controllers (main, engine, bugs). Several entries are duplicate layermap-harvest probes against a pinned HEAD, or they target `/private/tmp/pm2-*` worktrees rather than `Repos/pm2`.",
    "regolith": "This list is for the regolith-pyrolysis-simulator controllers (regolith-main, regolith-engine). **Stale-target risk is the whole point of this inventory:** several prompts name a specific worktree (`rps-b234`, `rps-t481`, `rps-b236`) or an uncommitted `engine-2026-08-16` working tree. Re-firing those from the Dropbox main tree, or after that tree has moved, would audit the wrong code and report confidently.",
    "goal-flight": "This list is for the goal-flight controller. Six `acp-*` rows are ledger leftovers of launch-path probes whose prompt file is gone. `t372-w1` is the worker that wrote this inventory. `rev-t371` is a live review of the reconcile-abandoned identity probe.",
}


def md_escape(s) -> str:
    if s is None:
        return ""
    return str(s).replace("|", "\\|")


def claim_cell(e: dict) -> str:
    pids = e.get("claim_pids") or []
    if not pids:
        return "none"
    parts = []
    for pid, status, pop in pids:
        loc = "" if pop == "queue-top" else f" ({pop})"
        parts.append(f"pid `{pid}` **{status}**{loc}")
    return "; ".join(parts)


def prompt_cell(e: dict) -> str:
    kind = e.get("prompt_source_kind")
    pf = e.get("prompt_file")
    if kind == "inline":
        return "inline text (pinned)"
    if pf:
        exists = e.get("prompt_file_exists")
        tag = "EXISTS on disk" if exists else "MISSING on disk"
        return f"`--prompt-file` `{pf}` — {tag}"
    return f"{kind} (no path)"


def pops_cell(e: dict) -> str:
    return ", ".join(f"`{p}`" for p in e.get("populations") or [])


def hint_cell(e: dict) -> str:
    hints = list(e.get("hints") or [])
    cwd = e.get("worker_cwd")
    root = e.get("project_root")
    extra = []
    if cwd and not Path(str(cwd)).is_dir():
        extra.append(f"HINT: worker_cwd missing: `{cwd}`")
    if root and not Path(str(root)).is_dir():
        extra.append(f"HINT: project_root missing: `{root}`")
    # de-dup
    seen = set()
    out = []
    for h in hints + extra:
        if h not in seen:
            seen.add(h)
            out.append(h)
    return "<br>".join(out) if out else "—"


def emit_entry(did: str) -> str:
    e = ENTRIES[did]
    tldr = TLDR.get(did, "(see pinned prompt)")
    lines = []
    lines.append(f"### `{did}`")
    lines.append("")
    lines.append(f"- **TLDR:** {tldr}")
    lines.append(f"- **Pinned prompt:** [`prompts/{did}.md`](prompts/{did}.md)")
    lines.append(f"- **Project name:** {e.get('project_name')}")
    lines.append(f"- **Project root:** `{e.get('project_root')}`")
    lines.append(f"- **Target cwd:** `{e.get('worker_cwd')}`")
    lines.append(f"- **Queue payload state:** `{e.get('queue_payload_state') or e.get('state')}`")
    lines.append(f"- **Ledger state (raw):** `{e.get('ledger_state')}`")
    lines.append(f"- **Agent:** `{e.get('agent')}`")
    lines.append(f"- **Created at / age:** `{e.get('created_at')}` / **{e.get('age')}**")
    lines.append(f"- **Owner label:** `{e.get('owner_label')}`")
    lines.append(f"- **Claim marker:** {claim_cell(e)}")
    lines.append(f"- **Prompt source:** {prompt_cell(e)}")
    lines.append(f"- **Prompt pin:** **{e.get('prompt_pin_status')}** ({e.get('prompt_chars') or 0} chars)")
    lines.append(f"- **Task ids:** `{e.get('task_ids')}`")
    lines.append(f"- **Base SHA:** `{e.get('base_sha')}`")
    lines.append(f"- **Populations:** {pops_cell(e)}")
    hc = hint_cell(e)
    if hc != "—":
        lines.append(f"- **Hints (not verdicts):** {hc}")
    lines.append("")
    return "\n".join(lines)


def main() -> None:
    grouped: dict[str, list[str]] = defaultdict(list)
    for did, e in ENTRIES.items():
        grouped[project_group(e)].append(did)
    for k in grouped:
        grouped[k] = sorted(grouped[k])

    # INDEX
    gen = CAT["generated_at"]
    pinnable = CAT["counts"]["pinnable"]
    missing = CAT["counts"]["missing"]
    union = CAT["counts"]["union_dispatch_ids"]
    nterm = CAT["counts"]["ledger_nonterminal"]
    ltot = CAT["counts"]["ledger_total"]
    states = CAT["counts"]["ledger_state_counts"]

    idx = []
    idx.append("# Dispatch-queue inventory — 2026-08-27")
    idx.append("")
    idx.append(BANNER)
    idx.append("")
    idx.append(f"Generated at `{gen}` (UTC). Queue path: `/tmp/goal-flight-501/dispatch-queue`. Ledger path: `/tmp/goal-flight-501/runs.d` (read as JSON files; **no** `StateLock`, **no** `open_reader`, **no** `reconcile-abandoned`).")
    idx.append("")
    idx.append("The queue lives in `/tmp` (macOS may reap it). A byte-for-byte copy of the queue tree, plus every non-terminal ledger record, is under `_raw-snapshot/`.")
    idx.append("")
    idx.append("## Why two controllers disagreed (28 vs 63)")
    idx.append("")
    idx.append("They counted **different populations**. There is no single 'queue depth'. Quote the population with every number:")
    idx.append("")
    idx.append("| Population | Pass-1 snapshot | Pass-2 snapshot | Live during catalog | What this is |")
    idx.append("|---|---:|---:|---:|---|")
    idx.append("| bare `<id>.json` at queue top | **29** | **28** | **28** | Drainable records. `b285-ring-coherence-adjudication.json` left the live dir during this inventory; it is preserved in pass-1 and in the ledger. |")
    idx.append("| `<id>.json.claimed-<pid>-<ts>` at queue top | **26** | **27** | **26** | Claim markers. t-369 saw 25/25 dead; we saw 26 on pass-1 (all dead), then 2 live pids appeared and later left. |")
    idx.append("| claimed-only (marker, no surviving bare `.json`) | **11** | **12** | **11** | Unrecoverable by drain. Matches t-369's 11. |")
    idx.append("| bare-only (json, no claim marker) | **14** | **13** | **13** | Sitting unclaimed. |")
    idx.append("| both bare and claim | **15** | **15** | **15** | Claimed copies of a still-present json. |")
    idx.append("| `quarantine/` files | **2 / 2** | **2 / 2** | **2 / 2** | Claim markers moved into quarantine (pids dead). |")
    idx.append("| `retired-by-bugs/` files | **12 / 12** | **12 / 12** | **12 / 12** | All bare json; unique ids 12. |")
    idx.append("| `retired-by-engine/` files | **3 / 3** | **3 / 3** | **3 / 3** | All three ids also still exist in the live queue. |")
    idx.append("| `retired-by-main/` files | **6 / 6** | **6 / 6** | **6 / 6** | 5 bare + 1 claim; 2 of the bare ids also still live. |")
    idx.append("| `retired-by-webui/` files | **3 / 3** | **3 / 3** | **3 / 3** | All bare json. |")
    idx.append("| retired unique dispatch ids | **23** | **23** | **23** | 12+3+5+3 unique across the four dirs (`fuzzspoke` has both bare and claim). |")
    idx.append("| other top-level | 1 (`.submit.lock`) | 1 | 1 | Not a dispatch. |")
    idx.append(f"| dispatch ledger records | **{ltot} / {ltot}** | — | — | Every `runs.d/*.json`. |")
    idx.append(f"| ledger non-terminal (raw `state` ∉ `TERMINAL_STATES`) | **{nterm} / {ltot}** | — | — | 23 `queued` + 18 `running` + 4 `starting` + 2 `waiting_capacity` + 1 `launch_unconfirmed`. |")
    idx.append("| ledger non-terminal ∩ live queue id | **28 / 48** | — | — | Same id in both places. |")
    idx.append("| ledger non-terminal with **no** live queue file | **20 / 48** | — | — | Includes this worker (`t372-w1`) and six `acp-*` probe leftovers. |")
    idx.append(f"| **union of unique dispatch ids in this audit** | **{union}** | — | — | Queue files ∪ retired ∪ quarantine ∪ ledger-nonterminal. |")
    idx.append("")
    idx.append("A count of **28** is the bare-json drainable set (29 on pass-1, 28 after `b285` left). A count of **63** is what you get by adding pass-1's 29 bare + 26 claims + 2 quarantine + 6 `retired-by-main` files = **63**, mixing live and retired populations. Neither number is wrong; they are different sets.")
    idx.append("")
    idx.append("### Overlaps (do not double-count these as extra work)")
    idx.append("")
    idx.append("- **retired ∩ live queue:** `codex-56849-1787443719-retry-63523187`, `codex-80486-1787414845-retry-d69b4bc9`, `magemin-status-review`, `status-r2-notfixed`, `t697-dunite`.")
    idx.append("- **quarantine ∩ live queue:** none. **quarantine ∩ retired:** none.")
    idx.append("- **claimed-only (no bare json, drain cannot recover):** pass-1 list: `codex-20873-1787795343`, `codex-35329-1787794960`, `codex-38318-1787793995`, `codex-45427-1787794032`, `codex-62503-1787807410`, `codex-74265-1787791956`, `grok-code-28489-1787781619`, `grok-code-58307-1787790827`, `layermap-harvest-d`, `sr2-closure`, `t800-pulse`.")
    idx.append("- **live queue with no ledger record at all:** `codex-356-1787583341-retry-03e74ab8`, `codex-38168-1787588171-retry-ae50e9f1`, `codex-56849-1787443719-retry-63523187`, `codex-73102-1787700838-retry-c2f707cf`, `codex-80486-1787414845-retry-d69b4bc9`, `fr-d1-r3-retry-b8fa0aba`, `t746-r2-retry-15cc828e`.")
    idx.append("")
    idx.append("## Method")
    idx.append("")
    idx.append("1. Copied `/tmp/goal-flight-501/dispatch-queue` to `_raw-snapshot/queue-dir-copy` **before** analysis (pass-1), then recopied (pass-2) to catch drift. The live dir mutated during this inventory: `b285-ring-coherence-adjudication.json` disappeared; two claim markers with live pids appeared and later left.")
    idx.append("2. Enumerated bare json, claim markers, `quarantine/`, and every `retired-by-*` directory separately.")
    idx.append("3. Read every `runs.d/*.json` as text. Non-terminal means the record's `state` field is not in `goalflight_dispatch_states.TERMINAL_STATES`. That is a vocabulary filter, not a liveness verdict.")
    idx.append("4. Claim-pid liveness: `os.kill(pid, 0)` on the pid parsed from the filename.")
    idx.append("5. Prompt pin: `request.prompt_file` / `--prompt-file`, else inline `request.prompt` / `--prompt`, else ledger `prompt_path`. Full text copied to `prompts/<dispatch_id>.md`. A missing file is recorded as missing — that is decision-relevant.")
    idx.append("6. Hints (already-done, missing cwd, terminal ledger state) are **hints**, not adjudications.")
    idx.append("7. Queue and ledger were not written, renamed, drained, or reconciled.")
    idx.append("")
    idx.append("## Prompt pinning")
    idx.append("")
    idx.append(f"- **{pinnable} / {union}** prompts pinnable (file still on disk, or inline text present).")
    idx.append(f"- **{missing} / {union}** premises missing (prompt-file gone or never stored). Honest default for those: abandon.")
    idx.append(f"- Source mix: `{CAT['counts']['prompt_source']}`.")
    idx.append("")
    idx.append("## Per-project summary")
    idx.append("")
    idx.append("| Project | Entries (union) | Live queue-top | Retired/quarantine only | Ledger-nonterm only | Pinnable | Missing premise | File |")
    idx.append("|---|---:|---:|---:|---:|---:|---:|---|")

    def flags(e):
        pops = set(e.get("populations") or [])
        live = bool({"bare-json", "claim-marker", "queue-top"} & pops) and e.get("live_in_queue_top")
        # live_in_queue_top may be false for claimed-only that still sit at top
        top = any(
            ("queue-top" in pops) or ("bare-json" in pops) or ("claim-marker" in pops)
            for _ in [0]
        )
        # more precise: locations
        has_top = "queue-top" in pops or "bare-json" in pops or "claim-marker" in pops
        retired_only = (not has_top) and any(
            str(p).startswith("retired-by-") or p == "quarantine" for p in pops
        )
        ledger_only = pops <= {"ledger-nonterminal", "ledger-terminal"} or pops == {"ledger-nonterminal"}
        return has_top, retired_only, ledger_only

    for proj in ["battery-tool-v2", "pm2", "regolith", "goal-flight"]:
        ids = grouped.get(proj, [])
        live_n = ret_n = led_n = pin_n = miss_n = 0
        for did in ids:
            e = ENTRIES[did]
            pops = set(e.get("populations") or [])
            has_top = bool({"bare-json", "claim-marker", "queue-top"} & pops)
            has_ret = any(str(p).startswith("retired-by-") or p == "quarantine" for p in pops)
            has_led_only = (not has_top) and (not has_ret)
            if has_top:
                live_n += 1
            elif has_ret:
                ret_n += 1
            else:
                led_n += 1
            if e.get("prompt_pin_status") == "pinnable":
                pin_n += 1
            else:
                miss_n += 1
        idx.append(
            f"| {PROJECT_TITLES[proj]} | {len(ids)} / {union} | {live_n} / {len(ids)} | {ret_n} / {len(ids)} | {led_n} / {len(ids)} | {pin_n} / {len(ids)} | {miss_n} / {len(ids)} | [`{proj}.md`]({proj}.md) |"
        )

    extra = [k for k in grouped if k not in PROJECT_TITLES]
    if extra:
        idx.append("")
        idx.append(f"Ungrouped slugs (should be empty): {extra}")

    idx.append("")
    idx.append("## Ledger non-terminal state histogram (raw)")
    idx.append("")
    idx.append("| state | count | in TERMINAL_STATES? |")
    idx.append("|---|---:|---|")
    terminal_like = {
        "complete", "released", "error", "failed", "blocked", "blocked_adapter_gate",
        "blocked_auth", "blocked_capacity", "blocked_completion_authority",
        "blocked_os_sandbox", "blocked_session_limit", "blocked_windows_dispatch",
        "inconclusive_timeout", "inconclusive_no_final", "worker_dead", "tool_timeout",
        "stalled", "remote_turn_silence", "failed_worktree", "controller_dead",
        "orphaned", "superseded", "quota_exhausted", "transient_throttle",
        "limit_unknown", "rate_limited", "idle_timeout", "wedged",
        "liveness_indeterminate",
    }
    for st, n in sorted(states.items(), key=lambda kv: (-kv[1], str(kv[0]))):
        flag = "yes (excluded from non-terminal population)" if st in terminal_like else "no (counted as non-terminal)"
        idx.append(f"| `{st}` | {n} / {ltot} | {flag} |")

    idx.append("")
    idx.append("## Per-project files")
    idx.append("")
    for proj in ["battery-tool-v2", "pm2", "regolith", "goal-flight"]:
        idx.append(f"- [`{proj}.md`]({proj}.md) — {len(grouped[proj])} entries")
    idx.append("")
    idx.append("Pinned prompts live in [`prompts/`](prompts/).")
    idx.append("")

    (AUDIT / "INDEX.md").write_text("\n".join(idx) + "\n", encoding="utf-8")

    for proj in ["battery-tool-v2", "pm2", "regolith", "goal-flight"]:
        ids = grouped[proj]
        body = []
        body.append(f"# Queue inventory for **{PROJECT_TITLES[proj]}** — 2026-08-27")
        body.append("")
        body.append(BANNER)
        body.append("")
        body.append(PROJECT_INTRO[proj])
        body.append("")
        body.append(f"See [INDEX.md](INDEX.md) for population counts and method. This file lists **{len(ids)} / {union}** union dispatch ids whose `project_root` (or cwd) belongs to this project.")
        body.append("")
        pin_n = sum(1 for d in ids if ENTRIES[d].get("prompt_pin_status") == "pinnable")
        miss_n = len(ids) - pin_n
        body.append(f"Prompts: **{pin_n} / {len(ids)}** pinnable, **{miss_n} / {len(ids)}** missing.")
        body.append("")
        body.append("## Quick list")
        body.append("")
        body.append("| dispatch_id | age | owner | agent | claim pid | prompt | TLDR |")
        body.append("|---|---|---|---|---|---|---|")
        for did in ids:
            e = ENTRIES[did]
            tldr = TLDR.get(did, "")
            pin = "pinned" if e.get("prompt_pin_status") == "pinnable" else "**MISSING**"
            body.append(
                f"| [`{did}`](#{did}) | {md_escape(e.get('age'))} | `{md_escape(e.get('owner_label'))}` | `{md_escape(e.get('agent'))}` | {md_escape(claim_cell(e))} | {pin} | {md_escape(tldr)} |"
            )
        body.append("")
        body.append("## Entries")
        body.append("")
        for did in ids:
            body.append(emit_entry(did))
        (AUDIT / f"{proj}.md").write_text("\n".join(body) + "\n", encoding="utf-8")

    missing_tldr = [d for d in ENTRIES if d not in TLDR]
    if missing_tldr:
        raise SystemExit(f"missing TLDRs: {missing_tldr}")
    print("wrote INDEX +", list(PROJECT_TITLES))
    print("per project", {k: len(v) for k, v in grouped.items()})


if __name__ == "__main__":
    main()
