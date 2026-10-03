"use strict";

const assert = require("node:assert/strict");
const { reconcileCounter, periodBounds, visualClusterEvents } = require("../static/js/activity.js");

// While activity is still presumed to be running, the local clock may move
// smoothly ahead of the most recent server checkpoint.
assert.equal(reconcileCounter(179, 175, 1, true), 180);

// Once idle is authoritative, the optimistic three-minute lookback must be
// discarded instead of being kept by a monotonic maximum.
assert.equal(reconcileCounter(180, 0, 0.2, false), 0);
assert.equal(reconcileCounter(540, 360, 0.2, false), 360);

// A later active checkpoint can still move the counter forward immediately.
assert.equal(reconcileCounter(360, 365, 0.2, true), 365);

assert.deepEqual(periodBounds("2026-08-17", 7), { start: "2026-08-11", end: "2026-08-17" });
assert.deepEqual(periodBounds("2026-08-17", 30), { start: "2026-07-19", end: "2026-08-17" });
assert.deepEqual(periodBounds("2026-08-17", 1, "2026-08-03"), { start: "2026-08-03", end: "2026-08-17" });

const clustered = visualClusterEvents([
  { started_at: "2026-08-17T10:00:00Z", ended_at: "2026-08-17T10:01:00Z", duration_seconds: 60, app_display_name: "Opera", display_name: "YouTube", event_count: 1 },
  { started_at: "2026-08-17T10:01:00Z", ended_at: "2026-08-17T10:02:00Z", duration_seconds: 60, app_display_name: "Opera", display_name: "LIVA", event_count: 1 },
], "foreground");
assert.equal(clustered.length, 1);
assert.equal(clustered[0].visual_name, "PC aktiv");
assert.equal(clustered[0].duration_seconds, 120);

console.log("activity UI counter reconciliation: ok");
