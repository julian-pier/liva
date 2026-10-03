(function () {
  "use strict";

  const STORAGE_KEY = "liva.sidebarCollapsed";
  const TABLET_STORAGE_KEY = "liva.sidebarCollapsed.tablet";
  const DESKTOP_STORAGE_KEY = "liva.sidebarCollapsed.desktop";
  const SIDEBAR_QUERY = window.matchMedia("(min-width: 641px)");
  const DESKTOP_QUERY = window.matchMedia("(min-width: 1200px)");
  const MOBILE_QUERY = window.matchMedia("(max-width: 640px)");

  const icons = {
    home: '<path d="m3 10 9-7 9 7v10a1 1 0 0 1-1 1h-5v-7H9v7H4a1 1 0 0 1-1-1Z"/>',
    database: '<ellipse cx="12" cy="5" rx="9" ry="3"/><path d="M3 5V19A9 3 0 0 0 21 19V5"/><path d="M3 12A9 3 0 0 0 21 12"/>',
    utensils: '<path d="M3 2v7c0 1.1.9 2 2 2h4a2 2 0 0 0 2-2V2"/><path d="M7 2v20"/><path d="M21 15V2a5 5 0 0 0-5 5v6c0 1.1.9 2 2 2h3Zm0 0v7"/>',
    dumbbell: '<path d="M17.596 12.768a2 2 0 1 0 2.829-2.829l-1.768-1.767a2 2 0 0 0 2.828-2.829l-2.828-2.828a2 2 0 0 0-2.829 2.828l-1.767-1.768a2 2 0 1 0-2.829 2.829z"/><path d="m2.5 21.5 1.4-1.4"/><path d="m20.1 3.9 1.4-1.4"/><path d="M5.343 21.485a2 2 0 1 0 2.829-2.828l1.767 1.768a2 2 0 1 0 2.829-2.829l-6.364-6.364a2 2 0 1 0-2.829 2.829l1.768 1.767a2 2 0 0 0-2.828 2.829z"/><path d="m9.6 14.4 4.8-4.8"/>',
    heart: '<path d="M2 9.5a5.5 5.5 0 0 1 9.591-3.676.56.56 0 0 0 .818 0A5.49 5.49 0 0 1 22 9.5c0 2.29-1.5 4-3 5.5l-5.492 5.313a2 2 0 0 1-3 .019L5 15c-1.5-1.5-3-3.2-3-5.5"/><path d="M3.22 13H9.5l.5-1 2 4.5 2-7 1.5 3.5h5.27"/>',
    route: '<circle cx="6" cy="19" r="3"/><path d="M9 19h8.5a3.5 3.5 0 0 0 0-7h-11a3.5 3.5 0 0 1 0-7H15"/><circle cx="18" cy="5" r="3"/>',
    calendar: '<path d="M8 2v4"/><path d="M16 2v4"/><rect width="18" height="18" x="3" y="4" rx="2"/><path d="M3 10h18"/><path d="M8 14h.01"/><path d="M12 14h.01"/><path d="M16 14h.01"/><path d="M8 18h.01"/><path d="M12 18h.01"/><path d="M16 18h.01"/>',
    chart: '<path d="M3 3v16a2 2 0 0 0 2 2h16"/><path d="M18 17V9"/><path d="M13 17V5"/><path d="M8 17v-3"/>',
    monitor: '<path d="M15.033 9.44a.647.647 0 0 1 0 1.12l-4.065 2.352a.645.645 0 0 1-.968-.56V7.648a.645.645 0 0 1 .967-.56z"/><path d="M12 17v4"/><path d="M8 21h8"/><rect x="2" y="3" width="20" height="14" rx="2"/>',
    settings: '<path d="M9.671 4.136a2.34 2.34 0 0 1 4.659 0 2.34 2.34 0 0 0 3.319 1.915 2.34 2.34 0 0 1 2.33 4.033 2.34 2.34 0 0 0 0 3.831 2.34 2.34 0 0 1-2.33 4.033 2.34 2.34 0 0 0-3.319 1.915 2.34 2.34 0 0 1-4.659 0 2.34 2.34 0 0 0-3.32-1.915 2.34 2.34 0 0 1-2.33-4.033 2.34 2.34 0 0 0 0-3.831A2.34 2.34 0 0 1 6.35 6.051a2.34 2.34 0 0 0 3.319-1.915"/><circle cx="12" cy="12" r="3"/>',
    chevron: '<path d="m15 18-6-6 6-6"/>',
    more: '<circle cx="5" cy="12" r="1.25" fill="currentColor" stroke="none"/><circle cx="12" cy="12" r="1.25" fill="currentColor" stroke="none"/><circle cx="19" cy="12" r="1.25" fill="currentColor" stroke="none"/>',
    close: '<path d="m18 6-12 12"/><path d="m6 6 12 12"/>'
  };

  const groups = [
    [
      { label: "MFP", path: "/mfp", icon: "database", prefixes: ["/mfp", "/meals"] }
    ],
    [
      { label: "Training", path: "/training", icon: "dumbbell", prefixes: ["/training"] },
      { label: "Recovery", path: "/hrv", icon: "heart", prefixes: ["/hrv"] },
      { label: "Cardio", path: "/cardio", icon: "route", prefixes: ["/cardio", "/runs"] }
    ],
    [
      { label: "Planung", path: "/planung", icon: "calendar", prefixes: ["/planung"] },
      { label: "Auswertung", path: "/auswertung", icon: "chart", prefixes: ["/auswertung"] }
    ],
    [
      { label: "Remote", path: "/remote", icon: "monitor", prefixes: ["/remote"] },
      { label: "System", path: "/settings", icon: "settings", prefixes: ["/settings"] }
    ]
  ];

  const mobilePrimary = [
    { label: "Training", path: "/training", icon: "dumbbell", prefixes: ["/training"] },
    { label: "Ernährung", path: "/mfp", icon: "database", prefixes: ["/mfp", "/meals"] },
    { label: "Heute", path: "/", icon: "home", brand: true, matches: ["/", "/dashboard"] },
    { label: "Auswertung", path: "/auswertung", icon: "chart", prefixes: ["/auswertung"] }
  ];

  const mobileOverflow = [
    { label: "Cardio", detail: "Ausdauer & Läufe", path: "/cardio", icon: "route", prefixes: ["/cardio", "/runs"] },
    { label: "Planung", detail: "Training & Ernährung", path: "/planung", icon: "calendar", prefixes: ["/planung"] },
    { label: "Recovery", detail: "HRV & Readiness", path: "/hrv", icon: "heart", prefixes: ["/hrv"] },
    { label: "Remote", detail: "Externe Steuerung", path: "/remote", icon: "monitor", prefixes: ["/remote"] },
    { label: "System", detail: "Einstellungen", path: "/settings", icon: "settings", prefixes: ["/settings"] }
  ];

  function svg(name) {
    return `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${icons[name]}</svg>`;
  }

  function localHref(path) {
    if (!path.startsWith("/")) return path;
    const current = new URL(window.location.href);
    const target = new URL(path, current.origin);
    if (current.searchParams.get("test_mode") === "1") target.searchParams.set("test_mode", "1");
    return `${target.pathname}${target.search}`;
  }

  function isActive(item) {
    const path = window.location.pathname;
    if (item.matches) return item.matches.includes(path);
    return (item.prefixes || []).some((prefix) => path.startsWith(prefix));
  }

  function buildSidebar() {
    const aside = document.createElement("aside");
    aside.className = "desktop-sidebar";
    aside.setAttribute("aria-label", "Desktop Navigation");
    aside.hidden = true;
    aside.setAttribute("aria-hidden", "true");
    aside.setAttribute("inert", "");
    const navigation = groups.map((group) => `
      <div class="sidebar-section">
        ${group.map((item) => `<a href="${localHref(item.path)}" class="sidebar-link${isActive(item) ? " is-active" : ""}" title="${item.label}"${item.external ? ' target="_blank" rel="noopener noreferrer"' : ""}${isActive(item) ? ' aria-current="page"' : ""}><span class="sidebar-link-icon">${svg(item.icon)}</span><span class="sidebar-label">${item.label}</span></a>`).join("")}
      </div>`).join("");
    aside.innerHTML = `
      <div class="desktop-sidebar-inner">
        <div class="sidebar-header">
          <a href="${localHref("/")}" class="sidebar-brand" aria-label="LIVA Dashboard">
            <span class="sidebar-brand-mark"><img src="/static/img/brand/liva-peak-sidebar.svg?v=liva-peak-1" alt="" aria-hidden="true"></span>
            <span class="sidebar-label">LIVA</span>
          </a>
        </div>
        <nav class="desktop-sidebar-nav" aria-label="Hauptnavigation" data-testid="sidebar-navigation">${navigation}</nav>
        <div class="desktop-sidebar-foot">
          <button class="desktop-sidebar-toggle" type="button" aria-label="Sidebar einklappen" title="Sidebar einklappen">
            <span class="sidebar-link-icon">${svg("chevron")}</span><span class="sidebar-label">Sidebar einklappen</span>
          </button>
        </div>
      </div>`;
    return aside;
  }

  function buildMobileNavigation() {
    const overflowActive = mobileOverflow.some(isActive);
    const shell = document.createElement("div");
    shell.className = "mobile-dock-shell";
    shell.innerHTML = `
      <button class="mobile-dock-backdrop" type="button" aria-label="Navigation schließen" hidden></button>
      <section class="mobile-dock-sheet" id="mobile-dock-sheet" role="dialog" aria-modal="true" aria-labelledby="mobile-dock-sheet-title" hidden>
        <header class="mobile-dock-sheet-head">
          <div>
            <span class="mobile-dock-sheet-kicker">Navigation</span>
            <h2 id="mobile-dock-sheet-title">Weitere Bereiche</h2>
          </div>
          <button class="mobile-dock-close" type="button" aria-label="Navigation schließen">${svg("close")}</button>
        </header>
        <nav class="mobile-dock-sheet-grid" aria-label="Weitere Bereiche">
          ${mobileOverflow.map((item) => `<a href="${localHref(item.path)}" class="mobile-dock-sheet-link${isActive(item) ? " is-active" : ""}"${item.external ? ' target="_blank" rel="noopener noreferrer"' : ""}${isActive(item) ? ' aria-current="page"' : ""}><span class="mobile-dock-sheet-icon">${svg(item.icon)}</span><span class="mobile-dock-sheet-copy"><strong>${item.label}</strong><small>${item.detail}</small></span></a>`).join("")}
        </nav>
      </section>
      <nav class="mobile-bottom-nav" aria-label="Hauptnavigation" data-testid="mobile-bottom-navigation">
        ${mobilePrimary.map((item) => `<a href="${localHref(item.path)}" class="mobile-bottom-link${item.brand ? " mobile-bottom-home" : ""}${isActive(item) ? " is-active" : ""}"${isActive(item) ? ' aria-current="page"' : ""}><span class="mobile-bottom-icon">${item.brand ? '<img class="mobile-bottom-brand mobile-bottom-brand--dark" src="/static/img/brand/liva-peak-sidebar.svg?v=liva-peak-1" alt="" aria-hidden="true"><img class="mobile-bottom-brand mobile-bottom-brand--light" src="/static/img/brand/liva-peak-sidebar-light.svg?v=liva-peak-1" alt="" aria-hidden="true">' : svg(item.icon)}</span><span>${item.label}</span></a>`).join("")}
        <button class="mobile-bottom-link mobile-bottom-more${overflowActive ? " is-active" : ""}" type="button" aria-haspopup="dialog" aria-controls="mobile-dock-sheet" aria-expanded="false"><span class="mobile-bottom-icon">${svg("more")}</span><span>Mehr</span></button>
      </nav>`;
    return shell;
  }

  function safeGet(key) {
    try { return window.localStorage.getItem(key); } catch (_) { return null; }
  }

  function safeSet(key, value) {
    try { window.localStorage.setItem(key, value); } catch (_) { /* storage may be unavailable */ }
  }

  function storedBoolean(key, fallback) {
    const value = safeGet(key);
    return value == null ? fallback : value === "true" || value === "1";
  }

  function scopedStorageKey() {
    return DESKTOP_QUERY.matches ? DESKTOP_STORAGE_KEY : TABLET_STORAGE_KEY;
  }

  function readCollapsed() {
    return storedBoolean(scopedStorageKey(), storedBoolean(STORAGE_KEY, true));
  }

  function init() {
    window.LIVA = window.LIVA || {};
    window.LIVA.sidebarControllerActive = true;

    const sidebar = buildSidebar();
    const mobileNavigation = buildMobileNavigation();
    const existing = document.querySelector(".desktop-sidebar");
    const host = document.querySelector("[data-liva-sidebar-host]");
    if (existing) existing.replaceWith(sidebar);
    else if (host) host.replaceWith(sidebar);
    else document.body.prepend(sidebar);
    document.querySelector(".mobile-dock-shell")?.remove();
    document.body.appendChild(mobileNavigation);

    const toggle = sidebar.querySelector(".desktop-sidebar-toggle");
    const mobileBottomNav = mobileNavigation.querySelector(".mobile-bottom-nav");
    const mobileSheet = mobileNavigation.querySelector(".mobile-dock-sheet");
    const mobileBackdrop = mobileNavigation.querySelector(".mobile-dock-backdrop");
    const mobileMore = mobileNavigation.querySelector(".mobile-bottom-more");
    const mobileClose = mobileNavigation.querySelector(".mobile-dock-close");
    let mobileCloseTimer = null;

    function setMobileMenuOpen(open, restoreFocus = false) {
      const shouldOpen = Boolean(open && MOBILE_QUERY.matches);
      window.clearTimeout(mobileCloseTimer);
      if (shouldOpen) {
        mobileSheet.hidden = false;
        mobileBackdrop.hidden = false;
        mobileSheet.getBoundingClientRect();
      }
      mobileSheet.classList.toggle("is-open", shouldOpen);
      mobileBackdrop.classList.toggle("is-open", shouldOpen);
      mobileMore.setAttribute("aria-expanded", shouldOpen ? "true" : "false");
      document.body.classList.toggle("mobile-dock-open", shouldOpen);
      if (shouldOpen) {
        mobileClose.focus({ preventScroll: true });
      } else {
        mobileCloseTimer = window.setTimeout(() => {
          mobileSheet.hidden = true;
          mobileBackdrop.hidden = true;
        }, 220);
        if (restoreFocus) mobileMore.focus({ preventScroll: true });
      }
    }

    function syncToggleLabel() {
      const collapsed = document.documentElement.classList.contains("sidebar-collapsed");
      const label = collapsed ? "Sidebar ausklappen" : "Sidebar einklappen";
      toggle.setAttribute("aria-label", label);
      toggle.setAttribute("title", label);
    }

    function applyCollapsed(collapsed, persist) {
      document.documentElement.classList.toggle("sidebar-collapsed", collapsed);
      if (persist) {
        safeSet(scopedStorageKey(), String(collapsed));
        safeSet(STORAGE_KEY, String(collapsed));
      }
      syncToggleLabel();
    }

    function syncVisibility() {
      const visible = SIDEBAR_QUERY.matches;
      const mobileVisible = MOBILE_QUERY.matches;
      sidebar.hidden = !visible;
      sidebar.setAttribute("aria-hidden", visible ? "false" : "true");
      if (visible) sidebar.removeAttribute("inert");
      else sidebar.setAttribute("inert", "");
      applyCollapsed(visible ? readCollapsed() : false, false);
      mobileBottomNav.hidden = !mobileVisible;
      mobileNavigation.setAttribute("aria-hidden", mobileVisible ? "false" : "true");
      document.body.classList.toggle("has-mobile-dock", mobileVisible);
      if (!mobileVisible) setMobileMenuOpen(false);
    }

    mobileMore.addEventListener("click", () => {
      setMobileMenuOpen(!mobileSheet.classList.contains("is-open"));
    });
    mobileClose.addEventListener("click", () => setMobileMenuOpen(false, true));
    mobileBackdrop.addEventListener("click", () => setMobileMenuOpen(false, true));
    mobileSheet.querySelectorAll("a").forEach((link) => link.addEventListener("click", () => setMobileMenuOpen(false)));
    document.addEventListener("keydown", (event) => {
      if (event.key === "Escape" && mobileSheet.classList.contains("is-open")) setMobileMenuOpen(false, true);
    });

    toggle.addEventListener("click", (event) => {
      event.stopPropagation();
      applyCollapsed(!document.documentElement.classList.contains("sidebar-collapsed"), true);
    });

    sidebar.addEventListener("click", (event) => {
      if (!document.documentElement.classList.contains("sidebar-collapsed")) return;
      if (event.target.closest("a, button")) return;
      applyCollapsed(false, true);
    });

    document.addEventListener("click", (event) => {
      if (!DESKTOP_QUERY.matches || document.documentElement.classList.contains("sidebar-collapsed")) return;
      if (sidebar.contains(event.target)) return;
      applyCollapsed(true, true);
    });

    [SIDEBAR_QUERY, DESKTOP_QUERY, MOBILE_QUERY].forEach((query) => query.addEventListener?.("change", syncVisibility));
    syncVisibility();
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init, { once: true });
  else init();
})();
