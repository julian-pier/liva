
(function () {

  let started = false;
  let lastSeen = null;
  let timer = null;
  let inFlight = null;
  let visibilityBound = false;

  function getCfg() {
    return window.LIVE_CONFIG || null;
  }

  async function checkUpdates(cfg) {
    if (document.hidden) return;
    if (inFlight) return inFlight;
    inFlight = (async () => {
    try {
      const r = await fetch("/api/last_update", {
        cache: "no-store",
        credentials: "same-origin",
      });
      if (!r.ok) throw new Error(`last_update ${r.status}`);
      const data = await r.json();

      if (lastSeen && data.last_update > lastSeen) {
        if (typeof cfg.onUpdate === "function") {
          cfg.onUpdate();
        } else if (cfg.reload) {
          location.reload();
        }
      }

      lastSeen = data.last_update;
    } catch (e) {
      console.warn("live update failed", e);
    } finally {
      inFlight = null;
    }
    })();
    return inFlight;
  }

  function stopTimer() {
    if (!timer) return;
    clearTimeout(timer);
    timer = null;
  }

  function scheduleNext(cfg) {
    stopTimer();
    if (document.hidden) return;
    const interval = Math.max(10000, Number(cfg?.interval) || 4000);
    timer = setTimeout(async () => {
      await checkUpdates(cfg);
      scheduleNext(cfg);
    }, interval);
  }

  function startTimer(cfg) {
    scheduleNext(cfg);
  }

  function bindVisibility(cfg) {
    if (visibilityBound) return;
    visibilityBound = true;
    document.addEventListener("visibilitychange", () => {
      if (document.hidden) {
        stopTimer();
        return;
      }
      checkUpdates(cfg).finally(() => startTimer(cfg));
    });
    window.addEventListener("pageshow", () => {
      if (document.hidden) return;
      checkUpdates(cfg).finally(() => startTimer(cfg));
    });
    window.addEventListener("pagehide", () => {
      stopTimer();
    });
  }

  function startIfConfigured() {
    if (started) return true;
    const cfg = getCfg();
    if (!cfg) return false; // Seite will keine Live-Updates

    started = true;
    bindVisibility(cfg);
    startTimer(cfg);
    // initial check (damit "neue Daten gerade eben" nicht erst nach interval kommen)
    checkUpdates(cfg);
    return true;
  }

  document.addEventListener("DOMContentLoaded", () => {
    if (startIfConfigured()) return;

    // Falls LIVE_CONFIG erst durch Page-Skripte gesetzt wird (die nach live.js laden).
    let tries = 0;
    const maxTries = 80; // ~20s bei 250ms
    const t = setInterval(() => {
      tries += 1;
      if (startIfConfigured() || tries >= maxTries) {
        clearInterval(t);
      }
    }, 250);
  });

})();
