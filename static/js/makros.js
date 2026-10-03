(() => {
  const app = document.getElementById("makros-app");
  if (!app) return;
  const authLevel = (app.dataset.authLevel || "none").toLowerCase();
  const isKey = authLevel === "key";

  const LOCALE = "de-DE";
  const TIME_ZONE = "Europe/Berlin";
  const DEFAULT_MODE = "maintenance";
  const DEFAULT_RANGE = 90;
  const PROTEIN_TARGET_PER_KG = 2.0;
  const MODE_KEYS = ["cut", "lean_bulk", "maintenance", "custom"];

  const els = {
    modeButton: document.getElementById("makros-mode-button"),
    rangeSelect: document.getElementById("makros-range"),
    controlsError: document.getElementById("makros-controls-error"),
    scoreTitle: document.getElementById("makros-score-title"),
    scoreValue: document.getElementById("makros-score-value"),
    scoreLabel: document.getElementById("makros-score-label"),
    scoreBadge: document.getElementById("makros-score-badge"),
    scoreBar: document.getElementById("makros-score-bar-fill"),
    driverKcalLabel: document.getElementById("makros-driver-kcal-label"),
    driverKcalValue: document.getElementById("makros-driver-kcal-value"),
    driverKcalDelta: document.getElementById("makros-driver-kcal-delta"),
    driverKcalPill: document.getElementById("makros-driver-kcal-pill"),
    driverProteinLabel: document.getElementById("makros-driver-protein-label"),
    driverProteinValue: document.getElementById("makros-driver-protein-value"),
    driverProteinDelta: document.getElementById("makros-driver-protein-delta"),
    driverProteinPill: document.getElementById("makros-driver-protein-pill"),
    driverWeightLabel: document.getElementById("makros-driver-weight-label"),
    driverWeightValue: document.getElementById("makros-driver-weight-value"),
    driverWeightDelta: document.getElementById("makros-driver-weight-delta"),
    driverWeightPill: document.getElementById("makros-driver-weight-pill"),
    scoreContext: document.getElementById("makros-score-context"),
    insightsList: document.getElementById("makros-insights-list"),
    miniProteinLabel: document.getElementById("makros-mini-protein-label"),
    miniProtein: document.getElementById("makros-mini-protein"),
    miniProteinTrend: document.getElementById("makros-mini-protein-trend"),
    miniCarbsLabel: document.getElementById("makros-mini-carbs-label"),
    miniCarbs: document.getElementById("makros-mini-carbs"),
    miniCarbsTrend: document.getElementById("makros-mini-carbs-trend"),
    miniFatLabel: document.getElementById("makros-mini-fat-label"),
    miniFat: document.getElementById("makros-mini-fat"),
    miniFatTrend: document.getElementById("makros-mini-fat-trend"),
    sparkProtein: document.getElementById("makros-spark-protein"),
    sparkCarbs: document.getElementById("makros-spark-carbs"),
    sparkFat: document.getElementById("makros-spark-fat"),
    rangeLabel: document.getElementById("makros-range-label"),
    rangeMeta: document.getElementById("makros-range-meta"),
    kcalChart: document.getElementById("makros-kcal-chart"),
    kcalCard: document.getElementById("makros-kcal-card"),
    kcalStatus: document.getElementById("makros-kcal-status"),
    weightChart: document.getElementById("makros-weight-chart"),
    weightCard: document.getElementById("makros-weight-card"),
    weightStatus: document.getElementById("makros-weight-status"),
    tableBody: document.getElementById("makros-tbody"),
    tableMeta: document.getElementById("makros-table-meta"),
    tableError: document.getElementById("makros-table-error"),
    exportBtn: document.getElementById("makros-export"),
    streakCurrent: document.getElementById("logging-current-value"),
    streakBest: document.getElementById("logging-best-value"),
    streakTotal: document.getElementById("logging-total-value"),
    modal: document.getElementById("makros-modal"),
    modalTabs: document.querySelectorAll(".makros-modal-tab"),
    modalTarget: document.getElementById("makros-mode-target"),
    modalProtein: document.getElementById("makros-mode-protein"),
    modalCarbs: document.getElementById("makros-mode-carbs"),
    modalFat: document.getElementById("makros-mode-fat"),
    modalGreenLow: document.getElementById("makros-mode-green-low"),
    modalGreenHigh: document.getElementById("makros-mode-green-high"),
    modalYellowLow: document.getElementById("makros-mode-yellow-low"),
    modalYellowHigh: document.getElementById("makros-mode-yellow-high"),
    modalBandPreview: document.getElementById("makros-band-preview"),
    modalError: document.getElementById("makros-modal-error"),
    modalSave: document.getElementById("makros-settings-save"),
    modalCancel: document.getElementById("makros-settings-cancel")
  };

  const state = {
    rows: [],
    rangeDays: DEFAULT_RANGE,
    rangeStart: null,
    rangeEnd: null,
    mode: DEFAULT_MODE,
    sortKey: "date",
    sortDir: "desc",
    modalMode: DEFAULT_MODE,
    settings: {
      active_mode: DEFAULT_MODE,
      modes: {
        cut: { kcal_target: null, green_low: null, green_high: null, yellow_low: null, yellow_high: null },
        lean_bulk: { kcal_target: null, green_low: null, green_high: null, yellow_low: null, yellow_high: null },
        maintenance: { kcal_target: null, green_low: null, green_high: null, yellow_low: null, yellow_high: null },
        custom: { kcal_target: null, green_low: null, green_high: null, yellow_low: null, yellow_high: null }
      }
    }
  };

  const charts = {
    kcal: null,
    weight: null
  };

  const dayLabels = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"];
  const MS_PER_DAY = 86400000;

  function evaluationNutritionInterval() {
    const value = window.LivaEvaluationControls?.getInterval?.("ernaehrung");
    if (value?.start && value?.end) return value;
    const end = new Date();
    const start = new Date(end);
    start.setDate(start.getDate() - (DEFAULT_RANGE - 1));
    return { start: start.toISOString().slice(0, 10), end: end.toISOString().slice(0, 10) };
  }

  function applyNutritionInterval(interval) {
    state.rangeStart = interval.start;
    state.rangeEnd = interval.end;
    const start = new Date(`${interval.start}T00:00:00`);
    const end = new Date(`${interval.end}T00:00:00`);
    state.rangeDays = Math.max(1, Math.round((end - start) / MS_PER_DAY) + 1);
  }

  function setText(el, value) {
    if (!el) return;
    el.textContent = value;
  }

  function setHtml(el, value) {
    if (!el) return;
    el.innerHTML = value;
  }

  function setHidden(el, hidden) {
    if (!el) return;
    el.classList.toggle("makros-hidden", hidden);
  }

  function toNumber(value) {
    if (value === null || value === undefined || value === "") return null;
    const num = Number(value);
    return Number.isFinite(num) ? num : null;
  }

  function formatInt(v) {
    if (v === null || v === undefined || !Number.isFinite(Number(v))) return "--";
    return String(Math.round(Number(v)));
  }

  function setStreakValue(el, value) {
    if (!el) return;
    if (value === null || value === undefined || !Number.isFinite(Number(value))) {
      el.textContent = "--";
      return;
    }
    const n = Math.round(Number(value));
    el.innerHTML = `<span class="pill-num">${n}</span><span class="pill-unit">d</span>`;
  }

  function setInvalid(input, isInvalid) {
    if (!input) return;
    input.classList.toggle("is-invalid", !!isInvalid);
  }

  function readModalValues() {
    return {
      target: toNumber(els.modalTarget?.value),
      greenLow: toNumber(els.modalGreenLow?.value),
      greenHigh: toNumber(els.modalGreenHigh?.value),
      yellowLow: toNumber(els.modalYellowLow?.value),
      yellowHigh: toNumber(els.modalYellowHigh?.value),
    };
  }

  function validateModalValues(values) {
    const { target, greenLow, greenHigh, yellowLow, yellowHigh } = values;
    if (target === null || target <= 0) return { ok: false, error: "Bitte ein gültiges kcal Ziel setzen." };
    if (greenLow === null || greenHigh === null || yellowLow === null || yellowHigh === null) {
      return { ok: false, error: "Bitte alle Band-Grenzen (grün/gelb unten/oben) setzen." };
    }
    if (!(greenLow <= target && target <= greenHigh)) {
      return { ok: false, error: "Grün-Band muss das Ziel enthalten (green_low <= Ziel <= green_high)." };
    }
    if (!(yellowLow <= target && target <= yellowHigh)) {
      return { ok: false, error: "Gelb-Band muss das Ziel enthalten." };
    }
    if (!(yellowLow <= greenLow && greenHigh <= yellowHigh)) {
      return { ok: false, error: "Gelb-Band muss das Grün-Band enthalten." };
    }
    if (yellowHigh <= yellowLow) {
      return { ok: false, error: "Gelb Obergrenze muss > Gelb Untergrenze sein." };
    }
    return { ok: true, error: "" };
  }

  function renderBandPreview() {
    const host = els.modalBandPreview;
    if (!host) return;

    const v = readModalValues();
    const validation = validateModalValues(v);

    // highlight inputs (live)
    setInvalid(els.modalTarget, v.target === null || v.target <= 0);
    setInvalid(els.modalGreenLow, v.greenLow === null);
    setInvalid(els.modalGreenHigh, v.greenHigh === null);
    setInvalid(els.modalYellowLow, v.yellowLow === null);
    setInvalid(els.modalYellowHigh, v.yellowHigh === null);

    if (!validation.ok) {
      host.classList.add("is-empty");
      host.innerHTML = `<div>${validation.error}</div>`;
      return;
    }

    host.classList.remove("is-empty");
    const span = v.yellowHigh - v.yellowLow;
    const pct = (x) => Math.max(0, Math.min(100, ((x - v.yellowLow) / span) * 100));
    const greenLeft = pct(v.greenLow);
    const greenRight = pct(v.greenHigh);
    const targetPos = pct(v.target);

    host.innerHTML = `
      <div class="makros-band-bar" aria-hidden="true">
        <div class="makros-band-seg is-yellow" style="left:0%;width:100%"></div>
        <div class="makros-band-seg is-green" style="left:${greenLeft}%;width:${Math.max(0, greenRight - greenLeft)}%"></div>
        <div class="makros-band-target" style="left:${targetPos}%"></div>
      </div>
      <div class="makros-band-meta">
        <div class="makros-band-chip" title="Ziel">
          <span class="makros-band-dot is-target"></span>
          <span>Ziel: ${formatInt(v.target)}</span>
        </div>
        <div class="makros-band-chip" title="Grün-Band (OK)">
          <span class="makros-band-dot is-green"></span>
          <span>Grün: ${formatInt(v.greenLow)}–${formatInt(v.greenHigh)}</span>
        </div>
        <div class="makros-band-chip" title="Gelb-Band (Warn)">
          <span class="makros-band-dot is-yellow"></span>
          <span>Gelb: ${formatInt(v.yellowLow)}–${formatInt(v.yellowHigh)}</span>
        </div>
      </div>
    `;
  }

  function mean(values) {
    if (!values.length) return null;
    const total = values.reduce((sum, v) => sum + v, 0);
    return total / values.length;
  }

  function stddev(values) {
    if (values.length < 2) return null;
    const avg = mean(values);
    if (avg === null) return null;
    const variance = mean(values.map((v) => (v - avg) ** 2));
    return Math.sqrt(variance);
  }

  function rollingMean(values, window) {
    const out = [];
    for (let i = 0; i < values.length; i += 1) {
      const slice = values.slice(Math.max(0, i - window + 1), i + 1).filter((v) => v !== null);
      if (slice.length < window) {
        out.push(null);
      } else {
        out.push(mean(slice));
      }
    }
    return out;
  }


  function formatValue(value, decimals) {
    if (value === null || value === undefined || Number.isNaN(value)) return "--";
    const num = Number(value);
    return Number.isFinite(num) ? num.toFixed(decimals) : "--";
  }

  function formatSigned(value, decimals) {
    if (value === null || value === undefined || Number.isNaN(value)) return "--";
    const sign = value > 0 ? "+" : value < 0 ? "-" : "";
    return `${sign}${Math.abs(value).toFixed(decimals)}`;
  }

  function formatPercent(value, decimals) {
    if (value === null || value === undefined || Number.isNaN(value)) return "--";
    return `${formatSigned(value, decimals)}%`;
  }

  function formatShortDate(date, includeYear = false) {
    if (!date) return "--";
    const options = {
      timeZone: TIME_ZONE,
      day: "2-digit",
      month: "2-digit"
    };
    if (includeYear) {
      options.year = "2-digit";
    }
    return new Intl.DateTimeFormat(LOCALE, options).format(date);
  }

  function formatLongDate(date) {
    if (!date) return "--";
    return new Intl.DateTimeFormat(LOCALE, {
      timeZone: TIME_ZONE,
      day: "2-digit",
      month: "2-digit",
      year: "2-digit"
    }).format(date);
  }

  function parseDate(value) {
    if (!value) return null;
    if (typeof value === "string") {
      if (value.includes("-")) {
        const [y, m, d] = value.split("-").map((part) => parseInt(part, 10));
        if (!y || !m || !d) return null;
        return new Date(Date.UTC(y, m - 1, d));
      }
      if (value.includes(".")) {
        const parts = value.split(".");
        if (parts.length >= 3) {
          const day = parseInt(parts[0], 10);
          const month = parseInt(parts[1], 10);
          let year = parseInt(parts[2], 10);
          if (year < 100) year += 2000;
          if (!day || !month || !year) return null;
          return new Date(Date.UTC(year, month - 1, day));
        }
      }
    }
    return null;
  }

  function formatModeLabel(mode) {
    if (mode === "cut") return "Cut";
    if (mode === "lean_bulk") return "Lean Bulk";
    if (mode === "maintenance") return "Maintenance";
    return "Custom";
  }

  function getTargetKcal() {
    const modeSettings = state.settings.modes?.[state.mode];
    return modeSettings ? modeSettings.kcal_target : null;
  }

  function getModeBounds(modeSettings, target) {
    if (!modeSettings || target === null || target === undefined) return null;
    const greenLow = modeSettings.green_low;
    const greenHigh = modeSettings.green_high;
    const yellowLow = modeSettings.yellow_low;
    const yellowHigh = modeSettings.yellow_high;

    if (
      greenLow === null || greenLow === undefined ||
      greenHigh === null || greenHigh === undefined ||
      yellowLow === null || yellowLow === undefined ||
      yellowHigh === null || yellowHigh === undefined
    ) {
      return null;
    }

    return {
      green_low: greenLow,
      green_high: greenHigh,
      yellow_low: yellowLow,
      yellow_high: yellowHigh,
      target
    };
  }

  function getTargetBand(target) {
    const modeSettings = state.settings.modes?.[state.mode];
    if (!modeSettings || target === null || target === undefined) return null;
    const bounds = getModeBounds(modeSettings, target);
    if (!bounds) return null;
    return { low: bounds.green_low - target, high: bounds.green_high - target };
  }

  function computeCompliance(values, target, band) {
    if (!values.length || target === null || band === null) return null;
    const low = target + band.low;
    const high = target + band.high;
    const hits = values.filter((v) => v >= low && v <= high);
    return {
      pct: (hits.length / values.length) * 100,
      hits: hits.length,
      total: values.length
    };
  }

  function setBadge(el, status, text) {
    if (!el) return;
    el.dataset.status = status || "neutral";
    const badgeText = text || "";
    el.textContent = badgeText;
    el.classList.toggle("is-hidden", !badgeText);
  }

  function setDriverPill(el, status) {
    const label = status === "green" ? "OK" : status === "yellow" ? "WARN" : status === "red" ? "ALERT" : "--";
    setBadge(el, status, label);
  }


  function setupSparkCanvas(canvas) {
    if (!canvas) return null;
    const rect = canvas.getBoundingClientRect();
    const cssWidth = rect.width || canvas.clientWidth || canvas.width || 0;
    const cssHeight = rect.height || canvas.clientHeight || canvas.height || 0;
    const width = Math.max(1, Math.round(cssWidth));
    const height = Math.max(1, Math.round(cssHeight));
    const dpr = window.devicePixelRatio || 1;
    const targetWidth = Math.max(1, Math.round(width * dpr));
    const targetHeight = Math.max(1, Math.round(height * dpr));

    if (canvas.width !== targetWidth || canvas.height !== targetHeight) {
      canvas.width = targetWidth;
      canvas.height = targetHeight;
    }

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

    const clean = values.filter((v) => v !== null);
    const hasScale = clean.length >= 2;
    if (!hasScale) {
      ctx.strokeStyle = "rgba(148,163,184,0.4)";
      ctx.beginPath();
      ctx.moveTo(0, height / 2);
      ctx.lineTo(width, height / 2);
      ctx.stroke();
      return;
    }

    const min = Math.min(...clean);
    const max = Math.max(...clean);
    const span = max - min || 1;

    ctx.strokeStyle = color;
    ctx.lineWidth = 2;
    ctx.beginPath();
    values.forEach((value, index) => {
      if (value === null) return;
      const x = values.length > 1 ? (index / (values.length - 1)) * width : width / 2;
      const y = height - ((value - min) / span) * height;
      if (index === 0) {
        ctx.moveTo(x, y);
      } else {
        ctx.lineTo(x, y);
      }
    });
    ctx.stroke();
  }

  function getScaleBounds(values, minPad = 0.2, maxPad = 0.5) {
    const clean = values.filter((v) => v !== null && v !== undefined && !Number.isNaN(v));
    if (!clean.length) return null;
    const min = Math.min(...clean);
    const max = Math.max(...clean);
    const span = max - min;
    const pad = span > 0 ? Math.max(minPad, span * 0.1) : maxPad;
    return { min: min - pad, max: max + pad };
  }

  function normalizeRows(rawRows) {
    return rawRows
      .map((row) => {
        const date = parseDate(row.date_iso || row.date);
        return {
          ...row,
          _date: date,
          _calories: toNumber(row.calories),
          _protein: toNumber(row.protein_g),
          _carbs: toNumber(row.carbs_g),
          _fat: toNumber(row.fat_g),
          _sugar: toNumber(row.sugar_g),
          _weight: toNumber(row.bodyweight_kg),
          _targetKcal: toNumber(row.target_kcal),
          _targetGreenLow: toNumber(row.green_low),
          _targetGreenHigh: toNumber(row.green_high),
          _targetYellowLow: toNumber(row.yellow_low),
          _targetYellowHigh: toNumber(row.yellow_high),
          _targetMode: row.mode ? String(row.mode) : "",
          _targetSource: row.source ? String(row.source) : "",
          _targetTemplateTitle: row.template_title ? String(row.template_title) : "",
          _note: row.note ? String(row.note) : ""
        };
      })
      .filter((row) => row._date);
  }

  function getEffectiveMode(rows = state.rows) {
    const latest = rows.slice().reverse().find((row) => row._targetMode);
    return latest?._targetMode || state.mode;
  }

  function getRowTargetContext(row) {
    if (!row) return null;
    const target = row._targetKcal;
    if (target !== null && target !== undefined) {
      return {
        target,
        greenLow: row._targetGreenLow,
        greenHigh: row._targetGreenHigh,
        yellowLow: row._targetYellowLow,
        yellowHigh: row._targetYellowHigh,
        mode: row._targetMode || state.mode,
        source: row._targetSource || "",
        templateTitle: row._targetTemplateTitle || ""
      };
    }
    const fallbackTarget = getTargetKcal();
    const bounds = getModeBounds(state.settings.modes?.[state.mode], fallbackTarget);
    if (!bounds || fallbackTarget === null || fallbackTarget === undefined) return null;
    return {
      target: fallbackTarget,
      greenLow: bounds.green_low,
      greenHigh: bounds.green_high,
      yellowLow: bounds.yellow_low,
      yellowHigh: bounds.yellow_high,
      mode: state.mode,
      source: "mode",
      templateTitle: ""
    };
  }

  function classifyKcalRow(row) {
    const ctx = getRowTargetContext(row);
    if (!ctx || row?._calories === null) return "neutral";
    const kcal = row._calories;
    if (ctx.greenLow !== null && ctx.greenHigh !== null && kcal >= ctx.greenLow && kcal <= ctx.greenHigh) return "green";
    if (ctx.yellowLow !== null && ctx.yellowHigh !== null && kcal >= ctx.yellowLow && kcal <= ctx.yellowHigh) return "yellow";
    if (ctx.target !== null) return "red";
    return "neutral";
  }

  function summarizeKcalTargets(rows) {
    const kcalValues = [];
    const targets = [];
    const deltas = [];
    const statusCounts = { green: 0, yellow: 0, red: 0 };
    let totalWithTarget = 0;
    let greenHits = 0;

    rows.forEach((row) => {
      if (row._calories === null) return;
      kcalValues.push(row._calories);
      const ctx = getRowTargetContext(row);
      if (!ctx || ctx.target === null || ctx.target === undefined) return;
      targets.push(ctx.target);
      deltas.push(row._calories - ctx.target);
      totalWithTarget += 1;
      const status = classifyKcalRow(row);
      if (status === "green") {
        greenHits += 1;
      }
      if (statusCounts[status] !== undefined) {
        statusCounts[status] += 1;
      }
    });

    let status = "neutral";
    if (totalWithTarget) {
      if (statusCounts.green >= Math.max(statusCounts.yellow, statusCounts.red)) status = "green";
      else if (statusCounts.yellow >= statusCounts.red) status = "yellow";
      else status = "red";
    }

    return {
      avgKcal: mean(kcalValues),
      avgTarget: mean(targets),
      avgDelta: mean(deltas),
      status,
      compliance: totalWithTarget
        ? { pct: (greenHits / totalWithTarget) * 100, hits: greenHits, total: totalWithTarget }
        : null
    };
  }

  function updateHeader(rows, targetKcal) {
    const modeLabel = formatModeLabel(getEffectiveMode(rows));
    if (els.modeButton) {
      els.modeButton.dataset.mode = getEffectiveMode(rows);
      setText(els.modeButton, modeLabel);
    }
  }

  function updateStatusCard(rows, targetKcal) {
    const windowDays = state.rangeDays;
    const rangeLabel = windowDays === "all" ? "ALL" : `${windowDays}T`;
    const windowRows = getWindowRows(rows, windowDays);
    const kcalSummary = summarizeKcalTargets(windowRows);
    const avgKcal = kcalSummary.avgKcal;
    const avgTarget = kcalSummary.avgTarget;
    const compliance = kcalSummary.compliance;
    const delta = kcalSummary.avgDelta;
    const hasTarget = avgTarget !== null && avgTarget !== undefined;
    const effectiveMode = getEffectiveMode(windowRows);

    setText(els.scoreTitle, `Ernährungs-Score (${rangeLabel})`);
    setText(els.driverKcalLabel, `Kalorien (Ø${rangeLabel})`);
    setText(els.driverProteinLabel, `Protein (Ø${rangeLabel})`);
    setText(els.driverWeightLabel, `Gewicht (${rangeLabel} Trend)`);

    if (avgKcal !== null && hasTarget) {
      setText(els.driverKcalValue, `${formatValue(avgKcal, 0)} kcal vs Ø Ziel ${formatValue(avgTarget, 0)} kcal`);
      setText(els.driverKcalDelta, `→ ${formatSigned(delta, 0)} kcal`);
    } else {
      setText(els.driverKcalValue, "--");
      setText(els.driverKcalDelta, "--");
    }
    setDriverPill(els.driverKcalPill, kcalSummary.status);

    const weights = windowRows.map((row) => row._weight).filter((v) => v !== null);
    const weightTrend = computeWeightTrendPerWeek(windowRows, windowDays);
    const weightTarget = getWeightTarget(effectiveMode);
    if (weightTrend !== null) {
      setText(els.driverWeightValue, `${formatSigned(weightTrend, 1)} kg/Woche`);
      if (weightTarget !== null) {
        const deltaTrend = weightTrend - weightTarget;
        setText(els.driverWeightDelta, `Δ ${formatSigned(deltaTrend, 2)} kg/Woche`);
      } else {
        setText(els.driverWeightDelta, "--");
      }
    } else {
      setText(els.driverWeightValue, "--");
      setText(els.driverWeightDelta, "--");
    }
    setDriverPill(els.driverWeightPill, getWeightStatus(weightTrend, effectiveMode));

    const proteinValues = windowRows.map((row) => row._protein).filter((v) => v !== null);
    const avgProtein = mean(proteinValues);
    const avgWeight = mean(weights);
    const gPerKg = avgProtein !== null && avgWeight ? avgProtein / avgWeight : null;
    const proteinTarget = PROTEIN_TARGET_PER_KG;
    if (avgProtein !== null && gPerKg !== null) {
      setText(els.driverProteinValue, `${formatValue(avgProtein, 0)} g (${formatValue(gPerKg, 1)} g/kg)`);
      setText(els.driverProteinDelta, `Δ ${formatSigned(gPerKg - proteinTarget, 1)} g/kg`);
    } else if (avgProtein !== null) {
      setText(els.driverProteinValue, `${formatValue(avgProtein, 0)} g`);
      setText(els.driverProteinDelta, "--");
    } else {
      setText(els.driverProteinValue, "--");
      setText(els.driverProteinDelta, "--");
    }
    setDriverPill(els.driverProteinPill, getProteinStatus(gPerKg, proteinTarget));

    const score = computeScore(delta, gPerKg, proteinTarget, weightTrend, null, effectiveMode);
    setText(els.scoreValue, score !== null ? String(score) : "--");
    setText(els.scoreLabel, `für ${formatModeLabel(effectiveMode)}`);

    if (els.scoreBar) {
      els.scoreBar.style.width = score !== null ? `${Math.min(100, Math.max(0, score))}%` : "0%";
    }

    const badge = getKcalBadge(delta, hasTarget, null, kcalSummary.status);
    setBadge(els.scoreBadge, badge.status, badge.text);

    if (els.scoreContext) {
      if (compliance && compliance.pct < 50) {
        setText(els.scoreContext, `Nur ${Math.round(compliance.pct)}% der Tage im Zielband des aktiven Plans (${compliance.hits}/${compliance.total}).`);
        setHidden(els.scoreContext, false);
      } else {
        setText(els.scoreContext, "");
        setHidden(els.scoreContext, true);
      }
    }
  }

  function getWindowRows(rows, windowDays) {
    const sorted = rows.slice().sort((a, b) => a._date - b._date);
    if (!sorted.length) return [];
    if (windowDays === "all" || !Number.isFinite(windowDays)) {
      return sorted;
    }
    const latest = state.rangeEnd ? new Date(`${state.rangeEnd}T00:00:00`) : sorted[sorted.length - 1]._date;
    if (!latest) return [];
    const cutoff = new Date(latest.getTime() - (windowDays - 1) * MS_PER_DAY);
    return sorted.filter((row) => row._date && row._date >= cutoff);
  }

  function getKcalStatus(delta, hasTarget, band) {
    if (delta === null || delta === undefined || !hasTarget || !band) return "neutral";
    const modeSettings = state.settings.modes?.[state.mode];
    const target = getTargetKcal();
    const bounds = getModeBounds(modeSettings, target);
    if (!bounds) return "neutral";
    const avg = Number(target) + Number(delta);
    if (avg >= bounds.green_low && avg <= bounds.green_high) return "green";
    if (avg >= bounds.yellow_low && avg <= bounds.yellow_high) return "yellow";
    return "red";
  }

  function getKcalBadge(delta, hasTarget, band, statusOverride = null) {
    const status = statusOverride || getKcalStatus(delta, hasTarget, band);
    if (delta === null || delta === undefined || !hasTarget) return { status: "neutral", text: "--" };
    if (status === "green") return { status: "green", text: "OK" };
    if (delta < 0) return { status, text: "UNDER" };
    if (delta > 0) return { status, text: "OVER" };
    return { status: "green", text: "OK" };
  }

  function getProteinStatus(gPerKg, target) {
    if (gPerKg === null || gPerKg === undefined) return "neutral";
    if (gPerKg >= target) return "green";
    if (gPerKg >= target * 0.95) return "yellow";
    return "red";
  }

  function getWeightStatus(trend, mode = getEffectiveMode()) {
    if (trend === null || trend === undefined) return "neutral";
    if (mode === "lean_bulk") {
      if (trend >= 0.05) return "green";
      if (trend > -0.05) return "yellow";
      return "red";
    }
    if (mode === "cut") {
      if (trend <= -0.05) return "green";
      if (trend < 0.05) return "yellow";
      return "red";
    }
    if (mode === "maintenance") {
      const abs = Math.abs(trend);
      if (abs < 0.05) return "green";
      if (abs < 0.1) return "yellow";
      return "red";
    }
    return "neutral";
  }

  function getWeightTarget(mode = getEffectiveMode()) {
    if (mode === "lean_bulk") return 0.05;
    if (mode === "cut") return -0.05;
    if (mode === "maintenance") return 0;
    return null;
  }

  function computeScore(delta, gPerKg, proteinTarget, weightTrend, band, mode = getEffectiveMode()) {
    if (delta === null || delta === undefined) return null;
    const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));
    let penalty = 0;

    // --- kcal (continuous within bands) ---
    const modeSettings = state.settings.modes?.[state.mode];
    const target = getTargetKcal();
    const bounds = getModeBounds(modeSettings, target);
    if (bounds) {
      const avg = Number(bounds.target) + Number(delta);
      if (avg >= bounds.green_low && avg <= bounds.green_high) {
        penalty += 0;
      } else if (avg >= bounds.yellow_low && avg <= bounds.yellow_high) {
        // between yellow and green => 0..10
        if (avg < bounds.green_low) {
          const denom = Math.max(1, bounds.green_low - bounds.yellow_low);
          penalty += clamp(((bounds.green_low - avg) / denom) * 10, 0, 10);
        } else {
          const denom = Math.max(1, bounds.yellow_high - bounds.green_high);
          penalty += clamp(((avg - bounds.green_high) / denom) * 10, 0, 10);
        }
      } else {
        // outside yellow => 10..30 (grows with distance; ~300 kcal -> max)
        const dist = avg < bounds.yellow_low ? (bounds.yellow_low - avg) : (avg - bounds.yellow_high);
        penalty += 10 + clamp((dist / 300) * 20, 0, 20);
      }
    } else {
      // fallback: discrete status if bounds missing
      const kcalStatus = getKcalStatus(delta, true, band);
      if (kcalStatus === "yellow") penalty += 10;
      if (kcalStatus === "red") penalty += 30;
    }

    // --- protein (continuous 0..20) ---
    if (gPerKg !== null && gPerKg !== undefined && proteinTarget) {
      const deficitPct = (proteinTarget - gPerKg) / proteinTarget;
      if (deficitPct > 0) penalty += clamp(deficitPct * 100, 0, 20);
    }

    // --- weight trend (continuous 0..20; mode-dependent) ---
    if (weightTrend !== null && weightTrend !== undefined) {
      const t = Number(weightTrend);
      if (Number.isFinite(t)) {
        if (mode === "maintenance") {
          const abs = Math.abs(t);
          if (abs <= 0.05) penalty += 0;
          else if (abs <= 0.1) penalty += clamp(((abs - 0.05) / 0.05) * 10, 0, 10);
          else penalty += 10 + clamp(((abs - 0.1) / 0.1) * 10, 0, 10);
        } else if (mode === "lean_bulk") {
          if (t >= 0.05) penalty += 0;
          else if (t >= -0.05) penalty += clamp(((0.05 - t) / 0.1) * 10, 0, 10);
          else penalty += 10 + clamp(((-0.05 - t) / 0.1) * 10, 0, 10);
        } else if (mode === "cut") {
          if (t <= -0.05) penalty += 0;
          else if (t < 0.05) penalty += clamp(((t + 0.05) / 0.1) * 10, 0, 10);
          else penalty += 10 + clamp(((t - 0.05) / 0.1) * 10, 0, 10);
        } else {
          // custom: no weight penalty
        }
      }
    }

    return Math.max(0, Math.min(100, Math.round(100 - penalty)));
  }


  function computeWeightTrendPerWeek(rows, windowDays) {
    const sorted = rows.slice().sort((a, b) => a._date - b._date);
    if (!sorted.length) return null;
    const latest = sorted[sorted.length - 1]._date;
    if (!latest) return null;
    let windowRows = sorted.filter((row) => row._date && row._weight !== null);
    if (Number.isFinite(windowDays)) {
      const cutoff = new Date(latest.getTime() - (windowDays - 1) * MS_PER_DAY);
      windowRows = windowRows.filter((row) => row._date >= cutoff);
    }
    if (windowRows.length < 2) return null;
    const first = windowRows[0];
    const last = windowRows[windowRows.length - 1];
    const daySpan = (last._date - first._date) / MS_PER_DAY;
    if (daySpan < 7) return null;
    return (last._weight - first._weight) / (daySpan / 7);
  }

  function buildInsights(rows, targetKcal) {
    const insights = [];
    const addInsight = (text, tone) => {
      if (insights.length >= 3) return;
      if (insights.some((item) => item.text === text)) return;
      insights.push({ text, tone });
    };

    const windowDays = state.rangeDays;
    const rangeLabel = windowDays === "all" ? "ALL" : `${windowDays}T`;
    const windowRows = getWindowRows(rows, windowDays);
    const kcalValues = windowRows.map((row) => row._calories).filter((v) => v !== null);
    const kcalSummary = summarizeKcalTargets(windowRows);
    const delta = kcalSummary.avgDelta;
    const effectiveMode = getEffectiveMode(windowRows);

    const weights = windowRows.map((row) => row._weight).filter((v) => v !== null);
    const avgWeight = mean(weights);
    const weightTrend = computeWeightTrendPerWeek(windowRows, windowDays);
    const proteinRows = windowRows.filter((row) => row._protein !== null);
    const belowProtein = avgWeight
      ? proteinRows.filter((row) => row._protein < (row._weight ?? avgWeight) * PROTEIN_TARGET_PER_KG).length
      : 0;
    const totalProtein = proteinRows.length;

    let mainType = null;
    if (effectiveMode === "lean_bulk") {
      if (delta !== null && delta < -150) mainType = "kcal";
      else if (weightTrend !== null && weightTrend <= -0.05) mainType = "weight";
      else if (belowProtein > 0) mainType = "protein";
      else if (delta !== null) mainType = "kcal";
    } else if (effectiveMode === "cut") {
      if (delta !== null && delta > 150) mainType = "kcal";
      else if (weightTrend !== null && weightTrend >= 0.05) mainType = "weight";
      else if (belowProtein > 0) mainType = "protein";
      else if (delta !== null) mainType = "kcal";
    } else {
      if (delta !== null && Math.abs(delta) > 200) mainType = "kcal";
      else if (weightTrend !== null && Math.abs(weightTrend) >= 0.1) mainType = "weight";
      else if (belowProtein > 0) mainType = "protein";
      else if (delta !== null) mainType = "kcal";
    }

    if (mainType === "kcal" && delta !== null) {
      const dir = delta < 0 ? "unter" : delta > 0 ? "über" : "im";
      const kcalStatus = kcalSummary.status;
      const tone = kcalStatus === "red" ? "bad" : kcalStatus === "yellow" ? "warn" : "neutral";
      addInsight(`Hauptproblem: Ø${rangeLabel} ${formatSigned(delta, 0)} kcal ${dir} dem aktiven Plan-Ziel.`, tone);
    } else if (mainType === "weight" && weightTrend !== null) {
      const status = getWeightStatus(weightTrend, effectiveMode);
      const tone = status === "red" ? "bad" : status === "yellow" ? "warn" : "neutral";
      addInsight(`Hauptproblem: Gewichttrend ${formatSigned(weightTrend, 1)} kg/Woche (${rangeLabel}).`, tone);
    } else if (mainType === "protein" && totalProtein) {
      const tone = belowProtein > 2 ? "bad" : "warn";
      addInsight(`Hauptproblem: Protein an ${belowProtein} von ${totalProtein} Tagen unter Ziel.`, tone);
    } else if (delta === null && !windowRows.length) {
      addInsight("Hauptproblem: keine Daten im Zeitraum.", "neutral");
    }

    if (weightTrend !== null && mainType !== "weight") {
      const status = getWeightStatus(weightTrend, effectiveMode);
      const tone = status === "red" ? "bad" : status === "yellow" ? "warn" : "neutral";
      addInsight(`Konsequenz: Gewichttrend ${formatSigned(weightTrend, 1)} kg/Woche (${rangeLabel}).`, tone);
    }

    if (kcalValues.length) {
      let maxRow = null;
      windowRows.forEach((row) => {
        if (row._calories === null) return;
        if (!maxRow || row._calories > maxRow._calories) maxRow = row;
      });
      if (maxRow) {
        const dayIndex = maxRow._date ? (maxRow._date.getUTCDay() + 6) % 7 : null;
        const dayLabel = dayIndex !== null ? dayLabels[dayIndex] : "--";
        addInsight(`Spitze: ${formatValue(maxRow._calories, 0)} kcal (${dayLabel}).`, "neutral");
      }
    }

    return insights.slice(0, 3);
  }

  function renderInsights(insights) {
    if (!els.insightsList) return;
    if (!insights.length) {
      setHtml(els.insightsList, "<div class='makros-insight-item makros-insight-neutral'>Keine Insights verfügbar.</div>");
      return;
    }

    const html = insights
      .map((item) => {
        const tone = item.tone === "good"
          ? "makros-insight-good"
          : item.tone === "bad"
            ? "makros-insight-bad"
            : item.tone === "warn"
              ? "makros-insight-warn"
              : "makros-insight-neutral";
        return `<div class="makros-insight-item ${tone}">${item.text}</div>`;
      })
      .join("");

    setHtml(els.insightsList, html);
  }

  function updateMiniCards(rows) {
    const sorted = rows.slice().sort((a, b) => a._date - b._date);
    const windowDays = Number.isFinite(state.rangeDays) ? state.rangeDays : null;
    const rangeLabel = windowDays ? `${windowDays}T` : "ALL";
    setText(els.miniProteinLabel, `Ø Protein (${rangeLabel})`);
    setText(els.miniCarbsLabel, `Ø Carbs (${rangeLabel})`);
    setText(els.miniFatLabel, `Ø Fett (${rangeLabel})`);

    if (!sorted.length) {
      setText(els.miniProtein, "--");
      setText(els.miniCarbs, "--");
      setMiniTrend(els.miniProteinTrend, null, null);
      setMiniTrend(els.miniCarbsTrend, null, null);
      setText(els.miniFat, "--");
      setMiniTrend(els.miniFatTrend, null, null);
      drawSparkline(els.sparkProtein, [], "#34d399");
      drawSparkline(els.sparkCarbs, [], "#fbbf24");
      drawSparkline(els.sparkFat, [], "#f97316");
      return;
    }

    const latest = state.rangeEnd ? new Date(`${state.rangeEnd}T00:00:00`) : sorted[sorted.length - 1]._date;
    const cutoff = windowDays && latest ? new Date(latest.getTime() - (windowDays - 1) * MS_PER_DAY) : null;
    const prevCutoff = windowDays && cutoff ? new Date(cutoff.getTime() - windowDays * MS_PER_DAY) : null;

    const windowRows = cutoff ? sorted.filter((row) => row._date && row._date >= cutoff) : sorted;
    const prevRows = prevCutoff ? sorted.filter((row) => row._date && row._date >= prevCutoff && row._date < cutoff) : [];

    const proteinSeries = windowRows.map((row) => row._protein).filter((v) => v !== null);
    const carbsSeries = windowRows.map((row) => row._carbs).filter((v) => v !== null);
    const fatSeries = windowRows.map((row) => row._fat).filter((v) => v !== null);

    const avgProt = mean(proteinSeries);
    const avgCarbs = mean(carbsSeries);
    const avgFat = mean(fatSeries);

    const prevAvgProt = mean(prevRows.map((row) => row._protein).filter((v) => v !== null));
    const prevAvgCarbs = mean(prevRows.map((row) => row._carbs).filter((v) => v !== null));
    const prevAvgFat = mean(prevRows.map((row) => row._fat).filter((v) => v !== null));

    setText(els.miniProtein, avgProt !== null ? `${formatValue(avgProt, 0)} g` : "--");
    setText(els.miniCarbs, avgCarbs !== null ? `${formatValue(avgCarbs, 0)} g` : "--");
    setText(els.miniFat, avgFat !== null ? `${formatValue(avgFat, 0)} g` : "--");

    setMiniTrend(els.miniProteinTrend, avgProt, prevAvgProt);
    setMiniTrend(els.miniCarbsTrend, avgCarbs, prevAvgCarbs);
    setMiniTrend(els.miniFatTrend, avgFat, prevAvgFat);

    drawSparkline(els.sparkProtein, proteinSeries, "#34d399");
    drawSparkline(els.sparkCarbs, carbsSeries, "#fbbf24");
    drawSparkline(els.sparkFat, fatSeries, "#f97316");
  }

  function setMiniTrend(el, recent, prev) {
    if (!el) return;
    if (recent === null || prev === null || prev === 0) {
      el.textContent = "";
      el.classList.remove("is-good", "is-bad", "is-neutral");
      el.classList.add("is-empty");
      return;
    }
    const delta = ((recent - prev) / prev) * 100;
    let cls = "is-neutral";
    if (delta > 0) cls = "is-good";
    if (delta < 0) cls = "is-bad";
    el.textContent = formatPercent(delta, 1);
    el.classList.remove("is-good", "is-bad", "is-neutral", "is-empty");
    el.classList.add(cls);
  }

  function initChartDefaults() {
    if (!window.Chart || Chart.defaults._makrosInit) return;
    Chart.defaults._makrosInit = true;
    Chart.defaults.color = "rgba(148,163,184,0.85)";
    Chart.defaults.borderColor = "rgba(148,163,184,0.2)";
    Chart.defaults.font.family = "Inter, system-ui, sans-serif";
    Chart.defaults.plugins.legend.labels.boxWidth = 12;
    Chart.defaults.plugins.legend.labels.boxHeight = 12;
  }

  const targetBandsPlugin = {
    id: "targetBands",
    beforeDatasetsDraw(chart, args, opts) {
      if (!opts || !chart.chartArea) return;
      const { ctx, chartArea, scales } = chart;
      const y = scales.y;
      const x = scales.x;
      if (!y) return;
      const segments = Array.isArray(opts.segments) ? opts.segments : [];
      if (!segments.length || !x) return;

      const toY = (val) => y.getPixelForValue(val);
      const rect = (left, right, low, high, fillStyle) => {
        const yTop = toY(high);
        const yBottom = toY(low);
        const top = Math.min(yTop, yBottom);
        const height = Math.abs(yBottom - yTop);
        ctx.fillStyle = fillStyle;
        ctx.fillRect(left, top, Math.max(0, right - left), height);
      };

      ctx.save();
      ctx.globalCompositeOperation = "screen";
      const yellowFill = opts.yellowColor || "rgba(244, 230, 74, 0.4)";
      const greenFill = opts.greenColor || "rgba(40, 184, 119, 0.61)";
      const centers = segments.map((_, index) => x.getPixelForValue(index));

      segments.forEach((segment, index) => {
        if (!segment) return;
        const greenLow = segment.greenLow;
        const greenHigh = segment.greenHigh;
        const yellowLow = segment.yellowLow;
        const yellowHigh = segment.yellowHigh;
        if ([greenLow, greenHigh, yellowLow, yellowHigh].some((v) => v === null || v === undefined)) return;
        const left = index === 0 ? chartArea.left : (centers[index - 1] + centers[index]) / 2;
        const right = index === centers.length - 1 ? chartArea.right : (centers[index] + centers[index + 1]) / 2;
        rect(left, right, yellowLow, greenLow, yellowFill);
        rect(left, right, greenHigh, yellowHigh, yellowFill);
        rect(left, right, greenLow, greenHigh, greenFill);
      });
      ctx.restore();
    }
  };

  function renderKcalChart(rows, targetKcal) {
    if (!els.kcalChart) return;
    const rowsWithKcal = rows.filter((row) => row._calories !== null);
    if (!rowsWithKcal.length) {
      setText(els.kcalStatus, "Keine Kaloriendaten im Zeitraum.");
      if (charts.kcal) {
        charts.kcal.destroy();
        charts.kcal = null;
      }
      return;
    }
    const includeYear = state.rangeDays === "all" || (Number.isFinite(state.rangeDays) && state.rangeDays >= 365);
    const labels = rowsWithKcal.map((row) => formatShortDate(row._date, includeYear));
    const kcalSeries = rowsWithKcal.map((row) => row._calories);
    const rolling = rollingMean(kcalSeries, 7);
    const targetSegments = rowsWithKcal.map((row) => getRowTargetContext(row));

    setText(els.kcalStatus, "");
    const rangeStart = new Date(`${state.rangeStart}T00:00:00`);
    const rangeEnd = new Date(`${state.rangeEnd}T00:00:00`);
    setText(els.rangeLabel, `${formatShortDate(rangeStart, true)} – ${formatShortDate(rangeEnd, true)}`);
    setText(els.rangeMeta, `${rowsWithKcal.length} Einträge • Ø Linie (7T)`);

    const shouldAnimate = !charts.kcal;
    const data = {
      labels,
      datasets: [
        {
          type: "bar",
          label: "kcal",
          data: kcalSeries,
          backgroundColor: "rgba(96,165,250,0.9)",
          borderColor: "rgba(30,64,175,0.9)",
          borderWidth: 1
        }
      ]
    };

    data.datasets.push({
      type: "line",
      label: "Ø Linie (7T)",
      data: rolling,
      borderColor: "#34d399",
      backgroundColor: "rgba(52,211,153,0.2)",
      tension: 0.25,
      pointRadius: 0,
      borderWidth: 2
    });

    const options = {
      animation: shouldAnimate ? { duration: 400 } : false,
      responsive: true,
      maintainAspectRatio: false,
      plugins: {
        legend: { display: false },
        tooltip: {
          callbacks: {
            label(context) {
              const v = context.parsed?.y;
              if (v === null || v === undefined) return "";
              const base = `${context.dataset?.label || "kcal"}: ${Math.round(v)}`;
              const ctx = targetSegments[context.dataIndex] || null;
              if (!ctx || ctx.target === null || ctx.target === undefined) return base;
              const delta = v - ctx.target;
              const sign = delta > 0 ? "+" : delta < 0 ? "-" : "";
              return `${base} (Δ ${sign}${Math.round(Math.abs(delta))} vs Ziel ${Math.round(ctx.target)})`;
            }
          }
        },
        targetBands: {
          segments: targetSegments,
          yellowColor: "rgba(234, 178, 8, 0.29)",  // muted yellow
          greenColor:  "rgba(16, 185, 129, 0.29)", // muted green
        }
      },
      scales: {
        x: { grid: { display: false }, ticks: { maxTicksLimit: 8 }, bounds: "data", offset: false },
        y: { beginAtZero: true, grid: { color: "rgba(148,163,184,0.15)" } }
      }
    };

    if (!charts.kcal) {
      charts.kcal = new Chart(els.kcalChart.getContext("2d"), { data, options, plugins: [targetBandsPlugin] });
    } else {
      charts.kcal.data = data;
      charts.kcal.options = options;
      charts.kcal.update();
    }
  }

  function renderWeightChart(rows) {
    if (!els.weightChart || !els.weightCard) return;
    const rowsWithWeight = rows.filter((row) => row._weight !== null);
    const weights = rowsWithWeight.map((row) => row._weight);
    const weightRolling = rollingMean(weights, 7);
    const hasWeight = weights.some((v) => v !== null);

    const shouldShow = hasWeight;
    setHidden(els.weightCard, !shouldShow);
    if (!shouldShow) {
      if (charts.weight) {
        charts.weight.destroy();
        charts.weight = null;
      }
      if (els.kcalCard) els.kcalCard.classList.add("is-wide");
      return;
    }

    if (els.kcalCard) els.kcalCard.classList.remove("is-wide");

    const includeYear = state.rangeDays === "all" || (Number.isFinite(state.rangeDays) && state.rangeDays >= 365);
    const labels = [];
    const series = [];
    const rawSeries = [];
    weightRolling.forEach((value, index) => {
      const row = rowsWithWeight[index];
      const displayValue = value !== null ? value : row._weight;
      if (displayValue === null) return;
      labels.push(formatShortDate(row._date, includeYear));
      series.push(displayValue);
      rawSeries.push(row._weight);
    });
    const data = {
      labels,
      datasets: [
        {
          type: "line",
          label: "Gewicht 7T Avg",
          data: series,
          borderColor: "#fbbf24",
          backgroundColor: "rgba(251,191,36,0.2)",
          tension: 0.3,
          pointRadius: 2
        }
      ]
    };

    if (rawSeries.length) {
      data.datasets.push({
        type: "line",
        label: "Gewicht (roh)",
        data: rawSeries,
        borderColor: "#38bdf8",
        backgroundColor: "rgba(59,130,246,0.12)",
        borderWidth: 2,
        pointRadius: 2,
        tension: 0.1
      });
    }

    const combined = series.concat(rawSeries);
    const bounds = getScaleBounds(combined.length ? combined : weights);
    const shouldAnimate = !charts.weight;
    const options = {
      animation: shouldAnimate ? { duration: 400 } : false,
      responsive: true,
      maintainAspectRatio: false,
      plugins: { legend: { display: false } },
      scales: {
        x: { grid: { display: false }, ticks: { maxTicksLimit: 8 }, bounds: "data", offset: false },
        y: {
          grid: { color: "rgba(148,163,184,0.15)" },
          suggestedMin: bounds ? bounds.min : undefined,
          suggestedMax: bounds ? bounds.max : undefined
        }
      }
    };

    setText(els.weightStatus, "");

    if (!charts.weight) {
      charts.weight = new Chart(els.weightChart.getContext("2d"), { data, options });
    } else {
      charts.weight.data = data;
      charts.weight.options = options;
      charts.weight.update();
    }
  }

  function updateTableMeta() {
    if (!els.tableBody || !els.tableMeta) return;
    const rows = els.tableBody.querySelectorAll("tr");
    setText(els.tableMeta, `Zeige ${rows.length} Einträge`);
  }

  async function loadTable() {
    if (!els.tableBody) return;
    const editingRow = els.tableBody.querySelector("tr.editing, tr.new-row");
    if (editingRow) return;
    try {
      const params = new URLSearchParams({ start: state.rangeStart, end: state.rangeEnd });
      const res = await fetch(`/api/makros/table?${params.toString()}`, { cache: "no-store" });
      const html = await res.text();
      els.tableBody.innerHTML = html;
      sortTableRows();
      updateSortIndicators();
      updateTableMeta();
      setText(els.tableError, "");
    } catch (err) {
      setText(els.tableError, "Fehler beim Laden der Tabelle.");
    }
  }

  async function loadLoggingStreaks() {
    try {
      const res = await fetch("/api/makros/logging", { cache: "no-store" });
      if (!res.ok) throw new Error("logging request failed");
      const json = await res.json();
      const logging = json?.logging || {};
      setStreakValue(els.streakCurrent, logging.current_streak_days ?? null);
      setStreakValue(els.streakBest, logging.longest_streak_days ?? null);
      setStreakValue(els.streakTotal, logging.total_logged_days ?? null);
    } catch (err) {
      console.warn("Logging streaks failed", err);
      setStreakValue(els.streakCurrent, null);
      setStreakValue(els.streakBest, null);
      setStreakValue(els.streakTotal, null);
    }
  }

  function exportCsvFromTable() {
    if (!els.tableBody) return;
    const rows = Array.from(els.tableBody.querySelectorAll("tr"));
    if (!rows.length) return;

    const headers = ["date", "weight", "kcal", "protein", "carbs", "fat", "sugar"];
    const lines = [headers.join(",")];
    rows.forEach((row) => {
      const cells = row.querySelectorAll("td");
      if (cells.length < 7) return;
      const values = [
        cells[0].textContent,
        cells[1].textContent,
        cells[2].textContent,
        cells[3].textContent,
        cells[4].textContent,
        cells[5].textContent,
        cells[6].textContent
      ].map((val) => `"${String(val || "").replace(/"/g, '""')}"`);
      lines.push(values.join(","));
    });

    const blob = new Blob([lines.join("\n")], { type: "text/csv;charset=utf-8;" });
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    const stamp = new Date().toISOString().slice(0, 10);
    link.href = url;
    link.download = `makros_export_${stamp}.csv`;
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);
    URL.revokeObjectURL(url);
  }

  function getCellText(cell) {
    if (!cell) return "";
    const input = cell.querySelector("input");
    return (input ? input.value : cell.textContent || "").trim();
  }

  function parseCellNumber(text) {
    if (!text) return null;
    const normalized = text.replace(",", ".").replace(/[^0-9.-]/g, "");
    if (!normalized) return null;
    const num = Number(normalized);
    return Number.isFinite(num) ? num : null;
  }

  function parseCellDateValue(text) {
    const date = parseDate(text);
    return date ? date.getTime() : null;
  }

  function sortTableRows() {
    if (!els.tableBody) return;
    const rows = Array.from(els.tableBody.querySelectorAll("tr"));
    if (!rows.length) return;

    const indexMap = {
      date: 0,
      weight: 1,
      kcal: 2,
      protein: 3,
      carbs: 4,
      fat: 5,
      sugar: 6
    };
    const colIndex = indexMap[state.sortKey] ?? 0;
    const dir = state.sortDir === "asc" ? 1 : -1;

    const sorted = rows.slice().sort((a, b) => {
      const aCell = a.querySelectorAll("td")[colIndex];
      const bCell = b.querySelectorAll("td")[colIndex];
      const aText = getCellText(aCell);
      const bText = getCellText(bCell);

      const aVal = state.sortKey === "date" ? parseCellDateValue(aText) : parseCellNumber(aText);
      const bVal = state.sortKey === "date" ? parseCellDateValue(bText) : parseCellNumber(bText);

      if (aVal === null || aVal === undefined) return 1;
      if (bVal === null || bVal === undefined) return -1;
      if (aVal < bVal) return -1 * dir;
      if (aVal > bVal) return 1 * dir;
      return 0;
    });

    sorted.forEach((row) => {
      els.tableBody.appendChild(row);
    });
  }

  function updateSortIndicators() {
    document.querySelectorAll("th[data-sort]").forEach((th) => {
      const indicator = th.querySelector(".makros-sort-indicator");
      if (!indicator) return;
      const key = th.getAttribute("data-sort");
      if (key === state.sortKey) {
        indicator.textContent = state.sortDir === "asc" ? "↑" : "↓";
      } else {
        indicator.textContent = "";
      }
    });
  }

  function readStateFromStorage() {
    applyNutritionInterval(evaluationNutritionInterval());
    localStorage.setItem("makros_range", String(state.rangeDays));
  }

  function persistState() {
    localStorage.setItem("makros_range", String(state.rangeDays));
  }

  function applyStateToControls() {
    if (els.rangeSelect) els.rangeSelect.value = String(state.rangeDays);
  }

  function openModal() {
    if (isKey) return;
    if (!els.modal) return;
    els.modal.classList.add("is-open");
    els.modal.setAttribute("aria-hidden", "false");
    state.modalMode = state.mode;
    setModalMode(state.modalMode);
    setText(els.modalError, "");
  }

  function closeModal() {
    if (!els.modal) return;
    els.modal.classList.remove("is-open");
    els.modal.setAttribute("aria-hidden", "true");
  }

  function setModalMode(mode) {
    if (!MODE_KEYS.includes(mode)) return;
    state.modalMode = mode;
    els.modalTabs.forEach((btn) => {
      btn.classList.toggle("is-active", btn.dataset.mode === mode);
    });
    const modeSettings = state.settings.modes?.[mode] || {};
    if (els.modalTarget) els.modalTarget.value = modeSettings.kcal_target ?? "";
    if (els.modalProtein) els.modalProtein.value = modeSettings.protein_target ?? "";
    if (els.modalCarbs) els.modalCarbs.value = modeSettings.carbs_target ?? "";
    if (els.modalFat) els.modalFat.value = modeSettings.fat_target ?? "";
    if (els.modalGreenLow) els.modalGreenLow.value = modeSettings.green_low ?? "";
    if (els.modalGreenHigh) els.modalGreenHigh.value = modeSettings.green_high ?? "";
    if (els.modalYellowLow) els.modalYellowLow.value = modeSettings.yellow_low ?? "";
    if (els.modalYellowHigh) els.modalYellowHigh.value = modeSettings.yellow_high ?? "";
    renderBandPreview();
  }

  async function saveSettings() {
    if (isKey) return;
    const v = readModalValues();
    const pre = validateModalValues(v);
    if (!pre.ok) {
      setText(els.modalError, pre.error || "Ungültige Eingaben.");
      renderBandPreview();
      return;
    }
    const kcalTarget = toNumber(els.modalTarget?.value);
    const proteinTarget = toNumber(els.modalProtein?.value);
    const carbsTarget = toNumber(els.modalCarbs?.value);
    const fatTarget = toNumber(els.modalFat?.value);
    const greenLow = toNumber(els.modalGreenLow?.value);
    const greenHigh = toNumber(els.modalGreenHigh?.value);
    const yellowLow = toNumber(els.modalYellowLow?.value);
    const yellowHigh = toNumber(els.modalYellowHigh?.value);
    const payload = {
      active_mode: state.modalMode,
      modes: {
        [state.modalMode]: {
          kcal_target: kcalTarget,
          protein_target: proteinTarget,
          carbs_target: carbsTarget,
          fat_target: fatTarget,
          green_low: greenLow,
          green_high: greenHigh,
          yellow_low: yellowLow,
          yellow_high: yellowHigh
        }
      }
    };

    try {
      const res = await fetch("/api/makros/settings", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload)
      });
      const json = await res.json();
      if (!res.ok || !json || !json.settings) {
        setText(els.modalError, json?.detail || "Speichern fehlgeschlagen.");
        return;
      }
      state.settings = json.settings;
      state.mode = json.settings.active_mode || state.modalMode;
      closeModal();
      renderAll();
    } catch (err) {
      console.warn("Settings save failed", err);
      setText(els.modalError, "Speichern fehlgeschlagen.");
    }
  }

  async function fetchSettings() {
    try {
      const res = await fetch("/api/makros/settings", { cache: "no-store" });
      const json = await res.json();
      if (json && json.settings) {
        state.settings = json.settings;
        const savedMode = json.settings.active_mode;
        if (savedMode && MODE_KEYS.includes(savedMode)) {
          state.mode = savedMode;
        }
      }
    } catch (err) {
      console.warn("Settings fetch failed", err);
    }
  }

  async function fetchSeries() {
    const previousStart = new Date(`${state.rangeStart}T00:00:00`);
    previousStart.setDate(previousStart.getDate() - state.rangeDays);
    const params = new URLSearchParams({
      start: previousStart.toISOString().slice(0, 10),
      end: state.rangeEnd,
    });

    try {
      const res = await fetch(`/api/makros/series?${params.toString()}`, { cache: "no-store" });
      const json = await res.json();
      if (!json || !json.data) throw new Error("Invalid data");
      state.rows = normalizeRows(json.data).sort((a, b) => a._date - b._date);
    } catch (err) {
      console.warn("Makros fetch failed", err);
      state.rows = [];
      setText(els.controlsError, "Fehler beim Laden der Makros.");
    }
  }

  function renderAll() {
    const rows = state.rows;
    const rangeRows = getWindowRows(rows, state.rangeDays);
    const targetKcal = getTargetKcal();

    updateHeader(rangeRows, targetKcal);
    updateStatusCard(rangeRows, targetKcal);

    const insights = buildInsights(rangeRows, targetKcal);
    renderInsights(insights);

    updateMiniCards(rows);
    renderKcalChart(rangeRows, targetKcal);
    renderWeightChart(rangeRows);

    persistState();
  }

  async function loadAll() {
    setText(els.controlsError, "");
    await fetchSettings();
    applyStateToControls();
    await fetchSeries();
    renderAll();
    await loadLoggingStreaks();
    await loadTable();
  }

  function bindControls() {
    if (els.rangeSelect) {
      els.rangeSelect.addEventListener("change", () => {
        state.rangeDays = els.rangeSelect.value === "all" ? "all" : parseInt(els.rangeSelect.value, 10);
        loadAll();
      });
    }

    window.addEventListener("liva:analysis-range-change", (event) => {
      if (event?.detail?.area !== "ernaehrung") return;
      if (!event.detail.start || !event.detail.end) return;
      applyNutritionInterval(event.detail);
      loadAll();
    });

    document.querySelectorAll(".makros-sort-btn").forEach((btn) => {
      btn.addEventListener("click", () => {
        const th = btn.closest("th");
        const key = th ? th.getAttribute("data-sort") : null;
        if (!key) return;
        if (state.sortKey === key) {
          state.sortDir = state.sortDir === "asc" ? "desc" : "asc";
        } else {
          state.sortKey = key;
          state.sortDir = "desc";
        }
        updateSortIndicators();
        sortTableRows();
      });
    });

    if (!isKey && els.exportBtn) {
      els.exportBtn.addEventListener("click", exportCsvFromTable);
    }

    if (isKey) return;

    els.modalTabs.forEach((btn) => {
      btn.addEventListener("click", () => {
        const mode = btn.dataset.mode;
        if (mode) setModalMode(mode);
      });
    });

    if (els.modal) {
      els.modal.addEventListener("click", (event) => {
        const target = event.target;
        if (target && target.dataset && target.dataset.close) {
          closeModal();
        }
      });
    }

    if (els.modalCancel) {
      els.modalCancel.addEventListener("click", closeModal);
    }

    if (els.modalSave) {
      els.modalSave.addEventListener("click", saveSettings);
    }

    // live preview + simple validation
    [els.modalTarget, els.modalGreenLow, els.modalGreenHigh, els.modalYellowLow, els.modalYellowHigh].forEach((inp) => {
      if (!inp) return;
      inp.addEventListener("input", () => {
        setText(els.modalError, "");
        renderBandPreview();
      });
      inp.addEventListener("blur", () => {
        renderBandPreview();
      });
    });
  }

  let initialized = false;

  function init() {
    if (initialized) return;
    initialized = true;
    initChartDefaults();
    readStateFromStorage();
    applyStateToControls();
    bindControls();
    loadAll();

    window.LIVE_CONFIG = {
      interval: 4000,
      onUpdate: async () => {
        await fetchSeries();
        renderAll();
        await loadLoggingStreaks();
        loadTable();
      }
    };
  }

  const evaluationWorkspace = document.getElementById("evaluation-workspace");
  if (!evaluationWorkspace || evaluationWorkspace.dataset.area === "ernaehrung") {
    init();
  } else {
    window.addEventListener("liva:analysis-area-change", (event) => {
      if (event?.detail?.area === "ernaehrung") init();
    });
  }
})();
