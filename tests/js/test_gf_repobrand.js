#!/usr/bin/env node
"use strict";

// gf.js repo-branding: every view's <title> and an injected top-of-<main> banner
// carry the repo name (basename of the derived repo root) so sibling-repo
// dashboards are distinguishable. Branding touches only the live DOM — the
// on-disk skeleton's "<title>goal-flight"/"<h1>goal-flight" markers are unchanged
// (doctor + byte-equality checks stay green).

const fs = require("fs");
const path = require("path");
const vm = require("vm");

const ROOT = path.resolve(__dirname, "..", "..");
const GF_JS = path.join(ROOT, "templates", "state-skeleton", "gf.js");
const GF_SRC = fs.readFileSync(GF_JS, "utf8");

function assert(name, cond) {
  if (!cond) {
    console.error("FAIL: " + name);
    process.exit(1);
  }
}

function makeEl(tag) {
  const el = {
    tag, children: [], className: "", _text: "", _id: undefined,
    appendChild(c) { this.children.push(c); return c; },
    insertBefore(c) { this.children.unshift(c); return c; },
    setAttribute() {},
    addEventListener() {},
  };
  Object.defineProperty(el, "textContent", { get() { return this._text; }, set(v) { this._text = v; } });
  Object.defineProperty(el, "firstChild", { get() { return this.children[0] || null; } });
  return el;
}

// Fresh gf.js instance + DOM mock per case (module-level repoBrandingApplied flag
// must reset). Returns {doc, main}. `pathname` drives the repo-name derivation.
function brand(pathname, initialTitle) {
  const byId = {};
  const main = makeEl("main");
  const head = makeEl("head");
  const doc = {
    title: initialTitle,
    head, body: makeEl("body"),
    visibilityState: "visible",
    querySelector(sel) { return sel === "main" ? main : null; },
    getElementById(id) {
      if (id in byId) return byId[id];
      if (id.indexOf("gf-repobar") === 0) return null; // not yet created
      const stub = makeEl("stub"); stub.innerHTML = ""; byId[id] = stub; return stub;
    },
    createElement(tag) {
      const el = makeEl(tag);
      Object.defineProperty(el, "id", { get() { return this._id; }, set(v) { this._id = v; byId[v] = this; } });
      return el;
    },
    addEventListener() {}, removeEventListener() {},
  };
  const win = {
    location: { pathname, href: "file://" + pathname, search: "", reload() {} },
    document: doc, GF_ITEMS: [], GF_PATH_PREFIXES: ["docs-private/"],
    addEventListener() {}, removeEventListener() {},
  };
  const context = vm.createContext({ window: win, document: doc, URL, URLSearchParams, Number, Date });
  vm.runInContext(GF_SRC, context, { filename: GF_JS, timeout: 5000 });
  const GF = context.window.GF;
  // applyRepoBranding runs first in attach(); guard the rest of attach so an
  // incomplete render mock can't mask the branding assertions.
  try { GF.attach({ onRender() {}, onMode() {} }); } catch (e) {}
  try { GF.attach({ onRender() {}, onMode() {} }); } catch (e) {} // idempotency
  return { doc, main };
}

// --- repo name derived from a real dashboard path ---
const r = brand("/Users/x/Repos/battery-tool-v2/dashboard/tickets.html", "goal-flight · board");
assert("title carries repo, repo-first", r.doc.title === "battery-tool-v2 · goal-flight · board");
const bar = r.main.firstChild;
assert("banner injected as first child of main", bar && bar._id === "gf-repobar");
assert("banner text = repo · goal-flight",
  bar.children.map((c) => c.textContent).join(" ") === "battery-tool-v2 · goal-flight");
assert("repo span styled", bar.children[0].className === "gf-repo");
assert("idempotent: single banner", r.main.children.filter((c) => c._id === "gf-repobar").length === 1);
assert("idempotent: title prepended once",
  r.doc.title === "battery-tool-v2 · goal-flight · board");

// --- legacy docs-private path still yields the repo root ---
const r2 = brand("/srv/kiln/docs-private/index.html", "goal-flight");
assert("docs-private path -> repo name", r2.doc.title === "kiln · goal-flight");

// --- unrecognizable path -> no-op (title unchanged, no banner) ---
const r3 = brand("/somewhere/else/page.html", "goal-flight");
assert("no derivable root -> title unchanged", r3.doc.title === "goal-flight");
assert("no derivable root -> no banner", r3.main.firstChild === null);

console.log("OK: gf.js repo-branding test pass");
