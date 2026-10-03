document.addEventListener("DOMContentLoaded", () => {
    const root = document.documentElement;
    const LAYOUT_STORAGE_KEY = "liva.layoutMode";
    const PAGE_PREFETCHED = new Set();
    const safeStorage = {
        get(key) {
            try {
                return window.localStorage ? window.localStorage.getItem(key) : null;
            } catch (_) {
                return null;
            }
        },
        set(key, value) {
            try {
                if (!window.localStorage) return false;
                window.localStorage.setItem(key, value);
                return true;
            } catch (_) {
                return false;
            }
        }
    };

    function normalizeLayoutMode(value) {
        return value === "compact" ? "compact" : "wide";
    }

    function applyLayoutMode(mode, options = {}) {
        const persist = options.persist !== false;
        const finalMode = normalizeLayoutMode(mode);
        root.dataset.layoutMode = finalMode;
        if (persist) {
            safeStorage.set(LAYOUT_STORAGE_KEY, finalMode);
        }
        window.dispatchEvent(new CustomEvent("layoutmodechange", {
            detail: { layoutMode: finalMode }
        }));
        window.LIVA = window.LIVA || {};
        window.LIVA.layoutMode = finalMode;
        return finalMode;
    }

    window.LIVA = window.LIVA || {};
    window.LIVA.applyLayoutMode = applyLayoutMode;
    window.LIVA.getLayoutMode = () => normalizeLayoutMode(root.dataset.layoutMode);
    applyLayoutMode(root.dataset.layoutMode, { persist: true });

    // =====================================================
    // MOBILE MODE (GLOBAL)
    // =====================================================
    const PHONE_QUERY = "(max-width: 640px)";
    const SIDEBAR_QUERY = "(min-width: 641px)";
    const DESKTOP_QUERY = "(min-width: 1200px)";
    const mobileMq = window.matchMedia(PHONE_QUERY);
    const sidebarMq = window.matchMedia(SIDEBAR_QUERY);
    const desktopSidebarMq = window.matchMedia(DESKTOP_QUERY);

    function applyMobileMode() {
        const isMobile = mobileMq.matches;
        document.documentElement.classList.toggle("is-mobile", isMobile);
        if (document.body) document.body.classList.toggle("is-mobile", isMobile);
        root.dataset.layoutMode = normalizeLayoutMode(
            safeStorage.get(LAYOUT_STORAGE_KEY) || root.dataset.layoutMode
        );
        window.LIVA = window.LIVA || {};
        window.LIVA.mobileMode = isMobile;
        window.LIVA.mobileQuery = PHONE_QUERY;
        window.LIVA.isMobile = () => mobileMq.matches;
        window.dispatchEvent(new CustomEvent("th:mobile", { detail: { isMobile } }));
    }

    function updateNavMode() {
        const width = window.innerWidth || document.documentElement.clientWidth || 0;
        const navMode = mobileMq.matches ? "phone" : (desktopSidebarMq.matches ? "desktop" : "tablet");
        root.dataset.navMode = navMode;
        root.dataset.navWidth = String(width);
    }

    applyMobileMode();

    // Desktop sidebar markup and behavior are provided by static/js/sidebar.js.

    // =====================================================
    // CHAT ICON -> OPEN CUSTOM GPT (NO SIDEBAR)
    // =====================================================
    const chatBtn = document.getElementById("chat-toggle");
    const GPT_URL = "https://chatgpt.com/g/g-694f06e7ff008191a2fd1fd19823767e-raspberry-pi";

    if (chatBtn) {
        chatBtn.addEventListener("click", (e) => {
            e.preventDefault();
            e.stopPropagation();
            window.location.href = GPT_URL;
        });
    }


    // =====================================================
    // MOBILE NAVIGATION
    // =====================================================
    const mobileMenuBtn = document.getElementById("mobile-menu-btn");
    const mobileNav = document.getElementById("mobile-nav");
    const mobileNavBackdrop = document.getElementById("mobile-nav-backdrop");

    function hasVisibleGlobalOverlay() {
        const cmdOverlay = document.getElementById("thCmdOverlay");
        if (cmdOverlay && cmdOverlay.getAttribute("aria-hidden") === "false") return true;
        if (mobileNav && mobileNav.classList.contains("open") && !mobileNav.hidden) return true;
        const coreExplainBackdrop = document.getElementById("core-explain-sheet-backdrop");
        if (coreExplainBackdrop && !coreExplainBackdrop.hidden) return true;
        const calendarOverlay = document.getElementById("calendar-full-overlay");
        if (calendarOverlay && calendarOverlay.classList.contains("is-open")) return true;
        return false;
    }

    function clearStaleScrollLocks() {
        if (hasVisibleGlobalOverlay()) return;
        const body = document.body;
        const html = document.documentElement;
        if (!body || !html) return;
        const scrollingElement = document.scrollingElement;
        body.classList.remove("mobile-nav-open", "core-explain-open");
        html.classList.remove("core-explain-open");
        body.style.overflow = "";
        body.style.overflowY = "";
        body.style.position = "";
        body.style.width = "";
        html.style.overflow = "";
        html.style.overflowY = "";
        html.style.position = "";
        html.style.width = "";
        body.style.touchAction = "";
        html.style.touchAction = "";
        if (scrollingElement && scrollingElement !== body && scrollingElement !== html) {
            scrollingElement.style.overflow = "";
            scrollingElement.style.overflowY = "";
            scrollingElement.style.position = "";
            scrollingElement.style.width = "";
            scrollingElement.style.touchAction = "";
        }
    }

    function clearStaleUiLocks() {
        const navIsOpen = !!mobileNav && mobileNav.classList.contains("open") && !mobileNav.hidden;
        if (!navIsOpen) {
            document.body.classList.remove("mobile-nav-open");
            if (mobileNav) {
                mobileNav.classList.remove("open");
                mobileNav.hidden = true;
                mobileNav.setAttribute("aria-hidden", "true");
            }
            if (mobileNavBackdrop) {
                mobileNavBackdrop.classList.remove("open");
                mobileNavBackdrop.hidden = true;
                mobileNavBackdrop.setAttribute("aria-hidden", "true");
            }
            if (mobileMenuBtn) {
                mobileMenuBtn.setAttribute("aria-expanded", "false");
            }
        }

        const cmdOverlay = document.getElementById("thCmdOverlay");
        if (cmdOverlay && cmdOverlay.getAttribute("aria-hidden") === "false" && !cmdOverlay.querySelector(".th-cmd-window")) {
            cmdOverlay.setAttribute("aria-hidden", "true");
        }

        clearStaleScrollLocks();
    }

    function initMobileNav() {
        if (!mobileMenuBtn || !mobileNav || mobileMenuBtn.dataset.navBound === "1") return;
        mobileMenuBtn.dataset.navBound = "1";

        const setNavOpen = (open) => {
            const isOpen = !!open;
            mobileNav.classList.toggle("open", isOpen);
            mobileNav.hidden = !isOpen;
            mobileNav.setAttribute("aria-hidden", isOpen ? "false" : "true");
            mobileMenuBtn.setAttribute("aria-expanded", isOpen ? "true" : "false");
            if (mobileNavBackdrop) {
                mobileNavBackdrop.hidden = !isOpen;
                mobileNavBackdrop.classList.toggle("open", isOpen);
                mobileNavBackdrop.setAttribute("aria-hidden", isOpen ? "false" : "true");
            }
        };

        const closeNav = () => setNavOpen(false);
        const toggleNav = () => setNavOpen(!mobileNav.classList.contains("open"));

        mobileMenuBtn.setAttribute("aria-controls", "mobile-nav");
        mobileMenuBtn.setAttribute("aria-expanded", "false");
        mobileNav.setAttribute("aria-hidden", "true");
        mobileNav.hidden = true;
        if (mobileNavBackdrop) {
            mobileNavBackdrop.setAttribute("aria-hidden", "true");
            mobileNavBackdrop.hidden = true;
            mobileNavBackdrop.classList.remove("open");
        }

        mobileMenuBtn.addEventListener("click", (e) => {
            e.preventDefault();
            e.stopPropagation();
            toggleNav();
        });

        document.querySelectorAll(".mobile-nav-link").forEach((link) => {
            link.addEventListener("click", () => {
                closeNav();
            });
        });

        mobileNavBackdrop?.addEventListener("click", closeNav);

        document.addEventListener("click", (e) => {
            if (!mobileNav.classList.contains("open")) return;
            if (mobileNav.contains(e.target) || mobileMenuBtn.contains(e.target)) return;
            closeNav();
        });

        document.addEventListener("keydown", (e) => {
            if (e.key === "Escape") closeNav();
        });

        window.addEventListener("pageshow", () => {
            closeNav();
            clearStaleUiLocks();
        });
        window.addEventListener("load", clearStaleUiLocks);
        window.addEventListener("focus", clearStaleUiLocks);
        window.addEventListener("resize", clearStaleUiLocks, { passive: true });
        document.addEventListener("visibilitychange", () => {
            if (!document.hidden) clearStaleUiLocks();
        });
        window.LIVA.closeMobileNav = closeNav;
        window.LIVA.openMobileNav = () => setNavOpen(true);
        window.LIVA.clearStaleUiLocks = clearStaleUiLocks;
        clearStaleUiLocks();
    }

    initMobileNav();

    function syncMobileNavVisibility() {
        if (sidebarMq.matches) {
            window.LIVA.closeMobileNav?.();
        }
    }

    syncMobileNavVisibility();
    [sidebarMq, desktopSidebarMq].forEach((mq) => {
        if (mq.addEventListener) {
            mq.addEventListener("change", syncMobileNavVisibility);
        } else if (mq.addListener) {
            mq.addListener(syncMobileNavVisibility);
        }
    });

    window.addEventListener("resize", updateNavMode, { passive: true });


    // =====================================================
    // SETTINGS BUTTON (TOP RIGHT)
    // =====================================================
    const settingsBtn = document.getElementById("settings-toggle");

    if (settingsBtn) {
        settingsBtn.addEventListener("click", () => {
            window.location.href = "/settings";
        });
    }


    // =====================================================
    // DARK MODE (GLOBAL, FLASH-SAFE)
    // Default = DARK
    // Only switch to LIGHT if user explicitly saved it
    // =====================================================
    const darkToggle = document.getElementById("darkmode-toggle");
    const settingsPage = document.querySelector(".settings-page");
    const csrfToken = settingsPage?.dataset?.csrfToken || "";
    const authLevel = (document.body?.dataset?.authLevel || "none").toLowerCase();
    const isKeyUser = authLevel === "key";
    const THEME_STORAGE_KEY = "theme_public";

    async function persistThemePreference(theme) {
        if (!isKeyUser || !csrfToken) return;
        try {
            await fetch("/api/settings/theme", {
                method: "POST",
                credentials: "same-origin",
                headers: {
                    "Content-Type": "application/json",
                    "X-CSRF-Token": csrfToken,
                },
                body: JSON.stringify({ theme }),
            });
        } catch (_) {
            // Keep UI responsive even if preference sync fails.
        }
    }

    function applyTheme(theme, options = {}) {
        const persist = !!options.persist;
        const isDark = theme !== "light";     // null/undefined/anything else => dark
        root.classList.toggle("dark", isDark);
        const finalTheme = isDark ? "dark" : "light";
        if (!isKeyUser) {
            safeStorage.set(THEME_STORAGE_KEY, finalTheme);
        }
        if (persist) persistThemePreference(finalTheme);
        window.dispatchEvent(new CustomEvent("themechange", {
            detail: { theme: finalTheme, isDark }
        }));
    }

    // Init: keep the class set by the early head script (server preference > localStorage fallback)
    const isDark = root.classList.contains("dark");
    if (!isKeyUser) {
        safeStorage.set(THEME_STORAGE_KEY, isDark ? "dark" : "light");
    }
    if (darkToggle) darkToggle.checked = isDark;

    if (darkToggle) {
        darkToggle.addEventListener("change", () => {
            applyTheme(darkToggle.checked ? "dark" : "light", { persist: true });
        });
    }


    // =====================================================
    // DASHBOARD STREAKS
    // =====================================================
    const streak1El = document.getElementById("streak1-value");
    const streak2El = document.getElementById("streak2-value");
    const streak2TargetEl = document.getElementById("streak2-target");
    const streak2LabelEl = document.getElementById("streak2-label");
    const streak2RatioEl = streak2LabelEl ? streak2LabelEl.querySelector(".streak2-ratio") : null;
    let lastStreak2Grams = null;

    function isCompactStreakLabel() {
        return window.matchMedia("(max-width: 1366px)").matches;
    }

    function updateStreak2Label(grams) {
        lastStreak2Grams = grams == null ? null : grams;
        if (!streak2TargetEl) return;
        const compact = isCompactStreakLabel();
        if (streak2RatioEl) {
            streak2RatioEl.style.display = compact ? "none" : "";
        }
        if (compact) {
            streak2TargetEl.textContent = grams != null ? `${grams}g` : "–";
        } else {
            streak2TargetEl.textContent = grams != null ? `(${grams}g)` : "(–)";
        }
    }

    function parseNumber(value) {
        if (!value) return null;
        const cleaned = value.replace(",", ".").replace(/[^0-9.-]/g, "").trim();
        if (cleaned === "") return null;
        const num = parseFloat(cleaned);
        return Number.isFinite(num) ? num : null;
    }

    function parseDateToDayIndex(raw) {
        if (!raw) return null;
        const text = raw.trim();

        // ISO: YYYY-MM-DD
        if (/^\d{4}-\d{2}-\d{2}$/.test(text)) {
            const [y, m, d] = text.split("-").map(n => parseInt(n, 10));
            const dt = new Date(Date.UTC(y, m - 1, d));
            if (Number.isNaN(dt.getTime())) return null;
            return Math.floor(dt.getTime() / 86400000);
        }

        // DE: DD.MM.YY or DD.MM.YYYY
        const parts = text.split(".");
        if (parts.length < 3) return null;
        const day = parseInt(parts[0], 10);
        const month = parseInt(parts[1], 10);
        let year = parseInt(parts[2], 10);
        if (!day || !month || !year) return null;
        if (year < 100) year += 2000;

        const dt = new Date(Date.UTC(year, month - 1, day));
        if (Number.isNaN(dt.getTime())) return null;
        return Math.floor(dt.getTime() / 86400000);
    }

    async function updateDashboardStreaks() {
        if (!streak1El || !streak2El) return;

        try {
            const resp = await fetch("/api/makros/table", { cache: "no-store" });
            if (!resp.ok) throw new Error("Makros fetch failed");

            const html = await resp.text();
            const tbody = document.createElement("tbody");
            tbody.innerHTML = html;

            const rows = Array.from(tbody.querySelectorAll("tr"));
            if (rows.length === 0) {
                streak1El.textContent = "0";
                streak2El.textContent = "0";
                updateStreak2Label(null);
                return;
            }

            const byDay = new Map();

            rows.forEach(row => {
                const cells = row.querySelectorAll("td");
                if (cells.length < 4) return;

                const dayIndex = parseDateToDayIndex(cells[0].textContent);
                if (dayIndex == null) return;

                const weight = parseNumber(cells[1].textContent);
                const protein = parseNumber(cells[3].textContent);

                if (!byDay.has(dayIndex)) {
                    byDay.set(dayIndex, { weight, protein });
                }
            });

            const days = Array.from(byDay.keys()).sort((a, b) => b - a);
            if (days.length === 0) {
                streak1El.textContent = "0";
                streak2El.textContent = "0";
                updateStreak2Label(null);
                return;
            }

            let streak1 = 1;
            let prevDay = days[0];
            for (let i = 1; i < days.length; i += 1) {
                if (days[i] === prevDay - 1) {
                    streak1 += 1;
                    prevDay = days[i];
                } else {
                    break;
                }
            }

            let streak2 = 0;
            let streak2Entry = null;
            prevDay = null;
            for (let i = 0; i < days.length; i += 1) {
                const day = days[i];
                const entry = byDay.get(day);
                const hasData = entry && entry.weight != null && entry.protein != null;

                if (!streak2Entry) {
                    if (!hasData) {
                        continue;
                    }
                    streak2Entry = entry;
                } else if (prevDay != null && day !== prevDay - 1) {
                    break;
                }

                if (!hasData || entry.protein < entry.weight * 2) break;
                streak2 += 1;
                prevDay = day;
            }

            let streak2Grams = null;
            if (streak2Entry && streak2Entry.weight != null) {
                streak2Grams = Math.round(streak2Entry.weight * 2);
            }
            updateStreak2Label(streak2Grams);

            streak1El.textContent = String(streak1);
            streak2El.textContent = String(streak2);
        } catch (err) {
            console.warn("Dashboard streaks failed:", err);
        }
    }

    function scheduleDashboardStreaks() {
        if (!streak1El || !streak2El) return;
        const run = () => updateDashboardStreaks();
        if ('requestIdleCallback' in window) {
            window.requestIdleCallback(run, { timeout: 1800 });
        } else {
            window.setTimeout(run, 900);
        }
    }

    scheduleDashboardStreaks();
    window.addEventListener("resize", () => {
        updateStreak2Label(lastStreak2Grams);
    }, { passive: true });

    function prefetchPage(url) {
        const href = String(url || "").trim();
        if (!href || PAGE_PREFETCHED.has(href)) return;
        if (!href.startsWith("/") || href.startsWith("//")) return;
        PAGE_PREFETCHED.add(href);
        const link = document.createElement("link");
        link.rel = "prefetch";
        link.as = "document";
        link.href = href;
        document.head.appendChild(link);
    }

    document.querySelectorAll("a[href^='/']").forEach((link) => {
        link.addEventListener("mouseenter", () => prefetchPage(link.getAttribute("href")), { passive: true });
    });
});
