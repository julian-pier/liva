(() => {
  const MOBILE_QUERY = "(max-width: 900px)";
  if (!window.matchMedia(MOBILE_QUERY).matches) return;
  const els = {
    root: document.getElementById("planning-page"),
    container: document.getElementById("mobile-plan-view"),
    gymSection: document.getElementById("mpv4-gym"),
    cardioSection: document.getElementById("mpv4-cardio"),
    nutritionSection: document.getElementById("mpv4-nutrition"),
    empty: document.getElementById("mobile-plan-empty"),
    domainToggle: document.getElementById("mpv4-domain-toggle"),
    pageTitle: document.getElementById("mpv4-title"),
    gymSwitch: document.getElementById("mpv4-gym-switch"),
    gymPrev: document.getElementById("mpv4-gym-prev"),
    gymNext: document.getElementById("mpv4-gym-next"),
    gymLabel: document.getElementById("mpv4-gym-label"),
  };

  if (!els.root || !els.container || !els.gymSection || !els.cardioSection || !els.nutritionSection) return;

  const DAY_ORDER = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"];
  const DAY_LONG = ["Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag"];
  const ENDURANCE_EVENT_KINDS = new Set(["run", "ergo", "bike", "row", "swim"]);

  const STORAGE = {
    tab: "planning_mobile_tab_v4",
    gymWeek: "planning_mobile_gym_week_v4",
    nutritionDay: "planning_mobile_nutrition_day_v4",
    cardioWeek: "planning_mobile_cardio_week_v1",
  };

  const storedDomain = localStorage.getItem("planning_tab") || localStorage.getItem(STORAGE.tab);

  const state = {
    tab: ["gym", "cardio", "nutrition"].includes(storedDomain) ? storedDomain : "gym",
    gymWeek: Number.parseInt(localStorage.getItem(STORAGE.gymWeek) || "", 10),
    nutritionDay: Number.parseInt(localStorage.getItem(STORAGE.nutritionDay) || "", 10),
    gymData: null,
    gymPlans: [],
    selectedGymPlanId: null,
    nutritionData: null,
    cardioData: null,
    cardioWeek: localStorage.getItem(STORAGE.cardioWeek) || "",
    expandedCardioSession: null,
    currentGymWeek: 1,
    longPressTimer: null,
    planPicker: null,
  };

  function esc(value) {
    return String(value || "")
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;")
      .replaceAll("'", "&#39;");
  }

  function fmtInt(value) {
    const n = Number(value);
    if (!Number.isFinite(n)) return "0";
    return String(Math.round(n));
  }

  function clamp(value, min, max) {
    const n = Number(value);
    if (!Number.isFinite(n)) return min;
    return Math.max(min, Math.min(max, Math.floor(n)));
  }

  function wrapIndex(value, total) {
    if (!Number.isFinite(value) || total <= 0) return 0;
    return ((value % total) + total) % total;
  }

  function asFiniteNumber(value) {
    const n = Number.parseFloat(value);
    return Number.isFinite(n) ? n : null;
  }

  function roundToStep(value, step) {
    if (!(step > 0)) return value;
    return Math.round(value / step) * step;
  }

  function roundInt(value) {
    return Math.floor(value + 0.5);
  }

  function exercisePriority(name) {
    const text = String(name || "").toLowerCase();
    const assist = ["fly", "seitheben", "lateral", "pushdown", "curl", "trizeps", "waden", "calf", "rear delt", "face pull"];
    const main = ["bank", "bench", "ohp", "overhead press", "squat", "kniebeuge", "deadlift", "kreuzheben", "rudern", "row", "latzug", "pull up", "pullup"];
    if (assist.some((k) => text.includes(k))) return "assist";
    if (main.some((k) => text.includes(k))) return "main";
    return "secondary";
  }

  function scaleSets(baseSets, factor, priority) {
    const base = Math.max(0, Number.parseInt(baseSets || 0, 10) || 0);
    const f = asFiniteNumber(factor);
    if (!Number.isFinite(f) || !(f > 0)) return base;
    const raw = roundInt(base * f);
    const minSets = 1;
    const maxUp = 2;
    const maxStepUp = { assist: 1, secondary: 1, main: 1 };
    const upper = Math.min(base + maxUp, base + (maxStepUp[priority] ?? 1));
    return Math.max(minSets, Math.min(raw, upper));
  }

  function applyRpeCap(values, cap, minRpe = 5.0, step = 0.5) {
    const list = Array.isArray(values) ? values.map((v) => Number.parseFloat(v)).filter(Number.isFinite) : [];
    const capNum = asFiniteNumber(cap);
    if (!list.length) return [];
    if (!Number.isFinite(capNum) || capNum <= 0) return list;
    const top = Math.max(...list);
    if (capNum >= top) return list;
    const newTop = Math.min(top, capNum);
    return list.map((v) => {
      const next = Math.max(minRpe, newTop - (top - v));
      return Math.max(minRpe, roundToStep(next, step));
    });
  }

  function fitRpeToSets(values, targetSets) {
    const list = Array.isArray(values) ? values.map((v) => Number.parseFloat(v)).filter(Number.isFinite) : [];
    const target = Math.max(0, Number.parseInt(targetSets || 0, 10) || 0);
    if (!list.length || target <= 0) return [];
    if (target === list.length) return list;
    const top = Math.max(...list);
    const deltas = list.map((v) => top - v);
    if (target < list.length) {
      const ranked = list.map((_, idx) => idx).sort((a, b) => (deltas[a] - deltas[b]) || (a - b));
      const keep = new Set(ranked.slice(0, target));
      return list.filter((_, idx) => keep.has(idx));
    }
    const easiest = Math.max(...deltas);
    const out = [...list];
    while (out.length < target) out.push(top - easiest);
    return out;
  }

  function previewItem(item, weekRule) {
    if (!item || typeof item !== "object") return item;
    const out = JSON.parse(JSON.stringify(item));
    if (out.kind === "exercise") {
      const sets = scaleSets(out.sets, weekRule?.strength_factor, exercisePriority(out.name));
      out.sets = sets;
      out.rpe_list = fitRpeToSets(applyRpeCap(out.rpe_list, weekRule?.rpe_cap), sets);
    }
    return out;
  }

  async function fetchJSON(url) {
    const res = await fetch(url, { credentials: "same-origin", cache: "no-store" });
    if (!res.ok) throw new Error(String(res.status));
    return res.json();
  }

  async function postJSON(url, payload) {
    const res = await fetch(url, {
      method: "POST",
      credentials: "same-origin",
      cache: "no-store",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload || {}),
    });
    if (!res.ok) throw new Error(String(res.status));
    return res.json();
  }

  async function copyToClipboard(text) {
    const raw = String(text || "");
    if (!raw.trim()) return;
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(raw);
      return;
    }
    const ta = document.createElement("textarea");
    ta.value = raw;
    ta.setAttribute("readonly", "true");
    ta.style.position = "fixed";
    ta.style.left = "-9999px";
    document.body.appendChild(ta);
    ta.select();
    document.execCommand("copy");
    ta.remove();
  }

  function normalizePlanJson(src) {
    const obj = src && typeof src === "object" ? src : {};
    const days = Array.isArray(obj.days) ? obj.days : [];
    const byDay = new Map(days.map((d) => [d?.day, d]));
    return {
      meta: obj.meta && typeof obj.meta === "object" ? obj.meta : {},
      weeks: Math.max(1, Number.parseInt(obj.weeks || 8, 10) || 8),
      sequence: Array.isArray(obj.sequence) ? obj.sequence.map((event) => ({
        kind: String(event?.kind || "gym"),
        time: event?.time || "",
        title: event?.title || "",
        items: Array.isArray(event?.items) ? event.items : [],
      })) : [],
      days: DAY_ORDER.map((dayName) => {
        const found = byDay.get(dayName);
        return {
          day: dayName,
          events: Array.isArray(found?.events)
            ? found.events.map((event) => ({
                kind: String(event?.kind || "gym"),
                time: event?.time || "",
                title: event?.title || "",
                items: Array.isArray(event?.items) ? event.items : [],
              }))
            : [],
        };
      }),
    };
  }

  function normalizeRules(raw, weeks) {
    const out = {};
    const src = raw?.weeks && typeof raw.weeks === "object" ? raw.weeks : (raw && typeof raw === "object" ? raw : {});
    for (let i = 1; i <= weeks; i += 1) {
      const key = `W${i}`;
      const row = src[key] && typeof src[key] === "object" ? src[key] : {};
      out[key] = {
        tag: String(row.tag || (row.deload ? "Deload" : "Build")),
        rpe_cap: row.rpe_cap ?? null,
      };
    }
    return out;
  }

  function normalizeBlocks(raw) {
    const out = {};
    if (!raw || typeof raw !== "object") return out;
    Object.entries(raw).forEach(([name, entries]) => {
      if (!name || !Array.isArray(entries)) return;
      out[name] = entries.map((entry) => ({
        day: DAY_ORDER.includes(entry?.day) ? entry.day : "",
        title: String(entry?.title || ""),
        lines: Array.isArray(entry?.lines) ? entry.lines.map((line) => String(line || "").trim()).filter(Boolean) : [],
      }));
    });
    return out;
  }

  function parseWeeksFromSpec(spec) {
    const text = String(spec || "").replaceAll("–", "-");
    const out = new Set();
    const rx = /(\d+)(?:\s*-\s*(\d+))?/g;
    let match = rx.exec(text);
    while (match) {
      const a = Number.parseInt(match[1], 10);
      const b = match[2] ? Number.parseInt(match[2], 10) : a;
      const lo = Math.min(a, b);
      const hi = Math.max(a, b);
      for (let i = lo; i <= hi; i += 1) out.add(i);
      match = rx.exec(text);
    }
    return Array.from(out);
  }

  function parseIsoDateOnly(iso) {
    const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(String(iso || "").trim());
    if (!match) return null;
    const y = Number.parseInt(match[1], 10);
    const m = Number.parseInt(match[2], 10) - 1;
    const d = Number.parseInt(match[3], 10);
    const dt = new Date(y, m, d);
    if (Number.isNaN(dt.getTime())) return null;
    dt.setHours(0, 0, 0, 0);
    return dt;
  }

  function weekCurrentFromStartDate(startIso, refDate) {
    const start = parseIsoDateOnly(startIso);
    if (!start) return 1;
    const today = new Date(refDate);
    today.setHours(0, 0, 0, 0);
    const startMonIdx = (start.getDay() + 6) % 7;
    const week1Start = new Date(start);
    if (startMonIdx !== 0) week1Start.setDate(week1Start.getDate() + (7 - startMonIdx));
    if (today < week1Start) return 1;
    const diffDays = Math.floor((today.getTime() - week1Start.getTime()) / 86400000);
    return Math.floor(diffDays / 7) + 1;
  }

  function computeCycleWeek(meta, totalWeeks) {
    const total = Math.max(1, Number.parseInt(meta?.total_weeks || totalWeeks || 8, 10) || totalWeeks || 8);
    const override = meta?.week_override;
    if (override != null && String(override).trim() !== "") {
      return Math.max(1, Math.min(total, Number.parseInt(override, 10) || 1));
    }
    const start = String(meta?.start_date || "").trim();
    if (!start) return 1;
    const elapsed = weekCurrentFromStartDate(start, new Date());
    if (meta?.loop_cycle) return ((Math.max(1, elapsed) - 1) % total) + 1;
    return Math.max(1, Math.min(total, elapsed));
  }

  function resolveRefBlock(item, dayName, weekNum, blocksJson) {
    const blockName = String(item?.block_name || "").trim();
    if (!blockName) return "";
    const entries = Array.isArray(blocksJson?.[blockName]) ? blocksJson[blockName] : [];
    if (!entries.length) return "";

    const display = String(item?.display_name || "").toLowerCase();
    const variation = String(item?.variation || "").toLowerCase();
    const preferredDay = String(item?.block_day || dayName || "");

    const ranked = entries
      .map((entry) => {
        let score = 0;
        const title = String(entry?.title || "").toLowerCase();
        if (preferredDay && entry.day === preferredDay) score += 2;
        if (display && title.includes(display)) score += 1;
        if (variation && title.includes(variation)) score += 1;
        return { entry, score };
      })
      .sort((a, b) => b.score - a.score)
      .map((x) => x.entry);

    const weekLine = /^Woche\s+([0-9]+(?:\s*[-–]\s*[0-9]+)?)(?:\s*\([^)]*\))?\s*:\s*(.+)$/i;
    for (const entry of ranked) {
      for (const raw of entry.lines || []) {
        const line = String(raw || "").trim().replace(/^\s*-\s*/, "");
        const match = weekLine.exec(line);
        if (!match) continue;
        const weeks = parseWeeksFromSpec(match[1]);
        if (weeks.includes(weekNum)) return match[2].trim();
      }
    }

    const fallback = ranked.flatMap((entry) => entry.lines || []).find((line) => String(line || "").trim());
    return fallback ? String(fallback).replace(/^\s*-\s*/, "").trim() : "";
  }

  function extractSetsFromText(text) {
    const rx = /(\d+)\s*x/gi;
    let sum = 0;
    let match = rx.exec(String(text || ""));
    while (match) {
      sum += Number.parseInt(match[1], 10) || 0;
      match = rx.exec(String(text || ""));
    }
    return sum;
  }

  function itemLine(item, dayName, weekNum, blocksJson, weekRule) {
    if (!item || typeof item !== "object") return "—";
    const shown = previewItem(item, weekRule);
    if (shown.kind === "exercise") {
      const reps = shown.reps && typeof shown.reps === "object"
        ? `${shown.reps.min || 0}-${shown.reps.max || shown.reps.min || 0}`
        : "";
      const rpe = Array.isArray(shown.rpe_list) && shown.rpe_list.length ? ` · RPE ${shown.rpe_list.join("/")}` : "";
      const variation = shown.variation ? ` (${shown.variation})` : "";
      return `${shown.name || "Übung"}${variation} · ${shown.sets || 0}x${reps}${rpe}`;
    }
    if (shown.kind === "cardio" || shown.kind === "run" || shown.kind === "ergo") {
      const explicit = String(shown.display || shown.display_text || "").trim();
      if (explicit) return explicit;
      const duration = Number.parseFloat(shown.duration_min ?? shown.amount_value ?? shown.amount);
      const unit = String(shown.amount_unit || shown.unit || "min").trim();
      const durationText = Number.isFinite(duration) && duration > 0 ? `${Number.isInteger(duration) ? duration : duration.toFixed(1)} ${unit}` : "";
      const intensity = String(shown.intensity || "").trim();
      const notes = String(shown.notes || shown.note || "").trim();
      const title = String(shown.title || shown.name || "").trim();
      const main = [durationText, intensity].filter(Boolean).join(" ").trim();
      if (main && notes) return `${main} @ ${notes}`;
      return main || notes || title || "—";
    }
    if (item.kind === "run_detail") return item.text || "Run";
    if (item.kind === "ref_block") {
      const base = String(item.display_name || "Block");
      const variation = item.variation ? ` (${item.variation})` : "";
      const resolved = resolveRefBlock(item, dayName, weekNum, blocksJson);
      return resolved ? `${base}${variation} · ${resolved}` : `${base}${variation} · Block`;
    }
    if (item.kind === "note") return item.text || "Notiz";
    return "—";
  }

  function eventStats(event, dayName, weekNum, blocksJson, weekRule) {
    const items = Array.isArray(event?.items) ? event.items : [];
    let sets = 0;
    let warnings = 0;
    items.forEach((item) => {
      if (item.kind === "exercise") {
        const shown = previewItem(item, weekRule);
        sets += Number.parseInt(shown?.sets || 0, 10) || 0;
      }
      if (item.kind === "ref_block") {
        const resolved = resolveRefBlock(item, dayName, weekNum, blocksJson);
        sets += extractSetsFromText(resolved);
      }
      if (item.kind === "warning") warnings += 1;
    });
    return { sets, warnings };
  }

  function nutritionDays(payload) {
    const out = Array.from({ length: 7 }, () => ({ slots: [], planned: {} }));
    const days = payload?.week_template?.days || [];
    days.forEach((day) => {
      const idx = Number(day.weekday);
      if (idx >= 0 && idx < 7) out[idx] = day;
    });
    return out;
  }

  function nutritionGroupForDay(payload, weekday) {
    const groups = Array.isArray(payload?.day_groups) ? payload.day_groups : [];
    return groups.find((group) => Array.isArray(group?.weekdays) && group.weekdays.includes(weekday)) || null;
  }

  function safeGroupColor(value) {
    const color = String(value || "").trim();
    return /^#[0-9a-f]{3,8}$/i.test(color) ? color : "";
  }

  function weekSnapshot(planDays, weekNum, rules, blocks) {
    const weekKey = `W${weekNum}`;
    const weekRule = rules[weekKey] || {};
    const rpeCap = Number(weekRule.rpe_cap);

    const summary = {
      sessions: 0,
      gym: 0,
      run: 0,
      rest: 0,
      sets: 0,
      warnings: 0,
      dayRows: [],
      weekTag: String(weekRule.tag || "Build"),
      rpeCap,
    };

    planDays.forEach((day, idx) => {
      const events = Array.isArray(day?.events) ? day.events : [];
      const rows = [];
      if (!events.length) summary.rest += 1;

      events.forEach((event) => {
        summary.sessions += 1;
        const kind = String(event.kind || "gym").toLowerCase();
        if (kind === "gym") summary.gym += 1;
        if (ENDURANCE_EVENT_KINDS.has(kind)) summary.run += 1;
        if (kind === "rest") summary.rest += 1;

        const stats = eventStats(event, day.day, weekNum, blocks, weekRule);
        summary.sets += stats.sets;
        summary.warnings += stats.warnings;

        (event.items || []).forEach((item) => {
          const shown = previewItem(item, weekRule);
          if (!Number.isFinite(rpeCap) || shown.kind !== "exercise" || !Array.isArray(shown.rpe_list)) return;
          const top = Math.max(...shown.rpe_list.map((v) => Number(v)).filter(Number.isFinite), -Infinity);
          if (Number.isFinite(top) && top > rpeCap) summary.warnings += 1;
        });

        rows.push({
          kind,
          time: event.time || "",
          title: event.title || "Session",
          lines: (event.items || []).map((item) => itemLine(item, day.day, weekNum, blocks, weekRule)),
          sets: stats.sets,
        });
      });

      summary.dayRows.push({
        dayLong: DAY_LONG[idx] || day.day || "Tag",
        rows,
      });
    });

    return summary;
  }

  function renderGymWeek(model, weekNum) {
    const isRolling = String(model.plan?.meta?.mode || "").toLowerCase() === "rolling_sequence";
    if (isRolling) return renderRollingSequence(model, weekNum);
    return renderFixedWeek(model, weekNum);
    const snapshot = weekSnapshot(model.plan.days, weekNum, model.rules, model.blocks);
    const host = document.createElement("article");
    host.className = "mpv4-card";
    host.innerHTML = `
      <div class="mpv4-card-head">
        <div>
          <div class="mpv4-card-title">Training</div>
          <div class="mpv4-card-sub mpv4-plan-name" title="Gedrückt halten zum Planwechsel">${esc(model.title)}</div>
        </div>
        <div class="mpv4-chip-wrap">
          <span class="mpv4-chip ${model.isActive ? "is-active" : ""}">${model.isActive ? "Aktiv" : "Template"}</span>
        </div>
      </div>
      ${model.focus ? `<div class="mpv4-focus">Fokus: ${esc(model.focus)}</div>` : ""}
      <div class="mpv4-week-head">
        <div class="mpv4-week-title">Woche ${weekNum}</div>
        <div class="mpv4-week-meta">${esc(String(snapshot.weekTag || "Build").toUpperCase())}${Number.isFinite(snapshot.rpeCap) ? ` · RPE <= ${fmtInt(snapshot.rpeCap)}` : ""}</div>
      </div>
      <div class="mpv4-stat-row">
        <span class="mpv4-stat">Sessions ${fmtInt(snapshot.sessions)}</span>
        <span class="mpv4-stat">Sets ${fmtInt(snapshot.sets)}</span>
        <span class="mpv4-stat">Gym ${fmtInt(snapshot.gym)}</span>
        <span class="mpv4-stat">Run ${fmtInt(snapshot.run)}</span>
        <span class="mpv4-stat">Warnungen ${fmtInt(snapshot.warnings)}</span>
      </div>
    `;

    const dayGrid = document.createElement("div");
    dayGrid.className = "mpv4-day-grid";

    snapshot.dayRows.forEach((day) => {
      const dayCard = document.createElement("article");
      dayCard.className = "mpv4-day-card";
      dayCard.innerHTML = `
        <div class="mpv4-day-head">
          <div class="mpv4-day-title">${esc(day.dayLong)}</div>
          <div class="mpv4-day-sub">${day.rows.length ? `${day.rows.length} Session${day.rows.length === 1 ? "" : "s"}` : "Ruhetag"}</div>
        </div>
      `;

      const body = document.createElement("div");
      body.className = "mpv4-day-body";

      if (!day.rows.length) {
        const rest = document.createElement("div");
        rest.className = "mpv4-empty";
        rest.textContent = "Kein Eintrag.";
        body.appendChild(rest);
      } else {
        day.rows.forEach((row) => {
          const rowEl = document.createElement("div");
          rowEl.className = `mpv4-event kind-${esc(row.kind || "gym")}`;
          rowEl.innerHTML = `
            <div class="mpv4-event-head">
              <span class="mpv4-event-time">${esc(row.time || "—")}</span>
              <span class="mpv4-event-name">${esc(row.title || "Session")}</span>
              <span class="mpv4-event-kind">${esc(String(row.kind || "gym").toUpperCase())}</span>
            </div>
            <div class="mpv4-event-sets">${fmtInt(row.sets)} Sets</div>
          `;
          const list = document.createElement("ul");
          list.className = "mpv4-lines";
          row.lines.forEach((line) => {
            const li = document.createElement("li");
            li.textContent = line;
            list.appendChild(li);
          });
          rowEl.appendChild(list);
          body.appendChild(rowEl);
        });
      }

      dayCard.appendChild(body);
      dayGrid.appendChild(dayCard);
    });

    host.appendChild(dayGrid);
    return host;
  }

  function renderFixedWeek(model, weekNum) {
    const snapshot = weekSnapshot(model.plan.days, weekNum, model.rules, model.blocks);
    const activeDays = snapshot.dayRows.filter((day) => day.rows.length);
    const hasPeriodization = Boolean(model.plan?.meta?.periodization_enabled);
    const host = document.createElement("article");
    host.className = "mpv4-sheet mpv4-training-sheet";
    host.innerHTML = `
      <div class="mpv4-sheet-kicker">WOCHENPLAN</div>
      <div class="mpv4-sheet-head">
        <div>
          <div class="mpv4-sheet-title mpv4-plan-name" title="Gedrückt halten zum Planwechsel">${esc(model.title)}</div>
          ${model.focus ? `<div class="mpv4-sheet-sub">${esc(model.focus)}</div>` : ""}
        </div>
        <span class="mpv4-mode-chip">${activeDays.length} Trainingstage</span>
      </div>
      <div class="mpv4-metric-rail">
        ${hasPeriodization ? `<span>W${weekNum} · ${esc(String(snapshot.weekTag || "Build").toUpperCase())}</span>` : ""}
        <span>${fmtInt(snapshot.sets)} Sets</span>
        ${hasPeriodization && Number.isFinite(snapshot.rpeCap) ? `<span>RPE ≤ ${fmtInt(snapshot.rpeCap)}</span>` : ""}
      </div>
    `;
    const list = document.createElement("div");
    list.className = "mpv4-rotation-list";
    activeDays.forEach((day) => {
      day.rows.forEach((event) => {
        const row = document.createElement("section");
        row.className = "mpv4-rotation-session";
        row.innerHTML = `
          <div class="mpv4-session-marker">${esc(day.dayLong.slice(0, 2))}</div>
          <div class="mpv4-session-main"><div class="mpv4-session-title">${esc(event.title)}</div><div class="mpv4-session-meta">${fmtInt(event.sets)} Arbeitssätze · ${esc(String(event.kind || "gym").toUpperCase())}</div></div>
        `;
        const exercises = document.createElement("div");
        exercises.className = "mpv4-exercise-lines";
        event.lines.forEach((line) => { const el = document.createElement("div"); el.textContent = line; exercises.appendChild(el); });
        row.appendChild(exercises);
        list.appendChild(row);
      });
    });
    if (!activeDays.length) list.innerHTML = '<div class="mpv4-empty">In dieser Woche sind noch keine Einheiten geplant.</div>';
    host.appendChild(list);
    return host;
  }

  function renderRollingSequence(model, weekNum) {
    const weekRule = model.rules[`W${weekNum}`] || {};
    const sequence = Array.isArray(model.plan?.sequence) ? model.plan.sequence : [];
    const hasPeriodization = Boolean(model.plan?.meta?.periodization_enabled);
    const totalSets = sequence.reduce((sum, event) => sum + eventStats(event, "", weekNum, model.blocks, weekRule).sets, 0);
    const host = document.createElement("article");
    host.className = "mpv4-sheet mpv4-training-sheet";
    host.innerHTML = `
      <div class="mpv4-sheet-kicker">ROLLING ROTATION</div>
      <div class="mpv4-sheet-head">
        <div>
          <div class="mpv4-sheet-title mpv4-plan-name" title="Gedrückt halten zum Planwechsel">${esc(model.title)}</div>
          ${model.focus ? `<div class="mpv4-sheet-sub">${esc(model.focus)}</div>` : ""}
        </div>
        <span class="mpv4-mode-chip">${sequence.length} Einheiten</span>
      </div>
      <div class="mpv4-metric-rail">
        ${hasPeriodization ? `<span>W${weekNum} · ${esc(String(weekRule.tag || "Build").toUpperCase())}</span>` : ""}
        <span>${fmtInt(totalSets)} Sets / Rotation</span>
        ${hasPeriodization && Number.isFinite(Number(weekRule.rpe_cap)) ? `<span>RPE ≤ ${fmtInt(weekRule.rpe_cap)}</span>` : ""}
      </div>
    `;

    const list = document.createElement("div");
    list.className = "mpv4-rotation-list";
    sequence.forEach((event, index) => {
      const stats = eventStats(event, "", weekNum, model.blocks, weekRule);
      const row = document.createElement("section");
      row.className = "mpv4-rotation-session";
      row.innerHTML = `
        <div class="mpv4-session-marker">${String(index + 1).padStart(2, "0")}</div>
        <div class="mpv4-session-main">
          <div class="mpv4-session-title">${esc(event.title || "Einheit")}</div>
          <div class="mpv4-session-meta">${fmtInt(stats.sets)} Arbeitssätze · ${Array.isArray(event.items) ? event.items.length : 0} Übungen</div>
        </div>
      `;
      const exercises = document.createElement("div");
      exercises.className = "mpv4-exercise-lines";
      (event.items || []).forEach((item) => {
        const line = document.createElement("div");
        line.textContent = itemLine(item, "", weekNum, model.blocks, weekRule);
        exercises.appendChild(line);
      });
      row.appendChild(exercises);
      list.appendChild(row);
    });
    if (!sequence.length) list.innerHTML = '<div class="mpv4-empty">Noch keine Einheiten in der Rotation.</div>';
    host.appendChild(list);
    return host;
  }

  function renderNutritionDay(payload, dayIndex) {
    const days = nutritionDays(payload);
    const day = days[dayIndex] || { slots: [], planned: {} };

    const totals = days.reduce((acc, row) => {
      acc.kcal += Number(row?.planned?.kcal || 0);
      acc.p += Number(row?.planned?.p || 0);
      acc.c += Number(row?.planned?.c || 0);
      acc.f += Number(row?.planned?.f || 0);
      acc.meals += Array.isArray(row?.slots) ? row.slots.length : 0;
      return acc;
    }, { kcal: 0, p: 0, c: 0, f: 0, meals: 0 });

    const avg = { kcal: totals.kcal / 7, p: totals.p / 7, c: totals.c / 7, f: totals.f / 7 };
    const rawSlots = Array.isArray(day?.slots) ? day.slots : [];
    const slots = rawSlots.filter((slot) => Array.isArray(slot?.items) && slot.items.length > 0);
    const macros = day?.planned || {};

    const host = document.createElement("article");
    host.className = "mpv4-sheet mpv4-nutrition-sheet";
    host.innerHTML = `
      <div class="mpv4-day-rail" role="tablist" aria-label="Wochentag auswählen">
        ${days.map((entry, index) => {
          const group = nutritionGroupForDay(payload, index);
          const color = safeGroupColor(group?.color);
          const groupStyle = color ? ` style="--mpv4-group-color:${color}"` : "";
          const groupTitle = group?.name ? ` title="${esc(group.name)}"` : "";
          return `<button type="button" class="mpv4-day-rail-btn ${index === dayIndex ? "is-active" : ""} ${group ? "is-linked" : ""}" data-mpv4-day="${index}" role="tab" aria-selected="${index === dayIndex}"${groupStyle}${groupTitle}><span>${DAY_ORDER[index]}</span><small>${fmtInt(entry?.planned?.kcal)} kcal</small></button>`;
        }).join("")}
      </div>
      <div class="mpv4-sheet-head">
        <div>
          <div class="mpv4-sheet-kicker">TAGESPLAN</div>
          <div class="mpv4-sheet-title">${esc(DAY_LONG[dayIndex] || "Tag")}</div>
        </div>
        <span class="mpv4-mode-chip">${slots.length} Meals</span>
      </div>
      <div class="mpv4-macro-rail">
        <span><b>${fmtInt(macros.kcal)}</b> kcal</span><span>P ${fmtInt(macros.p)}</span><span>C ${fmtInt(macros.c)}</span><span>F ${fmtInt(macros.f)}</span>
      </div>
    `;
    const body = document.createElement("div");
    body.className = "mpv4-meal-list";

    if (!slots.length) {
      const empty = document.createElement("div");
      empty.className = "mpv4-empty";
      empty.textContent = "Keine Meals geplant.";
      body.appendChild(empty);
    } else {
      slots.forEach((slot) => {
        const items = Array.isArray(slot?.items) ? slot.items : [];
        const slotEl = document.createElement("div");
        slotEl.className = "mpv4-meal";
        const titleRaw = slot.custom_title || slot.meal_title || slot.slot_title || "Meal";

        slotEl.innerHTML = `
          <div class="mpv4-meal-head">
            <span class="mpv4-meal-index">${esc(slot.time_text || "—")}</span>
            <span class="mpv4-meal-title">${esc(titleRaw)}</span>
          </div>
        `;

        if (items.length) {
          const list = document.createElement("ul");
          list.className = "mpv4-food-lines";
          items.forEach((item) => {
            const li = document.createElement("li");
            const amount = Number(item.amount);
            const hasAmount = Number.isFinite(amount);
            const unit = item.unit ? ` ${item.unit}` : "";
            const name = item.name || item.food_name || item.raw || "Food";
            li.innerHTML = `<span>${esc(name)}</span><b>${hasAmount ? `${fmtInt(amount)}${esc(unit)}` : ""}</b>`;
            list.appendChild(li);
          });
          slotEl.appendChild(list);
        }

        body.appendChild(slotEl);
      });
    }

    host.appendChild(body);
    host.querySelectorAll("[data-mpv4-day]").forEach((button) => button.addEventListener("click", () => {
      state.nutritionDay = Number(button.dataset.mpv4Day);
      render();
    }));
    return host;
  }

  function cardioDate(value) {
    const date = parseIsoDateOnly(value);
    return date || new Date();
  }

  function cardioIso(date) {
    const copy = new Date(date);
    copy.setHours(12, 0, 0, 0);
    return `${copy.getFullYear()}-${String(copy.getMonth() + 1).padStart(2, "0")}-${String(copy.getDate()).padStart(2, "0")}`;
  }

  function cardioMonday(value) {
    const date = value instanceof Date ? new Date(value) : cardioDate(value);
    date.setHours(12, 0, 0, 0);
    date.setDate(date.getDate() - ((date.getDay() + 6) % 7));
    return date;
  }

  function cardioAddDays(value, days) {
    const date = value instanceof Date ? new Date(value) : cardioDate(value);
    date.setDate(date.getDate() + days);
    return date;
  }

  function cardioPace(seconds) {
    const total = Math.round(Number(seconds) || 0);
    return total ? `${Math.floor(total / 60)}:${String(total % 60).padStart(2, "0")}` : "—";
  }

  function cardioClock(seconds) {
    const total = Math.round(Number(seconds) || 0);
    if (!total) return "—";
    const hours = Math.floor(total / 3600);
    const minutes = Math.floor((total % 3600) / 60);
    const secs = total % 60;
    return hours ? `${hours}:${String(minutes).padStart(2, "0")}:${String(secs).padStart(2, "0")}` : `${minutes}:${String(secs).padStart(2, "0")}`;
  }

  function cardioFlatSteps(steps) {
    const byParent = new Map();
    (steps || []).forEach((step) => {
      const parent = step.parent_step_id || "";
      if (!byParent.has(parent)) byParent.set(parent, []);
      byParent.get(parent).push(step);
    });
    const result = [];
    const walk = (parent, depth) => {
      (byParent.get(parent) || []).sort((a, b) => (a.sort_order || 0) - (b.sort_order || 0)).forEach((step) => {
        result.push({ ...step, depth });
        walk(step.id, depth + 1);
      });
    };
    walk("", 0);
    return result;
  }

  function cardioExecutionSteps(session) {
    return Array.isArray(session?.execution_steps) ? session.execution_steps : [];
  }

  function cardioKindLabel(kind) {
    return ({ open: "Locker", warmup: "Einlaufen", work: "Belastung", stride: "Steigerung", recovery: "Pause", cooldown: "Auslaufen" })[kind] || kind;
  }

  function cardioTarget(step) {
    return step?.resolved || step?.target || {};
  }

  function cardioPaceRange(target) {
    const fast = Number(target?.pace_min_s_per_km);
    const slow = Number(target?.pace_max_s_per_km);
    if (fast > 0 && slow > 0) return `${cardioPace(fast)}–${cardioPace(slow)}`;
    return cardioPace(target?.pace_s_per_km);
  }

  function cardioSessionPace(session) {
    const steps = cardioExecutionSteps(session);
    const work = steps.find((step) => !["open", "warmup", "cooldown", "recovery"].includes(step.kind) && Number(cardioTarget(step).pace_s_per_km) > 0);
    const paced = work || steps.find((step) => Number(cardioTarget(step).pace_s_per_km) > 0);
    return paced ? cardioPaceRange(cardioTarget(paced)) : "nach Gefühl";
  }

  function cardioDuration(seconds) {
    const total = Math.round(Number(seconds) || 0);
    if (total < 60) return `${total} s`;
    return total % 60 ? `${Math.floor(total / 60)}:${String(total % 60).padStart(2, "0")} min` : `${total / 60} min`;
  }

  function cardioStepText(step) {
    const executionTarget = cardioTarget(step);
    const internalTarget = step?.target || {};
    const amount = step.duration_s
      ? cardioDuration(step.duration_s)
      : step.distance_m
        ? `${Math.round(step.distance_m)} m`
        : "offen";
    const pace = Number(executionTarget.pace_s_per_km) > 0 ? `${cardioPaceRange(executionTarget)} /km` : "nach Gefühl";
    const currentMaxHr = Number(step?.resolved?.estimated_max_hr_bpm);
    const internalHrMin = internalTarget.hr_min_pct ?? step?.resolved?.internal_hr_min_pct;
    const internalHrMax = internalTarget.hr_max_pct ?? step?.resolved?.internal_hr_max_pct ?? internalHrMin;
    const heart = Number(internalTarget.hr_bpm) > 0
      ? ` · ${Math.round(internalTarget.hr_bpm)} bpm`
      : currentMaxHr > 0 && internalHrMin != null
        ? ` · ${Math.round(currentMaxHr * Number(internalHrMin) / 100)}–${Math.round(currentMaxHr * Number(internalHrMax) / 100)} bpm`
        : "";
    const rpeMin = internalTarget.rpe_min ?? (internalTarget.metric === "rpe" ? internalTarget.min : null);
    const rpeMax = internalTarget.rpe_max ?? (internalTarget.metric === "rpe" ? internalTarget.max : null);
    const rpe = rpeMin != null ? ` · RPE ${rpeMin}${rpeMax != null && rpeMax !== rpeMin ? `–${rpeMax}` : ""}` : "";
    return `${amount} · ${pace}${heart}${rpe}`;
  }

  function cardioWorkoutSummary(steps) {
    const lead = steps.find((step) => step.repeat_group_id && step.kind === "work") || steps.find((step) => step.repeat_group_id && step.kind === "stride");
    if (!lead) return `${steps.length} Schritte`;
    return `${lead.repeat_count} × ${lead.duration_s ? cardioDuration(lead.duration_s) : `${Math.round(lead.distance_m)} m`}${lead.kind === "stride" ? " Steigerungen" : ""}`;
  }

  function cardioWorkoutChart(session) {
    const steps = cardioExecutionSteps(session);
    if (!steps.length || !window.LivaWorkoutCharts?.buildProfile(steps, "pace").points.length) return "";
    return `<figure class="mpv4-cardio-workout-chart" aria-label="Pace-Verlauf dieser Einheit"><figcaption><span>Pace</span><b>${esc(cardioWorkoutSummary(steps))}</b></figcaption><div class="mpv4-cardio-workout-canvas"><canvas data-mobile-workout-chart="${esc(session.id)}"></canvas></div></figure>`;
  }

  function cardioInitialWeek(plan) {
    if (state.cardioWeek) return cardioMonday(state.cardioWeek);
    const sessions = Array.isArray(plan?.sessions) ? plan.sessions : [];
    const today = cardioMonday(new Date());
    if (!sessions.length) return today;
    const first = cardioMonday(sessions[0].scheduled_date);
    const last = cardioMonday(sessions[sessions.length - 1].scheduled_date);
    return today < first ? first : today > last ? last : today;
  }

  function renderCardioRead(plan) {
    const host = document.createElement("div");
    host.className = "mpv4-cardio-read";
    const event = plan?.event || {};
    const phases = (plan?.phases || []).slice().sort((a, b) => String(a.start_date).localeCompare(String(b.start_date)));
    const sessions = (plan?.sessions || []).slice().sort((a, b) => String(a.scheduled_date).localeCompare(String(b.scheduled_date)));
    const weekStart = cardioMonday(state.cardioWeek || cardioInitialWeek(plan));
    const weekEnd = cardioAddDays(weekStart, 6);
    const weekStartIso = cardioIso(weekStart);
    const weekEndIso = cardioIso(weekEnd);
    const weekSessions = sessions.filter((session) => session.scheduled_date >= weekStartIso && session.scheduled_date <= weekEndIso);
    const dateFormat = new Intl.DateTimeFormat("de-DE", { day: "numeric", month: "short" });
    const longDate = new Intl.DateTimeFormat("de-DE", { day: "numeric", month: "long", year: "numeric" });
    const today = new Date();
    today.setHours(12, 0, 0, 0);
    const seasonStart = phases.length ? cardioDate(phases[0].start_date) : today;
    const seasonEnd = event.event_date ? cardioDate(event.event_date) : (phases.length ? cardioDate(phases[phases.length - 1].end_date) : today);
    const totalDays = Math.max(1, Math.round((seasonEnd - seasonStart) / 86400000));
    const elapsed = Math.round((today - seasonStart) / 86400000);
    const progress = Math.max(0, Math.min(100, elapsed / totalDays * 100));
    const activePhase = phases.find((phase) => today >= cardioDate(phase.start_date) && today <= cardioDate(phase.end_date));
    const nextPhase = phases.find((phase) => today < cardioDate(phase.start_date));
    const daysToStart = Math.max(0, Math.ceil((seasonStart - today) / 86400000));
    const daysToGoal = Math.max(0, Math.ceil((seasonEnd - today) / 86400000));
    const phaseState = activePhase
      ? `${activePhase.name} · Tag ${Math.floor((today - cardioDate(activePhase.start_date)) / 86400000) + 1}`
      : today < seasonStart
        ? `Start in ${daysToStart} Tagen`
        : "Zielphase abgeschlossen";
    const phaseSegments = phases.map((phase) => {
      const days = Math.max(1, Math.round((cardioDate(phase.end_date) - cardioDate(phase.start_date)) / 86400000) + 1);
      return `<span data-kind="${esc(phase.phase_type)}" style="--phase-grow:${days}" title="${esc(phase.name)}"></span>`;
    }).join("");

    const weekly = Array.isArray(plan?.load_summary) ? plan.load_summary : [];
    const selectedSummary = weekly.find((row) => row.week_start === weekStartIso) || {};
    const selectedAvgPace = selectedSummary.distance_m > 0 ? selectedSummary.duration_s / (selectedSummary.distance_m / 1000) : 0;
    let selectedIndex = weekly.findIndex((row) => row.week_start === weekStartIso);
    if (selectedIndex < 0) selectedIndex = 0;
    const chartStart = Math.max(0, Math.min(Math.max(0, weekly.length - 8), selectedIndex - 3));
    const chartRows = weekly.slice(chartStart, chartStart + 8);
    const chartMax = Math.max(1, ...chartRows.map((row) => Number(row.distance_m || 0)));
    const chart = chartRows.map((row, index) => {
      const height = Math.max(8, Math.round(Number(row.distance_m || 0) / chartMax * 56));
      const avg = row.distance_m > 0 ? row.duration_s / (row.distance_m / 1000) : 0;
      const tip = `${((row.distance_m || 0) / 1000).toFixed(1)} km · ${Math.round((row.duration_s || 0) / 60)} min · Ø ${cardioPace(avg)} /km`;
      const edge = index < 2 ? " tip-left" : index > chartRows.length - 3 ? " tip-right" : "";
      return `<span class="${row.week_start === weekStartIso ? "is-selected" : ""}${edge}" tabindex="0" data-tip="${esc(tip)}" aria-label="${esc(tip)}"><i style="--bar-height:${height}px"></i><small>${dateFormat.format(cardioDate(row.week_start))}</small></span>`;
    }).join("");

    const sessionRows = weekSessions.map((session) => {
      const expanded = state.expandedCardioSession === session.id;
      const steps = cardioExecutionSteps(session);
      const stepRows = steps.map((step) => `<li><span>${String(step.execution_index || "").padStart(2, "0")}</span><div><b>${esc(cardioKindLabel(step.kind))}${step.repeat_index ? ` <em>${step.repeat_index}/${step.repeat_count}</em>` : ""}</b><small>${esc(cardioStepText(step))}</small>${step.notes ? `<small>${esc(step.notes)}</small>` : ""}</div></li>`).join("");
      return `<article class="mpv4-cardio-session type-${esc(session.session_type)} ${expanded ? "is-open" : ""}">
        <button type="button" class="mpv4-cardio-session-head" data-cardio-session="${esc(session.id)}" aria-expanded="${expanded}">
          <span class="mpv4-cardio-date"><b>${new Intl.DateTimeFormat("de-DE", { weekday: "short" }).format(cardioDate(session.scheduled_date))}</b><small>${dateFormat.format(cardioDate(session.scheduled_date))}</small></span>
          <span class="mpv4-cardio-session-copy"><b>${esc(session.title || session.session_type)}</b><small>${esc(cardioSessionPace(session))} /km</small></span>
          <span class="mpv4-cardio-session-numbers"><b>${session.duration_s ? Math.round(session.duration_s / 60) : "—"}<small> min</small></b><small>${session.distance_m ? (session.distance_m / 1000).toFixed(1) : "—"} km</small></span>
        </button>
        ${expanded ? `<div class="mpv4-cardio-session-detail"><div class="mpv4-cardio-loadline"><span>AUF DER UHR <b>PACE</b></span><span>${esc(cardioWorkoutSummary(steps))}</span></div>${cardioWorkoutChart(session)}<ol>${stepRows || "<li>Keine Schritte hinterlegt</li>"}</ol>${session.notes ? `<p>${esc(session.notes)}</p>` : ""}</div>` : ""}
      </article>`;
    }).join("");

    host.innerHTML = `<section class="mpv4-cardio-hero">
      <div class="mpv4-cardio-hero-head"><span>CARDIO · ${esc(plan.title || "Ausdauerplan")}</span><span>${daysToGoal} TAGE</span></div>
      <div class="mpv4-cardio-target"><div><strong>${cardioClock(event.target_time_s)}</strong><small>${event.distance_m ? `${event.distance_m / 1000} km` : esc(event.title || "Ziel")} · ${longDate.format(cardioDate(event.event_date))}</small></div><div><strong>${cardioPace(event.goal_pace_s_per_km)}</strong><small>Zielpace /km</small></div></div>
      <div class="mpv4-cardio-route"><div class="mpv4-cardio-route-track">${phaseSegments}<i style="--route-progress:${progress}%"></i></div><div><b>${esc(phaseState)}</b><span>${activePhase ? `${Math.round(progress)} % bis zum Ziel` : nextPhase ? `Als Nächstes: ${esc(nextPhase.name)}` : "Saison beendet"}</span></div></div>
    </section>
    <section class="mpv4-sheet mpv4-cardio-week">
      <div class="mpv4-sheet-head"><div><div class="mpv4-sheet-kicker">TRAININGSWOCHE</div><div class="mpv4-sheet-title">${dateFormat.format(weekStart)} – ${dateFormat.format(weekEnd)}</div></div><span class="mpv4-mode-chip">${weekSessions.length} Läufe</span></div>
      <div class="mpv4-cardio-week-metrics"><span><b>${((selectedSummary.distance_m || 0) / 1000).toFixed(1)}</b><small>km</small></span><span><b>${Math.round((selectedSummary.duration_s || 0) / 60)}</b><small>min</small></span><span><b>${cardioPace(selectedAvgPace)}</b><small>Ø Pace</small></span></div>
      <div class="mpv4-cardio-chart" aria-label="Distanz der umliegenden Wochen">${chart}</div>
      <div class="mpv4-cardio-session-list">${sessionRows || '<div class="mpv4-empty">In dieser Woche sind keine Läufe geplant.</div>'}</div>
    </section>`;

    host.querySelectorAll("[data-cardio-session]").forEach((button) => button.addEventListener("click", () => {
      state.expandedCardioSession = state.expandedCardioSession === button.dataset.cardioSession ? null : button.dataset.cardioSession;
      render();
    }));
    return host;
  }

  function buildGymExportSection(model) {
    const card = document.createElement("section");
    card.className = "mpv4-card mpv4-export-card";
    card.innerHTML = `
      <div class="mpv4-export-title">Export DSL</div>
      <div class="mpv4-export-body">
        <textarea class="mpv4-export-output is-empty" rows="8" readonly placeholder="Export erscheint hier"></textarea>
        <div class="mpv4-export-actions">
          <button type="button" class="mpv4-export-btn">Export generieren</button>
          <button type="button" class="mpv4-export-btn">Copy</button>
        </div>
      </div>
    `;

    const output = card.querySelector(".mpv4-export-output");
    const [generateBtn, copyBtn] = card.querySelectorAll(".mpv4-export-actions .mpv4-export-btn");
    generateBtn?.addEventListener("click", async () => {
      try {
        const res = await postJSON("/api/gym_plans/export_dsl", {
          plan_json: model.plan,
          rules_json: model.rules,
          blocks_json: model.blocks,
        });
        output.value = String(res?.text || "");
        output.classList.toggle("is-empty", !output.value.trim());
      } catch (_err) {
        output.value = "Export fehlgeschlagen.";
        output.classList.remove("is-empty");
      }
    });
    copyBtn?.addEventListener("click", async () => {
      await copyToClipboard(output.value);
      copyBtn.textContent = "Copied";
      setTimeout(() => { copyBtn.textContent = "Copy"; }, 900);
    });
    return card;
  }

  function buildGymCoachCard() {
    const card = document.createElement("section");
    card.className = "mpv4-card gym-coach-card";
    card.innerHTML = `
      <div class="panel-header">
        <div>
          <div class="title">Coach</div>
          <div class="sub">Volumen & Balance</div>
        </div>
      </div>
      <div class="gym-coach-summary"></div>
      <div class="gym-coach-warning-details">
        <button type="button" class="gym-coach-warning-title is-collapsed" aria-expanded="false">
          <span>Warnungen</span>
          <span class="gym-coach-warning-arrow" aria-hidden="true">▾</span>
        </button>
        <div class="gym-coach-warnings" hidden style="display:none"></div>
      </div>
    `;
    const toggle = card.querySelector(".gym-coach-warning-title");
    const warnings = card.querySelector(".gym-coach-warnings");
    toggle?.addEventListener("click", () => {
      const isOpen = toggle.getAttribute("aria-expanded") === "true";
      toggle.setAttribute("aria-expanded", isOpen ? "false" : "true");
      toggle.classList.toggle("is-collapsed", isOpen);
      if (warnings) {
        warnings.hidden = isOpen;
        warnings.style.display = isOpen ? "none" : "";
      }
    });
    return card;
  }

  function renderCoachIntoCard(card, coachData) {
    const summaryEl = card.querySelector(".gym-coach-summary");
    const warningsEl = card.querySelector(".gym-coach-warnings");
    if (!summaryEl || !warningsEl) return;
    summaryEl.innerHTML = "";
    warningsEl.innerHTML = "";

    const muscleSets = Array.isArray(coachData?.muscle_sets) ? coachData.muscle_sets : [];
    if (!muscleSets.length) {
      const row = document.createElement("div");
      row.className = "gym-coach-row";
      row.textContent = "Keine Sets für diese Woche.";
      summaryEl.appendChild(row);
    } else {
      muscleSets.forEach((m) => {
        const row = document.createElement("div");
        row.className = "gym-coach-row";
        const sets = Number.parseFloat(m.sets || 0) || 0;
        const setsRounded = Math.round(sets);
        const targetMin = Number.parseFloat(m.target_min || 0) || 0;
        const targetMax = Number.parseFloat(m.target_max || 0) || 0;
        const minRounded = Math.round(targetMin);
        const maxRounded = Math.round(targetMax);
        const warnMax = Number.parseFloat(m.warn_max ?? (targetMax + 2)) || (targetMax + 2);
        const domainMax = Math.max(warnMax, targetMax, setsRounded, 1);
        const toPct = (v) => Math.max(0, Math.min(100, (v / domainMax) * 100));
        const greenStart = toPct(targetMin);
        const greenEnd = toPct(targetMax);
        const marker = toPct(setsRounded);

        row.innerHTML = `
          <div class="gym-coach-row-head">
            <span>${esc(m.label || m.muscle || m.key || "Muskel")}</span>
            <span class="gym-coach-row-sets">${setsRounded} Sets</span>
          </div>
          <div class="gym-coach-bar-scale">
            <div class="gym-coach-bar-range">
              <div class="gym-coach-bar-zones" style="--green-start:${greenStart}%;--green-end:${greenEnd}%"></div>
              <span class="gym-coach-bar-marker" style="left:${marker}%"></span>
            </div>
            <div class="gym-coach-bar-labels">
              <span class="gym-coach-bar-label" style="left:${greenStart}%">${minRounded}</span>
              <span class="gym-coach-bar-label" style="left:${greenEnd}%">${maxRounded}</span>
            </div>
          </div>
        `;
        summaryEl.appendChild(row);
      });
    }

    const warnings = Array.isArray(coachData?.warnings) ? coachData.warnings : [];
    if (!warnings.length) {
      const row = document.createElement("div");
      row.className = "gym-warning-row";
      row.style.color = "var(--text-muted)";
      row.textContent = "Keine Warnungen.";
      warningsEl.appendChild(row);
    } else {
      warnings.forEach((w) => {
        const row = document.createElement("div");
        row.className = "gym-warning-row";
        row.textContent = `${w.kind}: ${w.message}`;
        warningsEl.appendChild(row);
      });
    }
  }

  async function hydrateGymCoachCard(card, model, weekNum) {
    const summaryEl = card.querySelector(".gym-coach-summary");
    const warningsEl = card.querySelector(".gym-coach-warnings");
    if (summaryEl) summaryEl.innerHTML = '<div class="gym-coach-row">Lade Coach…</div>';
    if (warningsEl) warningsEl.innerHTML = "";
    try {
      const data = await postJSON("/api/gym_plans/coach_summary", {
        plan_json: model.plan,
        blocks_json: model.blocks,
        rules_json: model.rules,
        week: `W${weekNum}`,
      });
      renderCoachIntoCard(card, data);
    } catch (_err) {
      if (summaryEl) summaryEl.innerHTML = '<div class="gym-coach-row">Coach konnte nicht geladen werden.</div>';
      if (warningsEl) warningsEl.innerHTML = '<div class="gym-warning-row">Fehler beim Laden.</div>';
    }
  }

  function buildNutritionExportSection(model) {
    const root = document.createElement("div");
    root.className = "mpv4-nutri-export-wrap";
    const templateId = model?.week_template?.id ? Number(model.week_template.id) : null;
    const qs = Number.isFinite(templateId) ? `?template_id=${encodeURIComponent(String(templateId))}` : "";

    function makeDrawer(title, endpoint, transform) {
      const card = document.createElement("section");
      card.className = "mpv4-card mpv4-export-card";
      card.innerHTML = `
        <div class="mpv4-export-title">${esc(title)}</div>
        <div class="mpv4-export-body">
          <pre class="mpv4-export-pre is-empty"></pre>
          <div class="mpv4-export-actions">
            <button type="button" class="mpv4-export-btn">Erstellen</button>
            <button type="button" class="mpv4-export-btn">Copy</button>
          </div>
        </div>
      `;
      const output = card.querySelector(".mpv4-export-pre");
      const [generateBtn, copyBtn] = card.querySelectorAll(".mpv4-export-actions .mpv4-export-btn");

      generateBtn?.addEventListener("click", async () => {
        try {
          const data = await fetchJSON(`${endpoint}${qs}`);
          if (data?.ok === false) throw new Error(String(data?.error || "export_failed"));
          output.textContent = transform(data);
          output.classList.toggle("is-empty", !String(output.textContent || "").trim());
        } catch (_err) {
          output.textContent = "Export fehlgeschlagen.";
          output.classList.remove("is-empty");
        }
      });

      copyBtn?.addEventListener("click", async () => {
        await copyToClipboard(output.textContent || "");
        copyBtn.textContent = "Copied";
        setTimeout(() => { copyBtn.textContent = "Copy"; }, 900);
      });

      return card;
    }

    const shopping = makeDrawer("Shopping List", "/api/nutrition/export/shopping_list", (data) => {
      const list = Array.isArray(data?.shopping_list) ? data.shopping_list : [];
      return list.map((item) => item.display_text || `${Math.round(Number(item.total_grams) || 0)} g ${item.name || ""}`.trim()).join("\n");
    });

    const checklist = makeDrawer("Checklist", "/api/nutrition/export/checklist", (data) => {
      const lines = [];
      const days = Array.isArray(data?.checklist) ? data.checklist : [];
      days.forEach((day) => {
        lines.push(`## ${day.label || "Tag"}`);
        (day.slots || []).forEach((slot) => {
          const timeText = String(slot.time_text || "").trim();
          const prefix = timeText ? `[${timeText}] ` : "";
          lines.push(`- ${prefix}${slot.slot_title || "Meal"}: ${slot.meal_title || ""} (${slot.servings || 1}x)`);
          (slot.ingredients || []).forEach((ingredient) => {
            lines.push(`  - ${ingredient.line || ""}`);
          });
        });
        lines.push("");
      });
      return lines.join("\n");
    });

    root.appendChild(shopping);
    root.appendChild(checklist);
    return root;
  }

  function clearLongPressTimer() {
    if (state.longPressTimer) {
      clearTimeout(state.longPressTimer);
      state.longPressTimer = null;
    }
  }

  function closePlanPicker() {
    if (!state.planPicker) return;
    state.planPicker.remove();
    state.planPicker = null;
  }

  async function selectGymPlan(planId) {
    const id = Number(planId);
    if (!Number.isFinite(id)) return;
    state.selectedGymPlanId = id;
    localStorage.setItem("planning_mobile_selected_gym_plan_v4", String(id));
    state.gymData = await loadGymPlanById(id);
    if (state.gymData) {
      state.gymWeek = clamp(state.gymData.currentWeek, 1, state.gymData.totalWeeks);
    }
    render();
  }

  async function openPlanPicker() {
    if (!state.gymPlans.length) {
      await loadGymPlans();
      if (!state.gymPlans.length) return;
    }
    closePlanPicker();

    const backdrop = document.createElement("div");
    backdrop.className = "mpv4-plan-picker-backdrop";
    backdrop.innerHTML = `
      <div class="mpv4-plan-picker" role="dialog" aria-modal="true" aria-label="Gym Plan wählen">
        <div class="mpv4-plan-picker-head">
          <div class="mpv4-plan-picker-title">Plan wählen</div>
          <button type="button" class="mpv4-plan-picker-close" data-role="close">✕</button>
        </div>
        <div class="mpv4-plan-picker-list"></div>
      </div>
    `;

    const list = backdrop.querySelector(".mpv4-plan-picker-list");
    state.gymPlans.forEach((plan) => {
      const id = Number(plan.id);
      if (!Number.isFinite(id)) return;
      const row = document.createElement("button");
      row.type = "button";
      row.className = `mpv4-plan-picker-row${id === state.selectedGymPlanId ? " is-selected" : ""}`;
      row.innerHTML = `
        <span class="mpv4-plan-picker-name">${esc(plan.name || plan.title || `Plan ${id}`)}</span>
        <span class="mpv4-plan-picker-badge">${plan.is_active ? "Aktiv" : "Plan"}</span>
      `;
      row.addEventListener("click", async () => {
        closePlanPicker();
        await selectGymPlan(id);
      });
      list.appendChild(row);
    });

    backdrop.addEventListener("click", (ev) => {
      if (ev.target === backdrop || ev.target.closest("[data-role='close']")) closePlanPicker();
    });

    document.body.appendChild(backdrop);
    state.planPicker = backdrop;
  }

  function bindGymPlanLongPress() {
    const targetEl = els.gymSection.querySelector(".mpv4-sheet-head");
    if (!targetEl) return;

    const start = () => {
      clearLongPressTimer();
      state.longPressTimer = setTimeout(() => {
        state.longPressTimer = null;
        openPlanPicker().catch(() => {});
      }, 420);
    };

    const end = () => clearLongPressTimer();
    targetEl.addEventListener("click", () => { openPlanPicker().catch(() => {}); });
    targetEl.addEventListener("touchstart", start, { passive: true });
    targetEl.addEventListener("touchend", end, { passive: true });
    targetEl.addEventListener("touchmove", end, { passive: true });
    targetEl.addEventListener("touchcancel", end, { passive: true });
    targetEl.addEventListener("mousedown", start);
    targetEl.addEventListener("mouseup", end);
    targetEl.addEventListener("mouseleave", end);
    targetEl.addEventListener("contextmenu", (ev) => {
      ev.preventDefault();
      openPlanPicker().catch(() => {});
    });
    targetEl.addEventListener("selectstart", (ev) => ev.preventDefault());
  }

  function setVisibility(domain) {
    const showGym = domain === "gym";
    const showCardio = domain === "cardio";
    if (!showGym) closePlanPicker();
    els.gymSection.classList.toggle("is-hidden", !showGym);
    els.cardioSection.classList.toggle("is-hidden", !showCardio);
    els.nutritionSection.classList.toggle("is-hidden", domain !== "nutrition");
    const hasPeriodization = Boolean(state.gymData?.plan?.meta?.periodization_enabled);
    els.gymSwitch?.classList.toggle("is-hidden", (!showGym || !hasPeriodization) && !showCardio);
    if (els.pageTitle) {
      els.pageTitle.textContent = showGym ? "Training" : showCardio ? "Cardio" : "Ernährung";
      els.pageTitle.classList.toggle("is-nutrition", domain === "nutrition");
      els.pageTitle.classList.toggle("is-cardio", showCardio);
    }
    const next = showGym ? "Cardio" : showCardio ? "Ernährung" : "Training";
    els.domainToggle?.setAttribute("aria-label", `Zu ${next} wechseln`);
  }

  function showEmpty(show) {
    if (!els.empty) return;
    els.empty.style.display = show ? "block" : "none";
  }

  function render() {
    const hasGym = !!state.gymData;
    const hasCardio = !!state.cardioData;
    const hasNutrition = !!state.nutritionData;

    if (!hasGym && !hasCardio && !hasNutrition) {
      els.gymSection.innerHTML = "";
      els.cardioSection.innerHTML = "";
      els.nutritionSection.innerHTML = "";
      showEmpty(true);
      return;
    }

    const available = [hasGym && "gym", hasCardio && "cardio", hasNutrition && "nutrition"].filter(Boolean);
    if (!available.includes(state.tab)) state.tab = available[0];

    showEmpty(false);

    setVisibility(state.tab);

    els.gymSection.innerHTML = "";
    els.cardioSection.innerHTML = "";
    els.nutritionSection.innerHTML = "";

    if (hasGym) {
      const total = state.gymData.totalWeeks;
      state.gymWeek = clamp(state.gymWeek, 1, total);
      els.gymLabel.textContent = `Woche ${state.gymWeek}/${total}`;
      els.gymSection.appendChild(renderGymWeek(state.gymData, state.gymWeek));
      bindGymPlanLongPress();
    } else {
      els.gymLabel.textContent = "Woche —";
    }

    if (hasCardio) {
      const week = cardioMonday(state.cardioWeek || cardioInitialWeek(state.cardioData));
      state.cardioWeek = cardioIso(week);
      if (state.tab === "cardio") {
        const end = cardioAddDays(week, 6);
        const formatter = new Intl.DateTimeFormat("de-DE", { day: "numeric", month: "short" });
        els.gymLabel.textContent = `${formatter.format(week)} – ${formatter.format(end)}`;
      }
      els.cardioSection.appendChild(renderCardioRead(state.cardioData));
      const workoutCanvas = els.cardioSection.querySelector("[data-mobile-workout-chart]");
      if (workoutCanvas && window.LivaWorkoutCharts) {
        const session = state.cardioData.sessions.find((item) => String(item.id) === workoutCanvas.dataset.mobileWorkoutChart);
        if (session) window.LivaWorkoutCharts.mount(workoutCanvas, cardioExecutionSteps(session), "pace");
      }
    }

    if (hasNutrition) {
      state.nutritionDay = clamp(state.nutritionDay, 0, 6);
      els.nutritionSection.appendChild(renderNutritionDay(state.nutritionData, state.nutritionDay));
    } else {
    }

    localStorage.setItem(STORAGE.tab, state.tab);
    localStorage.setItem(STORAGE.gymWeek, String(state.gymWeek));
    localStorage.setItem(STORAGE.nutritionDay, String(state.nutritionDay));
    localStorage.setItem(STORAGE.cardioWeek, state.cardioWeek);
  }

  function bind() {
    els.domainToggle?.addEventListener("click", () => {
      const order = ["gym", "cardio", "nutrition"];
      const next = order[(order.indexOf(state.tab) + 1) % order.length];
      const desktopButton = document.querySelector(`[data-planning-tab="${next}"]`);
      if (desktopButton && !desktopButton.disabled) desktopButton.click();
      else {
        state.tab = next;
        render();
      }
    });

    els.gymPrev?.addEventListener("click", () => {
      if (state.tab === "cardio" && state.cardioData) {
        state.cardioWeek = cardioIso(cardioAddDays(cardioMonday(state.cardioWeek), -7));
        state.expandedCardioSession = null;
        render();
        return;
      }
      if (!state.gymData) return;
      state.gymWeek = state.gymWeek <= 1 ? state.gymData.totalWeeks : state.gymWeek - 1;
      render();
    });

    els.gymNext?.addEventListener("click", () => {
      if (state.tab === "cardio" && state.cardioData) {
        state.cardioWeek = cardioIso(cardioAddDays(cardioMonday(state.cardioWeek), 7));
        state.expandedCardioSession = null;
        render();
        return;
      }
      if (!state.gymData) return;
      state.gymWeek = state.gymWeek >= state.gymData.totalWeeks ? 1 : state.gymWeek + 1;
      render();
    });

  }

  async function loadGymPlans() {
    const list = await fetchJSON("/api/gym_plans?include_archived=0");
    state.gymPlans = Array.isArray(list?.plans) ? list.plans : [];
    if (!state.gymPlans.length) return null;

    const stored = Number.parseInt(localStorage.getItem("planning_mobile_selected_gym_plan_v4") || "", 10);
    const hasStored = Number.isFinite(stored) && state.gymPlans.some((plan) => Number(plan.id) === stored);
    const selected = hasStored
      ? state.gymPlans.find((plan) => Number(plan.id) === stored)
      : (state.gymPlans.find((plan) => plan.is_active) || state.gymPlans[0]);
    state.selectedGymPlanId = Number(selected?.id);
    return state.selectedGymPlanId || null;
  }

  async function loadGymPlanById(planId) {
    const id = Number(planId);
    if (!Number.isFinite(id)) return null;
    const selectedMeta = state.gymPlans.find((plan) => Number(plan.id) === id) || null;

    const data = await fetchJSON(`/api/gym_plans/${id}`);
    const plan = normalizePlanJson(data.plan_json);
    const rules = normalizeRules(data.rules_json, plan.weeks);
    const blocks = normalizeBlocks(data.blocks_json);

    const autoWeek = computeCycleWeek(plan.meta || {}, plan.weeks);
    const currentWeek = Math.max(1, Math.min(plan.weeks, autoWeek));

    return {
      title: data.title || data.name || plan.meta?.title || "Gym-Plan",
      focus: data.focus || plan.meta?.focus || "",
      isActive: !!selectedMeta?.is_active,
      totalWeeks: plan.weeks,
      currentWeek,
      plan,
      rules,
      blocks,
    };
  }

  async function loadNutrition() {
    try {
      const payload = await fetchJSON("/api/nutrition/plan/week");
      return payload?.week_template ? payload : null;
    } catch (_err) {
      return null;
    }
  }

  async function loadCardio() {
    try {
      const list = await fetchJSON("/api/endurance/plans");
      const plans = Array.isArray(list?.plans) ? list.plans : [];
      const selected = plans.find((plan) => plan.status === "active") || plans[0];
      if (!selected?.id) return null;
      const detail = await fetchJSON(`/api/endurance/plans/${encodeURIComponent(selected.id)}`);
      return detail?.plan || null;
    } catch (_err) {
      return null;
    }
  }

  async function init() {
    bind();
    window.addEventListener("planning:tab", (event) => {
      const domain = event?.detail?.tab;
      if (!["gym", "cardio", "nutrition"].includes(domain)) return;
      state.tab = domain;
      render();
    });
    const [selectedPlanId, cardioData, nutritionData] = await Promise.all([loadGymPlans(), loadCardio(), loadNutrition()]);
    state.gymData = selectedPlanId ? await loadGymPlanById(selectedPlanId) : null;
    state.cardioData = cardioData;
    state.nutritionData = nutritionData;

    state.nutritionDay = wrapIndex((new Date().getDay() + 6) % 7, 7);

    if (state.gymData) {
      state.currentGymWeek = state.gymData.currentWeek;
      state.gymWeek = clamp(state.gymData.currentWeek, 1, state.gymData.totalWeeks);
    } else {
      state.gymWeek = 1;
    }

    if (state.cardioData) state.cardioWeek = cardioIso(cardioInitialWeek(state.cardioData));

    render();
  }

  init().catch(() => {
    showEmpty(true);
  });
})();
