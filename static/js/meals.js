(() => {
  const root = document.querySelector('[data-meals-app]');
  if (!root) return;
  const DASHBOARD_SYNC_KEY = 'liva_dashboard_sync_v1';
  const dashboardSyncChannel = (() => {
    try {
      return typeof BroadcastChannel !== 'undefined' ? new BroadcastChannel('liva_dashboard_sync') : null;
    } catch (_) {
      return null;
    }
  })();

  const els = {
    dateInput: document.getElementById('meals-date-input'),
    dateDisplay: document.getElementById('meals-date-display'),
    prevDayBtn: document.getElementById('meals-prev-day'),
    nextDayBtn: document.getElementById('meals-next-day'),
    todayBtn: document.getElementById('meals-today-btn'),
    dayTitle: document.getElementById('meals-day-title'),
    dayLabel: document.getElementById('meals-day-label'),
    copyYesterdayBtn: document.getElementById('copy-yesterday-btn'),

    openQuickAddBtn: document.getElementById('open-quick-add-btn'),

    dayFeedList: document.getElementById('day-feed-list'),
    dayFeedCount: document.getElementById('day-feed-count'),

    sidebarMacroSummary: document.getElementById('sidebar-macro-summary'),
    summaryDateContext: document.getElementById('summary-date-context'),
    plannedMealPicks: document.getElementById('planned-meal-picks'),
    quickShortcuts: document.getElementById('quick-shortcuts'),
    recentMeals: document.getElementById('recent-meals-list'),
    recentFoods: document.getElementById('recent-foods-list'),
    mobileQuickAddBtn: document.getElementById('mobile-quickadd-btn'),

    copyDialog: document.getElementById('copy-day-modal'),
    copyCloseBtn: document.getElementById('copy-day-close-btn'),
    copyCancelBtn: document.getElementById('copy-day-cancel-btn'),
    copySaveBtn: document.getElementById('copy-day-save-btn'),
    copyList: document.getElementById('copy-day-list'),
    copyNote: document.getElementById('copy-day-note'),

    quickDialog: document.getElementById('quick-add-modal'),
    quickCloseBtn: document.getElementById('quick-add-close-btn'),
    quickTitle: document.getElementById('quick-title-input'),
    quickSlot: document.getElementById('quick-slot-input'),
    quickTime: document.getElementById('quick-time-input'),
    quickTimeDisplay: document.getElementById('quick-time-display'),
    quickModeFoodsBtn: document.getElementById('quick-mode-foods'),
    quickModeRecentsBtn: document.getElementById('quick-mode-recents'),
    quickModeMealsBtn: document.getElementById('quick-mode-meals'),
    quickModeMacrosBtn: document.getElementById('quick-mode-macros'),
    quickSearchInput: document.getElementById('quick-search-input'),
    quickSearchResults: document.getElementById('quick-search-results'),
    quickRecentResults: document.getElementById('quick-recent-results'),
    quickMealResults: document.getElementById('quick-meal-results'),
    quickMacroPanel: document.getElementById('quick-macro-panel'),
    quickMacroName: document.getElementById('quick-macro-name-input'),
    quickMacroPortion: document.getElementById('quick-macro-portion-input'),
    quickMacroUnit: document.getElementById('quick-macro-unit-input'),
    quickMacroKcal: document.getElementById('quick-macro-kcal-input'),
    quickMacroProtein: document.getElementById('quick-macro-p-input'),
    quickMacroCarbs: document.getElementById('quick-macro-c-input'),
    quickMacroFat: document.getElementById('quick-macro-f-input'),
    quickMacroSugar: document.getElementById('quick-macro-sugar-input'),
    quickItems: document.getElementById('quick-items-list'),
    quickClearBtn: document.getElementById('quick-clear-btn'),
    quickSaveBtn: document.getElementById('quick-save-btn'),

    editorDialog: document.getElementById('meal-editor-modal'),
    editorTitle: document.getElementById('editor-title'),
    editorCloseBtn: document.getElementById('editor-close-btn'),
    editorMealTitle: document.getElementById('editor-meal-title'),
    editorMealSlot: document.getElementById('editor-meal-slot'),
    editorTime: document.getElementById('editor-time'),
    editorTimeDisplay: document.getElementById('editor-time-display'),
    editorSearch: document.getElementById('editor-food-search'),
    editorSearchResults: document.getElementById('editor-search-results'),
    editorItems: document.getElementById('editor-items-list'),
    editorSaveBtn: document.getElementById('editor-save-btn'),

    foodEditorDialog: document.getElementById('food-editor-modal'),
    foodEditorForm: document.getElementById('food-editor-form'),
    foodEditorTitle: document.getElementById('food-editor-title'),
    foodEditorCloseBtn: document.getElementById('food-editor-close-btn'),
    foodEditorCancelBtn: document.getElementById('food-editor-cancel-btn'),
    foodEditorSaveBtn: document.getElementById('food-editor-save-btn'),
    foodEditorError: document.getElementById('food-editor-error'),
  };

  const state = {
    date: '',
    todayIso: '',
    day: null,
    weekPlan: null,
    makrosSettings: { active_mode: null, modes: {} },
    recents: { recent_foods: [], recent_meals: [] },
    foodLastAmountById: {},
    selected: null,
    quick: {
      mode: 'foods',
      searchResults: [],
      mealResults: [],
      searchIndex: -1,
      mealIndex: -1,
      items: [],
      replaceIndex: null,
      suppressAutoOpenUntil: 0,
      mealCache: {},
      mealCatalog: [],
    },
    editor: {
      mode: null,
      slotId: null,
      mealId: null,
      items: [],
      searchResults: [],
      searchIndex: -1,
      replaceIndex: null,
    },
    foodEditor: {
      foodId: null,
      itemIndex: null,
      unitDefault: null,
      saving: false,
      dirty: false,
    },
    undoStack: [],
    undoing: false,
    copy: { sourceDate: '', meals: [] },
  };

  let backgroundDayRefreshTimer = null;
  let quickSearchTimer = null;
  let editorSearchTimer = null;
  const foodSearchRequest = { quick: 0, editor: 0 };

  function defaultMakrosSettings() {
    return { active_mode: null, modes: {} };
  }

  function defaultRecentsPayload() {
    return { recent_foods: [], recent_meals: [] };
  }

  async function safeApi(path, options = {}, fallback = null, label = path) {
    try {
      return await api(path, options);
    } catch (err) {
      console.warn(`[meals] ${label} failed`, err);
      return fallback;
    }
  }

  function localDateIso() {
    const d = new Date();
    const y = d.getFullYear();
    const m = String(d.getMonth() + 1).padStart(2, '0');
    const day = String(d.getDate()).padStart(2, '0');
    return `${y}-${m}-${day}`;
  }

  function addDaysIso(dateIso, deltaDays) {
    const text = String(dateIso || '').trim();
    const parts = text.split('-').map((x) => Number(x));
    if (parts.length !== 3 || parts.some((x) => !Number.isFinite(x))) return localDateIso();
    const [y, m, d] = parts;
    const utc = new Date(Date.UTC(y, m - 1, d, 12, 0, 0));
    utc.setUTCDate(utc.getUTCDate() + Number(deltaDays || 0));
    const yy = utc.getUTCFullYear();
    const mm = String(utc.getUTCMonth() + 1).padStart(2, '0');
    const dd = String(utc.getUTCDate()).padStart(2, '0');
    return `${yy}-${mm}-${dd}`;
  }

  function formatGermanDate(isoDate) {
    const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(String(isoDate || ''));
    return match ? `${match[3]}.${match[2]}.${match[1]}` : 'Datum wählen';
  }

  function formatLongGermanDate(isoDate) {
    const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(String(isoDate || ''));
    if (!match) return '';
    const date = new Date(Date.UTC(Number(match[1]), Number(match[2]) - 1, Number(match[3]), 12));
    return new Intl.DateTimeFormat('de-DE', { weekday: 'long', day: '2-digit', month: 'long' }).format(date);
  }

  function syncDateDisplay() {
    if (!els.dateDisplay) return;
    els.dateDisplay.textContent = formatGermanDate(els.dateInput?.value || state.date);
    const selected = els.dateInput?.value || state.date;
    if (els.dayTitle) els.dayTitle.textContent = selected === state.todayIso ? 'Heute' : formatGermanDate(selected);
    if (els.dayLabel) els.dayLabel.textContent = formatLongGermanDate(selected);
  }

  function nowTimeText() {
    const d = new Date();
    return `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`;
  }

  function syncTimeDisplay(input, display) {
    if (!display) return;
    const match = /^(\d{2}):(\d{2})/.exec(String(input?.value || ''));
    display.textContent = match ? `${match[1]}:${match[2]}` : '--:--';
  }

  function syncDialogTimeDisplays() {
    syncTimeDisplay(els.quickTime, els.quickTimeDisplay);
    syncTimeDisplay(els.editorTime, els.editorTimeDisplay);
  }

  function moveCaretToEnd(input) {
    if (!input || !input.value || input.readOnly || input.disabled) return;
    try {
      const end = input.value.length;
      input.setSelectionRange(end, end);
    } catch (_) {
      // Native controls such as time inputs do not expose text selections.
    }
  }

  function bindTextEntryCaret() {
    const selector = 'input:not([type]), input[type="text"], input[type="search"], input[type="email"], input[type="url"], input[type="tel"]';
    document.addEventListener('focusin', (event) => {
      const input = event.target;
      if (!(input instanceof HTMLInputElement) || !input.matches(selector)) return;
      if (!input.closest('[data-meals-app], dialog.quick-add-modal, dialog.meal-editor-modal, dialog.copy-day-modal')) return;
      moveCaretToEnd(input);
    });
  }

  function defaultAmountForFood(food, lastAmount = null) {
    const remembered = Number(lastAmount);
    if (Number.isFinite(remembered) && remembered > 0) return remembered;
    const unit = String(food?.unit_default || '').trim().toLowerCase();
    if (unit === 'pcs') return 1;
    return Number(food?.common_portion_size || 100) || 100;
  }

  function parseAmount(value) {
    const raw = String(value ?? '').trim().replace(',', '.');
    if (!raw) return 0;
    const num = Number(raw);
    return Number.isFinite(num) ? num : 0;
  }

  function round1(value) {
    const num = Number(value || 0);
    if (!Number.isFinite(num)) return 0;
    return Math.round(num * 10) / 10;
  }

  function clearQuickMacroForm() {
    if (els.quickMacroName) els.quickMacroName.value = '';
    if (els.quickMacroKcal) els.quickMacroKcal.value = '';
    if (els.quickMacroProtein) els.quickMacroProtein.value = '';
    if (els.quickMacroCarbs) els.quickMacroCarbs.value = '';
    if (els.quickMacroFat) els.quickMacroFat.value = '';
    if (els.quickMacroSugar) els.quickMacroSugar.value = '';
    if (els.quickMacroPortion) els.quickMacroPortion.value = '1';
    if (els.quickMacroUnit) els.quickMacroUnit.value = 'pcs';
  }

  function resetQuickState() {
    state.quick.items = [];
    state.quick.searchResults = [];
    state.quick.mealResults = [];
    state.quick.searchIndex = -1;
    state.quick.mealIndex = -1;
    state.quick.replaceIndex = null;
    if (els.quickSearchInput) els.quickSearchInput.value = '';
    if (els.quickTitle) els.quickTitle.value = '';
    if (els.quickSlot) els.quickSlot.value = '';
    clearQuickMacroForm();
  }

  function friendlyErrorMessage(error) {
    const raw = String(error?.message || error || 'Unbekannter Fehler').trim();
    if (!raw) return 'Unbekannter Fehler';
    if (raw === 'duplicate_name') return 'Ein Food mit diesem Namen existiert bereits. Bitte nimm einen anderen Namen.';
    if (raw === 'name_required') return 'Bitte gib einen Namen ein.';
    if (raw === 'macros_required') return 'Bitte trage kcal sowie Makros ein.';
    if (raw === 'food_create_failed') return 'Das Food konnte nicht angelegt werden.';
    if (raw === 'items_required') return 'Es konnte kein Food zum Loggen erstellt werden.';
    return raw;
  }

  function suppressQuickAutoOpen(durationMs = 300) {
    state.quick.suppressAutoOpenUntil = Date.now() + Math.max(0, Number(durationMs || 0));
  }

  function shouldSuppressQuickAutoOpen() {
    return Date.now() < Number(state.quick.suppressAutoOpenUntil || 0);
  }

  function isoAtDate(timeText) {
    const t = String(timeText || '').trim() || nowTimeText();
    return `${state.date}T${t}:00`;
  }

  function clampPercent(value) {
    const x = Number(value || 0);
    if (!Number.isFinite(x)) return 0;
    return Math.max(0, Math.min(100, x));
  }

  function macroLine(macros) {
    const m = macros || {};
    return `P ${Math.round(m.p || 0)} | C ${Math.round(m.c || 0)} | F ${Math.round(m.f || 0)}`;
  }

  function itemsPreview(items) {
    if (!items?.length) return 'Keine Items';
    return items
      .slice(0, 4)
      .map((it) => {
        const amount = Number(it.amount || 0) || '';
        const unit = it.unit === 'pcs' ? ' Stk.' : (it.unit || '');
        return `${it.food_name || it.name || 'Food'} ${amount}${unit}`;
      })
      .join(' | ');
  }

  function weekdayFromIso(dateIso) {
    const text = String(dateIso || '').trim();
    const [y, m, d] = text.split('-').map((x) => Number(x));
    if (!Number.isFinite(y) || !Number.isFinite(m) || !Number.isFinite(d)) return null;
    const utc = new Date(Date.UTC(y, m - 1, d, 12, 0, 0));
    const jsDay = utc.getUTCDay(); // 0=So ... 6=Sa
    return (jsDay + 6) % 7; // 0=Mo ... 6=So
  }

  function slotHasMeal(slot) {
    return Boolean(
      slot?.meal_template_id
      || slot?.slot_meal_id
      || slot?.meal_title
      || slot?.custom_title
      || (Array.isArray(slot?.items) && slot.items.length),
    );
  }

  function sumMacros(rows) {
    return (rows || []).reduce((acc, row) => {
      const m = row?.macros || {};
      acc.kcal += Number(m.kcal || 0);
      acc.p += Number(m.p || 0);
      acc.c += Number(m.c || 0);
      acc.f += Number(m.f || 0);
      return acc;
    }, { kcal: 0, p: 0, c: 0, f: 0 });
  }

  function fallbackPlannedMealsFromWeekPlan(dateIso) {
    const weekday = weekdayFromIso(dateIso);
    if (weekday === null) return [];
    const days = state.weekPlan?.week_template?.days || [];
    const day = days.find((d) => Number(d?.weekday) === Number(weekday));
    if (!day) return [];
    return (day.slots || [])
      .filter((slot) => slotHasMeal(slot) && Number(slot?.id || 0) > 0)
      .map((slot) => {
        const servings = Number(slot?.servings || 1) || 1;
        const override = slot?.override || null;
        const mealTpl = slot?.meal_template || {};
        const macros = override && override.kcal != null
          ? {
            kcal: Number((override.kcal || 0) * servings),
            p: Number((override.p || 0) * servings),
            c: Number((override.c || 0) * servings),
            f: Number((override.f || 0) * servings),
          }
          : {
            kcal: Number((mealTpl.kcal_per_serving || 0) * servings),
            p: Number((mealTpl.p_per_serving || 0) * servings),
            c: Number((mealTpl.c_per_serving || 0) * servings),
            f: Number((mealTpl.f_per_serving || 0) * servings),
          };
        return {
          slot_id: Number(slot.id),
          slot_index: Number(slot.slot_index || 0),
          time_text: slot.time_text || slot?.slot_template?.default_time || null,
          title: slot.custom_title || slot?.meal_template?.title || slot.meal_title || slot?.slot_template?.title || 'Meal',
          meal_slot: slot?.slot_template?.title || `Meal ${Number(slot.slot_index || 0) + 1}`,
          meal_template_id: slot.meal_template_id || slot?.meal_template?.id || null,
          servings,
          items: Array.isArray(slot.items) ? slot.items : [],
          macros,
          status: 'open',
          shifted_time_text: null,
          logged_meal_id: null,
          logged_meal: null,
        };
      })
      .sort((a, b) => Number(a.slot_index || 0) - Number(b.slot_index || 0));
  }

  function statusBadge(status) {
    const value = status || 'open';
    return `<span class="status-pill status-${value}">${value}</span>`;
  }

  function parseMinutes(timeText) {
    const text = String(timeText || '').trim();
    if (!text) return null;
    const parts = text.split(':');
    if (parts.length < 2) return null;
    const h = Number(parts[0]);
    const m = Number(parts[1]);
    if (!Number.isFinite(h) || !Number.isFinite(m)) return null;
    if (h < 0 || h > 23 || m < 0 || m > 59) return null;
    return (h * 60) + m;
  }

  function plannedMealTimeText(meal) {
    return String(meal?.shifted_time_text || meal?.time_text || '').trim();
  }

  function isSelectedDayToday() {
    const today = String(state.todayIso || localDateIso());
    return String(state.date || '') === today;
  }

  function isPlannedMealLogged(meal) {
    if (!meal) return false;
    if (meal.logged_meal || meal.logged_meal_id) return true;
    const slotId = Number(meal.slot_id || 0);
    if (slotId > 0) {
      const loggedForSlot = (state.day?.logged_meals || []).some((logged) => {
        const loggedSlotId = Number(logged?.slot_id || logged?.created_from_planned_slot_id || 0);
        return loggedSlotId === slotId;
      });
      if (loggedForSlot) return true;
    }
    const status = String(meal.status || '').toLowerCase();
    return ['logged', 'changed', 'telegram_confirmed', 'manual_override'].includes(status);
  }

  function isPlannedMealOpen(meal) {
    if (!meal || isPlannedMealLogged(meal)) return false;
    const status = String(meal.status || 'open').toLowerCase();
    return status !== 'skipped';
  }

  function plannedBucket(meal) {
    if (isPlannedMealLogged(meal)) return 'done';
    const status = String(meal?.status || 'open').toLowerCase();
    if (status === 'skipped') return 'missing';
    if (status === 'shifted' || status === 'adjusted_by_core' || status === 'catch_up' || status === 'manual_override' || status === 'planned') return 'open';
    if (status !== 'open') return 'open';
    if (!isSelectedDayToday()) return 'open';
    const now = new Date();
    const nowMinutes = (now.getHours() * 60) + now.getMinutes();
    const plannedMinutes = parseMinutes(meal?.shifted_time_text || meal?.time_text || '');
    if (plannedMinutes !== null && plannedMinutes < nowMinutes) return 'missing';
    return 'open';
  }

  async function api(path, options = {}) {
    const res = await fetch(path, options);
    const raw = await res.text();
    let data = null;
    try {
      data = raw ? JSON.parse(raw) : null;
    } catch (_) {
      data = null;
    }
    if (!res.ok || (data && data.ok === false)) {
      const errMsg = (data && data.error) ? String(data.error) : `HTTP ${res.status}`;
      throw new Error(errMsg);
    }
    if (!data) {
      throw new Error(`invalid_json_response (HTTP ${res.status})`);
    }
    return data;
  }

  function hydrateFoodLastAmounts() {
    state.foodLastAmountById = {};
    for (const food of state.recents.recent_foods || []) {
      if (!food?.id) continue;
      state.foodLastAmountById[Number(food.id)] = {
        amount: Number(food.last_amount || 0) || null,
        unit: food.last_unit || food.unit_default || 'g',
      };
    }
  }

  function buildMealCatalog() {
    const out = [];
    const seen = new Set();

    function add(candidate) {
      if (!candidate) return;
      const title = String(candidate.title || '').trim();
      const items = Array.isArray(candidate.items) ? candidate.items : [];
      const templateId = Number(candidate.template_id || 0) || null;
      if (!title) return;
      if (!items.length && !templateId) return;
      const key = `${title.toLowerCase()}::${templateId || 0}::${items.length}`;
      if (seen.has(key)) return;
      seen.add(key);
      out.push({
        kind: candidate.kind || 'catalog',
        source: candidate.source || 'Planung',
        title,
        items,
        template_id: templateId,
        kcal_per_serving: Number(candidate.kcal_per_serving || candidate.kcal || 0) || 0,
      });
    }

    for (const meal of state.day?.planned_meals || []) {
      add({
        kind: 'planned_today',
        source: 'Heute geplant',
        title: meal.title,
        items: meal.items || [],
        kcal: meal?.macros?.kcal || 0,
        template_id: meal.meal_template_id || null,
      });
    }

    for (const meal of state.recents?.recent_meals || []) {
      add({
        kind: 'recent',
        source: 'Recent',
        title: meal.title,
        items: meal.items || [],
      });
    }

    for (const day of state.weekPlan?.week_template?.days || []) {
      for (const slot of day.slots || []) {
        add({
          kind: 'plan_week',
          source: `Plan ${day.label || ''}`.trim(),
          title: slot.custom_title || slot.meal_title || slot?.meal_template?.title || slot?.slot_template?.title,
          items: slot.items || [],
          template_id: slot.meal_template_id || slot?.meal_template?.id || null,
          kcal: slot?.meal_template?.kcal_per_serving || 0,
        });
      }
    }

    for (const tpl of state.weekPlan?.meal_templates || []) {
      add({
        kind: 'plan_template',
        source: 'Template',
        title: tpl.title,
        items: [],
        template_id: tpl.id,
        kcal_per_serving: tpl.kcal_per_serving || 0,
      });
    }

    state.quick.mealCatalog = out;
  }

  async function loadDay() {
    const day = await api(`/api/nutrition/logging/day?date=${encodeURIComponent(state.date)}`);
    state.day = day;
    if (!state.todayIso) {
      state.todayIso = String(day?.date || '').trim() || localDateIso();
    }

    const [recents, weekPlan, makrosSettingsResp] = await Promise.all([
      safeApi('/api/nutrition/logging/recents?limit=20', {}, defaultRecentsPayload(), 'recents'),
      safeApi('/api/nutrition/plan/week', {}, null, 'weekPlan'),
      safeApi('/api/makros/settings', {}, { settings: defaultMakrosSettings() }, 'makrosSettings'),
    ]);

    state.recents = recents || defaultRecentsPayload();
    state.weekPlan = weekPlan || null;
    state.makrosSettings = makrosSettingsResp?.settings || defaultMakrosSettings();
    if (!(state.day?.planned_meals || []).length) {
      const fallback = fallbackPlannedMealsFromWeekPlan(state.date);
      if (fallback.length) {
        state.day.planned_meals = fallback;
        state.day.planned_totals = sumMacros(fallback);
      }
    }
    hydrateFoodLastAmounts();
    buildMealCatalog();
    render();
  }

  function queueBackgroundDayRefresh(delayMs = 250) {
    if (backgroundDayRefreshTimer) {
      window.clearTimeout(backgroundDayRefreshTimer);
    }
    backgroundDayRefreshTimer = window.setTimeout(() => {
      backgroundDayRefreshTimer = null;
      loadDay().catch((err) => console.warn('Background day refresh failed', err));
    }, Math.max(0, Number(delayMs || 0)));
  }

  function applyWriteDayPayload(dayPayload, { refresh = true } = {}) {
    if (!dayPayload || dayPayload.ok === false) return;
    state.day = dayPayload;
    if (!state.todayIso) {
      state.todayIso = String(dayPayload?.date || '').trim() || localDateIso();
    }
    buildMealCatalog();
    render();
    broadcastDashboardMealSync(dayPayload);
    if (refresh) queueBackgroundDayRefresh();
  }

  function broadcastDashboardMealSync(dayPayload) {
    const payload = {
      type: 'mealplan-sync',
      date: String(dayPayload?.date || state.date || ''),
      ts: Date.now(),
    };
    try {
      localStorage.setItem(DASHBOARD_SYNC_KEY, JSON.stringify(payload));
    } catch (_) {}
    try {
      dashboardSyncChannel?.postMessage(payload);
    } catch (_) {}
  }

  function cloneForUndo(value) {
    return JSON.parse(JSON.stringify(value || null));
  }

  function pushUndo(label, run) {
    if (state.undoing || typeof run !== 'function') return;
    state.undoStack.push({ label, run });
    if (state.undoStack.length > 20) {
      state.undoStack.shift();
    }
  }

  function clearUndoStack() {
    state.undoStack = [];
  }

  function loggedIds(dayPayload) {
    return new Set((dayPayload?.logged_meals || []).map((meal) => Number(meal.id || 0)).filter(Boolean));
  }

  function findNewLoggedMeal(beforeDay, afterDay) {
    const beforeIds = loggedIds(beforeDay);
    const created = (afterDay?.logged_meals || []).filter((meal) => {
      const id = Number(meal.id || 0);
      return id > 0 && !beforeIds.has(id);
    });
    return created.length ? created[created.length - 1] : null;
  }

  function loggedMealPayload(meal) {
    return {
      date: meal?.log_date || state.date,
      log_date: meal?.log_date || state.date,
      title: meal?.title || 'Meal',
      meal_slot: meal?.meal_slot || 'frei',
      logged_at: meal?.logged_at || isoAtDate(meal?.time_text || nowTimeText()),
      custom_time_text: meal?.time_text || _timeFromIso(meal?.logged_at) || nowTimeText(),
      time_mode: 'custom',
      items: cloneForUndo(meal?.items || []),
    };
  }

  function _timeFromIso(value) {
    const text = String(value || '');
    const match = text.match(/T(\d{2}:\d{2})/);
    return match ? match[1] : '';
  }

  async function deleteLoggedMealForUndo(mealId, dateIso = state.date) {
    const payload = await api(`/api/nutrition/logging/meals/${mealId}/delete`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ date: dateIso }),
    });
    applyWriteDayPayload(payload);
  }

  async function restoreLoggedMealForUndo(meal) {
    const plannedSlotId = Number(meal?.created_from_planned_slot_id || meal?.slot_id || 0);
    let payload = null;
    if (plannedSlotId > 0 && String(meal?.source || '') === 'planned') {
      payload = await api(`/api/nutrition/logging/planned/${plannedSlotId}/log`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(loggedMealPayload(meal)),
      });
    } else {
      payload = await api('/api/nutrition/logging/free', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          ...loggedMealPayload(meal),
          source: meal?.source || 'undo_restore',
        }),
      });
    }
    applyWriteDayPayload(payload);
  }

  async function runUndo() {
    if (state.undoing || !state.undoStack.length) return;
    const action = state.undoStack.pop();
    state.undoing = true;
    try {
      await action.run();
    } catch (err) {
      window.alert(`Rückgängig fehlgeschlagen: ${friendlyErrorMessage(err)}`);
      throw err;
    } finally {
      state.undoing = false;
    }
  }

  function renderSidebarMacro() {
    if (!els.sidebarMacroSummary) return;
    const targets = state.day?.targets || {};
    const logged = state.day?.logged_totals || {};
    const remaining = state.day?.remaining || {};
    const safeInt = (value) => Math.round(Number(value || 0));

    // Same kcal status logic as /makros (static/js/makros.js:getKcalStatus), reused unchanged.
    function getModeBounds(modeSettings, target) {
      if (!modeSettings || target === null || target === undefined) return null;
      const greenLow = modeSettings.green_low;
      const greenHigh = modeSettings.green_high;
      const yellowLow = modeSettings.yellow_low;
      const yellowHigh = modeSettings.yellow_high;
      if (
        greenLow === null || greenLow === undefined
        || greenHigh === null || greenHigh === undefined
        || yellowLow === null || yellowLow === undefined
        || yellowHigh === null || yellowHigh === undefined
      ) return null;
      return {
        green_low: greenLow,
        green_high: greenHigh,
        yellow_low: yellowLow,
        yellow_high: yellowHigh,
        target,
      };
    }

    // Same rule as /makros: inside green => green, inside yellow => yellow, else red.
    function getKcalStatusFromMakros(loggedKcalValue, targetKcalValue) {
      const hasTarget = targetKcalValue !== null && targetKcalValue !== undefined;
      if (!hasTarget) return 'neutral';
      const mode = state.makrosSettings?.active_mode || 'maintenance';
      const modeSettings = state.makrosSettings?.modes?.[mode];
      const bounds = getModeBounds(modeSettings, targetKcalValue);
      if (!bounds) return 'neutral';
      const avg = Number(loggedKcalValue || 0);
      if (avg >= bounds.green_low && avg <= bounds.green_high) return 'green';
      if (avg >= bounds.yellow_low && avg <= bounds.yellow_high) return 'yellow';
      return 'red';
    }

    function getProteinStatus(loggedP, targetP) {
      const t = Number(targetP || 0);
      if (t <= 0) return 'neutral';
      const ratio = Number(loggedP || 0) / t;
      if (ratio >= 0.9) return 'green';
      if (ratio >= 0.8) return 'yellow';
      return 'red';
    }

    // Corridor logic for carbs/fat (not linear "more is better"):
    // green within +/-10% of target, yellow within +/-20%, else red.
    // Minimum absolute bands avoid over-sensitivity for low targets.
    function getCorridorStatus(loggedValue, targetValue, minGreen = 10, minYellow = 20) {
      const t = Number(targetValue || 0);
      if (t <= 0) return 'neutral';
      const v = Number(loggedValue || 0);
      const delta = Math.abs(v - t);
      const greenBand = Math.max(minGreen, t * 0.10);
      const yellowBand = Math.max(minYellow, t * 0.20);
      if (delta <= greenBand) return 'green';
      if (delta <= yellowBand) return 'yellow';
      return 'red';
    }

    const pPct = clampPercent((Number(logged.p || 0) / Math.max(1, Number(targets.p || 0))) * 100);
    const cPct = clampPercent((Number(logged.c || 0) / Math.max(1, Number(targets.c || 0))) * 100);
    const fPct = clampPercent((Number(logged.f || 0) / Math.max(1, Number(targets.f || 0))) * 100);
    const kcalPct = clampPercent((Number(logged.kcal || 0) / Math.max(1, Number(targets.kcal || 0))) * 100);
    const kcalStatus = getKcalStatusFromMakros(logged.kcal, targets.kcal);
    const proteinStatus = getProteinStatus(logged.p, targets.p);
    const carbsStatus = getCorridorStatus(logged.c, targets.c, 15, 35);
    const fatStatus = getCorridorStatus(logged.f, targets.f, 6, 12);
    if (els.summaryDateContext) els.summaryDateContext.textContent = state.date === state.todayIso ? 'Heute' : formatGermanDate(state.date);

    els.sidebarMacroSummary.innerHTML = `
      <div class="summary-kcal status-${kcalStatus}">
        <div><span>kcal</span><strong>${safeInt(logged.kcal)} <small>/ ${safeInt(targets.kcal)}</small></strong></div>
        <span class="summary-remaining">${safeInt(remaining.kcal)} offen</span>
        <div class="summary-progress"><span style="width:${kcalPct}%"></span></div>
      </div>
      <div class="summary-macros">
        <div class="summary-macro status-${proteinStatus}"><span>Protein</span><strong>${safeInt(logged.p)} <small>/ ${safeInt(targets.p)} g</small></strong><i><b style="width:${pPct}%"></b></i></div>
        <div class="summary-macro status-${carbsStatus}"><span>Carbs</span><strong>${safeInt(logged.c)} <small>/ ${safeInt(targets.c)} g</small></strong><i><b style="width:${cPct}%"></b></i></div>
        <div class="summary-macro status-${fatStatus}"><span>Fett</span><strong>${safeInt(logged.f)} <small>/ ${safeInt(targets.f)} g</small></strong><i><b style="width:${fPct}%"></b></i></div>
      </div>
    `;
  }

  function renderDayFeed() {
    if (!els.dayFeedList) return;
    const planned = (state.day?.planned_meals || [])
      .filter(isPlannedMealOpen)
      .sort((a, b) => (parseMinutes(plannedMealTimeText(a)) ?? 9999) - (parseMinutes(plannedMealTimeText(b)) ?? 9999));
    const logged = (state.day?.logged_meals || [])
      .slice()
      .sort((a, b) => (parseMinutes(a.time_text) ?? 9999) - (parseMinutes(b.time_text) ?? 9999));
    const nextPlanned = isSelectedDayToday() ? planned[0] : null;
    const laterPlanned = nextPlanned ? planned.slice(1) : planned;
    if (els.dayFeedCount) {
      const openCount = planned.length;
      const loggedCount = logged.length;
      els.dayFeedCount.textContent = `${loggedCount} gegessen · ${openCount} offen`;
    }
    if (!planned.length && !logged.length) {
      els.dayFeedList.innerHTML = '<div class="meal-empty"><strong>Noch leer</strong><span>Mit „+ Essen“ startest du dein Tageslog.</span></div>';
      return;
    }

    const plannedRow = (meal, featured = false) => {
      const title = meal.title || 'Meal';
      const kcal = Math.round(meal?.macros?.kcal || 0);
      const slotId = Number(meal.slot_id);
      return `<article class="feed-row is-planned ${featured ? 'is-next' : ''}" data-planned-id="${slotId}">
        <div class="feed-time"><time>${plannedMealTimeText(meal) || '--:--'}</time></div>
        <button type="button" class="feed-content" data-action="planned-edit" data-slot="${slotId}" aria-label="${title} anpassen">
          <strong>${title}</strong><span>${itemsPreview(meal.items || [])}</span><small>${kcal} kcal · ${macroLine(meal.macros)}</small>
        </button>
        <div class="feed-actions"><button type="button" class="meal-check" data-action="planned-log" data-slot="${slotId}" aria-label="${title} als gegessen markieren" title="Als gegessen markieren"><svg aria-hidden="true" viewBox="0 0 24 24"><path d="M12 5v14M5 12h14"></path></svg></button><details class="meal-overflow"><summary aria-label="Weitere Aktionen">•••</summary><div><button type="button" data-action="planned-edit" data-slot="${slotId}">Anpassen</button><button type="button" data-action="planned-skip" data-slot="${slotId}">Auslassen</button></div></details></div>
      </article>`;
    };

    const loggedRow = (meal) => {
      const title = meal.title || 'Meal';
      const kcal = Math.round(meal?.macros?.kcal || 0);
      const mealId = Number(meal.id);
      const historical = state.date !== state.todayIso;
      return `<article class="feed-row is-logged" data-logged-id="${mealId}">
        <div class="feed-time"><time>${meal.time_text || '--:--'}</time><span class="meal-status" title="Gegessen"><svg aria-hidden="true" viewBox="0 0 24 24"><path d="m5 12 4 4L19 7"></path></svg></span></div>
        <button type="button" class="feed-content" data-action="logged-edit" data-meal="${mealId}"><strong>${title}</strong><span>${itemsPreview(meal.items || [])}</span><small>${kcal} kcal · ${macroLine(meal.macros)}</small></button>
        <div class="feed-actions">${historical ? `<button type="button" class="copy-today" data-action="logged-copy-today" data-meal="${mealId}">Heute</button>` : ''}<details class="meal-overflow"><summary aria-label="Weitere Aktionen">•••</summary><div><button type="button" data-action="logged-edit" data-meal="${mealId}">Bearbeiten</button>${historical ? `<button type="button" data-action="logged-copy-today" data-meal="${mealId}">Auf heute kopieren</button>` : `<button type="button" data-action="logged-duplicate" data-meal="${mealId}">Wiederholen</button>`}<button type="button" class="danger" data-action="logged-delete" data-meal="${mealId}">Löschen</button></div></details></div>
      </article>`;
    };

    const empty = (text) => `<div class="day-group-empty">${text}</div>`;
    els.dayFeedList.innerHTML = `
      ${nextPlanned ? `<section class="day-next"><div class="day-group-title"><span>Als Nächstes</span><b>${plannedMealTimeText(nextPlanned) || '--:--'}</b></div>${plannedRow(nextPlanned, true)}</section>` : ''}
      <div class="day-groups ${nextPlanned ? 'has-next' : ''}">
        <section class="day-group is-eaten"><header><div><span>Gegessen</span><strong>${logged.length}</strong></div><small>${logged.length ? `${Math.round(Number(state.day?.logged_totals?.kcal || 0))} kcal geloggt` : 'Noch kein Eintrag'}</small></header><div class="day-group-rows">${logged.length ? logged.map(loggedRow).join('') : empty('Noch nichts gegessen')}</div></section>
        <section class="day-group is-open"><header><div><span>Offen</span><strong>${laterPlanned.length}</strong></div><small>${isSelectedDayToday() ? 'Heute noch geplant' : 'Noch nicht als gegessen markiert'}</small></header><div class="day-group-rows">${laterPlanned.length ? laterPlanned.map((meal) => plannedRow(meal)).join('') : empty('Nichts mehr offen')}</div></section>
      </div>`;
  }

  function renderSidebarPicks() {
    if (!els.plannedMealPicks || !els.quickShortcuts || !els.recentMeals || !els.recentFoods) return;
    const planned = (state.day?.planned_meals || []).slice(0, 8);
    els.plannedMealPicks.innerHTML = planned
      .map((m) => `<button type="button" class="chip-plan-meal" data-planned-pick="${m.slot_id}">${plannedMealTimeText(m) || '--:--'} ${m.title || 'Meal'}</button>`)
      .join('');

    const recentMeals = state.recents?.recent_meals || [];
    const recentFoods = state.recents?.recent_foods || [];
    const favFoods = recentFoods.filter((f) => !!f.is_favorite).slice(0, 4);

    const shortcutButtons = [];
    if (recentMeals[0]) {
      shortcutButtons.push(`<button type="button" data-shortcut-recent-meal="${recentMeals[0].meal_key}">Letztes Meal</button>`);
    }
    for (const f of favFoods) {
      shortcutButtons.push(`<button type="button" data-shortcut-food="${f.id}">${f.name}</button>`);
    }
    shortcutButtons.push('<button type="button" data-shortcut-open-quick="1">Custom</button>');
    els.quickShortcuts.innerHTML = shortcutButtons.join('');

    els.recentMeals.innerHTML = recentMeals.slice(0, 10)
      .map((m) => `<button type="button" data-recent-meal="${m.meal_key}">${m.title}</button>`)
      .join('');

    els.recentFoods.innerHTML = recentFoods.slice(0, 14)
      .map((f) => `<button type="button" data-recent-food="${f.id}">${f.name}</button>`)
      .join('');
  }

  function renderQuickSearchResults() {
    if (!els.quickSearchResults) return;
    if (state.quick.mode !== 'foods') {
      els.quickSearchResults.innerHTML = '';
      return;
    }
    if (!state.quick.searchResults.length) {
      const query = String(els.quickSearchInput?.value || '').trim();
      els.quickSearchResults.innerHTML = query
        ? `<button type="button" class="create-food-result" data-create-food="${query.replace(/"/g, '&quot;')}">+ „${query}“ als neues Food</button>`
        : '<div class="quick-item">Kein Food gefunden.</div>';
      return;
    }
    els.quickSearchResults.innerHTML = state.quick.searchResults.map((food, idx) => `
      <button type="button" class="search-result ${idx === state.quick.searchIndex ? 'is-active' : ''}" data-quick-food-index="${idx}">
        <span>${food.name}</span>
        <span>${Math.round(food.kcal_per_100 || 0)} kcal</span>
      </button>
    `).join('');
  }

  function renderQuickRecents() {
    if (!els.quickRecentResults) return;
    const foods = (state.recents?.recent_foods || []).slice(0, 8);
    const meals = (state.recents?.recent_meals || []).slice(0, 6);
    els.quickRecentResults.innerHTML = `
      ${meals.length ? `<section><h4>Meals</h4>${meals.map((meal) => `<button type="button" data-quick-recent-meal="${meal.meal_key}"><strong>${meal.title}</strong><span>${meal.use_count || 0}× geloggt</span></button>`).join('')}</section>` : ''}
      ${foods.length ? `<section><h4>Foods</h4>${foods.map((food) => `<button type="button" data-quick-recent-food="${food.id}"><strong>${food.name}</strong><span>${food.last_amount || food.common_portion_size || 100} ${food.last_unit || food.unit_default || 'g'}</span></button>`).join('')}</section>` : ''}
    `;
  }

  function renderQuickMealResults() {
    if (!els.quickMealResults) return;
    if (state.quick.mode !== 'meals') {
      els.quickMealResults.innerHTML = '';
      return;
    }
    if (!state.quick.mealResults.length) {
      els.quickMealResults.innerHTML = '<div class="quick-item">Keine Meals gefunden.</div>';
      return;
    }
    els.quickMealResults.innerHTML = state.quick.mealResults.map((meal, idx) => `
      <button type="button" class="search-result ${idx === state.quick.mealIndex ? 'is-active' : ''}" data-quick-meal-index="${idx}">
        <span>${meal.title} <small style="opacity:.65">(${meal.source || 'Meal'})</small></span>
        <span>${Math.round(meal.kcal_per_serving || 0)} kcal</span>
      </button>
    `).join('');
  }

  function quantityButtons(item, idx, mode) {
    const last = state.foodLastAmountById[Number(item.food_id || 0)]?.amount;
    const lastLabel = mode === 'editor' ? 'Letzte' : 'Letzte Menge';
    return `
      <div class="portion-actions">
        <button type="button" class="ghost" data-${mode}-action="half" data-index="${idx}">½</button>
        <button type="button" class="ghost" data-${mode}-action="one" data-index="${idx}">1×</button>
        <button type="button" class="ghost" data-${mode}-action="double" data-index="${idx}">2×</button>
        ${last ? `<button type="button" class="ghost" data-${mode}-action="last" data-index="${idx}">${lastLabel}</button>` : ''}
      </div>
    `;
  }

  function itemMacroTotals(item) {
    if (!item) return { kcal: 0, p: 0, c: 0, f: 0 };
    if (String(item.item_type || '').toLowerCase() === 'kcal_only' || String(item.unit || '').toLowerCase() === 'kcal') {
      return {
        kcal: Number(item.calories || item.amount || 0),
        p: Number(item.protein_override || 0),
        c: Number(item.carbs_override || 0),
        f: Number(item.fat_override || 0),
      };
    }
    const amount = Math.max(0, Number(item.amount || 0));
    const unit = String(item.unit || 'g').toLowerCase();
    const foodUnit = String(item.unit_default || 'g').toLowerCase();
    const commonPortion = Math.max(0, Number(item.common_portion_size || 0));
    const portionWeight = Math.max(0, Number(item.portion_g || 0));
    const isPiece = ['pcs', 'piece', 'pieces', 'stück'].includes(unit);
    const foodIsPiece = ['pcs', 'piece', 'pieces', 'stück'].includes(foodUnit);
    let factor = 0;
    if (foodIsPiece) {
      const portion = commonPortion > 1 ? commonPortion : portionWeight;
      if (isPiece) factor = portion > 0 ? (amount * portion) / 100 : amount;
      else if (['g', 'gram', 'grams', 'ml'].includes(unit) && portion > 0) factor = amount / portion;
    } else {
      const grams = isPiece && commonPortion > 0 ? amount * commonPortion : amount;
      factor = grams / 100;
    }
    return {
      kcal: Number(item.kcal_per_100 || 0) * factor,
      p: Number(item.p_per_100 || 0) * factor,
      c: Number(item.c_per_100 || 0) * factor,
      f: Number(item.f_per_100 || 0) * factor,
    };
  }

  function itemMacroText(item) {
    const totals = itemMacroTotals(item);
    return `${Math.round(totals.kcal)} kcal · P ${round1(totals.p)} · C ${round1(totals.c)} · F ${round1(totals.f)}`;
  }

  function refreshEditorItemMacro(idx) {
    const target = els.editorItems?.querySelector(`[data-editor-macros="${idx}"]`);
    if (target && state.editor.items[idx]) target.textContent = itemMacroText(state.editor.items[idx]);
  }

  function renderQuickItems() {
    if (!els.quickItems) return;
    els.quickItems.classList.toggle('is-empty', !state.quick.items.length);
    if (!state.quick.items.length) {
      els.quickItems.innerHTML = '<div class="quick-item">Noch keine Items ausgewählt.</div>';
      return;
    }
    els.quickItems.innerHTML = state.quick.items.map((item, idx) => `
      <div class="quick-item" data-quick-item="${idx}">
        <div class="quick-item-head">
          <strong>${item.food_name || item.name || 'Food'}</strong>
          <button type="button" class="ghost item-remove" data-quick-action="remove" data-index="${idx}" aria-label="Entfernen">×</button>
        </div>
        <div class="quick-item-stepper">
          <button type="button" class="ghost" data-quick-action="minus" data-index="${idx}" aria-label="Menge verringern">−</button>
          <input type="text" inputmode="decimal" value="${Number(item.amount || 0)}" data-key="amount">
          <span class="item-unit" aria-label="Einheit">${item.unit || 'g'}</span>
          <button type="button" class="ghost" data-quick-action="plus" data-index="${idx}" aria-label="Menge erhöhen">+</button>
        </div>
      </div>
    `).join('');
  }

  function renderEditorSearchResults() {
    if (!els.editorSearchResults) return;
    if (!state.editor.searchResults.length) {
      els.editorSearchResults.innerHTML = '<div class="quick-item">Keine Foods gefunden.</div>';
      return;
    }
    els.editorSearchResults.innerHTML = state.editor.searchResults.map((food, idx) => `
      <button type="button" class="search-result ${idx === state.editor.searchIndex ? 'is-active' : ''}" data-editor-food-index="${idx}">
        <span>${food.name}</span>
        <span>${Math.round(food.kcal_per_100 || 0)} kcal</span>
      </button>
    `).join('');
  }

  function renderEditorItems() {
    if (!els.editorItems) return;
    if (!state.editor.items.length) {
      els.editorItems.innerHTML = '<div class="quick-item">Keine Items.</div>';
      return;
    }
    els.editorItems.innerHTML = state.editor.items.map((item, idx) => `
      <div class="quick-item editor-item" data-editor-item="${idx}">
        <div class="editor-item-identity"><strong>${item.food_name || item.name || 'Food'}</strong><small data-editor-macros="${idx}">${itemMacroText(item)}</small></div>
        <div class="editor-item-controls">
          <button type="button" class="ghost editor-amount-step" data-editor-action="minus" data-index="${idx}" aria-label="Menge verringern">−</button>
          <input class="editor-amount-input" type="text" inputmode="decimal" value="${Number(item.amount || 0)}" data-editor-key="amount" data-index="${idx}" aria-label="Menge">
          <span class="editor-unit" aria-label="Einheit">${item.unit || 'g'}</span>
          <button type="button" class="ghost editor-amount-step" data-editor-action="plus" data-index="${idx}" aria-label="Menge erhöhen">+</button>
          ${quantityButtons(item, idx, 'editor')}
        </div>
        <details class="ingredient-overflow"><summary aria-label="Zutat bearbeiten">•••</summary><div>${Number(item.food_id || 0) > 0 ? `<button type="button" data-editor-action="edit-food" data-food-id="${Number(item.food_id)}" data-index="${idx}">Food bearbeiten</button>` : ''}<button type="button" data-editor-action="replace" data-index="${idx}">Ersetzen</button><button type="button" class="danger" data-editor-action="remove" data-index="${idx}">Entfernen</button></div></details>
      </div>
    `).join('');
  }

  function foodEditorFields() {
    if (!els.foodEditorForm) return null;
    return {
      name: els.foodEditorForm.elements.namedItem('name'),
      kcal: els.foodEditorForm.elements.namedItem('kcal_per_100'),
      protein: els.foodEditorForm.elements.namedItem('p_per_100'),
      carbs: els.foodEditorForm.elements.namedItem('c_per_100'),
      fat: els.foodEditorForm.elements.namedItem('f_per_100'),
      sugar: els.foodEditorForm.elements.namedItem('sugar_per_100'),
    };
  }

  function syncFoodEditorLabels() {
    const fields = foodEditorFields();
    if (!fields) return;
    const basis = state.foodEditor.unitDefault === 'pcs' ? 'Stück' : state.foodEditor.unitDefault === 'ml' ? '100 ml' : '100 g';
    const labels = {
      kcal: `Kalorien / ${basis}`,
      protein: `Protein / ${basis}`,
      carbs: `Carbs / ${basis}`,
      fat: `Fett / ${basis}`,
      sugar: `Zucker / ${basis}`,
    };
    els.foodEditorForm.querySelectorAll('[data-food-macro-label]').forEach((label) => {
      label.textContent = labels[label.dataset.foodMacroLabel] || label.textContent;
    });
  }

  function setFoodEditorError(message = '') {
    if (els.foodEditorError) els.foodEditorError.textContent = message;
  }

  function fillFoodEditor(food) {
    const fields = foodEditorFields();
    if (!fields || !food) return;
    fields.name.value = food.name || '';
    state.foodEditor.unitDefault = food.unit_default || 'g';
    fields.kcal.value = Number(food.kcal_per_100 || 0);
    fields.protein.value = Number(food.p_per_100 || 0);
    fields.carbs.value = Number(food.c_per_100 || 0);
    fields.fat.value = Number(food.f_per_100 || 0);
    fields.sugar.value = Number(food.sugar_per_100 || 0);
    syncFoodEditorLabels();
  }

  async function openFoodEditor(itemIndex) {
    const item = state.editor.items[itemIndex];
    const foodId = Number(item?.food_id || 0);
    if (!item || !foodId || !els.foodEditorDialog) return;
    state.foodEditor.foodId = foodId;
    state.foodEditor.itemIndex = itemIndex;
    state.foodEditor.dirty = false;
    setFoodEditorError('');
    fillFoodEditor(item);
    if (!els.foodEditorDialog.open) els.foodEditorDialog.showModal();
    els.foodEditorCloseBtn?.focus({ preventScroll: true });
    try {
      const data = await api('/api/nutrition/foods?limit=500');
      const food = (data.foods || []).find((candidate) => Number(candidate.id) === foodId);
      if (!food) throw new Error('not_found');
      if (state.foodEditor.foodId === foodId && els.foodEditorDialog.open && !state.foodEditor.dirty) fillFoodEditor(food);
    } catch (err) {
      console.error(err);
      setFoodEditorError('Der Food-Datensatz konnte nicht geladen werden.');
    }
  }

  function closeFoodEditor() {
    if (state.foodEditor.saving) return;
    if (els.foodEditorDialog?.open) els.foodEditorDialog.close();
    state.foodEditor.foodId = null;
    state.foodEditor.itemIndex = null;
    state.foodEditor.unitDefault = null;
    state.foodEditor.dirty = false;
    setFoodEditorError('');
  }

  function mergeUpdatedFoodIntoEditor(food) {
    state.editor.items = state.editor.items.map((item) => {
      if (Number(item.food_id || 0) !== Number(food.id || 0)) return item;
      return {
        ...item,
        food_name: food.name,
        name: food.name,
        unit: food.unit_default || item.unit || 'g',
        unit_default: food.unit_default || 'g',
        common_portion_size: food.common_portion_size || null,
        portion_g: food.portion_g || null,
        kcal_per_100: Number(food.kcal_per_100 || 0),
        p_per_100: Number(food.p_per_100 || 0),
        c_per_100: Number(food.c_per_100 || 0),
        f_per_100: Number(food.f_per_100 || 0),
        sugar_per_100: Number(food.sugar_per_100 || 0),
      };
    });
    renderEditorItems();
  }

  async function saveFoodEditor() {
    if (state.foodEditor.saving || !state.foodEditor.foodId || !els.foodEditorForm) return;
    if (!els.foodEditorForm.reportValidity()) return;
    const fields = foodEditorFields();
    const payload = {
      name: fields.name.value.trim(),
      kcal_per_100: Number(fields.kcal.value),
      p_per_100: Number(fields.protein.value),
      c_per_100: Number(fields.carbs.value),
      f_per_100: Number(fields.fat.value),
      sugar_per_100: Number(fields.sugar.value),
    };
    state.foodEditor.saving = true;
    setFoodEditorError('');
    els.foodEditorSaveBtn.disabled = true;
    els.foodEditorSaveBtn.textContent = 'Speichert …';
    try {
      const data = await api(`/api/nutrition/foods/${state.foodEditor.foodId}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      mergeUpdatedFoodIntoEditor(data.food);
      state.foodEditor.saving = false;
      closeFoodEditor();
    } catch (err) {
      console.error(err);
      const message = String(err?.message || '');
      setFoodEditorError(message === 'duplicate_name'
        ? 'Ein Food mit diesem Namen existiert bereits.'
        : 'Änderungen konnten nicht gespeichert werden. Prüfe die Werte und versuche es erneut.');
    } finally {
      state.foodEditor.saving = false;
      els.foodEditorSaveBtn.disabled = false;
      els.foodEditorSaveBtn.textContent = 'Änderungen speichern';
    }
  }

  function render() {
    if (!state.day) return;
    els.dateInput.value = state.day.date || state.date;
    state.date = els.dateInput.value;
    syncDateDisplay();
    renderSidebarMacro();
    renderDayFeed();
    renderSidebarPicks();
    renderQuickSearchResults();
    renderQuickMealResults();
    renderQuickItems();
    renderQuickRecents();
    renderEditorItems();
    if (state.editor.searchResults.length) {
      renderEditorSearchResults();
    } else {
      els.editorSearchResults.innerHTML = '';
    }
  }

  async function searchFoods(query, context) {
    const target = context === 'editor' ? 'editor' : 'quick';
    const requestId = ++foodSearchRequest[target];
    const shell = target === 'editor'
      ? els.editorSearch?.closest('.editor-search-shell')
      : els.quickSearchInput?.closest('.quick-search-shell');
    shell?.classList.add('is-searching');
    try {
      const data = await api(`/api/nutrition/logging/search/foods?q=${encodeURIComponent(query || '')}&limit=16`);
      if (requestId !== foodSearchRequest[target]) return;
      if (target === 'editor') {
        state.editor.searchResults = data.foods || [];
        state.editor.searchIndex = state.editor.searchResults.length ? 0 : -1;
        renderEditorSearchResults();
        return;
      }
      state.quick.searchResults = data.foods || [];
      state.quick.searchIndex = state.quick.searchResults.length ? 0 : -1;
      renderQuickSearchResults();
    } finally {
      if (requestId === foodSearchRequest[target]) shell?.classList.remove('is-searching');
    }
  }

  async function searchMeals(query) {
    const q = String(query || '').trim().toLowerCase();
    const local = (state.quick.mealCatalog || []).filter((m) => !q || String(m.title || '').toLowerCase().includes(q));
    state.quick.mealResults = local;
    state.quick.mealIndex = local.length ? 0 : -1;
    renderQuickMealResults();
  }

  function setQuickMode(mode) {
    const next = ['recent', 'foods', 'meals', 'macros'].includes(mode) ? mode : 'recent';
    state.quick.mode = next;
    const isRecent = next === 'recent';
    const isMeals = next === 'meals';
    const isMacros = next === 'macros';
    const isFoods = next === 'foods';
    const searchShell = els.quickSearchInput.closest('.quick-search-shell');
    els.quickModeRecentsBtn?.classList.toggle('is-active', isRecent);
    els.quickModeFoodsBtn.classList.toggle('is-active', isFoods);
    els.quickModeMealsBtn.classList.toggle('is-active', isMeals);
    if (els.quickModeMacrosBtn) els.quickModeMacrosBtn.classList.toggle('is-active', isMacros);
    els.quickSearchInput.hidden = isMacros;
    if (searchShell) searchShell.hidden = isMacros;
    if (els.quickRecentResults) els.quickRecentResults.hidden = !isRecent;
    els.quickSearchResults.hidden = !isFoods;
    els.quickMealResults.hidden = !isMeals;
    if (els.quickMacroPanel) els.quickMacroPanel.hidden = !isMacros;
    els.quickItems.hidden = isMacros;
    if (els.quickRecentResults) els.quickRecentResults.style.display = isRecent ? 'grid' : 'none';
    els.quickSearchResults.style.display = isFoods ? 'grid' : 'none';
    els.quickMealResults.style.display = isMeals ? 'grid' : 'none';
    els.quickItems.style.display = isMacros ? 'none' : 'grid';
    els.quickSearchInput.placeholder = isMeals ? 'Meal suchen …' : 'Food suchen …';
    if (els.quickSaveBtn) {
      els.quickSaveBtn.textContent = isMacros ? 'Speichern & loggen' : 'Loggen';
    }
    if (els.quickClearBtn) {
      els.quickClearBtn.textContent = isMacros ? 'Felder leeren' : 'Leeren';
    }
    if (isRecent) {
      renderQuickRecents();
    } else if (isMeals) {
      searchMeals(els.quickSearchInput.value || '').catch((err) => console.error(err));
    } else if (!isMacros) {
      searchFoods(els.quickSearchInput.value || '', 'quick').catch((err) => console.error(err));
    }
  }

  function addFoodToQuick(food, replaceIndex = null) {
    if (!food) return;
    const last = state.foodLastAmountById[Number(food.id || 0)] || {};
    const entry = {
      item_type: 'food',
      food_id: food.id,
      food_name: food.name,
      amount: defaultAmountForFood(food, last.amount),
      unit: food.unit_default || 'g',
      unit_default: food.unit_default || 'g',
      common_portion_size: food.common_portion_size || null,
      portion_g: food.portion_g || null,
      kcal_per_100: Number(food.kcal_per_100 || 0),
      p_per_100: Number(food.p_per_100 || 0),
      c_per_100: Number(food.c_per_100 || 0),
      f_per_100: Number(food.f_per_100 || 0),
    };
    if (replaceIndex !== null && state.quick.items[replaceIndex]) {
      state.quick.items[replaceIndex] = entry;
    } else {
      state.quick.items.push(entry);
    }
    renderQuickItems();
  }

  async function resolveMealTemplate(templateId) {
    const key = String(templateId || '');
    if (!key) return null;
    if (state.quick.mealCache[key]) return state.quick.mealCache[key];
    const data = await api(`/api/nutrition/meal_templates/${encodeURIComponent(key)}/resolved`);
    state.quick.mealCache[key] = data.meal || null;
    return state.quick.mealCache[key];
  }

  async function addMealToQuick(meal, replaceIndex = null) {
    if (!meal) return;
    let ingredients = Array.isArray(meal.items) ? meal.items : [];
    if (!ingredients.length && meal.template_id) {
      const resolved = await resolveMealTemplate(meal.template_id);
      if (resolved) ingredients = resolved.ingredients || [];
    }
    if (!ingredients.length) return;
    const mapped = ingredients
      .filter((it) => (Number(it.food_id || 0) > 0 || String(it.item_type || '').toLowerCase() === 'kcal_only' || String(it.unit || '').toLowerCase() === 'kcal') && Number(it.amount || it.calories || 0) > 0)
      .map((it) => ({
        item_type: (String(it.item_type || '').toLowerCase() === 'kcal_only' || String(it.unit || '').toLowerCase() === 'kcal') ? 'kcal_only' : 'food',
        food_id: Number(it.food_id || 0) || null,
        food_name: it.food_name || it.name || 'Food',
        amount: Number(it.amount || it.calories || 0),
        unit: String(it.unit || it.unit_default || 'g').toLowerCase() === 'kcal' ? 'kcal' : (it.unit || it.unit_default || 'g'),
        calories: String(it.unit || '').toLowerCase() === 'kcal' ? Number(it.amount || it.calories || 0) : null,
      }));

    if (!mapped.length) return;

    if (replaceIndex !== null && state.quick.items[replaceIndex]) {
      state.quick.items.splice(replaceIndex, 1, ...mapped);
    } else {
      state.quick.items.push(...mapped);
    }

    if (!String(els.quickTitle.value || '').trim()) {
      els.quickTitle.value = meal.title || 'Meal';
    }
    renderQuickItems();
  }

  function addFoodToEditor(food) {
    if (!food) return;
    const last = state.foodLastAmountById[Number(food.id || 0)] || {};
    const entry = {
      item_type: 'food',
      food_id: food.id,
      food_name: food.name,
      amount: defaultAmountForFood(food, last.amount),
      unit: food.unit_default || 'g',
      unit_default: food.unit_default || 'g',
      common_portion_size: food.common_portion_size || null,
      portion_g: food.portion_g || null,
      kcal_per_100: Number(food.kcal_per_100 || 0),
      p_per_100: Number(food.p_per_100 || 0),
      c_per_100: Number(food.c_per_100 || 0),
      f_per_100: Number(food.f_per_100 || 0),
    };
    if (state.editor.replaceIndex !== null && state.editor.items[state.editor.replaceIndex]) {
      state.editor.items[state.editor.replaceIndex] = entry;
      state.editor.replaceIndex = null;
    } else {
      state.editor.items.push(entry);
    }
    state.editor.searchIndex = -1;
    renderEditorItems();
    renderEditorSearchResults();
  }

  function startEditorReplace(idx) {
    if (!state.editor.items[idx]) return;
    state.editor.replaceIndex = idx;
    renderEditorItems();
    const currentName = String(state.editor.items[idx].food_name || state.editor.items[idx].name || '').trim();
    els.editorSearch.value = currentName;
    searchFoods(currentName, 'editor').catch((err) => console.error(err));
    els.editorSearch.focus();
    els.editorSearch.select();
  }

  function applyPortionAction(list, idx, action, mode) {
    const item = list[idx];
    if (!item) return;
    const base = Number(item.amount || 0);
    if (action === 'half') item.amount = Math.max(0, base * 0.5);
    if (action === 'one') item.amount = 1;
    if (action === 'double') item.amount = Math.max(0, base * 2);
    if (action === 'minus') item.amount = Math.max(0, base - (String(item.unit || '').toLowerCase() === 'pcs' ? 1 : 10));
    if (action === 'plus') item.amount = Math.max(0, base + (String(item.unit || '').toLowerCase() === 'pcs' ? 1 : 10));
    if (action === 'last') {
      const last = state.foodLastAmountById[Number(item.food_id || 0)] || {};
      if (last.amount) {
        item.amount = Number(last.amount);
        item.unit = last.unit || item.unit || 'g';
      }
    }
    if (mode === 'quick') renderQuickItems();
    if (mode === 'editor') renderEditorItems();
  }

  async function saveQuickMeal() {
    if (state.quick.mode === 'macros') {
      await saveQuickMacro();
      return;
    }
    if (!state.quick.items.length) return;
    const beforeDay = cloneForUndo(state.day);
    const payload = {
      date: state.date,
      logged_at: isoAtDate(els.quickTime.value || nowTimeText()),
      items: state.quick.items,
      title: String(els.quickTitle?.value || '').trim() || (state.quick.items.length === 1 ? (state.quick.items[0].food_name || 'Essen') : 'Meal'),
      source: 'quick_add',
    };
    const dayPayload = await api('/api/nutrition/logging/free', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    resetQuickState();
    els.quickDialog.close();
    applyWriteDayPayload(dayPayload);
    const created = findNewLoggedMeal(beforeDay, dayPayload);
    if (created?.id) {
      pushUndo('Quick Add', () => deleteLoggedMealForUndo(created.id, created.log_date || state.date));
    }
  }

  async function saveQuickMacro() {
    const name = String(els.quickMacroName?.value || '').trim();
    const protein = round1(parseAmount(els.quickMacroProtein?.value));
    const carbs = round1(parseAmount(els.quickMacroCarbs?.value));
    const fat = round1(parseAmount(els.quickMacroFat?.value));
    const sugar = round1(parseAmount(els.quickMacroSugar?.value));
    let kcal = round1(parseAmount(els.quickMacroKcal?.value));
    const portion = Math.max(0.1, parseAmount(els.quickMacroPortion?.value) || 1);
    const unit = ['g', 'ml', 'pcs'].includes(String(els.quickMacroUnit?.value || 'pcs')) ? String(els.quickMacroUnit.value) : 'pcs';

    if (!name) {
      window.alert('Speichern fehlgeschlagen: Bitte gib einen Namen ein.');
      return;
    }

    if (kcal <= 0) {
      kcal = round1((protein * 4) + (carbs * 4) + (fat * 9));
    }

    if (kcal <= 0 && protein <= 0 && carbs <= 0 && fat <= 0) {
      window.alert('Speichern fehlgeschlagen: Bitte trage mindestens kcal oder Makros ein.');
      return;
    }

    const macroFactor = unit === 'pcs' ? 1 : (100 / portion);
    const foodPayload = {
      name,
      unit_default: unit,
      portion_g: portion,
      common_portion_size: portion,
      category: 'quick_add_macro',
      mfp_search_hint: 'quick_add_macro',
      kcal_per_100: round1(kcal * macroFactor),
      p_per_100: round1(protein * macroFactor),
      c_per_100: round1(carbs * macroFactor),
      f_per_100: round1(fat * macroFactor),
      sugar_per_100: round1(sugar * macroFactor),
    };

    try {
      const createdFood = await api('/api/nutrition/foods', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(foodPayload),
      });
      const food = createdFood?.food || null;
      if (!food?.id) {
        throw new Error('food_create_failed');
      }
      const beforeDay = cloneForUndo(state.day);
      const dayPayload = await api('/api/nutrition/logging/free', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          date: state.date,
          logged_at: isoAtDate(els.quickTime.value || nowTimeText()),
          items: [{
            item_type: 'food',
            food_id: food.id,
            food_name: food.name,
            amount: portion,
            unit,
          }],
          title: name,
          source: 'quick_add_macro',
        }),
      });
      resetQuickState();
      els.quickDialog.close();
      applyWriteDayPayload(dayPayload);
      const createdMeal = findNewLoggedMeal(beforeDay, dayPayload);
      if (createdMeal?.id) {
        pushUndo('Quick Macro', () => deleteLoggedMealForUndo(createdMeal.id, createdMeal.log_date || state.date));
      }
    } catch (err) {
      window.alert(`Speichern fehlgeschlagen: ${friendlyErrorMessage(err)}`);
      throw err;
    }
  }

  async function setPlannedStatus(slotId, status, shiftedTime = null) {
    const previous = cloneForUndo((state.day?.planned_meals || []).find((meal) => Number(meal.slot_id) === Number(slotId)));
    const dateIso = state.date;
    const dayPayload = await api(`/api/nutrition/logging/planned/${slotId}/status`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ date: dateIso, status, shifted_time_text: shiftedTime }),
    });
    applyWriteDayPayload(dayPayload, { refresh: false });
    if (previous) {
      const previousStatus = previous.status || 'open';
      const previousShifted = previous.shifted_time_text || null;
      pushUndo('Planstatus', async () => {
        const restored = await api(`/api/nutrition/logging/planned/${slotId}/status`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ date: dateIso, status: previousStatus, shifted_time_text: previousShifted }),
        });
        applyWriteDayPayload(restored, { refresh: false });
      });
    }
  }

  async function logPlanned(slotId, timeMode = 'planned') {
    const beforeDay = cloneForUndo(state.day);
    const meal = (state.day?.planned_meals || []).find((m) => Number(m.slot_id) === Number(slotId));
    const payload = { date: state.date, time_mode: timeMode };
    if (timeMode === 'planned') {
      const plannedTime = plannedMealTimeText(meal);
      if (plannedTime) {
        payload.time_mode = 'custom';
        payload.custom_time_text = plannedTime;
        payload.logged_at = isoAtDate(plannedTime);
      }
    }
    const dayPayload = await api(`/api/nutrition/logging/planned/${slotId}/log`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    applyWriteDayPayload(dayPayload);
    const created = findNewLoggedMeal(beforeDay, dayPayload);
    if (created?.id) {
      pushUndo('Geplantes Meal loggen', () => deleteLoggedMealForUndo(created.id, created.log_date || state.date));
    }
  }

  async function copyMealsToDate(mealIds, targetDate) {
    const dayPayload = await api('/api/nutrition/logging/meals/copy', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ meal_ids: mealIds, target_date: targetDate }),
    });
    state.date = targetDate;
    applyWriteDayPayload(dayPayload);
    if (els.copyDialog?.open) els.copyDialog.close();
  }

  async function openCopyDay(sourceDate = addDaysIso(state.date, -1)) {
    if (!els.copyDialog || !els.copyList) return;
    const payload = await api(`/api/nutrition/logging/day?date=${encodeURIComponent(sourceDate)}`);
    state.copy.sourceDate = sourceDate;
    state.copy.meals = payload.logged_meals || [];
    const targetHasMeals = Boolean((state.day?.logged_meals || []).length);
    if (els.copyNote) {
      els.copyNote.textContent = targetHasMeals
        ? `Wird zu deinen ${state.day.logged_meals.length} vorhandenen Einträgen hinzugefügt.`
        : `${formatGermanDate(sourceDate)} → ${formatGermanDate(state.date)}`;
    }
    els.copyList.innerHTML = state.copy.meals.length ? state.copy.meals.map((meal) => `
      <label class="copy-meal-row"><input type="checkbox" value="${meal.id}" checked><span><strong>${meal.time_text || '--:--'} · ${meal.title || 'Meal'}</strong><small>${itemsPreview(meal.items || [])}</small></span><b>${Math.round(meal?.macros?.kcal || 0)} kcal</b></label>
    `).join('') : '<div class="meal-empty"><strong>Nichts zu übernehmen</strong><span>An diesem Tag wurden keine Meals geloggt.</span></div>';
    els.copySaveBtn.disabled = !state.copy.meals.length;
    els.copyDialog.showModal();
  }

  function openQuickAdd(initialQuery = '', options = {}) {
    const source = String(options?.source || 'manual');
    if (!els.quickDialog) return;
    if (source === 'top-search' && !els.quickDialog.open && shouldSuppressQuickAutoOpen()) {
      return;
    }
    if (!els.quickDialog.open) {
      els.quickDialog.showModal();
    }
    if (els.quickTime && !els.quickTime.value) {
      els.quickTime.value = nowTimeText();
    }
    syncTimeDisplay(els.quickTime, els.quickTimeDisplay);
    if (initialQuery) {
      if (state.quick.mode === 'macros' && els.quickMacroName && !String(els.quickMacroName.value || '').trim()) {
        els.quickMacroName.value = initialQuery;
      } else {
        els.quickSearchInput.value = initialQuery;
      }
    }
    if (state.quick.mode === 'meals') {
      searchMeals(els.quickSearchInput.value || '').catch((err) => console.error(err));
    } else if (state.quick.mode === 'foods') {
      searchFoods(els.quickSearchInput.value || '', 'quick').catch((err) => console.error(err));
    }
    els.quickCloseBtn?.focus({ preventScroll: true });
  }

  function closeQuickAdd() {
    suppressQuickAutoOpen();
    if (els.quickDialog.open) els.quickDialog.close();
  }

  function openEditorForPlanned(slotId) {
    const planned = (state.day?.planned_meals || []).find((m) => Number(m.slot_id) === Number(slotId));
    if (!planned) return;
    state.editor.mode = 'planned';
    state.editor.slotId = Number(slotId);
    state.editor.mealId = null;
    state.editor.items = JSON.parse(JSON.stringify(planned.items || []));
    state.editor.searchResults = [];
    state.editor.searchIndex = -1;
    state.editor.replaceIndex = null;
    els.editorTitle.textContent = 'Meal bearbeiten';
    els.editorMealTitle.value = planned.title || '';
    els.editorMealSlot.value = planned.meal_slot || '';
    els.editorTime.value = plannedMealTimeText(planned) || nowTimeText();
    syncTimeDisplay(els.editorTime, els.editorTimeDisplay);
    renderEditorItems();
    els.editorSearchResults.innerHTML = '';
    els.editorDialog.showModal();
    els.editorCloseBtn?.focus({ preventScroll: true });
  }

  function openEditorForLogged(mealId) {
    const meal = (state.day?.logged_meals || []).find((m) => Number(m.id) === Number(mealId));
    if (!meal) return;
    state.editor.mode = 'logged';
    state.editor.slotId = null;
    state.editor.mealId = Number(mealId);
    state.editor.items = JSON.parse(JSON.stringify(meal.items || []));
    state.editor.searchResults = [];
    state.editor.searchIndex = -1;
    state.editor.replaceIndex = null;
    els.editorTitle.textContent = 'Meal bearbeiten';
    els.editorMealTitle.value = meal.title || '';
    els.editorMealSlot.value = meal.meal_slot || 'frei';
    els.editorTime.value = meal.time_text || nowTimeText();
    syncTimeDisplay(els.editorTime, els.editorTimeDisplay);
    renderEditorItems();
    els.editorSearchResults.innerHTML = '';
    els.editorDialog.showModal();
    els.editorCloseBtn?.focus({ preventScroll: true });
  }

  async function saveEditor() {
    const items = state.editor.items
      .map((it) => ({
        item_type: it.item_type || 'food',
        food_id: it.food_id || null,
        food_name: it.food_name || it.name || '',
        amount: parseAmount(it.amount),
        unit: it.unit || 'g',
        calories: it.calories || null,
      }))
      .filter((it) => Number(it.amount || 0) > 0);

    if (!items.length) {
      window.alert('Speichern nicht möglich: Keine gültigen Items mit Menge > 0.');
      return;
    }

    const payload = {
      date: state.date,
      log_date: state.date,
      title: String(els.editorMealTitle.value || '').trim() || 'Meal',
      meal_slot: els.editorMealSlot.value || 'frei',
      logged_at: isoAtDate(els.editorTime.value || nowTimeText()),
      custom_time_text: els.editorTime.value || nowTimeText(),
      time_mode: 'custom',
      items,
    };

    try {
      let dayPayload = null;
      const beforeDay = cloneForUndo(state.day);
      const previousMeal = cloneForUndo(
        state.editor.mode === 'logged' && state.editor.mealId
          ? (state.day?.logged_meals || []).find((meal) => Number(meal.id) === Number(state.editor.mealId))
          : null,
      );
      if (state.editor.mode === 'planned' && state.editor.slotId) {
        dayPayload = await api(`/api/nutrition/logging/planned/${state.editor.slotId}/log`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload),
        });
      } else if (state.editor.mode === 'logged' && state.editor.mealId) {
        dayPayload = await api(`/api/nutrition/logging/meals/${state.editor.mealId}/update`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload),
        });
      }

      els.editorDialog.close();
      applyWriteDayPayload(dayPayload);
      if (state.editor.mode === 'planned') {
        const created = findNewLoggedMeal(beforeDay, dayPayload);
        if (created?.id) {
          pushUndo('Geplantes Meal bearbeiten', () => deleteLoggedMealForUndo(created.id, created.log_date || state.date));
        }
      } else if (previousMeal?.id) {
        pushUndo('Meal bearbeiten', async () => {
          const restored = await api(`/api/nutrition/logging/meals/${previousMeal.id}/update`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(loggedMealPayload(previousMeal)),
          });
          applyWriteDayPayload(restored);
        });
      }
    } catch (err) {
      const msg = String(err?.message || 'Unbekannter Fehler');
      window.alert(`Speichern fehlgeschlagen: ${msg}`);
      throw err;
    }
  }

  function bindTopEvents() {
    if (!els.dateInput || !els.prevDayBtn || !els.nextDayBtn) return;
    els.dateInput.addEventListener('change', () => {
      state.date = els.dateInput.value;
      syncDateDisplay();
      clearUndoStack();
      loadDay().catch((err) => console.error(err));
    });

    els.prevDayBtn.addEventListener('click', () => {
      state.date = addDaysIso(state.date, -1);
      clearUndoStack();
      loadDay().catch((err) => console.error(err));
    });

    els.nextDayBtn.addEventListener('click', () => {
      state.date = addDaysIso(state.date, 1);
      clearUndoStack();
      loadDay().catch((err) => console.error(err));
    });

    els.todayBtn?.addEventListener('click', () => {
      state.date = state.todayIso || localDateIso();
      clearUndoStack();
      loadDay().catch((err) => console.error(err));
    });

    if (els.openQuickAddBtn) els.openQuickAddBtn.addEventListener('click', () => openQuickAdd(''));
    if (els.mobileQuickAddBtn) els.mobileQuickAddBtn.addEventListener('click', () => openQuickAdd(''));
    els.copyYesterdayBtn?.addEventListener('click', () => openCopyDay().catch((err) => console.error(err)));
  }

  function bindDayFeedEvents() {
    if (!els.dayFeedList) return;
    els.dayFeedList.addEventListener('click', (event) => {
      const button = event.target.closest('button[data-action]');
      if (!button) return;
      const action = button.getAttribute('data-action');
      const slotId = Number(button.getAttribute('data-slot'));
      const mealId = Number(button.getAttribute('data-meal'));
      if (action === 'planned-log' && slotId) logPlanned(slotId).catch((err) => console.error(err));
      if (action === 'planned-edit' && slotId) openEditorForPlanned(slotId);
      if (action === 'planned-skip' && slotId) setPlannedStatus(slotId, 'skipped').catch((err) => console.error(err));
      if (action === 'logged-edit' && mealId) openEditorForLogged(mealId);
      if (action === 'logged-copy-today' && mealId) copyMealsToDate([mealId], state.todayIso).catch((err) => console.error(err));
      if (action === 'logged-duplicate' && mealId) {
        const beforeDay = cloneForUndo(state.day);
        api(`/api/nutrition/logging/meals/${mealId}/duplicate`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ date: state.date }) })
          .then((payload) => { applyWriteDayPayload(payload); const created = findNewLoggedMeal(beforeDay, payload); if (created?.id) pushUndo('Meal wiederholen', () => deleteLoggedMealForUndo(created.id, state.date)); })
          .catch((err) => console.error(err));
      }
      if (action === 'logged-delete' && mealId) {
        const previousMeal = cloneForUndo((state.day?.logged_meals || []).find((meal) => Number(meal.id) === mealId));
        api(`/api/nutrition/logging/meals/${mealId}/delete`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ date: state.date }) })
          .then((payload) => { applyWriteDayPayload(payload); if (previousMeal?.id) pushUndo('Meal löschen', () => restoreLoggedMealForUndo(previousMeal)); })
          .catch((err) => console.error(err));
      }
    });
  }

  function bindCopyDayEvents() {
    if (!els.copyDialog) return;
    const close = () => els.copyDialog.close();
    els.copyCloseBtn?.addEventListener('click', close);
    els.copyCancelBtn?.addEventListener('click', close);
    els.copyDialog.addEventListener('click', (event) => {
      if (event.target === els.copyDialog) close();
    });
    els.copySaveBtn?.addEventListener('click', () => {
      const ids = [...els.copyList.querySelectorAll('input[type="checkbox"]:checked')].map((input) => Number(input.value)).filter(Boolean);
      if (!ids.length) return;
      copyMealsToDate(ids, state.date).catch((err) => console.error(err));
    });
  }

  function bindSidebarPickEvents() {
    els.plannedMealPicks?.addEventListener('click', (event) => {
      const btn = event.target.closest('[data-planned-pick]');
      if (!btn) return;
      const slotId = Number(btn.getAttribute('data-planned-pick'));
      if (!slotId) return;
      logPlanned(slotId).catch((err) => console.error(err));
    });

    els.quickShortcuts?.addEventListener('click', (event) => {
      const recentBtn = event.target.closest('[data-shortcut-recent-meal]');
      if (recentBtn) {
        const mealKey = recentBtn.getAttribute('data-shortcut-recent-meal');
        const beforeDay = cloneForUndo(state.day);
        api(`/api/nutrition/logging/recents/${encodeURIComponent(mealKey)}/log`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ date: state.date }),
        }).then((payload) => {
          applyWriteDayPayload(payload);
          const created = findNewLoggedMeal(beforeDay, payload);
          if (created?.id) {
            pushUndo('Recent Meal loggen', () => deleteLoggedMealForUndo(created.id, created.log_date || state.date));
          }
        }).catch((err) => console.error(err));
        return;
      }

      const foodBtn = event.target.closest('[data-shortcut-food]');
      if (foodBtn) {
        const foodId = Number(foodBtn.getAttribute('data-shortcut-food'));
        const food = (state.recents.recent_foods || []).find((f) => Number(f.id) === foodId);
        if (food) {
          openQuickAdd('');
          addFoodToQuick({ id: food.id, name: food.name, unit_default: food.unit_default, common_portion_size: food.last_amount || 100 });
        }
        return;
      }

      const customBtn = event.target.closest('[data-shortcut-open-quick]');
      if (customBtn) {
        openQuickAdd('');
      }
    });

    els.recentMeals?.addEventListener('click', (event) => {
      const btn = event.target.closest('[data-recent-meal]');
      if (!btn) return;
      const mealKey = btn.getAttribute('data-recent-meal');
      const beforeDay = cloneForUndo(state.day);
      api(`/api/nutrition/logging/recents/${encodeURIComponent(mealKey)}/log`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ date: state.date }),
      }).then((payload) => {
        applyWriteDayPayload(payload);
        const created = findNewLoggedMeal(beforeDay, payload);
        if (created?.id) {
          pushUndo('Recent Meal loggen', () => deleteLoggedMealForUndo(created.id, created.log_date || state.date));
        }
        }).catch((err) => console.error(err));
    });

    els.recentFoods?.addEventListener('click', (event) => {
      const btn = event.target.closest('[data-recent-food]');
      if (!btn) return;
      const foodId = Number(btn.getAttribute('data-recent-food'));
      const food = (state.recents.recent_foods || []).find((f) => Number(f.id) === foodId);
      if (!food) return;
      openQuickAdd('');
      addFoodToQuick({ id: food.id, name: food.name, unit_default: food.unit_default, common_portion_size: food.last_amount || 100 });
    });
  }

  function bindQuickAddEvents() {
    if (!els.quickDialog) return;
    els.quickCloseBtn.addEventListener('click', closeQuickAdd);
    els.quickDialog.addEventListener('cancel', () => suppressQuickAutoOpen());
    els.quickDialog.addEventListener('close', () => {
      suppressQuickAutoOpen(200);
    });
    els.quickDialog.addEventListener('click', (event) => {
      if (event.target !== els.quickDialog) return;
      closeQuickAdd();
    });
    els.quickDialog.addEventListener('keydown', (event) => {
      if ((event.ctrlKey || event.metaKey) && event.key === 'Enter') {
        event.preventDefault();
        event.stopPropagation();
        if (!els.quickSaveBtn.disabled) saveQuickMeal().catch((err) => console.error(err));
        return;
      }
      if (event.altKey && ['1', '2', '3', '4'].includes(event.key)) {
        event.preventDefault();
        const mode = { '1': 'recent', '2': 'foods', '3': 'meals', '4': 'macros' }[event.key];
        setQuickMode(mode);
      }
    }, true);

    els.quickModeFoodsBtn.addEventListener('click', () => setQuickMode('foods'));
    els.quickModeRecentsBtn?.addEventListener('click', () => setQuickMode('recent'));
    els.quickModeMealsBtn.addEventListener('click', () => setQuickMode('meals'));
    if (els.quickModeMacrosBtn) {
      els.quickModeMacrosBtn.addEventListener('click', () => setQuickMode('macros'));
    }

    els.quickSearchInput.addEventListener('input', () => {
      if (state.quick.mode === 'recent') {
        setQuickMode('foods');
        return;
      }
      clearTimeout(quickSearchTimer);
      if (state.quick.mode === 'meals') {
        searchMeals(els.quickSearchInput.value || '').catch((err) => console.error(err));
      } else if (state.quick.mode === 'foods') {
        const query = els.quickSearchInput.value || '';
        quickSearchTimer = setTimeout(() => {
          searchFoods(query, 'quick').catch((err) => console.error(err));
        }, 160);
      }
    });

    els.quickSearchInput.addEventListener('keydown', (event) => {
      if (state.quick.mode === 'macros') {
        return;
      }
      if (state.quick.mode === 'meals') {
        if (!state.quick.mealResults.length) return;
        if (event.key === 'ArrowDown') {
          event.preventDefault();
          state.quick.mealIndex = (state.quick.mealIndex + 1) % state.quick.mealResults.length;
          renderQuickMealResults();
        }
        if (event.key === 'ArrowUp') {
          event.preventDefault();
          state.quick.mealIndex = state.quick.mealIndex <= 0 ? state.quick.mealResults.length - 1 : state.quick.mealIndex - 1;
          renderQuickMealResults();
        }
        if (event.key === 'Enter') {
          event.preventDefault();
          addMealToQuick(state.quick.mealResults[state.quick.mealIndex], state.quick.replaceIndex)
            .then(() => { state.quick.replaceIndex = null; })
            .catch((err) => console.error(err));
        }
        return;
      }

      if (!state.quick.searchResults.length) return;
      if (event.key === 'ArrowDown') {
        event.preventDefault();
        state.quick.searchIndex = (state.quick.searchIndex + 1) % state.quick.searchResults.length;
        renderQuickSearchResults();
      }
      if (event.key === 'ArrowUp') {
        event.preventDefault();
        state.quick.searchIndex = state.quick.searchIndex <= 0 ? state.quick.searchResults.length - 1 : state.quick.searchIndex - 1;
        renderQuickSearchResults();
      }
      if (event.key === 'Enter') {
        event.preventDefault();
        const selected = state.quick.searchResults[state.quick.searchIndex];
        if (state.quick.replaceIndex !== null) {
          addFoodToQuick(selected, state.quick.replaceIndex);
          state.quick.replaceIndex = null;
        } else {
          addFoodToQuick(selected);
        }
      }
    });

    [els.quickMacroName, els.quickMacroKcal, els.quickMacroProtein, els.quickMacroCarbs, els.quickMacroFat]
      .filter(Boolean)
      .forEach((input) => {
        input.addEventListener('keydown', (event) => {
          if (event.key !== 'Enter') return;
          event.preventDefault();
          saveQuickMeal().catch((err) => console.error(err));
        });
      });

    els.quickSearchResults.addEventListener('click', (event) => {
      const createButton = event.target.closest('[data-create-food]');
      if (createButton) {
        const name = createButton.getAttribute('data-create-food') || '';
        setQuickMode('macros');
        if (els.quickMacroName) els.quickMacroName.value = name;
        return;
      }
      const btn = event.target.closest('[data-quick-food-index]');
      if (!btn) return;
      const idx = Number(btn.getAttribute('data-quick-food-index'));
      const selected = state.quick.searchResults[idx];
      if (!selected) return;
      if (state.quick.replaceIndex !== null) {
        addFoodToQuick(selected, state.quick.replaceIndex);
        state.quick.replaceIndex = null;
      } else {
        addFoodToQuick(selected);
      }
    });

    els.quickRecentResults?.addEventListener('click', (event) => {
      const foodButton = event.target.closest('[data-quick-recent-food]');
      if (foodButton) {
        const foodId = Number(foodButton.getAttribute('data-quick-recent-food'));
        const food = (state.recents?.recent_foods || []).find((item) => Number(item.id) === foodId);
        if (food) addFoodToQuick(food);
        return;
      }
      const mealButton = event.target.closest('[data-quick-recent-meal]');
      if (mealButton) {
        const key = mealButton.getAttribute('data-quick-recent-meal');
        const meal = (state.recents?.recent_meals || []).find((item) => String(item.meal_key) === String(key));
        if (meal) addMealToQuick(meal).catch((err) => console.error(err));
      }
    });

    els.quickMealResults.addEventListener('click', (event) => {
      const btn = event.target.closest('[data-quick-meal-index]');
      if (!btn) return;
      const idx = Number(btn.getAttribute('data-quick-meal-index'));
      const selected = state.quick.mealResults[idx];
      if (!selected) return;
      addMealToQuick(selected, state.quick.replaceIndex)
        .then(() => { state.quick.replaceIndex = null; })
        .catch((err) => console.error(err));
    });

    els.quickItems.addEventListener('input', (event) => {
      const wrap = event.target.closest('[data-quick-item]');
      if (!wrap) return;
      const idx = Number(wrap.getAttribute('data-quick-item'));
      const key = event.target.getAttribute('data-key');
      if (!state.quick.items[idx] || key !== 'amount') return;
      state.quick.items[idx].amount = parseAmount(event.target.value);
    });

    els.quickItems.addEventListener('click', (event) => {
      const btn = event.target.closest('[data-quick-action]');
      if (!btn) return;
      const action = btn.getAttribute('data-quick-action');
      const idx = Number(btn.getAttribute('data-index'));
      if (!state.quick.items[idx]) return;

      if (action === 'remove') {
        state.quick.items.splice(idx, 1);
        renderQuickItems();
      }
      if (action === 'replace') {
        state.quick.replaceIndex = idx;
        els.quickSearchInput.focus();
      }
      if (['half', 'one', 'double', 'last', 'minus', 'plus'].includes(action)) {
        applyPortionAction(state.quick.items, idx, action, 'quick');
      }
    });

    els.quickClearBtn.addEventListener('click', () => {
      if (state.quick.mode === 'macros') {
        clearQuickMacroForm();
        return;
      }
      state.quick.items = [];
      if (els.quickTitle) els.quickTitle.value = '';
      if (els.quickSlot) els.quickSlot.value = '';
      renderQuickItems();
    });

    els.quickSaveBtn.addEventListener('click', () => saveQuickMeal().catch((err) => console.error(err)));
  }

  function bindEditorEvents() {
    if (!els.editorDialog) return;
    els.editorCloseBtn.addEventListener('click', () => els.editorDialog.close());
    els.editorSaveBtn.addEventListener('click', () => saveEditor().catch((err) => console.error(err)));
    els.editorDialog.addEventListener('click', (event) => {
      if (event.target === els.editorDialog) els.editorDialog.close();
    });
    els.editorDialog.addEventListener('keydown', (event) => {
      if ((event.ctrlKey || event.metaKey) && event.key === 'Enter') {
        event.preventDefault();
        event.stopPropagation();
        saveEditor().catch((err) => console.error(err));
      }
    }, true);

    els.editorSearch.addEventListener('input', () => {
      clearTimeout(editorSearchTimer);
      const query = els.editorSearch.value || '';
      if (!query.trim()) {
        foodSearchRequest.editor += 1;
        state.editor.searchResults = [];
        state.editor.searchIndex = -1;
        els.editorSearch.closest('.editor-search-shell')?.classList.remove('is-searching');
        els.editorSearchResults.innerHTML = '';
        return;
      }
      editorSearchTimer = setTimeout(() => {
        searchFoods(query, 'editor').catch((err) => console.error(err));
      }, 180);
    });

    els.editorSearch.addEventListener('keydown', (event) => {
      if (!state.editor.searchResults.length) return;
      if (event.key === 'ArrowDown') {
        event.preventDefault();
        state.editor.searchIndex = (state.editor.searchIndex + 1) % state.editor.searchResults.length;
        renderEditorSearchResults();
      }
      if (event.key === 'ArrowUp') {
        event.preventDefault();
        state.editor.searchIndex = state.editor.searchIndex <= 0 ? state.editor.searchResults.length - 1 : state.editor.searchIndex - 1;
        renderEditorSearchResults();
      }
      if (event.key === 'Enter') {
        event.preventDefault();
        addFoodToEditor(state.editor.searchResults[state.editor.searchIndex]);
        renderEditorSearchResults();
      }
    });

    els.editorSearchResults.addEventListener('click', (event) => {
      const btn = event.target.closest('[data-editor-food-index]');
      if (!btn) return;
      const idx = Number(btn.getAttribute('data-editor-food-index'));
      addFoodToEditor(state.editor.searchResults[idx]);
      renderEditorSearchResults();
    });

    els.editorItems.addEventListener('input', (event) => {
      const idx = Number(event.target.getAttribute('data-index'));
      const key = event.target.getAttribute('data-editor-key');
      if (!state.editor.items[idx] || key !== 'amount') return;
      state.editor.items[idx].amount = parseAmount(event.target.value);
      refreshEditorItemMacro(idx);
    });

    const selectEditorField = (event) => {
      const input = event.target.closest('input[data-editor-key]');
      if (!input) return;
      requestAnimationFrame(() => input.select());
    };
    els.editorItems.addEventListener('focusin', selectEditorField);
    els.editorItems.addEventListener('pointerup', (event) => {
      if (!event.target.closest('input[data-editor-key]')) return;
      event.preventDefault();
      selectEditorField(event);
    });

    els.editorItems.addEventListener('click', (event) => {
      const btn = event.target.closest('[data-editor-action]');
      if (!btn) return;
      const action = btn.getAttribute('data-editor-action');
      const idx = Number(btn.getAttribute('data-index'));
      if (!state.editor.items[idx]) return;
      if (action === 'remove') {
        state.editor.items.splice(idx, 1);
        renderEditorItems();
      }
      if (action === 'replace') {
        startEditorReplace(idx);
      }
      if (action === 'edit-food') {
        btn.closest('details')?.removeAttribute('open');
        openFoodEditor(idx).catch((err) => console.error(err));
      }
      if (['half', 'one', 'double', 'last', 'minus', 'plus'].includes(action)) {
        applyPortionAction(state.editor.items, idx, action, 'editor');
      }
    });
  }

  function bindFoodEditorEvents() {
    if (!els.foodEditorDialog || !els.foodEditorForm) return;
    els.foodEditorCloseBtn?.addEventListener('click', closeFoodEditor);
    els.foodEditorCancelBtn?.addEventListener('click', closeFoodEditor);
    els.foodEditorDialog.addEventListener('click', (event) => {
      if (event.target === els.foodEditorDialog) closeFoodEditor();
    });
    els.foodEditorDialog.addEventListener('cancel', (event) => {
      if (state.foodEditor.saving) event.preventDefault();
    });
    els.foodEditorForm.addEventListener('submit', (event) => {
      event.preventDefault();
      saveFoodEditor().catch((err) => console.error(err));
    });
    els.foodEditorForm.addEventListener('input', () => {
      state.foodEditor.dirty = true;
    });
    els.foodEditorForm.elements.namedItem('unit_default')?.addEventListener('change', syncFoodEditorLabels);
  }

  function bindKeyboard() {
    document.addEventListener('click', (event) => {
      const activeMenu = event.target.closest('details.meal-overflow, details.ingredient-overflow');
      document.querySelectorAll('details.meal-overflow[open], details.ingredient-overflow[open]').forEach((menu) => {
        if (menu !== activeMenu) menu.removeAttribute('open');
      });
    });
    document.addEventListener('keydown', (event) => {
      const tag = (event.target?.tagName || '').toLowerCase();
      const inTyping = ['input', 'textarea', 'select'].includes(tag) || event.target?.isContentEditable;
      const key = event.key.toLowerCase();

      if (key === '/' && !inTyping) {
        event.preventDefault();
        openQuickAdd('');
      }

      if ((event.ctrlKey || event.metaKey) && !event.shiftKey && key === 'z' && !inTyping) {
        event.preventDefault();
        runUndo().catch((err) => console.error(err));
      }

      if (key === 'n' && !inTyping) {
        event.preventDefault();
        openQuickAdd('');
      }

      if (event.key === 'Escape') {
        if (els.foodEditorDialog?.open) {
          event.preventDefault();
          closeFoodEditor();
        } else if (els.editorDialog.open) {
          event.preventDefault();
          els.editorDialog.close();
        } else if (els.quickDialog.open) {
          event.preventDefault();
          closeQuickAdd();
        }
      }
    });
  }

  async function init() {
    state.todayIso = localDateIso();
    state.date = state.todayIso;
    bindTopEvents();
    bindDayFeedEvents();
    bindCopyDayEvents();
    bindSidebarPickEvents();
    bindQuickAddEvents();
    bindEditorEvents();
    bindFoodEditorEvents();
    bindKeyboard();
    bindTextEntryCaret();
    [els.quickTime, els.editorTime].filter(Boolean).forEach((input) => {
      input.addEventListener('input', syncDialogTimeDisplays);
      input.addEventListener('change', syncDialogTimeDisplays);
    });
    setQuickMode('recent');
    await loadDay();
  }

  init().catch((err) => console.error(err));
})();
