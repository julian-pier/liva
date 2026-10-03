"use strict";

const assert = require("node:assert/strict");
const { buildProfile, formatDuration, formatPace, targetFor } = require("../static/js/planning_workout_chart.js");

assert.equal(formatPace(278), "4:38 /km");
assert.equal(formatPace(342.7), "5:43 /km");
assert.equal(formatPace(0), "—");
assert.equal(formatDuration(963), "16:03");

const steps = [
  { kind: "warmup", duration_s: 600, target: { pace_min_s_per_km: 330, pace_max_s_per_km: 360 }, resolved: { estimated_max_hr_bpm: 190, internal_hr_min_pct: 65, internal_hr_max_pct: 72 } },
  { kind: "work", duration_s: 60, repeat_index: 1, repeat_count: 2, target: { pace_min_s_per_km: 255, pace_max_s_per_km: 265, rpe_min: 7, rpe_max: 8 }, resolved: { estimated_max_hr_bpm: 190, internal_hr_min_pct: 86, internal_hr_max_pct: 92 } },
  { kind: "recovery", duration_s: 120, repeat_index: 1, repeat_count: 2, target: { pace_s_per_km: 390 }, resolved: { estimated_max_hr_bpm: 190, internal_hr_min_pct: 60, internal_hr_max_pct: 70 } },
  { kind: "work", duration_s: 60, repeat_index: 2, repeat_count: 2, target: { pace_min_s_per_km: 255, pace_max_s_per_km: 265, rpe_min: 7, rpe_max: 8 }, resolved: { estimated_max_hr_bpm: 190, internal_hr_min_pct: 86, internal_hr_max_pct: 92 } },
  { kind: "cooldown", duration_s: 540, target: { pace_min_s_per_km: 340, pace_max_s_per_km: 370 }, resolved: { estimated_max_hr_bpm: 190, internal_hr_min_pct: 62, internal_hr_max_pct: 70 } },
];

const pace = buildProfile(steps, "pace");
assert.equal(pace.total, 1380);
assert.equal(pace.sections.length, 5);
assert.equal(pace.points.length, 10);
assert.ok(pace.low < 255 && pace.high > 390);
assert.equal(pace.sections[1].start, 600);
assert.equal(pace.sections[1].end, 660);

const hr = buildProfile(steps, "hr");
assert.equal(hr.sections[1].target.low, 163);
assert.equal(hr.sections[1].target.high, 175);
assert.ok(hr.low >= 50 && hr.high <= 190);

const rpe = buildProfile(steps, "rpe");
assert.equal(rpe.low, 1);
assert.equal(rpe.high, 10);
assert.equal(rpe.sections[1].target.value, 7.5);

assert.equal(targetFor({ target: {}, resolved: {} }, "pace", 190), null);
assert.equal(buildProfile([{ kind: "open", duration_s: 60, target: {} }], "pace").points.length, 0);

console.log("planned workout chart profile: ok");
