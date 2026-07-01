/* gf.js — shared client engine for the goal-flight Tier-2 views.
 *
 * Zero dependencies, no build step, no backend. Loaded by tickets.html,
 * ticket.html, current-activity.html and index.html via a single <script src>.
 *
 * Responsibilities:
 *   - DATA LOADING: read the window.GF_ITEMS snapshot shipped by tasks-data.js.
 *   - REFRESH: manual Refresh button + page reload on visibilitychange/focus
 *     so file:// views pick up a changed tasks-data.js.
 *   - RENDER: sectioned status board, kind/status/search filters, sort, counts.
 *   - AUTOLINK: \b[tbq]-\d+\b ids -> ticket.html?id=...
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
      // Durable dispatch breadcrumb (ADR-011 / task-lifecycle.md "Dispatch
      // provenance"). Each entry: { dispatch_id, agent, log, started_at,
      // ended_at, state, marker, worker_pid? }. Absent on hand-maintained items.
      dispatches: Array.isArray(it.dispatches) ? it.dispatches.filter(Boolean) : [],
      tags: Array.isArray(it.tags) ? it.tags : []
    };
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
    // resolve blockers against the item map
    var unresolved = (it.blocked_by || []).some(function (bid) {
      var b = byId[bid];
      return !b || !b.done; // missing or not-done blocker => still blocked
    });
    if (unresolved && (it.blocked_by || []).length) return "waiting";
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
  // current location: the views sit at <root>/docs-private/<page>, so stripping
  // the trailing '/docs-private/<page>' yields the repo root. Returns "" when it
  // can't be derived.
  function gfRoot() {
    if (typeof global.GF_ROOT === "string" && global.GF_ROOT) {
      // normalize: drop a trailing slash so the under-root prefix test is clean
      return global.GF_ROOT.replace(/\/+$/, "");
    }
    try {
      // pathname like /path/to/repo/docs-private/tickets.html (file://).
      var path = decodeURIComponent(global.location.pathname || "");
      var m = /^(.*)\/docs-private\/[^/]*$/.exec(path);
      return m ? m[1] : "";
    } catch (e) {
      return "";
    }
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

  // Resolve a raw path mention to a repo-root-relative form, or null if it must
  // stay plain text. Handles: repo-root-relative (allowlisted prefix), and
  // absolute-under-GF_ROOT (stripped to relative). Absolute paths NOT under the
  // repo, or relative paths outside the allowlist, return null (plain text).
  function resolvePathMention(raw) {
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
        // from a docs-private view.
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
    return '<a' + c + ' href="ticket.html?id=' + encodeURIComponent(esc(id)) + '">' + esc(id) + "</a>";
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

  function index(rawItems) {
    var items = (rawItems || []).map(normalize);
    var byId = {};
    items.forEach(function (it) {
      byId[it.id] = it;
    });
    // second pass: derive section now that the id map exists
    items.forEach(function (it) {
      it._section = sectionKey(it, byId);
    });
    store.raw = rawItems || [];
    store.items = items;
    store.byId = byId;
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

  // Full alphabetic prefix of an id (e.g. 'bp' for 'bp-001', 'b' for 'b-001',
  // 'ADR' for 'ADR-002'). Used so distinct prefixes never collapse in id-sort.
  function idPrefix(id) {
    var m = /^([A-Za-z]+)/.exec(id || "");
    return m ? m[1].toLowerCase() : "";
  }

  function sortItems(items, sort) {
    var arr = items.slice();
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
      // distinct ids sharing a prefix+number — e.g. b-001 vs bp-001 — order
      // deterministically rather than by input order)
      arr.sort(function (a, b) {
        return idPrefix(a.id).localeCompare(idPrefix(b.id)) ||
          idNum(a.id) - idNum(b.id) ||
          String(a.id).localeCompare(String(b.id));
      });
    }
    return arr;
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
      var b = store.byId[bid];
      var resolved = b && b.done;
      var cls = "blocker" + (resolved ? " blocker-ok" : "");
      var glyph = resolved ? "✓" : "⏸";
      var label = esc(bid) + ", blocker " + (resolved ? "resolved" : "still blocking");
      return '<a class="' + cls + '" href="ticket.html?id=' + encodeURIComponent(esc(bid)) +
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

  // One list row. Title autolinks ids; id is itself a ticket link.
  function rowHTML(it) {
    return (
      '<li class="row row-' + esc(it._section) + '" data-id="' + esc(it.id) + '">' +
      '<a class="id" href="ticket.html?id=' + encodeURIComponent(esc(it.id)) + '">' + esc(it.id) + "</a>" +
      '<span class="body">' +
      '<span class="title">' + autolink(it.title) + "</span>" +
      '<span class="tags">' + kindBadge(it.kind) + laneBadge(it) + sevBit(it) + blockerBits(it) + "</span>" +
      "</span>" +
      "</li>"
    );
  }

  // Render the full sectioned board into `mount`, honouring controls.
  // Returns total visible count.
  function renderBoard(mount, ctrl) {
    var filtered = applyControls(store.items, ctrl || {});
    var sorted = sortItems(filtered, (ctrl && ctrl.sort) || "id");

    // group by section
    var groups = {};
    SECTIONS.forEach(function (s) {
      groups[s.key] = [];
    });
    sorted.forEach(function (it) {
      (groups[it._section] || (groups[it._section] = [])).push(it);
    });

    var html = "";
    SECTIONS.forEach(function (sec) {
      var rows = groups[sec.key] || [];
      if (!rows.length) return;
      // Done newest-first (LIFO). Others keep the chosen sort.
      var ordered = sec.key === "done-reviewed" ? rows.slice().reverse() : rows;
      html +=
        '<section class="sec ' + sec.cls + '" aria-label="' + esc(sec.label) + '">' +
        '<h2>' + esc(sec.label) + ' <span class="count">' + rows.length + ' <span class="visually-hidden">items</span></span></h2>' +
        '<ul class="rows' + (sec.key === "done-reviewed" ? " done-list" : "") + '">' +
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
    gfRoot: gfRoot,
    idLink: idLink,
    normalize: normalize,
    sectionKey: sectionKey,
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
