#!/usr/bin/env node
"use strict";

const childProcess = require("child_process");
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const ROOT = path.resolve(__dirname, "..", "..");
const CONSOLE_DIR = path.join(ROOT, "templates", "fleet-console");
const HTML = fs.readFileSync(path.join(CONSOLE_DIR, "index.html"), "utf8");
const CSS = fs.readFileSync(path.join(CONSOLE_DIR, "fleet-console.css"), "utf8");
const JS_PATH = path.join(CONSOLE_DIR, "fleet-console.js");
const JS = fs.readFileSync(JS_PATH, "utf8");
const NOW = Date.parse("2030-01-01T00:03:00Z");

function assert(name, condition) {
  if (!condition) {
    console.error("FAIL: " + name);
    process.exit(1);
  }
}

function element(tagName) {
  const listeners = {};
  const node = {
    tagName: String(tagName).toUpperCase(),
    children: [],
    className: "",
    attributes: {},
    _text: "",
    appendChild(child) {
      this.children.push(child);
      child.parentNode = this;
      return child;
    },
    remove() {
      if (!this.parentNode) return;
      this.parentNode.children = this.parentNode.children.filter((child) => child !== this);
      this.parentNode = null;
    },
    setAttribute(name, value) { this.attributes[name] = String(value); },
    getAttribute(name) {
      return Object.prototype.hasOwnProperty.call(this.attributes, name) ? this.attributes[name] : null;
    },
    removeAttribute(name) { delete this.attributes[name]; },
    addEventListener(type, handler) { listeners[type] = handler; },
    dispatch(type) { if (listeners[type]) listeners[type](); },
    click() { if (listeners.click) listeners.click(); },
  };
  Object.defineProperty(node, "textContent", {
    get() { return this._text + this.children.map((child) => child.textContent).join(""); },
    set(value) { this._text = String(value); this.children = []; },
  });
  return node;
}

function descendants(node) {
  return [node].concat(node.children.flatMap(descendants));
}

function makeDom(fleet, attention) {
  const byId = {};
  [
    "plane-status", "machine", "vendors", "attention", "fleet",
    "attention-section", "fleet-section", "theme-toggle", "live-status",
  ].forEach((id) => {
    byId[id] = element(id === "theme-toggle" ? "button" : "div");
  });
  byId["attention-section"].className = "panel attn attention-section";
  byId["fleet-section"].className = "fleet-section";
  const documentElement = element("html");
  const head = element("head");
  const document = {
    readyState: "complete",
    documentElement,
    head,
    createElement: element,
    getElementById(id) { return byId[id] || null; },
    addEventListener() {},
  };
  const storage = {};
  const clock = { now: NOW };
  const window = {
    document,
    GF_FLEET: fleet || null,
    GF_ATTENTION: attention || null,
    localStorage: {
      getItem(key) { return Object.prototype.hasOwnProperty.call(storage, key) ? storage[key] : null; },
      setItem(key, value) { storage[key] = String(value); },
    },
    intervals: [],
    timeouts: [],
    setInterval(handler, delay) {
      this.intervals.push({ handler, delay });
      return this.intervals.length;
    },
    setTimeout(handler, delay) {
      this.timeouts.push({ handler, delay, cleared: false });
      return this.timeouts.length;
    },
    clearTimeout(id) {
      if (this.timeouts[id - 1]) this.timeouts[id - 1].cleared = true;
    },
  };
  const FakeDate = {
    parse(value) { clock.parses = (clock.parses || 0) + 1; return Date.parse(value); },
    now: () => clock.now,
  };
  return { byId, document, documentElement, storage, window, clock, FakeDate };
}

function loadConsole(fleet, attention) {
  const dom = makeDom(fleet, attention);
  const context = vm.createContext({
    window: dom.window,
    document: dom.document,
    Date: dom.FakeDate,
    Number,
    Object,
    Array,
    String,
    Set,
  });
  vm.runInContext(JS, context, { filename: JS_PATH, timeout: 5000 });
  return { ...dom, api: context.window.GFFleetConsole };
}

function workerRow(overrides) {
  return Object.assign({
    dispatch_id: "local-worker",
    node_id: "local",
    agent: "codex",
    engine: "codex",
    shape: "acp",
    transport: "acp",
    os_sandbox: "workspace-write",
    state: "running",
    classification: "expected_live",
    terminal_state: null,
    liveness_state: "running",
    worker_alive: true,
    started_at: "2030-01-01T00:00:00Z",
    ended_at: null,
    display_state: "running",
    is_terminal: false,
    classification_conflict: false,
  }, overrides || {});
}

function fleetPayload(overrides) {
  return Object.assign({
    schema: "goalflight.fleet-console.fleet.v2",
    generation_id: "fleet-generation",
    sample_started_at: "2030-01-01T00:02:09Z",
    sample_finished_at: "2030-01-01T00:02:10Z",
    last_success_at: "2030-01-01T00:02:10Z",
    producer: { name: "goalflight_fleet_console.py", plane: "fleet" },
    last_error: null,
    registry_total: 1433,
    registry_deep_sampled: 12,
    machine: {
      queue_depth: 9,
      operating_cap: 12,
      active_leases: 2,
      local_workers: 1,
      rate_pressure: [{ provider: "openai", scope: "agent", count: 3 }],
      warnings: [],
    },
    vendors: [{
      provider: "codex",
      seat_index: 1,
      remaining: "90% <img src=x>",
      reset_at: 1893459600,
      flags: ["healthy"],
    }],
    remote: { available: true, nodes: [], workers: [] },
    projects: [{
      project_id: "kiln-abc123",
      name: "kiln",
      registered: true,
      last_seen: "2030-01-01T00:02:00Z",
      skill_version: "1.3.0",
      queue: {
        depth: 7,
        lanes: [{ agent: "codex", count: 5 }, { agent: "claude", count: 2 }],
        oldest_created_at: "2030-01-01T00:00:00Z",
      },
      session: {
        available: true,
        active: true,
        queue_state: "active",
        queue_last_touched: "2030-01-01T00:01:00Z",
        active_leases: 1,
      },
      milestone: { available: true, active_cadence: true, commits_since: 4, cadence: 5, due: false },
      workers: [workerRow({ dispatch_id: "local-</script><img src=x>" })],
    }],
    unassigned_workers: [],
  }, overrides || {});
}

function attentionPayload(overrides) {
  return Object.assign({
    schema: "goalflight.fleet-console.attention.v1",
    generation_id: "attention-generation",
    sample_started_at: "2030-01-01T00:02:56Z",
    sample_finished_at: "2030-01-01T00:02:56Z",
    last_success_at: "2030-01-01T00:02:56Z",
    producer: { name: "goalflight_fleet_console.py", plane: "attention" },
    last_error: null,
    age_granularity: "minute",
    items: [{
      dispatch_id: "needs-review",
      seq: 1,
      kind: "user_need",
      action: "Review",
      observed_at: "2030-01-01T00:00:00Z",
      headline: "Need </script><img src=x onerror=alert(1)>",
    }],
  }, overrides || {});
}

let producerProbeCache = null;
function producerProbe() {
  if (producerProbeCache) return producerProbeCache;
  const code = String.raw`
import json, pathlib, sys
sys.path.insert(0, str(pathlib.Path.cwd() / "scripts"))
import goalflight_fleet_console as F
conflict = F._worker_row({
    "dispatch_id": "conflict", "state": "running",
    "classification": "worker_dead", "worker_still_alive": None,
})
sparse = F._worker_row({"dispatch_id": "sparse"})
remote = F._remote_row({
    "available": True,
    "dispatches": [{
        "dispatch_id": "remote-1", "node": "studio-1", "state": "running",
        "quarantine_reason": None, "ssh_reachable": True, "may_release": False,
    }],
    "nodes": [],
})["workers"][0]
terminal_conflict = F._worker_row({
    "dispatch_id": "terminal-conflict", "state": "complete", "classification": "worker_dead",
})
try:
    F._validate_scalar_types({"sample_started_at": 2030})
except F.ProjectionSecurityError:
    numeric_rejected = True
else:
    numeric_rejected = False
print(json.dumps({
    "conflict": conflict, "sparse": sparse, "remote": remote,
    "terminal_conflict": terminal_conflict, "numeric_rejected": numeric_rejected,
    "fleet_schema": F.FLEET_SCHEMA,
}))
`;
  const result = childProcess.spawnSync("python3", ["-c", code], {
    cwd: ROOT,
    encoding: "utf8",
  });
  if (result.status !== 0) throw new Error("producer probe failed: " + result.stderr);
  producerProbeCache = JSON.parse(result.stdout);
  return producerProbeCache;
}

// Timestamp strings and the exact two-cadence boundary fail closed.
{
  const { api } = loadConsole();
  const fresh = fleetPayload({
    sample_started_at: "2030-01-01T00:01:00.001Z",
    sample_finished_at: "2030-01-01T00:01:00.001Z",
    last_success_at: "2030-01-01T00:01:00.001Z",
  });
  const stale = fleetPayload({
    sample_started_at: "2030-01-01T00:01:00Z",
    sample_finished_at: "2030-01-01T00:01:00Z",
    last_success_at: "2030-01-01T00:01:00Z",
  });
  const future = fleetPayload({
    sample_started_at: "2030-01-01T00:04:00Z",
    sample_finished_at: "2030-01-01T00:04:01Z",
    last_success_at: "2030-01-01T00:04:01Z",
  });
  const reversed = fleetPayload({
    sample_started_at: "2030-01-01T00:02:20Z",
    sample_finished_at: "2030-01-01T00:02:10Z",
    last_success_at: "2030-01-01T00:02:10Z",
  });
  assert("timestamp parsing and cadence boundary", [
    api.ageBucket("2030-01-01T00:02:40Z", NOW) === "now",
    api.ageBucket(2030, NOW) === "age unknown",
    api.planeState(fresh, api.schemas.fleet, 60000, NOW).stale === false,
    api.planeState(stale, api.schemas.fleet, 60000, NOW).stale === true,
    api.planeState(future, api.schemas.fleet, 60000, NOW).freshnessIssue === "clock ahead",
    api.planeState(reversed, api.schemas.fleet, 60000, NOW).freshnessIssue === "timestamp order invalid",
  ].every(Boolean));
}

// Missing planes cannot manufacture zero, clear, or empty-provider claims.
{
  const { byId } = loadConsole(null, null);
  const all = [byId.machine, byId.vendors, byId.attention, byId.fleet].map((node) => node.textContent).join("|");
  assert("no data files suppress all semantics", [
    (all.match(/STALE/g) || []).length === 4,
    !all.includes("local running0"),
    !all.includes("0 waiting"),
    !all.includes("No provider budgets reported"),
  ].every(Boolean));
}

// Each plane remains independently useful when its peer is absent.
{
  const { byId } = loadConsole(fleetPayload(), null);
  assert("one present plane does not launder the missing plane", [
    byId.machine.textContent.includes("local running1"),
    byId.fleet.textContent.includes("local-</script><img src=x>"),
    byId.attention.textContent.includes("STALE · attention plane"),
    !byId.attention.textContent.includes("0 waiting"),
  ].every(Boolean));
}

// An unrecognised schema cannot lend credibility to familiar-looking metadata,
// while a producer error names its measured failure instead of its null timestamp.
{
  const wrongSchema = fleetPayload({ schema: "goalflight.fleet-console.fleet.v1" });
  const failedSample = fleetPayload({
    last_success_at: null,
    last_error: "local_status:PermissionError",
  });
  const wrong = loadConsole(wrongSchema, attentionPayload()).byId;
  const failed = loadConsole(failedSample, attentionPayload()).byId;
  assert("schema mismatch and producer error fail closed with honest causes", [
    wrong.fleet.textContent.includes("Reason: schema mismatch"),
    wrong.fleet.textContent.includes("Last observed age unknown"),
    !wrong.fleet.textContent.includes("local-worker"),
    failed.fleet.textContent.includes("Reason: producer error"),
    failed.fleet.textContent.includes("Last error: local_status:PermissionError"),
    !failed.fleet.textContent.includes("local-worker"),
  ].every(Boolean));
}

// Ten-minute-old fleet data is replaced everywhere it would assert fleet facts.
{
  const oldFleet = fleetPayload({
    sample_started_at: "2029-12-31T23:53:00Z",
    sample_finished_at: "2029-12-31T23:53:01Z",
    last_success_at: "2029-12-31T23:53:01Z",
  });
  const { byId } = loadConsole(oldFleet, attentionPayload());
  const invalidated = [byId.machine, byId.vendors, byId.fleet].map((node) => node.textContent).join("|");
  assert("ten-minute-old fleet replaces semantic rows", [
    (invalidated.match(/STALE · fleet plane/g) || []).length === 3,
    invalidated.includes("Last observed"),
    !invalidated.includes("local running1"),
    !invalidated.includes("90%"),
    !invalidated.includes("local-</script>"),
    !invalidated.includes("oldest"),
    byId.attention.textContent.includes("needs-review"),
  ].every(Boolean));
}

// A future clock is distrust, not a reassuring age bucket.
{
  const ahead = fleetPayload({
    sample_started_at: "2030-01-01T00:04:00Z",
    sample_finished_at: "2030-01-01T00:04:00Z",
    last_success_at: "2030-01-01T00:04:00Z",
  });
  const { byId } = loadConsole(ahead, attentionPayload());
  assert("clock-ahead fleet suppresses semantics", [
    byId.fleet.textContent.includes("Reason: clock ahead"),
    byId.fleet.textContent.includes("Last observed clock ahead"),
    !byId.fleet.textContent.includes("local-worker"),
    !byId.machine.textContent.includes("local running"),
  ].every(Boolean));
}

// Zero and clear are valid statements only for current empty payloads.
{
  const emptyFleet = fleetPayload({
    machine: {
      queue_depth: 0, operating_cap: 12, active_leases: 0, local_workers: 0,
      rate_pressure: [], warnings: [],
    },
    vendors: [],
    remote: { available: true, nodes: [], workers: [] },
    projects: [],
    unassigned_workers: [],
  });
  const { byId } = loadConsole(emptyFleet, attentionPayload({ items: [] }));
  assert("empty-but-fresh fleet may state measured zero", [
    byId.machine.textContent.includes("local running0"),
    byId.vendors.textContent.includes("No provider budgets reported"),
    byId.fleet.textContent.includes("No project has active workers"),
    byId.attention.textContent.includes("0 waiting"),
  ].every(Boolean));
}

// Adjacent producer fields are never substituted into missing measurements.
{
  const row = workerRow({
    dispatch_id: "literal-</script><img src=x>",
    node_id: "node-measured",
    agent: null,
    engine: "must-not-be-agent",
    transport: null,
    shape: "must-not-be-transport",
  });
  const fleet = fleetPayload({
    vendors: [],
    projects: [{
      project_id: "p", name: "p", registered: true, last_seen: null, skill_version: null,
      queue: { depth: 0, lanes: [], oldest_created_at: null },
      session: { available: false, active: null, queue_state: null, queue_last_touched: null, active_leases: null },
      milestone: { available: false, active_cadence: null, commits_since: null, cadence: null, due: null },
      workers: [row],
    }],
  });
  const { byId } = loadConsole(fleet, attentionPayload({ items: [] }));
  const agents = descendants(byId.fleet).filter((node) => node.className === "agent").map((node) => node.textContent);
  const wires = descendants(byId.fleet).filter((node) => node.className === "wire").map((node) => node.textContent);
  assert("missing worker measurements remain unknown", [
    byId.fleet.textContent.includes("literal-</script><img src=x>"),
    byId.fleet.textContent.includes("node-measured"),
    agents.includes("unknown"),
    !agents.includes("must-not-be-agent"),
    wires.includes("unknown"),
    !wires.includes("must-not-be-transport"),
  ].every(Boolean));
}

// Producer owns the verdict; contradictions and sparse records remain visible unknowns.
{
  const probe = producerProbe();
  const fleet = fleetPayload({
    machine: {
      queue_depth: 0, operating_cap: 12, active_leases: 0, local_workers: 0,
      rate_pressure: [], warnings: [],
    },
    projects: [{
      project_id: "p", name: "p", registered: true, last_seen: null, skill_version: null,
      queue: { depth: 0, lanes: [], oldest_created_at: null },
      session: { available: false, active: null, queue_state: null, queue_last_touched: null, active_leases: null },
      milestone: { available: false, active_cadence: null, commits_since: null, cadence: null, due: null },
      workers: [probe.conflict, probe.sparse],
    }],
  });
  const { byId } = loadConsole(fleet, attentionPayload({ items: [] }));
  const rowGlyphs = descendants(byId.fleet).filter((node) => /^glyph /.test(node.className)).map((node) => node.className);
  const states = descendants(byId.fleet).filter((node) => node.className === "state-txt").map((node) => node.textContent);
  assert("producer contradiction contract renders unknown", [
    probe.conflict.display_state === "unknown",
    probe.conflict.is_terminal === null,
    probe.conflict.classification_conflict === true,
    probe.sparse.display_state === "unknown",
    probe.sparse.is_terminal === null,
    probe.terminal_conflict.classification_conflict === true,
    byId.fleet.textContent.includes("conflict"),
    byId.fleet.textContent.includes("sparse"),
    states.some((value) => value.includes("conflicting authority fields")),
    rowGlyphs.every((value) => value === "glyph unknown"),
  ].every(Boolean));
}

// Machine count is authoritative; unassigned and remote rows are separate populations.
{
  const probe = producerProbe();
  const fleet = fleetPayload({
    machine: {
      queue_depth: 0, operating_cap: 12, active_leases: 1, local_workers: 1,
      rate_pressure: [], warnings: [],
    },
    projects: [],
    unassigned_workers: [workerRow({ dispatch_id: "unassigned-live" })],
    remote: { available: true, nodes: [], workers: [probe.remote] },
  });
  const { byId } = loadConsole(fleet, attentionPayload({ items: [] }));
  assert("authority count includes unassigned and remote seam renders", [
    byId.machine.textContent.includes("local running1"),
    byId.fleet.textContent.includes("Unassigned workers"),
    byId.fleet.textContent.includes("unassigned-live"),
    byId.fleet.textContent.includes("Remote workers"),
    byId.fleet.textContent.includes("remote-1"),
    byId.fleet.textContent.includes("studio-1"),
    probe.remote.display_state === "running",
  ].every(Boolean));
}

// Advisories stay visible but never inflate the human-waiting measurement.
{
  const advisory = attentionPayload({ items: [{
    dispatch_id: "notice", seq: 1, kind: "advisory", action: "review",
    observed_at: "2030-01-01T00:02:50Z", headline: "Informational only",
  }, {
    dispatch_id: "malformed", seq: 2, kind: "unrecognised", action: null,
    observed_at: "2030-01-01T00:02:51Z", headline: "Unrecognised is not actionable",
  }] });
  const { byId } = loadConsole(fleetPayload(), advisory);
  const glyphs = descendants(byId.attention).filter((node) => /^glyph /.test(node.className)).map((node) => node.className);
  assert("advisory-only payload reports zero waiting", [
    byId.attention.textContent.includes("0 waiting · 1 advisory"),
    !byId.attention.textContent.includes("1 waiting"),
    byId.attention.textContent.includes("Informational only"),
    byId.attention.textContent.includes("Unrecognised is not actionable"),
    !/\battn\b/.test(byId["attention-section"].className),
    glyphs.includes("glyph advisory"),
    glyphs.includes("glyph unknown"),
  ].every(Boolean));
}

// Large payloads retain an exact overflow statement instead of creating unbounded rows.
{
  const projects = Array.from({ length: 300 }, (_, projectIndex) => ({
    project_id: "bulk-" + projectIndex,
    name: "bulk-" + projectIndex,
    registered: true,
    last_seen: null,
    skill_version: null,
    queue: { depth: 0, lanes: [], oldest_created_at: null },
    session: { available: false, active: null, queue_state: null, queue_last_touched: null, active_leases: null },
    milestone: { available: false, active_cadence: null, commits_since: null, cadence: null, due: null },
    workers: Array.from({ length: 5 }, (_, workerIndex) => workerRow({
      dispatch_id: "bulk-" + projectIndex + "-" + workerIndex,
    })),
  }));
  const fleet = fleetPayload({
    machine: {
      queue_depth: 0, operating_cap: 2000, active_leases: 1500, local_workers: 1500,
      rate_pressure: [], warnings: [],
    },
    projects,
  });
  const { byId, api, clock } = loadConsole(fleet, attentionPayload({ items: [] }));
  const renderedRows = descendants(byId.fleet).filter((node) => node.className === "row").length;
  const renderedBands = descendants(byId.fleet).filter((node) => node.className === "panel band").length;
  assert("1500-worker payload is bounded and explicit", [
    renderedRows === api.maxVisibleWorkers,
    byId.fleet.textContent.includes("Showing 200 of 1500 active or unresolved worker rows"),
    byId.fleet.textContent.includes("across 50 of 300 groups"),
    renderedBands === 50,
    descendants(byId.fleet).length < 2600,
    clock.parses < 500,
  ].every(Boolean));
}

// Theme control is wired, cycles, and persists only accepted values.
{
  const { api, byId, documentElement, storage } = loadConsole(fleetPayload(), attentionPayload());
  const button = byId["theme-toggle"];
  const initial = button.textContent === "Theme: auto";
  button.click();
  const dark = documentElement.getAttribute("data-theme") === "dark" && storage["goalflight-fleet-theme"] === "dark";
  button.click();
  const light = documentElement.getAttribute("data-theme") === "light";
  button.click();
  const auto = documentElement.getAttribute("data-theme") === null && button.textContent === "Theme: auto";
  assert("theme button cycles and validates", initial && dark && light && auto && api.applyTheme("hostile") === "auto");
}

// The two mirrors reload independently; a failed reload invalidates only its plane.
{
  const { byId, document, window } = loadConsole(fleetPayload(), attentionPayload());
  const attentionTimer = window.intervals.find((timer) => timer.delay === 5000);
  const fleetTimer = window.intervals.find((timer) => timer.delay === 60000);
  attentionTimer.handler();
  const attentionScript = document.head.children[document.head.children.length - 1];
  window.GF_ATTENTION = attentionPayload({
    generation_id: "attention-refreshed",
    items: [{
      dispatch_id: "refresh-load", seq: 2, kind: "user_need", action: "Read",
      observed_at: "2030-01-01T00:02:59Z", headline: "Refreshed attention",
    }],
  });
  attentionScript.dispatch("load");
  fleetTimer.handler();
  const fleetScript = document.head.children[document.head.children.length - 1];
  fleetScript.dispatch("error");
  assert("independent script reload and failure invalidation", [
    attentionScript.src.startsWith("./attention-data.js?generation_check="),
    fleetScript.src.startsWith("./fleet-data.js?generation_check="),
    byId.attention.textContent.includes("Refreshed attention"),
    byId.fleet.textContent.includes("STALE · fleet plane"),
    !byId.fleet.textContent.includes("local-worker"),
    !document.head.children.includes(attentionScript),
    !document.head.children.includes(fleetScript),
  ].every(Boolean));
}

// A script request that never emits load/error cannot freeze that plane forever.
{
  const { byId, document, window } = loadConsole(fleetPayload(), attentionPayload());
  const attentionTimer = window.intervals.find((timer) => timer.delay === 5000);
  attentionTimer.handler();
  const stalledScript = document.head.children[document.head.children.length - 1];
  const watchdog = window.timeouts[window.timeouts.length - 1];
  watchdog.handler();
  attentionTimer.handler();
  const retryScript = document.head.children[document.head.children.length - 1];
  assert("hung script reload times out and retries its plane", [
    watchdog.delay === 5000,
    byId.attention.textContent.includes("STALE · attention plane"),
    !document.head.children.includes(stalledScript),
    retryScript !== stalledScript,
    retryScript.src.startsWith("./attention-data.js?generation_check="),
  ].every(Boolean));
}

// Age polling alone crosses stale thresholds even if neither file changes.
{
  const fleet = fleetPayload({
    sample_started_at: "2030-01-01T00:01:01Z",
    sample_finished_at: "2030-01-01T00:01:01Z",
    last_success_at: "2030-01-01T00:01:01Z",
  });
  const attention = attentionPayload({
    sample_started_at: "2030-01-01T00:02:51Z",
    sample_finished_at: "2030-01-01T00:02:51Z",
    last_success_at: "2030-01-01T00:02:51Z",
  });
  const { byId, clock, window } = loadConsole(fleet, attention);
  const ageTimer = window.intervals.find((timer) => timer.delay === 1000);
  const beganLive = !byId.fleet.textContent.includes("STALE") && !byId.attention.textContent.includes("STALE");
  clock.now += 2000;
  ageTimer.handler();
  assert("periodic age recomputation invalidates open tab", [
    beganLive,
    byId.fleet.textContent.includes("STALE · fleet plane"),
    byId.attention.textContent.includes("STALE · attention plane"),
  ].every(Boolean));
}

// Offline and injection boundaries remain unchanged by reload support.
{
  const executableSink = /\.\s*innerHTML\b|\[\s*["']innerHTML["']\s*\]/;
  const networkCall = /\b(?:fetch|XMLHttpRequest|WebSocket|EventSource)\s*\(/;
  assert("offline and text-only source contracts", [
    !executableSink.test(JS),
    !networkCall.test(JS),
    !/(?:src|href)=["'](?:https?:)?\/\//.test(HTML),
    HTML.includes('<script src="./fleet-data.js"></script>'),
    HTML.includes('<script src="./attention-data.js"></script>'),
    /id="live-status"[^>]*aria-live="polite"/.test(HTML),
    !/id="(?:plane-status|attention|fleet)"[^>]*aria-live/.test(HTML),
  ].every(Boolean));
}

// CSS matches the six emitted columns and no longer carries the reviewed dead mockup selectors.
{
  const cssWithoutComments = CSS.replace(/\/\*[\s\S]*?\*\//g, "");
  const dead = [
    "prose", "primary", "load-figure", "load-now", "load-of", "spark", "meter", "crit",
    "seatbars", "current", "pressure-dot", "age", "cpu", "cpubars", "lead", "kids",
    "cpu-lbl", "thinking", "subproc", "idle", "phase", "done", "now", "ticket", "bug",
    "eye", "watched", "parked", "dead", "hung", "bad", "calm",
  ];
  const deadAbsent = dead.every((name) => !(new RegExp("\\." + name + "\\b")).test(cssWithoutComments));
  assert("six-column CSS and wrapper styles", [
    /grid-template-columns:9px minmax\(168px,1\.3fr\) minmax\(112px,\.8fr\) minmax\(116px,1fr\) minmax\(80px,auto\) minmax\(96px,\.65fr\)/.test(CSS),
    /\.attention-section, \.fleet-section\s*\{[^}]*min-width:0/.test(CSS),
    /\.rows\s*\{[^}]*overflow-x:auto/.test(CSS),
    /\.glyph\.advisory\s*\{/.test(CSS),
    /\.panel-hd\s*\+\s*\.attn-row\s*\{[^}]*border-top:0/.test(CSS),
    !/\[hidden\]\s*\{/.test(cssWithoutComments),
    deadAbsent,
  ].every(Boolean));
}

// Projection schema rejects scalar timestamp coercion and emits canonical remote rows.
{
  const probe = producerProbe();
  assert("producer types and remote canonical fields", [
    probe.numeric_rejected === true,
    probe.remote.node_id === "studio-1",
    probe.remote.display_state === "running",
    probe.remote.is_terminal === false,
    probe.remote.classification_conflict === false,
    probe.fleet_schema === "goalflight.fleet-console.fleet.v2",
    JS.includes('fleet: "goalflight.fleet-console.fleet.v2"'),
  ].every(Boolean));
}

console.log("OK: fleet console renderer tests pass");
