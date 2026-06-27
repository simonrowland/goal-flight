// tasks-data.js — static snapshot mirror of tasks.jsonl
// Loaded via <script src> so it works on file:// (no fetch / no CORS / no server).
// Served mode prefers fetch(tasks.jsonl); this is the file:// fallback (see gf.js).
//
// INVARIANT: this file is a faithful field-for-field mirror of tasks.jsonl
//   (one JSON object per non-blank line). They are hand-maintained and drift
//   silently, so a hermetic test (scripts/check_tasks_mirror.js) keeps them
//   in lockstep. No `status` key on any item — status is derived at render time.
//
// Item schema (see protocols/task-lifecycle.md):
//   { id, kind:task|bug|decision, title, blocked_by:[ids], links:[], done:bool, acceptance? }
window.GF_ITEMS = [
  { id: "t-001", kind: "task", title: "Example open task — replace with your first real chunk.", blocked_by: [], links: [], done: false, acceptance: "this stub is overwritten by a real task during init/decompose." },
  { id: "t-002", kind: "task", title: "Example done task — mirrors a completed chunk.", blocked_by: ["t-001"], links: ["NORTH-STAR.md"], done: true, acceptance: "shown in the Done section of the dashboard." },
];

// Optional served-mode parity: emit the same array for CommonJS consumers.
// (Under the hermetic checker's vm window-shim, `module` is undefined so this is skipped.)
if (typeof module !== "undefined" && module.exports) { module.exports = window.GF_ITEMS; }
