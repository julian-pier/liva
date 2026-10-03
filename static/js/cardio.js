(function () {
  "use strict";

  const TZ = "Europe/Berlin";
  const state = { days: "all", sport: "all", view: "minutes", sortKey: "started_at", sortDir: "desc", rows: [], charts: {} };
  const $ = (id) => document.getElementById(id);
  const els = {
    app: $("cardio-app"), range: $("cardio-range"), sport: $("cardio-sport"), view: $("cardio-data-mode"),
    error: $("cardio-error"), primaryLabel: $("cardio-primary-label"), primary: $("cardio-load-total"),
    perWeek: $("cardio-load-week"), compare: $("cardio-load-compare"), activeTotal: $("cardio-active-total"),
    avgDuration: $("cardio-avg-duration"), longest: $("cardio-longest"), sessionCount: $("cardio-session-count"),
    sessionsWeek: $("cardio-sessions-week"), activeDays: $("cardio-active-days"), z2Total: $("cardio-z2-total"),
    z2Share: $("cardio-z2-share"), intensityBars: $("cardio-intensity-bars"), zoneList: $("cardio-intensity-list"),
    lastTitle: $("cardio-last-title"), lastMeta: $("cardio-last-meta"), lastDetails: $("cardio-last-details"),
    weeklyChart: $("cardio-weekly-chart"), performanceChart: $("cardio-performance-chart"), performanceNote: $("cardio-performance-note"),
    performanceTabs: $("cardio-performance-tabs"), performanceFacts: $("cardio-performance-facts"), sportSummary: $("cardio-sport-summary"),
    tableMeta: $("cardio-table-meta"), tableHead: $("cardio-table-head"), tbody: $("cardio-tbody"), add: $("cardio-add"), sync: $("cardio-sync"),
  };
  if (!els.app) return;

  const fmtDate = new Intl.DateTimeFormat("de-DE", { timeZone: TZ, weekday: "short", day: "2-digit", month: "2-digit", year: "2-digit" });
  const colors = { run: "#5f8fca", ergo: "#6aaa8b", stair: "#d08a55", other: "#9ca3af", hr: "#c76f61", Z1: "#8aa1b6", Z2: "#63a985", Z3: "#c4a457", Z4: "#c8894e", Z5: "#bd665f" };
  const has = (v) => v !== null && v !== undefined && v !== "" && Number.isFinite(Number(v));
  const esc = (v) => String(v ?? "").replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;").replaceAll('"', "&quot;");
  const setText = (el, v) => { if (el) el.textContent = v; };
  const setHtml = (el, v) => { if (el) el.innerHTML = v; };
  const minutes = (v) => has(v) ? `${Math.round(Number(v))} min` : "—";
  const bpm = (v) => has(v) ? `${Math.round(Number(v))} bpm` : "—";
  const watt = (v) => has(v) ? `${Math.round(Number(v))} W` : "—";
  const km = (v) => has(v) ? Number(v).toFixed(2) : "";
  const floors = (v) => has(v) ? `${Math.round(Number(v))}` : "";
  const pct = (v) => has(v) ? `${Math.round(Number(v) * 100)}%` : "—";
  const dt = (v) => { const d = new Date(v); return Number.isNaN(d.getTime()) ? null : d; };
  const dateText = (v) => { const d = dt(v); return d ? fmtDate.format(d) : "—"; };
  const displayDate = (v) => {
    const d = dt(v);
    if (!d) return "";
    return new Intl.DateTimeFormat("de-DE", { timeZone: TZ, weekday: "short", day: "2-digit", month: "2-digit", year: "2-digit" }).format(d).replace(",", "");
  };
  const editDate = (v) => {
    const d = dt(v);
    if (!d) return "";
    return new Intl.DateTimeFormat("de-DE", { timeZone: TZ, day: "2-digit", month: "2-digit", year: "2-digit" }).format(d);
  };
  function pace(sec) {
    if (!has(sec) || Number(sec) <= 0) return "—";
    const s = Math.round(Number(sec));
    return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}/km`;
  }
  function duration(sec) { return has(sec) ? minutes(Number(sec) / 60) : "—"; }
  function tableDuration(sec) {
    if (!has(sec) || Number(sec) <= 0) return "";
    const total = Math.round(Number(sec));
    const h = Math.floor(total / 3600);
    const m = Math.floor((total % 3600) / 60);
    const s = total % 60;
    return h > 0
      ? `${String(h).padStart(2, "0")}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`
      : `${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
  }
  function parseDurationInput(value) {
    const text = String(value || "").trim();
    if (!text) return "";
    if (text.includes(":")) {
      const parts = text.split(":").map((p) => Number(p.replace(",", ".")));
      if (parts.some((p) => !Number.isFinite(p) || p < 0)) return text;
      if (parts.length === 2) return (parts[0] * 60 + parts[1]) / 60;
      if (parts.length === 3) return (parts[0] * 3600 + parts[1] * 60 + parts[2]) / 60;
      return text;
    }
    const n = Number(text.replace(",", "."));
    return Number.isFinite(n) ? n : text;
  }
  async function api(url, options = {}) {
    const res = await fetch(url, { ...options, headers: { Accept: "application/json", "Content-Type": "application/json", ...(options.headers || {}) } });
    const data = await res.json().catch(() => ({}));
    if (!res.ok || data.ok === false || data.success === false) {
      const message = typeof data.error === "object" ? data.error?.message : data.error;
      const error = new Error(message || data.message || `HTTP ${res.status}`);
      error.code = typeof data.error === "object" ? data.error?.code : null;
      throw error;
    }
    return data;
  }
  function query() {
    const sp = new URLSearchParams({ days: state.days, sport: state.sport, primary_metric: "minutes", limit: "250", offset: "0" });
    if (state.view === "hr") sp.set("data_mode", "hr");
    return sp.toString();
  }
  function chartOptions(extra = {}) {
    return { responsive: true, maintainAspectRatio: false, interaction: { mode: "index", intersect: false }, plugins: { legend: { labels: { color: "#9ca3af", boxWidth: 10, usePointStyle: true } } }, scales: { x: { stacked: true, ticks: { color: "#9ca3af", maxRotation: 0 }, grid: { color: "transparent" } }, y: { stacked: true, ticks: { color: "#9ca3af" }, grid: { color: "rgba(148,163,184,.14)" } } }, ...extra };
  }
  function shortDate(value) {
    const d = dt(value);
    return d ? new Intl.DateTimeFormat("de-DE", { timeZone: TZ, day: "2-digit", month: "2-digit" }).format(d) : "—";
  }
  function sportSplit(s) {
    const run = Number(s.run_minutes || 0);
    const ergo = Number(s.ergo_minutes || 0);
    const total = run + ergo;
    if (!total) return "—";
    return `${Math.round((run / total) * 100)} / ${Math.round((ergo / total) * 100)}`;
  }
  function sessionSplit(s) {
    const run = Number(s.run_count || 0);
    const ergo = Number(s.ergo_count || 0);
    return `${run} Laufen / ${ergo} Ergo`;
  }
  function sportLabel(sport) {
    if (sport === "ergo") return "Ergo";
    if (sport === "stair") return "Stair Master";
    return "Laufen";
  }
  function mainLabel(sport) {
    if (sport === "ergo" || sport === "stair") return "Ø Watt";
    return "Pace";
  }
  function lastHeadline(session) {
    if (!session) return "—";
    return `${session.sport_label || "Cardio"} ${duration(session.duration_sec)}`;
  }
  function lastSubline(session) {
    if (!session) return "—";
    return `${bpm(session.avg_hr_bpm)} · ${zoneText(session) || "Zone —"} · ${shortDate(session.started_at)}`;
  }

  function renderSummary(payload) {
    const s = payload.summary || {};
    const totalMinutes = Number(s.total_active_minutes || 0);
    const z2Minutes = Number(s.z2_total_minutes || 0);
    const z2Share = totalMinutes > 0 ? z2Minutes / totalMinutes : null;
    const cards = [
      ["Aktive Minuten", minutes(totalMinutes), `${minutes(s.minutes_per_week)} / Woche`],
      ["Z2-Anteil", pct(z2Share), `${minutes(z2Minutes)} von ${minutes(totalMinutes)}`],
      ["Laufen / Ergo", sportSplit(s), "Laufen / Ergo"],
      ["Sessions", String(s.total_sessions ?? s.sessions ?? 0), sessionSplit(s)],
      ["Letzte Einheit", lastHeadline(s.last_session), lastSubline(s.last_session)],
    ];
    cards.forEach((c, i) => {
      const item = document.querySelectorAll(".cardio-ribbon-item")[i];
      if (!item) return;
      const label = item.querySelector(".cardio-kpi-label");
      const strong = item.querySelector("strong");
      const small = item.querySelector("small");
      if (label) label.textContent = c[0];
      if (strong) strong.textContent = c[1];
      if (small) small.textContent = c[2];
    });
    setHtml(els.lastDetails, "");
    renderZones(payload.hr_zone_distribution || payload.zones || []);
  }
  function renderZones(rows) {
    const total = rows.reduce((a, r) => a + Number(r.active_minutes || 0), 0);
    setHtml(els.intensityBars, rows.map((r) => `<span style="width:${total ? (Number(r.active_minutes || 0) / total) * 100 : 0}%;background:${colors[r.hr_zone] || colors.other}"></span>`).join(""));
    setHtml(els.zoneList, rows.map((r) => `<div class="cardio-zone-row"><span><i style="background:${colors[r.hr_zone] || colors.other}"></i>${esc(r.hr_zone)}</span><strong>${minutes(r.active_minutes)} <em>${pct(r.share)}</em></strong></div>`).join(""));
  }
  function renderLast(s) {
    if (!s) { setText(els.lastTitle, "Keine Einheit"); setText(els.lastMeta, "—"); setHtml(els.lastDetails, ""); return; }
    setText(els.lastTitle, s.sport_label || "Cardio");
    setText(els.lastMeta, dateText(s.started_at));
    const main = s.sport_type === "run" ? pace(s.avg_pace_sec_per_km) : s.sport_type === "ergo" || s.sport_type === "stair" ? watt(s.avg_power_w) : minutes(s.active_minutes);
    setHtml(els.lastDetails, [["Dauer", duration(s.duration_sec)], [mainLabel(s.sport_type), main], ["Ø Puls", bpm(s.avg_hr_bpm)], ["Zone", s.hr_zone || "—"]].map(([k, v]) => `<span><b>${k}</b>${esc(v)}</span>`).join(""));
  }
  function renderWeekly(rows) {
    if (!els.weeklyChart || typeof Chart === "undefined") return;
    if (state.charts.weekly) state.charts.weekly.destroy();
    state.charts.weekly = new Chart(els.weeklyChart, { type: "bar", data: { labels: rows.map((r) => r.week), datasets: [
      { label: "Laufen", data: rows.map((r) => Number(r.run_minutes || 0)), backgroundColor: `${colors.run}aa`, borderRadius: 3 },
      { label: "Ergo", data: rows.map((r) => Number(r.ergo_minutes || 0)), backgroundColor: `${colors.ergo}aa`, borderRadius: 3 },
      { label: "Alt/sonstiges", data: rows.map((r) => Number(r.other_minutes || 0)), backgroundColor: `${colors.other}55`, borderRadius: 3, hidden: true },
    ] }, options: chartOptions() });
  }
  function renderPerformance(perf) {
    if (!els.performanceChart || typeof Chart === "undefined") return;
    if (state.charts.performance) state.charts.performance.destroy();
    const series = perf?.series || {};
    const active = state.sport === "ergo" ? "ergo" : state.sport === "stair" ? "stair" : "run";
    const rows = state.sport === "all" ? (series.run || []) : (series[active] || []);
    setHtml(els.performanceTabs, state.sport === "all" ? "" : "");
    const labels = rows.map((r) => { const d = dt(r.started_at); return d ? new Intl.DateTimeFormat("de-DE", { timeZone: TZ, day: "2-digit", month: "2-digit" }).format(d) : ""; });
    let datasets = [];
    if (active === "run") {
      setText(els.performanceNote, state.sport === "all" ? "Default: Laufentwicklung, damit Pace und Watt nicht gemischt werden." : "Laufen: Pace und Ø Puls über Zeit.");
      datasets = [{ label: "Pace s/km", data: rows.map((r) => has(r.pace_sec_per_km) ? Number(r.pace_sec_per_km) : null), borderColor: colors.run, tension: .25, yAxisID: "y" }, { label: "Ø Puls", data: rows.map((r) => has(r.avg_hr) ? Number(r.avg_hr) : null), borderColor: colors.hr, tension: .25, yAxisID: "y1" }];
    } else if (active === "stair") {
      setText(els.performanceNote, "Stair Master: Ø Watt und Ø Puls über Zeit.");
      datasets = [{ label: "Ø Watt", data: rows.map((r) => has(r.avg_power) ? Number(r.avg_power) : null), borderColor: colors.stair, tension: .25, yAxisID: "y" }, { label: "Ø Puls", data: rows.map((r) => has(r.avg_hr) ? Number(r.avg_hr) : null), borderColor: colors.hr, tension: .25, yAxisID: "y1" }];
    } else {
      setText(els.performanceNote, "Ergo: Ø Watt und Ø Puls über Zeit.");
      datasets = [{ label: "Ø Watt", data: rows.map((r) => has(r.avg_power) ? Number(r.avg_power) : null), borderColor: colors.ergo, tension: .25, yAxisID: "y" }, { label: "Ø Puls", data: rows.map((r) => has(r.avg_hr) ? Number(r.avg_hr) : null), borderColor: colors.hr, tension: .25, yAxisID: "y1" }];
    }
    setHtml(els.performanceFacts, `<span>Einheiten <strong>${rows.length}</strong></span>`);
    const options = chartOptions({ scales: { x: { ticks: { color: "#9ca3af" }, grid: { color: "transparent" } }, y: { ticks: { color: "#9ca3af" }, grid: { color: "rgba(148,163,184,.14)" } }, y1: { position: "right", ticks: { color: "#9ca3af" }, grid: { drawOnChartArea: false } } } });
    state.charts.performance = new Chart(els.performanceChart, { type: "line", data: { labels, datasets }, options });
  }
  function renderSportSummary(rows) {
    const wanted = rows.filter((r) => ["run", "ergo", "stair"].includes(r.sport_type));
    setHtml(els.sportSummary, wanted.map((r) => `<article class="dashboard-card cardio-sport-card"><div class="cardio-sport-head"><strong>${esc(r.sport_label)}</strong><span>${minutes(r.active_minutes)}</span></div><div class="cardio-sport-metrics">${(r.sport_type === "run" ? [["Läufe", r.sessions], ["Ø Pace", pace(r.avg_pace_sec)], ["Best Pace", pace(r.best_pace_sec)], ["Ø Puls", bpm(r.avg_hr)]] : r.sport_type === "stair" ? [["Einheiten", r.sessions], ["Ø Watt", watt(r.avg_power)], ["Best Ø Watt", watt(r.best_avg_power)], ["Ø Etagen", floors(r.avg_stair_floors)]] : [["Einheiten", r.sessions], ["Ø Watt", watt(r.avg_power)], ["Best Ø Watt", watt(r.best_avg_power)], ["Ø Puls", bpm(r.avg_hr)]]).map(([k, v]) => `<span><b>${esc(k)}</b><strong>${esc(v)}</strong></span>`).join("")}</div></article>`).join("") || `<div class="cardio-muted">Keine Lauf-, Ergo- oder Stair-Daten im aktuellen Filter.</div>`);
  }
  function columns() {
    const main = state.sport === "ergo" || state.sport === "stair" ? "Ø Watt" : state.sport === "run" ? "Pace" : "Hauptwert";
    const dist = state.sport === "stair" ? "Etagen" : "Distanz km";
    return state.sport === "all" ? ["Datum", "Sportart", "Dauer", dist, main, "Ø Puls", "Zone", "Aktionen"] : ["Datum", "Dauer", dist, main, "Ø Puls", "Zone", "Aktionen"];
  }
  function mainValue(s) { return s.sport_type === "ergo" || s.sport_type === "stair" ? watt(s.avg_power_w) : s.sport_type === "run" ? pace(s.avg_pace_sec_per_km) : minutes(s.active_minutes); }
  function zoneText(s) {
    if (!s.hr_zone) return "";
    return s.hr_zone_source === "pace_fallback" ? `${s.hr_zone}*` : s.hr_zone;
  }
  function rawId(s) { return String(s.id || "").replace(/^run:/, ""); }
  function actionButtonsHtml() {
    return `<div class="actions-flex">
      <button class="icon-btn edit-btn" title="Bearbeiten">
        <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M3 17.25V21h3.75L17.81 9.94l-3.75-3.75L3 17.25z"/><path d="M14.06 6.19l2.75-2.75a1.5 1.5 0 0 1 2.12 0l1.63 1.63a1.5 1.5 0 0 1 0 2.12l-2.75 2.75"/></svg>
      </button>
      <button class="icon-btn danger delete-btn" title="Löschen">
        <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M3 6h18" stroke-linecap="round"/><path d="M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2" stroke-linecap="round"/><path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/><path d="M10 11v6" stroke-linecap="round"/><path d="M14 11v6" stroke-linecap="round"/></svg>
      </button>
    </div>`;
  }
  function saveCancelHtml(saveClass, cancelClass) {
    return `<div class="actions-flex">
      <button class="icon-btn ${saveClass}" title="Speichern">
        <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M5 13l4 4L19 7" /></svg>
      </button>
      <button class="icon-btn danger ${cancelClass}" title="Abbrechen">
        <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M6 6l12 12M18 6L6 18" /></svg>
      </button>
    </div>`;
  }
  function rowValues(s) {
    const common = state.sport === "all"
      ? [displayDate(s.started_at), sportLabel(s.sport_type), tableDuration(s.duration_sec), s.sport_type === "stair" ? floors(s.stair_floors) : km(s.distance_km), mainValue(s), has(s.avg_hr_bpm) ? Math.round(s.avg_hr_bpm) : "", zoneText(s)]
      : [displayDate(s.started_at), tableDuration(s.duration_sec), state.sport === "stair" ? floors(s.stair_floors) : km(s.distance_km), mainValue(s), has(s.avg_hr_bpm) ? Math.round(s.avg_hr_bpm) : "", zoneText(s)];
    return common;
  }
  function renderTable(list) {
    state.rows = list?.rows || [];
    const rows = [...state.rows].sort((a, b) => (dt(b.started_at)?.getTime() || 0) - (dt(a.started_at)?.getTime() || 0));
    const fallbackCount = rows.filter((r) => r.hr_zone_source === "pace_fallback").length;
    setText(els.tableMeta, `${rows.length}${has(list?.total) ? ` / ${list.total}` : ""} Sessions${fallbackCount ? ` · ${fallbackCount} Pace-Zonen (*)` : ""}`);
    setHtml(els.tableHead, columns().map((c) => `<th>${esc(c)}</th>`).join(""));
    if (!rows.length) { setHtml(els.tbody, `<tr><td colspan="${columns().length}">Keine Cardio-Sessions im aktuellen Filter.</td></tr>`); return; }
    setHtml(els.tbody, rows.map((s) => {
      const values = rowValues(s);
      return `<tr data-id="${esc(rawId(s))}" data-sport="${esc(s.sport_type || "run")}" data-distance="${esc(s.distance_km || "")}">
        ${values.map((v) => `<td>${esc(v)}</td>`).join("")}
        <td class="actions-cell actions-col">${actionButtonsHtml()}</td>
      </tr>`;
    }).join(""));
  }
  function inputHtml(value, placeholder = "") {
    return `<input value="${esc(value === "—" ? "" : value)}" placeholder="${esc(placeholder)}">`;
  }
  function addNewRow() {
    if (!els.tbody || els.tbody.querySelector("tr.new-row")) return;
    const sport = state.sport === "ergo" ? "ergo" : state.sport === "stair" ? "stair" : "run";
    const values = state.sport === "all"
      ? [inputHtml(editDate(new Date()), "23.04"), `<select><option value="run"${sport === "run" ? " selected" : ""}>Laufen</option><option value="ergo"${sport === "ergo" ? " selected" : ""}>Ergo</option><option value="stair"${sport === "stair" ? " selected" : ""}>Stair Master</option></select>`, inputHtml("", "31:40"), inputHtml("", sport === "stair" ? "Etagen" : "km"), inputHtml("", sport === "ergo" || sport === "stair" ? "W" : "5:45"), inputHtml("", "bpm"), ""]
      : [inputHtml(editDate(new Date()), "23.04"), inputHtml("", "31:40"), inputHtml("", state.sport === "stair" ? "Etagen" : "km"), inputHtml("", state.sport === "ergo" || state.sport === "stair" ? "W" : "5:45"), inputHtml("", "bpm"), ""];
    const row = document.createElement("tr");
    row.className = "new-row";
    row.dataset.sport = sport;
    row.innerHTML = `${values.map((v) => `<td>${v}</td>`).join("")}<td class="actions-cell">${saveCancelHtml("save-new", "cancel-new")}</td>`;
    els.tbody.prepend(row);
  }
  function enterEditMode(row) {
    if (row.classList.contains("editing")) return;
    row.classList.add("editing");
    const cells = row.querySelectorAll("td");
    const editableCount = columns().length - 1;
    for (let i = 0; i < editableCount; i += 1) {
      const value = i === 0 ? cells[i].textContent.trim().replace(/^[A-Za-zÄÖÜäöüß]{2,3}\.?,?\s*/u, "") : cells[i].textContent.trim();
      cells[i].dataset.original = value;
      if (state.sport === "all" && i === 1) {
        const sport = row.dataset.sport === "ergo" ? "ergo" : row.dataset.sport === "stair" ? "stair" : "run";
        cells[i].innerHTML = `<select><option value="run"${sport === "run" ? " selected" : ""}>Laufen</option><option value="ergo"${sport === "ergo" ? " selected" : ""}>Ergo</option><option value="stair"${sport === "stair" ? " selected" : ""}>Stair Master</option></select>`;
      } else if ((state.sport === "all" && i === 6) || (state.sport !== "all" && i === 5)) {
        cells[i].dataset.original = value;
      } else {
        cells[i].innerHTML = inputHtml(value);
      }
    }
    cells[editableCount].dataset.originalHTML = cells[editableCount].innerHTML;
    cells[editableCount].innerHTML = saveCancelHtml("save-edit", "cancel-edit");
  }
  function exitEditMode(row) {
    const cells = row.querySelectorAll("td");
    const editableCount = columns().length - 1;
    for (let i = 0; i < editableCount; i += 1) cells[i].textContent = cells[i].dataset.original || "";
    cells[editableCount].innerHTML = cells[editableCount].dataset.originalHTML || actionButtonsHtml();
    row.classList.remove("editing");
  }
  function cellValue(cell) {
    const field = cell.querySelector("input, select");
    return (field ? field.value : cell.textContent || "").trim();
  }
  function normalizeDate(value) {
    const text = String(value || "").trim();
    const clean = text.replace(/^[A-Za-zÄÖÜäöüß]{2,3}\.?,?\s*/u, "");
    const m = clean.match(/^(\d{1,2})\.(\d{1,2})(?:\.(\d{2}|\d{4}))?$/);
    if (m) {
      const currentYear = new Date().getFullYear();
      const yearRaw = m[3] || String(currentYear);
      const year = yearRaw.length === 2 ? `20${yearRaw}` : yearRaw;
      return `${year}-${m[2].padStart(2, "0")}-${m[1].padStart(2, "0")}`;
    }
    return text;
  }
  function payloadFromRow(row) {
    const cells = row.querySelectorAll("td");
    const all = state.sport === "all";
    const sport = all ? cellValue(cells[1]) : (state.sport === "ergo" ? "ergo" : state.sport === "stair" ? "stair" : "run");
    const offset = all ? 1 : 0;
    const main = cellValue(cells[3 + offset]);
    return {
      date: normalizeDate(cellValue(cells[0])),
      sport_type: sport,
      duration_min: parseDurationInput(cellValue(cells[1 + offset])),
      distance_km: sport === "stair" ? "" : cellValue(cells[2 + offset]).replace(/[^\d,.-]/g, ""),
      stair_floors: sport === "stair" ? (main || cellValue(cells[2 + offset])).replace(/[^\d,.-]/g, "") : "",
      pace: sport === "run" ? main.replace("/km", "") : "",
      avg_power_w: sport === "ergo" ? main.replace(/[^\d,.-]/g, "") : "",
      avg_hr_bpm: cellValue(cells[4 + offset]).replace(/[^\d,.-]/g, ""),
    };
  }
  async function saveNewRow(row) {
    await api("/api/cardio/sessions", { method: "POST", body: JSON.stringify(payloadFromRow(row)) });
    await refresh();
  }
  async function saveEditedRow(row) {
    await api(`/api/cardio/sessions/${row.dataset.id}`, { method: "PUT", body: JSON.stringify(payloadFromRow(row)) });
    await refresh();
  }
  async function deleteRow(row) {
    const id = row.dataset.id;
    if (!id) { row.remove(); return; }
    if (!confirm("Eintrag wirklich löschen?")) return;
    await api(`/api/cardio/sessions/${id}`, { method: "DELETE" });
    await refresh();
  }
  async function refresh() {
    setText(els.error, "");
    try {
      const payload = await api(`/api/cardio/bundle?${query()}`, { headers: { "Content-Type": "application/json" } });
      renderSummary(payload); renderWeekly(payload.weekly_series || []); renderPerformance(payload.performance || {}); renderSportSummary(payload.sport_detail_cards || []); renderTable(payload.list || {});
    } catch (err) {
      console.error(err); setText(els.error, "Fehler beim Laden der Cardio-Daten.");
    }
  }
  async function syncRuns({ force = false, quiet = false } = {}) {
    if (els.sync) {
      els.sync.disabled = true;
      els.sync.textContent = "Synchronisiere …";
    }
    try {
      const suffix = force ? "?force=1" : "";
      const result = await api(`/api/runs/sync${suffix}`, { method: "POST", body: "{}" });
      if (!result.skipped || force) await refresh();
      if (els.sync) {
        els.sync.textContent = result.skipped ? "Bereits aktuell" : (result.imported ? `${result.imported} neu importiert` : "Alles aktuell");
        window.setTimeout(() => { if (els.sync) els.sync.textContent = "Garmin synchronisieren"; }, 2500);
      }
    } catch (err) {
      console.error(err);
      if (!quiet) setText(els.error, `Sync fehlgeschlagen: ${err.message}`);
      if (els.sync) els.sync.textContent = err.code === "garmin_not_configured" ? "Garmin verbinden" : "Sync erneut versuchen";
    } finally {
      if (els.sync) els.sync.disabled = false;
    }
  }
  function wire() {
    els.range?.addEventListener("change", () => { state.days = els.range.value || "30"; refresh(); });
    els.sport?.addEventListener("change", () => { state.sport = els.sport.value || "all"; refresh(); });
    els.view?.addEventListener("change", () => { state.view = els.view.value || "minutes"; refresh(); });
    els.add?.addEventListener("click", addNewRow);
    els.sync?.addEventListener("click", () => syncRuns({ force: true }));
    els.tbody?.addEventListener("click", async (event) => {
      const btn = event.target.closest("button");
      if (!btn) return;
      const row = btn.closest("tr");
      if (!row) return;
      if (btn.classList.contains("cancel-new")) { row.remove(); return; }
      if (btn.classList.contains("save-new")) { await saveNewRow(row); return; }
      if (btn.classList.contains("edit-btn")) { enterEditMode(row); return; }
      if (btn.classList.contains("cancel-edit")) { exitEditMode(row); return; }
      if (btn.classList.contains("save-edit")) { await saveEditedRow(row); return; }
      if (btn.classList.contains("delete-btn")) { await deleteRow(row); }
    });
  }
  wire();
  refresh();
  syncRuns({ quiet: true });
})();
