(function () {
  "use strict";
  let lastState = null;

  function report() {
    const playing = Array.from(document.querySelectorAll("video, audio")).some((media) =>
      !media.paused && !media.ended && media.readyState >= 2
    );
    if (playing === lastState) return;
    lastState = playing;
    chrome.runtime.sendMessage({ type: "liva-media-state", playing }).catch(() => {});
  }

  ["play", "playing", "pause", "ended", "emptied", "abort"].forEach((eventName) =>
    document.addEventListener(eventName, report, true)
  );
  document.addEventListener("visibilitychange", report, true);
  new MutationObserver(report).observe(document.documentElement, { childList: true, subtree: true });
  // Restores the Boolean after an MV3 service-worker restart. No page data is sent.
  window.setInterval(() => { lastState = null; report(); }, 15000);
  report();
})();
