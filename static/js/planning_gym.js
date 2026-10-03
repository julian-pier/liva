(() => {
  const isMobile = window.LIVA?.isMobile
    ? window.LIVA.isMobile()
    : window.matchMedia("(max-width: 900px)").matches;
  if (isMobile) return;

  const root = document.querySelector('[data-gym-planning]');
  if (!root) return;

  const DAY_ORDER = ['Mo', 'Di', 'Mi', 'Do', 'Fr', 'Sa', 'So'];
  const DEFAULT_ROLLING_WEEK_PATTERN = { Mo: 'train', Di: 'optional', Mi: 'train', Do: 'rest', Fr: 'train', Sa: 'optional', So: 'rest' };
  const ENDURANCE_EVENT_KINDS = new Set(['run', 'cardio', 'ergo', 'bike', 'ergometer', 'row', 'swim']);
  const ALL_EVENT_KINDS = new Set(['gym', 'rest', ...ENDURANCE_EVENT_KINDS]);
  const SUGGESTIONS = Array.isArray(window.exerciseSuggestions) ? window.exerciseSuggestions : [];

  const els = {
    planList: document.getElementById('gym-plan-list'),
    planSearch: document.getElementById('gym-plan-search'),
    newBtn: document.getElementById('gym-new-plan-btn'),

    title: document.getElementById('gym-plan-title'),
    focus: document.getElementById('gym-plan-focus'),
    saveBtn: document.getElementById('gym-save-btn'),
    statGym: document.getElementById('gym-plan-stat-gym'),
    statRuns: document.getElementById('gym-plan-stat-runs'),
    statSets: document.getElementById('gym-plan-stat-sets'),
    statRpe: document.getElementById('gym-plan-stat-rpe'),
    dayGrid: document.getElementById('gym-day-grid'),
    sequenceList: document.getElementById('gym-sequence-list'),
    modeFixedBtn: document.getElementById('gym-mode-fixed-btn'),
    modeRollingBtn: document.getElementById('gym-mode-rolling-btn'),
    modeSubline: document.getElementById('gym-mode-subline'),
    modeBody: document.getElementById('gym-plan-mode-body'),

    coachSummary: document.getElementById('gym-coach-summary'),
    coachSubline: document.getElementById('gym-coach-subline'),
    coachMeta: document.getElementById('gym-coach-meta'),
    coachWarnings: document.getElementById('gym-coach-warnings'),
    coachWarningsToggle: document.getElementById('gym-coach-warning-toggle'),
    cycleStatus: document.getElementById('gym-cycle-status'),
    cycleStartDate: document.getElementById('gym-cycle-start-date'),
    cycleTotalWeeks: document.getElementById('gym-cycle-total-weeks'),
    cycleBlockLength: document.getElementById('gym-cycle-block-length'),
    cycleWeekOverride: document.getElementById('gym-cycle-week-override'),
    cycleLoopEnabled: document.getElementById('gym-cycle-loop-enabled'),
    cycleSetToday: document.getElementById('gym-cycle-set-today'),
    cycleClearOverride: document.getElementById('gym-cycle-clear-override'),

    rulesList: document.getElementById('gym-rules-list'),
    periodizationEnabled: document.getElementById('gym-periodization-enabled'),
    periodizationLabel: document.getElementById('gym-periodization-label'),

    dslText: document.getElementById('gym-dsl-text'),
    dslExportText: document.getElementById('gym-dsl-export-text'),
    parseBtn: document.getElementById('gym-parse-btn'),
    applyParseBtn: document.getElementById('gym-apply-parse-btn'),
    exportDslBtn: document.getElementById('gym-export-dsl-btn'),
    copyDslBtn: document.getElementById('gym-copy-dsl-btn'),
    dslSegImport: document.getElementById('gymDslSegImport'),
    dslSegExport: document.getElementById('gymDslSegExport'),
    dslPaneImport: root.querySelector('[data-gym-dsl-pane="import"]'),
    dslPaneExport: root.querySelector('[data-gym-dsl-pane="export"]'),
    parsePreview: document.getElementById('gym-parse-preview'),
    parseWarnings: document.getElementById('gym-parse-warnings'),
    warningCount: document.getElementById('gym-warning-count'),

    blockList: document.getElementById('gym-block-list'),
    newBlockBtn: document.getElementById('gym-new-block-btn'),
  };

  const state = {
    plans: [],
    selectedPlanId: null,
    selectedWeek: 'W1',
    planMeta: { name: '', focus: '' },
    planJson: null,
    rulesJson: {},
    blocksJson: {},
    dirty: false,
    isSaving: false,
    updatedAt: null,
    parseResult: null,
    parseWarnings: [],
    coach: null,
    coachTimer: null,
    openMenuPlanId: null,
    openMenuAnchor: null,
    dslMode: 'import',
    coachWarningsCollapsed: true,
    draggingEvent: null,
    undoStack: [],
  };

  const uid = () => (crypto.randomUUID ? crypto.randomUUID().slice(0, 10) : Math.random().toString(16).slice(2, 12));

  function autosizeTitleField() {
    if (!els.title) return;
    const computed = window.getComputedStyle(els.title);
    const lineHeight = Number.parseFloat(computed.lineHeight) || 27;
    const maxRows = els.title.matches(':focus, :focus-visible') ? 12 : 2;
    const maxHeight = Math.ceil(lineHeight * maxRows);
    els.title.style.height = '0px';
    const nextHeight = Math.max(Math.ceil(lineHeight), Math.min(maxHeight, els.title.scrollHeight));
    els.title.style.height = `${nextHeight}px`;
  }

  function autosizeFocusField() {
    if (!els.focus) return;
    const computed = window.getComputedStyle(els.focus);
    const lineHeight = Number.parseFloat(computed.lineHeight) || 19;
    const singleRowHeight = Math.ceil(lineHeight);
    els.focus.style.height = '0px';
    const nextHeight = Math.max(singleRowHeight, els.focus.scrollHeight);
    els.focus.style.height = `${nextHeight}px`;
  }

  const defaultPlanJson = () => ({
    meta: { title: 'Neuer Gym-Plan', focus: '', status: 'draft', schema_version: 2, mode: 'fixed_week', periodization_enabled: false, rolling_week_pattern: { ...DEFAULT_ROLLING_WEEK_PATTERN }, updated_at: null },
    weeks: 8,
    sequence: [],
    base_week: Object.fromEntries(DAY_ORDER.map((d) => [d, []])),
    days: DAY_ORDER.map((day) => ({ day, events: [] })),
  });

  const defaultRulesJson = (weeks = 8) => {
    const maxWeeks = Math.max(1, Math.min(52, Number.parseInt(weeks || 8, 10) || 8));
    const out = {};
    for (let i = 1; i <= maxWeeks; i += 1) {
      const w = `W${i}`;
      const deload = i % 4 === 0;
      out[w] = {
        rpe_cap: deload ? 7 : 9,
        strength_factor: 1,
        run_factor: 1,
        cardio_factor: 1,
        deload,
        tag: deload ? 'Deload' : 'Build',
      };
    }
    return out;
  };

  const clone = (obj) => JSON.parse(JSON.stringify(obj));

  function normalizeRollingWeekPattern(src) {
    const source = src && typeof src === 'object' ? src : {};
    const out = {};
    DAY_ORDER.forEach((day) => {
      const raw = String(source[day] || DEFAULT_ROLLING_WEEK_PATTERN[day]).trim().toLowerCase();
      out[day] = ['train', 'optional', 'rest'].includes(raw) ? raw : DEFAULT_ROLLING_WEEK_PATTERN[day];
    });
    return out;
  }

  function normalizedCardioMode(value) {
    const raw = String(value || '').trim().toLowerCase();
    if (raw === 'ergometer') return 'ergo';
    if (ENDURANCE_EVENT_KINDS.has(raw)) return raw === 'cardio' ? 'cardio' : raw;
    return '';
  }

  function normalizeEventKind(kind, mode) {
    const eventMode = normalizedCardioMode(mode || kind);
    if (eventMode) return 'cardio';
    const raw = String(kind || '').trim().toLowerCase();
    return ALL_EVENT_KINDS.has(raw) ? raw : 'gym';
  }

  function eventCardioMode(event) {
    return normalizedCardioMode(event?.mode || event?.kind);
  }

  function snapshotState() {
    return {
      selectedWeek: state.selectedWeek,
      planMeta: clone(state.planMeta || {}),
      planJson: clone(state.planJson || defaultPlanJson()),
      rulesJson: clone(state.rulesJson || {}),
      blocksJson: clone(state.blocksJson || {}),
    };
  }

  function pushUndoSnapshot(_label = '') {
    if (!state.planJson) return;
    state.undoStack.push(snapshotState());
    if (state.undoStack.length > 100) state.undoStack.shift();
  }

  function restoreSnapshot(snapshot) {
    if (!snapshot || typeof snapshot !== 'object') return;
    state.selectedWeek = snapshot.selectedWeek || 'W1';
    state.planMeta = snapshot.planMeta && typeof snapshot.planMeta === 'object' ? snapshot.planMeta : { name: '', focus: '' };
    state.planJson = normalizePlanJson(snapshot.planJson);
    state.rulesJson = normalizeRulesJson(snapshot.rulesJson, state.planJson.weeks);
    state.blocksJson = normalizeBlocksJson(snapshot.blocksJson);
    sanitizeSelectedWeek();
    setDirty(true);
    renderAll();
  }

  function undoLastAction() {
    const prev = state.undoStack.pop();
    if (!prev) return;
    restoreSnapshot(prev);
  }

  function normalizePlanJson(src) {
    const base = defaultPlanJson();
    const obj = src && typeof src === 'object' ? clone(src) : base;
    if (!obj.meta || typeof obj.meta !== 'object') obj.meta = {};
    obj.meta.title = obj.meta.title || base.meta.title;
    obj.meta.focus = obj.meta.focus || '';
    obj.meta.mode = String(obj.meta.mode || 'fixed_week').toLowerCase() === 'rolling_sequence' ? 'rolling_sequence' : 'fixed_week';
    obj.meta.periodization_enabled = !!obj.meta.periodization_enabled;
    obj.meta.rolling_week_pattern = normalizeRollingWeekPattern(obj.meta.rolling_week_pattern);
    const weeks = Math.max(1, Math.min(52, Number.parseInt(obj.weeks || 8, 10) || 8));
    obj.weeks = weeks;

    const map = new Map();
    if (obj.base_week && typeof obj.base_week === 'object') {
      DAY_ORDER.forEach((day) => {
        const events = Array.isArray(obj.base_week[day]) ? obj.base_week[day] : [];
        map.set(day, { day, events });
      });
    } else if (Array.isArray(obj.days)) {
      obj.days.forEach((d) => map.set(d?.day, d));
    }
    obj.days = DAY_ORDER.map((day) => {
      const d = map.get(day);
      return {
        day,
        events: Array.isArray(d?.events)
          ? d.events.map((e) => ({
              id: e?.id || uid(),
              kind: normalizeEventKind(e?.kind, e?.mode),
              mode: eventCardioMode(e) || null,
              time: e?.time || null,
              title: e?.title || '',
              frequency: Math.max(1, Number.parseInt(e?.frequency || 1, 10) || 1),
              items: Array.isArray(e?.items) ? e.items.map((it) => ({ ...it, id: it?.id || uid() })) : [],
            }))
          : [],
      };
    });
    obj.base_week = Object.fromEntries(obj.days.map((d) => [d.day, d.events]));
    obj.sequence = Array.isArray(obj.sequence)
      ? obj.sequence.map((e) => ({
          id: e?.id || uid(),
          kind: normalizeEventKind(e?.kind, e?.mode),
          mode: eventCardioMode(e) || null,
          time: e?.time || null,
          title: e?.title || '',
          frequency: Math.max(1, Number.parseInt(e?.frequency || 1, 10) || 1),
          items: Array.isArray(e?.items) ? e.items.map((it) => ({ ...it, id: it?.id || uid() })) : [],
          notes: e?.notes || e?.note || '',
        }))
      : [];
    obj.meta.total_weeks = Math.max(1, Number.parseInt(obj.meta.total_weeks || obj.weeks || 8, 10) || obj.weeks || 8);
    obj.meta.block_length = Math.max(1, Number.parseInt(obj.meta.block_length || 4, 10) || 4);
    obj.meta.loop_cycle = !!obj.meta.loop_cycle;
    if (obj.meta.week_override !== null && obj.meta.week_override !== undefined && String(obj.meta.week_override).trim() !== '') {
      obj.meta.week_override = Math.max(1, Number.parseInt(obj.meta.week_override, 10) || 1);
    } else {
      obj.meta.week_override = null;
    }
    return obj;
  }

  function todayIso() {
    const d = new Date();
    const m = String(d.getMonth() + 1).padStart(2, '0');
    const day = String(d.getDate()).padStart(2, '0');
    return `${d.getFullYear()}-${m}-${day}`;
  }

  function parseIsoDateOnly(iso) {
    const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(String(iso || "").trim());
    if (!m) return null;
    const y = Number.parseInt(m[1], 10);
    const mo = Number.parseInt(m[2], 10) - 1;
    const d = Number.parseInt(m[3], 10);
    const dt = new Date(y, mo, d);
    if (Number.isNaN(dt.getTime())) return null;
    dt.setHours(0, 0, 0, 0);
    return dt;
  }

  function weekCurrentFromStartDate(startIso, refDate) {
    const start = parseIsoDateOnly(startIso);
    if (!start) return 1;
    const today = new Date(refDate);
    today.setHours(0, 0, 0, 0);
    // JS weekday: So=0..Sa=6 -> Mo-index: 0..6
    const startMonIdx = (start.getDay() + 6) % 7;
    const week1Start = new Date(start);
    if (startMonIdx !== 0) week1Start.setDate(week1Start.getDate() + (7 - startMonIdx));
    if (today < week1Start) return 1;
    const diffDays = Math.floor((today.getTime() - week1Start.getTime()) / 86400000);
    return Math.floor(diffDays / 7) + 1;
  }

  function ensureCycleMeta() {
    if (!state.planJson || typeof state.planJson !== 'object') return;
    state.planJson.meta = state.planJson.meta || {};
    if (!state.planJson.meta.start_date) state.planJson.meta.start_date = todayIso();
    state.planJson.meta.total_weeks = Math.max(1, Number.parseInt(state.planJson.meta.total_weeks || state.planJson.weeks || 8, 10) || state.planJson.weeks || 8);
    state.planJson.meta.block_length = Math.max(1, Number.parseInt(state.planJson.meta.block_length || 4, 10) || 4);
    state.planJson.meta.loop_cycle = !!state.planJson.meta.loop_cycle;
    if (state.planJson.meta.week_override !== null && state.planJson.meta.week_override !== undefined && String(state.planJson.meta.week_override).trim() !== '') {
      state.planJson.meta.week_override = Math.max(1, Number.parseInt(state.planJson.meta.week_override, 10) || 1);
    } else {
      state.planJson.meta.week_override = null;
    }
  }

  function cycleStatus() {
    ensureCycleMeta();
    const meta = state.planJson?.meta || {};
    const total = Math.max(1, Number.parseInt(meta.total_weeks || state.planJson?.weeks || 8, 10) || 8);
    const loop = !!meta.loop_cycle;
    let current = 1;
    if (meta.week_override !== null && meta.week_override !== undefined && String(meta.week_override).trim() !== '') {
      current = Math.max(1, Math.min(total, Number.parseInt(meta.week_override, 10) || 1));
    } else if (meta.start_date) {
      const elapsed = weekCurrentFromStartDate(meta.start_date, new Date());
      if (loop) current = ((Math.max(1, elapsed) - 1) % total) + 1;
      else current = Math.max(1, Math.min(total, elapsed));
    }
    const label = `W${current}`;
    const row = state.rulesJson?.[label] || {};
    let phase = 'BUILD';
    if (typeof row.tag === 'string' && row.tag.trim()) phase = row.tag.trim().toUpperCase();
    else if (row.deload) phase = 'DELOAD';
    else {
      const next = state.rulesJson?.[`W${Math.min(total, current + 1)}`] || {};
      if (current < total && next.deload) phase = 'OVERREACH';
    }
    return { current, total, phase, loop };
  }

  function renderCycleCard() {
    if (!state.planJson) return;
    ensureCycleMeta();
    const meta = state.planJson.meta || {};
    const status = cycleStatus();
    if (els.cycleStatus) els.cycleStatus.textContent = `Woche ${status.current}/${status.total} · ${status.phase}${status.loop ? ' · LOOP' : ''}`;
    if (els.cycleStartDate) els.cycleStartDate.value = meta.start_date || '';
    if (els.cycleTotalWeeks) els.cycleTotalWeeks.value = String(meta.total_weeks || state.planJson.weeks || 8);
    if (els.cycleBlockLength) els.cycleBlockLength.value = String(meta.block_length || 4);
    if (els.cycleWeekOverride) els.cycleWeekOverride.value = meta.week_override == null ? '' : String(meta.week_override);
    if (els.cycleLoopEnabled) els.cycleLoopEnabled.checked = !!meta.loop_cycle;
    if (!isPeriodizationEnabled() && els.cycleStatus) els.cycleStatus.textContent = `Woche ${status.current}/${status.total}`;
  }

  function syncBaseWeekFromDays(plan) {
    if (!plan || typeof plan !== 'object') return;
    const days = Array.isArray(plan.days) ? plan.days : [];
    plan.base_week = Object.fromEntries(
      DAY_ORDER.map((day) => {
        const found = days.find((d) => d.day === day);
        return [day, Array.isArray(found?.events) ? found.events : []];
      }),
    );
    if (!Array.isArray(plan.sequence)) plan.sequence = [];
  }

  function cloneEventForModeSwitch(event) {
    const cloned = clone(event || {});
    cloned.id = uid();
    cloned.items = Array.isArray(cloned.items)
      ? cloned.items.map((item) => ({ ...clone(item || {}), id: uid() }))
      : [];
    cloned.frequency = Math.max(1, Number.parseInt(cloned.frequency || 1, 10) || 1);
    return cloned;
  }

  function fixedWeekEventsToSequence(plan, opts = {}) {
    const includeRest = !!opts.includeRest;
    const out = [];
    const days = Array.isArray(plan?.days) ? plan.days : [];
    DAY_ORDER.forEach((dayName) => {
      const day = days.find((d) => d?.day === dayName);
      const events = Array.isArray(day?.events) ? day.events : [];
      events.forEach((event) => {
        const kind = String(event?.kind || '').toLowerCase();
        if (!includeRest && kind === 'rest') return;
        out.push(cloneEventForModeSwitch(event));
      });
    });
    return out;
  }

  function rollingSequenceToFixedWeekDays(plan, opts = {}) {
    const startIndex = Math.max(0, Math.min(6, Number.parseInt(opts.startIndex ?? 0, 10) || 0));
    const days = DAY_ORDER.map((day) => ({ day, events: [] }));
    const seq = Array.isArray(plan?.sequence) ? plan.sequence : [];
    seq.forEach((event, idx) => {
      const targetIdx = Math.min(6, startIndex + idx);
      days[targetIdx].events.push(cloneEventForModeSwitch(event));
    });
    return days;
  }

  function daysHaveEvents(plan) {
    return (Array.isArray(plan?.days) ? plan.days : []).some((day) => Array.isArray(day?.events) && day.events.length > 0);
  }

  function sequenceHasEvents(plan) {
    return Array.isArray(plan?.sequence) && plan.sequence.length > 0;
  }

  function setPlanModeWithMigration(nextMode) {
    if (!state.planJson) return;
    const current = planMode();
    const target = nextMode === 'rolling_sequence' ? 'rolling_sequence' : 'fixed_week';
    if (current === target) return;

    pushUndoSnapshot('switch_plan_mode');
    if (!state.planJson.meta || typeof state.planJson.meta !== 'object') state.planJson.meta = {};
    if (!Array.isArray(state.planJson.days)) state.planJson.days = DAY_ORDER.map((day) => ({ day, events: [] }));
    if (!Array.isArray(state.planJson.sequence)) state.planJson.sequence = [];

    if (target === 'rolling_sequence') {
      if (!sequenceHasEvents(state.planJson) && daysHaveEvents(state.planJson)) {
        const migrate = window.confirm('Fixe Woche in Rolling Rotation übernehmen? Rest-Tage werden dabei übersprungen.');
        if (migrate) state.planJson.sequence = fixedWeekEventsToSequence(state.planJson, { includeRest: false });
      }
      state.planJson.meta.mode = 'rolling_sequence';
    } else {
      if (!daysHaveEvents(state.planJson) && sequenceHasEvents(state.planJson)) {
        const migrate = window.confirm('Rolling Rotation auf fixe Woche ab Montag verteilen?');
        if (migrate) {
          state.planJson.days = rollingSequenceToFixedWeekDays(state.planJson, { startIndex: 0 });
        }
      }
      state.planJson.meta.mode = 'fixed_week';
      syncBaseWeekFromDays(state.planJson);
    }

    setDirty(true);
    renderAll();
  }

  function normalizeRulesJson(src, weeks) {
    const base = defaultRulesJson(weeks);
    if (!src || typeof src !== 'object') return base;
    const source = src.weeks && typeof src.weeks === 'object' ? src.weeks : src;
    const out = { ...base };
    Object.keys(base).forEach((w) => {
      const row = source[w];
      if (!row || typeof row !== 'object') return;
      out[w] = {
        ...out[w],
        rpe_cap: row.rpe_cap,
        strength_factor: row.strength_factor,
        run_factor: row.run_factor ?? row.cardio_factor,
        cardio_factor: row.cardio_factor ?? row.run_factor,
        deload: !!row.deload,
        tag: row.tag || out[w].tag,
      };
    });
    return out;
  }

  function normalizeBlocksJson(src) {
    const out = {};
    if (!src || typeof src !== 'object') return out;
    Object.entries(src).forEach(([name, entries]) => {
      if (!name || !Array.isArray(entries)) return;
      out[name] = entries
        .filter((e) => e && typeof e === 'object')
        .map((e) => ({
          id: e.id || uid(),
          day: DAY_ORDER.includes(e.day) ? e.day : 'Mo',
          title: String(e.title || '').trim(),
          frequency: Math.max(1, Number.parseInt(e.frequency || 1, 10) || 1),
          lines: Array.isArray(e.lines) ? e.lines.map((line) => String(line || '').trim()).filter(Boolean) : [],
        }));
    });
    return out;
  }

  function autoLinkRefBlocksInPlan() {
    if (!state.planJson || !state.blocksJson) return 0;
    let changed = 0;
    const blocks = state.blocksJson;
    const days = Array.isArray(state.planJson.days) ? state.planJson.days : [];
    days.forEach((day) => {
      const dayName = String(day?.day || '');
      (day?.events || []).forEach((event) => {
        if ((event?.kind || '').toLowerCase() !== 'gym') return;
        const items = Array.isArray(event.items) ? event.items : [];
        items.forEach((item, idx) => {
          if (!item || item.kind !== 'exercise') return;
          const name = String(item.name || '').trim();
          if (!name) return;
          const variation = String(item.variation || '').trim();
          let best = null;
          Object.entries(blocks).forEach(([blockName, entries]) => {
            if (!Array.isArray(entries)) return;
            entries.forEach((entry) => {
              if (!entry || String(entry.day || '') !== dayName) return;
              const title = String(entry.title || '').toLowerCase();
              if (!title.includes(name.toLowerCase())) return;
              if (variation && !title.includes(variation.toLowerCase())) return;
              const score = 1 + (variation ? 1 : 0);
              if (!best || score > best.score) {
                best = { score, blockName };
              }
            });
          });
          if (!best) return;
          items[idx] = {
            id: item.id || uid(),
            kind: 'ref_block',
            display_name: name,
            variation,
            block_name: best.blockName,
            block_day: dayName,
          };
          changed += 1;
        });
      });
    });
    if (changed > 0) syncBaseWeekFromDays(state.planJson);
    return changed;
  }

  async function api(url, options = {}) {
    const res = await fetch(url, {
      headers: { 'Content-Type': 'application/json', ...(options.headers || {}) },
      ...options,
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok || data.ok === false) throw new Error(data.detail || data.error || `HTTP ${res.status}`);
    return data;
  }

  function formatSavedAgo(iso) {
    if (!iso) return 'Gespeichert';
    let normalized = String(iso || '').trim();
    if (normalized && !/[zZ]$/.test(normalized) && !/[+-]\d{2}:\d{2}$/.test(normalized)) {
      // Backend stores UTC timestamps without timezone suffix.
      normalized = `${normalized}Z`;
    }
    const ts = Date.parse(normalized);
    if (Number.isNaN(ts)) return 'Gespeichert';
    const diff = Math.max(0, Date.now() - ts);
    if (diff < 60000) return 'Gespeichert · Gerade eben';
    const m = Math.floor(diff / 60000);
    if (m < 60) return `Gespeichert · vor ${m} min`;
    const h = Math.floor(m / 60);
    if (h < 24) return `Gespeichert · vor ${h} h`;
    const d = Math.floor(h / 24);
    if (d < 7) return `Gespeichert · vor ${d} d`;
    return `Gespeichert · ${new Date(ts).toLocaleString('de-DE', { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' })}`;
  }

  function setDirty(flag) {
    state.dirty = !!flag;
    renderSaveState();
    renderHeaderStats();
    requestCoachSummaryDebounced();
  }

  function renderSaveState() {
    if (!els.saveBtn) return;
    if (state.isSaving) {
      els.saveBtn.textContent = 'Speichert…';
      els.saveBtn.disabled = true;
      return;
    }
    if (state.dirty) {
      els.saveBtn.textContent = 'Speichern*';
      els.saveBtn.disabled = false;
      return;
    }
    els.saveBtn.textContent = formatSavedAgo(state.updatedAt);
    els.saveBtn.disabled = false;
  }

  function currentWeekCount() {
    return Math.max(1, Number.parseInt(state.planJson?.weeks || 8, 10) || 8);
  }

  function planMode() {
    return String(state.planJson?.meta?.mode || 'fixed_week') === 'rolling_sequence' ? 'rolling_sequence' : 'fixed_week';
  }

  function isPeriodizationEnabled() {
    return !!state.planJson?.meta?.periodization_enabled;
  }

  function rollingWeekPattern() {
    return normalizeRollingWeekPattern(state.planJson?.meta?.rolling_week_pattern);
  }

  function expectedTrainingDaysPerWeek() {
    const weights = { train: 1, optional: 0.5, rest: 0 };
    return DAY_ORDER.reduce((sum, day) => sum + (weights[rollingWeekPattern()[day]] ?? 0), 0);
  }

  function isTrainingSequenceEvent(event) {
    const kind = String(event?.kind || '').toLowerCase();
    return kind === 'gym' || kind === 'cardio' || ENDURANCE_EVENT_KINDS.has(kind);
  }

  function sequenceTrainingSlots() {
    const seq = Array.isArray(state.planJson?.sequence) ? state.planJson.sequence : [];
    return seq.filter((event) => isTrainingSequenceEvent(event) && !['rest', 'note'].includes(String(event?.kind || '').toLowerCase())).length;
  }

  function rollingRotationFactor() {
    const slots = sequenceTrainingSlots();
    if (slots <= 0) return 0;
    return expectedTrainingDaysPerWeek() / slots;
  }

  function defaultEventTitle(kind) {
    if (kind === 'rest') return 'Rest';
    if (kind === 'cardio') {
      return 'Cardio';
    }
    if (ENDURANCE_EVENT_KINDS.has(String(kind || '').toLowerCase())) {
      if (String(kind || '').toLowerCase() === 'ergo' || String(kind || '').toLowerCase() === 'ergometer') return 'Ergo';
      return 'Run';
    }
    return 'Session';
  }

  function eventKindLabel(kind, mode = '') {
    const k = String(kind || '').toLowerCase();
    const cardioMode = normalizedCardioMode(mode || kind);
    if (!k) return 'GYM';
    if (k === 'cardio') return cardioMode ? cardioMode.toUpperCase() : 'CARDIO';
    if (k === 'ergo') return 'ERGO';
    if (k === 'bike') return 'BIKE';
    if (k === 'row') return 'ROW';
    if (k === 'swim') return 'SWIM';
    if (k === 'run') return 'RUN';
    if (k === 'rest') return 'REST';
    if (k === 'gym') return 'GYM';
    return k.toUpperCase();
  }

  function parseKmFromText(text) {
    const raw = String(text || '').toLowerCase().replace(',', '.');
    if (!raw) return 0;
    const m = raw.match(/(\d+(?:\.\d+)?)\s*km\b/);
    if (!m) return 0;
    const val = Number.parseFloat(m[1]);
    return Number.isFinite(val) && val > 0 ? val : 0;
  }

  function formatCardioItem(item) {
    if (!item || typeof item !== 'object') return '—';
    const explicit = String(item.display || item.display_text || '').trim();
    if (explicit) return explicit;
    const durationRaw = item.duration_min ?? item.amount_value ?? item.amount;
    const duration = Number.parseFloat(durationRaw);
    const rawUnit = String(item.amount_unit || item.unit || '').trim();
    const unit = rawUnit || (Number.isFinite(duration) ? 'min' : '');
    const durationText = Number.isFinite(duration) && duration > 0
      ? `${Number.isInteger(duration) ? duration : duration.toFixed(1)} ${unit}`.trim()
      : '';
    const intensity = String(item.intensity || '').trim();
    const notes = String(item.notes || item.note || '').trim();
    const title = String(item.title || item.name || '').trim();
    const main = [durationText, intensity].filter(Boolean).join(' ').trim();
    if (main && notes) return `${main} @ ${notes}`;
    return main || notes || title || '—';
  }

  function formatCompactRpe(values) {
    const rpes = Array.isArray(values) ? values.map((v) => Number.parseFloat(v)).filter(Number.isFinite) : [];
    if (!rpes.length) return '';
    const top = Math.max(...rpes);
    const low = Math.min(...rpes);
    const fmt = (value) => (Number.isInteger(value) ? String(value) : value.toFixed(1).replace(/\.0$/, ''));
    return low === top ? fmt(top) : `${fmt(low)}/${fmt(top)}`;
  }

  function eventMetaStats(event) {
    const items = Array.isArray(event?.items) ? event.items : [];
    const exerciseItems = items.filter((item) => item?.kind === 'exercise');
    const exerciseCount = exerciseItems.length;
    const totalSets = exerciseItems.reduce((sum, item) => sum + (Number.parseInt(item?.sets || 0, 10) || 0), 0);
    const rpeValues = exerciseItems.flatMap((item) => (Array.isArray(item?.rpe_list) ? item.rpe_list : []))
      .map((v) => Number.parseFloat(v))
      .filter(Number.isFinite);
    const avgRpe = rpeValues.length ? (rpeValues.reduce((sum, value) => sum + value, 0) / rpeValues.length) : null;
    const cardioItems = items.filter((item) => item?.kind === 'cardio' || item?.kind === 'run_detail').length;
    return { exerciseCount, totalSets, avgRpe, cardioItems };
  }

  function headerStats() {
    const eventBuckets = planMode() === 'rolling_sequence'
      ? [
          { events: Array.isArray(state.planJson?.sequence) ? state.planJson.sequence : [] },
          ...(Array.isArray(state.planJson?.days) ? state.planJson.days : []),
        ]
      : (Array.isArray(state.planJson?.days) ? state.planJson.days : []);
    let gymSessions = 0;
    let runSessions = 0;
    let sets = 0;
    let km = 0;
    let rpeWeightedSum = 0;
    let rpeWeightedCount = 0;

    eventBuckets.forEach((day) => {
      const events = Array.isArray(day?.events) ? day.events : [];
      events.forEach((event) => {
        const kind = (event?.kind || '').toLowerCase();
        if (kind === 'cardio' || ENDURANCE_EVENT_KINDS.has(kind)) {
          runSessions += 1;
          km += parseKmFromText(event?.title || '');
          const items = Array.isArray(event?.items) ? event.items : [];
          items.forEach((item) => {
            if (item?.kind === 'run_detail') km += parseKmFromText(item?.text || '');
          });
          return;
        }
        if (kind !== 'gym') return;
        gymSessions += 1;
        const items = Array.isArray(event?.items) ? event.items : [];
        items.forEach((item) => {
          if (item?.kind !== 'exercise') return;
          const itemSets = Math.max(0, Number.parseInt(item?.sets || 0, 10) || 0);
          sets += itemSets;
          const rpeList = Array.isArray(item?.rpe_list) ? item.rpe_list.map((x) => Number.parseFloat(x)).filter(Number.isFinite) : [];
          if (!rpeList.length) return;
          const meanRpe = rpeList.reduce((acc, n) => acc + n, 0) / rpeList.length;
          const weight = Math.max(1, itemSets);
          rpeWeightedSum += meanRpe * weight;
          rpeWeightedCount += weight;
        });
      });
    });

    return {
      gymSessions,
      runSessions,
      sets,
      km,
      avgRpe: rpeWeightedCount > 0 ? rpeWeightedSum / rpeWeightedCount : null,
    };
  }

  function renderHeaderStats() {
    const stats = headerStats();
    if (els.statGym) els.statGym.textContent = String(stats.gymSessions);
    if (els.statRuns) els.statRuns.textContent = String(stats.runSessions);
    if (els.statSets) els.statSets.textContent = String(stats.sets);
    if (els.statRpe) els.statRpe.textContent = Number.isFinite(stats.avgRpe) ? stats.avgRpe.toFixed(1) : '-';
  }

  function sanitizeSelectedWeek() {
    const w = Number.parseInt(String(state.selectedWeek || 'W1').replace('W', ''), 10) || 1;
    const max = currentWeekCount();
    state.selectedWeek = `W${Math.max(1, Math.min(max, w))}`;
  }

  async function loadPlans() {
    const data = await api('/api/gym_plans?include_archived=1');
    state.plans = Array.isArray(data.plans) ? data.plans : [];
    if (!state.selectedPlanId && state.plans.length) {
      const active = state.plans.find((p) => p.is_active && !p.is_archived) || state.plans[0];
      state.selectedPlanId = active.id;
    }
    renderPlanList();
    if (state.selectedPlanId) await loadPlan(state.selectedPlanId);
  }

  async function loadPlan(planId) {
    state.openMenuPlanId = null;
    const data = await api(`/api/gym_plans/${planId}`);
    state.selectedPlanId = data.id;
    state.planJson = normalizePlanJson(data.plan_json);
    state.rulesJson = normalizeRulesJson(data.rules_json, state.planJson.weeks);
    if (data.rules_json && typeof data.rules_json === 'object' && typeof data.rules_json.active_week === 'string') {
      state.selectedWeek = data.rules_json.active_week;
    }
    state.blocksJson = normalizeBlocksJson(data.blocks_json);
    state.undoStack = [];
    autoLinkRefBlocksInPlan();
    state.planMeta = { name: data.title || data.name || '', focus: data.focus || '' };
    state.updatedAt = data.updated_at || null;
    sanitizeSelectedWeek();
    setDirty(false);
    // Keep library selection state in sync with currently opened plan.
    renderPlanList();
    renderAll();
  }

  function renderPlanList() {
    // The action menu is rendered at document level so it is not trapped
    // below the adjacent planning column's stacking context.
    document.querySelectorAll('[data-gym-plan-menu]').forEach((menu) => menu.remove());
    els.planList.innerHTML = '';
    const q = String(els.planSearch.value || '').trim().toLowerCase();
    const list = state.plans.filter((p) => {
      if (!q) return true;
      return `${p.name || p.title || ''} ${p.focus || ''}`.toLowerCase().includes(q);
    });

    list.forEach((plan) => {
      const card = document.createElement('div');
      card.className = 'gym-plan-item';
      if (String(plan.id) === String(state.selectedPlanId)) card.classList.add('is-selected');
      if (plan.is_active) card.classList.add('is-active');
      if (plan.is_archived) card.classList.add('is-archived');

      const main = document.createElement('button');
      main.type = 'button';
      main.className = 'gym-plan-main';
      const activeDot = plan.is_active ? '<span class="gym-plan-active-dot"></span>' : '';
      const cycleText = plan.is_active && plan?.cycle?.display ? String(plan.cycle.display) : '';
      main.innerHTML = `
        <div class="gym-plan-name">${activeDot}${escapeHtml(plan.name || plan.title || 'Gym Plan')}</div>
        <div class="gym-plan-sub">${escapeHtml(plan.focus || 'kein Fokus')} · ${escapeHtml(formatSavedAgo(plan.updated_at).replace('Gespeichert · ', ''))}</div>
        ${cycleText ? `<div class="gym-plan-cycle">${escapeHtml(cycleText)}</div>` : ''}
      `;
      main.addEventListener('click', () => loadPlan(plan.id));
      card.appendChild(main);

      const menuWrap = document.createElement('div');
      menuWrap.className = 'gym-plan-kebab';
      const menuBtn = document.createElement('button');
      menuBtn.type = 'button';
      menuBtn.className = 'btn-ghost';
      menuBtn.textContent = '⋯';
      menuBtn.addEventListener('click', (ev) => {
        ev.stopPropagation();
        if (state.openMenuPlanId === plan.id) {
          state.openMenuPlanId = null;
          state.openMenuAnchor = null;
        } else {
          const rect = ev.currentTarget.getBoundingClientRect();
          state.openMenuPlanId = plan.id;
          state.openMenuAnchor = {
            top: Math.round(rect.bottom + 6),
            left: Math.round(rect.right),
          };
        }
        renderPlanList();
      });
      menuWrap.appendChild(menuBtn);
      if (state.openMenuPlanId === plan.id) {
        const menu = document.createElement('div');
        menu.className = 'gym-plan-menu';
        menu.dataset.gymPlanMenu = String(plan.id);
        menu.setAttribute('role', 'menu');
        menu.hidden = false;
        if (state.openMenuAnchor) {
          menu.style.position = 'fixed';
          menu.style.top = `${state.openMenuAnchor.top}px`;
          menu.style.left = `${state.openMenuAnchor.left}px`;
          menu.style.right = 'auto';
        }
        const actions = [
          ...(plan.is_active ? [] : [
            { label: 'Aktivieren', fn: () => api(`/api/gym_plans/${plan.id}/activate`, { method: 'POST' }) },
          ]),
          {
            label: 'Umbenennen',
            fn: async () => {
              const next = window.prompt('Neuer Name:', plan.name || plan.title || 'Gym Plan');
              if (!next || !next.trim()) return;
              await api(`/api/gym_plans/${plan.id}`, { method: 'PUT', body: JSON.stringify({ name: next.trim() }) });
            },
          },
          { label: 'Duplizieren', fn: () => api(`/api/gym_plans/${plan.id}/duplicate`, { method: 'POST' }) },
          {
            label: plan.is_archived ? 'Wiederherstellen' : 'Archivieren',
            fn: () => api(`/api/gym_plans/${plan.id}/archive`, { method: 'POST', body: JSON.stringify({ archive: !plan.is_archived }) }),
          },
          {
            label: 'Löschen',
            fn: async () => {
              if (!window.confirm('Plan wirklich löschen?')) return;
              await api(`/api/gym_plans/${plan.id}`, { method: 'DELETE' });
            },
          },
        ];
        actions.forEach((entry) => {
          const b = document.createElement('button');
          b.type = 'button';
          b.className = 'btn-ghost';
          b.setAttribute('role', 'menuitem');
          b.textContent = entry.label;
          b.addEventListener('click', async (ev) => {
            ev.stopPropagation();
            try {
              await entry.fn();
              state.openMenuPlanId = null;
              state.openMenuAnchor = null;
              await loadPlans();
            } catch (err) {
              window.alert(`Aktion fehlgeschlagen: ${err.message}`);
            }
          });
          menu.appendChild(b);
        });
        document.body.appendChild(menu);
      }
      card.appendChild(menuWrap);
      els.planList.appendChild(card);
    });
  }

  function dayByName(dayName) {
    return state.planJson.days.find((d) => d.day === dayName);
  }

  function findEventIndex(dayName, eventId) {
    const day = dayByName(dayName);
    if (!day || !Array.isArray(day.events)) return -1;
    return day.events.findIndex((e) => String(e?.id || '') === String(eventId || ''));
  }

  function clearDropHighlights() {
    root.querySelectorAll('.gym-event-card.is-drop-target, .gym-event-list.is-drop-target').forEach((el) => {
      el.classList.remove('is-drop-target');
    });
  }

  function clearDragState() {
    state.draggingEvent = null;
    clearDropHighlights();
    root.querySelectorAll('.gym-event-card.is-dragging').forEach((el) => el.classList.remove('is-dragging'));
  }

  function dropEventTo(targetDayName, targetEventId = null) {
    const drag = state.draggingEvent;
    if (!drag) return;
    const srcDay = dayByName(drag.sourceDay);
    const dstDay = dayByName(targetDayName);
    if (!srcDay || !dstDay) {
      clearDragState();
      return;
    }
    const srcIdx = findEventIndex(drag.sourceDay, drag.eventId);
    if (srcIdx < 0) {
      clearDragState();
      return;
    }
    pushUndoSnapshot('move_event');
    const [moved] = srcDay.events.splice(srcIdx, 1);
    if (!moved) {
      clearDragState();
      return;
    }

    let insertIdx = dstDay.events.length;
    if (targetEventId) {
      const rawTargetIdx = findEventIndex(targetDayName, targetEventId);
      if (rawTargetIdx >= 0) {
        insertIdx = rawTargetIdx;
        if (drag.sourceDay === targetDayName && srcIdx < rawTargetIdx) insertIdx -= 1;
      }
    }
    insertIdx = Math.max(0, Math.min(dstDay.events.length, insertIdx));
    dstDay.events.splice(insertIdx, 0, moved);
    clearDragState();
    setDirty(true);
    renderEditorSurface();
  }

  function addEvent(day) {
    const kindRaw = (window.prompt('Event-Typ (gym/run/ergo/bike/row/swim/rest):', 'gym') || '').trim().toLowerCase();
    if (!ALL_EVENT_KINDS.has(kindRaw)) return;
    const title = (window.prompt('Titel:', kindRaw === 'gym' ? 'Session' : kindRaw === 'run' ? 'Easy Z2' : 'Rest') || '').trim();
    const time = (window.prompt('Zeit (z.B. 18:30 oder —):', '18:30') || '').trim();
    pushUndoSnapshot('add_event');
    day.events.push({
      id: uid(),
      kind: kindRaw,
      time: time && time !== '—' ? time : null,
      title,
      frequency: 1,
      items: [],
    });
    setDirty(true);
    renderEditorSurface();
  }

  function normalizeEventTime(value) {
    const raw = String(value || '').trim();
    if (!raw || raw === '—' || raw === '-' || raw === '--') return null;
    const m = /^(\d{1,2})[:.](\d{1,2})$/.exec(raw);
    if (!m) return raw;
    const hh = Math.max(0, Math.min(23, Number.parseInt(m[1], 10) || 0));
    const mm = Math.max(0, Math.min(59, Number.parseInt(m[2], 10) || 0));
    return `${String(hh).padStart(2, '0')}:${String(mm).padStart(2, '0')}`;
  }

  function itemSummary(item, dayName) {
    if (!item || typeof item !== 'object') return '—';
    if (item.kind === 'exercise') {
      const variation = item.variation ? ` (${item.variation})` : '';
      const reps = item.reps && typeof item.reps === 'object' ? `${item.reps.min || 0}-${item.reps.max || item.reps.min || 0}` : '';
      const rpe = Array.isArray(item.rpe_list) && item.rpe_list.length ? ` · RPE ${item.rpe_list.join('/')}` : '';
      return `${item.name || ''}${variation} · ${item.sets || 0}×${reps}${rpe}`;
    }
    if (item.kind === 'cardio' || item.kind === 'run' || item.kind === 'ergo') return formatCardioItem(item);
    if (item.kind === 'run_detail') return item.text || 'Run Item';
    if (item.kind === 'ref_block') {
      const displayBase = String(item.display_name || 'Block').trim();
      const display = item.variation ? `${displayBase} (${item.variation})` : displayBase;
      const resolved = resolveRefBlockText(item, dayName);
      if (resolved) return `${display} · ${resolved}`;
      return `${display} → ${item.block_name || '—'}`;
    }
    if (item.kind === 'note') return item.text || 'Notiz';
    return '—';
  }

  function itemHasUnreadCoreChange(item) {
    return !!(item && typeof item === 'object' && item.core_change && typeof item.core_change === 'object' && item.core_change.seen !== true);
  }

  function coreChangeFields(item) {
    const fields = Array.isArray(item?.core_change?.fields) ? item.core_change.fields : [];
    return new Set(fields.map((field) => String(field || '').trim()));
  }

  function itemSummaryHtml(item, dayName, displayItem) {
    const base = displayItem && typeof displayItem === 'object' ? displayItem : item;
    if (!base || typeof base !== 'object') return escapeHtml(itemSummary(item, dayName));
    if (base.kind === 'cardio' || base.kind === 'run_detail') {
      const detail = escapeHtml(itemSummary(base, dayName));
      const duration = Number.isFinite(Number(base.duration_min ?? base.amount_value)) ? `${Number(base.duration_min ?? base.amount_value)} min` : '';
      const mode = String(base.mode || '').trim();
      const intensity = String(base.intensity || '').trim();
      return `
        <span class="session-exercise-row session-exercise-row--cardio">
          <span class="session-exercise-main">
            <span class="session-exercise-name">${detail}</span>
          </span>
          <span class="session-exercise-values">
            ${duration ? `<span class="session-exercise-target">${escapeHtml(duration)}</span>` : ''}
            ${mode ? `<span class="session-exercise-rpe">${escapeHtml(mode.charAt(0).toUpperCase() + mode.slice(1))}</span>` : intensity ? `<span class="session-exercise-rpe">${escapeHtml(intensity)}</span>` : ''}
          </span>
        </span>
      `;
    }
    if (base.kind === 'ref_block') {
      const name = escapeHtml(base.display_name || 'Block');
      const variation = base.variation ? ` (${escapeHtml(base.variation)})` : '';
      const blockName = escapeHtml(base.block_name || 'Referenz');
      return `
        <span class="session-exercise-row session-exercise-row--ref">
          <span class="session-exercise-main">
            <span class="session-exercise-name">Block-Referenz</span>
            <span class="session-exercise-meta">${name}${variation}</span>
          </span>
          <span class="session-exercise-values">
            <span class="session-exercise-target">${blockName}</span>
          </span>
        </span>
      `;
    }
    if (base.kind === 'note') {
      return `
        <span class="session-exercise-row session-exercise-row--note">
          <span class="session-exercise-main">
            <span class="session-exercise-name">Notiz</span>
            <span class="session-exercise-meta">${escapeHtml(base.text || '—')}</span>
          </span>
        </span>
      `;
    }
    if (base.kind !== 'exercise') return escapeHtml(itemSummary(base, dayName));
    const highlight = itemHasUnreadCoreChange(item);
    const fields = coreChangeFields(item);
    const wrap = (field, text) => {
      const safe = escapeHtml(text);
      if (!(highlight && fields.has(field))) return safe;
      return `<span class="gym-core-change-accent" data-core-field="${escapeAttr(field)}">${safe}</span>`;
    };
    const name = escapeHtml(base.name || 'Übung');
    const variationText = base.variation ? `(${base.variation})` : '';
    const repsText = base.reps && typeof base.reps === 'object' ? `${base.reps.min || 0}-${base.reps.max || base.reps.min || 0}` : '';
    const setText = `${escapeHtml(String(base.sets || 0))}×${wrap('reps', repsText)}`;
    const rpeCompact = formatCompactRpe(base.rpe_list);
    const rpeText = rpeCompact ? wrap('rpe_list', `RPE ${rpeCompact}`) : '';
    return `
      <span class="session-exercise-row session-exercise-row--exercise">
        <span class="session-exercise-main">
          <span class="session-exercise-name">${name}</span>
          ${variationText ? `<span class="session-exercise-meta session-exercise-meta--variation">${wrap('variation', variationText.trim())}</span>` : ''}
          ${base.note ? `<span class="session-exercise-meta session-exercise-meta--note">${escapeHtml(base.note)}</span>` : ''}
        </span>
        <span class="session-exercise-values">
          <span class="session-exercise-target">${setText}</span>
          ${rpeText ? `<span class="session-exercise-rpe">${rpeText}</span>` : ''}
        </span>
      </span>
    `;
  }

  function setItemSummary(summary, item, dayName, displayItem) {
    if (!summary) return;
    summary.innerHTML = itemSummaryHtml(item, dayName, displayItem);
  }

  async function acknowledgeCoreChange(item) {
    if (!state.selectedPlanId || !itemHasUnreadCoreChange(item)) return;
    const itemId = String(item?.id || '').trim();
    if (!itemId) return;
    try {
      await api(`/api/gym_plans/${state.selectedPlanId}/ack_core_change`, {
        method: 'POST',
        body: JSON.stringify({ item_id: itemId }),
      });
      if (item.core_change && typeof item.core_change === 'object') {
        item.core_change.seen = true;
      }
      renderEditorSurface();
    } catch (err) {
      console.warn('ack core change failed', err);
    }
  }

  function attachTabAutocomplete(input, onAccept) {
    input.addEventListener('keydown', (ev) => {
      if (ev.key !== 'Tab' || ev.shiftKey) return;
      const q = String(input.value || '').trim().toLowerCase();
      if (!q) return;
      const hit = SUGGESTIONS.find((s) => String(s).toLowerCase().startsWith(q));
      if (!hit) return;
      ev.preventDefault();
      input.value = hit;
      onAccept();
    });
  }

  function weekNumber() {
    return Number.parseInt(String(state.selectedWeek || 'W1').replace('W', ''), 10) || 1;
  }

  function currentWeekRule() {
    const wk = String(state.selectedWeek || 'W1');
    return state.rulesJson?.[wk] || {};
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
    const text = String(name || '').toLowerCase();
    const assist = ['fly', 'seitheben', 'lateral', 'pushdown', 'curl', 'trizeps', 'waden', 'calf', 'rear delt', 'face pull'];
    const main = ['bank', 'bench', 'ohp', 'overhead press', 'squat', 'kniebeuge', 'deadlift', 'kreuzheben', 'rudern', 'row', 'latzug', 'pull up', 'pullup'];
    if (assist.some((k) => text.includes(k))) return 'assist';
    if (main.some((k) => text.includes(k))) return 'main';
    return 'secondary';
  }

  function scaleSetsForPreview(baseSets, factor, priority) {
    const base = Math.max(0, Number.parseInt(baseSets || 0, 10) || 0);
    const f = asFiniteNumber(factor);
    if (!Number.isFinite(f) || !(f > 0)) return base;
    const raw = roundInt(base * f);
    const minSets = 1;
    const maxTotalUp = 2;
    const maxStepUpByPrio = { assist: 1, secondary: 1, main: 1 };
    const upLimit = Math.min(base + maxTotalUp, base + (maxStepUpByPrio[priority] ?? 1));
    return Math.max(minSets, Math.min(raw, upLimit));
  }

  function applyRpeCapForPreview(values, cap, minRpe = 5.0, roundStep = 0.5) {
    const rpes = Array.isArray(values) ? values.map((v) => Number.parseFloat(v)).filter(Number.isFinite) : [];
    const capNum = asFiniteNumber(cap);
    if (!rpes.length) return [];
    if (!Number.isFinite(capNum) || capNum <= 0) return rpes;
    const top = Math.max(...rpes);
    if (capNum >= top) return rpes;
    const newTop = Math.min(top, capNum);
    return rpes.map((v) => {
      const delta = top - v;
      const lowered = Math.max(minRpe, newTop - delta);
      return Math.max(minRpe, roundToStep(lowered, roundStep));
    });
  }

  function fitRpeToSets(values, targetSets) {
    const vals = Array.isArray(values) ? values.map((v) => Number.parseFloat(v)).filter(Number.isFinite) : [];
    const target = Math.max(0, Number.parseInt(targetSets || 0, 10) || 0);
    if (!vals.length || target <= 0) return [];
    if (target === vals.length) return vals;
    const top = Math.max(...vals);
    const deltas = vals.map((v) => top - v);
    if (target < vals.length) {
      const ranked = vals.map((_, idx) => idx).sort((a, b) => {
        const da = deltas[a];
        const db = deltas[b];
        if (da !== db) return da - db;
        return a - b;
      });
      const keep = new Set(ranked.slice(0, target));
      return vals.filter((_, idx) => keep.has(idx));
    }
    const easiestDelta = Math.max(...deltas);
    const out = [...vals];
    while (out.length < target) out.push(top - easiestDelta);
    return out;
  }

  function scaleRunTextPreview(text, factor) {
    const f = asFiniteNumber(factor);
    if (!Number.isFinite(f) || !(f > 0)) return String(text || '');
    const raw = String(text || '');
    const rx = /(\d+)(?:\s*[-–—]\s*(\d+))?\s*min/i;
    const m = rx.exec(raw);
    if (!m) return raw;
    const d1 = Number.parseInt(m[1], 10);
    const d2 = m[2] ? Number.parseInt(m[2], 10) : d1;
    if (!Number.isFinite(d1) || !Number.isFinite(d2)) return raw;
    const n1 = Math.max(5, roundToStep(d1 * f, 5));
    const n2 = Math.max(n1, roundToStep(d2 * f, 5));
    const repl = (n1 === n2) ? `${n1} min` : `${n1}-${n2} min`;
    return raw.replace(rx, repl);
  }

  function scaleRunDuration(value, factor) {
    const duration = asFiniteNumber(value);
    const f = asFiniteNumber(factor);
    if (!Number.isFinite(duration) || !(duration > 0)) return duration;
    if (!Number.isFinite(f) || !(f > 0)) return duration;
    return Math.max(5, roundToStep(duration * f, 5));
  }

  function buildPreviewItem(item, weekRule) {
    if (!item || typeof item !== 'object') return item;
    const out = JSON.parse(JSON.stringify(item));
    if (out.kind === 'exercise') {
      const sets = scaleSetsForPreview(out.sets, weekRule?.strength_factor, exercisePriority(out.name));
      out.sets = sets;
      const rpeCapped = applyRpeCapForPreview(out.rpe_list, weekRule?.rpe_cap);
      out.rpe_list = fitRpeToSets(rpeCapped.length ? rpeCapped : out.rpe_list, sets);
      return out;
    }
    if (out.kind === 'run_detail') {
      out.text = scaleRunTextPreview(out.text, weekRule?.cardio_factor ?? weekRule?.run_factor);
      return out;
    }
    if (out.kind === 'cardio') {
      const duration = Number.parseFloat(out.duration_min ?? out.amount_value);
      if (Number.isFinite(duration) && duration > 0) {
        const scaled = scaleRunDuration(duration, weekRule?.cardio_factor ?? weekRule?.run_factor);
        out.duration_min = scaled;
        out.amount_value = scaled;
        out.amount_unit = out.amount_unit || 'min';
      }
      const display = formatCardioItem(out);
      if (display && display !== '—') {
        out.display = display;
        out.display_text = display;
      }
      return out;
    }
    return out;
  }

  function parseWeeksFromSpec(spec) {
    const out = new Set();
    const normalized = String(spec || '').replaceAll('–', '-');
    const rx = /(\d+)(?:\s*-\s*(\d+))?/g;
    let m = rx.exec(normalized);
    while (m) {
      const a = Number.parseInt(m[1], 10);
      const b = m[2] ? Number.parseInt(m[2], 10) : a;
      const lo = Math.min(a, b);
      const hi = Math.max(a, b);
      for (let i = lo; i <= hi; i += 1) out.add(i);
      m = rx.exec(normalized);
    }
    return Array.from(out);
  }

  function resolveRefBlockText(item, dayName) {
    const blockName = String(item?.block_name || '').trim();
    if (!blockName) return '';
    const entries = Array.isArray(state.blocksJson?.[blockName]) ? state.blocksJson[blockName] : [];
    if (!entries.length) return '';
    const preferredDay = String(item.block_day || dayName || '').trim();
    const displayName = String(item.display_name || '').trim().toLowerCase();
    const variation = String(item.variation || '').trim().toLowerCase();
    const byTitle = (entry) => {
      const t = String(entry?.title || '').toLowerCase();
      if (displayName && !t.includes(displayName)) return false;
      if (variation && !t.includes(variation)) return false;
      return true;
    };
    const sorted = entries
      .map((entry) => {
        let score = 0;
        if (preferredDay && entry.day === preferredDay) score += 2;
        if (byTitle(entry)) score += 1;
        return { entry, score };
      })
      .sort((a, b) => b.score - a.score)
      .map((x) => x.entry);
    const targetWeek = weekNumber();
    const matchWeekLine = (rawLine) => {
      const line = String(rawLine || '').trim();
      if (!line) return null;
      const normalized = line.replace(/^\s*-\s*/, '');
      if (!/^Woche\s+/i.test(normalized)) return null;
      const rest = normalized.replace(/^Woche\s+/i, '');
      const colonIdx = rest.indexOf(':');
      if (colonIdx < 0) return null;
      const spec = rest.slice(0, colonIdx).trim();
      const text = rest.slice(colonIdx + 1).trim();
      if (!spec || !text) return null;
      return { spec, text };
    };

    for (const candidate of sorted) {
      const lines = Array.isArray(candidate?.lines) ? candidate.lines : [];
      for (const raw of lines) {
        const parsed = matchWeekLine(raw);
        if (!parsed) continue;
        const weeks = parseWeeksFromSpec(parsed.spec);
        if (weeks.includes(targetWeek)) return parsed.text;
      }
    }
    for (const candidate of sorted) {
      const lines = Array.isArray(candidate?.lines) ? candidate.lines : [];
      const fallback = lines.find((l) => String(l || '').trim());
      if (fallback) {
        const parsed = matchWeekLine(fallback);
        if (parsed) return parsed.text;
        return String(fallback).replace(/^\s*-\s*/, '').trim();
      }
    }
    return '';
  }

  function addItemByType(event, type, dayName) {
    pushUndoSnapshot('add_item');
    if (type === 'exercise') {
      event.items.push({ id: uid(), kind: 'exercise', name: '', variation: '', sets: 3, reps: { min: 6, max: 10 }, rpe_list: [8, 8, 8], note: '' });
    } else if (type === 'run_detail') {
      event.items.push({ id: uid(), kind: 'run_detail', text: '' });
    } else if (type === 'cardio') {
      event.items.push({ id: uid(), kind: 'cardio', mode: eventCardioMode(event) || 'run', name: event.title || 'Cardio', duration_min: 30, amount_value: 30, amount_unit: 'min', intensity: 'locker', notes: '', display: '30 min locker' });
    } else if (type === 'ref_block') {
      const blockNames = Object.keys(state.blocksJson || {});
      event.items.push({ id: uid(), kind: 'ref_block', display_name: 'Squats', variation: '', block_name: blockNames[0] || '', block_day: dayName || '' });
    } else {
      event.items.push({ id: uid(), kind: 'note', text: '' });
    }
  }

  function renderItemEditor(item, eventItems, dayName, opts = {}) {
    const readOnly = !!opts.readOnly;
    const displayItem = opts.displayItem && typeof opts.displayItem === 'object' ? opts.displayItem : item;
    const wrap = document.createElement('details');
    wrap.className = 'gym-item-line';
    wrap.dataset.kind = String(item?.kind || '');
    const summary = document.createElement('summary');
    setItemSummary(summary, item, dayName, displayItem);
    wrap.appendChild(summary);
    if (item.kind === 'ref_block') {
      summary.style.textDecoration = 'underline';
      summary.addEventListener('click', () => {
        const blockName = String(item.block_name || '').trim();
        if (!blockName) return;
        const target = els.blockList.querySelector(`[data-block-name="${CSS.escape(blockName)}"]`);
        if (target) {
          target.open = true;
          target.classList.add('is-highlight');
          target.scrollIntoView({ behavior: 'smooth', block: 'center' });
          setTimeout(() => target.classList.remove('is-highlight'), 1400);
        }
      });
    }
    if (item.kind === 'exercise' && itemHasUnreadCoreChange(item)) {
      summary.addEventListener('click', () => {
        acknowledgeCoreChange(item);
      }, { once: true });
      wrap.addEventListener('toggle', () => {
        if (wrap.open) acknowledgeCoreChange(item);
      }, { once: true });
    }

    const box = document.createElement('div');
    box.className = 'gym-item-edit';

    if (item.kind === 'exercise') {
      box.innerHTML = `
        <label>Name<input type="text" data-f="name" value="${escapeAttr(item.name || '')}" ${readOnly ? 'disabled' : ''} /></label>
        <label>Variation<input type="text" data-f="variation" value="${escapeAttr(item.variation || '')}" ${readOnly ? 'disabled' : ''} /></label>
        <label>Sets<input type="number" min="1" step="1" data-f="sets" value="${Number.parseInt(displayItem.sets || 1, 10)}" ${readOnly ? 'disabled' : ''} /></label>
        <label>RPE Liste<input type="text" data-f="rpe" value="${escapeAttr(Array.isArray(displayItem.rpe_list) ? displayItem.rpe_list.join('/') : '')}" ${readOnly ? 'disabled' : ''} /></label>
        <label>Reps Min<input type="number" min="1" step="1" data-f="rmin" value="${Number.parseInt(item.reps?.min || 6, 10)}" ${readOnly ? 'disabled' : ''} /></label>
        <label>Reps Max<input type="number" min="1" step="1" data-f="rmax" value="${Number.parseInt(item.reps?.max || 10, 10)}" ${readOnly ? 'disabled' : ''} /></label>
        <label class="full">Notiz<input type="text" data-f="note" value="${escapeAttr(item.note || '')}" ${readOnly ? 'disabled' : ''} /></label>
      `;
      const nameInput = box.querySelector('input[data-f="name"]');
      if (nameInput) attachTabAutocomplete(nameInput, () => {
        item.name = nameInput.value;
        setItemSummary(summary, item, dayName, item);
        setDirty(true);
      });
    } else if (item.kind === 'run_detail') {
      box.innerHTML = `<label class="full">Run Text<input type="text" data-f="text" value="${escapeAttr(displayItem.text || '')}" ${readOnly ? 'disabled' : ''} /></label>`;
    } else if (item.kind === 'cardio') {
      box.innerHTML = `
        <label>Name<input type="text" data-f="name" value="${escapeAttr(item.name || '')}" ${readOnly ? 'disabled' : ''} /></label>
        <label>Modus<select data-f="mode" ${readOnly ? 'disabled' : ''}>
          ${['run', 'ergo', 'bike', 'cardio'].map((mode) => `<option value="${escapeAttr(mode)}" ${String(item.mode || '').toLowerCase() === mode ? 'selected' : ''}>${escapeHtml(mode.toUpperCase())}</option>`).join('')}
        </select></label>
        <label>Dauer<input type="number" min="1" step="1" data-f="duration_min" value="${Number.parseFloat(displayItem.duration_min || displayItem.amount_value || 30)}" ${readOnly ? 'disabled' : ''} /></label>
        <label>Einheit<input type="text" data-f="amount_unit" value="${escapeAttr(displayItem.amount_unit || 'min')}" ${readOnly ? 'disabled' : ''} /></label>
        <label>Intensität<input type="text" data-f="intensity" value="${escapeAttr(item.intensity || '')}" ${readOnly ? 'disabled' : ''} /></label>
        <label class="full">Notiz<input type="text" data-f="notes" value="${escapeAttr(item.notes || item.note || '')}" ${readOnly ? 'disabled' : ''} /></label>
      `;
    } else if (item.kind === 'ref_block') {
      const options = Object.keys(state.blocksJson || {}).map((n) => `<option value="${escapeAttr(n)}" ${item.block_name === n ? 'selected' : ''}>${escapeHtml(n)}</option>`).join('');
      box.innerHTML = `
        <label>Name<input type="text" data-f="display_name" value="${escapeAttr(item.display_name || '')}" ${readOnly ? 'disabled' : ''} /></label>
        <label>Variation<input type="text" data-f="variation" value="${escapeAttr(item.variation || '')}" ${readOnly ? 'disabled' : ''} /></label>
        <label>Block<select data-f="block_name" ${readOnly ? 'disabled' : ''}><option value="">(leer)</option>${options}</select></label>
      `;
    } else {
      box.innerHTML = `<label class="full">Text<input type="text" data-f="text" value="${escapeAttr(item.text || '')}" ${readOnly ? 'disabled' : ''} /></label>`;
    }

    box.querySelectorAll('input,select').forEach((field) => {
      if (readOnly) return;
      field.addEventListener('focus', () => {
        pushUndoSnapshot('edit_item');
      }, { once: true });
      field.addEventListener('input', () => {
        const f = field.dataset.f;
        if (item.kind === 'exercise') {
          if (f === 'name') item.name = field.value;
          else if (f === 'variation') item.variation = field.value;
          else if (f === 'sets') item.sets = Math.max(1, Number.parseInt(field.value || '1', 10) || 1);
          else if (f === 'rpe') item.rpe_list = String(field.value || '').split('/').map((v) => Number.parseFloat(v.trim())).filter(Number.isFinite);
          else if (f === 'rmin') item.reps = { ...(item.reps || {}), min: Math.max(1, Number.parseInt(field.value || '1', 10) || 1) };
          else if (f === 'rmax') item.reps = { ...(item.reps || {}), max: Math.max(1, Number.parseInt(field.value || '1', 10) || 1) };
          else if (f === 'note') item.note = field.value;
        } else if (item.kind === 'cardio') {
          if (f === 'duration_min') {
            const next = Math.max(1, Number.parseInt(field.value || '1', 10) || 1);
            item.duration_min = next;
            item.amount_value = next;
          } else if (f === 'amount_unit') {
            item.amount_unit = field.value || 'min';
          } else {
            item[f] = field.value;
          }
          const display = formatCardioItem(item);
          if (display && display !== '—') {
            item.display = display;
            item.display_text = display;
          }
        } else {
          item[f] = field.value;
        }
        setItemSummary(summary, item, dayName, buildPreviewItem(item, currentWeekRule()));
        setDirty(true);
      });
    });

    const ctrl = document.createElement('div');
    ctrl.className = 'gym-item-ctrl full';
    ctrl.innerHTML = '<button type="button" data-act="up">↑</button><button type="button" data-act="down">↓</button><button type="button" data-act="remove">Entfernen</button>';
    ctrl.querySelectorAll('button').forEach((btn) => {
      if (readOnly) btn.disabled = true;
      btn.addEventListener('click', () => {
        if (readOnly) return;
        const idx = eventItems.findIndex((x) => x.id === item.id);
        if (idx < 0) return;
        pushUndoSnapshot('item_action');
        if (btn.dataset.act === 'remove') eventItems.splice(idx, 1);
        else if (btn.dataset.act === 'up' && idx > 0) {
          [eventItems[idx - 1], eventItems[idx]] = [eventItems[idx], eventItems[idx - 1]];
        } else if (btn.dataset.act === 'down' && idx < eventItems.length - 1) {
          [eventItems[idx + 1], eventItems[idx]] = [eventItems[idx], eventItems[idx + 1]];
        }
        setDirty(true);
        renderEditorSurface();
      });
    });
    box.appendChild(ctrl);

    wrap.appendChild(box);
    return wrap;
  }

  function renderEventCard(day, event, eventIdx, opts = {}) {
    const listRef = Array.isArray(opts.listRef) ? opts.listRef : day.events;
    const dayName = opts.dayName || day.day;
    const sequenceMode = !!opts.sequenceMode;
    const previewMode = isPeriodizationEnabled() && String(state.selectedWeek || 'W1') !== 'W1';
    const weekRule = isPeriodizationEnabled() ? currentWeekRule() : {};
    const card = document.createElement('article');
    const kindName = String(event?.kind || '').toLowerCase();
    const rawKindLabel = String(eventKindLabel(event.kind, event.mode) || '').trim();
    const kindLabel = rawKindLabel ? rawKindLabel.charAt(0).toUpperCase() + rawKindLabel.slice(1).toLowerCase() : 'Session';
    const sessionKindClass = kindName === 'gym' ? 'session-card--gym' : (kindName === 'rest' ? 'session-card--rest' : 'session-card--cardio');
    card.className = `gym-event-card session-card ${sessionKindClass}`;
    card.draggable = !sequenceMode;
    card.dataset.day = dayName;
    card.dataset.eventId = event.id;
    card.dataset.kind = kindName;
    const meta = eventMetaStats(event);
    let metaText = '';
    if (String(event?.kind || '').toLowerCase() === 'gym') {
      const parts = [];
      if (meta.exerciseCount > 0) parts.push(`${meta.exerciseCount} ${meta.exerciseCount === 1 ? 'Übung' : 'Übungen'}`);
      if (meta.totalSets > 0) parts.push(`${meta.totalSets} Sets`);
      if (Number.isFinite(meta.avgRpe)) parts.push(`Ø RPE ${meta.avgRpe.toFixed(1)}`);
      metaText = parts.join(' · ');
    } else if (meta.cardioItems > 0) {
      metaText = `${meta.cardioItems} ${meta.cardioItems === 1 ? 'Cardio-Item' : 'Cardio-Items'}`;
    }

    card.innerHTML = `
      <div class="session-card__rail" aria-hidden="true"></div>
      <header class="gym-event-head session-card__header">
        <div class="gym-event-head-left session-card__header-main">
          <div class="gym-event-topline session-card__topline">
            <div class="session-card__badges">
              <span class="gym-time-chip session-pill session-pill--time" data-role="time" contenteditable="plaintext-only" spellcheck="false">${escapeHtml(event.time || '—')}</span>
              <span class="gym-type-badge session-pill session-pill--kind ${escapeAttr(event.kind)}">${escapeHtml(kindLabel)}</span>
            </div>
            <div class="gym-sequence-head-actions session-card__actions">
              ${sequenceMode ? '<button type="button" class="gym-kebab-btn session-action-btn" data-act="up" aria-label="Nach oben">↑</button><button type="button" class="gym-kebab-btn session-action-btn" data-act="down" aria-label="Nach unten">↓</button>' : ''}
              <button type="button" class="gym-kebab-btn session-action-btn" data-act="remove" aria-label="Löschen">✕</button>
            </div>
          </div>
          <div class="session-card__titleblock">
            <h3 class="gym-event-title session-card__title" data-role="title" contenteditable="plaintext-only" spellcheck="false">${escapeHtml(event.title || defaultEventTitle(event.kind))}</h3>
            ${metaText ? `<p class="gym-event-meta session-card__summary">${escapeHtml(metaText)}</p>` : ''}
          </div>
        </div>
      </header>
      <div class="gym-item-surface session-card__body">
        <div class="gym-item-lines session-exercise-list"></div>
      </div>
      <footer class="gym-event-actions session-card__footer">
        <button type="button" class="session-footer-btn" data-act="add-ex">+ Übung</button>
        <button type="button" class="session-footer-btn" data-act="add-run">+ Cardio-Item</button>
        <button type="button" class="session-footer-btn" data-act="add-ref">+ Block-Referenz</button>
      </footer>
    `;

    const lines = card.querySelector('.gym-item-lines');
    (event.items || []).forEach((item) => {
      const displayItem = previewMode ? buildPreviewItem(item, weekRule) : item;
      lines.appendChild(renderItemEditor(item, event.items, dayName, { readOnly: previewMode, displayItem }));
    });

    card.querySelector('button[data-act="remove"]').addEventListener('click', () => {
      if (previewMode) return;
      pushUndoSnapshot('remove_event');
      listRef.splice(eventIdx, 1);
      setDirty(true);
      renderEditorSurface();
    });
    card.querySelector('button[data-act="up"]')?.addEventListener('click', () => {
      if (previewMode || eventIdx <= 0) return;
      pushUndoSnapshot('move_sequence_event');
      [listRef[eventIdx - 1], listRef[eventIdx]] = [listRef[eventIdx], listRef[eventIdx - 1]];
      setDirty(true);
      renderEditorSurface();
    });
    card.querySelector('button[data-act="down"]')?.addEventListener('click', () => {
      if (previewMode || eventIdx >= listRef.length - 1) return;
      pushUndoSnapshot('move_sequence_event');
      [listRef[eventIdx + 1], listRef[eventIdx]] = [listRef[eventIdx], listRef[eventIdx + 1]];
      setDirty(true);
      renderEditorSurface();
    });

    card.addEventListener('dragstart', (ev) => {
      if (previewMode) {
        ev.preventDefault();
        return;
      }
      if (ev.target.closest('input,textarea,select,button,summary,[contenteditable]')) {
        ev.preventDefault();
        return;
      }
      state.draggingEvent = { sourceDay: dayName, eventId: event.id };
      card.classList.add('is-dragging');
      if (ev.dataTransfer) {
        ev.dataTransfer.effectAllowed = 'move';
        ev.dataTransfer.setData('text/plain', String(event.id || ''));
      }
    });

    card.addEventListener('dragend', () => {
      clearDragState();
    });

    card.addEventListener('dragover', (ev) => {
      if (!state.draggingEvent) return;
      ev.preventDefault();
      card.classList.add('is-drop-target');
      if (ev.dataTransfer) ev.dataTransfer.dropEffect = 'move';
    });

    card.addEventListener('dragleave', () => {
      card.classList.remove('is-drop-target');
    });

    card.addEventListener('drop', (ev) => {
      if (!state.draggingEvent) return;
      ev.preventDefault();
      dropEventTo(day.day, event.id);
    });

    const timeChip = card.querySelector('[data-role="time"]');
    if (timeChip) {
      if (previewMode) timeChip.setAttribute('contenteditable', 'false');
      const commitTime = () => {
        if (previewMode) return;
        const before = event.time || null;
        const raw = String(timeChip.textContent || '').replace('[', '').replace(']', '').trim();
        const next = normalizeEventTime(raw);
        if (before !== next) pushUndoSnapshot('edit_event_time');
        event.time = next;
        timeChip.textContent = `[${next || '—'}]`;
        setDirty(true);
      };
      timeChip.addEventListener('keydown', (ev) => {
        if (ev.key === 'Enter') {
          ev.preventDefault();
          timeChip.blur();
        } else if (ev.key === 'Escape') {
          ev.preventDefault();
          timeChip.textContent = `[${event.time || '—'}]`;
          timeChip.blur();
        }
      });
      timeChip.addEventListener('focus', () => {
        const v = event.time || '';
        timeChip.textContent = v;
      });
      timeChip.addEventListener('blur', commitTime);
    }

    const titleNode = card.querySelector('[data-role="title"]');
    if (titleNode) {
      if (previewMode) titleNode.setAttribute('contenteditable', 'false');
      const commitTitle = () => {
        if (previewMode) return;
        const before = String(event.title || '');
        const next = String(titleNode.textContent || '').trim();
        if (before !== next) pushUndoSnapshot('edit_event_title');
        event.title = next;
        titleNode.textContent = next || defaultEventTitle(event.kind);
        setDirty(true);
      };
      titleNode.addEventListener('keydown', (ev) => {
        if (ev.key === 'Enter') {
          ev.preventDefault();
          titleNode.blur();
        } else if (ev.key === 'Escape') {
          ev.preventDefault();
          titleNode.textContent = event.title || defaultEventTitle(event.kind);
          titleNode.blur();
        }
      });
      titleNode.addEventListener('focus', () => {
        titleNode.textContent = event.title || '';
      });
      titleNode.addEventListener('blur', commitTitle);
    }

    card.querySelector('button[data-act="add-ex"]').addEventListener('click', () => {
      if (previewMode) return;
      addItemByType(event, 'exercise', dayName);
      setDirty(true);
      renderEditorSurface();
    });
    card.querySelector('button[data-act="add-run"]').addEventListener('click', () => {
      if (previewMode) return;
      addItemByType(event, event.kind === 'gym' ? 'run_detail' : 'cardio', dayName);
      setDirty(true);
      renderEditorSurface();
    });
    card.querySelector('button[data-act="add-ref"]').addEventListener('click', () => {
      if (previewMode) return;
      addItemByType(event, 'ref_block', dayName);
      setDirty(true);
      renderEditorSurface();
    });

    return card;
  }

  function renderSectionLabel(text, subline = '') {
    const wrap = document.createElement('div');
    wrap.className = 'gym-section-label';
    wrap.innerHTML = `
      <div class="gym-section-label__title">${escapeHtml(text)}</div>
      ${subline ? `<div class="gym-section-label__sub">${escapeHtml(subline)}</div>` : ''}
    `;
    return wrap;
  }

  function renderDayGrid() {
    const previewMode = isPeriodizationEnabled() && String(state.selectedWeek || 'W1') !== 'W1';
    const rolling = planMode() === 'rolling_sequence';
    els.dayGrid.innerHTML = '';
    const allDays = DAY_ORDER
      .map((dayName) => dayByName(dayName))
      .filter(Boolean);
    const visibleDays = rolling ? allDays.filter((day) => Array.isArray(day?.events) && day.events.length > 0) : allDays;
    if (rolling && visibleDays.length) {
      els.dayGrid.appendChild(renderSectionLabel('Feste Wochenanker', 'Nur verankerte Run-/Cardio-Tage. Keine 7-Tage-Fixwoche.'));
    }
    visibleDays.forEach((day, idx) => {
      const card = document.createElement('section');
      card.className = 'gym-day-card';
      if (idx === visibleDays.length - 1) card.classList.add('is-last');
      card.innerHTML = `
        <div class="gym-day-head">
          <div class="title">${escapeHtml(day.day)}</div>
          <button type="button" class="pill ghost">+ Event</button>
        </div>
        <div class="gym-event-list"></div>
      `;
      const addBtn = card.querySelector('button');
      if (addBtn) addBtn.disabled = previewMode;
      card.querySelector('button').addEventListener('click', () => {
        if (previewMode) return;
        addEvent(day);
      });
      const eventList = card.querySelector('.gym-event-list');
      eventList.addEventListener('dragover', (ev) => {
        if (!state.draggingEvent) return;
        ev.preventDefault();
        eventList.classList.add('is-drop-target');
        if (ev.dataTransfer) ev.dataTransfer.dropEffect = 'move';
      });
      eventList.addEventListener('dragleave', () => {
        eventList.classList.remove('is-drop-target');
      });
      eventList.addEventListener('drop', (ev) => {
        if (!state.draggingEvent) return;
        ev.preventDefault();
        dropEventTo(day.day, null);
      });
      (day.events || []).forEach((event, eventIdx) => eventList.appendChild(renderEventCard(day, event, eventIdx)));
      els.dayGrid.appendChild(card);
    });
  }

  function addSequenceEvent() {
    const seq = Array.isArray(state.planJson.sequence) ? state.planJson.sequence : (state.planJson.sequence = []);
    seq.push({ id: uid(), kind: 'gym', mode: null, time: null, title: 'Session', frequency: 1, items: [], notes: '' });
    setDirty(true);
    renderEditorSurface();
  }

  function renderSequenceList() {
    if (!els.sequenceList) return;
    els.sequenceList.innerHTML = '';
    const seq = Array.isArray(state.planJson.sequence) ? state.planJson.sequence : [];
    els.sequenceList.appendChild(renderSectionLabel('Rotation', 'Fortlaufende Gym-Reihenfolge. Nicht an feste Wochentage gebunden.'));
    const addBtn = document.createElement('button');
    addBtn.type = 'button';
    addBtn.className = 'pill ghost';
    addBtn.textContent = '+ Sequenz-Eintrag';
    addBtn.addEventListener('click', () => {
      pushUndoSnapshot('add_sequence_event');
      addSequenceEvent();
    });
    seq.forEach((event, idx) => {
      els.sequenceList.appendChild(renderEventCard({ day: `S${idx + 1}`, events: seq }, event, idx, { sequenceMode: true, listRef: seq, dayName: `S${idx + 1}` }));
    });
    els.sequenceList.appendChild(addBtn);
  }

  function renderPlanningMode() {
    const rolling = planMode() === 'rolling_sequence';
    const hasAnchoredDays = rolling && daysHaveEvents(state.planJson);
    els.modeFixedBtn?.classList.toggle('is-active', !rolling);
    els.modeRollingBtn?.classList.toggle('is-active', rolling);
    const modeSegmented = els.modeFixedBtn?.closest('.gym-mode-segmented');
    if (modeSegmented) modeSegmented.setAttribute('data-active', rolling ? 'rolling' : 'fixed');
    if (els.modeSubline) {
      els.modeSubline.textContent = rolling
        ? 'Rotation mit Wochenmuster und geschätzter Wochenlogik'
        : 'Fixe Woche mit direkter Mo-So Auswertung';
    }
    if (els.dayGrid) {
      els.dayGrid.hidden = rolling ? !hasAnchoredDays : false;
      els.dayGrid.style.display = rolling ? (hasAnchoredDays ? '' : 'none') : '';
      els.dayGrid.classList.toggle('gym-day-grid--rolling-anchors', !!hasAnchoredDays);
      if (rolling && hasAnchoredDays && els.sequenceList?.parentNode === els.dayGrid.parentNode) {
        els.sequenceList.insertAdjacentElement('afterend', els.dayGrid);
      }
    }
    if (els.sequenceList) {
      els.sequenceList.hidden = !rolling;
      els.sequenceList.style.display = rolling ? '' : 'none';
    }
    if (els.modeBody) {
      if (!rolling) {
        els.modeBody.innerHTML = `
          <div class="gym-mode-fixed-shell">
            <div class="gym-mode-fixed-note">Fixe Woche <span>Stats basieren direkt auf Mo-So.</span></div>
            <div class="gym-mode-hint">Beim Wechsel zu Rolling kann die Woche als Rotation übernommen werden.</div>
          </div>
        `;
      } else {
        const pattern = rollingWeekPattern();
        const avgDays = expectedTrainingDaysPerWeek();
        const slots = sequenceTrainingSlots();
        const factor = rollingRotationFactor();
        const anchoredDaysCount = (Array.isArray(state.planJson?.days) ? state.planJson.days : [])
          .filter((day) => Array.isArray(day?.events) && day.events.length > 0).length;
        els.modeBody.innerHTML = `
          <div class="gym-mode-week-pills">
            ${DAY_ORDER.map((day) => `<button type="button" class="gym-mode-day-pill is-${escapeAttr(pattern[day])}" data-day="${escapeAttr(day)}" aria-label="${escapeAttr(`${day} ${pattern[day]}`)}"><span class="gym-mode-day-name">${escapeHtml(day)}</span><span class="gym-mode-day-state">${escapeHtml(pattern[day] === 'train' ? 'Train' : pattern[day] === 'optional' ? 'Optional' : 'Rest')}</span></button>`).join('')}
          </div>
          <div class="gym-mode-stats">Rotation: ${slots} Gym-Slots · Wochenanker: ${anchoredDaysCount} Tage · Wochenmuster: Ø ${avgDays.toFixed(1)} Trainingstage · Stats ×${factor.toFixed(2)}</div>
          ${hasAnchoredDays ? '<div class="gym-mode-hint">Rolling bleibt Rolling: oben steht nur die Rotation. Feste Läufe/Cardio-Tage erscheinen separat als Wochenanker.</div>' : ''}
        `;
        els.modeBody.querySelectorAll('[data-day]').forEach((btn) => {
          btn.addEventListener('click', () => {
            const day = btn.dataset.day;
            const patternNow = rollingWeekPattern();
            const current = patternNow[day];
            const next = current === 'train' ? 'optional' : current === 'optional' ? 'rest' : 'train';
            pushUndoSnapshot('rolling_week_pattern');
            state.planJson.meta.rolling_week_pattern = { ...patternNow, [day]: next };
            setDirty(true);
            renderPlanningMode();
            renderCoach();
          });
        });
      }
    }
  }

  function renderEditorSurface() {
    renderPlanningMode();
    if (planMode() === 'rolling_sequence') {
      renderSequenceList();
      if (daysHaveEvents(state.planJson)) renderDayGrid();
      else if (els.dayGrid) els.dayGrid.innerHTML = '';
    } else {
      renderDayGrid();
    }
  }

  function renderRules() {
    const weeks = currentWeekCount();
    state.rulesJson = normalizeRulesJson(state.rulesJson, weeks);
    els.rulesList?.closest('.gym-rules-card')?.classList.toggle('is-disabled-card', !isPeriodizationEnabled());
    if (els.periodizationEnabled) els.periodizationEnabled.checked = isPeriodizationEnabled();
    if (els.periodizationLabel) els.periodizationLabel.textContent = isPeriodizationEnabled() ? 'Regel-Layer aktiv' : 'Optionales Regel-Layer';
    els.rulesList.innerHTML = '';
    for (let i = 1; i <= weeks; i += 1) {
      const w = `W${i}`;
      const row = state.rulesJson[w];
      const rpeTip = 'RPE-Cap skaliert Härte (Intensität). Beispiel: RPE 9/8/8 mit Cap 7 wird 7/6/6.';
      const strengthTip = 'Kraft-Faktor skaliert Sätze (Volumen), nicht Gewicht. Beispiel: Plan 3 Sätze, Faktor 1.2 -> 4 Sätze.';
      const runTip = 'Cardio-Faktor skaliert Cardio-Umfang, nicht Intensität. Beispiel: 30 min mit Faktor 0.8 -> 25 min.';
      const normalizedTag = String(row.tag || '');
      const phase = (normalizedTag === 'Overreach' ? 'Over' : normalizedTag);
      const safePhase = ['Build', 'Over', 'Deload'].includes(phase) ? phase : (row.deload ? 'Deload' : 'Build');
      row.tag = safePhase;
      row.deload = safePhase === 'Deload';
      const node = document.createElement('div');
      node.className = 'gym-rule-row';
      if (state.selectedWeek === w) node.classList.add('is-active');
      node.innerHTML = `
        <strong>${w}</strong>
        <input type="number" step="0.5" data-f="rpe_cap" value="${escapeAttr(row.rpe_cap ?? '')}" title="${escapeAttr(rpeTip)}" />
        <input type="number" step="0.05" data-f="strength_factor" value="${escapeAttr(row.strength_factor ?? 1)}" title="${escapeAttr(strengthTip)}" />
        <input type="number" step="0.05" data-f="cardio_factor" value="${escapeAttr(row.cardio_factor ?? row.run_factor ?? 1)}" title="${escapeAttr(runTip)}" />
        <button type="button" data-f="phase" title="Phase">${safePhase}</button>
      `;
      if (!isPeriodizationEnabled()) node.classList.add('is-disabled');
      node.addEventListener('click', (ev) => {
        if (ev.target.closest('input,button')) return;
        state.selectedWeek = w;
        renderEditorSurface();
        renderRules();
        requestCoachSummaryDebounced();
      });
      node.querySelectorAll('input').forEach((inp) => {
        inp.addEventListener('focus', () => {
          pushUndoSnapshot('edit_rule');
        }, { once: true });
        inp.addEventListener('input', () => {
          const f = inp.dataset.f;
          const num = Number.parseFloat(inp.value);
          row[f] = Number.isFinite(num) ? num : null;
          if (f === 'cardio_factor') row.run_factor = row[f];
          setDirty(true);
        });
      });
      node.querySelector('button[data-f="phase"]').addEventListener('click', (ev) => {
        pushUndoSnapshot('cycle_phase');
        const order = ['Build', 'Over', 'Deload'];
        const current = String(row.tag || 'Build');
        const idx = order.indexOf(current);
        const next = order[(idx + 1 + order.length) % order.length];
        row.tag = next;
        row.deload = next === 'Deload';
        ev.currentTarget.textContent = next;
        setDirty(true);
      });
      els.rulesList.appendChild(node);
    }
  }

  function renderBlocks() {
    els.blockList.innerHTML = '';
    const names = Object.keys(state.blocksJson || {});
    if (!names.length) {
      const empty = document.createElement('div');
      empty.className = 'gym-block-row';
      empty.textContent = 'Keine Referenzblöcke.';
      els.blockList.appendChild(empty);
      return;
    }
    names.forEach((name) => {
      const entries = Array.isArray(state.blocksJson[name]) ? state.blocksJson[name] : [];
      const wrap = document.createElement('details');
      wrap.className = 'gym-block-row';
      wrap.dataset.blockName = name;
      wrap.innerHTML = `
        <summary>
          <div class="gym-block-row-title">${escapeHtml(name)}</div>
          <div class="gym-block-row-sub">${entries.length} Einträge</div>
        </summary>
        <div class="gym-block-lines"></div>
      `;
      const lines = wrap.querySelector('.gym-block-lines');
      entries.forEach((entry, idx) => {
        const row = document.createElement('div');
        row.className = 'gym-block-entry';
        row.innerHTML = `
          <div class="gym-block-entry-head">
            <select data-f="day">
              ${DAY_ORDER.map((d) => `<option value="${d}" ${entry.day === d ? 'selected' : ''}>${d}</option>`).join('')}
            </select>
            <input type="text" data-f="title" value="${escapeAttr(entry.title || '')}" placeholder="Titel (z.B. Squats (LH) – schwer)" />
            <button type="button" data-act="remove-entry">Entfernen</button>
          </div>
          <textarea data-f="lines" rows="3" placeholder="Eine Zeile pro Regel">${escapeHtml((entry.lines || []).join('\n'))}</textarea>
        `;
        row.querySelector('[data-f="day"]').addEventListener('change', (ev) => {
          pushUndoSnapshot('edit_block_entry');
          entry.day = ev.target.value;
          setDirty(true);
        });
        const titleInput = row.querySelector('[data-f="title"]');
        titleInput.addEventListener('focus', () => {
          pushUndoSnapshot('edit_block_entry');
        }, { once: true });
        titleInput.addEventListener('input', (ev) => {
          entry.title = ev.target.value;
          setDirty(true);
        });
        const linesInput = row.querySelector('[data-f="lines"]');
        linesInput.addEventListener('focus', () => {
          pushUndoSnapshot('edit_block_entry');
        }, { once: true });
        linesInput.addEventListener('input', (ev) => {
          entry.lines = String(ev.target.value || '')
            .split('\n')
            .map((x) => x.trim())
            .filter(Boolean);
          setDirty(true);
        });
        row.querySelector('[data-act="remove-entry"]').addEventListener('click', () => {
          pushUndoSnapshot('remove_block_entry');
          entries.splice(idx, 1);
          setDirty(true);
          renderBlocks();
        });
        lines.appendChild(row);
      });

      const controls = document.createElement('div');
      controls.className = 'gym-block-controls';
      controls.innerHTML = `
        <button type="button" data-act="add-entry">+ Eintrag</button>
        <button type="button" data-act="rename-block">Umbenennen</button>
        <button type="button" data-act="remove-block">Block löschen</button>
      `;
      controls.querySelector('[data-act="add-entry"]').addEventListener('click', () => {
        pushUndoSnapshot('add_block_entry');
        entries.push({ id: uid(), day: 'Mo', title: '', frequency: 1, lines: [] });
        setDirty(true);
        renderBlocks();
      });
      controls.querySelector('[data-act="rename-block"]').addEventListener('click', () => {
        const next = window.prompt('Neuer Blockname:', name);
        if (!next || !next.trim() || next.trim() === name) return;
        pushUndoSnapshot('rename_block');
        const value = state.blocksJson[name];
        delete state.blocksJson[name];
        state.blocksJson[next.trim()] = value;
        setDirty(true);
        renderBlocks();
      });
      controls.querySelector('[data-act="remove-block"]').addEventListener('click', () => {
        if (!window.confirm(`Block "${name}" löschen?`)) return;
        pushUndoSnapshot('remove_block');
        delete state.blocksJson[name];
        setDirty(true);
        renderBlocks();
      });
      lines.appendChild(controls);

      els.blockList.appendChild(wrap);
    });
  }

  async function requestCoachSummary() {
    if (!state.planJson) return;
    try {
      const data = await api('/api/gym_plans/coach_summary', {
        method: 'POST',
        body: JSON.stringify({ plan_json: state.planJson, blocks_json: state.blocksJson, rules_json: state.rulesJson, week: state.selectedWeek }),
      });
      state.coach = data;
      renderCoach();
    } catch (err) {
      els.coachSummary.textContent = 'Coach konnte nicht geladen werden.';
      els.coachWarnings.textContent = err.message;
    }
  }

  function requestCoachSummaryDebounced() {
    if (state.coachTimer) clearTimeout(state.coachTimer);
    state.coachTimer = setTimeout(requestCoachSummary, 200);
  }

  function renderCoach() {
    const totals = state.coach?.totals || {};
    const rolling = totals.mode === 'rolling_sequence';
    if (els.coachSubline) {
      els.coachSubline.textContent = rolling ? 'Volumen & Balance · geschätzt/Woche' : 'Volumen & Balance · fixe Woche';
    }
    if (els.coachMeta) {
      if (rolling) {
        const avg = Number.parseFloat(totals.expected_training_days_per_week || 0) || 0;
        const factor = Number.parseFloat(totals.rotation_factor || 0) || 0;
        els.coachMeta.textContent = `${avg.toFixed(1)} Trainingstage/Woche · Rotation ×${factor.toFixed(2)}`;
      } else {
        els.coachMeta.textContent = 'Direkt aus Mo-So berechnet';
      }
    }
    els.coachSummary.innerHTML = '';
    els.coachWarnings.innerHTML = '';
    const muscleSets = Array.isArray(state.coach?.muscle_sets) ? state.coach.muscle_sets : [];
    if (!muscleSets.length) {
      const row = document.createElement('div');
      row.className = 'gym-coach-row';
      row.textContent = 'Keine Sets für diese Woche.';
      els.coachSummary.appendChild(row);
    } else {
      muscleSets.forEach((m) => {
        const row = document.createElement('div');
        row.className = 'gym-coach-row';
        const sets = Number.parseFloat(m.sets || 0) || 0;
        const setsRounded = Math.round(sets);
        const targetMin = Number.parseFloat(m.target_min || 0) || 0;
        const targetMax = Number.parseFloat(m.target_max || 0) || 0;
        const minRounded = Math.round(targetMin);
        const maxRounded = Math.round(targetMax);
        const warnMin = Number.parseFloat(m.warn_min ?? Math.max(0, targetMin - 1)) || 0;
        const warnMax = Number.parseFloat(m.warn_max ?? (targetMax + 2)) || (targetMax + 2);
        const domainMax = Math.max(warnMax, targetMax, setsRounded, 1);
        const toPct = (v) => Math.max(0, Math.min(100, (v / domainMax) * 100));
        const greenStart = toPct(targetMin);
        const greenEnd = toPct(targetMax);
        const marker = toPct(setsRounded);
        row.innerHTML = `
          <div class="gym-coach-row-head">
            <span>${escapeHtml(m.label || m.muscle || m.key || 'Muskel')}</span>
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
        els.coachSummary.appendChild(row);
      });
    }

    const warnings = Array.isArray(state.coach?.warnings) ? state.coach.warnings : [];
    if (!warnings.length) {
      const row = document.createElement('div');
      row.className = 'gym-warning-row';
      row.textContent = 'Keine Warnungen.';
      row.style.color = 'var(--text-muted)';
      els.coachWarnings.appendChild(row);
    } else {
      warnings.forEach((w) => {
        const row = document.createElement('div');
        row.className = 'gym-warning-row';
        row.textContent = `${w.kind}: ${w.message}`;
        els.coachWarnings.appendChild(row);
      });
    }

    if (els.coachWarnings) {
      els.coachWarnings.hidden = state.coachWarningsCollapsed;
      els.coachWarnings.style.display = state.coachWarningsCollapsed ? 'none' : '';
    }
    if (els.coachWarningsToggle) {
      els.coachWarningsToggle.setAttribute('aria-expanded', state.coachWarningsCollapsed ? 'false' : 'true');
      els.coachWarningsToggle.classList.toggle('is-collapsed', state.coachWarningsCollapsed);
    }
  }

  function renderParseResult() {
    els.parseWarnings.innerHTML = '';
    const warnings = Array.isArray(state.parseWarnings) ? state.parseWarnings : [];
    if (els.warningCount) els.warningCount.textContent = String(warnings.length);
    if (!warnings.length) {
      els.parseWarnings.textContent = 'Keine Warnings.';
    } else {
      warnings.forEach((w) => {
        const line = document.createElement('div');
        line.textContent = `${w.path || '-'} · ${w.kind}: ${w.message}`;
        els.parseWarnings.appendChild(line);
      });
    }

    if (!state.parseResult || !state.parseResult.plan_json) {
      els.parsePreview.textContent = '';
      return;
    }
    const p = state.parseResult.plan_json;
    const byDay = (p.days || []).map((d) => `${d.day}: ${(d.events || []).length} Event(s)`).join(' | ');
    const blockCount = Object.keys(state.parseResult.blocks_json || {}).length;
    els.parsePreview.textContent = `Preview · Wochen ${p.weeks} · ${byDay} · Blocks ${blockCount}`;
  }

  async function parseDsl() {
    const text = String(els.dslText.value || '');
    try {
      const res = await api('/api/gym_plans/parse_dsl', {
        method: 'POST',
        body: JSON.stringify({ text }),
      });
      state.parseResult = {
        plan_json: normalizePlanJson(res.plan_json),
        blocks_json: normalizeBlocksJson(res.blocks_json),
        rules_json: normalizeRulesJson(res.rules_json, res.plan_json?.weeks),
      };
      state.parseWarnings = Array.isArray(res.warnings) ? res.warnings : [];
      renderParseResult();
    } catch (err) {
      state.parseResult = null;
      state.parseWarnings = [{ kind: 'parse_failed', message: err.message, path: '-' }];
      renderParseResult();
    }
  }

  function applyParsed() {
    if (!state.parseResult) {
      window.alert('Bitte zuerst DSL parsen.');
      return;
    }
    const confirmed = window.confirm('Willst du wirklich alles überschreiben?');
    if (!confirmed) return;
    pushUndoSnapshot('apply_parsed');
    state.planJson = normalizePlanJson(state.parseResult.plan_json);
    syncBaseWeekFromDays(state.planJson);
    state.blocksJson = normalizeBlocksJson(state.parseResult.blocks_json);
    autoLinkRefBlocksInPlan();
    state.rulesJson = normalizeRulesJson(state.parseResult.rules_json, state.planJson.weeks);
    state.selectedWeek = 'W1';
    sanitizeSelectedWeek();
    console.debug('[gym] applyParsed -> state snapshot', {
      hasPlan: !!state.planJson,
      planKeys: Object.keys(state.planJson || {}),
      weeks: state.planJson.weeks,
      dayCount: state.planJson.days?.length || 0,
      baseWeekKeys: Object.keys(state.planJson.base_week || {}),
      blockCount: Object.keys(state.blocksJson || {}).length,
    });
    setDirty(true);
    renderAll();
  }

  function setDslMode(mode) {
    state.dslMode = mode === 'export' ? 'export' : 'import';
    if (els.dslSegImport) els.dslSegImport.classList.toggle('is-active', state.dslMode === 'import');
    if (els.dslSegExport) els.dslSegExport.classList.toggle('is-active', state.dslMode === 'export');
    const segmented = els.dslSegImport?.closest('.nutrition-mfp-segmented');
    if (segmented) segmented.setAttribute('data-active', state.dslMode);
    if (els.dslPaneImport) {
      const show = state.dslMode === 'import';
      els.dslPaneImport.hidden = !show;
      els.dslPaneImport.style.display = show ? '' : 'none';
    }
    if (els.dslPaneExport) {
      const show = state.dslMode === 'export';
      els.dslPaneExport.hidden = !show;
      els.dslPaneExport.style.display = show ? '' : 'none';
    }
  }

  async function exportDslToTextarea() {
    if (!state.planJson) return;
    try {
      const res = await api('/api/gym_plans/export_dsl', {
        method: 'POST',
        body: JSON.stringify({
          plan_json: state.planJson,
          rules_json: state.rulesJson,
          blocks_json: state.blocksJson,
        }),
      });
      if (els.dslExportText) {
        els.dslExportText.value = res.text || '';
      }
    } catch (err) {
      window.alert(`Export fehlgeschlagen: ${err.message}`);
    }
  }

  async function savePlan() {
    if (!state.planJson || !state.selectedPlanId) return;
    state.isSaving = true;
    renderSaveState();
    state.planMeta.name = String(els.title.value || '').trim() || 'Gym Plan';
    state.planMeta.focus = String(els.focus.value || '').trim();

    state.planJson.meta = state.planJson.meta || {};
    state.planJson.meta.title = state.planMeta.name;
    state.planJson.meta.focus = state.planMeta.focus;
    syncBaseWeekFromDays(state.planJson);

    try {
      const payload = {
        name: state.planMeta.name,
        title: state.planMeta.name,
        focus: state.planMeta.focus,
        plan_json: state.planJson,
        rules_json: { active_week: state.selectedWeek, weeks: state.rulesJson },
        blocks_json: state.blocksJson,
      };
      console.debug('[gym] save PUT payload', {
        planId: state.selectedPlanId,
        hasPlan: !!payload.plan_json,
        planKeys: Object.keys(payload.plan_json || {}),
        weeks: payload.plan_json?.weeks,
        dayCount: payload.plan_json?.days?.length || 0,
        baseWeekKeys: Object.keys(payload.plan_json?.base_week || {}),
        hasBlocks: !!payload.blocks_json,
        blockCount: Object.keys(payload.blocks_json || {}).length,
      });
      const res = await api(`/api/gym_plans/${state.selectedPlanId}`, {
        method: 'PUT',
        body: JSON.stringify(payload),
      });
      console.debug('[gym] save PUT response', {
        id: res.id,
        updated_at: res.updated_at,
        weeks: res.plan_json?.weeks,
        dayCount: res.plan_json?.days?.length || 0,
        blockCount: Object.keys(res.blocks_json || {}).length,
      });
      state.updatedAt = res.updated_at;
      state.planJson = normalizePlanJson(res.plan_json);
      state.rulesJson = normalizeRulesJson(res.rules_json, state.planJson.weeks);
      state.blocksJson = normalizeBlocksJson(res.blocks_json);
      state.planMeta = { name: res.title || state.planMeta.name, focus: res.focus || '' };
      setDirty(false);
      await loadPlans();
      await loadPlan(state.selectedPlanId);
    } catch (err) {
      window.alert(`Speichern fehlgeschlagen: ${err.message}`);
    } finally {
      state.isSaving = false;
      renderSaveState();
    }
  }

  async function createPlan() {
    try {
      const res = await api('/api/gym_plans', { method: 'POST', body: JSON.stringify({ set_active: false }) });
      await loadPlans();
      await loadPlan(res.id);
    } catch (err) {
      window.alert(`Neuer Plan fehlgeschlagen: ${err.message}`);
    }
  }

  async function duplicateCurrentPlan() {
    if (!state.selectedPlanId) return;
    try {
      const res = await api(`/api/gym_plans/${state.selectedPlanId}/duplicate`, { method: 'POST' });
      await loadPlans();
      await loadPlan(res.id);
    } catch (err) {
      window.alert(`Duplizieren fehlgeschlagen: ${err.message}`);
    }
  }

  function renderAll() {
    if (!state.planJson) return;
    ensureCycleMeta();
    els.title.value = state.planMeta.name || state.planJson.meta?.title || '';
    els.focus.value = state.planMeta.focus || state.planJson.meta?.focus || '';
    autosizeTitleField();
    autosizeFocusField();
    renderSaveState();
    renderHeaderStats();
    renderCycleCard();
    renderEditorSurface();
    renderRules();
    renderBlocks();
    requestCoachSummaryDebounced();
  }

  function escapeHtml(value) {
    return String(value || '')
      .replaceAll('&', '&amp;')
      .replaceAll('<', '&lt;')
      .replaceAll('>', '&gt;')
      .replaceAll('"', '&quot;')
      .replaceAll("'", '&#39;');
  }

  function escapeAttr(value) {
    return escapeHtml(value).replaceAll('`', '&#96;');
  }

  function wireEvents() {
    const bindUndoOnFocus = (el, label) => {
      el?.addEventListener('focus', () => pushUndoSnapshot(label));
    };

    els.planSearch?.addEventListener('input', renderPlanList);
    els.newBtn?.addEventListener('click', createPlan);
    els.newBlockBtn?.addEventListener('click', () => {
      const name = window.prompt('Blockname (z.B. Squat Block (8 Wochen)):', 'Neuer Block');
      if (!name || !name.trim()) return;
      pushUndoSnapshot('add_block');
      const key = name.trim();
      if (!state.blocksJson[key]) state.blocksJson[key] = [];
      setDirty(true);
      renderBlocks();
    });

    bindUndoOnFocus(els.title, 'edit_title');
    bindUndoOnFocus(els.focus, 'edit_focus');
    bindUndoOnFocus(els.cycleTotalWeeks, 'cycle_total_weeks');
    bindUndoOnFocus(els.cycleBlockLength, 'cycle_block_length');
    bindUndoOnFocus(els.cycleWeekOverride, 'cycle_week_override');

    els.title?.addEventListener('input', () => {
      state.planMeta.name = els.title.value;
      state.planJson.meta.title = els.title.value;
      autosizeTitleField();
      setDirty(true);
    });
    els.title?.addEventListener('focus', autosizeTitleField);
    els.title?.addEventListener('blur', autosizeTitleField);
    els.focus?.addEventListener('input', () => {
      state.planMeta.focus = els.focus.value;
      state.planJson.meta.focus = els.focus.value;
      autosizeFocusField();
      setDirty(true);
    });
    els.cycleStartDate?.addEventListener('change', () => {
      pushUndoSnapshot('cycle_start_date');
      state.planJson.meta.start_date = els.cycleStartDate.value || null;
      renderCycleCard();
      setDirty(true);
    });
    els.cycleTotalWeeks?.addEventListener('input', () => {
      const v = Math.max(1, Math.min(260, Number.parseInt(els.cycleTotalWeeks.value || '8', 10) || 8));
      state.planJson.meta.total_weeks = v;
      state.planJson.weeks = v;
      state.rulesJson = normalizeRulesJson(state.rulesJson, v);
      sanitizeSelectedWeek();
      renderCycleCard();
      renderRules();
      setDirty(true);
    });
    els.cycleBlockLength?.addEventListener('input', () => {
      const v = Math.max(1, Math.min(12, Number.parseInt(els.cycleBlockLength.value || '4', 10) || 4));
      state.planJson.meta.block_length = v;
      renderCycleCard();
      setDirty(true);
    });
    els.cycleWeekOverride?.addEventListener('input', () => {
      const raw = String(els.cycleWeekOverride.value || '').trim();
      state.planJson.meta.week_override = raw ? Math.max(1, Number.parseInt(raw, 10) || 1) : null;
      renderCycleCard();
      setDirty(true);
    });
    els.cycleLoopEnabled?.addEventListener('change', () => {
      pushUndoSnapshot('cycle_loop');
      state.planJson.meta.loop_cycle = !!els.cycleLoopEnabled.checked;
      renderCycleCard();
      setDirty(true);
    });
    els.modeFixedBtn?.addEventListener('click', () => {
      setPlanModeWithMigration('fixed_week');
    });
    els.modeRollingBtn?.addEventListener('click', () => {
      setPlanModeWithMigration('rolling_sequence');
    });
    els.periodizationEnabled?.addEventListener('change', () => {
      pushUndoSnapshot('toggle_periodization');
      state.planJson.meta.periodization_enabled = !!els.periodizationEnabled.checked;
      renderRules();
      renderEditorSurface();
      setDirty(true);
    });
    els.cycleSetToday?.addEventListener('click', () => {
      pushUndoSnapshot('cycle_set_today');
      state.planJson.meta.start_date = todayIso();
      renderCycleCard();
      setDirty(true);
    });
    els.cycleClearOverride?.addEventListener('click', () => {
      pushUndoSnapshot('cycle_clear_override');
      state.planJson.meta.week_override = null;
      renderCycleCard();
      setDirty(true);
    });

    els.saveBtn?.addEventListener('click', savePlan);

    els.parseBtn?.addEventListener('click', parseDsl);
    els.applyParseBtn?.addEventListener('click', applyParsed);
    els.exportDslBtn?.addEventListener('click', exportDslToTextarea);
    els.copyDslBtn?.addEventListener('click', async () => {
      const txt = String(els.dslExportText?.value || '');
      if (!txt.trim()) return;
      const btn = els.copyDslBtn;
      const prev = btn ? btn.textContent : null;
      try {
        await navigator.clipboard.writeText(txt);
        if (btn) {
          btn.textContent = 'Copied!';
          btn.disabled = true;
          setTimeout(() => {
            btn.textContent = prev || 'Copy';
            btn.disabled = false;
          }, 1200);
        }
      } catch (_e) {
        window.alert('Copy failed.');
      }
    });
    els.dslSegImport?.addEventListener('click', () => setDslMode('import'));
    els.dslSegExport?.addEventListener('click', () => setDslMode('export'));
    els.coachWarningsToggle?.addEventListener('click', () => {
      state.coachWarningsCollapsed = !state.coachWarningsCollapsed;
      renderCoach();
    });

    document.addEventListener('keydown', (ev) => {
      const isUndo = (ev.ctrlKey || ev.metaKey) && !ev.shiftKey && String(ev.key).toLowerCase() === 'z';
      if (!isUndo) return;
      const t = ev.target;
      if (t && (t.closest('input,textarea,select,[contenteditable]'))) return;
      ev.preventDefault();
      undoLastAction();
    });

    document.addEventListener('click', (ev) => {
      if (!ev.target.closest('.gym-plan-kebab, .gym-plan-menu')) {
        if (state.openMenuPlanId != null) {
          state.openMenuPlanId = null;
          state.openMenuAnchor = null;
          renderPlanList();
        }
      }
    });

    setInterval(() => {
      if (!state.dirty && !state.isSaving) renderSaveState();
    }, 30000);
  }

  wireEvents();
  setDslMode('import');
  loadPlans().catch((err) => {
    window.alert(`Gym-Pläne konnten nicht geladen werden: ${err.message}`);
  });
})();
