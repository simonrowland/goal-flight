/* Goal Flight fleet console — presentation over producer-owned verdicts. */
(function () {
  "use strict";

  var SCHEMAS = {
    fleet: "goalflight.fleet-console.fleet.v2",
    attention: "goalflight.fleet-console.attention.v1"
  };
  /* Mirror reload polling is intentionally independent of producer cadence.
   * Freshness never reads these UI timers; it follows each payload's stamp. */
  var CADENCES = { attention: 5000, fleet: 30000 };
  var CLOCK_TOLERANCE_MS = 2000;
  var MAX_VISIBLE_WORKERS = 200;
  var MAX_VISIBLE_BANDS = 50;
  var ACTIONABLE_KINDS = {
    user_need: true,
    user_confirm: true,
    blocked: true,
    controller_hung: true
  };
  var CONTROLLER_LIVENESS_STATES = {
    ALIVE: true,
    HUNG: true,
    "WAITING-ON-USER": true,
    DEAD: true,
    UNKNOWN: true
  };
  var CONTROLLER_IDENTITY_STATES = {
    label: true,
    session: true,
    owned_unknown: true,
    unowned: true
  };
  var DISPLAY_GLYPHS = {
    attention: "attn",
    queued: "quiet",
    waiting: "quiet",
    starting: "moving",
    running: "moving",
    running_quiet: "quiet",
    unknown: "unknown"
  };

  var FLEET = window.GF_FLEET || null;
  var ATTENTION = window.GF_ATTENTION || null;
  var reloadPending = { fleet: false, attention: false };
  var lastAgeSignature = null;
  var themeMode = "auto";
  var ageFilterEnabled = true;
  var ticketFilter = null;
  var historyPayload = null;
  var historyPromise = null;
  var historyPayloadKey = null;
  var historyPages = Object.create(null);
  var archivedHistoryPages = 0;
  var historyPageSize = 20;
  var promptCache = Object.create(null);
  var promptPromises = Object.create(null);
  var bandNodes = Object.create(null);
  var storageWriteWarned = false;

  function storedSet(key) {
    try {
      var value = JSON.parse(window.localStorage.getItem(key) || "[]");
      return new Set(Array.isArray(value) ? value.map(String) : []);
    } catch (_error) {
      return new Set();
    }
  }

  var openControllers = storedSet("goalflight-fleet-open-controllers");
  var openPrompts = storedSet("goalflight-fleet-open-prompts");
  var openDeadControllers = storedSet("goalflight-fleet-open-dead-controllers");
  var openIdleCheckouts = storedSet("goalflight-fleet-open-idle-checkouts");

  function persistValue(key, value) {
    try {
      window.localStorage.setItem(key, value);
      return true;
    } catch (error) {
      if (!storageWriteWarned) {
        storageWriteWarned = true;
        if (window.console && typeof window.console.warn === "function") {
          window.console.warn("Goal Flight localStorage unavailable; disclosure state is session-only.", error);
        }
      }
      return false;
    }
  }

  function persistSet(key, values) {
    persistValue(key, JSON.stringify(Array.from(values)));
  }

  function el(tag, cls, txt) {
    var node = document.createElement(tag);
    if (cls) node.className = cls;
    if (txt != null) node.textContent = txt;
    return node;
  }

  function chip(cls, label, value) {
    var node = el("span", "chip " + (cls || ""));
    node.appendChild(el("span", null, label));
    if (value != null) node.appendChild(el("b", null, String(value)));
    return node;
  }

  function textValue(value) {
    return value == null || value === "" ? "unknown" : String(value);
  }

  function controllerLiveness(worker) {
    var state = textValue(worker.controller_liveness_state);
    return CONTROLLER_LIVENESS_STATES[state] === true ? state : "UNKNOWN";
  }

  function controllerIdentityState(worker) {
    var state = textValue(worker.controller_state);
    return CONTROLLER_IDENTITY_STATES[state] === true ? state : "owned_unknown";
  }

  function parseTs(value) {
    if (typeof value !== "string" || !value.trim()) return null;
    var parsed = Date.parse(value);
    return isNaN(parsed) ? null : parsed;
  }

  /* Human age buckets use the nearest whole minute after the explicit "now"
   * window. Elapsed ms ÷ 60,000 ms/min = minutes (units cancel); rounding
   * 89 s gives 1 min and 90 s gives 2 min, while every value below 30 s stays
   * "now" instead of becoming a misleading zero-minute label. */
  function ageFrom(iso, now) {
    var parsed = parseTs(iso);
    if (parsed == null) return "age unknown";
    if (parsed - now > CLOCK_TOLERANCE_MS) return "clock ahead";
    var elapsed = Math.max(0, now - parsed);
    if (elapsed < 30 * 1000) return "now";
    var mins = Math.max(1, Math.round(elapsed / 60000));
    if (mins < 60) return mins + " min";
    var hours = Math.round(mins / 60);
    return hours < 48 ? hours + " h" : Math.round(hours / 24) + " d";
  }

  /* Fast planes can be stale while the general UI age bucket still says
   * "now". Preserve that friendly bucket elsewhere, but give freshness
   * notices enough precision to explain a sub-minute stale verdict. */
  function freshnessAgeFrom(iso, now) {
    var parsed = parseTs(iso);
    if (parsed == null) return "age unknown";
    if (parsed - now > CLOCK_TOLERANCE_MS) return "clock ahead";
    var elapsed = Math.max(0, now - parsed);
    if (elapsed < 60 * 1000) return Math.ceil(elapsed / 1000) + " sec";
    return ageFrom(iso, now);
  }

  function whenFrom(value, now) {
    var parsed = typeof value === "number" ? value * 1000 : parseTs(value);
    if (parsed == null || isNaN(parsed)) return null;
    var mins = Math.round((parsed - now) / 60000);
    if (mins <= 0) return "now";
    if (mins < 60) return "in " + mins + " min";
    var hours = Math.round(mins / 60);
    return hours < 48 ? "in " + hours + " h" : "in " + Math.round(hours / 24) + " d";
  }

  /* The one freshness threshold authority for headers and panel banners.
   * Units: producer-stamped seconds × 1,000 ms/s × 2 cadences = ms. */
  function freshnessLimitMs(cadenceSeconds) {
    var seconds = Number(cadenceSeconds);
    return Number.isFinite(seconds) && seconds > 0 ? seconds * 1000 * 2 : null;
  }

  /* Stale after two missed PRODUCER-stamped cadences. The renderer reload
   * interval is intentionally not an authority: changing a producer schedule
   * without this stamp once made every healthy sample look stale. */
  function planeState(payload, schema, _reloadCadenceMs, now) {
    var present = payload && typeof payload === "object" && !Array.isArray(payload);
    var data = present ? payload : {};
    var schemaMatches = present && data.schema === schema;
    /* A mismatched schema does not authorize even the familiar-looking
     * metadata fields. Treat their values as absent instead of using an
     * unrecognised payload to manufacture a trustworthy observation age. */
    var started = schemaMatches ? parseTs(data.sample_started_at) : null;
    var finished = schemaMatches ? parseTs(data.sample_finished_at) : null;
    var success = schemaMatches ? parseTs(data.last_success_at) : null;
    var freshnessLimit = schemaMatches ? freshnessLimitMs(data.cadence_seconds) : null;
    var issue = null;

    if (!present) issue = "payload absent";
    else if (!schemaMatches) issue = "schema mismatch";
    else if (data.last_error) issue = "producer error";
    else if (started == null || finished == null || success == null) issue = "timestamp missing";
    else if (started - now > CLOCK_TOLERANCE_MS || finished - now > CLOCK_TOLERANCE_MS ||
             success - now > CLOCK_TOLERANCE_MS) issue = "clock ahead";
    else if (finished < started) issue = "timestamp order invalid";
    else if (typeof data.generation_id !== "string" || !data.generation_id) issue = "generation missing";
    else if (freshnessLimit == null) issue = "cadence unknown";

    var age = success == null ? null : Math.max(0, now - success);
    /* Guard both operands explicitly: null cadence must not be coerced to a
     * zero-ms threshold, and null age must not participate in arithmetic. */
    var exceededCadence = age != null && freshnessLimit != null && age > freshnessLimit;
    var stale = issue !== null || exceededCadence;
    var observed = schemaMatches ? data.last_success_at : null;
    var observedAge = freshnessAgeFrom(observed, now);
    var reason = issue || (exceededCadence
      ? "age exceeds " + (freshnessLimit / 1000) + " sec freshness limit"
      : null);
    return {
      stale: stale,
      freshnessIssue: issue,
      label: stale ? (issue || "stale " + observedAge) : "live",
      detail: ((schemaMatches && data.producer && data.producer.plane) || "plane") +
        " · last success " + ageFrom(observed, now) +
        (schemaMatches && data.generation_id ? " · " + data.generation_id : "") +
        (schemaMatches && data.last_error ? " · last error: " + data.last_error : ""),
      lastObservedAge: observedAge,
      lastError: schemaMatches ? data.last_error : null,
      freshnessLimitMs: freshnessLimit,
      reason: reason
    };
  }

  function operatorAction(plane) {
    return "Read ~/.goal-flight/fleet-console-" + plane +
      "-launchd.log; run scripts/install-fleet-console.sh --status --plane " + plane + ".";
  }

  function staleNotice(plane, state) {
    var notice = el("div", "stale-state");
    var heading = state.freshnessIssue === "cadence unknown" ? "CADENCE UNKNOWN" : "STALE";
    notice.appendChild(el("strong", null, heading + " · " + plane + " plane"));
    notice.appendChild(el("span", null, "Last observed " + state.lastObservedAge));
    notice.appendChild(el("span", null, "Reason: " + (state.reason || state.label)));
    if (state.lastError) notice.appendChild(el("span", null, "Last error: " + state.lastError));
    notice.appendChild(el("span", "operator-action", "Next: " + operatorAction(plane)));
    return notice;
  }

  function replaceWithStale(host, plane, state) {
    if (!host) return;
    host.textContent = "";
    host.appendChild(staleNotice(plane, state));
  }

  /* A failed tick can keep last-good rows and set incomplete. Show those
   * rows with the existing stale/Untrusted chrome instead of wiping them. */
  function keepLastGood(payload) {
    return !!(payload && payload.incomplete === true);
  }

  function shouldReplaceWithStale(payload, state) {
    return !!(state && state.stale && !keepLastGood(payload));
  }

  function visibleWorker(worker) {
    return worker && typeof worker === "object";
  }

  function displayedWorker(worker) {
    return visibleWorker(worker) &&
      (worker.is_terminal === true || !ageFilterEnabled || worker.age_filter_match !== true) &&
      (!ticketFilter || (worker.task_ids || []).map(String).indexOf(ticketFilter) !== -1);
  }

  function workerAgeSummary() {
    var rows = [];
    (FLEET.projects || []).forEach(function (project) {
      rows = rows.concat(project.workers || []);
    });
    rows = rows.concat(FLEET.unassigned_workers || [], ((FLEET.remote || {}).workers) || []);
    var summary = { matches: 0, hidden: 0, unknown: 0 };
    rows.filter(function (worker) { return visibleWorker(worker) && worker.is_terminal !== true; }).forEach(function (worker) {
      if (worker.age_filter_match === true) summary.matches += 1;
      if (worker.age_filter_reason === "started_at_unknown") summary.unknown += 1;
    });
    summary.hidden = ageFilterEnabled ? summary.matches : 0;
    return summary;
  }

  function ageThresholdLabel() {
    var seconds = Number(((FLEET.worker_age_filter || {}).threshold_seconds));
    if (!Number.isFinite(seconds) || seconds <= 0) return "configured age";
    if (seconds % 3600 === 0) return (seconds / 3600) + "h";
    if (seconds % 60 === 0) return (seconds / 60) + "m";
    return seconds + "s";
  }

  function renderAgeFilterControl(fleetState) {
    var button = document.getElementById("age-filter-toggle");
    var note = document.getElementById("age-filter-note");
    if (!button || !note) return;
    if (shouldReplaceWithStale(FLEET, fleetState)) {
      button.textContent = "Age filter: unavailable";
      button.setAttribute("aria-pressed", ageFilterEnabled ? "true" : "false");
      note.textContent = "Fleet data stale; age filter not applied.";
      return;
    }
    var summary = workerAgeSummary();
    button.textContent = "Non-terminal / unresolved >" + ageThresholdLabel() + ": " +
      (ageFilterEnabled ? "hidden · " + summary.hidden + " hidden" : "shown");
    button.setAttribute("aria-pressed", ageFilterEnabled ? "true" : "false");
    note.textContent = (ticketFilter ? "Ticket " + ticketFilter + " selected · " : "") +
      "Observed live first · otherwise newest started · unknown start time stays visible.";
  }

  function glyphFor(worker) {
    if (worker.classification_conflict) return "unknown";
    return DISPLAY_GLYPHS[worker.display_state] || "unknown";
  }

  function controllerKey(worker) {
    return textValue(worker.controller_label || worker.controller_session_digest ||
      worker.controller_display || "unowned");
  }

  function controllerExpanded(key, terminalCount) {
    if (openControllers.has("open|" + key)) return true;
    if (openControllers.has("closed|" + key)) return false;
    return terminalCount <= 5;
  }

  function setControllerExpanded(key, expanded) {
    openControllers.delete("open|" + key);
    openControllers.delete("closed|" + key);
    openControllers.add((expanded ? "open|" : "closed|") + key);
    persistSet("goalflight-fleet-open-controllers", openControllers);
  }

  function workerKey(worker) {
    return textValue(worker.dispatch_id) + "|" + textValue(worker.node_id);
  }

  function setHidden(node, hidden) {
    if (hidden) node.setAttribute("hidden", "hidden");
    else node.removeAttribute("hidden");
  }

  function reconcileKeyed(parent, nodes, desired, createFn, updateFn) {
    Object.keys(nodes).forEach(function (key) {
      if (!Object.prototype.hasOwnProperty.call(desired, key)) {
        nodes[key].remove();
        delete nodes[key];
      }
    });
    Object.keys(desired).forEach(function (key) {
      var item = desired[key];
      var node = nodes[key];
      if (!node) {
        node = createFn(item);
        nodes[key] = node;
      }
      updateFn(node, item);
      parent.appendChild(node);
    });
  }

  function persistTicketFilter(value) {
    ticketFilter = value == null || value === "" ? null : String(value);
    persistValue("goalflight-fleet-ticket-filter", ticketFilter || "");
    return ticketFilter;
  }

  function initializeTicketFilter() {
    try {
      var saved = window.localStorage.getItem("goalflight-fleet-ticket-filter");
      if (saved) ticketFilter = saved;
    } catch (_error) { /* optional */ }
    return ticketFilter;
  }

  function fetchPrompt(entry) {
    var worker = entry._gf.worker;
    var filename = worker && worker.prompt_file;
    var key = workerKey(worker || {});
    if (!filename || typeof window.fetch !== "function") return Promise.resolve(null);
    if (Object.prototype.hasOwnProperty.call(promptCache, filename)) {
      entry._gf.prompt.textContent = promptCache[filename];
      return Promise.resolve(promptCache[filename]);
    }
    if (!promptPromises[filename]) {
      promptPromises[filename] = window.fetch("./prompts/" + encodeURIComponent(filename))
        .then(function (response) {
          if (!response || typeof response.text !== "function") throw new Error("prompt response unreadable");
          if (response.ok === false) throw new Error("prompt response unavailable");
          return response.text();
        })
        .then(function (text) {
          promptCache[filename] = String(text);
          return promptCache[filename];
        })
        .catch(function (error) {
          delete promptPromises[filename];
          throw error;
        });
    }
    return promptPromises[filename].then(function (text) {
      if (entry._gf.worker && workerKey(entry._gf.worker) === key) {
        entry._gf.prompt.textContent = text;
      }
      return text;
    }).catch(function () {
      entry._gf.prompt.textContent = "Prompt unavailable.";
      return null;
    });
  }

  function setPromptOpen(entry, open) {
    var worker = entry._gf.worker || {};
    var key = workerKey(worker);
    if (open) openPrompts.add(key); else openPrompts.delete(key);
    persistSet("goalflight-fleet-open-prompts", openPrompts);
    entry._gf.promptToggle.textContent = open ? "▾" : "▸";
    entry._gf.promptToggle.setAttribute("aria-expanded", open ? "true" : "false");
    setHidden(entry._gf.prompt, !open);
    return open ? fetchPrompt(entry) : Promise.resolve(null);
  }

  function createWorkerEntry() {
    var entry = el("div", "row");
    var row = entry;
    var glyph = el("i", "glyph unknown");
    var dispatch = el("div", "did");
    var promptToggle = el("button", "prompt-toggle", "▸");
    promptToggle.setAttribute("type", "button");
    var dispatchLabel = el("span", "did");
    var taskList = el("span", "task-list");
    var identity = el("div");
    var agent = el("div", "agent");
    var wire = el("div", "wire");
    identity.appendChild(agent);
    identity.appendChild(wire);
    var controller = el("div", "controller-cell");
    var controllerId = el("div", "controller-id");
    var controllerHealth = el("div", "controller-health unknown");
    controller.appendChild(controllerId);
    controller.appendChild(controllerHealth);
    var state = el("div", "state-txt");
    var started = el("div", "host");
    var host = el("div", "host");
    [glyph, dispatch, identity, controller, state, started, host].forEach(function (node) {
      row.appendChild(node);
    });
    var prompt = el("pre", "prompt-body");
    setHidden(prompt, true);
    entry._gf = {
      row: row, glyph: glyph, promptToggle: promptToggle,
      dispatchLabel: dispatchLabel, taskList: taskList,
      agent: agent, wire: wire, controllerId: controllerId,
      controllerHealth: controllerHealth, state: state,
      started: started, host: host, prompt: prompt, worker: null
    };
    promptToggle.addEventListener("click", function () {
      var current = entry._gf.worker || {};
      setPromptOpen(entry, !openPrompts.has(workerKey(current)));
    });
    return entry;
  }

  function updateWorkerEntry(entry, worker, now) {
    var refs = entry._gf;
    refs.worker = worker;
    entry.setAttribute("data-dispatch-id", textValue(worker.dispatch_id));
    refs.glyph.className = "glyph " + glyphFor(worker);
    var tasks = worker.task_ids || [];
    refs.dispatchLabel.textContent = textValue(worker.dispatch_id);
    refs.promptToggle.remove();
    refs.taskList.remove();
    refs.dispatchLabel.remove();
    refs.taskList.textContent = "";
    refs.row.children[1].textContent = "";
    if (worker.prompt_file || tasks.length) {
      refs.row.children[1].className = "dispatch-cell";
      if (worker.prompt_file) refs.row.children[1].appendChild(refs.promptToggle);
      refs.row.children[1].appendChild(refs.dispatchLabel);
      if (tasks.length) refs.row.children[1].appendChild(refs.taskList);
    } else {
      refs.row.children[1].className = "did";
      refs.row.children[1].textContent = textValue(worker.dispatch_id);
    }
    tasks.forEach(function (taskId) {
      var button = el("button", "task-chip", textValue(taskId));
      button.setAttribute("type", "button");
      button._taskId = String(taskId);
      button.setAttribute("aria-pressed", ticketFilter && ticketFilter === button._taskId ? "true" : "false");
      if (ticketFilter && ticketFilter === button._taskId) button.className = "task-chip active";
      button.addEventListener("click", function () {
        persistTicketFilter(button._taskId);
        render();
      });
      refs.taskList.appendChild(button);
    });
    refs.agent.textContent = textValue(worker.agent);
    refs.wire.textContent = textValue(worker.transport);
    if (worker.os_sandbox_requested || worker.os_sandbox_supported || worker.os_sandbox_enforced) {
      refs.wire.appendChild(el("b", null,
        " sandbox req=" + textValue(worker.os_sandbox_requested) +
        " sup=" + textValue(worker.os_sandbox_supported) +
        " enf=" + textValue(worker.os_sandbox_enforced)));
    } else if (worker.os_sandbox === "read-only") {
      refs.wire.appendChild(el("b", null, " ro"));
    }
    refs.controllerId.className = "controller-id " + controllerIdentityState(worker);
    refs.controllerId.textContent = textValue(worker.controller_display);
    var health = controllerLiveness(worker);
    refs.controllerHealth.className = "controller-health " + health.toLowerCase();
    refs.controllerHealth.textContent = health;
    var displayState = textValue(worker.display_state);
    if (worker.classification_conflict) displayState += " · conflicting authority fields";
    refs.state.textContent = displayState;
    refs.state.title = worker.authority_detail || "";
    if (worker.authority_detail) {
      refs.state.appendChild(el("small", "authority-detail", worker.authority_detail));
    }
    refs.started.textContent = ageFrom(worker.started_at, now);
    refs.host.textContent = textValue(worker.node_id);
    var isOpen = openPrompts.has(workerKey(worker));
    refs.promptToggle.textContent = isOpen ? "▾" : "▸";
    refs.promptToggle.setAttribute("aria-expanded", isOpen ? "true" : "false");
    if (worker.prompt_file) {
      if (!refs.prompt.parentNode) refs.row.appendChild(refs.prompt);
      setHidden(refs.prompt, !isOpen);
      if (isOpen) fetchPrompt(entry);
    } else {
      refs.prompt.remove();
    }
  }

  function createControllerSummary() {
    var summary = el("div", "controller-summary");
    var button = el("button", "controller-toggle");
    button.setAttribute("type", "button");
    summary.appendChild(button);
    summary._gf = { button: button, controllerKey: null };
    button.addEventListener("click", function () {
      var key = summary._gf.controllerKey;
      setControllerExpanded(key, !controllerExpanded(key, summary._gf.terminalCount));
      render();
    });
    return summary;
  }

  function updateControllerSummary(summary, key, terminal, expanded) {
    var complete = terminal.filter(function (worker) {
      return String(worker.terminal_state || worker.display_state || "").toLowerCase() === "complete";
    }).length;
    var failed = terminal.length - complete;
    var visible = expanded ? terminal.length : Math.min(3, terminal.length);
    summary._gf.controllerKey = key;
    summary._gf.terminalCount = terminal.length;
    summary._gf.button.textContent = (expanded ? "▾ " : "▸ ") + key + " · " +
      complete + " complete / " + failed + " failed / newest " + visible + " visible";
    summary._gf.button.setAttribute("aria-expanded", expanded ? "true" : "false");
  }

  function appendWorkerTable(parent, workers, now, budget, bandKey) {
    var rows = parent._gfRows;
    if (!rows) {
      rows = el("div", "rows scroll-frame");
      var header = el("div", "row-hd");
      ["", "dispatch", "agent · via", "controller", "state", "started", "host"].forEach(function (label) {
        header.appendChild(el("div", null, label));
      });
      rows.appendChild(header);
      rows._gf = { header: header, nodes: {}, quiet: null };
      parent._gfRows = rows;
      parent.appendChild(rows);
    }

    var groups = Object.create(null);
    var groupOrder = [];
    workers.forEach(function (worker) {
      var key = controllerKey(worker);
      if (!groups[key]) {
        groups[key] = { live: [], terminal: [] };
        groupOrder.push(key);
      }
      groups[key][worker.is_terminal === true ? "terminal" : "live"].push(worker);
    });
    var desired = Object.create(null);
    groupOrder.forEach(function (controller) {
      var group = groups[controller];
      group.live.forEach(function (worker) {
        desired["worker|" + workerKey(worker)] = { kind: "worker", worker: worker };
      });
      if (group.terminal.length) {
        var stateKey = bandKey + "|" + controller;
        var expanded = controllerExpanded(stateKey, group.terminal.length);
        desired["summary|" + stateKey] = {
          kind: "summary", key: stateKey, terminal: group.terminal, expanded: expanded
        };
        (expanded ? group.terminal : group.terminal.slice(0, 3)).forEach(function (worker) {
          desired["worker|" + workerKey(worker)] = { kind: "worker", worker: worker };
        });
      }
    });

    Object.keys(rows._gf.nodes).forEach(function (key) {
      if (!desired[key]) {
        rows._gf.nodes[key].remove();
        delete rows._gf.nodes[key];
      }
    });
    Object.keys(desired).forEach(function (key) {
      var item = desired[key];
      var node = rows._gf.nodes[key];
      if (item.kind === "worker") {
        budget.total += 1;
        if (budget.shown >= MAX_VISIBLE_WORKERS) {
          if (node) { node.remove(); delete rows._gf.nodes[key]; }
          return;
        }
        budget.shown += 1;
      }
      if (!node) {
        node = item.kind === "worker" ? createWorkerEntry() : createControllerSummary();
        rows._gf.nodes[key] = node;
        rows.appendChild(node);
      }
      if (item.kind === "worker") {
        setHidden(node, false);
        updateWorkerEntry(node, item.worker, now);
      } else {
        updateControllerSummary(node, item.key, item.terminal, item.expanded);
      }
      // appendChild moves an existing node, making keyed reconciliation also
      // reconcile order after recency or controller ownership changes.
      rows.appendChild(node);
    });
    if (!Object.keys(desired).length) {
      if (!rows._gf.quiet) {
        rows._gf.quiet = el("div", "quiet-state", "No worker rows match this view.");
        rows.appendChild(rows._gf.quiet);
      }
      setHidden(rows._gf.quiet, false);
    } else if (rows._gf.quiet) {
      setHidden(rows._gf.quiet, true);
    }
  }

  function renderPlaneStatus(fleetState, attentionState) {
    var host = document.getElementById("plane-status");
    if (!host) return;
    host.textContent = "";
    [["mailbox", attentionState], ["fleet", fleetState]].forEach(function (pair) {
      var row = el("div");
      row.title = pair[1].detail;
      row.appendChild(el("span", null, pair[0] + " "));
      var value = el("b", null, pair[1].label);
      if (pair[1].stale) value.setAttribute("style", "color:var(--clay)");
      row.appendChild(value);
      host.appendChild(row);
    });
    var live = document.getElementById("live-status");
    if (live) {
      var stalePlanes = [];
      if (attentionState.stale) stalePlanes.push("mailbox");
      if (fleetState.stale) stalePlanes.push("fleet");
      live.textContent = stalePlanes.length ? "Untrusted: " + stalePlanes.join(", ") : "Both data planes current";
    }
  }

  function renderMachine(fleetState) {
    var host = document.getElementById("machine");
    if (!host) return;
    if (shouldReplaceWithStale(FLEET, fleetState)) {
      replaceWithStale(host, "fleet", fleetState);
      return;
    }
    host.textContent = "";
    if (fleetState.stale && keepLastGood(FLEET)) {
      host.appendChild(staleNotice("fleet", fleetState));
    }
    var machine = FLEET.machine || {};
    var list = el("dl", "kv");
    var facts = [
      ["leases", textValue(machine.active_leases) + " / " + textValue(machine.operating_cap)],
      ["local running", textValue(machine.local_workers)],
      ["queued", textValue(machine.queue_depth)],
      ["registry sample", textValue(FLEET.registry_deep_sampled) + " / " + textValue(FLEET.registry_total)]
    ];
    if (Number(FLEET.history_excluded) > 0) {
      facts.push(["history", "+" + FLEET.history_excluded + " in history"]);
    }
    facts.forEach(function (item) {
      list.appendChild(el("dt", null, item[0]));
      list.appendChild(el("dd", null, item[1]));
    });
    host.appendChild(list);
    var unsampled = Number(FLEET.registry_unsampled);
    var omitted = FLEET.registry_unsampled_projects || [];
    if (Number.isFinite(unsampled) && unsampled > 0) {
      var omittedNames = [];
      omitted.forEach(function (row) {
        var name = omittedDisplayName(row);
        if (name) omittedNames.push(name);
      });
      var truncation = "+" + unsampled + " registered projects unsampled";
      if (omittedNames.length) truncation += " · " + omittedNames.join(", ");
      host.appendChild(el("div", "registry-truncation", truncation));
    }
    if ((machine.rate_pressure || []).length) {
      var pressure = el("div", "legend");
      machine.rate_pressure.forEach(function (row) {
        pressure.appendChild(chip("warn", textValue(row.provider), row.count));
      });
      host.appendChild(pressure);
    }
  }

  function renderVendors(fleetState, now) {
    var host = document.getElementById("vendors");
    if (!host) return;
    if (shouldReplaceWithStale(FLEET, fleetState)) {
      replaceWithStale(host, "fleet", fleetState);
      return;
    }
    host.textContent = "";
    if (fleetState.stale && keepLastGood(FLEET)) {
      host.appendChild(staleNotice("fleet", fleetState));
    }
    var vendors = FLEET.vendors || [];
    var groups = [];
    var index = Object.create(null);
    vendors.forEach(function (vendor) {
      var name = textValue(vendor.provider);
      if (!index[name]) {
        index[name] = { name: name, seats: [] };
        groups.push(index[name]);
      }
      index[name].seats.push(vendor);
    });
    if (!groups.length) host.appendChild(el("div", "quiet-state", "No provider budgets reported."));
    groups.forEach(function (group) {
      var details = el("details", "vendor" + (group.seats.length > 1 ? "" : " leaf"));
      var summary = el("summary");
      summary.appendChild(el("i", "tri"));
      summary.appendChild(el("span", "v-name", group.name));
      summary.appendChild(el("div", "v-right", group.seats.length > 1 ? group.seats.length + " seats" : ""));
      var remaining = el("div", "v-sub");
      remaining.appendChild(el("small", null, group.seats.map(function (seat) {
        return textValue(seat.remaining);
      }).join(" · ")));
      summary.appendChild(remaining);
      details.appendChild(summary);
      var body = el("div", "v-detail");
      group.seats.forEach(function (seat) {
        var row = el("div", "seat-row");
        row.appendChild(el("span", "lbl", seat.seat_index != null ? "seat " + seat.seat_index : "seat unknown"));
        row.appendChild(el("span", "who", (seat.flags || []).join(" · ")));
        row.appendChild(el("span", null, textValue(seat.remaining) +
          (seat.reset_at != null ? " · resets " + textValue(whenFrom(seat.reset_at, now)) : "")));
        body.appendChild(row);
      });
      details.appendChild(body);
      host.appendChild(details);
    });
  }

  function attentionItemKey(item) {
    return textValue(item.dispatch_id) + "|" + textValue(item.seq) + "|" + textValue(item.kind);
  }

  function createAttentionRow() {
    var row = el("div", "attn-row");
    var glyph = el("i", "glyph unknown");
    var identity = el("div");
    var dispatch = el("div", "did");
    var kind = el("div", "agent");
    identity.appendChild(dispatch);
    identity.appendChild(kind);
    var spacer = el("div");
    var headline = el("div", "state-txt");
    var waited = el("div", "waited");
    var action = el("div", "host");
    [glyph, identity, spacer, headline, waited, action].forEach(function (node) {
      row.appendChild(node);
    });
    row._gf = { glyph: glyph, dispatch: dispatch, kind: kind, headline: headline, waited: waited, action: action };
    return row;
  }

  function updateAttentionRow(row, item, now) {
    var refs = row._gf;
    var attentionGlyph = ACTIONABLE_KINDS[item.kind] === true
      ? "attn"
      : (item.kind === "advisory" ? "advisory" : "unknown");
    refs.glyph.className = "glyph " + attentionGlyph;
    refs.dispatch.textContent = textValue(item.dispatch_id);
    refs.kind.textContent = textValue(item.kind);
    refs.headline.textContent = textValue(item.headline);
    refs.waited.textContent = ageFrom(item.observed_at, now);
    refs.action.textContent = textValue(item.action);
  }

  function renderAttention(attentionState, now) {
    var host = document.getElementById("attention");
    var panel = document.getElementById("attention-section");
    if (!host) return;
    if (!host._gf) {
      host._gf = {
        header: null,
        title: null,
        count: null,
        truncation: null,
        quiet: null,
        stale: null,
        nodes: Object.create(null)
      };
    }
    var state = host._gf;
    if (shouldReplaceWithStale(ATTENTION, attentionState)) {
      if (panel) panel.className = "panel attention-section";
      Object.keys(state.nodes).forEach(function (key) {
        state.nodes[key].remove();
        delete state.nodes[key];
      });
      if (state.header) setHidden(state.header, true);
      if (state.truncation) setHidden(state.truncation, true);
      if (state.quiet) setHidden(state.quiet, true);
      if (!state.stale) {
        state.stale = staleNotice("attention", attentionState);
        host.appendChild(state.stale);
      } else {
        state.stale.remove();
        state.stale = staleNotice("attention", attentionState);
        host.appendChild(state.stale);
      }
      return;
    }
    if (attentionState.stale && keepLastGood(ATTENTION)) {
      if (!state.stale) {
        state.stale = staleNotice("attention", attentionState);
        if (host.firstChild) host.insertBefore(state.stale, host.firstChild);
        else host.appendChild(state.stale);
      }
    } else if (state.stale) {
      state.stale.remove();
      state.stale = null;
    }

    var items = (ATTENTION.items || []).slice();
    var waiting = items.filter(function (item) { return ACTIONABLE_KINDS[item.kind] === true; }).length;
    var advisories = items.filter(function (item) { return item.kind === "advisory"; }).length;
    if (panel) panel.className = "panel attention-section" + (waiting ? " attn" : "");
    if (!state.header) {
      state.header = el("div", "panel-hd");
      state.title = el("span", null, "Operator mailbox");
      state.count = el("span", "count");
      state.header.appendChild(state.title);
      state.header.appendChild(state.count);
      host.appendChild(state.header);
    }
    setHidden(state.header, false);
    state.count.textContent = waiting + " waiting" + (advisories
      ? " · " + advisories + (advisories === 1 ? " advisory" : " advisories")
      : "");
    var truncated = ATTENTION.controller_history_probes_truncated;
    var showTruncation = typeof truncated === "number" && Number.isFinite(truncated) && truncated > 0;
    if (showTruncation) {
      if (!state.truncation) {
        state.truncation = el("div", "attention-truncation");
        host.appendChild(state.truncation);
      }
      state.truncation.textContent = "+" + truncated +
        " older generations unprobed · click Show more in a project band for retained history";
      setHidden(state.truncation, false);
    } else if (state.truncation) {
      setHidden(state.truncation, true);
    }
    if (!items.length) {
      Object.keys(state.nodes).forEach(function (key) {
        state.nodes[key].remove();
        delete state.nodes[key];
      });
      if (!state.quiet) {
        state.quiet = el("div", "quiet-state", "Nothing is waiting on you.");
        host.appendChild(state.quiet);
      }
      setHidden(state.quiet, false);
      return;
    }
    if (state.quiet) setHidden(state.quiet, true);
    var desired = Object.create(null);
    items.forEach(function (item) {
      desired[attentionItemKey(item)] = item;
    });
    reconcileKeyed(
      host,
      state.nodes,
      desired,
      function () { return createAttentionRow(); },
      function (node, item) { updateAttentionRow(node, item, now); }
    );
  }

  function parseHistoryScript(text) {
    var prefix = "window.GF_HISTORY = ";
    var source = String(text || "").trim();
    var start = source.indexOf(prefix);
    if (start < 0 || source.charAt(source.length - 1) !== ";") throw new Error("history schema wrapper missing");
    var payload = JSON.parse(source.slice(start + prefix.length, -1));
    if (!payload || payload.schema !== "goalflight.fleet-console.history.v1" || !Array.isArray(payload.projects)) {
      throw new Error("history schema mismatch");
    }
    return payload;
  }

  function currentHistoryPayloadKey() {
    var count = Number((FLEET || {}).history_excluded);
    return String(Number.isFinite(count) && count > 0 ? count : 0);
  }

  function syncHistoryCacheKey() {
    var key = currentHistoryPayloadKey();
    if (historyPayloadKey !== key) {
      historyPayload = null;
      historyPromise = null;
      historyPages = Object.create(null);
      archivedHistoryPages = 0;
      historyPayloadKey = key;
    }
    return key;
  }

  function loadHistory() {
    var requestKey = syncHistoryCacheKey();
    if (historyPayload) return Promise.resolve(historyPayload);
    if (historyPromise) return historyPromise;
    if (typeof window.fetch !== "function") return Promise.reject(new Error("history fetch unavailable"));
    var request = window.fetch("./history-data.js?history_check=" + Date.now())
      .then(function (response) {
        if (!response || typeof response.text !== "function") throw new Error("history response unreadable");
        if (response.ok === false) throw new Error("history response unavailable");
        return response.text();
      })
      .then(parseHistoryScript)
      .then(function (payload) {
        if (historyPayloadKey === requestKey) historyPayload = payload;
        return payload;
      })
      .catch(function (error) {
        if (historyPromise === request) historyPromise = null;
        throw error;
      });
    historyPromise = request;
    return request;
  }

  function historyProject(projectId) {
    syncHistoryCacheKey();
    if (!historyPayload) return null;
    return (historyPayload.projects || []).filter(function (project) {
      return project.project_id === projectId;
    })[0] || null;
  }

  function historyWorkers(project) {
    var found = historyProject(project.project_id);
    var pageCount = historyPages[project.project_id] || 0;
    if (!found || pageCount <= 0) return [];
    var fastIds = {};
    (project.workers || []).forEach(function (worker) { fastIds[workerKey(worker)] = true; });
    return (found.workers || []).filter(function (worker) {
      return !fastIds[workerKey(worker)];
    }).slice(0, pageCount * historyPageSize);
  }

  function showMore(projectId) {
    return loadHistory().then(function () {
      historyPages[projectId] = (historyPages[projectId] || 0) + 1;
      render();
      return historyProject(projectId);
    });
  }

  function archivedHistoryCount() {
    var reachableInCurrentBands = (FLEET && FLEET.projects || []).reduce(function (total, project) {
      return total + Math.max(0, Number(project.history_excluded || 0));
    }, 0);
    return Math.max(0, Number((FLEET || {}).history_excluded || 0) - reachableInCurrentBands);
  }

  function allArchivedHistoryWorkers() {
    if (!historyPayload) return [];
    var currentProjectIds = Object.create(null);
    (FLEET && FLEET.projects || []).forEach(function (project) {
      currentProjectIds[textValue(project.project_id)] = true;
    });
    var workers = [];
    (historyPayload.projects || []).forEach(function (project) {
      if (!currentProjectIds[textValue(project.project_id)]) {
        workers = workers.concat(project.workers || []);
      }
    });
    return workers;
  }

  function archivedHistoryWorkers() {
    return allArchivedHistoryWorkers().slice(0, archivedHistoryPages * historyPageSize);
  }

  function showArchived() {
    return loadHistory().then(function () {
      archivedHistoryPages += 1;
      render();
      return archivedHistoryWorkers();
    });
  }

  function projectBands() {
    var result = [];
    (FLEET.projects || []).forEach(function (project) {
      var workers = (project.workers || []).concat(historyWorkers(project)).filter(displayedWorker);
      if (workers.length || ((project.queue || {}).depth || 0) > 0 || (project.history_excluded || 0) > 0) {
        result.push({ kind: "project", project: project, workers: workers });
      }
    });
    var unassigned = (FLEET.unassigned_workers || []).filter(displayedWorker);
    if (unassigned.length) result.push({ kind: "unassigned", workers: unassigned });
    var remote = (((FLEET.remote || {}).workers) || []).filter(displayedWorker);
    if (remote.length) result.push({ kind: "remote", workers: remote });
    var archivedCount = archivedHistoryCount();
    if (archivedCount > 0) {
      result.push({
        kind: "archived",
        history_excluded: archivedCount,
        workers: archivedHistoryWorkers().filter(displayedWorker)
      });
    }
    result.sort(function (left, right) {
      if (left.kind === "archived" || right.kind === "archived") {
        if (left.kind !== right.kind) return left.kind === "archived" ? 1 : -1;
      }
      var leftLive = (((left.workers || [])[0] || {}).observed_live) === true;
      var rightLive = (((right.workers || [])[0] || {}).observed_live) === true;
      if (leftLive !== rightLive) return leftLive ? -1 : 1;
      var leftValue = ((left.workers || [])[0] || {}).started_at;
      var rightValue = ((right.workers || [])[0] || {}).started_at;
      var leftStarted = typeof leftValue === "string" ? leftValue : "";
      var rightStarted = typeof rightValue === "string" ? rightValue : "";
      if (leftStarted !== rightStarted) return leftStarted > rightStarted ? -1 : 1;
      var leftId = textValue(((left.project || {}).project_id) || left.kind);
      var rightId = textValue(((right.project || {}).project_id) || right.kind);
      return leftId < rightId ? -1 : (leftId > rightId ? 1 : 0);
    });
    return result;
  }

  function repoDisplay(identity) {
    if (identity == null || identity === "") return null;
    var text = String(identity);
    if (text.indexOf("file") === 0) {
      var segs = text.split("/");
      return segs[segs.length - 1] || "local";
    }
    var parts = text.split("/");
    if (parts.length >= 3) return parts.slice(1).join("/");
    return text;
  }

  var GENERIC_PROJECT_NAMES = { project: true, proj: true };

  function omittedDisplayName(row) {
    var shown = repoDisplay(row && row.repo_identity);
    if (shown) return shown;
    var name = row && textValue(row.name);
    if (name && name !== "unknown" && !GENERIC_PROJECT_NAMES[String(name).toLowerCase()]) {
      return name;
    }
    var projectId = row && textValue(row.project_id);
    if (name && name !== "unknown" && projectId && projectId !== "unknown") {
      var parts = String(projectId).split("-");
      var suffix = parts.length ? parts[parts.length - 1] : "";
      if (suffix && suffix !== name) return name + " · " + suffix;
    }
    if (name && name !== "unknown") return name;
    return projectId && projectId !== "unknown" ? projectId : "";
  }

  function repoIdentityOf(project) {
    var identity = project && project.repo_identity;
    return typeof identity === "string" && identity ? identity : null;
  }

  function repoGroups() {
    var groups = [];
    var index = Object.create(null);
    projectBands().forEach(function (entry) {
      if (entry.kind !== "project") {
        groups.push(entry);
        return;
      }
      var project = entry.project || {};
      var identity = repoIdentityOf(project);
      var key = identity ? "repo|" + identity : "unlinked|" + textValue(project.project_id);
      if (!index[key]) {
        var group = {
          kind: "repo",
          repo_key: key,
          repo_identity: identity,
          repo_label: identity ? repoDisplay(identity) : textValue(project.name),
          unlinked: !identity,
          children: [],
          workers: []
        };
        index[key] = group;
        groups.push(group);
      }
      index[key].children.push(entry);
      index[key].workers = index[key].workers.concat(entry.workers || []);
    });
    return groups;
  }

  function disclosurePayloadKeys() {
    var workerKeys = Object.create(null);
    var controllerKeys = Object.create(null);
    var currentProjectIds = Object.create(null);
    function addRows(bandKey, workers) {
      (workers || []).forEach(function (worker) {
        if (!visibleWorker(worker)) return;
        workerKeys[workerKey(worker)] = true;
        if (worker.is_terminal === true) {
          controllerKeys[bandKey + "|" + controllerKey(worker)] = true;
        }
      });
    }
    (FLEET && FLEET.projects || []).forEach(function (project) {
      var projectId = textValue(project.project_id);
      currentProjectIds[projectId] = true;
      addRows(projectId, project.workers);
    });
    if (historyPayload) {
      (historyPayload.projects || []).forEach(function (project) {
        var projectId = textValue(project.project_id);
        if (currentProjectIds[projectId]) addRows(projectId, project.workers);
      });
      addRows("archived", archivedHistoryWorkers());
    }
    addRows("unassigned", FLEET && FLEET.unassigned_workers);
    addRows("remote", FLEET && FLEET.remote && FLEET.remote.workers);
    return { workers: workerKeys, controllers: controllerKeys };
  }

  function pruneDisclosureState() {
    var valid = disclosurePayloadKeys();
    var controllersChanged = false;
    Array.from(openControllers).forEach(function (entry) {
      var separator = entry.indexOf("|");
      var stateKey = separator < 0 ? "" : entry.slice(separator + 1);
      if (!valid.controllers[stateKey]) {
        openControllers.delete(entry);
        controllersChanged = true;
      }
    });
    if (controllersChanged) persistSet("goalflight-fleet-open-controllers", openControllers);
    var promptsChanged = false;
    Array.from(openPrompts).forEach(function (entry) {
      if (!valid.workers[entry]) {
        openPrompts.delete(entry);
        promptsChanged = true;
      }
    });
    if (promptsChanged) persistSet("goalflight-fleet-open-prompts", openPrompts);
  }

  function createBand() {
    var panel = el("section", "panel band");
    var header = el("div", "band-hd");
    var queueHost = el("div", "queue-host");
    panel.appendChild(header);
    panel._gf = { header: header, queueHost: queueHost };
    return panel;
  }

  function renderBand(entry, now, budget, panel, opts) {
    var project = entry.project || {};
    opts = opts || {};
    panel = panel || createBand();
    var header = panel._gf.header;
    header.textContent = "";
    var identity = el("div");
    var name;
    if (entry.kind === "remote") name = "Remote workers";
    else if (entry.kind === "unassigned") name = "Unassigned workers";
    else if (entry.kind === "archived") name = "Archived projects (+" + entry.history_excluded + ")";
    else if (opts.title) name = opts.title;
    else if (opts.asChild) name = textValue(project.name);
    else name = repoDisplay(project.repo_identity) || textValue(project.name);
    identity.appendChild(el("div", "proj", name));
    if (entry.kind === "project") {
      var pathBits = [];
      if (opts.unlinked || !repoIdentityOf(project)) pathBits.push("unlinked");
      else if (opts.asChild) {
        var liveCount = (entry.workers || []).filter(function (worker) {
          return displayedWorker(worker) && worker.is_terminal !== true;
        }).length;
        pathBits.push(liveCount + " live");
      } else {
        pathBits.push(project.registered ? "registered" : "unregistered");
        if (project.skill_version) pathBits.push("skill " + project.skill_version);
        if (project.name && repoDisplay(project.repo_identity) !== String(project.name)) {
          pathBits.push(textValue(project.name));
        }
      }
      identity.appendChild(el("div", "proj-path", pathBits.join(" · ")));
    } else {
      identity.appendChild(el("div", "proj-path", entry.kind === "remote" ? "remote authority" :
        (entry.kind === "archived" ? "terminal-only projects · lazy slow history" : "no measured project")));
    }
    header.appendChild(identity);
    var vitals = el("div", "vitals");
    var session = project.session || {};
    if (session.available && session.active) vitals.appendChild(chip("good", "active"));
    if (session.active_leases) vitals.appendChild(chip("", "leases", session.active_leases));
    var milestone = project.milestone || {};
    if (milestone.available && milestone.due) {
      vitals.appendChild(chip("warn", "sweep due", milestone.commits_since + "/" + milestone.cadence));
    }
    if (entry.kind === "project" && (project.history_excluded || 0) > 0) {
      var more = el("button", "ghost-btn history-more");
      more.setAttribute("type", "button");
      var found = historyProject(project.project_id);
      var loadedCount = historyWorkers(project).length;
      var totalOlder = found ? (found.workers || []).filter(function (worker) {
        return !(project.workers || []).some(function (fast) { return workerKey(fast) === workerKey(worker); });
      }).length : Number(project.history_excluded || 0);
      var remaining = Math.max(0, totalOlder - loadedCount);
      more.textContent = found && remaining === 0
        ? "+" + project.history_excluded + " in history · loaded"
        : "Show more · +" + (found ? remaining : project.history_excluded) + " in history";
      more._projectId = project.project_id;
      more.addEventListener("click", function () {
        more.textContent = "Loading history…";
        more.disabled = true;
        showMore(more._projectId).catch(function () {
          more.textContent = "History unavailable · retry";
          more.disabled = false;
        });
      });
      vitals.appendChild(more);
    }
    if (entry.kind === "archived") {
      var archivedMore = el("button", "ghost-btn history-more archived-history-more");
      archivedMore.setAttribute("type", "button");
      var archivedLoaded = archivedHistoryWorkers().length;
      var archivedTotal = historyPayload ? allArchivedHistoryWorkers().length : Number(entry.history_excluded || 0);
      var archivedRemaining = Math.max(0, archivedTotal - archivedLoaded);
      archivedMore.textContent = historyPayload && archivedRemaining === 0
        ? "+" + entry.history_excluded + " in history · loaded"
        : (archivedLoaded ? "Show more · +" + archivedRemaining + " in history" : "Open archived projects · +" + entry.history_excluded + " in history");
      archivedMore.addEventListener("click", function () {
        archivedMore.textContent = "Loading history…";
        archivedMore.disabled = true;
        showArchived().catch(function () {
          archivedMore.textContent = "History unavailable · retry";
          archivedMore.disabled = false;
        });
      });
      vitals.appendChild(archivedMore);
    }
    header.appendChild(vitals);

    var queue = project.queue || {};
    var queueHost = panel._gf.queueHost;
    queueHost.textContent = "";
    if (queue.depth) {
      if (!queueHost.parentNode) panel.appendChild(queueHost);
      var strip = el("div", "qstrip");
      strip.appendChild(el("span", "lbl", "queue"));
      (queue.lanes || []).forEach(function (lane) {
        var laneNode = el("span", "qlane");
        laneNode.appendChild(el("span", null, textValue(lane.agent)));
        laneNode.appendChild(el("b", null, textValue(lane.count)));
        var bar = el("div", "qbar");
        var fill = el("i");
        fill.setAttribute("style", "width:" + Math.round((lane.count / queue.depth) * 100) + "%");
        bar.appendChild(fill);
        laneNode.appendChild(bar);
        strip.appendChild(laneNode);
      });
      strip.appendChild(el("span", "drain", "oldest " + ageFrom(queue.oldest_created_at, now)));
      queueHost.appendChild(strip);
    } else {
      queueHost.remove();
    }
    appendWorkerTable(panel, entry.workers, now, budget, textValue(project.project_id || entry.kind));
    return panel;
  }

  function controllerPanelKey(row) {
    return textValue(row.controller_key || ((row.project_id || "") + ":" + (row.label || "")));
  }

  function createControllerPanelRow() {
    var wrap = el("div", "controller-entry");
    var row = el("div", "controller-row");
    var expand = el("button", "controller-toggle owner-expand", "▸");
    expand.setAttribute("type", "button");
    var label = el("div", "controller-label");
    var project = el("div", "controller-project");
    var health = el("div", "controller-health unknown");
    var pool = el("div", "controller-pool");
    var inflight = el("div", "controller-inflight");
    var seen = el("div", "controller-seen");
    var action = el("div", "controller-action");
    [expand, label, project, health, pool, inflight, seen, action].forEach(function (node) {
      row.appendChild(node);
    });
    var owned = el("div", "controller-owned");
    wrap.appendChild(row);
    wrap.appendChild(owned);
    wrap._gf = {
      row: row, expand: expand, label: label, project: project, health: health,
      pool: pool, inflight: inflight, seen: seen, action: action, owned: owned,
      ownedNodes: Object.create(null), ownerKey: null
    };
    expand.addEventListener("click", function () {
      var key = wrap._gf.ownerKey;
      if (!key) return;
      setOwnerExpanded(key, !ownerExpanded(key));
      render();
    });
    return wrap;
  }

  function updateControllerPanelRow(wrap, item, now) {
    var refs = wrap._gf;
    var state = textValue(item.controller_liveness_state);
    if (CONTROLLER_LIVENESS_STATES[state] !== true) state = "UNKNOWN";
    var key = controllerPanelKey(item);
    refs.ownerKey = key;
    refs.label.textContent = textValue(item.label);
    refs.project.textContent = textValue(item.parent_name || item.project_name);
    refs.health.className = "controller-health " + state.toLowerCase();
    refs.health.textContent = state;
    var live = item.listener_live;
    var target = item.listener_target;
    refs.pool.textContent = (live == null ? "?" : String(live)) +
      "/" + (target == null ? "?" : String(target));
    var ownedCount = item.owned_live == null ? item.in_flight_count : item.owned_live;
    refs.inflight.textContent = ownedCount == null ? "0" : String(ownedCount);
    refs.seen.textContent = ageFrom(item.last_seen, now);
    refs.action.textContent = "";
    if (state === "UNKNOWN") {
      if (item.last_error) {
        refs.action.appendChild(el("div", "controller-error", textValue(item.last_error)));
      }
      if (item.probe_command) {
        refs.action.appendChild(el("div", "controller-probe", String(item.probe_command)));
      }
    } else if (item.retire_command) {
      refs.action.textContent = String(item.retire_command);
    }
    refs.action.title = refs.action.textContent || "";
    wrap.setAttribute("data-controller-label", textValue(item.label));
    refs.row.className = "controller-row" + (state === "DEAD" ? " dead" : "");
    var canExpand = state !== "DEAD";
    var expanded = canExpand && ownerExpanded(key);
    refs.expand.textContent = expanded ? "▾" : "▸";
    refs.expand.setAttribute("aria-expanded", expanded ? "true" : "false");
    setHidden(refs.expand, !canExpand);
    if (!canExpand || !expanded) {
      setHidden(refs.owned, true);
      return;
    }
    var groups = ownedWorkerGroups(item.label);
    var desired = Object.create(null);
    if (!groups.length) {
      desired.quiet = { kind: "quiet" };
    } else {
      groups.forEach(function (group) {
        desired["group|" + group.title] = { kind: "group", group: group };
      });
    }
    var ownerBudget = { shown: 0, total: 0 };
    reconcileKeyed(
      refs.owned,
      refs.ownedNodes,
      desired,
      createOwnedGroup,
      function (node, desiredItem) {
        updateOwnedGroup(node, desiredItem, now, key, ownerBudget);
      }
    );
    setHidden(refs.owned, false);
  }

  function createOwnedGroup(item) {
    if (item.kind === "quiet") {
      return el("div", "quiet-state", "No displayed workers attributed to this label.");
    }
    var block = el("div", "owned-group");
    var title = el("div", "owned-title");
    block.appendChild(title);
    block._gf = { title: title };
    return block;
  }

  function updateOwnedGroup(block, item, now, ownerKey, ownerBudget) {
    if (item.kind === "quiet") {
      block.textContent = "No displayed workers attributed to this label.";
      return;
    }
    block._gf.title.textContent = item.group.title + " · " + item.group.workers.length;
    appendWorkerTable(
      block,
      item.group.workers,
      now,
      ownerBudget || { shown: 0, total: 0 },
      "owner|" + ownerKey + "|" + item.group.title
    );
  }

  var openControllerOwners = storedSet("goalflight-fleet-open-controller-owners");

  function ownerExpanded(key) {
    return openControllerOwners.has("open|" + key);
  }

  function setOwnerExpanded(key, expanded) {
    openControllerOwners.delete("open|" + key);
    openControllerOwners.delete("closed|" + key);
    openControllerOwners.add((expanded ? "open|" : "closed|") + key);
    persistSet("goalflight-fleet-open-controller-owners", openControllerOwners);
  }

  function workerOwnerLabel(worker) {
    var label = worker && worker.controller_label;
    return typeof label === "string" && label ? label : null;
  }

  function ownedWorkerGroups(label) {
    var wanted = String(label || "");
    var matchUnowned = wanted === "unowned";
    var groups = [];
    function collect(project, workers) {
      var matched = (workers || []).filter(function (worker) {
        if (!displayedWorker(worker)) return false;
        var owner = workerOwnerLabel(worker);
        return matchUnowned ? owner == null : owner === wanted;
      });
      if (!matched.length) return;
      var repoName = repoDisplay(project.repo_identity);
      var checkout = textValue(project.worktree_name || project.name || project.parent_name);
      var title = repoName ? repoName + " / " + checkout : checkout + " (unlinked)";
      groups.push({ title: title, workers: matched });
    }
    (FLEET && FLEET.projects || []).forEach(function (project) {
      collect(project, project.workers);
    });
    collect({ name: "unassigned" }, FLEET && FLEET.unassigned_workers);
    collect({ name: "remote" }, FLEET && FLEET.remote && FLEET.remote.workers);
    return groups;
  }

  function unlabeledDisplayedWorkers() {
    var rows = [];
    function add(workers) {
      (workers || []).forEach(function (worker) {
        if (displayedWorker(worker) && workerOwnerLabel(worker) == null) rows.push(worker);
      });
    }
    (FLEET && FLEET.projects || []).forEach(function (project) {
      add(project.workers);
    });
    add(FLEET && FLEET.unassigned_workers);
    add(FLEET && FLEET.remote && FLEET.remote.workers);
    return rows;
  }

  function unownedControllerRow() {
    var workers = unlabeledDisplayedWorkers();
    if (!workers.length) return null;
    var liveCount = 0;
    workers.forEach(function (worker) {
      if (worker.is_terminal !== true) liveCount += 1;
    });
    return {
      controller_key: "unowned",
      label: "unowned",
      project_id: null,
      project_name: "no recorded owner",
      parent_project_id: null,
      parent_name: "no recorded owner",
      controller_liveness_state: "UNKNOWN",
      listener_live: null,
      listener_target: null,
      wake_mode: null,
      in_flight_count: liveCount,
      owned_live: liveCount,
      last_seen: null,
      generation: null,
      retire_command: null,
      last_error: null,
      probe_command: null
    };
  }

  function deadControllersExpanded() {
    return openDeadControllers.has("open");
  }

  function setDeadControllersExpanded(expanded) {
    openDeadControllers.clear();
    if (expanded) openDeadControllers.add("open");
    persistSet("goalflight-fleet-open-dead-controllers", openDeadControllers);
  }

  function renderControllers(fleetState, now) {
    var section = document.getElementById("fleet-section");
    if (!section) return;
    if (!section._gfCtl) {
      var panel = el("section", "panel controllers-panel");
      var header = el("div", "panel-hd");
      var title = el("span", null, "Controllers");
      var count = el("span", "count");
      header.appendChild(title);
      header.appendChild(count);
      var liveHost = el("div", "controller-live");
      var deadToggle = el("button", "controller-toggle dead-controllers-toggle");
      deadToggle.setAttribute("type", "button");
      deadToggle.addEventListener("click", function () {
        setDeadControllersExpanded(!deadControllersExpanded());
        render();
      });
      var deadHost = el("div", "controller-dead");
      var quiet = el("div", "quiet-state", "No registered controllers in this sample.");
      panel.appendChild(header);
      panel.appendChild(liveHost);
      panel.appendChild(deadToggle);
      panel.appendChild(deadHost);
      panel.appendChild(quiet);
      section._gfCtl = {
        panel: panel, header: header, count: count, liveHost: liveHost,
        deadToggle: deadToggle, deadHost: deadHost, quiet: quiet,
        liveNodes: Object.create(null), deadNodes: Object.create(null)
      };
      if (section.firstChild) section.insertBefore(panel, section.firstChild);
      else section.appendChild(panel);
    }
    var ctl = section._gfCtl;
    if (shouldReplaceWithStale(FLEET, fleetState)) {
      setHidden(ctl.liveHost, true);
      setHidden(ctl.deadToggle, true);
      setHidden(ctl.deadHost, true);
      setHidden(ctl.quiet, true);
      if (!ctl.stale) {
        ctl.stale = staleNotice("fleet", fleetState);
        ctl.panel.appendChild(ctl.stale);
      }
      return;
    }
    if (ctl.stale) {
      ctl.stale.remove();
      ctl.stale = null;
    }
    var rows = (FLEET && FLEET.controllers) || [];
    var live = [];
    var dead = [];
    rows.forEach(function (row) {
      if (!row || typeof row !== "object") return;
      if (textValue(row.controller_liveness_state) === "DEAD") dead.push(row);
      else live.push(row);
    });
    var hasUnownedLabel = live.concat(dead).some(function (row) {
      return textValue(row.label) === "unowned";
    });
    var unowned = hasUnownedLabel ? null : unownedControllerRow();
    if (unowned) live.push(unowned);
    ctl.count.textContent = live.length + " live" + (dead.length ? " · " + dead.length + " dead" : "");
    if (!live.length && !dead.length) {
      Object.keys(ctl.liveNodes).forEach(function (key) {
        ctl.liveNodes[key].remove();
        delete ctl.liveNodes[key];
      });
      Object.keys(ctl.deadNodes).forEach(function (key) {
        ctl.deadNodes[key].remove();
        delete ctl.deadNodes[key];
      });
      setHidden(ctl.liveHost, true);
      setHidden(ctl.deadToggle, true);
      setHidden(ctl.deadHost, true);
      setHidden(ctl.quiet, false);
      return;
    }
    setHidden(ctl.quiet, true);
    setHidden(ctl.liveHost, false);
    var liveDesired = Object.create(null);
    live.forEach(function (row) { liveDesired[controllerPanelKey(row)] = row; });
    reconcileKeyed(
      ctl.liveHost,
      ctl.liveNodes,
      liveDesired,
      function () { return createControllerPanelRow(); },
      function (node, item) { updateControllerPanelRow(node, item, now); }
    );
    if (!dead.length) {
      Object.keys(ctl.deadNodes).forEach(function (key) {
        ctl.deadNodes[key].remove();
        delete ctl.deadNodes[key];
      });
      setHidden(ctl.deadToggle, true);
      setHidden(ctl.deadHost, true);
      return;
    }
    var expanded = deadControllersExpanded();
    ctl.deadToggle.textContent = (expanded ? "▾ " : "▸ ") + dead.length +
      " dead · retire leftover labels";
    ctl.deadToggle.setAttribute("aria-expanded", expanded ? "true" : "false");
    setHidden(ctl.deadToggle, false);
    if (!expanded) {
      Object.keys(ctl.deadNodes).forEach(function (key) {
        setHidden(ctl.deadNodes[key], true);
      });
      setHidden(ctl.deadHost, true);
      return;
    }
    setHidden(ctl.deadHost, false);
    var deadDesired = Object.create(null);
    dead.forEach(function (row) { deadDesired[controllerPanelKey(row)] = row; });
    reconcileKeyed(
      ctl.deadHost,
      ctl.deadNodes,
      deadDesired,
      function () { return createControllerPanelRow(); },
      function (node, item) {
        setHidden(node, false);
        updateControllerPanelRow(node, item, now);
      }
    );
  }

  function checkoutHasLiveWork(entry) {
    return (entry.workers || []).some(function (worker) {
      return displayedWorker(worker) && worker.is_terminal !== true;
    });
  }

  function idleCheckoutsExpanded(key) {
    return openIdleCheckouts.has("open|" + key);
  }

  function setIdleCheckoutsExpanded(key, expanded) {
    openIdleCheckouts.delete("open|" + key);
    openIdleCheckouts.delete("closed|" + key);
    openIdleCheckouts.add((expanded ? "open|" : "closed|") + key);
    persistSet("goalflight-fleet-open-idle-checkouts", openIdleCheckouts);
  }

  function renderRepo(entry, now, budget, panel) {
    var children = entry.children || [];
    if (children.length === 1) {
      return renderBand(children[0], now, budget, panel, {
        title: entry.unlinked ? textValue((children[0].project || {}).name) : entry.repo_label,
        unlinked: entry.unlinked === true,
        asChild: false
      });
    }
    panel = panel || createBand();
    var header = panel._gf.header;
    header.textContent = "";
    var identity = el("div");
    identity.appendChild(el("div", "proj", textValue(entry.repo_label)));
    identity.appendChild(el("div", "proj-path", children.length + " checkouts"));
    header.appendChild(identity);
    if (!panel._gf.childHost) {
      panel._gf.childHost = el("div", "repo-checkouts repo-worktrees");
      panel._gf.childBands = Object.create(null);
      panel.appendChild(panel._gf.childHost);
    }
    var live = [];
    var idle = [];
    children.forEach(function (child) {
      if (checkoutHasLiveWork(child)) live.push(child);
      else idle.push(child);
    });
    var desired = Object.create(null);
    function place(child, host) {
      var key = textValue(((child.project || {}).project_id) || child.kind);
      desired[key] = child;
      if (!panel._gf.childBands[key]) {
        panel._gf.childBands[key] = createBand();
      }
      renderBand(child, now, budget, panel._gf.childBands[key], { asChild: true });
      host.appendChild(panel._gf.childBands[key]);
    }
    live.forEach(function (child) { place(child, panel._gf.childHost); });
    if (idle.length) {
      if (!panel._gf.idleToggle) {
        panel._gf.idleToggle = el("button", "ghost-btn collapsed-checkouts-toggle");
        panel._gf.idleToggle.setAttribute("type", "button");
        panel._gf.idleHost = el("div", "repo-collapsed-checkouts");
        panel._gf.idleToggle.addEventListener("click", function () {
          setIdleCheckoutsExpanded(entry.repo_key, !idleCheckoutsExpanded(entry.repo_key));
          render();
        });
      }
      var expanded = idleCheckoutsExpanded(entry.repo_key);
      panel._gf.idleToggle.textContent = (expanded ? "▾ " : "▸ ") + idle.length +
        " idle checkout" + (idle.length === 1 ? "" : "s");
      panel._gf.idleToggle.setAttribute("aria-expanded", expanded ? "true" : "false");
      panel._gf.childHost.appendChild(panel._gf.idleToggle);
      panel._gf.childHost.appendChild(panel._gf.idleHost);
      setHidden(panel._gf.idleHost, !expanded);
      if (expanded) {
        idle.forEach(function (child) { place(child, panel._gf.idleHost); });
      }
    } else if (panel._gf.idleToggle) {
      panel._gf.idleToggle.remove();
      panel._gf.idleHost.remove();
      panel._gf.idleToggle = null;
      panel._gf.idleHost = null;
    }
    Object.keys(panel._gf.childBands).forEach(function (key) {
      if (!desired[key]) {
        panel._gf.childBands[key].remove();
        delete panel._gf.childBands[key];
      }
    });
    return panel;
  }

  function renderFleet(fleetState, now) {
    var host = document.getElementById("fleet");
    if (!host) return;
    if (shouldReplaceWithStale(FLEET, fleetState)) {
      host.textContent = "";
      bandNodes = Object.create(null);
      host._gf = null;
      host.appendChild(staleNotice("fleet", fleetState));
      return;
    }
    if (fleetState.stale && keepLastGood(FLEET)) {
      if (!host._gfLastGood) {
        host._gfLastGood = staleNotice("fleet", fleetState);
        if (host.firstChild) host.insertBefore(host._gfLastGood, host.firstChild);
        else host.appendChild(host._gfLastGood);
      }
    } else if (host._gfLastGood) {
      host._gfLastGood.remove();
      host._gfLastGood = null;
    }
    var entries = repoGroups();
    var ageSummary = workerAgeSummary();
    if (!host._gf) host._gf = { quiet: null, overflow: null, ticket: null };
    if (ticketFilter) {
      if (!host._gf.ticket) {
        host._gf.ticket = el("button", "ghost-btn ticket-filter-clear");
        host._gf.ticket.setAttribute("type", "button");
        host._gf.ticket.addEventListener("click", function () { persistTicketFilter(null); render(); });
        host.appendChild(host._gf.ticket);
      }
      host._gf.ticket.textContent = "Ticket " + ticketFilter + " · clear filter";
      setHidden(host._gf.ticket, false);
    } else if (host._gf.ticket) {
      host._gf.ticket.textContent = "";
      setHidden(host._gf.ticket, true);
    }
    if (!entries.length) {
      Object.keys(bandNodes).forEach(function (key) { bandNodes[key].remove(); delete bandNodes[key]; });
      if (!host._gf.quiet) {
        host._gf.quiet = el("div", "quiet-state");
        host.appendChild(host._gf.quiet);
      }
      host._gf.quiet.textContent = ageSummary.hidden ?
        "No recent active or unresolved worker rows. " + ageSummary.hidden + " older rows hidden by age filter." :
        "No project has active workers, retained history, or queued work in this view.";
      setHidden(host._gf.quiet, false);
      if (host._gf.overflow) setHidden(host._gf.overflow, true);
      return;
    }
    if (host._gf.quiet) setHidden(host._gf.quiet, true);
    var budget = { shown: 0, total: 0 };
    var desiredBands = Object.create(null);
    entries.slice(0, MAX_VISIBLE_BANDS).forEach(function (entry) {
      var key = entry.kind === "repo"
        ? textValue(entry.repo_key)
        : textValue(((entry.project || {}).project_id) || entry.kind);
      desiredBands[key] = true;
      if (!bandNodes[key]) {
        bandNodes[key] = createBand();
      }
      if (entry.kind === "repo") renderRepo(entry, now, budget, bandNodes[key]);
      else renderBand(entry, now, budget, bandNodes[key]);
      host.appendChild(bandNodes[key]);
    });
    Object.keys(bandNodes).forEach(function (key) {
      if (!desiredBands[key]) { bandNodes[key].remove(); delete bandNodes[key]; }
    });
    entries.slice(MAX_VISIBLE_BANDS).forEach(function (entry) { budget.total += entry.workers.length; });
    if (budget.shown < budget.total || entries.length > MAX_VISIBLE_BANDS) {
      if (!host._gf.overflow) {
        host._gf.overflow = el("div", "overflow-state");
        host.appendChild(host._gf.overflow);
      }
      host._gf.overflow.textContent = "Showing " + budget.shown + " of " + budget.total + " active or unresolved worker rows" +
        (entries.length > MAX_VISIBLE_BANDS ? " across " + MAX_VISIBLE_BANDS + " of " + entries.length + " groups" : "") + ".";
      setHidden(host._gf.overflow, false);
      host.appendChild(host._gf.overflow);
    } else if (host._gf.overflow) {
      setHidden(host._gf.overflow, true);
    }
  }

  function ageSignature(now) {
    var fleetState = planeState(FLEET, SCHEMAS.fleet, CADENCES.fleet, now);
    var attentionState = planeState(ATTENTION, SCHEMAS.attention, CADENCES.attention, now);
    var values = [fleetState.label, attentionState.label];
    if (!attentionState.stale || keepLastGood(ATTENTION)) {
      (ATTENTION.items || []).forEach(function (item) { values.push(ageFrom(item.observed_at, now)); });
    }
    if (!fleetState.stale || keepLastGood(FLEET)) {
      (FLEET.controllers || []).forEach(function (row) { values.push(ageFrom(row.last_seen, now)); });
      (FLEET.vendors || []).forEach(function (vendor) { values.push(whenFrom(vendor.reset_at, now)); });
      var remainingWorkers = MAX_VISIBLE_WORKERS;
      /* Age work follows the same visible limits as DOM work: at most 50 groups
       * and 200 workers per tick. The caps are counts (unitless); a payload of
       * 300 groups × 5 workers still parses ages for only min(1,500, 200) = 200
       * displayed workers, so hidden overflow cannot grow the one-second poll. */
      projectBands().slice(0, MAX_VISIBLE_BANDS).forEach(function (entry) {
        var queue = (entry.project || {}).queue || {};
        if (queue.depth) values.push(ageFrom(queue.oldest_created_at, now));
        var visibleWorkers = entry.workers.slice(0, remainingWorkers);
        remainingWorkers -= visibleWorkers.length;
        visibleWorkers.forEach(function (worker) {
          values.push(ageFrom(worker.started_at, now));
        });
      });
    }
    return values.join("|");
  }

  function render(nowMs) {
    var now = nowMs != null ? nowMs : Date.now();
    syncHistoryCacheKey();
    pruneDisclosureState();
    var fleetState = planeState(FLEET, SCHEMAS.fleet, CADENCES.fleet, now);
    var attentionState = planeState(ATTENTION, SCHEMAS.attention, CADENCES.attention, now);
    renderPlaneStatus(fleetState, attentionState);
    renderAgeFilterControl(fleetState);
    renderMachine(fleetState);
    renderVendors(fleetState, now);
    renderAttention(attentionState, now);
    renderControllers(fleetState, now);
    renderFleet(fleetState, now);
    lastAgeSignature = ageSignature(now);
  }

  function reloadPlane(name) {
    if (reloadPending[name]) return;
    var config = name === "attention"
      ? { file: "./attention-data.js", global: "GF_ATTENTION" }
      : { file: "./fleet-data.js", global: "GF_FLEET" };
    reloadPending[name] = true;
    window[config.global] = null;
    var script = document.createElement("script");
    script.src = config.file + "?generation_check=" + Date.now();
    var finished = false;
    /* A reload gets one UI polling interval to settle. If the repeating
     * interval fires before this watchdog it skips once, the watchdog releases
     * the plane, and the following interval retries. Freshness remains governed
     * only by the payload's producer-stamped cadence. */
    var watchdog = window.setTimeout(function () { finish(false); }, CADENCES[name]);
    function finish(ok) {
      if (finished) return;
      finished = true;
      window.clearTimeout(watchdog);
      var payload = ok ? window[config.global] : null;
      if (name === "attention") ATTENTION = payload;
      else FLEET = payload;
      reloadPending[name] = false;
      script.remove();
      render();
    }
    script.addEventListener("load", function () { finish(true); });
    script.addEventListener("error", function () { finish(false); });
    document.head.appendChild(script);
  }

  function recomputeAges() {
    var now = Date.now();
    if (ageSignature(now) !== lastAgeSignature) render(now);
  }

  function normalizeTheme(mode) {
    return mode === "dark" || mode === "light" ? mode : "auto";
  }

  function applyTheme(mode, persist) {
    themeMode = normalizeTheme(mode);
    if (themeMode === "auto") document.documentElement.removeAttribute("data-theme");
    else document.documentElement.setAttribute("data-theme", themeMode);
    var button = document.getElementById("theme-toggle");
    if (button) button.textContent = "Theme: " + themeMode;
    if (persist !== false) {
      persistValue("goalflight-fleet-theme", themeMode);
    }
    return themeMode;
  }

  function initializeTheme() {
    var saved = "auto";
    try { saved = window.localStorage.getItem("goalflight-fleet-theme") || "auto"; } catch (_error) { /* optional */ }
    applyTheme(saved, false);
    var button = document.getElementById("theme-toggle");
    if (button) {
      button.addEventListener("click", function () {
        applyTheme(themeMode === "auto" ? "dark" : (themeMode === "dark" ? "light" : "auto"));
      });
    }
  }

  function applyAgeFilter(enabled, persist) {
    ageFilterEnabled = enabled === true;
    if (persist !== false) {
      persistValue("goalflight-fleet-age-filter", ageFilterEnabled ? "hide" : "show");
    }
    return ageFilterEnabled;
  }

  function initializeAgeFilter() {
    var policy = FLEET && FLEET.worker_age_filter;
    var initial = !policy || policy.default_enabled !== false;
    try {
      var saved = window.localStorage.getItem("goalflight-fleet-age-filter");
      if (saved === "hide") initial = true;
      else if (saved === "show") initial = false;
    } catch (_error) { /* optional */ }
    applyAgeFilter(initial, false);
    var button = document.getElementById("age-filter-toggle");
    if (button) {
      button.addEventListener("click", function () {
        applyAgeFilter(!ageFilterEnabled);
        render();
      });
    }
  }

  window.GFFleetConsole = {
    ageBucket: ageFrom,
    freshnessLimitMs: freshnessLimitMs,
    planeState: planeState,
    applyTheme: applyTheme,
    applyAgeFilter: applyAgeFilter,
    render: render,
    reloadPlane: reloadPlane,
    setFleetData: function (payload) {
      FLEET = payload;
      window.GF_FLEET = payload;
      render();
      return payload;
    },
    setTicketFilter: function (taskId) {
      persistTicketFilter(taskId == null ? null : String(taskId));
      render();
      return ticketFilter;
    },
    setAttentionData: function (payload) {
      ATTENTION = payload;
      window.GF_ATTENTION = payload;
      render();
      return payload;
    },
    showMore: showMore,
    showArchived: showArchived,
    openPrompt: function (dispatchId) {
      var wanted = String(dispatchId);
      var found = null;
      Object.keys(bandNodes).some(function (bandKey) {
        var rows = (bandNodes[bandKey]._gfRows || {})._gf;
        if (!rows) return false;
        return Object.keys(rows.nodes).some(function (key) {
          var node = rows.nodes[key];
          if (node._gf && node._gf.worker && String(node._gf.worker.dispatch_id) === wanted) {
            found = node;
            return true;
          }
          return false;
        });
      });
      return found ? setPromptOpen(found, true) : Promise.resolve(null);
    },
    rowNode: function (dispatchId) {
      var wanted = String(dispatchId);
      var found = null;
      Object.keys(bandNodes).some(function (bandKey) {
        var rows = (bandNodes[bandKey]._gfRows || {})._gf;
        if (!rows) return false;
        return Object.keys(rows.nodes).some(function (key) {
          var node = rows.nodes[key];
          if (node._gf && node._gf.worker && String(node._gf.worker.dispatch_id) === wanted) {
            found = node;
            return true;
          }
          return false;
        });
      });
      return found;
    },
    schemas: SCHEMAS,
    maxVisibleWorkers: MAX_VISIBLE_WORKERS
  };

  initializeTheme();
  initializeAgeFilter();
  initializeTicketFilter();
  render();
  window.setInterval(function () { reloadPlane("attention"); }, CADENCES.attention);
  window.setInterval(function () { reloadPlane("fleet"); }, CADENCES.fleet);
  /* One 1,000 ms poll per second means a stale boundary is reflected at most
   * one second late; the signature guard avoids rebuilding unchanged buckets. */
  window.setInterval(recomputeAges, 1000);
})();
