importScripts("domain.js");

const ENDPOINT = "http://127.0.0.1:8765/browser-context";
const browserName = /OPR\//.test(navigator.userAgent) ? "opera" : (/Edg\//.test(navigator.userAgent) ? "edge" : "chrome");
let lastPayloadKey = "";
const mediaPlayingByTab = new Map();

async function settings() {
  return chrome.storage.local.get({ localToken: "", endpoint: ENDPOINT });
}

async function currentState() {
  const windows = await chrome.windows.getAll({ populate: true, windowTypes: ["normal"] });
  const timestamp = new Date().toISOString();
  const dashboardOpen = windows.some((browserWindow) =>
    (browserWindow.tabs || []).some((tab) => LivaActivityDomain.isActivityDashboard(tab.url))
  );
  return {
    browser: browserName,
    timestamp,
    dashboard_open: dashboardOpen,
    contexts: windows.map((browserWindow) => {
      const tab = (browserWindow.tabs || []).find((candidate) => candidate.active);
      return {
        window_id: browserWindow.id,
        focused: Boolean(browserWindow.focused),
        domain: LivaActivityDomain.normalizeDomain(tab && tab.url),
        page_title: tab && tab.title ? String(tab.title).slice(0, 1024) : null,
        audible: Boolean(tab && tab.audible && !(tab.mutedInfo && tab.mutedInfo.muted)),
        media_playing: Boolean(tab && (tab.audible || mediaPlayingByTab.get(tab.id))),
        timestamp
      };
    })
  };
}

async function publish(force) {
  try {
    const config = await settings();
    if (!config.localToken) return;
    const payload = await currentState();
    const key = JSON.stringify(payload);
    if (!force && key === lastPayloadKey) return;
    const response = await fetch(config.endpoint || ENDPOINT, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-LIVA-Activity-Token": config.localToken },
      body: JSON.stringify(payload)
    });
    if (response.ok) lastPayloadKey = key;
  } catch (_) {
    // The Windows agent may be offline. A future browser event retries naturally.
  }
}

chrome.tabs.onActivated.addListener(() => publish(true));
chrome.tabs.onUpdated.addListener((_tabId, changeInfo, tab) => {
  if (changeInfo.url !== undefined) mediaPlayingByTab.delete(tab.id);
  if (tab.active && (changeInfo.url !== undefined || changeInfo.title !== undefined || changeInfo.audible !== undefined || changeInfo.mutedInfo !== undefined || changeInfo.status === "complete")) publish(false);
});
chrome.tabs.onRemoved.addListener((tabId) => { mediaPlayingByTab.delete(tabId); publish(true); });
chrome.windows.onFocusChanged.addListener(() => publish(true));
chrome.windows.onRemoved.addListener(() => publish(true));
chrome.runtime.onStartup.addListener(() => {
  chrome.alarms.create("liva-activity-presence", { periodInMinutes: 0.5 });
  publish(true);
});
chrome.runtime.onInstalled.addListener(() => {
  chrome.alarms.create("liva-activity-presence", { periodInMinutes: 0.5 });
  publish(true);
});
chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name === "liva-activity-presence") publish(true);
});
chrome.runtime.onMessage.addListener((message, sender) => {
  if (message && message.type === "liva-media-state" && sender.tab && sender.tab.id !== undefined) {
    mediaPlayingByTab.set(sender.tab.id, Boolean(message.playing));
    publish(true);
  }
});
chrome.storage.onChanged.addListener((_changes, area) => { if (area === "local") publish(true); });
