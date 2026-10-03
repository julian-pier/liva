(function () {
  "use strict";

  function reconcileCounter(previous, authoritative, elapsed, running) {
    const serverValue = Math.max(0, Number(authoritative) || 0);
    if (!running) return serverValue;
    return Math.max(serverValue, Math.max(0, Number(previous) || 0) + Math.max(0, Number(elapsed) || 0));
  }

  function periodBounds(endIso, range, customStart) {
    const end = new Date(`${endIso}T12:00:00Z`);
    const start = customStart ? new Date(`${customStart}T12:00:00Z`) : new Date(end);
    if (!customStart) start.setUTCDate(start.getUTCDate() - Number(range) + 1);
    return { start: start.toISOString().slice(0, 10), end: endIso };
  }

  function visualClusterEvents(events, laneKind) {
    const ordered = [...(events || [])].sort((a, b) => new Date(a.started_at) - new Date(b.started_at));
    const output = [];
    ordered.forEach((raw) => {
      const event = { ...raw };
      const key = event.is_idle ? "idle" : laneKind === "foreground"
        ? "PC aktiv"
        : String(event.display_name || event.app_display_name || event.app_name || "Aktivität");
      const previous = output[output.length - 1];
      const gap = previous ? (new Date(event.started_at) - new Date(previous.ended_at)) / 1000 : Infinity;
      if (previous && previous.visual_key === key && gap <= 45) {
        previous.ended_at = event.ended_at;
        previous.ended_at_local = event.ended_at_local;
        previous.duration_seconds += Number(event.duration_seconds || 0);
        previous.event_count += Number(event.event_count || 1);
        previous.page_title = event.page_title || previous.page_title;
        previous.window_title = event.window_title || previous.window_title;
        previous.domain = event.domain || previous.domain;
      } else {
        output.push({ ...event, visual_key: key, visual_name: key === "idle" ? "Idle" : key, duration_seconds: Number(event.duration_seconds || 0), event_count: Number(event.event_count || 1) });
      }
    });
    const minimum = laneKind === "foreground" ? 5 : 15;
    return output.filter((event, index) => Number(event.duration_seconds || 0) >= minimum || index === output.length - 1);
  }

  if (typeof module !== "undefined" && module.exports) module.exports = { reconcileCounter, periodBounds, visualClusterEvents };
  if (typeof document === "undefined") return;

  const root = document.querySelector("[data-activity-root]");
  if (!root) return;
  const $ = (id) => document.getElementById(id);
  const dateInput = $("activity-date");
  const state = {
    range: "day", tab: "overview", summary: null, timeline: [], iphone: null, period: null,
    loading: false, timer: null, dataThrough: 0, displayedActive: 0, customStart: "", mixKind: "apps",
    daySignature: "", liveSignature: "", lastTick: 0, drawerLists: {}
  };

  function escapeHtml(value) {
    const node = document.createElement("span"); node.textContent = value == null ? "" : String(value); return node.innerHTML;
  }

  function duration(seconds, compact) {
    const value = Math.max(0, Math.floor(Number(seconds) || 0));
    const h = Math.floor(value / 3600), m = Math.floor((value % 3600) / 60), s = value % 60;
    if (h) return compact ? `${h} h ${m} min` : `${h} h ${String(m).padStart(2, "0")} min`;
    if (m) return compact ? `${m} min` : `${m} min${s ? ` ${s} s` : ""}`;
    return `${s} s`;
  }

  function durationClock(seconds) {
    const value = Math.max(0, Math.floor(Number(seconds) || 0));
    const h = Math.floor(value / 3600), m = Math.floor((value % 3600) / 60), s = value % 60;
    return h ? `${h}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}` : `${m}:${String(s).padStart(2, "0")}`;
  }

  function localTime(value, seconds) {
    if (!value) return "–";
    return new Intl.DateTimeFormat("de-DE", { timeZone: "Europe/Berlin", hour: "2-digit", minute: "2-digit", ...(seconds ? { second: "2-digit" } : {}) }).format(new Date(value));
  }

  function formatDay(iso, long) {
    if (!iso) return "Datum";
    return new Intl.DateTimeFormat("de-DE", long ? { day: "numeric", month: "short", year: "numeric" } : { day: "2-digit", month: "2-digit" }).format(new Date(`${iso}T12:00:00Z`));
  }

  function currentActivityDay() {
    const parts = Object.fromEntries(new Intl.DateTimeFormat("en-GB", { timeZone: "Europe/Berlin", year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", hourCycle: "h23" }).formatToParts(new Date()).filter((p) => p.type !== "literal").map((p) => [p.type, p.value]));
    const day = new Date(Date.UTC(+parts.year, +parts.month - 1, +parts.day)); if (+parts.hour < 4) day.setUTCDate(day.getUTCDate() - 1); return day.toISOString().slice(0, 10);
  }

  function shiftIso(iso, days) { const date = new Date(`${iso}T12:00:00Z`); date.setUTCDate(date.getUTCDate() + days); return date.toISOString().slice(0, 10); }
  function isCurrentDay() { return dateInput.value === currentActivityDay(); }
  function laneKey(event) { return (event.track_kind || "foreground") === "visible" ? `visible:${event.monitor_id || "monitor"}` : "foreground"; }

  function currentLanes() {
    const latestByLane = new Map(); state.timeline.forEach((event) => latestByLane.set(laneKey(event), event));
    return [...latestByLane.entries()].filter(([, event]) => Math.abs(new Date(event.ended_at).getTime() - state.dataThrough) < 5000).map(([key, event]) => ({ key, event, monitor: key.startsWith("visible:") }));
  }

  function runningState() {
    return isCurrentDay() && state.summary?.current_foreground_active === true && Date.now() - state.dataThrough < 15000;
  }

  function laneSubtitle(event) {
    const app = event.app_display_name || event.app_name || event.app_exe || "Windows";
    let title = event.page_title || event.window_title || app;
    title = String(title).replace(/\s+[–-]\s+(Opera|Chrome|Microsoft Edge|Edge|Firefox)$/i, "").trim();
    if (!title || title === event.display_name) title = app;
    return title === app ? app : `${title} · ${app}`;
  }

  function structuralSignature() {
    const lanes = currentLanes().map((row) => [row.key, row.event.id, row.event.display_name, row.event.window_title]);
    const clusters = clusteredLanes().flatMap((lane) => lane.events.map((event) => [lane.key, event.started_at, event.visual_key]));
    const apps = preferredApps().slice(0, 6).map((row) => row.name);
    const sites = preferredSites().slice(0, 6).map((row) => row.name);
    const phone = state.iphone?.available ? [state.iphone.imported_at, ...(state.iphone.apps || []).slice(0, 4).map((row) => row.name)] : [state.iphone?.status];
    return JSON.stringify([dateInput.value, lanes, clusters, apps, sites, phone]);
  }

  function preferredApps() { const visible = state.summary?.usage_by_visible_app || []; return visible.length ? visible : state.summary?.usage_by_app || []; }
  function preferredSites() { const visible = state.summary?.usage_by_visible_website || []; return visible.length ? visible : state.summary?.usage_by_website || []; }

  function infoButton(text) { return `<button type="button" class="activity-info" data-tooltip="${escapeHtml(text)}" aria-label="Info">i</button>`; }

  function renderKpis() {
    const phone = state.iphone, phoneReady = Boolean(phone?.available);
    const phoneStatus = phoneReady ? `${formatDay(phone.usage_day)} · abgeschlossen` : phone?.status === "not_completed" ? "Noch nicht abgeschlossen" : "Noch nicht importiert";
    $("activity-kpis").innerHTML = `
      <article class="activity-kpi" data-source-kpi="windows"><div class="activity-kpi-top"><p>Windows</p><span class="activity-state"><i></i>${runningState() ? "Live" : "Erfasst"}</span></div><strong id="activity-active">${duration(state.displayedActive)}</strong><div class="activity-kpi-label"><span>Aktive PC-Zeit</span>${infoButton("Parallele Monitore werden nicht zur PC-Gesamtzeit addiert.")}</div></article>
      <article class="activity-kpi" data-source-kpi="iphone"><div class="activity-kpi-top"><p>iPhone</p></div><strong>${phoneReady ? duration(phone.total_usage_seconds) : "–"}</strong><div class="activity-kpi-label"><span>${escapeHtml(phoneStatus)}</span>${infoButton("iPhone-Daten stammen aus abgeschlossenen StayFree-Tagesimporten und werden nicht live hochgezählt.")}</div></article>`;
  }

  function renderNow(changed) {
    const lanes = currentLanes(), focus = lanes.find((row) => !row.monitor), monitors = lanes.filter((row) => row.monitor);
    const rows = monitors.length ? monitors : (focus ? [focus] : []);
    const signature = JSON.stringify(rows.map((row) => [row.key, row.event.id, row.event.display_name, row.event.window_title]));
    const didChange = changed && state.liveSignature && signature !== state.liveSignature; state.liveSignature = signature;
    if (!rows.length) { $("activity-now").innerHTML = '<div class="activity-empty">Gerade keine aktuelle Windows-Aktivität.</div>'; return; }
    $("activity-now").innerHTML = rows.map((row, index) => {
      const event = row.event;
      const isFocus = Boolean(focus && (focus.event.display_name === event.display_name || (focus.event.window_title && focus.event.window_title === event.window_title)));
      return `<article class="activity-now-card${isFocus ? " is-focus" : ""}${didChange ? " is-changing" : ""}">
        <div class="activity-now-meta"><span>${row.monitor ? `Monitor ${index + 1}` : "Aktueller Fokus"}</span>${isFocus ? "<b>Fokus</b>" : ""}</div>
        <div class="activity-now-copy"><strong>${escapeHtml(event.display_name || event.app_display_name || "Aktivität")}</strong><span>${escapeHtml(laneSubtitle(event))}</span><span class="activity-now-duration" data-live-start="${escapeHtml(event.started_at)}" data-live-idle="${event.is_idle ? "1" : "0"}">${durationClock(event.duration_seconds)}</span></div>
      </article>`;
    }).join("");
  }

  function clusteredLanes() {
    const lanes = new Map(); state.timeline.forEach((event) => { const key = laneKey(event); if (!lanes.has(key)) lanes.set(key, []); lanes.get(key).push(event); });
    return [...lanes.entries()].map(([key, events]) => ({ key, monitor: key.startsWith("visible:"), events: visualClusterEvents(events, key === "foreground" ? "foreground" : "visible") }));
  }

  function railPercent(value) {
    const parts = Object.fromEntries(new Intl.DateTimeFormat("en-GB", { timeZone: "Europe/Berlin", hour: "2-digit", minute: "2-digit", second: "2-digit", hourCycle: "h23" }).formatToParts(new Date(value)).filter((p) => p.type !== "literal").map((p) => [p.type, p.value]));
    let seconds = +parts.hour * 3600 + +parts.minute * 60 + +parts.second - 14400; if (seconds < 0) seconds += 86400; return Math.max(0, Math.min(100, seconds / 86400 * 100));
  }

  function tooltipFor(event, laneLabel) {
    const app = event.app_display_name || event.app_name || event.app_exe || "Windows";
    const lines = [event.visual_name || event.display_name || app, `${localTime(event.started_at)}–${localTime(event.ended_at)} · ${duration(event.duration_seconds, true)}`];
    if (event.domain) lines.push(event.domain);
    lines.push(`${app}${laneLabel ? ` · ${laneLabel}` : ""}`); return lines.join("\n");
  }

  function renderRail() {
    const lanes = clusteredLanes();
    if (!lanes.length) { $("activity-rail").innerHTML = '<div class="activity-empty">Noch kein Tagesverlauf vorhanden.</div>'; return; }
    const axis = '<div class="activity-rail-axis"><span></span><div class="activity-rail-axis-track"><span>04</span><span>08</span><span>12</span><span>16</span><span>20</span><span>00</span><span>04</span></div></div>';
    let monitorIndex = 0;
    const rows = lanes.map((lane) => {
      const laneLabel = lane.monitor ? `Monitor ${++monitorIndex}` : "PC";
      const segments = lane.events.map((event) => {
        const left = railPercent(event.started_at), right = railPercent(event.ended_at), width = Math.max(.15, right >= left ? right - left : 100 - left);
        const id = `${lane.key}|${event.started_at}|${event.visual_key}`;
        return `<button type="button" class="activity-rail-segment${event.is_idle ? " is-idle" : ""}" data-rail-id="${escapeHtml(id)}" style="left:${left}%;width:${width}%" data-tooltip="${escapeHtml(tooltipFor(event, laneLabel))}" aria-label="${escapeHtml(tooltipFor(event, laneLabel))}"></button>`;
      }).join("");
      const nowLine = isCurrentDay() ? '<i class="activity-now-line"></i>' : "";
      return `<div class="activity-rail-row${lane.monitor ? " is-monitor" : ""}"><div class="activity-rail-label"><strong>${laneLabel}</strong><span>${lane.monitor ? "sichtbar" : "aktive PC-Zeit"}</span></div><div class="activity-rail-track">${segments}${nowLine}</div></div>`;
    }).join("");
    $("activity-rail").innerHTML = axis + rows; updateNowLine();
  }

  function patchRail() {
    clusteredLanes().forEach((lane) => lane.events.forEach((event) => {
      const id = `${lane.key}|${event.started_at}|${event.visual_key}`;
      const node = [...document.querySelectorAll("[data-rail-id]")].find((item) => item.dataset.railId === id);
      if (!node) return;
      const left = railPercent(event.started_at), right = railPercent(event.ended_at); node.style.width = `${Math.max(.15, right >= left ? right - left : 100 - left)}%`;
      node.dataset.tooltip = tooltipFor(event, lane.monitor ? "Monitor" : "PC");
    }));
  }

  function updateNowLine() {
    if (!isCurrentDay()) return;
    const left = railPercent(new Date().toISOString()); document.querySelectorAll(".activity-now-line").forEach((line) => { line.style.left = `${left}%`; line.dataset.tooltip = `Jetzt · ${localTime(new Date())}`; });
  }

  function ranking(items, kind, limit, full) {
    const all = (items || []).map((row) => ({ ...row, seconds: Number(row.seconds ?? row.duration_seconds ?? 0) }));
    const rows = full ? all : all.slice(0, limit || 5);
    if (!rows.length) return '<div class="activity-empty">Für diesen Zeitraum liegen keine Daten vor.</div>';
    const total = all.reduce((sum, row) => sum + row.seconds, 0) || 1, max = Math.max(...all.map((row) => row.seconds), 1);
    state.drawerLists[kind] = all;
    const list = rows.map((row, index) => `<button type="button" class="activity-ranking-row" data-detail-kind="${kind}" data-detail-name="${escapeHtml(row.name)}" data-detail-seconds="${row.seconds}">
      <span class="activity-ranking-name">${escapeHtml(row.name)}</span><span class="activity-ranking-value">${duration(row.seconds, true)}</span><span class="activity-ranking-share">${Math.round(row.seconds / total * 100)} %</span>
      ${index < 3 ? `<span class="activity-ranking-track"><i class="activity-ranking-fill" style="width:${Math.max(1, row.seconds / max * 100).toFixed(2)}%"></i></span>` : ""}
    </button>`).join("");
    const more = !full && all.length > rows.length ? `<button type="button" class="activity-ranking-more" data-more-kind="${kind}">+ ${all.length - rows.length} weitere</button>` : "";
    return `<div class="activity-ranking">${list}${more}</div>`;
  }

  function renderRankings() {
    $("activity-apps").innerHTML = ranking(preferredApps(), "apps", 5, false);
    $("activity-domains").innerHTML = ranking(preferredSites(), "sites", 5, false);
  }

  function renderIphone() {
    const node = $("activity-iphone-content"), phone = state.iphone;
    if (!phone?.available) { node.innerHTML = `<div class="activity-iphone-intro"><p>iPhone</p><strong>Noch nicht abgeschlossen</strong><span>Der abgeschlossene StayFree-Tag erscheint nach dem nächsten Import.</span></div>`; return; }
    const apps = phone.apps || [], top = apps.slice(0, 3), total = Number(phone.total_usage_seconds || 0), topTwo = top.slice(0, 2).reduce((sum, row) => sum + Number(row.duration_seconds || 0), 0);
    const imported = phone.imported_at ? localTime(phone.imported_at) : "–";
    const rows = top.map((row) => `<button type="button" class="activity-iphone-row" data-detail-kind="iphone" data-detail-name="${escapeHtml(row.name)}" data-detail-seconds="${row.duration_seconds}"><span>${escapeHtml(row.name)}</span><strong>${duration(row.duration_seconds, true)}</strong></button>`).join("");
    const first = Number(top[0]?.duration_seconds || 0), second = Number(top[1]?.duration_seconds || 0), rest = Math.max(0, total - first - second);
    state.drawerLists.iphone = apps.map((row) => ({ name: row.name, seconds: Number(row.duration_seconds || 0) }));
    node.innerHTML = `<div class="activity-iphone-summary"><div class="activity-iphone-intro"><p>iPhone · ${formatDay(phone.usage_day)}</p><strong>${duration(total)}</strong><span>Bildschirmzeit</span></div><div class="activity-iphone-list">${rows}<button type="button" class="activity-ranking-more" data-more-kind="iphone">${apps.length > 3 ? `+ ${apps.length - 3} weitere` : "Alle Apps"}</button></div><div class="activity-iphone-visual"><div class="activity-iphone-strip"><i style="width:${first / Math.max(total, 1) * 100}%"></i><i style="width:${second / Math.max(total, 1) * 100}%"></i><i style="width:${rest / Math.max(total, 1) * 100}%"></i></div><div class="activity-iphone-facts"><span>Top 2 Anteil <b>${Math.round(topTwo / Math.max(total, 1) * 100)} %</b></span><span>Apps verwendet <b>${apps.length}</b></span><span>StayFree importiert <b>${imported}</b></span></div></div></div>`;
  }

  function renderDetails() {
    const summary = state.summary || {}, facts = [
      [localTime(summary.first_activity_at), "Erste Aktivität"], [isCurrentDay() ? "aktuell" : localTime(summary.last_activity_at), "Letzte Aktivität"],
      [String(summary.app_switches || 0), "App-Wechsel"], [summary.longest_focus_block ? duration(summary.longest_focus_block.seconds, true) : "–", "Fokusblock"],
      [`${Math.round(Number(summary.idle_share || 0) * 100)} %`, "Idle"], [duration(summary.parallel_active_seconds || 0, true), "Parallel aktiv"]
    ];
    $("activity-details").innerHTML = facts.map(([value, label]) => `<div class="activity-detail"><strong title="${escapeHtml(value)}">${escapeHtml(value)}</strong><span>${label}</span></div>`).join("");
  }

  function applyTabVisibility() {
    document.querySelectorAll("[data-source-section], [data-source-kpi], [data-source-bar]").forEach((node) => { const source = node.dataset.sourceSection || node.dataset.sourceKpi || node.dataset.sourceBar; node.hidden = state.tab !== "overview" && source !== state.tab; });
  }

  function renderDay(force) {
    const signature = structuralSignature();
    if (!force && state.daySignature === signature) { patchRail(); patchAuthoritativeValues(); return; }
    const changed = Boolean(state.daySignature); state.daySignature = signature;
    renderKpis(); renderNow(changed); renderRail(); renderDetails(); renderRankings(); renderIphone(); applyTabVisibility();
  }

  function patchAuthoritativeValues() {
    const active = $("activity-active"); if (active) active.textContent = duration(state.displayedActive);
    document.querySelectorAll("[data-detail-kind='apps'], [data-detail-kind='sites']").forEach((node) => {
      const source = node.dataset.detailKind === "apps" ? preferredApps() : preferredSites(); const row = source.find((item) => item.name === node.dataset.detailName); if (!row) return;
      const value = node.querySelector(".activity-ranking-value"); if (value) value.textContent = duration(row.seconds, true);
    });
  }

  function changeLabel(current, previous) {
    if (previous == null || !Number(previous)) return "Kein Vergleich";
    const delta = Number(current || 0) - Number(previous); return `${delta >= 0 ? "+" : "−"}${duration(Math.abs(delta), true)} zur Vorperiode`;
  }

  function renderHeatmap(heatmap) {
    const hours = Array.from({ length: 24 }, (_, index) => (index + 4) % 24), days = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"];
    const values = new Map((heatmap || []).map((cell) => [`${cell.weekday}:${cell.hour}`, Number(cell.seconds || 0)])), max = Math.max(...values.values(), 1);
    const labels = `<span></span>${hours.map((hour) => `<span>${hour % 2 === 0 ? String(hour).padStart(2, "0") : ""}</span>`).join("")}`;
    const cells = days.map((day, weekday) => `<strong>${day}</strong>${hours.map((hour) => { const seconds = values.get(`${weekday}:${hour}`) || 0, level = seconds ? Math.max(1, Math.ceil(seconds / max * 4)) : 0; return `<i class="activity-heat-cell level-${level}" data-tooltip="${day} · ${String(hour).padStart(2, "0")}–${String((hour + 1) % 24).padStart(2, "0")} Uhr\n${duration(seconds, true)} aktiv"></i>`; }).join("")}`).join("");
    return `<div class="activity-heatmap"><div class="activity-heat-grid">${labels}</div><div class="activity-heat-grid">${cells}</div></div>`;
  }

  function renderMix(windows) {
    const field = state.mixKind === "apps" ? "usage_by_app" : "usage_by_website";
    const top = (state.mixKind === "apps" ? windows.usage_by_app : windows.usage_by_website).slice(0, 4).map((row) => row.name), groups = [];
    for (let index = 0; index < windows.daily.length; index += 7) groups.push(windows.daily.slice(index, index + 7));
    const rows = groups.map((days) => { const totals = Object.fromEntries(top.map((name) => [name, 0])); let other = 0; days.forEach((day) => (day[field] || []).forEach((row) => top.includes(row.name) ? totals[row.name] += Number(row.seconds || 0) : other += Number(row.seconds || 0))); const sum = Object.values(totals).reduce((a, b) => a + b, other) || 1; const segments = top.map((name, index) => `<i class="mix-${index + 1}" style="width:${totals[name] / sum * 100}%" data-tooltip="${escapeHtml(name)} · ${duration(totals[name], true)}"></i>`).join("") + `<i class="mix-other" style="width:${other / sum * 100}%" data-tooltip="Andere · ${duration(other, true)}"></i>`; return `<div class="activity-mix-row"><span>${formatDay(days[0].date)}–${formatDay(days[days.length - 1].date)}</span><div>${segments}</div></div>`; }).join("");
    const legend = top.map((name, index) => `<span><i class="mix-${index + 1}"></i>${escapeHtml(name)}</span>`).join("") + '<span><i class="mix-other"></i>Andere</span>';
    return `<div class="activity-mix">${rows}<div class="activity-mix-legend">${legend}</div></div>`;
  }

  function renderPeriod() {
    const p = state.period, w = p.windows, phone = p.iphone;
    const values = [...w.daily.map((day) => Number(day.total_active_seconds || 0)), ...phone.daily.filter((day) => day.available).map((day) => Number(day.total_usage_seconds || 0))], max = Math.max(...values, 1);
    const windowsCoverage = w.daily.filter((day) => Number(day.total_active_seconds || 0) > 0).length;
    const bars = w.daily.map((day, index) => { const mobile = phone.daily[index], showLabel = p.days <= 7 || index % 5 === 0 || index === w.daily.length - 1; return `<div class="activity-day-bars"><i data-source-bar="windows" class="${day.total_active_seconds ? "" : "is-no-data"}" style="height:${day.total_active_seconds ? Math.max(1, day.total_active_seconds / max * 100) : 100}%" data-tooltip="${formatDay(day.date, true)}\nWindows · ${day.total_active_seconds ? duration(day.total_active_seconds, true) : "keine Daten"}"></i><i data-source-bar="iphone" class="${mobile?.available ? "" : "is-no-data"}" style="height:${mobile?.available ? Math.max(1, mobile.total_usage_seconds / max * 100) : 100}%" data-tooltip="${formatDay(day.date, true)}\niPhone · ${mobile?.available ? duration(mobile.total_usage_seconds, true) : "keine Daten"}"></i>${showLabel ? `<span>${p.days <= 7 ? new Intl.DateTimeFormat("de-DE", { weekday: "short" }).format(new Date(`${day.date}T12:00:00Z`)) : formatDay(day.date)}</span>` : ""}</div>`; }).join("");
    $("activity-period-view").innerHTML = `<section class="activity-period-kpis"><article class="activity-kpi" data-source-kpi="windows"><p>Ø Windows / Tag</p><strong>${duration(w.average_active_seconds)}</strong><div class="activity-kpi-label">${changeLabel(w.average_active_seconds, w.previous_average_active_seconds)}</div></article><article class="activity-kpi" data-source-kpi="iphone"><p>Ø iPhone / importiert</p><strong>${phone.average_usage_seconds == null ? "–" : duration(phone.average_usage_seconds)}</strong><div class="activity-kpi-label">${changeLabel(phone.average_usage_seconds, phone.previous_average_usage_seconds)}</div></article><article class="activity-kpi"><p>Coverage</p><strong>${windowsCoverage} / ${p.days}</strong><div class="activity-kpi-label">Windows · ${phone.completed_days} iPhone-Importe</div></article></section>
      <div class="activity-analytics-grid"><section class="activity-section activity-grid-usage"><header class="activity-section-head"><div><p>Vergleich</p><h2>Nutzung pro Tag</h2></div><span>Windows · iPhone</span></header><div class="activity-period-chart">${bars}</div><div class="activity-coverage-note">Schraffiert bedeutet: keine Daten – nicht null Nutzung.</div></section>
      <section class="activity-section activity-grid-apps" data-source-section="windows"><header class="activity-section-head"><div><p>Windows</p><h2>Top Apps</h2></div></header>${ranking(w.usage_by_app, "apps", 5, false)}</section>
      <section class="activity-section activity-grid-heat" data-source-section="windows"><header class="activity-section-head"><div><p>Windows</p><h2>Aktivitätsrhythmus</h2></div></header>${renderHeatmap(w.heatmap)}</section>
      <section class="activity-section activity-grid-mix" data-source-section="windows"><header class="activity-section-head"><div><p>Verteilung</p><h2>Nutzungsmix</h2></div><div class="activity-mini-switch"><button type="button" data-mix="apps" class="${state.mixKind === "apps" ? "is-active" : ""}">Apps</button><button type="button" data-mix="websites" class="${state.mixKind === "websites" ? "is-active" : ""}">Websites</button></div></header>${renderMix(w)}</section>
      <section class="activity-section activity-grid-sites" data-source-section="windows"><header class="activity-section-head"><div><p>Browser</p><h2>Top Websites</h2></div></header>${ranking(w.usage_by_website, "sites", 5, false)}</section>
      <section class="activity-section activity-grid-iphone" data-source-section="iphone"><header class="activity-section-head"><div><p>StayFree</p><h2>iPhone-Apps</h2></div><span>${phone.completed_days} importierte Tage</span></header>${ranking(phone.usage_by_app, "iphone", 5, false)}</section></div>`;
    applyTabVisibility();
  }

  function animateContent() { const node = document.querySelector(".activity-dashboard"); node.classList.remove("is-switching"); requestAnimationFrame(() => node.classList.add("is-switching")); }
  function render(force) { const day = state.range === "day"; $("activity-day-view").hidden = !day; $("activity-period-view").hidden = day; day ? renderDay(force) : renderPeriod(); }

  async function load() {
    if (state.loading) return; state.loading = true;
    try {
      if (state.range === "day") {
        const day = encodeURIComponent(dateInput.value), responses = await Promise.all([fetch(`/api/activity/summary?date=${day}`, { cache: "no-store" }), fetch(`/api/activity/timeline?date=${day}`, { cache: "no-store" }), fetch(`/api/activity/iphone/summary?date=${day}`, { cache: "no-store" })]);
        if (!responses[0].ok || !responses[1].ok) throw new Error("load failed"); state.summary = await responses[0].json(); state.timeline = (await responses[1].json()).events || []; state.iphone = responses[2].ok ? await responses[2].json() : null; state.dataThrough = state.summary.data_through ? new Date(state.summary.data_through).getTime() : 0;
        if (!runningState()) state.displayedActive = Number(state.summary.total_active_seconds || 0); else state.displayedActive = Math.max(state.displayedActive, Number(state.summary.total_active_seconds || 0));
      } else {
        const bounds = periodBounds(dateInput.value, state.range === "custom" ? 1 : state.range, state.range === "custom" ? state.customStart : null), response = await fetch(`/api/activity/period?start_date=${bounds.start}&end_date=${bounds.end}`, { cache: "no-store" }); if (!response.ok) throw new Error("period load failed"); state.period = await response.json();
      }
      $("activity-status-text").textContent = "Live"; updateDateLabel(); render(false);
    } catch (_) { $("activity-status-text").textContent = "Verbindung wird geprüft"; }
    finally { state.loading = false; schedule(); }
  }

  function tick() {
    const now = Date.now(), elapsed = state.lastTick ? Math.min(2, Math.max(0, (now - state.lastTick) / 1000)) : 1; state.lastTick = now;
    $("activity-live-clock").textContent = localTime(new Date(), true); updateNowLine();
    if (state.range !== "day" || !state.summary) return;
    const running = runningState(); state.displayedActive = reconcileCounter(state.displayedActive, state.summary.total_active_seconds, elapsed, running);
    const active = $("activity-active"); if (active) active.textContent = duration(state.displayedActive);
    document.querySelectorAll("[data-live-start]").forEach((counter) => { if (counter.dataset.liveIdle === "1" || !running) return; counter.textContent = durationClock((now - new Date(counter.dataset.liveStart).getTime()) / 1000); });
  }

  function schedule() { clearTimeout(state.timer); state.timer = setTimeout(load, document.hidden || state.range !== "day" ? 20000 : 1000); }
  function updateDateLabel() { $("activity-date-label").textContent = state.range === "day" ? formatDay(dateInput.value, true) : state.range === "custom" && state.customStart ? `${formatDay(state.customStart)}–${formatDay(dateInput.value)}` : `${state.range} Tage bis ${formatDay(dateInput.value)}`; }

  function showDrawer(kind, title, body) { $("activity-drawer-kind").textContent = kind; $("activity-drawer-title").textContent = title; $("activity-drawer-body").innerHTML = body; $("activity-drawer-backdrop").hidden = false; $("activity-drawer").classList.add("is-open"); $("activity-drawer").setAttribute("aria-hidden", "false"); }
  function openDetail(button) {
    const name = button.dataset.detailName, seconds = Number(button.dataset.detailSeconds || 0), kind = button.dataset.detailKind;
    showDrawer(kind === "iphone" ? "iPhone-App" : kind === "sites" ? "Website" : "Windows-App", name, `<div class="activity-drawer-stat"><span>${state.range === "day" ? "Heute" : "Gewählter Zeitraum"}</span><strong>${duration(seconds)}</strong></div><div class="activity-drawer-stat"><span>Quelle</span><strong>${kind === "iphone" ? "StayFree" : kind === "sites" ? "Browser" : "Windows"}</strong></div><div class="activity-drawer-stat"><span>Zählweise</span><strong>${kind === "sites" ? "Teil der Browserzeit" : kind === "iphone" ? "Separat von Windows" : "Parallel sichtbar möglich"}</strong></div>`);
  }
  function openRanking(kind) {
    const labels = { apps: ["Windows", "Alle Apps"], sites: ["Browser", "Alle Websites"], iphone: ["StayFree", "Alle iPhone-Apps"] }, items = state.drawerLists[kind] || [];
    showDrawer(labels[kind][0], labels[kind][1], `<div class="activity-drawer-ranking">${ranking(items, kind, items.length, true)}</div>`);
  }
  function closeDrawer() { $("activity-drawer").classList.remove("is-open"); $("activity-drawer").setAttribute("aria-hidden", "true"); setTimeout(() => { $("activity-drawer-backdrop").hidden = true; }, 200); }

  dateInput.value = currentActivityDay(); $("activity-end-date").value = dateInput.value; $("activity-start-date").value = shiftIso(dateInput.value, -6); updateDateLabel();
  document.querySelectorAll("[data-range]").forEach((button) => button.addEventListener("click", () => { state.range = button.dataset.range; document.querySelectorAll("[data-range]").forEach((item) => item.classList.toggle("is-active", item === button)); $("activity-custom-range").hidden = state.range !== "custom"; state.daySignature = ""; animateContent(); updateDateLabel(); if (state.range !== "custom") load(); }));
  document.querySelectorAll("[data-tab]").forEach((button) => button.addEventListener("click", () => { state.tab = button.dataset.tab; document.querySelectorAll("[data-tab]").forEach((item) => item.classList.toggle("is-active", item === button)); animateContent(); applyTabVisibility(); }));
  $("activity-prev").addEventListener("click", () => { dateInput.value = shiftIso(dateInput.value, state.range === "day" ? -1 : -Number(state.range === "custom" ? 1 : state.range)); state.displayedActive = 0; state.daySignature = ""; animateContent(); updateDateLabel(); load(); });
  $("activity-next").addEventListener("click", () => { dateInput.value = shiftIso(dateInput.value, state.range === "day" ? 1 : Number(state.range === "custom" ? 1 : state.range)); state.displayedActive = 0; state.daySignature = ""; animateContent(); updateDateLabel(); load(); });
  dateInput.addEventListener("change", () => { state.displayedActive = 0; state.daySignature = ""; animateContent(); updateDateLabel(); load(); });
  $("activity-apply-range").addEventListener("click", () => { state.customStart = $("activity-start-date").value; dateInput.value = $("activity-end-date").value; animateContent(); updateDateLabel(); load(); });
  document.addEventListener("click", (event) => { const mix = event.target.closest("[data-mix]"); if (mix) { state.mixKind = mix.dataset.mix; renderPeriod(); return; } const more = event.target.closest("[data-more-kind]"); if (more) { openRanking(more.dataset.moreKind); return; } const detail = event.target.closest("[data-detail-name]"); if (detail) openDetail(detail); });
  document.addEventListener("mouseover", (event) => { const target = event.target.closest("[data-tooltip]"); if (!target) return; const tip = $("activity-tooltip"); tip.textContent = target.dataset.tooltip; tip.hidden = false; });
  document.addEventListener("mousemove", (event) => { const tip = $("activity-tooltip"); if (!tip.hidden) { tip.style.left = `${Math.min(innerWidth - 250, event.clientX + 12)}px`; tip.style.top = `${Math.min(innerHeight - 90, event.clientY + 12)}px`; } });
  document.addEventListener("mouseout", (event) => { if (event.target.closest("[data-tooltip]")) $("activity-tooltip").hidden = true; });
  $("activity-drawer-close").addEventListener("click", closeDrawer); $("activity-drawer-backdrop").addEventListener("click", closeDrawer); document.addEventListener("keydown", (event) => { if (event.key === "Escape") closeDrawer(); });
  document.addEventListener("visibilitychange", () => document.hidden ? schedule() : load()); setInterval(tick, 1000); load();
})();
