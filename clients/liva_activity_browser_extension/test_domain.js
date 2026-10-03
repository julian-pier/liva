const assert = require("node:assert/strict");
const { normalizeDomain, isActivityDashboard } = require("./domain.js");

assert.equal(normalizeDomain("https://www.Example.COM/path?q=secret#fragment"), "example.com");
assert.equal(normalizeDomain("https://sub.example.com:8443/a"), "sub.example.com");
assert.equal(normalizeDomain("chrome://settings"), null);
assert.equal(normalizeDomain("not a url"), null);
assert.equal(isActivityDashboard("https://liva.example.com/activity"), true);
assert.equal(isActivityDashboard("https://liva.example.com/activity/"), true);
assert.equal(isActivityDashboard("https://liva.example.com/hrv"), false);
console.log("domain normalization: ok");
