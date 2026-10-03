(function () {
  const cfg = window.TEST_UI_CONFIG || null;
  if (!cfg || !cfg.active || !window.fetch) return;

  const originalFetch = window.fetch.bind(window);
  const dryRunEl = document.getElementById("test-mode-dry-run-message");

  function showDryRunMessage(message) {
    if (!dryRunEl) return;
    dryRunEl.textContent = message || "Testmodus: keine echte Änderung gespeichert.";
    dryRunEl.hidden = false;
    dryRunEl.classList.add("is-visible");
    window.clearTimeout(showDryRunMessage._timer);
    showDryRunMessage._timer = window.setTimeout(() => {
      dryRunEl.classList.remove("is-visible");
      dryRunEl.hidden = true;
      dryRunEl.textContent = "";
    }, 3200);
  }

  window.fetch = async function testModeFetch(input, init) {
    const response = await originalFetch(input, init);
    try {
      const cloned = response.clone();
      const payload = await cloned.json();
      if (payload && payload.test_mode && payload.dry_run && payload.blocked_write) {
        showDryRunMessage(payload.message);
      }
    } catch (_) {
      // non-json response
    }
    return response;
  };
})();
