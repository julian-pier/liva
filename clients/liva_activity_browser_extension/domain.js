(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.LivaActivityDomain = api;
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";
  function normalizeDomain(rawUrl) {
    try {
      const parsed = new URL(String(rawUrl || ""));
      if (parsed.protocol !== "http:" && parsed.protocol !== "https:") return null;
      let host = parsed.hostname.toLowerCase().replace(/\.$/, "");
      if (host.startsWith("www.")) host = host.slice(4);
      return host || null;
    } catch (_) {
      return null;
    }
  }
  function isActivityDashboard(rawUrl) {
    try {
      const parsed = new URL(String(rawUrl || ""));
      if (parsed.protocol !== "http:" && parsed.protocol !== "https:") return false;
      return parsed.pathname === "/activity" || parsed.pathname.startsWith("/activity/");
    } catch (_) {
      return false;
    }
  }
  return { normalizeDomain, isActivityDashboard };
});
