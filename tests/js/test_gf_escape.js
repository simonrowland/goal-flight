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

console.log("OK: gf.js escaping/autolink test pass");
