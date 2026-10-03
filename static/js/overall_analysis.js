(function () {
  "use strict";

  const root = document.getElementById("overall-analysis");
  const canvas = document.getElementById("overall-analysis-chart");
  const status = document.getElementById("overall-analysis-status");
  const scaleLabel = document.getElementById("overall-analysis-scale");
  const seriesPicker = document.getElementById("overall-analysis-series-picker");
  const seriesCount = document.getElementById("overall-analysis-series-count");
  const readouts = document.getElementById("overall-analysis-readouts");
  const emptyState = document.getElementById("overall-analysis-empty");
  const phaseTitle = document.getElementById("overall-analysis-phase-title");
  const phaseSummary = document.getElementById("overall-analysis-phase-summary");
  const phaseMeta = document.getElementById("overall-analysis-phase-meta");
  const phaseList = document.getElementById("overall-analysis-phase-list");
  if (!root || !canvas) return;

  const SERIES = {
    weight: { label: "Gewicht", unit: "kg", color: "--series-weight", fallback: "#e4e9f2", decimals: 1, smooth: 7 },
    kcal: { label: "Kalorien", unit: "kcal", color: "--series-kcal", fallback: "#e7c46a", decimals: 0, smooth: 3 },
    protein: { label: "Protein", unit: "g", color: "--series-protein", fallback: "#72cfa7", decimals: 0, smooth: 3 },
    carbs: { label: "Carbs", unit: "g", color: "--series-carbs", fallback: "#75a7df", decimals: 0, smooth: 3 },
    fat: { label: "Fett", unit: "g", color: "--series-fat", fallback: "#e59a7a", decimals: 0, smooth: 3 },
    training: { label: "Trainingslast", unit: "Index", color: "--series-training", fallback: "#a99adc", decimals: 1 },
    progression: { label: "Progressionsquote", unit: "%", deltaUnit: "Pp.", color: "--series-progression", fallback: "#afc978", decimals: 0, contextLabel: "letzte 6 Einheiten", fixedRange: [0, 100] },
    hrv: { label: "HRV · Polar", unit: "ms", color: "--series-hrv", fallback: "#62c6cb", decimals: 0, smooth: 3 },
    nightPulse: { label: "Nachtpuls", unit: "bpm", color: "--series-night-pulse", fallback: "#d98aaa", decimals: 0, smooth: 3 },
    sleep: { label: "Schlaf", unit: "h", color: "--series-sleep", fallback: "#8c9fd5", decimals: 1, smooth: 3 },
    calendar: { label: "Belegte Zeit", unit: "h", color: "--series-calendar", fallback: "#c7a477", decimals: 1 },
    flags: { label: "Ereignisse", unit: "", color: "--chart-5", fallback: "#a1a7b3", decimals: 0, context: true },
  };
  const FLAG_TYPES = [
    { flag: "sick", label: "Krankheit", color: "--event-sick", fallback: "#ff6673", pointStyle: "triangle" },
    { flag: "alcohol", label: "Alkohol", color: "--event-alcohol", fallback: "#4da3ff", pointStyle: "rectRot" },
  ];

  const toggles = Array.from(root.querySelectorAll("[data-series]"));
  let chart = null;
  let loadController = null;
  let loadSequence = 0;
  let sourceFailures = 0;
  let hasRenderedOnce = false;
  let seriesRows = Object.fromEntries(Object.keys(SERIES).map((name) => [name, []]));
  const active = new Set(["weight"]);

  function iso(value) { return String(value || "").slice(0, 10); }
  function escapeHtml(value) { return String(value ?? "").replace(/[&<>'"]/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;" }[char])); }
  function number(value) {
    if (value === null || value === undefined || value === "") return null;
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : null;
  }
  function selectedInterval() { return window.LivaEvaluationControls?.getInterval?.("gesamt") || { start: "", end: "" }; }
  function inRange(day, interval) { return day >= interval.start && day <= interval.end; }
  function pointsInRange(name, interval) { return (seriesRows[name] || []).filter((item) => inRange(item.x, interval)); }
  function point(day, value, detail = {}) {
    const parsed = number(value);
    return day && parsed !== null ? { x: iso(day), rawValue: parsed, ...detail } : null;
  }
  function mapped(rows, dayKey, valueKey, transform = (value) => value) {
    return rows.map((row) => point(row[dayKey], transform(row[valueKey]), { source: row })).filter(Boolean).sort((a, b) => a.x.localeCompare(b.x));
  }
  function trainingPoints(payload, key) {
    const scope = payload?.scopes?.overall || Object.values(payload?.scopes || {})[0] || {};
    return (payload?.dates || []).map((day, index) => point(day, scope[key]?.[index])).filter(Boolean);
  }
  function progressionPoints(payload) {
    return (payload?.points || [])
      .map((row) => {
        const trend = number(row.trend);
        return point(row.t_iso || row.session_date, trend === null ? null : trend * 100, { source: row });
      })
      .filter(Boolean)
      .sort((a, b) => a.x.localeCompare(b.x));
  }
  function rootColor(token, fallback) {
    return getComputedStyle(document.documentElement).getPropertyValue(token).trim() || fallback;
  }
  function cssColor(name) {
    return rootColor(SERIES[name].color, SERIES[name].fallback);
  }
  function axisColor() {
    return rootColor("--chart-5", "#a1a7b3");
  }
  function updateStatus(interval) {
    const visible = visibleSeries(interval).length;
    status.textContent = sourceFailures ? `${sourceFailures} Quelle${sourceFailures === 1 ? " fehlt" : "n fehlen"}` : "";
    if (seriesCount) seriesCount.textContent = String(visible);
  }
  async function fetchJson(url, signal) {
    const response = await fetch(url, { cache: "no-store", signal });
    const payload = await response.json();
    if (!response.ok || payload?.ok === false) throw new Error(payload?.error || `HTTP ${response.status}`);
    return payload;
  }

  function hydrateSeries(nutrition, training, progression, recovery, calendar) {
    const nutritionRows = Array.isArray(nutrition?.data) ? nutrition.data : [];
    const recoveryRows = Array.isArray(recovery?.rows) ? recovery.rows : [];
    const polarRecoveryRows = recoveryRows.filter((row) => row.source === "polar");
    const calendarRows = Array.isArray(calendar?.days) ? calendar.days : [];
    seriesRows = {
      weight: mapped(nutritionRows, "date_iso", "bodyweight_kg"),
      kcal: mapped(nutritionRows, "date_iso", "calories"),
      protein: mapped(nutritionRows, "date_iso", "protein_g"),
      carbs: mapped(nutritionRows, "date_iso", "carbs_g"),
      fat: mapped(nutritionRows, "date_iso", "fat_g"),
      training: trainingPoints(training, "load"),
      progression: progressionPoints(progression),
      hrv: mapped(polarRecoveryRows, "date", "rmssd"),
      nightPulse: mapped(polarRecoveryRows, "date", "nightPulse"),
      sleep: mapped(polarRecoveryRows, "date", "actualSleepMinutes", (value) => number(value) === null ? null : Number(value) / 60),
      calendar: mapped(calendarRows, "date", "busy_minutes", (value) => number(value) === null ? null : Number(value) / 60).map((item) => ({ ...item, importantCount: Number(item.source?.important_count || 0) })),
      flags: [...new Map(recoveryRows
        .map((row) => [iso(row.date), row.flag || row.manual_flag])
        .filter(([day, flag]) => day && flag && flag !== "none")).entries()]
        .map(([x, flag]) => ({ x, rawValue: 100, flag }))
        .sort((a, b) => a.x.localeCompare(b.x)),
    };
  }

  async function renderNutritionFirst(nutritionRequest, interval, sequence) {
    if (hasRenderedOnce) return;
    try {
      const nutritionResult = await nutritionRequest;
      if (sequence !== loadSequence) return;
      hydrateSeries(nutritionResult, null, null, null, null);
      syncToggleAvailability(interval);
      render();
      hasRenderedOnce = true;
      status.textContent = "";
    } catch (error) {
      if (error?.name !== "AbortError" && sequence === loadSequence) status.textContent = "Lade weitere Daten …";
    }
  }

  async function load(interval) {
    loadController?.abort();
    loadController = new AbortController();
    const sequence = ++loadSequence;
    status.textContent = "Lade …";
    const params = new URLSearchParams({ start: interval.start, end: interval.end });
    const urls = [
      `/api/makros/series?${params}`,
      `/api/training/response?${params}`,
      `/api/analyse/default_progress?${params}&trend_window_sessions=6`,
      `/api/hrv/recovery?${params}&baseline_days=28`,
      `/api/analysis/calendar_load?${params}`,
      `/api/analysis/phases?${params}&history_days=3650`,
    ];
    const nutritionRequest = fetchJson(urls[0], loadController.signal);
    await renderNutritionFirst(nutritionRequest, interval, sequence);
    const requests = [nutritionRequest, ...urls.slice(1).map((url) => fetchJson(url, loadController.signal))];
    const allResults = Promise.allSettled(requests);
    const results = await allResults;
    if (sequence !== loadSequence) return;
    if (results.every((result) => result.status === "rejected" && result.reason?.name === "AbortError")) return;
    const values = results.map((result) => result.status === "fulfilled" ? result.value : null);
    hydrateSeries(...values);
    renderPhase(values[5], interval);
    syncToggleAvailability(interval);
    render();
    sourceFailures = results.filter((result) => result.status === "rejected" && result.reason?.name !== "AbortError").length;
    updateStatus(interval);
  }

  function compactDate(value) {
    const parsed = new Date(`${value}T00:00:00Z`);
    return Number.isNaN(parsed.getTime()) ? value : parsed.toLocaleDateString("de-DE", { day: "2-digit", month: "2-digit", year: "2-digit", timeZone: "UTC" });
  }

  function targetLabel(min, max) {
    if (min === null || min === undefined) return "Kein belastbares historisches Kalorienziel";
    if (Number(min) === Number(max)) return `${Number(min).toLocaleString("de-DE")} kcal historisches Ziel`;
    return `${Number(min).toLocaleString("de-DE")}–${Number(max).toLocaleString("de-DE")} kcal historische Ziele`;
  }

  function renderPhase(payload, interval) {
    if (!phaseTitle || !phaseSummary || !phaseMeta || !phaseList) return;
    const selected = payload?.selected;
    if (!selected) {
      phaseTitle.textContent = "Freier Zeitraum";
      phaseSummary.textContent = "Zielkontext fehlt.";
      phaseMeta.innerHTML = ""; phaseList.innerHTML = ""; return;
    }
    phaseTitle.textContent = selected.label || "Der gewählte Zeitraum";
    phaseSummary.textContent = targetLabel(selected.target_min, selected.target_max);
    const mode = selected.modes?.length === 1 ? selected.modes[0].replace("lean_bulk", "Aufbau").replace("maintenance", "Erhalt").replace("cut", "Cut") : (selected.modes?.length ? "Mehrere Zielphasen" : "Kein Modus");
    phaseMeta.innerHTML = `<span>${escapeHtml(mode)}</span>${selected.context_ambiguous ? "<span class=\"is-ambiguous\">Zielquellen weichen ab</span>" : ""}`;
    phaseList.innerHTML = (payload.suggestions || []).map((phase) => `<button type="button" data-phase-start="${escapeHtml(phase.start)}" data-phase-end="${escapeHtml(phase.end)}"><strong>${escapeHtml(phase.label)}</strong><span>${compactDate(phase.start)} – ${compactDate(phase.end)}</span></button>`).join("") || "<span class=\"overall-analysis__phase-empty\">Keine Abschnitte.</span>";
  }

  function syncToggleAvailability(interval) {
    toggles.forEach((toggle) => {
      const name = toggle.dataset.series;
      const available = pointsInRange(name, interval).length > 0;
      toggle.disabled = !available;
      toggle.classList.toggle("is-empty", !available);
      toggle.title = available ? `${SERIES[name].label} ein- oder ausblenden` : "Keine Daten im gewählten Zeitraum";
      if (!available) active.delete(name);
      toggle.classList.toggle("is-active", active.has(name));
      toggle.setAttribute("aria-pressed", active.has(name) ? "true" : "false");
    });
  }

  function trendPoints(name, interval) {
    const values = pointsInRange(name, interval);
    const windowSize = SERIES[name].smooth || 1;
    if (windowSize === 1 || SERIES[name].context) return values.map((item) => ({ ...item, plotValue: item.rawValue }));
    return values.map((item, index) => {
      const windowValues = values.slice(Math.max(0, index - windowSize + 1), index + 1).map((entry) => entry.rawValue);
      return { ...item, plotValue: windowValues.reduce((sum, value) => sum + value, 0) / windowValues.length };
    });
  }

  function plottedPoints(name, interval, compareMode) {
    const values = trendPoints(name, interval);
    if (!compareMode) return values.map((item) => ({ ...item, y: item.plotValue }));
    const plotted = values.map((item) => item.plotValue);
    const min = Math.min(...plotted); const max = Math.max(...plotted);
    return values.map((item) => ({ ...item, y: max === min ? 50 : 12 + ((item.plotValue - min) / (max - min)) * 76 }));
  }

  function flagDatasets(interval) {
    const points = pointsInRange("flags", interval);
    return FLAG_TYPES.map((flagMeta) => {
      const color = rootColor(flagMeta.color, flagMeta.fallback);
      return {
        label: flagMeta.label,
        data: points.filter((item) => item.flag === flagMeta.flag).map((item) => ({ ...item, y: 6 })),
        borderColor: color,
        backgroundColor: color,
        yAxisID: "trend",
        showLine: false,
        pointStyle: flagMeta.pointStyle,
        pointRadius: 5,
        pointHoverRadius: 7,
        metaKey: "flags",
      };
    }).filter((dataset) => dataset.data.length);
  }

  function visibleSeries(interval) {
    return Object.keys(SERIES).filter((name) => active.has(name) && pointsInRange(name, interval).length);
  }

  function datasets(interval, names, compareMode) {
    return names.flatMap((name) => {
      if (name === "flags") return flagDatasets(interval);
      const meta = SERIES[name]; const color = cssColor(name);
      return [{
        label: meta.label, data: plottedPoints(name, interval, compareMode), borderColor: color, backgroundColor: color,
        borderDash: [], yAxisID: "trend", tension: .18, spanGaps: false, showLine: true,
        pointStyle: "circle", pointRadius: 0, pointHoverRadius: 4,
        borderWidth: name === "weight" ? 2.8 : 2.2, metaKey: name,
      }];
    });
  }

  function axisOptions(interval, names, compareMode) {
    if (names.length === 1 && names[0] === "flags") {
      if (scaleLabel) scaleLabel.textContent = "Ereignisse";
      return { axis: "y", display: false, min: 0, max: 100, grid: { display: false }, border: { display: false } };
    }
    if (compareMode) {
      if (scaleLabel) scaleLabel.textContent = "Vergleich · je eigene Skala";
      return {
        axis: "y", display: true, min: 0, max: 100,
        grid: { color: axisColor() + "20", drawTicks: false },
        border: { display: false },
        ticks: {
          color: axisColor(), count: 3, padding: 7, font: { size: 9 },
          callback(value) { return Number(value) < 30 ? "tief" : Number(value) > 70 ? "hoch" : "mittel"; },
        },
      };
    }
    const name = names[0]; const meta = SERIES[name]; const values = trendPoints(name, interval).map((item) => item.plotValue);
    const min = Math.min(...values); const max = Math.max(...values); const spread = max - min;
    const padding = spread > 0 ? spread * .16 : Math.max(Math.abs(max) * .025, 1);
    if (scaleLabel) scaleLabel.textContent = `${meta.label} · ${meta.unit || "Ereignisse"}${meta.smooth ? ` · ${meta.smooth}T Trend` : ""}${meta.contextLabel ? ` · ${meta.contextLabel}` : ""}`;
    return {
      axis: "y", display: true, min: meta.fixedRange?.[0] ?? min - padding, max: meta.fixedRange?.[1] ?? max + padding,
      grid: { color: axisColor() + "20", drawTicks: false },
      border: { display: false },
      ticks: {
        color: axisColor(), maxTicksLimit: 4, padding: 7, font: { size: 9 },
        callback(value) { return Number(value).toLocaleString("de-DE", { maximumFractionDigits: meta.decimals }); },
      },
    };
  }

  function formatValue(name, value) {
    const meta = SERIES[name];
    if (name === "flags") return value === "sick" ? "Krankheit" : "Alkohol";
    return `${Number(value).toLocaleString("de-DE", { minimumFractionDigits: meta.decimals, maximumFractionDigits: meta.decimals })}${meta.unit ? ` ${meta.unit}` : ""}`;
  }

  function formatDelta(name, value) {
    const meta = SERIES[name];
    if (!meta.deltaUnit) return formatValue(name, value);
    return `${Number(value).toLocaleString("de-DE", { minimumFractionDigits: meta.decimals, maximumFractionDigits: meta.decimals })} ${meta.deltaUnit}`;
  }

  function renderReadouts(interval) {
    if (!readouts) return;
    readouts.innerHTML = Object.keys(SERIES).filter((name) => active.has(name) && name !== "flags").map((name) => {
      const values = pointsInRange(name, interval);
      if (!values.length) return "";
      const first = values[0]; const last = values[values.length - 1]; const delta = last.rawValue - first.rawValue;
      const deltaPrefix = delta > 0 ? "+" : "";
      return `<div style="--series-color:${cssColor(name)}"><span>${SERIES[name].label}</span><strong>${formatValue(name, last.rawValue)}</strong><small>${deltaPrefix}${formatDelta(name, delta)}</small></div>`;
    }).join("");
  }

  function render() {
    if (!window.Chart) return;
    const interval = selectedInterval(); const names = visibleSeries(interval); const compareMode = names.length !== 1 || SERIES[names[0]]?.context;
    const chartDatasets = datasets(interval, names, compareMode);
    emptyState.hidden = chartDatasets.length > 0; canvas.hidden = chartDatasets.length === 0;
    chart?.destroy(); chart = null; renderReadouts(interval);
    if (!chartDatasets.length) { if (scaleLabel) scaleLabel.textContent = ""; return; }
    const textMain = getComputedStyle(document.documentElement).getPropertyValue("--foreground").trim() || "#e7e9ee";
    const textMuted = getComputedStyle(document.documentElement).getPropertyValue("--muted-foreground").trim() || "#a1a7b3";
    chart = new window.Chart(canvas, {
      type: "line", data: { datasets: chartDatasets },
      options: {
        responsive: true, maintainAspectRatio: false, interaction: { mode: "nearest", intersect: false, axis: "x" },
        animation: window.matchMedia("(prefers-reduced-motion: reduce)").matches ? false : { duration: 260 },
        plugins: {
          legend: { display: false },
          tooltip: {
            backgroundColor: "rgba(14, 16, 19, .96)", titleColor: textMain, bodyColor: textMuted, padding: 10,
            callbacks: { label(context) {
              const name = context.dataset.metaKey; const item = context.raw;
              if (name === "flags") return context.dataset.label;
              let label = `${SERIES[name].label}: ${formatValue(name, name === "flags" ? item.flag : item.rawValue)}`;
              if (name !== "flags" && SERIES[name].smooth && Math.abs(item.plotValue - item.rawValue) > .001) label += ` · Trend ${formatValue(name, item.plotValue)}`;
              if (name === "progression") {
                const compared = Number(item.source?.comparable_sets || item.source?.comparable || 0);
                const improved = Number(item.source?.progress_sets || item.source?.improved || 0);
                const stable = Number(item.source?.stable_sets || item.source?.same || 0);
                const worse = Number(item.source?.regress_sets || item.source?.worse || 0);
                const variationChanges = Number(item.source?.exclusion_reasons?.different_variation || 0);
                const lines = [label];
                if (compared) lines.push(`Tag: ${improved}/${compared} verbessert · ${stable} stabil · ${worse} rückläufig`);
                if (variationChanges) lines.push(`${variationChanges} Variantenwechsel · nicht gewertet`);
                return lines;
              }
              if (name === "calendar" && item.importantCount) label += ` · ${item.importantCount} Klausur/Prüfung`;
              return label;
            } },
          },
        },
        scales: {
          x: {
            type: "time", time: { unit: "day", tooltipFormat: "dd.MM.yyyy" },
            grid: { display: false }, border: { color: axisColor() + "40" },
            ticks: { color: axisColor(), maxRotation: 0, maxTicksLimit: window.matchMedia("(max-width: 720px)").matches ? 4 : 7, font: { size: 9 } },
          },
          trend: axisOptions(interval, names, compareMode),
        },
      },
    });
  }

  toggles.forEach((toggle) => toggle.addEventListener("click", () => {
    const name = toggle.dataset.series; if (toggle.disabled) return;
    if (active.has(name)) active.delete(name); else active.add(name);
    toggle.classList.toggle("is-active", active.has(name)); toggle.setAttribute("aria-pressed", active.has(name) ? "true" : "false"); render(); updateStatus(selectedInterval());
  }));
  phaseList?.addEventListener("click", (event) => {
    const button = event.target.closest("[data-phase-start][data-phase-end]");
    if (!button) return;
    window.LivaEvaluationControls?.setInterval?.("gesamt", { start: button.dataset.phaseStart, end: button.dataset.phaseEnd });
  });

  function shiftInterval(days) {
    const interval = selectedInterval(); const start = new Date(`${interval.start}T00:00:00Z`); const end = new Date(`${interval.end}T00:00:00Z`);
    start.setUTCDate(start.getUTCDate() + days); end.setUTCDate(end.getUTCDate() + days);
    window.LivaEvaluationControls?.setInterval?.("gesamt", { start: iso(start.toISOString()), end: iso(end.toISOString()) });
  }
  function zoomInterval(multiplier) {
    const interval = selectedInterval(); const start = new Date(`${interval.start}T00:00:00Z`); const end = new Date(`${interval.end}T00:00:00Z`);
    const currentDays = Math.max(7, Math.round((end - start) / 86400000) + 1); const nextDays = Math.max(7, Math.min(730, Math.round(currentDays * multiplier)));
    const center = new Date((start.getTime() + end.getTime()) / 2); const nextStart = new Date(center.getTime() - Math.floor(nextDays / 2) * 86400000);
    const nextEnd = new Date(nextStart.getTime() + (nextDays - 1) * 86400000);
    window.LivaEvaluationControls?.setInterval?.("gesamt", { start: iso(nextStart.toISOString()), end: iso(nextEnd.toISOString()) });
  }
  let dragStartX = null;
  canvas.addEventListener("wheel", (event) => { event.preventDefault(); zoomInterval(event.deltaY > 0 ? 1.28 : .78); }, { passive: false });
  canvas.addEventListener("pointerdown", (event) => { dragStartX = event.clientX; canvas.setPointerCapture?.(event.pointerId); });
  canvas.addEventListener("pointerup", (event) => {
    if (dragStartX === null) return;
    const interval = selectedInterval(); const days = Math.max(1, Math.round((new Date(`${interval.end}T00:00:00Z`) - new Date(`${interval.start}T00:00:00Z`)) / 86400000) + 1);
    const distance = event.clientX - dragStartX; dragStartX = null; if (Math.abs(distance) < 8) return;
    shiftInterval(-Math.round(distance / Math.max(canvas.clientWidth, 1) * days));
  });

  window.addEventListener("liva:analysis-range-change", (event) => { if (event?.detail?.area === "gesamt") load(event.detail); });
  const compactPicker = window.matchMedia("(max-width: 720px)");
  function syncPicker() {
    if (!seriesPicker) return;
    if (compactPicker.matches) seriesPicker.removeAttribute("open"); else seriesPicker.setAttribute("open", "");
  }
  compactPicker.addEventListener?.("change", syncPicker);
  syncPicker();
  load(selectedInterval());
})();
