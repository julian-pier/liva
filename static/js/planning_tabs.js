(function () {
  const root = document.getElementById("planning-page");
  if (!root) return;

  const STORAGE_KEY = "planning_tab";
  const allTabButtons = Array.from(document.querySelectorAll("[data-planning-tab]"));
  const paneGym = document.getElementById("planning-pane-gym");
  const paneCardio = document.getElementById("planning-pane-cardio");
  const paneNutrition = document.getElementById("planning-pane-nutrition");
  const libraryTabs = Array.from(root.querySelectorAll("[data-library-tab]"));
  const libraryPanels = Array.from(root.querySelectorAll("[data-library-panel]"));

  if (!paneGym || !paneCardio || !paneNutrition || allTabButtons.length === 0) return;
  const nutritionButtons = allTabButtons.filter((btn) => btn.dataset.planningTab === "nutrition");
  const isNutritionLocked = nutritionButtons.some(
    (btn) => btn.hasAttribute("disabled") || btn.getAttribute("aria-disabled") === "true",
  );

  function setBtnState(btn, active) {
    btn.classList.toggle("is-active", active);
    btn.setAttribute("aria-selected", active ? "true" : "false");
  }

  function setPaneState(pane, active) {
    pane.classList.toggle("hidden", !active);
    pane.setAttribute("aria-hidden", active ? "false" : "true");
  }

  function setTab(tab) {
    const requested = ["gym", "cardio", "nutrition"].includes(tab) ? tab : "gym";
    const t = (requested === "nutrition" && isNutritionLocked) ? "gym" : requested;
    localStorage.setItem(STORAGE_KEY, t);

    allTabButtons.forEach((btn) => {
      const tabName = btn.dataset.planningTab;
      setBtnState(btn, tabName === t);
    });
    setPaneState(paneGym, t === "gym");
    setPaneState(paneCardio, t === "cardio");
    setPaneState(paneNutrition, t === "nutrition");
    root.classList.toggle("is-cardio-active", t === "cardio");

    window.dispatchEvent(new CustomEvent("planning:tab", { detail: { tab: t } }));
  }

  allTabButtons.forEach((btn) => {
    btn.addEventListener("click", () => {
      const tabName = btn.dataset.planningTab;
      if (tabName === "nutrition" && isNutritionLocked) return;
      setTab(tabName);
    });
  });

  function setLibraryTab(tab) {
    const selected = tab === "foods" ? "foods" : "meals";
    libraryTabs.forEach((btn) => {
      const active = btn.dataset.libraryTab === selected;
      btn.classList.toggle("is-active", active);
      btn.setAttribute("aria-selected", active ? "true" : "false");
    });
    libraryPanels.forEach((panel) => {
      const active = panel.dataset.libraryPanel === selected;
      panel.classList.toggle("is-library-hidden", !active);
      panel.setAttribute("aria-hidden", active ? "false" : "true");
    });
  }

  libraryTabs.forEach((btn) => {
    btn.addEventListener("click", () => setLibraryTab(btn.dataset.libraryTab));
  });
  if (libraryTabs.length && libraryPanels.length) setLibraryTab("meals");

  const params = new URLSearchParams(window.location.search);
  const forced = params.get("tab");
  const saved = localStorage.getItem(STORAGE_KEY) || "gym";
  if (["nutrition", "gym", "cardio"].includes(forced)) {
    setTab(forced);
  } else {
    setTab(saved);
  }
})();
