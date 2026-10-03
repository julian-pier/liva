(() => {
  const app = document.getElementById("dashboard-app");
  if (!app) return;
  const DASHBOARD_SYNC_KEY = "liva_dashboard_sync_v1";
  const dashboardSyncChannel = (() => {
    try {
      return typeof BroadcastChannel !== "undefined" ? new BroadcastChannel("liva_dashboard_sync") : null;
    } catch (_) {
      return null;
    }
  })();

  // =========================================================
  // CONFIG
  // =========================================================
  const LOCALE = "de-DE";
  const TIME_ZONE = "Europe/Berlin";
  const MS_DAY = 24 * 60 * 60 * 1000;
  const DASH = "-";
  const ALL_DAYS = 3650;
  const AUTOPILOT_MEMORY_DAYS = 56;

  const RANGE_MAP = {
    7: "Woche",
    30: "Monat",
    365: "Jahr",
    all: "Gesamt"
  };

  const STORAGE = {
    autoInsights: "dashboard_auto_insights",
    coreDecisionCard: "dashboard_core_decision_card_cache_v2",
    mealplanToday: "dashboard_mealplan_today_cache_v1",
    enduranceMode: "dashboard_endurance_mode_v1"
  };

  const MOBILE_QUERY = window.LIVA?.mobileQuery || "(max-width: 640px)";
  const mobileMq = window.matchMedia(MOBILE_QUERY);
  const MEALPLAN_SYNC_INTERVAL_MS = 60000;
  const MEALPLAN_MIN_REFRESH_GAP_MS = 45000;
  const isIpadLike = () => {
    const ua = navigator.userAgent || "";
    const platform = navigator.platform || "";
    return /iPad/i.test(ua) || (platform === "MacIntel" && Number(navigator.maxTouchPoints || 0) > 1);
  };
  app.classList.toggle("is-ipad", isIpadLike());

  // =========================================================
  // DOM HELPERS
  // =========================================================
  const authLevel = (document.body?.dataset?.authLevel || "none").toLowerCase();
  const isKey = authLevel === "key";
  const byId = (id) => document.getElementById(id);
  const setText = (el, v) => { if (el) el.textContent = v; };
  const setHtml = (el, v) => { if (el) el.innerHTML = v; };
  const escapeHtml = (s) => String(s || "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/\"/g, "&quot;")
    .replace(/'/g, "&#39;");

  /**
   * @typedef {"recovery"|"strength"|"run"|"nutrition"|"rhythm"|"other"} LearningCategory
   */
  /**
   * @typedef {{
   *   id: string,
   *   title: string,
   *   summary: string,
   *   conf: number,
   *   n: number,
   *   rank: number,
   *   category: LearningCategory,
   *   priority: "high"|"med"|"low",
   *   priorityLabel: string,
   *   compare: string,
   *   details: string,
   *   raw: any
   * }} LearningView
   */

  function pcStripParenDelta(s) {
    // entfernt "(+1/+1 rep)" am Ende (oder generell letzte Klammergruppe)
    const t = String(s || "").trim();
    return t.replace(/\s*\([^)]*\)\s*$/, "").trim();
  }

  function pcStripReassessLabel(s) {
    return String(s || "")
      .replace(/\s*·\s*Reassess\b/gi, "")
      .replace(/\s*\(Reassess\)\s*/gi, " ")
      .replace(/\s{2,}/g, " ")
      .trim();
  }

  function pcFormatKg(x) {
    const v = Number(x);
    if (!Number.isFinite(v)) return "";
    const s = v.toLocaleString("de-DE", { maximumFractionDigits: 1 });
    return s.replace(/,0$/, "");
  }

  function pcFormatWeights(weights) {
    const ws = (weights || []).filter((v) => Number.isFinite(v));
    if (!ws.length) return "";
    const allSame = ws.every((w) => Math.abs(w - ws[0]) < 0.0001);
    if (allSame) return `${pcFormatKg(ws[0])}kg`;
    return `${ws.map((w) => pcFormatKg(w)).join("/")}kg`;
  }

  function pcBuildLastLineFromDone(done) {
    const reps = (done?.reps || []).filter((v) => Number.isFinite(v));
    const weights = (done?.weights || []).filter((v) => Number.isFinite(v));
    if (!reps.length || !weights.length) return "";

    // expand weight wenn nur 1 value vorhanden, aber mehrere reps
    let w = weights.slice();
    if (w.length === 1 && reps.length > 1) w = Array.from({ length: reps.length }, () => w[0]);

    const repsText = reps.join("/");
    const weightText = pcFormatWeights(w);

    const rpes = (done?.rpes || []).filter((v) => Number.isFinite(v));
    const lastRpe = rpes.length ? rpes[rpes.length - 1] : null;
    const rpeSuffix = Number.isFinite(lastRpe) ? ` @RPE ${String(lastRpe).replace(".", ",")}` : "";

    return `${repsText} x ${weightText}${rpeSuffix}`.trim();
  }

  function pcComputeDeltaFromArrays(lastReps, lastWeights, todayReps, todayWeights) {
    const lw = (lastWeights || []).filter((v) => Number.isFinite(v));
    const tw = (todayWeights || []).filter((v) => Number.isFinite(v));
    const pairCount = Math.min(lw.length, tw.length);

    if (pairCount > 0) {
      let maxNegAbs = 0;
      let maxPos = 0;

      for (let i = 0; i < pairCount; i += 1) {
        const d = tw[i] - lw[i];
        if (!Number.isFinite(d)) continue;
        if (d < 0) maxNegAbs = Math.max(maxNegAbs, Math.abs(d));
        if (d > 0) maxPos = Math.max(maxPos, d);
      }

      if (maxNegAbs > 0) return `(-${pcFormatKg(maxNegAbs)} kg)`;
      if (maxPos > 0) return `(+${pcFormatKg(maxPos)} kg)`;
    }

    const sum = (arr) => (arr || []).reduce((a, b) => a + (Number.isFinite(b) ? b : 0), 0);
    const rd = sum(todayReps) - sum(lastReps);
    if (rd > 0) return `(+${rd} rep)`;
    if (rd < 0) return `(${rd} rep)`;
    return "";
  }

  function applyTodayWeekPill(info){
    const label = document.getElementById("today-week-label");
    const pill  = document.getElementById("today-week-pill");
    if (!pill) return;

    pill.classList.remove("is-build","is-deload","is-overreach");

    if (!info || !info.phase_label) {
      if (label) label.textContent = "";
      pill.textContent = "";
      pill.style.display = "none";
      return;
    }

    if (label) label.textContent = `Woche ${info.week_current}/${info.total_weeks ?? "—"}`;

    pill.textContent = info.phase_label;
    pill.style.display = "";
    const wt = (info.week_type || "").toLowerCase();
    pill.classList.add(wt === "deload" ? "is-deload" : wt === "overreach" ? "is-overreach" : "is-build");
  }


  function _arrowFromState(stateRaw){
    const n = normalizeSignalState(stateRaw);
    if (n === "good" || n === "present") return "↑";
    if (n === "bad") return "↓";
    return "→";
  }

  const DASHBOARD_CARD_SELECTORS = [
    "#dashboard-today-card",
    "#today-agenda-card",
    "#plan-check-card",
    "#dashboard-mealplan-card",
    "#dashboard-endurance-card",
    "#dashboard-signals-card",
  ];

  function getDashboardCards() {
    return DASHBOARD_CARD_SELECTORS
      .map((selector) => app.querySelector(selector))
      .filter(Boolean);
  }

  function markDashboardCardReady(target) {
    const card = typeof target === "string" ? document.querySelector(target) : target;
    if (card) card.classList.add("is-card-ready");
  }

  function primeDashboardCardAnimation() {
    getDashboardCards().forEach((card, index) => {
      card.style.setProperty("--dash-delay", String(index));
    });
  }

  function applyDashboardLayout() {
    // Card ownership never changes with viewport size. CSS alone controls the
    // layout, avoiding resize races and broken intermediate widths.
    const compact = mobileMq.matches;
    app.classList.toggle("is-compact", compact);
    app.dataset.viewport = compact ? "compact" : "regular";
    setText(byId("signals-range-title"), compact ? "Letzte 7 Tage" : "Letzte 14 Tage");
  }
  function _arrowDirFromState(stateRaw){
    const norm = normalizeSignalState(stateRaw);
    if (norm === "good") return "up";
    if (norm === "bad")  return "down";
    return "flat";
  }

  function _arrowSvg(dir){
    if (dir === "up") {
      return `<svg viewBox="0 0 16 16" width="14" height="14" aria-hidden="true">
        <path d="M8 13V3M8 3L3.5 7.5M8 3l4.5 4.5" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/>
      </svg>`;
    }
    if (dir === "down") {
      return `<svg viewBox="0 0 16 16" width="14" height="14" aria-hidden="true">
        <path d="M8 3v10M8 13l-4.5-4.5M8 13l4.5-4.5" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/>
      </svg>`;
    }
    // flat
    return `<svg viewBox="0 0 16 16" width="14" height="14" aria-hidden="true">
      <path d="M3 8h10M13 8l-3-3M13 8l-3 3" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/>
    </svg>`;
  }

  function _tileMeta(label, vs, stateRaw, arrowChar){
    const norm = normalizeSignalState(stateRaw);
    const arrow = arrowChar || "→";

    return `
      <div class="today-tile-meta">
        <div class="today-tile-meta-left">
          <span class="today-dot is-${norm}" title="${label}: ${stateRaw}"></span>
          <span class="today-tile-meta-label">${label}</span>
        </div>
        <div class="today-tile-meta-right">
          <span class="today-tile-meta-vs">${vs}</span>
          <span class="today-tile-meta-arrow is-${norm}">${arrow}</span>
        </div>
      </div>
    `;
  }



  // entfernt NUR das alte HTML-label (direktes Kind), nicht den neuen Header
  function _removeLegacyTileLabel(tile){
    if (!tile) return;
    const kids = Array.from(tile.children || []);
    for (const el of kids) {
      if (el && el.classList && el.classList.contains("today-tile-label")) el.remove();
    }
  }


  function _sparkPathFromSeries(series, w = 320, h = 86, padX = 8, padTop = 11, padBottom = 7) {
    const vals = (series || []).map(v => (Number.isFinite(v) ? v : null));
    const finite = vals.filter(v => v !== null);
    if (!finite.length) return "";

    const min = Math.min(...finite);
    const max = Math.max(...finite);
    const span = (max - min) || 1;

    const n = vals.length || 1;
    const xStep = (w - padX * 2) / Math.max(1, n - 1);
    const usableH = Math.max(1, h - padTop - padBottom);
    const yMin = padTop;
    const yMax = h - padBottom;
    const clampY = (v) => Math.min(yMax, Math.max(yMin, v));

    // -> Punkte (x,y) + Segmente (wegen null)
    const segments = [];
    let cur = [];

    for (let i = 0; i < n; i++) {
      const v = vals[i];
      if (v === null) {
        if (cur.length) segments.push(cur);
        cur = [];
        continue;
      }
      const x = padX + i * xStep;
      const y = padTop + usableH * (1 - (v - min) / span);
      cur.push({ x, y });
    }
    if (cur.length) segments.push(cur);

    // Catmull-Rom -> Bezier
    const toFixed = (num) => Number(num).toFixed(2);
    let d = "";

    for (const pts of segments) {
      if (pts.length === 1) {
        d += `M ${toFixed(pts[0].x)} ${toFixed(pts[0].y)} `;
        continue;
      }
      d += `M ${toFixed(pts[0].x)} ${toFixed(pts[0].y)} `;

      for (let i = 0; i < pts.length - 1; i++) {
        const p0 = pts[i - 1] || pts[i];
        const p1 = pts[i];
        const p2 = pts[i + 1];
        const p3 = pts[i + 2] || p2;

        const c1x = p1.x + (p2.x - p0.x) / 6;
        const c1y = clampY(p1.y + (p2.y - p0.y) / 6);
        const c2x = p2.x - (p3.x - p1.x) / 6;
        const c2y = clampY(p2.y - (p3.y - p1.y) / 6);

        d += `C ${toFixed(c1x)} ${toFixed(c1y)} ${toFixed(c2x)} ${toFixed(c2y)} ${toFixed(p2.x)} ${toFixed(p2.y)} `;
      }
    }

    return d.trim();
  }

  function _sparkSvg(series, extraClass = "") {
    const w = 320, h = 86;
    const d = _sparkPathFromSeries(series, w, h);
    return `
      <svg class="today-large-sparkline ${extraClass}" viewBox="0 0 ${w} ${h}" preserveAspectRatio="none" aria-hidden="true">
        <path class="today-large-sparkline-path" d="${d}"></path>
      </svg>
    `;
  }


  function _arrow(diff) {
    if (!Number.isFinite(diff) || diff === 0) return "→";
    return diff > 0 ? "↑" : "↓";
  }

  function _meanLastN(series, n) {
    const a = (series || []).map(Number).filter(Number.isFinite);
    const tail = a.slice(-n);
    if (!tail.length) return null;
    return tail.reduce((s,x)=>s+x,0) / tail.length;
  }

  // =========================================================
  // ELEMENT REGISTRY
  // =========================================================
  const els = {
    autoInsights: byId("dashboard-auto-insights"),

    // Pills
    pillPlan: byId("dashboard-pill-plan"),
    pillHrv: byId("dashboard-pill-hrv"),
    pillTraining: byId("dashboard-pill-gym"),
    pillRun: byId("dashboard-pill-run"),
	    pillNutrition: byId("dashboard-pill-nutrition"),

      // Mealplan today
      mealplanCard: byId("dashboard-mealplan-card"),
      mealplanBody: byId("dashboard-mealplan-body"),
      mealplanDate: byId("dashboard-mealplan-date"),
      mealplanProgress: byId("dashboard-mealplan-progress"),
      mealplanCorePill: byId("mealplan-core-pill"),
      mealplanCoreExplainSlot: byId("mealplan-core-explain-slot"),
      enduranceCard: byId("dashboard-endurance-card"),
      enduranceBody: byId("dashboard-endurance-body"),
      enduranceStatus: byId("dashboard-endurance-status"),
      enduranceSource: byId("dashboard-endurance-source"),
      planCheckCard: byId("plan-check-card"),
      planCheckCoreExplainSlot: byId("plan-check-core-explain-slot"),
      coreExplainPanelTemplate: byId("core-explain-panel-template"),
      coreExplainSheetBackdrop: byId("core-explain-sheet-backdrop"),
      coreExplainSheetHost: byId("core-explain-sheet-host"),
      blockStatusCard: byId("dashboard-block-status-card"),
      coreDecisionConfidence: byId("core-decision-confidence"),
      coreDecisionUpdated: byId("core-decision-updated"),
      coreStatusPhase: byId("core-status-phase"),
      coreStatusCompliance: byId("core-status-compliance"),
      coreStatusTimeline: byId("core-status-timeline"),
      coreStatusDiff: byId("core-status-diff"),
      coreStatusDiffList: byId("core-status-diff-list"),
      coreDecisionPending: byId("core-decision-pending"),
      coreDecisionInfo: byId("core-decision-info"),
      coreDecisionError: byId("core-decision-error"),
      coreDecisionLoadingTemplate: byId("core-decision-loading-template"),
      corePrimeHeadline: byId("core-home-prime-headline"),
      corePrimeSubline: byId("core-home-prime-subline"),
      corePrimeChipList: byId("core-home-prime-chip-list"),
      corePrimeRing: byId("core-home-prime-ring"),
      corePrimeConfidenceValue: byId("core-home-prime-confidence-value"),
      corePrimeMorgenStatus: byId("core-home-prime-morgen-status"),
      corePrimeReadiness: byId("core-home-prime-readiness"),
      corePrimeRkValue: byId("core-home-prime-rk-value"),
      corePrimeRkMarker: byId("core-home-prime-rk-marker"),
      corePrimeMiniPlan: byId("core-home-prime-mini-plan"),
      corePrimeMiniMode: byId("core-home-prime-mini-mode"),
      corePrimeMiniFocus: byId("core-home-prime-mini-focus"),
      corePrimeMiniRisk: byId("core-home-prime-mini-risk"),
      corePrimeSupportText1: byId("core-home-prime-support-text-1"),
      corePrimeSupportText2: byId("core-home-prime-support-text-2"),
      corePrimeSupportText3: byId("core-home-prime-support-text-3"),
      corePrimeSupportBar1: byId("core-home-prime-support-bar-1"),
      corePrimeSupportBar2: byId("core-home-prime-support-bar-2"),
      corePrimeSupportBar3: byId("core-home-prime-support-bar-3"),
      corePrimeBrakeText1: byId("core-home-prime-brake-text-1"),
      corePrimeBrakeText2: byId("core-home-prime-brake-text-2"),
      corePrimeBrakeText3: byId("core-home-prime-brake-text-3"),
      corePrimeBrakeBar1: byId("core-home-prime-brake-bar-1"),
      corePrimeBrakeBar2: byId("core-home-prime-brake-bar-2"),
      corePrimeBrakeBar3: byId("core-home-prime-brake-bar-3"),
      corePrimeTip: byId("core-home-prime-tip"),
      calendarCard: byId("dashboard-calendar-card"),
      calendarMonthLabel: byId("calendar-month-label"),
      calendarPrev: byId("calendar-prev"),
      calendarNext: byId("calendar-next"),
      calendarExpand: byId("calendar-expand-btn"),
      calendarMiniWeekdays: byId("calendar-mini-weekdays"),
      calendarMiniGrid: byId("calendar-mini-grid"),
      calendarMiniFooter: byId("calendar-mini-footer"),
      calendarMiniLegend: byId("calendar-mini-legend"),
      calendarModal: byId("calendar-full-overlay"),
      calendarModalPrev: byId("calendar-modal-prev"),
      calendarModalNext: byId("calendar-modal-next"),
      calendarModalTodayBtn: byId("calendar-modal-today-btn"),
      calendarRangeToggle: byId("calendar-range-toggle"),
      calendarGotoInputInline: byId("calendar-goto-input-inline"),
      calendarDayClose: byId("calendar-day-close"),
      calendarProblemsToggle: byId("calendar-problems-toggle"),
      calendarBlocksToggle: byId("calendar-blocks-toggle"),
      calendarModeToggle: byId("calendar-mode-toggle"),
      calendarViewToggle: byId("calendar-view-toggle"),
      calendarFocusToggle: byId("calendar-focus-toggle"),
      calendarModalMonthSelect: byId("calendar-modal-month-select"),
      calendarModalYearSelect: byId("calendar-modal-year-select"),
      calendarModalMonthLabel: byId("cal-full-title"),
      calendarModalGrid: byId("cal-full-grid"),
      calendarModalWeekdays: byId("cal-full-weekdays"),
      calendarFullBars: byId("cal-full-bars"),
      calendarFullCounts: byId("cal-full-counts"),
      calendarFullSummary: byId("cal-full-summary"),
      calendarDayPanel: byId("calendar-day-panel"),
      calendarSpanLane: byId("cal-full-bars"),
      calendarYearTiles: byId("calendar-year-tiles"),
      calendarNavYearPrev: byId("calendar-nav-year-prev"),
      calendarNavYearNext: byId("calendar-nav-year-next"),
      calendarNavYearLabel: byId("calendar-nav-year-label"),
      calendarGotoInput: byId("calendar-goto-input"),
      calendarFilterPills: byId("calendar-filter-chips"),
      calendarQuickRanges: byId("calendar-quick-ranges"),
      calendarContextStrip: byId("calendar-context-strip"),
      calendarFocusLegend: byId("calendar-focus-legend"),
      calendarPhaseSummary: byId("calendar-phase-summary"),
      calendarJumpNextTraining: byId("calendar-jump-next-training"),
      calendarJumpNextSpan: byId("calendar-jump-next-span"),
      calendarJumpPrevSpan: byId("calendar-jump-prev-span"),

    // Top cards (right column cards)
    hrvValue: byId("dashboard-hrv-value"),
    hrvSub: byId("dashboard-hrv-sub"),
    hrvBadge: byId("dashboard-hrv-badge"),
    hrvProgress: byId("dashboard-hrv-progress"),

    trainingValue: byId("dashboard-training-value"),
    trainingSub: byId("dashboard-training-sub"),
    trainingBadge: byId("dashboard-training-badge"),
    trainingProgress: byId("dashboard-training-progress"),

    runsValue: byId("dashboard-runs-value"),
    runsSub: byId("dashboard-runs-sub"),
    runsBadge: byId("dashboard-runs-badge"),
    runsProgress: byId("dashboard-runs-progress"),

    nutritionValue: byId("dashboard-nutrition-value"),
    nutritionSub: byId("dashboard-nutrition-sub"),
    nutritionBadge: byId("dashboard-nutrition-badge"),
    nutritionProgress: byId("dashboard-nutrition-progress"),

    // Focus
    focusCard: byId("dashboard-focus-card"),
    focusTitle: byId("dashboard-focus-title"),
    focusSub: byId("dashboard-focus-sub"),
    focusChip1: byId("dashboard-focus-chip-1"),
    focusChip2: byId("dashboard-focus-chip-2"),
    focusAction: byId("dashboard-focus-action"),

    // Plan summary (Heute card text + next/open)
    planCheckCardTitle: byId("plan-check-card-title"),
    planToday: byId("dashboard-plan-today"),
    planNext: byId("dashboard-plan-next"),
    planOpen: byId("dashboard-plan-open"),
    planOpenRow: byId("dashboard-plan-open-row"),
    coreTrainingCard: byId("dashboard-core-training-card"),

    // Signals (right card)
    signalRecovery: byId("signal-recovery"),
    signalEnergy: byId("signal-energy"),
    signalRun: byId("signal-run"),
    signalStrength: byId("signal-strength"),
    signalDayLabels: byId("signal-day-labels"),

    // Today sparklines (inside "Heute"-Card)
    todayRecovery: byId("today-signal-recovery"),
    todayEnergy: byId("today-signal-energy"),
    todayLoad: byId("today-signal-load"), // falls vorhanden; sonst bleibt null

    todayAgendaCard: byId("today-agenda-card"),
    todayAgendaDayMeta: byId("today-agenda-day-meta"),
    todayAgendaCountPill: byId("today-agenda-count-pill"),
    todayAgendaBody: byId("today-agenda-body")
  };

  // =========================================================
  // STATE
  // =========================================================
  const state = {
    rangeDays: 30,         // 7,30,365,"all"
    autoInsights: true,
    coreModeEnabled: false,
    autopilotExplainAvailable: false,
    autopilotExplainPayload: null,
    mealplanCoreExplainPayload: null,
    coreExplainActiveKey: null,
    coreExplainActiveLayout: null,
    coreExplainLastTrigger: null,
    coreExplainScrollLock: null,
    makrosSettings: null,  // /api/makros/settings -> settings
    lastData: null,
    pillTextCache: {},
    hrvSyncMeta: null,
    hrvSyncMetaLastFetchTs: 0,
    todayFooterLastLabel: null,
    todayFooterText: null,
    mealplanLoaded: false,
    mealplanBusy: false,
    mealplanPayload: null,
    mealplanLastFetchTs: 0,
    mealplanRenderKey: null,
    mealplanDirty: false,
    mealplanSyncTimer: null,
    mealplanSyncPending: false,
    mealplanRecentLogsByDate: {},
    mealplanHoldTimer: null,
    mealplanHoldRaf: null,
    mealplanHoldRow: null,
    mealplanHoldLogging: false,
    enduranceLoaded: false,
    enduranceBusy: false,
    endurancePayload: null,
    enduranceLastFetchTs: 0,
    enduranceMode: "laufen",
    enduranceModeSaving: false,
    enduranceModeError: "",
    calendarMonth: null,
    calendarPayload: null,
    calendarSelectedDate: null,
    calendarMode: "both",
    calendarRangeDays: 14,
    calendarShowProblemsOnly: false,
    calendarShowBlocks: true,
    calendarCache: new Map(),
    calendarRangeCache: new Map(),
    calendarDayCache: new Map(),
    calendarLastFetchTs: 0,
    calendarView: "timeline",
    calendarFocus: "types",
    calendarRange: null,
    calendarRangePayload: null,
    calendarNavYear: null,
    blockStatusCache: null,
    blockStatusDirty: false,
    blockStatusCacheDay: null,
    blockStatusPlanName: null,
    coreDecisionCardCache: null,
    coreDecisionCardCacheDay: null,
    coreDecisionCardLastFetchTs: 0,
    coreDecisionCardDirty: false,
    coreDecisionCardPromise: null,
    coreDecisionPendingMode: null,
    coreDecisionPendingSecondary: null,
    coreDecisionPendingSecondaryType: null,
    coreDecisionSaving: false,
    coreTodaySaving: false,
    coreStatusActiveSessionId: null,
    coreStatusActiveDayKey: null,
    coreStatusEventTab: "events",
    coreStatusPopover: null,
    coreStatusPopoverSessionId: null,
    coreStatusPopoverTarget: null,
    lastFullDashboardTs: 0,
    lastLiveUpdateTs: 0,
    pcInitQueued: false,
    pcSelectedKey: null,
    pcLoadSeq: 0,
    trainingCardLastKnownGoodPayload: null,
    trainingCardLastRenderKey: null,
    trainingCardHeadlineStatus: "",
    trainingTodayCardPayload: null,
    trainingTodayCardPromise: null,
    nextSessionRefreshPromise: null,
    autopilotMemoryCache: null,
    autopilotMemoryCacheDay: null,
    autopilotMemoryLastFetchTs: 0,
    autopilotMemoryDirty: false,
    autopilotMemoryRenderKey: null,
    autopilotMemoryFilter: "all",
    autopilotMemoryRows: [],
    autopilotMemoryAllRows: [],
    autopilotMemoryDetailsOpen: false,
    autopilotMemorySelectedId: null,
    todayAgendaRenderKey: null,
    calendarFilters: {
      strength: true,
      run: true,
      sick: true,
      deload: true,
      functional_overreach: true,
      rest: true
    }
  };

  // =========================================================
  // FORMAT
  // =========================================================
  function formatNumber(value, decimals = 0) {
    if (value === null || value === undefined || Number.isNaN(value)) return DASH;
    return Number(value).toLocaleString(LOCALE, {
      minimumFractionDigits: decimals,
      maximumFractionDigits: decimals
    });
  }

  function formatSigned(value, decimals = 0) {
    if (value === null || value === undefined || Number.isNaN(value)) return DASH;
    const n = Number(value);
    const sign = n > 0 ? "+" : n < 0 ? "-" : "";
    return `${sign}${formatNumber(Math.abs(n), decimals)}`;
  }

  function formatPace(seconds) {
    if (seconds === null || seconds === undefined || Number.isNaN(seconds)) return DASH;
    const total = Math.round(Number(seconds));
    const mins = Math.floor(total / 60);
    const secs = total % 60;
    return `${mins}:${String(secs).padStart(2, "0")}`;
  }

  function parseIsoDate(value) {
    if (!value) return null;
    const [y, m, d] = String(value).split("-").map((x) => parseInt(x, 10));
    if (!y || !m || !d) return null;
    return new Date(Date.UTC(y, m - 1, d));
  }

  function parseDateTime(value) {
    if (!value) return null;
    const dt = new Date(value);
    if (Number.isNaN(dt.getTime())) return null;
    return dt;
  }

  function parseFlexibleDate(value) {
    if (!value) return null;
    const raw = String(value).trim();
    if (!raw) return null;
    const full = parseDateTime(raw);
    if (full) return full;
    const isoPart = raw.slice(0, 10);
    return parseIsoDate(isoPart);
  }

  function getDateKey(date) {
    if (!date) return null;
    return new Intl.DateTimeFormat("en-CA", {
      timeZone: TIME_ZONE,
      year: "numeric",
      month: "2-digit",
      day: "2-digit"
    }).format(date);
  }

  function getTodayDateUtcKey() {
    // "today" in Berlin -> key -> parseIsoDate as UTC midnight
    const key = getDateKey(new Date());
    return key;
  }

  function shiftDate(date, days) {
    return new Date(date.getTime() + days * MS_DAY);
  }

  function mean(values) {
    const clean = values.filter((v) => v !== null && v !== undefined && Number.isFinite(Number(v)));
    if (!clean.length) return null;
    return clean.reduce((s, v) => s + Number(v), 0) / clean.length;
  }

  function splitByRange(rows, rangeDays, dateFn) {
    const endKey = getTodayDateUtcKey();
    const endDate = parseIsoDate(endKey);
    if (!endDate) return { current: [], previous: [] };

    const days = rangeDays === "all" ? ALL_DAYS : rangeDays;
    const startDate = shiftDate(endDate, -(days - 1));
    const prevEnd = shiftDate(startDate, -1);
    const prevStart = shiftDate(prevEnd, -(days - 1));

    const current = [];
    const previous = [];

    for (const row of rows) {
      const date = dateFn(row);
      if (!date) continue;
      if (date >= startDate && date <= endDate) current.push({ row, date });
      else if (date >= prevStart && date <= prevEnd) previous.push({ row, date });
    }
    return { current, previous };
  }

  // =========================================================
  // state normalization
  // =========================================================
  function statusToLevel(status) {
    if (status === "alert") return 3;
    if (status === "warn") return 2;
    if (status === "ok") return 1;
    return 0;
  }

  function levelToStatus(level) {
    if (level >= 3) return "alert";
    if (level === 2) return "warn";
    if (level === 1) return "ok";
    return "neutral";
  }

  // Backend signals: good/neutral/bad/missing -> Frontend: ok/neutral/bad/missing
  function normalizeSignalState(s){
    if(!s) return "missing";
    if(s === "ok" || s === "good") return "good";     // grün
    if(s === "neutral") return "neutral";             // weiß/gelb je nach CSS
    if(s === "bad") return "bad";                     // rot
    if(s === "present") return "present";             // training/run marker
    if(s === "missing") return "missing";
    return "missing";
  }

  function combineStatus(statuses) {
    const maxLevel = Math.max(0, ...statuses.map(statusToLevel));
    return levelToStatus(maxLevel);
  }

  function getTrendDirection(pct) {
    if (pct === null || pct === undefined || Number.isNaN(pct)) return "flat";
    if (pct > 1) return "up";
    if (pct < -1) return "down";
    return "flat";
  }

  function getTrendIndicator(dir) {
    if (dir === "up") return "->";
    if (dir === "down") return "<-";
    return "-";
  }

  function getReadinessStatus(score) {
    if (score === null || score === undefined || Number.isNaN(score)) return "neutral";
    if (score >= 70) return "ok";
    if (score >= 55) return "warn";
    return "alert";
  }

  function applyTrendAdjustment(status, trendDir) {
    let lvl = statusToLevel(status);
    if (trendDir === "down") lvl += 1;
    if (trendDir === "up") lvl -= 1;
    lvl = Math.max(0, Math.min(3, lvl));
    return levelToStatus(lvl);
  }

  // =========================================================
  // MAKROS SETTINGS (new structure)
  // /api/makros/settings -> { settings: { active_mode, modes: { lean_bulk: {kcal_target, green_tol, yellow_tol}, ... } } }
  // =========================================================
  function getMakrosMode() {
    const s = state.makrosSettings;
    const backendMode = s?.active_mode || "maintenance";
    return backendMode;
  }

  function getMakrosModeConfig() {
    const s = state.makrosSettings;
    const mode = getMakrosMode();
    return s?.modes?.[mode] || null;
  }

  function getTargetKcal() {
    const cfg = getMakrosModeConfig();
    const t = cfg?.kcal_target;
    return Number.isFinite(Number(t)) ? Number(t) : null;
  }

  function getTolGreen() {
    const cfg = getMakrosModeConfig();
    const t = cfg?.green_tol;
    return Number.isFinite(Number(t)) ? Number(t) : null;
  }

  function getTolYellow() {
    const cfg = getMakrosModeConfig();
    const t = cfg?.yellow_tol;
    return Number.isFinite(Number(t)) ? Number(t) : null;
  }

  function isFiniteNum(v) {
    return v !== null && v !== undefined && Number.isFinite(Number(v));
  }

  function getMakrosKcalBounds() {
    const cfg = getMakrosModeConfig();
    const target = cfg?.kcal_target;
    if (!isFiniteNum(target)) return null;

    const greenLow = isFiniteNum(cfg?.green_low) ? Number(cfg.green_low)
      : (isFiniteNum(cfg?.green_tol) ? Number(target) - Number(cfg.green_tol) : null);
    const greenHigh = isFiniteNum(cfg?.green_high) ? Number(cfg.green_high)
      : (isFiniteNum(cfg?.green_tol) ? Number(target) + Number(cfg.green_tol) : null);
    const yellowLow = isFiniteNum(cfg?.yellow_low) ? Number(cfg.yellow_low)
      : (isFiniteNum(cfg?.yellow_tol) ? Number(target) - Number(cfg.yellow_tol) : null);
    const yellowHigh = isFiniteNum(cfg?.yellow_high) ? Number(cfg.yellow_high)
      : (isFiniteNum(cfg?.yellow_tol) ? Number(target) + Number(cfg.yellow_tol) : null);

    if (!isFiniteNum(greenLow) || !isFiniteNum(greenHigh) || !isFiniteNum(yellowLow) || !isFiniteNum(yellowHigh)) return null;
    if (!(yellowLow <= greenLow && greenLow <= Number(target) && Number(target) <= greenHigh && greenHigh <= yellowHigh)) return null;

    return {
      target: Number(target),
      green_low: greenLow,
      green_high: greenHigh,
      yellow_low: yellowLow,
      yellow_high: yellowHigh
    };
  }

  // ENERGY fallback: only used if the backend did not already provide a day-specific state.
  // Mapping: green -> "good", yellow -> "neutral", red -> "bad"
  function energyStateFromMakros(kcalIntake) {
    const bounds = getMakrosKcalBounds();
    if (!bounds || !isFiniteNum(kcalIntake)) return null;
    const kcal = Number(kcalIntake);
    if (kcal >= bounds.green_low && kcal <= bounds.green_high) return "good";
    if (kcal >= bounds.yellow_low && kcal <= bounds.yellow_high) return "neutral";
    return "bad";
  }

  function energyLaneStateFromMakros(laneItems) {
    const arr = Array.isArray(laneItems) ? laneItems : [];
    for (let i = arr.length - 1; i >= 0; i -= 1) {
      const it = arr[i] || {};
      if (it.state && String(it.state) !== "missing") return String(it.state);
      const kcal = it.kcal_intake ?? it.value ?? null;
      const computed = energyStateFromMakros(kcal);
      if (computed) return computed;
    }
    return "missing";
  }

  // =========================================================
  // UI helpers
  // =========================================================
  function setBadge(el, status) {
    if (!el) return;
    el.dataset.status = status || "neutral";
    if (status === "ok") el.textContent = "OK";
    else if (status === "warn") el.textContent = "WARN";
    else if (status === "alert") el.textContent = "KRIT";
    else el.textContent = DASH;
  }

  function setProgress(barEl, pct) {
    if (!barEl) return;
    const ok = pct !== null && pct !== undefined && Number.isFinite(Number(pct));
    const v = ok ? Math.max(0, Math.min(100, Number(pct))) : 0;
    barEl.style.width = `${v}%`;
    const wrap = barEl.parentElement;
    if (wrap) wrap.classList.toggle("is-empty", !ok);
  }

  // =========================================================
  // FETCH
  // =========================================================
  async function safeJson(url) {
    try {
      const res = await fetch(url, { cache: "no-store" });
      if (!res.ok) return null;
      return await res.json();
    } catch (e) {
      return null;
    }
  }

  async function safePostJson(url, body) {
    try {
      const res = await fetch(url, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body || {}),
        cache: "no-store"
      });
      if (!res.ok) return null;
      return await res.json();
    } catch (e) {
      return null;
    }
  }

  function _mapDashboardPayload(snapshot) {
    if (!snapshot || snapshot.ok === false) return null;
    const settingsRes = snapshot.settings || null;
    const makrosSettingsRes = snapshot.makrosSettings || null;
    const plans = Array.isArray(snapshot.plans) ? snapshot.plans : [];
    const plan = Array.isArray(plans)
      ? (plans.find((p) => p.is_active) || plans[0] || null)
      : null;

    return {
      settings: settingsRes,
      makrosSettings: makrosSettingsRes,
      makrosSeries: snapshot.makrosSeries || null,
      hrvLatest: snapshot.hrvLatest || null,
      hrvSeries: snapshot.hrvSeries || null,
      trainingCurrent: snapshot.trainingCurrent || null,
      trainingDouble: snapshot.trainingDouble || null,
      trainingWeek: snapshot.trainingWeek || null,
      runsCurrent: snapshot.runsCurrent || null,
      runsDouble: snapshot.runsDouble || null,
      plan,
      plans,
      planWeek: snapshot.planWeek?.ok ? snapshot.planWeek : null,
      nextSession: snapshot.nextSession || null,
      lastLogs: snapshot.lastLogs?.ok ? snapshot.lastLogs : null,
      signals14: snapshot.signals14?.days ? snapshot.signals14 : null,
      todayAgenda: snapshot.todayAgenda && typeof snapshot.todayAgenda === "object" ? snapshot.todayAgenda : null,
      controlPlane: (snapshot.controlPlane && snapshot.controlPlane.ok) ? snapshot.controlPlane : null,
      blockStatus: snapshot.blockStatus?.ok ? snapshot.blockStatus : null,
      enduranceApproval: snapshot.enduranceApproval?.ok ? snapshot.enduranceApproval : null,
      mealplanToday: snapshot.mealplanToday && typeof snapshot.mealplanToday === "object" ? snapshot.mealplanToday : null,
      today: snapshot.today?.ok ? snapshot.today : null,
      coreDecisionCard: snapshot.coreDecisionCard?.ok ? snapshot.coreDecisionCard : null,
      coreDashboardCard: snapshot.coreDashboardCard?.ok ? snapshot.coreDashboardCard : null,
      coreTrainingCard: snapshot.coreTrainingCard && typeof snapshot.coreTrainingCard === "object" ? snapshot.coreTrainingCard : null,
      trainingTodayCard: snapshot.trainingTodayCard && typeof snapshot.trainingTodayCard === "object" ? snapshot.trainingTodayCard : null,
      generatedAt: snapshot.generated_at || null,
      loading: !!snapshot.loading,
      stale: !!snapshot.stale,
      partial: !!snapshot.partial,
      snapshotHit: !!snapshot.snapshot_hit,
      sourceLatencyMs: Number(snapshot.build_latency_ms || 0),
    };
  }

  function _consumeDashboardBootstrap() {
    const payload = window.__DASHBOARD_BOOTSTRAP__;
    if (!payload || typeof payload !== "object") return null;
    try {
      delete window.__DASHBOARD_BOOTSTRAP__;
    } catch (_) {
      window.__DASHBOARD_BOOTSTRAP__ = null;
    }
    return payload;
  }

  async function fetchDashboardDataFallback(todayKey) {
    const [
      settingsRes,
      makrosSettingsRes,
      makrosSeriesRes,
      hrvLatestRes,
      hrvSeriesRes,
      trainingCurrentRes,
      trainingDoubleRes,
      trainingWeekRes,
      runsCurrentRes,
      runsDoubleRes,
      plansRes,
      planWeekRes,
      nextSessionRes,
      lastLogsRes,
      signalsRes,
      todayRes,
      controlPlaneRes,
      blockStatusRes,
      enduranceApprovalRes,
      todayAgendaRes,
    ] = await Promise.all([
      safeJson("/api/settings"),
      safeJson("/api/makros/settings"),
      safeJson("/api/makros/series?days=60"),
      safeJson("/api/hrv/latest"),
      safeJson("/api/hrv/series?days=60"),
      safeJson("/api/dashboard/training_summary?days=30"),
      safeJson("/api/dashboard/training_summary?days=60"),
      safeJson("/api/dashboard/training_summary?days=7"),
      safeJson("/api/dashboard/runs_summary?days=30"),
      safeJson("/api/dashboard/runs_summary?days=60"),
      safeJson("/api/plans"),
      safeJson(`/api/dashboard/plan_check_week_overview?day=${encodeURIComponent(todayKey)}`),
      safeJson(`/api/next_session?day=${encodeURIComponent(todayKey)}`),
      safeJson("/api/dashboard/last_logs"),
      safeJson("/api/dashboard/signals?days=14"),
      safeJson(`/api/dashboard/today?day=${encodeURIComponent(todayKey)}`),
      safeJson(`/api/dashboard/control_plane?day=${encodeURIComponent(todayKey)}`),
      safeJson("/api/dashboard/block_status"),
      safeJson(`/api/dashboard/endurance-approval?day=${encodeURIComponent(todayKey)}`),
      safeJson(`/api/dashboard/today_agenda?day=${encodeURIComponent(todayKey)}`),
    ]);

    const fallbackSnapshot = {
      ok: true,
      settings: settingsRes || {},
      makrosSettings: makrosSettingsRes || {},
      makrosSeries: makrosSeriesRes || {},
      hrvLatest: hrvLatestRes || {},
      hrvSeries: hrvSeriesRes || {},
      trainingCurrent: trainingCurrentRes || {},
      trainingDouble: trainingDoubleRes || {},
      trainingWeek: trainingWeekRes || {},
      runsCurrent: runsCurrentRes || {},
      runsDouble: runsDoubleRes || {},
      plans: Array.isArray(plansRes) ? plansRes : [],
      planWeek: planWeekRes || {},
      nextSession: nextSessionRes || null,
      lastLogs: lastLogsRes || {},
      signals14: signalsRes || {},
      mealplanToday: null,
      today: todayRes || {},
      controlPlane: controlPlaneRes || {},
      blockStatus: blockStatusRes || {},
      enduranceApproval: enduranceApprovalRes || {},
      todayAgenda: todayAgendaRes || null,
      coreDecisionCard: state.coreDecisionCardCache || null,
      coreDashboardCard: null,
      coreTrainingCard: null,
      partial: true,
      stale: true,
      snapshot_hit: false,
      build_latency_ms: 0,
    };
    return _mapDashboardPayload(fallbackSnapshot);
  }

  async function fetchDashboardData() {
    const todayKey = getTodayDateUtcKey();
    const bootstrap = _consumeDashboardBootstrap();
    const bootstrapPayload = _mapDashboardPayload(bootstrap);
    if (bootstrapPayload) return bootstrapPayload;
    const snapshot = await safeJson(`/api/dashboard_snapshot?day=${encodeURIComponent(todayKey)}`);
    if (snapshot && snapshot.ok !== false) {
      return _mapDashboardPayload(snapshot);
    }
    return fetchDashboardDataFallback(todayKey);
  }

  async function fetchHrvSyncMeta({ force = false } = {}) {
    const fresh = (Date.now() - Number(state.hrvSyncMetaLastFetchTs || 0)) < (5 * 60 * 1000);
    if (!force && fresh && state.hrvSyncMeta) return state.hrvSyncMeta;
    const payload = await safeJson("/api/hrv/recovery?range_days=30&baseline_days=60");
    const meta = payload && payload.ok !== false
      ? {
          lastSyncAt: parseFlexibleDate(payload.last_sync_at),
          polarConnected: !!payload.polar_connected,
        }
      : null;
    if (meta) {
      state.hrvSyncMeta = meta;
      state.hrvSyncMetaLastFetchTs = Date.now();
    }
    return meta;
  }

  const AUTOPILOT_MEMORY_TTL_MS = 10 * 60 * 1000;
  const CORE_DECISION_CARD_TTL_MS = 30 * 1000;
  const MEALPLAN_TTL_MS = 5 * 60 * 1000;
  const ENDURANCE_TTL_MS = 5 * 60 * 1000;

  function _enduranceBadgeText(status) {
    const norm = String(status || "UNKNOWN").toUpperCase();
    if (norm === "GREEN") return "GRÜN";
    if (norm === "YELLOW") return "GELB";
    if (norm === "RED") return "ROT";
    return "GRAU";
  }

  function _loadEnduranceMode() {
    try {
      const raw = String(localStorage.getItem(STORAGE.enduranceMode) || "").trim().toLowerCase();
      if (raw === "ergo" || raw === "laufen") return raw;
    } catch (_) {}
    return "laufen";
  }

  function _saveEnduranceMode(mode) {
    const next = String(mode || "").trim().toLowerCase() === "ergo" ? "ergo" : "laufen";
    state.enduranceMode = next;
    try {
      localStorage.setItem(STORAGE.enduranceMode, next);
    } catch (_) {}
  }

  function _enduranceSportLabel(mode, fallbackSport) {
    const raw = String(fallbackSport || "").trim();
    if (!raw) return "Laufen";
    return /^run$/i.test(raw) ? "Run" : raw;
  }

  function _enduranceRecommendationText(payload, mode, fallbackSport) {
    const base = String(payload?.recommendation || "Noch offen").trim();
    return base;
  }

  function _enduranceReasonText(payload, mode, fallbackSport) {
    const base = String(payload?.reason || payload?.message || "Keine Begründung verfügbar.").trim();
    return base;
  }

  function _enduranceSummaryText(payload, mode, fallbackSport) {
    const base = String(payload?.final_summary || "").trim();
    return base;
  }

  function _executionModeFromPayload(payload) {
    const raw = String(payload?.execution_mode || "").trim().toUpperCase();
    return raw === "ERGO" ? "ergo" : "laufen";
  }

  async function saveEnduranceMode(nextMode, payload) {
    if (!payload || state.enduranceModeSaving) return false;
    const prevMode = _executionModeFromPayload(payload);
    state.enduranceModeSaving = true;
    state.enduranceModeError = "";
    state.enduranceMode = nextMode;
    renderEnduranceData(payload);
    try {
      const res = await safePostJson("/api/dashboard/endurance-approval/mode", {
        date: payload.day || getTodayDateUtcKey(),
        intervals_event_id: payload?.normalized_workout?.raw_event_id || null,
        execution_mode: nextMode === "ergo" ? "ERGO" : "RUN",
      });
      if (!res?.ok || !res?.approval) throw new Error("save_failed");
      state.endurancePayload = res.approval;
      state.enduranceMode = _executionModeFromPayload(res.approval);
      _saveEnduranceMode(state.enduranceMode);
      renderEnduranceData(res.approval);
      return true;
    } catch (_) {
      state.enduranceMode = prevMode;
      state.enduranceModeError = "Auswahl konnte nicht gespeichert werden.";
      renderEnduranceData(payload);
      return false;
    } finally {
      state.enduranceModeSaving = false;
      renderEnduranceData(state.endurancePayload || payload);
    }
  }

  function renderEnduranceSkeleton() {
    if (!els.enduranceBody) return;
    els.enduranceBody.innerHTML = `<div class="endurance-empty">Lade Cardio-Plan…</div>`;
  }

  function renderEnduranceEmpty(message) {
    if (!els.enduranceBody) return;
    state.endurancePayload = null;
    if (els.enduranceSource) els.enduranceSource.textContent = "LIVA · lokal";
    if (els.enduranceStatus) {
      els.enduranceStatus.className = "endurance-status-badge is-unknown";
      els.enduranceStatus.textContent = "Kein Lauf";
    }
    els.enduranceBody.innerHTML = `<div class="endurance-empty">${escapeHtml(message || "Keine Daten verfügbar.")}</div>`;
  }

  function renderEnduranceData(payload) {
    if (!els.enduranceBody) return;
    state.endurancePayload = payload || null;
    const session = payload?.session && typeof payload.session === "object" ? payload.session : null;
    const sync = payload?.sync && typeof payload.sync === "object" ? payload.sync : {};
    if (!payload?.available || !payload?.has_workout || !session) {
      renderEnduranceEmpty(payload?.message || "Kein offener Lauf geplant.");
      return;
    }
    const syncState = String(sync.state || session.sync_state || "not_synced");
    if (els.enduranceStatus) {
      const tone = syncState === "synced" ? "green" : syncState === "sync_error" ? "red" : "yellow";
      els.enduranceStatus.className = `endurance-status-badge is-${tone}`;
      els.enduranceStatus.textContent = syncState === "synced" ? "Sync ✓" : syncState === "sync_error" ? "Syncfehler" : "Sync offen";
    }
    if (els.enduranceSource) els.enduranceSource.textContent = "LIVA · lokal";
    const runDate = new Date(`${session.scheduled_date}T12:00:00`);
    const today = new Date(`${getTodayDateUtcKey()}T12:00:00`);
    const daysAway = Math.round((runDate - today) / 86400000);
    const dateLabel = new Intl.DateTimeFormat("de-DE", { weekday: "long", day: "numeric", month: "long" }).format(runDate);
    const relation = daysAway === 0 ? "Heute" : daysAway === 1 ? "Morgen" : daysAway > 1 ? `In ${daysAway} Tagen` : dateLabel;
    const duration = Number(session.duration_s || 0);
    const distance = Number(session.distance_m || 0);
    const meta = [
      duration ? `${Math.round(duration / 60)} min` : "",
      distance ? `${(distance / 1000).toLocaleString("de-DE", { maximumFractionDigits: 1 })} km` : "",
      payload?.phase?.name || "",
    ].filter(Boolean);
    const steps = Array.isArray(session.execution_steps) ? session.execution_steps : [];
    const weights = steps.map((step) => Number(step.duration_s || step.distance_m || 1));
    const totalWeight = weights.reduce((sum, value) => sum + value, 0) || 1;
    const profile = steps.slice(0, 36).map((step, index) => {
      const kind = String(step.kind || "open").toLowerCase();
      const width = Math.max(1.5, weights[index] / totalWeight * 100);
      return `<i class="is-${escapeHtml(kind)}" style="width:${width}%" title="${escapeHtml(kind)}"></i>`;
    }).join("");
    const note = String(session.notes || "").trim().split(/\n+/)[0];
    const syncText = syncState === "synced"
      ? "Intervals synchronisiert · Garmin über Intervals"
      : syncState === "sync_error" ? "Nicht an Intervals/Garmin übergeben" : "Wartet auf Intervals/Garmin";
    els.enduranceBody.innerHTML = `
      <a class="next-run-link" href="/planung?tab=cardio" aria-label="Cardio-Planung öffnen">
        <div class="next-run-date"><span>${escapeHtml(relation)}</span><time datetime="${escapeHtml(session.scheduled_date)}">${escapeHtml(dateLabel)}</time></div>
        <div class="next-run-title">${escapeHtml(session.title || "Geplanter Lauf")}</div>
        <div class="next-run-meta">${meta.map((item) => `<span>${escapeHtml(item)}</span>`).join("")}</div>
        ${profile ? `<div class="next-run-profile" aria-label="Ablauf der Einheit">${profile}</div>` : ""}
        ${note ? `<p class="next-run-note">${escapeHtml(note)}</p>` : ""}
        <div class="next-run-footer">
          <span class="next-run-sync is-${escapeHtml(syncState)}"><i></i>${escapeHtml(syncText)}</span>
          <span class="next-run-open">Plan öffnen <b>›</b></span>
        </div>
      </a>
    `;
  }

  async function loadEnduranceApproval({ force = false } = {}) {
    if (!els.enduranceBody) return;
    const isFresh = (Date.now() - Number(state.enduranceLastFetchTs || 0)) < ENDURANCE_TTL_MS;
    if (!force && state.endurancePayload && isFresh) return state.endurancePayload;
    if (state.enduranceBusy) return;
    state.enduranceBusy = true;
    if (!state.enduranceLoaded) renderEnduranceSkeleton();
    try {
      const dayKey = getTodayDateUtcKey();
      const payload = await safeJson(`/api/dashboard/endurance-approval?day=${encodeURIComponent(dayKey)}`);
      if (payload) {
        renderEnduranceData(payload);
        state.endurancePayload = payload;
        state.enduranceLastFetchTs = Date.now();
      } else {
        renderEnduranceEmpty("Cardio-Plan aktuell nicht erreichbar.");
      }
      state.enduranceLoaded = true;
    } catch (_) {
      renderEnduranceEmpty("Cardio-Plan aktuell nicht erreichbar.");
      state.enduranceLoaded = true;
    } finally {
      state.enduranceBusy = false;
    }
  }

  async function fetchCoreDecisionCard({ force = false } = {}) {
    const dayKey = getTodayDateUtcKey();
    const dayChanged = !state.coreDecisionCardCacheDay || state.coreDecisionCardCacheDay !== dayKey;
    const stale = (Date.now() - Number(state.coreDecisionCardLastFetchTs || 0)) > CORE_DECISION_CARD_TTL_MS;
    const hasExplainV2 = !!(state.coreDecisionCardCache?.explain_v2 && state.coreDecisionCardCache.explain_v2.ok);
    const allowDecisionCardCache = true;
    if (allowDecisionCardCache && !force && !state.coreDecisionCardDirty && !dayChanged && !stale && hasExplainV2 && state.coreDecisionCardCache) {
      return state.coreDecisionCardCache;
    }
    if (state.coreDecisionCardPromise && !force) return state.coreDecisionCardPromise;
    state.coreDecisionCardPromise = Promise.all([
      safeJson(`/api/core/decision_card?day=${dayKey}`),
      safeJson(`/api/core/v2/explain`).catch(() => null),
      safeJson(`/api/core/dashboard_card_v3?day=${dayKey}`).catch(() => null),
    ]).then(([payload, explainV2, dashboardCard]) => {
      state.coreDecisionCardDirty = false;
      state.coreDecisionCardLastFetchTs = Date.now();
      state.coreDecisionCardCacheDay = dayKey;
      if (payload && payload.ok) {
        const dashboardPayload = (dashboardCard && dashboardCard.ok) ? dashboardCard : null;
        const overrideFlag = dashboardPayload?.override_flag || payload?.override_flag || null;
        const merged = {
          ...payload,
          today_mini: dashboardPayload?.today_mini || payload?.today_mini || null,
          explain_v2: (explainV2 && explainV2.ok) ? explainV2 : null,
          override_flag: overrideFlag,
          dashboard_timeline: dashboardPayload?.timeline?.days || null,
        };
        state.coreDecisionCardCache = merged;
        try {
          sessionStorage.setItem(
            STORAGE.coreDecisionCard,
            JSON.stringify({ day: dayKey, ts: Date.now(), payload: merged })
          );
        } catch (_) {}
        state.coreDecisionPendingMode = null;
        state.coreDecisionPendingSecondary = null;
        state.coreDecisionPendingSecondaryType = null;
        return merged;
      }
      return state.coreDecisionCardCache;
    }).finally(() => {
      state.coreDecisionCardPromise = null;
    });
    return state.coreDecisionCardPromise;
  }

  function loadCoreDecisionCardSessionCache() {
    const dayKey = getTodayDateUtcKey();
    if (!dayKey) return null;
    try {
      const raw = sessionStorage.getItem(STORAGE.coreDecisionCard);
      if (!raw) return null;
      const parsed = JSON.parse(raw);
      if (!parsed || parsed.day !== dayKey) return null;
      const ts = Number(parsed.ts || 0);
      if (Number.isFinite(ts) && ts > 0 && (Date.now() - ts) > (12 * 60 * 60 * 1000)) return null;
      const payload = parsed.payload;
      if (!payload || payload.ok !== true) return null;
      if (!payload.explain_v2 || payload.explain_v2.ok !== true) return null;
      return payload;
    } catch (_) {
      return null;
    }
  }

  function loadMealplanSessionCache() {
    const dayKey = getTodayDateUtcKey();
    if (!dayKey) return null;
    try {
      const raw = sessionStorage.getItem(STORAGE.mealplanToday);
      if (!raw) return null;
      const parsed = JSON.parse(raw);
      if (!parsed || parsed.day !== dayKey) return null;
      if (!parsed.payload || parsed.payload.ok !== true) return null;
      const ts = Number(parsed.ts || 0);
      if (!Number.isFinite(ts) || (Date.now() - ts) > MEALPLAN_TTL_MS) return null;
      return parsed.payload;
    } catch (_) {
      return null;
    }
  }

  async function fetchAutopilotMemory({ force = false } = {}) {
    const dayKey = getTodayDateUtcKey();
    const dayChanged = !state.autopilotMemoryCacheDay || state.autopilotMemoryCacheDay !== dayKey;
    const stale = (Date.now() - Number(state.autopilotMemoryLastFetchTs || 0)) > AUTOPILOT_MEMORY_TTL_MS;
    if (!force && !state.autopilotMemoryDirty && !dayChanged && !stale && state.autopilotMemoryCache) {
      return state.autopilotMemoryCache;
    }
    const payload = await safeJson(`/api/dashboard/autopilot_memory?day=${dayKey}&days=${AUTOPILOT_MEMORY_DAYS}`);
    state.autopilotMemoryDirty = false;
    state.autopilotMemoryLastFetchTs = Date.now();
    state.autopilotMemoryCacheDay = dayKey;
    if (payload && payload.ok) {
      state.autopilotMemoryCache = payload;
      return payload;
    }
    return state.autopilotMemoryCache;
  }

  async function fetchBlockStatus({ force = false, planName = "" } = {}) {
    if (!force && !state.blockStatusDirty && state.blockStatusCache) return state.blockStatusCache;
    const payload = await safeJson("/api/dashboard/block_status");
    if (payload && payload.ok) {
      state.blockStatusCache = payload;
      state.blockStatusDirty = false;
      state.blockStatusCacheDay = getTodayDateUtcKey();
      state.blockStatusPlanName = String(planName || payload.plan_name || "").trim() || null;
      return payload;
    }
    if (state.blockStatusCache) return state.blockStatusCache;
    return { _error: true };
  }

  async function saveCoreDecisionOverride() {
    const card = state.coreDecisionCardCache || {};
    const knobs = card.knobs && typeof card.knobs === "object" ? card.knobs : {};
    const modeObj = knobs.mode && typeof knobs.mode === "object" ? knobs.mode : {};
    const secObj = knobs.secondary && typeof knobs.secondary === "object" ? knobs.secondary : {};
    const hasPendingMode = state.coreDecisionPendingMode !== null && state.coreDecisionPendingMode !== undefined;
    const hasPendingSecondary = state.coreDecisionPendingSecondary !== null && state.coreDecisionPendingSecondary !== undefined;
    if (!hasPendingMode && !hasPendingSecondary) return false;
    let modeValue = String(state.coreDecisionPendingMode || "").trim().toUpperCase();
    let secValue = String(state.coreDecisionPendingSecondary || "").trim().toUpperCase();
    if (modeValue === "HEAVY") modeValue = "PUSH";
    if (secValue === "HEAVY") secValue = "PUSH";

    state.coreDecisionSaving = true;
    renderCoreDecisionCard(card);
    const ops = [];
    const modeTarget = String(modeObj.target || "").trim().toLowerCase();
    const modeSessionId = String(modeObj.session_id || "").trim();
    if (hasPendingMode && modeValue && modeTarget && modeSessionId) {
      ops.push({ session_id: modeSessionId, target: modeTarget, value: modeValue, lock: false });
    }
    const secTarget = String(secObj.target || secObj.type || "").trim().toLowerCase();
    const secSessionId = String(secObj.session_id || "").trim();
    if (hasPendingSecondary && secValue && secTarget && secSessionId) {
      ops.push({ session_id: secSessionId, target: secTarget, value: secValue, lock: false });
    }
    if (!ops.length) {
      state.coreDecisionSaving = false;
      return false;
    }
    let res = null;
    const changedDays = new Set();
    for (const op of ops) {
      res = await safePostJson("/api/core/override", op);
      if (!res || !res.ok) break;
      const day = String(res?.override_day || "");
      if (day) changedDays.add(day);
    }
    state.coreDecisionSaving = false;
    if (!res || !res.ok) {
      state.coreDecisionPendingMode = null;
      state.coreDecisionPendingSecondary = null;
      state.coreDecisionPendingSecondaryType = null;
      _showCoreDecisionInfo("Konnte nicht speichern.", true);
      renderCoreDecisionCard(card);
      return false;
    }
    state.coreDecisionPendingMode = null;
    state.coreDecisionPendingSecondary = null;
    state.coreDecisionPendingSecondaryType = null;
    state.coreDecisionCardDirty = true;
    const fresh = await fetchCoreDecisionCard({ force: true });
    if (fresh && fresh.ok) state.coreDecisionCardCache = fresh;
    const overrideDay = String(res?.override_day || getTodayDateUtcKey() || "");
    try {
      window.dispatchEvent(new CustomEvent("coreDecisionChanged", {
        detail: { day: overrideDay || null, source: "core-override" }
      }));
    } catch (_) {}
    for (const day of changedDays) {
      if (!day || day === overrideDay) continue;
      try {
        window.dispatchEvent(new CustomEvent("coreDecisionChanged", {
          detail: { day, source: "core-override" }
        }));
      } catch (_) {}
    }
    try { pcInit(); } catch (_) {}
    _showCoreDecisionInfo("Override gespeichert.", false);
    renderCoreDecisionCard(state.coreDecisionCardCache || fresh || card);
    return true;
  }

  // =========================================================
  // BUILD DATA
  // =========================================================
  function buildData(payload) {
    const range = state.rangeDays;
    const rangeNum = range === "all" ? ALL_DAYS : range;

    // ---------- NUTRITION ----------
    const bounds = getMakrosKcalBounds();
    const targetKcal = bounds ? bounds.target : getTargetKcal();

    const nutritionRows = payload.makrosSeries?.data || [];
    const nutSplit = splitByRange(nutritionRows, range, (r) => parseIsoDate(r.date_iso));
    const curNut = nutSplit.current.map((x) => x.row);

    const kcalRows = curNut.filter((r) => r.calories !== null && r.calories !== undefined);
    const avgKcal = mean(kcalRows.map((r) => r.calories));

    const deltaKcal = (avgKcal !== null && targetKcal !== null) ? (avgKcal - targetKcal) : null;

    const inBand = (bounds && targetKcal !== null)
      ? kcalRows.filter((r) => {
        const c = Number(r.calories);
        return Number.isFinite(c) && c >= bounds.yellow_low && c <= bounds.yellow_high;
      }).length
      : null;

    const bandPct = (inBand !== null && kcalRows.length > 0)
      ? (inBand / kcalRows.length) * 100
      : null;

    // weight trend kg/week
    const weightRows = curNut
      .filter((r) => r.bodyweight_kg !== null && r.bodyweight_kg !== undefined && r.date_iso)
      .map((r) => ({ date: parseIsoDate(r.date_iso), v: Number(r.bodyweight_kg) }))
      .filter((x) => x.date && Number.isFinite(x.v))
      .sort((a, b) => a.date - b.date);

    let weightTrend = null;
    if (weightRows.length >= 2) {
      const first = weightRows[0], last = weightRows[weightRows.length - 1];
      const daySpan = (last.date - first.date) / MS_DAY;
      if (daySpan > 0) weightTrend = (last.v - first.v) / (daySpan / 7);
    }

    // protein hit rate
    const PROTEIN_TARGET_PER_KG = 2.0;
    const avgWeight = mean(weightRows.map((x) => x.v));
    const proteinRows = curNut.filter((r) => r.protein_g !== null && r.protein_g !== undefined);

    let proteinMet = 0;
    let proteinTotal = 0;
    const gkg = [];
    for (const r of proteinRows) {
      const p = Number(r.protein_g);
      if (!Number.isFinite(p)) continue;
      const w = (r.bodyweight_kg !== null && r.bodyweight_kg !== undefined) ? Number(r.bodyweight_kg) : avgWeight;
      if (!Number.isFinite(w)) continue;
      const val = p / w;
      gkg.push(val);
      proteinTotal += 1;
      if (val >= PROTEIN_TARGET_PER_KG) proteinMet += 1;
    }
    const avgGPerKg = mean(gkg);
    const proteinRate = proteinTotal ? (proteinMet / proteinTotal) : null;

    // ---------- HRV ----------
    const hrvSeries = payload.hrvSeries?.data || [];
    const hrvSplit = splitByRange(hrvSeries, range, (r) => parseDateTime(r.date_utc || r.ts_measurement));
    const curHrv = hrvSplit.current.map((x) => x.row);
    const prevHrv = hrvSplit.previous.map((x) => x.row);

    const rmssdCur = mean(curHrv.map((r) => r.rmssd).filter((x) => Number.isFinite(Number(x))));
    const rmssdPrev = mean(prevHrv.map((r) => r.rmssd).filter((x) => Number.isFinite(Number(x))));
    const rmssdTrendPct = rmssdPrev ? ((rmssdCur - rmssdPrev) / rmssdPrev) * 100 : null;
    const trendDir = getTrendDirection(rmssdTrendPct);

    const readinessLatest = payload.hrvLatest?.data?.readiness_score;
    const readinessScore = (readinessLatest !== null && readinessLatest !== undefined)
      ? Number(readinessLatest)
      : null;

    const readinessStatus = applyTrendAdjustment(getReadinessStatus(readinessScore), trendDir);
    let lastHrvDate = parseFlexibleDate(payload.hrvLatest?.data?.date_utc || payload.hrvLatest?.data?.ts_measurement);
    if (!lastHrvDate) {
      for (let i = hrvSeries.length - 1; i >= 0; i -= 1) {
        const row = hrvSeries[i] || {};
        const fallbackDate = parseFlexibleDate(row.date_utc || row.ts_measurement || row.date || row.day);
        if (fallbackDate) {
          lastHrvDate = fallbackDate;
          break;
        }
      }
    }

    // ---------- TRAINING / RUNS ----------
    const plan = payload.plan;
    const strengthDays = plan?.summary?.strength_days || 0;
    const runDays = plan?.summary?.run_days || 0;

    const strengthExpected = strengthDays ? Math.max(1, Math.round(strengthDays * (rangeNum / 7))) : 0;
    const runsExpected = runDays ? Math.max(1, Math.round(runDays * (rangeNum / 7))) : 0;

    const trainingSessions = payload.trainingCurrent?.sessions ?? 0;
    const trainingTonnage = payload.trainingCurrent?.tonnage ?? 0;

    const runsCount = payload.runsCurrent?.runs ?? 0;
    const runsTotalKm = payload.runsCurrent?.total_km ?? 0;
    const runsAvgPace = payload.runsCurrent?.avg_pace_s ?? null;

    // simple status (plan adherence)
    const trainingStatus = strengthExpected > 0
      ? (trainingSessions / strengthExpected >= 0.85 ? "ok" : trainingSessions / strengthExpected >= 0.7 ? "warn" : "alert")
      : "neutral";

    const runsStatus = runsExpected > 0
      ? (runsCount / runsExpected >= 0.85 ? "ok" : runsCount / runsExpected >= 0.7 ? "warn" : "alert")
      : "neutral";

    // ---------- SIGNALS (14d) ----------
    const signals14 = payload.signals14;

    // last logs
    const lastTrainingDate = parseIsoDate(payload.lastLogs?.training?.date_iso);
    const lastRunDate = parseDateTime(payload.lastLogs?.runs?.date);
    const lastNutritionDate = parseIsoDate(payload.lastLogs?.nutrition?.date_iso);

    // plan today / next / open
    const planWeek = payload.planWeek || {};
    const sessions = planWeek.sessions || [];
    const todayInfo = planWeek.today || {};
    const hasPlan = Boolean(plan && plan.name);

    const todaySession = hasPlan ? (todayInfo.today_session_name || "Rest") : DASH;
    const nextSession = hasPlan
      ? pickSessionLabel(sessions, todayInfo.default_session_key, todayInfo.default_session_name || DASH)
      : DASH;

    const openSessions = strengthDays
      ? Math.max(0, strengthDays - (payload.trainingWeek?.sessions || 0))
      : 0;

    // kcal/weight/protein statuses (simple)
    const kcalStatus = (deltaKcal === null || targetKcal === null || !bounds)
      ? "neutral"
      : (() => {
        const avg = Number(targetKcal) + Number(deltaKcal);
        if (avg >= bounds.green_low && avg <= bounds.green_high) return "ok";
        if (avg >= bounds.yellow_low && avg <= bounds.yellow_high) return "warn";
        return "alert";
      })();

    const weightStatus = (weightTrend === null)
      ? "neutral"
      : "warn"; // bewusst soft (kannst du später mode-basiert machen)

    const proteinStatus = (proteinRate === null)
      ? "neutral"
      : (proteinRate >= 0.8 ? "ok" : proteinRate >= 0.6 ? "warn" : "alert");

    return {
      rangeLabel: RANGE_MAP[range] || String(range),

      planName: plan?.name || "Kein aktiver Plan",
      todaySession,
      nextSession,
      openSessions,

      readinessScore,
      readinessStatus,
      rmssdTrendPct,
      trendDir,

      trainingSessions,
      trainingTonnage,
      strengthExpected,
      trainingStatus,

      runsCount,
      runsTotalKm,
      runsAvgPace,
      runsExpected,
      runsStatus,

      avgKcal,
      deltaKcal,
      bandPct,
      inBand,
      kcalDays: kcalRows.length,
      kcalStatus,

      weightTrend,
      weightStatus,

      proteinMet,
      proteinTotal,
      proteinRate,
      avgGPerKg,
      proteinStatus,

      lastTrainingDate,
      lastRunDate,
      lastNutritionDate,
      lastHrvDate,

      signals14
    };
  }

  function pickSessionLabel(sessions, key, fallback) {
    if (!Array.isArray(sessions) || !key) return fallback || DASH;
    const t = sessions.find((s) => s.session_key === key) || sessions.find((s) => s.session_name === key);
    if (!t) return fallback || DASH;
    const day = t.planned_weekday ? ` (${t.planned_weekday})` : "";
    return `${t.session_name}${day}`;
  }

  // =========================================================
  // RENDER: Pills + Top Cards
  // =========================================================
  function setPills(data) {
    const planText = `Plan: ${data.planName ? data.planName : DASH}`;
    const hrvText = `HRV: ${fmtLastPillDate(data.lastHrvDate)}`;
    const trainingText = `Gym: ${fmtLastPillDate(data.lastTrainingDate)}`;
    const runText = `Run: ${fmtLastPillDate(data.lastRunDate)}`;
    const nutritionText = `Nutrition: ${fmtLastPillDate(data.lastNutritionDate)}`;

    updatePillText("plan", els.pillPlan, planText);
    updatePillText("hrv", els.pillHrv, hrvText);
    updatePillText("training", els.pillTraining, trainingText);
    updatePillText("run", els.pillRun, runText);
    updatePillText("nutrition", els.pillNutrition, nutritionText);
  }

  function updateHrvPillFromSyncMeta(syncMeta, fallbackData = null) {
    if (!els.pillHrv) return;
    const syncDate = syncMeta?.lastSyncAt || null;
    if (!syncDate) {
      if (fallbackData) updatePillText("hrv", els.pillHrv, `HRV: ${fmtLastPillDate(fallbackData.lastHrvDate)}`);
      return;
    }
    updatePillText("hrv", els.pillHrv, `HRV: ${fmtLastPillDate(syncDate)}`);
  }

  function fmtLastPillDate(dateObj) {
    function fmtShortDdMm(dateKey) {
      if (!dateKey) return null;
      const parts = String(dateKey).split("-");
      if (parts.length !== 3) return null;
      return `${parts[2]}.${parts[1]}`;
    }
    if (!dateObj) return DASH;
    const todayKey = getDateKey(new Date());
    const key = getDateKey(dateObj);
    if (!key || !todayKey) return DASH;
    if (key === todayKey) return "Heute";
    const yKey = getDateKey(shiftDate(parseIsoDate(todayKey), -1));
    const yyKey = getDateKey(shiftDate(parseIsoDate(todayKey), -2));
    if (key === yKey) return "Gestern";
    if (key === yyKey) return "Vorgestern";
    return fmtShortDdMm(key) || DASH;
  }

  function updatePillText(key, el, text) {
    if (!el) return;
    if (state.pillTextCache[key] === text) return;
    state.pillTextCache[key] = text;
    setText(el, text);
  }

  function setTopCards(data) {
    // HRV
    setText(els.hrvValue, data.readinessScore !== null ? `Readiness ${Math.round(data.readinessScore)}` : `Readiness ${DASH}`);
    setText(els.hrvSub, data.rmssdTrendPct !== null ? `RMSSD-Trend: ${formatSigned(data.rmssdTrendPct, 1)}%` : `RMSSD-Trend: ${DASH}`);
    setBadge(els.hrvBadge, data.readinessStatus);
    setProgress(els.hrvProgress, data.readinessScore);

    // Training
    setText(els.trainingValue, `${formatNumber(data.trainingSessions, 0)} Einheiten`);
    setText(els.trainingSub, data.trainingTonnage ? `Tonnage: ${formatNumber(Number(data.trainingTonnage) / 1000, 1)} t` : `Tonnage: ${DASH}`);
    setBadge(els.trainingBadge, data.trainingStatus);
    const tp = data.strengthExpected > 0 ? (data.trainingSessions / data.strengthExpected) * 100 : null;
    setProgress(els.trainingProgress, tp);

    // Runs
    setText(els.runsValue, data.runsTotalKm ? `${formatNumber(data.runsTotalKm, 1)} km` : `${formatNumber(data.runsCount, 0)} Runs`);
    setText(els.runsSub, data.runsAvgPace ? `Ø Pace: ${formatPace(data.runsAvgPace)}` : `Ø Pace: ${DASH}`);
    setBadge(els.runsBadge, data.runsStatus);
    const rp = data.runsExpected > 0 ? (data.runsCount / data.runsExpected) * 100 : null;
    setProgress(els.runsProgress, rp);

    // Nutrition
    setText(els.nutritionValue, data.deltaKcal !== null ? `Δ kcal: ${formatSigned(data.deltaKcal, 0)}` : `Δ kcal: ${DASH}`);
    setText(els.nutritionSub, data.weightTrend !== null ? `Gewichttrend: ${formatSigned(data.weightTrend, 1)} kg/Woche` : `Gewichttrend: ${DASH}`);
    setBadge(els.nutritionBadge, combineStatus([data.kcalStatus, data.weightStatus]));
    setProgress(els.nutritionProgress, data.bandPct);
  }

  // =========================================================
  // RENDER: Plan summary
  // =========================================================
  function setPlanSummary(data) {
    setText(els.planToday, data.todaySession || DASH);
    setText(els.planNext, data.nextSession || DASH);

    if (data.openSessions > 0) {
      setText(els.planOpen, String(data.openSessions));
      if (els.planOpenRow) els.planOpenRow.style.display = "";
    } else if (els.planOpenRow) {
      els.planOpenRow.style.display = "none";
    }
  }

  function renderCoreTrainingCard(payload) {
    const host = els.coreTrainingCard;
    if (!host) return;
    const selectRow = document.querySelector("#plan-check-card .plan-check-select-row");
    const legacyLink = pcEl("plan-check-link");
    const legacyExercises = pcEl("plan-check-exercises");
    const legacyMessage = pcEl("plan-check-message");
    const card = payload?.training_card || payload?.coreTrainingCard || null;
    try {
      console.debug("[dashboard] core training card", {
        hasCard: !!card,
        title: card?.title || null,
        itemCount: Array.isArray(card?.items) ? card.items.length : 0,
        source: payload?.training_card ? "api" : payload?.coreTrainingCard ? "bootstrap" : "empty",
        compactAdjustments: card?.compact_adjustments || [],
        itemDebug: Array.isArray(card?.items) ? card.items.slice(0, 6).map((item) => ({
          exercise_name: item?.exercise_name || item?.display_name || null,
          device: item?.device || null,
          target_sets: item?.target_sets || item?.recommended_sets || null,
          target_reps: item?.target_reps || item?.rep_target || null,
          target_weight: item?.target_weight || item?.weight_target || null,
          target_rpe: item?.target_rpe || item?.rpe_cap || null,
          reference_weight: item?.reference_weight || item?.history_weight || null,
          reference_reps: item?.reference_reps || item?.history_reps || null,
          reference_rpe: item?.reference_rpe || item?.history_rpe || null,
          adjustment_type: item?.adjustment_type || item?.progression_hint || null,
          adjustment_reason: item?.adjustment_reason || item?.note || null,
        })) : [],
      });
    } catch (_) {}
    const intentLabel = {
      full_send: "frei",
      train_normal: "normal",
      train_controlled: "kontrolliert",
      reduce_volume: "reduziert",
      technique_only: "technisch",
      recovery_only: "recovery",
      hard_stop: "stopp",
      unknown: "offen",
    };
    if (!card) {
      host.hidden = true;
      host.innerHTML = "";
      if (selectRow) selectRow.hidden = true;
      if (legacyLink) legacyLink.hidden = false;
      if (legacyExercises) legacyExercises.hidden = false;
      if (legacyMessage) legacyMessage.hidden = false;
      return;
    }
    host.hidden = false;
    if (selectRow) selectRow.hidden = true;
    if (legacyLink) legacyLink.hidden = true;
    if (legacyExercises) { legacyExercises.hidden = true; legacyExercises.innerHTML = ""; }
    if (legacyMessage) { legacyMessage.hidden = true; legacyMessage.textContent = ""; }
    const items = Array.isArray(card.items) ? card.items : [];
    const metaBits = ["aus CORE Board abgeleitet"];
    if (card.decision_intent && intentLabel[card.decision_intent]) metaBits.push(intentLabel[card.decision_intent]);
    const quality = payload?.training_card?.data_quality || {};
    if (Number.isFinite(Number(quality?.confidence_pct))) metaBits.push(`${Math.round(Number(quality.confidence_pct))} % Sicherheit`);

    const gymItems = items.filter((item) => (item?.item_type || "") === "exercise");
    const otherItems = items.filter((item) => (item?.item_type || "") !== "exercise");

    const compactPieces = (parts) => parts.filter((part) => String(part || "").trim()).join(" · ");
    const normalizeNote = (value) => String(value || "").replace(/\s+/g, " ").trim();
    const isGenericWorkingWeight = (value) => {
      const text = String(value || "").trim().toLowerCase();
      return text.includes("letztes gutes arbeitsgewicht") || text.includes("last_good_working_weight");
    };
    const normalizeVisibleWeight = (value) => {
      const text = String(value || "").trim();
      if (!text) return "";
      return isGenericWorkingWeight(text) ? "kein sicheres Zielgewicht · Verlauf prüfen" : text;
    };
    const collectRepeatedNotes = (rows) => {
      const counts = new Map();
      for (const row of rows) {
        const note = normalizeNote(row?.note);
        if (!note) continue;
        counts.set(note, (counts.get(note) || 0) + 1);
      }
      return new Set(Array.from(counts.entries()).filter(([, count]) => count >= 2).map(([note]) => note));
    };
    const repeatedItemNotes = collectRepeatedNotes(gymItems);
    const globalGuidance = [];
    const addGlobalGuidance = (value) => {
      const note = normalizeNote(value);
      if (!note || globalGuidance.includes(note)) return;
      globalGuidance.push(note);
    };
    addGlobalGuidance(card.global_hint);
    addGlobalGuidance(card.footer_note);
    repeatedItemNotes.forEach((note) => addGlobalGuidance(note));

    const compactAdjustments = Array.isArray(card.compact_adjustments)
      ? card.compact_adjustments.filter((line) => line && !isGenericWorkingWeight(line)).slice(0, 3)
      : [];
    if (!compactAdjustments.length) {
      gymItems.forEach((item) => {
        if (compactAdjustments.length >= 3) return;
        const label = String(item?.display_name || "Übung");
        const note = normalizeNote(item?.adjustment_reason || item?.note);
        const progression = String(item?.adjustment_type || item?.progression_hint || "").trim();
        const weight = normalizeVisibleWeight(item?.target_weight || item?.weight_target);
        if (weight && weight !== "kein sicheres Zielgewicht · Verlauf prüfen") {
          compactAdjustments.push(`${label}: ${weight} anpeilen · ${note || (progression === "Gewicht ↑" ? "nur bei normalem Warm-up" : progression || "kontrolliert")}`.trim());
        } else if (note) {
          compactAdjustments.push(`${label}: ${note}`);
        } else if (weight) {
          compactAdjustments.push(`${label}: ${weight}`);
        }
      });
    }

    const cardioRows = otherItems.map((item) => {
      const type = String(item?.item_type || "").toLowerCase();
      if (type === "cardio") {
        const line = compactPieces([
          item?.duration_target ? String(item.duration_target) : "",
          item?.hr_target ? String(item.hr_target).replace(" respektieren", "") : "",
          item?.pace_target || "",
        ]);
        return `
          <div class="dashboard-core-training-compact-row">
            <strong>${escapeHtml(String(item?.display_name || "Cardio"))}</strong>
            <span>${escapeHtml(line || String(item?.target || "ruhig und kontrolliert"))}</span>
          </div>
        `;
      }
      if (type === "mobility") {
        return `
          <div class="dashboard-core-training-compact-row">
            <strong>${escapeHtml(String(item?.display_name || "Mobility"))}</strong>
            <span>${escapeHtml(compactPieces([String(item?.target || "kurz & sauber"), "danach abhaken"]))}</span>
          </div>
        `;
      }
      return `
        <article class="dashboard-core-training-support">
          <strong>${escapeHtml(String(item?.display_name || "Heute"))}</strong>
          <span>${escapeHtml(String(item?.target || item?.duration_target || "aus Plan übernehmen"))}</span>
          ${item?.note ? `<div class="dashboard-core-training-note">${escapeHtml(String(item.note))}</div>` : ""}
        </article>
      `;
    }).join("");

    const modeLabel = intentLabel[card.decision_intent] || intentLabel.unknown;
    const ruleText = String(card.rule || globalGuidance[0] || card.summary || "CORE entscheidet heute kleinschrittig.").trim();
    const whyLines = Array.isArray(card.why_lines) ? card.why_lines.filter(Boolean).slice(0, 4) : [];
    const adjustmentHtml = compactAdjustments.length
      ? `<div class="dashboard-core-training-adjustments"><div class="dashboard-core-training-section-label">Anpassungen</div>${compactAdjustments.map((line) => `<div class="dashboard-core-training-adjustment">${escapeHtml(String(line))}</div>`).join("")}</div>`
      : "";
    const whyHtml = whyLines.length
      ? `<details class="dashboard-core-training-why"><summary>Warum diese Vorgaben?</summary><div class="dashboard-core-training-why-list">${whyLines.map((line) => `<div class="dashboard-core-training-why-item">${escapeHtml(String(line))}</div>`).join("")}</div></details>`
      : "";
    let bodyHtml = "";
    if (card.session_type === "gym") {
      bodyHtml = `${adjustmentHtml}${whyHtml}` || `<div class="dashboard-core-training-empty">Training-Card ist gebaut, aber noch ohne konkrete Hinweise.</div>`;
    } else if (card.session_type === "run" || card.session_type === "ergo" || card.session_type === "recovery" || card.session_type === "hard_stop") {
      bodyHtml = `${cardioRows}${whyHtml}` || `<div class="dashboard-core-training-empty">Training-Card ist gebaut, aber noch ohne konkrete Hinweise.</div>`;
    } else {
      bodyHtml = `${adjustmentHtml || cardioRows}${whyHtml}` || `<div class="dashboard-core-training-empty">Training-Card ist gebaut, aber noch ohne konkrete Hinweise.</div>`;
    }
    host.className = "dashboard-core-training-card";
    if (card.session_type === "gym") {
      host.hidden = false;
      host.classList.add("is-inline-footer");
      host.innerHTML = `
        <details class="dashboard-core-training-why">
          <summary>CORE-Regel für heute</summary>
          <div class="dashboard-core-training-why-list">
            <div class="dashboard-core-training-why-item">${escapeHtml(ruleText)}</div>
            ${whyLines.map((line) => `<div class="dashboard-core-training-why-item">${escapeHtml(String(line))}</div>`).join("")}
          </div>
        </details>
      `;
      return;
    }
    host.innerHTML = `
      <div class="dashboard-core-training-head">
        <div>
          <span class="card-eyebrow">CORE Training-Card</span>
          <div class="dashboard-core-training-title">${escapeHtml(String(card.title || "Training"))}</div>
          <div class="dashboard-core-training-meta">${metaBits.map((part) => escapeHtml(String(part))).join(" · ")}</div>
        </div>
        <a href="/core" class="dashboard-core-training-link">CORE öffnen →</a>
      </div>
      <div class="dashboard-core-training-mode-line">${escapeHtml(String(card.session_label || "Training"))} · ${escapeHtml(modeLabel)}</div>
      <div class="dashboard-core-training-global-hint"><strong>Regel:</strong> ${escapeHtml(ruleText)}</div>
      ${card.adapted_from_plan ? `<div class="dashboard-core-training-adaptation">${escapeHtml(String(card.adaptation_note || `CORE-Anpassung: ${card.planned_session_label || "Plan"} → ${card.recommended_session_label || card.session_label || "Empfehlung"}`))}</div>` : ""}
      ${!card.adapted_from_plan && card.board_consistency?.status === "mismatch" ? `<div class="dashboard-core-training-adaptation">${escapeHtml(String(`Training-Card folgt dem Plan: ${card.recommended_session_label || card.planned_session_label || card.session_label || "Training"}.`))}</div>` : ""}
      <p class="dashboard-core-training-summary">${escapeHtml(String(card.summary || ""))}</p>
      <div class="dashboard-core-training-items dashboard-core-training-items-${escapeHtml(String(card.session_type || "unknown"))}">${bodyHtml}</div>
      ${globalGuidance.slice(1).length ? `<div class="dashboard-core-training-foot">${escapeHtml(globalGuidance.slice(1).join(" "))}</div>` : ""}
    `;
  }

  async function fetchTrainingTodayCard(dayKey) {
    const key = String(dayKey || getTodayDateUtcKey() || "").trim();
    if (!key) return null;
    const payload = await safeJson(`/api/dashboard/training_today_card?date=${encodeURIComponent(key)}`);
    if (!payload || payload.ok === false) return null;
    return payload;
  }

  function _setTrainingTodayCardVisibleHost(host) {
    if (!host) return;
    host.hidden = false;
    host.className = "dashboard-core-training-card";
    host.dataset.gptTrainingCard = "1";
    host.style.setProperty("display", "grid", "important");
    host.style.setProperty("visibility", "visible");
    host.style.setProperty("opacity", "1");
  }

  function _renderTrainingTodayCardError(host, title, message) {
    if (!host) return false;
    const esc = (value) => String(value ?? "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
    _setTrainingTodayCardVisibleHost(host);
    host.innerHTML = `
      <section class="training-core-forecast" data-status="failed">
        <div class="training-core-forecast-header">
          <div class="training-core-forecast-title-row">
            <div class="training-core-forecast-title">
              <div class="training-core-forecast-title-main">GPT-Coaching</div>
            </div>
            <div class="training-plan-badge badge-neutral is-hold">failed</div>
          </div>
          <div class="training-core-forecast-mode">${esc(title || "Training-Card Renderfehler")}</div>
          <div class="training-core-forecast-sub">
            <div class="training-core-forecast-line">${esc(message || "Unbekannter Fehler beim Rendern.")}</div>
          </div>
        </div>
      </section>
    `;
    return true;
  }

  function formatTrainingPrescriptionDisplay(raw) {
    const text = String(raw || "").trim();
    if (!text) return text;
    const formatted = text
      .split(/\s*;\s*/)
      .map((part) => {
        const value = String(part || "").trim();
        if (!value) return "";
        const match = value.match(/^(.+?)\s*kg\s*x\s*(.+?)\s*@\s*RPE\s*(.+)$/i);
        if (!match) return value;
        const load = String(match[1] || "").trim();
        const reps = String(match[2] || "").trim();
        const rpe = String(match[3] || "").trim();
        if (!load || !reps || !rpe) return value;
        return `${reps} x ${load} @ ${rpe}`;
      })
      .filter(Boolean);
    return formatted.length ? formatted.join(" · ") : text;
  }

  function setTrainingTodayCardLoading(message) {
    const host = els.coreTrainingCard;
    if (!host) return;
    _setTrainingTodayCardVisibleHost(host);
    host.innerHTML = `
      <section class="training-core-forecast" data-status="loading">
        <div class="training-core-forecast-header">
          <div class="training-core-forecast-title-row">
            <div class="training-core-forecast-title">
              <div class="training-core-forecast-title-main">GPT-Coaching</div>
            </div>
            <div class="training-plan-badge badge-neutral is-hold">loading</div>
          </div>
          <div class="training-core-forecast-sub">
            <div class="training-core-forecast-line">${String(message || "GPT-Coaching wird geladen …")}</div>
          </div>
        </div>
      </section>
    `;
  }

  function trainingCardBadgeView(actionRaw, badgeRaw) {
    const action = String(actionRaw || "normal").trim().toLowerCase() || "normal";
    const badge = String(badgeRaw || "").trim();
    const byAction = {
      progression: { label: "Progress", badgeClass: "badge-green", stateClass: "is-up" },
      normal: { label: badge || "Normal", badgeClass: "badge-neutral", stateClass: "is-hold" },
      reduced: { label: "Kontrolliert", badgeClass: "badge-yellow", stateClass: "is-reworked" },
      skip: { label: "Pause", badgeClass: "badge-red", stateClass: "is-down" },
      replace: { label: "Ersetzen", badgeClass: "badge-yellow", stateClass: "is-reworked" },
      technique: { label: "Technik", badgeClass: "badge-yellow", stateClass: "is-reworked" },
      test_load: { label: "Test", badgeClass: "badge-green", stateClass: "is-up" },
      no_history: { label: "Neu", badgeClass: "badge-neutral", stateClass: "is-hold" },
    };
    const fallback = byAction[action] || byAction.normal;
    return {
      label: badge || fallback.label,
      badgeClass: fallback.badgeClass,
      stateClass: fallback.stateClass,
    };
  }

  function renderTrainingTodayCard(cardPayload, options) {
    const host = els.coreTrainingCard;
    if (!host) return false;
    const payload = cardPayload && typeof cardPayload === "object" ? cardPayload : null;
    const status = String(payload?.status || "").trim().toLowerCase();
    if (!payload || !["fresh", "missing", "stale", "failed"].includes(status)) return false;

    const esc = (value) => String(value ?? "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");

    const selectRow = document.querySelector("#plan-check-card .plan-check-select-row");
    const legacyLink = pcEl("plan-check-link");
    const legacyExercises = pcEl("plan-check-exercises");
    const legacyMessage = pcEl("plan-check-message");
    const title = String(payload?.title || "Nächste geplante Einheit · —").trim() || "Nächste geplante Einheit · —";
    const subtitle = String(payload?.subtitle || "").trim();
    const sourceLabel = String(payload?.source_label || "GPT-Coaching").trim() || "GPT-Coaching";
    const headline = String(payload?.headline || "").trim();
    const globalNote = String(payload?.global_note || "").trim();
    const fallback = payload?.fallback && typeof payload.fallback === "object" ? payload.fallback : {};
    const fallbackMessage = String(fallback.message || "").trim();
    const items = Array.isArray(payload?.items) ? payload.items : [];

    try {
      const infoLines = [];
      if (headline) infoLines.push(`<div class="training-core-forecast-mode">${esc(headline)}</div>`);
      if (globalNote) infoLines.push(`<div class="training-core-forecast-sub"><div class="training-core-forecast-line">${esc(globalNote)}</div></div>`);
      if (subtitle) infoLines.push(`<div class="training-core-forecast-sub"><div class="training-core-forecast-line">${esc(subtitle)}</div></div>`);
      if (status === "missing") infoLines.push(`<div class="training-core-forecast-sub"><div class="training-core-forecast-line">${esc(fallbackMessage || "GPT-Entscheidung fehlt – Morning Check-in ausführen oder Entscheidung generieren.")}</div></div>`);
      if (status === "failed") infoLines.push(`<div class="training-core-forecast-sub"><div class="training-core-forecast-line">${esc(fallbackMessage || "GPT-Entscheidung konnte nicht geladen werden.")}</div></div>`);

      const rowsHtml = (status === "fresh" || status === "stale")
        ? items.map((item) => {
            const exercise = esc(String(item?.exercise || "Übung").trim() || "Übung");
            const left = esc(formatTrainingPrescriptionDisplay(String(item?.left || "—").trim() || "—"));
            const middleNote = esc(String(item?.middle_note || "").trim());
            const action = esc(String(item?.action || "normal").trim().toLowerCase() || "normal");
            const muted = !!item?.muted;
            const badgeView = trainingCardBadgeView(item?.action, item?.badge);
            return `
              <div class="training-plan-row training-gpt-row${muted ? " is-muted" : ""}" data-action="${action}">
                <div class="training-plan-name">${exercise}</div>
                <div class="training-plan-badge ${badgeView.badgeClass} ${badgeView.stateClass}">${esc(badgeView.label || "Normal")}</div>
                <div class="training-plan-target">${left}</div>
                <div class="training-plan-reference">${middleNote}</div>
              </div>
            `;
          }).join("")
        : "";

      const html = `
        <section class="training-core-forecast" data-status="${esc(status)}">
          <div class="training-core-forecast-header">
            <div class="training-core-forecast-title-row">
              <div class="training-core-forecast-title">
                <div class="training-core-forecast-title-main">${esc(sourceLabel)}</div>
              </div>
              <div class="training-plan-badge badge-neutral is-hold">${esc(status)}</div>
            </div>
            ${infoLines.join("")}
          </div>
          ${rowsHtml ? `<div class="training-plan-list">${rowsHtml}</div>` : ""}
        </section>
      `;

      _setTrainingTodayCardVisibleHost(host);
      host.innerHTML = html;

      if (els.planCheckCardTitle) {
        els.planCheckCardTitle.textContent = title;
      }
      if (selectRow) selectRow.hidden = true;
      if (legacyLink) {
        legacyLink.hidden = false;
        legacyLink.textContent = subtitle || "Letzte Ausführung: —";
        legacyLink.removeAttribute("href");
      }
      if (legacyExercises) {
        legacyExercises.hidden = true;
        legacyExercises.innerHTML = "";
      }
      if (legacyMessage) {
        legacyMessage.hidden = true;
        legacyMessage.textContent = "";
      }

      state.trainingCardHeadlineStatus = sourceLabel;
      state.trainingTodayCardPayload = payload;
      state.trainingTodayCardRenderActive = true;
      try { console.debug("[training-card] rendered", status, options?.source || "default", payload); } catch (_) {}
      return true;
    } catch (err) {
      state.trainingTodayCardRenderActive = false;
      try { console.error("[training-card] render failed", err, payload); } catch (_) {}
      return _renderTrainingTodayCardError(host, "Training-Card Renderfehler", err?.message || String(err || "Unbekannter Fehler"));
    }
  }

  function hasActiveTrainingTodayCard() {
    const payload = state.trainingTodayCardPayload && typeof state.trainingTodayCardPayload === "object"
      ? state.trainingTodayCardPayload
      : null;
    const status = String(payload?.status || "").trim().toLowerCase();
    if (!["fresh", "missing", "stale", "failed"].includes(status)) return false;
    const cardDate = String(payload?.date || "").trim();
    const today = String(getTodayDateUtcKey() || "").trim();
    return !cardDate || !today || cardDate === today;
  }

  async function refreshCoreTrainingCard(dayKey, options = {}) {
    const key = String(dayKey || getTodayDateUtcKey() || "").trim();
    if (!key) return;
    const force = options.force === true;
    const snapshot = state.dashboardSnapshot || null;
    const todayKey = getTodayDateUtcKey();
    const isToday = String(key || "") === String(todayKey || "");
    if (!force && isToday && snapshot?.trainingTodayCard && typeof snapshot.trainingTodayCard === "object") {
      if (renderTrainingTodayCard(snapshot.trainingTodayCard, { source: "snapshot" })) {
        return snapshot.trainingTodayCard;
      }
    }
    if (!force && hasActiveTrainingTodayCard()) return state.trainingTodayCardPayload;
    if (state.trainingTodayCardPromise) return state.trainingTodayCardPromise;
    setTrainingTodayCardLoading();
    state.trainingTodayCardPromise = (async () => {
      try {
        const trainingTodayCard = await fetchTrainingTodayCard(key);
        if (trainingTodayCard) {
          try { console.debug("[training-card] fetched", trainingTodayCard); } catch (_) {}
          if (renderTrainingTodayCard(trainingTodayCard, { source: "refresh" })) return trainingTodayCard;
        }
      } catch (err) {
        try { console.warn("[training-card] fetch failed", err); } catch (_) {}
        _renderTrainingTodayCardError(els.coreTrainingCard, "Training-Card Renderfehler", err?.message || "Fetch fehlgeschlagen.");
      }
      if (els.coreTrainingCard) {
        _renderTrainingTodayCardError(els.coreTrainingCard, "GPT-Training-Card nicht verfügbar", "Es gibt keinen CORE-/Autopilot-Fallback mehr.");
      }
      return null;
    })().finally(() => {
      state.trainingTodayCardPromise = null;
    });
    return state.trainingTodayCardPromise;
  }


  function _setBlockStatusState(view) {
    if (!els.blockStatusCard) return;
    els.blockStatusCard.dataset.view = view;
    const isLoading = view === "loading";
    const isError = view === "error";
    if (els.blockStatusBody) els.blockStatusBody.hidden = isError;
    if (els.blockStatusError) els.blockStatusError.hidden = !isError;
    if (els.blockStatusInfo && view !== "content") els.blockStatusInfo.hidden = true;
    els.blockStatusCard.classList.toggle("is-loading", isLoading);
  }

  function _clipRule(text, maxLen = 70) {
    const raw = String(text || "").replace(/\s+/g, " ").trim();
    if (!raw) return "";
    if (raw.length <= maxLen) return raw;
    return `${raw.slice(0, maxLen - 1).trimEnd()}…`;
  }

  function _splitStatusLine(rawStatus) {
    const raw = String(rawStatus || "").trim();
    if (!raw) return { left: "", right: "" };
    const marker = "→";
    const idx = raw.indexOf(marker);
    if (idx < 0) return { left: raw, right: "" };
    return {
      left: raw.slice(0, idx).trim(),
      right: raw.slice(idx + marker.length).trim(),
    };
  }

  function _emphasizePrefix(line, prefix) {
    const txt = String(line || "").trim();
    if (!txt) return "";
    if (txt.startsWith(prefix)) {
      const rest = txt.slice(prefix.length).trim();
      return `<strong>${escapeHtml(prefix)}</strong>${rest ? ` ${escapeHtml(rest)}` : ""}`;
    }
    return escapeHtml(txt);
  }

  function _buildHrvPlanFitLine(blockStatusPayload, hrvSeriesPayload) {
    const expRaw = String(blockStatusPayload?.hrv_expected || "").trim();
    const obsRaw = String(blockStatusPayload?.hrv_observed || "").trim();
    const verdictRaw = String(blockStatusPayload?.hrv_verdict || "").trim().toLowerCase();
    if (expRaw && obsRaw && verdictRaw) {
      const verdict = (verdictRaw === "kritisch" || verdictRaw === "passt" || verdictRaw === "neutral")
        ? verdictRaw
        : "neutral";
      return { expected: expRaw, observed: obsRaw, verdict };
    }
    // Backend owns HRV classification for this card; frontend fallback stays neutral.
    return { expected: null, observed: "keine Daten", verdict: "neutral" };
  }

  function _blockStatusHeroLine(blockStatusPayload) {
    if (!blockStatusPayload?.has_plan) return "Kein aktiver Block";
    const planName = String(blockStatusPayload.plan_name || "").trim() || "Block";
    const week = Number(blockStatusPayload.block_week);
    const total = Number(blockStatusPayload.block_weeks_total);
    if (Number.isFinite(week) && Number.isFinite(total) && total > 0) return `${planName} · Woche ${week}/${total}`;
    return planName;
  }

  function _phaseStatusShort(blockStatusPayload) {
    const phase = String(blockStatusPayload?.phase || "").trim().toUpperCase();
    if (phase === "DELOAD") return "Deload läuft";
    const days = Number(blockStatusPayload?.days_to_deload);
    if (Number.isFinite(days) && days > 0) return `Deload in ${days} ${days === 1 ? "Tag" : "Tagen"}`;
    const statusLine = String(blockStatusPayload?.status_line || "").trim();
    const split = _splitStatusLine(statusLine);
    return split.right || "";
  }

  function _blockStatusSubline(blockStatusPayload) {
    if (!blockStatusPayload?.has_plan) return "Wähle einen Plan in /planung";
    const phase = String(blockStatusPayload?.phase || "").trim().toUpperCase();
    const short = _phaseStatusShort(blockStatusPayload);
    if (phase && short) return `Phase ${phase} · ${short}`;
    if (phase) return `Phase ${phase}`;
    return short;
  }

  function _normalizeJobLine(rawJob) {
    const raw = String(rawJob || "").trim();
    if (!raw) return "Job: Fokus halten.";
    if (raw.toLowerCase().startsWith("job:")) return `Job:${raw.slice(4).trim() ? ` ${raw.slice(4).trim()}` : ""}`;
    return `Job: ${raw}`;
  }

  function _hrvExpectationCopy(blockStatusPayload) {
    const phase = String(blockStatusPayload?.phase || "").trim().toUpperCase();
    if (phase === "DELOAD") return "stabil → Richtung Baseline";
    if (phase === "OVERREACH") return "leicht ↓ ist ok";
    if (phase === "REST" || phase === "OFF") return "↑ oder stabil";
    return "stabil bis leicht ↑";
  }

  function _hrvObservedCopy(hrvFit) {
    const observed = String(hrvFit?.observed || "").trim().toLowerCase();
    if (!observed || observed === "keine daten") return "keine Daten";
    if (observed === "stabil" || observed === "↔") return "↔";
    if (observed === "up" || observed === "↑") return "↑";
    if (observed === "down" || observed === "↓") return "↓";
    if (observed === "volatil") return "↕ volatil";
    return String(hrvFit?.observed || "keine Daten").trim();
  }

  function _hrvPhaseKey(blockStatusPayload) {
    const phase = String(blockStatusPayload?.phase || "").trim().toUpperCase();
    if (phase === "DELOAD") return "DELOAD";
    if (phase === "OVERREACH") return "OVERREACH";
    if (phase === "REST" || phase === "OFF") return "REST_OFF";
    return "BUILD";
  }

  function _hrvArrowKey(observedCopy) {
    const txt = String(observedCopy || "").trim();
    if (txt.includes("↑")) return "up";
    if (txt.includes("↓")) return "down";
    if (txt.includes("↔")) return "flat";
    return "unknown";
  }

  function _hrvMultiDayUiFlag(blockStatusPayload, hrvFit) {
    if (blockStatusPayload?.hrv_mehrtaegig === true) return true;
    if (blockStatusPayload?.hrv_multiday === true) return true;
    if (blockStatusPayload?.hrv_down_multiday === true) return true;
    const obsRaw = String(hrvFit?.observed || "").toLowerCase();
    if (obsRaw.includes("mehrt")) return true;
    return false;
  }

  function _hrvChipByUiRules(blockStatusPayload, hrvFit) {
    const phaseKey = _hrvPhaseKey(blockStatusPayload);
    const observed = _hrvObservedCopy(hrvFit);
    const arrowKey = _hrvArrowKey(observed);
    const isMultiDay = _hrvMultiDayUiFlag(blockStatusPayload, hrvFit);

    let critical = false;
    if (phaseKey === "DELOAD" || phaseKey === "BUILD" || phaseKey === "REST_OFF") {
      critical = (arrowKey === "down" && isMultiDay);
    } else if (phaseKey === "OVERREACH") {
      critical = false;
    }

    return {
      label: critical ? "kritisch" : "passt",
      className: critical ? "is-kritisch" : "is-passt",
      observed,
      critical,
    };
  }

  function _hrvLineHtml(blockStatusPayload, hrvFit) {
    if (!blockStatusPayload?.has_plan) return "<strong>HRV:</strong> keine Daten";
    const expectation = _hrvExpectationCopy(blockStatusPayload);
    const status = _hrvChipByUiRules(blockStatusPayload, hrvFit);
    const chipTooltip = status.critical
      ? "Chip ist konsistent zum Trendpfeil. Kritisch erscheint nur bei klarer, mehrtägiger Verschlechterung."
      : "Chip ist konsistent zum Trendpfeil, kein Alarm bei Rebound im Deload.";
    return `<strong>HRV:</strong> Erwartung ${escapeHtml(expectation)} · Aktuell ${escapeHtml(status.observed)} · <span class="block-status-hrv-status ${status.className}" title="${escapeHtml(chipTooltip)}">${escapeHtml(status.label)}</span>`;
  }

  function _chipExpectationCopy(blockStatusPayload, rawExpectation) {
    const phase = String(blockStatusPayload?.phase || "").trim().toUpperCase();
    if (phase === "DELOAD") return "Erwartung: stabil → Richtung Baseline";
    const raw = String(rawExpectation || "").trim();
    if (!raw) return "Erwartung: stabil bis leicht ↑";
    if (raw.toLowerCase().startsWith("erwartung:")) return raw;
    return `Erwartung: ${raw}`;
  }

  function _driftReasonShort(reasonRaw) {
    const reason = String(reasonRaw || "").trim();
    if (!reason) return "Muster";
    if (reason.includes("RPE-Cap")) return "RPE-Cap";
    if (reason.includes("Autopilot")) return "Autopilot";
    if (reason.includes("Target")) return "Targets";
    if (reason.includes("Override")) return "Overrides";
    return reason;
  }

  function _driftBadgeContext(blockStatusPayload, reasonShort) {
    const events = blockStatusPayload?.events || {};
    const rpe7 = Math.max(0, Number(events.rpe_cap_violations_7d) || 0);
    const rpe14 = Math.max(0, Number(events.rpe_cap_violations) || 0);
    const override7 = Math.max(0, Number(events.override_count_7d) || 0);
    const recent14 = Math.max(0, Number(events.recent_violations) || 0);
    const fresh = (rpe7 > 0 || override7 > 0);

    if (reasonShort === "RPE-Cap" && rpe14 > 0) return { text: `${rpe14}× (letzte 14T)`, age: fresh ? "frisch" : "alt" };
    if ((reasonShort === "Autopilot" || reasonShort === "Overrides") && override7 > 0) return { text: `${override7}× (letzte 7T)`, age: "frisch" };
    if (recent14 > 0) return { text: `${recent14}× (letzte 14T)`, age: fresh ? "frisch" : "alt" };
    return { text: "älter", age: "alt" };
  }

  function _badgeView(blockStatusPayload, hasPlan) {
    const badge = String(blockStatusPayload?.badge || (hasPlan ? "CONSISTENT" : "NO PLAN")).toUpperCase();
    const reasonRaw = String(blockStatusPayload?.badge_reason || "").trim();
    if (badge === "DRIFT") {
      const reasonShort = _driftReasonShort(reasonRaw);
      const ctx = _driftBadgeContext(blockStatusPayload, reasonShort);
      return {
        text: `DRIFT: ${reasonShort}${ctx.text ? ` • ${ctx.text}` : ""}`,
        badgeKey: "drift",
        badgeAge: ctx.age,
        tooltip: "DRIFT zeigt ein wiederkehrendes Muster. Er beruhigt sich, wenn mehrere Einheiten wieder sauber innerhalb der Leitplanke liegen.",
      };
    }
    if (!hasPlan || badge === "NO PLAN") {
      return {
        text: "KEIN PLAN",
        badgeKey: "no-plan",
        badgeAge: "",
        tooltip: "",
      };
    }
    return {
      text: "STABIL",
      badgeKey: "consistent",
      badgeAge: "",
      tooltip: "",
    };
  }

  function _normalizeBlockStatusPayload(payload) {
    if (!payload || typeof payload !== "object" || payload._error) return { mode: "error", payload: null, partial: false };
    if (typeof payload.has_plan !== "boolean") return { mode: "error", payload: null, partial: false };
    let partial = !!payload.partial;
    if (payload.has_plan) {
      const hasName = String(payload.plan_name || "").trim().length > 0;
      const hasPhase = String(payload.phase || "").trim().length > 0;
      const hasWeek = Number.isFinite(Number(payload.block_week)) && Number.isFinite(Number(payload.block_weeks_total));
      partial = !(hasName && hasPhase && hasWeek);
    }
    return { mode: "content", payload, partial };
  }

  function renderBlockStatusLoading() {
    if (!els.blockStatusCard) return;
    _corePrimeRender(null);
    _setBlockStatusState("loading");
    if (els.coreDecisionConfidence) {
      els.coreDecisionConfidence.textContent = "Vertrauen: --";
      els.coreDecisionConfidence.dataset.badge = "unknown";
    }
    setText(els.coreStatusPhase, "Phase: --");
    if (els.coreStatusTimeline) setHtml(els.coreStatusTimeline, "<div class=\"core-overview-loading\"><div class=\"core-overview-loading-line\"></div></div>");
    if (els.coreStatusSummary) setText(els.coreStatusSummary, "Vergangene 3 Tage: --");
    setText(els.coreDecisionUpdated, "aktualisiert: --");
    if (els.coreDecisionInfo) els.coreDecisionInfo.hidden = true;
    if (els.coreDecisionError) els.coreDecisionError.hidden = true;
  }

  function renderBlockStatusError() {
    if (!els.blockStatusCard) return;
    _corePrimeRender(null);
    _setBlockStatusState("error");
    if (els.coreDecisionConfidence) {
      els.coreDecisionConfidence.textContent = "Vertrauen: --";
      els.coreDecisionConfidence.dataset.badge = "unknown";
    }
    setText(els.coreStatusPhase, "Phase: --");
    if (els.coreStatusTimeline) setHtml(els.coreStatusTimeline, "<div class=\"core-overview-fallback\">Session-Strip lädt neu.</div>");
    if (els.coreStatusSummary) setText(els.coreStatusSummary, "Vergangene 3 Tage: --");
    setText(els.coreDecisionUpdated, "aktualisiert: --");
    if (els.coreDecisionInfo) els.coreDecisionInfo.hidden = true;
    if (els.coreDecisionError) els.coreDecisionError.hidden = false;
    console.error("CORE decision card nicht verfügbar.");
  }

  function _coreDomainLabel(domain) {
    const key = String(domain || "").toLowerCase();
    if (key === "training") return "Training";
    if (key === "recovery") return "Recovery";
    return "Rhythmus";
  }

  function _setCoreDomainPill(el, label, status, key) {
    if (!el) return;
    const txt = String(status || "neutral").trim() || "neutral";
    el.textContent = `${label}: ${txt}`;
    el.classList.toggle("is-training", key === "training");
    el.classList.toggle("is-recovery", key === "recovery");
    el.classList.toggle("is-rhythmus", key === "rhythmus");
  }

  function _coreStatusEventItems(payload) {
    return Array.isArray(payload?.events?.items) ? payload.events.items : [];
  }

  function _coreStatusClosePopover() {
    if (state.coreStatusPopover && state.coreStatusPopover.parentNode) {
      state.coreStatusPopover.parentNode.removeChild(state.coreStatusPopover);
    }
    state.coreStatusPopover = null;
    state.coreStatusPopoverSessionId = null;
    state.coreStatusPopoverTarget = null;
  }

  function _patchCoreDecisionCacheOverride(sessionId, target, value) {
    const sid = String(sessionId || "").trim();
    const tgt = String(target || "").trim().toLowerCase();
    const val = String(value || "").trim().toUpperCase();
    if (!sid || !tgt || !val) return false;
    const payload = state.coreDecisionCardCache;
    const days = Array.isArray(payload?.timeline?.days) ? payload.timeline.days : null;
    if (!days) return false;
    let changed = false;
    for (const day of days) {
      const items = Array.isArray(day?.items) ? day.items : [];
      for (const item of items) {
        if (String(item?.session_id || "") !== sid) continue;
        const effective = item?.effective && typeof item.effective === "object" ? item.effective : null;
        if (tgt === "gym_mode") {
          if (effective) effective.mode_or_plan = val;
          item.effective_mode = val;
          item.mode_effective = val;
          item.mode_label = (val === "LIGHT") ? "LIGHT" : ((val === "PUSH" || val === "HEAVY") ? "HEAVY" : "");
        } else if (tgt === "run_plan") {
          if (effective) effective.mode_or_plan = val;
          item.effective_plan = val;
          item.plan_effective = val;
        }
        if (effective) effective.changed_by = "user";
        item.changed_by = "user";
        changed = true;
      }
    }
    return changed;
  }

  function _coreDayFromSessionId(sessionId) {
    const sid = String(sessionId || "").trim();
    const m = /^(?:gym|run)_(\d{4}-\d{2}-\d{2})_/.exec(sid);
    return m ? String(m[1] || "") : "";
  }

  function _setPlanCheckCardTitle(sessionResp) {
    if (!els.planCheckCardTitle) return;
    const coreCard = state.dashboardSnapshot?.coreTrainingCard && typeof state.dashboardSnapshot.coreTrainingCard === "object"
      ? state.dashboardSnapshot.coreTrainingCard
      : null;
    const coreSessionType = String(coreCard?.session_type || "").toLowerCase();
    const coreIntent = String(coreCard?.decision_intent || "").toLowerCase();
    const recoveryOverride = coreSessionType === "recovery" || coreSessionType === "hard_stop" || coreIntent === "recovery_only" || coreIntent === "hard_stop";
    if (recoveryOverride) {
      els.planCheckCardTitle.textContent = String(coreCard?.title || "Heute Recovery").trim() || "Heute Recovery";
      return;
    }
    const selectedDay = String(sessionResp?.selected_day_id || "").trim();
    const weekDays = Array.isArray(sessionResp?.week_days) ? sessionResp.week_days : [];
    const selectedRow = selectedDay ? weekDays.find((d) => String(d?.day_id || "") === selectedDay) : null;
    const rowFinal = String(selectedRow?.final_label || "").trim();
    const name = rowFinal || String(sessionResp?.session?.session_name || "").trim();
    const dailyUnit = state.coreDecisionCardCache?.daily_ui?.planned_unit || state.coreDecisionCardCache?.daily_decision?.planned_unit || {};
    const dailyType = String(dailyUnit?.type || "").toLowerCase();
    const dailyLabel = String(dailyUnit?.display_label || dailyUnit?.plan_name || "").trim();
    const sameAsCore = dailyType && dailyType !== "rest" && dailyType !== "waiting" && dailyLabel && name && dailyLabel.toLowerCase() === name.toLowerCase();
    if (sameAsCore) {
      els.planCheckCardTitle.textContent = `Heute ausführen · ${name}`;
    } else if (dailyType === "rest") {
      els.planCheckCardTitle.textContent = name ? `Nächste geplante Einheit · ${name}` : "Nächste geplante Einheit";
    } else {
      els.planCheckCardTitle.textContent = name ? `Nächste geplante Einheit · ${name}` : "Nächste geplante Einheit";
    }
  }

  function _isFreshNextSessionPayload(resp) {
    return !!(resp && resp.ok === true && !resp.loading && !resp.stale);
  }

  function _planCheckHasRenderedItems() {
    const box = pcEl("plan-check-exercises");
    return !!(box && box.childElementCount > 0);
  }

  function _renderPlanCheckPending(message = "Nächste Einheit wird geladen …", { preserveContent = false } = {}) {
    const box = pcEl("plan-check-exercises");
    const msg = pcEl("plan-check-message");
    const link = pcEl("plan-check-link");
    if (box && !(preserveContent && _planCheckHasRenderedItems())) box.innerHTML = "";
    if (msg) msg.textContent = message;
    if (link) {
      link.textContent = "Letzte Ausführung: —";
      link.href = "#";
    }
  }

  function _renderPlanCheckWeekFallback(weekOverview) {
    const sel = pcEl("plan-check-select");
    if (!sel || !weekOverview || weekOverview.ok !== true) return;
    const sessions = Array.isArray(weekOverview.sessions) ? weekOverview.sessions : [];
    const today = weekOverview.today || {};
    if (els.planCheckCardTitle) {
      const name = String(today.default_session_name || today.today_session_name || sessions?.[0]?.session_name || "").trim();
      els.planCheckCardTitle.textContent = name ? `Nächste geplante Einheit · ${name}` : "Nächste geplante Einheit";
    }
    sel.innerHTML = "";
    for (const s of sessions) {
      const opt = document.createElement("option");
      opt.value = String(s?.session_key || "");
      opt.textContent = String(s?.session_name || "");
      sel.appendChild(opt);
    }
    const defKey = String(today.default_session_key || sessions?.[0]?.session_key || "");
    if (defKey) sel.value = defKey;
  }

  function _coreTodayTrainingView(snapshot) {
    const decisionCard = snapshot?.coreDecisionCard && typeof snapshot.coreDecisionCard === "object" ? snapshot.coreDecisionCard : {};
    const dashboardCard = snapshot?.coreDashboardCard && typeof snapshot.coreDashboardCard === "object" ? snapshot.coreDashboardCard : {};
    const today = decisionCard?.today && typeof decisionCard.today === "object" ? decisionCard.today : {};
    const todayMini = dashboardCard?.today_mini && typeof dashboardCard.today_mini === "object"
      ? dashboardCard.today_mini
      : (decisionCard?.today_mini && typeof decisionCard.today_mini === "object" ? decisionCard.today_mini : {});
    const gym = today?.gym && typeof today.gym === "object" ? today.gym : {};
    const run = today?.run && typeof today.run === "object" ? today.run : {};
    const plan = String(todayMini.plan || "").trim();
    const mode = String(todayMini.mode || gym.mode_effective || "").trim();
    const modeUpper = mode.toUpperCase();
    const planLower = plan.toLowerCase();
    const gymPlanned = gym.is_planned === true;
    const runPlanned = run.is_planned === true;
    const gymKnown = typeof gym.is_planned === "boolean";
    const runKnown = typeof run.is_planned === "boolean";
    const hasTrainingPlan = !!plan && planLower !== "rest";
    const hasTrainingMode = !!mode && modeUpper !== "REST";
    const hasRestPlan = planLower === "rest";
    const hasRestMode = modeUpper === "REST";
    const explicitRestFromFlags = (gymKnown || runKnown) && !gymPlanned && !runPlanned;
    const isResolved = hasTrainingPlan || hasTrainingMode || hasRestPlan || hasRestMode || gymPlanned || runPlanned || explicitRestFromFlags;
    const isRest = hasRestPlan || hasRestMode || (explicitRestFromFlags && !hasTrainingPlan && !hasTrainingMode);
    return {
      isResolved,
      isRest,
      plan,
      mode,
      gym,
      run,
    };
  }

  function _coreTodayIsRest(snapshot = null) {
    const source = snapshot || state.dashboardSnapshot || {
      coreDecisionCard: state.coreDecisionCardCache || null,
      coreDashboardCard: null,
    };
    const view = _coreTodayTrainingView(source);
    return !!(view.isResolved && view.isRest);
  }

  function _isTodayDayKey(dayKey) {
    const key = String(dayKey || "").trim();
    return !!key && key === String(getTodayDateUtcKey() || "").trim();
  }

  function _pcCurrentSelectedKey() {
    const sel = pcEl("plan-check-select");
    return String(sel?.value || state.pcSelectedKey || "").trim();
  }

  function _pcIsCurrentSelection(key) {
    const wanted = String(key || "").trim();
    return !!wanted && _pcCurrentSelectedKey() === wanted;
  }

  function _pcPayloadMatchesKey(resp, key) {
    const wanted = String(key || "").trim();
    if (!wanted) return true;
    const got = String(resp?.selected_day_id || resp?.day_id || "").trim();
    return !got || got === wanted;
  }

  function _pcHasRenderableItems(resp) {
    const items = Array.isArray(resp?.items) ? resp.items : [];
    if (!items.length) return false;
    return items.some((it) => {
      const name = String(it?.title || it?.name || "").trim();
      const hasStructured = (
        (Array.isArray(it?.structured?.new_reps) && it.structured.new_reps.length > 0) ||
        (Array.isArray(it?.structured?.new_weights) && it.structured.new_weights.length > 0)
      );
      const hasSuggestion = String(it?.suggestion_inline || "").trim().length > 0;
      const hasReference = !!(it?.done && (
        (Array.isArray(it.done.reps) && it.done.reps.length > 0) ||
        (Array.isArray(it.done.weights) && it.done.weights.length > 0) ||
        it.done.last_reference
      ));
      return !!name && (hasStructured || hasSuggestion || hasReference);
    });
  }

  function _pcRenderReferenceLink(link, workout) {
    if (!link) return;
    if (workout?.id) {
      link.textContent = `Letzte Ausführung: ${workout.display_date || workout.date_iso || ""}`;
      link.href = `/training?workout_id=${encodeURIComponent(workout.id)}`;
    } else {
      link.textContent = "Letzte Ausführung: —";
      link.href = "#";
    }
  }

  function _pcRenderSimpleSession(resp) {
    const box = pcEl("plan-check-exercises");
    const msg = pcEl("plan-check-message");
    if (!box || !msg) return;
    box.innerHTML = "";
    msg.textContent = "";
    const title = els.planCheckCardTitle;
    const sessionName = String(resp?.session?.session_name || resp?.session_name || "").trim();
    if (title) title.textContent = sessionName ? `Nächste geplante Einheit · ${sessionName}` : "Nächste geplante Einheit";
    const items = Array.isArray(resp?.items) ? resp.items : [];
    if (!items.length) {
      msg.textContent = String(resp?.message || "Keine Einträge für diesen Tag.");
      return;
    }
    for (const it of items) {
      const row = document.createElement("div");
      row.className = "plan-check-item";
      const nameEl = document.createElement("div");
      nameEl.className = "plan-check-item-name";
      nameEl.textContent = String(it?.title || it?.name || "Einheit");
      const sugEl = document.createElement("div");
      sugEl.className = "plan-check-item-suggestion";
      sugEl.textContent = String(it?.suggestion_inline || it?.summary || it?.note || "").trim() || "Geplant";
      row.appendChild(nameEl);
      row.appendChild(sugEl);
      box.appendChild(row);
    }
  }

  function _pcRenderSessionSafe(resp, weekOverview) {
    if (hasActiveTrainingTodayCard()) {
      try { console.debug("[training-card] skip legacy _pcRenderSessionSafe because GPT card is active"); } catch (_) {}
      return;
    }
    const isValid = !!(resp && resp.ok === true && (resp?.session?.session_name || resp?.session?.session_key) && _pcHasRenderableItems(resp));
    try { console.log("[training-card] refresh valid?", isValid, resp); } catch (_) {}
    if (!isValid) {
      if (state.trainingCardLastKnownGoodPayload) {
        try { pcRenderSession(state.trainingCardLastKnownGoodPayload, weekOverview || null); } catch (_) {}
      }
      return;
    }
    state.trainingCardLastKnownGoodPayload = resp;
    try {
      pcRenderSession(resp, weekOverview || null);
    } catch (err) {
      try { console.error("plan-check render failed", err); } catch (_) {}
      if (state.trainingCardLastKnownGoodPayload && state.trainingCardLastKnownGoodPayload !== resp) {
        try { pcRenderSession(state.trainingCardLastKnownGoodPayload, weekOverview || null); return; } catch (_) {}
      }
      _pcRenderSimpleSession(resp);
    }
  }

  function _renderPlanCheckRestFromCore(snapshot) {
    const title = els.planCheckCardTitle;
    const box = pcEl("plan-check-exercises");
    const msg = pcEl("plan-check-message");
    const link = pcEl("plan-check-link");
    const sel = pcEl("plan-check-select");
    const customSel = pcEl("plan-check-select-ui");
    const view = _coreTodayTrainingView(snapshot);
    if (title) title.textContent = "Training heute · Rest";
    if (box) box.innerHTML = "";
    if (msg) msg.textContent = "Heute laut CORE: Rest.";
    if (link) {
      link.textContent = "Letzte Ausführung: —";
      link.href = "#";
    }
    if (sel) {
      sel.innerHTML = "";
      sel.style.display = "none";
    }
    if (customSel) {
      customSel.hidden = true;
      customSel.innerHTML = "";
    }
    return view;
  }

  function _syncPlanCheckSelectorFromSession(sessionResp) {
    const sel = pcEl("plan-check-select");
    if (!sel) return;
    const weekDays = Array.isArray(sessionResp?.week_days) ? sessionResp.week_days : [];
    if (!weekDays.length) return;

    const byDay = new Map();
    for (const row of weekDays) {
      const dayId = String(row?.day_id || "");
      if (!dayId) continue;
      const raw = row?.label || `${row?.weekday_short || ""} · ${row?.final_label || row?.planned_label || ""}`;
      byDay.set(dayId, pcStripReassessLabel(raw));
    }

    Array.from(sel.options || []).forEach((opt) => {
      const val = String(opt?.value || "");
      if (!val || !byDay.has(val)) return;
      opt.textContent = String(byDay.get(val) || opt.textContent || "");
    });

    const customSel = pcEl("plan-check-select-ui");
    if (customSel) {
      customSel.hidden = true;
      customSel.innerHTML = "";
    }
  }

  async function _refreshNextSessionForDay(dayKey, sessionPayload = null) {
    const key = String(dayKey || "").trim();
    const applyPayload = (resp) => {
      if (key && !_pcIsCurrentSelection(key)) return false;
      if (!_isFreshNextSessionPayload(resp)) return false;
      _pcRenderSessionSafe(resp, null);
      _setPlanCheckCardTitle(resp);
      _syncPlanCheckSelectorFromSession(resp);
      const link = pcEl("plan-check-link");
      _pcRenderReferenceLink(link, resp?.reference_workout);
      return true;
    };
    if (!key) {
      if (applyPayload(sessionPayload)) return;
      try { pcInit(); } catch (_) {}
      return;
    }
    if (applyPayload(sessionPayload)) {
      const sel = pcEl("plan-check-select");
      if (sel) {
        const hasOption = Array.from(sel.options || []).some((opt) => String(opt?.value || "") === key);
        if (hasOption) {
          sel.value = key;
          state.pcSelectedKey = key;
          sel.dispatchEvent(new Event("change"));
        }
      }
      return;
    }
    const direct = await safeJson(`/api/next_session?day=${encodeURIComponent(key)}&refresh=1`);
    if (applyPayload(direct)) {
      const sel = pcEl("plan-check-select");
      if (sel) {
        const hasOption = Array.from(sel.options || []).some((opt) => String(opt?.value || "") === key);
        if (hasOption) {
          sel.value = key;
          state.pcSelectedKey = key;
        }
      }
      return;
    }
    const sel = pcEl("plan-check-select");
    if (!sel) {
      try { pcInit(); } catch (_) {}
      return;
    }
    const hasOption = Array.from(sel.options || []).some((opt) => String(opt?.value || "") === key);
    if (!hasOption) {
      try { pcInit(); } catch (_) {}
      return;
    }
    sel.value = key;
    state.pcSelectedKey = key;
    sel.dispatchEvent(new Event("change"));
  }

  async function _coreStatusSendOverride(sessionId, target, value, lock = false, opts = {}) {
    const fast = !!(opts && opts.fast);
    const overrideDay = _coreDayFromSessionId(sessionId);
    if (fast && _patchCoreDecisionCacheOverride(sessionId, target, value)) {
      renderCoreDecisionCard(state.coreDecisionCardCache || {});
      try { window.dispatchEvent(new CustomEvent("coreDecisionChanged")); } catch (_) {}
    }
    const endpoint = fast ? "/api/core/override?fast=1" : "/api/core/override";
    const res = await safePostJson(endpoint, {
      session_id: sessionId,
      target,
      value,
      lock,
    });
    if (!res || !res.ok) {
      _showCoreDecisionInfo("Konnte nicht speichern.", true);
      if (fast) {
        state.coreDecisionCardDirty = true;
        fetchCoreDecisionCard({ force: true }).then((fresh) => {
          if (fresh && fresh.ok) state.coreDecisionCardCache = fresh;
          renderCoreDecisionCard(state.coreDecisionCardCache || fresh || {});
        });
      }
      return false;
    }
    const responseCard = (res && res.dashboard_card && res.dashboard_card.ok) ? res.dashboard_card : null;
    state.coreDecisionCardDirty = true;
    try { window.dispatchEvent(new CustomEvent("coreDecisionChanged", { detail: { source: "core-decision" } })); } catch (_) {}
    if (responseCard) {
      state.coreDecisionCardCache = responseCard;
      renderCoreDecisionCard(responseCard);
      state.coreDecisionCardDirty = false;
    }
    if (!responseCard) {
      if (!fast) {
        const refreshPromise = fetchCoreDecisionCard({ force: true }).then((fresh) => {
          if (fresh && fresh.ok) state.coreDecisionCardCache = fresh;
          renderCoreDecisionCard(state.coreDecisionCardCache || fresh || {});
          return fresh;
        });
        await refreshPromise;
      } else {
        setTimeout(() => {
          fetchCoreDecisionCard({ force: true }).then((fresh) => {
            if (fresh && fresh.ok) state.coreDecisionCardCache = fresh;
            renderCoreDecisionCard(state.coreDecisionCardCache || fresh || {});
          });
        }, 900);
      }
    } else if (!fast) {
      // no-op: response already carried fresh card snapshot
    }
    if (fast) {
      setTimeout(() => { _refreshNextSessionForDay(overrideDay, res?.next_session || null); }, 0);
    } else {
      await _refreshNextSessionForDay(overrideDay, res?.next_session || null);
    }
    _showCoreDecisionInfo("Override gespeichert.", false);
    return true;
  }

  function _coreStatusOpenPopover(anchorEl, item) {
    _coreStatusClosePopover();
    const normRunPlan = (v) => {
      const raw = String(v || "").toUpperCase();
      if (raw === "LONG" || raw === "QUALITY" || raw === "EASY") return raw;
      if (raw === "SHORTEN" || raw === "SKIP") return "EASY";
      return "EASY";
    };
    const target = item?.type === "gym" ? "gym_mode" : "run_plan";
    const allowed = item?.type === "gym" ? ["LIGHT", "NORMAL", "PUSH"] : ["EASY", "QUALITY", "LONG"];
    const current = String(
      item?.type === "gym"
        ? (item?.mode_effective || item?.effective_mode || item?.effective?.mode_or_plan || item?.mode_suggested || item?.planned_mode || item?.planned?.mode_or_plan || "NORMAL")
        : (item?.plan_effective || item?.effective_plan || item?.effective?.mode_or_plan || item?.plan_suggested || item?.planned_plan || item?.planned?.mode_or_plan || "EASY")
    ).toUpperCase();
    const currentNorm = item?.type === "gym" ? current : normRunPlan(current);
    const pop = document.createElement("div");
    pop.className = "core-status-popover";
    pop.innerHTML = `
      <div class="core-status-pop-title">${escapeHtml(String(item?.slot || "Session"))}</div>
      <div class="core-decision-segment">${allowed.map((v) => `<button type="button" class="core-decision-segment-btn${v === currentNorm ? " is-active" : ""}" data-value="${escapeHtml(v)}">${escapeHtml(item?.type === "gym" ? v : (v === "EASY" ? "Easy" : (v === "QUALITY" ? "Quality" : "Long")))}</button>`).join("")}</div>
      <div class="core-status-pop-actions">
        <button type="button" class="core-overview-details" id="core-status-pop-apply">Übernehmen</button>
        <button type="button" class="core-overview-details" id="core-status-pop-reset">Reset</button>
      </div>`;
    document.body.appendChild(pop);
    const rect = anchorEl.getBoundingClientRect();
    pop.style.left = `${Math.max(12, rect.left)}px`;
    pop.style.top = `${Math.min(window.innerHeight - 180, rect.bottom + 6)}px`;
    state.coreStatusPopover = pop;
    state.coreStatusPopoverSessionId = String(item?.session_id || "");
    state.coreStatusPopoverTarget = target;
    let selectedValue = currentNorm;
    pop.addEventListener("click", async (ev) => {
      const btn = ev.target.closest("button");
      if (!btn) return;
      if (btn.id === "core-status-pop-apply") {
        await _coreStatusSendOverride(state.coreStatusPopoverSessionId, state.coreStatusPopoverTarget, selectedValue, false);
        _coreStatusClosePopover();
        return;
      }
      if (btn.id === "core-status-pop-reset") {
        await _coreStatusSendOverride(state.coreStatusPopoverSessionId, state.coreStatusPopoverTarget, "RESET", false);
        _coreStatusClosePopover();
        return;
      }
      const value = String(btn.getAttribute("data-value") || "").toUpperCase();
      if (!value) return;
      selectedValue = value;
      pop.querySelectorAll(".core-decision-segment-btn").forEach((b) => b.classList.toggle("is-active", b === btn));
    });
  }

  function _showCoreDecisionInfo(text, isError = false) {
    if (!els.coreDecisionInfo) return;
    const msg = String(text || "").trim();
    els.coreDecisionInfo.hidden = !msg;
    if (!msg) return;
    els.coreDecisionInfo.textContent = msg;
    els.coreDecisionInfo.classList.toggle("is-error", !!isError);
  }

  function _corePrimeClamp(value, min, max) {
    const n = Number(value);
    if (!Number.isFinite(n)) return min;
    return Math.max(min, Math.min(max, n));
  }

  function _corePrimeMode(rawMode) {
    const raw = String(rawMode || "").trim().toUpperCase();
    if (raw === "PUSH" || raw === "HEAVY") return "HEAVY";
    if (raw === "LIGHT") return "LIGHT";
    if (raw === "NORMAL") return "NORMAL";
    if (raw === "QUALITY") return "QUALITY";
    if (raw === "LONG") return "LONG";
    if (raw === "EASY") return "EASY";
    if (raw === "REST") return "REST";
    return "NORMAL";
  }

  function _corePrimeSetBar(el, widthPct, tone = "is-cool") {
    if (!el) return;
    const w = _corePrimeClamp(widthPct, 0, 100);
    el.style.width = `${Math.round(w)}%`;
    el.classList.remove("is-good", "is-cool", "is-warm", "is-risk");
    el.classList.add(tone);
  }

  function _corePrimeSetText(el, text) {
    if (!el) return;
    el.textContent = String(text || DASH);
  }

  function _corePrimeResolveOverrideSource(payload) {
    const fromFlag = String(payload?.override_flag?.source || "").toLowerCase();
    if (fromFlag === "user" || fromFlag === "core") return fromFlag;
    return "none";
  }

  function _corePrimeOverrideLabelFromChoice(choiceRaw) {
    const key = String(choiceRaw || "").trim().toLowerCase();
    if (key === "heavy_run") return ["HEAVY", "RUN"].join(" + ");
    if (key === "normal_run") return ["NORMAL", "RUN"].join(" + ");
    if (key === "light_run") return "LIGHT + RUN";
    if (key === "heavy") return "HEAVY";
    if (key === "normal") return "NORMAL";
    if (key === "light") return "LIGHT";
    if (key === "rest") return "REST";
    return "";
  }

  function _corePrimeExtractOverrideMode(payload, type) {
    const targetType = String(type || "").toLowerCase() || "gym";
    const refs = state.coreDecisionCardCache?.knobs && typeof state.coreDecisionCardCache.knobs === "object"
      ? state.coreDecisionCardCache.knobs
      : {};
    const modeSessionId = String(refs.mode?.session_id || "").trim();
    const runSessionId = String(refs.secondary?.session_id || "").trim();
    let days = Array.isArray(payload?.dashboard_timeline) ? payload.dashboard_timeline : null;
    if (!days) {
      days = Array.isArray(payload?.timeline?.days) ? payload.timeline.days : null;
    }
    if (!days) {
      days = Array.isArray(payload?.rolling_days) ? payload.rolling_days : [];
    }
    for (const day of days) {
      const rel = String(day?.relation || "").toLowerCase();
      if (rel === "past") continue;
      const items = Array.isArray(day?.items) ? day.items : [];
      for (const it of items) {
        const itType = String(it?.type || "").toLowerCase();
        if (itType !== targetType) continue;
        const sessionId = String(it?.session_id || "").trim();
        if (itType === "gym" && modeSessionId && sessionId !== modeSessionId) continue;
        if (itType === "run" && runSessionId && sessionId !== runSessionId) continue;
        const by = String(it?.changed_by || it?.effective?.changed_by || "").toLowerCase();
        const isUser = Boolean(it?.is_user_override) || by === "user";
        if (!isUser) continue;
        const modeCandidate = String(
          (itType === "gym" ? (it?.effective_mode || it?.effective?.mode_or_plan) : (it?.effective_plan || it?.effective?.mode_or_plan))
        ).trim().toUpperCase();
        if (modeCandidate) {
          if (itType === "run") return modeCandidate;
          if (modeCandidate === "PUSH") return "HEAVY";
          return modeCandidate;
        }
      }
    }
    return null;
  }

  function _corePrimeSetRow(textEl, barEl, text, widthPct, tone) {
    _corePrimeSetText(textEl, text);
    _corePrimeSetBar(barEl, widthPct, tone);
  }

  function _corePrimeToggleRow(rowId, textId, value) {
    const row = document.getElementById(rowId);
    const textEl = document.getElementById(textId);
    const clean = _corePrimeCleanSentence(value);
    if (textEl) textEl.textContent = clean;
    if (row) row.hidden = !clean;
    return clean;
  }

  function _corePrimeRenderRuleStack(primaryTitle, rules, secondaryTitle = "", secondaryRules = []) {
    const sides = Array.from(document.querySelectorAll("#dashboard-app .core-home-prime-side"));
    const primary = sides[0] || null;
    const secondary = sides[1] || null;
    const primaryTitleEl = primary?.querySelector(".core-home-prime-side-title") || null;
    const secondaryTitleEl = secondary?.querySelector(".core-home-prime-side-title") || null;
    const safePrimary = _corePrimeDedupe(rules, { limit: 3 });
    const safeSecondary = _corePrimeDedupe(secondaryRules, { limit: 3, used: safePrimary });

    if (primaryTitleEl) primaryTitleEl.textContent = primaryTitle || "REGELN HEUTE";
    if (secondaryTitleEl) secondaryTitleEl.textContent = secondaryTitle || "HINWEISE";

    const renderedPrimary = [
      _corePrimeToggleRow("core-home-prime-support-row-1", "core-home-prime-support-text-1", safePrimary[0]),
      _corePrimeToggleRow("core-home-prime-support-row-2", "core-home-prime-support-text-2", safePrimary[1]),
      _corePrimeToggleRow("core-home-prime-support-row-3", "core-home-prime-support-text-3", safePrimary[2]),
    ].filter(Boolean);

    const renderedSecondary = [
      _corePrimeToggleRow("core-home-prime-brake-row-1", "core-home-prime-brake-text-1", safeSecondary[0]),
      _corePrimeToggleRow("core-home-prime-brake-row-2", "core-home-prime-brake-text-2", safeSecondary[1]),
      _corePrimeToggleRow("core-home-prime-brake-row-3", "core-home-prime-brake-text-3", safeSecondary[2]),
    ].filter(Boolean);

    if (primary) primary.hidden = renderedPrimary.length === 0;
    if (secondary) secondary.hidden = renderedSecondary.length === 0;
  }

  function _corePrimeSetHeadline(planRaw, modeRaw, overrideInline = false) {
    if (!els.corePrimeHeadline) return;
    const plan = String(planRaw || "Heute").trim() || "Heute";
    const mode = String(modeRaw || "NORMAL").trim().toUpperCase() || "NORMAL";
    const overrideHtml = overrideInline ? `<span class="core-home-prime-override-inline">OVERRIDE</span>` : "";
    els.corePrimeHeadline.innerHTML = `${overrideHtml}<span>${escapeHtml(plan)} bleibt heute</span> <b>${escapeHtml(mode)}</b>`;
  }

  function _corePrimeTextItem(item) {
    if (item == null) return "";
    if (typeof item === "string") return item.trim();
    if (typeof item === "number") return String(item);
    if (!item || typeof item !== "object") return "";
    return String(item.text || item.title || item.label || item.name || item.reason || item.summary || item.code || "").trim();
  }

  function _corePrimeTextList(items) {
    return (Array.isArray(items) ? items : []).map(_corePrimeTextItem).map((s) => String(s || "").trim()).filter(Boolean);
  }

  function _corePrimeCleanSentence(textRaw) {
    return _corePrimeTextItem(textRaw).replace(/\s+/g, " ").trim().replace(/[. ]*$/, "");
  }

  function _corePrimeNormalize(textRaw) {
    const text = _corePrimeCleanSentence(textRaw).toLowerCase();
    if (!text) return "";
    return text
      .replace(/[,:;!?.]/g, " ")
      .replace(/\b(training|heute|geplante einheit|grenze|progression|abbruchregel|tempo|datenlage)\b/g, " ")
      .replace(/\s+/g, " ")
      .trim();
  }

  function _corePrimeDedupe(items, options = {}) {
    const { limit = 99, used = [] } = options;
    const seen = new Set((used || []).map(_corePrimeNormalize).filter(Boolean));
    const out = [];
    for (const item of (items || [])) {
      const text = _corePrimeCleanSentence(item);
      const key = _corePrimeNormalize(text);
      if (!text || !key || seen.has(key)) continue;
      const fuzzy = Array.from(seen).some((prev) => prev.includes(key) || key.includes(prev));
      if (fuzzy) continue;
      seen.add(key);
      out.push(text);
      if (out.length >= limit) break;
    }
    return out;
  }

  function _corePrimeShortHero(daily, dailyUi) {
    const unit = dailyUi?.planned_unit && typeof dailyUi.planned_unit === "object"
      ? dailyUi.planned_unit
      : (daily?.planned_unit && typeof daily.planned_unit === "object" ? daily.planned_unit : {});
    const raw = _corePrimeCleanSentence(dailyUi?.display_hero || dailyUi?.hero || daily?.display_hero || daily?.hero);
    const type = String(unit?.type || "").toLowerCase();
    const label = _corePrimeCleanSentence(unit?.display_label || unit?.plan_name || unit?.final_label);
    if (type === "rest") return "Heute kein Training - Recovery priorisieren.";
    if (type === "ergo") return "Ergo Z2 statt Lauf.";
    if (type === "run") return ((label.toLowerCase().includes("z2") ? "Z2 Run" : (label || "Run")) + " kontrolliert ausführen.").slice(0, 56);
    if (type === "gym") {
      if (/lower\s*a/i.test(label)) return "LOWER A normal ausführen.";
      if (/lower\s*b/i.test(label)) return "LOWER B normal ausführen.";
      if (/upper\s*a/i.test(label)) return "UPPER A normal ausführen.";
      if (/upper\s*b/i.test(label)) return "UPPER B normal ausführen.";
      return ((label || "Gym") + " normal ausführen.").slice(0, 56);
    }
    const direct = raw.split(/\s*(?:;|\s+weil\s+|\s+wenn\s+|\s+aber\s+|\s+solange\s+|\s+deshalb\s+)\s*/i)[0].trim();
    if (direct && direct.length <= 55) return direct.replace(/[. ]*$/, "") + ".";
    return raw ? raw.slice(0, 55).replace(/[. ]*$/, "") + "." : "CORE entscheidet heute kleinschrittig.";
  }

  function _corePrimeSetPills(chips) {
    if (!els.corePrimeChipList) return;
    const rows = _corePrimeDedupe(chips, { limit: 3 });
    els.corePrimeChipList.innerHTML = rows.length
      ? rows.map((chip) => `<span class="core-home-prime-chip">${escapeHtml(chip)}</span>`).join("")
      : `<span class="core-home-prime-chip">Datenlage offen</span>`;
  }

  function _corePrimeDailyDecision(payload) {
    const ui = payload?.daily_ui && typeof payload.daily_ui === "object" ? payload.daily_ui : null;
    const direct = ui?.daily_decision && typeof ui.daily_decision === "object"
      ? ui.daily_decision
      : (payload?.daily_decision && typeof payload.daily_decision === "object" ? payload.daily_decision : null);
    if (direct && Object.keys(direct).length) return direct;
    const nested = payload?.explain_v2?.daily_decision && typeof payload.explain_v2.daily_decision === "object" ? payload.explain_v2.daily_decision : null;
    return nested && Object.keys(nested).length ? nested : null;
  }

  function _corePrimeConstraintSummary(daily, dailyUi) {
    const cons = daily?.consequences && typeof daily.consequences === "object" ? daily.consequences : {};
    const constraints = cons.training_constraints && typeof cons.training_constraints === "object" ? cons.training_constraints : {};
    const unit = dailyUi?.planned_unit && typeof dailyUi.planned_unit === "object"
      ? dailyUi.planned_unit
      : (daily?.planned_unit && typeof daily.planned_unit === "object" ? daily.planned_unit : {});
    const lines = [];
    const blockedExercises = Array.isArray(constraints.blocked_exercises) ? constraints.blocked_exercises.filter(Boolean) : [];
    const blockedPatterns = Array.isArray(constraints.blocked_patterns) ? constraints.blocked_patterns.filter(Boolean) : [];
    const cautionPatterns = Array.isArray(constraints.caution_patterns) ? constraints.caution_patterns.filter(Boolean) : [];
    const cardio = constraints.cardio_after && typeof constraints.cardio_after === "object" ? constraints.cardio_after : null;
    const unitType = String(unit?.type || "").toLowerCase();
    const label = _corePrimeCleanSentence(unit?.display_label || unit?.plan_name || unit?.final_label);

    if (unitType === "rest") {
      return ["Recovery priorisieren.", "Bewegung niedrigschwellig halten.", "Schlafroutine sauber setzen.", "Kein spontanes Ballern."];
    }
    if (unitType === "run" || unitType === "ergo") {
      return [
        "Pulsdeckel respektieren.",
        "Keine Pace-/Watt-Jagd.",
        "Bei Pulsdrift lockerer werden oder abbrechen.",
      ];
    }
    if (unitType === "gym") {
      lines.push(`Training: ${label || "Gym"} sauber ausführen.`);
      if (constraints.rpe_cap != null) lines.push(`RPE-Cap ${constraints.rpe_cap}.`);
      lines.push(constraints.allow_progression === false ? "Progression aus." : "Progression nur bei stabiler Ausführung.");
      if (Number.isFinite(Number(constraints.volume_multiplier))) {
        const vm = Number(constraints.volume_multiplier);
        lines.push(vm < 0.95 ? "Volumen reduziert." : "Volumen normal.");
      } else {
        lines.push("Volumen normal.");
      }
      if (blockedExercises.length || blockedPatterns.length) lines.push(`Blockiert: ${blockedExercises.concat(blockedPatterns).slice(0, 3).join(", ")}.`);
      if (cautionPatterns.length) lines.push(`Achten auf ${cautionPatterns.slice(0, 2).join(", ")}.`);
      return lines.slice(0, 5);
    }

    if (constraints.allow_progression === false) lines.push("Progression heute nicht aktiv suchen.");
    if (constraints.rpe_cap != null) lines.push(`RPE-Cap ${constraints.rpe_cap}.`);
    if (blockedExercises.length || blockedPatterns.length) lines.push(`Blockiert: ${blockedExercises.concat(blockedPatterns).slice(0, 3).join(", ")}.`);
    if (cardio) lines.push(`Cardio: ${String(cardio.type || "kontrolliert")} ${cardio.duration_min ? `· ${cardio.duration_min} min` : ""}${cardio.target_hr ? ` · Puls ${cardio.target_hr}` : ""}.`);
    if (cautionPatterns.length) lines.push(`Achten auf ${cautionPatterns.slice(0, 2).join(", ")}.`);

    const doRows = _corePrimeTextList(cons.do);
    const avoidRows = _corePrimeTextList(cons.avoid);
    const watchRows = _corePrimeTextList(cons.watch);
    doRows.slice(0, 2).forEach((row) => lines.push(row.replace(/[. ]*$/, "") + "."));
    avoidRows.slice(0, 1).forEach((row) => lines.push(row.replace(/[. ]*$/, "") + "."));
    watchRows.slice(0, 1).forEach((row) => lines.push(row.replace(/[. ]*$/, "") + "."));

    if (!lines.length) {
      const status = String(dailyUi?.display_status || "").trim();
      if (status) lines.push(status.replace(/[. ]*$/, "") + ".");
    }
    return lines.slice(0, 5);
  }

  function _corePrimeRenderDailyDecision(payload) {
    const daily = _corePrimeDailyDecision(payload);
    if (!daily) {
      _corePrimeSetText(els.corePrimeHeadline, "Noch keine Daily Decision. CORE wartet auf Morgenwerte.");
      _corePrimeSetText(els.corePrimeSubline, "Daily Decision noch offen");
      _corePrimeSetPills(["Datenlage offen", "Morgenwerte eintragen", "keine Eskalation"]);
      if (els.corePrimeRing) els.corePrimeRing.style.setProperty("--score", "0");
      _corePrimeSetText(els.corePrimeConfidenceValue, "--");
      _corePrimeSetText(els.corePrimeMorgenStatus, "Datenlage");
      _corePrimeSetText(els.corePrimeReadiness, "--");
      _corePrimeSetText(els.corePrimeRkValue, "--");
      if (els.corePrimeRkMarker) els.corePrimeRkMarker.style.left = "0%";
      _corePrimeSetText(els.corePrimeMiniPlan, "wartet auf Morgenwerte");
      _corePrimeSetText(els.corePrimeMiniMode, "keine Entscheidung");
      _corePrimeSetText(els.corePrimeMiniFocus, "neutral");
      _corePrimeSetText(els.corePrimeMiniRisk, "offen");
      _corePrimeRenderRuleStack("REGELN HEUTE", ["Morgenwerte eintragen", "Bis dahin konservativ bleiben"], "HINWEISE", []);
      _corePrimeSetText(els.corePrimeTip, "In CORE öffnen");
      return true;
    }
    const dailyUi = payload?.daily_ui && typeof payload.daily_ui === "object" ? payload.daily_ui : {};
    const unit = dailyUi.planned_unit && typeof dailyUi.planned_unit === "object"
      ? dailyUi.planned_unit
      : (daily.planned_unit && typeof daily.planned_unit === "object" ? daily.planned_unit : {});
    const cons = daily.consequences && typeof daily.consequences === "object" ? daily.consequences : {};
    const constraints = cons.training_constraints && typeof cons.training_constraints === "object" ? cons.training_constraints : {};
    const readiness = dailyUi.data_readiness && typeof dailyUi.data_readiness === "object"
      ? dailyUi.data_readiness
      : (daily.data_readiness && typeof daily.data_readiness === "object" ? daily.data_readiness : {});
    const planName = _corePrimeCleanSentence(unit.display_label || unit.plan_name || unit.final_label || "Noch offen");
    const unitType = String(unit.type || "waiting").trim();
    const readinessText = daily.missing_data_used
      ? "fehlende Daten genutzt"
      : (readiness.complete ? "Morgenwerte vollständig" : (Array.isArray(readiness.missing) && readiness.missing.length ? `wartet auf ${readiness.missing.join(", ")}` : "Datenlage offen"));
    const summaryLines = _corePrimeConstraintSummary(daily, dailyUi);
    const chips = [readinessText];
    if (unitType === "run" || unitType === "ergo") {
      chips.push("Pulsdeckel", "kein Pace-Jagen", "bei Drift lockerer");
    } else if (unitType === "gym") {
      if (constraints.rpe_cap != null) chips.push(`RPE-Cap ${constraints.rpe_cap}`);
      chips.push(constraints.allow_progression === false ? "Progression aus" : "Progression sauber");
      if (Number.isFinite(Number(constraints.volume_multiplier))) chips.push(Number(constraints.volume_multiplier) < 0.95 ? "Volumen reduziert" : "Volumen normal");
      else chips.push("Volumen normal");
    } else if (unitType === "rest") {
      chips.push("Recovery", "ruhig bewegen", "Schlafroutine");
    } else {
      chips.push(...summaryLines.slice(0, 3).map((line) => line.replace(/[. ]*$/, "")));
    }

    _corePrimeSetText(els.corePrimeHeadline, _corePrimeShortHero(daily, dailyUi));
    _corePrimeSetText(els.corePrimeSubline, planName);
    _corePrimeSetPills(chips);
    if (els.corePrimeRing) els.corePrimeRing.style.setProperty("--score", String(Math.max(18, Math.min(92, 64))));
    _corePrimeSetText(els.corePrimeConfidenceValue, daily.missing_data_used ? "fehlende Daten" : "Daily Decision");
    _corePrimeSetText(els.corePrimeMorgenStatus, "Datenlage");
    _corePrimeSetText(els.corePrimeReadiness, readinessText);
    _corePrimeSetText(els.corePrimeRkValue, chips[1] || "--");
    if (els.corePrimeRkMarker) els.corePrimeRkMarker.style.left = `${constraints.allow_progression === false ? 28 : 62}%`;

    _corePrimeSetText(els.corePrimeMiniPlan, planName);
    _corePrimeSetText(els.corePrimeMiniMode, summaryLines[0] || "kontrolliert ausführen");
    _corePrimeSetText(els.corePrimeMiniFocus, chips.slice(1, 4).join(" · ") || "--");
    _corePrimeSetText(els.corePrimeMiniRisk, readinessText);

    const doRules = _corePrimeDedupe(_corePrimeTextList(cons.do), { limit: 3, used: summaryLines });
    const cautionRules = _corePrimeDedupe(_corePrimeTextList(cons.avoid).concat(_corePrimeTextList(cons.watch)), { limit: 3 });
    const ruleStack = _corePrimeDedupe(summaryLines.concat(doRules), { limit: 3 });
    _corePrimeRenderRuleStack("REGELN HEUTE", ruleStack, "HINWEISE", cautionRules);
    _corePrimeSetText(els.corePrimeTip, "");
    return true;
  }

  function _corePrimeReadinessStatusLabel(delta) {
    const d = Number(delta);
    if (!Number.isFinite(d)) return "stabil";
    if (d <= -10) return "stark gedrückt";
    if (d <= -4) return "leicht gedrückt";
    if (d >= 5) return "solide";
    return "stabil";
  }

  function _corePrimeRender(payload) {
    if (!els.blockStatusCard || !els.corePrimeHeadline) return;
    if (!payload || payload._error) {
      _corePrimeSetText(els.corePrimeHeadline, "--");
      _corePrimeSetText(els.corePrimeSubline, "--");
      _corePrimeSetPills(["Datenlage offen"]);
      if (els.corePrimeRing) els.corePrimeRing.style.setProperty("--score", "0");
      _corePrimeSetText(els.corePrimeConfidenceValue, "--%");
      _corePrimeSetText(els.corePrimeMorgenStatus, "● --");
      _corePrimeSetText(els.corePrimeReadiness, "--%");
      _corePrimeSetText(els.corePrimeRkValue, "--");
      if (els.corePrimeRkMarker) els.corePrimeRkMarker.style.left = "50%";
      _corePrimeSetText(els.corePrimeMiniPlan, "--");
      _corePrimeSetText(els.corePrimeMiniMode, "--");
      _corePrimeSetText(els.corePrimeMiniFocus, "--");
      _corePrimeSetText(els.corePrimeMiniRisk, "--");
      _corePrimeRenderRuleStack("REGELN HEUTE", [], "HINWEISE", []);
      _corePrimeSetText(els.corePrimeTip, "◉ --");
      return;
    }
    if (_corePrimeRenderDailyDecision(payload)) return;

    const explainV2 = (payload?.explain_v2 && typeof payload.explain_v2 === "object") ? payload.explain_v2 : {};
    const explainDecision = (explainV2?.decision && typeof explainV2.decision === "object") ? explainV2.decision : {};
    const explainNight = (explainV2?.night_cycle && typeof explainV2.night_cycle === "object") ? explainV2.night_cycle : {};
    const overrideSource = _corePrimeResolveOverrideSource(payload);
    const decisionLite = (payload?.today && typeof payload.today === "object") ? payload.today : {};
    const timelineDays = Array.isArray(payload?.timeline?.days) ? payload.timeline.days : [];
    const todayDay = timelineDays.find((d) => String(d?.relation || "").toLowerCase() === "today") || timelineDays[0] || {};
    const todayItems = Array.isArray(todayDay?.items) ? todayDay.items : [];
    const todayItem = todayItems[0] && typeof todayItems[0] === "object" ? todayItems[0] : {};
    const todayMini = payload?.today_mini && typeof payload.today_mini === "object" ? payload.today_mini : {};
    const todayView = _coreTodayTrainingView({
      coreDecisionCard: payload,
      coreDashboardCard: todayMini && Object.keys(todayMini).length ? { today_mini: todayMini } : null,
    });

    const todayType = String(
      todayItem?.type
      || (decisionLite?.gym?.is_planned ? "gym" : (decisionLite?.run?.is_planned ? "run" : (todayView.isResolved && todayView.isRest ? "rest" : "")))
      || ""
    ).toLowerCase();
    const explainChoiceMode = String(explainDecision?.choice_context?.chosen_label || explainDecision?.mode || "").trim();
    const baseModeRaw = String(
      explainChoiceMode
      || (todayType === "gym"
        ? (
          decisionLite?.gym?.mode_effective
          || todayItem?.effective_mode
          || todayItem?.effective?.mode_or_plan
          || payload?.knobs?.mode?.effective
          || todayMini.mode
          || "NORMAL"
        )
        : (todayType === "run"
          ? (
            decisionLite?.run?.label
            || todayItem?.effective_plan
            || todayItem?.effective?.mode_or_plan
            || payload?.knobs?.secondary?.effective
            || todayMini.mode
            || "EASY"
          )
          : (todayMini.mode || (todayView.isResolved && todayView.isRest ? "REST" : ""))))
    );
    const mode = _corePrimeMode(baseModeRaw);
    const overrideChoiceLabel = _corePrimeOverrideLabelFromChoice(payload?.override_choice);
    const overrideSelectionMode = overrideSource !== "none"
      ? _corePrimeExtractOverrideMode(payload, todayType)
      : null;

    const fallbackPlan = todayView.isResolved && todayView.isRest ? "Rest" : "Heute";
    let plan = String(
      todayMini.plan
      || (decisionLite?.gym?.is_planned ? decisionLite?.gym?.slot : "")
      || (decisionLite?.run?.is_planned ? decisionLite?.run?.label : "")
      || todayItem?.title
      || todayItem?.slot
      || fallbackPlan
    ).trim();
    plan = plan.replace(/^Gym\s+/i, "").replace(/^Run\s+/i, "").trim() || fallbackPlan;

    const confidenceRaw = Number(explainDecision?.final_confidence ?? payload?.confidence?.value);
    const confidencePct = Number.isFinite(confidenceRaw)
      ? _corePrimeClamp(Math.round(confidenceRaw * 100), 0, 100)
      : (String(payload?.confidence?.label || "").toLowerCase() === "hoch" ? 78 : (String(payload?.confidence?.label || "").toLowerCase() === "niedrig" ? 42 : 62));

    const data = state.lastData || {};
    const signals = data?.signals14 && typeof data.signals14 === "object" ? data.signals14 : {};
    const sigCtx = signals?.context && typeof signals.context === "object" ? signals.context : {};
    const recCtx = sigCtx?.recovery && typeof sigCtx.recovery === "object" ? sigCtx.recovery : {};
    const loadCtx = sigCtx?.load && typeof sigCtx.load === "object" ? sigCtx.load : {};
    const energyCtx = sigCtx?.energy && typeof sigCtx.energy === "object" ? sigCtx.energy : {};

    const readinessToday = Number.isFinite(Number(data?.readinessScore)) ? Number(data.readinessScore) : 60;
    const hrvToday = Number.isFinite(Number(recCtx?.today)) ? Number(recCtx.today) : null;
    const hrvAvg7 = Number.isFinite(Number(recCtx?.avg_7d)) ? Number(recCtx.avg_7d) : null;
    const hrvDeltaPct = (Number.isFinite(hrvToday) && Number.isFinite(hrvAvg7) && Math.abs(hrvAvg7) > 0.0001)
      ? ((hrvToday - hrvAvg7) / hrvAvg7) * 100
      : null;
    const recState = normalizeSignalState(recCtx?.state || "missing");
    const energyState = normalizeSignalState(energyCtx?.state || "missing");
    const energyTrendDiff = Number.isFinite(Number(energyCtx?.trend_diff)) ? Number(energyCtx.trend_diff) : null;

    const loadSeries = Array.isArray(loadCtx?.series) ? loadCtx.series : [];
    let loadLatest = null;
    for (let i = loadSeries.length - 1; i >= 0; i -= 1) {
      const v = Number(loadSeries[i]);
      if (Number.isFinite(v)) {
        loadLatest = v;
        break;
      }
    }
    const loadMax = Number.isFinite(Number(loadCtx?.max)) ? Number(loadCtx.max) : null;
    const loadPct = (Number.isFinite(loadLatest) && Number.isFinite(loadMax) && loadMax > 0)
      ? _corePrimeClamp((loadLatest / loadMax) * 100, 0, 100)
      : 0;

    const pastDays = timelineDays.filter((d) => String(d?.relation || "").toLowerCase() === "past").slice(-3);
    let hardDays = 0;
    for (const day of pastDays) {
      const items = Array.isArray(day?.items) ? day.items : [];
      const first = items[0] || {};
      const avgRpe = Number(first?.actual?.avg_rpe);
      const avgHr = Number(first?.actual?.avg_hr);
      const effortColor = String(day?.past_metrics?.effort_color || "").toLowerCase();
      if ((Number.isFinite(avgRpe) && avgRpe >= 8.3) || (Number.isFinite(avgHr) && avgHr >= 162) || effortColor === "red") {
        hardDays += 1;
      }
    }
    if (!pastDays.length) {
      const hd = Number(explainDecision?.kpis?.heavy_days_3);
      if (Number.isFinite(hd)) hardDays = Math.max(0, Math.min(3, Math.round(hd)));
    }
    const densityBase = pastDays.length || 3;
    const densityPct = _corePrimeClamp((hardDays / Math.max(1, densityBase)) * 100, 0, 100);

    const stimulusBase = { HEAVY: 78, NORMAL: 58, LIGHT: 42, EASY: 36, QUALITY: 66, LONG: 52, REST: 10 }[mode] ?? 52;
    const costBase = { HEAVY: 74, NORMAL: 55, LIGHT: 39, EASY: 34, QUALITY: 63, LONG: 57, REST: 15 }[mode] ?? 52;
    const recBoost = recState === "good" ? 8 : (recState === "bad" ? -8 : 0);
    const energyBoost = energyState === "good" ? 6 : (energyState === "bad" ? -7 : 0);
    const densityPenalty = Math.round(densityPct * 0.14);
    const loadPenalty = Math.round(loadPct * 0.12);

    const stimulusScore = _corePrimeClamp(stimulusBase + recBoost + Math.round((readinessToday - 60) * 0.22) - Math.round(loadPenalty * 0.45), 5, 100);
    const costScore = _corePrimeClamp(costBase + densityPenalty + loadPenalty + (energyState === "bad" ? 6 : 0), 5, 100);
    const reizKosten = _corePrimeClamp(stimulusScore / Math.max(1, costScore), 0.6, 1.35);

    const modeShift = { HEAVY: -8, NORMAL: -4, LIGHT: -2, EASY: -1, QUALITY: -6, LONG: -5, REST: 6 }[mode] ?? -3;
    const energyShift = energyState === "good" ? 2 : (energyState === "bad" ? -3 : 0);
    const recoveryShift = Number.isFinite(hrvDeltaPct) ? _corePrimeClamp(hrvDeltaPct * 0.12, -4, 3) : 0;
    const tomorrowReadiness = _corePrimeClamp(Math.round(readinessToday + modeShift + energyShift + recoveryShift - (loadPct * 0.06)), 22, 95);
    const readinessDelta = tomorrowReadiness - readinessToday;
    const morgenStatus = _corePrimeReadinessStatusLabel(readinessDelta);

    const riskLabel = (tomorrowReadiness < 45 || readinessDelta <= -12)
      ? "Hoch"
      : ((tomorrowReadiness < 60 || readinessDelta <= -6) ? "Moderat" : "Niedrig");

    const focusLabel = (() => {
      if (mode === "HEAVY") return "Progression & Kontrolle";
      if (mode === "NORMAL") return "Balance & Qualität";
      if (mode === "LIGHT" || mode === "EASY") return "Qualität & Kontrolle";
      if (mode === "QUALITY") return "Pace & Kontrolle";
      if (mode === "REST") return "Erholung & Reserve";
      return "System sauber halten";
    })();

    const subline = (() => {
      if (mode === "HEAVY") {
        return "Heute ist noch genug Puffer für einen starken Reiz da. Morgen fällt die Reserve dafür spürbar schwächer aus.";
      }
      if (mode === "NORMAL") {
        return "Heute trägt ein solider Reiz. Morgen bleibt spürbar mehr Reserve als bei HEAVY.";
      }
      if (mode === "LIGHT" || mode === "EASY") {
        return "Heute liegt der Fokus auf sauberer Qualität. So bleibt für morgen deutlich mehr Reserve erhalten.";
      }
      if (mode === "REST") {
        return "Heute steht Erholung im Vordergrund. Morgen startest du mit klar mehr Reserve.";
      }
      return "Heute ist der Reiz gut steuerbar. Für morgen bleibt die Reserve planbar.";
    })();
    const headlineMode = (overrideChoiceLabel || overrideSelectionMode || mode);
    _corePrimeSetHeadline(plan, headlineMode, overrideSource !== "none");
    _corePrimeSetText(els.corePrimeSubline, subline);

    if (els.corePrimeRing) els.corePrimeRing.style.setProperty("--score", String(confidencePct));
    _corePrimeSetText(els.corePrimeConfidenceValue, `${confidencePct}%`);
    _corePrimeSetText(els.corePrimeMorgenStatus, `● ${morgenStatus}`);
    _corePrimeSetText(els.corePrimeReadiness, `${Math.round(tomorrowReadiness)}%`);
    _corePrimeSetText(els.corePrimeRkValue, formatNumber(reizKosten, 2));
    if (els.corePrimeRkMarker) {
      const markerPct = _corePrimeClamp(((reizKosten - 0.75) / (1.25 - 0.75)) * 100, 0, 100);
      els.corePrimeRkMarker.style.left = `${markerPct}%`;
    }

    _corePrimeSetText(els.corePrimeMiniPlan, plan);
    _corePrimeSetText(els.corePrimeMiniMode, mode);
    _corePrimeSetText(els.corePrimeMiniFocus, focusLabel);
    _corePrimeSetText(els.corePrimeMiniRisk, riskLabel);
    if (els.corePrimeMiniMode) {
      els.corePrimeMiniMode.classList.toggle("is-warm", mode === "LIGHT" || mode === "QUALITY" || mode === "LONG");
    }
    if (els.corePrimeMiniRisk) {
      els.corePrimeMiniRisk.classList.toggle("is-warm", riskLabel === "Moderat");
    }

    const againstSignals = Array.isArray(explainDecision?.against_signals) ? explainDecision.against_signals : [];

    const hrvLine = Number.isFinite(hrvToday)
      ? `HRV - ${formatNumber(hrvToday, 1)} ms${Number.isFinite(hrvDeltaPct) ? ` - ${formatSigned(hrvDeltaPct, 0)}%` : ""}`
      : "HRV - heute offen";
    const hrvBar = Number.isFinite(hrvDeltaPct) ? _corePrimeClamp(55 + (hrvDeltaPct * 2), 10, 95) : 26;
    const hrvTone = Number.isFinite(hrvDeltaPct) ? (hrvDeltaPct >= 0 ? "is-good" : (hrvDeltaPct <= -8 ? "is-risk" : "is-cool")) : "is-cool";

    const recoveryLine = `Recovery - ${Math.round(readinessToday)}%`;
    const recoveryTone = readinessToday >= 70 ? "is-good" : (readinessToday >= 55 ? "is-warm" : "is-risk");
    const recoveryBar = _corePrimeClamp(readinessToday, 6, 100);

    const reizLine = `Reizscore - ${Math.round(stimulusScore)}%`;
    const reizTone = stimulusScore >= 65 ? "is-good" : (stimulusScore >= 45 ? "is-cool" : "is-warm");

    const preloadLow = !againstSignals.some((s) => String(s?.label || "").toLowerCase().includes("vorermüdet"));
    const support1 = "Erholung noch stabil";
    const support2 = "Vorermüdung nicht limitierend";
    const support3 = "Trainingsfenster passt";
    const stressDaily = Number(payload?.explain_v2?.inputs?.stress?.daily_stress);
    const stressPct = Number.isFinite(stressDaily) ? Math.round(Math.max(0, Math.min(1, stressDaily)) * 100) : 45;

    const densityLine = `Belastungsdichte - ${hardDays} von ${Math.max(1, densityBase)} hart`;
    const densityTone = densityPct >= 66 ? "is-risk" : (densityPct >= 34 ? "is-warm" : "is-cool");

    const loadLine = `Load-Folge - ${Math.round(loadPct)}%`;
    const loadTone = loadPct >= 72 ? "is-risk" : (loadPct >= 45 ? "is-warm" : "is-cool");

    const energyLine = Number.isFinite(energyTrendDiff)
      ? `Energie-Drift - ${formatSigned(energyTrendDiff, 0)} kcal`
      : "Energie-Drift - stabil";
    const energyBar = Number.isFinite(energyTrendDiff) ? _corePrimeClamp((Math.abs(energyTrendDiff) / 400) * 100, 8, 100) : 28;
    const energyTone = energyState === "bad" ? "is-risk" : (energyState === "good" ? "is-cool" : "is-warm");

    const brake1 = String(againstSignals?.[0]?.label || densityLine);
    const brake2 = String(againstSignals?.[1]?.label || loadLine);
    const brake3 = String(againstSignals?.[2]?.label || energyLine);
    _corePrimeRenderRuleStack(
      "REGELN HEUTE",
      [support1, support2, support3],
      "HINWEISE",
      [brake1, brake2, brake3]
    );

    const tip = (() => {
      const hero = String(explainNight?.future_lab?.hero_line || "").trim();
      if (hero) return `◉ ${hero}`;
      if (mode === "REST") return "◉ Tipp: Heute aktive Erholung, dann morgen mit hoher Reserve starten.";
      if (mode === "HEAVY" && tomorrowReadiness < 60) return "◉ Tipp: Schlaf priorisieren und Carbs offen halten, um den morgigen Drop abzufangen.";
      if (riskLabel === "Hoch") return "◉ Tipp: Volumen strikt deckeln und Technik vor Last priorisieren.";
      if (energyState === "bad") return "◉ Tipp: Kalorien heute im Zielband halten, damit die Kosten nicht weiter steigen.";
      return "◉ Tipp: Saubere Ausführung vor Eskalation - Progression ja, aber kontrolliert.";
    })();
    _corePrimeSetText(els.corePrimeTip, tip);
  }

  function renderCoreDecisionCard(payload) {
    if (!els.blockStatusCard) return;
    if (!payload || payload._error) {
      renderBlockStatusError();
      return;
    }
    _corePrimeRender(payload);
    _setBlockStatusState("content");
    if (els.coreDecisionError) els.coreDecisionError.hidden = true;

    const phaseName = String(payload?.phase?.name || "--").toUpperCase();
    const deloadIn = payload?.phase?.deload_in_days;
    const overrideSource = _corePrimeResolveOverrideSource(payload);
    const phaseTxt = Number.isFinite(Number(deloadIn))
      ? `Phase: ${phaseName} · Deload in ${Number(deloadIn)} Tagen`
      : `Phase: ${phaseName}`;
    setText(els.coreStatusPhase, phaseTxt);

    const compliance = payload?.compliance_7d && typeof payload.compliance_7d === "object" ? payload.compliance_7d : {};
    const timelineForCounts = Array.isArray(payload?.timeline?.days)
      ? payload.timeline.days
      : (Array.isArray(payload?.rolling_days) ? payload.rolling_days : []);
    let coreCountLive = 0;
    let userCountLive = 0;
    for (const d of timelineForCounts) {
      const items = Array.isArray(d?.items) ? d.items : [];
      for (const it of items) {
        const t = String(it?.type || "").toLowerCase();
        if (t !== "gym") continue;
        const effObj = it?.effective && typeof it.effective === "object" ? it.effective : {};
        const modeRaw = String(
          effObj?.mode_or_plan
          || it?.effective_mode
          || it?.mode_effective
          || ""
        ).toUpperCase();
        const isHeavyOrLight = modeRaw === "LIGHT" || modeRaw === "PUSH" || modeRaw === "HEAVY";
        if (!isHeavyOrLight) continue;
        const by = String(effObj?.changed_by || it?.changed_by || "").toLowerCase();
        if (by === "user") userCountLive += 1;
        else coreCountLive += 1;
      }
    }
    const coreCount = coreCountLive || Number(compliance.core_deviation_count ?? compliance.core_changed ?? 0);
    const userCount = userCountLive || Number(compliance.user_override_count ?? compliance.user_changed ?? 0);
    if (els.coreStatusCompliance) {
      setHtml(
        els.coreStatusCompliance,
        `<span class="core-status-mini-chip is-adhered"><span class="k">Eingehalten</span><span class="v">${escapeHtml(String(compliance.adhered_count ?? compliance.adhered ?? 0))}</span></span>
         <span class="core-status-mini-chip is-core"><span class="k">CORE</span><span class="v">${escapeHtml(String(coreCount || 0))}</span></span>
         <span class="core-status-mini-chip is-user"><span class="k">Du</span><span class="v">${escapeHtml(String(userCount || 0))}</span></span>
         <span class="core-status-mini-chip is-missed"><span class="k">Missed</span><span class="v">${escapeHtml(String(compliance.missed_count ?? compliance.missed ?? 0))}</span></span>`
      );
    }

    const timelineDaysRaw = Array.isArray(payload?.timeline?.days)
      ? payload.timeline.days
      : (Array.isArray(payload?.rolling_days) ? payload.rolling_days : []);
    const timelineDays = (() => {
      if (!isIpadLike()) return timelineDaysRaw;
      if (!Array.isArray(timelineDaysRaw) || !timelineDaysRaw.length) return [];

      const todayIdx = timelineDaysRaw.findIndex((d) => String(d?.relation || "").toLowerCase() === "today");
      if (todayIdx >= 0) {
        const start = Math.max(0, todayIdx - 1);
        const end = Math.min(timelineDaysRaw.length, todayIdx + 2);
        const aroundToday = timelineDaysRaw.slice(start, end);
        if (aroundToday.length === 3) return aroundToday;
      }

      const yesterday = [...timelineDaysRaw].reverse().find((d) => String(d?.relation || "").toLowerCase() === "past");
      const today = timelineDaysRaw.find((d) => String(d?.relation || "").toLowerCase() === "today");
      const tomorrow = timelineDaysRaw.find((d) => String(d?.relation || "").toLowerCase() === "future");
      const fallback = [yesterday, today, tomorrow].filter(Boolean);
      if (fallback.length) return fallback;
      return timelineDaysRaw.slice(0, 3);
    })();
    if (els.coreStatusTimeline) {
      const uiModeLabel = (modeRaw) => {
        const m = String(modeRaw || "").toUpperCase();
        if (m === "LIGHT") return "LIGHT";
        if (m === "PUSH" || m === "HEAVY") return "HEAVY";
        return "";
      };
      const uiRunPlanLabel = (planRaw) => {
        const p = String(planRaw || "").toUpperCase();
        if (p === "LONG") return "LONG";
        if (p === "QUALITY") return "QUALITY";
        return "EASY";
      };
      const normalizeRunTitle = (slotRaw) => {
        const slot = String(slotRaw || "").trim();
        if (!slot) return "Run";
        return /^run\b/i.test(slot) ? slot : `Run ${slot}`;
      };
      const badgeForItem = (it) => {
        const actual = it?.actual && typeof it.actual === "object" ? it.actual : {};
        const status = String(actual?.status || it?.status || "").toLowerCase();
        const eff = it?.effective && typeof it.effective === "object" ? it.effective : {};
        const by = String(eff?.changed_by || it?.changed_by || "").toLowerCase();
        if (status === "done") return { label: "DONE", cls: "done" };
        if (status === "missed") return { label: "MISSED", cls: "missed" };
        if (by === "user") return { label: "DU", cls: "user" };
        if (by === "core") return { label: "CORE", cls: "core" };
        return { label: "PLANNED", cls: "planned" };
      };
      const itemTitle = (it, t, planned) => {
        const raw = String(it?.title || "").trim();
        if (raw) return raw;
        if (t === "gym") return `Gym ${String(planned?.label || "").trim()}`.trim();
        return normalizeRunTitle(planned?.label || "");
      };
      const intensityRoleLine = (type, modeUi, relation) => {
        if (type === "run") {
          if (modeUi === "QUALITY") return "Qualitaets-Slot";
          if (modeUi === "LONG") return "Long-Run";
          return relation === "future" ? "Easy-Zwischentag" : "Easy sauber";
        }
        if (modeUi === "HEAVY") return relation === "future" ? "Heavy-Preview" : "Heavy sauber";
        if (modeUi === "LIGHT") return relation === "future" ? "Light-Preview" : "Light sauber";
        return relation === "future" ? "Plan-Preview" : "Plan-nah";
      };
      const buildCardCopy = ({ type, relation, actualStatus, beforeUi, afterUi, changedBy, avgRpe, isActiveDay }) => {
        const aligned = beforeUi === afterUi;
        const hasRpe = Number.isFinite(avgRpe);
        const byLabel = changedBy === "user" ? "DU" : (changedBy === "core" ? "CORE" : "");
        const role = intensityRoleLine(type, afterUi, relation);
        let summary = "";
        let context = "";
        const pick = (...vals) => {
          for (const v of vals) {
            const t = String(v || "").trim();
            if (t) return t;
          }
          return "";
        };

        if (relation === "past") {
          if (actualStatus === "missed") {
            summary = "Nicht umgesetzt";
            context = pick(byLabel ? `${byLabel}-Override` : "", "offen geblieben");
          } else if (actualStatus === "done") {
            if (!aligned && byLabel) summary = `${afterUi} mit Override`;
            else summary = role;
            context = pick(
              (hasRpe && type === "gym") ? `oRPE ${avgRpe.toFixed(1)}` : "",
              aligned ? "plan-nah" : "angepasst"
            );
          } else {
            summary = aligned ? "Plan-nah" : `${afterUi} aktiv`;
            context = pick(byLabel ? `${byLabel}-Override` : "", aligned ? "stabil" : "angepasst");
          }
        } else if (relation === "today") {
          if (isActiveDay) {
            summary = aligned ? `${afterUi} aktiv` : `${afterUi} gesetzt`;
          } else {
            summary = aligned ? `${afterUi} aktiv` : `${afterUi} vorbereitet`;
          }
          context = pick(
            (!aligned && byLabel) ? `${byLabel}-Override` : "",
            aligned ? "plan-nah" : "angepasst",
            isActiveDay ? "Steuerpunkt" : ""
          );
        } else {
          summary = role;
          context = pick(
            (!aligned && byLabel) ? `${byLabel}-Override` : "",
            (type === "run" && afterUi === "QUALITY") ? "Build-Anker" : "",
            (type === "gym" && afterUi === "HEAVY") ? "Build-Anker" : "",
            (type === "run" && afterUi === "EASY") ? "ruhig geplant" : "",
            (Number.isFinite(Number(deloadIn)) && Number(deloadIn) <= 4) ? "nah an Deload" : "",
            "planmaessig"
          );
        }

        if (!summary) summary = aligned ? `${afterUi} plan-nah` : `${afterUi} aktiv`;
        return { summary, context };
      };
      const buildChip = (it, relation, compact = false, isDayActive = false) => {
        const t = String(it?.type || "").toLowerCase();
        const planned = it?.planned && typeof it.planned === "object" ? it.planned : {};
        const effective = it?.effective && typeof it.effective === "object" ? it.effective : {};
        const actual = it?.actual && typeof it.actual === "object" ? it.actual : {};
        const actualStatus = String(actual?.status || it?.status || "planned").toLowerCase();
        const before = t === "gym" ? String(planned?.mode_or_plan || "NORMAL") : String(planned?.mode_or_plan || "EASY");
        const after = t === "gym" ? String(effective?.mode_or_plan || before) : String(effective?.mode_or_plan || before);
        const beforeUi = t === "gym" ? (uiModeLabel(before) || "PLAN") : uiRunPlanLabel(before);
        const afterUi = t === "gym" ? (uiModeLabel(after) || "PLAN") : uiRunPlanLabel(after);
        const changedBy = String(effective?.changed_by || it?.changed_by || "").toLowerCase();
        const intensity = String(it?.intensity_color || "neutral").toLowerCase();
        const title = itemTitle(it, t, planned);
        const avgRpe = Number(actual?.avg_rpe);
        const display = buildCardCopy({
          type: t,
          relation,
          actualStatus,
          beforeUi,
          afterUi,
          changedBy,
          avgRpe,
          isActiveDay: !!isDayActive,
        });
        const badge = badgeForItem(it);
        return `<button type="button" class="core-status-chip is-${escapeHtml(t || "core")} is-${escapeHtml(intensity)}${compact ? " is-compact" : ""}" data-core-session-id="${escapeHtml(String(it?.session_id || ""))}" data-core-type="${escapeHtml(t)}">
          <div class="core-status-chip-main">${escapeHtml(title)}</div>
          <div class="core-status-chip-sub">${escapeHtml(display.summary)}</div>
          ${display.context ? `<div class="core-status-chip-diff">${escapeHtml(display.context)}</div>` : ""}
          <div class="core-status-chip-badges"><span class="core-overview-status-pill is-${escapeHtml(badge.cls)}">${escapeHtml(badge.label)}</span></div>
        </button>`;
      };
      const dayDescriptors = timelineDays.map((day, idx) => {
        const items = Array.isArray(day?.items) ? day.items : [];
        const relation = String(day?.relation || "").toLowerCase();
        const dayKey = String(day?.date || `${idx}`);
        const editableItem = (relation === "past")
          ? null
          : (items.find((it) => String(it?.type || "").toLowerCase() === "gym")
            || items.find((it) => String(it?.type || "").toLowerCase() === "run")
            || null);
        const controlItem = (relation === "past")
          ? null
          : (editableItem || items[0] || null);
        return { day, items, relation, dayKey, editableItem, controlItem };
      });
      const todayDescriptor = dayDescriptors.find((d) => d.relation === "today") || dayDescriptors.find((d) => d.relation !== "past") || dayDescriptors[0] || null;
      const selectableDayKeys = dayDescriptors
        .filter((d) => d.relation !== "past")
        .map((d) => d.dayKey);
      let activeDayKey = String(state.coreStatusActiveDayKey || "");
      if (activeDayKey && !selectableDayKeys.includes(activeDayKey)) {
        activeDayKey = "";
      }
      state.coreStatusActiveDayKey = activeDayKey || null;

      const renderControls = (activeControlData) => {
        if (!activeControlData) return "";
        const isGym = activeControlData.target === "gym_mode";
        const allowed = isGym ? ["LIGHT", "NORMAL", "PUSH"] : ["EASY", "QUALITY", "LONG"];
        const currentRun = uiRunPlanLabel(activeControlData.value);
        const isLocked = !activeControlData.sessionId;
        return `
        <div class="core-status-today-controls" data-core-session-id="${escapeHtml(activeControlData.sessionId)}" data-core-target="${escapeHtml(activeControlData.target)}" data-core-current-value="${escapeHtml(isGym ? activeControlData.value : currentRun)}">
          <div class="core-status-control-label">Modusauswahl</div>
          <div class="core-status-segmented" role="group" aria-label="Modusauswahl">
            ${allowed.map((m) => {
              const label = isGym ? (m === "PUSH" ? "HEAVY" : m) : (m === "EASY" ? "Easy" : (m === "QUALITY" ? "Quality" : "Long"));
              const activeVal = isGym ? activeControlData.value : currentRun;
              return `<button type="button" class="core-mini-mode-btn${activeVal===m?' is-active':''}" data-core-day-control="${escapeHtml(m)}"${isLocked ? " disabled" : ""}>${escapeHtml(label)}</button>`;
            }).join("")}
          </div>
        </div>`;
      };

      const renderDay = (descriptor) => {
        const day = descriptor.day;
        const items = descriptor.items;
        const relation = descriptor.relation;
        const dayKey = descriptor.dayKey;
        const editableItem = descriptor.editableItem;
        const controlItem = descriptor.controlItem;
        const isActiveDay = !!(activeDayKey && dayKey && dayKey === activeDayKey && relation !== "past");
        const isToday = relation === "today";
        const isTodayPassive = isToday && !isActiveDay;
        let activeControlData = null;
        if (isActiveDay && controlItem) {
          const tRaw = String(controlItem?.type || "").toLowerCase();
          const t = tRaw === "run" ? "run" : "gym";
          const effective = controlItem?.effective && typeof controlItem.effective === "object" ? controlItem.effective : {};
          const val = String(
            effective?.mode_or_plan
            || controlItem?.effective_mode
            || controlItem?.mode_effective
            || controlItem?.effective_plan
            || controlItem?.plan_effective
            || (t === "gym" ? "NORMAL" : "EASY")
          ).toUpperCase();
          activeControlData = {
            sessionId: String(controlItem?.session_id || ""),
            target: t === "gym" ? "gym_mode" : "run_plan",
            value: val,
          };
        }
        const dayItems = items.slice(0, 1);
        const chips = dayItems.map((it) => buildChip(it, relation, relation !== "today", isActiveDay)).join("");
        const focusTitle = (() => {
          const focusItem = controlItem || editableItem;
          if (!focusItem) return "";
          const p = focusItem?.planned && typeof focusItem.planned === "object" ? focusItem.planned : {};
          const t = String(focusItem?.type || "").toLowerCase();
          return itemTitle(focusItem, t, p);
        })();
        const classes = [
          "core-status-day",
          `is-${relation || "future"}`,
          isActiveDay ? "is-mode-active" : "",
          isTodayPassive ? "is-today-passive" : "",
          relation !== "today" ? "is-compact" : "",
          (isToday && isActiveDay) ? "is-today-focus" : "",
        ].filter(Boolean).join(" ");
        const daySessionAttr = (relation !== "past")
          ? ` data-core-day-session-id="${escapeHtml(String(controlItem?.session_id || ""))}" data-core-day-editable="1"`
          : "";
        const dayKeyAttr = ` data-core-day-key="${escapeHtml(dayKey)}"`;
        return `<article class="${classes}" data-core-day-relation="${escapeHtml(relation)}"${dayKeyAttr}${daySessionAttr}>
          <div class="core-status-day-top">
            <div class="core-status-day-label">${escapeHtml(String(day?.label || ""))}</div>
            ${isActiveDay ? `<div class="core-status-day-focusline">${escapeHtml(focusTitle || "Aktiver Slot")}</div>` : ""}
          </div>
          <div class="core-status-day-items">${chips || '<div class="core-overview-fallback">Rest</div>'}</div>
          ${isActiveDay ? renderControls(activeControlData) : ""}
        </article>`;
      };
      const html = dayDescriptors.map((d) => renderDay(d)).join("");
      const todayIdx = dayDescriptors.findIndex((d) => d.relation === "today");
      const activeIdx = dayDescriptors.findIndex((d) => d.dayKey === activeDayKey);
      const focusIdx = activeIdx >= 0 ? activeIdx : todayIdx;
      const focusPct = (focusIdx >= 0 && dayDescriptors.length > 0)
        ? `${(((focusIdx + 0.5) / dayDescriptors.length) * 100).toFixed(2)}%`
        : "50%";
      setHtml(
        els.coreStatusTimeline,
        html
          ? `<div class="core-status-flow" style="--core-focus-pct:${escapeHtml(focusPct)}">${html}</div>`
          : "<div class=\"core-overview-fallback\">Nächste Sessions werden vorbereitet.</div>"
      );
    }

    setText(els.coreStatusDiff, "Änderungen (7d)");

    if (els.coreStatusDiffList) {
      const items = Array.isArray(payload?.changes_7d) ? payload.changes_7d : (Array.isArray(payload?.diff?.items) ? payload.diff.items : []);
      const html = items.slice(0, 1).map((it) => {
        const byRaw = String(it?.by || "CORE");
        const by = byRaw.toUpperCase() === "USER" ? "DU" : "CORE";
        const byCls = by === "DU" ? "user" : "core";
        return `<div class="core-status-diff-single"><span class="txt">${escapeHtml(String(it?.text || ""))}</span><span class="core-overview-status-pill is-${escapeHtml(byCls)}">${escapeHtml(by)}</span></div>`;
      }).join("");
      setHtml(
        els.coreStatusDiffList,
        html || `<div class="core-status-diff-empty">Keine relevanten Änderungen im 7-Tage-Fenster.</div>`
      );
    }
    if (els.coreDecisionInfo) {
      const note = String(payload.note || "").trim();
      if (note) _showCoreDecisionInfo(note, false);
    }
  }

  function renderBlockStatus(payload, hrvCtx = null, controlPlane = null) {
    if (!els.blockStatusCard) return;
    const normalized = _normalizeBlockStatusPayload(payload);
    if (normalized.mode === "error") {
      renderBlockStatusError();
      return;
    }
    _setBlockStatusState("content");

    const data = normalized.payload || {};
    const hasPlan = !!data.has_plan;
    const statusLine = String(data.status_line || "").trim() || (hasPlan ? "Phase/Woche unbekannt" : "Kein aktiver Plan");
    const roleLine = hasPlan
      ? "Job: Leitplanken im Block halten (Caps/Faktoren), keine Tagesentscheidung hier."
      : "Job: Plan wählen und aktivieren.";
    const jobLine = _normalizeJobLine(roleLine);
    const heroLine = _blockStatusHeroLine(data);
    const subLine = _blockStatusSubline(data) || (hasPlan ? "" : "Wähle einen Plan in /planung");
    const badgeView = _badgeView(data, hasPlan);
    if (els.blockStatusBadge) {
      els.blockStatusBadge.textContent = badgeView.text;
      els.blockStatusBadge.dataset.badge = badgeView.badgeKey;
      els.blockStatusBadge.dataset.age = badgeView.badgeAge || "";
      els.blockStatusBadge.title = badgeView.tooltip || "";
    }

    setText(els.blockStatusStatusLine, heroLine || statusLine);
    setText(els.blockStatusSubline, subLine || "");
    if (els.blockStatusSubline) els.blockStatusSubline.hidden = !String(subLine || "").trim();
    setHtml(els.blockStatusJobLine, _emphasizePrefix(jobLine, "Job:"));
    setHtml(els.blockStatusHrvLine, "");
    let chipGuardrail = hasPlan ? String(data.chip_guardrail || "Leitplanke: Planvorgaben einhalten") : "Leitplanke: —";
    const cap = controlPlane?.governor?.final_gym_rpe_cap;
    if (hasPlan && Number.isFinite(Number(cap))) {
      chipGuardrail = `Leitplanke: Topsatz max RPE ${_fmtNum(Number(cap), 1)}`;
    }
    const chipExpectation = hasPlan ? _chipExpectationCopy(data, data.chip_expectation || "") : "Erwartung: —";
    setText(els.blockStatusChipGuardrail, _clipRule(chipGuardrail));
    setText(els.blockStatusChipExpectation, _clipRule(chipExpectation));
    if (els.blockStatusJobWrap) els.blockStatusJobWrap.hidden = false;
    if (els.blockStatusHrvWrap) els.blockStatusHrvWrap.hidden = true;
    if (els.blockStatusPillsWrap) els.blockStatusPillsWrap.hidden = !hasPlan;
    els.blockStatusCard.classList.toggle("is-no-plan", !hasPlan);
    if (els.blockStatusError) els.blockStatusError.hidden = true;
    if (els.blockStatusInfo) {
      els.blockStatusInfo.hidden = !normalized.partial;
      if (normalized.partial) els.blockStatusInfo.textContent = "Hinweis: Teilweise Daten fehlen.";
    }
  }

  const MEMORY_CATEGORY_MAP = {
    load_response: "recovery",
    sleep_response: "recovery",
    alcohol_effect: "recovery",
    lower_heavy_nextday_recovery: "recovery",
    kienzl_intervention_effect: "recovery",
    sleep_to_rpe_drift: "recovery",
    stacked_hard_days: "recovery",
    first_set_daytype: "strength",
    strength_trend: "strength",
    run_rhythm: "run",
    run_consistency: "run",
    lower_to_run_interference: "run",
    nutrition_variance_recovery: "nutrition",
    planning_drift_pattern: "rhythm",
    weekday_pattern: "rhythm",
  };

  const MEMORY_TITLE_MAP = {
    load_response: "High Load → RMSSD ↓",
    lower_heavy_nextday_recovery: "Heavy Legs (Topset ≥8.5) → RMSSD ↓",
    lower_to_run_interference: "Heavy Legs → Run-HF ↑",
    stacked_hard_days: "2 Hard-Days in Folge → Next-Day RMSSD ↓",
    sleep_to_rpe_drift: "Schlafqualität ≤6 → Session-RPE ↑",
    first_set_daytype: "Erster Satz RPE hoch → Session-RPE ↑",
    run_rhythm: "Run-Gap/Frequenz → Rhythmusdrift",
    run_consistency: "≥2 Runs/Woche → RMSSD ↑",
    strength_trend: "Kraft-Load (28d vs 28d) → Trend",
    weekday_pattern: "Wochentag → Recovery-Differenz",
    nutrition_variance_recovery: "Kalorien-Varianz hoch → Recovery ↓",
    planning_drift_pattern: "Planungsdrift ↑ → Bad-Actions ↑",
    kienzl_intervention_effect: "EASY_DOWN/STOP → Folgetag Recovery ↑",
  };

  const MEMORY_MOCK = [
    { id: "m1", tag: "recovery", title: "Heavy Legs → RMSSD ↓", finding: "Nach harten Legs ist der nächste Morgen oft schwerer.", implication: "Plane den Folgetag eher kontrolliert statt maximal.", confidence: 0.86, evidence: { samples: 12 }, trigger: "Lower + Topset ≥8.5 vs Next-Day-RMSSD" },
    { id: "m2", tag: "run", title: "Run Gap >6 Tage → Pace Drift ↑", finding: "Lange Laufpausen machen den nächsten Run unrund.", implication: "Lieber kürzere, konstante Laufabstände halten.", confidence: 0.72, evidence: { samples: 7 }, trigger: "Run Gap >6 vs ≤6 Tage" },
    { id: "m3", tag: "recovery", title: "Late Training >20:30 → RHR ↑", finding: "Späte harte Einheiten kosten häufig Erholung.", implication: "Abends eher leicht trainieren oder früher starten.", confidence: 0.79, evidence: { samples: 10 }, trigger: "Training-Zeitpunkt spät vs normal" },
    { id: "m4", tag: "strength", title: "2 Tage nahe Cap → 3. Tag Satzqualität ↓", finding: "Zwei harte Tage hintereinander drücken oft den dritten Tag.", implication: "Hard + kontrolliert funktioniert stabiler als Hard + Hard.", confidence: 0.74, evidence: { samples: 11 }, trigger: "Hard+Hard vs Hard+Easy" },
    { id: "m5", tag: "run", title: "Threshold-Run → Lower-Tag danach zäher", finding: "Nach harten Runs fühlt sich Lower oft schwerer an.", implication: "Umfeld nach harten Runs bewusst einfacher planen.", confidence: 0.69, evidence: { samples: 9 }, trigger: "Run-Typ Threshold vs Easy" },
    { id: "m6", tag: "rhythm", title: "Run-Frequenz stabil → Recovery stabiler", finding: "Konstante Run-Frequenz macht die Woche ruhiger.", implication: "Rhythmus halten statt mit Einzelsessions zu kompensieren.", confidence: 0.77, evidence: { samples: 8 }, trigger: "Wochenvergleich Runs" },
    { id: "m7", tag: "nutrition", title: "Spätes Essen → RHR ↑", finding: "Sehr spätes Essen stört oft den nächsten Tag.", implication: "Späte Mahlzeiten kleiner und planbarer halten.", confidence: 0.71, evidence: { samples: 9 }, trigger: "Meal-Timing spät vs früh" },
    { id: "m8", tag: "strength", title: "Erster Satz RPE hoch → Session driftet", finding: "Ein harter erster Satz kippt oft die ganze Einheit.", implication: "Früh drosseln, wenn der Einstieg schon zu schwer ist.", confidence: 0.81, evidence: { samples: 13 }, trigger: "First Set ≥8.5 vs ≤7.5" },
    { id: "m9", tag: "recovery", title: "High Load → Next-Day RMSSD ↓", finding: "Hohe Last wirkt oft bis in den nächsten Morgen.", implication: "Nach Peak-Tagen aktiv Recovery einplanen.", confidence: 0.9, evidence: { samples: 17 }, trigger: "Load quartile Q4 vs Q1" },
    { id: "m10", tag: "nutrition", title: "Kalorien-Varianz ↑ → Plan-Drift ↑", finding: "Starke Schwankungen machen den Plan instabiler.", implication: "Konstantere Defaults helfen dir, im Plan zu bleiben.", confidence: 0.68, evidence: { samples: 12 }, trigger: "CAL_VARIANCE_HIGH" },
  ];

  function _memoryCategory(entry) {
    const id = String(entry?.id || "");
    const tag = String(entry?.tag || "").toLowerCase();
    if (MEMORY_CATEGORY_MAP[id]) return MEMORY_CATEGORY_MAP[id];
    if (["recovery", "sleep", "risk"].includes(tag)) return "recovery";
    if (["run"].includes(tag)) return "run";
    if (["nutrition"].includes(tag)) return "nutrition";
    if (["strength"].includes(tag)) return "strength";
    if (["rhythm", "planning"].includes(tag)) return "rhythm";
    return "other";
  }

  function _memoryNormConf(raw) {
    const n = Number(raw);
    if (!Number.isFinite(n)) return 0;
    return n > 1 ? Math.max(0, Math.min(1, n / 100)) : Math.max(0, Math.min(1, n));
  }

  function _memoryPriority(rank) {
    if (rank >= 0.7) return "high";
    if (rank >= 0.45) return "med";
    return "low";
  }

  function _memoryPriorityLabel(priority) {
    if (priority === "high") return "HOCH";
    if (priority === "med") return "MITTEL";
    return "NIEDRIG";
  }

  function _buildLearningViewRows(payload) {
    const source = Array.isArray(payload?.learnings) && payload.learnings.length ? payload.learnings : MEMORY_MOCK;
    /** @type {LearningView[]} */
    const rows = source.map((entry) => {
      const conf = _memoryNormConf(entry?.confidence);
      const n = Number((entry?.evidence || {}).samples || 0);
      const finding = String(entry?.finding || "").trim();
      const implication = String(entry?.implication || "").trim();
      const rank = conf * (0.65 + (Math.min(1, Math.log(Math.max(2, n)) / Math.log(18)) * 0.35));
      const priority = _memoryPriority(rank);
      const category = _memoryCategory(entry);
      const compactTitle = _clipRule(MEMORY_TITLE_MAP[String(entry?.id || "")] || String(entry?.title || "Learning"), 80);
      return {
        id: String(entry?.id || `learning-${Math.random()}`),
        title: compactTitle,
        summary: _clipRule(implication || finding || "Keine Kurzfassung verfügbar.", 96),
        conf,
        n,
        rank,
        category,
        priority,
        priorityLabel: _memoryPriorityLabel(priority),
        compare: String(entry?.trigger || "Vergleich nicht spezifiziert"),
        details: _clipRule(finding || implication || "Kein Detailtext vorhanden.", 200),
        raw: entry,
      };
    });
    return rows;
  }

  function _selectMainLearningRows(allRows) {
    const sorted = [...allRows].sort((a, b) => b.rank - a.rank);
    const selected = [];
    const fallback = [];
    const categoryCounts = { recovery: 0, strength: 0, run: 0, nutrition: 0, rhythm: 0, other: 0 };
    const usedFingerprint = new Set();
    const eligibleByCategory = { recovery: [], strength: [], run: [], nutrition: [], rhythm: [], other: [] };

    const tryAdd = (row) => {
      if (!row) return false;
      if ((categoryCounts[row.category] || 0) >= 3) return false;
      const fp = `${row.category}:${row.title.split("→")[1] || row.title}`;
      if (usedFingerprint.has(fp)) return false;
      if (selected.some((x) => x.id === row.id)) return false;
      usedFingerprint.add(fp);
      categoryCounts[row.category] = (categoryCounts[row.category] || 0) + 1;
      selected.push(row);
      return true;
    };

    for (const row of sorted) {
      const passN = row.n >= 4;
      const passQuality = row.conf >= 0.45;
      if (passN && passQuality) {
        (eligibleByCategory[row.category] || eligibleByCategory.other).push(row);
      } else {
        fallback.push(row);
      }
    }

    ["recovery", "strength", "run", "nutrition", "rhythm"].forEach((cat) => {
      const pool = eligibleByCategory[cat] || [];
      for (let i = 0; i < Math.min(2, pool.length); i += 1) {
        tryAdd(pool[i]);
        if (selected.length >= 12) return;
      }
    });

    for (const row of sorted) {
      if (selected.length >= 12) break;
      const passN = row.n >= 4;
      const passQuality = row.conf >= 0.45;
      if (!passN || !passQuality) continue;
      tryAdd(row);
      if (selected.length >= 12) break;
    }
    if (selected.length < 8) {
      for (const row of fallback) {
        if (selected.some((x) => x.id === row.id)) continue;
        selected.push(row);
        if (selected.length >= 8) break;
      }
    }
    return selected;
  }

  function _memoryFilterMatch(row, filter) {
    if (!filter || filter === "all") return true;
    if (filter === "gym") return row.category === "strength" || row.category === "rhythm";
    if (filter === "run") return row.category === "run";
    if (filter === "recovery") return row.category === "recovery";
    if (filter === "nutrition") return row.category === "nutrition";
    return true;
  }

  function _renderLearningRows(rows) {
    if (!els.autopilotMemoryList) return;
    const html = rows.map((row) => {
      return `
        <button type="button" class="memory-row" data-learning-id="${escapeHtml(row.id)}">
          <span class="memory-row-main">
            <span class="memory-row-title">${escapeHtml(row.title)}</span>
            <span class="memory-row-summary">${escapeHtml(row.summary)}</span>
          </span>
          <span class="memory-row-right">
            <span class="memory-row-pill is-${row.priority}">${escapeHtml(row.priorityLabel)}</span>
            <span class="memory-row-arrow">›</span>
          </span>
        </button>
      `;
    }).join("");
    setHtml(els.autopilotMemoryList, html);
  }

  function _openMemoryDrawerById(id) {
    const row = (state.autopilotMemoryAllRows || []).find((x) => x.id === id);
    if (!row || !els.autopilotMemoryDrawer) return;
    state.autopilotMemorySelectedId = row.id;
    state.autopilotMemoryDetailsOpen = true;
    setText(els.autopilotMemoryDrawerTitle, row.title);
    setText(els.autopilotMemoryDrawerEffect, row.summary);
    setText(els.autopilotMemoryDrawerN, row.n > 0 ? `${row.n} Beobachtungen` : "wenig Daten");
    setText(els.autopilotMemoryDrawerConf, row.priorityLabel);
    setText(els.autopilotMemoryDrawerCompare, row.compare);
    setText(els.autopilotMemoryDrawerDesc, row.details);
    if (els.autopilotMemoryDrawerList) {
      const listHtml = (state.autopilotMemoryAllRows || []).slice(0, 20).map((x) =>
        `<button type="button" class="memory-drawer-item${x.id === row.id ? " is-active" : ""}" data-learning-id="${escapeHtml(x.id)}">${escapeHtml(x.title)}</button>`
      ).join("");
      setHtml(els.autopilotMemoryDrawerList, listHtml);
    }
    els.autopilotMemoryDrawer.hidden = false;
  }

  function _closeMemoryDrawer() {
    state.autopilotMemoryDetailsOpen = false;
    if (els.autopilotMemoryDrawer) els.autopilotMemoryDrawer.hidden = true;
  }

  function renderAutopilotMemory(payload) {
    if (!els.blockStatusCard) return;
    if (!payload || payload._error) {
      renderBlockStatusError();
      return;
    }

    const allRows = _buildLearningViewRows(payload);
    const selectedRows = _selectMainLearningRows(allRows);
    const renderKey = JSON.stringify({
      day: payload.as_of_day || "",
      filter: state.autopilotMemoryFilter,
      rows: selectedRows.map((x) => [x.id, x.rank, x.summary]),
    });
    if (state.autopilotMemoryRenderKey && state.autopilotMemoryRenderKey === renderKey) return;
    state.autopilotMemoryRenderKey = renderKey;

    _setBlockStatusState("content");
    els.blockStatusCard.classList.remove("is-no-plan");
    state.autopilotMemoryAllRows = [...allRows].sort((a, b) => b.rank - a.rank);

    const filter = state.autopilotMemoryFilter || "all";
    if (els.autopilotMemoryFilters) {
      els.autopilotMemoryFilters.querySelectorAll("[data-memory-filter]").forEach((chip) => {
        chip.classList.toggle("is-active", String(chip.getAttribute("data-memory-filter")) === filter);
      });
    }
    const visibleRows = selectedRows.filter((x) => _memoryFilterMatch(x, filter)).slice(0, 6);
    state.autopilotMemoryRows = visibleRows;

    if (els.autopilotMemoryWindowSub) {
      setText(els.autopilotMemoryWindowSub, `letzte ${Number(payload.window_days || AUTOPILOT_MEMORY_DAYS)} Tage`);
    }

    const top = visibleRows[0] || selectedRows[0] || allRows[0] || null;
    const topConf = Number(top?.conf || 0);
    if (els.blockStatusBadge) {
      els.blockStatusBadge.textContent = top ? `FOKUS ${top.priorityLabel}` : "FOKUS --";
      els.blockStatusBadge.dataset.badge = topConf >= 0.75 ? "consistent" : (topConf >= 0.5 ? "drift" : "no-plan");
      els.blockStatusBadge.dataset.age = "";
      els.blockStatusBadge.title = top ? top.summary : "";
    }

    _renderLearningRows(visibleRows);
    setText(els.blockStatusChipGuardrail, top ? `Top: ${_clipRule(top.title, 66)}` : "Top: —");
    setText(els.autopilotMemoryShowAll, `Mehr (${state.autopilotMemoryAllRows.length})`);
    if (els.blockStatusPillsWrap) els.blockStatusPillsWrap.hidden = false;
    if (els.blockStatusError) els.blockStatusError.hidden = true;
    if (els.blockStatusInfo) {
      els.blockStatusInfo.hidden = visibleRows.length >= 5;
      if (!els.blockStatusInfo.hidden) els.blockStatusInfo.textContent = "Noch wenige klare Muster im gewählten Filter.";
    }
    if (state.autopilotMemoryDetailsOpen && state.autopilotMemorySelectedId) {
      _openMemoryDrawerById(state.autopilotMemorySelectedId);
    } else if (els.autopilotMemoryDrawer) {
      els.autopilotMemoryDrawer.hidden = true;
    }
  }

  // =========================================================
  // RENDER: Focus (simple priority)
  // =========================================================
  function setFocus(data) {
    if (!els.focusCard) return;

    if (!state.autoInsights) {
      els.focusCard.classList.add("is-hidden");
      return;
    }
    els.focusCard.classList.remove("is-hidden");

    const candidates = [
      { key: "recovery", status: data.readinessStatus },
      { key: "calories", status: data.kcalStatus },
      { key: "protein", status: data.proteinStatus },
      { key: "training", status: data.trainingStatus },
      { key: "runs", status: data.runsStatus }
    ].sort((a, b) => statusToLevel(b.status) - statusToLevel(a.status));

    const main = candidates[0];

    let title = "Fokus: Plan halten.";
    let why = "Warum: Keine kritischen Abweichungen.";
    let chip1 = `Konsequenz: stabil.`;
    let chip2 = `Hebel: weiter so`;
    let action = "weiter so";

    if (main.key === "runs") {
      title = "Fokus: Lauf-Plan.";
      why = data.runsExpected > 0 ? `Warum: Runs ${data.runsCount}/${data.runsExpected}.` : `Warum: Runs ${data.runsCount}.`;
      chip1 = "Konsequenz: Laufvolumen fehlt.";
      chip2 = "Hebel: Easy Run 20 min";
      action = "Easy Run 20 min";
    } else if (main.key === "training") {
      title = "Fokus: Kraft-Plan.";
      why = data.strengthExpected > 0 ? `Warum: Einheiten ${data.trainingSessions}/${data.strengthExpected}.` : `Warum: Einheiten ${data.trainingSessions}.`;
      chip1 = "Konsequenz: Plan driftet.";
      chip2 = "Hebel: 1 Einheit loggen";
      action = "1 Einheit loggen";
    } else if (main.key === "protein") {
      title = "Fokus: Protein treffen.";
      why = data.proteinTotal ? `Warum: Treffer ${data.proteinMet}/${data.proteinTotal}.` : "Warum: Protein unklar.";
      chip1 = data.avgGPerKg ? `Konsequenz: Ø ${formatNumber(data.avgGPerKg, 1)} g/kg.` : "Konsequenz: g/kg unklar.";
      chip2 = "Hebel: Protein +30 g";
      action = "Protein +30 g";
    } else if (main.key === "calories") {
      title = "Fokus: Kalorien treffen.";
      why = data.deltaKcal !== null ? `Warum: Δ kcal ${formatSigned(data.deltaKcal, 0)} vs Ziel.` : "Warum: Ziel/Kalorien unklar.";
      chip1 = data.bandPct !== null ? `Konsequenz: Band ${formatNumber(data.bandPct, 0)}%.` : "Konsequenz: Band unklar.";
      chip2 = data.deltaKcal !== null ? `Hebel: ${data.deltaKcal < 0 ? "+200 kcal" : "-200 kcal"}` : "Hebel: tracken";
      action = data.deltaKcal !== null ? (data.deltaKcal < 0 ? "+200 kcal" : "-200 kcal") : "tracken";
    } else if (main.key === "recovery") {
      title = "Fokus: Regeneration.";
      why = data.readinessScore !== null ? `Warum: Readiness ${formatNumber(data.readinessScore, 0)}.` : "Warum: Readiness unklar.";
      chip1 = `Konsequenz: Trend ${getTrendIndicator(data.trendDir)}.`;
      chip2 = "Hebel: Belastung reduzieren";
      action = "Belastung reduzieren";
    }

    setText(els.focusTitle, title);
    setText(els.focusSub, why);
    setText(els.focusChip1, chip1);
    setText(els.focusChip2, chip2);
    setText(els.focusAction, action);
  }

  // =========================================================
  // RENDER: Signals (14d squares) + day labels
  // =========================================================
  function weekdayShortLabel(isoDate) {
    const d = parseIsoDate(isoDate);
    if (!d) return "";
    const s = new Intl.DateTimeFormat("de-DE", { timeZone: TIME_ZONE, weekday: "short" }).format(d);
    return String(s || "").replace(".", "").slice(0, 2);
  }

  function weekdayLongLabel(isoDate) {
    const d = parseIsoDate(isoDate);
    if (!d) return "";
    return new Intl.DateTimeFormat("de-DE", {
      timeZone: TIME_ZONE,
      weekday: "long",
      day: "2-digit",
      month: "2-digit",
    }).format(d);
  }


  // ===============================
  // PLAN-CHECK (restored)
  // ===============================

  const pcFinite = (x) => Number.isFinite(Number(x));
  const pcNum = (x) => (pcFinite(x) ? Number(x) : null);

  function pcEl(id) {
    return document.getElementById(id);
  }

  function pcStripSetPrefix(s) {
    const t = String(s || "").trim();
    const tokenCount = (t.match(/[x×]/gi) || []).length;
    if (tokenCount < 2) return t;
    return t.replace(/^\s*\d+\s*[x×]\s*/i, "").trim();
  }

  function stepForDevice(name, device) {
    const hay = `${String(name || "")} ${String(device || "")}`.toLowerCase();
    if (/(kurzhantel|dumbbell|db\b|dbs\b)/i.test(hay)) return 2;
    if (/(kabel|cable|seil|sz\b|ez\b|wadenheben|calf|egym)/i.test(hay)) return 1;
    if (/(smith|langhantel|barbell|lh\b|multipresse)/i.test(hay)) return 2.5;
    return 1;
  }

  function pcBuildReferenceSnapshot(done, planned, step) {
    const repsRaw = Array.isArray(done?.reps) ? done.reps.map((x) => parseInt(x, 10)).filter((n) => Number.isFinite(n)) : [];
    const weightsRaw = Array.isArray(done?.weights) ? done.weights.map((x) => pcNum(x)).filter((n) => Number.isFinite(n)) : [];
    const completedSets = parseInt(done?.completed_sets, 10);
    const visibleSetCount = Math.max(
      repsRaw.length,
      weightsRaw.length,
      Number.isFinite(completedSets) ? completedSets : 0
    );
    const hasVisibleLoggedReference = repsRaw.length > 0 && weightsRaw.length > 0;
    if (hasVisibleLoggedReference) {
      return {
        reps: repsRaw.slice(),
        weights: weightsRaw.slice(),
        setCount: visibleSetCount || Math.max(repsRaw.length, weightsRaw.length),
        source: "logged",
      };
    }

    const ref = done?.last_reference && typeof done.last_reference === "object" ? done.last_reference : null;
    if (!ref) return null;

    const refReps = parseInt(ref.reps, 10);
    const refWeight = pcNum(ref.weight);
    const plannedSets = parseInt(planned?.sets, 10);
    const refSets = parseInt(ref.sets, 10);
    const setCount = Math.max(
      1,
      Number.isFinite(refSets) ? refSets : 0,
      Number.isFinite(plannedSets) ? plannedSets : 0,
      Number.isFinite(completedSets) ? completedSets : 0
    );

    return {
      reps: Array.from({ length: setCount }, () => (Number.isFinite(refReps) ? refReps : null)),
      weights: Array.from({ length: setCount }, () => (Number.isFinite(refWeight) ? roundToStep(refWeight, step) : null)),
      setCount,
      source: "last_reference",
    };
  }

  function pcNormalizePlanSnapshot(today, reference, step) {
    const refSetCount = Number.isFinite(parseInt(reference?.setCount, 10)) ? parseInt(reference.setCount, 10) : 0;
    const todayReps = Array.isArray(today?.reps) ? today.reps.map((x) => parseInt(x, 10)).map((v) => (Number.isFinite(v) ? v : null)) : [];
    const todayWeights = Array.isArray(today?.weights) ? today.weights.map((x) => pcNum(x)).map((v) => (Number.isFinite(v) ? roundToStep(v, step) : null)) : [];
    const refReps = Array.isArray(reference?.reps) ? reference.reps.map((x) => parseInt(x, 10)).map((v) => (Number.isFinite(v) ? v : null)) : [];
    const refWeights = Array.isArray(reference?.weights) ? reference.weights.map((x) => pcNum(x)).map((v) => (Number.isFinite(v) ? roundToStep(v, step) : null)) : [];
    const setCount = Math.max(todayReps.length, todayWeights.length, refReps.length, refWeights.length, refSetCount, 0);

    const expand = (arr) => {
      if (!setCount) return [];
      if (!arr.length) return Array.from({ length: setCount }, () => null);
      if (arr.length === 1 && setCount > 1) return Array.from({ length: setCount }, () => arr[0]);
      const out = arr.slice(0, setCount);
      while (out.length < setCount) out.push(out[out.length - 1] ?? null);
      return out;
    };

    return {
      setCount,
      todayReps: expand(todayReps),
      todayWeights: expand(todayWeights),
      refReps: expand(refReps),
      refWeights: expand(refWeights),
    };
  }

  function pcComputeVolume(reps, weights) {
    const pairCount = Math.min(Array.isArray(reps) ? reps.length : 0, Array.isArray(weights) ? weights.length : 0);
    let total = 0;
    for (let i = 0; i < pairCount; i += 1) {
      const r = parseInt(reps[i], 10);
      const w = pcNum(weights[i]);
      if (!Number.isFinite(r) || !Number.isFinite(w)) continue;
      total += r * w;
    }
    return total;
  }

  function pcCompareWithTolerance(todayValue, refValue, tolerance) {
    const a = Number(todayValue);
    const b = Number(refValue);
    if (!Number.isFinite(a) || !Number.isFinite(b)) return 0;
    if (Math.abs(a - b) <= Math.max(0, Number(tolerance) || 0)) return 0;
    return a > b ? 1 : -1;
  }

  function pcParseVisiblePlanMetrics(text) {
    const raw = String(text || "").trim();
    if (!raw) return { weights: [], avgWeight: null, topWeight: null, setCount: 0 };

    const normalized = raw.replace(/\s+/g, " ").replace(/·/g, " · ");
    const beforeAt = String(normalized.split("@")[0] || "").trim();

    const parseNumbers = (value) => String(value || "")
      .split("/")
      .map((part) => part.trim().replace(",", "."))
      .filter(Boolean)
      .map((part) => Number(part))
      .filter((num) => Number.isFinite(num));

    const setMatch = beforeAt.match(/^\s*(\d+)\s*[×x]/);
    let setCount = setMatch ? parseInt(setMatch[1], 10) : 0;

    let weightPart = "";
    const dotSplit = beforeAt.split("·");
    if (dotSplit.length > 1) {
      weightPart = String(dotSplit[dotSplit.length - 1] || "").replace(/kg/gi, "").trim();
    } else {
      const xParts = beforeAt.split(/[×x]/).map((part) => String(part || "").trim()).filter(Boolean);
      if (xParts.length >= 3) {
        weightPart = String(xParts[xParts.length - 1] || "").replace(/kg/gi, "").trim();
      } else if (xParts.length === 2 && /kg/i.test(xParts[0])) {
        weightPart = String(xParts[0] || "").replace(/kg/gi, "").trim();
        setCount = 1;
      } else if (xParts.length >= 2) {
        weightPart = String(xParts[1] || "").replace(/kg/gi, "").trim();
      }
    }

    const weights = parseNumbers(weightPart);
    const avgWeight = weights.length ? (weights.reduce((sum, val) => sum + val, 0) / weights.length) : null;
    const topWeight = weights.length ? weights[0] : null;
    return { weights, avgWeight, topWeight, setCount };
  }

  function pcNormalizeExerciseKey(value) {
    return String(value || "")
      .toLowerCase()
      .replace(/\([^)]*\)/g, " ")
      .replace(/[^a-z0-9äöüß]+/gi, "")
      .trim();
  }

  function pcFindCoreTrainingItem(coreCard, exerciseName) {
    const items = Array.isArray(coreCard?.items) ? coreCard.items : [];
    const targetKey = pcNormalizeExerciseKey(exerciseName);
    if (!targetKey) return null;
    return items.find((item) => {
      const candidates = [
        item?.display_name,
        item?.exercise_name,
        item?.title,
        item?.label,
      ];
      return candidates.some((candidate) => {
        const candidateKey = pcNormalizeExerciseKey(candidate);
        return candidateKey && (candidateKey === targetKey || candidateKey.includes(targetKey) || targetKey.includes(candidateKey));
      });
    }) || null;
  }

  function pcFormatTodayFromCoreItem(coreItem) {
    if (!coreItem || typeof coreItem !== "object") return "";
    const sets = String(coreItem.target_sets || coreItem.recommended_sets || "").trim();
    const reps = String(coreItem.target_reps || coreItem.rep_target || "").trim().replace(/\//g, "–");
    const weight = String(coreItem.target_weight || coreItem.weight_target || "").trim().replace(/\s+/g, " ");
    const rpe = String(coreItem.target_rpe || coreItem.rpe_cap || "").trim();
    let text = "";
    if (sets && reps) text = `${sets}×${reps}`;
    else if (sets) text = sets;
    else if (reps) text = reps;
    if (weight) text = text ? `${text} · ${weight}` : weight;
    if (rpe) text = `${text} @${rpe}`.trim();
    return text;
  }

  function pcFormatReferenceFromCoreItem(coreItem) {
    if (!coreItem || typeof coreItem !== "object") return "";
    return String(coreItem.reference_line || "")
      .trim()
      .replace(/\s*@RPE\s*/i, " @")
      .replace(/\s*·\s*\d{4}-\d{2}-\d{2}\s*$/i, "")
      .trim();
  }

  function pcComputeExerciseBadge({ name, planned, todayText, lastText, step }) {
    const tolerance = Math.max(0, Number(stepForDevice(name, planned?.device) || step || 1));
    const todayMetrics = pcParseVisiblePlanMetrics(todayText);
    const refMetrics = pcParseVisiblePlanMetrics(lastText);

    const todayTop = todayMetrics.topWeight;
    const refTop = refMetrics.topWeight;
    const weightDir = (Number.isFinite(todayTop) && Number.isFinite(refTop))
      ? pcCompareWithTolerance(todayTop, refTop, tolerance)
      : 0;

    let label = "bleibt";
    let color = "neutral";
    let stateClass = "is-hold";

    if (weightDir > 0) {
      label = "Gewicht ↑";
      color = "green";
      stateClass = "is-up";
    } else if (weightDir < 0) {
      label = "Gewicht ↓";
      color = "red";
      stateClass = "is-down";
    } else {
      const setDir = pcCompareWithTolerance(todayMetrics.setCount, refMetrics.setCount, 0);
      if (setDir > 0) {
        label = "Sätze ↑";
        color = "green";
        stateClass = "is-up";
      } else if (setDir < 0) {
        label = "Sätze ↓";
        color = "red";
        stateClass = "is-down";
      }
    }

    return {
      label,
      color,
      stateClass,
      details: {
        weightUp: weightDir > 0,
        weightDown: weightDir < 0,
        setsUp: label === "Sätze ↑",
        setsDown: label === "Sätze ↓",
        hasCompetingChanges: false,
        referenceSource: "visible",
      },
    };
  }

  function pcBuildForecastSummary({ sessionName, mode, badges, changeLines }) {
    const badgeList = Array.isArray(badges) ? badges : [];
    const total = Math.max(badgeList.length, 1);
    const weightDownCount = badgeList.filter((b) => b?.details?.weightDown).length;
    const weightUpCount = badgeList.filter((b) => b?.details?.weightUp).length;
    const setChanges = badgeList.filter((b) => b?.details?.setsUp || b?.details?.setsDown).length;
    const downRatio = weightDownCount / total;
    const upRatio = weightUpCount / total;

    const rawLines = (Array.isArray(changeLines) ? changeLines : [])
      .map((line) => String(line || "").trim())
      .filter(Boolean);
    const joined = rawLines.join(" | ").toLowerCase();

    const strongReasonTests = [
      /(kalender.*konflikt|trainingskonflikt|zeitkonflikt|collision|conflict)/i,
      /(alltagsdruck hoch|stress hoch|daily stress high|overloaded day)/i,
      /(abendlast hoch|sp[aä]ter termin|late.*training|late.*slot|abend.*voll)/i,
      /(recovery.*konflikt|recovery.*negativ|hrv.*auff[aä]llig|ruhepuls.*auff[aä]llig|rhr.*high|hrv.*down)/i,
      /(mehrere.*leistung.*schlecht|mehrere.*rpe.*hoch|mehrere.*verfehlt|mehrere.*geskippt|mehrere.*abgebrochen)/i,
    ];
    const mediumReasonTests = [
      /(kalenderdichte hoch|calendar density|dichte hoch)/i,
      /(schulstress|schule.*stress|exam|pr[uü]fung)/i,
      /(gewichtstrend f[aä]llt|weight trend|trend fallend)/i,
      /(schlaf.*schlechter|sleep.*low|sleep.*worse|sleep debt)/i,
      /(limitierend|fatigue|müdigkeit|motivation niedrig|readiness niedrig|signal)/i,
    ];
    const matchedStrongReasons = strongReasonTests.filter((pattern) => pattern.test(joined)).length;
    const matchedMediumReasons = mediumReasonTests.filter((pattern) => pattern.test(joined)).length;
    const strongReductionIsJustified = matchedStrongReasons >= 1 || matchedMediumReasons >= 2;

    let status = "freigegeben";
    let summary = "Belastung nah an der Referenz, Ausführung sauber halten.";
    let anomaly = false;
    if (downRatio >= 0.8) {
      if (strongReductionIsJustified) {
        status = "stark angepasst";
        summary = "Heute niedriger geladen, weil die Tagesbelastung die Einheit begrenzt.";
      } else {
        status = "Plan auffällig";
        summary = "Fast alle Gewichte liegen unter der Referenz. Nicht blind übernehmen.";
        anomaly = true;
      }
    } else if (downRatio > 0.5) {
      if (strongReductionIsJustified) {
        status = "angepasst freigegeben";
        summary = "Training ist möglich, aber die Gewichte sind bewusst konservativer gewählt.";
      } else {
        status = "Plan auffällig";
        summary = "Viele Gewichte liegen unter der Referenz. Nicht blind übernehmen.";
        anomaly = true;
      }
    } else if (weightDownCount > 0 || setChanges > 0) {
      status = "leicht reduziert";
      summary = "Belastung leicht reduziert, Ausführung sauber halten.";
    } else if (upRatio >= 0.5) {
      status = "progressiv";
      summary = "Mehrere Übungen sind höher angesetzt, aber sauber ausführen.";
    } else if (weightUpCount > 0) {
      status = "freigegeben";
      summary = "Einzelne Gewichte sind erhöht, insgesamt bleibt der Plan gut tragbar.";
    } else if (badgeList.every((b) => String(b?.label || "") === "bleibt")) {
      status = "nah an Referenz";
      summary = "Der Plan bleibt nah an der letzten Referenz.";
    }

    const detailLines = [];
    const pushDetail = (line) => {
      const text = String(line || "").trim();
      if (!text || detailLines.includes(text) || detailLines.length >= 3) return;
      detailLines.push(text);
    };

    const reasonLabels = [];
    if (/(kalender.*konflikt|trainingskonflikt|zeitkonflikt|collision|conflict)/i.test(joined)) reasonLabels.push("Kalenderkonflikt");
    if (/(alltagsdruck hoch|stress hoch|daily stress high|overloaded day|schulstress)/i.test(joined)) reasonLabels.push("Alltagsdruck hoch");
    if (/(abendlast hoch|sp[aä]ter termin|late.*training|late.*slot|abend.*voll)/i.test(joined)) reasonLabels.push("Abendlast hoch");
    if (/(recovery.*konflikt|recovery.*negativ|hrv.*auff[aä]llig|ruhepuls.*auff[aä]llig|rhr.*high|hrv.*down)/i.test(joined)) reasonLabels.push("Recovery eingeschränkt");
    if (/(schlaf.*schlechter|sleep.*low|sleep.*worse|sleep debt)/i.test(joined)) reasonLabels.push("Schlaf schwächer");
    if (/(gewichtstrend f[aä]llt|weight trend|trend fallend)/i.test(joined)) reasonLabels.push("Gewichtstrend fallend");

    if (/(ruhepuls|hrv|recovery|erholung|sleep|schlaf)/i.test(joined)) {
      pushDetail("Erholung: stabil genug.");
    }
    if (anomaly) {
      pushDetail("Auffällig: viele Gewichte unter Referenz.");
      pushDetail("Es fehlt eine klare Begründung aus den heutigen Signalen.");
      pushDetail("Plan nicht blind übernehmen.");
    } else {
      if (reasonLabels.length) {
        pushDetail(`Grundlage: ${reasonLabels.slice(0, 3).join(", ")}.`);
      }
      if (downRatio > 0.5) {
        pushDetail("Umsetzung: Gewichte reduziert, Einheit bleibt freigegeben.");
      } else if (weightDownCount > 0 || setChanges > 0) {
        pushDetail("Plan: konservativer geladen.");
      } else if (weightUpCount > 0) {
        pushDetail("Plan: Steigerung ist möglich.");
      } else {
        pushDetail("Plan: nah an der Referenz.");
      }
      pushDetail(status === "progressiv"
        ? "Fokus: sauber steigern, nicht überziehen."
        : "Fokus: sauber trainieren, keine Extras erzwingen.");
    }

    return {
      title: sessionName || "Training",
      status,
      summary,
      details: detailLines,
      anomaly,
    };
  }

  function roundToStep(value, step) {
    const v = Number(value);
    const s = Number(step);
    if (!Number.isFinite(v) || !Number.isFinite(s) || s <= 0) return null;
    return Math.round(v / s) * s;
  }

  function formatDeNumber(x, digits) {
    const v = Number(x);
    if (!Number.isFinite(v)) return "";
    const s = v.toLocaleString("de-DE", { maximumFractionDigits: digits, minimumFractionDigits: 0 });
    return s.replace(/,0$/, "");
  }

  function formatRpeInt(x) {
    const v = Number(x);
    if (!Number.isFinite(v)) return "";
    return String(Math.round(v));
  }

  function formatDeltaPiece(delta, unit) {
    const v = Number(delta);
    if (!Number.isFinite(v) || Math.abs(v) < 0.0001) return "";
    const sign = v > 0 ? "+" : "-";
    if (unit === "rep" || unit === "set") return `${sign}${Math.round(Math.abs(v))} ${unit}`.trim();
    return `${sign}${formatDeNumber(Math.abs(v), 1)} ${unit}`.trim();
  }

  function formatRepsInline(reps) {
    const rs = (reps || []).map((x) => parseInt(x, 10)).filter((n) => Number.isFinite(n));
    return rs.length ? rs.join("/") : "";
  }

  function formatWeightsInline(weights, step) {
    void step;
    const wsRaw = (weights || []).map((x) => pcNum(x)).filter((n) => n != null);
    if (!wsRaw.length) return "";

    // expand: wenn nur 1 weight, aber mehrere sets impliziert
    let ws = wsRaw.slice();
    const allSame = ws.every((w) => Math.abs(w - ws[0]) < 0.0001);
    if (allSame) {
      return formatDeNumber(ws[0], 1);
    }

    ws = ws
      .filter((w) => w != null)
      .map((w) => formatDeNumber(w, 1));
    return ws.join("/");
  }

  function pcRpeFromDone(done) {
    const rpes = Array.isArray(done?.rpes) ? done.rpes.map((x) => pcNum(x)).filter((v) => v != null) : [];
    if (rpes.length) return rpes[rpes.length - 1];
    return pcNum(done?.avg_rpe);
  }

  function pcRpeTarget(planned, layer) {
    const display = planned?.display_rpe;
    if (typeof display === "string" && display.trim().length) return display.trim();
    const mn = pcNum(planned?.rpe_min);
    const mx = pcNum(planned?.rpe_max);
    const cap = pcNum(layer?.rpe_cap);
    if (mn != null || mx != null) {
      let lo = mn;
      let hi = mx;
      if (cap != null) {
        if (lo != null) lo = Math.min(lo, cap);
        if (hi != null) hi = Math.min(hi, cap);
      }
      if (lo != null && hi != null) {
        if (Math.abs(lo - hi) < 0.001) return formatRpeInt(lo);
        return `${formatRpeInt(lo)}-${formatRpeInt(hi)}`;
      }
      if (hi != null) return formatRpeInt(hi);
      if (lo != null) return formatRpeInt(lo);
    }
    if (cap != null) return formatRpeInt(cap);
    return null;
  }

  function normalizeSuggestionArrays({ reps, weights }) {
    let r = Array.isArray(reps) ? reps.slice() : [];
    let w = Array.isArray(weights) ? weights.slice() : [];
    const count = Math.max(r.length, w.length);
    if (!count) return { reps: [], weights: [] };

    if (r.length < count) {
      const fill = r.length ? r[r.length - 1] : 0;
      while (r.length < count) r.push(fill);
    }
    if (w.length === 1 && count > 1) {
      w = Array.from({ length: count }, () => w[0]);
    } else if (w.length < count) {
      const fill = w.length ? w[w.length - 1] : null;
      while (w.length < count) w.push(fill);
    }
    return { reps: r, weights: w };
  }

  function ensureSuggestionSetCount(arr, targetCount) {
    const target = parseInt(targetCount, 10);
    if (!Number.isFinite(target) || target <= 0) return arr;
    const out = normalizeSuggestionArrays(arr || { reps: [], weights: [] });
    if (!out.reps.length && !out.weights.length) return out;
    while (out.reps.length < target) {
      out.reps.push(out.reps.length ? out.reps[out.reps.length - 1] : 0);
    }
    while (out.weights.length < target) {
      out.weights.push(out.weights.length ? out.weights[out.weights.length - 1] : null);
    }
    if (out.reps.length > target) out.reps = out.reps.slice(0, target);
    if (out.weights.length > target) out.weights = out.weights.slice(0, target);
    return out;
  }

  function applyWeekLayerToSuggestion(arr, layer) {
    const vol = pcNum(layer?.volume_mult);
    if (vol == null) return arr;

    const baseCount = Math.max(arr.reps.length, arr.weights.length);
    if (!baseCount) return arr;

    const target = Math.max(1, Math.round(baseCount * vol));
    if (target === baseCount) return arr;

    const out = { reps: arr.reps.slice(), weights: arr.weights.slice() };
    if (target < baseCount) {
      out.reps = out.reps.slice(0, target);
      out.weights = out.weights.slice(0, target);
      return out;
    }

    // Extend (vol > 1): repeat last entries (keine neue Logik erfinden)
    const repFill = out.reps.length ? out.reps[out.reps.length - 1] : 0;
    const wFill = out.weights.length ? out.weights[out.weights.length - 1] : null;
    while (out.reps.length < target) out.reps.push(repFill);
    while (out.weights.length < target) out.weights.push(wFill);
    return out;
  }

  function clampRepsProgression({ lastReps, suggestedReps, gateOk }) {
    const lr = Array.isArray(lastReps) ? lastReps.map((x) => parseInt(x, 10)).filter((n) => Number.isFinite(n)) : [];
    const sr = Array.isArray(suggestedReps) ? suggestedReps.map((x) => parseInt(x, 10)).filter((n) => Number.isFinite(n)) : [];
    if (!sr.length) return sr;

    const out = sr.slice();
    const pair = Math.min(lr.length, out.length);

    for (let i = 0; i < pair; i += 1) {
      const base = lr[i];
      if (!Number.isFinite(base)) continue;
      if (!gateOk) {
        // no rep increases unless last RPE was inside target window for this week
        out[i] = Math.min(out[i], base);
      } else {
        // keep progression conservative: max +1 per set
        out[i] = Math.min(out[i], base + 1);
      }
    }

    // For added sets: never invent higher reps than last
    if (lr.length && out.length > lr.length) {
      const fill = lr[lr.length - 1];
      for (let i = lr.length; i < out.length; i += 1) {
        out[i] = Math.min(out[i], fill);
      }
    }

    return out;
  }

  function isRpeInTargetWindow(lastAvgRpe, planned, layer, weekType) {
    // deload week: never progress reps
    if ((weekType || "").toLowerCase() === "deload") return false;
    const rpe = pcNum(lastAvgRpe);
    if (rpe == null) return false;

    const pMin = pcNum(planned?.rpe_min);
    const pMax = pcNum(planned?.rpe_max);
    const cap = pcNum(layer?.rpe_cap);
    const maxEff = (pMax != null && cap != null) ? Math.min(pMax, cap) : (cap != null ? cap : pMax);
    const minEff = pMin;

    if (minEff != null && maxEff != null) return rpe >= (minEff - 0.001) && rpe <= (maxEff + 0.001);
    if (maxEff != null) return rpe <= (maxEff + 0.001);
    return false;
  }

  function formatSuggestionLine({ reps, weights, rpe }, step) {
    const repsStr = formatRepsInline(reps);
    const wStr = formatWeightsInline(weights, step);
    const rpeStr = (typeof rpe === "string" && rpe.trim().length)
      ? rpe.trim()
      : (pcFinite(rpe) ? formatRpeInt(rpe) : "");
    const base = [repsStr, "x", wStr].filter(Boolean).join(" ");
    return rpeStr ? `${base} @ ${rpeStr}` : base;
  }

  function formatRpesInline(rpes, count) {
    const rsRaw = Array.isArray(rpes) ? rpes.map((x) => pcNum(x)).filter((v) => v != null) : [];
    if (!rsRaw.length) return "";
    let rs = rsRaw.slice();
    if (count && rs.length < count) {
      const fill = rs.length ? rs[rs.length - 1] : null;
      while (rs.length < count) rs.push(fill);
    }
    rs = rs.map((v) => (v == null ? null : Math.round(v))).filter((v) => v != null);
    if (!rs.length) return "";
    const allSame = rs.every((v) => v === rs[0]);
    if (allSame) return String(rs[0]);
    return rs.join("/");
  }

  function formatLastLine(done, step) {
    const repsRaw = Array.isArray(done?.reps) ? done.reps : [];
    const weightsRaw = Array.isArray(done?.weights) ? done.weights : [];
    const rpesRaw = Array.isArray(done?.rpes) ? done.rpes : [];
    const completedSets = parseInt(done?.completed_sets, 10);
    const setCount = Math.max(
      repsRaw.length,
      weightsRaw.length,
      rpesRaw.length,
      Number.isFinite(completedSets) ? completedSets : 0
    );

    const ref = done?.last_reference && typeof done.last_reference === "object" ? done.last_reference : null;
    if (!setCount) {
      if (ref) {
        const rr = Number.isFinite(parseInt(ref.reps, 10)) ? String(parseInt(ref.reps, 10)) : "—";
        const wwNum = pcNum(ref.weight);
        const ww = Number.isFinite(wwNum) ? `${pcFormatKg(wwNum)}kg` : "—";
        const rrpeNum = pcNum(ref.rpe);
        const rrpe = Number.isFinite(rrpeNum) ? formatRpeInt(rrpeNum) : "";
        const base = `${rr} x ${ww}`;
        return rrpe ? `${base} @ ${rrpe}` : base;
      }
      return "—";
    }

    const reps = Array.from({ length: setCount }, (_, i) => {
      const v = parseInt(repsRaw[i], 10);
      return Number.isFinite(v) ? String(v) : "—";
    });

    const weights = Array.from({ length: setCount }, (_, i) => {
      const v = pcNum(weightsRaw[i]);
      return Number.isFinite(v) ? pcFormatKg(v) : "—";
    });

    const rpes = Array.from({ length: setCount }, (_, i) => {
      const v = pcNum(rpesRaw[i]);
      return Number.isFinite(v) ? formatRpeInt(v) : "—";
    });

    const hasReps = reps.some((v) => v !== "—");
    const hasWeights = weights.some((v) => v !== "—");
    const hasRpes = rpes.some((v) => v !== "—");

    if (!hasReps && !hasWeights && !hasRpes) {
      if (ref) {
        const rr = Number.isFinite(parseInt(ref.reps, 10)) ? String(parseInt(ref.reps, 10)) : "—";
        const wwNum = pcNum(ref.weight);
        const ww = Number.isFinite(wwNum) ? `${pcFormatKg(wwNum)}kg` : "—";
        const rrpeNum = pcNum(ref.rpe);
        const rrpe = Number.isFinite(rrpeNum) ? formatRpeInt(rrpeNum) : "";
        const base = `${rr} x ${ww}`;
        return rrpe ? `${base} @ ${rrpe}` : base;
      }
      return "—";
    }

    // If current log is partial (e.g. skipped reps/weights), prefer last valid reference for readability.
    if ((!hasReps || !hasWeights) && ref) {
      const rr = Number.isFinite(parseInt(ref.reps, 10)) ? String(parseInt(ref.reps, 10)) : "—";
      const wwNum = pcNum(ref.weight);
      const ww = Number.isFinite(wwNum) ? `${pcFormatKg(wwNum)}kg` : "—";
      const rrpeNum = pcNum(ref.rpe);
      const rrpe = Number.isFinite(rrpeNum) ? formatRpeInt(rrpeNum) : "";
      const base = `${rr} x ${ww}`;
      return rrpe ? `${base} @ ${rrpe}` : base;
    }

    const repsText = hasReps ? reps.join("/") : "—";
    const weightsText = hasWeights ? `${weights.join("/")}kg` : "—";
    const rpeText = hasRpes ? rpes.join("/") : "";
    const incomplete = reps.includes("—") || weights.includes("—") || (hasRpes && rpes.includes("—"));
    const base = `${repsText} x ${weightsText}`;
    const withRpe = rpeText ? `${base} @ ${rpeText}` : base;
    return incomplete ? `${withRpe} (unvollständig)` : withRpe;
  }

  function applyRepTargetAndWeightAdjust({ done, planned, layer, weekType, reps, weights, step }) {
    const repsMin = planned?.reps_min != null ? parseInt(planned.reps_min, 10) : null;
    const repsMax = planned?.reps_max != null ? parseInt(planned.reps_max, 10) : null;
    const lastReps = Array.isArray(done?.reps) ? done.reps.map((x) => parseInt(x, 10)).filter((n) => Number.isFinite(n)) : [];

    const out = normalizeSuggestionArrays({ reps, weights });

    // If last avg RPE was above max allowed, bias weight down a step (no rep progression).
    if ((weekType || "").toLowerCase() !== "deload") {
      const lastAvg = pcNum(done?.avg_rpe);
      const pMax = pcNum(planned?.rpe_max);
      const cap = pcNum(layer?.rpe_cap);
      const maxEff = (pMax != null && cap != null) ? Math.min(pMax, cap) : (cap != null ? cap : pMax);
      if (lastAvg != null && maxEff != null && lastAvg > (maxEff + 0.001)) {
        out.weights = out.weights.map((w) => {
          const ww = pcNum(w);
          if (ww == null) return w;
          const next = roundToStep(ww - step, step);
          return next != null && next > 0 ? next : ww;
        });
      }
    }

    // Ensure suggested reps are within target; if last was below min -> lower weight.
    const pair = Math.min(lastReps.length, out.reps.length, out.weights.length);
    for (let i = 0; i < pair; i += 1) {
      const last = lastReps[i];
      if (!Number.isFinite(last)) continue;

      if (Number.isFinite(repsMin) && last < repsMin) {
        out.reps[i] = repsMin;
        const ww = pcNum(out.weights[i]);
        if (ww != null) {
          const next = roundToStep(ww - step, step);
          if (next != null && next > 0) out.weights[i] = next;
        }
      } else if (Number.isFinite(repsMax) && last > repsMax) {
        out.reps[i] = repsMax;
        const ww = pcNum(out.weights[i]);
        if (ww != null) {
          const next = roundToStep(ww + step, step);
          if (next != null && next > 0) out.weights[i] = next;
        }
      }
    }

    return out;
  }

  function pcBuildReferenceWeightVector(done, planned, targetCount, step) {
    const target = Math.max(0, parseInt(targetCount, 10) || 0);
    if (!target) return [];
    const doneWeights = Array.isArray(done?.weights)
      ? done.weights.map((x) => pcNum(x)).filter((n) => Number.isFinite(n)).map((v) => roundToStep(v, step))
      : [];
    if (doneWeights.length) {
      if (doneWeights.length === 1 && target > 1) return Array.from({ length: target }, () => doneWeights[0]);
      const out = doneWeights.slice(0, target);
      while (out.length < target) out.push(out[out.length - 1] ?? doneWeights[0]);
      return out;
    }

    const ref = done?.last_reference && typeof done.last_reference === "object" ? done.last_reference : null;
    const refWeight = pcNum(ref?.weight);
    if (!Number.isFinite(refWeight)) return [];
    const rounded = roundToStep(refWeight, step);
    return Array.from({ length: target }, () => rounded);
  }

  function pcGetExerciseWeightReason({ done, planned, layer, weekType, step, note }) {
    const reasons = [];
    const noteText = String(note || "").trim().toLowerCase();
    const plannedSets = parseInt(planned?.sets, 10);
    const completedSets = parseInt(done?.completed_sets, 10);
    const maxLoggedRpe = Math.max(
      pcNum(done?.avg_rpe) ?? -Infinity,
      ...(Array.isArray(done?.rpes) ? done.rpes.map((x) => pcNum(x)).filter((v) => Number.isFinite(v)) : [])
    );
    const pMax = pcNum(planned?.rpe_max);
    const cap = pcNum(layer?.rpe_cap);
    const maxEff = (pMax != null && cap != null) ? Math.min(pMax, cap) : (cap != null ? cap : pMax);

    if ((weekType || "").toLowerCase() === "deload") reasons.push("deload");
    if (Number.isFinite(maxLoggedRpe) && Number.isFinite(maxEff) && maxLoggedRpe > (maxEff + 0.5)) reasons.push("rpe_high");
    if (Number.isFinite(plannedSets) && Number.isFinite(completedSets) && completedSets < plannedSets) reasons.push("sets_missed");
    if (/(schmerz|pain|warn|problem|injury|skip|skipped|missed|abbruch|abgebrochen|verfehlt|failed)/i.test(noteText)) reasons.push("local_note");

    return {
      hasLocalReason: reasons.length > 0,
      reasons,
    };
  }

  function pcStabilizeWeightsNearReference({ done, planned, suggested, step, localReason }) {
    if (localReason?.hasLocalReason) return suggested;
    const out = normalizeSuggestionArrays(suggested || { reps: [], weights: [] });
    const refs = pcBuildReferenceWeightVector(done, planned, out.weights.length, step);
    if (!refs.length) return out;
    for (let i = 0; i < out.weights.length; i += 1) {
      const sug = pcNum(out.weights[i]);
      const ref = pcNum(refs[i]);
      if (!Number.isFinite(sug) || !Number.isFinite(ref)) continue;
      const floor = roundToStep(Math.max(step, ref - step), step);
      if (Number.isFinite(floor) && sug < floor) out.weights[i] = floor;
    }
    return out;
  }

  function pcExerciseWeightPolicy(name) {
    const n = String(name || "").toLowerCase();

    // Very small moves: cap weight drops hard (2 steps = 5kg at 2.5 step)
    if (/(curl|curls|bayes|bayesian|seitheb|lateral|raise|triceps|pushdown|extension|french|skull|fly|flies)/i.test(n)) {
      return { maxDownSteps: 2 };
    }

    // Medium compounds/accessories
    if (/(upright|row|latzug|pulldown|overhead|ohp|shoulder press|press)/i.test(n)) {
      return { maxDownSteps: 1 };
    }

    // Big lower-body / hinge / squat patterns
    if (/(rdl|romanian|deadlift|squat|beinpresse|leg press|hip thrust|good morning)/i.test(n)) {
      return { maxDownSteps: 4 };
    }

    // Default: conservative
    return { maxDownSteps: 1 };
  }

  function pcEnsureTopSetFirst(weights, step) {
    const ws = Array.isArray(weights) ? weights.slice() : [];
    const vals = ws.map((x) => pcNum(x)).map((v) => (v == null ? null : roundToStep(v, step)));

    // Find max weight index
    let maxIdx = -1;
    let maxVal = -Infinity;
    for (let i = 0; i < vals.length; i += 1) {
      const v = vals[i];
      if (!Number.isFinite(v)) continue;
      if (v > maxVal) {
        maxVal = v;
        maxIdx = i;
      }
    }
    if (maxIdx > 0) {
      const tmp = vals[0];
      vals[0] = vals[maxIdx];
      vals[maxIdx] = tmp;
    }

    // Ensure later sets are not heavier than earlier ones.
    for (let i = 1; i < vals.length; i += 1) {
      if (!Number.isFinite(vals[i]) || !Number.isFinite(vals[i - 1])) continue;
      if (vals[i] > vals[i - 1] + 0.0001) vals[i] = vals[i - 1];
    }

    return vals.map((v) => (v == null ? null : v));
  }

  function pcClampWeightDropsByExercise(name, doneWeights, sugWeights, step) {
    const { maxDownSteps } = pcExerciseWeightPolicy(name);
    const eps = 0.0001;

    function expand(raw, targetCount) {
      const ws = Array.isArray(raw) ? raw.map((x) => pcNum(x)).map((v) => (v == null ? null : roundToStep(v, step))) : [];
      const clean = ws.map((v) => (Number.isFinite(v) ? v : null));
      if (!targetCount || targetCount <= 0) return [];
      if (clean.length === 1 && targetCount > 1) return Array.from({ length: targetCount }, () => clean[0]);
      return clean.slice(0, targetCount);
    }

    const lastCount = Array.isArray(doneWeights) ? doneWeights.length : 0;
    const sugCount = Array.isArray(sugWeights) ? sugWeights.length : 0;
    const pair = Math.min(lastCount || 0, sugCount || 0);
    if (!pair) return sugWeights;

    const lw = expand(doneWeights, pair);
    const sw = expand(sugWeights, pair);

    const maxDownKg = Math.max(1, parseInt(maxDownSteps, 10) || 1) * step;
    const out = sugWeights.slice();
    for (let i = 0; i < pair; i += 1) {
      const last = lw[i];
      const sug = sw[i];
      if (!Number.isFinite(last) || !Number.isFinite(sug)) continue;
      if (sug < last - eps) {
        const floor = roundToStep(Math.max(step, last - maxDownKg), step);
        if (floor != null && sug < floor - eps) out[i] = floor;
      }
    }
    return out;
  }

  function computeDelta(done, today, rpeTarget, step) {
    void rpeTarget;
    void step;

    const eps = 0.0001;

    function fmtKg(delta) {
      const v = Number(delta);
      if (!Number.isFinite(v) || Math.abs(v) < eps) return null;
      const sign = v > 0 ? "+" : "-";
      return `${sign}${formatDeNumber(Math.abs(v), 1)}kg`;
    }

    function expandWeights(raw, targetCount) {
      const ws = Array.isArray(raw) ? raw.map((x) => pcNum(x)) : [];
      const clean = ws.map((v) => (Number.isFinite(v) ? v : null));
      if (!targetCount || targetCount <= 0) return [];
      if (clean.length === 1 && targetCount > 1) return Array.from({ length: targetCount }, () => clean[0]);
      return clean.slice(0, targetCount);
    }

    const lastReps = Array.isArray(done?.reps) ? done.reps.map((x) => parseInt(x, 10)).filter((n) => Number.isFinite(n)) : [];
    const todayReps = Array.isArray(today?.reps) ? today.reps.map((x) => parseInt(x, 10)).filter((n) => Number.isFinite(n)) : [];

    const lastWeightsRaw = Array.isArray(done?.weights) ? done.weights : [];
    const todayWeightsRaw = Array.isArray(today?.weights) ? today.weights : [];

    const lastCount = Math.max(lastReps.length, Array.isArray(lastWeightsRaw) ? lastWeightsRaw.length : 0);
    const todayCount = Math.max(todayReps.length, Array.isArray(todayWeightsRaw) ? todayWeightsRaw.length : 0);
    const pairCount = Math.min(lastCount, todayCount);

    const parts = [];

    const setDelta = todayCount - lastCount;
    if (setDelta !== 0) parts.push(`${setDelta > 0 ? "+" : ""}${setDelta} set`);

    // Weight deltas (paired sets only). If any weight delta exists, we do NOT show rep deltas.
    const lw = expandWeights(lastWeightsRaw, pairCount);
    const tw = expandWeights(todayWeightsRaw, pairCount);
    const wDeltas = [];
    for (let i = 0; i < pairCount; i += 1) {
      const a = lw[i];
      const b = tw[i];
      const d = (Number.isFinite(a) && Number.isFinite(b)) ? (b - a) : 0;
      wDeltas.push(d);
    }
    const hasWeightDelta = wDeltas.some((d) => Math.abs(d) > eps);

    if (hasWeightDelta) {
      const nonZero = wDeltas.filter((d) => Math.abs(d) > eps);
      const allSame = nonZero.length && nonZero.every((d) => Math.abs(d - nonZero[0]) < eps);
      if (allSame) {
        const piece = fmtKg(nonZero[0]);
        if (piece) parts.push(piece);
      } else {
        const pieces = wDeltas
          .filter((d) => Math.abs(d) > eps)
          .map((d) => fmtKg(d))
          .filter(Boolean);
        if (pieces.length) parts.push(pieces.join(" / "));
      }
    } else {
      // Reps deltas per set (paired sets only)
      const repPairs = Math.min(lastReps.length, todayReps.length, pairCount);
      const repDeltas = [];
      for (let i = 0; i < repPairs; i += 1) repDeltas.push(todayReps[i] - lastReps[i]);
      if (repDeltas.some((d) => Number.isFinite(d) && d !== 0)) {
        const fmt = repDeltas.map((d) => (d >= 0 ? `+${d}` : String(d))).join("/");
        parts.push(`${fmt} rep`);
      }
    }

    if (!parts.length) return "";
    return `(${parts.join("; ")})`;
  }

  function computePlanCheckPill(done, today, step) {
    const eps = 0.0001;
    void step;

    function expandWeights(raw, targetCount) {
      const ws = Array.isArray(raw) ? raw.map((x) => pcNum(x)) : [];
      const clean = ws.map((v) => (Number.isFinite(v) ? v : null));
      if (!targetCount || targetCount <= 0) return [];
      if (clean.length === 1 && targetCount > 1) return Array.from({ length: targetCount }, () => clean[0]);
      return clean.slice(0, targetCount);
    }

    const lastReps = Array.isArray(done?.reps) ? done.reps.map((x) => parseInt(x, 10)).filter((n) => Number.isFinite(n)) : [];
    const todayReps = Array.isArray(today?.reps) ? today.reps.map((x) => parseInt(x, 10)).filter((n) => Number.isFinite(n)) : [];
    const lastWeightsRaw = Array.isArray(done?.weights) ? done.weights : [];
    const todayWeightsRaw = Array.isArray(today?.weights) ? today.weights : [];

    const lastCount = Math.max(lastReps.length, Array.isArray(lastWeightsRaw) ? lastWeightsRaw.length : 0);
    const todayCount = Math.max(todayReps.length, Array.isArray(todayWeightsRaw) ? todayWeightsRaw.length : 0);
    const pairCount = Math.min(lastCount, todayCount);

    const lw = expandWeights(lastWeightsRaw, pairCount);
    const tw = expandWeights(todayWeightsRaw, pairCount);

    let weightDown = false;
    let weightUp = false;
    for (let i = 0; i < pairCount; i += 1) {
      const a = lw[i];
      const b = tw[i];
      if (!Number.isFinite(a) || !Number.isFinite(b)) continue;
      if (b < a - eps) weightDown = true;
      if (b > a + eps) weightUp = true;
    }

    let repsDown = false;
    let repsUp = false;
    const repPairs = Math.min(lastReps.length, todayReps.length, pairCount);
    for (let i = 0; i < repPairs; i += 1) {
      const a = lastReps[i];
      const b = todayReps[i];
      if (!Number.isFinite(a) || !Number.isFinite(b)) continue;
      if (b < a) repsDown = true;
      if (b > a) repsUp = true;
    }

    if (weightDown) return { text: "Gewicht ↓", color: "red" };
    if (weightUp) return { text: "Gewicht ↑", color: "green" };
    if (repsDown) return { text: "Reps ↓", color: "red" };
    if (repsUp) return { text: "Reps ↑", color: "green" };
    if (todayCount !== lastCount) {
      const up = todayCount > lastCount;
      return { text: `Sätze ${up ? "↑" : "↓"}`, color: "neutral" };
    }
    return { text: "", color: "" };
  }

  function pillFromPrimaryChange(change) {
    const kind = String(change?.kind || "").toLowerCase();
    const dir = String(change?.dir || "").toLowerCase();
    if (kind === "weight") {
      if (dir === "down") return { text: "Gewicht ↓", color: "red" };
      if (dir === "up") return { text: "Gewicht ↑", color: "green" };
    }
    if (kind === "reps") {
      if (dir === "down") return { text: "Reps ↓", color: "red" };
      if (dir === "up") return { text: "Reps ↑", color: "green" };
    }
    if (kind === "sets") {
      if (dir === "down") return { text: "Sätze ↓", color: "neutral" };
      if (dir === "up") return { text: "Sätze ↑", color: "neutral" };
    }
    return null;
  }

  function pcRenderWeekMeta(weekOverview) {
    const badge = pcEl("plan-check-week-badge");
    const hint = pcEl("plan-check-week-hint");
    if (!badge || !hint) return;

    const phase = (weekOverview?.week?.phase_label || "").trim();
    const wt = (weekOverview?.week?.week_type || weekOverview?.layer?.week_type || "").toLowerCase();
    badge.textContent = phase || (wt ? wt.toUpperCase() : "");

    const wc = weekOverview?.week?.week_current;
    const tw = weekOverview?.week?.total_weeks;
    const weekTxt = (wc != null) ? `Woche ${wc}/${tw != null ? tw : "—"}` : "";
    hint.textContent = weekTxt || (weekOverview?.today?.today_is_rest ? "Restday" : "");
  }

  function renderCoreSteuertBadge(forcedEnabled = null) {
    const apBadge = pcEl("autopilot-badge");
    if (!apBadge) return;
    const isOpen = state.coreExplainActiveKey === "training";
    apBadge.hidden = true;
    apBadge.textContent = state.trainingCardHeadlineStatus || "CORE steuert";
    apBadge.classList.toggle("is-active", isOpen);
    apBadge.classList.toggle("is-disabled", !state.autopilotExplainAvailable);
    apBadge.setAttribute("aria-expanded", isOpen ? "true" : "false");
    if (state.coreExplainActiveKey === "training") {
      closeCoreExplainPanel({ restoreFocus: false });
    }
  }

  function _fallbackTrainingExplainPayload() {
    const sessionTitle = String(pcEl("plan-check-card-title")?.textContent || "").trim();
    const sessionInfo = String(pcEl("plan-check-message")?.textContent || "").trim();
    return {
      title: "CORE Training",
      status: "Normal",
      meta: "Live-Briefing",
      verdict: sessionTitle ? `CORE führt heute: ${sessionTitle}` : "CORE hält heute die Trainingslinie stabil.",
      subtext: "Die Entscheidung bleibt auf Wochenstabilität ausgerichtet und vermeidet unnötige Eskalation.",
      drivers: [
        { title: "Systemlage", text: "Belastung wird kontrolliert gehalten, um die Woche nicht zu kippen." },
        { title: "Qualitätsschutz", text: "Härte wird nur gesetzt, wenn das Timing sauber passt." },
        { title: "Führungskurs", text: "Heute gilt Ausführung mit Struktur statt spontane Übersteuerung." },
      ],
      allowed: [
        "Session sauber wie geplant ausführen",
        "Ruhige, kontrollierte Belastung",
        "Rhythmus vor Pace-Jagd",
      ],
      avoided: [
        "Zusätzliche Härte ohne klares Signal",
        "Ego-Pacing gegen den Wochenplan",
        "Spontane Eskalation außerhalb des Rahmens",
      ],
      rationale: [
        "Heute zählt planbare Qualität mehr als eine aggressive Leistungsspitze.",
        sessionInfo || "CORE hält die Session im tragbaren Bereich, damit die Woche stabil bleibt.",
      ],
      signals: [
        "Signal-Layer: aktuell ohne vollständigen Explain-Datensatz",
        "Modus: konservative Steuerung aktiv",
      ],
      practical: [
        "Session durchführen, Intensität kontrollieren",
        "Saubere Technik und Tempo-Disziplin halten",
        "Danach normal in den Tagesplan zurückkehren",
      ],
      footer: "Fallback-Briefing aktiv. Bei neuen Daten wird das Urteil automatisch ersetzt.",
    };
  }

  function _coreExplainStatusKey(input) {
    const raw = String(input || "").trim().toLowerCase();
    if (!raw) return "neutral";
    if (raw.includes("manuell") || raw.includes("eingriff") || raw.includes("override")) return "eingriff";
    if (raw.includes("schutz") || raw.includes("vorsicht")) return "vorsicht";
    if (raw.includes("aktiv") || raw.includes("freigabe") || raw.includes("steuert")) return "freigabe";
    if (raw.includes("neu") || raw.includes("recompute") || raw.includes("berechnet")) return "recalc";
    return "bestätigt";
  }

  function _polishCoreExplainText(line) {
    const raw = String(line || "").trim();
    if (!raw) return "";
    return raw
      .replace(/^Status:\s*/i, "")
      .replace(/berücksichtigt/gi, "fließt ein")
      .replace(/Verwendete Daten/gi, "Eingangslage")
      .replace(/CORE adjusted/gi, "CORE neu bewertet")
      .replace(/\s{2,}/g, " ")
      .trim();
  }

  function _coreExplainLines(lines, fallback) {
    const list = Array.isArray(lines) ? lines.map(_polishCoreExplainText).filter(Boolean) : [];
    if (list.length) return list;
    return [fallback];
  }

  function _compactUnique(lines, limit = 4) {
    const out = [];
    const seen = new Set();
    for (const raw of Array.isArray(lines) ? lines : []) {
      const line = String(raw || "").trim();
      if (!line) continue;
      const key = line.toLowerCase();
      if (seen.has(key)) continue;
      seen.add(key);
      out.push(line);
      if (out.length >= limit) break;
    }
    return out;
  }

  function _renderCoreExplainList(lines, host, className = "core-explain-panel-list-item") {
    if (!host) return;
    host.innerHTML = "";
    for (const line of _compactUnique(lines, 8)) {
      const row = document.createElement("div");
      row.className = className;
      row.textContent = String(line || "");
      host.appendChild(row);
    }
  }

  function _renderCoreExplainDrivers(drivers, host) {
    if (!host) return;
    host.innerHTML = "";
    const rows = Array.isArray(drivers) ? drivers.slice(0, 3) : [];
    for (const item of rows) {
      const card = document.createElement("article");
      card.className = "core-explain-driver-card";
      const title = document.createElement("div");
      title.className = "core-explain-driver-title";
      title.textContent = String(item?.title || "Treiber");
      const text = document.createElement("p");
      text.className = "core-explain-driver-text";
      text.textContent = String(item?.text || "");
      card.appendChild(title);
      card.appendChild(text);
      const badgeText = String(item?.badge || "").trim();
      if (badgeText) {
        const badge = document.createElement("span");
        badge.className = "core-explain-driver-badge";
        badge.textContent = badgeText;
        card.appendChild(badge);
      }
      host.appendChild(card);
    }
  }

  function _renderCoreExplainSignalPills(lines, host) {
    if (!host) return;
    host.innerHTML = "";
    const rows = _compactUnique(lines, 10);
    for (const line of rows) {
      const chip = document.createElement("span");
      chip.className = "core-explain-signal-chip";
      chip.textContent = line;
      host.appendChild(chip);
    }
  }

  function _normalizeDrivers(lines, fallbackPrefix) {
    const rows = _compactUnique(lines, 3);
    return rows.map((line, idx) => {
      const clean = _polishCoreExplainText(line);
      const parts = clean.split(":");
      if (parts.length > 1) {
        return {
          title: parts.shift()?.trim() || `${fallbackPrefix} ${idx + 1}`,
          text: parts.join(":").trim() || clean,
          badge: "",
        };
      }
      return {
        title: `${fallbackPrefix} ${idx + 1}`,
        text: clean,
        badge: "",
      };
    });
  }

  function _coreExplainParseHm(value) {
    const raw = String(value || "").trim();
    const m = raw.match(/(\d{1,2}):(\d{2})/);
    if (!m) return null;
    const h = Number(m[1]);
    const mm = Number(m[2]);
    if (!Number.isFinite(h) || !Number.isFinite(mm)) return null;
    if (h < 0 || h > 23 || mm < 0 || mm > 59) return null;
    return h * 60 + mm;
  }

  function _coreExplainFmtHm(mins) {
    if (!Number.isFinite(mins)) return "--:--";
    const v = ((Math.round(mins) % 1440) + 1440) % 1440;
    const h = Math.floor(v / 60);
    const m = v % 60;
    return `${String(h).padStart(2, "0")}:${String(m).padStart(2, "0")}`;
  }

  function _coreExplainNowHm() {
    try {
      const fmt = new Intl.DateTimeFormat("de-DE", {
        hour: "2-digit",
        minute: "2-digit",
        hour12: false,
        timeZone: TIME_ZONE,
      }).format(new Date());
      return _coreExplainParseHm(fmt);
    } catch (_) {
      const d = new Date();
      return d.getHours() * 60 + d.getMinutes();
    }
  }

  function _buildMealplanCoreStrategyStations(core) {
    const out = [];
    const nowMins = _coreExplainNowHm();

    const planned = Array.isArray(core?.planned_meals) ? core.planned_meals : [];
    const openMeals = planned
      .filter((m) => ["open", "planned", "shifted", "adjusted_by_core"].includes(String(m?.status || "open").toLowerCase()))
      .map((m) => ({
        label: String(m?.title || "Meal").trim() || "Meal",
        time: String(m?.time_text || "").trim(),
        mins: _coreExplainParseHm(m?.time_text),
      }))
      .filter((m) => Number.isFinite(m.mins))
      .sort((a, b) => a.mins - b.mins)
      .slice(0, 4);

    const training = core?.decision_state?.context_snapshot?.training || {};
    const trainingTime = String(training?.training_time || "").trim();
    const trainingMins = _coreExplainParseHm(trainingTime);
    const events = [];
    openMeals.forEach((m, idx) => {
      events.push({
        label: `Meal ${idx + 1}`,
        time: m.time,
        mins: m.mins,
        emphasis: "meal",
      });
    });
    if (training?.training_today && Number.isFinite(trainingMins)) {
      events.push({
        label: "Training",
        time: trainingTime,
        mins: trainingMins,
        emphasis: "training",
      });
    }

    const normalized = events
      .filter((e) => Number.isFinite(e.mins))
      .map((e) => {
        const minsAbs = e.mins < nowMins ? e.mins + 1440 : e.mins;
        return { ...e, minsAbs };
      })
      .sort((a, b) => a.minsAbs - b.minsAbs);

    while (normalized.length < 4) {
      const base = normalized.length ? normalized[normalized.length - 1].minsAbs : nowMins;
      const next = base + 150;
      normalized.push({
        label: `Meal ${normalized.length + 1}`,
        time: _coreExplainFmtHm(next),
        mins: next % 1440,
        minsAbs: next,
        emphasis: "meal",
      });
    }

    const trimmed = normalized.slice(0, 5);
    const trainingIdx = trimmed.findIndex((e) => e.emphasis === "training");
    const staged = trimmed.map((e, idx) => {
      let stage = "hold";
      if (trainingIdx >= 0) {
        if (idx < trainingIdx) stage = "hold";
        else if (idx === trainingIdx) stage = "load";
        else stage = "refill";
      } else if (idx >= 2) {
        stage = "refill";
      }
      return { ...e, stage };
    });

    out.push({
      label: "Jetzt",
      time: _coreExplainFmtHm(nowMins),
      mins: nowMins,
      minsAbs: nowMins,
      emphasis: "now",
      stage: "status",
    });
    out.push(...staged);
    return out.slice(0, 6);
  }

  function _buildMealplanCoreExplainPanel(payload, layout) {
    const node = document.createElement("section");
    node.className = "core-explain-panel is-mealplan-nutrition";
    node.id = "core-explain-panel";
    node.setAttribute("role", "dialog");
    node.setAttribute("aria-modal", (layout === "sheet" || layout === "modal") ? "true" : "false");
    node.setAttribute("aria-labelledby", "core-explain-panel-title");

    const top = document.createElement("div");
    top.className = "core-explain-mp-top";

    const head = document.createElement("header");
    head.className = "core-explain-mp-head";
    const overlineRow = document.createElement("div");
    overlineRow.className = "core-explain-mp-overline-row";
    const overline = document.createElement("div");
    overline.className = "core-explain-mp-overline";
    overline.textContent = "CORE ENTSCHEIDUNG";
    const status = document.createElement("div");
    status.className = "core-explain-mp-status";
    status.textContent = String(payload?.status || "NORMAL");
    status.dataset.status = _coreExplainStatusKey(payload?.status);
    overlineRow.append(overline, status);

    const title = document.createElement("h3");
    title.className = "core-explain-mp-title";
    title.id = "core-explain-panel-title";
    title.textContent = String(payload?.headline || payload?.verdict || "Heute hält CORE die Ernährung bewusst ruhig.");

    const sub = document.createElement("p");
    sub.className = "core-explain-mp-sub";
    sub.textContent = String(payload?.subtext || "Stabilität vor kurzfristiger Eskalation.");
    head.append(overlineRow, title, sub);

    const target = document.createElement("aside");
    target.className = "core-explain-mp-target";
    const closeBtn = document.createElement("button");
    closeBtn.type = "button";
    closeBtn.className = "core-explain-panel-close core-explain-mp-close";
    closeBtn.id = "core-explain-panel-close";
    closeBtn.setAttribute("aria-label", "CORE-Erklärung schließen");
    closeBtn.textContent = "×";
    target.appendChild(closeBtn);

    const ringWrap = document.createElement("div");
    ringWrap.className = "core-explain-mp-target-ring";
    const ring = document.createElement("div");
    ring.className = "core-explain-mp-ring";
    ring.style.setProperty("--ring-progress", `${Math.max(6, Math.min(97, Number(payload?.targetProgressPct || 0)))}%`);
    const ringValue = document.createElement("div");
    ringValue.className = "core-explain-mp-ring-value";
    ringValue.innerHTML = `<strong>${Math.round(Number(payload?.targetKcal || 0))}</strong><span>kcal Ziel</span>`;
    ring.appendChild(ringValue);
    ringWrap.appendChild(ring);
    target.appendChild(ringWrap);

    const targetStats = document.createElement("div");
    targetStats.className = "core-explain-mp-target-stats";
    const statRows = [
      { tone: "kcal", txt: `Offen: ${Math.round(Number(payload?.remainingKcal || 0))} kcal` },
      { tone: "protein", txt: `Protein: ${Math.round(Number(payload?.targetProtein || 0))} g · offen ${Math.round(Number(payload?.remainingProtein || 0))} g` },
      { tone: "fat", txt: `Fett: ${Math.round(Number(payload?.targetFat || 0))} g · offen ${Math.round(Number(payload?.remainingFat || 0))} g` },
    ];
    statRows.forEach((row) => {
      const el = document.createElement("div");
      el.className = `core-explain-mp-target-stat is-${row.tone}`;
      el.textContent = row.txt;
      targetStats.appendChild(el);
    });
    target.appendChild(targetStats);
    top.append(head, target);
    node.appendChild(top);

    const reasons = document.createElement("section");
    reasons.className = "core-explain-mp-section";
    reasons.innerHTML = `<div class="core-explain-mp-section-title">3 Kerngründe</div>`;
    const reasonGrid = document.createElement("div");
    reasonGrid.className = "core-explain-mp-reasons";
    const drivers = Array.isArray(payload?.drivers) ? payload.drivers.slice(0, 3) : [];
    drivers.forEach((d, idx) => {
      const card = document.createElement("article");
      card.className = "core-explain-mp-reason";
      card.innerHTML = `
        <div class="core-explain-mp-reason-head">
          <span class="core-explain-mp-reason-dot is-${idx + 1}"></span>
          <span class="core-explain-mp-reason-title">${escapeHtml(String(d?.title || `Treiber ${idx + 1}`))}</span>
        </div>
        <p class="core-explain-mp-reason-text">${escapeHtml(String(d?.text || ""))}</p>
        ${d?.badge ? `<span class="core-explain-mp-reason-badge">${escapeHtml(String(d.badge))}</span>` : ""}
      `;
      reasonGrid.appendChild(card);
    });
    reasons.appendChild(reasonGrid);
    node.appendChild(reasons);

    const middle = document.createElement("section");
    middle.className = "core-explain-mp-middle";

    const guardCol = document.createElement("div");
    guardCol.className = "core-explain-mp-col";
    guardCol.innerHTML = `<div class="core-explain-mp-section-title">Leitplanken & Grenzen</div>`;
    const guardList = document.createElement("div");
    guardList.className = "core-explain-mp-guard-list";
    (Array.isArray(payload?.guardrails) ? payload.guardrails : []).slice(0, 5).forEach((g) => {
      const pct = Math.max(0, Math.min(100, Number(g?.progress || 0)));
      const row = document.createElement("div");
      row.className = "core-explain-mp-guard";
      row.innerHTML = `
        <div class="core-explain-mp-guard-top">
          <span class="core-explain-mp-guard-label">${escapeHtml(String(g?.label || ""))}</span>
          <span class="core-explain-mp-guard-value">${escapeHtml(String(g?.value || ""))}</span>
        </div>
        <div class="core-explain-mp-guard-bar"><span style="width:${pct}%"></span></div>
      `;
      guardList.appendChild(row);
    });
    const guardTail = document.createElement("div");
    guardTail.className = "core-explain-mp-guard-tail";
    guardTail.innerHTML = `<span class="core-explain-mp-chip">Timing-Fokus: ${escapeHtml(String(payload?.timingFocus || "später auffüllen"))}</span><span class="core-explain-mp-chip is-warm">Catch-up: ${escapeHtml(String(payload?.catchupRule || "aktiv"))}</span>`;
    guardCol.append(guardList, guardTail);

    const strategyCol = document.createElement("div");
    strategyCol.className = "core-explain-mp-col";
    strategyCol.innerHTML = `<div class="core-explain-mp-section-title">Tagesverlauf - empfohlene Strategie</div>`;
    const strategy = document.createElement("div");
    strategy.className = "core-explain-mp-strategy";
    const stationsRaw = Array.isArray(payload?.strategyStations) ? payload.strategyStations.slice(0, 8) : [];
    const nowHm = _coreExplainNowHm();
    const toAbsMins = (station, idx) => {
      const explicitAbs = Number(station?.minsAbs);
      if (Number.isFinite(explicitAbs)) return explicitAbs;
      const raw = _coreExplainParseHm(station?.time);
      if (!Number.isFinite(raw)) return nowHm + (idx * 135);
      return raw < nowHm ? raw + 1440 : raw;
    };
    const normalizedStations = stationsRaw.map((station, idx) => {
      const emphasis = String(station?.emphasis || "").toLowerCase();
      const minsAbs = toAbsMins(station, idx);
      return {
        ...station,
        emphasis: emphasis || "meal",
        minsAbs,
        time: String(station?.time || _coreExplainFmtHm(minsAbs)),
      };
    });
    let nowStation = normalizedStations.find((s) => s.emphasis === "now") || null;
    if (!nowStation) {
      nowStation = {
        label: "Jetzt",
        time: _coreExplainFmtHm(nowHm),
        emphasis: "now",
        minsAbs: nowHm,
      };
    }
    const orderedEvents = normalizedStations
      .filter((s) => s.emphasis !== "now")
      .sort((a, b) => Number(a.minsAbs || 0) - Number(b.minsAbs || 0))
      .slice(0, 5);
    const stations = [nowStation, ...orderedEvents].slice(0, 6);
    while (stations.length < 6) {
      const idx = stations.length;
      const minsAbs = nowHm + (idx * 135);
      stations.push({
        label: `Meal ${Math.max(1, idx)}`,
        time: _coreExplainFmtHm(minsAbs),
        emphasis: "meal",
        minsAbs,
      });
    }
    let mealCounter = 1;
    const normalizedForChart = stations.map((station) => {
      const emphasis = String(station?.emphasis || "").toLowerCase();
      let label = String(station?.label || "").trim();
      if (emphasis === "now") label = "JETZT";
      else if (emphasis === "training") label = "TRAINING";
      else if (emphasis === "meal" || /meal/i.test(label)) label = `MEAL ${mealCounter++}`;
      else label = label ? label.toUpperCase() : `STEP ${mealCounter++}`;
      return {
        ...station,
        emphasis: emphasis || "meal",
        label,
        time: String(station?.time || "--:--"),
      };
    });
    const chart = document.createElement("div");
    chart.className = "core-explain-mp-chart";

    const trainingIdx = normalizedForChart.findIndex((s) => s.emphasis === "training");
    const n = Math.max(2, normalizedForChart.length);
    const anchorPts = normalizedForChart.map((s, idx) => {
      const x = 6 + (idx * (88 / (n - 1)));
      let y = 35.4;
      if (trainingIdx >= 0) {
        if (idx === trainingIdx) {
          y = 24;
        } else if (idx < trainingIdx) {
          const preCount = Math.max(1, trainingIdx);
          const preProgress = idx / preCount;
          y = 36 - (preProgress * 2.8) + (idx % 2 === 0 ? 2.2 : -2.8);
        } else {
          const post = idx - trainingIdx;
          y = 31.2 + (post * 0.45) + (post % 2 === 1 ? 2.8 : -1.7);
        }
      } else {
        const ratio = n > 1 ? (idx / (n - 1)) : 0;
        y = 34.8 + (Math.sin(ratio * Math.PI * 1.8) * 2.3) + (idx % 2 === 0 ? 1.4 : -1.4);
      }
      if (s.emphasis === "now") y = Math.max(y, 37.2);
      return {
        x,
        y: Math.max(22, Math.min(42, y)),
        label: String(s?.label || "STEP"),
        time: String(s?.time || "--:--"),
        emphasis: String(s?.emphasis || "meal"),
      };
    });

    const curvePts = [anchorPts[0]];
    for (let i = 0; i < anchorPts.length - 1; i += 1) {
      const cur = anchorPts[i];
      const next = anchorPts[i + 1];
      const midX = (cur.x + next.x) / 2;
      let midY = (cur.y + next.y) / 2;
      if (trainingIdx >= 0 && i === trainingIdx - 1) {
        midY = Math.min(cur.y, next.y) - 2.6;
      } else if (trainingIdx >= 0 && i === trainingIdx) {
        midY = Math.max(cur.y, next.y) + 4.2;
      } else {
        midY += (i % 2 === 0 ? 2.8 : -2.6);
      }
      curvePts.push({
        x: midX,
        y: Math.max(21.2, Math.min(43.5, midY)),
        label: "",
        time: "",
        emphasis: "ghost",
      });
      curvePts.push(next);
    }
    const lastAnchor = anchorPts[anchorPts.length - 1];
    const tailMidX = Math.min(97, lastAnchor.x + 3.8);
    curvePts.push({ x: tailMidX, y: Math.min(44.5, lastAnchor.y + 2.1), label: "", time: "", emphasis: "ghost" });
    curvePts.push({ x: 99, y: Math.min(45.8, lastAnchor.y + 4.4), label: "", time: "", emphasis: "ghost" });

    const toPath = (arr) => {
      if (!arr.length) return "";
      if (arr.length === 1) return `M ${arr[0].x.toFixed(2)} ${arr[0].y.toFixed(2)}`;
      let d = `M ${arr[0].x.toFixed(2)} ${arr[0].y.toFixed(2)}`;
      for (let i = 0; i < arr.length - 1; i += 1) {
        const p0 = arr[i - 1] || arr[i];
        const p1 = arr[i];
        const p2 = arr[i + 1];
        const p3 = arr[i + 2] || p2;
        const c1x = p1.x + (p2.x - p0.x) / 6;
        const c1y = p1.y + (p2.y - p0.y) / 6;
        const c2x = p2.x - (p3.x - p1.x) / 6;
        const c2y = p2.y - (p3.y - p1.y) / 6;
        d += ` C ${c1x.toFixed(2)} ${c1y.toFixed(2)} ${c2x.toFixed(2)} ${c2y.toFixed(2)} ${p2.x.toFixed(2)} ${p2.y.toFixed(2)}`;
      }
      return d;
    };
    const toAreaPath = (arr) => {
      if (!arr.length) return "";
      const line = toPath(arr);
      const last = arr[arr.length - 1];
      const first = arr[0];
      const baseY = 51;
      return `${line} L ${last.x.toFixed(2)} ${baseY} L ${first.x.toFixed(2)} ${baseY} Z`;
    };
    const curveD = toPath(curvePts);
    const areaD = toAreaPath(curvePts);
    const trainingPoint = anchorPts.find((p) => p.emphasis === "training") || null;
    const trainOffset = trainingPoint ? Math.max(0, Math.min(100, ((trainingPoint.x - 6) / 88) * 100)) : 58;
    const guideTopY = 19.5;
    const guideBaseY = 51;
    const viewH = 72;

    chart.innerHTML = `
      <svg class="core-explain-mp-chart-svg" viewBox="0 0 100 72" preserveAspectRatio="none" aria-hidden="true">
        <defs>
          <linearGradient id="core-strategy-grad-v3" x1="0%" y1="0%" x2="100%" y2="0%">
            <stop offset="0%" stop-color="#2dd4bf"></stop>
            <stop offset="34%" stop-color="#4ade80"></stop>
            <stop offset="${Math.max(40, trainOffset - 10).toFixed(1)}%" stop-color="#facc15"></stop>
            <stop offset="${Math.min(94, trainOffset + 2).toFixed(1)}%" stop-color="#f97316"></stop>
            <stop offset="100%" stop-color="rgba(148,163,184,0.32)"></stop>
          </linearGradient>
          <linearGradient id="core-strategy-area-grad-v3" x1="0%" y1="0%" x2="100%" y2="0%">
            <stop offset="0%" stop-color="rgba(45,212,191,0.18)"></stop>
            <stop offset="48%" stop-color="rgba(250,204,21,0.13)"></stop>
            <stop offset="${Math.min(94, trainOffset + 2).toFixed(1)}%" stop-color="rgba(249,115,22,0.18)"></stop>
            <stop offset="100%" stop-color="rgba(148,163,184,0.06)"></stop>
          </linearGradient>
          <linearGradient id="core-strategy-guide-v3" x1="0%" y1="0%" x2="0%" y2="100%">
            <stop offset="0%" stop-color="rgba(148,163,184,0.48)"></stop>
            <stop offset="100%" stop-color="rgba(148,163,184,0.05)"></stop>
          </linearGradient>
          <linearGradient id="core-strategy-train-guide-v3" x1="0%" y1="0%" x2="0%" y2="100%">
            <stop offset="0%" stop-color="rgba(249,115,22,0.42)"></stop>
            <stop offset="100%" stop-color="rgba(249,115,22,0.02)"></stop>
          </linearGradient>
        </defs>
        <path class="core-explain-mp-chart-floor-top" d="M2 ${guideBaseY + 3.6} L98 ${guideBaseY + 3.6}"></path>
        ${trainingPoint ? `<line class="core-explain-mp-chart-training-guide" x1="${trainingPoint.x.toFixed(2)}" y1="${(trainingPoint.y + 1.6).toFixed(2)}" x2="${trainingPoint.x.toFixed(2)}" y2="${(guideBaseY + 2.2).toFixed(2)}"></line>` : ""}
        ${anchorPts.map((p) => {
          const guideEnd = p.emphasis === "now" ? Math.max(p.y - 1.2, 28) : guideBaseY;
          return `<line class="core-explain-mp-chart-guide is-${p.emphasis}" x1="${p.x.toFixed(2)}" y1="${guideTopY}" x2="${p.x.toFixed(2)}" y2="${guideEnd.toFixed(2)}"></line>`;
        }).join("")}
        ${anchorPts
          .filter((p) => p.emphasis !== "now")
          .map((p) => `<circle class="core-explain-mp-chart-guide-cap is-${p.emphasis}" cx="${p.x.toFixed(2)}" cy="${guideTopY}" r="0.62"></circle>`)
          .join("")}
        <path class="core-explain-mp-chart-area" d="${areaD}"></path>
        <path class="core-explain-mp-chart-glow" d="${curveD}"></path>
        <path class="core-explain-mp-chart-curve" d="${curveD}"></path>
        <rect class="core-explain-mp-chart-subbox" x="2" y="57.4" width="96" height="9.8" rx="1.45"></rect>
      </svg>
    `;

    const labels = document.createElement("div");
    labels.className = "core-explain-mp-chart-labels";
    anchorPts.forEach((p) => {
      const item = document.createElement("div");
      item.className = `core-explain-mp-chart-label is-${p.emphasis}`;
      item.dataset.row = "0";
      item.style.left = `${p.x}%`;
      item.innerHTML = `
        <div class="core-explain-mp-chart-label-name">${escapeHtml(p.label)}</div>
        <div class="core-explain-mp-chart-label-time">${escapeHtml(p.time)}</div>
      `;
      labels.appendChild(item);
    });
    chart.appendChild(labels);

    const nodes = document.createElement("div");
    nodes.className = "core-explain-mp-chart-nodes";
    anchorPts.forEach((p) => {
      const node = document.createElement("span");
      node.className = `core-explain-mp-chart-node is-${p.emphasis}`;
      node.style.left = `${p.x}%`;
      node.style.top = `${(p.y / viewH) * 100}%`;
      nodes.appendChild(node);
    });
    chart.appendChild(nodes);

    strategy.appendChild(chart);
    const strategyNote = document.createElement("div");
    strategyNote.className = "core-explain-mp-strategy-note";
    strategyNote.textContent = String(payload?.strategyNote || "Kalorienfenster bewusst bis nach Training offen halten, dann kontrolliert auffüllen.");
    strategy.appendChild(strategyNote);
    strategyCol.appendChild(strategy);

    middle.append(guardCol, strategyCol);
    node.appendChild(middle);

    const lower = document.createElement("section");
    lower.className = "core-explain-mp-lower";
    const observe = document.createElement("div");
    observe.className = "core-explain-mp-box";
    observe.innerHTML = `<div class="core-explain-mp-section-title">Was CORE beobachtet</div>`;
    const observeGrid = document.createElement("div");
    observeGrid.className = "core-explain-mp-observe-grid";
    (Array.isArray(payload?.observes) ? payload.observes : []).slice(0, 8).forEach((o) => {
      const cell = document.createElement("div");
      cell.className = "core-explain-mp-observe";
      cell.innerHTML = `<div class="core-explain-mp-observe-label">${escapeHtml(String(o?.label || ""))}</div><div class="core-explain-mp-observe-value">${escapeHtml(String(o?.value || ""))}</div>`;
      observeGrid.appendChild(cell);
    });
    observe.appendChild(observeGrid);

    const avoid = document.createElement("div");
    avoid.className = "core-explain-mp-box";
    avoid.innerHTML = `<div class="core-explain-mp-section-title">Was CORE nicht empfiehlt</div>`;
    const avoidList = document.createElement("div");
    avoidList.className = "core-explain-mp-avoid-list";
    _compactUnique(payload?.avoidList || [], 4).forEach((line) => {
      const row = document.createElement("div");
      row.className = "core-explain-mp-avoid";
      row.textContent = line;
      avoidList.appendChild(row);
    });
    avoid.appendChild(avoidList);
    lower.append(observe, avoid);
    node.appendChild(lower);

    const decision = document.createElement("section");
    decision.className = "core-explain-mp-decision";
    decision.innerHTML = `<div class="core-explain-mp-section-title">Entscheidung heute</div>`;
    const chain = document.createElement("div");
    chain.className = "core-explain-mp-chain";
    const steps = (Array.isArray(payload?.steps) ? payload.steps : []).slice(0, 4);
    steps.forEach((step, idx) => {
      const s = document.createElement("div");
      s.className = "core-explain-mp-step";
      s.textContent = String(step || "");
      chain.appendChild(s);
      if (idx < steps.length - 1 && idx < 3) {
        const arrow = document.createElement("span");
        arrow.className = "core-explain-mp-step-arrow";
        arrow.textContent = "->";
        chain.appendChild(arrow);
      }
    });
    decision.appendChild(chain);
    if (payload?.footer) {
      const meta = document.createElement("div");
      meta.className = "core-explain-mp-footer";
      meta.textContent = String(payload.footer);
      decision.appendChild(meta);
    }
    node.appendChild(decision);
    return node;
  }

  function _buildCoreExplainPanel(payload, layout) {
    if (payload?.variant === "mealplan_nutrition") {
      return _buildMealplanCoreExplainPanel(payload, layout);
    }
    const tpl = els.coreExplainPanelTemplate;
    if (!tpl?.content) return null;
    const node = tpl.content.firstElementChild.cloneNode(true);
    const title = node.querySelector("#core-explain-panel-title");
    const status = node.querySelector("#core-explain-panel-status");
    const meta = node.querySelector("#core-explain-panel-meta");
    const verdict = node.querySelector("#core-explain-hero-verdict");
    const subtext = node.querySelector("#core-explain-hero-subtext");
    const drivers = node.querySelector("#core-explain-driver-grid");
    const allow = node.querySelector("#core-explain-matrix-allow");
    const avoid = node.querySelector("#core-explain-matrix-avoid");
    const rationale = node.querySelector("#core-explain-rationale");
    const signals = node.querySelector("#core-explain-signal-chips");
    const practical = node.querySelector("#core-explain-practical");
    const footer = node.querySelector("#core-explain-panel-footer");

    if (title) title.textContent = String(payload?.title || "CORE");
    const statusText = String(payload?.status || "").trim();
    if (status) {
      if (statusText) {
        status.hidden = false;
        status.textContent = statusText;
        status.dataset.status = _coreExplainStatusKey(statusText);
      } else {
        status.hidden = true;
        status.textContent = "";
      }
    }
    const metaText = String(payload?.meta || "").trim();
    if (meta) {
      meta.hidden = !metaText;
      meta.textContent = metaText;
    }
    if (verdict) verdict.textContent = String(payload?.verdict || "CORE hält heute den stabilen Kurs.");
    if (subtext) subtext.textContent = String(payload?.subtext || "Die heutige Entscheidung schützt die Woche und lässt nur tragbare Belastung zu.");
    _renderCoreExplainDrivers(
      Array.isArray(payload?.drivers) && payload.drivers.length
        ? payload.drivers
        : _normalizeDrivers(payload?.rationale || [], "Treiber"),
      drivers
    );
    _renderCoreExplainList(
      _coreExplainLines(payload?.allowed, "Locker und sauber wie geplant."),
      allow
    );
    _renderCoreExplainList(
      _coreExplainLines(payload?.avoided, "Unnötige Eskalation bleibt heute außen vor."),
      avoid
    );
    _renderCoreExplainList(
      _coreExplainLines(payload?.rationale, "Heute wäre zusätzliche Härte schlecht getimt."),
      rationale
    );
    _renderCoreExplainSignalPills(
      _coreExplainLines(payload?.signals, "Signal-Lage stabil"),
      signals
    );
    _renderCoreExplainList(
      _coreExplainLines(payload?.practical, "Die Session bleibt kontrolliert und zählt als saubere Systemarbeit."),
      practical
    );
    const footerText = String(payload?.footer || "").trim();
    if (footer) {
      footer.hidden = !footerText;
      footer.textContent = footerText;
    }
    if (layout === "sheet" || layout === "modal") {
      node.setAttribute("aria-modal", "true");
    }
    return node;
  }

  function _mealplanTrendDescriptor(trendRaw) {
    const trend = Number(trendRaw);
    if (!Number.isFinite(trend)) {
      return {
        short: "Trend noch unklar",
        detail: "Gewichtstrend wird mit neuen Logs weiter geschärft.",
        badge: "noch wenig Daten",
      };
    }
    const abs = Math.abs(trend);
    if (trend >= 0.3) {
      return {
        short: `Gewichtstrend steigt kontrolliert (${trend.toFixed(1).replace(".", ",")} kg/Woche)`,
        detail: "Aufwärtstrend ist intakt, daher reicht heute eine strukturierte Führung statt aggressivem Catch-up.",
        badge: `+${trend.toFixed(1).replace(".", ",")} kg/W`,
      };
    }
    if (trend >= 0.1) {
      return {
        short: `Trend aktuell positiv (${trend.toFixed(1).replace(".", ",")} kg/Woche)`,
        detail: "Die Richtung passt, deshalb bleibt CORE heute bei moderater Steuerung.",
        badge: `+${trend.toFixed(1).replace(".", ",")} kg/W`,
      };
    }
    if (abs < 0.1) {
      return {
        short: "Gewicht entwickelt sich aktuell stabil",
        detail: "Kein harter Richtungsdruck erkennbar, daher Fokus auf saubere Tagesstruktur.",
        badge: `${trend.toFixed(1).replace(".", ",")} kg/W`,
      };
    }
    return {
      short: `Trend fällt aktuell (${trend.toFixed(1).replace(".", ",")} kg/Woche)`,
      detail: "CORE lässt heute bewusst Raum für kontrolliertes Auffüllen statt hektischer Einzelmaßnahmen.",
      badge: `${trend.toFixed(1).replace(".", ",")} kg/W`,
    };
  }

  function _coreExplainConfig(key) {
    if (key === "training") {
      return null;
    }
    if (key === "mealplan") {
      return {
        key,
        host: els.mealplanCard,
        slot: els.mealplanCoreExplainSlot,
        trigger: els.mealplanCorePill,
        payload: () => state.mealplanCoreExplainPayload,
        allowed: () => !!state.mealplanPayload?.core_nutrition,
      };
    }
    return null;
  }

  function _coreExplainLayoutForHost(host) {
    void host;
    return "modal";
  }

  function _setCoreExplainTriggerState() {
    const items = [_coreExplainConfig("training"), _coreExplainConfig("mealplan")].filter(Boolean);
    for (const item of items) {
      const isOpen = state.coreExplainActiveKey === item.key;
      const trigger = item.trigger;
      if (!trigger) continue;
      trigger.classList.toggle("is-active", isOpen);
      trigger.setAttribute("aria-expanded", isOpen ? "true" : "false");
    }
  }

  function closeCoreExplainPanel(opts = {}) {
    const restoreFocus = opts.restoreFocus !== false;
    const prevKey = state.coreExplainActiveKey;
    const active = prevKey ? _coreExplainConfig(prevKey) : null;
    if (active?.host) {
      active.host.classList.remove("has-core-explain-panel");
      active.host.removeAttribute("data-core-explain-layout");
    }
    if (active?.slot) {
      active.slot.hidden = true;
      active.slot.innerHTML = "";
    }
    if (els.coreExplainSheetBackdrop) {
      els.coreExplainSheetBackdrop.hidden = true;
    }
    if (els.coreExplainSheetHost) {
      els.coreExplainSheetHost.hidden = true;
      els.coreExplainSheetHost.innerHTML = "";
    }
    state.coreExplainActiveKey = null;
    state.coreExplainActiveLayout = null;
    _unlockCoreExplainPageScroll();
    if (restoreFocus && state.coreExplainLastTrigger?.focus) {
      try { state.coreExplainLastTrigger.focus(); } catch (_) {}
    }
    state.coreExplainLastTrigger = null;
    _setCoreExplainTriggerState();
    renderCoreSteuertBadge(state.coreModeEnabled);
  }

  function openCoreExplainPanel(key) {
    const cfg = _coreExplainConfig(key);
    if (!cfg || !cfg.host || !cfg.allowed()) return;
    const payload = cfg.payload();
    if (!payload) return;
    if (state.coreExplainActiveKey && state.coreExplainActiveKey !== key) {
      closeCoreExplainPanel({ restoreFocus: false });
    }
    const layout = _coreExplainLayoutForHost(cfg.host);
    const panel = _buildCoreExplainPanel(payload, layout);
    if (!panel) return;
    const closeBtn = panel.querySelector("#core-explain-panel-close");
    if (closeBtn) closeBtn.addEventListener("click", () => closeCoreExplainPanel());

    state.coreExplainActiveKey = key;
    state.coreExplainActiveLayout = layout;
    state.coreExplainLastTrigger = cfg.trigger || null;

    if (els.coreExplainSheetBackdrop) {
      els.coreExplainSheetBackdrop.hidden = false;
    }
    if (els.coreExplainSheetHost) {
      els.coreExplainSheetHost.hidden = false;
      els.coreExplainSheetHost.innerHTML = "";
      els.coreExplainSheetHost.appendChild(panel);
    }
    _lockCoreExplainPageScroll();

    _setCoreExplainTriggerState();
    renderCoreSteuertBadge(state.coreModeEnabled);
    requestAnimationFrame(() => {
      try { closeBtn?.focus(); } catch (_) {}
    });
  }

  function _lockCoreExplainPageScroll() {
    if (state.coreExplainScrollLock) return;
    const body = document.body;
    const html = document.documentElement;
    if (!body || !html) return;
    state.coreExplainScrollLock = {
      bodyOverflow: body.style.overflow || "",
      htmlOverflow: html.style.overflow || "",
    };
    body.classList.add("core-explain-open");
    html.classList.add("core-explain-open");
    body.style.overflow = "hidden";
    html.style.overflow = "hidden";
  }

  function _unlockCoreExplainPageScroll() {
    const lock = state.coreExplainScrollLock;
    const body = document.body;
    const html = document.documentElement;
    if (!body || !html) return;
    body.classList.remove("core-explain-open");
    html.classList.remove("core-explain-open");
    if (lock) {
      body.style.overflow = lock.bodyOverflow;
      html.style.overflow = lock.htmlOverflow;
    } else {
      body.style.overflow = "";
      html.style.overflow = "";
    }
    state.coreExplainScrollLock = null;
  }

  function toggleCoreExplainPanel(key) {
    if (state.coreExplainActiveKey === key) {
      closeCoreExplainPanel();
      return;
    }
    openCoreExplainPanel(key);
  }

  function pcRenderSession(sessionResp, weekOverview) {
    if (hasActiveTrainingTodayCard()) {
      try { console.debug("[training-card] skip legacy pcRenderSession because GPT card is active"); } catch (_) {}
      return;
    }
    const box = pcEl("plan-check-exercises");
    const msg = pcEl("plan-check-message");
    if (!box || !msg) return;

    const payloadCoreEnabled = (typeof sessionResp?.core_mode_enabled === "boolean")
      ? sessionResp.core_mode_enabled
      : ((typeof sessionResp?.session?.meta?.core_mode_enabled === "boolean")
        ? sessionResp.session.meta.core_mode_enabled
        : null);
    const isAutopilot = !!(sessionResp?.session?.meta?.autopilot);
    const hasAutopilotBadge = Array.isArray(sessionResp?.session?.meta?.badges)
      ? sessionResp.session.meta.badges.includes("AUTOPILOT")
      : false;
    const autopilotMode = isAutopilot || hasAutopilotBadge;
    const coreBadgeVisible = (payloadCoreEnabled !== null)
      ? !!payloadCoreEnabled
      : !!state.coreModeEnabled;
    state.coreModeEnabled = coreBadgeVisible;
    renderCoreSteuertBadge(coreBadgeVisible);

    pcRenderAutopilotExplain(sessionResp);

    if (!sessionResp || sessionResp.ok !== true) {
      _setPlanCheckCardTitle(null);
      msg.textContent = "Plan-Check nicht verfügbar.";
      return;
    }

    _setPlanCheckCardTitle(sessionResp);

    const coreCard = state.dashboardSnapshot?.coreTrainingCard && typeof state.dashboardSnapshot.coreTrainingCard === "object"
      ? state.dashboardSnapshot.coreTrainingCard
      : null;
    const coreSessionType = String(coreCard?.session_type || "").toLowerCase();
    const coreIntent = String(coreCard?.decision_intent || "").toLowerCase();
    const recoveryOverride = coreSessionType === "recovery" || coreSessionType === "hard_stop" || coreIntent === "recovery_only" || coreIntent === "hard_stop";

    const items = sessionResp?.items || [];
    if (!items.length && !recoveryOverride) {
      msg.textContent = sessionResp?.message || "Keine Übungen gefunden.";
      return;
    }

    const layer = weekOverview?.layer || {};
    const weekType = weekOverview?.week?.week_type || layer?.week_type || "";
    const effectiveCoreMode = String(sessionResp?.autopilot_mode_effective || "").toUpperCase();
    const strictHeavyFromCore = (effectiveCoreMode === "HEAVY");
    try {
      const badge = document.getElementById("autopilot-badge");
      if (badge) badge.textContent = "CORE steuert";
      const modeLine = document.getElementById("plan-check-mode-line");
      if (modeLine) modeLine.textContent = "LIVA-Vorschlag · kleinschrittig angepasst";
      const staticDay = document.getElementById("plan-check-static-day");
      const selectedDay = String(sessionResp?.selected_day_id || "").trim();
      const weekDays = Array.isArray(sessionResp?.week_days) ? sessionResp.week_days : [];
      const selectedRow = selectedDay ? weekDays.find((d) => String(d?.day_id || "") === selectedDay) : null;
      const staticLabel = selectedRow
        ? pcStripReassessLabel(selectedRow.label || `${selectedRow?.weekday_short || ""} · ${selectedRow?.final_label || selectedRow?.planned_label || ""}`)
        : "";
      if (staticDay) staticDay.textContent = `Heute: ${staticLabel || String(sessionResp?.session?.session_name || "—")}`;
    } catch (_) {}

    try { console.log("[training-card] final payload", sessionResp); } catch (_) {}

    const renderKey = JSON.stringify({
      session: sessionResp?.session?.session_key || sessionResp?.session?.session_name || "",
      selected: sessionResp?.selected_day_id || "",
      todayChangedSource: String(state.dashboardSnapshot?.coreTrainingCard?.today_changed_source || ""),
      todayChangedLines: Array.isArray(state.dashboardSnapshot?.coreTrainingCard?.today_changed_lines)
        ? state.dashboardSnapshot.coreTrainingCard.today_changed_lines.map((line) => String(line || "").trim()).filter(Boolean)
        : [],
      items: items.map((it) => ({
        title: it?.title || it?.name || "",
        done: it?.done || {},
        structured: it?.structured || {},
        pill: it?.pill || {},
      })),
    });
    if (renderKey === state.trainingCardLastRenderKey) {
      msg.textContent = "";
      return;
    }

    const fallbackTodayChangedLines = () => {
      return [
        "CORE bewertet die Einheit über einzelne Stellschrauben.",
        "Gewichte und Sätze werden bei Bedarf separat angepasst.",
        "Heute sauber ausführen und keine Extras erzwingen.",
      ];
    };
    const resolveTodayChangedLines = () => {
      const trainingCard = state.dashboardSnapshot?.coreTrainingCard && typeof state.dashboardSnapshot.coreTrainingCard === "object"
        ? state.dashboardSnapshot.coreTrainingCard
        : null;
      const todayChangedSource = String(trainingCard?.today_changed_source || "").trim();
      const candidates = [
        [todayChangedSource || "coreTrainingCard.today_changed_lines", trainingCard?.today_changed_lines],
        ["coreTrainingCard.gpt_reason_lines", trainingCard?.gpt_reason_lines],
        ["coreTrainingCard.reason_lines", trainingCard?.reason_lines],
        ["coreTrainingCard.why_today", trainingCard?.why_today],
        ["coreTrainingCard.source_context.visible_reasons", trainingCard?.source_context?.visible_reasons],
        ["session.autopilot_explain.why_lines", sessionResp?.autopilot_explain?.why_lines],
      ];
      for (const [source, value] of candidates) {
        const lines = Array.isArray(value)
          ? value.map((line) => String(line?.text || line?.label || line?.reason || line || "").trim()).filter(Boolean).slice(0, 3)
          : [];
        if (lines.length) return { source, lines };
      }
      return { source: "fallbackTodayChangedLines", lines: fallbackTodayChangedLines() };
    };

    const deriveVisibleTargetRep = ({ explicitRep, repsMin, repsMax, setIndex, setCount, mode, exerciseName }) => {
      const explicit = parseInt(explicitRep, 10);
      if (Number.isFinite(explicit)) return explicit;
      const min = parseInt(repsMin, 10);
      const max = parseInt(repsMax, 10);
      if (Number.isFinite(min) && Number.isFinite(max)) {
        const span = max - min;
        const isTop = setIndex === 0;
        const isAccessory = /(fly|flies|seitheb|lateral|raise|curl|triceps|pushdown|extension)/i.test(String(exerciseName || ""));
        if (min === max) return min;
        if (isTop) {
          return Math.min(max, min + Math.max(1, Math.round(span * 0.5)));
        }
        if (String(mode || "").toUpperCase() === "LIGHT") {
          return isAccessory ? Math.min(max, min + Math.max(1, Math.round(span * 0.33))) : max;
        }
        if (isAccessory && setCount <= 2) {
          return Math.min(max, min + Math.max(1, Math.round(span * 0.5)));
        }
        return max;
      }
      if (Number.isFinite(min)) return min;
      if (Number.isFinite(max)) return max;
      return null;
    };
    const formatCompactToday = (it, plannedRpeDisplay, step) => {
      const reps = Array.isArray(it?.structured?.new_reps) ? it.structured.new_reps.map((x) => parseInt(x, 10)).filter(Number.isFinite) : [];
      const weights = Array.isArray(it?.structured?.new_weights) ? it.structured.new_weights.map((x) => pcNum(x)).filter((x) => x != null) : [];
      if (reps.length && weights.length) {
        const targetCount = Math.max(reps.length, weights.length);
        const repParts = [];
        for (let i = 0; i < targetCount; i += 1) {
          const visibleRep = deriveVisibleTargetRep({
            explicitRep: reps[i],
            repsMin: it?.planned?.reps_min,
            repsMax: it?.planned?.reps_max,
            setIndex: i,
            setCount: targetCount,
            mode: effectiveCoreMode,
            exerciseName: it?.title || it?.name || "",
          });
          repParts.push(Number.isFinite(visibleRep) ? String(visibleRep) : "");
        }
        const repValues = repParts.filter(Boolean);
        const repText = repValues.length >= 2
          ? `${repValues[0]}–${repValues[repValues.length - 1]}`
          : repValues.join("");
        const weightText = formatWeightsInline(weights, step);
        const rpeText = String(plannedRpeDisplay || "").replace(/-/g, "/");
        const setText = targetCount ? `${targetCount}×${repText}` : repText;
        return `${setText}${weightText ? ` · ${weightText}` : ""}${rpeText ? ` @${rpeText}` : ""}`.trim();
      }
      const inline = pcStripSetPrefix(String(it?.suggestion_inline || "").trim())
        .replace(/\s*x\s*/gi, " × ")
        .replace(/\s*@\s*/g, " @");
      return inline || "—";
    };
    const formatCompactLast = (it) => {
      const formatted = formatLastLine(it?.done || {}, stepForDevice(String(it?.title || it?.name || ""), it?.planned?.device))
        .replace(/\s*x\s*/gi, " × ")
        .replace(/\s*@RPE\s*/i, " @");
      if (formatted && formatted !== "—") return formatted;
      const real = pcBuildLastLineFromDone(it?.done || {})
        .replace(/\s*x\s*/gi, " × ")
        .replace(/\s*@RPE\s*/i, " @")
        .replace(/\s*·\s*\d{4}-\d{2}-\d{2}\s*$/i, "")
        .trim();
      if (real) return real;
      const ref = it?.done?.last_reference || null;
      if (ref) {
        const rr = Number.isFinite(parseInt(ref.reps, 10)) ? String(parseInt(ref.reps, 10)) : "—";
        const ww = Number.isFinite(pcNum(ref.weight)) ? pcFormatKg(pcNum(ref.weight)) : "—";
        const rp = Number.isFinite(pcNum(ref.rpe)) ? formatRpeInt(pcNum(ref.rpe)) : "";
        return `${rr} × ${ww}${rp ? ` @${rp}` : ""}`;
      }
      return "kein Verlauf";
    };
    const changeLines = [];
    const pushChangeLine = (line) => {
      const text = String(line || "").trim();
      if (!text || changeLines.includes(text)) return;
      changeLines.push(text);
    };
    const todayChanged = resolveTodayChangedLines();
    try { console.log("[training-card] today_changed_source", todayChanged.source, todayChanged.lines); } catch (_) {}
    todayChanged.lines.forEach((line) => pushChangeLine(line));

    if (recoveryOverride) {
      const forecastMode = coreSessionType === "hard_stop" ? "hard stop" : "recovery";
      const forecastLabel = String(coreCard?.title || "Recovery statt Training").trim() || "Recovery statt Training";
      const forecastSummary = pcBuildForecastSummary({
        sessionName: forecastLabel,
        mode: forecastMode,
        badges: [],
        changeLines,
      });
      const recoveryItems = Array.isArray(coreCard?.items) ? coreCard.items : [];
      const recoveryNote = recoveryItems
        .map((item) => String(item?.note || item?.target || item?.display_name || "").trim())
        .filter(Boolean)[0] || "";
      state.trainingCardHeadlineStatus = forecastSummary.anomaly ? "AUFFÄLLIG" : "ANGEPASST";
      renderCoreSteuertBadge(coreBadgeVisible);
      box.innerHTML = `
        <section class="training-core-forecast">
          <details class="training-core-forecast-accordion"${forecastSummary.details.length ? "" : " open"}>
            <summary class="training-core-forecast-header">
              <div class="training-core-forecast-title-row">
                <div class="training-core-forecast-title">
                  <div class="training-core-forecast-title-main">GPT-Coaching</div>
                </div>
                <span class="training-core-forecast-chevron" aria-hidden="true"></span>
              </div>
              <div class="training-core-forecast-mode">${escapeHtml(forecastLabel)} · ${escapeHtml(forecastSummary.status || "prüfen")}</div>
              <div class="training-core-forecast-sub"><div class="training-core-forecast-line">${escapeHtml(forecastSummary.summary)}</div></div>
            </summary>
            ${forecastSummary.details.length ? `<div class="training-core-forecast-body">${forecastSummary.details.map((line) => `<div class="training-core-forecast-line">${escapeHtml(line)}</div>`).join("")}</div>` : ""}
          </details>
          <div class="training-plan-list">
            <div class="training-plan-row training-plan-row-recovery">
              <div class="training-plan-name">${escapeHtml(String(recoveryItems?.[0]?.display_name || "Heute kein Training"))}</div>
              <div class="training-plan-badge badge-neutral is-hold">Rest</div>
              <div class="training-plan-target">${escapeHtml(String(recoveryItems?.[0]?.target || "kein Training heute"))}</div>
              <div class="training-plan-reference">${escapeHtml(recoveryNote || "Upper bleibt die nächste sinnvolle Einheit.")}</div>
            </div>
          </div>
        </section>
      `;
      state.trainingCardLastRenderKey = renderKey;
      msg.textContent = "";
      const changesBox = document.getElementById("plan-check-changes-box");
      if (changesBox) {
        changesBox.hidden = true;
        changesBox.innerHTML = "";
      }
      return;
    }

    const rowsHtml = [];
    const rowBadges = [];
    const rowDebugHtml = [];
    const globalPressureText = todayChanged.lines.join(" | ");
    for (const it of items) {
      try { console.log("[training-card] item payload", it); } catch (_) {}
      const name = String(it?.title || "").trim() || "Übung";
      const coreItem = pcFindCoreTrainingItem(coreCard, name);
      const planned = it?.planned || {};
      const step = stepForDevice(name, planned?.device);

      const base = normalizeSuggestionArrays({
        reps: it?.structured?.new_reps || [],
        weights: it?.structured?.new_weights || [],
      });
      let layered = applyWeekLayerToSuggestion(base, layer);
      const rpeTarget = pcRpeTarget(planned, layer);

      if (strictHeavyFromCore) {
        // In CORE-HEAVY, keep backend suggestion as source of truth.
        // Frontend post-processing was softening visible output and masking backend improvements.
        layered = normalizeSuggestionArrays({
          reps: base.reps,
          weights: base.weights,
        });
        const plannedSets = parseInt(planned?.sets, 10);
        const normalized = ensureSuggestionSetCount({ reps: layered.reps, weights: layered.weights }, plannedSets);
        layered.reps = normalized.reps;
        layered.weights = normalized.weights;
      } else {
        // Rep progression gate: only allow rep increases if last avg RPE was inside plan target window for this week.
        const gateOk = isRpeInTargetWindow(it?.done?.avg_rpe, planned, layer, weekType);
        layered.reps = clampRepsProgression({
          lastReps: it?.done?.reps || [],
          suggestedReps: layered.reps,
          gateOk,
        });

        // Ensure reps targets; if last reps were below min -> reduce weight.
        const adjusted = applyRepTargetAndWeightAdjust({
          done: it?.done || {},
          planned,
          layer,
          weekType,
          reps: layered.reps,
          weights: layered.weights,
          step,
        });
        layered.reps = adjusted.reps;
        layered.weights = adjusted.weights;
        const plannedSets = parseInt(planned?.sets, 10);
        const normalized = ensureSuggestionSetCount({ reps: layered.reps, weights: layered.weights }, plannedSets);
        layered.reps = normalized.reps;
        layered.weights = normalized.weights;

        // Rule: first set must have the highest weight (top set first), and later sets must not be heavier.
        layered.weights = pcEnsureTopSetFirst(layered.weights, step);

        // Cap overly aggressive weight drops depending on exercise (e.g. curls vs RDL).
        layered.weights = pcClampWeightDropsByExercise(name, it?.done?.weights || [], layered.weights, step);
      }

      const inlineText = (it?.suggestion_inline || "").trim();
      const suggestionLine = (it?.force_inline && inlineText) ? inlineText : formatSuggestionLine({
        reps: layered.reps,
        weights: layered.weights,
        rpe: rpeTarget,
      }, step);

      const prescription = it?.todays_prescription && typeof it.todays_prescription === "object" ? it.todays_prescription : null;
      const gptReason = String(prescription?.gpt_reason_short || "").trim();
      if (gptReason) pushChangeLine(gptReason);
      const localWeightReason = pcGetExerciseWeightReason({
        done: it?.done || {},
        planned,
        layer,
        weekType,
        step,
        note: gptReason,
      });
      const globalReasonApplied = /(kalender|abend|stress|schule|alltag|sleep|schlaf|recovery|hrv|ruhepuls)/i.test(globalPressureText);
      layered = pcStabilizeWeightsNearReference({
        done: it?.done || {},
        planned,
        suggested: layered,
        step,
        localReason: localWeightReason,
      });
      const coreTodayText = pcFormatTodayFromCoreItem(coreItem);
      const todayText = coreTodayText || formatCompactToday({ ...it, structured: { ...(it?.structured || {}), new_reps: layered.reps, new_weights: layered.weights } }, rpeTarget, step) || suggestionLine || "—";
      const lastLine = formatLastLine(it?.done || {}, step);
      const coreLastText = pcFormatReferenceFromCoreItem(coreItem);
      const lastText = coreLastText || formatCompactLast(it) || (lastLine && lastLine !== "—" ? lastLine : "kein Verlauf");
      const badgeInfo = pcComputeExerciseBadge({
        name,
        planned,
        todayText,
        lastText,
        step,
      });
      const adjustmentLabel = String(badgeInfo?.label || "halten").trim() || "halten";
      const pillColor = String(badgeInfo?.color || "").toLowerCase();
      const badgeStateClass = String(badgeInfo?.stateClass || "is-hold");
      const badgeClass = pillColor === "green" ? "badge-green"
        : pillColor === "red" ? "badge-red"
        : pillColor === "neutral" ? "badge-neutral"
        : "badge-yellow";
      rowBadges.push(badgeInfo);
      const refAvg = pcParseVisiblePlanMetrics(lastText).avgWeight;
      const planAvg = pcParseVisiblePlanMetrics(todayText).avgWeight;
      rowDebugHtml.push(`
        <div class="training-core-debug-row">
          <strong>${escapeHtml(name)}</strong>
          <div>Referenz: ${escapeHtml(lastText)}</div>
          <div>Heute: ${escapeHtml(todayText)}</div>
          <div>Differenz: ${escapeHtml((Number.isFinite(planAvg) && Number.isFinite(refAvg)) ? `${formatDeNumber((planAvg - refAvg), 1)} kg` : "—")}</div>
          <div>Lokaler Grund: ${escapeHtml(localWeightReason.hasLocalReason ? "ja" : "nein")}</div>
          <div>Globaler Grund: ${escapeHtml(globalReasonApplied ? "ja" : "nein")}</div>
          <div>Stellschraube: ${escapeHtml(adjustmentLabel)}</div>
          <div>Finale Entscheidung: ${escapeHtml(localWeightReason.hasLocalReason ? "lokale Anpassung erlaubt" : "nahe an Referenz gehalten")}</div>
        </div>
      `);
      rowsHtml.push(`
        <div class="training-plan-row">
          <div class="training-plan-name">${escapeHtml(name)}</div>
          <div class="training-plan-badge ${badgeClass} ${badgeStateClass}">${escapeHtml(adjustmentLabel)}</div>
          <div class="training-plan-target">${escapeHtml(todayText)}</div>
          <div class="training-plan-reference">${escapeHtml(lastText)}</div>
        </div>
      `);
    }
    const forecastSummary = pcBuildForecastSummary({
      sessionName: String(sessionResp?.session?.session_name || "Training"),
      mode: effectiveCoreMode,
      badges: rowBadges,
      changeLines,
    });
    state.trainingCardHeadlineStatus = forecastSummary.anomaly
      ? "AUFFÄLLIG"
      : /angepasst|reduziert/i.test(String(forecastSummary.status || ""))
        ? "ANGEPASST"
        : "CORE steuert";
    renderCoreSteuertBadge(coreBadgeVisible);
    box.innerHTML = `
      <section class="training-core-forecast">
        <details class="training-core-forecast-accordion"${forecastSummary.details.length ? "" : " open"}>
          <summary class="training-core-forecast-header">
            <div class="training-core-forecast-title-row">
              <div class="training-core-forecast-title">
                <div class="training-core-forecast-title-main">GPT-Coaching</div>
              </div>
              <span class="training-core-forecast-chevron" aria-hidden="true"></span>
            </div>
            <div class="training-core-forecast-mode">${escapeHtml(forecastSummary.title)} · ${escapeHtml(forecastSummary.status || "freigegeben")}</div>
            <div class="training-core-forecast-sub"><div class="training-core-forecast-line">${escapeHtml(forecastSummary.summary)}</div></div>
          </summary>
          ${(forecastSummary.details.length || rowDebugHtml.length) ? `<div class="training-core-forecast-body">${forecastSummary.details.map((line) => `<div class="training-core-forecast-line">${escapeHtml(line)}</div>`).join("")}${rowDebugHtml.length ? `<details class="training-core-debug"><summary>Details je Übung</summary><div class="training-core-debug-body">${rowDebugHtml.join("")}</div></details>` : ""}</div>` : ""}
        </details>
        <div class="training-plan-list">
          ${rowsHtml.join("")}
        </div>
      </section>
    `;
    state.trainingCardLastRenderKey = renderKey;
    msg.textContent = "";
    const changesBox = document.getElementById("plan-check-changes-box");
    if (changesBox) {
      changesBox.hidden = true;
      changesBox.innerHTML = "";
    }
  }

  let pcInitBusy = false;

  function _queueFreshNextSession(dayKey, weekOverview) {
    const key = String(dayKey || getTodayDateUtcKey() || "").trim();
    if (!key) return;
    if (state.nextSessionRefreshPromise) return;
    state.nextSessionRefreshPromise = safeJson(`/api/next_session?day=${encodeURIComponent(key)}&refresh=1`)
      .then((resp) => {
        if (!_pcIsCurrentSelection(key)) return;
        if (_isFreshNextSessionPayload(resp)) {
          _pcRenderSessionSafe(resp, weekOverview || null);
          _setPlanCheckCardTitle(resp);
          _syncPlanCheckSelectorFromSession(resp);
          const link = pcEl("plan-check-link");
          _pcRenderReferenceLink(link, resp?.reference_workout);
        }
      })
      .catch(() => {})
      .finally(() => {
        state.nextSessionRefreshPromise = null;
      });
  }

  async function pcInit() {
    if (pcInitBusy) {
      state.pcInitQueued = true;
      return;
    }
    pcInitBusy = true;
    try {
      const sel = pcEl("plan-check-select");
      const link = pcEl("plan-check-link");
      const msg = pcEl("plan-check-message");
      if (!sel || !link) return;

      const cachedDashboard = state.dashboardSnapshot
        || _mapDashboardPayload(window.__DASHBOARD_BOOTSTRAP__)
        || null;
      if (cachedDashboard && !state.dashboardSnapshot) {
        state.dashboardSnapshot = cachedDashboard;
      }
      const weekOverview = cachedDashboard?.planWeek || await safeJson("/api/dashboard/plan_check_week_overview");
      const weekOverviewOk = !!(weekOverview && weekOverview.ok === true);
      if (weekOverviewOk) {
        pcRenderWeekMeta(weekOverview);
      }

      let nextSession = cachedDashboard?.nextSession || null;
      if (!nextSession) {
        try {
          nextSession = await safeJson("/api/next_session");
        } catch (_) {
          nextSession = null;
        }
      }
      let nextSessionOk = !!(nextSession && nextSession.ok === true);
      if (nextSession?.loading || nextSession?.stale) {
        const requestedDay = String(nextSession?.selected_day_id || getTodayDateUtcKey() || "").trim();
        let refreshedSession = null;
        if (requestedDay) {
          try {
            refreshedSession = await safeJson(`/api/next_session?day=${encodeURIComponent(requestedDay)}&refresh=1`);
          } catch (_) {
            refreshedSession = null;
          }
        }
        if (_isFreshNextSessionPayload(refreshedSession)) {
          nextSession = refreshedSession;
          nextSessionOk = true;
        } else {
          if (weekOverviewOk) _renderPlanCheckWeekFallback(weekOverview);
          _renderPlanCheckPending(
            nextSession?.loading ? "Nächste Einheit wird vorbereitet …" : "Nächste Einheit wird aktualisiert …",
            { preserveContent: true }
          );
          _queueFreshNextSession(requestedDay, weekOverviewOk ? weekOverview : null);
          setTimeout(() => {
            try { pcInit(); } catch (_) {}
          }, 2000);
          return;
        }
      }
      if (!weekOverviewOk && !nextSessionOk) {
        if (msg) msg.textContent = "Nächste Einheit konnte gerade nicht geladen werden.";
        link.textContent = "Letzte Ausführung: —";
        return;
      }

      const hasWeekDays = Array.isArray(nextSession?.week_days) && nextSession.week_days.length === 7;

      sel.innerHTML = "";
      if (hasWeekDays) {
        for (const optRow of nextSession.week_days) {
          const opt = document.createElement("option");
          opt.value = optRow.day_id;
          const raw = optRow.label || `${optRow.weekday_short || ""} · ${optRow.final_label || optRow.planned_label || ""}`;
          opt.textContent = pcStripReassessLabel(raw);
          sel.appendChild(opt);
        }
      } else if (weekOverviewOk) {
        for (const s of weekOverview.sessions || []) {
          const opt = document.createElement("option");
          opt.value = s.session_key;
          opt.textContent = s.session_name;
          sel.appendChild(opt);
        }
      }

      const defKey = (weekOverviewOk
        ? (weekOverview?.today?.default_session_key || (weekOverview.sessions?.[0]?.session_key ?? ""))
        : "");
      const customSel = pcEl("plan-check-select-ui") || (() => {
        const div = document.createElement("div");
        div.id = "plan-check-select-ui";
        div.className = "pc-day-select";
        sel?.parentElement?.appendChild(div);
        return div;
      })();
      const list = document.createElement("div");
      list.className = "pc-day-select-list";
      list.hidden = true;
      const display = document.createElement("div");
      display.className = "pc-day-select-display";
      customSel.innerHTML = "";
      customSel.appendChild(display);
      customSel.appendChild(list);

      function buildOptionRow(optRow) {
        const row = document.createElement("div");
        row.className = "pc-day-option";
        row.dataset.dayId = String(optRow?.day_id || "");
        const label = pcStripReassessLabel(optRow.label || "");
        if (!label.includes("→")) {
          row.textContent = label;
          return row;
        }
        const arrowIdx = label.indexOf("→");
        const left = label.slice(0, arrowIdx).trimEnd();
        const right = label.slice(arrowIdx + 1).trimStart();
        const daySepIdx = left.indexOf("·");
        if (daySepIdx >= 0) {
          const dayPrefix = left.slice(0, daySepIdx + 1).trim();
          const oldSession = left.slice(daySepIdx + 1).trim();
          const day = document.createElement("span");
          day.className = "pc-day-prefix";
          day.textContent = `${dayPrefix} `;
          row.appendChild(day);
          const muted = document.createElement("span");
          muted.className = "pc-day-muted";
          muted.textContent = `${oldSession} →`;
          row.appendChild(muted);
          row.appendChild(document.createTextNode(` ${right}`));
          return row;
        }
        const muted = document.createElement("span");
        muted.className = "pc-day-muted";
        muted.textContent = `${left}→`;
        row.appendChild(muted);
        row.appendChild(document.createTextNode(right));
        return row;
      }

    function syncActiveDayOption() {
      const activeDay = String(sel.value || "");
      list.querySelectorAll(".pc-day-option").forEach((node) => {
        if (String(node?.dataset?.dayId || "") === activeDay) node.classList.add("is-active");
        else node.classList.remove("is-active");
      });
    }

    function setDisplay(optRow) {
      const displayRow = buildOptionRow(optRow);
      display.innerHTML = "";
      display.appendChild(displayRow);
    }
    if (hasWeekDays) {
      const todayKey = String(getTodayDateUtcKey() || "");
      const hasTodayOption = nextSession.week_days.some((d) => String(d?.day_id || "") === todayKey);
      const selected = state.pcSelectedKey || (hasTodayOption ? todayKey : "") || nextSession?.selected_day_id || "";
      if (selected) sel.value = selected;
      state.pcSelectedKey = String(sel.value || selected || "");
      const selectedRow = nextSession?.week_days?.find((d) => d.day_id === sel.value) || nextSession?.week_days?.[0];
      if (selectedRow) setDisplay(selectedRow);
      list.innerHTML = "";
      for (const optRow of nextSession.week_days) {
        const optEl = buildOptionRow(optRow);
        optEl.addEventListener("click", () => {
          const dayId = String(optRow.day_id || "");
          sel.value = dayId;
          state.pcSelectedKey = dayId;
          list.hidden = true;
          setDisplay(optRow);
          syncActiveDayOption();
          loadSession(dayId);
        });
        list.appendChild(optEl);
      }
      syncActiveDayOption();
      sel.style.display = "none";
      customSel.hidden = true;
      customSel.innerHTML = "";
      list.hidden = true;
    } else if (defKey) {
      sel.value = defKey;
      state.pcSelectedKey = String(defKey || "");
    }

    async function loadSession(key) {
      if (!key) return;
      const requestedKey = String(key || "").trim();
      state.pcSelectedKey = requestedKey;
      const loadSeq = ++state.pcLoadSeq;
      const isActiveLoad = () => loadSeq === state.pcLoadSeq && _pcIsCurrentSelection(requestedKey);
      if (hasWeekDays) {
        const resp = await safeJson(`/api/next_session?day=${encodeURIComponent(requestedKey)}&refresh=1`);
        if (!isActiveLoad()) return;
        if (!_isFreshNextSessionPayload(resp) || !_pcPayloadMatchesKey(resp, requestedKey)) {
          if (weekOverviewOk) _renderPlanCheckWeekFallback(weekOverview);
          _renderPlanCheckPending("Nächste Einheit wird aktualisiert …", { preserveContent: true });
          _queueFreshNextSession(requestedKey, weekOverviewOk ? weekOverview : null);
          return;
        }
        _pcRenderSessionSafe(resp, weekOverviewOk ? weekOverview : null);
        _pcRenderReferenceLink(link, resp?.reference_workout);
        return;
      }
      const resp = await safeJson(`/api/dashboard/plan_check_for_session?session_key=${encodeURIComponent(key)}`);
      if (!isActiveLoad()) return;
      if (!_pcPayloadMatchesKey(resp, requestedKey)) return;
      _pcRenderSessionSafe(resp, weekOverviewOk ? weekOverview : null);
      _pcRenderReferenceLink(link, resp?.reference_workout);
    }

      sel.onchange = () => {
        state.pcSelectedKey = String(sel.value || "");
        if (hasWeekDays) {
          const row = nextSession?.week_days?.find((d) => String(d?.day_id || "") === String(sel.value || ""));
          if (row) setDisplay(row);
          syncActiveDayOption();
        }
        loadSession(sel.value);
      };

      if (nextSessionOk) {
        const initialKey = String(sel.value || nextSession?.selected_day_id || "");
        if (initialKey && _isFreshNextSessionPayload(nextSession) && _pcPayloadMatchesKey(nextSession, initialKey)) {
          state.pcSelectedKey = initialKey || state.pcSelectedKey;
          _pcRenderSessionSafe(nextSession, weekOverviewOk ? weekOverview : null);
          _pcRenderReferenceLink(link, nextSession?.reference_workout);
        } else if (initialKey) {
          await loadSession(initialKey);
        } else {
          state.pcSelectedKey = initialKey || state.pcSelectedKey;
          _pcRenderSessionSafe(nextSession, weekOverviewOk ? weekOverview : null);
          _pcRenderReferenceLink(link, nextSession?.reference_workout);
        }
      } else {
        await loadSession(sel.value || defKey);
      }
    } finally {
      pcInitBusy = false;
      if (state.pcInitQueued) {
        state.pcInitQueued = false;
        setTimeout(() => {
          try { pcInit(); } catch (_) {}
        }, 0);
      }
    }
  }

  async function pcInitSimpleDayDropdown() {
    const sel = pcEl("plan-check-select");
    const link = pcEl("plan-check-link");
    const customSel = pcEl("plan-check-select-ui");
    const msg = pcEl("plan-check-message");
    if (!sel || !link) return;
    const todayKey = String(getTodayDateUtcKey() || "");
    try {
      const trainingTodayCard = await fetchTrainingTodayCard(todayKey);
      if (trainingTodayCard && renderTrainingTodayCard(trainingTodayCard)) return;
    } catch (err) {
      try { console.warn("[training-card] GPT card pre-render failed", err); } catch (_) {}
    }
    const box = pcEl("plan-check-exercises");
    const staticDay = document.getElementById("plan-check-static-day");
    const modeLine = document.getElementById("plan-check-mode-line");
    const changesBox = document.getElementById("plan-check-changes-box");
    const bootstrapSession = window.__DASHBOARD_BOOTSTRAP__?.nextSession || state.dashboardSnapshot?.nextSession || null;
    const hasLastGood = !!state.trainingCardLastKnownGoodPayload;
    if (!hasLastGood && !_pcHasRenderableItems(bootstrapSession) && box) box.innerHTML = "";
    if (msg) msg.textContent = hasLastGood ? "" : "Trainingskarte wird geladen …";
    if (staticDay && !hasLastGood) staticDay.textContent = "Heute: —";
    if (modeLine && !hasLastGood) modeLine.textContent = "Lade Trainingsvorgaben…";
    if (changesBox) {
      changesBox.hidden = true;
      changesBox.innerHTML = "";
    }
    if (customSel) customSel.hidden = true;
    sel.hidden = true;

    if (_pcHasRenderableItems(bootstrapSession) && !state.trainingCardLastKnownGoodPayload) {
      _pcRenderReferenceLink(link, bootstrapSession?.reference_workout);
      _pcRenderSessionSafe(bootstrapSession, null);
    }

    try {
      const initial = await safeJson(`/api/next_session?day=${encodeURIComponent(todayKey)}&refresh=1`);
      if (!initial || initial.ok !== true) {
        if (state.trainingCardLastKnownGoodPayload || _pcHasRenderableItems(bootstrapSession)) {
          refreshCoreTrainingCard(todayKey);
          return;
        }
        if (msg) msg.textContent = "Nächste Einheit konnte gerade nicht geladen werden.";
        link.textContent = "Letzte Ausführung: —";
        link.href = "#";
        return;
      }
      _pcRenderReferenceLink(link, initial.reference_workout);
      _pcRenderSessionSafe(initial, null);
      refreshCoreTrainingCard(todayKey);
    } catch (err) {
      try { console.error("pcInitSimpleDayDropdown failed", err); } catch (_) {}
      if (!state.trainingCardLastKnownGoodPayload && !_pcHasRenderableItems(bootstrapSession)) {
        if (msg) msg.textContent = "Nächste Einheit konnte gerade nicht geladen werden.";
        link.textContent = "Letzte Ausführung: —";
        link.href = "#";
      }
      refreshCoreTrainingCard(todayKey);
    }
  }

  function pcRenderAutopilotExplain(sessionResp) {
    const explain = sessionResp?.autopilot_explain;
    if (!explain) {
      state.autopilotExplainAvailable = false;
      state.autopilotExplainPayload = null;
      if (state.coreExplainActiveKey === "training") closeCoreExplainPanel({ restoreFocus: false });
      renderCoreSteuertBadge(state.coreModeEnabled);
      return;
    }
    state.autopilotExplainAvailable = true;

    const statusLine = String(explain.status_line || explain.recovery_line || "").trim();
    const statusText = statusLine.replace(/^Status:\s*/i, "");
    const whyLines = Array.isArray(explain.why_lines) ? explain.why_lines : [];
    const signalLines = Array.isArray(explain.signal_lines) ? explain.signal_lines : [];
    const statusLabel = statusText.split("·")[0]?.trim() || "";
    const decisionLine = String(explain.decision_line || explain.today || "").trim();
    const planned = String(explain.planned || "").trim();
    const phase = String(explain.phase_line || "").trim();
    const headline = String(explain.headline || explain.change_line || "").trim();
    const polishedWhy = _coreExplainLines(whyLines, "Heute bleibt CORE bei kontrollierter Belastung.");
    const drivers = _normalizeDrivers(polishedWhy.slice(0, 3), "Treiber").map((it, idx) => ({
      ...it,
      title: it.title.startsWith("Treiber") ? (
        idx === 0 ? "Belastungssteuerung" : idx === 1 ? "Qualitätsschutz" : "Systemlage"
      ) : it.title
    }));

    state.autopilotExplainPayload = {
      title: explain.title || "CORE Training",
      status: statusLabel || "CORE steuert",
      meta: phase || "",
      verdict: decisionLine || "CORE gibt heute grün für kontrollierte Belastung.",
      subtext: headline || "Die Einheit bleibt freigegeben, weil sie die Woche strukturell stützt statt zu kippen.",
      drivers,
      allowed: [
        planned || "Umfang wie geplant",
        "Locker und sauber umsetzen",
        "Fokus auf Rhythmus statt Pace-Jagd",
      ],
      avoided: [
        "Zusätzliche Härte außerhalb des Plans",
        "Pace-Ego und unnötige Eskalation",
        statusText ? `Reaktion gegen Signal: ${statusText}` : "Schlecht getimte Qualitätsspitze",
      ],
      rationale: polishedWhy,
      signals: signalLines.length ? signalLines : [statusText || "Signal-Lage ohne harte Warnung"],
      practical: [
        planned || "Session wie geplant ausführen",
        "Intensität bewusst kontrolliert halten",
        "Als saubere Systemarbeit verbuchen",
      ],
      footer: explain.hint || "Neue Daten können das Urteil heute neu einordnen.",
    };
    if (state.coreExplainActiveKey === "training") openCoreExplainPanel("training");
    renderCoreSteuertBadge(state.coreModeEnabled);
  }

  function renderTodayCardFromSignals({ signals, planWeek, planMeta, lastLogs }) {
    if (!signals) return;

    const card = document.getElementById("today-signal-recovery")?.closest("article, .dashboard-card");
    if (!card) return;

    const headline = document.getElementById("today-headline");
    const footer   = document.getElementById("today-footer-line");

    const recHost  = document.getElementById("today-signal-recovery");
    const enHost   = document.getElementById("today-signal-energy");
    const loadHost = document.getElementById("today-signal-load");
    if (!recHost || !enHost || !loadHost) return;

    // Week UI (Label + Pill) — kommt aus /api/dashboard/today -> plan_week
    const weekLabelEl = document.getElementById("today-week-label");
    const weekPillEl  = document.getElementById("today-week-pill");

    const applyWeekPill = (info) => {
      if (!weekPillEl) return;

      // reset classes
      weekPillEl.classList.remove("is-build", "is-deload", "is-overreach");

      if (!info) {
        if (weekLabelEl) weekLabelEl.textContent = "";
        weekPillEl.textContent = "";
        weekPillEl.style.display = "none";
        return;
      }

      // label: Woche X/Y
      if (weekLabelEl) {
        const tw = (info.total_weeks != null ? info.total_weeks : "—");
        const wc = (info.week_current != null ? info.week_current : "—");
        weekLabelEl.textContent = `Woche ${wc}/${tw}`;
      }

      // pill text: BUILD/DELOAD/OVERREACH (kommt fix aus backend)
      const txt = (info.phase_label || "").trim();
      weekPillEl.textContent = txt;

      // hide if empty -> verhindert die "leere Pill"
      if (!txt) {
        weekPillEl.style.display = "none";
        return;
      }
      weekPillEl.style.display = "";

      const wt = (info.week_type || "").toLowerCase();
      if (wt === "deload") weekPillEl.classList.add("is-deload");
      else if (wt === "overreach") weekPillEl.classList.add("is-overreach");
      else weekPillEl.classList.add("is-build");
    };

    // planMeta ist dein /api/dashboard/today plan_week
    // (planWeek ist dein /api/dashboard/plan_check_week_overview Objekt mit today.*)
    applyWeekPill(planMeta || null);

    // 1) Headline
    const sessionName = planWeek?.today?.today_session_name || "—";
    if (headline) headline.textContent = sessionName;

    // 2) States
    const lanes = signals.lanes || {};
    const lastState = (arr) =>
      [...(arr || [])].reverse().find(x => x?.state && x.state !== "missing")?.state || "missing";

    const recState = lastState(lanes.recovery);
    const enState  = energyLaneStateFromMakros(lanes.energy);

    const loSeries = signals.context?.load?.series || [];
    const m3 = _meanLastN(loSeries, 3);
    const m7 = _meanLastN(loSeries, 7);
    const loState =
      Number.isFinite(m3) && Number.isFinite(m7)
        ? (m3 <= m7 ? "good" : "neutral")
        : "missing";

    const loadArrow = (Number.isFinite(m3) && Number.isFinite(m7))
      ? (m3 > m7 ? "↑" : "↓")
      : "→";

    // helper: meta sauber “idempotent” setzen
    const applyMeta = (tile, label, vs, stateRaw, arrowChar) => {
      if (!tile) return;

      // ALLE bestehenden Meta-Zeilen killen (Bugfix)
      tile.querySelectorAll(".today-tile-meta").forEach(n => n.remove());

      // alten Header aus dem HTML ausblenden (damit nix doppelt ist)
      const legacyLabel = tile.querySelector(".today-tile-label");
      if (legacyLabel) legacyLabel.classList.add("is-hidden");

      // state fürs CSS (Border/Arrow-Farbe)
      const norm = normalizeSignalState(stateRaw);
      tile.setAttribute("data-state", norm);

      // neue Meta oben rein
      tile.insertAdjacentHTML("afterbegin", _tileMeta(label, vs, stateRaw, arrowChar));
    };

    const recTile = recHost.closest(".today-tile");
    const enTile  = enHost.closest(".today-tile");
    const loTile  = loadHost.closest(".today-tile");

    applyMeta(recTile, "RECOVERY", "14T", recState, null);
    applyMeta(enTile,  "ENERGY",   "13T", enState, null);
    applyMeta(loTile,  "LOAD",     "14T", loState, loadArrow);

    // 3) Sparklines nur in die Body-Hosts (nicht Tile überschreiben)
    recHost.innerHTML  = _sparkSvg(signals.context?.recovery?.series || [], "is-recovery");
    const energySeriesNoToday = (() => {
      const src = Array.isArray(signals.context?.energy?.series) ? signals.context.energy.series : [];
      return src.length > 1 ? src.slice(0, -1) : src;
    })();
    enHost.innerHTML   = _sparkSvg(energySeriesNoToday, "is-energy");
    loadHost.innerHTML = _sparkSvg(loSeries || [], "is-load");

    // 4) Footer
    if (footer) {
      const last = lastLogs?.training;
      const hasLast = Boolean(last && (last.date_iso || last.name));
      const lastTxt = last?.date_iso
        ? `${last.name} (${last.date_iso.slice(8,10)}.${last.date_iso.slice(5,7)}.)`
        : (last?.name || "—");
      if (hasLast) {
        state.todayFooterLastLabel = lastTxt;
      }
      const label = state.todayFooterLastLabel || lastTxt;

      const next = planWeek?.today?.default_session_name || "—";
      const wd = planWeek?.today?.planned_weekday || "";

      const footerText = `Letzte Einheit: ${label} · Nächste Einheit: ${next}${wd ? ` (${wd})` : ""}`;
      if (state.todayFooterText !== footerText) {
        footer.textContent = footerText;
        state.todayFooterText = footerText;
      }
    }
  }

  function todayTile(label, vs, state, series, cls) {
    const norm = normalizeSignalState(state);
    return `
      <div class="today-tile ${cls}">
        <div class="today-tile-head">
          <div class="today-tile-left">
            <span class="signal-cell today-dot is-${state} is-${norm}"></span>
            <span class="today-tile-label">${label}</span>
          </div>
          <div class="today-tile-vs">${vs}</div>
        </div>
        ${_sparkSvg(series || [], cls)}
      </div>
    `;
  }

function setSignals(data) {
  const s = data.signals14;
  if (!s || !Array.isArray(s.days) || !s.lanes) return;

  // --- local helper: render 14-day squares ---
  function _renderSignalRow(container, laneItems, titlePrefix, laneKey) {
    if (!container) return;
    container.innerHTML = "";

    const frag = document.createDocumentFragment();
    const arr = Array.isArray(laneItems) ? laneItems : [];

    for (let i = 0; i < arr.length; i++) {
      const it = arr[i] || {};
      const dateIso = Array.isArray(s.days) ? s.days[i] : "";
      const dateLabel = /^\d{4}-\d{2}-\d{2}$/.test(String(dateIso || ""))
        ? `${String(dateIso).slice(8, 10)}.${String(dateIso).slice(5, 7)}`
        : "";
      let raw = String(it.state || "missing");
      if (laneKey === "energy" && (!raw || raw === "missing")) {
        const kcal = it.kcal_intake ?? it.value ?? null;
        const computed = energyStateFromMakros(kcal);
        if (computed) raw = computed;
      }
      const norm = normalizeSignalState(raw);

      const span = document.createElement("span");
      span.className = `signal-cell is-${raw} is-${norm}`;
      span.dataset.state = raw;
      span.dataset.status = norm;

      const recoveryValue = it.recoveryScore ?? it.score ?? it.value ?? null;
      const genericValue = it.value ?? it.rmssd ?? it.kcal_intake ?? null;
      if (laneKey === "recovery" && isFiniteNum(recoveryValue)) {
        span.title = `${dateLabel ? `${dateLabel} · ` : ""}${titlePrefix}: ${Number(recoveryValue).toFixed(0)}`;
      } else if (laneKey === "energy" && isFiniteNum(genericValue)) {
        const target = isFiniteNum(it.kcal_target) ? Number(it.kcal_target) : getMakrosModeConfig()?.kcal_target;
        const tt = isFiniteNum(target) ? ` (Δ ${formatSigned(Number(genericValue) - Number(target), 0)})` : "";
        span.title = `${dateLabel ? `${dateLabel} · ` : ""}${titlePrefix}: ${Number(genericValue).toFixed(0)}${tt}`;
      } else {
        span.title = (genericValue !== null && genericValue !== undefined && Number.isFinite(Number(genericValue)))
          ? `${dateLabel ? `${dateLabel} · ` : ""}${titlePrefix}: ${Number(genericValue).toFixed(0)}`
          : `${dateLabel ? `${dateLabel} · ` : ""}${titlePrefix}: ${raw}`;
      }

      frag.appendChild(span);
    }

    container.appendChild(frag);
  }

  // Derive weekday labels from the same ISO dates as the cells. A separate
  // weekdays array can become stale or shift independently from the data.
  function _renderDayLabels(container, signals) {
    if (!container) return;

    const days = signals?.days || [];

    container.innerHTML = "";
    const frag = document.createDocumentFragment();

    // spacer for left label column
    const spacer = document.createElement("span");
    spacer.className = "signal-day-spacer";
    spacer.textContent = "";
    frag.appendChild(spacer);

    const dayGrid = document.createElement("span");
    dayGrid.className = "signal-day-grid";

    for (let i = 0; i < days.length; i++) {
      const el = document.createElement("span");
      el.className = "signal-day";
      el.textContent = weekdayShortLabel(days[i]);
      el.title = weekdayLongLabel(days[i]);
      dayGrid.appendChild(el);
    }

    frag.appendChild(dayGrid);
    container.appendChild(frag);
  }

  const lanes = s.lanes;

  _renderSignalRow(els.signalRecovery, lanes.recovery || [], "Recovery", "recovery");
  _renderSignalRow(els.signalEnergy,   lanes.energy   || [], "Energy", "energy");
  _renderSignalRow(els.signalRun,      lanes.run_load || lanes.runs || [], "Cardio", "run_load");
  _renderSignalRow(els.signalStrength, lanes.strength || [], "Strength", "strength");

  _renderDayLabels(els.signalDayLabels, s);
  app.style.setProperty("--signal-columns", String(s.days.length));
}




  // =========================================================
  // TODAY SPARKLINES (Recovery/Energy/Load) aus signals.context.series
  // =========================================================
  function sparklineSvg(series, opts = {}) {
    const w = 220, h = 70, padX = 6, padTop = 10, padBottom = 7;
    const clean = series.map((v) => (v === null || v === undefined || !Number.isFinite(Number(v)) ? null : Number(v)));

    // need at least 2 points with values
    const vals = clean.filter((v) => v !== null);
    if (vals.length < 2) return `<div class="today-sparkline-empty">${DASH}</div>`;

    const min = Math.min(...vals);
    const max = Math.max(...vals);
    const span = (max - min) || 1;

    const n = clean.length;
    const dx = (w - padX * 2) / Math.max(1, (n - 1));
    const usableH = Math.max(1, h - padTop - padBottom);

    let d = "";
    let started = false;

    for (let i = 0; i < n; i++) {
      const v = clean[i];
      if (v === null) continue;
      const x = padX + i * dx;
      const y = padTop + usableH * (1 - (v - min) / span);
      if (!started) {
        d += `M ${x.toFixed(2)} ${y.toFixed(2)}`;
        started = true;
      } else {
        d += ` L ${x.toFixed(2)} ${y.toFixed(2)}`;
      }
    }

    const cls = opts.pathClass || "today-large-sparkline-path";
    return `
      <svg class="today-large-sparkline" viewBox="0 0 ${w} ${h}" preserveAspectRatio="none" aria-hidden="true">
        <path class="${cls}" d="${d}" fill="none"></path>
      </svg>
    `;
  }

  function setTodaySparklines(data) {
    const s = data.signals14;
    if (!s || !s.context) return;

    // Container lane classes (damit CSS Farben setzen kann, falls vorhanden)
    if (els.todayRecovery) els.todayRecovery.classList.add("is-recovery");
    if (els.todayEnergy) els.todayEnergy.classList.add("is-energy");
    if (els.todayLoad) els.todayLoad.classList.add("is-load");

    const recSeries = s.context?.recovery?.series || [];
    const enSeriesRaw = s.context?.energy?.series || [];
    const enSeries = Array.isArray(enSeriesRaw) && enSeriesRaw.length > 1 ? enSeriesRaw.slice(0, -1) : enSeriesRaw;
    const loadSeries = s.context?.load?.series || [];

    if (els.todayRecovery) setHtml(els.todayRecovery, sparklineSvg(recSeries));
    if (els.todayEnergy) setHtml(els.todayEnergy, sparklineSvg(enSeries));
    if (els.todayLoad) setHtml(els.todayLoad, sparklineSvg(loadSeries));
  }

  // =========================================================
  // KIENZL
  // =========================================================
  function _fmtNum(n, decimals = 1) {
    if (n === null || n === undefined || !Number.isFinite(Number(n))) return DASH;
    return Number(n).toLocaleString(LOCALE, { maximumFractionDigits: decimals });
  }

  function _fmtPct(ratio, decimals = 0) {
    if (ratio === null || ratio === undefined || !Number.isFinite(Number(ratio))) return DASH;
    return `${Math.round(Number(ratio) * 100)}%`;
  }

  function _humanLabel(key) {
    const m = {
      rmssd_today: "RMSSD (heute)",
      rmssd_baseline: "RMSSD Baseline",
      rmssd_3d: "RMSSD Ø 3 Tage",
      rmssd_ratio: "RMSSD vs Baseline",
      rmssd_3d_ratio: "RMSSD 3T vs Baseline",
      hr_today: "Ruhepuls (heute)",
      hr_baseline: "Ruhepuls Baseline",
      hr_delta: "Ruhepuls Δ",
      sleep_today: "Schlafqualität (heute)",
      sleep_baseline: "Schlafqualität Baseline",
      sleep_delta: "Schlafqualität Δ",
      wakeup_avg: "Aufstehzeit Ø",
      wakeup_variance_min: "Aufstehzeit Streuung",
      ready_today: "Readiness (heute)",
      ready_baseline: "Readiness Baseline",
      sickness: "Krankheit markiert",

      kcal_today: "Kalorien (heute)",
      kcal_avg_7d: "Kalorien Ø 7 Tage",
      kcal_avg_prev7d: "Kalorien Ø Vorwoche",
      kcal_diff_7d: "Kalorien Δ zur Vorwoche",
      kcal_std_7d: "Kalorien Schwankung (7T)",
      protein_today: "Protein (heute)",
      protein_avg_7d: "Protein Ø 7 Tage",
      protein_low_thr: "Protein Minimum",
      sugar_today: "Zucker (heute)",
      sugar_avg_7d: "Zucker Ø 7 Tage",
      weight_today: "Gewicht (heute)",

      weight_start: "Gewicht Start",
      weight_end: "Gewicht Ende",
      trend_delta: "Gewicht Trend (14T)",
      noise_std: "Gewicht Schwankung",
      active_mode: "Ernährungsmodus",

      planned_7d: "Geplant (7T)",
      done_7d: "Gemacht (7T)",
      missed_7d: "Verpasst (7T)",
      active_plans: "Aktive Pläne",
      override: "Overrides aktiv",

      tonnage_14d: "Tonnage (14T)",
      tonnage_prev14d: "Tonnage Vorperiode",
      tonnage_pct: "Tonnage Δ",

      eff_14d: "Lauf-Effizienz (14T)",
      eff_prev14d: "Lauf-Effizienz Vorperiode",
      eff_pct: "Lauf-Effizienz Δ"
    };
    return m[key] || key.replace(/_/g, " ");
  }

  function _humanValue(key, v) {
    if (v === null || v === undefined) return DASH;
    const modeMap = { cut: "Cut", lean_bulk: "Lean Bulk", maintenance: "Maintenance", custom: "Custom" };
    if (key === "active_mode") return modeMap[String(v)] || String(v);
    if (key === "override") return v ? "ja" : "nein";
    if (key === "sickness") return (Number(v) ? "ja" : "nein");
    if (key === "wakeup_avg") return String(v);
    if (key === "wakeup_variance_min") return `${_fmtNum(v, 0)} min`;

    if (key.includes("rmssd")) return `${_fmtNum(v, 1)} ms`;
    if (key.startsWith("hr_") || key === "hr_delta") return `${_fmtNum(v, 1)} bpm`;
    if (key.includes("sleep")) return _fmtNum(v, 1);
    if (key.includes("ready")) return _fmtNum(v, 0);
    if (key.startsWith("kcal")) return `${_fmtNum(v, 0)} kcal`;
    if (key.includes("protein")) return `${_fmtNum(v, 0)} g`;
    if (key.includes("sugar")) return `${_fmtNum(v, 0)} g`;
    if (key.includes("weight")) return `${_fmtNum(v, 1)} kg`;
    if (key.endsWith("_ratio")) return _fmtPct(v, 0);
    if (key.endsWith("_pct")) return _fmtPct(v, 0);
    if (key === "noise_std") return `${_fmtNum(v, 1)} kg`;
    if (key === "trend_delta") return `${_fmtNum(v, 1)} kg`;
    if (key === "tonnage_14d" || key === "tonnage_prev14d") return `${_fmtNum(v, 0)} kg`;
    if (key === "tonnage_pct") return _fmtPct(v, 0);
    if (key === "eff_pct") return _fmtPct(v, 0);
    if (typeof v === "boolean") return v ? "ja" : "nein";
    if (typeof v === "number") return _fmtNum(v, 1);
    return String(v);
  }

  function _buildEvidenceRows(item) {
    const ev = item?.evidence && typeof item.evidence === "object" ? item.evidence : {};
    const keys = Object.keys(ev || {});
    if (!keys.length) return [];

    // keep only known keys first, then a few unknown (but readable)
    const knownOrder = [
      "sickness",
      "rmssd_today", "rmssd_baseline", "rmssd_3d", "rmssd_ratio", "rmssd_3d_ratio",
      "hr_today", "hr_baseline", "hr_delta",
      "sleep_today", "sleep_baseline", "sleep_delta",
      "wakeup_avg", "wakeup_variance_min",
      "ready_today", "ready_baseline",
      "kcal_today", "kcal_avg_7d", "kcal_avg_prev7d", "kcal_diff_7d", "kcal_std_7d",
      "protein_today", "protein_avg_7d", "protein_low_thr",
      "sugar_today", "sugar_avg_7d",
      "weight_today", "weight_start", "weight_end", "trend_delta", "noise_std", "active_mode",
      "planned_7d", "done_7d", "missed_7d", "active_plans", "override",
      "tonnage_14d", "tonnage_prev14d", "tonnage_pct",
      "eff_14d", "eff_prev14d", "eff_pct"
    ];
    const known = knownOrder.filter((k) => keys.includes(k));
    const unknown = keys.filter((k) => !known.includes(k)).slice(0, 3);
    const use = [...known, ...unknown].slice(0, 10);

    return use.map((k) => [_humanLabel(k), _humanValue(k, ev[k])]);
  }

  function renderTodayAgendaCard(payload) {
    if (!els.todayAgendaCard || !els.todayAgendaBody) return;
    const agenda = payload && typeof payload === "object" ? payload : {};
    const items = Array.isArray(agenda.items) ? agenda.items : [];
    const renderKey = JSON.stringify(items.map((item) => [
      item?.kind || "",
      item?.title || "",
      item?.subtitle || "",
      item?.time_label || "",
    ]));
    if (state.todayAgendaRenderKey && state.todayAgendaRenderKey === renderKey) return;
    state.todayAgendaRenderKey = renderKey;
    setText(els.todayAgendaDayMeta, agenda?.day ? (formatDateDe(agenda.day, { weekday: true, short: true }) || agenda.day) : DASH);
    setText(els.todayAgendaCountPill, String(items.length));
    if (!items.length) {
      setHtml(els.todayAgendaBody, `<div class="today-agenda-empty">Heute sind keine Einträge vorhanden.</div>`);
      return;
    }
    const stripEmails = (value) => String(value || "").replace(/\b[A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,}\b/gi, "").replace(/[ ·,\-]+$/g, "").trim();
    const html = items.map((item) => {
      const kind = escapeHtml(String(item?.kind || "other"));
      const title = escapeHtml(String(item?.title || "Eintrag"));
      const subtitle = escapeHtml(stripEmails(String(item?.subtitle || "").trim()));
      const time = escapeHtml(String(item?.time_label || "Offen"));
      const source = escapeHtml(String(item?.source_label || "Termin"));
      return `
        <div class="today-agenda-item" data-kind="${kind}">
          <div class="today-agenda-time">${time}</div>
          <div class="today-agenda-item-body">
            <div class="today-agenda-item-head">
              <div class="today-agenda-item-title">${title}</div>
              <div class="today-agenda-item-source">${source}</div>
            </div>
            ${subtitle ? `<div class="today-agenda-item-subtitle">${subtitle}</div>` : ""}
          </div>
        </div>
      `;
    }).join("");
    setHtml(els.todayAgendaBody, html);
  }

  // =========================================================
  // INIT / EVENTS
  // =========================================================
  function loadPrefs() {
    // auto insights
    const ai = localStorage.getItem(STORAGE.autoInsights);
    if (ai !== null) state.autoInsights = ai === "1";
    state.enduranceMode = _loadEnduranceMode();

    if (els.autoInsights) els.autoInsights.checked = state.autoInsights;

  }

  function bindEvents() {
    if (app && !app.dataset.navGuardBound) {
      app.dataset.navGuardBound = "1";
      app.addEventListener("click", (ev) => {
        const anchor = ev.target?.closest?.("a[href]");
        if (!anchor || !app.contains(anchor)) return;
        if (anchor.target === "_blank" || ev.metaKey || ev.ctrlKey || ev.shiftKey || ev.altKey) return;
        const rawHref = String(anchor.getAttribute("href") || "").trim();
        if (!rawHref || rawHref === "#" || rawHref.startsWith("javascript:")) return;

        let targetUrl = null;
        try {
          targetUrl = new URL(anchor.href, window.location.href);
        } catch (_) {
          return;
        }
        if (targetUrl.origin !== window.location.origin) return;
        if (targetUrl.pathname === window.location.pathname && targetUrl.search === window.location.search && targetUrl.hash) return;

        ev.preventDefault();
        ev.stopPropagation();
        window.location.assign(targetUrl.href);
      }, true);
    }

    // Auto insights toggle
    if (els.autoInsights) {
      els.autoInsights.addEventListener("change", (e) => {
        state.autoInsights = !!e.target.checked;
        localStorage.setItem(STORAGE.autoInsights, state.autoInsights ? "1" : "0");
        if (state.lastData) setFocus(state.lastData);
      });
    }
    window.addEventListener("liva:block-status:invalidate", () => {
      state.blockStatusDirty = true;
      state.autopilotMemoryDirty = true;
      state.coreDecisionCardDirty = true;
    });
    window.addEventListener("coreDecisionChanged", (ev) => {
      const day = String(ev?.detail?.day || "").trim();
      if (day) {
        _refreshNextSessionForDay(day).catch(() => {});
      }
      setTimeout(() => {
        try { pcInit(); } catch (_) {}
      }, 0);
    });

    if (els.coreDecisionModeSegment) {
      els.coreDecisionModeSegment.addEventListener("click", (ev) => {
        const btn = ev.target?.closest?.("[data-knob=\"mode\"]");
        if (!btn) return;
        const value = String(btn.getAttribute("data-value") || "").trim().toUpperCase();
        if (!value) return;
        state.coreDecisionPendingMode = value;
        renderCoreDecisionCard(state.coreDecisionCardCache || { _error: true });
      });
    }

    if (els.coreDecisionSecondarySegment) {
      els.coreDecisionSecondarySegment.addEventListener("click", (ev) => {
        const btn = ev.target?.closest?.("[data-knob=\"secondary\"]");
        if (!btn) return;
        const value = String(btn.getAttribute("data-value") || "").trim().toLowerCase();
        if (!value) return;
        state.coreDecisionPendingSecondary = value;
        const sec = state.coreDecisionCardCache?.knobs?.secondary || {};
        state.coreDecisionPendingSecondaryType = String(sec.type || "").trim();
        renderCoreDecisionCard(state.coreDecisionCardCache || { _error: true });
      });
    }

    if (els.coreDecisionApply) {
      els.coreDecisionApply.addEventListener("click", async () => {
        await saveCoreDecisionOverride();
      });
    }

    if (els.mealplanCorePill) {
      els.mealplanCorePill.addEventListener("click", (ev) => {
        ev.preventDefault();
        ev.stopPropagation();
        openCoreExplainPanel("mealplan");
      });
    }
    if (els.enduranceBody) {
      els.enduranceBody.addEventListener("click", async (ev) => {
        const btn = ev.target?.closest?.("[data-endurance-mode]");
        if (!btn || !state.endurancePayload) return;
        const mode = String(btn.getAttribute("data-endurance-mode") || "").trim().toLowerCase();
        if (mode !== "laufen" && mode !== "ergo") return;
        await saveEnduranceMode(mode, state.endurancePayload);
      });
    }
    if (els.enduranceBody) {
      els.enduranceBody.addEventListener("click", (ev) => {
        const btn = ev.target?.closest?.("[data-endurance-mode]");
        if (!btn) return;
        const mode = String(btn.getAttribute("data-endurance-mode") || "").trim().toLowerCase();
        if (mode !== "laufen" && mode !== "ergo") return;
        _saveEnduranceMode(mode);
        if (state.endurancePayload) renderEnduranceData(state.endurancePayload);
      });
    }

    if (els.coreStatusTimeline) {
      els.coreStatusTimeline.addEventListener("click", async (ev) => {
        // Prevent the global document click handler from immediately collapsing
        // the card after we re-render the timeline on the same click.
        ev.stopPropagation();
        const modeBtn = ev.target?.closest?.("[data-core-day-control]");
        if (modeBtn) {
          ev.preventDefault();
          const wrap = modeBtn.closest(".core-status-today-controls[data-core-session-id]");
          const sid = String(wrap?.getAttribute?.("data-core-session-id") || "");
          const target = String(wrap?.getAttribute?.("data-core-target") || "").toLowerCase();
          const mode = String(modeBtn.getAttribute("data-core-day-control") || "").toUpperCase();
          const current = String(wrap?.getAttribute?.("data-core-current-value") || "").toUpperCase();
          if (!sid || !target || !mode || mode === current) return;
          const confirmLabel = (target === "gym_mode" && mode === "PUSH") ? "HEAVY" : mode;
          if (!window.confirm(`Wirklich auf ${confirmLabel} wechseln?`)) return;
          _coreStatusSendOverride(sid, target, mode, false, { fast: true });
          return;
        }
        if (ev.target?.closest?.(".core-status-today-controls")) return;
        const dayEl = ev.target?.closest?.(".core-status-day[data-core-day-key]");
        if (dayEl) {
          const dayKey = String(dayEl.getAttribute("data-core-day-key") || "");
          const dayRelation = String(dayEl.getAttribute("data-core-day-relation") || "").toLowerCase();
          if (dayRelation !== "past" && dayKey) {
            const prevActive = String(state.coreStatusActiveDayKey || "");
            const nextActive = prevActive === dayKey ? null : dayKey;
            state.coreStatusActiveDayKey = nextActive;
            renderCoreDecisionCard(state.coreDecisionCardCache || { _error: true });
            if (nextActive) void _refreshNextSessionForDay(dayKey);
          }
          const insideChip = !!ev.target?.closest?.("[data-core-session-id]");
          if (!insideChip) return;
        }
        const chip = ev.target?.closest?.("[data-core-session-id]");
        if (!chip) return;
        const sid = String(chip.getAttribute("data-core-session-id") || "");
        if (!sid) return;
        const dayWrap = chip.closest(".core-status-day");
        const dayRelation = String(dayWrap?.getAttribute?.("data-core-day-relation") || "").toLowerCase();
        const chipType = String(chip.getAttribute("data-core-type") || "").toLowerCase();
        const dayKey = String(dayWrap?.getAttribute?.("data-core-day-key") || "");
        if (chipType === "gym" && dayRelation !== "past" && dayKey) {
          if (state.coreStatusActiveDayKey !== dayKey) {
            state.coreStatusActiveDayKey = dayKey;
            renderCoreDecisionCard(state.coreDecisionCardCache || { _error: true });
          }
          void _refreshNextSessionForDay(dayKey);
          return;
        }
        const timeline = Array.isArray(state.coreDecisionCardCache?.timeline?.days) ? state.coreDecisionCardCache.timeline.days : [];
        let item = null;
        for (const day of timeline) {
          const items = Array.isArray(day?.items) ? day.items : [];
          const found = items.find((x) => String(x?.session_id || "") === sid);
          if (found) { item = found; break; }
        }
        if (!item) return;
        _coreStatusOpenPopover(chip, item);
      });
    }

    document.addEventListener("click", (ev) => {
      if (!state.coreStatusPopover) return;
      const pop = state.coreStatusPopover;
      if (pop.contains(ev.target)) return;
      if (ev.target?.closest?.("[data-core-session-id]")) return;
      _coreStatusClosePopover();
    });

    document.addEventListener("click", (ev) => {
      if (!state.coreStatusActiveDayKey) return;
      if (ev.target?.closest?.("#core-status-timeline")) return;
      state.coreStatusActiveDayKey = null;
      renderCoreDecisionCard(state.coreDecisionCardCache || { _error: true });
    });

    if (els.autopilotMemoryFilters) {
      els.autopilotMemoryFilters.addEventListener("click", (ev) => {
        const btn = ev.target?.closest?.("[data-memory-filter]");
        if (!btn) return;
        const next = String(btn.getAttribute("data-memory-filter") || "all").toLowerCase();
        if (!next || state.autopilotMemoryFilter === next) return;
        state.autopilotMemoryFilter = next;
        els.autopilotMemoryFilters.querySelectorAll("[data-memory-filter]").forEach((chip) => {
          chip.classList.toggle("is-active", String(chip.getAttribute("data-memory-filter")) === next);
        });
        renderAutopilotMemory(state.autopilotMemoryCache || { learnings: MEMORY_MOCK, window_days: AUTOPILOT_MEMORY_DAYS });
      });
    }

    if (els.autopilotMemoryList) {
      els.autopilotMemoryList.addEventListener("click", (ev) => {
        const rowEl = ev.target?.closest?.("[data-learning-id]");
        if (!rowEl) return;
        const id = String(rowEl.getAttribute("data-learning-id") || "");
        if (!id) return;
        _openMemoryDrawerById(id);
      });
    }

    if (els.autopilotMemoryShowAll) {
      els.autopilotMemoryShowAll.addEventListener("click", () => {
        const first = (state.autopilotMemoryAllRows || [])[0];
        if (first?.id) _openMemoryDrawerById(first.id);
      });
    }

    if (els.autopilotMemoryDrawerClose) {
      els.autopilotMemoryDrawerClose.addEventListener("click", () => _closeMemoryDrawer());
    }

    if (els.autopilotMemoryDrawerList) {
      els.autopilotMemoryDrawerList.addEventListener("click", (ev) => {
        const item = ev.target?.closest?.("[data-learning-id]");
        if (!item) return;
        const id = String(item.getAttribute("data-learning-id") || "");
        if (!id) return;
        _openMemoryDrawerById(id);
      });
    }

    const autopilotBadge = byId("autopilot-badge");
    if (autopilotBadge) {
      autopilotBadge.addEventListener("click", (ev) => {
        ev.preventDefault();
        ev.stopPropagation();
        openCoreExplainPanel("training");
      });
    }

    if (els.coreExplainSheetBackdrop) {
      els.coreExplainSheetBackdrop.addEventListener("click", () => closeCoreExplainPanel());
    }

    window.addEventListener("keydown", (ev) => {
      if (ev.key === "Escape" && state.coreExplainActiveKey) {
        closeCoreExplainPanel();
      }
    });

    window.addEventListener("resize", () => {
      if (state.coreExplainActiveKey) {
        const key = state.coreExplainActiveKey;
        closeCoreExplainPanel({ restoreFocus: false });
        openCoreExplainPanel(key);
      }
    });

    window.addEventListener("th:mobile", () => {
      applyDashboardLayout();
      if (state.coreExplainActiveKey) {
        const key = state.coreExplainActiveKey;
        closeCoreExplainPanel({ restoreFocus: false });
        openCoreExplainPanel(key);
      }
    });
    if (mobileMq.addEventListener) {
      mobileMq.addEventListener("change", () => {
        applyDashboardLayout();
        if (state.coreExplainActiveKey) {
          const key = state.coreExplainActiveKey;
          closeCoreExplainPanel({ restoreFocus: false });
          openCoreExplainPanel(key);
        }
      });
    } else if (mobileMq.addListener) {
      mobileMq.addListener(() => {
        applyDashboardLayout();
        if (state.coreExplainActiveKey) {
          const key = state.coreExplainActiveKey;
          closeCoreExplainPanel({ restoreFocus: false });
          openCoreExplainPanel(key);
        }
      });
    }

    if (els.calendarPrev) {
      els.calendarPrev.addEventListener("click", async () => {
        state.calendarMonth = shiftMonth(state.calendarMonth || currentMonthIsoBerlin(), -1);
        await renderCalendarMonth(state.calendarMonth, true);
      });
    }
    if (els.calendarNext) {
      els.calendarNext.addEventListener("click", async () => {
        state.calendarMonth = shiftMonth(state.calendarMonth || currentMonthIsoBerlin(), 1);
        await renderCalendarMonth(state.calendarMonth, true);
      });
    }
    if (els.calendarExpand) {
      els.calendarExpand.addEventListener("click", (ev) => {
        ev.stopPropagation();
        openCalendarModal();
      });
    }
    if (els.calendarCard) {
      els.calendarCard.addEventListener("click", (ev) => {
        const target = ev.target;
        if (target?.closest?.(".calendar-nav-btn, .calendar-expand-btn, .calendar-cell, .calendar-strip-day")) return;
        openCalendarModal();
      });
    }
    if (els.calendarModal) {
      els.calendarModal.addEventListener("click", (ev) => {
        const closeEl = ev.target?.closest?.("[data-calendar-close=\"true\"]");
        if (closeEl) closeCalendarModal();
      });
    }
    if (els.calendarModalPrev) {
      els.calendarModalPrev.addEventListener("click", async () => {
        state.calendarMonth = shiftMonth(state.calendarMonth || currentMonthIsoBerlin(), -1);
        await renderCalendarMonth(state.calendarMonth, true);
        await renderCalendarExplorer(true);
      });
    }
    if (els.calendarModalNext) {
      els.calendarModalNext.addEventListener("click", async () => {
        state.calendarMonth = shiftMonth(state.calendarMonth || currentMonthIsoBerlin(), 1);
        await renderCalendarMonth(state.calendarMonth, true);
        await renderCalendarExplorer(true);
      });
    }
    if (els.calendarModalTodayBtn) {
      els.calendarModalTodayBtn.addEventListener("click", async () => {
        const todayIso = getTodayDateUtcKey();
        if (!todayIso) return;
        state.calendarMonth = todayIso.slice(0, 7);
        state.calendarSelectedDate = todayIso;
        await renderCalendarMonth(state.calendarMonth, true);
        await renderCalendarExplorer(true);
        await selectCalendarDate(todayIso, true);
      });
    }
    if (els.calendarRangeToggle) {
      els.calendarRangeToggle.addEventListener("click", async (ev) => {
        const btn = ev.target?.closest?.("[data-range]");
        if (!btn) return;
        const days = Number(btn.getAttribute("data-range"));
        if (!Number.isFinite(days) || days < 7) return;
        state.calendarRangeDays = days;
        els.calendarRangeToggle.querySelectorAll(".calendar-mode-btn").forEach((b) => {
          b.classList.toggle("is-active", b === btn);
        });
        await renderCalendarExplorer(true);
      });
    }
    if (els.calendarProblemsToggle) {
      els.calendarProblemsToggle.addEventListener("click", async () => {
        state.calendarShowProblemsOnly = !state.calendarShowProblemsOnly;
        els.calendarProblemsToggle.classList.toggle("is-active", state.calendarShowProblemsOnly);
        await renderCalendarExplorer(false);
      });
    }
    if (els.calendarGotoInputInline) {
      els.calendarGotoInputInline.addEventListener("keydown", async (ev) => {
        if (ev.key !== "Enter") return;
        const parsed = parseGoToDate(els.calendarGotoInputInline.value);
        if (!parsed) return;
        state.calendarSelectedDate = parsed;
        state.calendarMonth = parsed.slice(0, 7);
        await renderCalendarMonth(state.calendarMonth, true);
        await renderCalendarExplorer(true);
        await selectCalendarDate(parsed, true);
      });
    }
    if (els.calendarModeToggle) {
      els.calendarModeToggle.addEventListener("click", async (ev) => {
        const btn = ev.target?.closest?.("[data-mode]");
        if (!btn) return;
        const mode = String(btn.getAttribute("data-mode") || "both").toLowerCase();
        if (!["both", "plan", "actual"].includes(mode)) return;
        if (state.calendarMode === mode) return;
        state.calendarMode = mode;
        els.calendarModeToggle.querySelectorAll(".calendar-mode-btn").forEach((b) => {
          b.classList.toggle("is-active", b === btn);
        });
        await renderCalendarMonth(state.calendarMonth || currentMonthIsoBerlin(), true);
        await renderCalendarExplorer(true);
      });
    }
    if (els.calendarViewToggle) {
      els.calendarViewToggle.addEventListener("click", async (ev) => {
        const btn = ev.target?.closest?.("[data-view]");
        if (!btn) return;
        const view = String(btn.getAttribute("data-view") || "").toLowerCase();
        if (!["month", "timeline", "week", "year"].includes(view)) return;
        state.calendarView = view;
        els.calendarViewToggle.querySelectorAll(".calendar-mode-btn").forEach((b) => {
          b.classList.toggle("is-active", b === btn);
        });
        await renderCalendarExplorer(true);
      });
    }
    if (els.calendarFocusToggle) {
      els.calendarFocusToggle.addEventListener("click", async (ev) => {
        const btn = ev.target?.closest?.("[data-focus]");
        if (!btn) return;
        const focus = String(btn.getAttribute("data-focus") || "").toLowerCase();
        if (!["types", "load", "recovery", "performance"].includes(focus)) return;
        state.calendarFocus = focus;
        els.calendarFocusToggle.querySelectorAll(".calendar-mode-btn").forEach((b) => {
          b.classList.toggle("is-active", b === btn);
        });
        await renderCalendarExplorer(false);
      });
    }
    if (els.calendarModalMonthSelect) {
      els.calendarModalMonthSelect.addEventListener("change", async () => {
        const year = String(els.calendarModalYearSelect?.value || "").trim();
        const month = String(els.calendarModalMonthSelect.value || "").trim();
        if (!/^\d{4}$/.test(year) || !/^\d{2}$/.test(month)) return;
        state.calendarMonth = `${year}-${month}`;
        await renderCalendarMonth(state.calendarMonth, true);
      });
    }
    if (els.calendarModalYearSelect) {
      els.calendarModalYearSelect.addEventListener("change", async () => {
        const year = String(els.calendarModalYearSelect?.value || "").trim();
        const month = String(els.calendarModalMonthSelect?.value || "").trim() || "01";
        if (!/^\d{4}$/.test(year) || !/^\d{2}$/.test(month)) return;
        state.calendarMonth = `${year}-${month}`;
        await renderCalendarMonth(state.calendarMonth, true);
      });
    }
    if (els.calendarBlocksToggle) {
      els.calendarBlocksToggle.addEventListener("click", () => {
        state.calendarShowBlocks = !state.calendarShowBlocks;
        els.calendarBlocksToggle.classList.toggle("is-active", state.calendarShowBlocks);
        if (state.calendarPayload) {
          renderCalendarGrid(els.calendarMiniGrid, state.calendarPayload, {
            selectedDate: state.calendarSelectedDate,
            onPick: (cell) => selectCalendarDate(cell?.date, false),
            showBlocks: state.calendarShowBlocks,
          });
          if (els.calendarModal?.getAttribute("aria-hidden") === "false") {
            renderCalendarExplorer(false);
          }
        }
      });
    }
    if (els.calendarYearTiles) {
      els.calendarYearTiles.addEventListener("click", async (ev) => {
        const btn = ev.target?.closest?.("[data-month-jump]");
        if (!btn) return;
        const monthIso = String(btn.getAttribute("data-month-jump") || "").trim();
        if (!/^\d{4}-\d{2}$/.test(monthIso)) return;
        state.calendarMonth = monthIso;
        updateCalendarModalSelectors(state.calendarMonth);
        await renderCalendarMonth(state.calendarMonth, true);
        await renderCalendarExplorer(true);
      });
    }
    if (els.calendarNavYearPrev) {
      els.calendarNavYearPrev.addEventListener("click", async () => {
        state.calendarNavYear = Number(state.calendarNavYear || new Date().getUTCFullYear()) - 1;
        renderYearTiles();
        if (state.calendarView === "year") await renderCalendarExplorer(true);
      });
    }
    if (els.calendarNavYearNext) {
      els.calendarNavYearNext.addEventListener("click", async () => {
        state.calendarNavYear = Number(state.calendarNavYear || new Date().getUTCFullYear()) + 1;
        renderYearTiles();
        if (state.calendarView === "year") await renderCalendarExplorer(true);
      });
    }
    if (els.calendarModalGrid) {
      els.calendarModalGrid.addEventListener("click", async (ev) => {
        const btn = ev.target?.closest?.("[data-month-jump]");
        if (!btn) return;
        const monthIso = String(btn.getAttribute("data-month-jump") || "").trim();
        if (!/^\d{4}-\d{2}$/.test(monthIso)) return;
        state.calendarMonth = monthIso;
        state.calendarView = "month";
        if (els.calendarViewToggle) {
          els.calendarViewToggle.querySelectorAll(".calendar-mode-btn").forEach((b) => {
            b.classList.toggle("is-active", String(b.getAttribute("data-view")) === "month");
          });
        }
        await renderCalendarMonth(state.calendarMonth, true);
        await renderCalendarExplorer(true);
      });
      els.calendarModalGrid.addEventListener("wheel", async (ev) => {
        if (!ev.shiftKey) return;
        ev.preventDefault();
        state.calendarMonth = shiftMonth(state.calendarMonth || currentMonthIsoBerlin(), ev.deltaY > 0 ? 1 : -1);
        await renderCalendarMonth(state.calendarMonth, true);
        await renderCalendarExplorer(true);
      }, { passive: false });
    }
    if (els.calendarGotoInput) {
      els.calendarGotoInput.addEventListener("keydown", async (ev) => {
        if (ev.key !== "Enter") return;
        const parsed = parseGoToDate(els.calendarGotoInput.value);
        if (!parsed) return;
        state.calendarMonth = parsed.slice(0, 7);
        state.calendarSelectedDate = parsed;
        await renderCalendarMonth(state.calendarMonth, true);
        await renderCalendarExplorer(true);
        await selectCalendarDate(parsed, true);
      });
    }
    if (els.calendarFilterPills) {
      els.calendarFilterPills.addEventListener("click", async (ev) => {
        const btn = ev.target?.closest?.("[data-filter]");
        if (!btn) return;
        const key = String(btn.getAttribute("data-filter") || "");
        if (!(key in state.calendarFilters)) return;
        state.calendarFilters[key] = !state.calendarFilters[key];
        btn.classList.toggle("is-active", state.calendarFilters[key]);
        await renderCalendarExplorer(false);
      });
    }
    if (els.calendarQuickRanges) {
      els.calendarQuickRanges.addEventListener("click", async (ev) => {
        const btn = ev.target?.closest?.("[data-range]");
        if (!btn) return;
        const type = String(btn.getAttribute("data-range") || "");
        const today = getTodayDateUtcKey();
        if (!today) return;
        if (type === "90d") {
          state.calendarView = "timeline";
          if (els.calendarViewToggle) {
            els.calendarViewToggle.querySelectorAll(".calendar-mode-btn").forEach((b) => {
              b.classList.toggle("is-active", String(b.getAttribute("data-view")) === "timeline");
            });
          }
          const e = parseIsoDate(today);
          state.calendarRange = { start: getDateKey(shiftDate(e, -89)), end: today };
          const payload = await fetchCalendarRange(state.calendarRange.start, state.calendarRange.end, true);
          if (payload) {
            state.calendarRangePayload = payload;
            renderSpanLane(payload);
            renderFocusLegend();
            renderCalendarGrid(els.calendarModalGrid, buildWeeksPayloadFromRange(payload, "timeline"), {
              modal: true,
              selectedDate: state.calendarSelectedDate,
              onPick: (cell) => selectCalendarDate(cell?.date, false),
              showBlocks: state.calendarShowBlocks,
              focus: state.calendarFocus,
              filters: state.calendarFilters,
              view: "timeline",
            });
          }
          return;
        }
        if (type === "month") {
          state.calendarView = "month";
          await renderCalendarExplorer(true);
          return;
        }
        if (type === "block") {
          const spans = Array.isArray(state.calendarRangePayload?.spans) ? state.calendarRangePayload.spans : [];
          const sel = state.calendarSelectedDate;
          const sp = spans.find((s) => sel && s?.start && s?.end && sel >= s.start && sel <= s.end);
          if (sp) {
            const s = parseIsoDate(sp.start);
            const e = parseIsoDate(sp.end);
            state.calendarView = "timeline";
            if (els.calendarViewToggle) {
              els.calendarViewToggle.querySelectorAll(".calendar-mode-btn").forEach((b) => {
                b.classList.toggle("is-active", String(b.getAttribute("data-view")) === "timeline");
              });
            }
            state.calendarRange = { start: getDateKey(shiftDate(s, -3)), end: getDateKey(shiftDate(e, 3)) };
            const payload = await fetchCalendarRange(state.calendarRange.start, state.calendarRange.end, true);
            if (payload) {
              state.calendarRangePayload = payload;
              renderSpanLane(payload);
              renderFocusLegend();
              renderCalendarGrid(els.calendarModalGrid, buildWeeksPayloadFromRange(payload, "timeline"), {
                modal: true,
                selectedDate: state.calendarSelectedDate,
                onPick: (cell) => selectCalendarDate(cell?.date, false),
                showBlocks: state.calendarShowBlocks,
                focus: state.calendarFocus,
                filters: state.calendarFilters,
                view: "timeline",
              });
            }
          }
        }
      });
    }
    if (els.calendarSpanLane) {
      els.calendarSpanLane.addEventListener("click", async (ev) => {
        const btn = ev.target?.closest?.("[data-span-start][data-span-end]");
        if (!btn) return;
        const start = String(btn.getAttribute("data-span-start") || "").trim();
        if (!/^\d{4}-\d{2}-\d{2}$/.test(start)) return;
        state.calendarSelectedDate = start;
        state.calendarMonth = start.slice(0, 7);
        await renderCalendarMonth(state.calendarMonth, true);
        await renderCalendarExplorer(true);
        await selectCalendarDate(start, true);
      });
    }
    if (els.calendarPhaseSummary) {
      els.calendarPhaseSummary.addEventListener("click", async (ev) => {
        const btn = ev.target?.closest?.("[data-jump-kind]");
        if (!btn) return;
        const kind = String(btn.getAttribute("data-jump-kind") || "");
        const spans = Array.isArray(state.calendarRangePayload?.spans) ? state.calendarRangePayload.spans : [];
        const sp = spans.find((s) => s?.kind === kind);
        if (!sp?.start) return;
        state.calendarSelectedDate = sp.start;
        state.calendarMonth = sp.start.slice(0, 7);
        await renderCalendarMonth(state.calendarMonth, true);
        await renderCalendarExplorer(true);
        await selectCalendarDate(sp.start, true);
      });
    }
    if (els.calendarJumpNextTraining) {
      els.calendarJumpNextTraining.addEventListener("click", async () => {
        const days = Array.isArray(state.calendarRangePayload?.days) ? state.calendarRangePayload.days : [];
        const sel = state.calendarSelectedDate || getTodayDateUtcKey();
        const next = days.find((d) => d?.date && d.date > sel && (d?.actual?.did || d?.types?.dominant === "strength" || d?.types?.dominant === "run"));
        if (!next?.date) return;
        state.calendarSelectedDate = next.date;
        state.calendarMonth = next.date.slice(0, 7);
        await renderCalendarExplorer(true);
        await selectCalendarDate(next.date, true);
      });
    }
    if (els.calendarJumpNextSpan) {
      els.calendarJumpNextSpan.addEventListener("click", async () => {
        await jumpToSpanBoundary("next");
      });
    }
    if (els.calendarJumpPrevSpan) {
      els.calendarJumpPrevSpan.addEventListener("click", async () => {
        await jumpToSpanBoundary("prev");
      });
    }
    if (els.calendarDayPanel) {
      els.calendarDayPanel.addEventListener("click", async (ev) => {
        const btn = ev.target?.closest?.("[data-day-shift]");
        if (!btn) return;
        const delta = Number(btn.getAttribute("data-day-shift"));
        if (!Number.isFinite(delta) || !state.calendarSelectedDate) return;
        const cur = parseIsoDate(state.calendarSelectedDate);
        if (!cur) return;
        const nextIso = getDateKey(shiftDate(cur, delta));
        if (!nextIso) return;
        if (state.calendarMonth !== nextIso.slice(0, 7)) {
          state.calendarMonth = nextIso.slice(0, 7);
          await renderCalendarMonth(state.calendarMonth, true);
        }
        await selectCalendarDate(nextIso, true);
      });
    }
    if (els.calendarDayClose) {
      els.calendarDayClose.addEventListener("click", () => {
        els.calendarDayPanel?.classList.remove("is-open");
        els.calendarDayPanel?.setAttribute("aria-hidden", "true");
      });
    }

    document.addEventListener("keydown", async (ev) => {
      if (ev.key === "Escape") {
        closeKienzlModal();
        closeCalendarModal();
        return;
      }
      if (els.calendarModal?.getAttribute("aria-hidden") === "false") {
        if (ev.key === "ArrowLeft") {
          ev.preventDefault();
          state.calendarMonth = shiftMonth(state.calendarMonth || currentMonthIsoBerlin(), -1);
          await renderCalendarMonth(state.calendarMonth, true);
          await renderCalendarExplorer(true);
          return;
        }
        if (ev.key === "ArrowRight") {
          ev.preventDefault();
          state.calendarMonth = shiftMonth(state.calendarMonth || currentMonthIsoBerlin(), 1);
          await renderCalendarMonth(state.calendarMonth, true);
          await renderCalendarExplorer(true);
          return;
        }
        if (ev.key === "t" || ev.key === "T") {
          ev.preventDefault();
          const todayIso = getTodayDateUtcKey();
          if (!todayIso) return;
          state.calendarSelectedDate = todayIso;
          state.calendarMonth = todayIso.slice(0, 7);
          await renderCalendarMonth(state.calendarMonth, true);
          await renderCalendarExplorer(true);
        }
      }
    });
  }

  async function loadDashboard(opts = {}) {
    const isLiveRefresh = !!opts.live;
    applyDashboardLayout();
    state.enduranceMode = _loadEnduranceMode();
    const bootstrapMealplan = (!isLiveRefresh && window.__DASHBOARD_BOOTSTRAP__ && typeof window.__DASHBOARD_BOOTSTRAP__ === "object")
      ? (window.__DASHBOARD_BOOTSTRAP__.mealplanToday || null)
      : null;
    if (!state.coreDecisionCardCache) {
      const cachedCard = loadCoreDecisionCardSessionCache();
      if (cachedCard && cachedCard.ok) {
        state.coreDecisionCardCache = cachedCard;
        state.coreDecisionCardCacheDay = getTodayDateUtcKey();
        state.coreDecisionCardLastFetchTs = Date.now();
      } else {
        state.coreDecisionCardCache = null;
      }
    }
    if (state.coreDecisionCardCache && state.coreDecisionCardCache.ok) {
      renderCoreDecisionCard(state.coreDecisionCardCache);
    } else {
      renderBlockStatusLoading();
    }
    markDashboardCardReady("#plan-check-card");

    let mealplanPromise = Promise.resolve();
    let endurancePromise = Promise.resolve();
    if (!isLiveRefresh) {
      if (!state.mealplanLoaded) {
        const mealplanCached = bootstrapMealplan || loadMealplanSessionCache();
        if (mealplanCached) {
          renderMealplanData(mealplanCached);
          state.mealplanPayload = mealplanCached;
          state.mealplanLastFetchTs = Date.now();
          state.mealplanLoaded = true;
          if (bootstrapMealplan) {
            try {
              sessionStorage.setItem(
                STORAGE.mealplanToday,
                JSON.stringify({ day: getTodayDateUtcKey(), ts: Date.now(), payload: bootstrapMealplan })
              );
            } catch (_) {}
          }
        } else {
          renderMealplanSkeleton();
        }
        markDashboardCardReady("#dashboard-mealplan-card");
      }
      mealplanPromise = loadMealplanToday({ force: false });
      if (!state.enduranceLoaded) {
        renderEnduranceSkeleton();
      }
      markDashboardCardReady("#dashboard-endurance-card");
      endurancePromise = loadEnduranceApproval({ force: false });
    }

    const payloadPromise = fetchDashboardData();
    const payload = await payloadPromise;
    if (!payload) return;
    if (payload.settings?.settings) {
      state.coreModeEnabled = !!(payload.settings.settings.core_mode_enabled ?? payload.settings.settings.autopilot_enabled);
      renderCoreSteuertBadge(state.coreModeEnabled);
    }
    if (payload.makrosSettings?.settings) {
      state.makrosSettings = payload.makrosSettings.settings;
    }
    if (payload.enduranceApproval) {
      renderEnduranceData(payload.enduranceApproval);
      state.enduranceLoaded = true;
      state.endurancePayload = payload.enduranceApproval;
      state.enduranceLastFetchTs = Date.now();
    }
    if (!isLiveRefresh && payload.mealplanToday && !state.mealplanLoaded) {
      renderMealplanData(payload.mealplanToday);
      state.mealplanPayload = payload.mealplanToday;
      state.mealplanLastFetchTs = Date.now();
      state.mealplanLoaded = true;
    }
    state.dashboardSnapshot = payload;
    if (payload.trainingTodayCard && typeof payload.trainingTodayCard === "object") {
      try {
        renderTrainingTodayCard(payload.trainingTodayCard, { source: "snapshot" });
      } catch (err) {
        console.error("dashboard renderTrainingTodayCard failed", err);
      }
    } else {
      refreshCoreTrainingCard(getTodayDateUtcKey()).catch((err) => {
        console.error("dashboard renderCoreTrainingCard failed", err);
      });
    }
    if (payload.coreDecisionCard && payload.coreDecisionCard.ok) {
      const previousCard = state.coreDecisionCardCache && state.coreDecisionCardCache.ok
        ? state.coreDecisionCardCache
        : null;
      state.coreDecisionCardCache = {
        ...(previousCard || {}),
        ...payload.coreDecisionCard,
        today_mini: payload.coreDashboardCard?.today_mini || payload.coreDecisionCard?.today_mini || null,
        dashboard_timeline: payload.coreDashboardCard?.timeline?.days || null,
      };
    }
    let data = null;
    try {
      data = buildData(payload);
    } catch (err) {
      console.error("dashboard buildData failed", err);
    }
    if (!data) {
      setTimeout(() => {
        loadDashboard({ live: false }).catch(() => {});
      }, 1500);
      return;
    }
    state.lastData = data;
    if (state.coreDecisionCardCache && state.coreDecisionCardCache.ok) {
      renderCoreDecisionCard(state.coreDecisionCardCache);
    } else {
      renderBlockStatusLoading();
    }
    markDashboardCardReady("#plan-check-card");

    try { setPills(data); } catch (err) { console.error("dashboard setPills failed", err); }
    fetchHrvSyncMeta({ force: false })
      .then((syncMeta) => {
        try { updateHrvPillFromSyncMeta(syncMeta, data); } catch (err) { console.error("dashboard updateHrvPillFromSyncMeta failed", err); }
      })
      .catch(() => {});
    try { setTopCards(data); } catch (err) { console.error("dashboard setTopCards failed", err); }
    try { setPlanSummary(data); } catch (err) { console.error("dashboard setPlanSummary failed", err); }
    try { setSignals(data); } catch (err) { console.error("dashboard setSignals failed", err); }
    markDashboardCardReady("#dashboard-today-card");
    markDashboardCardReady("#dashboard-signals-card");
    try {
      renderTodayCardFromSignals({
        signals: payload.signals14,
        planWeek: payload.planWeek,
        planMeta: payload.today?.plan_week || null,
        lastLogs: payload.lastLogs
      });
    } catch (err) {
      console.error("dashboard renderTodayCardFromSignals failed", err);
    }
    markDashboardCardReady("#dashboard-today-card");
    try { renderTodayAgendaCard(payload.todayAgenda); } catch (err) { console.error("dashboard renderTodayAgendaCard failed", err); }
    markDashboardCardReady("#today-agenda-card");
    try {
      if (payload.blockStatus) {
        renderBlockStatus(payload.blockStatus, payload.hrvLatest || null, payload.controlPlane || null);
      } else {
        renderBlockStatusError();
      }
    } catch (err) {
      console.error("dashboard renderBlockStatus failed", err);
      renderBlockStatusError();
    }
    markDashboardCardReady("#plan-check-card");

    // Mealplan is intentionally excluded from aggressive live refresh polling.
    mealplanPromise.finally(() => {
      markDashboardCardReady("#dashboard-mealplan-card");
    });
    endurancePromise.finally(() => {
      markDashboardCardReady("#dashboard-endurance-card");
    });

    if (!isLiveRefresh && payload.loading) {
      setTimeout(() => {
        loadDashboard({ live: false }).catch(() => {});
      }, 1500);
    }
    if (!isLiveRefresh && (payload.stale || !payload.coreDecisionCard)) {
      fetchCoreDecisionCard().then((card) => {
        if (card && card.ok) {
          state.coreDecisionCardCache = card;
          state.dashboardSnapshot = {
            ...(state.dashboardSnapshot || {}),
            coreDecisionCard: card,
          };
        }
        if (state.coreDecisionCardCache && state.coreDecisionCardCache.ok) renderCoreDecisionCard(state.coreDecisionCardCache);
        else renderBlockStatusLoading();
      }).catch(() => {});
    }
    state.lastFullDashboardTs = Date.now();
  }

  async function refreshDashboardLiveLite() {
    try {
      const fresh = await fetchCoreDecisionCard({ force: true });
      if (fresh && fresh.ok) state.coreDecisionCardCache = fresh;
      if (state.coreDecisionCardCache && state.coreDecisionCardCache.ok) renderCoreDecisionCard(state.coreDecisionCardCache);
      else renderBlockStatusLoading();
    } catch (_) {
      if (state.coreDecisionCardCache && state.coreDecisionCardCache.ok) renderCoreDecisionCard(state.coreDecisionCardCache);
      else renderBlockStatusLoading();
    }
    try {
      await _refreshNextSessionForDay(getTodayDateUtcKey());
    } catch (_) {}
  }

  function createMealplanRenderKey(payload) {
    if (!payload || typeof payload !== "object") return "empty";
    const rows = resolveMealplanCardRows(payload).map((row) => ({
      slotId: row.slotId || null,
      state: row.state || "",
      time: row.time || "",
      name: row.name || "",
      badge: mealplanStateBadge(row) || "",
      isHistorical: !!row.isHistorical,
      items: Array.isArray(row.items) ? row.items.map((item) => formatMealplanItem(item)).filter(Boolean) : []
    }));
    return JSON.stringify({
      date: payload.date || "",
      weekday: payload.weekday || "",
      rows,
      core: payload.core_nutrition || null
    });
  }

  function shouldRefreshMealplanNow(force) {
    if (force || state.mealplanDirty || !state.mealplanLoaded || !state.mealplanPayload) return true;
    return (Date.now() - Number(state.mealplanLastFetchTs || 0)) >= MEALPLAN_MIN_REFRESH_GAP_MS;
  }

  function formatMealplanDate(dateIso, weekday) {
    if (!dateIso) return "—";
    const parts = String(dateIso).split("-");
    if (parts.length < 3) return "—";
    const day = parts[2];
    const month = parts[1];
    let label = weekday || "";
    if (!label) {
      try {
        const dt = new Date(`${String(dateIso)}T12:00:00`);
        label = dt.toLocaleDateString("de-DE", { weekday: "short" }).replace(".", "");
        if (label) label = `${label.slice(0, 1).toUpperCase()}${label.slice(1)}`;
      } catch (_) {
        label = "—";
      }
    }
    return `${label}, ${day}.${month}.`;
  }

  function coreMealStatusMarker(meal) {
    const state = String(meal?.state || "").toLowerCase();
    if (state === "logged") return "done";
    if (state === "replaced") return "replaced";
    if (state === "shifted_by_core") return "shifted";
    if (state === "portion_adjusted_by_core" || state === "accepted_adjustment" || state === "switched") return "adjusted";
    if (state === "skipped" || state === "fulfilled_by_alternative" || state === "cancelled_by_replan" || state === "merged_into_other_meal") return "missed";
    const marker = String(meal?.core_marker || "").toLowerCase();
    if (marker) return marker;
    const status = String(meal?.status || "").toLowerCase();
    if (status === "logged" || status === "telegram_confirmed") return "done";
    if (status === "skipped" || status === "missed") return "missed";
    if (status === "catch_up") return "catch_up";
    if (status === "adjusted_by_core") return "adjusted";
    return "";
  }

  function renderMealplanCorePill(core) {
    const pill = els.mealplanCorePill;
    if (!pill) return;
    pill.hidden = true;
    pill.textContent = "";
    pill.dataset.status = "";
    pill.classList.remove("is-active");
    pill.setAttribute("aria-expanded", "false");
  }

  function renderMealplanCoreExplain(core) {
    state.mealplanCoreExplainPayload = null;
    if (state.coreExplainActiveKey === "mealplan") closeCoreExplainPanel({ restoreFocus: false });
    _setCoreExplainTriggerState();
  }

  function formatAmount(value) {
    if (value === null || value === undefined) return "";
    const num = Number(value);
    if (!Number.isFinite(num)) return "";
    const rounded = Math.round(num);
    if (Math.abs(num - rounded) < 0.001) return String(rounded);
    return num.toFixed(1).replace(/\.0$/, "");
  }

  function formatMealplanItem(item) {
    if (!item) return "";
    const name = String(item.name || "").trim();
    if (!name) return "";
    const amount = formatAmount(item.amount);
    const rawUnit = item.unit ? String(item.unit).trim() : "";
    const unit = rawUnit.toLowerCase() === "pcs" ? "Stk." : rawUnit;
    if (!amount) return name;
    return unit ? `${name} ${amount} ${unit}` : `${name} ${amount}`;
  }

  function isMealplanMealLogged(meal, payload) {
    if (!meal) return false;
    if (meal.logged_meal || meal.logged_meal_id) return true;
    const slotId = Number(meal?.slot_id || 0);
    if (slotId > 0) {
      const loggedForSlot = (payload?.logged_meals || []).some((logged) => {
        const loggedSlotId = Number(logged?.slot_id || logged?.created_from_planned_slot_id || 0);
        return loggedSlotId === slotId;
      });
      if (loggedForSlot) return true;
    }
    const status = String(meal?.status || "").toLowerCase();
    return ["logged", "changed", "telegram_confirmed", "manual_override"].includes(status);
  }

  function getMealplanRecentLogMap(dateIso) {
    const key = String(dateIso || getTodayDateUtcKey());
    const slots = state.mealplanRecentLogsByDate?.[key];
    return slots instanceof Map ? slots : new Map();
  }

  function rememberRecentMealplanLog(dateIso, slotId) {
    const key = String(dateIso || getTodayDateUtcKey());
    const next = {};
    Object.entries(state.mealplanRecentLogsByDate || {}).forEach(([k, value]) => {
      if (value instanceof Map && value.size) next[k] = value;
    });
    const map = next[key] instanceof Map ? next[key] : new Map();
    const normalizedSlot = Number(slotId || 0);
    if (normalizedSlot > 0) map.set(normalizedSlot, Date.now());
    next[key] = map;
    state.mealplanRecentLogsByDate = next;
  }

  function pruneRecentMealplanLogs(dateIso, payload = null) {
    const key = String(dateIso || getTodayDateUtcKey());
    const current = getMealplanRecentLogMap(key);
    if (!current.size) return;
    const confirmed = extractLoggedMealplanSlotIds(payload);
    const nextMap = new Map();
    const now = Date.now();
    current.forEach((ts, slotId) => {
      if (confirmed.has(slotId)) return;
      if (now - Number(ts || 0) < 10000) nextMap.set(slotId, Number(ts || 0));
    });
    const next = {};
    Object.entries(state.mealplanRecentLogsByDate || {}).forEach(([k, value]) => {
      if (k === key) return;
      if (value instanceof Map && value.size) next[k] = value;
    });
    if (nextMap.size) next[key] = nextMap;
    state.mealplanRecentLogsByDate = next;
  }

  function extractLoggedMealplanSlotIds(dayPayload) {
    const slotIds = new Set();
    (dayPayload?.logged_meals || []).forEach((meal) => {
      const slotId = Number(meal?.slot_id || meal?.created_from_planned_slot_id || 0);
      if (slotId > 0) slotIds.add(slotId);
    });
    (dayPayload?.planned_meals || []).forEach((meal) => {
      if (!isMealplanMealLogged(meal, dayPayload)) return;
      const slotId = Number(meal?.slot_id || 0);
      if (slotId > 0) slotIds.add(slotId);
    });
    return slotIds;
  }

  function resolveMealplanDisplayState(meal, payload) {
    if (isMealplanMealLogged(meal, payload)) return "done";
    const slotId = Number(meal?.slot_id || 0);
    if (slotId > 0 && getMealplanRecentLogMap(payload?.date).has(slotId)) return "done";
    const state = String(meal?.state || "").toLowerCase();
    const status = String(meal?.status || "").toLowerCase();
    const source = String(meal?.source_of_truth || meal?.status_source || "").toLowerCase();
    const mutated = !!(meal?.core_adjusted_time || meal?.core_adjusted_name || String(meal?.last_mutation_type || "").trim());
    if (state === "replaced") return "replaced";
    if (state === "fulfilled_by_alternative" || state === "cancelled_by_replan" || state === "merged_into_other_meal") return "historical";
    if (state === "skipped" || status === "skipped" || status === "missed") return "skipped";
    if (
      state === "shifted_by_core" ||
      state === "portion_adjusted_by_core" ||
      state === "accepted_adjustment" ||
      state === "switched" ||
      status === "shifted" ||
      status === "adjusted_by_core" ||
      (mutated && source === "core")
    ) {
      return "open";
    }
    return "open";
  }

  function resolveMealplanCardRows(payload) {
    const srcMeals = Array.isArray(payload?.planned_meals)
      ? payload.planned_meals
      : (Array.isArray(payload?.meals) ? payload.meals : []);
    if (!srcMeals.length) return [];

    const rows = srcMeals.map((meal, idx) => {
      const slotId = Number(meal?.slot_id || 0);
      const state = resolveMealplanDisplayState(meal, payload);
      const slotIndex = Number(meal?.slot_index || idx);
      const time = String(meal?.time || meal?.time_text || meal?.base_time || "").trim() || "—";
      const name = String(meal?.name || meal?.title || meal?.base_name || "Meal").trim() || "Meal";
      const items = Array.isArray(meal?.items) ? meal.items : [];
      return {
        raw: meal,
        slotId,
        slotIndex,
        time,
        name,
        items,
        state,
        marker: coreMealStatusMarker(meal),
        isHistorical: state === "historical" || state === "replaced" || state === "skipped",
      };
    });

    const nextSlotId = Number(payload?.core_nutrition?.nutrition_state?.next_open_meal?.slot_id || 0);
    const nextBySlot = nextSlotId > 0 ? rows.find((r) => r.slotId === nextSlotId && r.state === "open") : null;
    if (nextBySlot) {
      nextBySlot.state = "next_up";
      nextBySlot.isNextUp = true;
    } else {
      const firstOpen = rows.find((r) => r.state === "open");
      if (firstOpen) {
        firstOpen.state = "next_up";
        firstOpen.isNextUp = true;
      }
    }

    return rows;
  }

  function mealplanStateBadge(row) {
    if (!row) return "";
    if (row.state === "done") return "ERLEDIGT";
    if (row.state === "skipped" || row.state === "replaced" || row.state === "historical") return "ERLEDIGT";
    return "";
  }

  function renderMealplanProgress(rows) {
    if (!els.mealplanProgress) return;
    const list = Array.isArray(rows) ? rows : [];
    if (!list.length) {
      els.mealplanProgress.textContent = "Heute ist nichts geplant";
      return;
    }
    const done = list.filter((row) => row.state === "done" || row.state === "skipped" || row.state === "replaced" || row.state === "historical").length;
    const open = list.length - done;
    if (done === 0) {
      els.mealplanProgress.textContent = `${list.length} Meals geplant`;
      return;
    }
    els.mealplanProgress.textContent = `${open} offen · ${done} erledigt`;
  }

  function renderMealplanSkeleton() {
    if (!els.mealplanBody || !els.mealplanCard) return;
    els.mealplanBody.innerHTML = "";
    const wrap = document.createElement("div");
    wrap.className = "mealplan-skeleton";
    for (let i = 0; i < 3; i += 1) {
      const block = document.createElement("div");
      block.className = "mealplan-skeleton-block";
      const line1 = document.createElement("div");
      line1.className = "mealplan-skeleton-line wide";
      const line2 = document.createElement("div");
      line2.className = "mealplan-skeleton-line";
      const line3 = document.createElement("div");
      line3.className = "mealplan-skeleton-line";
      block.appendChild(line1);
      block.appendChild(line2);
      block.appendChild(line3);
      wrap.appendChild(block);
    }
    els.mealplanBody.appendChild(wrap);
  }

  function renderMealplanEmpty(message) {
    if (!els.mealplanBody || !els.mealplanCard) return;
    state.mealplanPayload = null;
    state.mealplanCoreExplainPayload = null;
    if (els.mealplanProgress) els.mealplanProgress.textContent = message;
    renderMealplanCorePill(null);
    if (state.coreExplainActiveKey === "mealplan") {
      closeCoreExplainPanel({ restoreFocus: false });
    }
    els.mealplanBody.innerHTML = "";
    const text = document.createElement("div");
    text.className = "mealplan-empty";
    text.textContent = message;
    els.mealplanBody.appendChild(text);
  }

  function renderMealplanError() {
    renderMealplanEmpty("Mealplan konnte nicht geladen werden.");
  }

  function handleExternalMealplanSync(rawPayload) {
    const payload = rawPayload && typeof rawPayload === "object" ? rawPayload : null;
    if (!payload || payload.type !== "mealplan-sync") return;
    const syncDate = String(payload.date || "").trim();
    const todayDate = getTodayDateUtcKey();
    if (syncDate && syncDate !== todayDate) return;
    state.mealplanDirty = true;
    if (state.mealplanHoldRow || state.mealplanHoldLogging) {
      state.mealplanSyncPending = true;
      return;
    }
    loadMealplanToday({ force: true }).catch((err) => console.warn("dashboard mealplan sync failed", err));
  }

  function flushPendingMealplanSync() {
    if (!state.mealplanSyncPending) return;
    if (state.mealplanHoldRow || state.mealplanHoldLogging || state.mealplanBusy) return;
    state.mealplanSyncPending = false;
    loadMealplanToday({ force: true }).catch((err) => console.warn("dashboard pending mealplan sync failed", err));
  }

  function stopMealplanSyncLoop() {
    if (state.mealplanSyncTimer) {
      window.clearInterval(state.mealplanSyncTimer);
      state.mealplanSyncTimer = null;
    }
  }

  function startMealplanSyncLoop() {
    stopMealplanSyncLoop();
    if (document.hidden) return;
    state.mealplanSyncTimer = window.setInterval(() => {
      if (document.hidden || state.mealplanBusy || state.mealplanHoldRow || state.mealplanHoldLogging) return;
      if (!shouldRefreshMealplanNow(false)) return;
      loadMealplanToday({ force: true }).catch(() => {});
    }, MEALPLAN_SYNC_INTERVAL_MS);
  }

  function getMealplanHoldProgress(row) {
    if (!row) return 0;
    const raw = Number(row.dataset.holdProgress || row.style.getPropertyValue("--mealplan-hold-progress") || 0);
    if (!Number.isFinite(raw)) return 0;
    return Math.max(0, Math.min(1, raw));
  }

  function ensureMealplanHoldRing(row) {
    if (!row) return null;
    let svg = row.querySelector(".mealplan-hold-ring");
    let rect = row.querySelector(".mealplan-hold-ring-rect");
    if (!svg || !rect) {
      svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
      svg.setAttribute("class", "mealplan-hold-ring");
      svg.setAttribute("aria-hidden", "true");
      svg.setAttribute("focusable", "false");
      rect = document.createElementNS("http://www.w3.org/2000/svg", "rect");
      rect.setAttribute("class", "mealplan-hold-ring-rect");
      svg.appendChild(rect);
      row.appendChild(svg);
    }
    syncMealplanHoldRing(row);
    return rect;
  }

  function syncMealplanHoldRing(row) {
    if (!row) return;
    const svg = row.querySelector(".mealplan-hold-ring");
    const rect = row.querySelector(".mealplan-hold-ring-rect");
    if (!svg || !rect) return;
    const width = Math.max(0, row.clientWidth - 2);
    const height = Math.max(0, row.clientHeight - 2);
    svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
    const radius = Math.max(0, Math.min(18, Math.min(width, height) / 2 - 1));
    rect.setAttribute("x", "1");
    rect.setAttribute("y", "1");
    rect.setAttribute("width", String(Math.max(0, width - 2)));
    rect.setAttribute("height", String(Math.max(0, height - 2)));
    rect.setAttribute("rx", String(radius));
    rect.setAttribute("ry", String(radius));
    const length = rect.getTotalLength();
    row.dataset.holdRingLength = String(length);
    rect.style.strokeDasharray = `${length}`;
    rect.style.strokeDashoffset = `${(1 - getMealplanHoldProgress(row)) * length}`;
  }

  function setMealplanHoldProgress(row, progress) {
    if (!row) return;
    const clamped = Math.max(0, Math.min(1, Number(progress) || 0));
    row.dataset.holdProgress = clamped.toFixed(4);
    row.style.setProperty("--mealplan-hold-progress", clamped.toFixed(4));
    const rect = ensureMealplanHoldRing(row);
    const length = Number(row.dataset.holdRingLength || 0);
    if (rect && Number.isFinite(length) && length > 0) {
      rect.style.strokeDashoffset = `${(1 - clamped) * length}`;
    }
  }

  function clearMealplanHoldVisual(row, options = {}) {
    if (!row) return;
    const preserveComplete = options.preserveComplete === true;
    row.classList.remove("is-hold-arming");
    row.classList.remove("is-hold-firing");
    if (!preserveComplete) row.classList.remove("is-hold-complete");
    if (!preserveComplete) {
      delete row.dataset.holdProgress;
      setMealplanHoldProgress(row, 0);
    }
    row.style.removeProperty("--mealplan-hold-progress");
  }

  function stopMealplanHoldProgress() {
    if (state.mealplanHoldRaf) {
      window.cancelAnimationFrame(state.mealplanHoldRaf);
      state.mealplanHoldRaf = null;
    }
  }

  function animateMealplanHoldProgress(row, targetProgress, durationMs, options = {}) {
    if (!row) return;
    stopMealplanHoldProgress();
    const start = getMealplanHoldProgress(row);
    const target = Math.max(0, Math.min(1, Number(targetProgress) || 0));
    const duration = Math.max(0, Number(durationMs) || 0);
    if (duration <= 0 || Math.abs(start - target) < 0.0005) {
      setMealplanHoldProgress(row, target);
      if (typeof options.onComplete === "function") options.onComplete();
      return;
    }
    const startedAt = performance.now();
    const tick = (now) => {
      const t = Math.max(0, Math.min(1, (now - startedAt) / duration));
      const progress = start + (target - start) * t;
      setMealplanHoldProgress(row, progress);
      if (t >= 1) {
        state.mealplanHoldRaf = null;
        if (typeof options.onComplete === "function") options.onComplete();
        return;
      }
      state.mealplanHoldRaf = window.requestAnimationFrame(tick);
    };
    setMealplanHoldProgress(row, start);
    state.mealplanHoldRaf = window.requestAnimationFrame(tick);
  }

  function cancelMealplanHold(row = state.mealplanHoldRow, options = {}) {
    if (state.mealplanHoldTimer) {
      window.clearTimeout(state.mealplanHoldTimer);
      state.mealplanHoldTimer = null;
    }
    const preserveComplete = options.preserveComplete === true;
    const animateBack = options.animateBack !== false;
    const shouldFlushPending = options.flushPending !== false;
    const releasePointer = () => {
      if (row && row.releasePointerCapture && row.dataset.activePointerId) {
        try { row.releasePointerCapture(Number(row.dataset.activePointerId)); } catch (_) {}
      }
      if (row) delete row.dataset.activePointerId;
      state.mealplanHoldRow = null;
    };
    if (!row) {
      stopMealplanHoldProgress();
      state.mealplanHoldRow = null;
      if (shouldFlushPending) flushPendingMealplanSync();
      return;
    }
    if (preserveComplete) {
      stopMealplanHoldProgress();
      clearMealplanHoldVisual(row, options);
      setMealplanHoldProgress(row, 1);
      releasePointer();
      if (shouldFlushPending) flushPendingMealplanSync();
      return;
    }
    if (animateBack && getMealplanHoldProgress(row) > 0 && !state.mealplanHoldLogging) {
      row.classList.remove("is-hold-firing");
      row.classList.add("is-hold-arming");
      animateMealplanHoldProgress(row, 0, Math.max(180, getMealplanHoldProgress(row) * 260), {
        onComplete: () => {
          clearMealplanHoldVisual(row);
          releasePointer();
          if (shouldFlushPending) flushPendingMealplanSync();
        }
      });
      return;
    }
    stopMealplanHoldProgress();
    clearMealplanHoldVisual(row, options);
    releasePointer();
    if (shouldFlushPending) flushPendingMealplanSync();
  }

  async function logMealplanRow(slotId, rowEl) {
    if (!slotId || state.mealplanHoldLogging) return;
    state.mealplanHoldLogging = true;
    let holdReleasedBeforeRender = false;
    if (rowEl) {
      rowEl.classList.remove("is-hold-arming");
      rowEl.classList.add("is-hold-firing");
    }
    try {
      const dateIso = getTodayDateUtcKey();
      const payload = { date: dateIso, time_mode: "planned" };
      const res = await fetch(`/api/nutrition/logging/planned/${encodeURIComponent(slotId)}/log`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
        cache: "no-store",
      });
      const dayPayload = await res.json().catch(() => null);
      if (!res.ok) throw new Error("mealplan_log_failed");
      rememberRecentMealplanLog(dayPayload?.date || dateIso, slotId);
      if (rowEl) {
        rowEl.classList.remove("is-hold-firing");
        rowEl.classList.add("is-hold-complete");
        rowEl.classList.add("is-done");
        rowEl.dataset.completed = "1";
        setMealplanHoldProgress(rowEl, 1);
      }
      await new Promise((resolve) => window.setTimeout(resolve, 220));
      if (rowEl && state.mealplanHoldRow === rowEl) {
        cancelMealplanHold(rowEl, { preserveComplete: true, flushPending: false });
        holdReleasedBeforeRender = true;
      }
      if (dayPayload && dayPayload.ok !== false) {
        pruneRecentMealplanLogs(dayPayload?.date || dateIso, dayPayload);
        renderMealplanData(dayPayload);
        state.mealplanPayload = dayPayload;
        state.mealplanLastFetchTs = Date.now();
        state.mealplanDirty = false;
        state.mealplanLoaded = true;
      } else {
        await loadMealplanToday({ force: true });
      }
    } catch (err) {
      console.error("dashboard mealplan hold log failed", err);
    } finally {
      state.mealplanHoldLogging = false;
      if (!holdReleasedBeforeRender) {
        cancelMealplanHold(rowEl, { preserveComplete: true });
      }
      flushPendingMealplanSync();
    }
  }

  function bindMealplanHoldLogging() {
    if (isKey || !els.mealplanBody) return;
    els.mealplanBody.querySelectorAll(".mealplan-meal[data-slot-id]").forEach((row) => {
      if (row.dataset.holdBound === "1") return;
      if (row.classList.contains("is-done") || row.dataset.completed === "1") return;
      row.dataset.holdBound = "1";

      const startHold = (event) => {
        if (state.mealplanHoldLogging) return;
        if (row.classList.contains("is-done") || row.dataset.completed === "1") return;
        if (event.button !== undefined && event.button !== 0) return;
        const slotId = Number(row.dataset.slotId || 0);
        if (!slotId) return;
        cancelMealplanHold(undefined, { flushPending: false, animateBack: false });
        state.mealplanHoldRow = row;
        row.classList.add("is-hold-arming");
        setMealplanHoldProgress(row, 0);
        animateMealplanHoldProgress(row, 1, 2000);
        if (event.pointerId !== undefined && row.setPointerCapture) {
          try {
            row.setPointerCapture(event.pointerId);
            row.dataset.activePointerId = String(event.pointerId);
          } catch (_) {}
        }
        state.mealplanHoldTimer = window.setTimeout(() => {
          state.mealplanHoldTimer = null;
          logMealplanRow(slotId, row);
        }, 2000);
      };

      const stopHold = () => {
        if (state.mealplanHoldLogging && state.mealplanHoldRow === row) return;
        if (state.mealplanHoldRow === row) cancelMealplanHold(row);
      };

      row.addEventListener("pointerdown", startHold);
      row.addEventListener("pointerup", stopHold);
      row.addEventListener("pointercancel", stopHold);
      row.addEventListener("pointerleave", stopHold);
      row.addEventListener("lostpointercapture", stopHold);
      row.addEventListener("contextmenu", (event) => event.preventDefault());
    });
  }

  function renderMealplanData(payload) {
    if (!els.mealplanBody || !els.mealplanCard) return;
    state.mealplanPayload = payload || null;
    const rows = resolveMealplanCardRows(payload);
    const core = payload?.core_nutrition || null;
    const renderKey = createMealplanRenderKey(payload);
    renderMealplanCorePill(core);
    renderMealplanCoreExplain(core);
    if (els.mealplanDate) {
      els.mealplanDate.textContent = formatMealplanDate(payload?.date, payload?.weekday);
    }
    renderMealplanProgress(rows);
    if (!rows.length) {
      state.mealplanRenderKey = renderKey;
      renderMealplanEmpty("Heute ist nichts geplant.");
      return;
    }
    if (state.mealplanRenderKey === renderKey) {
      bindMealplanHoldLogging();
      return;
    }
    state.mealplanRenderKey = renderKey;
    els.mealplanBody.innerHTML = "";
    if (els.mealplanCard) {
      if (rows.length > 10) {
        els.mealplanCard.classList.add("is-compact");
      } else {
        els.mealplanCard.classList.remove("is-compact");
      }
    }

    rows.forEach((row) => {
      const block = document.createElement("div");
      block.className = `mealplan-meal is-${row.state}`;
      if (row.slotId) block.dataset.slotId = String(row.slotId);
      if (row.state === "done") block.dataset.completed = "1";
      if (row.isHistorical) block.classList.add("is-historical");

      const header = document.createElement("div");
      header.className = "mealplan-meal-header";
      const time = document.createElement("span");
      time.className = "mealplan-time";
      time.textContent = row.time;
      const title = document.createElement("span");
      title.className = "mealplan-name";
      title.textContent = row.name;
      const baseName = String(row?.raw?.base_name || "").trim();
      if (baseName && baseName.toLowerCase() !== row.name.toLowerCase()) {
        title.title = `Basis: ${baseName}`;
      }
      header.appendChild(time);
      header.appendChild(title);
      const badgeLabel = mealplanStateBadge(row);
      if (badgeLabel) {
        const badge = document.createElement("span");
        badge.className = `mealplan-status-dot is-${row.state}`;
        badge.textContent = badgeLabel;
        header.appendChild(badge);
      }
      block.appendChild(header);

      const items = Array.isArray(row.items) ? row.items : [];
      if (items.length) {
        const inline = document.createElement("div");
        inline.className = "mealplan-items-inline";
        const lines = items.map(formatMealplanItem).filter(Boolean);
        let displayLines = lines;
        if (lines.length > 8) {
          displayLines = lines.slice(0, 7);
          displayLines.push(`+${lines.length - 7} Zutaten`);
        }
        inline.textContent = displayLines.join(" · ");
        block.appendChild(inline);
      }

      els.mealplanBody.appendChild(block);
      if (row.slotId || row.state === "done") {
        ensureMealplanHoldRing(block);
        window.requestAnimationFrame(() => {
          syncMealplanHoldRing(block);
          if (row.state === "done") {
            block.classList.add("is-done");
            setMealplanHoldProgress(block, 1);
          }
        });
      }
    });
    bindMealplanHoldLogging();
  }

  async function loadMealplanToday({ force = false } = {}) {
    if (!els.mealplanBody || !els.mealplanCard) return;
    const isFresh = (Date.now() - Number(state.mealplanLastFetchTs || 0)) < MEALPLAN_TTL_MS;
    if (!force && state.mealplanPayload && isFresh) return state.mealplanPayload;
    if (state.mealplanBusy) return;
    if (!shouldRefreshMealplanNow(force)) return;
    state.mealplanBusy = true;
    if (!state.mealplanLoaded) {
      renderMealplanSkeleton();
    }
    try {
      const dateIso = getTodayDateUtcKey();
      const res = await fetch(`/api/nutrition/logging/day?date=${encodeURIComponent(dateIso)}`, { cache: "no-store" });
      const payload = await res.json();
      pruneRecentMealplanLogs(payload?.date || dateIso, payload);
      renderMealplanData(payload);
      state.mealplanPayload = payload;
      state.mealplanLastFetchTs = Date.now();
      state.mealplanDirty = false;
      try {
        sessionStorage.setItem(
          STORAGE.mealplanToday,
          JSON.stringify({ day: getTodayDateUtcKey(), ts: Date.now(), payload })
        );
      } catch (_) {}
      state.mealplanLoaded = true;
    } catch (e) {
      renderMealplanError();
      state.mealplanLoaded = true;
    } finally {
      state.mealplanBusy = false;
    }
  }

  window.addEventListener("storage", (event) => {
    if (event.key !== DASHBOARD_SYNC_KEY || !event.newValue) return;
    try {
      handleExternalMealplanSync(JSON.parse(event.newValue));
    } catch (_) {}
  });

  try {
    dashboardSyncChannel?.addEventListener("message", (event) => {
      handleExternalMealplanSync(event.data);
    });
  } catch (_) {}

  document.addEventListener("visibilitychange", () => {
    if (document.hidden) {
      stopMealplanSyncLoop();
      return;
    }
    if (state.mealplanHoldRow || state.mealplanHoldLogging) return;
    loadMealplanToday({ force: true }).catch(() => {});
    startMealplanSyncLoop();
  });

  window.addEventListener("focus", () => {
    if (state.mealplanHoldRow || state.mealplanHoldLogging) return;
    loadMealplanToday({ force: true }).catch(() => {});
    startMealplanSyncLoop();
  });

  startMealplanSyncLoop();

  const CAL_WEEKDAYS = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"];

  function monthLabel(monthIso) {
    if (!monthIso || !/^\d{4}-\d{2}$/.test(monthIso)) return "--";
    const year = Number(monthIso.slice(0, 4));
    const month = Number(monthIso.slice(5, 7));
    if (!Number.isFinite(year) || !Number.isFinite(month) || month < 1 || month > 12) return "--";
    const dt = new Date(Date.UTC(year, month - 1, 1));
    const label = dt.toLocaleDateString("de-DE", { month: "long", year: "numeric", timeZone: "UTC" });
    return label ? `${label.slice(0, 1).toUpperCase()}${label.slice(1)}` : "--";
  }

  function monthStartEnd(monthIso) {
    if (!monthIso || !/^\d{4}-\d{2}$/.test(monthIso)) return null;
    const y = Number(monthIso.slice(0, 4));
    const m = Number(monthIso.slice(5, 7));
    if (!Number.isFinite(y) || !Number.isFinite(m) || m < 1 || m > 12) return null;
    const start = new Date(Date.UTC(y, m - 1, 1));
    const end = new Date(Date.UTC(y, m, 0));
    return { start: getDateKey(start), end: getDateKey(end) };
  }

  function monthLiveToGridPayload(payload) {
    const days = Array.isArray(payload?.days) ? payload.days : [];
    const bars = Array.isArray(payload?.bars) ? payload.bars : [];
    const spans = bars.map((b) => ({
      kind: b?.kind === "functional_overreach" ? "overreach" : b?.kind,
      label: b?.label,
      start: b?.start,
      end: b?.end,
      confidence: b?.confidence,
      trigger_reasons: Array.isArray(b?.reasons) ? b.reasons : [],
      why: b?.why || {}
    }));
    const todayIso = String(payload?.meta?.today || getTodayDateUtcKey() || "");
    const cells = days.map((d) => {
      const strength = d?.actual?.strength || {};
      const run = d?.actual?.run || {};
      const actualDid = !!(strength?.has || run?.has);
      const plannedKind = d?.planned?.kind || null;
      const plannedHas = plannedKind !== null;
      const isFuture = String(d?.date || "") > todayIso;
      const phaseDeload = isFuture ? false : !!d?.phase?.deload;
      const phaseFo = isFuture ? false : !!d?.phase?.functional_overreach;
      return {
        date: d?.date,
        day: d?.day,
        dow: Math.max(0, Number(d?.dow || 1) - 1),
        in_month: !!d?.in_month,
        types: d?.types || { dominant: "off", secondary: [] },
        planned: {
          has: plannedHas,
          has_plan: plannedHas,
          kind: plannedKind === "strength" || plannedKind === "run" ? "train" : (plannedKind === "rest" || plannedKind === "off" ? "off" : null),
          title: d?.planned?.title || null,
          plan_title: d?.planned?.title || null
        },
        actual: {
          did: actualDid,
          did_train: actualDid,
          title: strength?.title || (run?.has ? "Run" : null),
          summary: strength?.has
            ? `${Number(strength.total_sets || 0)} Sets · +${Number(strength.improved_sets || 0)}${Number(strength.worse_sets || 0) > 0 ? `/-${Number(strength.worse_sets || 0)}` : ""}`
            : (run?.has ? `${run.distance_km ? Number(run.distance_km).toFixed(1) : "—"} km` : null),
          strength,
          run
        },
        flags: d?.flags || { sick: false },
        phase: {
          deload: phaseDeload,
          overreach: phaseFo
        },
        state: {
          rest: !!d?.tags?.rest_day
        },
        readiness: d?.readiness || {},
        perf: d?.perf || {}
      };
    });
    const weeks = [];
    for (let i = 0; i < cells.length; i += 7) weeks.push(cells.slice(i, i + 7));
    return {
      meta: {
        month: payload?.meta?.month,
        today: payload?.meta?.today
      },
      weeks,
      spans
    };
  }

  function parseGoToDate(raw) {
    const t = String(raw || "").trim();
    if (!t) return null;
    if (/^\d{4}-\d{2}-\d{2}$/.test(t)) return t;
    const m = t.match(/^(\d{1,2})\.(\d{1,2})\.(\d{2,4})$/);
    if (!m) return null;
    let year = Number(m[3]);
    if (year < 100) year += 2000;
    const month = Number(m[2]);
    const day = Number(m[1]);
    if (!Number.isFinite(year) || !Number.isFinite(month) || !Number.isFinite(day)) return null;
    if (month < 1 || month > 12 || day < 1 || day > 31) return null;
    const iso = `${String(year).padStart(4, "0")}-${String(month).padStart(2, "0")}-${String(day).padStart(2, "0")}`;
    return parseIsoDate(iso) ? iso : null;
  }

  function formatDateDe(dateIso, opts = {}) {
    if (!dateIso) return "—";
    const dt = parseIsoDate(dateIso);
    if (!dt) return "—";
    const withWeekday = !!opts.weekday;
    const short = !!opts.short;
    if (withWeekday) {
      return dt.toLocaleDateString("de-DE", {
        weekday: short ? "short" : "short",
        day: "2-digit",
        month: "2-digit",
        year: short ? undefined : "numeric",
        timeZone: "UTC"
      });
    }
    return dt.toLocaleDateString("de-DE", {
      day: "2-digit",
      month: "2-digit",
      year: short ? undefined : "numeric",
      timeZone: "UTC"
    });
  }

  function currentMonthIsoBerlin() {
    const key = getTodayDateUtcKey();
    if (!key) return null;
    return String(key).slice(0, 7);
  }

  function shiftMonth(monthIso, delta) {
    if (!monthIso || !/^\d{4}-\d{2}$/.test(monthIso)) return currentMonthIsoBerlin();
    const year = Number(monthIso.slice(0, 4));
    const month = Number(monthIso.slice(5, 7));
    const d = new Date(Date.UTC(year, month - 1 + delta, 1));
    const y = d.getUTCFullYear();
    const m = String(d.getUTCMonth() + 1).padStart(2, "0");
    return `${y}-${m}`;
  }

  function formatCalendarTooltip(cell) {
    const actual = cell?.actual || {};
    const planned = cell?.planned || {};
    const phase = cell?.phase || {};
    const flags = cell?.flags || {};
    const actualDid = !!(actual.did_train ?? actual.did);
    const plannedHas = !!(planned.has_plan ?? planned.has);
    const plannedTitle = planned.plan_title || planned.title || null;
    const actualTitle = actual.summary || actual.session_title || actual.title || null;
    const bits = [];
    if (actualDid) {
      const txt = actualTitle || "Training";
      bits.push(`Gemacht: ${txt}`);
    } else if (plannedHas) {
      bits.push(`Geplant: ${plannedTitle || (planned.kind === "off" ? "Off" : "Training")}`);
    } else {
      bits.push("Kein Eintrag");
    }
    if (phase.deload) bits.push("DELOAD");
    if (phase.overreach) bits.push("OVERREACH");
    if (flags.sick) bits.push("SICK");
    const strength = actual?.strength || {};
    if (strength?.has) {
      bits.push(`+${Number(strength.improved_sets || 0)} / -${Number(strength.worse_sets || 0)}`);
    }
    if (cell?.readiness?.score !== null && cell?.readiness?.score !== undefined) {
      bits.push(`Readiness ${Math.round(Number(cell.readiness.score))}`);
    }
    return bits.join(" · ");
  }

  function renderMiniLegend(payload) {
    if (!els.calendarMiniLegend) return;
    const spans = Array.isArray(payload?.spans) ? payload.spans : [];
    const hasDeload = spans.some((s) => s?.kind === "deload");
    const hasOverreach = spans.some((s) => s?.kind === "overreach");
    els.calendarMiniLegend.innerHTML = `
      <span class="calendar-legend-chip"><span class="calendar-legend-dot is-strength"></span>Strength</span>
      <span class="calendar-legend-chip"><span class="calendar-legend-dot is-run"></span>Run</span>
      <span class="calendar-legend-chip"><span class="calendar-legend-dot is-sick"></span>Sick</span>
      ${hasDeload ? '<span class="calendar-legend-chip"><span class="calendar-legend-bar is-deload"></span>Deload</span>' : ""}
      ${hasOverreach ? '<span class="calendar-legend-chip"><span class="calendar-legend-bar is-overreach"></span>Overreach</span>' : ""}
      <span class="calendar-legend-chip">▲ besser</span>
      <span class="calendar-legend-chip">▼ schlechter</span>
    `;
  }

  function getTimelineWindow(centerIso, days = 14) {
    const center = parseIsoDate(centerIso || getTodayDateUtcKey());
    if (!center) return null;
    const span = Math.max(7, Number(days) || 14);
    const left = Math.floor((span - 1) / 2);
    const right = span - 1 - left;
    return {
      start: getDateKey(shiftDate(center, -left)),
      end: getDateKey(shiftDate(center, right))
    };
  }

  function rangeLabel(startIso, endIso) {
    const s = formatDateDe(startIso, { short: true }) || startIso || "";
    const e = formatDateDe(endIso, { short: true }) || endIso || "";
    return `${s} – ${e}`;
  }

  function _timelineCellBadges(day) {
    const chips = [];
    if (day?.phase?.deload) chips.push("DELOAD");
    if (day?.phase?.overreach) chips.push("OVERREACH");
    if (day?.flags?.sick) chips.push("SICK");
    if (day?.state?.rest) chips.push("REST");
    return chips;
  }

  function renderMiniTimelineStrip(container, payload, opts = {}) {
    if (!container) return;
    const selectedDate = opts.selectedDate || null;
    const onPick = typeof opts.onPick === "function" ? opts.onPick : null;
    const days = Array.isArray(payload?.days) ? payload.days : [];
    const todayIso = payload?.meta?.today || getTodayDateUtcKey();
    const html = days.map((day) => {
      const types = day?.types || {};
      const dominant = String(types?.dominant || "off").toLowerCase();
      const actual = day?.actual || {};
      const strength = actual?.strength || {};
      const run = actual?.run || {};
      const planned = day?.planned || {};
      const actualDid = !!(actual?.did || strength?.has || run?.has);
      const plannedHas = !!(planned?.has || planned?.kind);
      const improved = Number(strength?.improved_sets || 0);
      const worse = Number(strength?.worse_sets || 0);
      const classes = [
        "calendar-strip-day",
        dominant ? `is-type-${dominant}` : "",
        (day?.date === todayIso) ? "is-today" : "",
        (selectedDate && day?.date === selectedDate) ? "is-selected" : "",
        (actualDid ? "is-actual" : ""),
        (!actualDid && plannedHas ? "is-planned" : ""),
        day?.flags?.sick ? "is-sick" : ""
      ].filter(Boolean).join(" ");
      const mark = actualDid ? "●" : (plannedHas ? "○" : "");
      return `<button type="button" class="${classes}" data-date="${day?.date || ""}" title="${formatCalendarTooltip(day)}">
        <span class="calendar-strip-daynum">${day?.day || ""}</span>
        <span class="calendar-strip-mark">${mark}</span>
        <span class="calendar-strip-perf">${actualDid && strength?.has ? `▲${improved}${worse > 0 ? ` ▼${worse}` : ""}` : " "}</span>
      </button>`;
    }).join("");
    container.classList.add("calendar-mini-strip");
    container.innerHTML = `<div class="calendar-strip-track">${html}</div>`;
    const track = container.querySelector(".calendar-strip-track");
    if (track) {
      let down = false;
      let startX = 0;
      let startScroll = 0;
      track.addEventListener("pointerdown", (ev) => {
        down = true;
        startX = ev.clientX;
        startScroll = track.scrollLeft;
        track.setPointerCapture?.(ev.pointerId);
      });
      track.addEventListener("pointermove", (ev) => {
        if (!down) return;
        const dx = ev.clientX - startX;
        track.scrollLeft = startScroll - dx;
      });
      const endDrag = () => { down = false; };
      track.addEventListener("pointerup", endDrag);
      track.addEventListener("pointercancel", endDrag);
      track.addEventListener("pointerleave", endDrag);
    }
    container.querySelectorAll(".calendar-strip-day").forEach((btn) => {
      btn.addEventListener("click", () => {
        const d = btn.getAttribute("data-date");
        if (d && onPick) onPick({ date: d });
      });
    });
  }

  function renderCalendarWeekdays(container) {
    if (!container) return;
    container.innerHTML = "";
    CAL_WEEKDAYS.forEach((wd) => {
      const el = document.createElement("div");
      el.className = "calendar-weekday";
      el.textContent = wd;
      container.appendChild(el);
    });
  }

  function renderCalendarGrid(container, payload, opts = {}) {
    if (!container) return;
    const isModal = !!opts.modal;
    const selectedDate = opts.selectedDate || null;
    const onPick = typeof opts.onPick === "function" ? opts.onPick : null;
    const showBlocks = opts.showBlocks !== false;
    const showProblemsOnly = !!opts.showProblemsOnly;
    const focus = String(opts.focus || "types");
    const view = String(opts.view || "month");
    const filters = opts.filters || {};
    container.innerHTML = "";
    const weeks = Array.isArray(payload?.weeks) ? payload.weeks : [];
    const spans = Array.isArray(payload?.spans) ? payload.spans : [];
    const todayIso = payload?.meta?.today || getTodayDateUtcKey();

    weeks.forEach((week) => {
      const row = document.createElement("div");
      row.className = `calendar-week-row${isModal ? " is-modal" : ""}`;
      const barsLayer = document.createElement("div");
      barsLayer.className = "calendar-week-bars";
      const cellsLayer = document.createElement("div");
      cellsLayer.className = "calendar-week-cells";
      const weekCells = Array.isArray(week) ? week : [];
      const weekStart = weekCells[0]?.date;
      const weekEnd = weekCells[6]?.date;

      if (showBlocks && weekStart && weekEnd && spans.length) {
        spans.forEach((sp) => {
          if (!sp?.start || !sp?.end) return;
          const kind = String(sp.kind || "");
          if (kind === "deload" && filters.deload === false) return;
          if (kind === "overreach" && filters.functional_overreach === false) return;
          if (kind === "sick_cluster" && filters.sick === false) return;
          const s = sp.start > weekStart ? sp.start : weekStart;
          const e = sp.end < weekEnd ? sp.end : weekEnd;
          if (s > e) return;
          const leftIdx = Math.max(0, Math.min(6, Math.round((new Date(`${s}T00:00:00Z`) - new Date(`${weekStart}T00:00:00Z`)) / (24 * 3600 * 1000))));
          const rightIdx = Math.max(0, Math.min(6, Math.round((new Date(`${e}T00:00:00Z`) - new Date(`${weekStart}T00:00:00Z`)) / (24 * 3600 * 1000))));
          const bar = document.createElement("div");
          bar.className = `calendar-week-bar is-${sp.kind || "misc"}`;
          bar.style.left = `${(leftIdx / 7) * 100}%`;
          bar.style.width = `${((rightIdx - leftIdx + 1) / 7) * 100}%`;
          const startLabel = formatDateDe(s, { short: true });
          const endLabel = formatDateDe(e, { short: true });
          bar.title = `${sp.label || sp.kind || "Block"} · ${startLabel} → ${endLabel}`;
          barsLayer.appendChild(bar);
        });
      }

      weekCells.forEach((cell) => {
        const btn = document.createElement("button");
        btn.type = "button";
        btn.className = `calendar-cell${isModal ? " is-modal" : ""}`;
        const types = cell?.types || {};
        const dominant = String(types?.dominant || "").toLowerCase();
        const actualDid = !!(cell?.actual?.did_train ?? cell?.actual?.did);
        const plannedHas = !!(cell?.planned?.has_plan ?? cell?.planned?.has);
        const sick = !!cell?.flags?.sick;
        if (!cell?.in_month) btn.classList.add("is-outside");
        if (cell?.date === todayIso) btn.classList.add("is-today");
        if (cell?.date && selectedDate && cell.date === selectedDate) btn.classList.add("is-selected");
        if (cell?.phase?.deload) btn.classList.add("is-deload");
        if (cell?.phase?.overreach) btn.classList.add("is-overreach");
        const isRest = !!cell?.state?.rest;
        if (isRest) btn.classList.add("is-rest");
        if (dominant) btn.classList.add(`is-type-${dominant}`);
        if (sick) btn.classList.add("is-type-sick");
        btn.classList.add(`is-focus-${focus}`);
        const focusMeta = cell?.focus || {};
        if (focus === "load") {
          btn.dataset.focusLevel = String(Number(focusMeta.load || 0));
          btn.style.setProperty("--focus-color", "var(--accent-blue)");
        } else if (focus === "recovery") {
          btn.dataset.focusLevel = String(Number(focusMeta.recovery || 0));
          btn.style.setProperty("--focus-color", "var(--status-ok)");
        } else if (focus === "performance") {
          btn.dataset.focusLevel = String(Number(focusMeta.performance || 0));
          btn.style.setProperty("--focus-color", "var(--status-warn)");
        } else {
          btn.dataset.focusLevel = "0";
          btn.style.removeProperty("--focus-color");
        }

        const visibleByFilter =
          ((filters.strength !== false) || dominant !== "strength") &&
          ((filters.run !== false) || dominant !== "run") &&
          ((filters.sick !== false) || !sick) &&
          ((filters.rest !== false) || !isRest) &&
          ((filters.functional_overreach !== false) || !cell?.phase?.overreach);
        const perfScore = Number(cell?.perf?.score);
        const problemDay = (!!sick) || (!!cell?.phase?.overreach) || (Number.isFinite(perfScore) && perfScore < 0);
        if (!visibleByFilter || (showProblemsOnly && !problemDay)) btn.classList.add("is-filter-muted");
        btn.dataset.date = cell?.date || "";
        btn.title = formatCalendarTooltip(cell);
        if (view === "week") btn.classList.add("is-week-card");

        const day = document.createElement("div");
        day.className = "calendar-cell-day";
        day.textContent = String(cell?.day || "");
        btn.appendChild(day);

        const ready = cell?.readiness || {};
        const readyPip = document.createElement("div");
        readyPip.className = "calendar-cell-readiness";
        const rTrend = String(ready?.trend || "");
        if (rTrend === "up") readyPip.textContent = "●";
        else if (rTrend === "down") readyPip.textContent = "●";
        else if (ready?.score !== null && ready?.score !== undefined) readyPip.textContent = "•";
        if (rTrend === "up") readyPip.classList.add("is-up");
        if (rTrend === "down") readyPip.classList.add("is-down");
        btn.appendChild(readyPip);

        const indicators = document.createElement("div");
        indicators.className = "calendar-cell-indicators";

        if (actualDid) {
          const dot = document.createElement("span");
          dot.className = "calendar-dot is-actual";
          dot.title = "Gemacht";
          indicators.appendChild(dot);
        } else if (plannedHas) {
          const dot = document.createElement("span");
          dot.className = "calendar-dot is-planned";
          dot.title = "Geplant";
          indicators.appendChild(dot);

          const dIso = cell?.date || "";
          if (dIso && dIso < todayIso) {
            const miss = document.createElement("span");
            miss.className = "calendar-missed";
            miss.title = "Geplant, nicht geloggt";
            indicators.appendChild(miss);
          }
        }

        if (sick) {
          const badge = document.createElement("span");
          badge.className = "calendar-flag";
          badge.textContent = "+";
          badge.title = "Krank";
          indicators.appendChild(badge);
        }
        if (isRest) {
          const restBadge = document.createElement("span");
          restBadge.className = "calendar-rest-badge";
          restBadge.textContent = "REST";
          restBadge.title = "Geplanter Resttag";
          indicators.appendChild(restBadge);
        }

        const secondaryWrap = document.createElement("div");
        secondaryWrap.className = "calendar-secondary-types";
        const secondaries = Array.isArray(types?.secondary) ? types.secondary.slice(0, 2) : [];
        secondaries.forEach((t) => {
          const dd = document.createElement("span");
          dd.className = `calendar-type-dot is-${String(t || "").toLowerCase()}`;
          secondaryWrap.appendChild(dd);
        });

        btn.appendChild(indicators);
        btn.appendChild(secondaryWrap);

        const strength = cell?.actual?.strength || {};
        const perf = cell?.perf || {};
        if (strength?.has || Number.isFinite(Number(perf?.score))) {
          const perfEl = document.createElement("div");
          perfEl.className = "calendar-cell-perf";
          const plus = Number(strength?.improved_sets || 0);
          const minus = Number(strength?.worse_sets || 0);
          const score = Number.isFinite(Number(perf?.score)) ? Number(perf.score) : (plus - minus);
          perfEl.classList.add(score > 0 ? "is-pos" : (score < 0 ? "is-neg" : "is-flat"));
          perfEl.textContent = `${score > 0 ? "+" : ""}${score}`;
          btn.appendChild(perfEl);
        }

        const plannedTitle = String(cell?.planned?.plan_title || cell?.planned?.title || "").trim();
        if (plannedTitle) {
          const token = document.createElement("div");
          token.className = "calendar-cell-plan-token";
          token.textContent = plannedTitle.slice(0, 7);
          btn.appendChild(token);
        }

        if (isModal) {
          const micro = document.createElement("div");
          micro.className = "calendar-focus-micro";
          const lvLoad = Math.max(0, Math.min(3, Number(focusMeta.load || 0)));
          const lvPerf = Math.max(0, Math.min(3, Number(focusMeta.performance || 0)));
          const lvRec = Math.max(0, Math.min(3, Number(focusMeta.recovery || 0)));
          micro.innerHTML = `
            <span class="calendar-micro-band is-load lv-${lvLoad}"></span>
            <span class="calendar-micro-band is-performance lv-${lvPerf}"></span>
            <span class="calendar-micro-band is-recovery lv-${lvRec}"></span>
          `;
          btn.appendChild(micro);
        }

        if (view === "week") {
          const weekMeta = document.createElement("div");
          weekMeta.className = "calendar-week-meta";
          const plannedTitle = cell?.planned?.title || (cell?.planned?.kind === "off" ? "Off" : null);
          const actualSummary = cell?.actual?.summary || cell?.actual?.title || null;
          weekMeta.innerHTML = `
            <div class="calendar-week-plan">${plannedTitle ? `GEPLANT · ${plannedTitle}` : "GEPLANT · —"}</div>
            <div class="calendar-week-actual">${actualSummary ? `GEMACHT · ${actualSummary}` : "GEMACHT · —"}</div>
          `;
          btn.appendChild(weekMeta);
        }

        if (onPick) {
          btn.addEventListener("click", () => onPick(cell));
        }
        cellsLayer.appendChild(btn);
      });
      row.appendChild(barsLayer);
      row.appendChild(cellsLayer);
      container.appendChild(row);
    });
  }

  function setMiniFooter(cell) {
    if (!els.calendarMiniFooter) return;
    if (!cell) {
      els.calendarMiniFooter.textContent = "Tag wählen…";
      return;
    }
    const actual = cell.actual || {};
    const planned = cell.planned || {};
    const phase = cell.phase || {};
    const actualDid = !!(actual.did_train ?? actual.did);
    const plannedHas = !!(planned.has_plan ?? planned.has);
    const plannedTitle = planned.plan_title || planned.title || null;
    const dateLabel = formatDateDe(cell.date, { weekday: true, short: true });
    const phaseBits = [];
    if (phase.deload) phaseBits.push("Deload");
    if (phase.overreach) phaseBits.push("Overreach");
    const isRest = !!cell?.state?.rest;
    if (actualDid) {
      const txt = actual.toplift || actual.summary || actual.session_title || actual.title || "Training";
      let line = `${dateLabel}: GEMACHT · ${txt}`;
      if (phaseBits.length) line += ` · ${phaseBits.join(" · ")}`;
      if (isRest) line += " · REST";
      els.calendarMiniFooter.textContent = line;
      return;
    }
    if (plannedHas) {
      let line = `${dateLabel}: GEPLANT · ${plannedTitle || (planned.kind === "off" ? "Off" : "Training")}`;
      if (phaseBits.length) line += ` · ${phaseBits.join(" · ")}`;
      if (isRest) line += " · REST";
      els.calendarMiniFooter.textContent = line;
      return;
    }
    els.calendarMiniFooter.textContent = `${dateLabel}: Kein Eintrag`;
  }

  function findCalendarCell(payload, dateIso) {
    const weeks = Array.isArray(payload?.weeks) ? payload.weeks : [];
    for (const week of weeks) {
      for (const cell of (Array.isArray(week) ? week : [])) {
        if (cell?.date === dateIso) return cell;
      }
    }
    return null;
  }

  function updateCalendarModalSelectors(monthIso) {
    if (!els.calendarModalMonthSelect || !els.calendarModalYearSelect) return;
    const month = Number(monthIso?.slice(5, 7) || "1");
    const year = Number(monthIso?.slice(0, 4) || new Date().getUTCFullYear());
    if (!els.calendarModalMonthSelect.dataset.ready) {
      els.calendarModalMonthSelect.innerHTML = "";
      for (let idx = 0; idx < 12; idx += 1) {
        const dt = new Date(Date.UTC(2026, idx, 1));
        const name = dt.toLocaleDateString("de-DE", { month: "long", timeZone: "UTC" });
        const opt = document.createElement("option");
        opt.value = String(idx + 1).padStart(2, "0");
        opt.textContent = name;
        els.calendarModalMonthSelect.appendChild(opt);
      }
      els.calendarModalMonthSelect.dataset.ready = "1";
    }
    if (!els.calendarModalYearSelect.dataset.ready) {
      els.calendarModalYearSelect.innerHTML = "";
      const nowYear = Number(new Date().toLocaleDateString("en-CA", { year: "numeric", timeZone: TIME_ZONE }));
      for (let y = nowYear - 5; y <= nowYear + 2; y += 1) {
        const opt = document.createElement("option");
        opt.value = String(y);
        opt.textContent = String(y);
        els.calendarModalYearSelect.appendChild(opt);
      }
      els.calendarModalYearSelect.dataset.ready = "1";
    }
    els.calendarModalMonthSelect.value = String(month).padStart(2, "0");
    if (!Array.from(els.calendarModalYearSelect.options).some((o) => o.value === String(year))) {
      const opt = document.createElement("option");
      opt.value = String(year);
      opt.textContent = String(year);
      els.calendarModalYearSelect.appendChild(opt);
    }
    els.calendarModalYearSelect.value = String(year);
    if (els.calendarModalMonthLabel) {
      els.calendarModalMonthLabel.textContent = monthLabel(`${year}-${String(month).padStart(2, "0")}`);
    }
  }

  function sliceDaysToWeeks(days) {
    const out = [];
    for (let i = 0; i < days.length; i += 7) out.push(days.slice(i, i + 7));
    return out;
  }

  function buildWeeksPayloadFromRange(rangePayload, view = "timeline") {
    const days = Array.isArray(rangePayload?.days) ? rangePayload.days : [];
    if (!days.length) return { meta: rangePayload?.meta || {}, weeks: [], spans: rangePayload?.spans || [] };
    const dayMap = new Map(days.map((d) => [d.date, d]));
    const first = days[0]?.date;
    const last = days[days.length - 1]?.date;
    if (!first || !last) return { meta: rangePayload?.meta || {}, weeks: [], spans: rangePayload?.spans || [] };

    if (view === "week") {
      const sel = state.calendarSelectedDate || first;
      const selDt = parseIsoDate(sel) || parseIsoDate(first);
      if (!selDt) return { meta: rangePayload?.meta || {}, weeks: [], spans: rangePayload?.spans || [] };
      const start = shiftDate(selDt, -selDt.getUTCDay() + 1);
      const cells = [];
      for (let i = 0; i < 7; i += 1) {
        const iso = getDateKey(shiftDate(start, i));
        const base = dayMap.get(iso) || { date: iso, day: Number(iso?.slice(8, 10)), in_month: true, types: { dominant: "none", secondary: [] }, planned: { has: false, title: null, kind: null }, actual: { did: false, title: null }, flags: { sick: false, alcohol: false }, phase: { deload: false, overreach: false }, state: { rest: false }, focus: { load: 0, recovery: 0, performance: 0 } };
        cells.push(base);
      }
      return { meta: rangePayload.meta, weeks: [cells], spans: rangePayload?.spans || [] };
    }

    if (view === "month") {
      const monthIso = state.calendarMonth || first.slice(0, 7);
      const bounds = monthStartEnd(monthIso);
      if (!bounds) return { meta: rangePayload?.meta || {}, weeks: sliceDaysToWeeks(days), spans: rangePayload?.spans || [] };
      const startDt = parseIsoDate(bounds.start);
      const endDt = parseIsoDate(bounds.end);
      if (!startDt || !endDt) return { meta: rangePayload?.meta || {}, weeks: sliceDaysToWeeks(days), spans: rangePayload?.spans || [] };
      const gridStart = shiftDate(startDt, -((startDt.getUTCDay() + 6) % 7));
      const gridEnd = shiftDate(endDt, 6 - ((endDt.getUTCDay() + 6) % 7));
      const cells = [];
      for (let d = gridStart; d <= gridEnd; d = shiftDate(d, 1)) {
        const iso = getDateKey(d);
        const inMonth = iso?.slice(0, 7) === monthIso;
        const base = dayMap.get(iso) || { date: iso, day: Number(iso?.slice(8, 10)), in_month: inMonth, types: { dominant: "none", secondary: [] }, planned: { has: false, title: null, kind: null }, actual: { did: false, title: null }, flags: { sick: false, alcohol: false }, phase: { deload: false, overreach: false }, state: { rest: false }, focus: { load: 0, recovery: 0, performance: 0 } };
        base.in_month = inMonth;
        cells.push(base);
      }
      return { meta: rangePayload.meta, weeks: sliceDaysToWeeks(cells), spans: rangePayload?.spans || [] };
    }

    return { meta: rangePayload.meta, weeks: sliceDaysToWeeks(days), spans: rangePayload?.spans || [] };
  }

  function currentViewRange() {
    const monthIso = state.calendarMonth || currentMonthIsoBerlin();
    const bounds = monthStartEnd(monthIso);
    const today = getTodayDateUtcKey();
    if (!bounds || !today) return null;
    if (state.calendarView === "month") {
      const s = parseIsoDate(bounds.start);
      const e = parseIsoDate(bounds.end);
      if (!s || !e) return null;
      const gridStart = getDateKey(shiftDate(s, -((s.getUTCDay() + 6) % 7)));
      const gridEnd = getDateKey(shiftDate(e, 6 - ((e.getUTCDay() + 6) % 7)));
      return { start: gridStart, end: gridEnd };
    }
    if (state.calendarView === "week") {
      const sel = parseIsoDate(state.calendarSelectedDate || today) || parseIsoDate(today);
      if (!sel) return null;
      const start = getDateKey(shiftDate(sel, -((sel.getUTCDay() + 6) % 7)));
      const end = getDateKey(shiftDate(parseIsoDate(start), 6));
      return { start, end };
    }
    if (state.calendarView === "year") {
      const year = Number(state.calendarNavYear || monthIso.slice(0, 4));
      return { start: `${year}-01-01`, end: `${year}-12-31` };
    }
    if (state.calendarView === "timeline") {
      const sel = parseIsoDate(state.calendarSelectedDate || today) || parseIsoDate(today);
      if (!sel) return null;
      const start = getDateKey(shiftDate(sel, -42));
      const end = getDateKey(shiftDate(sel, 41));
      return { start, end };
    }
    const monthStart = parseIsoDate(bounds.start);
    if (!monthStart) return null;
    const monday = getDateKey(shiftDate(monthStart, -((monthStart.getUTCDay() + 6) % 7)));
    const end = getDateKey(shiftDate(parseIsoDate(monday), 27));
    return { start: monday, end };
  }

  async function fetchCalendarRange(startIso, endIso, force = false) {
    if (!startIso || !endIso) return null;
    const mode = state.calendarMode || "both";
    const key = `${startIso}|${endIso}|${mode}`;
    const now = Date.now();
    const cached = state.calendarRangeCache.get(key);
    if (!force && cached && (now - cached.ts) < 60_000) return cached.payload;
    const payload = await safeJson(`/api/calendar/timeline_v1?start=${encodeURIComponent(startIso)}&end=${encodeURIComponent(endIso)}&mode=${encodeURIComponent(mode)}`);
    if (payload && Array.isArray(payload.days)) {
      state.calendarRangeCache.set(key, { ts: now, payload });
      return payload;
    }
    return cached?.payload || null;
  }

  function renderSpanLane(rangePayload) {
    if (!els.calendarFullBars) return;
    const days = Array.isArray(rangePayload?.days) ? rangePayload.days : [];
    const spans = Array.isArray(rangePayload?.spans) ? rangePayload.spans : [];
    if (!spans.length || !days.length) {
      els.calendarFullBars.innerHTML = `<div class="calendar-span-empty">Keine Phasen im Monat</div>`;
      return;
    }
    const start = days[0]?.date;
    const end = days[days.length - 1]?.date;
    const total = Math.max(1, days.length);
    const dayIdx = new Map(days.map((d, i) => [d.date, i]));
    const todayIso = String(rangePayload?.meta?.today || getTodayDateUtcKey() || "");
    const rows = {
      overreach: [],
      deload: [],
      sick_cluster: [],
    };
    spans.forEach((sp) => {
      if (!sp?.start || !sp?.end) return;
      let s = sp.start < start ? start : sp.start;
      let e = sp.end > end ? end : sp.end;
      // hard frontend guard: no future phase bars.
      if (s > todayIso) return;
      if (e > todayIso) e = todayIso;
      if (s > e) return;
      const sIdx = dayIdx.has(s) ? dayIdx.get(s) : 0;
      const eIdx = dayIdx.has(e) ? dayIdx.get(e) : (total - 1);
      const left = (Number(sIdx) / total) * 100;
      const width = ((Number(eIdx) - Number(sIdx) + 1) / total) * 100;
      const why = sp?.why || {};
      const reasons = Array.isArray(sp?.trigger_reasons) ? sp.trigger_reasons.join(", ") : "";
      const info = `days=${why.days ?? "—"}${reasons ? ` · ${reasons}` : ""}`;
      const kind = String(sp.kind || "");
      if (!(kind in rows)) return;
      rows[kind].push(`<button type="button" class="calFull__band is-${kind}" data-span-start="${s}" data-span-end="${e}" style="left:${left}%;width:${width}%;" title="${info}">${sp.label || (kind === "overreach" ? "FO" : kind.toUpperCase())}</button>`);
    });
    const rowHtml = (kind, label) => `
      <div class="calFull__bandRow is-${kind}">
        <span class="calFull__bandLabel">${label}</span>
        <div class="calFull__bandTrack">${rows[kind].join("")}</div>
      </div>
    `;
    els.calendarFullBars.innerHTML = `
      <div class="calFull__bands">
        ${rowHtml("overreach", "FO")}
        ${rowHtml("deload", "Deload")}
        ${rowHtml("sick_cluster", "Sick")}
      </div>
    `;
  }

  function renderYearTiles() {
    if (!els.calendarYearTiles) return;
    const base = state.calendarMonth || currentMonthIsoBerlin();
    const y = Number(state.calendarNavYear || (base || "").slice(0, 4)) || Number(new Date().toLocaleDateString("en-CA", { year: "numeric", timeZone: TIME_ZONE }));
    const mNow = Number((base || "").slice(5, 7)) || 1;
    state.calendarNavYear = y;
    if (els.calendarNavYearLabel) els.calendarNavYearLabel.textContent = String(y);
    const parts = [];
    for (let m = 1; m <= 12; m += 1) {
      const iso = `${y}-${String(m).padStart(2, "0")}`;
      const active = m === mNow ? "is-active" : "";
      const name = new Date(Date.UTC(y, m - 1, 1)).toLocaleDateString("de-DE", { month: "short", timeZone: "UTC" });
      parts.push(`<button type="button" class="calendar-year-tile ${active}" data-month-jump="${iso}">${name}</button>`);
    }
    els.calendarYearTiles.innerHTML = parts.join("");
  }

  function renderFocusLegend() {
    if (!els.calendarFocusLegend) return;
    if (state.calendarFocus === "types") {
      els.calendarFocusLegend.innerHTML = `
        <span class="calendar-legend-chip"><span class="calendar-legend-dot is-strength"></span>Strength</span>
        <span class="calendar-legend-chip"><span class="calendar-legend-dot is-run"></span>Run</span>
        <span class="calendar-legend-chip"><span class="calendar-legend-dot is-sick"></span>Sick</span>
      `;
      return;
    }
    const label = state.calendarFocus === "load" ? "Load Level" : (state.calendarFocus === "recovery" ? "Recovery Cost" : "Performance Signal");
    els.calendarFocusLegend.innerHTML = `
      <span class="calendar-legend-chip">${label}</span>
      <span class="calendar-legend-chip">0</span>
      <span class="calendar-legend-chip">1</span>
      <span class="calendar-legend-chip">2</span>
      <span class="calendar-legend-chip">3</span>
    `;
  }

  function renderPhaseSummary(rangePayload) {
    if (!els.calendarPhaseSummary) return;
    if (state.calendarView !== "month") {
      els.calendarPhaseSummary.innerHTML = "";
      return;
    }
    const days = Array.isArray(rangePayload?.days) ? rangePayload.days : [];
    const monthIso = state.calendarMonth || "";
    const rows = days.filter((d) => String(d?.date || "").startsWith(monthIso));
    const strength = rows.filter((d) => String(d?.types?.dominant || "") === "strength").length;
    const run = rows.filter((d) => String(d?.types?.dominant || "") === "run").length;
    const sick = rows.filter((d) => !!d?.flags?.sick).length;
    const spans = Array.isArray(rangePayload?.spans) ? rangePayload.spans : [];
    const deload = spans.filter((s) => s?.kind === "deload").length;
    const overreach = spans.filter((s) => s?.kind === "overreach").length;
    els.calendarPhaseSummary.innerHTML = `
      <span class="calendar-legend-chip">Strength ${strength}</span>
      <span class="calendar-legend-chip">Run ${run}</span>
      <span class="calendar-legend-chip">Sick ${sick}</span>
      <button type="button" class="calendar-legend-chip" data-jump-kind="deload">Deload ${deload}</button>
      <button type="button" class="calendar-legend-chip" data-jump-kind="overreach">Overreach ${overreach}</button>
    `;
  }

  async function jumpToSpanBoundary(direction = "next") {
    const spans = Array.isArray(state.calendarRangePayload?.spans) ? state.calendarRangePayload.spans : [];
    if (!spans.length) return;
    const sel = state.calendarSelectedDate || getTodayDateUtcKey();
    if (!sel) return;
    const points = [];
    spans.forEach((s) => {
      if (s?.start) points.push(String(s.start));
      if (s?.end) points.push(String(s.end));
    });
    const uniq = Array.from(new Set(points)).sort();
    if (!uniq.length) return;
    let target = null;
    if (direction === "prev") {
      for (let i = uniq.length - 1; i >= 0; i -= 1) {
        if (uniq[i] < sel) {
          target = uniq[i];
          break;
        }
      }
    } else {
      for (let i = 0; i < uniq.length; i += 1) {
        if (uniq[i] > sel) {
          target = uniq[i];
          break;
        }
      }
    }
    if (!target) return;
    state.calendarSelectedDate = target;
    state.calendarMonth = target.slice(0, 7);
    await renderCalendarExplorer(true);
    await selectCalendarDate(target, true);
  }

  function renderExplorerContext(cell) {
    if (!els.calendarFullSummary) return;
    if (!cell) {
      els.calendarFullSummary.textContent = "Tag wählen…";
      return;
    }
    const d = formatDateDe(cell.date, { weekday: true, short: true }) || cell.date;
    const planned = cell?.planned || {};
    const actual = cell?.actual || {};
    const strength = actual?.strength || {};
    const run = actual?.run || {};
    const perf = cell?.perf || {};
    const bits = [];
    if (planned?.title) bits.push(`Geplant ${planned.title}`);
    if (strength?.has) bits.push(`+${Number(strength.improved_sets || 0)} / -${Number(strength.worse_sets || 0)}`);
    if (run?.has) bits.push(`Run ${run.distance_km ? `${Number(run.distance_km).toFixed(1)} km` : ""}`.trim());
    if (Number.isFinite(Number(perf?.score))) bits.push(`Perf ${Number(perf.score) > 0 ? "+" : ""}${Number(perf.score)}`);
    els.calendarFullSummary.textContent = `${d}: ${bits.join(" · ") || "Kein Eintrag"}`;
  }

  function renderCalFullWeekdays() {
    if (!els.calendarModalWeekdays) return;
    els.calendarModalWeekdays.innerHTML = CAL_WEEKDAYS.map((wd) => `<div class="calFull__weekday">${wd}</div>`).join("");
  }

  function renderCalFullCounts(live) {
    if (!els.calendarFullCounts) return;
    const days = Array.isArray(live?.days) ? live.days : [];
    const bars = Array.isArray(live?.bars) ? live.bars : [];
    const strengthDays = days.filter((d) => d?.actual?.strength?.has).length;
    const runDays = days.filter((d) => d?.actual?.run?.has).length;
    const sickDays = days.filter((d) => d?.flags?.sick).length;
    const deloadBars = bars.filter((b) => b?.kind === "deload").length;
    const foBars = bars.filter((b) => b?.kind === "functional_overreach").length;
    els.calendarFullCounts.innerHTML = `
      <span class="calendar-legend-chip">Strength ${strengthDays}</span>
      <span class="calendar-legend-chip">Run ${runDays}</span>
      <span class="calendar-legend-chip">Sick ${sickDays}</span>
      <span class="calendar-legend-chip">Deload ${deloadBars}</span>
      <span class="calendar-legend-chip">FO ${foBars}</span>
    `;
  }

  function renderCalFullGrid(gridPayload) {
    if (!els.calendarModalGrid) return;
    const weeks = Array.isArray(gridPayload?.weeks) ? gridPayload.weeks : [];
    const todayIso = gridPayload?.meta?.today || getTodayDateUtcKey();
    const filters = state.calendarFilters || {};
    const problemsOnly = !!state.calendarShowProblemsOnly;
    const cells = [];
    weeks.forEach((week) => {
      (week || []).forEach((cell) => {
        const types = cell?.types || {};
        const dominant = String(types?.dominant || "off").toLowerCase();
        const planned = cell?.planned || {};
        const actual = cell?.actual || {};
        const strength = actual?.strength || {};
        const run = actual?.run || {};
        const perf = Number(cell?.perf?.score);
        const sick = !!cell?.flags?.sick;
        const rest = !!cell?.state?.rest;
        const isProblem = sick || !!cell?.phase?.overreach || (Number.isFinite(perf) && perf < 0);
        const visibleByFilter =
          ((filters.strength !== false) || dominant !== "strength") &&
          ((filters.run !== false) || dominant !== "run") &&
          ((filters.sick !== false) || !sick) &&
          ((filters.rest !== false) || !rest) &&
          ((filters.functional_overreach !== false) || !cell?.phase?.overreach) &&
          ((filters.deload !== false) || !cell?.phase?.deload);
        const muted = (!visibleByFilter) || (problemsOnly && !isProblem);
        const token = String(planned?.plan_title || planned?.title || "").trim();
        const plus = Number(strength?.improved_sets || 0);
        const minus = Number(strength?.worse_sets || 0);
        const pip = cell?.readiness?.trend === "up" ? "is-up" : (cell?.readiness?.trend === "down" ? "is-down" : "");
        const typeDots = `${strength?.has ? '<i class="dot is-strength"></i>' : ''}${run?.has ? '<i class="dot is-run"></i>' : ''}${sick ? '<i class="dot is-sick"></i>' : ''}`;
        const perfBadges = strength?.has
          ? `<span class="calFull__perf is-pos">+${plus}</span>${minus > 0 ? `<span class="calFull__perf is-neg">-${minus}</span>` : ""}`
          : "";
        cells.push(`
          <button type="button"
            class="calFull__day ${cell?.in_month ? "" : "is-outside"} ${cell?.date === todayIso ? "is-today" : ""} ${state.calendarSelectedDate === cell?.date ? "is-selected" : ""} is-type-${dominant} ${cell?.phase?.deload ? "is-deload" : ""} ${cell?.phase?.overreach ? "is-overreach" : ""} ${sick ? "is-sick" : ""} ${muted ? "is-muted" : ""}"
            data-date="${cell?.date || ""}"
            title="${formatCalendarTooltip(cell)}">
            <span class="calFull__top">
              <span class="calFull__dayNum">${cell?.day || ""}</span>
              <span class="calFull__ready ${pip}"></span>
            </span>
            <span class="calFull__mid">
              <span class="calFull__token">${token ? token.slice(0, 10) : (rest ? "Rest" : "")}</span>
            </span>
            <span class="calFull__bottom">
              <span class="calFull__dots">${typeDots}</span>
              <span class="calFull__perfWrap">${perfBadges}</span>
            </span>
          </button>
        `);
      });
    });
    els.calendarModalGrid.innerHTML = cells.join("");
    els.calendarModalGrid.querySelectorAll(".calFull__day").forEach((el) => {
      el.addEventListener("mouseenter", () => {
        const d = el.getAttribute("data-date");
        if (!d) return;
        const c = weeks.flat().find((x) => x?.date === d);
        if (c) renderExplorerContext(c);
      });
      el.addEventListener("click", async () => {
        const d = el.getAttribute("data-date");
        if (!d) return;
        await selectCalendarDate(d, false);
      });
    });
  }

  function renderTimelineExplorerTrack(payload) {
    if (!els.calendarModalGrid) return;
    const days = Array.isArray(payload?.days) ? payload.days : [];
    const todayIso = payload?.meta?.today || getTodayDateUtcKey();
    const selected = state.calendarSelectedDate;
    const html = days.map((day) => {
      const d = day?.date || "";
      const types = day?.types || {};
      const dominant = String(types?.dominant || "off").toLowerCase();
      const actual = day?.actual || {};
      const strength = actual?.strength || {};
      const run = actual?.run || {};
      const actualDid = !!(actual?.did || strength?.has || run?.has);
      const plannedHas = !!(day?.planned?.has || day?.planned?.kind);
      const cls = [
        "calendar-tl-day",
        dominant ? `is-type-${dominant}` : "",
        actualDid ? "is-actual" : "",
        (!actualDid && plannedHas) ? "is-planned" : "",
        day?.flags?.sick ? "is-sick" : "",
        day?.state?.rest ? "is-rest" : "",
        day?.phase?.deload ? "is-deload" : "",
        day?.phase?.overreach ? "is-overreach" : "",
        d === todayIso ? "is-today" : "",
        selected && d === selected ? "is-selected" : "",
      ].filter(Boolean).join(" ");
      const up = Number(strength?.improved_sets || 0);
      const down = Number(strength?.worse_sets || 0);
      const badges = _timelineCellBadges(day).map((b) => `<span class="calendar-tl-badge">${b}</span>`).join("");
      return `<button type="button" class="${cls}" data-date="${d}" title="${formatCalendarTooltip(day)}">
        <span class="calendar-tl-date">${formatDateDe(d, { short: true }) || d}</span>
        <span class="calendar-tl-markers">${actualDid ? "●" : (plannedHas ? "○" : "")}</span>
        <span class="calendar-tl-perf">${strength?.has ? `▲${up}${down > 0 ? ` ▼${down}` : ""}` : ""}</span>
        <span class="calendar-tl-badges">${badges}</span>
      </button>`;
    }).join("");
    els.calendarModalGrid.classList.remove("is-year-view");
    els.calendarModalGrid.innerHTML = `<div class="calendar-timeline-track" id="calendar-timeline-track">${html}</div>`;
    const track = byId("calendar-timeline-track");
    if (track && selected) {
      const selEl = track.querySelector(`[data-date="${selected}"]`);
      selEl?.scrollIntoView?.({ block: "nearest", inline: "center" });
    }
    if (track) {
      let down = false;
      let startX = 0;
      let startScroll = 0;
      track.addEventListener("pointerdown", (ev) => {
        down = true;
        startX = ev.clientX;
        startScroll = track.scrollLeft;
        track.setPointerCapture?.(ev.pointerId);
      });
      track.addEventListener("pointermove", (ev) => {
        if (!down) return;
        const dx = ev.clientX - startX;
        track.scrollLeft = startScroll - dx;
      });
      const endDrag = () => { down = false; };
      track.addEventListener("pointerup", endDrag);
      track.addEventListener("pointercancel", endDrag);
      track.addEventListener("pointerleave", endDrag);
    }
    els.calendarModalGrid.querySelectorAll(".calendar-tl-day").forEach((btn) => {
      btn.addEventListener("click", async () => {
        const d = btn.getAttribute("data-date");
        if (!d) return;
        await selectCalendarDate(d, true);
        els.calendarDayPanel?.setAttribute("aria-hidden", "false");
        els.calendarDayPanel?.classList.add("is-open");
      });
    });
  }

  function renderYearView(rangePayload) {
    if (!els.calendarModalGrid) return;
    const days = Array.isArray(rangePayload?.days) ? rangePayload.days : [];
    const y = Number((state.calendarMonth || currentMonthIsoBerlin() || "").slice(0, 4)) || Number(new Date().toLocaleDateString("en-CA", { year: "numeric", timeZone: TIME_ZONE }));
    const byMonth = new Map();
    for (let m = 1; m <= 12; m += 1) byMonth.set(m, []);
    const spans = Array.isArray(rangePayload?.spans) ? rangePayload.spans : [];
    days.forEach((d) => {
      const m = Number(String(d?.date || "").slice(5, 7));
      if (m >= 1 && m <= 12) byMonth.get(m).push(d);
    });

    els.calendarModalWeekdays.innerHTML = "";
    els.calendarModalGrid.classList.add("is-year-view");
    const cards = [];
    for (let m = 1; m <= 12; m += 1) {
      const monthIso = `${y}-${String(m).padStart(2, "0")}`;
      const rows = byMonth.get(m) || [];
      const trainCount = rows.filter((d) => d?.actual?.did).length;
      const intensity = rows.map((d) => Number(d?.focus?.load || 0));
      const maxLv = intensity.length ? Math.max(...intensity) : 0;
      const name = new Date(Date.UTC(y, m - 1, 1)).toLocaleDateString("de-DE", { month: "long", timeZone: "UTC" });
      const heat = rows.slice(0, 31).map((d) => `<span class="calendar-year-heat lv-${Math.max(0, Math.min(3, Number(d?.focus?.load || 0)))}"></span>`).join("");
      const monthStart = `${y}-${String(m).padStart(2, "0")}-01`;
      const monthEnd = getDateKey(new Date(Date.UTC(y, m, 0)));
      const monthSpans = spans.filter((sp) => sp?.start && sp?.end && sp.start <= monthEnd && sp.end >= monthStart);
      const monthBars = monthSpans.slice(0, 2).map((sp) => `<span class="calendar-year-span is-${sp.kind || "misc"}">${sp.label || sp.kind}</span>`).join("");
      cards.push(`
        <button type="button" class="calendar-year-month-card" data-month-jump="${monthIso}" title="${name} ${y}">
          <div class="calendar-year-month-title">${name}</div>
          <div class="calendar-year-month-sub">${trainCount} Trainingstage · Lv ${maxLv}</div>
          <div class="calendar-year-span-row">${monthBars}</div>
          <div class="calendar-year-heat-grid">${heat}</div>
        </button>
      `);
    }
    els.calendarModalGrid.innerHTML = cards.join("");
  }

  async function renderCalendarExplorer(force = false) {
    if (els.calendarModal?.getAttribute("aria-hidden") !== "false") return;
    const monthIso = state.calendarMonth || currentMonthIsoBerlin();
    const live = await fetchCalendarMonth(monthIso, force);
    if (!live) return;
    const gridPayload = monthLiveToGridPayload(live);
    state.calendarRangePayload = { days: live.days || [], spans: gridPayload.spans || [] };
    if (els.calendarModalMonthLabel) {
      els.calendarModalMonthLabel.textContent = monthLabel(monthIso);
    }
    renderSpanLane({ days: live.days || [], spans: gridPayload.spans || [] });
    renderCalFullWeekdays();
    renderCalFullGrid(gridPayload);
    renderCalFullCounts(live);
    const selected = state.calendarSelectedDate ? (live.days || []).find((d) => d.date === state.calendarSelectedDate) : null;
    renderExplorerContext(selected);
  }

  async function fetchCalendarMonth(monthIso, force = false) {
    if (!monthIso) return null;
    const mode = state.calendarMode || "both";
    const cacheKey = `${monthIso}|${mode}`;
    const now = Date.now();
    const cached = state.calendarCache.get(cacheKey);
    if (!force && cached && (now - cached.ts) < 60_000) {
      return cached.payload;
    }
    const payload = await safeJson(`/api/calendar/month_live_v1?month=${encodeURIComponent(monthIso)}&mode=${encodeURIComponent(mode)}`);
    if (payload && Array.isArray(payload.days)) {
      state.calendarCache.set(cacheKey, { ts: now, payload });
      return payload;
    }
    return cached?.payload || null;
  }

  async function fetchCalendarDay(dateIso, force = false) {
    if (!dateIso) return null;
    const now = Date.now();
    const cached = state.calendarDayCache.get(dateIso);
    if (!force && cached && (now - cached.ts) < 60_000) return cached.payload;
    const payload = await safeJson(`/api/dashboard/calendar_day_v2?date=${encodeURIComponent(dateIso)}`);
    if (payload && payload.day) {
      state.calendarDayCache.set(dateIso, { ts: now, payload });
      return payload;
    }
    return cached?.payload || null;
  }

  function renderDayPanel(payload) {
    if (!els.calendarDayPanel) return;
    const day = payload?.day;
    if (!day) {
      els.calendarDayPanel.innerHTML = `<div class=\"calendar-day-empty\">Keine Details.</div>`;
      return;
    }

    const actual = day.actual || {};
    const planned = day.planned || {};
    const phase = day.phase || {};
    const flags = day.flags || {};
    const workouts = Array.isArray(actual.workouts) ? actual.workouts : [];
    const runs = Array.isArray(actual.runs) ? actual.runs : [];
    const strength = actual.strength || {};
    const runQuick = actual.run || {};
    const plannedTitle = planned.plan_title || planned.title || null;
    const rangeSpans = Array.isArray(state.calendarRangePayload?.spans) ? state.calendarRangePayload.spans : [];
    const activeSpan = day?.date
      ? rangeSpans.find((s) => s?.start && s?.end && day.date >= s.start && day.date <= s.end)
      : null;
    let insight = "";
    if (activeSpan?.kind === "overreach" || activeSpan?.kind === "functional_overreach") {
      const reasons = (activeSpan?.trigger_reasons || []).join(", ");
      insight = `Overreach: ${reasons || "Load-Kontext"} (confidence: ${activeSpan?.confidence || "med"})`;
    } else if (activeSpan?.kind === "deload") {
      const reasons = (activeSpan?.trigger_reasons || []).join(", ");
      insight = `Deload: ${reasons || "phase"} (${activeSpan?.confidence || "med"})`;
    }
    if (!insight && day?.state?.rest) insight = "REST — geplant";
    if (!insight && !flags?.sick) insight = "Normaler Tag";

    const badges = [];
    if (phase.deload) badges.push("<span class=\"calendar-detail-badge\">DELOAD</span>");
    if (phase.overreach || phase.functional_overreach) badges.push("<span class=\"calendar-detail-badge\">OVERREACH</span>");
    if (day?.state?.rest) badges.push("<span class=\"calendar-detail-badge\">REST</span>");
    if (flags.sick) badges.push("<span class=\"calendar-detail-badge\">Krank</span>");
    if (flags.alcohol) badges.push("<span class=\"calendar-detail-badge\">Alkohol</span>");

    const workoutRows = workouts.length
      ? workouts.map((w) => `<div class=\"calendar-detail-row\"><strong>${w.title || "Workout"}</strong><span>${w.summary || "—"}${Number.isFinite(w.avg_rpe) ? ` · RPE ${Number(w.avg_rpe).toFixed(1)}` : ""}</span></div>`).join("")
      : (strength?.has
        ? `<div class=\"calendar-detail-row\"><strong>${strength.title || "Strength"}</strong><span>Sets ${Number(strength.total_sets || 0)} · ▲${Number(strength.improved_sets || 0)}${Number(strength.worse_sets || 0) > 0 ? ` · ▼${Number(strength.worse_sets || 0)}` : ""}</span></div>`
        : `<div class=\"calendar-detail-empty\">Kein Training geloggt</div>`);
    const runRows = runs.length
      ? runs.map((r) => `<div class=\"calendar-detail-row\"><strong>Run</strong><span>${r.duration_min ? `${Math.round(r.duration_min)} min` : "—"}${r.distance_km ? ` · ${r.distance_km.toFixed(1)} km` : ""}</span></div>`).join("")
      : (runQuick?.has
        ? `<div class=\"calendar-detail-row\"><strong>Run</strong><span>${runQuick.time_s ? `${Math.round(Number(runQuick.time_s) / 60)} min` : "—"}${runQuick.distance_km ? ` · ${Number(runQuick.distance_km).toFixed(1)} km` : ""}</span></div>`
        : `<div class=\"calendar-detail-empty\">Kein Run geloggt</div>`);

    els.calendarDayPanel.innerHTML = `
      <div class="calendar-day-head">
        <div class="calendar-day-title">${formatDateDe(day.date, { weekday: true }) || ""}</div>
        <div class="calendar-day-head-right">
          <div class="calendar-day-badges">${badges.join("")}</div>
          <div class="calendar-day-nav">
            <button type="button" class="calendar-nav-btn" data-day-shift="-1" aria-label="Vorheriger Tag">‹</button>
            <button type="button" class="calendar-nav-btn" data-day-shift="1" aria-label="Nächster Tag">›</button>
          </div>
        </div>
      </div>
      ${insight ? `<div class="calendar-day-insight">${insight}</div>` : ""}
      <div class="calendar-day-section">
        <div class="calendar-day-section-title">Geplant</div>
        <div class="calendar-detail-row"><strong>${plannedTitle || (planned.kind === "off" ? "Off" : "—")}</strong><span>${planned.kind === "train" ? "GEPLANT" : (planned.kind === "off" ? "OFF" : "—")}</span></div>
      </div>
      <div class="calendar-day-section">
        <div class="calendar-day-section-title">Gemacht</div>
        ${workoutRows}
      </div>
      <div class="calendar-day-section">
        <div class="calendar-day-section-title">Cardio</div>
        ${runRows}
      </div>
      <div class="calendar-day-note">${actual.toplift ? `Toplift: ${actual.toplift}` : ""}</div>
    `;
  }

  async function selectCalendarDate(dateIso, loadDetails = true) {
    if (!dateIso) return;
    state.calendarSelectedDate = dateIso;
    const cell = Array.isArray(state.calendarPayload?.days)
      ? state.calendarPayload.days.find((d) => d?.date === dateIso)
      : findCalendarCell(state.calendarPayload, dateIso);
    setMiniFooter(cell);
    if (state.calendarPayload) {
      renderCalendarGrid(els.calendarMiniGrid, state.calendarPayload, {
        selectedDate: state.calendarSelectedDate,
        onPick: (picked) => selectCalendarDate(picked?.date, true),
        showBlocks: true,
      });
      if (els.calendarModal?.getAttribute("aria-hidden") === "false") await renderCalendarExplorer(false);
    }
    const fromRange = Array.isArray(state.calendarRangePayload?.days)
      ? state.calendarRangePayload.days.find((d) => d?.date === dateIso)
      : null;
    if (fromRange) {
      renderDayPanel({ day: fromRange, meta: { date: dateIso } });
      renderExplorerContext(fromRange);
    }
    if (loadDetails && els.calendarDayPanel) {
      const detail = await fetchCalendarDay(dateIso);
      renderDayPanel(detail);
    }
  }

  async function renderCalendarMonth(monthIso, force = false) {
    if (!els.calendarCard || !els.calendarMiniGrid) return;
    const payload = await fetchCalendarMonth(monthIso, force);
    if (!payload || !Array.isArray(payload.days)) {
      els.calendarMiniGrid.innerHTML = `<div class=\"calendar-mini-error\">Kalender konnte nicht geladen werden.</div>`;
      return;
    }
    const gridPayload = monthLiveToGridPayload(payload);
    state.calendarPayload = gridPayload;
    state.calendarRangePayload = { days: payload.days || [], spans: gridPayload.spans || [] };
    state.calendarMonth = payload?.meta?.month || monthIso;
    state.calendarLastFetchTs = Date.now();
    if (els.calendarMonthLabel) {
      els.calendarMonthLabel.textContent = monthLabel(state.calendarMonth);
    }
    renderMiniLegend({ spans: gridPayload.spans || [] });
    renderCalendarWeekdays(els.calendarMiniWeekdays);
    renderCalendarGrid(els.calendarMiniGrid, gridPayload, {
      selectedDate: state.calendarSelectedDate,
      onPick: (cell) => selectCalendarDate(cell?.date, true),
      showBlocks: true,
    });
    if (!state.calendarSelectedDate || !findCalendarCell(gridPayload, state.calendarSelectedDate)) {
      const todayCell = findCalendarCell(gridPayload, gridPayload?.meta?.today);
      state.calendarSelectedDate = todayCell?.date || (gridPayload?.weeks?.[0]?.[0]?.date ?? null);
    }
    await selectCalendarDate(state.calendarSelectedDate, false);

    if (els.calendarModal?.getAttribute("aria-hidden") === "false") {
      updateCalendarModalSelectors(state.calendarMonth);
      await renderCalendarExplorer(force);
      await selectCalendarDate(state.calendarSelectedDate, true);
    }
  }

  function closeCalendarModal() {
    if (!els.calendarModal) return;
    els.calendarModal.classList.remove("is-open");
    els.calendarModal.setAttribute("aria-hidden", "true");
  }

  function closeKienzlModal() {
    const node = document.getElementById("kienzl-modal");
    if (!node) return;
    node.setAttribute("aria-hidden", "true");
    if ("hidden" in node) node.hidden = true;
  }

  async function openCalendarModal() {
    if (!els.calendarModal) return;
    els.calendarModal.classList.add("is-open");
    els.calendarModal.setAttribute("aria-hidden", "false");
    state.calendarView = "month";
    if (els.calendarDayPanel) {
      els.calendarDayPanel.classList.remove("is-open");
      els.calendarDayPanel.setAttribute("aria-hidden", "true");
    }
    if (els.calendarProblemsToggle) {
      els.calendarProblemsToggle.classList.toggle("is-active", state.calendarShowProblemsOnly);
    }
    if (els.calendarModeToggle) {
      const mode = state.calendarMode || "both";
      els.calendarModeToggle.querySelectorAll(".calendar-mode-btn").forEach((b) => {
        b.classList.toggle("is-active", String(b.getAttribute("data-mode")) === mode);
      });
    }
    if (els.calendarBlocksToggle) {
      els.calendarBlocksToggle.classList.toggle("is-active", state.calendarShowBlocks);
    }
    if (els.calendarViewToggle) {
      els.calendarViewToggle.querySelectorAll(".calendar-mode-btn").forEach((b) => {
        b.classList.toggle("is-active", String(b.getAttribute("data-view")) === state.calendarView);
      });
    }
    if (els.calendarFocusToggle) {
      els.calendarFocusToggle.querySelectorAll(".calendar-mode-btn").forEach((b) => {
        b.classList.toggle("is-active", String(b.getAttribute("data-focus")) === state.calendarFocus);
      });
    }
    updateCalendarModalSelectors(state.calendarMonth || currentMonthIsoBerlin());
    if (!state.calendarPayload) await renderCalendarMonth(state.calendarMonth || currentMonthIsoBerlin(), false);
    await renderCalendarExplorer(false);
    await selectCalendarDate(state.calendarSelectedDate || state.calendarPayload?.meta?.today, false);
  }

  async function loadCalendarMonthIfNeeded(force = false) {
    const monthIso = state.calendarMonth || currentMonthIsoBerlin();
    if (!monthIso) return;
    const stale = (Date.now() - state.calendarLastFetchTs) > 60_000;
    await renderCalendarMonth(monthIso, force || stale);
  }

  loadPrefs();
  bindEvents();
  renderCoreSteuertBadge(state.coreModeEnabled);

  function enhanceDashboardPresentation() {
    const reduceMotion = window.matchMedia?.("(prefers-reduced-motion: reduce)")?.matches;
    primeDashboardCardAnimation();
    if (reduceMotion) {
      app.classList.add("is-dashboard-ready");
      getDashboardCards().forEach((card) => card.classList.add("is-card-ready"));
      return;
    }
    requestAnimationFrame(() => {
      app.classList.add("is-dashboard-ready");
    });
  }

  enhanceDashboardPresentation();

  window.addEventListener("pageshow", (event) => {
    renderCoreSteuertBadge(state.coreModeEnabled);
    if (event?.persisted) {
      enhanceDashboardPresentation();
      try { pcInit(); } catch (_) {}
      loadDashboard();
    }
  });

  // Live updates (triggered by /api/last_update)
  let liveBusy = false;
  window.LIVE_CONFIG = {
    interval: 90000,
    onUpdate: async () => {
      if (document.hidden) return;
      if (liveBusy) return;
      const now = Date.now();
      const sinceLive = now - Number(state.lastLiveUpdateTs || 0);
      const sinceFull = now - Number(state.lastFullDashboardTs || 0);
      if (sinceLive < 45000) return;
      if (sinceFull < 60000) return;
      liveBusy = true;
      try {
        await refreshDashboardLiveLite();
        state.lastLiveUpdateTs = Date.now();
      } finally {
        liveBusy = false;
      }
    }
  };

  try { pcInit(); } catch (_) {}
  loadDashboard().catch(() => {});
})();
