(function () {
  const app = document.getElementById("hrv-app");
  if (!app) return;

  const LOCALE = "de-DE";
  const MS_DAY = 24 * 60 * 60 * 1000;
  const DASH = "–";
  const FLAG_ORDER = { none: 0, sick: 1, alcohol: 2 };

  const state = {
    payload: null,
    rows: [],
    allRows: [],
    usableRows: [],
    rangeDays: 30,
    baselineDays: 28,
    sortKey: "date",
    sortDir: "desc",
    syncInFlight: false,
    charts: {
      rmssd: null,
      pulse: null,
      scatter: null
    }
  };

  const els = {
    range: document.getElementById("hrv-range"),
    rangeButtons: Array.from(document.querySelectorAll(".hrv-range-preset")),
    controlError: document.getElementById("hrv-controls-error"),
    syncButton: document.getElementById("hrv-sync-button"),
    latestMeasurement: document.getElementById("hrv-latest-measurement"),
    lastSync: document.getElementById("hrv-last-sync"),
    readinessScore: document.getElementById("hrv-readiness-score"),
    readinessStatus: document.getElementById("hrv-readiness-status"),
    readinessGauge: document.getElementById("hrv-readiness-gauge"),
    readinessFlag: document.getElementById("hrv-readiness-flag"),
    readinessNote: document.getElementById("hrv-readiness-note"),
    actionTitle: document.getElementById("hrv-action-title"),
    actionCopy: document.getElementById("hrv-action-copy"),
    polarScore: document.getElementById("hrv-polar-score"),
    polarStatus: document.getElementById("hrv-polar-status"),
    polarConfidence: document.getElementById("hrv-polar-confidence"),
    sleepProfile: document.getElementById("hrv-sleep-profile"),
    statusLines: document.getElementById("hrv-status-lines"),
    signalList: document.getElementById("hrv-signal-list"),
    contextMeta: document.getElementById("hrv-context-meta"),
    todaySummary: document.getElementById("hrv-today-summary"),
    keyGrid: document.getElementById("hrv-key-grid"),
    tableMeta: document.getElementById("hrv-table-meta"),
    tableBody: document.getElementById("hrv-table-body"),
    tableError: document.getElementById("hrv-table-error"),
    chartRmssd: document.getElementById("chart-rmssd"),
    chartPulse: document.getElementById("chart-pulse"),
    chartScatter: document.getElementById("chart-scatter"),
    chartRmssdStatus: document.getElementById("chart-rmssd-status"),
    chartPulseStatus: document.getElementById("chart-pulse-status"),
    chartScatterStatus: document.getElementById("chart-scatter-status"),
    miniRmssdValue: document.getElementById("mini-rmssd-value"),
    miniRmssdLabel: document.getElementById("mini-rmssd-label"),
    miniRmssdTrend: document.getElementById("mini-rmssd-trend"),
    miniRmssdSpark: document.getElementById("mini-rmssd-spark"),
    miniPulseValue: document.getElementById("mini-pulse-value"),
    miniPulseLabel: document.getElementById("mini-pulse-label"),
    miniPulseTrend: document.getElementById("mini-pulse-trend"),
    miniPulseSpark: document.getElementById("mini-pulse-spark"),
    miniBalanceValue: document.getElementById("mini-balance-value"),
    miniBalanceLabel: document.getElementById("mini-balance-label"),
    miniBalanceTrend: document.getElementById("mini-balance-trend"),
    miniBalanceSpark: document.getElementById("mini-balance-spark"),
    miniSleepValue: document.getElementById("mini-sleep-value"),
    miniSleepLabel: document.getElementById("mini-sleep-label"),
    miniSleepTrend: document.getElementById("mini-sleep-trend"),
    miniSleepSpark: document.getElementById("mini-sleep-spark")
  };

  function setText(el, value) {
    if (el) el.textContent = value;
  }

  function setHtml(el, value) {
    if (el) el.innerHTML = value;
  }

  function toNumber(value) {
    if (value === null || value === undefined || value === "") return null;
    const num = Number(value);
    return Number.isFinite(num) ? num : null;
  }

  function parseDateOnly(value) {
    if (!value) return null;
    const text = String(value).trim();
    if (!text) return null;
    const date = new Date(`${text}T00:00:00Z`);
    return Number.isNaN(date.getTime()) ? null : date;
  }

  function parseTimestamp(value) {
    if (!value) return null;
    const text = String(value).trim();
    if (!text) return null;
    const date = new Date(text);
    return Number.isNaN(date.getTime()) ? null : date;
  }

  function normalizeFlag(row) {
    const explicit = String(row?.flag ?? "").trim().toLowerCase();
    if (explicit === "sick" || explicit === "alcohol") return explicit;
    if (explicit === "none" || explicit === "null" || explicit === "-") return null;
    if (row?.sick || row?.krank || row?.sickness) return "sick";
    if (row?.alcohol || row?.alkohol) return "alcohol";
    return null;
  }

  function nextFlagCycle(flag) {
    if (flag === "sick") return "alcohol";
    if (flag === "alcohol") return null;
    return "sick";
  }

  function flagMeta(flag) {
    if (flag === "sick") return { label: "Krank", title: "Krank", symbol: "K", tone: "sick" };
    if (flag === "alcohol") return { label: "Alkohol", title: "Alkohol", symbol: "A", tone: "alcohol" };
    return { label: "Kein Flag", title: "Kein Flag", symbol: DASH, tone: "none" };
  }

  function normalizeRow(row) {
    const dateObj = parseDateOnly(row.date);
    const timeObj = parseTimestamp(row.time);
    return {
      ...row,
      _date: dateObj,
      _time: timeObj,
      rmssd: toNumber(row.rmssd),
      nightPulse: toNumber(row.nightPulse),
      rri: toNumber(row.rri),
      breathingRate: toNumber(row.breathingRate),
      sdnn: toNumber(row.sdnn),
      avnn: toNumber(row.avnn),
      ansStatus: toNumber(row.ansStatus),
      recoveryIndicator: toNumber(row.recoveryIndicator),
      recoveryIndicatorSublevel: toNumber(row.recoveryIndicatorSublevel),
      ansRate: toNumber(row.ansRate),
      sleepScore: toNumber(row.sleepScore),
      sleepStart: row.sleepStart || null,
      sleepEnd: row.sleepEnd || null,
      sleepWindowLabel: row.sleepWindowLabel || null,
      sleepMinutes: toNumber(row.sleepMinutes),
      actualSleepMinutes: toNumber(row.actualSleepMinutes),
      deepSleepMinutes: toNumber(row.deepSleepMinutes),
      remSleepMinutes: toNumber(row.remSleepMinutes),
      lightSleepMinutes: toNumber(row.lightSleepMinutes),
      awakeMinutes: toNumber(row.awakeMinutes),
      interruptions: toNumber(row.interruptions),
      interruptionMinutes: toNumber(row.interruptionMinutes),
      avgDayHr: toNumber(row.avgDayHr),
      minHr: toNumber(row.minHr),
      maxHr: toNumber(row.maxHr),
      activitySteps: toNumber(row.activitySteps),
      activityMinutes: toNumber(row.activityMinutes),
      activityCalories: toNumber(row.activityCalories),
      recoveryScore: toNumber(row.recovery_score),
      recoveryStatus: row.recovery_status || null,
      recoveryConfidence: row.recovery_confidence || null,
      recoveryReasons: Array.isArray(row.recovery_reasons) ? row.recovery_reasons.filter(Boolean) : [],
      recoveryComponents: row.recovery_components && typeof row.recovery_components === "object" ? row.recovery_components : {},
      recoveryBaselines: row.recovery_baselines && typeof row.recovery_baselines === "object" ? row.recovery_baselines : {},
      flag: normalizeFlag(row)
    };
  }

  function fmtNumber(value, decimals = 0) {
    const num = toNumber(value);
    if (num === null) return DASH;
    return num.toLocaleString(LOCALE, {
      minimumFractionDigits: decimals,
      maximumFractionDigits: decimals
    });
  }

  function fmtSignedPct(value) {
    const num = toNumber(value);
    if (num === null) return DASH;
    const sign = num > 0 ? "+" : "";
    return `${sign}${fmtNumber(num, 1)}%`;
  }

  function fmtMeasurement(value, unit, decimals = 0) {
    const num = toNumber(value);
    return num === null ? DASH : `${fmtNumber(num, decimals)} ${unit}`;
  }

  function fmtPlainInt(value) {
    const num = toNumber(value);
    if (num === null) return DASH;
    return String(Math.round(num));
  }

  function fmtMinutes(value) {
    const num = toNumber(value);
    if (num === null) return DASH;
    const hours = Math.floor(num / 60);
    const minutes = Math.round(num % 60);
    if (hours <= 0) return `${minutes} min`;
    return `${hours} h ${String(minutes).padStart(2, "0")} min`;
  }

  function fmtDateTime(value) {
    const date = parseTimestamp(value);
    if (!date) return DASH;
    return new Intl.DateTimeFormat(LOCALE, {
      dateStyle: "medium",
      timeStyle: "short"
    }).format(date);
  }

  function fmtDate(date) {
    if (!date) return DASH;
    return new Intl.DateTimeFormat(LOCALE, {
      day: "2-digit",
      month: "2-digit",
      year: "numeric"
    }).format(date);
  }

  function fmtTime(value) {
    const date = parseTimestamp(value);
    if (!date) return DASH;
    return new Intl.DateTimeFormat(LOCALE, { timeStyle: "short" }).format(date);
  }

  function fmtCompactHours(value) {
    const num = toNumber(value);
    if (num === null) return DASH;
    const hours = Math.floor(num / 60);
    const minutes = Math.round(num % 60);
    if (hours <= 0) return `${minutes} min`;
    return `${hours} h ${String(minutes).padStart(2, "0")} min`;
  }

  function fmtBreathing(value) {
    const num = toNumber(value);
    if (num === null) return DASH;
    return `${fmtNumber(num, 1)}/min`;
  }

  function interruptionMinutes(row) {
    const direct = toNumber(row?.interruptionMinutes);
    if (direct !== null && direct > 0) return direct;
    const awake = toNumber(row?.awakeMinutes);
    if (awake !== null && awake > 0) return awake;
    const sleep = toNumber(row?.sleepMinutes);
    const actual = toNumber(row?.actualSleepMinutes);
    if (sleep !== null && actual !== null && sleep > actual) {
      return sleep - actual;
    }
    return null;
  }

  function hasValue(value) {
    return value !== null && value !== undefined && value !== "";
  }

  function latestRow() {
    return state.rows[0] || null;
  }

  function filterRowsByRange(rows) {
    const source = Array.isArray(rows) ? rows : [];
    if (state.rangeDays === "all") return source.slice();
    const latest = source[0] || null;
    if (!latest || !latest._date) return source.slice();
    const start = new Date(latest._date.getTime() - (Number(state.rangeDays) - 1) * MS_DAY);
    return source.filter((row) => row._date && row._date >= start && row._date <= latest._date);
  }

  function isDisplayRow(row) {
    if (!row || !row._date) return false;
    if (row.flag === "sick" || row.flag === "alcohol") return true;
    return Boolean(
      row.rmssd !== null ||
      row.rri !== null ||
      row.nightPulse !== null ||
      row.ansStatus !== null ||
      row.sleepScore !== null
    );
  }

  function applyRangeFilter() {
    state.rows = filterRowsByRange(state.allRows.filter(isDisplayRow));
  }

  function rangeRows() {
    return state.rows
      .slice()
      .sort((a, b) => (a._date?.getTime() || 0) - (b._date?.getTime() || 0));
  }

  function mean(values) {
    const clean = values.map(toNumber).filter((value) => value !== null);
    if (!clean.length) return null;
    return clean.reduce((sum, value) => sum + value, 0) / clean.length;
  }

  function movingAverage(values, windowSize) {
    return values.map((_, index) => {
      const slice = values.slice(Math.max(0, index - windowSize + 1), index + 1).map(toNumber).filter((value) => value !== null);
      return slice.length ? mean(slice) : null;
    });
  }

  function pctChange(recent, previous) {
    const a = toNumber(recent);
    const b = toNumber(previous);
    if (a === null || b === null || Math.abs(b) < 0.0001) return null;
    return ((a - b) / b) * 100;
  }

  function toneForTrend(value, inverse = false) {
    const num = toNumber(value);
    if (num === null || Math.abs(num) < 0.25) return "neutral";
    const positive = num > 0;
    if (inverse) return positive ? "bad" : "good";
    return positive ? "good" : "bad";
  }

  function toneForReadiness(status) {
    if (status === "READY") return "ready";
    if (status === "NORMAL") return "normal";
    if (status === "LIGHT") return "light";
    return "warning";
  }

  function baselineValue(key) {
    return toNumber(state.payload?.baseline?.[key]);
  }

  function compareToBaseline(value, baseline, higherIsBetter = true) {
    const current = toNumber(value);
    const base = toNumber(baseline);
    if (current === null || base === null) return { text: "Keine Baseline", tone: "neutral" };
    const pct = pctChange(current, base);
    if (pct === null) return { text: "Keine Baseline", tone: "neutral" };
    const diff = current - base;
    if (Math.abs(pct) < 3) return { text: "Nahe Baseline", tone: "neutral" };
    const better = higherIsBetter ? diff > 0 : diff < 0;
    return {
      text: better ? "Über Baseline" : "Unter Baseline",
      tone: better ? "good" : "bad"
    };
  }

  function lineSummary(row) {
    if (!row) return "Keine Datenlage verfügbar.";
    if (row.flag === "sick") return "Krank markiert – Recovery vorsichtig bewerten.";
    if (row.flag === "alcohol") return "Alkohol markiert – HRV/Schlafwerte können verfälscht sein.";
    const contradictions = state.payload?.readiness?.contradiction;
    if (contradictions) return "Schwache Erholung trotz stabiler HRV.";
    if (row.ansStatus !== null && row.ansStatus < -4) return "Erholung klar gedämpft.";
    if (row.sleepScore !== null && row.sleepScore < 60) return "Schlafqualität bremst die Erholung.";
    if (row.rmssd !== null && row.nightPulse !== null) return "HRV stabil, Nachtpuls eher erhöht.";
    return "Recovery-Lage heute gemischt.";
  }

  function sleepStatus(row) {
    const sleep = row?.sleepScore;
    const windowLabel = row?.sleepWindowLabel || null;
    const duration = row?.actualSleepMinutes ?? row?.sleepMinutes;
    if (sleep === null) return { badge: "Keine Daten", tone: "neutral", detail: "Sleep Score fehlt." };
    const parts = [`Score ${fmtNumber(sleep, 0)}`];
    if (windowLabel) parts.push(windowLabel.replace(/\s+/g, ""));
    parts.push(duration !== null ? fmtMinutes(duration) : "Dauer fehlt");
    const detail = parts.join(" · ");
    if (sleep < 60) return { badge: "Schlecht", tone: "bad", detail };
    if (sleep <= 70) return { badge: "Mittel", tone: "mid", detail };
    return { badge: "Gut", tone: "good", detail };
  }

  function nightlyStatus(row) {
    const ans = row?.ansStatus;
    if (ans === null) return { badge: "Keine Daten", tone: "neutral", detail: "Nightly Recharge fehlt." };
    if (ans < -4) return { badge: "Schwach", tone: "bad", detail: `ANS ${fmtNumber(ans, 2)}.` };
    if (ans <= -2) return { badge: "Mäßig", tone: "mid", detail: `ANS ${fmtNumber(ans, 2)}.` };
    return { badge: "Gut", tone: "good", detail: `ANS ${fmtNumber(ans, 2)}.` };
  }

  function hrvStatus(row) {
    const cmp = compareToBaseline(row?.rmssd, baselineValue("rmssd"), true);
    const value = row?.rmssd;
    const detail = value === null ? "RMSSD fehlt." : `${fmtNumber(value, 0)} ms.`;
    return {
      badge: value === null ? "Keine Daten" : (cmp.tone === "good" ? "Gut" : cmp.tone === "bad" ? "Niedrig" : "Stabil"),
      tone: cmp.tone === "neutral" ? "mid" : (cmp.tone === "good" ? "good" : "bad"),
      detail
    };
  }

  function pulseStatus(row) {
    const cmp = compareToBaseline(row?.nightPulse, baselineValue("nightPulse"), false);
    const value = row?.nightPulse;
    const detail = value === null ? "Nachtpuls fehlt." : `${fmtNumber(value, 0)} bpm.`;
    return {
      badge: value === null ? "Keine Daten" : (cmp.tone === "good" ? "Ruhig" : cmp.tone === "bad" ? "Erhöht" : "Normal"),
      tone: cmp.tone === "neutral" ? "mid" : (cmp.tone === "good" ? "good" : "bad"),
      detail
    };
  }

  function statusBadgeForTone(tone) {
    if (tone === "good") return "Positiv";
    if (tone === "bad") return "Achtung";
    if (tone === "mid") return "Vorsicht";
    return "Hinweis";
  }

  function pushContextItem(items, tone, text, key) {
    if (!text) return;
    if (key && items.some((item) => item.key === key)) return;
    items.push({ tone, text, key: key || text });
  }

  function renderHeader() {
    setText(els.latestMeasurement, fmtDateTime(state.payload?.latest_measurement_at));
    setText(els.lastSync, fmtDateTime(state.payload?.last_sync_at));
  }

  function renderReadiness() {
    const readiness = state.payload?.readiness || {};
    const score = toNumber(readiness.score);
    const status = String(readiness.status || "WARNUNG");
    const latest = latestRow();
    setText(els.readinessScore, score === null ? DASH : fmtNumber(score, 0));
    if (els.readinessStatus) {
      els.readinessStatus.dataset.tone = toneForReadiness(status);
      els.readinessStatus.textContent = status;
    }
    if (els.readinessGauge) {
      els.readinessGauge.style.width = score === null ? "0%" : `${Math.max(0, Math.min(100, score))}%`;
    }
    const count = state.payload?.baseline?.count ?? 0;
    const latestDateText = latest?._date ? fmtDate(latest._date) : DASH;
    const actionTitles = {
      READY: "Volle Belastung möglich",
      NORMAL: "Normal trainieren",
      LIGHT: "Belastung reduzieren",
      WARNING: "Recovery priorisieren"
    };
    setText(els.actionTitle, actionTitles[status] || "Recovery priorisieren");
    setText(els.actionCopy, recommendationForStatus(status));
    setText(els.polarScore, latest?.recoveryScore === null ? DASH : fmtNumber(latest?.recoveryScore, 0));
    setText(els.polarStatus, latest?.recoveryStatus || DASH);
    setText(els.polarConfidence, latest?.recoveryConfidence || latest?.dataQuality || DASH);
    if (els.readinessFlag) {
      const flagText = latest?.flag === "sick"
        ? "Krank markiert – Recovery vorsichtig bewerten."
        : latest?.flag === "alcohol"
          ? "Alkohol markiert – Recovery konservativ einordnen."
          : "";
      els.readinessFlag.hidden = !flagText;
      els.readinessFlag.textContent = flagText;
    }
    setText(
      els.readinessNote,
      `Training Readiness · 28-Tage-Baseline · ${count} Vergleichstage · Stand ${latestDateText}`
    );
    setHtml(els.statusLines, "");
  }

  function recommendationForStatus(status) {
    if (status === "READY") return "Intensität ist heute okay. Nur bei subjektiver Müdigkeit zurücknehmen.";
    if (status === "NORMAL") return "Kontrolliert trainieren; maximale Leistung heute nicht erzwingen.";
    if (status === "LIGHT") return "Technik, moderater Pump oder lockeres Z2 statt maximaler Belastung.";
    return "Kein Maximaltraining. Locker bewegen oder pausieren und Erholung priorisieren.";
  }

  function renderSignals() {
    const readiness = state.payload?.readiness || {};
    const row = latestRow();
    const reasons = Array.isArray(readiness.reasons) ? readiness.reasons.slice(0, 4) : [];
    if (row?.flag === "sick") reasons.unshift("Krank-Flag aktiv: Recovery konservativ bewerten.");
    if (row?.flag === "alcohol") reasons.unshift("Alkohol-Flag aktiv: Schlaf und HRV vorsichtiger lesen.");
    if (!reasons.length) reasons.push("Noch keine belastbare Einordnung verfügbar.");
    const classify = (text) => {
      const value = String(text || "").toLowerCase();
      if (/(grün|über baseline|stabil|gut)/.test(value)) return "good";
      if (/(schwach|krank|widersprüch|unter baseline)/.test(value)) return "bad";
      if (/(mittel|alkohol|reduzier|vorsicht)/.test(value)) return "warn";
      return "neutral";
    };
    setHtml(els.signalList, reasons.map((text) => `<div class="hrv-reason-line is-${classify(text)}">${text}</div>`).join(""));
  }

  function renderKeys() {
    const row = latestRow();
    if (!row) {
      setHtml(els.keyGrid, `<div class="hrv-driver-row"><div class="hrv-driver-name"><span>Status</span><strong>${DASH}</strong></div></div>`);
      return;
    }

    const baseline = state.payload?.baseline || {};
    const rows = [];

    const addDriver = ({ label, value, unit, base, inverse = false, rawDelta = null, statusText = null }) => {
      const current = toNumber(value);
      const baselineValue = toNumber(base);
      let delta = rawDelta;
      if (delta === null && current !== null && baselineValue !== null && baselineValue !== 0) {
        delta = ((current / baselineValue) - 1) * 100;
        if (inverse) delta *= -1;
      }
      const position = delta === null ? 50 : Math.max(4, Math.min(96, 50 + (delta * 2.3)));
      let tone = "neutral";
      if (delta !== null && delta >= 3) tone = "good";
      if (delta !== null && delta <= -6) tone = "warn";
      if (delta !== null && delta <= -18) tone = "bad";
      const deltaText = statusText || (delta === null ? DASH : fmtSignedPct(delta));
      const baseText = baselineValue === null ? "keine Basis" : `Basis ${fmtNumber(baselineValue, unit === "ANS" ? 2 : 0)}`;
      rows.push(`
        <div class="hrv-driver-row is-${tone}">
          <div class="hrv-driver-name">
            <span>${label}</span>
            <strong>${current === null ? DASH : `${fmtNumber(current, unit === "ANS" ? 2 : 0)}${unit === "ANS" ? "" : ` ${unit}`}`}</strong>
          </div>
          <div class="hrv-driver-track" aria-label="${label}: ${deltaText} zur Basis">
            <span class="hrv-driver-marker" style="--driver-position:${position}%"></span>
          </div>
          <div class="hrv-driver-delta"><strong>${deltaText}</strong><span>${baseText}</span></div>
        </div>
      `);
    };

    addDriver({ label: "HRV / RMSSD", value: row.rmssd, unit: "ms", base: baseline.rmssd });
    addDriver({ label: "Nachtpuls", value: row.nightPulse, unit: "bpm", base: baseline.nightPulse, inverse: true });
    addDriver({ label: "Schlafscore", value: row.sleepScore, unit: "pts", base: baseline.sleepScore });
    const ans = toNumber(row.ansStatus);
    const ansDelta = ans === null ? null : Math.max(-20, Math.min(20, ans * 4));
    const ansLabel = ans === null ? DASH : (ans < -4 ? "schwach" : ans < -2 ? "gedämpft" : ans >= 0.5 ? "stabil" : "normal");
    addDriver({ label: "ANS", value: ans, unit: "ANS", base: 0, rawDelta: ansDelta, statusText: ansLabel });
    setHtml(els.keyGrid, rows.join(""));
  }

  function renderSleepProfile() {
    if (!els.sleepProfile) return;
    const row = latestRow();
    if (!row) {
      setHtml(els.sleepProfile, `<div class="hrv-sleep-fact"><span>Keine Schlafdaten</span><strong>${DASH}</strong></div>`);
      return;
    }
    const actual = toNumber(row.actualSleepMinutes ?? row.sleepMinutes);
    const deep = toNumber(row.deepSleepMinutes) || 0;
    const rem = toNumber(row.remSleepMinutes) || 0;
    const light = toNumber(row.lightSleepMinutes) || 0;
    const awake = toNumber(row.awakeMinutes) || 0;
    const total = Math.max(1, deep + rem + light + awake);
    const pct = (value) => `${Math.max(0, (value / total) * 100).toFixed(2)}%`;
    const scoreMeta = sleepStatus(row);
    setHtml(els.sleepProfile, `
      <div class="hrv-sleep-topline">
        <div class="hrv-sleep-duration"><span>Effektiver Schlaf</span><strong>${fmtCompactHours(actual)}</strong></div>
        <div class="hrv-sleep-score"><span>Sleep Score</span><strong>${row.sleepScore === null ? DASH : fmtNumber(row.sleepScore, 0)} · ${scoreMeta.badge}</strong></div>
      </div>
      <div>
        <div class="hrv-sleep-stage-bar" aria-label="Schlafphasen">
          <span class="hrv-sleep-stage hrv-stage-deep" style="--stage-size:${pct(deep)}"></span>
          <span class="hrv-sleep-stage hrv-stage-rem" style="--stage-size:${pct(rem)}"></span>
          <span class="hrv-sleep-stage hrv-stage-light" style="--stage-size:${pct(light)}"></span>
          <span class="hrv-sleep-stage hrv-stage-awake" style="--stage-size:${pct(awake)}"></span>
        </div>
        <div class="hrv-sleep-legend">
          <div><span><i style="--legend-color:#4678a8"></i>Tief</span><strong>${fmtMinutes(deep)}</strong></div>
          <div><span><i style="--legend-color:var(--hrv-blue)"></i>REM</span><strong>${fmtMinutes(rem)}</strong></div>
          <div><span><i style="--legend-color:color-mix(in srgb, var(--hrv-blue) 38%, var(--hrv-surface-soft))"></i>Leicht</span><strong>${fmtMinutes(light)}</strong></div>
          <div><span><i style="--legend-color:var(--hrv-warn)"></i>Wach</span><strong>${fmtMinutes(awake)}</strong></div>
        </div>
      </div>
      <div class="hrv-sleep-facts">
        <div class="hrv-sleep-fact"><span>Schlaffenster</span><strong>${row.sleepWindowLabel || DASH}</strong></div>
        <div class="hrv-sleep-fact"><span>Unterbrechungen</span><strong>${row.interruptions === null ? DASH : fmtNumber(row.interruptions, 0)}</strong></div>
        <div class="hrv-sleep-fact"><span>Nightly Recharge</span><strong>${row.ansStatus === null ? DASH : fmtNumber(row.ansStatus, 2)}</strong></div>
        <div class="hrv-sleep-fact"><span>Ruhepuls-Kandidat</span><strong>${row.nightPulse === null ? DASH : `${fmtNumber(row.nightPulse, 0)} bpm`}</strong></div>
      </div>
    `);
  }

  function seriesForLast(days, key) {
    const rows = state.rows.slice(0, days).reverse();
    return rows.map((row) => row[key]).filter((value) => toNumber(value) !== null);
  }

  function renderMiniCard(valueEl, trendEl, sparkEl, current, previous, values, unit, inverse = false, decimals = 0) {
    setText(valueEl, current === null ? DASH : `${fmtNumber(current, decimals)} ${unit}`.trim());
    const delta = pctChange(current, previous);
    const tone = toneForTrend(delta, inverse);
    if (trendEl) {
      trendEl.classList.remove("is-good", "is-bad", "is-neutral");
      trendEl.classList.add(`is-${tone}`);
      trendEl.textContent = delta === null ? DASH : `${delta > 0 ? "↑" : "↓"} ${fmtSignedPct(Math.abs(delta)).replace("+", "")}`;
    }
    drawSparkline(sparkEl, values, inverse ? "#f59e0b" : "#60a5fa");
  }

  function renderMiniCards() {
    const latest7 = state.rows.slice(0, 7);
    const prev7 = state.rows.slice(7, 14);
    const recoveryNow = mean(latest7.map((row) => row.recoveryScore));
    const recoveryPrev = mean(prev7.map((row) => row.recoveryScore));
    const rmssdNow = mean(latest7.map((row) => row.rmssd));
    const rmssdPrev = mean(prev7.map((row) => row.rmssd));
    const pulseNow = mean(latest7.map((row) => row.nightPulse));
    const pulsePrev = mean(prev7.map((row) => row.nightPulse));
    const sleepNow = mean(latest7.map((row) => row.sleepScore));
    const sleepPrev = mean(prev7.map((row) => row.sleepScore));

    setText(els.miniRmssdLabel, `Ø Recovery · ${Math.min(7, latest7.filter((row) => row.recoveryScore !== null).length)} Tage`);
    renderMiniCard(els.miniRmssdValue, els.miniRmssdTrend, null, recoveryNow, recoveryPrev, [], "pts", false, 0);

    setText(els.miniPulseLabel, `Ø RMSSD · ${Math.min(7, latest7.filter((row) => row.rmssd !== null).length)} Tage`);
    renderMiniCard(els.miniPulseValue, els.miniPulseTrend, null, rmssdNow, rmssdPrev, [], "ms", false, 0);

    setText(els.miniBalanceLabel, `Ø Nachtpuls · ${Math.min(7, latest7.filter((row) => row.nightPulse !== null).length)} Tage`);
    renderMiniCard(els.miniBalanceValue, els.miniBalanceTrend, null, pulseNow, pulsePrev, [], "bpm", true, 0);

    setText(els.miniSleepLabel, `Ø Schlafscore · ${Math.min(7, latest7.filter((row) => row.sleepScore !== null).length)} Nächte`);
    renderMiniCard(els.miniSleepValue, els.miniSleepTrend, null, sleepNow, sleepPrev, [], "pts", false, 0);
  }

  function setupSparkCanvas(canvas) {
    if (!canvas) return null;
    const width = canvas.clientWidth || canvas.width || 260;
    const height = canvas.clientHeight || canvas.height || 60;
    const dpr = window.devicePixelRatio || 1;
    canvas.width = Math.round(width * dpr);
    canvas.height = Math.round(height * dpr);
    const ctx = canvas.getContext("2d");
    if (!ctx) return null;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    return { ctx, width, height };
  }

  function drawSparkline(canvas, values, color) {
    const layout = setupSparkCanvas(canvas);
    if (!layout) return;
    const { ctx, width, height } = layout;
    ctx.clearRect(0, 0, width, height);
    const clean = values.map(toNumber).filter((value) => value !== null);
    if (!clean.length) {
      ctx.strokeStyle = "rgba(148,163,184,0.35)";
      ctx.beginPath();
      ctx.moveTo(0, height / 2);
      ctx.lineTo(width, height / 2);
      ctx.stroke();
      return;
    }
    const min = Math.min(...clean);
    const max = Math.max(...clean);
    const span = Math.max(1, max - min);
    ctx.strokeStyle = color;
    ctx.lineWidth = 2;
    ctx.beginPath();
    values.forEach((raw, index) => {
      const value = toNumber(raw);
      if (value === null) return;
      const x = values.length <= 1 ? width / 2 : (index / (values.length - 1)) * width;
      const y = height - ((value - min) / span) * (height - 8) - 4;
      if (index === 0) ctx.moveTo(x, y);
      else ctx.lineTo(x, y);
    });
    ctx.stroke();
  }

  function chartColors() {
    const styles = getComputedStyle(document.documentElement);
    return {
      text: styles.getPropertyValue("--text-soft").trim() || "#94a3b8",
      grid: "rgba(148,163,184,0.14)",
      rmssd: "#34d399",
      pulse: "#60a5fa",
      baseline: "rgba(148,163,184,0.18)",
      accent: "#f59e0b",
      bad: "#ef7378"
    };
  }

  function destroyCharts() {
    Object.keys(state.charts).forEach((key) => {
      if (state.charts[key]) {
        state.charts[key].destroy();
        state.charts[key] = null;
      }
    });
  }

  function buildLineChart(canvas, labels, datasets, overrides = {}) {
    if (!canvas || typeof Chart === "undefined") return null;
    const colors = chartColors();
    return new Chart(canvas, {
      type: "line",
      data: { labels, datasets },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        plugins: {
          legend: {
            labels: { color: colors.text }
          }
        },
        scales: {
          x: {
            ticks: { color: colors.text, maxRotation: 45, minRotation: 45 },
            grid: { color: colors.grid }
          },
          y: {
            ticks: { color: colors.text },
            grid: { color: colors.grid },
            ...(overrides.y || {})
          }
        },
        ...(overrides.options || {})
      }
    });
  }

  function renderCharts() {
    destroyCharts();
    const rows = rangeRows();
    const labels = rows.map((row) => fmtDate(row._date));
    const colors = chartColors();
    const recoverySeries = rows.map((row) => row.recoveryScore);

    if (rows.filter((row) => row.recoveryScore !== null).length >= 2) {
      state.charts.rmssd = buildLineChart(els.chartRmssd, labels, [
        {
          label: "Recovery",
          data: recoverySeries,
          borderColor: colors.pulse,
          backgroundColor: "rgba(128,205,247,0.10)",
          fill: true,
          spanGaps: true,
          tension: 0.28,
          borderWidth: 2.2,
          pointRadius: 2,
          pointHoverRadius: 5,
          segment: {
            borderColor: (ctx) => {
              const score = toNumber(ctx?.p1?.parsed?.y);
              if (score === null) return colors.pulse;
              if (score >= 80) return colors.rmssd;
              if (score < 55) return colors.bad;
              if (score < 65) return colors.accent;
              return colors.pulse;
            }
          }
        },
        {
          label: "Bereit ab 80",
          data: rows.map(() => 80),
          borderColor: "rgba(44,228,170,0.32)",
          borderDash: [5, 5],
          borderWidth: 1,
          pointRadius: 0,
          fill: false
        },
        {
          label: "Normal ab 65",
          data: rows.map(() => 65),
          borderColor: "rgba(128,205,247,0.25)",
          borderDash: [3, 5],
          borderWidth: 1,
          pointRadius: 0,
          fill: false
        }
      ], { y: { min: 0, max: 100 } });
      setText(els.chartRmssdStatus, "");
    } else {
      setText(els.chartRmssdStatus, "Zu wenig Recovery-Daten im gewählten Zeitraum.");
    }

    const rmssdBase = baselineValue("rmssd");
    const pulseBase = baselineValue("nightPulse");
    const sleepBase = baselineValue("sleepScore");
    const deviation = (value, base, inverse = false) => {
      const current = toNumber(value);
      const reference = toNumber(base);
      if (current === null || reference === null || reference === 0) return null;
      const result = ((current / reference) - 1) * 100;
      return inverse ? result * -1 : result;
    };
    const rmssdDeviation = rows.map((row) => deviation(row.rmssd, rmssdBase));
    const pulseDeviation = rows.map((row) => deviation(row.nightPulse, pulseBase, true));
    const sleepDeviation = rows.map((row) => deviation(row.sleepScore, sleepBase));
    const driverPoints = [...rmssdDeviation, ...pulseDeviation, ...sleepDeviation].filter((value) => value !== null).length;

    if (driverPoints >= 4) {
      state.charts.pulse = buildLineChart(els.chartPulse, labels, [
        {
          label: "HRV",
          data: rmssdDeviation,
          borderColor: colors.rmssd,
          tension: 0.25,
          spanGaps: true,
          pointRadius: 1.5
        },
        {
          label: "Nachtpuls",
          data: pulseDeviation,
          borderColor: colors.pulse,
          tension: 0.25,
          spanGaps: true,
          pointRadius: 1.5
        },
        {
          label: "Schlaf",
          data: sleepDeviation,
          borderColor: colors.accent,
          tension: 0.25,
          spanGaps: true,
          pointRadius: 1.5
        },
        {
          label: "Baseline",
          data: rows.map(() => 0),
          borderColor: "rgba(148,163,184,0.38)",
          borderDash: [4, 4],
          borderWidth: 1,
          pointRadius: 0
        }
      ], {
        y: {
          suggestedMin: -25,
          suggestedMax: 25,
          ticks: {
            color: colors.text,
            callback: (value) => `${value > 0 ? "+" : ""}${value}%`
          }
        }
      });
      setText(els.chartPulseStatus, "");
    } else {
      setText(els.chartPulseStatus, "Zu wenig Baseline-Daten für den Treibervergleich.");
    }
  }

  function sortValue(row, key) {
    if (key === "date") return row._date ? row._date.getTime() : null;
    if (key === "recovery") return row.recoveryScore;
    if (key === "sleep_start") return row.sleepStart ? (parseTimestamp(row.sleepStart)?.getTime() ?? null) : null;
    if (key === "sleep_end") return row.sleepEnd ? (parseTimestamp(row.sleepEnd)?.getTime() ?? null) : null;
    if (key === "sleep_duration") return toNumber(row.actualSleepMinutes ?? row.sleepMinutes);
    if (key === "sleep_score") return row.sleepScore;
    if (key === "rmssd") return row.rmssd;
    if (key === "sleep_pulse") return row.nightPulse;
    if (key === "ans") return row.ansStatus;
    if (key === "rem") return row.remSleepMinutes;
    if (key === "interruptions") return interruptionMinutes(row);
    if (key === "flag") return FLAG_ORDER[row.flag || "none"] ?? 0;
    return null;
  }

  function recoveryBadgeMeta(row) {
    const score = toNumber(row?.recoveryScore);
    const status = String(row?.recoveryStatus || "").toLowerCase();
    const confidence = String(row?.recoveryConfidence || "").toLowerCase();
    const reasons = Array.isArray(row?.recoveryReasons) ? row.recoveryReasons.slice(0, 4) : [];
    const titleLines = [];
    if (score !== null) titleLines.push(`Recovery ${fmtNumber(score, 0)} / 100`);
    if (status) titleLines.push(`Status: ${status}`);
    if (confidence) titleLines.push(`Confidence: ${confidence}`);
    reasons.forEach((reason) => titleLines.push(`- ${reason}`));
    return {
      label: score === null ? DASH : fmtNumber(score, 0),
      status: status || "unavailable",
      confidence: confidence || "niedrig",
      title: titleLines.join("\n"),
      reasons
    };
  }

  function sortedTableRows() {
    const dir = state.sortDir === "asc" ? 1 : -1;
    return state.rows.slice().sort((a, b) => {
      const aVal = sortValue(a, state.sortKey);
      const bVal = sortValue(b, state.sortKey);
      const aMissing = aVal === null || aVal === undefined || Number.isNaN(aVal);
      const bMissing = bVal === null || bVal === undefined || Number.isNaN(bVal);
      if (aMissing && bMissing) return 0;
      if (aMissing) return 1;
      if (bMissing) return -1;
      if (aVal < bVal) return -1 * dir;
      if (aVal > bVal) return 1 * dir;
      return 0;
    });
  }

  function updateSortIndicators() {
    document.querySelectorAll(".hrv-sort-indicator").forEach((el) => {
      el.textContent = "";
    });
    document.querySelectorAll("th[data-sort]").forEach((th) => {
      const key = th.getAttribute("data-sort");
      const indicator = th.querySelector(".hrv-sort-indicator");
      if (indicator && key === state.sortKey) {
        indicator.textContent = state.sortDir === "asc" ? "↑" : "↓";
      }
    });
  }

  function renderTable() {
    if (!state.rows.length) {
      setHtml(els.tableBody, `<tr><td colspan="12">${DASH}</td></tr>`);
      setText(els.tableMeta, "Keine verwertbaren Recovery-Messungen");
      return;
    }

    const hiddenShells = Math.max(0, state.allRows.length - state.rows.length);
    setText(els.tableMeta, `${state.rows.length} sichtbare Tage${hiddenShells ? ` · ${hiddenShells} Shell-Zeilen ausgeblendet` : ""}`);

    setHtml(els.tableBody, sortedTableRows().map((row) => {
      const sleepValue = row.sleepScore !== null ? fmtNumber(row.sleepScore, 0) : DASH;
      const flag = flagMeta(row.flag);
      const recovery = recoveryBadgeMeta(row);
      const sleepStart = row.sleepStart ? fmtTime(row.sleepStart) : DASH;
      const sleepEnd = row.sleepEnd ? fmtTime(row.sleepEnd) : DASH;
      return `
        <tr>
          <td><strong>${fmtDate(row._date)}</strong></td>
          <td>
            <div class="hrv-recovery-cell">
              <span class="hrv-recovery-badge is-${recovery.status} is-confidence-${recovery.confidence}" title="${recovery.title.replace(/"/g, "&quot;")}">${recovery.label}</span>
            </div>
          </td>
          <td>${sleepStart}</td>
          <td>${sleepEnd}</td>
          <td>${fmtCompactHours(row.actualSleepMinutes ?? row.sleepMinutes)}</td>
          <td>${sleepValue}</td>
          <td>${row.rmssd !== null ? `${fmtNumber(row.rmssd, 0)} ms` : DASH}</td>
          <td>${row.nightPulse !== null ? `${fmtNumber(row.nightPulse, 0)} bpm` : DASH}</td>
          <td>${row.ansStatus !== null ? fmtNumber(row.ansStatus, 2) : DASH}</td>
          <td>${row.remSleepMinutes !== null ? `${fmtNumber(row.remSleepMinutes, 0)} min` : DASH}</td>
          <td>${interruptionMinutes(row) !== null ? `${fmtNumber(interruptionMinutes(row), 0)} min` : DASH}</td>
          <td class="hrv-flag-cell"><button type="button" class="hrv-flag-button is-${flag.tone}" data-date="${row.date || ""}" title="${flag.title}" aria-label="Flag setzen: aktuell ${flag.label}">${flag.symbol}</button></td>
        </tr>
      `;
    }).join(""));
  }

  function updateRowFlag(dateIso, flag) {
    state.rows.forEach((row) => {
      if (row.date === dateIso) row.flag = flag;
    });
    state.allRows.forEach((row) => {
      if (row.date === dateIso) row.flag = flag;
    });
    if (state.payload?.rows) {
      state.payload.rows.forEach((row) => {
        if (row.date === dateIso) row.flag = flag;
      });
    }
    if (state.payload?.usable_rows) {
      state.payload.usable_rows.forEach((row) => {
        if (row.date === dateIso) row.flag = flag;
      });
    }
    if (state.payload?.latest?.date === dateIso) {
      state.payload.latest.flag = flag;
    }
  }

  async function setFlag(dateIso, nextFlag) {
    const currentRow = state.rows.find((row) => row.date === dateIso);
    const previous = currentRow ? currentRow.flag : null;
    updateRowFlag(dateIso, nextFlag);
    renderAll();
    try {
      const response = await fetch("/api/hrv/flag", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ date: dateIso, flag: nextFlag })
      });
      const payload = await response.json();
      if (!response.ok || payload?.ok === false) {
        throw new Error(payload?.detail || "Flag konnte nicht gespeichert werden.");
      }
      updateRowFlag(dateIso, normalizeFlag(payload));
      setText(els.tableError, "");
      renderAll();
    } catch (error) {
      updateRowFlag(dateIso, previous);
      renderAll();
      setText(els.tableError, error?.message || "Flag konnte nicht gespeichert werden.");
    }
  }

  function renderAll() {
    renderHeader();
    renderReadiness();
    renderSignals();
    renderKeys();
    renderSleepProfile();
    renderMiniCards();
    renderCharts();
    renderTable();
    setText(els.tableError, "");
  }

  async function loadRecoveryData() {
    try {
      setText(els.controlError, "Lade Recovery-Daten...");
      const rangeParam = state.rangeDays === "all" ? "all" : String(state.rangeDays);
      const response = await fetch(`/api/hrv/recovery?range_days=${encodeURIComponent(rangeParam)}&baseline_days=${state.baselineDays}`);
      const payload = await response.json();
      if (!payload || payload.ok === false) {
        throw new Error(payload?.detail || "Recovery-Daten konnten nicht geladen werden.");
      }
      state.payload = payload;
      state.allRows = (Array.isArray(payload.rows) ? payload.rows : []).map(normalizeRow);
      state.usableRows = (Array.isArray(payload.usable_rows) ? payload.usable_rows : []).map(normalizeRow);
      applyRangeFilter();
      updateSortIndicators();
      setText(els.controlError, "");
      renderAll();
    } catch (error) {
      destroyCharts();
      state.payload = null;
      state.rows = [];
      state.allRows = [];
      state.usableRows = [];
      setText(els.controlError, "Recovery-Daten konnten nicht geladen werden.");
      setText(els.tableError, "Bitte Polar- und Legacy-Daten prüfen.");
      renderAll();
    }
  }

  function setSyncButtonState(isLoading) {
    state.syncInFlight = Boolean(isLoading);
    if (!els.syncButton) return;
    els.syncButton.disabled = state.syncInFlight;
    els.syncButton.textContent = state.syncInFlight ? "Suche…" : "Neue Daten";
  }

  async function triggerPolarSync() {
    if (state.syncInFlight) return;
    setSyncButtonState(true);
    setText(els.controlError, "Suche nach neuen Polar-Daten...");
    try {
      const response = await fetch("/api/polar/sync", {
        method: "POST",
        headers: { "Content-Type": "application/json", Accept: "application/json" },
        body: JSON.stringify({ days: 3 })
      });
      const payload = await response.json();
      if (!response.ok || payload?.ok === false) {
        throw new Error(payload?.message || "Polar-Sync konnte nicht gestartet werden.");
      }
      await loadRecoveryData();
      setText(els.controlError, "Recovery-Daten aktualisiert.");
    } catch (error) {
      setText(els.controlError, error?.message || "Polar-Sync konnte nicht gestartet werden.");
    } finally {
      setSyncButtonState(false);
    }
  }

  function bindEvents() {
    const applyRange = (rawValue) => {
      const value = String(rawValue || "30");
      if (els.range) els.range.value = value;
      state.rangeDays = value === "all" ? "all" : Number(value || 30);
      els.rangeButtons.forEach((button) => {
        const active = button.dataset.rangeValue === value;
        button.classList.toggle("is-active", active);
        button.setAttribute("aria-pressed", String(active));
      });
      loadRecoveryData();
    };

    if (els.range) {
      els.range.addEventListener("change", () => {
        applyRange(els.range.value);
      });
    }
    els.rangeButtons.forEach((button) => {
      button.addEventListener("click", () => {
        const value = button.dataset.rangeValue || "30";
        if (String(state.rangeDays) === value) return;
        applyRange(value);
      });
    });
    if (els.syncButton) {
      els.syncButton.addEventListener("click", () => {
        triggerPolarSync();
      });
    }
    document.querySelectorAll("th[data-sort] .hrv-sort-btn").forEach((btn) => {
      btn.addEventListener("click", () => {
        const th = btn.closest("th[data-sort]");
        const key = th?.getAttribute("data-sort");
        if (!key) return;
        if (state.sortKey === key) {
          state.sortDir = state.sortDir === "asc" ? "desc" : "asc";
        } else {
          state.sortKey = key;
          state.sortDir = key === "date" ? "desc" : "asc";
        }
        updateSortIndicators();
        renderTable();
      });
    });
    if (els.tableBody) {
      els.tableBody.addEventListener("click", (event) => {
        const button = event.target.closest(".hrv-flag-button");
        if (!button) return;
        const dateIso = button.getAttribute("data-date");
        if (!dateIso) return;
        const current = state.rows.find((row) => row.date === dateIso)?.flag ?? null;
        setFlag(dateIso, nextFlagCycle(current));
      });
    }
    window.addEventListener("resize", () => {
      drawSparkline(els.miniRmssdSpark, seriesForLast(14, "rmssd"), "#34d399");
      drawSparkline(els.miniPulseSpark, seriesForLast(14, "nightPulse"), "#60a5fa");
    });
  }

  function init() {
    setSyncButtonState(false);
    bindEvents();
    loadRecoveryData();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }

  if (typeof module !== "undefined" && module.exports) {
    module.exports = { nextFlagCycle };
  }
})();
