# Views — rendering model (two tiers)

Artifacts split by who edits them. (Layout + what links where:
[project-state-layout.md](project-state-layout.md); item model:
[task-lifecycle.md](task-lifecycle.md).)

## Tier 1 — controller-edited docs (markdown, zero-JS, context-light)

The docs the controller/human authors directly: `NORTH-STAR`, `SRS`,
`ARCHITECTURE` (+ `-increments`), `TEST-PLAN`, the newest `RESUME-NOTES` pin,
`history`. Plain markdown, no JavaScript — render in a browser AND the chat-
console preview. Zero-JS here keeps the controller's *editing* context light
(no file complexity); it is NOT a constraint on the derived views below.

## Tier 2 — json-derived views (self-contained HTML+JS, no backend, no generator)

Views derived from `tasks.jsonl` are STATIC pages + a shared JS include that
reads the data client-side and renders — there is **no Python page-generator**:

- `tickets.html` — the work-item list (tasks + bugs) with live sort/filter
  (kind, status, severity).
- `ticket.html` — item details (`?id=t-014`).
- bug backlog / dashboard — filter-views (`kind` + status) of the same data.

Mechanics (offline, no server):

- Data ships as **`tasks-data.js`** (`window.GF_ITEMS = [...]`), a trivial mirror
  of `tasks.jsonl` the helper writes on change. Pages load it via
  `<script src="tasks-data.js">` — a script include works on `file://` (it is not
  a `fetch`, so no CORS block, no local server). `fetch('tasks.jsonl')` would be
  blocked on `file://` — that's why the data is a `.js`.
- A shared **`gf.js`** renders list/details + **autolinks** every `t-NNN` /
  `b-NNN` / `q-NNN` id it finds (incl. in the recap) to the ticket views.
- Pages are static, vendored once (no per-change build); they always show current
  data because they read it live — **no staleness, no generator-must-run hazard.**
- LIFO / grey-done / sort / filter are JS here; CSS + JS inlined or vendored
  locally (no external deps; keep it self-contained).
- Trade-off: JS pages render in a real browser only — NOT the chat-console
  preview. Tier-1 markdown stays console-viewable.

The narrative recap (`history` / the overnight "copied-and-expanded" story) is
minimalist HTML that includes `gf.js` purely to make the ids clickable.

## Refresh — no server, ever

The data loads as a plain `<script src="tasks-data.js">` (a script include, NOT a
`fetch`), so a **double-clicked `file://` page displays it** — no server, no port.
To update: a **Reload** button (`location.reload()`) + an optional
`<meta http-equiv="refresh">` auto-reload; a full reload re-reads `tasks-data.js`
from disk. Persist filter/sort/search in the URL so a reload keeps the view.

There is no `fetch`, no `Last-Modified`, and no `localhost` server anywhere. A
server would only buy auto-refresh *without* a manual reload — the Reload button
covers that, so it isn't worth a dependency.

## Autolinking (ids + file paths)

`gf.js`'s linkify pass (HTML-escape → wrap matches) turns two things into links:

- **Item ids** `t-NNN` / `b-NNN` / `q-NNN` → `ticket.html?id=…`.
- **File-path mentions:**
  - *opinionated-dir, repo-root-relative* — path whose repo-root-relative form
    starts with an allowlisted prefix (`docs-private/`, `specs/`, … — configurable)
    and ends in `.md` (+ any tracked text exts you add). href = `../` + the path
    (views sit one level below repo-root in `docs-private/`). No saved variable;
    resolves on `file://` and served.
  - *absolute* — derive the repo-root at LOAD TIME from `window.location` (+ the
    page's known depth), strip that prefix → relative `../` href. NO baked
    `GF_ROOT` — it would break when the same files open from a different checkout
    dir or machine. An absolute path with a foreign prefix stays plain text.

Links resolve on `file://` with no server — a relative `../specs/…` href from
`…/docs-private/tickets.html` opens `…/specs/…` directly in the browser.
