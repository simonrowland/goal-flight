#!/usr/bin/env node
// check_tasks_mirror.js — verify tasks-data.js is a faithful field-for-field
// mirror of tasks.jsonl.
//
// Why this exists: served viewers read tasks.jsonl; file:// viewers read the
// window.GF_ITEMS array in tasks-data.js. goalflight_task.py `sync` writes the
// generated mirror, and this checker fails loudly on any drift.
//
// Usage:  node scripts/check_tasks_mirror.js [<dir>]
//   <dir> holds tasks.jsonl + tasks-data.js.
//   Defaults to templates/state-skeleton/ (the tracked known-good fixture).
//
// Hermetic: node-only, no network, no localhost. tasks-data.js is loaded inside
// a node `vm` sandbox with a `window` shim — never eval'd into real globals.
//
// Checks:
//   1. tasks.jsonl parses as one JSON object per non-blank line.
//   2. tasks-data.js assigns window.GF_ITEMS (loaded via vm sandbox).
//   3. id-sets are equal across both files.
//   4. every item is deep-equal field-for-field (canonical: recursively sort
//      object keys, JSON.stringify both, compare).
//   5. NO item in EITHER file carries a `status` key (status is render-derived).
//
// On mismatch: prints a clear diff and exits non-zero.

"use strict";

const fs = require("fs");
const path = require("path");
const vm = require("vm");

const DEFAULT_DIR = path.join(__dirname, "..", "templates", "state-skeleton");

function fail(msg) {
  console.error("FAIL: tasks mirror check");
  for (const line of String(msg).split("\n")) {
    console.error("  " + line);
  }
  process.exit(1);
}

function requireRegularFile(file) {
  let st;
  try {
    st = fs.lstatSync(file);
  } catch (err) {
    if (err && err.code === "ENOENT") {
      fail(`missing file: ${file}`);
    }
    fail(`${file}: cannot stat safely before read: ${err.message}`);
  }
  if (st.isSymbolicLink() || !st.isFile()) {
    fail(`${file}: refusing to read non-regular file`);
  }
}

// Recursively sort object keys so JSON.stringify is order-independent.
function canonical(value) {
  if (Array.isArray(value)) {
    return value.map(canonical);
  }
  if (value && typeof value === "object") {
    const out = {};
    for (const key of Object.keys(value).sort()) {
      out[key] = canonical(value[key]);
    }
    return out;
  }
  return value;
}

function canonicalStr(value) {
  return JSON.stringify(canonical(value));
}

function parseJsonl(text, file) {
  const items = [];
  const lines = text.split("\n");
  for (let i = 0; i < lines.length; i++) {
    const raw = lines[i];
    if (raw.trim() === "") continue; // skip blank lines
    let obj;
    try {
      obj = JSON.parse(raw);
    } catch (err) {
      fail(`${file}: line ${i + 1} is not valid JSON: ${err.message}\n  line: ${raw}`);
    }
    if (obj === null || typeof obj !== "object" || Array.isArray(obj)) {
      fail(`${file}: line ${i + 1} is not a JSON object.`);
    }
    items.push(obj);
  }
  return items;
}

// Load window.GF_ITEMS from tasks-data.js inside a vm sandbox with a window shim.
function loadDataJs(text, file) {
  const sandbox = { window: {} };
  // No `module`, no `require`, no real globals exposed — the CommonJS tail
  // guard (typeof module !== "undefined") sees module as undefined and skips.
  const context = vm.createContext(sandbox);
  try {
    vm.runInContext(text, context, { filename: file, timeout: 5000 });
  } catch (err) {
    fail(`${file}: failed to evaluate in vm sandbox: ${err.message}`);
  }
  const items = sandbox.window.GF_ITEMS;
  if (!Array.isArray(items)) {
    fail(`${file}: window.GF_ITEMS is not an array (got ${typeof items}).`);
  }
  return items;
}

function idOf(item, file, index) {
  if (typeof item.id !== "string" || item.id === "") {
    fail(`${file}: item at index ${index} has no string id.`);
  }
  return item.id;
}

function indexById(items, file) {
  const map = new Map();
  items.forEach((item, index) => {
    const id = idOf(item, file, index);
    if (map.has(id)) {
      fail(`${file}: duplicate id ${id}.`);
    }
    map.set(id, item);
  });
  return map;
}

function assertNoStatus(items, file) {
  for (const item of items) {
    if (Object.prototype.hasOwnProperty.call(item, "status")) {
      fail(`${file}: item ${item.id} carries a stray \`status\` key (status is derived at render time, not stored).`);
    }
  }
}

function main() {
  const dir = process.argv[2] ? path.resolve(process.argv[2]) : DEFAULT_DIR;
  const jsonlPath = path.join(dir, "tasks.jsonl");
  const dataJsPath = path.join(dir, "tasks-data.js");

  for (const p of [jsonlPath, dataJsPath]) {
    requireRegularFile(p);
  }

  const jsonlItems = parseJsonl(fs.readFileSync(jsonlPath, "utf8"), "tasks.jsonl");
  const dataItems = loadDataJs(fs.readFileSync(dataJsPath, "utf8"), "tasks-data.js");

  // Check 5 first — a stray status key is a hard fail regardless of mirror state.
  assertNoStatus(jsonlItems, "tasks.jsonl");
  assertNoStatus(dataItems, "tasks-data.js");

  const jsonlById = indexById(jsonlItems, "tasks.jsonl");
  const dataById = indexById(dataItems, "tasks-data.js");

  // Check 3 — id-sets equal.
  const onlyJsonl = [...jsonlById.keys()].filter((id) => !dataById.has(id)).sort();
  const onlyData = [...dataById.keys()].filter((id) => !jsonlById.has(id)).sort();
  if (onlyJsonl.length || onlyData.length) {
    const parts = [];
    if (onlyJsonl.length) parts.push(`only in tasks.jsonl: ${onlyJsonl.join(", ")}`);
    if (onlyData.length) parts.push(`only in tasks-data.js: ${onlyData.join(", ")}`);
    fail("id-sets differ between the two files.\n  " + parts.join("\n  "));
  }

  // Check 4 — every item deep-equal field-for-field, with per-field diff.
  for (const id of [...jsonlById.keys()].sort()) {
    const a = jsonlById.get(id);
    const b = dataById.get(id);
    if (canonicalStr(a) === canonicalStr(b)) continue;

    const keys = new Set([...Object.keys(a), ...Object.keys(b)]);
    const diffs = [];
    for (const key of [...keys].sort()) {
      const sa = canonicalStr(a[key]);
      const sb = canonicalStr(b[key]);
      if (sa !== sb) {
        diffs.push(`field "${key}": tasks.jsonl=${sa === undefined ? "<absent>" : sa} vs tasks-data.js=${sb === undefined ? "<absent>" : sb}`);
      }
    }
    fail(`item ${id} differs between the two files:\n  ` + diffs.join("\n  "));
  }

  console.log(`OK: tasks mirror in sync — ${jsonlItems.length} items, ids match, no status key (${dir})`);
}

main();
