(() => {
  const isMobile = window.LIVA?.isMobile
    ? window.LIVA.isMobile()
    : window.matchMedia("(max-width: 900px)").matches;
  if (isMobile) return;

  const root = document.querySelector('[data-nutrition-plan]');
  if (!root) return;

  const els = {
    slotList: document.getElementById('slot-template-list'),
    mealList: document.getElementById('meal-template-list'),
    mealSearch: document.getElementById('meal-search'),
    mealBuilderForm: document.getElementById('meal-builder-form'),
    mealSaveBtn: document.getElementById('meal-save-btn'),
    mealDeleteBtn: document.getElementById('meal-delete-btn'),
    mealAddToDayBtn: document.getElementById('meal-add-to-day-btn'),
    ingredientTray: document.getElementById('ingredient-tray'),
    addIngredientBtn: document.getElementById('add-ingredient-btn'),
    foodList: document.getElementById('food-list'),
    foodSearch: document.getElementById('food-search'),
    foodForm: document.getElementById('food-form'),
    foodLabelKcal: document.getElementById('food-label-kcal'),
    foodLabelProtein: document.getElementById('food-label-protein'),
    foodLabelCarbs: document.getElementById('food-label-carbs'),
    foodLabelSugar: document.getElementById('food-label-sugar'),
    foodLabelFat: document.getElementById('food-label-fat'),
    foodSubmitBtn: document.getElementById('food-submit-btn'),
    foodDeleteBtn: document.getElementById('food-delete-btn'),
    foodAddToDayBtn: document.getElementById('food-add-to-day-btn'),
    planDayTabs: document.getElementById('nutrition-day-tabs-v2'),
    dayGroupRibbon: document.getElementById('nutrition-day-group-ribbon'),
    dayGroupSelection: document.getElementById('nutrition-day-group-selection'),
    dayGroupActions: document.getElementById('nutrition-day-group-actions'),
    dayGroupLink: document.getElementById('nutrition-day-group-link'),
    dayGroupClear: document.getElementById('nutrition-day-group-clear'),
    dayGroupFocus: document.getElementById('nutrition-day-group-focus'),
    templateDialog: document.getElementById('nutrition-template-dialog'),
    templateDialogKicker: document.getElementById('nutrition-template-dialog-kicker'),
    templateDialogTitle: document.getElementById('nutrition-template-dialog-title'),
    templateDialogCopy: document.getElementById('nutrition-template-dialog-copy'),
    templateDialogCancel: document.getElementById('nutrition-template-dialog-cancel'),
    templateDialogConfirm: document.getElementById('nutrition-template-dialog-confirm'),
    dayTitle: document.getElementById('nutrition-day-title'),
    dayMeta: document.getElementById('nutrition-day-meta'),
    dayContent: document.getElementById('nutrition-day-content'),
    versionPill: document.getElementById('version-pill'),
    versionPillName: document.getElementById('version-pill-name'),
    versionPillDot: document.getElementById('version-pill-dot'),
    versionPicker: document.getElementById('version-picker'),
    versionSearch: document.getElementById('version-search-input'),
    segmentActive: document.getElementById('segment-active'),
    segmentArchived: document.getElementById('segment-archived'),
    versionListRecent: document.getElementById('version-list-recent'),
    versionListAll: document.getElementById('version-list-all'),
    versionListTitle: document.getElementById('version-list-title'),
    versionStatus: document.getElementById('version-status'),
    overflowBtn: document.getElementById('btn-overflow'),
    overflowMenu: document.getElementById('overflow-menu'),
    overflowActivate: document.getElementById('overflow-activate'),
    overflowArchive: document.getElementById('overflow-archive'),
    overflowDelete: document.getElementById('overflow-delete'),
    createDialog: document.getElementById('version-create-dialog'),
    createCancel: document.getElementById('create-cancel'),
    createEmpty: document.getElementById('create-empty'),
    createCopy: document.getElementById('create-copy'),
    renameDialog: document.getElementById('version-rename-dialog'),
    renameInput: document.getElementById('rename-input'),
    renameCancel: document.getElementById('rename-cancel'),
    renameSave: document.getElementById('rename-save'),
    overflowClearWeek: document.getElementById('overflow-clear-week'),
    clearWeekBtn: document.getElementById('overflow-clear-week'),
    checklistBtn: document.getElementById('export-checklist-btn'),
    weekSummary: document.getElementById('week-summary'),
    gapNotes: document.getElementById('gap-notes'),
    shoppingBtn: document.getElementById('generate-shopping-btn'),
    shoppingOutput: document.getElementById('shopping-output'),
    checklistOutput: document.getElementById('checklist-output'),
    copyShoppingBtn: document.getElementById('copy-shopping-btn'),
    copyChecklistBtn: document.getElementById('copy-checklist-btn'),
    copyMealCollection: document.getElementById('copy-meal-collection-btn'),
    deleteMealCollection: document.getElementById('delete-meal-collection-btn'),
    copyFoodCollection: document.getElementById('copy-food-collection-btn'),
    mfpModeButtons: document.querySelectorAll('[data-mfp-mode]'),
    mfpPaneExport: document.querySelector('[data-mfp-pane="export"]'),
    mfpPaneImport: document.querySelector('[data-mfp-pane="import"]'),
    mfpSegmented: document.getElementById('nutrition-mfp-segmented'),
    mfpToggleButtons: document.querySelectorAll('[data-mfp-toggle]'),
    mfpImportInput: document.getElementById('mfpImportText'),
    mfpImportError: document.getElementById('mfpImportError'),
    mfpImportBtn: document.getElementById('mfpImportBtn'),
    mfpValidateBtn: document.getElementById('mfpValidateBtn'),
    mfpImportMode: document.getElementById('mfpTargetSelect'),
    mfpImportTemplateName: document.getElementById('mfpNewName'),
    mfpImportTemplateNameWrapper: document.getElementById('mfpNewNameWrap'),
    mfpScopeSlots: document.getElementById('mfpScopeSlots'),
    mfpScopeTemplates: document.getElementById('mfpScopeTemplates'),
    mfpScopeHint: document.getElementById('mfpScopeHint'),
    mfpImportResult: document.getElementById('mfp-import-result'),
    mfpImportSummary: document.getElementById('mfp-import-summary'),
    mfpImportErrors: document.getElementById('mfp-import-errors'),
    mfpImportWarnings: document.getElementById('mfp-import-warnings'),
    mfpImportUnmatched: document.getElementById('mfp-import-unmatched'),
    templateTargetEditBtn: document.getElementById('nutrition-template-target-edit-btn'),
    templateTargetSummary: document.getElementById('nutrition-template-target-summary'),
    macroModal: document.getElementById('planning-macro-modal'),
    macroModalTabs: document.querySelectorAll('.planning-macro-modal-tab'),
    macroModalTarget: document.getElementById('planning-macro-target'),
    macroModalProtein: document.getElementById('planning-macro-protein'),
    macroModalCarbs: document.getElementById('planning-macro-carbs'),
    macroModalFat: document.getElementById('planning-macro-fat'),
    macroModalGreenLow: document.getElementById('planning-macro-green-low'),
    macroModalGreenHigh: document.getElementById('planning-macro-green-high'),
    macroModalYellowLow: document.getElementById('planning-macro-yellow-low'),
    macroModalYellowHigh: document.getElementById('planning-macro-yellow-high'),
    macroModalBandPreview: document.getElementById('planning-macro-band-preview'),
    macroModalScale: document.getElementById('planning-macro-scale'),
    macroModalScaleTooltip: document.getElementById('planning-macro-scale-tooltip'),
    macroModalScaleMin: document.getElementById('planning-macro-scale-min'),
    macroModalScaleMax: document.getElementById('planning-macro-scale-max'),
    macroModalZoneChips: document.getElementById('planning-macro-zone-chips'),
    macroModalCarbsLive: document.getElementById('planning-macro-carbs-live'),
    macroModalCarbsSub: document.getElementById('planning-macro-carbs-sub'),
    macroModalError: document.getElementById('planning-macro-modal-error'),
    macroModalSave: document.getElementById('planning-macro-settings-save'),
    macroModalCancel: document.getElementById('planning-macro-settings-cancel'),
  };

  const UNIT_OPTIONS = ['g', 'ml', 'pcs'];
  const MFP_MODE_STORAGE_KEY = 'planning_mfpbridge_mode';
  const TEMPLATE_MACRO_MODE_KEYS = ['cut', 'lean_bulk', 'maintenance', 'custom'];
  const storedMfpMode = typeof localStorage === 'undefined' ? null : localStorage.getItem(MFP_MODE_STORAGE_KEY);

  function unitSelectHtml(selected = 'g', dataAttr = '') {
    const attr = dataAttr ? ` ${dataAttr}` : '';
    return `<select class="meta-input"${attr}>${UNIT_OPTIONS.map((u) => `<option value="${u}"${u === selected ? ' selected' : ''}>${u}</option>`).join('')}</select>`;
  }

  const state = {
    plan: null,
    selectedDay: 0,
    selectedProjectionDay: 0,
    selectedSlotId: null,
    openEditorSlotId: null,
    selectedMealTemplateId: null,
    mealTemplates: [],
    foods: [],
    slotTemplates: [],
    mealIngredientsMap: {},
    foodEditId: null,
    gapOpen: false,
    weekTemplates: [],
    selectedTemplateId: null,
    activeTemplateId: null,
    activeSegment: 'active',
    dirty: false,
    lastSavedAt: null,
    lastSavedAtOverride: null,
    isSaving: false,
    selectedContextId: null,
    mfpBridgeMode: storedMfpMode === 'import' ? 'import' : 'export',
    isImportingMfp: false,
    isValidatingMfp: false,
    isSavingMeal: false,
    isSavingFood: false,
    isAddingMealToDay: false,
    isAddingFoodToDay: false,
    isClearingWeek: false,
    mfpExportOpen: {
      shopping: false,
      checklist: false,
    },
    mfpImportResult: {
      importId: null,
      summary: null,
      unmatched: [],
      stats: null,
      mode: null,
      errors: [],
      warnings: [],
      writeErrors: [],
      ok: false,
    },
    templateMacroSettings: null,
    templateMacroModalMode: 'maintenance',
    isSavingTemplateMacroSettings: false,
    templateMacroDraft: null,
    templateMacroDrag: null,
    selectedGroupDays: new Set(),
    focusedDayGroupId: null,
    isSelectingDayGroup: false,
    templateDialogAction: null,
  };
  const ingredientSaveQueue = new Map();

  function formatMacros(meal, servings = 1) {
    if (!meal) return '';
    const factor = Number(servings) || 1;
    const kcal = (Number(meal.kcal_per_serving) || 0) * factor;
    const p = (Number(meal.p_per_serving) || 0) * factor;
    const c = (Number(meal.c_per_serving) || 0) * factor;
    const s = (Number(meal.sugar_per_serving) || 0) * factor;
    const f = (Number(meal.f_per_serving) || 0) * factor;
    return `kcal ${kcal.toFixed(0)} | P ${p.toFixed(0)} | C ${c.toFixed(0)} | S ${s.toFixed(0)} | F ${f.toFixed(0)}`;
  }

  function renderVersionStatus() {
    if (!els.versionStatus) return;
    if (state.isSaving) {
      els.versionStatus.textContent = 'Speichern…';
      els.versionStatus.disabled = true;
      els.versionStatus.classList.remove('is-dirty');
      return;
    }
    if (state.dirty) {
      els.versionStatus.textContent = 'Speichern';
      els.versionStatus.disabled = false;
      els.versionStatus.classList.add('is-dirty');
      return;
    }
    els.versionStatus.textContent = formatSavedAgo();
    els.versionStatus.disabled = true;
    els.versionStatus.classList.remove('is-dirty');
  }

  function setDirty(flag) {
    state.dirty = !!flag;
    renderVersionStatus();
    if (els.versionPillDot) {
      els.versionPillDot.classList.toggle('is-dirty', state.dirty);
    }
    updateActionStates();
  }

  function updateActionStates() {
    const hasTemplate = !!state.selectedTemplateId;
    const isSelectedActive = hasTemplate && state.selectedTemplateId === state.activeTemplateId;
    if (els.overflowActivate) {
      els.overflowActivate.disabled = !hasTemplate || isSelectedActive;
      els.overflowActivate.textContent = isSelectedActive ? 'Bereits aktiv' : 'Aktivieren';
    }
    if (els.overflowArchive) els.overflowArchive.disabled = !hasTemplate;
    if (els.overflowDelete) els.overflowDelete.disabled = !hasTemplate;
    if (els.overflowClearWeek) els.overflowClearWeek.disabled = !hasTemplate;
  }

  function updateVersionName() {
    const current = state.weekTemplates.find((t) => t.id === state.selectedTemplateId);
    if (els.versionPillName) {
      els.versionPillName.textContent = current?.title || (state.plan?.week_template?.title || '—');
    }
    if (els.versionPillDot) {
      els.versionPillDot.classList.toggle('is-dirty', state.dirty);
      els.versionPillDot.classList.toggle('is-active-template', !!state.selectedTemplateId && state.selectedTemplateId === state.activeTemplateId);
    }
  }

  function getPlanTimestamp() {
    const plan = state.plan?.week_template;
    const candidates = [plan?.last_used_at, plan?.updated_at];
    for (const candidate of candidates) {
      if (!candidate) continue;
      const ts = Date.parse(candidate);
      if (!Number.isNaN(ts)) return ts;
    }
    return state.lastSavedAt || Date.now();
  }

  function formatSavedAgo() {
    const timestamp = state.lastSavedAtOverride || state.lastSavedAt;
    if (!timestamp) return 'Gespeichert';
    const diffMs = Math.max(0, Date.now() - timestamp);
    if (diffMs < 60000) return 'Gespeichert · Gerade eben';
    const totalMinutes = Math.floor(diffMs / 60000);
    if (totalMinutes < 60) return `Gespeichert · vor ${totalMinutes} min`;
    const hours = Math.floor(totalMinutes / 60);
    const minutes = totalMinutes % 60;
    if (hours < 24) {
      if (minutes === 0) return `Gespeichert · vor ${hours} h`;
      return `Gespeichert · vor ${hours} h ${minutes} min`;
    }
    const days = Math.floor(hours / 24);
    if (days < 7) {
      return `Gespeichert · vor ${days} d`;
    }
    const d = new Date(timestamp);
    const date = d.toLocaleDateString('de-DE', { day: '2-digit', month: '2-digit' });
    const time = d.toLocaleTimeString('de-DE', { hour: '2-digit', minute: '2-digit' });
    return `Gespeichert · ${date} ${time}`;
  }

  function formatRelativeTime(ts) {
    const diff = Math.max(0, Math.floor((Date.now() - ts) / 1000));
    if (diff < 60) return 'gerade eben';
    const mins = Math.floor(diff / 60);
    if (mins < 60) return `vor ${mins} min`;
    const hours = Math.floor(mins / 60);
    if (hours < 24) return `vor ${hours} h`;
    const d = new Date(ts);
    return d.toLocaleDateString('de-DE', { day: '2-digit', month: '2-digit' }) + ' ' + d.toLocaleTimeString('de-DE', { hour: '2-digit', minute: '2-digit' });
  }

  function snapshotWeek() {
    const days = state.plan?.week_template?.days || [];
    const slots = [];
    days.forEach((day) => {
      (day.slots || []).forEach((slot) => {
        slots.push({
          id: slot.id,
          meal_template_id: slot.meal_template_id || null,
          custom_title: slot.custom_title || null,
          note_text: slot.note_text || null,
          time_text: slot.time_text || null,
          override_ingredients: slot.override?.ingredients ? slot.override.ingredients.map((i) => ({
            food_id: i.food_id,
            amount: i.amount,
            unit: i.unit,
          })) : [],
        });
      });
    });
    return { slots };
  }

  async function restoreWeek(snapshot) {
    if (!snapshot || !snapshot.slots) return;
    for (const slot of snapshot.slots) {
      await patchSlot(slot.id, {
        meal_template_id: slot.meal_template_id,
        custom_title: slot.custom_title,
        note_text: slot.note_text,
        time_text: slot.time_text,
      });
      if (slot.override_ingredients && slot.override_ingredients.length) {
        await saveSlotIngredients(slot.id, slot.override_ingredients);
      } else {
        await saveSlotIngredients(slot.id, []);
      }
    }
  }

  function showSnackbar(message, onUndo) {
    const existing = document.querySelector('.snackbar');
    if (existing) existing.remove();
    const bar = document.createElement('div');
    bar.className = 'snackbar';
    bar.innerHTML = `<span>${message}</span>`;
    if (onUndo) {
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.textContent = 'Rückgängig';
      btn.addEventListener('click', async () => {
        await onUndo();
        bar.remove();
      });
      bar.appendChild(btn);
    }
    document.body.appendChild(bar);
    setTimeout(() => bar.remove(), 5000);
  }

  function confirmClearWeek() {
    return new Promise((resolve) => {
      const backdrop = document.createElement('div');
      backdrop.className = 'modal-backdrop';
      backdrop.innerHTML = `
        <div class="modal-card" role="dialog" aria-modal="true" aria-label="Woche leeren">
          <div class="modal-title">Woche wirklich leeren?</div>
          <div class="modal-body">Alle Slots und Zutaten dieser Woche werden entfernt.</div>
          <div class="modal-actions">
            <button id="modal-cancel" class="pill ghost" type="button">Abbrechen</button>
            <button id="modal-confirm" class="pill danger" type="button">Leeren</button>
          </div>
        </div>
      `;
      document.body.appendChild(backdrop);
      backdrop.querySelector('#modal-cancel').addEventListener('click', () => {
        backdrop.remove();
        resolve(false);
      });
      backdrop.querySelector('#modal-confirm').addEventListener('click', () => {
        backdrop.remove();
        resolve(true);
      });
      backdrop.addEventListener('click', (e) => {
        if (e.target === backdrop) {
          backdrop.remove();
          resolve(false);
        }
      });
    });
  }

  async function showNewVersionDialog() {
    return new Promise((resolve) => {
      const backdrop = document.createElement('div');
      backdrop.className = 'modal-backdrop';
      backdrop.innerHTML = `
        <div class="modal-card" role="dialog" aria-modal="true" aria-label="Neue Version">
          <div class="modal-title">Neue Version</div>
          <div class="modal-body">Wie soll die Version starten?</div>
          <div class="modal-actions">
            <button id="modal-empty" class="pill ghost" type="button">Leer</button>
            <button id="modal-copy" class="pill primary" type="button">Kopie</button>
          </div>
        </div>
      `;
      document.body.appendChild(backdrop);
      backdrop.querySelector('#modal-empty').addEventListener('click', () => {
        backdrop.remove();
        resolve('empty');
      });
      backdrop.querySelector('#modal-copy').addEventListener('click', () => {
        backdrop.remove();
        resolve('copy');
      });
      backdrop.addEventListener('click', (e) => {
        if (e.target === backdrop) {
          backdrop.remove();
          resolve(null);
        }
      });
    });
  }

  async function showDirtyDialog(targetTitle) {
    return new Promise((resolve) => {
      const backdrop = document.createElement('div');
      backdrop.className = 'modal-backdrop';
      backdrop.innerHTML = `
        <div class="modal-card" role="dialog" aria-modal="true" aria-label="Ungespeichert">
          <div class="modal-title">Ungespeicherte Änderungen</div>
          <div class="modal-body">In '${state.plan?.week_template?.title || 'Version'}'. Wechseln zu '${targetTitle}'?</div>
          <div class="modal-actions">
            <button id="modal-cancel" class="pill ghost" type="button">Abbrechen</button>
            <button id="modal-discard" class="pill ghost danger" type="button">Verwerfen</button>
            <button id="modal-save" class="pill primary" type="button">Speichern</button>
          </div>
        </div>
      `;
      document.body.appendChild(backdrop);
      backdrop.querySelector('#modal-cancel').addEventListener('click', () => {
        backdrop.remove();
        resolve(null);
      });
      backdrop.querySelector('#modal-discard').addEventListener('click', () => {
        backdrop.remove();
        resolve('discard');
      });
      backdrop.querySelector('#modal-save').addEventListener('click', () => {
        backdrop.remove();
        resolve('save');
      });
      backdrop.addEventListener('click', (e) => {
        if (e.target === backdrop) {
          backdrop.remove();
          resolve(null);
        }
      });
    });
  }

  function buildUniqueTitle(base) {
    const existing = new Set(state.weekTemplates.map((t) => (t.title || '').trim()).filter(Boolean));
    if (!existing.has(base)) return base;
    let idx = 2;
    let candidate = `${base} ${idx}`;
    while (existing.has(candidate)) {
      idx += 1;
      candidate = `${base} ${idx}`;
    }
    return candidate;
  }

  async function onSaveVersion() {
    if (state.isSaving || !state.selectedTemplateId) return;
    state.isSaving = true;
    setDirty(state.dirty);
    try {
      await savePendingPlanEdits();
      await saveTemplateTitle();
      showSnackbar('Gespeichert');
    } catch (err) {
      console.error(err);
      showSnackbar('Speichern fehlgeschlagen');
    } finally {
      state.isSaving = false;
      setDirty(state.dirty);
    }
  }

  function normalizedTextValue(value) {
    const text = String(value || '').replace(/\u00a0/g, ' ');
    const trimmed = text.trim();
    return trimmed || null;
  }

  function ingredientsComparable(ingredients) {
    return (ingredients || []).map((item) => ({
      food_id: item?.food_id == null ? null : Number(item.food_id),
      amount: item?.amount == null ? null : Number(item.amount),
      unit: normalizedTextValue(item?.unit),
      item_type: normalizedTextValue(item?.item_type),
      calories: item?.calories == null ? null : Number(item.calories),
      food_name: normalizedTextValue(item?.food_name),
      name: normalizedTextValue(item?.name),
    }));
  }

  function collectOpenEditorIngredients() {
    const editor = els.dayContent?.querySelector('.nutrition-slot-card.is-expanded .nutrition-meal-editor');
    if (!editor) return [];
    return Array.from(editor.querySelectorAll('.nutrition-ingredient-row')).map((row) => {
      const foodEl = row.querySelector('.ingredient-name[data-food-id]');
      const amountInput = row.querySelector('.ingredient-amount');
      const unitEl = row.querySelector('.ingredient-unit');
      const foodId = Number(foodEl?.getAttribute('data-food-id') || 0);
      const unit = normalizedTextValue(unitEl?.textContent) || 'g';
      if (foodId) {
        return {
          food_id: foodId,
          food_name: normalizedTextValue(foodEl?.textContent),
          amount: Number(amountInput?.value || 0),
          unit,
        };
      }
      return {
        item_type: 'kcal_only',
        name: normalizedTextValue(foodEl?.textContent) || 'Kcal',
        food_name: normalizedTextValue(foodEl?.textContent) || 'Kcal',
        amount: Number(amountInput?.value || 0),
        calories: Number(amountInput?.value || 0),
        unit: 'kcal',
      };
    });
  }

  async function savePendingPlanEdits() {
    const pending = [];
    const slotCards = Array.from(document.querySelectorAll('.nutrition-slot-card[data-slot-id]'));
    slotCards.forEach((card) => {
      const slotId = Number(card.dataset.slotId || 0);
      if (!slotId) return;
      const slot = getSlotById(slotId);
      if (!slot) return;

      const nextPatch = {};
      const mealTitleEl = card.querySelector(`[data-slot-meal-title="${slotId}"]`);
      if (mealTitleEl) {
        const nextTitle = normalizedTextValue(mealTitleEl.textContent);
        const currentTitle = normalizedTextValue(slot.custom_title);
        if (nextTitle !== currentTitle) {
          nextPatch.custom_title = nextTitle;
        }
      }

      const noteEl = card.querySelector(`[data-slot-note="${slotId}"]`);
      if (noteEl) {
        const nextNote = normalizedTextValue(noteEl.textContent);
        const currentNote = normalizedTextValue(slot.note_text);
        if (nextNote !== currentNote) {
          nextPatch.note_text = nextNote;
        }
      }

      const timeEl = card.querySelector(`[data-slot-time="${slotId}"]`);
      if (timeEl) {
        const nextTime = normalizedTextValue(timeEl.textContent);
        const currentTime = normalizedTextValue(slot.time_text);
        if (nextTime !== currentTime) {
          nextPatch.time_text = nextTime;
        }
      }

      if (Object.keys(nextPatch).length > 0) {
        pending.push(() => patchSlot(slotId, nextPatch));
      }
    });

    const editorSlotId = state.openEditorSlotId;
    if (editorSlotId && els.dayContent?.querySelector('.nutrition-meal-editor')) {
      const slot = getSlotById(editorSlotId);
      if (slot) {
        const editorIngredients = collectOpenEditorIngredients();
        const currentIngredients = slot.override?.ingredients || [];
        if (JSON.stringify(ingredientsComparable(editorIngredients)) !== JSON.stringify(ingredientsComparable(currentIngredients))) {
          pending.push(() => saveSlotIngredients(editorSlotId, editorIngredients));
        }
      }
    }

    for (const commit of pending) {
      await commit();
    }
  }

  async function onDuplicateVersion(templateId = null) {
    const sourceId = templateId || state.selectedTemplateId;
    if (!sourceId) return;
    const current = state.weekTemplates.find((t) => t.id === sourceId);
    const title = buildUniqueTitle(`${current?.title || 'Version'} Kopie`);
    const res = await fetch('/api/nutrition/plan/templates', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ title, source_template_id: sourceId, set_active: false }),
    });
    const data = await res.json();
    if (!res.ok || data.ok === false) {
      showSnackbar('Duplizieren fehlgeschlagen');
      return;
    }
    state.selectedTemplateId = data.template?.id || null;
    resetSelectionsForTemplateSwitch();
    await loadTemplates();
    await loadPlan(state.selectedTemplateId);
    showSnackbar('Dupliziert');
  }

  function onOpenNewVersionDialog() {
    if (!els.createDialog) return;
    els.createDialog.removeAttribute('hidden');
    els.createDialog.querySelector('button')?.focus();
  }

  async function onCreateVersion(mode, triggerEl = null) {
    if (!mode) return;
    if (triggerEl) {
      triggerEl.setAttribute('disabled', 'true');
      triggerEl.dataset.originalLabel = triggerEl.textContent;
      triggerEl.textContent = 'Erstelle…';
    }
    const base = mode === 'copy' ? `${state.plan?.week_template?.title || 'Version'} Kopie` : 'Neue Version';
    const title = buildUniqueTitle(base);
    const body = { title, set_active: false };
    if (mode === 'copy' && state.selectedTemplateId) body.source_template_id = state.selectedTemplateId;
    const res = await fetch('/api/nutrition/plan/templates', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    const data = await res.json();
    if (!res.ok || data.ok === false) {
      showSnackbar('Erstellen fehlgeschlagen');
      if (triggerEl) {
        triggerEl.removeAttribute('disabled');
        triggerEl.textContent = triggerEl.dataset.originalLabel || triggerEl.textContent;
        delete triggerEl.dataset.originalLabel;
      }
      return;
    }
    state.selectedTemplateId = data.template?.id || null;
    resetSelectionsForTemplateSwitch();
    await loadTemplates();
    await loadPlan(state.selectedTemplateId);
    els.createDialog?.setAttribute('hidden', 'true');
    showSnackbar('Version erstellt');
    if (triggerEl) {
      triggerEl.removeAttribute('disabled');
      triggerEl.textContent = triggerEl.dataset.originalLabel || triggerEl.textContent;
      delete triggerEl.dataset.originalLabel;
    }
  }

  async function onArchiveVersion(templateId = null) {
    const id = templateId || state.selectedTemplateId;
    if (!id) return;
    const name = state.weekTemplates.find((t) => t.id === id)?.title || 'Version';
    if (!confirm(`Version '${name}' archivieren?`)) return;
    const res = await fetch(`/api/nutrition/plan/templates/${id}/archive`, { method: 'POST' });
    const data = await res.json();
    if (!res.ok || data.ok === false) {
      showSnackbar('Archivieren fehlgeschlagen');
      return;
    }
    await loadTemplates();
    if (state.activeTemplateId === id) {
      const next = state.weekTemplates.find((t) => !t.archived_at && t.id !== id) || null;
      if (next) {
        await onActivateVersion(next.id, { silent: true });
      }
    }
    if (state.selectedTemplateId === id) {
      const next = state.weekTemplates.find((t) => !t.archived_at) || null;
      if (next) await switchTemplate(next.id);
    }
    showSnackbar('Archiviert');
  }

  async function onRestoreVersion(templateId) {
    if (!templateId) return;
    const res = await fetch(`/api/nutrition/plan/templates/${templateId}/restore`, { method: 'POST' });
    const data = await res.json();
    if (!res.ok || data.ok === false) {
      showSnackbar('Wiederherstellen fehlgeschlagen');
      return;
    }
    await loadTemplates();
    showSnackbar('Wiederhergestellt');
  }

  async function onDeleteVersion(templateId = null) {
    const id = templateId || state.selectedTemplateId;
    if (!id) return;
    const name = state.weekTemplates.find((t) => t.id === id)?.title || 'Version';
    if (!confirm(`Version '${name}' endgültig löschen?`)) return;
    const res = await fetch(`/api/nutrition/plan/templates/${id}`, { method: 'DELETE' });
    const data = await res.json();
    if (!res.ok || data.ok === false) {
      showSnackbar('Löschen fehlgeschlagen');
      return;
    }
    await loadTemplates();
    if (state.activeTemplateId === id) {
      const next = state.weekTemplates.find((t) => !t.archived_at && t.id !== id) || null;
      if (next) {
        await onActivateVersion(next.id, { silent: true });
      }
    }
    if (state.selectedTemplateId === id) {
      const next = state.weekTemplates.find((t) => !t.archived_at) || null;
      if (next) await switchTemplate(next.id);
    }
    showSnackbar('Gelöscht');
  }

  async function onActivateVersion(templateId = null, options = {}) {
    const id = templateId || state.selectedTemplateId;
    if (!id || id === state.activeTemplateId) return;
    const res = await fetch(`/api/nutrition/plan/templates/${id}/set_active`, { method: 'POST' });
    const data = await res.json();
    if (!res.ok || data.ok === false) {
      showSnackbar('Aktivieren fehlgeschlagen');
      return;
    }
    state.activeTemplateId = id;
    await loadTemplates();
    if (!options.silent) {
      showSnackbar('Version aktiviert');
    }
  }

  async function confirmDirtySwitch(targetTitle) {
    if (!state.dirty) return true;
    const decision = await showDirtyDialog(targetTitle);
    if (decision === 'save') {
      try {
        await savePendingPlanEdits();
        await saveTemplateTitle();
        return true;
      } catch {
        return false;
      }
    }
    if (decision === 'discard') {
      setDirty(false);
      return true;
    }
    return false;
  }

  function resolveGrams(amount, unit, commonPortion) {
    const a = Number(amount) || 0;
    const u = String(unit || 'g').toLowerCase();
    if (u === 'g' || u === 'ml') return a;
    if (u === 'pcs') {
      const portion = Number(commonPortion) || 0;
      return portion > 0 ? a * portion : a;
    }
    return a;
  }

  function foodMacroFactor(food, amount, unit) {
    const a = Number(amount) || 0;
    const normalizedUnit = String(unit || 'g').toLowerCase();
    const defaultUnit = String(food?.unit_default || 'g').toLowerCase();
    if (defaultUnit === 'pcs') {
      if (normalizedUnit === 'pcs') return a;
      const portion = Number(food?.common_portion_size || food?.portion_g) || 0;
      if ((normalizedUnit === 'g' || normalizedUnit === 'ml') && portion > 0) return a / portion;
      return a;
    }
    const grams = resolveGrams(a, normalizedUnit, food?.common_portion_size || food?.portion_g);
    return grams / 100;
  }

  function computeIngredientsMacros(ingredients) {
    const totals = { kcal: 0, p: 0, c: 0, sugar: 0, f: 0 };
    (ingredients || []).forEach((item) => {
      if (item?.item_type === 'kcal_only' || String(item?.unit || '').toLowerCase() === 'kcal') {
        totals.kcal += Number(item.calories ?? item.amount) || 0;
        return;
      }
      const food = state.foods.find((f) => f.id === Number(item.food_id));
      if (!food) return;
      const factor = foodMacroFactor(food, item.amount, item.unit);
      totals.kcal += (Number(food.kcal_per_100) || 0) * factor;
      totals.p += (Number(food.p_per_100) || 0) * factor;
      totals.c += (Number(food.c_per_100) || 0) * factor;
      totals.sugar += (Number(food.sugar_per_100) || 0) * factor;
      totals.f += (Number(food.f_per_100) || 0) * factor;
    });
    return totals;
  }

  function computeItemMacros(item) {
    if (!item) return { kcal: 0, p: 0, c: 0, sugar: 0, f: 0 };
    if (item.item_type === 'kcal_only' || String(item.unit || '').toLowerCase() === 'kcal') {
      return { kcal: Number(item.calories ?? item.amount) || 0, p: 0, c: 0, sugar: 0, f: 0 };
    }
    const food = state.foods.find((f) => f.id === Number(item.food_id));
    if (!food) return { kcal: null, p: null, c: null, sugar: null, f: null };
    const factor = foodMacroFactor(food, item.amount, item.unit);
    return {
      kcal: (Number(food.kcal_per_100) || 0) * factor,
      p: (Number(food.p_per_100) || 0) * factor,
      c: (Number(food.c_per_100) || 0) * factor,
      sugar: (Number(food.sugar_per_100) || 0) * factor,
      f: (Number(food.f_per_100) || 0) * factor,
    };
  }

  function formatMacroLine(m) {
    const val = (v) => (v == null || !Number.isFinite(Number(v)) ? '–' : Math.round(Number(v)));
    return `kcal ${val(m.kcal)} | P ${val(m.p)} | C ${val(m.c)} | S ${val(m.sugar)} | F ${val(m.f)}`;
  }

  function joinFoodNames(ingredients) {
    const names = [];
    (ingredients || []).forEach((item) => {
      const name = (item.food_name || item.name || '').trim();
      if (!name) return;
      if (!names.includes(name)) names.push(name);
    });
    return names.join(' + ');
  }

  function getWeekSlotTitle(slot) {
    if (!slot) return 'Meal';
    // `custom_title` is the day-template's own name.  It must win even when
    // an older API worker still sends a stale derived `display_title`.
    return slot.custom_title
      || slot.display_title
      || slot.meal_title
      || slot.title
      || slot.name
      || slot.meal_template?.title
      || 'Meal';
  }

  function clamp01(value) {
    const n = Number(value);
    if (!Number.isFinite(n)) return 0;
    if (n < 0) return 0;
    if (n > 1) return 1;
    return n;
  }

  function formatNum(value) {
    if (value == null || !Number.isFinite(Number(value))) return '--';
    return Math.round(Number(value)).toString();
  }

  function formatAmount(value) {
    const n = Number(value);
    if (!Number.isFinite(n)) return '';
    if (Math.abs(n - Math.round(n)) < 0.001) return String(Math.round(n));
    return n.toFixed(1).replace(/\.0$/, '');
  }

  function setText(el, value) {
    if (!el) return;
    el.textContent = value == null ? '' : String(value);
  }

  function setInvalid(input, isInvalid) {
    if (!input) return;
    input.classList.toggle('is-invalid', !!isInvalid);
  }

  function toNumber(value) {
    if (value === null || value === undefined || value === '') return null;
    const num = Number(value);
    return Number.isFinite(num) ? num : null;
  }

  function formatModeLabel(mode) {
    if (mode === 'cut') return 'Cut';
    if (mode === 'lean_bulk') return 'Lean Bulk';
    if (mode === 'maintenance') return 'Maintenance';
    return 'Custom';
  }

  function clampValue(value, min, max) {
    return Math.min(max, Math.max(min, value));
  }

  function buildDefaultTemplateMacroSettings() {
    const modes = {};
    TEMPLATE_MACRO_MODE_KEYS.forEach((key) => {
      modes[key] = {
        kcal_target: null,
        protein_target: null,
        carbs_target: null,
        fat_target: null,
        green_low: null,
        green_high: null,
        yellow_low: null,
        yellow_high: null,
      };
    });
    return { active_mode: 'maintenance', modes };
  }

  function normalizeTemplateMacroSettings(settings) {
    const base = buildDefaultTemplateMacroSettings();
    const source = settings && typeof settings === 'object' ? settings : {};
    const activeMode = TEMPLATE_MACRO_MODE_KEYS.includes(source.active_mode) ? source.active_mode : 'maintenance';
    const modeMap = source.modes && typeof source.modes === 'object' ? source.modes : {};
    TEMPLATE_MACRO_MODE_KEYS.forEach((key) => {
      const srcMode = modeMap[key] && typeof modeMap[key] === 'object' ? modeMap[key] : {};
      const kcalTarget = toNumber(srcMode.kcal_target);
      const proteinTarget = toNumber(srcMode.protein_target);
      const fatTarget = toNumber(srcMode.fat_target);
      base.modes[key] = {
        kcal_target: kcalTarget,
        protein_target: proteinTarget,
        carbs_target: toNumber(srcMode.carbs_target),
        fat_target: fatTarget,
        green_low: toNumber(srcMode.green_low),
        green_high: toNumber(srcMode.green_high),
        yellow_low: toNumber(srcMode.yellow_low),
        yellow_high: toNumber(srcMode.yellow_high),
      };
      if (base.modes[key].carbs_target == null) {
        base.modes[key].carbs_target = computeMacroCarbs(kcalTarget, proteinTarget, fatTarget);
      }
    });
    base.active_mode = activeMode;
    return base;
  }

  function getActiveTemplateMacroMode() {
    const settings = normalizeTemplateMacroSettings(state.templateMacroSettings);
    return settings.active_mode || 'maintenance';
  }

  function computeMacroCarbs(target, protein, fat) {
    const kcal = toNumber(target);
    const p = toNumber(protein);
    const f = toNumber(fat);
    if (kcal == null || p == null || f == null) return null;
    const value = (kcal - (p * 4) - (f * 9)) / 4;
    if (!Number.isFinite(value)) return null;
    return Math.max(0, Math.round(value));
  }

  function buildTemplateMacroPreset(mode, seed = null) {
    const base = {
      cut: { target: 2400, protein: 180, fat: 65, redLow: 2200, greenLow: 2325, greenHigh: 2525, yellowHigh: 2700 },
      lean_bulk: { target: 3800, protein: 145, fat: 100, redLow: 3500, greenLow: 3650, greenHigh: 4450, yellowHigh: 4800 },
      maintenance: { target: 3000, protein: 165, fat: 80, redLow: 2800, greenLow: 2925, greenHigh: 3125, yellowHigh: 3300 },
      custom: { target: 2800, protein: 160, fat: 75, redLow: 2600, greenLow: 2725, greenHigh: 2925, yellowHigh: 3100 },
    }[mode] || { target: 2800, protein: 160, fat: 75, redLow: 2600, greenLow: 2725, greenHigh: 2925, yellowHigh: 3100 };
    const target = toNumber(seed?.kcal_target) ?? base.target;
    const protein = toNumber(seed?.protein_target) ?? base.protein;
    const fat = toNumber(seed?.fat_target) ?? base.fat;
    const redLow = toNumber(seed?.yellow_low) ?? base.redLow;
    const greenLow = toNumber(seed?.green_low) ?? base.greenLow;
    const greenHigh = toNumber(seed?.green_high) ?? base.greenHigh;
    const yellowHigh = toNumber(seed?.yellow_high) ?? base.yellowHigh;
    const carbs = toNumber(seed?.carbs_target) ?? computeMacroCarbs(target, protein, fat);
    return sanitizeTemplateMacroDraft({
      redLow,
      greenLow,
      target,
      greenHigh,
      yellowHigh,
      protein,
      fat,
      carbs,
    });
  }

  function modeHasConcreteMacroSettings(modeSettings) {
    return ['kcal_target', 'protein_target', 'fat_target', 'green_low', 'green_high', 'yellow_low', 'yellow_high']
      .some((key) => toNumber(modeSettings?.[key]) != null);
  }

  function draftFromModeSettings(mode, modeSettings) {
    if (!modeHasConcreteMacroSettings(modeSettings)) {
      return buildTemplateMacroPreset(mode, modeSettings);
    }
    return sanitizeTemplateMacroDraft({
      redLow: toNumber(modeSettings?.yellow_low),
      greenLow: toNumber(modeSettings?.green_low),
      target: toNumber(modeSettings?.kcal_target),
      greenHigh: toNumber(modeSettings?.green_high),
      yellowHigh: toNumber(modeSettings?.yellow_high),
      protein: toNumber(modeSettings?.protein_target),
      fat: toNumber(modeSettings?.fat_target),
      carbs: toNumber(modeSettings?.carbs_target),
    });
  }

  function sanitizeTemplateMacroDraft(raw) {
    const MIN_GAP = 50;
    const base = {
      redLow: 2600,
      greenLow: 2725,
      target: 2800,
      greenHigh: 2925,
      yellowHigh: 3100,
      protein: 160,
      fat: 75,
    };
    const draft = {
      redLow: toNumber(raw?.redLow) ?? base.redLow,
      greenLow: toNumber(raw?.greenLow) ?? base.greenLow,
      target: toNumber(raw?.target) ?? base.target,
      greenHigh: toNumber(raw?.greenHigh) ?? base.greenHigh,
      yellowHigh: toNumber(raw?.yellowHigh) ?? base.yellowHigh,
      protein: toNumber(raw?.protein) ?? base.protein,
      fat: toNumber(raw?.fat) ?? base.fat,
      carbs: toNumber(raw?.carbs),
    };
    draft.protein = clampValue(draft.protein, 0, 450);
    draft.fat = clampValue(draft.fat, 0, 250);
    draft.redLow = Math.round(draft.redLow);
    draft.greenLow = Math.max(draft.redLow + MIN_GAP, Math.round(draft.greenLow));
    draft.target = Math.max(draft.greenLow + MIN_GAP, Math.round(draft.target));
    draft.greenHigh = Math.max(draft.target + MIN_GAP, Math.round(draft.greenHigh));
    draft.yellowHigh = Math.max(draft.greenHigh + MIN_GAP, Math.round(draft.yellowHigh));
    draft.carbs = computeMacroCarbs(draft.target, draft.protein, draft.fat);
    return draft;
  }

  function readTemplateMacroModalValues() {
    return sanitizeTemplateMacroDraft({
      redLow: toNumber(els.macroModalYellowLow?.value),
      greenLow: toNumber(els.macroModalGreenLow?.value),
      target: toNumber(els.macroModalTarget?.value),
      greenHigh: toNumber(els.macroModalGreenHigh?.value),
      yellowHigh: toNumber(els.macroModalYellowHigh?.value),
      protein: toNumber(els.macroModalProtein?.value),
      fat: toNumber(els.macroModalFat?.value),
      carbs: toNumber(els.macroModalCarbs?.value),
    });
  }

  function writeTemplateMacroModalValues(draft) {
    const next = sanitizeTemplateMacroDraft(draft);
    state.templateMacroDraft = next;
    if (els.macroModalTarget) els.macroModalTarget.value = next.target ?? '';
    if (els.macroModalProtein) els.macroModalProtein.value = next.protein ?? '';
    if (els.macroModalFat) els.macroModalFat.value = next.fat ?? '';
    if (els.macroModalCarbs) els.macroModalCarbs.value = next.carbs ?? '';
    if (els.macroModalYellowLow) els.macroModalYellowLow.value = next.redLow ?? '';
    if (els.macroModalGreenLow) els.macroModalGreenLow.value = next.greenLow ?? '';
    if (els.macroModalGreenHigh) els.macroModalGreenHigh.value = next.greenHigh ?? '';
    if (els.macroModalYellowHigh) els.macroModalYellowHigh.value = next.yellowHigh ?? '';
    if (els.macroModalCarbsLive) els.macroModalCarbsLive.textContent = formatNum(next.carbs);
    if (els.macroModalCarbsSub) {
      els.macroModalCarbsSub.textContent = next.carbs == null
        ? 'Automatisch aus kcal, Protein und Fett berechnet'
        : `${formatNum(next.target)} kcal - P ${formatNum(next.protein)} - F ${formatNum(next.fat)}`;
    }
  }

  function validateTemplateMacroModalValues(values) {
    const {
      target, protein, fat, redLow, greenLow, greenHigh, yellowHigh,
    } = sanitizeTemplateMacroDraft(values);
    const carbs = computeMacroCarbs(target, protein, fat);
    if (target === null || target <= 0) return { ok: false, error: 'Bitte ein gültiges kcal Ziel setzen.' };
    if (protein === null || protein < 0 || fat === null || fat < 0) {
      return { ok: false, error: 'Bitte Protein und Fett gültig ausfüllen.' };
    }
    if (carbs === null || carbs < 0) {
      return { ok: false, error: 'Die Makroverteilung ergibt negative Carbs.' };
    }
    if (!(redLow < greenLow && greenLow < target && target < greenHigh && greenHigh < yellowHigh)) {
      return { ok: false, error: 'Die Marker-Reihenfolge ist ungültig.' };
    }
    if (!(greenLow < target && target < greenHigh)) {
      return { ok: false, error: 'Das Ziel muss im grünen Bereich liegen.' };
    }
    return { ok: true, error: '' };
  }

  function getTemplateMacroScaleBounds(draft = state.templateMacroDraft) {
    const values = sanitizeTemplateMacroDraft(draft);
    const outerPad = 250;
    const rawMin = Math.max(800, values.redLow - outerPad);
    const rawMax = values.yellowHigh + outerPad;
    const min = Math.floor(rawMin / 50) * 50;
    const max = Math.ceil(Math.max(rawMax, min + 800) / 50) * 50;
    return { min, max };
  }

  function getTemplateMacroScalePercent(value, bounds) {
    if (!bounds || bounds.max <= bounds.min) return 0;
    return clampValue(((value - bounds.min) / (bounds.max - bounds.min)) * 100, 0, 100);
  }

  function renderTemplateMacroBandPreview() {
    const host = els.macroModalBandPreview;
    if (!host || !els.macroModalScale) return;
    const values = readTemplateMacroModalValues();
    const validation = validateTemplateMacroModalValues(values);
    const bounds = state.templateMacroDrag?.bounds || getTemplateMacroScaleBounds(values);
    const percent = (key) => getTemplateMacroScalePercent(values[key], bounds);

    setInvalid(els.macroModalTarget, values.target === null || values.target <= 0);
    setInvalid(els.macroModalProtein, values.protein === null || values.protein < 0);
    setInvalid(els.macroModalFat, values.fat === null || values.fat < 0);
    host.classList.toggle('is-empty', !validation.ok);
    setText(els.macroModalScaleMin, `${formatNum(bounds.min)} kcal`);
    setText(els.macroModalScaleMax, `${formatNum(bounds.max)} kcal`);

    const segments = {
      'red-left': { left: 0, width: percent('redLow') },
      'yellow-left': { left: percent('redLow'), width: percent('greenLow') - percent('redLow') },
      green: { left: percent('greenLow'), width: percent('greenHigh') - percent('greenLow') },
      'yellow-right': { left: percent('greenHigh'), width: percent('yellowHigh') - percent('greenHigh') },
      'red-right': { left: percent('yellowHigh'), width: 100 - percent('yellowHigh') },
    };
    Object.entries(segments).forEach(([name, layout]) => {
      const el = els.macroModalScale.querySelector(`[data-seg="${name}"]`);
      if (!el) return;
      el.style.left = `${layout.left}%`;
      el.style.width = `${Math.max(0, layout.width)}%`;
    });

    ['redLow', 'greenLow', 'target', 'greenHigh', 'yellowHigh'].forEach((key) => {
      const marker = els.macroModalScale.querySelector(`[data-marker="${key}"]`);
      if (!marker) return;
      marker.style.left = `${percent(key)}%`;
    });

    if (els.macroModalZoneChips) {
      els.macroModalZoneChips.innerHTML = `
        <div class="planning-macro-zone-chip is-red">Rot < ${formatNum(values.redLow)}</div>
        <div class="planning-macro-zone-chip is-yellow">Gelb ${formatNum(values.redLow)}-${formatNum(values.greenLow)}</div>
        <div class="planning-macro-zone-chip is-green">Grün ${formatNum(values.greenLow)}-${formatNum(values.greenHigh)}</div>
        <div class="planning-macro-zone-chip is-yellow">Gelb ${formatNum(values.greenHigh)}-${formatNum(values.yellowHigh)}</div>
        <div class="planning-macro-zone-chip is-red">Rot > ${formatNum(values.yellowHigh)}</div>
        <div class="planning-macro-zone-chip is-target">Ziel ${formatNum(values.target)}</div>
      `;
    }

    if (state.templateMacroDrag?.key && els.macroModalScaleTooltip) {
      const activeKey = state.templateMacroDrag.key;
      const activeLeft = percent(activeKey);
      els.macroModalScaleTooltip.hidden = false;
      els.macroModalScaleTooltip.textContent = `${formatNum(values[activeKey])} kcal`;
      els.macroModalScaleTooltip.style.left = `${activeLeft}%`;
    } else if (els.macroModalScaleTooltip) {
      els.macroModalScaleTooltip.hidden = true;
    }

    if (!validation.ok) {
      setText(els.macroModalError, validation.error);
    } else {
      setText(els.macroModalError, '');
    }
  }

  function renderTemplateMacroSummary() {
    if (!els.templateTargetSummary) return;
    const settings = normalizeTemplateMacroSettings(state.templateMacroSettings);
    const mode = getActiveTemplateMacroMode();
    const modeSettings = settings.modes?.[mode] || {};
    const draft = draftFromModeSettings(mode, modeSettings);
    const hasTarget = Number.isFinite(Number(draft.target)) && Number(draft.target) > 0;
    if (els.templateTargetEditBtn) {
      els.templateTargetEditBtn.disabled = !state.selectedTemplateId;
    }
    if (!state.selectedTemplateId) {
      els.templateTargetSummary.innerHTML = '';
      return;
    }
    if (!hasTarget) {
      els.templateTargetSummary.innerHTML = `
        <div class="macro-empty">
          <div class="macro-empty-title">Kein Template-Ziel gesetzt</div>
          <div class="macro-card-subtitle">Dieses Ziel gilt nur für diese Nutrition-Version.</div>
        </div>
      `;
      return;
    }
    els.templateTargetSummary.innerHTML = `
      <div class="nutrition-template-target-top">
        <span class="nutrition-template-target-mode">${formatModeLabel(mode)}</span>
        <span class="nutrition-template-target-main">
          <strong class="nutrition-template-target-kcal">${formatNum(draft.target)} kcal</strong>
        </span>
      </div>
      <div class="nutrition-template-target-macros">
        P ${formatNum(draft.protein)} g | C ${formatNum(draft.carbs)} g | F ${formatNum(draft.fat)} g
      </div>
      <div class="nutrition-template-target-bands">
        <div class="nutrition-template-target-chip">
          <span class="nutrition-template-target-dot is-yellow"></span>
          <span>Gelb ${formatNum(draft.redLow)}-${formatNum(draft.greenLow)}</span>
        </div>
        <div class="nutrition-template-target-chip">
          <span class="nutrition-template-target-dot is-green"></span>
          <span>Grün ${formatNum(draft.greenLow)}-${formatNum(draft.greenHigh)}</span>
        </div>
        <div class="nutrition-template-target-chip">
          <span class="nutrition-template-target-dot is-yellow"></span>
          <span>Gelb ${formatNum(draft.greenHigh)}-${formatNum(draft.yellowHigh)}</span>
        </div>
      </div>
    `;
  }

  function seedTemplateMacroMode(mode) {
    const settings = normalizeTemplateMacroSettings(state.templateMacroSettings);
    const existing = settings.modes?.[mode] || {};
    const draft = draftFromModeSettings(mode, existing);
    settings.modes[mode] = {
      kcal_target: draft.target,
      protein_target: draft.protein,
      carbs_target: draft.carbs,
      fat_target: draft.fat,
      green_low: draft.greenLow,
      green_high: draft.greenHigh,
      yellow_low: draft.redLow,
      yellow_high: draft.yellowHigh,
    };
    state.templateMacroSettings = settings;
    return draft;
  }

  function stashTemplateMacroDraftForMode(mode = state.templateMacroModalMode) {
    if (!mode || !TEMPLATE_MACRO_MODE_KEYS.includes(mode)) return;
    if (state.templateMacroDraft == null && toNumber(els.macroModalTarget?.value) == null) return;
    const draft = readTemplateMacroModalValues();
    const settings = normalizeTemplateMacroSettings(state.templateMacroSettings);
    settings.modes[mode] = {
      kcal_target: draft.target,
      protein_target: draft.protein,
      carbs_target: draft.carbs,
      fat_target: draft.fat,
      green_low: draft.greenLow,
      green_high: draft.greenHigh,
      yellow_low: draft.redLow,
      yellow_high: draft.yellowHigh,
    };
    state.templateMacroSettings = settings;
  }

  function setTemplateMacroModalMode(mode) {
    if (!TEMPLATE_MACRO_MODE_KEYS.includes(mode)) return;
    stashTemplateMacroDraftForMode();
    state.templateMacroModalMode = mode;
    els.macroModalTabs.forEach((btn) => {
      btn.classList.toggle('is-active', btn.dataset.mode === mode);
    });
    const draft = seedTemplateMacroMode(mode);
    writeTemplateMacroModalValues(draft);
    setText(els.macroModalError, '');
    renderTemplateMacroBandPreview();
  }

  function openTemplateMacroModal() {
    if (!els.macroModal || !state.selectedTemplateId) return;
    state.templateMacroSettings = normalizeTemplateMacroSettings(state.plan?.week_template?.macro_settings);
    state.templateMacroDraft = null;
    state.templateMacroDrag = null;
    els.macroModal.classList.add('is-open');
    els.macroModal.setAttribute('aria-hidden', 'false');
    setTemplateMacroModalMode(getActiveTemplateMacroMode());
  }

  function closeTemplateMacroModal() {
    if (!els.macroModal) return;
    const activeMarker = els.macroModalScale?.querySelector('.planning-macro-marker.is-dragging');
    activeMarker?.classList.remove('is-dragging');
    state.templateMacroDraft = null;
    state.templateMacroDrag = null;
    els.macroModal.classList.remove('is-open');
    els.macroModal.setAttribute('aria-hidden', 'true');
    setText(els.macroModalError, '');
    if (els.macroModalScaleTooltip) els.macroModalScaleTooltip.hidden = true;
  }

  function applyTemplateMacroTargetInput(value) {
    const draft = readTemplateMacroModalValues();
    const nextTarget = toNumber(value);
    if (nextTarget == null) {
      renderTemplateMacroBandPreview();
      return;
    }
    draft.target = Math.round(nextTarget);
    const gap = 50;
    if (draft.target <= draft.greenLow) draft.greenLow = draft.target - gap;
    if (draft.target >= draft.greenHigh) draft.greenHigh = draft.target + gap;
    if (draft.greenLow <= draft.redLow) draft.redLow = draft.greenLow - gap;
    if (draft.greenHigh >= draft.yellowHigh) draft.yellowHigh = draft.greenHigh + gap;
    writeTemplateMacroModalValues(draft);
    renderTemplateMacroBandPreview();
  }

  function applyTemplateMacroMacroInput(key, value) {
    const draft = readTemplateMacroModalValues();
    draft[key] = toNumber(value);
    writeTemplateMacroModalValues(draft);
    renderTemplateMacroBandPreview();
  }

  function startTemplateMacroDrag(markerKey, event) {
    if (!els.macroModalScale) return;
    event.preventDefault();
    const marker = els.macroModalScale.querySelector(`[data-marker="${markerKey}"]`);
    marker?.classList.add('is-dragging');
    state.templateMacroDrag = {
      key: markerKey,
      bounds: getTemplateMacroScaleBounds(readTemplateMacroModalValues()),
    };
    renderTemplateMacroBandPreview();
  }

  function moveTemplateMacroDrag(event) {
    if (!state.templateMacroDrag || !els.macroModalScale) return;
    const rect = els.macroModalScale.getBoundingClientRect();
    if (!rect.width) return;
    const gap = 50;
    const bounds = state.templateMacroDrag.bounds;
    const ratio = clampValue((event.clientX - rect.left) / rect.width, 0, 1);
    const rawValue = Math.round((bounds.min + ((bounds.max - bounds.min) * ratio)) / 25) * 25;
    const draft = readTemplateMacroModalValues();
    const key = state.templateMacroDrag.key;
    const floor = {
      redLow: bounds.min,
      greenLow: draft.redLow + gap,
      target: draft.greenLow + gap,
      greenHigh: draft.target + gap,
      yellowHigh: draft.greenHigh + gap,
    }[key];
    const ceil = {
      redLow: draft.greenLow - gap,
      greenLow: draft.target - gap,
      target: draft.greenHigh - gap,
      greenHigh: draft.yellowHigh - gap,
      yellowHigh: bounds.max,
    }[key];
    draft[key] = clampValue(rawValue, floor, ceil);
    writeTemplateMacroModalValues(draft);
    setText(els.macroModalError, '');
    renderTemplateMacroBandPreview();
  }

  function stopTemplateMacroDrag() {
    if (!state.templateMacroDrag) return;
    els.macroModalScale?.querySelectorAll('.planning-macro-marker.is-dragging').forEach((node) => {
      node.classList.remove('is-dragging');
    });
    state.templateMacroDrag = null;
    renderTemplateMacroBandPreview();
  }

  async function saveTemplateMacroSettings() {
    if (!state.selectedTemplateId || state.isSavingTemplateMacroSettings) return;
    stashTemplateMacroDraftForMode();
    const values = readTemplateMacroModalValues();
    const validation = validateTemplateMacroModalValues(values);
    if (!validation.ok) {
      setText(els.macroModalError, validation.error);
      renderTemplateMacroBandPreview();
      return;
    }
    const modePayload = {
      kcal_target: values.target,
      protein_target: values.protein,
      carbs_target: values.carbs,
      fat_target: values.fat,
      green_low: values.greenLow,
      green_high: values.greenHigh,
      yellow_low: values.redLow,
      yellow_high: values.yellowHigh,
    };
    const payload = {
      active_mode: state.templateMacroModalMode,
      modes: {
        [state.templateMacroModalMode]: modePayload,
      },
    };

    state.isSavingTemplateMacroSettings = true;
    if (els.macroModalSave) {
      els.macroModalSave.disabled = true;
      els.macroModalSave.classList.add('is-loading');
    }
    try {
      const res = await fetch(`/api/nutrition/plan/templates/${encodeURIComponent(state.selectedTemplateId)}/macro_settings`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      const data = await res.json();
      if (!res.ok || !data || data.ok === false || !data.settings) {
        setText(els.macroModalError, data?.error || data?.detail || 'Speichern fehlgeschlagen.');
        return;
      }
      state.templateMacroSettings = normalizeTemplateMacroSettings(data.settings);
      closeTemplateMacroModal();
      await loadPlan(state.selectedTemplateId);
      showSnackbar('Template-Ziel gespeichert');
    } catch (err) {
      console.error(err);
      setText(els.macroModalError, 'Speichern fehlgeschlagen.');
    } finally {
      state.isSavingTemplateMacroSettings = false;
      if (els.macroModalSave) {
        els.macroModalSave.disabled = false;
        els.macroModalSave.classList.remove('is-loading');
      }
    }
  }

  function formatIngredientLine(item) {
    if (!item) return '';
    const unit = String(item.unit || '').toLowerCase();
    if (item.item_type === 'kcal_only' || unit === 'kcal') {
      const kcal = Math.round(Number(item.calories ?? item.amount) || 0);
      const name = item.name || item.food_name || '';
      return `${kcal} kcal${name ? ` ${name}` : ''}`.trim();
    }
    const amount = formatAmount(item.amount);
    const name = item.name || item.food_name || '';
    const unitText = item.unit ? ` ${item.unit}` : '';
    return `${amount}${unitText} ${name}`.trim();
  }

  function renderSlotIngredientPreview(slot, card) {
    const preview = document.createElement('div');
    preview.className = 'nutrition-slot-ingredients-preview';

    const heading = document.createElement('div');
    heading.className = 'nutrition-slot-ingredients-head';
    const label = document.createElement('span');
    label.className = 'nutrition-slot-ingredients-label';
    label.textContent = 'Foods';
    const edit = document.createElement('button');
    edit.type = 'button';
    edit.className = 'nutrition-slot-edit-btn';
    edit.textContent = 'Bearbeiten';
    edit.addEventListener('click', (event) => {
      event.stopPropagation();
      const isOpen = card.dataset.expanded === '1';
      if (!isOpen) {
        card.dataset.expanded = '1';
        card.classList.add('is-expanded');
        toggleMealEditor(card, slot);
      }
    });
    heading.append(label, edit);
    preview.appendChild(heading);

    const items = (slot.override?.ingredients?.length ? slot.override.ingredients : slot.items) || [];
    if (!items.length) {
      const empty = document.createElement('div');
      empty.className = 'nutrition-slot-ingredients-empty';
      empty.textContent = 'Keine Foods hinterlegt';
      preview.appendChild(empty);
      return preview;
    }

    const list = document.createElement('div');
    list.className = 'nutrition-slot-ingredients-list';
    items.forEach((item) => {
      const row = document.createElement('div');
      row.className = 'nutrition-slot-ingredient-row';
      const name = document.createElement('span');
      name.className = 'nutrition-slot-ingredient-name';
      name.textContent = item.food_name || item.name || 'Food';
      const amount = document.createElement('span');
      amount.className = 'nutrition-slot-ingredient-amount';
      const unit = String(item.unit || '').toLowerCase();
      amount.textContent = unit === 'kcal'
        ? `${formatAmount(item.calories ?? item.amount)} kcal`
        : `${formatAmount(item.amount)} ${item.unit || ''}`.trim();
      row.append(name, amount);
      list.appendChild(row);
    });
    preview.appendChild(list);
    return preview;
  }

  function getFoodMacroRefs() {
    if (!els.foodForm) return null;
    return {
      unit: els.foodForm.querySelector('[name="unit_default"]'),
      kcal: els.foodForm.querySelector('[name="kcal_per_100"]'),
      p: els.foodForm.querySelector('[name="p_per_100"]'),
      c: els.foodForm.querySelector('[name="c_per_100"]'),
      sugar: els.foodForm.querySelector('[name="sugar_per_100"]'),
      f: els.foodForm.querySelector('[name="f_per_100"]'),
    };
  }

  function setFoodLabelMode(mode) {
    const perPcs = mode === 'perpcs';
    if (els.foodLabelKcal) els.foodLabelKcal.textContent = perPcs ? 'kcal / pcs' : 'kcal / 100g';
    if (els.foodLabelProtein) els.foodLabelProtein.textContent = perPcs ? 'Protein / pcs' : 'Protein / 100g';
    if (els.foodLabelCarbs) els.foodLabelCarbs.textContent = perPcs ? 'Carbs / pcs' : 'Carbs / 100g';
    if (els.foodLabelSugar) els.foodLabelSugar.textContent = perPcs ? 'Sugar / pcs' : 'Sugar / 100g';
    if (els.foodLabelFat) els.foodLabelFat.textContent = perPcs ? 'Fett / pcs' : 'Fett / 100g';
    if (els.foodForm) els.foodForm.dataset.macroMode = mode;
  }

  function applyFoodUnitMode(unitValue) {
    const target = unitValue === 'pcs' ? 'perpcs' : 'per100';
    setFoodLabelMode(target);
  }

  function progressPercent(planned, target) {
    if (!Number.isFinite(Number(target)) || Number(target) <= 0) return 0;
    return clamp01(Number(planned) / Number(target)) * 100;
  }

  function deriveMacroOverview() {
    if (!state.plan?.week_summary || !state.plan?.week_template) return null;
    const summary = state.plan.week_summary;
    const days = (state.plan.week_template.days || []).slice().sort((a, b) => a.weekday - b.weekday);
    const isOffDay = (day) => {
      const raw = String(day?.day_type || '').toLowerCase();
      return raw.includes('off') || raw.includes('rest') || raw.includes('frei');
    };
    const plannedDays = days.filter((day) => !isOffDay(day));
    const activeDayIdx = Number(state.selectedDay);
    const activeDay = days.find((d) => d.weekday === activeDayIdx) || plannedDays[0] || days[0];
    const dayTarget = (day) => ({
      kcal: Number(day?.target?.kcal) || Number(summary.targets?.kcal) || 0,
      p: Number(day?.target?.p) || Number(summary.targets?.p) || 0,
      c: Number(day?.target?.c) || Number(summary.targets?.c) || 0,
      f: Number(day?.target?.f) || Number(summary.targets?.f) || 0,
    });
    const dayPlanned = (day) => ({
      kcal: Number(day?.planned?.kcal) || 0,
      p: Number(day?.planned?.p) || 0,
      c: Number(day?.planned?.c) || 0,
      f: Number(day?.planned?.f) || 0,
    });

    let weekSum = 0;
    let weekSumP = 0;
    let weekCount = 0;
    let weekMin = null;
    let weekMax = null;
    plannedDays.forEach((day) => {
      const planned = dayPlanned(day);
      weekSum += planned.kcal;
      weekSumP += planned.p;
      weekCount += 1;
      if (weekMin == null || planned.kcal < weekMin) weekMin = planned.kcal;
      if (weekMax == null || planned.kcal > weekMax) weekMax = planned.kcal;
    });
    if (weekCount === 0 && days.length) {
      days.forEach((day) => {
        const planned = dayPlanned(day);
        weekSum += planned.kcal;
        weekSumP += planned.p;
        weekCount += 1;
        if (weekMin == null || planned.kcal < weekMin) weekMin = planned.kcal;
        if (weekMax == null || planned.kcal > weekMax) weekMax = planned.kcal;
      });
    }
    const weekAvg = weekCount > 0 ? weekSum / weekCount : 0;
    const weekAvgP = weekCount > 0 ? weekSumP / weekCount : 0;

    const target = dayTarget(activeDay);
    const activeBase = dayPlanned(activeDay);
    const proteinTol = 8;
    const proteinGaps = [];
    days.forEach((day) => {
      if (isOffDay(day)) return;
      const targetP = dayTarget(day).p;
      if (targetP <= 0) return;
      const planned = dayPlanned(day).p;
      const gap = targetP - (Number(planned) || 0);
      if (gap > proteinTol) {
        let severity = 'mild';
        if (gap > 80) severity = 'critical';
        else if (gap > 40) severity = 'high';
        proteinGaps.push({ label: day.label, weekday: day.weekday, gap, severity });
      }
    });
    proteinGaps.sort((a, b) => b.gap - a.gap);

    return {
      activeDay,
      activeDayIdx,
      target,
      activeBase,
      weekAvg,
      weekAvgP,
      weekMin,
      weekMax,
      proteinGaps,
    };
  }

  let macroRenderScheduled = false;
  function scheduleMacroRender() {
    if (macroRenderScheduled) return;
    macroRenderScheduled = true;
    requestAnimationFrame(() => {
      macroRenderScheduled = false;
      const derived = deriveMacroOverview();
      if (!derived) return;
      renderSummary(derived);
      renderGapNotes(derived);
    });
  }

  function recomputePlanStats() {
    if (!state.plan?.week_template?.days || !state.plan?.week_summary?.targets) return;
    const targets = state.plan.week_summary.targets || {};
    const week = { kcal: 0, p: 0, c: 0, f: 0 };
    state.plan.week_template.days.forEach((day) => {
      const dayTotals = { kcal: 0, p: 0, c: 0, f: 0 };
      (day.slots || []).forEach((slot) => {
        let source = null;
        if (slot.items?.length) {
          source = computeIngredientsMacros(slot.items);
        } else if (slot.override?.ingredients?.length) {
          if (slot.override.kcal != null) {
            source = {
              kcal: Number(slot.override.kcal) || 0,
              p: Number(slot.override.p) || 0,
              c: Number(slot.override.c) || 0,
              f: Number(slot.override.f) || 0,
            };
          } else {
            source = computeIngredientsMacros(slot.override.ingredients);
          }
        } else if (slot.meal_template) {
          source = {
            kcal: Number(slot.meal_template.kcal_per_serving) || 0,
            p: Number(slot.meal_template.p_per_serving) || 0,
            c: Number(slot.meal_template.c_per_serving) || 0,
            f: Number(slot.meal_template.f_per_serving) || 0,
          };
        } else {
          source = { kcal: 0, p: 0, c: 0, f: 0 };
        }
        dayTotals.kcal += source.kcal;
        dayTotals.p += source.p;
        dayTotals.c += source.c;
        dayTotals.f += source.f;
      });
      day.planned = {
        kcal: Number(dayTotals.kcal.toFixed(1)),
        p: Number(dayTotals.p.toFixed(1)),
        c: Number(dayTotals.c.toFixed(1)),
        f: Number(dayTotals.f.toFixed(1)),
      };
      day.remaining = {
        kcal: targets.kcal == null ? null : Number((Number(targets.kcal) - dayTotals.kcal).toFixed(1)),
        p: targets.p == null ? null : Number((Number(targets.p) - dayTotals.p).toFixed(1)),
        c: targets.c == null ? null : Number((Number(targets.c) - dayTotals.c).toFixed(1)),
        f: targets.f == null ? null : Number((Number(targets.f) - dayTotals.f).toFixed(1)),
      };
      week.kcal += dayTotals.kcal;
      week.p += dayTotals.p;
      week.c += dayTotals.c;
      week.f += dayTotals.f;
    });
    const days = Math.max(1, state.plan.week_template.days.length);
    const avg = { kcal: week.kcal / days, p: week.p / days, c: week.c / days, f: week.f / days };
    state.plan.week_summary.planned = {
      kcal: Number(avg.kcal.toFixed(1)),
      p: Number(avg.p.toFixed(1)),
      c: Number(avg.c.toFixed(1)),
      f: Number(avg.f.toFixed(1)),
    };
    state.plan.week_summary.remaining = {
      kcal: targets.kcal == null ? null : Number((Number(targets.kcal) - avg.kcal).toFixed(1)),
      p: targets.p == null ? null : Number((Number(targets.p) - avg.p).toFixed(1)),
      c: targets.c == null ? null : Number((Number(targets.c) - avg.c).toFixed(1)),
      f: targets.f == null ? null : Number((Number(targets.f) - avg.f).toFixed(1)),
    };
  }

  function getUsageCounts() {
    const mealCounts = {};
    const foodCounts = {};
    const days = state.plan?.week_template?.days || [];
    days.forEach((day) => {
      (day.slots || []).forEach((slot) => {
        const mealId = Number(slot.meal_template_id || 0);
        if (mealId) {
          mealCounts[mealId] = (mealCounts[mealId] || 0) + 1;
        }
        let ingredients = [];
        if ((slot.items || []).length) {
          ingredients = slot.items;
        } else if (slot.override?.ingredients?.length) {
          ingredients = slot.override.ingredients;
        } else if (mealId && state.mealIngredientsMap[mealId]) {
          ingredients = state.mealIngredientsMap[mealId];
        }
        ingredients.forEach((item) => {
          const foodId = Number(item.food_id || 0);
          if (!foodId) return;
          foodCounts[foodId] = (foodCounts[foodId] || 0) + 1;
        });
      });
    });
    return { mealCounts, foodCounts };
  }

  function copyText(text, btn) {
    if (!text) {
      showSnackbar('Noch kein Inhalt vorhanden.');
      return;
    }
    navigator.clipboard?.writeText(text).then(() => {
      const original = btn.textContent;
      btn.textContent = 'Copied!';
      setTimeout(() => (btn.textContent = original), 1200);
    });
  }

  function copyFoodCollectionList() {
    const foods = state.foods || [];
    if (!foods.length) {
      showSnackbar('Keine Foods zum Kopieren verfügbar.');
      return;
    }
    const lines = foods.map((food) => {
      const kcal = Math.round(food.kcal_per_100 ?? 0);
      const p = Math.round(food.p_per_100 ?? 0);
      const c = Math.round(food.c_per_100 ?? 0);
      const f = Math.round(food.f_per_100 ?? 0);
      const s = Math.round(food.sugar_per_100 ?? 0);
      return `${food.name} · kcal ${kcal} | P ${p} | C ${c} | S ${s} | F ${f}`;
    });
    const payload = lines.join('\n');
    if (!payload) {
      showSnackbar('Food Sammlung ist leer.');
      return;
    }
    navigator.clipboard?.writeText(payload).then(
      () => showSnackbar('Food Sammlung kopiert'),
      () => showSnackbar('Kopieren fehlgeschlagen'),
    );
  }

  function copyMealCollectionList() {
    const meals = state.mealTemplates || [];
    if (!meals.length) {
      showSnackbar('Keine Meal Templates zum Kopieren.');
      return;
    }
    const lines = [];
    meals.forEach((meal) => {
      const defaultServings = Number(meal.default_servings) || 1;
      const ingredients = state.mealIngredientsMap?.[meal.id] || [];
      const ingredientTotals = computeIngredientsMacros(ingredients);
      const sugarFromTemplate = Number(meal.sugar_per_serving);
      const sugarTotal = Number.isFinite(sugarFromTemplate) && sugarFromTemplate > 0
        ? sugarFromTemplate * defaultServings
        : (Number(ingredientTotals.sugar) || 0);
      const kcal = (Number(meal.kcal_per_serving) || 0) * defaultServings;
      const p = (Number(meal.p_per_serving) || 0) * defaultServings;
      const c = (Number(meal.c_per_serving) || 0) * defaultServings;
      const f = (Number(meal.f_per_serving) || 0) * defaultServings;
      const macroText = `kcal ${kcal.toFixed(0)} | P ${p.toFixed(0)} | C ${c.toFixed(0)} | S ${sugarTotal.toFixed(0)} | F ${f.toFixed(0)}`;
      const title = meal.title || 'Unnamed Meal';
      lines.push(`${title} · ${macroText}`);
      ingredients.forEach((ing) => {
        const amount = Number(ing.amount) ? Number(ing.amount).toFixed(1) : '0';
        lines.push(`  - ${amount} ${ing.unit || 'g'} ${ing.food_name || 'Unknown'}`);
      });
      lines.push('');
    });
    const payload = lines.join('\n').trim();
    navigator.clipboard?.writeText(payload).then(
      () => showSnackbar('Meal Templates kopiert'),
      () => showSnackbar('Kopieren fehlgeschlagen'),
    );
  }

  async function deleteAllMealTemplates() {
    if (!state.mealTemplates?.length) {
      showSnackbar('Keine Meal Templates vorhanden.');
      return;
    }
    const confirmed = window.confirm('Alle Meal Templates löschen? Dieser Schritt kann nicht rückgängig gemacht werden.');
    if (!confirmed) return;
    const res = await fetch('/api/nutrition/meal_templates', { method: 'DELETE' });
    const data = await res.json();
    if (!res.ok || data.ok === false) {
      throw new Error(data.error || 'Meal Templates konnten nicht gelöscht werden.');
    }
    await loadPlan(state.selectedTemplateId);
    showSnackbar('Alle Meal Templates gelöscht');
  }

  function toErrorMessage(err, fallback) {
    const raw = err && err.message ? String(err.message) : '';
    if (raw === 'food_in_use') return 'Food wird noch in Meals verwendet und kann nicht gelöscht werden.';
    if (raw === 'duplicate_signature') return 'Mahlzeit existiert bereits.';
    if (raw === 'duplicate_name') return 'Food existiert bereits.';
    if (raw === 'title_required') return 'Bitte einen Titel eingeben.';
    if (raw === 'name_required') return 'Bitte einen Namen eingeben.';
    if (raw === 'macros_required') return 'Bitte alle Makros ausfüllen.';
    if (raw === 'not_found') return 'Eintrag nicht gefunden.';
    return raw || fallback || 'Aktion fehlgeschlagen.';
  }

  function showError(err, fallback) {
    alert(toErrorMessage(err, fallback));
  }

  async function fetchPlan(templateId = null) {
    const qs = templateId ? `?template=${encodeURIComponent(templateId)}` : '';
    const res = await fetch(`/api/nutrition/plan/week${qs}`, { cache: 'no-store' });
    const data = await res.json();
    if (!res.ok || data.ok === false) {
      throw new Error(data.error || 'Plan konnte nicht geladen werden.');
    }
    return data;
  }

  async function loadPlan(templateId = null) {
    const plan = await fetchPlan(templateId);
    state.plan = plan;
    state.templateMacroSettings = normalizeTemplateMacroSettings(plan.week_template?.macro_settings);
    state.mealTemplates = plan.meal_templates || [];
    state.foods = plan.foods || [];
    let needsRecalc = false;
    if (!state.foods || state.foods.length === 0) {
      try {
        const res = await fetch('/api/nutrition/foods?limit=500', { cache: 'no-store' });
        const data = await res.json();
        if (res.ok && data.ok !== false) {
          state.foods = data.foods || [];
          needsRecalc = true;
        }
      } catch {}
    }
    state.slotTemplates = plan.slot_templates || [];
    state.mealIngredientsMap = plan.meal_ingredients_map || {};
    recomputePlanStats();
    state.lastSavedAt = getPlanTimestamp();
    state.lastSavedAtOverride = null;
    renderVersionStatus();
    if (plan.week_template?.id) {
      state.selectedTemplateId = plan.week_template.id;
      renderTemplateMenu();
    }
    const day = plan.week_template?.days?.find((d) => d.weekday === state.selectedDay);
    if (day && state.selectedSlotId) {
      const stillExists = (day.slots || []).some((slot) => slot.id === state.selectedSlotId);
      if (!stillExists) state.selectedSlotId = null;
    }
    if (state.selectedMealTemplateId) {
      const mealExists = state.mealTemplates.some((meal) => meal.id === state.selectedMealTemplateId);
      if (!mealExists) state.selectedMealTemplateId = null;
    }
    if (state.foodEditId) {
      const foodExists = state.foods.some((food) => food.id === state.foodEditId);
      if (!foodExists) state.foodEditId = null;
    }
    if (els.mealDeleteBtn) {
      els.mealDeleteBtn.disabled = !state.selectedMealTemplateId;
    }
    if (els.foodDeleteBtn) {
      els.foodDeleteBtn.disabled = !state.foodEditId;
    }
    renderSlots();
    renderMeals();
    renderFoods();
    renderDayGroups();
    renderDayGroupFocus();
    renderDayTabs();
    renderTemplateMacroSummary();
    renderSelectedDay();
    scheduleMacroRender();
    if (needsRecalc) {
      recomputePlanStats();
      renderMeals();
      renderFoods();
      renderDayGroups();
      renderDayGroupFocus();
      renderDayTabs();
      renderTemplateMacroSummary();
      renderSelectedDay();
      scheduleMacroRender();
    }
    setDirty(false);
    if (els.ingredientTray && els.ingredientTray.childElementCount === 0) {
      resetIngredientRows();
    }
    if (state.openEditorSlotId) {
      const target = document.querySelector(`[data-slot-id="${state.openEditorSlotId}"]`);
      const slot = getSlotById(state.openEditorSlotId);
      if (!target || !slot) {
        state.openEditorSlotId = null;
      }
    }
  }

  function renderSlots() {
    if (!els.slotList) return;
    const defaults = ['Meal 1', 'Meal 2', 'Meal 3', 'Meal 4'];
    els.slotList.innerHTML = defaults
      .map((name) => `<div class="nutrition-list-card"><div class="title">${name}</div><div class="nutrition-slot-meta">Uhrzeit anpassbar</div></div>`)
      .join('');
  }

  function renderMeals() {
    if (!els.mealList) return;
    const filter = (els.mealSearch?.value || '').trim().toLowerCase();
    els.mealList.innerHTML = '';
    const { mealCounts } = getUsageCounts();
    state.mealTemplates
      .filter((meal) => !filter || meal.title.toLowerCase().includes(filter))
      .sort((a, b) => {
        const ca = mealCounts[a.id] || 0;
        const cb = mealCounts[b.id] || 0;
        if (cb !== ca) return cb - ca;
        return a.title.localeCompare(b.title, 'de');
      })
      .forEach((meal) => {
        const card = document.createElement('div');
        card.className = 'nutrition-list-card compact-entry';
        if (state.selectedMealTemplateId === meal.id) {
          card.classList.add('is-selected');
        }
        card.innerHTML = `
          <div class="title">${meal.title}</div>
          <div class="nutrition-slot-meta">${formatMacros(meal, meal.default_servings)}</div>
        `;
        card.addEventListener('click', (event) => {
          selectMealTemplate(meal.id);
        });
        els.mealList.appendChild(card);
      });
  }

  function renderFoods() {
    if (!els.foodList) return;
    const filter = (els.foodSearch?.value || '').trim().toLowerCase();
    els.foodList.innerHTML = '';
    const { foodCounts } = getUsageCounts();
    state.foods
      .filter((food) => !filter || food.name.toLowerCase().includes(filter))
      .sort((a, b) => {
        const ca = foodCounts[a.id] || 0;
        const cb = foodCounts[b.id] || 0;
        if (cb !== ca) return cb - ca;
        return a.name.localeCompare(b.name, 'de');
      })
      .forEach((food) => {
        const card = document.createElement('div');
        card.className = 'nutrition-list-card compact-entry';
        if (state.foodEditId === food.id) {
          card.classList.add('is-selected');
        }
        card.innerHTML = `
          <div class="title">${food.name}</div>
          <div class="nutrition-slot-meta">kcal ${food.kcal_per_100} | P ${food.p_per_100} | C ${food.c_per_100} | S ${food.sugar_per_100 || 0} | F ${food.f_per_100}</div>
        `;
        card.addEventListener('click', () => startFoodEdit(food));
        els.foodList.appendChild(card);
      });
  }

  function renderDayTabs() {
    if (!els.planDayTabs || !state.plan?.week_template) return;
    els.planDayTabs.innerHTML = '';
    getDayGroups().forEach((group) => groupedRuns(group.weekdays || []).forEach(([start, end]) => {
      const bar = document.createElement('span');
      bar.className = 'nutrition-day-link-bar'; bar.style.gridColumn = `${start + 1} / ${end + 2}`; bar.style.setProperty('--day-group-color', group.color);
      bar.title = `${group.name}: ${group.weekdays.map(dayLabel).join(', ')}`; els.planDayTabs.appendChild(bar);
    }));
    state.plan.week_template.days.forEach((day) => {
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.style.gridColumn = String(day.weekday + 1);
      const group = getDayGroup(day.weekday);
      btn.className = `pill ${day.weekday === state.selectedProjectionDay ? 'primary' : 'ghost'}${group ? ' is-linked-day' : ''}${state.selectedGroupDays.has(day.weekday) ? ' is-link-selecting' : ''}`;
      if (group) { btn.style.setProperty('--day-group-color', group.color); btn.title = `${group.name} · Doppelklick: Verbindung auflösen`; }
      const planned = day.planned?.kcal;
      const kcalText = planned == null ? '-- kcal' : `${Math.round(planned)} kcal`;
      btn.innerHTML = `<span class="day-label">${day.label}</span><span class="day-kcal">${kcalText}</span>`;
      btn.addEventListener('pointerdown', () => {
        state.isSelectingDayGroup = true; state.dayGroupDragDays = new Set([day.weekday]); state.dayGroupDragged = false;
      });
      btn.addEventListener('pointerenter', () => {
        if (!state.isSelectingDayGroup) return;
        state.dayGroupDragged = true; state.dayGroupDragDays.add(day.weekday); btn.classList.add('is-link-selecting');
      });
      btn.addEventListener('click', (event) => {
        if (state.ignoreDayTabClick) { state.ignoreDayTabClick = false; return; }
        if (event.shiftKey) {
          state.selectedGroupDays.has(day.weekday) ? state.selectedGroupDays.delete(day.weekday) : state.selectedGroupDays.add(day.weekday);
          scheduleDayGroupLink(); renderDayTabs(); return;
        }
        const group = getDayGroup(day.weekday);
        state.focusedDayGroupId = group?.id || null;
        state.selectedDay = group ? group.source_weekday : day.weekday;
        state.selectedProjectionDay = day.weekday;
        state.selectedSlotId = null;
        renderDayTabs();
        renderSelectedDay();
        scheduleMacroRender();
      });
      btn.addEventListener('dblclick', () => {
        const linkedGroup = getDayGroup(day.weekday);
        if (linkedGroup) detachDayGroup(linkedGroup).catch((err) => showError(err));
      });
      els.planDayTabs.appendChild(btn);
    });
  }

  function getDayGroups() { return state.plan?.day_groups || []; }
  function getDayGroup(weekday) { return getDayGroups().find((group) => (group.weekdays || []).includes(weekday)); }
  function dayLabel(weekday) { return state.plan?.week_template?.days?.find((day) => day.weekday === weekday)?.label || ['Mo', 'Di', 'Mi', 'Do', 'Fr', 'Sa', 'So'][weekday]; }

  function groupedRuns(days) { const sorted = [...days].sort((a, b) => a - b); const runs = []; sorted.forEach((day) => { const last = runs.at(-1); if (last && day === last[1] + 1) last[1] = day; else runs.push([day, day]); }); return runs; }
  function renderDayGroups() { /* The weekday strip is the only group control. */ }

  async function linkSelectedDayGroup() {
    const weekdays = [...state.selectedGroupDays].sort((a, b) => a - b);
    if (weekdays.length < 2) return;
    const name = groupedRuns(weekdays).map(([start, end]) => start === end ? dayLabel(start) : `${dayLabel(start)}–${dayLabel(end)}`).join(' · ');
    openTemplateDialog({
      kind: 'link', name, weekdays, source_weekday: weekdays[0],
      title: `${name} verbinden?`,
      copy: `${dayLabel(weekdays[0])} wird das gemeinsame Tages-Template. Alle ausgewählten Tage erhalten anschließend identische Meals, Slots, Uhrzeiten, Zutaten und Mengen.`,
      confirm: 'Verbinden',
    });
  }

  async function persistDayGroupLink({ name, weekdays, source_weekday }) {
    const res = await fetch('/api/nutrition/plan/day-groups', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ template_id: state.selectedTemplateId, weekdays, source_weekday, name }) });
    const payload = await res.json();
    if (!res.ok || !payload.ok) throw new Error(payload.error || 'Tage konnten nicht verbunden werden.');
    state.focusedDayGroupId = payload.day_group.id;
    state.selectedDay = payload.day_group.source_weekday;
    state.selectedProjectionDay = payload.day_group.source_weekday;
    state.selectedGroupDays.clear();
    await loadPlan(state.selectedTemplateId);
    showSnackbar('Tage werden jetzt gemeinsam verwaltet');
  }

  function scheduleDayGroupLink() {
    clearTimeout(state.dayGroupLinkTimer);
    if (state.selectedGroupDays.size < 2) return;
    state.dayGroupLinkTimer = window.setTimeout(() => linkSelectedDayGroup().catch((err) => showError(err)), 900);
  }

  async function detachDayGroup(group) {
    openTemplateDialog({
      kind: 'detach', group,
      title: `${group.name} auflösen?`,
      copy: `Die Verbindung zwischen ${group.weekdays.map(dayLabel).join(', ')} wird entfernt. Der aktuelle Stand bleibt auf jedem Tag als eigene Kopie erhalten.`,
      confirm: 'Verbindung auflösen',
    });
  }

  async function persistDayGroupDetach(group) {
    const res = await fetch(`/api/nutrition/plan/day-groups/${encodeURIComponent(group.id)}/detach`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ template_id: state.selectedTemplateId, weekdays: group.weekdays }) });
    const payload = await res.json();
    if (!res.ok || !payload.ok) throw new Error(payload.error || 'Gruppe konnte nicht aufgelöst werden.');
    state.focusedDayGroupId = null; state.selectedGroupDays.clear(); await loadPlan(state.selectedTemplateId); showSnackbar('Tage werden wieder einzeln verwaltet');
  }

  function openTemplateDialog(action) {
    state.templateDialogAction = action;
    if (!els.templateDialog) return;
    els.templateDialogKicker.textContent = action.kind === 'detach' ? 'Verbindung lösen' : 'Tages-Template erstellen';
    els.templateDialogTitle.textContent = action.title;
    els.templateDialogCopy.textContent = action.copy;
    els.templateDialogConfirm.textContent = action.confirm;
    els.templateDialog.hidden = false;
    els.templateDialogConfirm.focus();
  }

  function closeTemplateDialog() { state.templateDialogAction = null; if (els.templateDialog) els.templateDialog.hidden = true; }

  async function confirmTemplateDialog() {
    const action = state.templateDialogAction;
    closeTemplateDialog();
    if (!action) return;
    if (action.kind === 'link') await persistDayGroupLink(action);
    if (action.kind === 'detach') await persistDayGroupDetach(action.group);
  }

  function renderDayGroupFocus() { /* Scope is stated in the single editor header. */ }

  function groupForDayId(dayId) { return state.plan?.week_template?.days?.find((day) => day.id === Number(dayId)) ? getDayGroup(state.plan.week_template.days.find((day) => day.id === Number(dayId)).weekday) : null; }
  function groupForSlotId(slotId) { const day = state.plan?.week_template?.days?.find((entry) => (entry.slots || []).some((slot) => slot.id === Number(slotId))); return day ? getDayGroup(day.weekday) : null; }
  async function syncGroupAfterTemplateEdit(group) {
    if (!group) return;
    const res = await fetch(`/api/nutrition/plan/day-groups/${encodeURIComponent(group.id)}/sync`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ template_id: state.selectedTemplateId }) });
    const payload = await res.json();
    if (!res.ok || !payload.ok) throw new Error(payload.error || 'Tages-Template konnte nicht auf alle Tage übertragen werden.');
    await refreshDayGroupProjection(group);
  }

  // A linked day is a projection, not a one-time copy. Refresh just that
  // projection after a write so switching to another member is immediately
  // consistent without tearing down the ingredient editor currently in use.
  async function refreshDayGroupProjection(group) {
    const freshPlan = await fetchPlan(state.selectedTemplateId);
    const freshDays = freshPlan.week_template?.days || [];
    const linkedWeekdays = new Set(group.weekdays || []);
    if (!state.plan?.week_template || linkedWeekdays.size === 0) return;

    state.plan.week_template.days = state.plan.week_template.days.map((day) => (
      linkedWeekdays.has(day.weekday)
        ? freshDays.find((candidate) => candidate.weekday === day.weekday) || day
        : day
    ));
    state.plan.day_groups = freshPlan.day_groups || [];
    state.plan.week_template.revision = freshPlan.week_template?.revision || state.plan.week_template.revision;
    state.plan.revision = freshPlan.revision || state.plan.revision;
    recomputePlanStats();
    renderDayTabs();
    renderTemplateMacroSummary();
    scheduleMacroRender();
  }

  function renderSelectedDay() {
    if (!state.plan?.week_template) return;
    const day = state.plan.week_template.days.find((d) => d.weekday === state.selectedDay);
    if (!day) return;
    const linkedGroup = getDayGroup(day.weekday);
    if (els.dayTitle) els.dayTitle.textContent = linkedGroup ? linkedGroup.name : day.label;
    if (els.dayMeta) {
      const planned = day.planned?.kcal;
      const scope = linkedGroup ? `Tages-Template · ${linkedGroup.weekdays.map(dayLabel).join(' · ')}` : null;
      els.dayMeta.textContent = scope || (planned == null ? '-- kcal' : `${Math.round(planned)} kcal`);
    }
    if (!els.dayContent) return;
    els.dayContent.innerHTML = '';
    const slots = (day.slots || [])
      .filter((slot) => slotHasMeal(slot))
      .sort((a, b) => (a.slot_index || 0) - (b.slot_index || 0));

    for (const slot of slots) {
      const card = document.createElement('article');
      card.className = 'nutrition-slot-card meal-card';
      card.dataset.slotId = slot.id;
      card.dataset.day = day.label || '';
      card.dataset.slot = String((slot.slot_index ?? 0) + 1);
      card.dataset.slotMealId = slot.slot_meal_id ? String(slot.slot_meal_id) : '';
      card.dataset.mealCard = '1';
      // Meal slots are always editing surfaces; there is no collapsed state.
      card.dataset.expanded = '1';
      if (state.selectedSlotId === slot.id) card.classList.add('is-selected');
      card.classList.add('is-expanded');

      const timeValue = slot.time_text || slot.slot_template?.default_time || '';
      const slotTitle = `Meal ${(slot.slot_index ?? 0) + 1}`;
      const noteValue = slot.note_text || '';
      card.innerHTML = `
        <div class="nutrition-slot-header meal-card-header" data-action="toggle-meal">
          <div class="slot-header-left">
            <span class="slot-title slot-title-muted" data-slot-title="${slot.id}">${slotTitle}</span>
            <span class="slot-note" contenteditable="true" spellcheck="false" data-slot-note="${slot.id}" data-placeholder="Hinweis">${noteValue}</span>
          </div>
          <span class="nutrition-slot-time" contenteditable="true" spellcheck="false" data-slot-time="${slot.id}">${timeValue}</span>
        </div>
      `;

      const active = document.createElement('div');
      active.className = 'nutrition-slot-active';
      const hasItems = (slot.items || []).length > 0;
      let macrosText = '';
      if (hasItems) {
        const m = computeIngredientsMacros(slot.items || []);
        macrosText = formatMacroLine(m);
      } else if (slot.override?.kcal != null) {
        macrosText = formatMacroLine(slot.override);
      } else if (slot.meal_template) {
        macrosText = formatMacros(slot.meal_template, slot.servings || slot.meal_template.default_servings);
      }
      const mealTitle = getWeekSlotTitle(slot) || joinFoodNames(slot.items || slot.override?.ingredients) || 'Food';
      active.innerHTML = `
        <strong class="slot-meal-title" contenteditable="true" spellcheck="false" data-slot-meal-title="${slot.id}">${mealTitle}</strong>
        <span class="nutrition-slot-meta" data-slot-macros="${slot.id}">${macrosText}</span>
      `;
      card.appendChild(active);

      const body = document.createElement('div');
      body.className = 'meal-card-body meal-body';
      body.setAttribute('data-no-toggle', '1');
      card.appendChild(body);

      const actions = document.createElement('div');
      actions.className = 'nutrition-slot-actions';
      const deleteBtn = document.createElement('button');
      deleteBtn.type = 'button';
      deleteBtn.className = 'pill ghost';
      deleteBtn.textContent = 'Meal löschen';
      deleteBtn.addEventListener('click', async (event) => {
        event.stopPropagation();
        if (deleteBtn.hasAttribute('disabled')) return;
        deleteBtn.setAttribute('disabled', 'true');
        deleteBtn.classList.add('is-loading');
        try {
          await clearSlot(slot.id);
        } catch (err) {
          showError(err, 'Meal konnte nicht gelöscht werden.');
        } finally {
          deleteBtn.removeAttribute('disabled');
          deleteBtn.classList.remove('is-loading');
        }
      });
      actions.append(deleteBtn);
      card.appendChild(actions);

      const noteEl = card.querySelector(`[data-slot-note="${slot.id}"]`);
      noteEl?.addEventListener('click', () => {
        noteEl.classList.add('is-editing');
      });
      noteEl?.addEventListener('blur', () => {
        const value = noteEl.textContent?.trim() || '';
        patchSlot(slot.id, { note_text: value || null }).catch((err) => console.error(err));
        noteEl.classList.remove('is-editing');
        if (!value) noteEl.textContent = '';
      });
      noteEl?.addEventListener('keydown', (event) => {
        if (event.key === 'Enter') {
          event.preventDefault();
          noteEl.blur();
        }
      });

      const timeEl = card.querySelector(`[data-slot-time="${slot.id}"]`);
      timeEl?.addEventListener('blur', () => {
        const value = timeEl.textContent?.trim() || '';
        updateSlotTime(slot.id, value);
      });
      timeEl?.addEventListener('keydown', (event) => {
        if (event.key === 'Enter') {
          event.preventDefault();
          timeEl.blur();
        }
      });
      els.dayContent.appendChild(card);
      // Mount the real editor directly into the slot card. Foods and amounts
      // are therefore editable immediately and never duplicated as a preview.
      toggleMealEditor(card, slot);

      const mealTitleEl = card.querySelector(`[data-slot-meal-title="${slot.id}"]`);
      mealTitleEl?.addEventListener('blur', () => {
        const value = mealTitleEl.textContent?.trim() || '';
        patchSlot(slot.id, { custom_title: value || null }).catch((err) => console.error(err));
      });
      mealTitleEl?.addEventListener('keydown', (event) => {
        if (event.key === 'Enter') {
          event.preventDefault();
          mealTitleEl.blur();
        }
      });
    }

    const addRow = document.createElement('div');
    addRow.className = 'nutrition-slot-add-row';
    addRow.innerHTML = '<button type="button" class="pill primary nutrition-slot-add-btn">Neuer Slot</button>';
    addRow.querySelector('.nutrition-slot-add-btn')?.addEventListener('click', () => {
      handleNewSlotClick(day);
    });
    els.dayContent.appendChild(addRow);

  }

  function renderSummary(derived) {
    if (!els.weekSummary || !derived) return;
    const { activeDay, target, activeBase, weekAvg, weekAvgP, weekMin, weekMax } = derived;
    if (!Number.isFinite(Number(target.kcal)) || Number(target.kcal) <= 0) {
      els.weekSummary.innerHTML = `
        <div class="macro-empty">
          <div class="macro-empty-title">Keine Woche geplant</div>
          <div class="macro-card-subtitle">Ziel anpassen im Versionsmenü.</div>
        </div>
      `;
      return;
    }

    const focusLabel = `TAG · ${activeDay?.label || '—'}`;
    const neutralDelta = (Number(activeBase?.kcal) || 0) - (Number(weekAvg) || 0);

    const macroRows = [
      { key: 'p', label: 'Protein', color: 'var(--macroP)' },
      { key: 'c', label: 'Carbs', color: 'var(--macroC)' },
      { key: 'f', label: 'Fat', color: 'var(--macroF)' },
    ];

    els.weekSummary.innerHTML = `
      <div class="macro-overview">
        <div class="macro-week-wrap">
          <div class="macro-section-title">Woche</div>
          <div class="macro-week-lines">
            <div class="macro-line"><span>Ø kcal/Tag (Woche)</span><strong>${formatNum(weekAvg)} kcal</strong></div>
            ${weekMin != null && weekMax != null ? `<div class="macro-line"><span>Min / Max Tag</span><strong>${formatNum(weekMin)} · ${formatNum(weekMax)} kcal</strong></div>` : ''}
            <div class="macro-line"><span>Protein Ø/Tag (Woche)</span><strong>${formatNum(weekAvgP)} g</strong></div>
          </div>
        </div>

        <div class="macro-today-wrap">
          <div class="macro-section-title">${focusLabel}</div>
          <div class="macro-today-mini">
            <div class="macro-today-kpis">
              <div class="macro-today-card">
                Geplant
                <strong>${formatNum(activeBase?.kcal)} kcal</strong>
              </div>
              <div class="macro-today-card">
                Target
                <strong>${formatNum(target.kcal)} kcal</strong>
              </div>
            </div>
            ${Number.isFinite(neutralDelta) && Math.abs(neutralDelta) >= 1
              ? `<div class="macro-kpi-remain">${neutralDelta >= 0 ? '+' : ''}${formatNum(neutralDelta)} ${neutralDelta >= 0 ? 'über' : 'unter'} Ø</div>`
              : ''}
            <div class="macro-week-macros">
              ${macroRows.map((row) => {
                const actual = activeBase ? activeBase[row.key] : 0;
                const t = target[row.key];
                const ratio = progressPercent(actual, t);
                return `
                  <div class="macro-row">
                    <span class="macro-row-label">${row.label}</span>
                    <div class="mini-track"><div class="mini-fill" style="width:${ratio}%; background:${row.color};"></div></div>
                    <span class="macro-row-value">${formatNum(actual)} / ${formatNum(t)}g</span>
                  </div>
                `;
              }).join('')}
            </div>
          </div>
        </div>
      </div>
    `;
  }

  function renderGapNotes(derived) {
    if (!els.gapNotes || !derived) return;
    const gapItems = derived.proteinGaps || [];
    if (!gapItems.length) {
      els.gapNotes.innerHTML = '';
      return;
    }
    const dayText = gapItems.length === 1 ? '1 Tag unter Ziel' : `${gapItems.length} Tage unter Ziel`;
    const shouldOpen = state.gapOpen;
    els.gapNotes.innerHTML = `
      <button type="button" class="gap-summary ${shouldOpen ? 'is-open' : ''}" id="gap-toggle-btn">
        <span class="gap-left">Protein-Lücken (Woche)</span>
        <span class="gap-right">${dayText}</span>
        <span class="gap-chevron">▾</span>
      </button>
      <div class="gap-list ${shouldOpen ? 'is-open' : ''}">
        ${gapItems.map((item) => `
          <div class="gap-row">
            <span class="gap-day"><i class="gap-dot ${item.severity}"></i>${item.label}</span>
            <strong>-${Math.round(item.gap)}g</strong>
          </div>
        `).join('')}
      </div>
    `;
    const toggle = document.getElementById('gap-toggle-btn');
    toggle?.addEventListener('click', () => {
      state.gapOpen = !state.gapOpen;
      scheduleMacroRender();
    });
  }

  // Makro-Übersicht nutzt den zentralen Day-Switch (state.selectedDay).

  function setLocalSlotOverride(slotId, ingredients) {
    const days = state.plan?.week_template?.days || [];
    const sourceDay = days.find((day) => (day.slots || []).some((slot) => slot.id === Number(slotId)));
    const sourceSlot = sourceDay?.slots?.find((slot) => slot.id === Number(slotId));
    if (!sourceDay || !sourceSlot) return;

    // Mirror the in-flight edit into every materialized projection right away.
    // The API sync below is still authoritative, but this keeps tabs, macros
    // and a just-opened linked weekday coherent before the save round-trip.
    const group = getDayGroup(sourceDay.weekday);
    const mirroredWeekdays = new Set(group?.weekdays || [sourceDay.weekday]);
    days.forEach((day) => {
      if (!mirroredWeekdays.has(day.weekday)) return;
      (day.slots || []).forEach((slot) => {
        if (slot.slot_index !== sourceSlot.slot_index) return;
        const localIngredients = (ingredients || []).map((item) => ({ ...item }));
        const m = computeIngredientsMacros(localIngredients);
        slot.override = {
          ingredients: localIngredients,
          kcal: Number(m.kcal.toFixed(1)),
          p: Number(m.p.toFixed(1)),
          c: Number(m.c.toFixed(1)),
          f: Number(m.f.toFixed(1)),
          export_text: localIngredients.map((i) => `${i.amount}${i.unit} ${i.food_name || ''}`.trim()).join(', '),
        };
        slot.items = localIngredients;
        slot.meal_title = sourceSlot.meal_title || sourceSlot.custom_title || joinFoodNames(localIngredients) || sourceSlot.meal_template?.title || 'Food';
        slot.display_title = getWeekSlotTitle(slot);
      });
    });
    recomputePlanStats();
    renderDayTabs();
    const day = state.plan?.week_template?.days?.find((d) => d.weekday === state.selectedDay);
    if (els.dayMeta && day?.planned?.kcal != null) {
      els.dayMeta.textContent = `${Math.round(day.planned.kcal)} kcal`;
    }
    scheduleMacroRender();
  }

  async function selectMealTemplate(mealId) {
    state.selectedMealTemplateId = mealId;
    const res = await fetch(`/api/nutrition/meal_templates/${mealId}/resolved`, { cache: 'no-store' });
    const data = await res.json();
    if (!res.ok || data.ok === false || !data.meal || !els.mealBuilderForm) return;
    const meal = data.meal;
    els.mealBuilderForm.querySelector('[name="title"]').value = meal.title || '';
    els.mealBuilderForm.querySelector('[name="default_servings"]').value = meal.default_servings || 1;
    if (els.mealSaveBtn) {
      els.mealSaveBtn.textContent = 'Mahlzeit speichern';
    }
    if (els.mealDeleteBtn) {
      els.mealDeleteBtn.disabled = false;
    }
    if (els.ingredientTray) {
      els.ingredientTray.innerHTML = '';
      (meal.ingredients || []).forEach((item) => addIngredientRow(item));
      if (!meal.ingredients || meal.ingredients.length === 0) addIngredientRow();
    }
    renderMeals();
  }

  async function patchSlot(slotId, data) {
    const group = groupForSlotId(slotId);
    setDirty(true);
    const res = await fetch(`/api/nutrition/plan/slots/${slotId}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(data),
    });
    const payload = await res.json();
    if (!res.ok) {
      throw new Error(payload.error || 'Slot konnte nicht aktualisiert werden.');
    }
    // Keep the visible editor's source of truth current immediately.  Without
    // this, a follow-up save could compare against the previous title and
    // race a fresh plan read back to the old display value.
    const updatedSlot = payload.slot || {};
    (state.plan?.week_template?.days || []).forEach((day) => {
      const localSlot = (day.slots || []).find((slot) => slot.id === Number(slotId));
      if (!localSlot) return;
      Object.assign(localSlot, updatedSlot);
      if (Object.prototype.hasOwnProperty.call(data, 'custom_title')) {
        localSlot.custom_title = updatedSlot.custom_title || null;
        localSlot.display_title = getWeekSlotTitle(localSlot);
      }
    });
    await syncGroupAfterTemplateEdit(group);
    setDirty(false);
    return payload.slot;
  }

  async function createSlot(dayId, slotTemplateId) {
    const group = groupForDayId(dayId);
    setDirty(true);
    const res = await fetch(`/api/nutrition/plan/days/${dayId}/slots`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ slot_template_id: slotTemplateId }),
    });
    const payload = await res.json();
    if (!res.ok || payload.ok === false) {
      throw new Error(payload.error || 'Slot konnte nicht erstellt werden.');
    }
    await syncGroupAfterTemplateEdit(group);
    setDirty(false);
    return payload.slot;
  }

  async function deleteSlot(slotId) {
    const group = groupForSlotId(slotId);
    setDirty(true);
    const res = await fetch(`/api/nutrition/plan/slots/${slotId}`, { method: 'DELETE' });
    const payload = await res.json();
    if (!res.ok || payload.ok === false) {
      throw new Error(payload.error || 'Slot konnte nicht gelöscht werden.');
    }
    await syncGroupAfterTemplateEdit(group);
    setDirty(false);
  }

  async function clearSlot(slotId) {
    if (state.openEditorSlotId === slotId) state.openEditorSlotId = null;
    await deleteSlot(slotId);
    await loadPlan();
  }

  async function updateSlotTime(slotId, value) {
    await patchSlot(slotId, { time_text: value || null });
    await loadPlan();
  }

  function getSlotById(slotId) {
    const day = state.plan?.week_template?.days?.find((d) => d.weekday === state.selectedDay);
    if (!day || !day.slots) return null;
    return day.slots.find((slot) => slot.id === slotId) || null;
  }

  function getSelectedSlot() {
    const day = state.plan?.week_template?.days?.find((d) => d.weekday === state.selectedDay);
    if (!day || !day.slots?.length) return null;
    if (state.selectedSlotId) {
      return day.slots.find((slot) => slot.id === state.selectedSlotId) || day.slots[0];
    }
    return day.slots[0];
  }

  function slotHasMeal(slot) {
    if (!slot) return false;
    return !!((slot.items || []).length > 0 || slot.override?.ingredients?.length || slot.slot_meal_id || slot.meal_template);
  }

  async function openSlotEditorById(slotId) {
    state.selectedSlotId = slotId;
    state.openEditorSlotId = slotId;
    renderSelectedDay();
    const card = document.querySelector(`[data-slot-id="${slotId}"]`);
    const slot = getSlotById(slotId);
    if (card && slot && !card.querySelector('.nutrition-meal-editor')) {
      await toggleMealEditor(card, slot);
    }
  }

  async function handleNewSlotClick(day) {
    try {
      let currentDay = state.plan?.week_template?.days?.find((entry) => entry.id === day?.id) || day;
      const emptySlot = (currentDay?.slots || []).find((slot) => !slotHasMeal(slot));
      if (emptySlot?.id) {
        await deleteSlot(emptySlot.id);
        await loadPlan(state.selectedTemplateId);
        currentDay = state.plan?.week_template?.days?.find((entry) => entry.id === day?.id) || currentDay;
      }
      const occupied = new Set((currentDay?.slots || []).map((slot) => Number(slot.slot_index || 0)));
      let nextIndex = 0;
      while (occupied.has(nextIndex)) nextIndex += 1;
      const slotTemplate = state.slotTemplates.find((entry) => entry.title === `Meal ${nextIndex + 1}`) || state.slotTemplates[nextIndex] || state.slotTemplates[0];
      if (!currentDay?.id || !slotTemplate?.id) {
        throw new Error('slot_template_not_found');
      }
      const created = await createSlot(currentDay.id, slotTemplate.id);
      await loadPlan(state.selectedTemplateId);
      if (created?.id) {
        await openSlotEditorById(created.id);
      }
    } catch (err) {
      showError(err, 'Neuer Slot konnte nicht erstellt werden.');
    }
  }

  async function insertMealToSelectedSlot(meal) {
    const slot = getSelectedSlot();
    if (!slot) {
      alert('Bitte zuerst einen Slot auswählen.');
      return;
    }
    await patchSlot(slot.id, { meal_template_id: meal.id });
    await loadPlan();
  }

  async function toggleMealEditor(container, slot) {
    const existing = container.querySelector('.nutrition-meal-editor');
    if (existing) return existing;
    container.dataset.expanded = '1';
    container.classList.add('is-expanded');
    // The week payload already contains the resolved ingredients. Reusing it
    // avoids one extra request per slot during page load and keeps the inline
    // editor usable on slower LIVA connections.
    const embeddedIngredients = slot.override?.ingredients?.length
      ? slot.override.ingredients
      : (slot.items || []);
    const data = { ingredients: embeddedIngredients };
    const editor = document.createElement('div');
    editor.className = 'nutrition-meal-editor';
    editor.innerHTML = `
      <div class="nutrition-slot-meta">Foods &amp; Mengen</div>
      <div class="nutrition-ingredient-list"></div>
      <div class="nutrition-ingredient-add">
        <select class="meta-input" data-food-select>
          <option value="">Food wählen</option>
          ${state.foods.map((food) => `<option value="${food.id}">${food.name}</option>`).join('')}
        </select>
        <input class="meta-input ingredient-amount" data-food-amount type="number" step="0.1" placeholder="Menge" />
        <button class="pill ghost" data-food-add type="button">+ Hinzufügen</button>
      </div>
    `;
    const list = editor.querySelector('.nutrition-ingredient-list');
    const seen = new Set();
    const working = (data.ingredients || []).filter((item) => {
      const key = item.id ?? item.fingerprint ?? `${item.food_id || 'k'}|${item.amount}|${item.unit}|${item.food_name || item.name}`;
      if (seen.has(key)) return false;
      seen.add(key);
      return true;
    }).map((item) => ({ ...item }));

    let saveTimer = null;
    function updateHeaderMacros() {
      const macros = computeIngredientsMacros(working);
      const header = container.querySelector(`[data-slot-macros="${slot.id}"]`);
      if (header) {
        header.textContent = formatMacroLine(macros);
      }
    }
    function refreshRowMacros() {
      const rows = list.querySelectorAll('.nutrition-ingredient-row');
      rows.forEach((row, idx) => {
        const item = working[idx];
        if (!item) return;
        const macroEl = row.querySelector('.ingredient-macros');
        if (macroEl) {
          macroEl.textContent = formatMacroLine(item.item_type === 'kcal_only' ? { kcal: Number(item.calories ?? item.amount) || 0, p: 0, c: 0, f: 0 } : computeItemMacros(item));
        }
      });
    }
    function scheduleSave() {
      if (saveTimer) window.clearTimeout(saveTimer);
      saveTimer = window.setTimeout(async () => {
        try {
          const updated = await saveSlotIngredients(slot.id, working);
          if (updated?.ingredients) {
            working.splice(0, working.length, ...updated.ingredients.map((i) => ({ ...i })));
            refreshRowMacros();
            updateHeaderMacros();
          }
          setLocalSlotOverride(slot.id, working);
        } catch (err) {
          showError(err, 'Zutaten konnten nicht gespeichert werden.');
        }
      }, 350);
    }
    function renderList() {
      list.innerHTML = '';
      working.forEach((item, index) => {
        const row = document.createElement('div');
        row.className = 'nutrition-ingredient-row';
        const isKcal = item.item_type === 'kcal_only' || String(item.unit || '').toLowerCase() === 'kcal';
        if (isKcal) {
          const label = item.food_name || item.name || 'Kcal';
          const kcalValue = Number(item.calories ?? item.amount ?? 0);
          const macroLine = formatMacroLine({ kcal: kcalValue, p: 0, c: 0, f: 0 });
          row.innerHTML = `
            <div class="ingredient-left">
              <span class="ingredient-name">${label}</span>
              <span class="ingredient-macros">${macroLine}</span>
            </div>
            <div class="ingredient-right">
              <span class="ingredient-unit">kcal</span>
              <button class="icon-btn subtle" data-item-delete="${index}" type="button" aria-label="Zutat löschen">✕</button>
            </div>
          `;
        } else {
          const macros = computeItemMacros(item);
          const macroLine = formatMacroLine(macros);
          row.innerHTML = `
            <div class="ingredient-left">
              <span class="ingredient-name" data-food-id="${item.food_id || ''}">${item.food_name || item.name || 'Food'}</span>
              <span class="ingredient-macros" data-food-id="${item.food_id || ''}">${macroLine}</span>
            </div>
            <div class="ingredient-right">
              <input class="meta-input ingredient-amount" type="number" step="0.1" value="${item.amount}" data-item-amount="${index}" />
              <span class="ingredient-unit">${item.unit || ''}</span>
              <button class="icon-btn subtle" data-item-delete="${index}" type="button" aria-label="Zutat löschen">✕</button>
            </div>
          `;
          const amountInput = row.querySelector(`[data-item-amount="${index}"]`);
          amountInput?.addEventListener('input', (event) => {
            const raw = String(event.target.value || '').replace(',', '.');
            const value = Number(raw);
            working[index].amount = Number.isFinite(value) ? value : 0;
            const macroEl = row.querySelector('.ingredient-macros');
            if (macroEl) macroEl.textContent = formatMacroLine(computeItemMacros(working[index]));
            updateHeaderMacros();
            setLocalSlotOverride(slot.id, working);
            scheduleSave();
          });
        }
        row.querySelectorAll('[data-food-id]')?.forEach((el) => {
          el.addEventListener('click', () => {
            const fid = Number(el.getAttribute('data-food-id'));
            if (!fid) return;
            const food = state.foods.find((f) => f.id === fid);
            if (!food) return;
            startFoodEdit(food);
          });
        });
        row.querySelector(`[data-item-delete="${index}"]`)?.addEventListener('click', async () => {
          working.splice(index, 1);
          const updated = await saveSlotIngredients(slot.id, working);
          if (updated?.ingredients) {
            working.splice(0, working.length, ...updated.ingredients);
          }
          renderList();
          updateHeaderMacros();
          setLocalSlotOverride(slot.id, working);
        });
        list.appendChild(row);
      });
    }
    renderList();
    editor.querySelector('[data-food-add]')?.addEventListener('click', async (event) => {
      event.preventDefault();
      const foodId = Number(editor.querySelector('[data-food-select]')?.value);
      const amountRaw = editor.querySelector('[data-food-amount]')?.value || '';
      const amount = Number(String(amountRaw).replace(',', '.'));
      if (!foodId || !amount) return;
      const food = state.foods.find((f) => f.id === foodId);
      if (!food) return;
      const btn = editor.querySelector('[data-food-add]');
      if (btn?.hasAttribute('disabled')) return;
      if (btn) btn.setAttribute('disabled', 'true');
      try {
        const reqId = (typeof crypto !== 'undefined' && crypto.randomUUID) ? crypto.randomUUID() : `${Date.now()}-${Math.random().toString(16).slice(2)}`;
        const unit = food.unit_default || 'g';
        working.push({ food_id: foodId, food_name: food.name, amount, unit, client_req_id: reqId });
        editor.querySelector('[data-food-select]').value = '';
        editor.querySelector('[data-food-amount]').value = '';
        const updated = await saveSlotIngredients(slot.id, working);
        if (updated?.ingredients) {
          working.splice(0, working.length, ...updated.ingredients);
        }
        renderList();
        updateHeaderMacros();
        setLocalSlotOverride(slot.id, working);
      } catch (err) {
        showError(err, 'Zutat konnte nicht gespeichert werden.');
      } finally {
        if (btn) btn.removeAttribute('disabled');
      }
    });
    const body = container.querySelector('.meal-body') || container;
    body.appendChild(editor);
    return editor;
  }

  function collapseAllSlotEditors() {
    // Kept as a compatibility no-op for older event paths. Meal slots are
    // intentionally always open and independently editable now.
  }

  async function saveSlotIngredients(slotId, ingredients) {
    const group = groupForSlotId(slotId);
    const prev = ingredientSaveQueue.get(slotId) || Promise.resolve();
    const next = prev
      .catch(() => {})
      .then(async () => {
        setDirty(true);
        const res = await fetch(`/api/nutrition/plan/slots/${slotId}/ingredients`, {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ ingredients }),
        });
        const data = await res.json();
        if (!res.ok) {
          throw new Error(data.error || 'Zutaten konnten nicht gespeichert werden.');
        }
        await syncGroupAfterTemplateEdit(group);
        setDirty(false);
        return data;
      })
      .finally(() => {
        if (ingredientSaveQueue.get(slotId) === next) {
          ingredientSaveQueue.delete(slotId);
        }
      });
    ingredientSaveQueue.set(slotId, next);
    return next;
  }

  async function updateFood(foodId, payload) {
    const res = await fetch(`/api/nutrition/foods/${foodId}`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    const data = await res.json();
    if (!res.ok) {
      throw new Error(data.error || 'Food konnte nicht aktualisiert werden.');
    }
    await loadPlan();
  }

  async function deleteFood(foodId) {
    const res = await fetch(`/api/nutrition/foods/${foodId}`, { method: 'DELETE' });
    const data = await res.json();
    if (!res.ok) {
      throw new Error(data.error || 'Food konnte nicht gelöscht werden.');
    }
    await loadPlan();
  }

  function startFoodEdit(food) {
    if (!els.foodForm) return;
    state.foodEditId = food.id;
    const setFieldValue = (name, value) => {
      const input = els.foodForm.querySelector(`[name="${name}"]`);
      if (!input) return;
      const next = value == null ? '' : String(value);
      if (input.value !== next) input.value = next;
    };
    const unit = String(food.unit_default || 'g');
    setFieldValue('name', food.name || '');
    setFieldValue('unit_default', unit);
    setFieldValue('kcal_per_100', food.kcal_per_100 ?? 0);
    setFieldValue('p_per_100', food.p_per_100 ?? 0);
    setFieldValue('c_per_100', food.c_per_100 ?? 0);
    setFieldValue('sugar_per_100', food.sugar_per_100 ?? 0);
    setFieldValue('f_per_100', food.f_per_100 ?? 0);
    setFoodLabelMode(unit === 'pcs' ? 'perpcs' : 'per100');
    if (els.foodSubmitBtn) els.foodSubmitBtn.textContent = 'Änderungen speichern';
    if (els.foodDeleteBtn) els.foodDeleteBtn.disabled = false;
    renderFoods();
  }

  function resetFoodForm() {
    if (!els.foodForm) return;
    els.foodForm.reset();
    state.foodEditId = null;
    if (els.foodSubmitBtn) els.foodSubmitBtn.textContent = 'Food speichern';
    if (els.foodDeleteBtn) els.foodDeleteBtn.disabled = true;
    setFoodLabelMode('per100');
  }

  async function sendFoodForm(payload) {
    const res = await fetch('/api/nutrition/foods', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    const data = await res.json();
    if (!res.ok) {
      throw new Error(data.error || 'Food konnte nicht gespeichert werden.');
    }
    await loadPlan();
    return data.food || null;
  }

  async function sendMealBuilder(payload, templateId = null) {
    const url = templateId ? `/api/nutrition/meal_templates/${templateId}` : '/api/nutrition/meal_templates';
    const method = templateId ? 'PUT' : 'POST';
    const res = await fetch(url, {
      method,
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    const data = await res.json();
    if (!res.ok) {
      throw new Error(data.error || 'Meal konnte nicht gespeichert werden.');
    }
    return data.template || null;
  }

  async function deleteSelectedMealTemplate() {
    if (!state.selectedMealTemplateId) return;
    if (!confirm('Mahlzeit wirklich löschen?')) return;
    const res = await fetch(`/api/nutrition/meal_templates/${state.selectedMealTemplateId}`, { method: 'DELETE' });
    const data = await res.json();
    if (!res.ok || data.ok === false) {
      throw new Error(data.error || 'Mahlzeit konnte nicht gelöscht werden.');
    }
    state.selectedMealTemplateId = null;
    if (els.mealBuilderForm) {
      els.mealBuilderForm.reset();
    }
    resetIngredientRows();
    if (els.mealDeleteBtn) {
      els.mealDeleteBtn.disabled = true;
    }
    await loadPlan();
  }

  async function deleteSelectedFood() {
    if (!state.foodEditId) return;
    if (!confirm('Food wirklich löschen?')) return;
    await deleteFood(state.foodEditId);
    resetFoodForm();
  }

  async function addSelectedFoodToDay() {
    let food = state.foods.find((f) => f.id === state.foodEditId);
    if (!food && els.foodForm) {
      const fd = new FormData(els.foodForm);
      const payload = {
        name: fd.get('name'),
        unit_default: fd.get('unit_default') || 'g',
        kcal_per_100: Number(fd.get('kcal_per_100')) || 0,
        p_per_100: Number(fd.get('p_per_100')) || 0,
        c_per_100: Number(fd.get('c_per_100')) || 0,
        sugar_per_100: Number(fd.get('sugar_per_100')) || 0,
        f_per_100: Number(fd.get('f_per_100')) || 0,
      };
      if (!payload.name) return;
      const created = await sendFoodForm(payload);
      food = created;
      state.foodEditId = created?.id || null;
    }
    if (!food) return;
    const slot = getSelectedSlot();
    if (!slot) {
      alert('Bitte zuerst einen Slot auswählen.');
      return;
    }
    const res = await fetch(`/api/nutrition/plan/slots/${slot.id}/ingredients`, { cache: 'no-store' });
    const data = await res.json();
    if (!res.ok || data.ok === false) {
      throw new Error(data.error || 'Slot-Zutaten konnten nicht geladen werden.');
    }
    const unit = food.unit_default || 'g';
    const amount = unit === 'pcs' ? 1 : (Number(food.common_portion_size || food.portion_g) || 100);
    const ingredients = (data.ingredients || []).map((item) => ({ ...item }));
    ingredients.push({ food_id: food.id, food_name: food.name, amount, unit });
    setLocalSlotOverride(slot.id, ingredients);
    await saveSlotIngredients(slot.id, ingredients);
    renderSelectedDay();
  }

  function addIngredientRow(initial = null) {
    if (!els.ingredientTray) return;
    const row = document.createElement('div');
    row.className = 'ingredient-row';
    const select = document.createElement('select');
    const placeholder = document.createElement('option');
    placeholder.value = '';
    placeholder.textContent = 'Food wählen';
    select.appendChild(placeholder);
    state.foods.forEach((food) => {
      const option = document.createElement('option');
      option.value = food.id;
      option.textContent = food.name;
      select.appendChild(option);
    });
    if (initial?.food_id) {
      select.value = String(initial.food_id);
    }
    const amount = document.createElement('input');
    amount.type = 'number';
    amount.step = '0.1';
    amount.min = '0';
    amount.className = 'ingredient-amount';
    amount.placeholder = 'Menge';
    if (initial?.amount != null) {
      amount.value = initial.amount;
    }
    const unit = document.createElement('select');
    unit.className = 'ingredient-unit';
    UNIT_OPTIONS.forEach((u) => {
      const option = document.createElement('option');
      option.value = u;
      option.textContent = u;
      if ((initial?.unit || 'g') === u) option.selected = true;
      unit.appendChild(option);
    });
    const remove = document.createElement('button');
    remove.type = 'button';
    remove.className = 'ingredient-remove';
    remove.textContent = '×';
    remove.addEventListener('click', () => {
      if (els.ingredientTray.childElementCount > 1) {
        row.remove();
      }
    });
    row.append(select, amount, unit, remove);
    els.ingredientTray.appendChild(row);
  }

  function resetIngredientRows() {
    if (!els.ingredientTray) return;
    els.ingredientTray.innerHTML = '';
    addIngredientRow();
  }

  function resetMealForm() {
    state.selectedMealTemplateId = null;
    if (els.mealBuilderForm) {
      els.mealBuilderForm.reset();
    }
    resetIngredientRows();
    if (els.mealDeleteBtn) {
      els.mealDeleteBtn.disabled = true;
    }
    renderMeals();
  }

  async function fetchShoppingList() {
    const qs = state.selectedTemplateId ? `?template_id=${encodeURIComponent(state.selectedTemplateId)}` : '';
    const res = await fetch(`/api/nutrition/export/shopping_list${qs}`, { cache: 'no-store' });
    const data = await res.json();
    if (!res.ok || data.ok === false) {
      throw new Error(data.error || 'Einkaufsliste konnte nicht geladen werden.');
    }
    const text = data.shopping_list
      .map((item) => item.display_text || `${Math.round(item.total_grams)} g ${item.name}`)
      .join('\n');
    els.shoppingOutput.textContent = text;
    if (!state.mfpExportOpen.shopping) {
      state.mfpExportOpen.shopping = true;
    }
    updateMfpDrawerVisibility('shopping');
  }

  async function fetchChecklist() {
    const qs = state.selectedTemplateId ? `?template_id=${encodeURIComponent(state.selectedTemplateId)}` : '';
    const res = await fetch(`/api/nutrition/export/checklist${qs}`, { cache: 'no-store' });
    const data = await res.json();
    if (!res.ok || data.ok === false) {
      throw new Error(data.error || 'Checklist konnte nicht geladen werden.');
    }
    const lines = [];
    (data.checklist || []).forEach((day) => {
      lines.push(`## ${day.label}`);
      day.slots.forEach((slot) => {
        const timeText = (slot.time_text || '').trim();
        const prefix = timeText ? `[${timeText}] ` : '';
        lines.push(`- ${prefix}${slot.slot_title}: ${slot.meal_title} (${slot.servings}x)`);
        (slot.ingredients || []).forEach((ingredient) => {
          lines.push(`  - ${ingredient.line}`);
        });
      });
      lines.push('');
    });
    els.checklistOutput.textContent = lines.join('\n');
    if (!state.mfpExportOpen.checklist) {
      state.mfpExportOpen.checklist = true;
    }
    updateMfpDrawerVisibility('checklist');
  }

  function getMfpDrawerContent(key) {
    if (key === 'shopping') return els.shoppingOutput?.textContent || '';
    if (key === 'checklist') return els.checklistOutput?.textContent || '';
    return '';
  }

  function updateMfpDrawerVisibility(key) {
    const drawer = document.querySelector(`[data-mfp-drawer="${key}"]`);
    const toggleBtn = document.querySelector(`[data-mfp-toggle="${key}"]`);
    if (!drawer) return;
    const shouldShow = !!state.mfpExportOpen[key];
    drawer.classList.toggle('is-open', shouldShow);
    if (toggleBtn) {
      toggleBtn.setAttribute('aria-expanded', shouldShow ? 'true' : 'false');
    }
  }

  function toggleMfpDrawer(key) {
    state.mfpExportOpen[key] = !state.mfpExportOpen[key];
    updateMfpDrawerVisibility(key);
  }

  function renderMfpBridgeMode() {
    const mode = state.mfpBridgeMode === 'import' ? 'import' : 'export';
    if (els.mfpPaneExport) {
      els.mfpPaneExport.toggleAttribute('hidden', mode !== 'export');
    }
    if (els.mfpPaneImport) {
      els.mfpPaneImport.toggleAttribute('hidden', mode !== 'import');
    }
    if (els.mfpSegmented) {
      els.mfpSegmented.setAttribute('data-active', mode);
    }
    Array.from(els.mfpModeButtons || []).forEach((btn) => {
      btn.classList.toggle('is-active', btn.dataset.mfpMode === mode);
    });
  }

  function setMfpBridgeMode(mode) {
    const normalized = mode === 'import' ? 'import' : 'export';
    state.mfpBridgeMode = normalized;
    try {
      localStorage.setItem(MFP_MODE_STORAGE_KEY, normalized);
    } catch (err) {
      console.warn('Local storage unavailable', err);
    }
    renderMfpBridgeMode();
  }

  function toggleImportModeDetails() {
    const showTemplateName = (els.mfpImportMode?.value || 'overwrite') === 'new_template';
    if (!els.mfpImportTemplateNameWrapper) return;
    if (showTemplateName) {
      els.mfpImportTemplateNameWrapper.removeAttribute('hidden');
    } else {
      els.mfpImportTemplateNameWrapper.setAttribute('hidden', 'true');
    }
  }

  function renderImportResult(result = state.mfpImportResult) {
    if (!els.mfpImportResult) return;
    const hasContent =
      (result.summary && Object.keys(result.summary.days || {}).length > 0) ||
      (result.stats && Object.keys(result.stats || {}).length > 0) ||
      (result.errors && result.errors.length > 0) ||
      (result.warnings && result.warnings.length > 0) ||
      (result.writeErrors && result.writeErrors.length > 0) ||
      (result.unmatched && result.unmatched.length > 0);
    els.mfpImportResult.classList.toggle('hidden', !hasContent);
    renderImportSummary(result.summary, result.stats, result.mode);
    renderImportErrors(result.errors, result.writeErrors);
    renderImportWarnings(result.warnings);
    renderImportUnmatched(result);
  }

  function showImportInlineError(message) {
    if (!els.mfpImportError) return;
    els.mfpImportError.textContent = message;
    els.mfpImportError.removeAttribute('hidden');
    els.mfpImportInput?.classList.add('has-error');
  }

  function clearImportInlineError() {
    if (!els.mfpImportError) return;
    els.mfpImportError.setAttribute('hidden', 'true');
    els.mfpImportInput?.classList.remove('has-error');
  }

  function updateImportScopeState() {
    const slots = !!els.mfpScopeSlots?.checked;
    const templates = !!els.mfpScopeTemplates?.checked;
    if (els.mfpScopeHint) {
      if (!slots && !templates) els.mfpScopeHint.removeAttribute('hidden');
      else els.mfpScopeHint.setAttribute('hidden', 'true');
    }
    if (els.mfpImportBtn) {
      els.mfpImportBtn.disabled = !slots && !templates;
    }
  }

  function renderImportSummary(summary, stats, mode) {
    if (!els.mfpImportSummary) return;
    els.mfpImportSummary.innerHTML = '';
    if (!summary && !stats) return;
    const frag = document.createDocumentFragment();
    if (mode) {
      const modeChip = document.createElement('span');
      modeChip.textContent = mode === 'validate' ? 'Vorschau (nicht importiert)' : 'Import ausgeführt';
      frag.appendChild(modeChip);
    }
    if (summary) {
      const days = summary.days || {};
      Object.entries(days).forEach(([label, counts]) => {
        const span = document.createElement('span');
        span.textContent = `${label}: ${counts.meals} Meals · ${counts.items} Items`;
        frag.appendChild(span);
      });
      const total = document.createElement('strong');
      total.textContent = `Gesamt: ${summary.total_meals} Meals · ${summary.total_items} Items`;
      frag.appendChild(total);
    }
    if (stats) {
      const extra = document.createElement('span');
      extra.textContent = `Templates: ${stats.created_templates || 0} neu, ${stats.reused_templates || 0} reused · Instances: ${stats.created_instances || 0}`;
      frag.appendChild(extra);
      const extra2 = document.createElement('span');
      extra2.textContent = `Unmatched: ${stats.unmatched_count || 0} · kcal-only: ${stats.kcal_only_count || 0}`;
      frag.appendChild(extra2);
      if (summary && summary.total_meals != null) {
        const extra3 = document.createElement('span');
        extra3.textContent = `Slots: ${stats.created_instances || 0}/${summary.total_meals}`;
        frag.appendChild(extra3);
      }
    }
    els.mfpImportSummary.appendChild(frag);
  }

  function renderImportErrors(errors = [], writeErrors = []) {
    if (!els.mfpImportErrors) return;
    els.mfpImportErrors.innerHTML = '';
    if (!errors.length && !writeErrors.length) return;
    if (errors.length) {
      const heading = document.createElement('div');
      heading.className = 'nutrition-mfp-issue-heading';
      heading.textContent = 'Parse-Fehler';
      els.mfpImportErrors.appendChild(heading);
      errors.forEach((issue) => {
        const wrapper = document.createElement('div');
        wrapper.className = 'nutrition-mfp-error-line';
        const label = document.createElement('span');
        const severity = (issue.severity || 'error').toUpperCase();
        label.textContent = `${severity}${issue.line ? ` · Zeile ${issue.line}` : ''}`;
        const message = document.createElement('span');
        message.textContent = issue.message;
        wrapper.append(label, message);
        if (issue.raw) {
          const raw = document.createElement('div');
          raw.className = 'nutrition-unmatched-raw';
          raw.textContent = issue.raw;
          wrapper.appendChild(raw);
        }
        if (issue.raw_repr) {
          const rawRepr = document.createElement('div');
          rawRepr.className = 'nutrition-unmatched-raw nutrition-mfp-raw-repr';
          rawRepr.textContent = issue.raw_repr;
          wrapper.appendChild(rawRepr);
        }
        if (issue.hint) {
          const hint = document.createElement('div');
          hint.className = 'nutrition-mfp-error-hint';
          hint.textContent = issue.hint;
          wrapper.appendChild(hint);
        }
        els.mfpImportErrors.appendChild(wrapper);
      });
      const tip = document.createElement('div');
      tip.className = 'nutrition-mfp-error-tip';
      tip.textContent = 'Tipp: Tabs/Leerzeichen – kopiere den Export direkt aus LIVA.';
      els.mfpImportErrors.appendChild(tip);
    }
    if (writeErrors.length) {
      const heading = document.createElement('div');
      heading.className = 'nutrition-mfp-issue-heading';
      heading.textContent = 'Speicherfehler';
      els.mfpImportErrors.appendChild(heading);
      writeErrors.forEach((issue) => {
        const wrapper = document.createElement('div');
        wrapper.className = 'nutrition-mfp-error-line';
        const label = document.createElement('span');
        label.textContent = 'ERROR';
        const message = document.createElement('span');
        message.textContent = issue.message || 'DB write fehlgeschlagen';
        wrapper.append(label, message);
        if (issue.detail) {
          const raw = document.createElement('div');
          raw.className = 'nutrition-unmatched-raw';
          raw.textContent = issue.detail;
          wrapper.appendChild(raw);
        }
        els.mfpImportErrors.appendChild(wrapper);
      });
    }
  }

  function renderImportWarnings(warnings = []) {
    if (!els.mfpImportWarnings) return;
    els.mfpImportWarnings.innerHTML = '';
    if (!warnings.length) return;
    const heading = document.createElement('div');
    heading.className = 'nutrition-mfp-issue-heading';
    heading.textContent = 'Hinweise';
    els.mfpImportWarnings.appendChild(heading);
    warnings.forEach((issue) => {
      const wrapper = document.createElement('div');
      wrapper.className = 'nutrition-mfp-error-line is-warning';
      const label = document.createElement('span');
      const severity = (issue.severity || 'warning').toUpperCase();
      label.textContent = `${severity}${issue.line ? ` · Zeile ${issue.line}` : ''}`;
      const message = document.createElement('span');
      message.textContent = issue.message;
      wrapper.append(label, message);
      if (issue.raw) {
        const raw = document.createElement('div');
        raw.className = 'nutrition-unmatched-raw';
        raw.textContent = issue.raw;
        wrapper.appendChild(raw);
      }
      els.mfpImportWarnings.appendChild(wrapper);
    });
  }

  function renderImportUnmatched(result) {
    if (!els.mfpImportUnmatched) return;
    const unmatched = result.unmatched || [];
    els.mfpImportUnmatched.innerHTML = '';
    const hasBlockingIssues =
      (result.errors && result.errors.length > 0) ||
      (result.writeErrors && result.writeErrors.length > 0) ||
      result.ok === false;
    if (!unmatched.length) {
      if (!hasBlockingIssues) {
        const empty = document.createElement('div');
        empty.className = 'nutrition-unmatched-empty';
        empty.textContent = 'Alle Items zugeordnet.';
        els.mfpImportUnmatched.appendChild(empty);
      }
      return;
    }
    const heading = document.createElement('div');
    heading.className = 'nutrition-unmatched-heading';
    heading.textContent = 'Zuordnungen';
    els.mfpImportUnmatched.appendChild(heading);
    unmatched.forEach((item) => {
      const row = document.createElement('div');
      row.className = 'nutrition-unmatched-row';
      row.dataset.unmatchedKey = item.unmatched_key;
      const meta = document.createElement('div');
      meta.className = 'nutrition-unmatched-meta';
      const title = document.createElement('span');
      title.textContent = `${item.day} · Meal ${item.meal_slot || '—'}`;
      meta.appendChild(title);
      if (item.line) {
        const lineHint = document.createElement('span');
        lineHint.textContent = `Zeile ${item.line}`;
        meta.appendChild(lineHint);
      }
      const raw = document.createElement('div');
      raw.className = 'nutrition-unmatched-raw';
      raw.textContent = item.raw || '';
      if (item.parsed && item.parsed.name) {
        const parsed = document.createElement('div');
        parsed.className = 'nutrition-unmatched-parsed';
        const amount = item.parsed.amount != null ? item.parsed.amount : '';
        const unit = item.parsed.unit || '';
        parsed.textContent = `${amount} ${unit} ${item.parsed.name}`.trim();
        row.append(meta, raw, parsed);
      } else {
        row.append(meta, raw);
      }
      const actions = document.createElement('div');
      actions.className = 'nutrition-unmatched-actions';
      const select = document.createElement('select');
      select.className = 'nutrition-unmatched-select';
      select.dataset.unmatchedSelect = item.unmatched_key;
      const defaultOption = document.createElement('option');
      defaultOption.value = '';
      defaultOption.textContent = 'Food wählen';
      select.appendChild(defaultOption);
      (item.candidates || []).forEach((candidate) => {
        const option = document.createElement('option');
        option.value = candidate.food_id;
        option.textContent = candidate.label;
        select.appendChild(option);
      });
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'pill nutrition-unmatched-button';
      button.dataset.importResolve = 'true';
      button.dataset.unmatchedKey = item.unmatched_key;
      button.textContent = 'Zuordnen speichern';
      if (!state.mfpImportResult.importId) {
        button.setAttribute('disabled', 'true');
        button.setAttribute('title', 'Erst nach dem Import verfügbar');
      }
      actions.append(select, button);
      row.append(actions);
      els.mfpImportUnmatched.appendChild(row);
    });
  }

  async function submitMealplanImport() {
    if (state.isImportingMfp) return;
    if (!els.mfpImportInput) return;
    const text = els.mfpImportInput.value.trim();
    if (!text) {
      showImportInlineError('Füge einen Exporttext ein, um zu importieren.');
      return;
    }
    updateImportScopeState();
    if (els.mfpImportBtn?.disabled) return;
    clearImportInlineError();
    const importBtn = els.mfpImportBtn;
    state.isImportingMfp = true;
    importBtn?.classList.add('is-loading');
    importBtn?.setAttribute('disabled', 'true');
    const payload = buildImportPayload(text);
    try {
      const res = await fetch('/api/planning/mealplan/import', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      const data = await res.json();
      if (!res.ok || data.ok === false) {
      state.mfpImportResult = {
        importId: data.import_id || null,
        summary: null,
        unmatched: data.unmatched || [],
        stats: data.stats || null,
        mode: data.mode || null,
        errors: data.errors?.length
          ? data.errors
          : [{ line: null, message: data.error || 'Import fehlgeschlagen', raw: '', severity: 'error' }],
        warnings: data.warnings || [],
        writeErrors: data.write_errors || [],
          ok: false,
        };
        renderImportResult(state.mfpImportResult);
        showSnackbar('Import fehlgeschlagen');
        return;
      }
      state.mfpImportResult = {
        importId: data.import_id,
        summary: data.summary,
        unmatched: data.unmatched || [],
        stats: data.stats || null,
        mode: data.mode || null,
        errors: data.errors || [],
        warnings: data.warnings || [],
        writeErrors: data.write_errors || [],
        ok: true,
      };
      renderImportResult(state.mfpImportResult);
      const nextTemplateId = data.template_id ? Number(data.template_id) : state.selectedTemplateId;
      try {
        await loadTemplates();
        if (nextTemplateId) {
          state.selectedTemplateId = nextTemplateId;
        }
        await loadPlan(nextTemplateId || state.selectedTemplateId);
      } catch (reloadErr) {
        console.error(reloadErr);
      }
      showSnackbar('Import erfolgreich');
    } catch (err) {
      console.error(err);
      showSnackbar('Import fehlgeschlagen');
    } finally {
      state.isImportingMfp = false;
      importBtn?.classList.remove('is-loading');
      importBtn?.removeAttribute('disabled');
    }
  }

  function buildImportPayload(text) {
    return {
      target: {
        type: 'template',
        id: state.selectedTemplateId,
      },
      mode: els.mfpImportMode?.value || 'overwrite',
      new_template_name: els.mfpImportTemplateName?.value,
      text,
      import_scope: {
        slots: !!els.mfpScopeSlots?.checked,
        templates: !!els.mfpScopeTemplates?.checked,
      },
    };
  }

  async function validateMealplanImport() {
    if (state.isValidatingMfp) return;
    if (!els.mfpImportInput) return;
    const text = els.mfpImportInput.value.trim();
    if (!text) {
      showImportInlineError('Füge einen Exporttext ein, um zu validieren.');
      return;
    }
    updateImportScopeState();
    clearImportInlineError();
    state.isValidatingMfp = true;
    const validateBtn = els.mfpValidateBtn;
    if (validateBtn) {
      validateBtn.dataset.originalLabel = validateBtn.textContent;
      validateBtn.textContent = 'Validiere…';
      validateBtn.classList.add('is-loading');
      validateBtn.setAttribute('disabled', 'true');
    }
    try {
      const payload = buildImportPayload(text);
      const res = await fetch('/api/planning/mealplan/validate', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      const data = await res.json();
      if (!res.ok || data.ok === false) {
      state.mfpImportResult = {
        importId: null,
        summary: data.summary || null,
        unmatched: data.unmatched || [],
        stats: data.stats || null,
        mode: data.mode || null,
        errors: data.errors?.length
          ? data.errors
          : [{ line: null, message: data.error || 'Validierung fehlgeschlagen', raw: '', severity: 'error' }],
        warnings: data.warnings || [],
        writeErrors: [],
          ok: false,
        };
        renderImportResult(state.mfpImportResult);
        showSnackbar('Validierung fehlgeschlagen');
        return;
      }
      state.mfpImportResult = {
        importId: null,
        summary: data.summary,
        unmatched: data.unmatched || [],
        stats: data.stats || null,
        mode: data.mode || null,
        errors: data.errors || [],
        warnings: data.warnings || [],
        writeErrors: data.write_errors || [],
        ok: true,
      };
      renderImportResult(state.mfpImportResult);
      showSnackbar('Validierung abgeschlossen');
    } catch (err) {
      console.error(err);
      showSnackbar('Validierung fehlgeschlagen');
    } finally {
      state.isValidatingMfp = false;
      if (validateBtn) {
        validateBtn.textContent = validateBtn.dataset.originalLabel || 'Validieren';
        validateBtn.removeAttribute('disabled');
        validateBtn.classList.remove('is-loading');
        delete validateBtn.dataset.originalLabel;
      }
    }
  }

  async function resolveImportMapping(key, foodId, button) {
    if (!key || !foodId || !state.mfpImportResult.importId) return;
    const originalText = button.textContent;
    button.disabled = true;
    button.textContent = 'Speichere…';
    try {
      const res = await fetch('/api/planning/mealplan/import/resolve', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          import_id: state.mfpImportResult.importId,
          mappings: [{ unmatched_key: key, food_id: Number(foodId) }],
        }),
      });
      const data = await res.json();
      if (!res.ok || data.ok === false) {
        throw new Error(data.error || 'resolve_failed');
      }
      state.mfpImportResult.unmatched = state.mfpImportResult.unmatched.filter(
        (item) => item.unmatched_key !== key,
      );
      renderImportResult(state.mfpImportResult);
      try {
        await loadTemplates();
        await loadPlan(state.selectedTemplateId);
      } catch (reloadErr) {
        console.error(reloadErr);
      }
      showSnackbar('Zuordnung gespeichert');
    } catch (err) {
      console.error(err);
      showSnackbar('Zuordnung fehlgeschlagen');
    } finally {
      button.disabled = false;
      button.textContent = originalText;
    }
  }

  function handleImportResolveClick(event) {
    const target = event.target.closest('[data-import-resolve]');
    if (!target) return;
    if (!state.mfpImportResult.importId) {
      showSnackbar('Zuordnung erst nach dem Import möglich.');
      return;
    }
    const key = target.dataset.unmatchedKey;
    const select = document.querySelector(`[data-unmatched-select="${key}"]`);
    const foodId = select?.value;
    if (!foodId) {
      showSnackbar('Bitte ein Food auswählen.');
      return;
    }
    resolveImportMapping(key, foodId, target);
  }

  async function saveTemplateTitle() {
    if (!state.selectedTemplateId) return;
    const current = state.weekTemplates.find((t) => t.id === state.selectedTemplateId);
    const title = (current?.title || state.plan?.week_template?.title || '').trim();
    if (!title) return;
    const res = await fetch('/api/nutrition/plan/template', {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ title, template_id: state.selectedTemplateId }),
    });
    const data = await res.json();
    if (!res.ok) {
      throw new Error(data.error || 'Template konnte nicht gespeichert werden.');
    }
    const nowIso = new Date().toISOString();
    state.lastSavedAt = Date.now();
    state.lastSavedAtOverride = state.lastSavedAt;
    state.weekTemplates = state.weekTemplates.map((tpl) =>
      tpl.id === state.selectedTemplateId ? { ...tpl, updated_at: nowIso } : tpl
    );
    updateVersionName();
    setDirty(false);
  }

  async function loadTemplates() {
    const qs = '?include_archived=1';
    const res = await fetch(`/api/nutrition/plan/templates${qs}`, { cache: 'no-store' });
    const data = await res.json();
    if (!res.ok || data.ok === false) {
      throw new Error(data.error || 'Templates konnten nicht geladen werden.');
    }
    state.weekTemplates = data.templates || [];
    const active = state.weekTemplates.find((t) => t.is_active) || null;
    state.activeTemplateId = active?.id || null;
    const initialSelection = active || state.weekTemplates[0] || null;
    if (initialSelection && !state.selectedTemplateId) {
      state.selectedTemplateId = initialSelection.id;
    }
    if (!state.activeSegment) state.activeSegment = 'active';
    renderTemplateMenu();
    updateVersionName();
    setDirty(state.dirty);
  }

  function renderTemplateMenu() {
    if (!els.versionPicker) return;
    const query = (els.versionSearch?.value || '').trim().toLowerCase();
    const templates = state.weekTemplates || [];
    const recent = [...templates].sort((a, b) => {
      const ta = a.last_used_at || a.updated_at || '';
      const tb = b.last_used_at || b.updated_at || '';
      return tb.localeCompare(ta);
    });
    const filtered = (list) => list.filter((t) => !query || (t.title || '').toLowerCase().includes(query));

    if (els.versionListRecent) els.versionListRecent.innerHTML = '';
    if (els.versionListAll) els.versionListAll.innerHTML = '';

    const segment = state.activeSegment;
    const activeList = templates.filter((t) => !t.archived_at);
    const archivedList = templates.filter((t) => t.archived_at);
    const mainList = segment === 'archived' ? archivedList : activeList;
    if (els.versionListTitle) els.versionListTitle.textContent = segment === 'archived' ? 'Archiv' : 'Alle';
    const recentSection = els.versionListRecent?.closest('.version-section');
    if (recentSection) recentSection.style.display = segment === 'archived' ? 'none' : '';

    const renderList = (list, container) => {
      if (!container) return;
      const items = filtered(list);
      if (items.length === 0) {
        container.innerHTML = '<div class="version-empty">Keine Versionen</div>';
        return;
      }
      items.forEach((tpl) => {
        const row = document.createElement('div');
        row.className = `version-item ${state.selectedTemplateId === tpl.id ? 'is-active' : ''}`;
        row.dataset.versionId = tpl.id;
        const meta = document.createElement('div');
        meta.className = 'version-meta';
        const title = document.createElement('div');
        title.className = 'version-title';
        title.textContent = tpl.title || `Version ${tpl.id}`;
        if (tpl.id === state.activeTemplateId) {
          title.textContent += ' · Aktiv';
        }
        const sub = document.createElement('div');
        sub.className = 'version-sub';
        const ts = tpl.updated_at ? Date.parse(tpl.updated_at) : null;
        sub.textContent = ts ? `gespeichert ${formatRelativeTime(ts)}` : '—';
        meta.append(title, sub);
        const actions = document.createElement('div');
        actions.className = 'version-actions';
        const menuBtn = document.createElement('button');
        menuBtn.type = 'button';
        menuBtn.className = 'btn-ghost';
        menuBtn.textContent = '⋯';
        menuBtn.addEventListener('click', (event) => {
          event.stopPropagation();
          openVersionContext(tpl.id, event.target);
        });
        actions.append(menuBtn);
        row.append(meta, actions);
        row.addEventListener('click', async () => {
          await switchTemplate(tpl.id);
          toggleVersionPicker(false);
        });
        container.appendChild(row);
      });
    };

    if (segment !== 'archived') {
      const recentActive = recent.filter((t) => !t.archived_at);
      renderList(recentActive.slice(0, 5), els.versionListRecent);
    }
    renderList(mainList, els.versionListAll);
  }

  function positionVersionPicker() {
    if (!els.versionPicker || !els.versionPill || els.versionPicker.hasAttribute('hidden')) return;
    const rect = els.versionPill.getBoundingClientRect();
    const pickerWidth = Math.min(460, Math.max(320, Math.floor(window.innerWidth * 0.92)));
    const gap = 8;
    const maxLeft = Math.max(8, window.innerWidth - pickerWidth - 8);
    const left = Math.min(rect.left, maxLeft);
    els.versionPicker.style.position = 'fixed';
    els.versionPicker.style.top = `${Math.min(rect.bottom + gap, Math.max(8, window.innerHeight - 24))}px`;
    els.versionPicker.style.left = `${Math.max(8, left)}px`;
    els.versionPicker.style.width = `${pickerWidth}px`;
    const maxHeight = Math.max(220, window.innerHeight - (rect.bottom + gap) - 16);
    els.versionPicker.style.maxHeight = `${maxHeight}px`;
    els.versionPicker.style.overflow = 'auto';
  }

  function toggleVersionPicker(force = null) {
    if (!els.versionPicker) return;
    const shouldOpen = force == null ? els.versionPicker.hasAttribute('hidden') : force;
    if (shouldOpen) els.versionPicker.removeAttribute('hidden');
    else els.versionPicker.setAttribute('hidden', 'true');
    if (els.versionPill) {
      els.versionPill.setAttribute('aria-expanded', shouldOpen ? 'true' : 'false');
    }
    if (shouldOpen) {
      positionVersionPicker();
      els.versionSearch?.focus();
      renderTemplateMenu();
    }
  }

  function resetSelectionsForTemplateSwitch() {
    state.selectedDay = 0;
    state.selectedSlotId = null;
    state.openEditorSlotId = null;
    state.selectedMealTemplateId = null;
    state.foodEditId = null;
    state.gapOpen = false;
    resetFoodForm();
    resetMealForm();
  }

  async function switchTemplate(templateId) {
    if (!templateId || templateId === state.selectedTemplateId) return;
    const target = state.weekTemplates.find((t) => t.id === templateId);
    const ok = await confirmDirtySwitch(target?.title || 'Version');
    if (!ok) return;
    resetSelectionsForTemplateSwitch();
    if (target?.archived_at) {
      await fetch(`/api/nutrition/plan/templates/${templateId}/restore`, { method: 'POST' });
    }
    state.selectedTemplateId = templateId;
    await loadTemplates();
    await loadPlan(templateId);
    setDirty(false);
  }

  function openVersionContext(templateId, anchorEl) {
    const menuId = 'version-context-menu';
    let menu = document.getElementById(menuId);
    if (!menu) {
      menu = document.createElement('div');
      menu.id = menuId;
      menu.className = 'version-overflow-menu version-context-menu';
      menu.setAttribute('hidden', 'true');
      document.body.appendChild(menu);
    }
    const isSameTarget = Number(menu.dataset.versionId || 0) === Number(templateId);
    const isOpen = !menu.hasAttribute('hidden');
    if (isOpen && isSameTarget) {
      closeContextMenu();
      return;
    }
    const tpl = state.weekTemplates.find((t) => t.id === templateId);
    if (tpl?.archived_at) {
      menu.innerHTML = `
        <button type="button" class="pill ghost${tpl.id === state.activeTemplateId ? ' is-disabled' : ''}" data-action="activate"${tpl.id === state.activeTemplateId ? ' disabled' : ''}>${tpl.id === state.activeTemplateId ? 'Bereits aktiv' : 'Aktivieren'}</button>
        <div class="version-menu-divider" aria-hidden="true"></div>
        <button type="button" class="pill ghost" data-action="target-settings">Ziel anpassen</button>
        <div class="version-menu-divider" aria-hidden="true"></div>
        <button type="button" class="pill ghost" data-action="restore">Wiederherstellen</button>
        <button type="button" class="pill ghost danger" data-action="delete">Löschen</button>
      `;
    } else {
      menu.innerHTML = `
        <button type="button" class="pill ghost${tpl.id === state.activeTemplateId ? ' is-disabled' : ''}" data-action="activate"${tpl.id === state.activeTemplateId ? ' disabled' : ''}>${tpl.id === state.activeTemplateId ? 'Bereits aktiv' : 'Aktivieren'}</button>
        <div class="version-menu-divider" aria-hidden="true"></div>
        <button type="button" class="pill ghost" data-action="target-settings">Ziel anpassen</button>
        <div class="version-menu-divider" aria-hidden="true"></div>
        <button type="button" class="pill ghost" data-action="rename">Umbenennen</button>
        <button type="button" class="pill ghost" data-action="duplicate">Duplizieren</button>
        <button type="button" class="pill ghost danger" data-action="archive">Archivieren</button>
        <button type="button" class="pill ghost danger" data-action="delete">Löschen</button>
      `;
    }
    ensureActionHandlers(menu);
    menu.dataset.versionId = String(templateId);
    state.selectedContextId = templateId;
    const rect = anchorEl.getBoundingClientRect();
    menu.style.position = 'fixed';
    menu.style.top = `${rect.bottom + 6}px`;
    menu.style.left = `${Math.min(rect.left, window.innerWidth - 220)}px`;
    menu.removeAttribute('hidden');
  }

  async function clearWeek() {
    if (state.isClearingWeek) return;
    const confirmed = await confirmClearWeek();
    if (!confirmed) return;
    state.isClearingWeek = true;
    const btn = els.clearWeekBtn;
    if (btn) {
      btn.setAttribute('disabled', 'true');
      btn.classList.add('is-loading');
    }
    setDirty(true);
    try {
      const payload = state.selectedTemplateId ? { template_id: state.selectedTemplateId } : {};
      const res = await fetch('/api/nutrition/plan/week/clear', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      const data = await res.json();
      if (!res.ok || data.ok === false) {
        throw new Error(data.error || 'Woche konnte nicht geleert werden.');
      }
      state.selectedSlotId = null;
      state.openEditorSlotId = null;
      await loadPlan();
      setDirty(false);
      showSnackbar('Woche geleert');
    } catch (err) {
      showError(err, 'Woche konnte nicht geleert werden.');
    } finally {
      state.isClearingWeek = false;
      if (btn) {
        btn.removeAttribute('disabled');
        btn.classList.remove('is-loading');
      }
    }
  }

  function setActiveSegment(segment) {
    state.activeSegment = segment;
    if (els.segmentActive) {
      const active = segment === 'active';
      els.segmentActive.classList.toggle('is-active', active);
      els.segmentActive.setAttribute('aria-selected', active ? 'true' : 'false');
    }
    if (els.segmentArchived) {
      const archived = segment === 'archived';
      els.segmentArchived.classList.toggle('is-active', archived);
      els.segmentArchived.setAttribute('aria-selected', archived ? 'true' : 'false');
    }
    renderTemplateMenu();
  }

  function closeOverflowMenu() {
    if (!els.overflowMenu) return;
    els.overflowMenu.setAttribute('hidden', 'true');
    els.overflowBtn?.setAttribute('aria-expanded', 'false');
  }

  function toggleOverflowMenu() {
    if (!els.overflowMenu) return;
    const isOpen = !els.overflowMenu.hasAttribute('hidden');
    if (isOpen) {
      closeOverflowMenu();
    } else {
      els.overflowMenu.removeAttribute('hidden');
      els.overflowBtn?.setAttribute('aria-expanded', 'true');
    }
  }

  function closeContextMenu() {
    const ctx = document.getElementById('version-context-menu');
    if (ctx) ctx.setAttribute('hidden', 'true');
  }

  function getActionTemplateId(actionEl) {
    const ctx = actionEl.closest('#version-context-menu');
    if (ctx) {
      const ctxId = Number(ctx.dataset.versionId);
      if (ctxId) return ctxId;
    }
    return state.selectedTemplateId;
  }

  const actionHandlers = {
    save: () => onSaveVersion(),
    activate: (templateId) => onActivateVersion(templateId),
    duplicate: (templateId) => onDuplicateVersion(templateId),
    new: () => onOpenNewVersionDialog(),
    'target-settings': () => openTemplateMacroModal(),
    'clear-week': () => clearWeek().catch((err) => console.error(err)),
    archive: (templateId) => onArchiveVersion(templateId),
    delete: (templateId) => onDeleteVersion(templateId),
    restore: (templateId) => onRestoreVersion(templateId),
    rename: (templateId) => {
      const current = state.weekTemplates.find((t) => t.id === templateId);
      if (els.renameInput) els.renameInput.value = current?.title || '';
      if (els.renameDialog) {
        els.renameDialog.removeAttribute('hidden');
        els.renameInput?.focus();
      }
    },
  };

  function ensureActionHandlers(container = document) {
    container.querySelectorAll('[data-action]').forEach((el) => {
      const action = el.getAttribute('data-action');
      if (!actionHandlers[action]) {
        console.warn(`Missing handler for action: ${action}`);
        el.setAttribute('title', 'Noch nicht verfügbar');
        el.setAttribute('disabled', 'true');
      }
    });
  }

  function bindEvents() {
    setFoodLabelMode('per100');
    els.mealSearch?.addEventListener('input', renderMeals);
    els.foodSearch?.addEventListener('input', renderFoods);
    els.templateDialogCancel?.addEventListener('click', closeTemplateDialog);
    els.templateDialog?.addEventListener('click', (event) => { if (event.target.closest('[data-template-dialog-close]')) closeTemplateDialog(); });
    els.templateDialogConfirm?.addEventListener('click', () => confirmTemplateDialog().catch((err) => showError(err)));
    window.addEventListener('pointerup', () => {
      if (state.isSelectingDayGroup && state.dayGroupDragged) {
        state.ignoreDayTabClick = true;
        state.selectedGroupDays = new Set(state.dayGroupDragDays || []);
        renderDayTabs();
        scheduleDayGroupLink();
      }
      state.isSelectingDayGroup = false;
      state.dayGroupDragged = false;
    });
    els.templateTargetEditBtn?.addEventListener('click', openTemplateMacroModal);
    els.macroModalTabs.forEach((btn) => {
      btn.addEventListener('click', () => {
        const mode = btn.dataset.mode;
        if (mode) setTemplateMacroModalMode(mode);
      });
    });
    els.macroModal?.addEventListener('click', (event) => {
      const target = event.target;
      if (target && target.dataset && target.dataset.close) {
        closeTemplateMacroModal();
      }
    });
    els.macroModalCancel?.addEventListener('click', closeTemplateMacroModal);
    els.macroModalSave?.addEventListener('click', () => saveTemplateMacroSettings().catch((err) => console.error(err)));
    els.macroModalTarget?.addEventListener('input', (event) => {
      setText(els.macroModalError, '');
      applyTemplateMacroTargetInput(event.target.value);
    });
    els.macroModalTarget?.addEventListener('blur', () => applyTemplateMacroTargetInput(els.macroModalTarget.value));
    els.macroModalProtein?.addEventListener('input', (event) => {
      setText(els.macroModalError, '');
      applyTemplateMacroMacroInput('protein', event.target.value);
    });
    els.macroModalFat?.addEventListener('input', (event) => {
      setText(els.macroModalError, '');
      applyTemplateMacroMacroInput('fat', event.target.value);
    });
    [els.macroModalProtein, els.macroModalFat].forEach((input) => {
      input?.addEventListener('blur', () => renderTemplateMacroBandPreview());
    });
    els.macroModalScale?.querySelectorAll('[data-marker]').forEach((marker) => {
      marker.addEventListener('pointerdown', (event) => {
        const markerKey = marker.dataset.marker;
        if (!markerKey) return;
        startTemplateMacroDrag(markerKey, event);
      });
    });
    window.addEventListener('pointermove', moveTemplateMacroDrag);
    window.addEventListener('pointerup', stopTemplateMacroDrag);
    window.addEventListener('pointercancel', stopTemplateMacroDrag);

    const mealPanel = root.querySelector('.nutrition-meals-card');
    const foodPanel = root.querySelector('.nutrition-foods-card');

    mealPanel?.addEventListener('click', (event) => {
      if (event.target.closest('.nutrition-list-card')) return;
      if (event.target.closest('.nutrition-builder')) return;
      if (event.target.closest('#meal-search')) return;
      resetMealForm();
    });

    foodPanel?.addEventListener('click', (event) => {
      if (event.target.closest('.nutrition-list-card')) return;
      if (event.target.closest('.nutrition-builder')) return;
      if (event.target.closest('#food-search')) return;
      resetFoodForm();
      renderFoods();
    });

    els.dayContent?.addEventListener('click', (event) => {
      if (event.target.closest('input, select, textarea, button, a')) return;
      if (event.target.closest('.nutrition-meal-editor')) return;
      if (event.target.closest('.meal-card-body')) return;
      const card = event.target.closest('.nutrition-slot-card');
      if (!card) return;
      const slotId = Number(card.dataset.slotId || 0);
      if (!slotId) return;
      const slot = getSlotById(slotId);
      if (!slot) return;
      document.querySelectorAll('.nutrition-slot-card.is-selected').forEach((el) => el.classList.remove('is-selected'));
      card.classList.add('is-selected');
      state.selectedSlotId = slotId;
    });

    root.addEventListener('keydown', (event) => {
      if (event.key !== 'Escape') return;
      if (event.target && event.target.closest('input, textarea, select, [contenteditable="true"]')) {
        event.target.blur();
      }
      resetMealForm();
      resetFoodForm();
      renderFoods();
    });

    els.mealBuilderForm?.addEventListener('submit', async (event) => {
      event.preventDefault();
      if (state.isSavingMeal) return;
      const formData = new FormData(els.mealBuilderForm);
      const ingredients = [];
      els.ingredientTray?.querySelectorAll('.ingredient-row').forEach((row) => {
        const foodId = Number(row.querySelector('select')?.value);
        const amountValue = Number(row.querySelector('.ingredient-amount')?.value);
        const unitValue = row.querySelector('.ingredient-unit')?.value || 'g';
        if (foodId && amountValue > 0) {
          ingredients.push({ food_id: foodId, amount: amountValue, unit: unitValue });
        }
      });
      const payload = {
        title: formData.get('title'),
        default_servings: Number(formData.get('default_servings')) || 1,
        ingredients,
      };
      state.isSavingMeal = true;
      const btn = els.mealSaveBtn;
      if (btn) {
        btn.setAttribute('disabled', 'true');
        btn.classList.add('is-loading');
      }
      try {
        const mealTemplate = await sendMealBuilder(payload, state.selectedMealTemplateId);
        if (mealTemplate?.id) state.selectedMealTemplateId = mealTemplate.id;
        await loadPlan();
        renderMeals();
      } catch (err) {
        console.error(err);
        showError(err, 'Meal konnte nicht gespeichert werden.');
      } finally {
        state.isSavingMeal = false;
        if (btn) {
          btn.removeAttribute('disabled');
          btn.classList.remove('is-loading');
        }
      }
    });
    els.mealAddToDayBtn?.addEventListener('click', async () => {
      if (state.isAddingMealToDay) return;
      state.isAddingMealToDay = true;
      const btn = els.mealAddToDayBtn;
      if (btn) {
        btn.setAttribute('disabled', 'true');
        btn.classList.add('is-loading');
      }
      try {
        let mealTemplateId = state.selectedMealTemplateId;
        if (!mealTemplateId) {
          const formData = new FormData(els.mealBuilderForm);
          const ingredients = [];
          els.ingredientTray?.querySelectorAll('.ingredient-row').forEach((row) => {
            const foodId = Number(row.querySelector('select')?.value);
            const amountValue = Number(row.querySelector('.ingredient-amount')?.value);
            const unitValue = row.querySelector('.ingredient-unit')?.value || 'g';
            if (foodId && amountValue > 0) {
              ingredients.push({ food_id: foodId, amount: amountValue, unit: unitValue });
            }
          });
          const payload = {
            title: formData.get('title'),
            default_servings: Number(formData.get('default_servings')) || 1,
            ingredients,
          };
          const mealTemplate = await sendMealBuilder(payload, null);
          mealTemplateId = mealTemplate?.id || null;
          state.selectedMealTemplateId = mealTemplateId;
        }
        if (!mealTemplateId) return;
        const meal = state.mealTemplates.find((m) => m.id === mealTemplateId) || { id: mealTemplateId };
        await insertMealToSelectedSlot(meal);
      } catch (err) {
        console.error(err);
        showError(err, 'Meal konnte nicht hinzugefügt werden.');
      } finally {
        state.isAddingMealToDay = false;
        if (btn) {
          btn.removeAttribute('disabled');
          btn.classList.remove('is-loading');
        }
      }
    });
    els.mealDeleteBtn?.addEventListener('click', async () => {
      const btn = els.mealDeleteBtn;
      if (btn?.hasAttribute('disabled')) return;
      if (btn) {
        btn.setAttribute('disabled', 'true');
        btn.classList.add('is-loading');
      }
      try {
        await deleteSelectedMealTemplate();
      } catch (err) {
        console.error(err);
        showError(err, 'Mahlzeit konnte nicht gelöscht werden.');
      } finally {
        if (btn && state.selectedMealTemplateId) {
          btn.removeAttribute('disabled');
        }
        btn?.classList.remove('is-loading');
      }
    });

    els.addIngredientBtn?.addEventListener('click', () => addIngredientRow());

    els.foodForm?.addEventListener('submit', async (event) => {
      event.preventDefault();
      if (state.isSavingFood) return;
      const formData = new FormData(els.foodForm);
      const payload = {
        name: formData.get('name'),
        unit_default: formData.get('unit_default') || 'g',
        kcal_per_100: Number(formData.get('kcal_per_100')) || 0,
        p_per_100: Number(formData.get('p_per_100')) || 0,
        c_per_100: Number(formData.get('c_per_100')) || 0,
        sugar_per_100: Number(formData.get('sugar_per_100')) || 0,
        f_per_100: Number(formData.get('f_per_100')) || 0,
      };
      state.isSavingFood = true;
      const btn = els.foodSubmitBtn;
      if (btn) {
        btn.setAttribute('disabled', 'true');
        btn.classList.add('is-loading');
      }
      try {
        if (state.foodEditId) {
          await updateFood(state.foodEditId, payload);
        } else {
          await sendFoodForm(payload);
        }
      } catch (err) {
        console.error(err);
        showError(err, 'Food konnte nicht gespeichert werden.');
      } finally {
        state.isSavingFood = false;
        if (btn) {
          btn.removeAttribute('disabled');
          btn.classList.remove('is-loading');
        }
      }
    });
    els.foodForm?.querySelector('[name="unit_default"]')?.addEventListener('change', (event) => {
      applyFoodUnitMode(event.target.value || 'g');
    });
    els.foodDeleteBtn?.addEventListener('click', async () => {
      const btn = els.foodDeleteBtn;
      if (btn?.hasAttribute('disabled')) return;
      if (btn) {
        btn.setAttribute('disabled', 'true');
        btn.classList.add('is-loading');
      }
      try {
        await deleteSelectedFood();
      } catch (err) {
        console.error(err);
        showError(err, 'Food konnte nicht gelöscht werden.');
      } finally {
        if (btn && state.foodEditId) {
          btn.removeAttribute('disabled');
        }
        btn?.classList.remove('is-loading');
      }
    });
    els.foodAddToDayBtn?.addEventListener('click', async () => {
      if (state.isAddingFoodToDay) return;
      state.isAddingFoodToDay = true;
      const btn = els.foodAddToDayBtn;
      if (btn) {
        btn.setAttribute('disabled', 'true');
        btn.classList.add('is-loading');
      }
      try {
        await addSelectedFoodToDay();
      } catch (err) {
        console.error(err);
        showError(err, 'Food konnte nicht hinzugefügt werden.');
      } finally {
        state.isAddingFoodToDay = false;
        if (btn) {
          btn.removeAttribute('disabled');
          btn.classList.remove('is-loading');
        }
      }
    });

    els.clearWeekBtn?.addEventListener('click', () => clearWeek().catch((err) => console.error(err)));
    els.checklistBtn?.addEventListener('click', () => fetchChecklist().catch((err) => console.error(err)));
    els.shoppingBtn?.addEventListener('click', () => fetchShoppingList().catch((err) => console.error(err)));
    els.copyShoppingBtn?.addEventListener('click', () => copyText(els.shoppingOutput.textContent, els.copyShoppingBtn));
    els.copyChecklistBtn?.addEventListener('click', () => copyText(els.checklistOutput.textContent, els.copyChecklistBtn));
    els.copyMealCollection?.addEventListener('click', copyMealCollectionList);
    els.deleteMealCollection?.addEventListener('click', () => deleteAllMealTemplates().catch((err) => {
      console.error(err);
      showError(err, 'Meal Templates konnten nicht gelöscht werden.');
    }));
    els.copyFoodCollection?.addEventListener('click', copyFoodCollectionList);
    // Makro-Übersicht ist read-only; keine eigenen Day-Switches.
    Array.from(els.mfpToggleButtons || []).forEach((btn) => {
      btn.addEventListener('click', () => toggleMfpDrawer(btn.dataset.mfpToggle));
    });
    Array.from(els.mfpModeButtons || []).forEach((btn) => {
      btn.addEventListener('click', () => setMfpBridgeMode(btn.dataset.mfpMode));
    });
    els.mfpImportBtn?.addEventListener('click', () => submitMealplanImport().catch((err) => console.error(err)));
    els.mfpValidateBtn?.addEventListener('click', () => validateMealplanImport().catch((err) => console.error(err)));
    els.mfpImportMode?.addEventListener('change', toggleImportModeDetails);
    els.mfpScopeSlots?.addEventListener('change', updateImportScopeState);
    els.mfpScopeTemplates?.addEventListener('change', updateImportScopeState);
    els.mfpImportUnmatched?.addEventListener('click', handleImportResolveClick);
    els.mfpImportInput?.addEventListener('input', clearImportInlineError);
    updateImportScopeState();
    els.versionPill?.addEventListener('click', (event) => {
      event.preventDefault();
      toggleVersionPicker();
    });
    els.versionPill?.addEventListener('keydown', (event) => {
      if (event.key === 'Enter' || event.key === ' ') {
        event.preventDefault();
        toggleVersionPicker();
      }
    });

    els.versionSearch?.addEventListener('input', renderTemplateMenu);
    els.segmentActive?.addEventListener('click', () => setActiveSegment('active'));
    els.segmentArchived?.addEventListener('click', () => setActiveSegment('archived'));

    els.overflowBtn?.addEventListener('click', (event) => {
      event.preventDefault();
      toggleOverflowMenu();
    });

    document.addEventListener('click', (event) => {
      if (els.versionPicker) {
        if (!event.target.closest('#version-picker') && !event.target.closest('#version-pill')) {
          toggleVersionPicker(false);
        }
      }
      if (els.overflowMenu) {
        if (!event.target.closest('#overflow-menu') && !event.target.closest('#btn-overflow')) {
          closeOverflowMenu();
        }
      }
      const ctx = document.getElementById('version-context-menu');
      if (ctx && !event.target.closest('#version-context-menu')) {
        closeContextMenu();
      }
    });

    document.addEventListener('keydown', (event) => {
      if (event.key === 'Escape') {
        closeTemplateMacroModal();
        toggleVersionPicker(false);
        closeOverflowMenu();
        closeContextMenu();
        if (els.createDialog && !els.createDialog.hasAttribute('hidden')) {
          els.createDialog.setAttribute('hidden', 'true');
        }
        if (els.renameDialog && !els.renameDialog.hasAttribute('hidden')) {
          els.renameDialog.setAttribute('hidden', 'true');
        }
      }
    });

    window.addEventListener('resize', () => positionVersionPicker());
    window.addEventListener('scroll', () => positionVersionPicker(), true);

    document.addEventListener('keydown', (event) => {
      const isCmdK = (event.key.toLowerCase() === 'k') && (event.metaKey || event.ctrlKey);
      if (isCmdK) {
        event.preventDefault();
        toggleVersionPicker(true);
        els.versionSearch?.focus();
      }
    });

    setInterval(() => {
      if (!state.dirty) renderVersionStatus();
    }, 60000);

    document.addEventListener('click', (event) => {
      const actionEl = event.target.closest('[data-action]');
      if (!actionEl) return;
      if (actionEl.hasAttribute('disabled')) return;
      const action = actionEl.getAttribute('data-action');
      const handler = actionHandlers[action];
      if (!handler) {
        console.warn(`Missing handler for action: ${action}`);
        actionEl.setAttribute('title', 'Noch nicht verfügbar');
        actionEl.setAttribute('disabled', 'true');
        return;
      }
      const templateId = getActionTemplateId(actionEl);
      handler(templateId, actionEl);
      closeOverflowMenu();
      if (actionEl.closest('#version-context-menu')) {
        closeContextMenu();
      }
    });

    els.overflowClearWeek?.setAttribute('data-action', 'clear-week');
    els.overflowArchive?.setAttribute('data-action', 'archive');
    els.overflowDelete?.setAttribute('data-action', 'delete');

    els.createCancel?.addEventListener('click', () => {
      els.createDialog?.setAttribute('hidden', 'true');
    });
    els.createEmpty?.addEventListener('click', () => onCreateVersion('empty', els.createEmpty));
    els.createCopy?.addEventListener('click', () => onCreateVersion('copy', els.createCopy));
    els.renameCancel?.addEventListener('click', () => els.renameDialog?.setAttribute('hidden', 'true'));
    els.renameSave?.addEventListener('click', async () => {
      const title = (els.renameInput?.value || '').trim();
      const targetId = state.selectedContextId || state.selectedTemplateId;
      if (!title || !targetId) return;
      els.renameSave.setAttribute('disabled', 'true');
      try {
        await fetch('/api/nutrition/plan/template', {
          method: 'PATCH',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ title, template_id: targetId }),
        });
        await loadTemplates();
        await loadPlan(state.selectedTemplateId);
        showSnackbar('Version umbenannt');
        els.renameDialog?.setAttribute('hidden', 'true');
      } catch (err) {
        console.error(err);
        showSnackbar('Umbenennen fehlgeschlagen');
      } finally {
        els.renameSave.removeAttribute('disabled');
      }
    });

    ensureActionHandlers();
    toggleImportModeDetails();
    updateMfpDrawerVisibility('shopping');
    updateMfpDrawerVisibility('checklist');
    renderMfpBridgeMode();
  }

  bindEvents();
  loadTemplates()
    .then(() => loadPlan(state.selectedTemplateId))
    .catch((err) => console.error(err));
})();
