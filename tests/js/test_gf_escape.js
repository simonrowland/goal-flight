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
const CHECKER = path.join(ROOT, "scripts", "check_tasks_mirror.js");

function assert(name, condition) {
  if (!condition) {
    throw new Error(name);
  }
}

function loadGF() {
  const win = {
    location: { href: "file:///repo/docs-private/tickets.html", pathname: "/repo/docs-private/tickets.html", search: "" },
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
      href: "file:///repo/docs-private/ticket.html?id=t-ctrl",
      pathname: "/repo/docs-private/ticket.html",
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
assert("derived_status renders awaiting review section", derivedMount.innerHTML.includes("Awaiting review"));
assert("derived_status item is not pending", !derivedMount.innerHTML.includes("To do"));

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
  assert(label + " link sink rejects href", html.includes("<a href='#'>"));
  assert(label + " prompt_path sink rejects href", html.includes('<p><a href="#">'));
  assert(label + " dispatch log sink rejects href", html.includes('<a class="dispatch-log" href="#">'));
  assert(label + " no normalized javascript href", !/href=["']java[\n\r\t]*script:/i.test(html));
});

console.log("OK: gf.js escaping/autolink test pass");
