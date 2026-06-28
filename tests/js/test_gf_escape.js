#!/usr/bin/env node
"use strict";

const fs = require("fs");
const path = require("path");
const vm = require("vm");

const ROOT = path.resolve(__dirname, "..", "..");
const GF_JS = path.join(ROOT, "templates", "state-skeleton", "gf.js");

function assert(name, condition) {
  if (!condition) {
    throw new Error(name);
  }
}

function loadGF() {
  const win = {
    location: { pathname: "/repo/docs-private/tickets.html", search: "" },
    GF_ITEMS: [],
    GF_PATH_PREFIXES: ["docs-private/"]
  };
  const context = vm.createContext({ window: win });
  vm.runInContext(fs.readFileSync(GF_JS, "utf8"), context, { filename: GF_JS, timeout: 5000 });
  return context.window.GF;
}

const GF = loadGF();
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

console.log("OK: gf.js escaping/autolink test pass");
