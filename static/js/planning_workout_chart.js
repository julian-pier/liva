(function (global) {
  "use strict";
  const instances = new WeakMap();
  const finite = (value) => value === null || value === undefined || value === "" ? null : (Number.isFinite(Number(value)) ? Number(value) : null);

  function formatPace(value) {
    const seconds = finite(value);
    if (!seconds || seconds <= 0) return "—";
    const rounded = Math.round(seconds);
    return `${Math.floor(rounded / 60)}:${String(rounded % 60).padStart(2, "0")} /km`;
  }
  function formatDuration(value) {
    const seconds = Math.max(0, Math.round(finite(value) || 0));
    const hours = Math.floor(seconds / 3600), minutes = Math.floor((seconds % 3600) / 60), rest = seconds % 60;
    return hours ? `${hours}:${String(minutes).padStart(2, "0")}:${String(rest).padStart(2, "0")}` : `${minutes}:${String(rest).padStart(2, "0")}`;
  }
  function kindLabel(kind) {
    return ({ open: "Locker", warmup: "Einlaufen", work: "Belastung", stride: "Steigerung", recovery: "Pause", cooldown: "Auslaufen" })[kind] || kind || "Abschnitt";
  }
  function targetFor(step, metric, maxHr) {
    const target = step.target || {}, resolved = step.resolved || {};
    if (metric === "pace") {
      const source = resolved.pace_s_per_km || resolved.pace_min_s_per_km ? resolved : target;
      const center = finite(source.pace_s_per_km), fast = finite(source.pace_min_s_per_km) || center, slow = finite(source.pace_max_s_per_km) || center;
      return fast && slow ? { value: center || (fast + slow) / 2, low: fast, high: slow } : null;
    }
    if (metric === "hr") {
      const bpm = finite(target.hr_bpm);
      if (bpm) return { value: bpm, low: bpm, high: bpm };
      const maximum = finite(maxHr) || finite(resolved.estimated_max_hr_bpm);
      const lowPct = finite(target.hr_min_pct) ?? finite(resolved.internal_hr_min_pct);
      const highPct = finite(target.hr_max_pct) ?? finite(resolved.internal_hr_max_pct) ?? lowPct;
      if (!maximum || lowPct === null) return null;
      const low = Math.round(maximum * lowPct / 100), high = Math.round(maximum * highPct / 100);
      return { value: (low + high) / 2, low, high };
    }
    const low = finite(target.rpe_min) ?? (target.metric === "rpe" ? finite(target.min) : finite(target.rpe));
    const high = finite(target.rpe_max) ?? (target.metric === "rpe" ? finite(target.max) : low);
    return low === null ? null : { value: (low + (high ?? low)) / 2, low, high: high ?? low };
  }
  function buildProfile(steps, metric) {
    const rows = Array.isArray(steps) ? steps : [];
    const maxHr = rows.reduce((value, step) => Math.max(value, finite(step?.resolved?.estimated_max_hr_bpm) || 0), 0);
    let cursor = 0;
    const sections = rows.map((step, index) => {
      const pace = targetFor(step, "pace", maxHr);
      const seconds = finite(step.duration_s) || ((finite(step.distance_m) || 0) * (pace?.value || 360) / 1000) || 1;
      const start = cursor; cursor += seconds;
      return { index, start, end: cursor, seconds, step, target: targetFor(step, metric, maxHr) };
    });
    const values = sections.flatMap((section) => section.target ? [section.target.low, section.target.high] : []);
    if (!values.length) return { sections, points: [], low: null, high: null, total: cursor };
    let low = Math.min(...values), high = Math.max(...values);
    const minSpan = metric === "pace" ? 30 : metric === "hr" ? 20 : 2;
    const pad = Math.max(metric === "pace" ? 12 : metric === "hr" ? 5 : .5, (high - low) * .16), middle = (low + high) / 2;
    low = Math.min(low - pad, middle - minSpan / 2); high = Math.max(high + pad, middle + minSpan / 2);
    if (metric === "pace") { low = Math.max(150, low); high = Math.min(900, high); }
    if (metric === "hr") { low = Math.max(50, low); high = Math.min(maxHr || 230, high); }
    if (metric === "rpe") { low = 1; high = 10; }
    const points = [];
    sections.forEach((section) => {
      if (!section.target) return;
      const base = { sectionIndex: section.index, step: section.step, low: section.target.low, high: section.target.high };
      points.push({ x: section.start, y: section.target.value, ...base }, { x: section.end, y: section.target.value, ...base });
    });
    return { sections, points, low, high, total: cursor };
  }

  const workoutOverlay = {
    id: "livaWorkoutOverlay",
    beforeDatasetsDraw(chart) {
      const profile = chart.options.plugins.livaWorkoutOverlay?.profile;
      if (!profile) return;
      const { ctx, chartArea, scales } = chart; ctx.save();
      profile.sections.forEach((section) => {
        const x0 = scales.x.getPixelForValue(section.start), x1 = scales.x.getPixelForValue(section.end);
        const color = ({ warmup: "93,209,183", cooldown: "93,209,183", open: "93,209,183", recovery: "126,142,156", stride: "169,133,255" })[section.step.kind] || "104,174,245";
        ctx.fillStyle = `rgba(${color},.055)`; ctx.fillRect(x0, chartArea.top, Math.max(1, x1 - x0), chartArea.bottom - chartArea.top);
        if (section.index) { ctx.strokeStyle = "rgba(139,157,173,.20)"; ctx.lineWidth = 1; ctx.beginPath(); ctx.moveTo(x0, chartArea.top); ctx.lineTo(x0, chartArea.bottom); ctx.stroke(); }
      });
      ctx.restore();
    },
    afterDraw(chart) {
      const active = chart.tooltip?.getActiveElements?.()[0]; if (!active) return;
      const { ctx, chartArea } = chart; ctx.save(); ctx.strokeStyle = "rgba(224,238,249,.58)"; ctx.lineWidth = 1;
      ctx.beginPath(); ctx.moveTo(active.element.x, chartArea.top); ctx.lineTo(active.element.x, chartArea.bottom); ctx.stroke(); ctx.restore();
    }
  };

  function mount(canvas, steps, metric) {
    if (!canvas || typeof global.Chart === "undefined") return null;
    instances.get(canvas)?.destroy();
    const profile = buildProfile(steps, metric); if (!profile.points.length) return null;
    const isPace = metric === "pace", isHr = metric === "hr";
    const accent = isPace ? "#72b7ff" : isHr ? "#ff8f87" : "#b9a1ff";
    const band = isPace ? "rgba(93,166,231,.17)" : isHr ? "rgba(232,121,114,.15)" : "rgba(169,133,255,.15)";
    const lower = profile.points.map((point) => ({ ...point, y: point.low }));
    const upper = profile.points.map((point) => ({ ...point, y: point.high }));
    const chart = new global.Chart(canvas, {
      type: "line", plugins: [workoutOverlay],
      data: { datasets: [
        { data: lower, borderWidth: 0, pointRadius: 0, stepped: "after", fill: false },
        { data: upper, borderWidth: 0, pointRadius: 0, stepped: "after", fill: "-1", backgroundColor: band },
        { label: metric, data: profile.points, borderColor: accent, borderWidth: 2, pointRadius: 0, pointHoverRadius: 3, pointHoverBackgroundColor: "#eef7ff", tension: 0, stepped: "after", spanGaps: false }
      ] },
      options: {
        animation: { duration: 180 }, responsive: true, maintainAspectRatio: false, normalized: true, parsing: false,
        interaction: { mode: "nearest", axis: "x", intersect: false }, layout: { padding: { top: 4, right: 4 } },
        scales: {
          x: { type: "linear", min: 0, max: profile.total, border: { display: false }, grid: { color: "rgba(139,157,173,.10)", drawTicks: false }, ticks: { color: "#71808d", maxTicksLimit: global.innerWidth < 700 ? 4 : 6, padding: 7, callback: (value) => formatDuration(value) } },
          y: { min: profile.low, max: profile.high, reverse: isPace, afterFit(scale) { scale.width = global.innerWidth < 700 ? 49 : 58; }, border: { display: false }, grid: { color: "rgba(139,157,173,.12)", drawTicks: false }, ticks: { color: "#71808d", maxTicksLimit: global.innerWidth < 700 ? 4 : 5, padding: 7, callback: (value) => isPace ? formatPace(value).replace(" /km", "") : isHr ? `${Math.round(value)}` : `${Math.round(value * 10) / 10}` } }
        },
        plugins: {
          legend: { display: false }, livaWorkoutOverlay: { profile },
          tooltip: {
            backgroundColor: "rgba(19,27,35,.97)", borderColor: "rgba(121,146,168,.42)", borderWidth: 1, titleColor: "#edf5fb", bodyColor: "#aebdca", padding: 10, displayColors: false,
            filter: (item) => item.datasetIndex === 2,
            callbacks: {
              title(items) { const point = items[0]?.raw; return point ? `${kindLabel(point.step.kind)} · ${formatDuration(point.x)}` : ""; },
              label(item) { const point = item.raw, value = isPace ? formatPace(point.y) : isHr ? `${Math.round(point.y)} bpm` : `RPE ${Math.round(point.y * 10) / 10}`; return `${isPace ? "Pace" : isHr ? "Puls" : "Belastung"}: ${value}`; },
              afterLabel(item) { const point = item.raw; if (!point || point.low === point.high) return ""; return `Zielbereich: ${isPace ? `${formatPace(point.low)}–${formatPace(point.high)}` : `${Math.round(point.low)}–${Math.round(point.high)}${isHr ? " bpm" : ""}`}`; }
            }
          }
        }
      }
    });
    instances.set(canvas, chart); return chart;
  }
  const api = { buildProfile, formatDuration, formatPace, mount, targetFor };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  global.LivaWorkoutCharts = api;
})(typeof window !== "undefined" ? window : globalThis);
