/* gf.js — shared client engine for the goal-flight Tier-2 views.
 *
 * Zero dependencies, no build step, no backend. Loaded by tickets.html,
 * ticket.html, current-activity.html, burndown.html and index.html via a
 * single <script src>.
 *
 * Responsibilities:
 *   - DATA LOADING: read the window.GF_ITEMS snapshot shipped by tasks-data.js.
 *   - REFRESH: manual Refresh button + page reload on visibilitychange/focus
 *     so file:// views pick up a changed tasks-data.js.
 *   - RENDER: sectioned status board, kind/status/search filters, sort, counts.
 *   - AUTOLINK: \b[tbq]-\d+\b ids -> ticket.html?id=...
 *   - MENTIONS: single ids/path mentions -> safe links, everything else code.
 *
 * Public surface: window.GF = { load, items, status, autolink, ... }.
 */
(function (global) {
  "use strict";

  /* ------------------------------------------------------------------ utils */

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  function qs(name) {
    try {
      return new URLSearchParams(global.location.search).get(name);
    } catch (e) {
      return null;
    }
  }

  function hasOwn(obj, key) {
    return Object.prototype.hasOwnProperty.call(obj, key);
  }

  var CONTROL_CHARS_RE = /[\x00-\x1F\x7F]/;
  var SCHEME_RE = /^[a-zA-Z][a-zA-Z0-9+.-]*:/;

  function isRepoRelativeHref(value) {
    return value.indexOf("//") !== 0 &&
      value.charAt(0) !== "/" &&
      value.indexOf("\\") < 0 &&
      !SCHEME_RE.test(value) &&
      !/(^|\/)\.\.(?:\/|$)/.test(value);
  }

  function safeHref(url) {
    var raw = String(url == null ? "" : url);
    if (CONTROL_CHARS_RE.test(raw)) return "#";
    var value = raw.trim();
    if (!value) return "#";
    if (value.charAt(0) === "#") return esc(value);
    try {
      var base = "file:///";
      if (global.location && global.location.href) base = global.location.href;
      var parsed = new URL(value, base);
      var protocol = String(parsed.protocol || "").toLowerCase();
      if (protocol === "http:" || protocol === "https:" || protocol === "mailto:") return esc(value);
      if (isRepoRelativeHref(value)) return esc(value);
    } catch (e) {}
    return "#";
  }

  function ticketHref(id) {
    return "ticket.html?id=" + encodeURIComponent(String(id == null ? "" : id));
  }

  /* --------------------------------------------------------------- model */

  // Canonical status order + display metadata. 'worker-finished' remains a
  // tolerated input state, but the section vocabulary is 'awaiting-review'.
  var SECTIONS = [
    { key: "decision", label: "Decisions needed", cls: "sec-decision" },
    { key: "pending", label: "To do", cls: "sec-pending" },
    { key: "working", label: "In progress", cls: "sec-working" },
    { key: "awaiting-review", label: "Awaiting review", cls: "sec-review" },
    { key: "worker-failed", label: "Failed / needs attention", cls: "sec-failed" },
    { key: "waiting", label: "Waiting", cls: "sec-waiting" },
    { key: "backlog", label: "Backlog", cls: "sec-backlog" },
    { key: "done-reviewed", label: "Done", cls: "sec-done" }
  ];

  var STATUS_LABELS = {
    decision: "decision",
    pending: "to do",
    working: "in progress",
    "awaiting-review": "awaiting review",
    "worker-failed": "worker failed",
    waiting: "waiting",
    backlog: "backlog",
    "done-reviewed": "done reviewed"
  };

  var SEV_RANK = { critical: 4, high: 3, medium: 2, low: 1 };
  var RESERVED_LANES = { deferred: true, held: true };

  function canonicalSection(status) {
    var s = String(status == null ? "" : status).trim().toLowerCase();
    if (!s) return null;
    if (s === "done" || s === "closed") return "done-reviewed";
    if (s === "worker-finished") return "awaiting-review";
    if (s === "blocked") return "waiting";
    if (s === "outstanding") return "pending";
    if (s === "delegated") return "working";
    if (s === "decision" || s === "pending" || s === "working" ||
      s === "awaiting-review" || s === "worker-failed" || s === "waiting" ||
      s === "done-reviewed") return s;
    return null;
  }

  // Normalize a raw item: fill defaults, never throw on a missing field.
  function normalize(raw) {
    var it = raw && typeof raw === "object" ? raw : {};
    var id = typeof it.id === "string" && it.id ? it.id : "(no-id)";
    var kind = it.kind === "bug" || it.kind === "decision" ? it.kind : "task";
    var schemaVersion = typeof it.schema_version === "number" ? it.schema_version : null;
    var legacyDone = schemaVersion == null && !!it.done && it.done_reviewed == null;
    var doneReviewed = !!it.done_reviewed || it.status === "done-reviewed" || it.status === "done" ||
      legacyDone || (kind === "decision" && !!it.done);
    var workerDone = !doneReviewed && (!!it.done || it.status === "awaiting-review" || it.status === "worker-finished");
    return {
      id: id,
      kind: kind,
      title: typeof it.title === "string" ? it.title : "(untitled)",
      lane: typeof it.lane === "string" ? it.lane : null,
      status: it.status || null, // may be derived below
      derived_status: typeof it.derived_status === "string" ? it.derived_status : null,
      blocked_by: Array.isArray(it.blocked_by) ? it.blocked_by.filter(Boolean) : [],
      links: Array.isArray(it.links) ? it.links.filter(Boolean) : [],
      done: doneReviewed,
      done_reviewed: doneReviewed,
      worker_done: workerDone,
      severity: it.severity || null,
      pattern: it.pattern || null,
      source: it.source || null,
      acceptance: it.acceptance || null,
      prompt: it.prompt || null,
      prompt_path: it.prompt_path || null,
      created_at: it.created_at || null,
      done_at: it.done_at || null,
      done_reviewed_at: it.done_reviewed_at || null,
      closed_at: it.closed_at || null,
      reviewed_at: it.reviewed_at || null,
      completed_at: it.completed_at || null,
      audit: Array.isArray(it.audit) ? it.audit.filter(Boolean) : [],
      // Durable dispatch breadcrumb (ADR-011 / task-lifecycle.md "Dispatch
      // provenance"). Each entry: { dispatch_id, agent, log, started_at,
      // ended_at, state, marker, worker_pid? }. Absent on hand-maintained items.
      dispatches: Array.isArray(it.dispatches) ? it.dispatches.filter(Boolean) : [],
      tags: Array.isArray(it.tags) ? it.tags : []
    };
  }

  function blockerResolved(bid, byId) {
    var b = byId && byId[bid];
    return !!(b && b.done);
  }

  function unresolvedBlockerIds(it, byId) {
    var out = [];
    (it.blocked_by || []).forEach(function (bid) {
      if (!blockerResolved(bid, byId)) out.push(bid);
    });
    return out;
  }

  // Section key for an item. Honours explicit lifecycle flags; otherwise derives
  // from status, blockers, and kind. Unknown blockers count as unresolved.
  function sectionKey(it, byId) {
    if (it.done_reviewed) return "done-reviewed";
    if (RESERVED_LANES[it.lane]) return "backlog";
    var derived = canonicalSection(it.derived_status);
    if (derived) return derived;
    var s = it.status;
    if (it.kind === "decision") return "decision";
    if (it.worker_done || s === "worker-finished" || s === "awaiting-review") return "awaiting-review";
    if (s === "working" || s === "worker-failed") return s;
    if (unresolvedBlockerIds(it, byId).length) return "waiting";
    if (s === "waiting") return "waiting";
    if (s === "done" || s === "done-reviewed") return "done-reviewed";
    if (s === "pending" || !s) return "pending";
    return "pending";
  }

  /* ----------------------------------------------------------- autolink */

  // Match every linkable item-id family actually used as an id: t-/b-/q- plus
  // ADR-/bp- (case-insensitive, to match idLink/linkList which already accept
  // ADR-/bp- and Q-). The captured id is emitted verbatim so casing is preserved.
  var ID_RE = /\b((?:ADR|bp|[tbq])-\d+)\b/gi;
  var ID_EXACT_RE = /^(?:ADR|bp|[tbq])-\d+$/i;

  // Allowlisted repo-root-relative directory prefixes for file-path autolinking
  // (ADR-012 / progress-dashboard.md "Autolinking"). Configurable: a deployment
  // may extend it via window.GF_PATH_PREFIXES before gf.js loads, or splice this
  // array at runtime. A mention only links when its repo-root-relative form
  // starts with one of these AND ends in an allowlisted extension (below).
  var PATH_PREFIXES = Array.isArray(global.GF_PATH_PREFIXES) && global.GF_PATH_PREFIXES.length
    ? global.GF_PATH_PREFIXES.slice()
    : ["docs-private/", "specs/", "reviews/", "research/", "architecture/", "plans/"];

  // Linkable file extensions. .md is the spec'd default; a deployment can extend
  // window.GF_PATH_EXTS to add tracked text exts without editing gf.js.
  var PATH_EXTS = Array.isArray(global.GF_PATH_EXTS) && global.GF_PATH_EXTS.length
    ? global.GF_PATH_EXTS.slice()
    : ["md"];

  // Absolute repo root. The helper will write window.GF_ROOT (absolute repo root)
  // into tasks-data.js; honour it if already set. Otherwise DERIVE it from the
  // current location: the views sit at <root>/dashboard/<page> (or legacy
  // <root>/docs-private/<page>), so stripping that trailing segment yields the
  // repo root. Returns "" when it can't be derived.
  function gfRoot() {
    if (typeof global.GF_ROOT === "string" && global.GF_ROOT) {
      // normalize: drop a trailing slash so the under-root prefix test is clean
      return global.GF_ROOT.replace(/\/+$/, "");
    }
    try {
      // pathname like /path/to/repo/dashboard/tickets.html (file://).
      var path = decodeURIComponent(global.location.pathname || "");
      var m = /^(.*)\/(?:dashboard|docs-private)\/[^/]*$/.exec(path);
      return m ? m[1] : "";
    } catch (e) {
      return "";
    }
  }

  // Repo/project name = basename of the derived repo root. Used to brand every
  // view so sibling-repo dashboards are distinguishable at a glance. "" when the
  // root can't be derived (branding is then a no-op; the on-disk skeleton stands).
  function gfRepoName() {
    var root = gfRoot();
    if (!root) return "";
    var segs = root.split("/").filter(Boolean);
    return segs.length ? segs[segs.length - 1] : "";
  }

  // Brand the current view with the repo name: prepend it to <title> (so it is
  // the leading, non-truncated token in the browser tab) and inject a slim repo
  // banner as the first child of <main> (every view has one). Runs once per page;
  // touches only the live DOM, never the on-disk skeleton, so the doctor
  // "<title>goal-flight"/"<h1>goal-flight" markers and byte-equality checks hold.
  var repoBrandingApplied = false;
  function applyRepoBranding() {
    if (repoBrandingApplied) return;
    repoBrandingApplied = true;
    var repo = gfRepoName();
    var doc = global.document;
    if (!repo || !doc) return;
    try {
      if (typeof doc.title === "string" && doc.title.indexOf(repo + " · ") !== 0) {
        doc.title = repo + " · " + doc.title;
      }
    } catch (e) {}
    try {
      var main = doc.querySelector && doc.querySelector("main");
      if (!main || doc.getElementById("gf-repobar")) return;
      if (!doc.getElementById("gf-repobar-style")) {
        var st = doc.createElement("style");
        st.id = "gf-repobar-style";
        st.textContent =
          "#gf-repobar{font:.8rem ui-monospace,SFMono-Regular,Menlo,monospace;" +
          "color:var(--muted);letter-spacing:.02em;margin:0 0 .6rem;display:flex;" +
          "align-items:baseline;gap:.4rem;flex-wrap:wrap}" +
          "#gf-repobar .gf-repo{color:var(--fg);font-weight:600;letter-spacing:-.01em}" +
          "#gf-repobar .gf-sep{opacity:.5}";
        (doc.head || doc.body || main).appendChild(st);
      }
      var bar = doc.createElement("div");
      bar.id = "gf-repobar";
      bar.setAttribute("aria-label", "Project");
      var name = doc.createElement("span");
      name.className = "gf-repo";
      name.textContent = repo;
      var sep = doc.createElement("span");
      sep.className = "gf-sep";
      sep.textContent = "·";
      var mark = doc.createElement("span");
      mark.textContent = "goal-flight";
      bar.appendChild(name);
      bar.appendChild(sep);
      bar.appendChild(mark);
      main.insertBefore(bar, main.firstChild);
    } catch (e) {}
  }

  // Build the path regex once from the configurable prefix + ext lists. A path
  // token is a run of path-ish chars; we then validate prefix/ext in the handler
  // (cheaper + clearer than a monster regex). Optional leading '/' captures the
  // absolute-under-repo case, resolved against GF_ROOT in the handler.
  // Stops at whitespace, quotes, parens, commas and the common sentence-enders so
  // a trailing period/comma after a filename isn't swallowed into the href.
  var PATH_RE = /(^|[\s(>])(\/?(?:[\w.\-]+\/)+[\w.\-]+\.[A-Za-z0-9]+)/g;

  function hasAllowedExt(p) {
    var dot = p.lastIndexOf(".");
    if (dot < 0) return false;
    var ext = p.slice(dot + 1).toLowerCase();
    for (var i = 0; i < PATH_EXTS.length; i++) {
      if (ext === PATH_EXTS[i].toLowerCase()) return true;
    }
    return false;
  }

  function hasAllowedPrefix(rel) {
    for (var i = 0; i < PATH_PREFIXES.length; i++) {
      if (rel.indexOf(PATH_PREFIXES[i]) === 0) return true;
    }
    return false;
  }

  // Canonical path-token grammar shared by every link sink: the same charset
  // the autolink tokenizer (PATH_RE) accepts, enforced here so direct callers
  // (renderMention) can't smuggle quotes, angle brackets, control chars,
  // backslashes, spaces, or scheme-ish strings into an href — PLUS a
  // dot-segment ban so an allowlisted prefix can't be escaped with a later
  // '..' (docs-private/../SKILL.md must stay plain text).
  var PATH_TOKEN_RE = /^\/?(?:[\w.\-]+\/)*[\w.\-]+$/;

  function isSafePathToken(p) {
    if (!PATH_TOKEN_RE.test(p)) return false;
    var segs = String(p).split("/");
    for (var i = 0; i < segs.length; i++) {
      if (segs[i] === "." || segs[i] === "..") return false;
    }
    return true;
  }

  // Resolve a raw path mention to a repo-root-relative form, or null if it must
  // stay plain text. Handles: repo-root-relative (allowlisted prefix), and
  // absolute-under-GF_ROOT (stripped to relative). Absolute paths NOT under the
  // repo, relative paths outside the allowlist, and anything failing the path
  // token grammar (hostile chars, dot segments) return null (plain text).
  function resolvePathMention(raw) {
    if (!isSafePathToken(raw)) return null;
    if (!hasAllowedExt(raw)) return null;
    var rel = raw;
    if (raw.charAt(0) === "/") {
      var root = gfRoot();
      // Only an absolute path UNDER the repo root is linkable.
      if (!root) return null;
      if (raw === root || raw.indexOf(root + "/") !== 0) return null;
      rel = raw.slice(root.length + 1); // strip "<root>/"
    }
    return hasAllowedPrefix(rel) ? rel : null;
  }

  // Run the file-path linkify over an ALREADY-ESCAPED html string, but only on
  // text OUTSIDE existing <a ...>…</a> tags — so we never double-link, never
  // re-link an id link, and never touch href/attribute text. We split on whole
  // anchor elements and only transform the gaps between them.
  var ANCHOR_SPLIT_RE = /(<a\b[^>]*>[\s\S]*?<\/a>)/gi;

  function linkifyPaths(escapedHtml) {
    var parts = String(escapedHtml).split(ANCHOR_SPLIT_RE);
    for (var i = 0; i < parts.length; i++) {
      // odd indices are the captured <a>…</a> blocks — leave them untouched
      if (i % 2 === 1) continue;
      parts[i] = parts[i].replace(PATH_RE, function (whole, lead, raw) {
        // `raw` is already HTML-escaped text; path chars (\w . - /) are escape-
        // safe so it equals the literal path. Resolve against the allowlist.
        var rel = resolvePathMention(raw);
        if (!rel) return whole; // not linkable -> leave verbatim
        // views sit one level below repo-root, so '../' + rel reaches the file
        // from a dashboard view.
        var href = "../" + rel;
        return lead + '<a class="pathlink" href="' + esc(href) + '">' + raw + "</a>";
      });
    }
    return parts.join("");
  }

  // Wrap every recognized item id AND allowlisted file-path mention in text as a
  // link. Input may be plain text OR already-escaped html; we escape here (escape
  // FIRST), so pass raw. Ids → ticket.html?id=…; paths → '../'+repo-relative.
  function autolink(text) {
    var safe = esc(text);
    safe = safe.replace(ID_RE, function (m, id) {
      return '<a class="idlink" href="ticket.html?id=' + encodeURIComponent(id) + '">' + id + "</a>";
    });
    // path-link AFTER id-link, skipping inside the <a> blocks just created.
    return linkifyPaths(safe);
  }

  // A single id rendered as a ticket link (with optional class).
  function idLink(id, cls) {
    var c = cls ? ' class="' + cls + '"' : "";
    return '<a' + c + ' href="' + ticketHref(id) + '">' + esc(id) + "</a>";
  }

  // Policy: a trailing ':NNN' is always a line reference — the href drops it,
  // the visible text keeps it. This is unambiguous because ':' is outside the
  // path-token grammar, so a literal colon-containing filename is never
  // linkable in the first place.
  function stripPathLineSuffix(raw) {
    return String(raw == null ? "" : raw).trim().replace(/:\d+$/, "");
  }

  // Id-only mention sink for fields whose CONTRACT is item ids (e.g. live
  // worker task_ids): exact id -> ticket link, anything else -> inert code
  // text. Path-shaped values in an ids field must not become path links.
  function renderIdOnly(raw, cls) {
    var text = String(raw == null ? "" : raw).trim();
    if (!text) return "<code></code>";
    if (ID_EXACT_RE.test(text)) return idLink(text, cls || "idlink");
    return "<code>" + esc(text) + "</code>";
  }

  // A single mention sink for detail views. It intentionally emits only:
  // item-id ticket links; allowlisted repo path links; or inert code text.
  function renderMention(raw, cls) {
    var text = String(raw == null ? "" : raw).trim();
    if (!text) return "<code></code>";
    if (ID_EXACT_RE.test(text)) return idLink(text, cls || "idlink");
    var rel = resolvePathMention(stripPathLineSuffix(text));
    if (rel) {
      return '<a class="pathlink" href="' + esc("../" + rel) + '">' + esc(text) + "</a>";
    }
    return "<code>" + esc(text) + "</code>";
  }

  /* ------------------------------------------------------------- loading */

  // Read the tasks-data.js snapshot. Resolves { items, mode:'snapshot', sig }.
  function snapshot() {
    var arr = Array.isArray(global.GF_ITEMS) ? global.GF_ITEMS : [];
    return { items: arr, mode: "snapshot", sig: "snapshot:" + arr.length };
  }

  /* --------------------------------------------------------------- store */

  // The store holds the last loaded raw items + the indexed/normalized view.
  var store = {
    raw: [],
    items: [],
    byId: {},
    mode: "snapshot",
    sig: null
  };

  function deriveItems(rawItems) {
    var items = (rawItems || []).map(function (raw, idx) {
      var it = normalize(raw);
      it._pos = idx;
      return it;
    });
    var byId = {};
    items.forEach(function (it) {
      byId[it.id] = it;
    });
    // second pass: derive section now that the id map exists
    items.forEach(function (it) {
      it._section = sectionKey(it, byId);
    });
    return { items: items, byId: byId };
  }

  function index(rawItems) {
    var derived = deriveItems(rawItems);
    store.raw = rawItems || [];
    store.items = derived.items;
    store.byId = derived.byId;
    return store;
  }

  /* ---------------------------------------------------- filter / sort */

  function applyControls(items, ctrl) {
    var kind = ctrl.kind || "all";
    var status = ctrl.status || "all";
    var q = (ctrl.q || "").trim().toLowerCase();
    var out = items.filter(function (it) {
      if (kind !== "all" && it.kind !== kind) return false;
      if (status !== "all" && it._section !== status) return false;
      if (q) {
        var hay = (it.id + " " + it.title + " " + (it.pattern || "") + " " + (it.tags || []).join(" ")).toLowerCase();
        if (hay.indexOf(q) === -1) return false;
      }
      return true;
    });
    return out;
  }

  function idNum(id) {
    var m = /(\d+)/.exec(id || "");
    return m ? parseInt(m[1], 10) : 0;
  }

  // Full alphabetic prefix of an id (e.g. 'bp' for 'bp-100', 'b' for 'b-100',
  // 'ADR' for 'ADR-002'). Used so distinct prefixes never collapse in id-sort.
  function idPrefix(id) {
    var m = /^([A-Za-z]+)/.exec(id || "");
    return m ? m[1].toLowerCase() : "";
  }

  function sortItems(items, sort) {
    var arr = items.slice();
    if (sort === "frontier") {
      return arr;
    }
    if (sort === "severity") {
      arr.sort(function (a, b) {
        return (SEV_RANK[b.severity] || 0) - (SEV_RANK[a.severity] || 0) || idNum(a.id) - idNum(b.id);
      });
    } else if (sort === "status") {
      var order = {};
      SECTIONS.forEach(function (s, i) {
        order[s.key] = i;
      });
      arr.sort(function (a, b) {
        return (order[a._section] - order[b._section]) || idNum(a.id) - idNum(b.id);
      });
    } else {
      // id (alpha prefix, then numeric within prefix, then full-id fallback so
      // distinct ids sharing a prefix+number — e.g. b-100 vs bp-100 — order
      // deterministically rather than by input order)
      arr.sort(function (a, b) {
        return idPrefix(a.id).localeCompare(idPrefix(b.id)) ||
          idNum(a.id) - idNum(b.id) ||
          String(a.id).localeCompare(String(b.id));
      });
    }
    return arr;
  }

  function normalizedSort(sort) {
    return sort === "id" || sort === "status" || sort === "severity" ? sort : "frontier";
  }

  /* --------------------------------------------------------------- render */

  function kindBadge(kind) {
    return '<span class="kind kind-' + esc(kind) + '" title="' + esc(kind) + '">' + esc(kind) + "</span>";
  }

  function statusBadge(section) {
    return '<span class="badge badge-' + esc(section) + '">' + esc(STATUS_LABELS[section] || section) + "</span>";
  }

  function laneBadge(it) {
    if (!it.lane) return "";
    return '<span class="badge lane lane-' + esc(it.lane) + '">lane ' + esc(it.lane) + "</span>";
  }

  function blockerBits(it) {
    if (!it.blocked_by || !it.blocked_by.length) return "";
    var parts = it.blocked_by.map(function (bid) {
      var resolved = blockerResolved(bid, store.byId);
      var cls = "blocker" + (resolved ? " blocker-ok" : "");
      var glyph = resolved ? "✓" : "⏸";
      var label = esc(bid) + ", blocker " + (resolved ? "resolved" : "still blocking");
      return '<a class="' + cls + '" href="' + ticketHref(bid) +
        '" aria-label="' + label + '"><span aria-hidden="true">' + glyph + " </span>" + esc(bid) + "</a>";
    });
    return '<span class="blockers">' + parts.join(" ") + "</span>";
  }

  function openDecisions() {
    return store.items.filter(function (it) {
      return it._section === "decision";
    });
  }

  function decisionBlockIds(it) {
    var holds = store.items.filter(function (t) {
      return (t.blocked_by || []).indexOf(it.id) >= 0;
    }).map(function (t) {
      return t.id;
    });
    if (!holds.length && it.links) {
      holds = it.links.filter(function (l) {
        return /^[tbq]-\d+$/.test(l);
      });
    }
    return holds;
  }

  function renderDecisionSummary(it) {
    var holds = decisionBlockIds(it);
    var tail = holds.length
      ? " — blocks " + holds.map(function (h) { return idLink(h); }).join(", ")
      : "";
    return '<span class="decision-summary">' + idLink(it.id) + tail + "</span>";
  }

  function renderDecisionStrip() {
    var open = openDecisions();
    if (!open.length) return "";
    return '<span aria-hidden="true">⚠ </span><span class="visually-hidden">Attention: </span>' +
      open.map(renderDecisionSummary).join(" · ");
  }

  function renderDecisionList() {
    var open = openDecisions();
    if (!open.length) return '<p class="empty decisions-empty">no open decisions</p>';
    return '<div class="decision-list">' + open.map(function (it) {
      var holds = decisionBlockIds(it);
      var blocks = holds.length
        ? holds.map(function (h) { return idLink(h); }).join(", ")
        : '<span class="muted">none</span>';
      var body = it.acceptance ? '<p>' + autolink(it.acceptance) + "</p>" : "";
      return '<section class="decision-item" id="' + esc(it.id) + '">' +
        '<h2><span class="qid">' + idLink(it.id) + '</span> <span class="decision-title">' + autolink(it.title) + "</span></h2>" +
        '<p class="meta">blocks ' + blocks + "</p>" +
        body +
        "</section>";
    }).join("") + "</div>";
  }

  function sevBit(it) {
    if (it.kind !== "bug" || !it.severity) return "";
    return '<span class="sev sev-' + esc(it.severity) + '">' + esc(it.severity) + "</span>";
  }

  function cyclicBit(enabled) {
    if (!enabled) return "";
    return '<span class="badge cyclic" title="dependency cycle">⟳ cyclic</span>';
  }

  function blockedMissingBit(ids) {
    if (!ids || !ids.length) return "";
    var label = ids.length === 1 ? "⚠ blocked by missing " + ids[0] : "⚠ blocked by missing " + ids.length;
    return '<span class="blocker blocked-missing" title="' +
      esc("blocked by missing: " + ids.join(", ")) + '">' + esc(label) + "</span>";
  }

  // One list row. Title autolinks ids; id is itself a ticket link.
  function rowHTML(it, opts) {
    opts = opts || {};
    return (
      '<li class="row row-' + esc(it._section) + '" data-id="' + esc(it.id) + '">' +
      '<a class="id" href="' + ticketHref(it.id) + '">' + esc(it.id) + "</a>" +
      '<span class="body">' +
      '<span class="title">' + autolink(it.title) + "</span>" +
      '<span class="tags">' + kindBadge(it.kind) + statusBadge(it._section) + laneBadge(it) + sevBit(it) +
        blockerBits(it) + blockedMissingBit(opts.blockedMissing) + cyclicBit(opts.cyclic) + "</span>" +
      "</span>" +
      "</li>"
    );
  }

  function dependencyLayers(openItems, byId) {
    var ids = [];
    var idSet = Object.create(null);
    var pos = Object.create(null);
    openItems.forEach(function (it, idx) {
      ids.push(it.id);
      idSet[it.id] = true;
      pos[it.id] = typeof it._pos === "number" ? it._pos : idx;
    });

    var deps = Object.create(null);
    var external = Object.create(null);
    var remaining = Object.create(null);
    ids.forEach(function (id) {
      remaining[id] = true;
    });
    openItems.forEach(function (it) {
      deps[it.id] = [];
      external[it.id] = [];
      unresolvedBlockerIds(it, byId).forEach(function (bid) {
        if (idSet[bid]) deps[it.id].push(bid);
        else external[it.id].push(bid);
      });
    });

    var levels = Object.create(null);
    var cyclic = Object.create(null);
    var blockedMissing = Object.create(null);
    var peeled = Object.create(null);
    var level = 0;

    function mergeMissing(id, missingIds) {
      if (!missingIds || !missingIds.length) return false;
      if (!blockedMissing[id]) blockedMissing[id] = [];
      var changed = false;
      missingIds.forEach(function (missingId) {
        if (blockedMissing[id].indexOf(missingId) < 0) {
          blockedMissing[id].push(missingId);
          changed = true;
        }
      });
      return changed;
    }

    ids.forEach(function (id) {
      mergeMissing(id, external[id]);
    });

    while (true) {
      var ready = ids.filter(function (id) {
        return remaining[id] && !external[id].length && deps[id].every(function (bid) {
          return peeled[bid];
        });
      });
      if (!ready.length) break;
      ready.sort(function (a, b) {
        return pos[a] - pos[b];
      });
      ready.forEach(function (id) {
        levels[id] = level;
        peeled[id] = true;
        delete remaining[id];
      });
      level += 1;
    }

    var changed = true;
    while (changed) {
      changed = false;
      ids.forEach(function (id) {
        if (!remaining[id]) return;
        deps[id].forEach(function (bid) {
          if (blockedMissing[bid]) changed = mergeMissing(id, blockedMissing[bid]) || changed;
        });
      });
    }

    var visiting = Object.create(null);
    var visited = Object.create(null);
    var stack = [];

    function markCycle(start) {
      for (var i = start; i < stack.length; i += 1) cyclic[stack[i]] = true;
    }

    function visit(id) {
      if (visited[id] || blockedMissing[id]) return;
      visiting[id] = stack.length;
      stack.push(id);
      deps[id].forEach(function (bid) {
        if (!remaining[bid] || blockedMissing[bid]) return;
        if (hasOwn(visiting, bid)) {
          markCycle(visiting[bid]);
        } else {
          visit(bid);
        }
      });
      stack.pop();
      delete visiting[id];
      visited[id] = true;
    }

    ids.forEach(function (id) {
      if (remaining[id]) visit(id);
    });

    return { levels: levels, cyclic: cyclic, blockedMissing: blockedMissing };
  }

  function laneName(it) {
    return it.lane ? String(it.lane) : "";
  }

  function laneKind(name) {
    if (!name) return "default";
    return RESERVED_LANES[name] ? "parked" : "active";
  }

  function laneGroups(rows) {
    var byName = Object.create(null);
    var groups = [];
    rows.forEach(function (it) {
      var name = laneName(it);
      var group = byName[name];
      if (!group) {
        group = { name: name, kind: laneKind(name), firstPos: it._pos || 0, rows: [] };
        byName[name] = group;
        groups.push(group);
      }
      group.rows.push(it);
      if (typeof it._pos === "number" && it._pos < group.firstPos) group.firstPos = it._pos;
    });

    function byFirstPos(a, b) {
      return a.firstPos - b.firstPos;
    }

    return groups.filter(function (g) { return g.kind === "default"; }).sort(byFirstPos)
      .concat(groups.filter(function (g) { return g.kind === "active"; }).sort(byFirstPos))
      .concat(groups.filter(function (g) { return g.kind === "parked"; }).sort(byFirstPos));
  }

  function orderedLaneRows(rows, layerInfo) {
    return rows.slice().sort(function (a, b) {
      var ac = !!layerInfo.cyclic[a.id];
      var bc = !!layerInfo.cyclic[b.id];
      if (ac !== bc) return ac ? 1 : -1;
      var al = hasOwn(layerInfo.levels, a.id) ? layerInfo.levels[a.id] : 0;
      var bl = hasOwn(layerInfo.levels, b.id) ? layerInfo.levels[b.id] : 0;
      return (al - bl) || ((a._pos || 0) - (b._pos || 0));
    });
  }

  function renderLaneGroup(group, layerInfo, sort) {
    var parked = group.kind === "parked";
    var label = group.name ? esc(group.name) : "No lane";
    var rows = sort === "frontier" ? orderedLaneRows(group.rows, layerInfo) : group.rows.slice();
    return '<section class="lane-group lane-group-' + group.kind + (parked ? " lane-group-parked" : "") + '">' +
      '<h3><span class="lane-name">' + label + '</span> ' +
      '<span class="count" data-lane-count="' + group.rows.length + '">' + group.rows.length +
      ' open</span>' + (parked ? ' <span class="parked-label">parked</span>' : "") + "</h3>" +
      '<ul class="rows">' +
      rows.map(function (it) {
        return rowHTML(it, {
          blockedMissing: layerInfo.blockedMissing[it.id],
          cyclic: !!layerInfo.cyclic[it.id]
        });
      }).join("") +
      "</ul></section>";
  }

  function renderOpenLaneGroups(rows, layerInfo, sort) {
    if (!rows.length) return "";
    return '<section class="sec sec-open-lanes" aria-label="Open tickets">' +
      '<h2>Open tickets <span class="count" data-open-count="' + rows.length + '">' + rows.length +
      ' <span class="visually-hidden">items</span></span></h2>' +
      laneGroups(rows).map(function (group) {
        return renderLaneGroup(group, layerInfo, sort);
      }).join("") +
      "</section>";
  }

  // Render the full sectioned board into `mount`, honouring controls.
  // Returns total visible count.
  function renderBoard(mount, ctrl) {
    ctrl = ctrl || {};
    var sort = normalizedSort(ctrl.sort);
    var filtered = applyControls(store.items, ctrl);
    var sorted = sort === "frontier" ? filtered.slice() : sortItems(filtered, sort);
    var openAll = store.items.filter(function (it) {
      return it._section !== "done-reviewed";
    });
    var layerInfo = dependencyLayers(openAll, store.byId);
    var openRows = sorted.filter(function (it) {
      return it._section !== "done-reviewed";
    });
    var doneRows = sorted.filter(function (it) {
      return it._section === "done-reviewed";
    });

    var html = "";
    html += renderOpenLaneGroups(openRows, layerInfo, sort);
    SECTIONS.forEach(function (sec) {
      if (sec.key !== "done-reviewed") return;
      if (!doneRows.length) return;
      // Done newest-first (LIFO), preserving the previous done section behaviour.
      var ordered = sort === "frontier" ? doneRows.slice().reverse() : doneRows;
      html +=
        '<section class="sec ' + sec.cls + '" aria-label="' + esc(sec.label) + '">' +
        '<h2>' + esc(sec.label) + ' <span class="count">' + doneRows.length + ' <span class="visually-hidden">items</span></span></h2>' +
        '<ul class="rows done-list">' +
        ordered.map(rowHTML).join("") +
        "</ul></section>";
    });

    if (!html) {
      html = '<p class="empty">No items match. <button type="button" class="linkbtn" data-gf-clear>Clear filters</button></p>';
    }
    mount.innerHTML = html;
    return filtered.length;
  }

  /* ----------------------------------------------------- refresh driver */

  // Drive snapshot load + re-render. Manual Refresh and focus/visibility reload
  // the page so file:// views read a fresh tasks-data.js from disk.
  // onRender gets the store after each changed load; onMode is called once.
  function attach(opts) {
    var onRender = opts.onRender || function () {};
    var onMode = opts.onMode || function () {};
    var lastSig = null;
    var destroyed = false;

    // Brand every view with the repo name (tab title + top-of-main banner).
    applyRepoBranding();

    function loadAndMaybeRender(force) {
      var res = snapshot();
      store.mode = res.mode;
      if (force || res.sig !== lastSig) {
        lastSig = res.sig;
        store.sig = res.sig;
        index(res.items);
        onRender(store);
      }
      return Promise.resolve(res);
    }

    function reloadPage() {
      if (destroyed) return Promise.resolve(snapshot());
      try {
        if (global.location && typeof global.location.reload === "function") {
          global.location.reload();
          return Promise.resolve({ items: store.raw, mode: store.mode, sig: store.sig });
        }
      } catch (e) {}
      return loadAndMaybeRender(true);
    }

    // Reload on focus/visibility so changed tasks-data.js is read from disk.
    function onVisible() {
      if (destroyed) return;
      if (global.document && global.document.visibilityState === "visible") {
        reloadPage();
      }
    }
    // Named focus handler so destroy() can actually detach it.
    function onFocus() {
      if (destroyed) return;
      reloadPage();
    }

    // Initial load determines the mode, then wires the refresh path.
    var ready = loadAndMaybeRender(true).then(function (res) {
      onMode(res.mode);
      try {
        global.document.addEventListener("visibilitychange", onVisible);
        global.addEventListener("focus", onFocus);
        // Tear down listeners when the page goes away so a re-attach on the same
        // document can't duplicate handlers.
        global.addEventListener("pagehide", destroy);
      } catch (e) {}
      return res;
    });

    function destroy() {
      destroyed = true;
      try {
        global.document.removeEventListener("visibilitychange", onVisible);
        global.removeEventListener("focus", onFocus);
        global.removeEventListener("pagehide", destroy);
      } catch (e) {}
    }

    return {
      ready: ready,
      mode: function () {
        return store.mode;
      },
      // manual Refresh button handler (file:// + always available)
      refresh: function () {
        return reloadPage();
      },
      destroy: destroy
    };
  }

  /* -------------------------------------------------------------- counts */

  // Per-section counts over ALL items (ignores filters) — for index.html.
  function counts() {
    var c = {};
    SECTIONS.forEach(function (s) {
      c[s.key] = 0;
    });
    store.items.forEach(function (it) {
      c[it._section] = (c[it._section] || 0) + 1;
    });
    c._total = store.items.length;
    return c;
  }

  /* ------------------------------------------------------------ burndown */

  function parseTime(value) {
    if (typeof value !== "string" || !value.trim()) return null;
    var n = Date.parse(value);
    return Number.isFinite(n) ? n : null;
  }

  function formatDurationMs(ms) {
    if (!Number.isFinite(ms) || ms < 0) return "";
    var total = Math.max(0, Math.round(ms / 1000));
    if (total < 60) return total + "s";
    var minutes = Math.round(total / 60);
    if (minutes < 60) return minutes + "m";
    var hours = Math.floor(minutes / 60);
    var remMinutes = minutes % 60;
    if (hours < 24) return hours + "h" + (remMinutes ? " " + remMinutes + "m" : "");
    var days = Math.floor(hours / 24);
    var remHours = hours % 24;
    return days + "d" + (remHours ? " " + remHours + "h" : "");
  }

  function formatDurationSeconds(seconds) {
    var n = Number(seconds);
    return Number.isFinite(n) ? formatDurationMs(n * 1000) : "";
  }

  function fmtClock(ms) {
    if (!Number.isFinite(ms)) return "";
    try {
      return new Date(ms).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
    } catch (e) {
      return "";
    }
  }

  function fmtDayLabel(ms) {
    if (!Number.isFinite(ms)) return "";
    try {
      return new Date(ms).toLocaleDateString([], { weekday: "short", month: "short", day: "numeric" });
    } catch (e) {
      return "";
    }
  }

  function freshnessInfo(meta, staleMs, prefix) {
    if (!meta || typeof meta !== "object" || meta.schema !== 1) return null;
    var t = parseTime(meta.generated_at);
    if (t == null) return null;
    var age = Math.max(0, Date.now() - t);
    var label = prefix || "data as of";
    return {
      generatedAt: t,
      ageMs: age,
      stale: age > staleMs,
      text: label + " " + fmtClock(t) + " · " + formatDurationMs(age) + " ago"
    };
  }

  function metaFreshness(staleMs) {
    return freshnessInfo(global.GF_META, staleMs == null ? 10 * 60 * 1000 : staleMs, "data as of");
  }

  function statusFreshness(staleMs) {
    return freshnessInfo(global.GF_STATUS, staleMs == null ? 90 * 1000 : staleMs, "live as of");
  }

  function statusSnapshot() {
    var status = global.GF_STATUS;
    if (!status || typeof status !== "object" || status.schema !== 1) return null;
    return {
      generated_at: status.generated_at || null,
      project_root: status.project_root || "",
      dispatches: Array.isArray(status.dispatches) ? status.dispatches : [],
      counts: status.counts && typeof status.counts === "object" ? status.counts : {}
    };
  }

  function firstAuditTime(it, actions) {
    var audit = Array.isArray(it.audit) ? it.audit : [];
    for (var i = 0; i < audit.length; i++) {
      var row = audit[i] || {};
      if (actions.indexOf(String(row.action || "")) < 0) continue;
      var t = parseTime(row.at || row.ts || row.time);
      if (t != null) return t;
    }
    return null;
  }

  function itemCreatedTime(it) {
    return parseTime(it.created_at) || firstAuditTime(it, ["new", "harvest", "review-bug-capture"]);
  }

  function itemDoneTime(it) {
    return parseTime(it.done_at) || parseTime(it.closed_at) || parseTime(it.reviewed_at) ||
      parseTime(it.completed_at) || firstAuditTime(it, ["done", "close", "closed", "review", "accept"]);
  }

  function itemReopenTime(it, finished) {
    var reopened = firstAuditTime(it, ["reopen", "reopened", "open", "opened", "undo-done", "undone", "mark-open"]);
    if (reopened != null && (finished == null || reopened >= finished)) return reopened;
    if (finished != null && it.done === false) return finished + 1;
    return null;
  }

  function fmtDate(ms) {
    try {
      return new Date(ms).toISOString().slice(0, 10);
    } catch (e) {
      return "";
    }
  }

  function burndownData(rawItems) {
    var raw = rawItems || store.raw || [];
    var derived = deriveItems(raw);
    var done = 0;
    var open = 0;
    var inFlight = 0;
    var events = [];
    var timestamped = 0;

    derived.items.forEach(function (it) {
      var isDone = it._section === "done-reviewed";
      if (isDone) done += 1;
      else {
        open += 1;
        if (it._section === "working") inFlight += 1;
      }

      var created = itemCreatedTime(it);
      if (created == null) return;
      timestamped += 1;
      events.push({ t: created, delta: 1 });
      var finished = itemDoneTime(it);
      if (finished != null && finished >= created) {
        if (isDone) {
          events.push({ t: finished, delta: -1 });
        } else {
          events.push({ t: finished, delta: -1 });
          events.push({ t: itemReopenTime(it, finished), delta: 1 });
        }
      }
    });

    events.sort(function (a, b) {
      return a.t - b.t || b.delta - a.delta;
    });

    var points = [];
    var running = 0;
    for (var i = 0; i < events.length;) {
      var t = events[i].t;
      var delta = 0;
      while (i < events.length && events[i].t === t) {
        delta += events[i].delta;
        i += 1;
      }
      running += delta;
      points.push({ t: t, open: Math.max(0, running) });
    }

    var burnRate = null;
    if (points.length >= 2) {
      var first = points[0];
      var last = points[points.length - 1];
      var days = Math.max((last.t - first.t) / 86400000, 1);
      burnRate = (first.open - last.open) / days;
    }

    return {
      open: open,
      done: done,
      total: derived.items.length,
      points: points,
      burnRate: burnRate,
      timestamped: timestamped,
      inFlight: inFlight,
      projection: inFlight > 0 ? { open: Math.max(0, open - inFlight), inFlight: inFlight } : null
    };
  }

  function burndownCoverageNote(data) {
    if (!data || data.timestamped >= data.total) return "";
    var missing = data.total - data.timestamped;
    return '<p class="mode coverage-note">trend covers ' + data.timestamped + " of " + data.total +
      " items (" + missing + " lack timestamps)</p>";
  }

  function renderBurndownSvg(points, projection) {
    if (!points || points.length < 2) {
      return '<p class="empty">Trend unavailable until items include created/done timestamps.</p>';
    }
    var w = 720;
    var h = 260;
    var pad = 34;
    var minT = points[0].t;
    var maxT = points[points.length - 1].t;
    var maxOpen = Math.max.apply(null, points.map(function (p) { return p.open; }).concat([
      projection ? projection.open : 1,
      1
    ]));
    var span = Math.max(maxT - minT, 1);
    var plotRight = projection ? w - pad - 96 : w - pad;
    function x(t) {
      return pad + ((t - minT) / span) * (plotRight - pad);
    }
    function y(open) {
      return h - pad - (open / maxOpen) * (h - pad * 2);
    }
    var d = "";
    points.forEach(function (p, i) {
      var px = x(p.t).toFixed(1);
      var py = y(p.open).toFixed(1);
      if (i === 0) d += "M " + px + " " + py;
      else {
        var prev = points[i - 1];
        d += " H " + px + " V " + py;
      }
    });
    var projectionHtml = "";
    if (projection) {
      var last = points[points.length - 1];
      var px = w - pad;
      var py = y(projection.open);
      var ly = Math.max(pad + 12, py - 10);
      projectionHtml =
        '<path class="projection" d="M ' + x(last.t).toFixed(1) + " " + y(last.open).toFixed(1) +
        " H " + px.toFixed(1) + " V " + py.toFixed(1) + '"></path>' +
        '<circle class="projection-point" cx="' + px.toFixed(1) + '" cy="' + py.toFixed(1) + '" r="5"></circle>' +
        '<text class="projection-label" x="' + px.toFixed(1) + '" y="' + ly.toFixed(1) +
        '">if in-flight completes: ' + esc(projection.open) + "</text>";
    }
    return '<svg class="burndown-svg" viewBox="0 0 ' + w + " " + h + '" role="img" aria-label="Open item count trend' +
      (projection ? " with projected in-flight completion point" : "") + '">' +
      '<line class="axis" x1="' + pad + '" y1="' + (h - pad) + '" x2="' + (w - pad) + '" y2="' + (h - pad) + '"></line>' +
      '<line class="axis" x1="' + pad + '" y1="' + pad + '" x2="' + pad + '" y2="' + (h - pad) + '"></line>' +
      '<text class="tick" x="' + pad + '" y="' + (h - 10) + '">' + esc(fmtDate(minT)) + "</text>" +
      '<text class="tick end" x="' + plotRight.toFixed(1) + '" y="' + (h - 10) + '">' + esc(fmtDate(maxT)) + "</text>" +
      '<text class="tick" x="6" y="' + y(maxOpen).toFixed(1) + '">' + maxOpen + "</text>" +
      '<path class="series" d="' + d + '"></path>' +
      points.map(function (p) {
        return '<circle class="point" cx="' + x(p.t).toFixed(1) + '" cy="' + y(p.open).toFixed(1) + '" r="3"></circle>';
      }).join("") +
      projectionHtml +
      "</svg>";
  }

  function renderBurndown(mount, summaryMount, rawItems) {
    var data = burndownData(rawItems);
    var rate = data.burnRate == null ? "n/a" : Math.abs(data.burnRate).toFixed(1) + "/day " +
      (data.burnRate >= 0 ? "down" : "up");
    if (summaryMount) {
      summaryMount.textContent = data.open + " open / " + data.done + " done / burn rate " + rate;
    }
    mount.innerHTML = burndownCoverageNote(data) + renderBurndownSvg(data.points, data.projection);
    return data;
  }

  /* --------------------------------------------------------------- export */

  global.GF = {
    SECTIONS: SECTIONS,
    STATUS_LABELS: STATUS_LABELS,
    esc: esc,
    qs: qs,
    safeHref: safeHref,
    canonicalSection: canonicalSection,
    autolink: autolink,
    linkifyPaths: linkifyPaths,
    resolvePathMention: resolvePathMention,
    renderMention: renderMention,
    renderIdOnly: renderIdOnly,
    gfRoot: gfRoot,
    idLink: idLink,
    normalize: normalize,
    sectionKey: sectionKey,
    deriveItems: deriveItems,
    index: index,
    applyControls: applyControls,
    sortItems: sortItems,
    renderBoard: renderBoard,
    openDecisions: openDecisions,
    decisionBlockIds: decisionBlockIds,
    renderDecisionStrip: renderDecisionStrip,
    renderDecisionList: renderDecisionList,
    attach: attach,
    counts: counts,
    statusBadge: statusBadge,
    kindBadge: kindBadge,
    blockerBits: blockerBits,
    parseTime: parseTime,
    fmtClock: fmtClock,
    fmtDayLabel: fmtDayLabel,
    formatDurationMs: formatDurationMs,
    formatDurationSeconds: formatDurationSeconds,
    metaFreshness: metaFreshness,
    statusFreshness: statusFreshness,
    statusSnapshot: statusSnapshot,
    burndownData: burndownData,
    burndownCoverageNote: burndownCoverageNote,
    renderBurndownSvg: renderBurndownSvg,
    renderBurndown: renderBurndown,
    store: store,
    // direct loader — resolves the indexed store from tasks-data.js
    load: function () {
      var res = snapshot();
      store.mode = res.mode;
      store.sig = res.sig;
      return Promise.resolve(index(res.items));
    }
  };
})(typeof window !== "undefined" ? window : this);
