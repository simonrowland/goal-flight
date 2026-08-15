/* Goal Flight fleet console — presentation over producer-owned verdicts. */
(function () {
  "use strict";

  var SCHEMAS = {
    fleet: "goalflight.fleet-console.fleet.v2",
    attention: "goalflight.fleet-console.attention.v1"
  };
  /* Each mirror reloads once per producer cadence. Seconds × 1,000 ms/s gives
   * 5 × 1,000 = 5,000 ms for attention and 60 × 1,000 = 60,000 ms for fleet;
   * one interval therefore observes the next scheduled publication without
   * coupling the fast mailbox plane to the slower fleet sample. */
  var CADENCES = { attention: 5000, fleet: 60000 };
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

  function whenFrom(value, now) {
    var parsed = typeof value === "number" ? value * 1000 : parseTs(value);
    if (parsed == null || isNaN(parsed)) return null;
    var mins = Math.round((parsed - now) / 60000);
    if (mins <= 0) return "now";
    if (mins < 60) return "in " + mins + " min";
    var hours = Math.round(mins / 60);
    return hours < 48 ? "in " + hours + " h" : "in " + Math.round(hours / 24) + " d";
  }

  /* Stale after two missed producer cadences. Units: cadence ms × 2 = ms;
   * 5 s attention becomes stale at 10 s, while 60 s fleet becomes stale at
   * 120 s. A sample one millisecond younger remains usable. */
  function planeState(payload, schema, cadenceMs, now) {
    var present = payload && typeof payload === "object" && !Array.isArray(payload);
    var data = present ? payload : {};
    var schemaMatches = present && data.schema === schema;
    /* A mismatched schema does not authorize even the familiar-looking
     * metadata fields. Treat their values as absent instead of using an
     * unrecognised payload to manufacture a trustworthy observation age. */
    var started = schemaMatches ? parseTs(data.sample_started_at) : null;
    var finished = schemaMatches ? parseTs(data.sample_finished_at) : null;
    var success = schemaMatches ? parseTs(data.last_success_at) : null;
    var issue = null;

    if (!present) issue = "payload absent";
    else if (!schemaMatches) issue = "schema mismatch";
    else if (data.last_error) issue = "producer error";
    else if (started == null || finished == null || success == null) issue = "timestamp missing";
    else if (started - now > CLOCK_TOLERANCE_MS || finished - now > CLOCK_TOLERANCE_MS ||
             success - now > CLOCK_TOLERANCE_MS) issue = "clock ahead";
    else if (finished < started) issue = "timestamp order invalid";
    else if (typeof data.generation_id !== "string" || !data.generation_id) issue = "generation missing";

    var age = success == null ? null : Math.max(0, now - success);
    var stale = issue !== null || age >= cadenceMs * 2;
    var observed = schemaMatches ? data.last_success_at : null;
    return {
      stale: stale,
      freshnessIssue: issue,
      label: stale ? (issue || "stale " + ageFrom(observed, now)) : "live",
      detail: ((schemaMatches && data.producer && data.producer.plane) || "plane") +
        " · last success " + ageFrom(observed, now) +
        (schemaMatches && data.generation_id ? " · " + data.generation_id : "") +
        (schemaMatches && data.last_error ? " · last error: " + data.last_error : ""),
      lastObservedAge: ageFrom(observed, now),
      lastError: schemaMatches ? data.last_error : null
    };
  }

  function staleNotice(plane, state) {
    var notice = el("div", "stale-state");
    notice.appendChild(el("strong", null, "STALE · " + plane + " plane"));
    notice.appendChild(el("span", null, "Last observed " + state.lastObservedAge));
    notice.appendChild(el("span", null, "Reason: " + (state.freshnessIssue || state.label)));
    if (state.lastError) notice.appendChild(el("span", null, "Last error: " + state.lastError));
    return notice;
  }

  function replaceWithStale(host, plane, state) {
    if (!host) return;
    host.textContent = "";
    host.appendChild(staleNotice(plane, state));
  }

  function visibleWorker(worker) {
    return worker && typeof worker === "object" && worker.is_terminal !== true;
  }

  function displayedWorker(worker) {
    return visibleWorker(worker) && (!ageFilterEnabled || worker.age_filter_match !== true);
  }

  function workerAgeSummary() {
    var rows = [];
    (FLEET.projects || []).forEach(function (project) {
      rows = rows.concat(project.workers || []);
    });
    rows = rows.concat(FLEET.unassigned_workers || [], ((FLEET.remote || {}).workers) || []);
    var summary = { matches: 0, hidden: 0, unknown: 0 };
    rows.filter(visibleWorker).forEach(function (worker) {
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
    if (fleetState.stale) {
      button.textContent = "Older rows: unavailable";
      button.setAttribute("aria-pressed", ageFilterEnabled ? "true" : "false");
      note.textContent = "Fleet data stale; age filter not applied.";
      return;
    }
    var summary = workerAgeSummary();
    button.textContent = "Non-terminal / unresolved >" + ageThresholdLabel() + ": " +
      (ageFilterEnabled ? "hidden · " + summary.hidden + " hidden" : "shown");
    button.setAttribute("aria-pressed", ageFilterEnabled ? "true" : "false");
    note.textContent = "Observed live first · otherwise newest started · unknown start time stays visible.";
  }

  function glyphFor(worker) {
    if (worker.classification_conflict) return "unknown";
    return DISPLAY_GLYPHS[worker.display_state] || "unknown";
  }

  function appendWorkerTable(parent, workers, now, budget) {
    var rows = el("div", "rows scroll-frame");
    if (!workers.length) {
      rows.appendChild(el("div", "quiet-state", "No active or unresolved worker rows."));
      parent.appendChild(rows);
      return;
    }

    var header = el("div", "row-hd");
    ["", "dispatch", "agent · via", "controller", "state", "started", "host"].forEach(function (label) {
      header.appendChild(el("div", null, label));
    });
    rows.appendChild(header);

    workers.forEach(function (worker) {
      budget.total += 1;
      if (budget.shown >= MAX_VISIBLE_WORKERS) return;
      budget.shown += 1;
      var row = el("div", "row");
      row.appendChild(el("i", "glyph " + glyphFor(worker)));
      row.appendChild(el("div", "did", textValue(worker.dispatch_id)));
      var identity = el("div");
      identity.appendChild(el("div", "agent", textValue(worker.agent)));
      var wire = el("div", "wire", textValue(worker.transport));
      if (worker.os_sandbox_requested || worker.os_sandbox_supported || worker.os_sandbox_enforced) {
        wire.appendChild(el("b", null,
          " sandbox req=" + textValue(worker.os_sandbox_requested) +
          " sup=" + textValue(worker.os_sandbox_supported) +
          " enf=" + textValue(worker.os_sandbox_enforced)));
      } else if (worker.os_sandbox === "read-only") {
        wire.appendChild(el("b", null, " ro"));
      }
      identity.appendChild(wire);
      row.appendChild(identity);
      var controller = el("div", "controller-cell");
      controller.appendChild(el(
        "div",
        "controller-id " + controllerIdentityState(worker),
        textValue(worker.controller_display)
      ));
      var controllerState = controllerLiveness(worker);
      controller.appendChild(el(
        "div",
        "controller-health " + controllerState.toLowerCase(),
        controllerState
      ));
      row.appendChild(controller);
      var displayState = textValue(worker.display_state);
      if (worker.classification_conflict) displayState += " · conflicting authority fields";
      row.appendChild(el("div", "state-txt", displayState));
      row.appendChild(el("div", "host", ageFrom(worker.started_at, now)));
      row.appendChild(el("div", "host", textValue(worker.node_id)));
      rows.appendChild(row);
    });
    parent.appendChild(rows);
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
    if (fleetState.stale) {
      replaceWithStale(host, "fleet", fleetState);
      return;
    }
    host.textContent = "";
    var machine = FLEET.machine || {};
    var list = el("dl", "kv");
    [
      ["leases", textValue(machine.active_leases) + " / " + textValue(machine.operating_cap)],
      ["local running", textValue(machine.local_workers)],
      ["queued", textValue(machine.queue_depth)],
      ["registry sample", textValue(FLEET.registry_deep_sampled) + " / " + textValue(FLEET.registry_total)]
    ].forEach(function (item) {
      list.appendChild(el("dt", null, item[0]));
      list.appendChild(el("dd", null, item[1]));
    });
    host.appendChild(list);
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
    if (fleetState.stale) {
      replaceWithStale(host, "fleet", fleetState);
      return;
    }
    host.textContent = "";
    var vendors = FLEET.vendors || [];
    var groups = [];
    var index = {};
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

  function renderAttention(attentionState, now) {
    var host = document.getElementById("attention");
    var panel = document.getElementById("attention-section");
    if (!host) return;
    host.textContent = "";
    if (attentionState.stale) {
      if (panel) panel.className = "panel attention-section";
      host.appendChild(staleNotice("attention", attentionState));
      return;
    }

    var items = (ATTENTION.items || []).slice();
    var waiting = items.filter(function (item) { return ACTIONABLE_KINDS[item.kind] === true; }).length;
    var advisories = items.filter(function (item) { return item.kind === "advisory"; }).length;
    if (panel) panel.className = "panel attention-section" + (waiting ? " attn" : "");
    var header = el("div", "panel-hd");
    header.appendChild(el("span", null, "Operator mailbox"));
    var count = waiting + " waiting" + (advisories
      ? " · " + advisories + (advisories === 1 ? " advisory" : " advisories")
      : "");
    header.appendChild(el("span", "count", count));
    host.appendChild(header);
    var truncated = ATTENTION.controller_history_probes_truncated;
    if (typeof truncated === "number" && Number.isFinite(truncated) && truncated > 0) {
      host.appendChild(el(
        "div",
        "attention-truncation",
        "+" + truncated + " older generations unprobed"
      ));
    }
    if (!items.length) {
      host.appendChild(el("div", "quiet-state", "Nothing is waiting on you."));
      return;
    }
    items.forEach(function (item) {
      var row = el("div", "attn-row");
      var attentionGlyph = ACTIONABLE_KINDS[item.kind] === true
        ? "attn"
        : (item.kind === "advisory" ? "advisory" : "unknown");
      row.appendChild(el("i", "glyph " + attentionGlyph));
      var identity = el("div");
      identity.appendChild(el("div", "did", textValue(item.dispatch_id)));
      identity.appendChild(el("div", "agent", textValue(item.kind)));
      row.appendChild(identity);
      row.appendChild(el("div"));
      row.appendChild(el("div", "state-txt", textValue(item.headline)));
      row.appendChild(el("div", "waited", ageFrom(item.observed_at, now)));
      row.appendChild(el("div", "host", textValue(item.action)));
      host.appendChild(row);
    });
  }

  function projectBands() {
    var result = [];
    (FLEET.projects || []).forEach(function (project) {
      var workers = (project.workers || []).filter(displayedWorker);
      if (workers.length || ((project.queue || {}).depth || 0) > 0) {
        result.push({ kind: "project", project: project, workers: workers });
      }
    });
    var unassigned = (FLEET.unassigned_workers || []).filter(displayedWorker);
    if (unassigned.length) result.push({ kind: "unassigned", workers: unassigned });
    var remote = (((FLEET.remote || {}).workers) || []).filter(displayedWorker);
    if (remote.length) result.push({ kind: "remote", workers: remote });
    result.sort(function (left, right) {
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

  function renderBand(entry, now, budget) {
    var project = entry.project || {};
    var panel = el("section", "panel band");
    var header = el("div", "band-hd");
    var identity = el("div");
    var name = entry.kind === "remote" ? "Remote workers" :
      (entry.kind === "unassigned" ? "Unassigned workers" : textValue(project.name));
    identity.appendChild(el("div", "proj", name));
    if (entry.kind === "project") {
      identity.appendChild(el("div", "proj-path", (project.registered ? "registered" : "unregistered") +
        (project.skill_version ? " · skill " + project.skill_version : "")));
    } else {
      identity.appendChild(el("div", "proj-path", entry.kind === "remote" ? "remote authority" : "no measured project"));
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
    header.appendChild(vitals);
    panel.appendChild(header);

    var queue = project.queue || {};
    if (queue.depth) {
      var strip = el("div", "qstrip");
      strip.appendChild(el("span", "lbl", "queue"));
      (queue.lanes || []).forEach(function (lane) {
        var laneNode = el("span", "qlane");
        laneNode.appendChild(el("span", null, textValue(lane.agent)));
        laneNode.appendChild(el("b", null, textValue(lane.count)));
        var bar = el("div", "qbar");
        var fill = el("i");
        /* Lane share is count ÷ total depth × 100. Workers cancel, leaving
         * percent; 5 ÷ 10 × 100 = 50%, a midpoint sanity check. */
        fill.setAttribute("style", "width:" + Math.round((lane.count / queue.depth) * 100) + "%");
        bar.appendChild(fill);
        laneNode.appendChild(bar);
        strip.appendChild(laneNode);
      });
      strip.appendChild(el("span", "drain", "oldest " + ageFrom(queue.oldest_created_at, now)));
      panel.appendChild(strip);
    }
    appendWorkerTable(panel, entry.workers, now, budget);
    return panel;
  }

  function renderFleet(fleetState, now) {
    var host = document.getElementById("fleet");
    if (!host) return;
    host.textContent = "";
    if (fleetState.stale) {
      host.appendChild(staleNotice("fleet", fleetState));
      return;
    }
    var entries = projectBands();
    var ageSummary = workerAgeSummary();
    if (!entries.length) {
      host.appendChild(el("div", "quiet-state", ageSummary.hidden ?
        "No recent active or unresolved worker rows. " + ageSummary.hidden + " older rows hidden by age filter." :
        "No project has active workers or queued work."));
      return;
    }
    var budget = { shown: 0, total: 0 };
    entries.slice(0, MAX_VISIBLE_BANDS).forEach(function (entry) {
      host.appendChild(renderBand(entry, now, budget));
    });
    entries.slice(MAX_VISIBLE_BANDS).forEach(function (entry) {
      budget.total += entry.workers.length;
    });
    if (budget.shown < budget.total || entries.length > MAX_VISIBLE_BANDS) {
      host.appendChild(el("div", "overflow-state",
        "Showing " + budget.shown + " of " + budget.total + " active or unresolved worker rows" +
        (entries.length > MAX_VISIBLE_BANDS ? " across " + MAX_VISIBLE_BANDS + " of " + entries.length + " groups" : "") + "."));
    }
  }

  function ageSignature(now) {
    var fleetState = planeState(FLEET, SCHEMAS.fleet, CADENCES.fleet, now);
    var attentionState = planeState(ATTENTION, SCHEMAS.attention, CADENCES.attention, now);
    var values = [fleetState.label, attentionState.label];
    if (!attentionState.stale) {
      (ATTENTION.items || []).forEach(function (item) { values.push(ageFrom(item.observed_at, now)); });
    }
    if (!fleetState.stale) {
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
    var fleetState = planeState(FLEET, SCHEMAS.fleet, CADENCES.fleet, now);
    var attentionState = planeState(ATTENTION, SCHEMAS.attention, CADENCES.attention, now);
    renderPlaneStatus(fleetState, attentionState);
    renderAgeFilterControl(fleetState);
    renderMachine(fleetState);
    renderVendors(fleetState, now);
    renderAttention(attentionState, now);
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
    /* A reload gets one producer cadence to settle. Cadence ms is already the
     * timer unit; if the repeating interval fires before this watchdog it skips
     * once, the watchdog releases the plane, and the following interval retries.
     * Worst case is therefore two cadences: 2 × 5 s = 10 s for attention and
     * 2 × 60 s = 120 s for fleet, exactly when the old sample becomes stale. */
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
      try { window.localStorage.setItem("goalflight-fleet-theme", themeMode); } catch (_error) { /* optional */ }
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
      try { window.localStorage.setItem("goalflight-fleet-age-filter", ageFilterEnabled ? "hide" : "show"); } catch (_error) { /* optional */ }
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
    planeState: planeState,
    applyTheme: applyTheme,
    applyAgeFilter: applyAgeFilter,
    render: render,
    reloadPlane: reloadPlane,
    schemas: SCHEMAS,
    maxVisibleWorkers: MAX_VISIBLE_WORKERS
  };

  initializeTheme();
  initializeAgeFilter();
  render();
  window.setInterval(function () { reloadPlane("attention"); }, CADENCES.attention);
  window.setInterval(function () { reloadPlane("fleet"); }, CADENCES.fleet);
  /* One 1,000 ms poll per second means a stale boundary is reflected at most
   * one second late; the signature guard avoids rebuilding unchanged buckets. */
  window.setInterval(recomputeAges, 1000);
})();
