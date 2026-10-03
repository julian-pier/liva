(async function () {
  "use strict";
  const token = document.getElementById("local-token");
  const endpoint = document.getElementById("endpoint");
  const status = document.getElementById("status");
  const saved = await chrome.storage.local.get({ localToken: "", endpoint: "http://127.0.0.1:8765/browser-context" });
  token.value = saved.localToken;
  endpoint.value = saved.endpoint;
  document.getElementById("save").addEventListener("click", async () => {
    const parsed = new URL(endpoint.value);
    if (parsed.protocol !== "http:" || parsed.hostname !== "127.0.0.1") { status.textContent = "Nur 127.0.0.1 ist erlaubt."; return; }
    if (token.value.trim().length < 24) { status.textContent = "Token ist zu kurz."; return; }
    await chrome.storage.local.set({ localToken: token.value.trim(), endpoint: parsed.toString() });
    status.textContent = "Gespeichert.";
  });
})();
