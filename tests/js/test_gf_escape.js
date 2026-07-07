#!/usr/bin/env node
"use strict";

const fs = require("fs");
const os = require("os");
const path = require("path");
const childProcess = require("child_process");
const vm = require("vm");

const ROOT = path.resolve(__dirname, "..", "..");
const GF_JS = path.join(ROOT, "templates", "state-skeleton", "gf.js");
const TICKET_HTML = path.join(ROOT, "templates", "state-skeleton", "ticket.html");
const TICKETS_HTML = path.join(ROOT, "templates", "state-skeleton", "tickets.html");
const BURNDOWN_HTML = path.join(ROOT, "templates", "state-skeleton", "burndown.html");
const INDEX_HTML = path.join(ROOT, "templates", "state-skeleton", "index.html");
const CURRENT_HTML = path.join(ROOT, "templates", "state-skeleton", "current-activity.html");
const STATE_TEMPLATES_DIR = path.join(ROOT, "templates", "state-skeleton");
const CHECKER = path.join(ROOT, "scripts", "check_tasks_mirror.js");

function assert(name, condition) {
  if (!condition) {
    throw new Error(name);
  }
}

function loadGF() {
  const win = {
    location: { href: "file:///repo/dashboard/tickets.html", pathname: "/repo/dashboard/tickets.html", search: "" },
    GF_ITEMS: [],
    GF_PATH_PREFIXES: ["docs-private/"]
  };
  const context = vm.createContext({ window: win, URL, URLSearchParams });
  vm.runInContext(fs.readFileSync(GF_JS, "utf8"), context, { filename: GF_JS, timeout: 5000 });
  return context.window.GF;
}

function renderTicket(item) {
  const elements = {
    detail: { innerHTML: "" },
    crumbId: { textContent: "" }
  };
  const doc = {
    visibilityState: "visible",
    title: "",
    getElementById(id) {
      if (!elements[id]) elements[id] = { innerHTML: "", textContent: "" };
      return elements[id];
    },
    addEventListener() {},
    removeEventListener() {}
  };
  const win = {
    location: {
      href: "file:///repo/dashboard/ticket.html?id=t-ctrl",
      pathname: "/repo/dashboard/ticket.html",
      search: "?id=t-ctrl",
      reload() {}
    },
    document: doc,
    GF_ITEMS: [item],
    GF_PATH_PREFIXES: ["docs-private/"],
    addEventListener() {},
    removeEventListener() {}
  };
  const context = vm.createContext({ window: win, document: doc, URL, URLSearchParams });
  vm.runInContext(fs.readFileSync(GF_JS, "utf8"), context, { filename: GF_JS, timeout: 5000 });
  context.GF = context.window.GF;
  const scripts = Array.from(fs.readFileSync(TICKET_HTML, "utf8").matchAll(/<script>([\s\S]*?)<\/script>/g));
  vm.runInContext(scripts[scripts.length - 1][1], context, { filename: TICKET_HTML + "#inline", timeout: 5000 });
  return elements.detail.innerHTML;
}

function renderBurndownFixture(items) {
  const elements = {
    burndownChart: { innerHTML: "" },
    burndownSummary: { textContent: "" },
    foot: { textContent: "" },
    mode: { classList: { remove() {} } },
    modeText: { textContent: "" }
  };
  const doc = {
    visibilityState: "visible",
    getElementById(id) {
      if (!elements[id]) elements[id] = { innerHTML: "", textContent: "", classList: { remove() {} } };
      return elements[id];
    },
    addEventListener() {},
    removeEventListener() {}
  };
  const win = {
    location: {
      href: "file:///repo/dashboard/burndown.html",
      pathname: "/repo/dashboard/burndown.html",
      search: "",
      reload() {}
    },
    document: doc,
    GF_ITEMS: items,
    GF_PATH_PREFIXES: ["docs-private/"],
    addEventListener() {},
    removeEventListener() {}
  };
  const context = vm.createContext({ window: win, document: doc, URL, URLSearchParams, Number, Date });
  vm.runInContext(fs.readFileSync(GF_JS, "utf8"), context, { filename: GF_JS, timeout: 5000 });
  context.GF = context.window.GF;
  const scripts = Array.from(fs.readFileSync(BURNDOWN_HTML, "utf8").matchAll(/<script>([\s\S]*?)<\/script>/g));
  vm.runInContext(scripts[scripts.length - 1][1], context, { filename: BURNDOWN_HTML + "#inline", timeout: 5000 });
  return elements;
}

function makeElement(extra) {
  const attrs = {};
  return Object.assign({
    innerHTML: "",
    textContent: "",
    value: "",
    listeners: {},
    classList: { remove() {}, add() {}, toggle() {} },
    addEventListener(type, fn) { this.listeners[type] = fn; },
    removeEventListener() {},
    setAttribute(name, value) { attrs[name] = String(value); },
    getAttribute(name) { return attrs[name]; },
    hasAttribute(name) { return Object.prototype.hasOwnProperty.call(attrs, name); }
  }, extra || {});
}

function renderTicketsFixture(items, search, opts) {
  opts = opts || {};
  const kindButtons = ["all", "task", "bug", "decision"].map((kind) => {
    const button = makeElement({
      closest() { return button; }
    });
    button.setAttribute("data-kind", kind);
    return button;
  });
  const elements = {
    board: makeElement(),
    boardStatus: makeElement(),
    q: makeElement(),
    statusSel: makeElement(),
    sortSel: makeElement(),
    kindSeg: makeElement({
      querySelectorAll(selector) {
        return selector === "button" || selector === "button[data-kind]" ? kindButtons : [];
      }
    }),
    refresh: makeElement(),
    mode: makeElement(),
    modeText: makeElement()
  };
  const doc = {
    visibilityState: "visible",
    getElementById(id) {
      if (!elements[id]) elements[id] = makeElement();
      return elements[id];
    },
    addEventListener() {},
    removeEventListener() {}
  };
  const win = {
    location: {
      href: "file:///repo/dashboard/tickets.html" + (search || ""),
      pathname: "/repo/dashboard/tickets.html",
      search: search || "",
      reload() {}
    },
    history: {
      replaceState(_state, _title, url) {
        const next = new URL(url, win.location.href);
        win.location.href = next.href;
        win.location.pathname = next.pathname;
        win.location.search = next.search;
      }
    },
    document: doc,
    GF_ITEMS: items,
    GF_META: opts.meta || undefined,
    GF_STATUS: opts.status || undefined,
    GF_PATH_PREFIXES: ["docs-private/"],
    addEventListener() {},
    removeEventListener() {}
  };
  const context = vm.createContext({
    window: win,
    document: doc,
    URL,
    URLSearchParams,
    setTimeout(fn) { fn(); return 1; },
    clearTimeout() {}
  });
  vm.runInContext(fs.readFileSync(GF_JS, "utf8"), context, { filename: GF_JS, timeout: 5000 });
  context.GF = context.window.GF;
  const scripts = Array.from(fs.readFileSync(TICKETS_HTML, "utf8").matchAll(/<script>([\s\S]*?)<\/script>/g));
  vm.runInContext(scripts[scripts.length - 1][1], context, { filename: TICKETS_HTML + "#inline", timeout: 5000 });
  elements.kindButtons = kindButtons;
  elements.window = win;
  return elements;
}

function renderCurrentActivityFixture(items, opts) {
  opts = opts || {};
  const elements = {
    working: makeElement(),
    review: makeElement(),
    done: makeElement(),
    cWorking: makeElement(),
    cReview: makeElement(),
    cDone: makeElement(),
    activityStatus: makeElement(),
    idleBanner: makeElement({ hidden: true }),
    mode: makeElement(),
    modeText: makeElement(),
    hint: makeElement(),
    freshness: makeElement(),
    liveWorkersLane: makeElement({ hidden: true }),
    liveWorkers: makeElement(),
    cLiveWorkers: makeElement(),
    liveFreshness: makeElement()
  };
  const doc = {
    visibilityState: opts.visibilityState || "visible",
    body: makeElement(),
    getElementById(id) {
      if (!elements[id]) elements[id] = makeElement();
      return elements[id];
    },
    addEventListener() {},
    removeEventListener() {}
  };
  let reloads = 0;
  let intervalMs = null;
  const win = {
    location: {
      href: "file:///repo/dashboard/current-activity.html",
      pathname: "/repo/dashboard/current-activity.html",
      search: "",
      reload() { reloads += 1; }
    },
    document: doc,
    GF_ITEMS: items,
    GF_META: opts.meta || undefined,
    GF_STATUS: opts.status || undefined,
    GF_PATH_PREFIXES: ["docs-private/"],
    addEventListener() {},
    removeEventListener() {},
    setInterval(_fn, ms) { intervalMs = ms; return 7; },
    clearInterval() {}
  };
  const context = vm.createContext({ window: win, document: doc, URL, URLSearchParams, Date, Number });
  vm.runInContext(fs.readFileSync(GF_JS, "utf8"), context, { filename: GF_JS, timeout: 5000 });
  context.GF = context.window.GF;
  const scripts = Array.from(fs.readFileSync(CURRENT_HTML, "utf8").matchAll(/<script>([\s\S]*?)<\/script>/g));
  vm.runInContext(scripts[scripts.length - 1][1], context, { filename: CURRENT_HTML + "#inline", timeout: 5000 });
  elements.window = win;
  elements.reloads = () => reloads;
  elements.intervalMs = () => intervalMs;
  return elements;
}

function renderedIds(html) {
  return Array.from(html.matchAll(/data-id="([^"]+)"/g)).map((m) => m[1]);
}

function numericAttrs(html, attr) {
  return Array.from(html.matchAll(new RegExp(attr + '="(\\d+)"', "g"))).map((m) => Number(m[1]));
}

function dispatchStored(el, type, event) {
  assert(type + " listener is wired", el.listeners && typeof el.listeners[type] === "function");
  el.listeners[type](Object.assign({ target: el }, event || {}));
}

function rowSlice(html, id) {
  const start = html.indexOf('data-id="' + id + '"');
  assert("row exists for " + id, start >= 0);
  const end = html.indexOf("</li>", start);
  assert("row closes for " + id, end >= start);
  return html.slice(start, end);
}

function assertCheckerRejectsMissingDerivedStatus() {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "gf-mirror-"));
  try {
    const item = {
      schema_version: 1,
      id: "t-missing-derived",
      kind: "task",
      title: "Delegated",
      blocked_by: [],
      links: [],
      done: false,
      dispatches: [{ dispatch_id: "d1", state: "working", ts: "2026-06-01T00:00:00+00:00" }]
    };
    fs.writeFileSync(path.join(dir, "tasks.jsonl"), JSON.stringify(item) + "\n");
    fs.writeFileSync(path.join(dir, "tasks-data.js"), "window.GF_ITEMS = " + JSON.stringify([item], null, 2) + ";\n");
    const result = childProcess.spawnSync(process.execPath, [CHECKER, dir], { encoding: "utf8" });
    const output = String(result.stdout || "") + String(result.stderr || "");
    assert("checker rejects mirror item missing derived_status", result.status !== 0);
    assert("checker reports missing derived_status", output.includes("derived_status"));
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
}

const GF = loadGF();
assertCheckerRejectsMissingDerivedStatus();
const payload = "</script><img src=x onerror=alert(1)> t-001 docs-private/proof.md";
const linked = GF.autolink(payload);

assert("autolink escapes script terminator", !/<\/script/i.test(linked));
assert("autolink escapes raw img tag", !/<img/i.test(linked));
assert("autolink preserves escaped text", linked.includes("&lt;/script&gt;&lt;img"));
assert("autolink emits item id link", linked.includes('href="ticket.html?id=t-001"'));
assert("autolink emits allowlisted path link", linked.includes('href="../docs-private/proof.md"'));
const mentionPath = GF.renderMention("docs-private/proof.md:42");
assert("renderMention strips line suffix from href", mentionPath.includes('href="../docs-private/proof.md"'));
assert("renderMention preserves line suffix in visible text", mentionPath.includes("docs-private/proof.md:42"));
assert("renderMention leaves bare protocol doc inert", GF.renderMention("task-lifecycle.md") === "<code>task-lifecycle.md</code>");
assert("renderMention leaves absolute tmp path inert", GF.renderMention("/tmp/status.json") === "<code>/tmp/status.json</code>");
// rA P1: the allowlist must not be escapable with dot segments, and hostile
// path tokens must stay inert code text — in every sink that builds hrefs.
assert("renderMention rejects path traversal", GF.renderMention("docs-private/../SKILL.md") === "<code>docs-private/../SKILL.md</code>");
assert("renderMention rejects absolute traversal", !GF.renderMention("/repo/docs-private/../SKILL.md").includes("<a "));
assert("renderMention rejects quote/angle path", !GF.renderMention('docs-private/a"><img src=x>.md').includes("<a "));
assert("renderMention rejects control-char path", !GF.renderMention("docs-private/a\n.md").includes("<a "));
assert("renderMention rejects backslash path", !GF.renderMention("docs-private\\evil.md").includes("<a "));
assert("renderMention rejects dot-segment inside path", GF.renderMention("docs-private/./x/../../SKILL.md").indexOf("<code>") === 0);
assert("autolink leaves traversal path unlinked", !GF.autolink("see docs-private/../SKILL.md now").includes("<a "));
const idOnlyHostile = GF.renderIdOnly("docs-private/../SKILL.md");
assert("renderIdOnly never path-links", !idOnlyHostile.includes("<a ") && idOnlyHostile.includes("<code>"));
assert("renderIdOnly links exact ids", GF.renderIdOnly("t-152").includes('href="ticket.html?id=t-152"'));
assert("renderIdOnly escapes hostile text", !/<img/i.test(GF.renderIdOnly('t-1"><img src=x>')));

GF.index([
  {
    id: "t-001",
    kind: "task",
    title: "</script><img src=x onerror=alert(1)>",
    blocked_by: [],
    links: [],
    done: false
  }
]);
const mount = { innerHTML: "" };
GF.renderBoard(mount, {});

assert("renderBoard escapes script terminator", !/<\/script/i.test(mount.innerHTML));
assert("renderBoard escapes raw img tag", !/<img/i.test(mount.innerHTML));
assert("renderBoard includes escaped title", mount.innerHTML.includes("&lt;/script&gt;&lt;img"));

GF.index([
  {
    schema_version: 1,
    id: "t-<&>",
    kind: "task",
    title: "Special id",
    blocked_by: ["b-<&>"],
    links: [],
    done: false
  },
  {
    schema_version: 1,
    id: "b-<&>",
    kind: "bug",
    title: "Blocker",
    blocked_by: [],
    links: [],
    done: false
  }
]);
const specialMount = { innerHTML: "" };
GF.renderBoard(specialMount, {});
assert("idLink encodes raw id once", GF.idLink("t-<&>").includes('href="ticket.html?id=t-%3C%26%3E"'));
assert("idLink does not encode escaped entity", !GF.idLink("t-<&>").includes("%26lt%3B"));
assert("rowHTML href encodes raw id once", specialMount.innerHTML.includes('href="ticket.html?id=t-%3C%26%3E"'));
assert("blockerBits href encodes raw id once", specialMount.innerHTML.includes('href="ticket.html?id=b-%3C%26%3E"'));

GF.index([
  {
    id: "t-legacy",
    kind: "task",
    title: "Legacy done",
    blocked_by: [],
    links: [],
    done: true
  },
  {
    schema_version: 1,
    id: "t-review",
    kind: "task",
    title: "Worker done",
    blocked_by: [],
    links: [],
    done: true,
    done_reviewed: false
  },
  {
    schema_version: 1,
    id: "t-accepted",
    kind: "task",
    title: "Accepted",
    blocked_by: [],
    links: [],
    done: true,
    done_reviewed: true
  },
  {
    schema_version: 1,
    id: "t-blocked",
    kind: "task",
    title: "Blocked",
    blocked_by: ["t-review"],
    links: [],
    done: false
  },
  {
    schema_version: 1,
    id: "t-unblocked",
    kind: "task",
    title: "Unblocked",
    blocked_by: ["t-accepted"],
    links: [],
    done: false
  }
]);

assert("legacy done stays done-reviewed", GF.store.byId["t-legacy"]._section === "done-reviewed");
assert("v1 done waits for review", GF.store.byId["t-review"]._section === "awaiting-review");
assert("accepted item is done-reviewed", GF.store.byId["t-accepted"]._section === "done-reviewed");
assert("awaiting review blocker still blocks", GF.store.byId["t-blocked"]._section === "waiting");
assert("done-reviewed blocker resolves", GF.store.byId["t-unblocked"]._section === "pending");

GF.index([
  {
    schema_version: 1,
    id: "t-derived",
    kind: "task",
    title: "Worker finished",
    blocked_by: [],
    links: [],
    done: false,
    derived_status: "awaiting-review",
    dispatches: [{ dispatch_id: "d1", state: "working" }]
  }
]);
const derivedMount = { innerHTML: "" };
GF.renderBoard(derivedMount, {});
assert("derived_status wins over dispatch breadcrumbs", GF.store.byId["t-derived"]._section === "awaiting-review");
assert("derived_status renders awaiting review badge", derivedMount.innerHTML.includes("awaiting review"));
assert("derived_status item is not pending", !derivedMount.innerHTML.includes("To do"));

GF.index([
  {
    schema_version: 1,
    id: "t-deferred",
    kind: "task",
    title: "Deferred task",
    blocked_by: [],
    links: [],
    done: false,
    derived_status: "pending",
    lane: "deferred"
  },
  {
    schema_version: 1,
    id: "t-active",
    kind: "task",
    title: "Active task",
    blocked_by: [],
    links: [],
    done: false,
    derived_status: "pending"
  }
]);
const laneMount = { innerHTML: "" };
GF.renderBoard(laneMount, {});
const laneCounts = GF.counts();
assert("reserved lane carries through normalize", GF.store.byId["t-deferred"].lane === "deferred");
assert("reserved lane routes to backlog", GF.store.byId["t-deferred"]._section === "backlog");
assert("active item remains pending", GF.store.byId["t-active"]._section === "pending");
assert("pending count excludes reserved lane", laneCounts.pending === 1);
assert("backlog count includes reserved lane", laneCounts.backlog === 1);
assert("reserved lane renders parked", laneMount.innerHTML.includes("lane-group-parked"));
assert("reserved lane labels parked", laneMount.innerHTML.includes("parked"));
assert("lane chip renders escaped lane", laneMount.innerHTML.includes("lane deferred"));
const backlogOnlyMount = { innerHTML: "" };
const backlogVisible = GF.renderBoard(backlogOnlyMount, { status: "backlog" });
assert("backlog filter returns reserved lane", backlogVisible === 1 && backlogOnlyMount.innerHTML.includes("t-deferred"));
assert("backlog filter hides active task", !backlogOnlyMount.innerHTML.includes("t-active"));

GF.index([
  {
    schema_version: 1,
    id: "b-10",
    kind: "bug",
    title: "Low bug",
    severity: "low",
    blocked_by: [],
    links: [],
    done: false
  },
  {
    schema_version: 1,
    id: "b-2",
    kind: "bug",
    title: "Critical bug",
    severity: "critical",
    blocked_by: [],
    links: [],
    done: false
  },
  {
    schema_version: 1,
    id: "t-working",
    kind: "task",
    title: "Working task",
    blocked_by: [],
    links: [],
    done: false,
    derived_status: "working"
  },
  {
    schema_version: 1,
    id: "t-done-control",
    kind: "task",
    title: "Done control",
    blocked_by: [],
    links: [],
    done: true,
    done_reviewed: true
  }
]);
const idSortMount = { innerHTML: "" };
GF.renderBoard(idSortMount, { sort: "id" });
const idSortOrder = renderedIds(idSortMount.innerHTML);
assert("id sort control orders ids", idSortOrder.indexOf("b-2") < idSortOrder.indexOf("b-10"));
const severityMount = { innerHTML: "" };
const severityVisible = GF.renderBoard(severityMount, { kind: "bug", sort: "severity" });
const severityOrder = renderedIds(severityMount.innerHTML);
assert("severity sort control filters bugs", severityVisible === 2 && !severityMount.innerHTML.includes("t-working"));
assert("severity sort control orders bugs high first", severityOrder.indexOf("b-2") < severityOrder.indexOf("b-10"));
const workingMount = { innerHTML: "" };
const workingVisible = GF.renderBoard(workingMount, { status: "working" });
assert("status control filters to working section", workingVisible === 1 && workingMount.innerHTML.includes("t-working"));
assert("status control hides non-working rows", !workingMount.innerHTML.includes("b-2") && !workingMount.innerHTML.includes("t-done-control"));

const controlPage = renderTicketsFixture(GF.store.raw, "?status=done-reviewed");
assert("ticket controls read initial status URL", controlPage.statusSel.value === "done-reviewed");
controlPage.statusSel.value = "working";
dispatchStored(controlPage.statusSel, "change");
assert("status listener filters board", controlPage.board.innerHTML.includes("t-working") && !controlPage.board.innerHTML.includes("t-done-control"));
assert("status listener syncs URL", new URLSearchParams(controlPage.window.location.search).get("status") === "working");
controlPage.statusSel.value = "all";
dispatchStored(controlPage.statusSel, "change");
assert("status listener clears URL param", !new URLSearchParams(controlPage.window.location.search).has("status"));
dispatchStored(controlPage.kindSeg, "click", { target: controlPage.kindButtons[2] });
assert("kind listener filters board", controlPage.board.innerHTML.includes("b-2") && !controlPage.board.innerHTML.includes("t-working"));
assert("kind listener syncs URL", new URLSearchParams(controlPage.window.location.search).get("kind") === "bug");
controlPage.sortSel.value = "severity";
dispatchStored(controlPage.sortSel, "change");
const controlSeverityOrder = renderedIds(controlPage.board.innerHTML);
assert("sort listener orders board", controlSeverityOrder.indexOf("b-2") < controlSeverityOrder.indexOf("b-10"));
assert("sort listener syncs URL", new URLSearchParams(controlPage.window.location.search).get("sort") === "severity");
controlPage.q.value = "Critical";
dispatchStored(controlPage.q, "input");
assert("search listener filters board", controlPage.board.innerHTML.includes("b-2") && !controlPage.board.innerHTML.includes("b-10"));
assert("search listener syncs URL", new URLSearchParams(controlPage.window.location.search).get("q") === "Critical");
const clearButton = makeElement();
clearButton.setAttribute("data-gf-clear", "");
dispatchStored(controlPage.board, "click", { target: clearButton });
assert("clear listener resets board", controlPage.board.innerHTML.includes("t-working") && controlPage.board.innerHTML.includes("t-done-control"));
assert("clear listener removes URL filters", controlPage.window.location.search === "");

const hostileLane = "<img src=x onerror=alert(1)>";
GF.index([
  {
    schema_version: 1,
    id: "t-c",
    kind: "task",
    title: "Third",
    lane: hostileLane,
    blocked_by: ["t-b"],
    links: [],
    done: false
  },
  {
    schema_version: 1,
    id: "t-default",
    kind: "task",
    title: "Default lane item",
    blocked_by: [],
    links: [],
    done: false
  },
  {
    schema_version: 1,
    id: "t-b",
    kind: "task",
    title: "Second",
    lane: hostileLane,
    blocked_by: ["t-a"],
    links: [],
    done: false
  },
  {
    schema_version: 1,
    id: "t-a",
    kind: "task",
    title: "First",
    lane: hostileLane,
    blocked_by: [],
    links: [],
    done: false
  },
  {
    schema_version: 1,
    id: "t-e",
    kind: "task",
    title: "Cycle E",
    lane: "ops",
    blocked_by: ["t-d"],
    links: [],
    done: false
  },
  {
    schema_version: 1,
    id: "t-d",
    kind: "task",
    title: "Cycle D",
    lane: "ops",
    blocked_by: ["t-e"],
    links: [],
    done: false
  },
  {
    schema_version: 1,
    id: "t-f",
    kind: "task",
    title: "Ops frontier",
    lane: "ops",
    blocked_by: [],
    links: [],
    done: false
  },
  {
    schema_version: 1,
    id: "t-park",
    kind: "task",
    title: "Deferred",
    lane: "deferred",
    blocked_by: [],
    links: [],
    done: false
  },
  {
    schema_version: 1,
    id: "t-done",
    kind: "task",
    title: "Done",
    blocked_by: [],
    links: [],
    done: true,
    done_reviewed: true
  }
]);
const groupedMount = { innerHTML: "" };
const groupedVisible = GF.renderBoard(groupedMount, {});
const groupedHtml = groupedMount.innerHTML;
const groupedOrder = renderedIds(groupedHtml);
const openHeadline = numericAttrs(groupedHtml, "data-open-count")[0];
const groupCountSum = numericAttrs(groupedHtml, "data-lane-count").reduce((acc, n) => acc + n, 0);
assert("lane fixture visible count includes open plus done", groupedVisible === 9);
assert("open headline excludes done", openHeadline === 8);
assert("lane group counts sum to open headline", groupCountSum === openHeadline);
assert("hostile lane name escaped", groupedHtml.includes("&lt;img src=x onerror=alert(1)&gt;"));
assert("hostile lane name not raw html", !/<img/i.test(groupedHtml));
assert("default group leads", groupedHtml.indexOf("No lane") < groupedHtml.indexOf("&lt;img src=x onerror=alert(1)&gt;"));
assert("reserved parked group follows active lanes", groupedHtml.indexOf(">ops<") < groupedHtml.indexOf(">deferred<"));
assert("reserved parked group precedes done section", groupedHtml.indexOf(">deferred<") < groupedHtml.indexOf(">Done<"));
assert("dependency chain renders frontier first", groupedOrder.indexOf("t-a") < groupedOrder.indexOf("t-b") && groupedOrder.indexOf("t-b") < groupedOrder.indexOf("t-c"));
assert("cycle marker renders", groupedHtml.includes("⟳ cyclic"));
assert("cyclic rows render after ops frontier", groupedOrder.indexOf("t-f") < groupedOrder.indexOf("t-e") && groupedOrder.indexOf("t-f") < groupedOrder.indexOf("t-d"));
assert("cyclic rows keep store order", groupedOrder.indexOf("t-e") < groupedOrder.indexOf("t-d"));

GF.index([
  {
    schema_version: 1,
    id: "t-missing",
    kind: "task",
    title: "Missing blocker",
    blocked_by: ["nonexistent"],
    links: [],
    done: false
  },
  {
    schema_version: 1,
    id: "t-cycle-a",
    kind: "task",
    title: "Cycle A",
    blocked_by: ["t-cycle-b"],
    links: [],
    done: false
  },
  {
    schema_version: 1,
    id: "t-cycle-b",
    kind: "task",
    title: "Cycle B",
    blocked_by: ["t-cycle-a"],
    links: [],
    done: false
  }
]);
const missingCycleMount = { innerHTML: "" };
GF.renderBoard(missingCycleMount, {});
const missingCycleHtml = missingCycleMount.innerHTML;
assert("missing blocker renders missing badge", missingCycleHtml.includes("⚠ blocked by missing nonexistent"));
assert("missing blocker is not cyclic", !rowSlice(missingCycleHtml, "t-missing").includes("⟳ cyclic"));
assert("real cycle still renders cyclic badge", rowSlice(missingCycleHtml, "t-cycle-a").includes("⟳ cyclic") && rowSlice(missingCycleHtml, "t-cycle-b").includes("⟳ cyclic"));

const burndownItems = [
  {
    schema_version: 1,
    id: "t-open",
    kind: "task",
    title: "</script><img src=x onerror=alert(1)> open",
    blocked_by: [],
    links: [],
    done: false,
    created_at: "2026-01-01T00:00:00Z"
  },
  {
    schema_version: 1,
    id: "t-done",
    kind: "task",
    title: "Done",
    blocked_by: [],
    links: [],
    done: true,
    done_reviewed: true,
    created_at: "2026-01-01T00:00:00Z",
    done_at: "2026-01-03T00:00:00Z"
  },
  {
    schema_version: 1,
    id: "q-open",
    kind: "decision",
    title: "Choose",
    blocked_by: [],
    links: [],
    done: false,
    audit: [{ action: "new", at: "2026-01-02T00:00:00Z" }]
  },
  {
    schema_version: 1,
    id: "t-legacy-done",
    kind: "task",
    title: "Legacy done",
    blocked_by: [],
    links: [],
    done: true,
    done_reviewed: true,
    created_at: "2026-01-02T00:00:00Z",
    closed_at: "2026-01-04T00:00:00Z"
  },
  {
    schema_version: 1,
    id: "t-waiting",
    kind: "task",
    title: "Waiting",
    blocked_by: ["q-open"],
    links: [],
    done: false,
    created_at: "2026-01-05T00:00:00Z"
  }
];
const burndown = GF.burndownData(burndownItems);
assert("burndown counts open items", burndown.open === 3);
assert("burndown counts done items", burndown.done === 2);
assert("burndown counts timestamped item coverage", burndown.timestamped === 5);
assert("burndown reconstructs timestamp trend", burndown.points.map((p) => p.open).join(",") === "2,4,3,2,3");
const burndownMount = { innerHTML: "" };
const burndownSummary = { textContent: "" };
GF.renderBurndown(burndownMount, burndownSummary, burndownItems);
assert("burndown headline renders counts", burndownSummary.textContent.includes("3 open / 2 done"));
assert("burndown renders svg trend", burndownMount.innerHTML.includes("<svg"));
assert("burndown omits full-coverage warning", !burndownMount.innerHTML.includes("trend covers"));
assert("burndown omits projection when no in-flight", !burndownMount.innerHTML.includes("if in-flight completes"));
assert("burndown render excludes hostile title text", !burndownMount.innerHTML.includes("<img"));
const burndownPage = renderBurndownFixture(burndownItems);
assert("burndown page renders from fixture", burndownPage.burndownSummary.textContent.includes("3 open / 2 done"));
assert("burndown page remains XSS-safe", !/<\/script|<img/i.test(burndownPage.burndownChart.innerHTML));

const projectedBurndownItems = [
  {
    schema_version: 1,
    id: "t-open-projected",
    kind: "task",
    title: "Open projected",
    blocked_by: [],
    links: [],
    done: false,
    created_at: "2026-04-01T00:00:00Z"
  },
  {
    schema_version: 1,
    id: "t-working-projected",
    kind: "task",
    title: "</script><img src=x onerror=alert(1)> working",
    blocked_by: [],
    links: [],
    done: false,
    derived_status: "delegated",
    created_at: "2026-04-02T00:00:00Z"
  },
  {
    schema_version: 1,
    id: "t-done-projected",
    kind: "task",
    title: "Done projected",
    blocked_by: [],
    links: [],
    done: true,
    done_reviewed: true,
    created_at: "2026-04-01T00:00:00Z",
    done_at: "2026-04-03T00:00:00Z"
  }
];
const projectedBurndown = GF.burndownData(projectedBurndownItems);
assert("burndown projection counts in-flight", projectedBurndown.inFlight === 1);
assert("burndown projection deducts in-flight from open", projectedBurndown.projection.open === 1);
const projectedMount = { innerHTML: "" };
GF.renderBurndown(projectedMount, { textContent: "" }, projectedBurndownItems);
assert("burndown renders dashed projection segment", projectedMount.innerHTML.includes('class="projection"'));
assert("burndown renders hollow projection point", projectedMount.innerHTML.includes('class="projection-point"'));
assert("burndown labels projection as conditional", projectedMount.innerHTML.includes("if in-flight completes: 1"));
assert("burndown projection remains XSS-safe", !/<\/script|<img/i.test(projectedMount.innerHTML));

const mixedBurndownItems = [
  {
    schema_version: 1,
    id: "t-stamped",
    kind: "task",
    title: "Stamped",
    blocked_by: [],
    links: [],
    done: false,
    created_at: "2026-02-01T00:00:00Z"
  },
  {
    schema_version: 1,
    id: "t-unstamped",
    kind: "task",
    title: "Legacy no stamps",
    blocked_by: [],
    links: [],
    done: false
  }
];
const mixedBurndown = GF.burndownData(mixedBurndownItems);
assert("mixed burndown counts every open item", mixedBurndown.open === 2);
assert("mixed burndown counts every item total", mixedBurndown.total === 2);
assert("mixed burndown reports timestamped subset", mixedBurndown.timestamped === 1);
const mixedBurndownMount = { innerHTML: "" };
GF.renderBurndown(mixedBurndownMount, { textContent: "" }, mixedBurndownItems);
assert(
  "mixed burndown renders timestamp coverage warning",
  mixedBurndownMount.innerHTML.includes("trend covers 1 of 2 items (1 lack timestamps)")
);

const reopenedBurndown = GF.burndownData([
  {
    schema_version: 1,
    id: "t-reopened",
    kind: "task",
    title: "Reopened",
    blocked_by: [],
    links: [],
    done: false,
    created_at: "2026-03-01T00:00:00Z",
    done_at: "2026-03-03T00:00:00Z"
  }
]);
assert("reopened item remains currently open", reopenedBurndown.open === 1 && reopenedBurndown.done === 0);
assert("reopened burndown derives open-closed-reopen trend", reopenedBurndown.points.map((p) => p.open).join(",") === "1,0,1");

GF.index([
  {
    schema_version: 1,
    id: "q-001",
    kind: "decision",
    title: "</script><img src=x onerror=alert(1)> choose path",
    blocked_by: [],
    links: [],
    done: false
  },
  {
    schema_version: 1,
    id: "t-001",
    kind: "task",
    title: "Blocked task",
    blocked_by: ["q-001"],
    links: [],
    done: false
  }
]);
const decisions = GF.renderDecisionList();
assert("renderDecisionList escapes script terminator", !/<\/script/i.test(decisions));
assert("renderDecisionList escapes raw img tag", !/<img/i.test(decisions));
assert("renderDecisionList includes escaped hostile title", decisions.includes("&lt;/script&gt;&lt;img"));
assert("renderDecisionList links blocked task", decisions.includes('href="ticket.html?id=t-001"'));

const harvestedTicket = renderTicket({
  schema_version: 1,
  id: "t-ctrl",
  kind: "task",
  title: "Harvested",
  blocked_by: [],
  links: [],
  done: true,
  done_reviewed: true,
  source: "harvest",
  closed_at: "2026-07-02T00:00:00+00:00"
});
assert("ticket labels non-bug source as source", harvestedTicket.includes("<dt>source</dt><dd>harvest</dd>"));
assert("ticket completion uses completion timestamp", harvestedTicket.includes("<dt>completed</dt><dd>2026-07-02T00:00:00+00:00</dd>"));
assert("ticket does not label source as completion", !harvestedTicket.includes("<dt>completed</dt><dd>harvest</dd>"));

const timelineTicket = renderTicket({
  schema_version: 1,
  id: "t-ctrl",
  kind: "task",
  title: "Timeline docs-private/title.md",
  blocked_by: [],
  links: ["docs-private/BACKLOG.md:6", "task-lifecycle.md", "/tmp/status.json", "t-missing"],
  prompt_path: "docs-private/prompts/wA.md:12",
  created_at: "2026-07-07T10:00:00Z",
  done_at: "2026-07-07T12:30:00Z",
  done_reviewed_at: "2026-07-07T12:33:00Z",
  audit: [
    { action: "new", at: "2026-07-07T10:00:00Z", actor: "store" },
    { action: "append", at: "2026-07-07T10:30:00Z", actor: "worker", dispatch_id: "d-exec", note: "<img src=x> t-001 docs-private/note.md" },
    { action: "review", at: "2026-07-07T12:32:00Z", actor: "reviewer", review_dispatch_id: "d-review", verdict: "GO" }
  ],
  dispatches: [
    { dispatch_id: "d-review", role: "reviewer", verdict: "GO", ts: "2026-07-07T12:31:00Z", findings_ref: "docs-private/reviews/review.md:9" },
    { dispatch_id: "d-exec", agent: "codex", state: "worker-finished", started_at: "2026-07-07T11:00:00Z", ended_at: "2026-07-07T12:20:00Z", status_path: "/tmp/status.json", marker: { kind: "COMPLETE", text: "done t-001" } }
  ],
  done: true,
  done_reviewed: true
});
assert("ticket linkList line suffix href resolves from dashboard", timelineTicket.includes('href="../docs-private/BACKLOG.md"'));
assert("ticket linkList keeps line suffix visible", timelineTicket.includes("docs-private/BACKLOG.md:6"));
assert("ticket bare protocol doc is inert code", timelineTicket.includes("<code>task-lifecycle.md</code>"));
assert("ticket absolute path is inert code", timelineTicket.includes("<code>/tmp/status.json</code>"));
assert("ticket prompt_path uses mention renderer", timelineTicket.includes('href="../docs-private/prompts/wA.md"'));
assert("ticket review dispatch renders role", timelineTicket.includes("<strong>role:</strong> reviewer"));
assert("ticket review dispatch renders verdict", timelineTicket.includes("<strong>verdict:</strong> GO"));
assert("ticket review findings link strips suffix", timelineTicket.includes('href="../docs-private/reviews/review.md"'));
assert("ticket executor dispatch renders marker text safely", timelineTicket.includes("COMPLETE: done") && timelineTicket.includes('href="ticket.html?id=t-001"'));
assert("ticket dispatch history omits placeholder dash", !timelineTicket.includes("</strong> -</span>"));
assert("ticket timeline renders section", timelineTicket.includes('<p class="section-title">Timeline</p>'));
assert("ticket timeline escapes audit note", timelineTicket.includes("&lt;img src=x&gt;"));
assert("ticket timeline renders logged to dispatch chip", timelineTicket.includes("logged→dispatch 1h"));
assert("ticket timeline renders done to reviewed chip", timelineTicket.includes("done→reviewed 3m"));

[
  ["newline", "java\nscript:alert(1)"],
  ["tab", "java\tscript:alert(1)"],
  ["carriage return", "java\rscript:alert(1)"]
].forEach(function ([label, href]) {
  const html = renderTicket({
    schema_version: 1,
    id: "t-ctrl",
    kind: "task",
    title: "Control char href",
    blocked_by: [],
    links: [href],
    prompt_path: href,
    dispatches: [{ dispatch_id: "d1", state: "worker-finished", log: href }],
    done: false
  });
  assert(label + " link sink renders inert code", html.includes("<code>java"));
  assert(label + " prompt_path sink renders inert code", !html.includes('<p><a href="#">'));
  assert(label + " dispatch log sink renders inert code", html.includes('<span class="dispatch-log"><code>java'));
  assert(label + " no dead href placeholder", !html.includes('href="#"') && !html.includes("href='#'"));
  assert(label + " no normalized javascript href", !/href=["']java[\n\r\t]*script:/i.test(html));
});

const ticketsBody = fs.readFileSync(path.join(STATE_TEMPLATES_DIR, "tickets.html"), "utf8");
assert("tickets.html exposes backlog status filter", ticketsBody.includes('<option value="backlog">backlog</option>'));
assert("tickets.html exposes frontier sort truth", ticketsBody.includes('<option value="frontier">frontier</option>'));
const indexBody = fs.readFileSync(INDEX_HTML, "utf8");
assert("index done card links to done status filter", indexBody.includes('href:"tickets.html?status=done-reviewed"'));
const doneFilterPage = renderTicketsFixture([
  {
    schema_version: 1,
    id: "t-open-link",
    kind: "task",
    title: "Open link",
    blocked_by: [],
    links: [],
    done: false
  },
  {
    schema_version: 1,
    id: "t-done-link",
    kind: "task",
    title: "Done link",
    blocked_by: [],
    links: [],
    done: true,
    done_reviewed: true
  }
], "?status=done-reviewed");
assert("tickets page reads done status from URL", doneFilterPage.statusSel.value === "done-reviewed");
assert("done status URL shows done row", doneFilterPage.board.innerHTML.includes("t-done-link"));
assert("done status URL hides open row", !doneFilterPage.board.innerHTML.includes("t-open-link"));

const freshBoard = renderTicketsFixture([
  {
    schema_version: 1,
    id: "t-fresh",
    kind: "task",
    title: "Fresh",
    blocked_by: [],
    links: [],
    done: false
  }
], "", { meta: { schema: 1, generated_at: new Date().toISOString() } });
assert("tickets page renders GF_META freshness", freshBoard.freshness.textContent.includes("data as of"));

const liveActivity = renderCurrentActivityFixture([
  {
    schema_version: 1,
    id: "t-123",
    kind: "task",
    title: "Live target",
    blocked_by: [],
    links: [],
    done: false,
    status: "working"
  }
], {
  meta: { schema: 1, generated_at: new Date().toISOString() },
  status: {
    schema: 1,
    generated_at: new Date().toISOString(),
    project_root: "/repo",
    dispatches: [
      { dispatch_id: "d-stalled", agent: "codex<script>", state: "running", classification: "stalled", task_ids: ["t-123"], age_s: 120, idle_s: 61, tail_last_line: "<img src=x onerror=alert(1)> tail" },
      { dispatch_id: "d-failed", agent: "codex", state: "worker_failed", classification: "worker_failed", task_ids: ["docs-private/../SKILL.md"], age_s: 3600, idle_s: 30, tail_last_line: "failed" }
    ],
    counts: { running: 1, worker_finished: 1, worker_failed: 1, worker_dead: 0, stalled: 1 }
  }
});
assert("current activity wires 15s visible refresh", liveActivity.intervalMs() === 15000);
assert("current activity renders live worker count", liveActivity.cLiveWorkers.textContent === "2");
assert("current activity links live task ids", liveActivity.liveWorkers.innerHTML.includes('href="ticket.html?id=t-123"'));
assert("current activity never path-links task ids", !liveActivity.liveWorkers.innerHTML.includes('href="../docs-private'));
assert("current activity renders path-shaped task id inert", liveActivity.liveWorkers.innerHTML.includes("<code>docs-private/../SKILL.md</code>"));
assert("current activity escapes live worker agent", liveActivity.liveWorkers.innerHTML.includes("codex&lt;script&gt;"));
assert("current activity escapes live tail", liveActivity.liveWorkers.innerHTML.includes("&lt;img src=x onerror=alert(1)&gt; tail"));
assert("current activity marks stalled worker amber", liveActivity.liveWorkers.innerHTML.includes('class="card stalled"'));
assert("current activity marks failed worker red", liveActivity.liveWorkers.innerHTML.includes('class="card danger"'));
assert("current activity renders live freshness", liveActivity.liveFreshness.textContent.includes("live as of"));

[
  "index.html",
  "current-activity.html",
  "tickets.html",
  "ticket.html",
  "burndown.html",
  "gf.js"
].forEach(function (name) {
  const body = fs.readFileSync(path.join(STATE_TEMPLATES_DIR, name), "utf8");
  assert(name + " does not claim live polling", !/live\s*(?:\u00b7|&middot;|\|)?\s*polling/i.test(body));
  assert(name + " does not claim timed polling", !/(polling\s+every|every\s+\d+s|poll\s+tick)/i.test(body));
});

console.log("OK: gf.js escaping/autolink test pass");
