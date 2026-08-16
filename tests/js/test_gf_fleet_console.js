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
      if (child.parentNode) {
        child.parentNode.children = child.parentNode.children.filter((item) => item !== child);
      }
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

function makeDom(fleet, attention, fetchImpl) {
  const byId = {};
  [
    "plane-status", "machine", "vendors", "attention", "fleet",
    "attention-section", "fleet-section", "theme-toggle", "live-status",
    "age-filter-toggle", "age-filter-note",
  ].forEach((id) => {
    byId[id] = element(id === "theme-toggle" || id === "age-filter-toggle" ? "button" : "div");
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
  const warnings = [];
  const clock = { now: NOW };
  const window = {
    document,
    GF_FLEET: fleet || null,
    GF_ATTENTION: attention || null,
    fetch: fetchImpl,
    localStorage: {
      getItem(key) { return Object.prototype.hasOwnProperty.call(storage, key) ? storage[key] : null; },
      setItem(key, value) { storage[key] = String(value); },
    },
    console: {
      warn(...args) { warnings.push(args.map(String).join(" ")); },
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
  return { byId, document, documentElement, storage, warnings, window, clock, FakeDate };
}

function loadConsole(fleet, attention, fetchImpl, source) {
  const dom = makeDom(fleet, attention, fetchImpl);
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
  vm.runInContext(source || JS, context, { filename: JS_PATH, timeout: 5000 });
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
    os_sandbox_requested: null,
    os_sandbox_supported: null,
    os_sandbox_enforced: null,
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
    authority_detail: null,
    authority_resolution: null,
    controller_session_digest: null,
    controller_pid: null,
    controller_label: null,
    controller_display: "unowned",
    controller_state: "unowned",
    controller_liveness_state: "UNKNOWN",
    age_filter_match: false,
    age_filter_reason: "within_threshold",
    observed_live: true,
    observed_live_source: "identity_recheck",
    task_ids: [],
    prompt_file: null,
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
    cadence_seconds: 30,
    registry_total: 1433,
    registry_deep_sampled: 12,
    history_excluded: 0,
    worker_age_filter: {
      threshold_seconds: 43200,
      default_enabled: true,
      unknown_started_at: "show",
    },
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
    remote: { available: true, history_excluded: 0, nodes: [], workers: [] },
    projects: [{
      project_id: "kiln-abc123",
      name: "kiln",
      registered: true,
      last_seen: "2030-01-01T00:02:00Z",
      skill_version: "1.3.0",
      history_excluded: 0,
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

function projectRow(workers, overrides) {
  return Object.assign({
    project_id: "p", name: "p", registered: true, last_seen: null, skill_version: null,
    history_excluded: 0,
    queue: { depth: 0, lanes: [], oldest_created_at: null },
    session: { available: false, active: null, queue_state: null, queue_last_touched: null, active_leases: null },
    milestone: { available: false, active_cadence: null, commits_since: null, cadence: null, due: null },
    workers,
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
    cadence_seconds: 5,
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
import json, os, pathlib, sys, tempfile
sys.path.insert(0, str(pathlib.Path.cwd() / "scripts"))
import goalflight_fleet_console as F
sampled_at = F._parse_timestamp("2030-01-01T00:03:00Z")
def worker(dispatch_id, started_at, **overrides):
    record = {
        "dispatch_id": dispatch_id, "state": "running",
        "classification": "expected_live", "worker_still_alive": None,
        "started_at": started_at,
    }
    record.update(overrides)
    return F._worker_row(
        record, sampled_at=sampled_at, controller_labels=controller_labels,
    )
with tempfile.TemporaryDirectory() as tmp:
    project_root = pathlib.Path(tmp)
    os.environ.update({
        "GOALFLIGHT_MESSAGES_DIR": str(project_root / "messages"),
        "GOALFLIGHT_FLEET_DIR": str(project_root / "fleet"),
        "GOALFLIGHT_JOURNAL_DIR": str(project_root / "state"),
        "GOALFLIGHT_TASK_STORE_DIR": str(project_root / "task-store"),
        "GOALFLIGHT_STATE_DIR": str(project_root / "state-root"),
        "GOALFLIGHT_WAKE_LEDGER_DIR": str(project_root / "wake-ledger"),
        "GOAL_FLIGHT_PIDFILE_DIR": str(project_root / "pids"),
        "GOALFLIGHT_TEST_MODE": "1",
    })
    claimed = F.goalflight_journal.open_or_create_journal(project_root).claim_or_renew_lease(
        "battery-main",
        principal={"principal_id": "fleet-console-renderer-probe"},
        nonce="controller-session",
    )
    assert claimed.committed
    controller_labels = F._controller_labels_by_session(
        project_root, [{"controller_session_id": "controller-session"}],
    )
    status_path = project_root / "status.json"
    status_path.write_text(json.dumps({
        "dispatch_id": "resumed-status-worker", "worker_alive": True,
        "heartbeat_at": "2030-01-01T00:03:30Z",
    }))
    old_status_live = worker(
        "resumed-status-worker", "2029-12-31T12:02:59Z", status_path=str(status_path),
    )
    missing_id_status_path = project_root / "status-missing-id.json"
    missing_id_status_path.write_text(json.dumps({
        "worker_alive": True, "heartbeat_at": "2030-01-01T00:03:30Z",
    }))
    old_status_missing_id = worker(
        "status-missing-id", "2029-12-31T12:02:59Z",
        status_path=str(missing_id_status_path),
    )
    mismatched_id_status_path = project_root / "status-mismatched-id.json"
    mismatched_id_status_path.write_text(json.dumps({
        "dispatch_id": "different-worker", "worker_alive": True,
        "heartbeat_at": "2030-01-01T00:03:30Z",
    }))
    old_status_mismatched_id = worker(
        "status-mismatched-id", "2029-12-31T12:02:59Z",
        status_path=str(mismatched_id_status_path),
    )
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
old = worker("old-worker", "2029-12-31T12:02:59Z")
old_live = worker("resumed-old-worker", "2029-12-31T12:02:59Z", worker_still_alive=True)
recent = worker("recent-worker", "2030-01-01T00:02:00Z")
malformed = worker("unknown-age-worker", "not-a-timestamp")
future = worker("future-worker", "2030-01-01T00:04:00Z")
ordered = F._sort_worker_rows([
    worker("tie-b", "2030-01-01T00:01:00Z"),
    malformed,
    worker("newest", "2030-01-01T00:02:00Z"),
    worker("tie-a", "2030-01-01T00:01:00Z"),
])
controller_rows = [
    dict(worker("owned-label", "2030-01-01T00:02:00Z", controller_session_id="controller-session", controller_pid=101), controller_liveness_state="ALIVE"),
    dict(worker("owned-session", "2030-01-01T00:02:00Z", controller_session_id="session-only", controller_pid=102), controller_liveness_state="HUNG"),
    dict(worker("owned-unknown", "2030-01-01T00:02:00Z", controller_pid=103), controller_liveness_state="WAITING-ON-USER"),
    dict(worker("unowned", "2030-01-01T00:02:00Z"), controller_liveness_state="DEAD"),
    dict(worker("unknown-probe", "2030-01-01T00:02:00Z", controller_session_id="unknown-session", controller_pid=104), controller_liveness_state="UNKNOWN"),
]
try:
    F._validate_scalar_types({"sample_started_at": 2030})
except F.ProjectionSecurityError:
    numeric_rejected = True
else:
    numeric_rejected = False
print(json.dumps({
    "conflict": conflict, "sparse": sparse, "remote": remote,
    "terminal_conflict": terminal_conflict, "numeric_rejected": numeric_rejected,
    "old": old, "old_live": old_live, "old_status_live": old_status_live,
    "old_status_missing_id": old_status_missing_id,
    "old_status_mismatched_id": old_status_mismatched_id,
    "recent": recent, "malformed": malformed, "future": future,
    "ordered": ordered, "controller_rows": controller_rows,
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
    sample_started_at: "2030-01-01T00:02:00Z",
    sample_finished_at: "2030-01-01T00:02:00Z",
    last_success_at: "2030-01-01T00:02:00Z",
  });
  const stale = fleetPayload({
    sample_started_at: "2030-01-01T00:01:59.999Z",
    sample_finished_at: "2030-01-01T00:01:59.999Z",
    last_success_at: "2030-01-01T00:01:59.999Z",
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

// Staleness follows the producer stamp in both directions, never the renderer
// reload interval supplied by the caller.
{
  const slowProducer = attentionPayload({
    cadence_seconds: 30,
    sample_started_at: "2030-01-01T00:02:01Z",
    sample_finished_at: "2030-01-01T00:02:01Z",
    last_success_at: "2030-01-01T00:02:01Z",
  });
  const fastProducer = attentionPayload({
    cadence_seconds: 5,
    sample_started_at: "2030-01-01T00:02:49.999Z",
    sample_finished_at: "2030-01-01T00:02:49.999Z",
    last_success_at: "2030-01-01T00:02:49.999Z",
  });
  const { api } = loadConsole();
  assert("producer-stamped cadence controls staleness both directions", [
    api.planeState(slowProducer, api.schemas.attention, 5000, NOW).stale === false,
    api.planeState(fastProducer, api.schemas.attention, 30000, NOW).stale === true,
  ].every(Boolean));
}

// A degraded payload and the stale banner both tell the operator exactly what
// to read and which status command to run.
{
  const degraded = attentionPayload({
    last_success_at: null,
    last_error: "mail:RuntimeError · action: read ~/.goal-flight/fleet-console-attention-launchd.log; run scripts/install-fleet-console.sh --status --plane attention",
    items: [],
  });
  const { byId } = loadConsole(fleetPayload(), degraded);
  assert("constructed DEGRADED payload renders operator action", [
    byId.attention.textContent.includes("~/.goal-flight/fleet-console-attention-launchd.log"),
    byId.attention.textContent.includes("scripts/install-fleet-console.sh --status --plane attention"),
    byId.attention.textContent.includes("Next:"),
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
    last_error: "local_status:PermissionError · action: read ~/.goal-flight/fleet-console-fleet-launchd.log; run scripts/install-fleet-console.sh --status --plane fleet",
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

// Sandbox request, support, and observed enforcement stay visibly independent.
{
  const row = workerRow({
    dispatch_id: "sandbox-refused",
    os_sandbox_requested: "read-only",
    os_sandbox_supported: "off",
    os_sandbox_enforced: null,
  });
  const fleet = fleetPayload({
    vendors: [],
    projects: [projectRow([row])],
  });
  const { byId } = loadConsole(fleet, attentionPayload({ items: [] }));
  const wires = descendants(byId.fleet).filter((node) => node.className === "wire").map((node) => node.textContent);
  assert("sandbox posture renders requested supported enforced separately", [
    wires.includes("acp sandbox req=read-only sup=off enf=unknown"),
    !wires.includes("acp ro"),
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

// The producer-owned age verdict drives a persisted browser-only presentation toggle.
{
  const probe = producerProbe();
  const fleet = fleetPayload({
    projects: [projectRow([probe.old_live, probe.old_status_live, probe.old, probe.recent])],
  });
  const { byId, storage } = loadConsole(fleet, attentionPayload({ items: [] }));
  const defaultDispatches = descendants(byId.fleet)
    .filter((node) => node.className === "did").map((node) => node.textContent);
  const hiddenByDefault = !defaultDispatches.includes("old-worker") &&
    defaultDispatches.includes("recent-worker") &&
    defaultDispatches.includes("resumed-old-worker") &&
    defaultDispatches.includes("resumed-status-worker");
  byId["age-filter-toggle"].click();
  const shownDispatches = descendants(byId.fleet)
    .filter((node) => node.className === "did").map((node) => node.textContent);
  assert("old non-terminal toggle hides by default and reveals without regeneration", [
    hiddenByDefault,
    defaultDispatches[0] === "resumed-old-worker",
    probe.old_live.observed_live === true,
    probe.old_live.age_filter_reason === "observed_live",
    probe.old_status_live.observed_live_source === "fresh_status",
    probe.old_status_live.age_filter_match === false,
    probe.old_status_missing_id.observed_live_source === "unobserved",
    probe.old_status_missing_id.age_filter_match === true,
    probe.old_status_mismatched_id.observed_live_source === "unobserved",
    probe.old_status_mismatched_id.age_filter_match === true,
    shownDispatches.includes("old-worker"),
    shownDispatches.includes("recent-worker"),
    byId["age-filter-toggle"].textContent.includes("shown"),
    storage["goalflight-fleet-age-filter"] === "show",
  ].every(Boolean));
}

// An empty-looking view must say when rows were removed by the active age filter.
{
  const probe = producerProbe();
  const secondOld = Object.assign({}, probe.old, { dispatch_id: "old-worker-2" });
  const { byId } = loadConsole(
    fleetPayload({ projects: [projectRow([probe.old, secondOld])] }),
    attentionPayload({ items: [] })
  );
  assert("hidden age-filter count remains visible", [
    byId["age-filter-toggle"].textContent.includes("Non-terminal / unresolved >12h"),
    byId["age-filter-toggle"].textContent.includes("2 hidden"),
    byId.fleet.textContent.includes("2 older rows hidden by age filter"),
    !byId.fleet.textContent.includes("No project has active workers or queued work"),
  ].every(Boolean));
}

// Producer order is newest-started first, then dispatch identity; unknown ages
// stay visible and sort after measured start times.
{
  const probe = producerProbe();
  const { byId } = loadConsole(
    fleetPayload({ projects: [projectRow(probe.ordered)] }),
    attentionPayload({ items: [] })
  );
  const dispatches = descendants(byId.fleet)
    .filter((node) => node.className === "did").map((node) => node.textContent);
  const bandDom = loadConsole(fleetPayload({ projects: [
    projectRow([probe.malformed], { project_id: "z", name: "unknown-start" }),
    projectRow([probe.ordered[1]], { project_id: "b", name: "older-start" }),
    projectRow([probe.old_live], { project_id: "live", name: "observed-live-old" }),
    projectRow([probe.recent], { project_id: "a", name: "recent-start" }),
  ] }), attentionPayload({ items: [] })).byId;
  const bandNames = descendants(bandDom.fleet)
    .filter((node) => node.className === "proj").map((node) => node.textContent);
  const futureDom = loadConsole(
    fleetPayload({ projects: [projectRow([probe.future])] }),
    attentionPayload({ items: [] })
  ).byId;
  assert("newest-started order is deterministic and unknown age policy is explicit", [
    JSON.stringify(dispatches) === JSON.stringify(["newest", "tie-a", "tie-b", "unknown-age-worker"]),
    JSON.stringify(bandNames) === JSON.stringify(["observed-live-old", "recent-start", "older-start", "unknown-start"]),
    probe.malformed.started_at === null,
    probe.malformed.age_filter_match === false,
    probe.malformed.age_filter_reason === "started_at_unknown",
    probe.future.age_filter_match === false,
    probe.future.age_filter_reason === "started_at_future",
    futureDom.fleet.textContent.includes("future-worker"),
    byId["age-filter-note"].textContent.includes("unknown start time stays visible"),
    byId["age-filter-note"].textContent.includes("Observed live first"),
  ].every(Boolean));
}

// Controller display uses only stamped ownership plus the matching beacon label.
{
  const probe = producerProbe();
  const { byId } = loadConsole(
    fleetPayload({ projects: [projectRow(probe.controller_rows)] }),
    attentionPayload({ items: [] })
  );
  const controllers = descendants(byId.fleet)
    .filter((node) => /^controller-id /.test(node.className));
  const controllerHealth = descendants(byId.fleet)
    .filter((node) => /^controller-health /.test(node.className));
  assert("controller label/session/unknown-owner/unowned states remain distinct", [
    controllers.some((node) => node.textContent === "battery-main" && node.className === "controller-id label"),
    controllers.some((node) => /^session · [0-9a-f]{16}$/.test(node.textContent) && node.className === "controller-id session"),
    controllers.some((node) => node.textContent === "owned · identity unknown" && node.className === "controller-id owned_unknown"),
    controllers.some((node) => node.textContent === "unowned" && node.className === "controller-id unowned"),
    !controllers.some((node) => node.textContent === "p" || node.textContent === "103"),
  ].every(Boolean));
  assert("all controller liveness states render and HUNG is distinct", [
    controllerHealth.some((node) => node.textContent === "ALIVE" && node.className === "controller-health alive"),
    controllerHealth.some((node) => node.textContent === "HUNG" && node.className === "controller-health hung"),
    controllerHealth.some((node) => node.textContent === "WAITING-ON-USER" && node.className === "controller-health waiting-on-user"),
    controllerHealth.some((node) => node.textContent === "DEAD" && node.className === "controller-health dead"),
    controllerHealth.some((node) => node.textContent === "UNKNOWN" && node.className === "controller-health unknown"),
  ].every(Boolean));
}

// HUNG controller attention is actionable and renderer values remain closed.
{
  const hostile = workerRow({ controller_liveness_state: "HUNG injected-class" });
  const legacy = workerRow({ dispatch_id: "legacy-v2-controller" });
  delete legacy.controller_liveness_state;
  const fleet = fleetPayload({ projects: [projectRow([hostile, legacy])] });
  const attention = attentionPayload({ items: [{
    dispatch_id: "project:controller:main",
    seq: null,
    kind: "controller_hung",
    action: "investigate",
    observed_at: null,
    headline: "Controller main is HUNG",
  }] });
  const { byId } = loadConsole(fleet, attention);
  const health = descendants(byId.fleet)
    .filter((node) => /^controller-health /.test(node.className));
  const glyphs = descendants(byId.attention)
    .filter((node) => /^glyph /.test(node.className));
  assert("HUNG attention is actionable and unknown renderer input cannot inject a class", [
    byId.attention.textContent.includes("Controller main is HUNG"),
    byId.attention.textContent.includes("1 waiting"),
    glyphs.some((node) => node.className === "glyph attn"),
    health.length === 2,
    health.every((node) => node.textContent === "UNKNOWN"),
    health.every((node) => node.className === "controller-health unknown"),
  ].every(Boolean));
}

// Bounded controller-history probes disclose omissions without inventing a note at zero.
{
  const truncated = loadConsole(
    fleetPayload(),
    attentionPayload({ controller_history_probes_truncated: 4 })
  ).byId.attention.textContent;
  const complete = loadConsole(
    fleetPayload(),
    attentionPayload({ controller_history_probes_truncated: 0 })
  ).byId.attention.textContent;
  assert("attention surfaces only positive controller history truncation", [
    truncated.includes("+4 older generations unprobed"),
    truncated.includes("click Show more"),
    !complete.includes("older generations unprobed"),
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
    descendants(byId.fleet).length < 3200,
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
  const fleetTimer = window.intervals.find((timer) => timer.delay === 30000);
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
    sample_started_at: "2030-01-01T00:02:01Z",
    sample_finished_at: "2030-01-01T00:02:01Z",
    last_success_at: "2030-01-01T00:02:01Z",
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

// Lazy fetches stay same-origin and all content sinks remain text-only.
{
  const executableSink = /\.\s*innerHTML\b|\[\s*["']innerHTML["']\s*\]/;
  const remoteNetworkCall = /fetch\(\s*["'](?:https?:)?\/\//;
  assert("offline and text-only source contracts", [
    !executableSink.test(JS),
    !remoteNetworkCall.test(JS),
    JS.includes('window.fetch("./history-data.js?history_check="'),
    JS.includes('window.fetch("./prompts/"'),
    !/(?:src|href)=["'](?:https?:)?\/\//.test(HTML),
    HTML.includes('<script src="./fleet-data.js"></script>'),
    HTML.includes('<script src="./attention-data.js"></script>'),
    /id="age-filter-toggle"[^>]*aria-pressed="true"/.test(HTML),
    /id="live-status"[^>]*aria-live="polite"/.test(HTML),
    !/id="(?:plane-status|attention|fleet)"[^>]*aria-live/.test(HTML),
  ].every(Boolean));
}

// CSS matches the seven emitted columns and no longer carries the reviewed dead mockup selectors.
{
  const cssWithoutComments = CSS.replace(/\/\*[\s\S]*?\*\//g, "");
  const dead = [
    "prose", "primary", "load-figure", "load-now", "load-of", "spark", "meter", "crit",
    "seatbars", "current", "pressure-dot", "age", "cpu", "cpubars", "lead", "kids",
    "cpu-lbl", "thinking", "subproc", "idle", "phase", "done", "now", "bug",
    "eye", "watched", "parked", "bad", "calm",
  ];
  const deadAbsent = dead.every((name) => !(new RegExp("\\." + name + "\\b")).test(cssWithoutComments));
  assert("seven-column CSS and wrapper styles", [
    /grid-template-columns:9px minmax\(168px,1\.3fr\) minmax\(112px,\.8fr\) minmax\(128px,\.9fr\) minmax\(116px,1fr\) minmax\(80px,auto\) minmax\(96px,\.65fr\)/.test(CSS),
    /\.attention-section, \.fleet-section\s*\{[^}]*min-width:0/.test(CSS),
    /\.rows\s*\{[^}]*overflow-x:auto/.test(CSS),
    /\.glyph\.advisory\s*\{/.test(CSS),
    /\.controller-id\.unowned\s*\{/.test(CSS),
    /\.controller-id\.owned_unknown\s*\{/.test(CSS),
    /\.controller-health\.hung\s*\{/.test(CSS),
    /\.controller-health\.dead\s*\{/.test(CSS),
    /\.panel-hd\s*\+\s*\.attn-row\s*\{[^}]*border-top:0/.test(CSS),
    /\[hidden\]\s*\{/.test(cssWithoutComments),
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

// Key dictionaries cannot be confused by JavaScript prototype property names.
{
  const hostileNames = fleetPayload({
    history_excluded: 3,
    vendors: [{ provider: "__proto__", seat_index: 1, remaining: "ok", reset_at: null, flags: [] }],
    projects: [projectRow([workerRow({
      dispatch_id: "prototype-controller",
      controller_label: "__proto__",
      controller_display: "__proto__",
      controller_state: "label",
    })])],
  });
  const { byId } = loadConsole(hostileNames, attentionPayload({ items: [] }));
  assert("prototype-shaped labels render and the global excluded counter is visible", [
    byId.vendors.textContent.includes("__proto__"),
    byId.fleet.textContent.includes("prototype-controller"),
    byId.machine.textContent.includes("+3 in history"),
  ].every(Boolean));
}

async function testInteractiveHistoryAndKeyedRows() {
  function terminalRow(id, state, taskIds) {
    return workerRow({
      dispatch_id: id,
      state,
      classification: state,
      terminal_state: state,
      display_state: state,
      is_terminal: true,
      worker_alive: false,
      observed_live: false,
      observed_live_source: "terminal_history",
      age_filter_reason: "terminal",
      controller_label: "main",
      controller_display: "main",
      controller_state: "label",
      task_ids: taskIds || [],
    });
  }

  // More than five terminals collapse by controller, live rows remain, and a
  // keyed refresh preserves both the open state and the exact row node.
  {
    const terminals = [
      terminalRow("term-0", "complete"), terminalRow("term-1", "complete"),
      terminalRow("term-2", "complete"), terminalRow("term-3", "complete"),
      terminalRow("term-4", "failed"), terminalRow("term-5", "failed"),
    ];
    const live = workerRow({ dispatch_id: "live-always", controller_label: "main", controller_display: "main", controller_state: "label" });
    const initial = fleetPayload({ projects: [projectRow([live].concat(terminals))] });
    const { api, byId, storage } = loadConsole(initial, attentionPayload({ items: [] }));
    const summary = descendants(byId.fleet).find((node) => node.className === "controller-toggle");
    const collapsedText = summary && summary.textContent;
    const collapsedRows = descendants(byId.fleet).filter((node) => node.className === "row");
    summary.click();
    const before = api.rowNode("term-5");
    const changed = terminals.map((row) => Object.assign({}, row));
    changed[5].ended_at = "2030-01-01T00:02:30Z";
    api.setFleetData(fleetPayload({ generation_id: "fleet-keyed-refresh", projects: [projectRow([live].concat(changed))] }));
    const after = api.rowNode("term-5");
    const expandedSummary = descendants(byId.fleet).find((node) => node.className === "controller-toggle");
    assert("keyed update preserves open controller disclosure", [
      collapsedText.includes("4 complete / 2 failed / newest 3 visible"),
      collapsedRows.some((node) => node.textContent.includes("live-always")),
      collapsedRows.length === 4,
      before && before === after,
      expandedSummary.getAttribute("aria-expanded") === "true",
      storage["goalflight-fleet-open-controllers"].includes("open|p|main"),
    ].every(Boolean));
  }

  // Mutation pair for keyed reconciliation: removing either appendChild move
  // leaves the old row/band order or the old controller grouping in place.
  {
    const alpha = workerRow({ dispatch_id: "alpha", started_at: "2030-01-01T00:02:00Z" });
    const beta = workerRow({ dispatch_id: "beta", started_at: "2030-01-01T00:01:00Z" });
    const projectA = projectRow([alpha], { project_id: "a", name: "project-a" });
    const projectB = projectRow([beta], { project_id: "b", name: "project-b" });
    const { api, byId } = loadConsole(
      fleetPayload({ projects: [projectA, projectB] }),
      attentionPayload({ items: [] })
    );

    const betaNewest = Object.assign({}, beta, { started_at: "2030-01-01T00:02:30Z" });
    api.setFleetData(fleetPayload({
      generation_id: "fleet-band-reorder",
      projects: [
        projectRow([Object.assign({}, alpha, { started_at: "2030-01-01T00:00:30Z" })], { project_id: "a", name: "project-a" }),
        projectRow([betaNewest], { project_id: "b", name: "project-b" }),
      ],
    }));
    const bandNames = descendants(byId.fleet)
      .filter((node) => node.className === "proj").map((node) => node.textContent);

    const ownedA = terminalRow("owned-a", "complete");
    Object.assign(ownedA, { controller_label: "owner-a", controller_display: "owner-a", controller_state: "label" });
    const ownedB = terminalRow("owned-b", "complete");
    Object.assign(ownedB, { controller_label: "owner-b", controller_display: "owner-b", controller_state: "label" });
    api.setFleetData(fleetPayload({
      generation_id: "fleet-owner-initial",
      projects: [projectRow([ownedA, ownedB])],
    }));
    const regroupedA = Object.assign({}, ownedA, {
      controller_label: "owner-b", controller_display: "owner-b", controller_state: "label",
    });
    api.setFleetData(fleetPayload({
      generation_id: "fleet-owner-regrouped",
      projects: [projectRow([ownedB, regroupedA])],
    }));
    const summaries = descendants(byId.fleet).filter((node) => node.className === "controller-toggle");
    const regroupedRows = descendants(byId.fleet)
      .filter((node) => node.className === "row" && node._gf && node._gf.worker)
      .map((node) => node._gf.worker.dispatch_id);
    assert("keyed reconciliation reorders bands and follows ownership regrouping", [
      JSON.stringify(bandNames) === JSON.stringify(["project-b", "project-a"]),
      summaries.length === 1,
      summaries[0].textContent.includes("owner-b"),
      JSON.stringify(regroupedRows) === JSON.stringify(["owned-b", "owned-a"]),
    ].every(Boolean));
  }

  // The first show-more action fetches the slow blob once and pages older
  // rows into the existing keyed project band.
  {
    const oldRows = [terminalRow("history-1", "complete"), terminalRow("history-2", "failed")];
    const history = {
      schema: "goalflight.fleet-console.history.v1",
      updated_at: "2030-01-01T00:02:00Z",
      projects: [{ project_id: "p", name: "p", workers: oldRows }],
    };
    const fetches = [];
    const fetchStub = (url) => {
      fetches.push(url);
      return Promise.resolve({ text: () => Promise.resolve("window.GF_HISTORY = " + JSON.stringify(history) + ";\n") });
    };
    const fleet = fleetPayload({ projects: [projectRow([], { history_excluded: 2 })], history_excluded: 2 });
    const { api, byId } = loadConsole(fleet, attentionPayload({ items: [] }), fetchStub);
    assert("excluded-rows counter is visible before history fetch", byId.fleet.textContent.includes("+2 in history"));
    await api.showMore("p");
    assert("show-more lazily fetches and renders slow history", [
      fetches.length === 1 && fetches[0].startsWith("./history-data.js?history_check="),
      byId.fleet.textContent.includes("history-1"),
      byId.fleet.textContent.includes("history-2"),
      byId.fleet.textContent.includes("+2 in history · loaded"),
    ].every(Boolean));
  }

  // Count-only terminal projects remain reachable through one collapsed band.
  // Dropping the global-minus-visible count recreates a '+N' with no button.
  {
    const archivedRows = [
      terminalRow("archived-1", "complete"),
      terminalRow("archived-2", "failed"),
    ];
    const history = {
      schema: "goalflight.fleet-console.history.v1",
      updated_at: "2030-01-01T00:02:00Z",
      projects: [{ project_id: "archived-project", name: "archived-project", workers: archivedRows }],
    };
    const fetches = [];
    const fetchStub = (url) => {
      fetches.push(url);
      return Promise.resolve({ text: () => Promise.resolve("window.GF_HISTORY = " + JSON.stringify(history) + ";\n") });
    };
    const { api, byId } = loadConsole(
      fleetPayload({ projects: [], history_excluded: 2 }),
      attentionPayload({ items: [] }),
      fetchStub
    );
    assert("count-only projects have one collapsed lazy archived band", [
      byId.fleet.textContent.includes("Archived projects (+2)"),
      byId.fleet.textContent.includes("Open archived projects · +2 in history"),
      api.rowNode("archived-1") === null,
      fetches.length === 0,
    ].every(Boolean));
    await api.showArchived();
    assert("archived band lazily makes every counted row reachable", [
      fetches.length === 1,
      api.rowNode("archived-1") !== null,
      api.rowNode("archived-2") !== null,
      byId.fleet.textContent.includes("+2 in history · loaded"),
    ].every(Boolean));
    api.setFleetData(fleetPayload({ generation_id: "archived-count-removed", projects: [], history_excluded: 0 }));
    assert("zero-count mutation removes the archived disclosure band",
      !byId.fleet.textContent.includes("Archived projects"));
  }

  // Mutation pair: a page-lifetime cache reports '+3 loaded' from the stale
  // two-row blob; keying it by the fleet exclusion counter forces a refetch.
  {
    const versions = [
      [terminalRow("grow-1", "complete"), terminalRow("grow-2", "failed")],
      [terminalRow("grow-1", "complete"), terminalRow("grow-2", "failed"), terminalRow("grow-3", "complete")],
    ];
    const fetches = [];
    const fetchStub = (url) => {
      const workers = versions[Math.min(fetches.length, versions.length - 1)];
      fetches.push(url);
      const history = {
        schema: "goalflight.fleet-console.history.v1",
        updated_at: "2030-01-01T00:02:00Z",
        projects: [{ project_id: "p", name: "p", workers }],
      };
      return Promise.resolve({ text: () => Promise.resolve("window.GF_HISTORY = " + JSON.stringify(history) + ";\n") });
    };
    const { api, byId } = loadConsole(
      fleetPayload({ projects: [projectRow([], { history_excluded: 2 })], history_excluded: 2 }),
      attentionPayload({ items: [] }),
      fetchStub
    );
    await api.showMore("p");
    api.setFleetData(fleetPayload({
      generation_id: "fleet-history-grew",
      projects: [projectRow([], { history_excluded: 3 })],
      history_excluded: 3,
    }));
    await api.showMore("p");
    assert("history counter growth invalidates and refetches the cache", [
      fetches.length === 2,
      byId.fleet.textContent.includes("grow-3"),
      byId.fleet.textContent.includes("+3 in history · loaded"),
    ].every(Boolean));
  }

  // A response for the old counter may finish after the replacement fetch;
  // the request key must prevent it from overwriting the newer cache.
  {
    const oldWorkers = [terminalRow("race-1", "complete"), terminalRow("race-2", "failed")];
    const newWorkers = oldWorkers.concat([terminalRow("race-3", "complete")]);
    const fetches = [];
    let releaseOld;
    function responseFor(workers) {
      const history = {
        schema: "goalflight.fleet-console.history.v1",
        updated_at: "2030-01-01T00:02:00Z",
        projects: [{ project_id: "p", name: "p", workers }],
      };
      return { text: () => Promise.resolve("window.GF_HISTORY = " + JSON.stringify(history) + ";\n") };
    }
    const fetchStub = (url) => {
      fetches.push(url);
      if (fetches.length === 1) {
        return new Promise((resolve) => { releaseOld = () => resolve(responseFor(oldWorkers)); });
      }
      return Promise.resolve(responseFor(newWorkers));
    };
    const { api, byId } = loadConsole(
      fleetPayload({ projects: [projectRow([], { history_excluded: 2 })], history_excluded: 2 }),
      attentionPayload({ items: [] }),
      fetchStub
    );
    const oldShowMore = api.showMore("p");
    api.setFleetData(fleetPayload({
      generation_id: "fleet-history-race-grew",
      projects: [projectRow([], { history_excluded: 3 })],
      history_excluded: 3,
    }));
    await api.showMore("p");
    releaseOld();
    await oldShowMore;
    assert("stale in-flight history response cannot replace the newer counter", [
      fetches.length === 2,
      byId.fleet.textContent.includes("race-3"),
      byId.fleet.textContent.includes("+3 in history · loaded"),
    ].every(Boolean));
  }

  // The stale request's rejection must not clear a newer pending request.
  // Removing the promise-identity guard makes the joined call issue fetch #3.
  {
    const workers = [
      terminalRow("reject-1", "complete"),
      terminalRow("reject-2", "failed"),
      terminalRow("reject-3", "complete"),
    ];
    const history = {
      schema: "goalflight.fleet-console.history.v1",
      updated_at: "2030-01-01T00:02:00Z",
      projects: [{ project_id: "p", name: "p", workers }],
    };
    const response = { text: () => Promise.resolve("window.GF_HISTORY = " + JSON.stringify(history) + ";\n") };
    const fetches = [];
    let rejectOld;
    let resolveReplacement;
    const fetchStub = (url) => {
      fetches.push(url);
      if (fetches.length === 1) {
        return new Promise((_resolve, reject) => { rejectOld = reject; });
      }
      if (fetches.length === 2) {
        return new Promise((resolve) => { resolveReplacement = () => resolve(response); });
      }
      return Promise.resolve(response);
    };
    const { api, byId } = loadConsole(
      fleetPayload({ projects: [projectRow([], { history_excluded: 2 })], history_excluded: 2 }),
      attentionPayload({ items: [] }),
      fetchStub
    );
    const staleRequest = api.showMore("p").catch(() => null);
    api.setFleetData(fleetPayload({
      generation_id: "fleet-history-rejection-race",
      projects: [projectRow([], { history_excluded: 3 })],
      history_excluded: 3,
    }));
    const replacement = api.showMore("p");
    rejectOld(new Error("stale history request failed"));
    await staleRequest;
    const joinedReplacement = api.showMore("p");
    const fetchCountBeforeRelease = fetches.length;
    resolveReplacement();
    await Promise.all([replacement, joinedReplacement]);
    assert("stale rejection cannot clear the replacement history promise", [
      fetchCountBeforeRelease === 2,
      fetches.length === 2,
      byId.fleet.textContent.includes("reject-3"),
    ].every(Boolean));
  }

  // Prompt text is fetched only on disclosure and enters the DOM via
  // textContent, so hostile markup remains inert text.
  {
    const fetches = [];
    const promptText = "<img src=x onerror=alert(1)>\noperator prompt";
    const fetchStub = (url) => {
      fetches.push(url);
      return Promise.resolve({ text: () => Promise.resolve(promptText) });
    };
    const promptRow = workerRow({ dispatch_id: "prompt-worker", prompt_file: "safe-prompt.txt" });
    const { api, byId } = loadConsole(
      fleetPayload({ projects: [projectRow([promptRow])] }),
      attentionPayload({ items: [] }),
      fetchStub
    );
    assert("prompt is not fetched while disclosure is closed", fetches.length === 0);
    await api.openPrompt("prompt-worker");
    const body = descendants(byId.fleet).find((node) => node.className === "prompt-body");
    assert("prompt disclosure fetches once and renders through textContent", [
      fetches.length === 1 && fetches[0] === "./prompts/safe-prompt.txt",
      body && body.textContent === promptText,
      body.children.length === 0,
    ].every(Boolean));
  }

  // Mutation pair: retaining the first rejected promise makes the second
  // disclosure reuse the rejection instead of observing the recovery.
  {
    const fetches = [];
    const fetchStub = (url) => {
      fetches.push(url);
      if (fetches.length === 1) return Promise.reject(new Error("transient prompt failure"));
      return Promise.resolve({ text: () => Promise.resolve("prompt recovered") });
    };
    const promptRow = workerRow({ dispatch_id: "prompt-retry", prompt_file: "retry.txt" });
    const { api, byId } = loadConsole(
      fleetPayload({ projects: [projectRow([promptRow])] }),
      attentionPayload({ items: [] }),
      fetchStub
    );
    await api.openPrompt("prompt-retry");
    await api.openPrompt("prompt-retry");
    const body = descendants(byId.fleet).find((node) => node.className === "prompt-body");
    assert("transient prompt rejection is evicted before retry", [
      fetches.length === 2,
      body && body.textContent === "prompt recovered",
    ].every(Boolean));
  }

  // Mutation pair: without per-render pruning, a disappeared identity inherits
  // its old open states. Quota failure must change only persistence, not the
  // in-session pruning decision, and warn once for both set writes.
  {
    const terminals = Array.from({ length: 6 }, (_, index) => terminalRow(
      "persist-" + index,
      "complete"
    ));
    terminals[0].prompt_file = "persist.txt";
    const fetchStub = () => Promise.resolve({ text: () => Promise.resolve("persisted prompt") });
    const { api, byId, warnings, window } = loadConsole(
      fleetPayload({ projects: [projectRow(terminals)] }),
      attentionPayload({ items: [] }),
      fetchStub
    );
    const summary = descendants(byId.fleet).find((node) => node.className === "controller-toggle");
    summary.click();
    await api.openPrompt("persist-0");
    window.localStorage.setItem = () => { throw new Error("quota exceeded"); };
    api.setFleetData(fleetPayload({ generation_id: "fleet-row-gone", projects: [] }));
    api.setFleetData(fleetPayload({ generation_id: "fleet-row-returned", projects: [projectRow(terminals)] }));
    const returnedSummary = descendants(byId.fleet).find((node) => node.className === "controller-toggle");
    const returnedPrompt = descendants(byId.fleet).find((node) => node.className === "prompt-toggle");
    assert("disclosure keys prune and quota failure warns once", [
      returnedSummary.getAttribute("aria-expanded") === "false",
      returnedPrompt.getAttribute("aria-expanded") === "false",
      warnings.length === 1,
      warnings[0].includes("localStorage"),
    ].every(Boolean));
  }

  // Loaded slow-history rows count as current only while their project remains
  // in the fleet payload. Removing the currentProjectIds guard recreates the
  // stale prompt disclosure when the project disappears and later returns.
  {
    const historyWorker = terminalRow("history-disclosure", "complete");
    historyWorker.prompt_file = "history-disclosure.txt";
    const history = {
      schema: "goalflight.fleet-console.history.v1",
      updated_at: "2030-01-01T00:02:00Z",
      projects: [{ project_id: "p", name: "p", workers: [historyWorker] }],
    };
    const fetchStub = (url) => url.startsWith("./history-data.js")
      ? Promise.resolve({ text: () => Promise.resolve("window.GF_HISTORY = " + JSON.stringify(history) + ";\n") })
      : Promise.resolve({ text: () => Promise.resolve("history prompt") });
    const currentProject = projectRow([], { history_excluded: 1 });
    const { api, byId } = loadConsole(
      fleetPayload({ projects: [currentProject], history_excluded: 1 }),
      attentionPayload({ items: [] }),
      fetchStub
    );
    await api.showMore("p");
    await api.openPrompt("history-disclosure");
    api.setFleetData(fleetPayload({ generation_id: "history-project-gone", projects: [], history_excluded: 1 }));
    api.setFleetData(fleetPayload({ generation_id: "history-project-returned", projects: [currentProject], history_excluded: 1 }));
    const returnedPrompt = descendants(byId.fleet).find((node) => node.className === "prompt-toggle");
    assert("history disclosure key prunes when its project leaves the fleet payload", [
      returnedPrompt,
      returnedPrompt.getAttribute("aria-expanded") === "false",
    ].every(Boolean));
  }

  // Task chips are scalar buttons; clicking one filters every project to the
  // ticket's linked dispatches and exposes a clear-filter control.
  {
    const first = workerRow({ dispatch_id: "ticket-one", task_ids: ["b-151"] });
    const second = workerRow({ dispatch_id: "ticket-two", task_ids: ["t-243"] });
    const conflict = workerRow({
      dispatch_id: "named-conflict",
      task_ids: ["b-151"],
      display_state: "unknown",
      classification_conflict: true,
      authority_detail: "status.json.state=running; ledger.terminal_state=failed",
    });
    const { api, byId } = loadConsole(
      fleetPayload({ projects: [projectRow([first, second, conflict])] }),
      attentionPayload({ items: [] })
    );
    const chip = descendants(byId.fleet).find((node) => node.className === "task-chip" && node.textContent === "b-151");
    chip.click();
    const detail = descendants(byId.fleet).find((node) => node.className === "authority-detail");
    assert("ticket chip filters dispatches and named authority detail renders", [
      api.rowNode("ticket-one") !== null,
      api.rowNode("ticket-two") === null,
      api.rowNode("named-conflict") !== null,
      byId.fleet.textContent.includes("Ticket b-151 · clear filter"),
      detail && detail.textContent.includes("status.json.state") && detail.textContent.includes("ledger.terminal_state"),
    ].every(Boolean));
  }
}

testInteractiveHistoryAndKeyedRows().then(function () {
  console.log("OK: fleet console renderer tests pass");
}).catch(function (error) {
  console.error(error && error.stack ? error.stack : error);
  process.exit(1);
});
