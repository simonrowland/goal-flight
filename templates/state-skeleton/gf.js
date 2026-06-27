/* gf.js — shared client engine for the goal-flight Tier-2 views.
 *
 * Zero dependencies, no build step, no backend. Loaded by tickets.html,
 * ticket.html, current-activity.html and index.html via a single <script src>.
 *
 * Responsibilities:
 *   - DATA LOADING (dual-mode): served -> fetch('tasks.jsonl', {cache:'no-store'})
 *     and parse JSONL; on any failure (e.g. file://, CORS, 404) fall back to the
 *     window.GF_ITEMS snapshot shipped by tasks-data.js.
 *   - POLLING: served -> cheap HEAD Last-Modified probe ~3s; re-fetch + re-render
 *     ONLY when the resource changed. file:// -> manual Refresh button + re-render
 *     on visibilitychange/focus. No busy-loop, no flicker on a no-op re-render.
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

  // Are we on a real http(s) origin (served) or file:// (snapshot)?
  function isServed() {
    try {
      return /^https?:$/.test(global.location.protocol);
    } catch (e) {
      return false;
    }
  }

  /* --------------------------------------------------------------- model */

  // Canonical status order + display metadata. 'working' / 'worker-finished'
  // are supported even though the seeded snapshot has none yet.
  var SECTIONS = [
    { key: "decision", label: "Decisions needed", cls: "sec-decision" },
    { key: "pending", label: "To do", cls: "sec-pending" },
    { key: "working", label: "In progress", cls: "sec-working" },
    { key: "worker-finished", label: "Awaiting review", cls: "sec-review" },
    { key: "waiting", label: "Waiting", cls: "sec-waiting" },
    { key: "done", label: "Done", cls: "sec-done" }
  ];

  var STATUS_LABELS = {
    decision: "decision",
    pending: "to do",
    working: "in progress",
    "worker-finished": "awaiting review",
    waiting: "waiting",
    done: "done"
  };

  var SEV_RANK = { critical: 4, high: 3, medium: 2, low: 1 };

  // Normalize a raw item: fill defaults, never throw on a missing field.
  function normalize(raw) {
    var it = raw && typeof raw === "object" ? raw : {};
    var id = typeof it.id === "string" && it.id ? it.id : "(no-id)";
    var kind = it.kind === "bug" || it.kind === "decision" ? it.kind : "task";
    // Canonical done predicate: the boolean OR an explicit status of 'done'.
    // Keeps the Done bucket (sectionKey) and blocker-resolution (b.done) from
    // ever disagreeing when status and the boolean diverge.
    var isDone = !!it.done || it.status === "done";
    return {
      id: id,
      kind: kind,
      title: typeof it.title === "string" ? it.title : "(untitled)",
      status: it.status || null, // may be derived below
      blocked_by: Array.isArray(it.blocked_by) ? it.blocked_by.filter(Boolean) : [],
      links: Array.isArray(it.links) ? it.links.filter(Boolean) : [],
      done: isDone,
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

  // Section key for an item. Honours an explicit status; otherwise DERIVES it:
  //   done -> 'done'; decision kind -> 'decision'/'done'; unresolved blocked_by
  //   -> 'waiting'; else -> 'pending'. Unknown blockers count as unresolved.
  function sectionKey(it, byId) {
    if (it.done) return "done";
    var s = it.status;
    // kind:decision flows decision -> done only; it has no dispatch lifecycle,
    // so kind dominates over a (spurious) working/worker-finished status.
    if (it.kind === "decision") return s === "done" ? "done" : "decision";
    if (s === "working" || s === "worker-finished") return s;
    // resolve blockers against the item map
    var unresolved = (it.blocked_by || []).some(function (bid) {
      var b = byId[bid];
      return !b || !b.done; // missing or not-done blocker => still blocked
    });
    if (unresolved && (it.blocked_by || []).length) return "waiting";
    if (s === "waiting") return "waiting";
    if (s === "pending" || s === "done" || !s) return s === "done" ? "done" : "pending";
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
  // the trailing '/docs-private/<page>' yields the repo root. Works on file://
  // and served-from-repo-root alike. Returns "" when it can't be derived.
  function gfRoot() {
    if (typeof global.GF_ROOT === "string" && global.GF_ROOT) {
      // normalize: drop a trailing slash so the under-root prefix test is clean
      return global.GF_ROOT.replace(/\/+$/, "");
    }
    try {
      // pathname like /Users/u/repo/docs-private/tickets.html (file://) or
      // /docs-private/tickets.html (served from repo root).
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
        // on both file:// and served-from-repo-root.
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

  // Parse JSONL (one JSON object per non-blank line). Tolerant: skips bad lines.
  function parseJSONL(text) {
    var out = [];
    var lines = String(text).split(/\r?\n/);
    for (var i = 0; i < lines.length; i++) {
      var ln = lines[i].trim();
      if (!ln) continue;
      try {
        out.push(JSON.parse(ln));
      } catch (e) {
        /* tolerate a malformed line rather than blow up the whole board */
      }
    }
    return out;
  }

  var DATA_URL = "tasks.jsonl";

  // Try served fetch first; on any failure resolve with the snapshot.
  // Resolves { items, mode:'served'|'snapshot', sig } where sig changes on update.
  function fetchData() {
    if (!isServed() || typeof global.fetch !== "function") {
      return Promise.resolve(snapshot());
    }
    return global
      .fetch(DATA_URL, { cache: "no-store" })
      .then(function (r) {
        if (!r.ok) throw new Error("HTTP " + r.status);
        var lm = r.headers.get("Last-Modified") || "";
        return r.text().then(function (txt) {
          var raw = parseJSONL(txt);
          if (!raw.length) throw new Error("empty jsonl");
          return { items: raw, mode: "served", sig: lm || "len:" + txt.length + ":" + hash(txt) };
        });
      })
      .catch(function () {
        return snapshot();
      });
  }

  function snapshot() {
    var arr = Array.isArray(global.GF_ITEMS) ? global.GF_ITEMS : [];
    return { items: arr, mode: "snapshot", sig: "snapshot:" + arr.length };
  }

  // tiny non-crypto string hash for change detection
  function hash(s) {
    var h = 5381;
    for (var i = 0; i < s.length; i++) h = ((h << 5) + h + s.charCodeAt(i)) | 0;
    return h >>> 0;
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
      '<span class="tags">' + kindBadge(it.kind) + sevBit(it) + blockerBits(it) + "</span>" +
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
      var ordered = sec.key === "done" ? rows.slice().reverse() : rows;
      html +=
        '<section class="sec ' + sec.cls + '" aria-label="' + esc(sec.label) + '">' +
        '<h2>' + esc(sec.label) + ' <span class="count">' + rows.length + ' <span class="visually-hidden">items</span></span></h2>' +
        '<ul class="rows' + (sec.key === "done" ? " done-list" : "") + '">' +
        ordered.map(rowHTML).join("") +
        "</ul></section>";
    });

    if (!html) {
      html = '<p class="empty">No items match. <button type="button" class="linkbtn" data-gf-clear>Clear filters</button></p>';
    }
    mount.innerHTML = html;
    return filtered.length;
  }

  /* ----------------------------------------------------- polling driver */

  // Drive load + re-render. Served: HEAD Last-Modified probe ~3s, re-fetch on
  // change. file://: manual Refresh + focus/visibility re-render. onRender gets
  // the store after each (changed) load; onMode is called once with the mode.
  function attach(opts) {
    var onRender = opts.onRender || function () {};
    var onMode = opts.onMode || function () {};
    var interval = opts.interval || 3000;
    var timer = null;
    var lastSig = null;
    var lastProbe = null; // Last-Modified that was actually LOADED (not just seen)
    var destroyed = false;
    var inFlight = false; // reentrancy guard: at most one load in flight

    function loadAndMaybeRender(force) {
      // Skip overlapping loads; a late-resolving older GET must not clobber
      // newer bytes (last-writer-wins) nor leave lastProbe advanced past a
      // failed fetch. The next tick / event retries.
      if (inFlight) return Promise.resolve(null);
      inFlight = true;
      return fetchData()
        .then(function (res) {
          store.mode = res.mode;
          if (force || res.sig !== lastSig) {
            lastSig = res.sig;
            store.sig = res.sig;
            index(res.items);
            onRender(store);
          }
          // Only advance the probe marker once the content has actually loaded,
          // so a failed/raced GET can never permanently latch the view stale.
          if (res.mode === "served" && res.sig) lastProbe = res.sig;
          return res;
        })
        .finally(function () {
          inFlight = false;
        });
    }

    // Cheap served probe: HEAD for Last-Modified; only do a full GET on change.
    function probe() {
      if (destroyed) return;
      if (!isServed() || typeof global.fetch !== "function") return;
      if (inFlight) return; // a load is already running; don't stack
      global
        .fetch(DATA_URL, { method: "HEAD", cache: "no-store" })
        .then(function (r) {
          if (!r.ok) return;
          var lm = r.headers.get("Last-Modified") || "";
          if (lm && lm === lastProbe) return; // unchanged, skip the GET
          // Do NOT advance lastProbe here — loadAndMaybeRender advances it only
          // after a successful GET, so a failed GET re-probes next tick.
          return loadAndMaybeRender(false);
        })
        .catch(function () {
          /* transient; next tick retries */
        });
    }

    function startPolling() {
      if (timer || !isServed()) return;
      timer = global.setInterval(probe, interval);
    }
    function stopPolling() {
      if (timer) {
        global.clearInterval(timer);
        timer = null;
      }
    }

    // file:// (and served) re-render on focus/visibility — cheap, snapshot reload
    function onVisible() {
      if (destroyed) return;
      if (global.document && global.document.visibilityState === "visible") {
        loadAndMaybeRender(false);
        startPolling();
      } else {
        stopPolling();
      }
    }
    // Named focus handler so destroy() can actually detach it.
    function onFocus() {
      if (destroyed) return;
      loadAndMaybeRender(false);
    }

    // initial load determines the mode, then wires the right refresh path.
    // lastProbe is seeded inside loadAndMaybeRender (served + sig) so the first
    // probe tick skips a redundant GET when nothing has changed.
    var ready = loadAndMaybeRender(true).then(function (res) {
      onMode(res.mode);
      if (res.mode === "served") startPolling();
      try {
        global.document.addEventListener("visibilitychange", onVisible);
        global.addEventListener("focus", onFocus);
        // Tear down timer + listeners when the page goes away so a re-attach
        // on the same document can't leak an interval or duplicate listeners.
        global.addEventListener("pagehide", destroy);
      } catch (e) {}
      return res;
    });

    function destroy() {
      destroyed = true;
      stopPolling();
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
        return loadAndMaybeRender(true);
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
    isServed: isServed,
    autolink: autolink,
    linkifyPaths: linkifyPaths,
    resolvePathMention: resolvePathMention,
    gfRoot: gfRoot,
    idLink: idLink,
    normalize: normalize,
    sectionKey: sectionKey,
    index: index,
    fetchData: fetchData,
    parseJSONL: parseJSONL,
    applyControls: applyControls,
    sortItems: sortItems,
    renderBoard: renderBoard,
    attach: attach,
    counts: counts,
    statusBadge: statusBadge,
    kindBadge: kindBadge,
    blockerBits: blockerBits,
    store: store,
    // direct loader (no polling) — resolves the indexed store
    load: function () {
      return fetchData().then(function (res) {
        store.mode = res.mode;
        store.sig = res.sig;
        return index(res.items);
      });
    }
  };
})(typeof window !== "undefined" ? window : this);
