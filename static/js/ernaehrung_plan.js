(() => {
  const root = document.querySelector('[data-nutrition-plan]');
  if (!root) return;

  const els = {
    slotList: document.getElementById('slot-template-list'),
    slotForm: document.getElementById('slot-template-form'),
    mealList: document.getElementById('meal-template-list'),
    mealSearch: document.getElementById('meal-search'),
    mealBuilderForm: document.getElementById('meal-builder-form'),
    ingredientTray: document.getElementById('ingredient-tray'),
    addIngredientBtn: document.getElementById('add-ingredient-btn'),
    foodList: document.getElementById('food-list'),
    foodSearch: document.getElementById('food-search'),
    foodForm: document.getElementById('food-form'),
    planGrid: document.getElementById('nutrition-plan-grid'),
    weekdayRibbon: document.getElementById('weekday-ribbon'),
    dayLinkSelection: document.getElementById('day-link-selection'),
    dayGroupActions: document.getElementById('day-group-actions'),
    linkSelectedDays: document.getElementById('link-selected-days'),
    clearDaySelection: document.getElementById('clear-day-selection'),
    dayGroupFocus: document.getElementById('day-group-focus'),
    weekSummary: document.getElementById('week-summary'),
    gapNotes: document.getElementById('gap-notes'),
    applyBtn: document.getElementById('apply-week-btn'),
    shoppingBtn: document.getElementById('generate-shopping-btn'),
    checklistBtn: document.getElementById('export-checklist-btn'),
    shoppingOutput: document.getElementById('shopping-output'),
    checklistOutput: document.getElementById('checklist-output'),
    copyShoppingBtn: document.getElementById('copy-shopping-btn'),
    copyChecklistBtn: document.getElementById('copy-checklist-btn'),
  };

  const state = {
    plan: null,
    slot_templates: [],
    meal_templates: [],
    foods: [],
    selectedWeekdays: new Set(),
    focusedGroupId: null,
    dragSelecting: false,
  };

  const GROUP_COLORS = ['#f6b93b', '#73d0c3', '#9aa8ff', '#f38ba8', '#c9a7ff', '#80c56a'];

  function formatMacros(meal, servings = 1) {
    if (!meal) return '';
    const factor = Number(servings) || 1;
    const kcal = (Number(meal.kcal_per_serving) || 0) * factor;
    const p = (Number(meal.p_per_serving) || 0) * factor;
    const c = (Number(meal.c_per_serving) || 0) * factor;
    const f = (Number(meal.f_per_serving) || 0) * factor;
    return `kcal ${kcal.toFixed(0)} | P ${p.toFixed(0)} | C ${c.toFixed(0)} | F ${f.toFixed(0)}`;
  }

  function copyText(text, btn) {
    if (!text) return;
    navigator.clipboard?.writeText(text).then(() => {
      const original = btn.textContent;
      btn.textContent = 'Copied!';
      setTimeout(() => (btn.textContent = original), 1200);
    });
  }

  function renderSlots() {
    if (!els.slotList) return;
    els.slotList.innerHTML = '';
    state.slot_templates.forEach((slot) => {
      const card = document.createElement('div');
      card.className = 'slot-card';
      card.innerHTML = `
        <div class="slot-card-header">
          <strong>${slot.title}</strong>
          <span class="slot-meta">${slot.context_tag || ''}</span>
        </div>
        <div class="slot-meta">${slot.macro_intent || slot.default_time || ''}</div>
        <div class="slot-meta">Options: ${slot.options_count || 0}</div>
      `;
      els.slotList.appendChild(card);
    });
  }

  function renderMeals() {
    if (!els.mealList) return;
    const filter = (els.mealSearch?.value || '').trim().toLowerCase();
    els.mealList.innerHTML = '';
    const meals = state.meal_templates.filter((meal) => !filter || meal.title.toLowerCase().includes(filter));
    meals.forEach((meal) => {
      const card = document.createElement('div');
      card.className = 'meal-card';
      card.innerHTML = `
        <div>
          <div class="meal-card-header">
            <strong class="meal-card-title">${meal.title}</strong>
          </div>
          <div class="meal-meta">${meal.tags || ''}</div>
          <div class="meal-meta">${formatMacros(meal, meal.default_servings)}</div>
          <div class="meal-meta">MFP: ${meal.mfp_alias || '—'}</div>
        </div>
        <button type="button" data-copy="${meal.export_text || ''}">Copy</button>
      `;
      const copyBtn = card.querySelector('button');
      copyBtn?.addEventListener('click', (evt) => {
        copyText(evt.target.getAttribute('data-copy') || '', copyBtn);
      });
      els.mealList.appendChild(card);
    });
  }

  function renderFoods() {
    if (!els.foodList) return;
    const filter = (els.foodSearch?.value || '').trim().toLowerCase();
    els.foodList.innerHTML = '';
    state.foods
      .filter((food) => !filter || food.name.toLowerCase().includes(filter))
      .forEach((food) => {
        const card = document.createElement('div');
        card.className = 'food-card';
        card.innerHTML = `
          <div>
            <strong>${food.name}</strong>
            <small>${food.unit_default || 'g'}</small>
          </div>
          <span>${Math.round(food.kcal_per_100 || 0)} kcal</span>
        `;
        els.foodList.appendChild(card);
      });
  }

  function renderPlan() {
    if (!els.planGrid) return;
    const plan = state.plan;
    if (!plan?.week_template) {
      els.planGrid.innerHTML = '<div class="day-column">Plan laden…</div>';
      return;
    }
    els.planGrid.innerHTML = '';
    plan.week_template.days.forEach((day) => {
      const column = document.createElement('div');
      column.className = 'day-column';
      const group = groupForWeekday(day.weekday);
      if (group) {
        column.classList.add('is-linked-day');
        column.style.setProperty('--day-group-color', group.color);
      }
      if (state.focusedGroupId && group?.id === state.focusedGroupId) column.classList.add('is-group-focus');
      const header = document.createElement('div');
      header.className = 'day-header';
      const headerLeft = document.createElement('div');
      headerLeft.innerHTML = `<strong class="day-label">${day.label}</strong><span class="day-summary">${day.day_type}</span>`;
      const headerRight = document.createElement('div');
      headerRight.className = 'day-summary';
      headerRight.textContent = `${day.planned.kcal}/${day.target.kcal} kcal`;
      header.append(headerLeft, headerRight);
      column.appendChild(header);
      (day.slots || []).forEach((slot) => {
        const card = document.createElement('article');
        card.className = 'slot-card';
        const title = document.createElement('div');
        title.className = 'slot-card-header';
        title.innerHTML = `<span class="slot-title">${slot.slot_template?.title || 'Slot'}</span><span class="slot-meta">${slot.slot_template?.default_time || ''}</span>`;
        card.appendChild(title);
        const activeDiv = document.createElement('div');
        activeDiv.className = 'slot-active';
        if (slot.active_option?.meal_template) {
          const active = slot.active_option.meal_template;
          activeDiv.innerHTML = `
            <strong class="meal-name">${active.title}</strong>
            <span class="meal-macros">${formatMacros(active, slot.active_option.default_servings)}</span>
            <span class="slot-meta">MFP: ${active.mfp_alias || '—'}</span>
          `;
        } else {
          activeDiv.innerHTML = '<span class="meal-name">Noch keine Option</span>';
        }
        card.appendChild(activeDiv);
        if (slot.options?.length) {
          const optionsList = document.createElement('div');
          optionsList.className = 'slot-options';
          slot.options.forEach((option) => {
            const optionBtn = document.createElement('button');
            optionBtn.type = 'button';
            optionBtn.className = 'option-chip';
            if (option.id === slot.active_option_id) {
              optionBtn.classList.add('is-active');
            }
            optionBtn.textContent = option.meal_template.title;
            optionBtn.addEventListener('click', () => setActiveOption(slot.id, option.id));
            optionsList.appendChild(optionBtn);
          });
          card.appendChild(optionsList);
        }
        const controls = document.createElement('div');
        controls.className = 'slot-controls';
        const rotateBtn = document.createElement('button');
        rotateBtn.type = 'button';
        rotateBtn.className = 'ghost';
        rotateBtn.textContent = 'Option rotieren';
        rotateBtn.addEventListener('click', () => rotateSlotOption(slot));
        const lockBtn = document.createElement('button');
        lockBtn.type = 'button';
        lockBtn.className = 'ghost';
        lockBtn.textContent = slot.locked ? 'Entsperren' : 'Fixieren';
        lockBtn.addEventListener('click', () => toggleSlotLock(slot));
        controls.append(rotateBtn, lockBtn);
        card.appendChild(controls);
        column.appendChild(card);
      });
      els.planGrid.appendChild(column);
    });
  }

  function groups() { return state.plan?.day_groups || []; }
  function groupForWeekday(weekday) { return groups().find((group) => group.weekdays?.includes(weekday)); }

  function weekdayLabel(day) { return state.plan?.week_template?.days?.find((entry) => entry.weekday === day)?.label || ['Mo', 'Di', 'Mi', 'Do', 'Fr', 'Sa', 'So'][day]; }

  function renderWeekdayRibbon() {
    if (!els.weekdayRibbon || !state.plan?.week_template) return;
    els.weekdayRibbon.innerHTML = '';
    state.plan.week_template.days.forEach((day) => {
      const group = groupForWeekday(day.weekday);
      const chip = document.createElement('button');
      chip.type = 'button'; chip.className = 'weekday-chip'; chip.dataset.weekday = day.weekday;
      chip.setAttribute('aria-pressed', state.selectedWeekdays.has(day.weekday) ? 'true' : 'false');
      if (state.selectedWeekdays.has(day.weekday)) chip.classList.add('is-selected');
      if (group) { chip.classList.add('is-linked'); chip.style.setProperty('--group-color', group.color); }
      chip.innerHTML = `<span>${day.label}</span>${group ? `<small>${group.name}</small>` : '<small>eigenständig</small>'}`;
      chip.addEventListener('click', (event) => {
        if (event.shiftKey && state.selectedWeekdays.size) {
          const start = Math.min(...state.selectedWeekdays); const end = day.weekday;
          for (let i = Math.min(start, end); i <= Math.max(start, end); i += 1) state.selectedWeekdays.add(i);
        } else if (group && !event.metaKey && !event.ctrlKey) {
          state.focusedGroupId = group.id;
          state.selectedWeekdays = new Set(group.weekdays);
        } else {
          state.selectedWeekdays.has(day.weekday) ? state.selectedWeekdays.delete(day.weekday) : state.selectedWeekdays.add(day.weekday);
          state.focusedGroupId = null;
        }
        renderWeekdayRibbon(); renderDayGroupFocus(); renderPlan();
      });
      chip.addEventListener('pointerdown', () => { state.dragSelecting = true; state.selectedWeekdays.add(day.weekday); renderWeekdayRibbon(); });
      chip.addEventListener('pointerenter', () => { if (state.dragSelecting) { state.selectedWeekdays.add(day.weekday); renderWeekdayRibbon(); } });
      els.weekdayRibbon.appendChild(chip);
    });
    const count = state.selectedWeekdays.size;
    els.dayLinkSelection.textContent = count ? `${count} ${count === 1 ? 'Tag' : 'Tage'} ausgewählt` : 'Tage auswählen';
    els.dayGroupActions.hidden = count < 2;
  }

  async function linkSelectedDays() {
    const weekdays = [...state.selectedWeekdays].sort((a, b) => a - b);
    if (weekdays.length < 2) return;
    const source = weekdays[0];
    const defaultName = weekdays.length === 5 && weekdays.join(',') === '0,1,2,3,4' ? 'Arbeitstag' : `${weekdayLabel(weekdays[0])}–${weekdayLabel(weekdays.at(-1))}`;
    const name = window.prompt(`Name für ${weekdays.map(weekdayLabel).join(', ')}:`, defaultName);
    if (name === null) return;
    const res = await fetch('/api/nutrition/plan/day-groups', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ template_id: state.plan.week_template.id, weekdays, source_weekday: source, name, color: GROUP_COLORS[groups().length % GROUP_COLORS.length] }),
    });
    const payload = await res.json();
    if (!res.ok || !payload.ok) throw new Error(payload.error || 'Tage konnten nicht verbunden werden.');
    state.focusedGroupId = payload.day_group.id;
    await loadPlan();
  }

  async function detachFocusedGroup() {
    const group = groups().find((entry) => entry.id === state.focusedGroupId);
    if (!group || !window.confirm(`${group.name} auflösen? Jeder Tag bleibt als eigene Kopie erhalten.`)) return;
    const res = await fetch(`/api/nutrition/plan/day-groups/${encodeURIComponent(group.id)}/detach`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ template_id: state.plan.week_template.id, weekdays: group.weekdays }),
    });
    const payload = await res.json();
    if (!res.ok || !payload.ok) throw new Error(payload.error || 'Gruppe konnte nicht aufgelöst werden.');
    state.focusedGroupId = null; state.selectedWeekdays.clear(); await loadPlan();
  }

  async function updateFocusedAmount(group, sourceDay, slot, item, input) {
    const amount = Number(input.value);
    if (!Number.isFinite(amount) || amount < 0) return;
    const res = await fetch(`/api/nutrition/plan/day-groups/${encodeURIComponent(group.id)}/items/amount`, {
      method: 'PATCH', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ template_id: state.plan.week_template.id, slot_index: slot.slot_index, time_text: slot.time_text, food_id: item.food_id, amount, unit: item.unit || 'g' }),
    });
    const payload = await res.json();
    if (!res.ok || !payload.ok) { input.setCustomValidity(payload.error || 'Menge konnte nicht gespeichert werden.'); input.reportValidity(); return; }
    await loadPlan();
  }

  function renderDayGroupFocus() {
    if (!els.dayGroupFocus) return;
    const group = groups().find((entry) => entry.id === state.focusedGroupId);
    if (!group) { els.dayGroupFocus.innerHTML = '<p class="day-group-hint">Verbinde gleiche Tage einmal – anschließend bearbeitest du sie nur noch hier.</p>'; return; }
    const day = state.plan.week_template.days.find((entry) => entry.weekday === group.source_weekday);
    if (!day) return;
    els.dayGroupFocus.innerHTML = '';
    const header = document.createElement('div'); header.className = 'day-group-focus-head';
    header.innerHTML = `<div><span class="group-dot" style="background:${group.color}"></span><strong>${group.name}</strong><small>Gilt für ${group.weekdays.map(weekdayLabel).join(', ')}</small></div>`;
    const detach = document.createElement('button'); detach.type = 'button'; detach.className = 'ghost'; detach.textContent = 'Gruppe auflösen'; detach.addEventListener('click', () => detachFocusedGroup().catch(console.error)); header.appendChild(detach); els.dayGroupFocus.appendChild(header);
    (day.slots || []).forEach((slot) => {
      const items = (slot.items || []).filter((item) => item.food_id);
      if (!items.length) return;
      const block = document.createElement('div'); block.className = 'group-slot-editor';
      block.innerHTML = `<strong>${slot.display_title || slot.meal_title || slot.slot_template?.title || 'Meal'}</strong><small>${slot.time_text || ''} · globale Mengen</small>`;
      items.forEach((item) => {
        const row = document.createElement('label'); row.className = 'group-item-row';
        const input = document.createElement('input'); input.type = 'number'; input.min = '0'; input.step = '0.1'; input.value = item.amount ?? ''; input.setAttribute('aria-label', `${item.name} Menge`);
        input.addEventListener('change', () => updateFocusedAmount(group, day, slot, item, input).catch(console.error));
        row.append(document.createTextNode(item.name), input, document.createTextNode(item.unit || 'g')); block.appendChild(row);
      });
      els.dayGroupFocus.appendChild(block);
    });
  }

  function renderSummary() {
    if (!els.weekSummary || !state.plan) return;
    const summary = state.plan.week_summary || {};
    const entries = [
      { label: 'Planned', value: summary.planned },
      { label: 'Targets', value: summary.targets },
      { label: 'Remaining', value: summary.remaining },
    ];
    els.weekSummary.innerHTML = '';
    entries.forEach((entry) => {
      const card = document.createElement('div');
      card.className = 'summary-card';
      card.innerHTML = `
        <span>${entry.label}</span>
        <span class="value">${entry.value?.kcal || 0} kcal</span>
        <span class="summary-inline">P ${entry.value?.p || 0} | C ${entry.value?.c || 0} | F ${entry.value?.f || 0}</span>
      `;
      els.weekSummary.appendChild(card);
    });
  }

  function renderGapNotes() {
    if (!els.gapNotes || !state.plan) return;
    els.gapNotes.innerHTML = '';
    (state.plan.gap_notes || []).forEach((note) => {
      const span = document.createElement('span');
      span.textContent = note;
      els.gapNotes.appendChild(span);
    });
  }

  async function fetchPlan() {
    const res = await fetch('/api/nutrition/plan/week', { cache: 'no-store' });
    const data = await res.json();
    if (!res.ok || data.ok === false) {
      throw new Error(data.error || 'Plan konnte nicht geladen werden.');
    }
    return data;
  }

  async function loadPlan() {
    try {
      const plan = await fetchPlan();
      state.plan = plan;
      state.slot_templates = plan.slot_templates || [];
      state.meal_templates = plan.meal_templates || [];
      state.foods = plan.foods || [];
      renderSlots();
      renderMeals();
      renderFoods();
      renderPlan();
      renderWeekdayRibbon();
      renderDayGroupFocus();
      renderSummary();
      renderGapNotes();
      resetIngredientRows();
    } catch (err) {
      console.error(err);
    }
  }

  async function patchSlot(slotId, data) {
    const res = await fetch(`/api/nutrition/plan/slots/${slotId}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(data),
    });
    const payload = await res.json();
    if (!res.ok) {
      throw new Error(payload.error || 'Slot konnte nicht aktualisiert werden.');
    }
    return payload.slot;
  }

  async function setActiveOption(slotId, optionId) {
    await patchSlot(slotId, { active_option_id: optionId });
    await loadPlan();
  }

  async function rotateSlotOption(slot) {
    if (!slot?.options?.length) return;
    const options = slot.options;
    const idx = options.findIndex((opt) => opt.id === slot.active_option_id);
    const next = options[(idx + 1) % options.length];
    if (next) {
      await setActiveOption(slot.id, next.id);
    }
  }

  async function toggleSlotLock(slot) {
    await patchSlot(slot.id, { locked: !slot.locked });
    await loadPlan();
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
  }

  async function sendMealBuilder(payload) {
    const res = await fetch('/api/nutrition/meal_templates', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    const data = await res.json();
    if (!res.ok) {
      throw new Error(data.error || 'Meal konnte nicht gespeichert werden.');
    }
    await loadPlan();
  }

  async function sendSlotTemplate(payload) {
    const res = await fetch('/api/nutrition/slots/templates', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    const data = await res.json();
    if (!res.ok) {
      throw new Error(data.error || 'Slot Template konnte nicht gespeichert werden.');
    }
    await loadPlan();
  }

  function populateMealBuilderFoods() {
    if (!els.ingredientTray) return;
    els.ingredientTray.innerHTML = '';
    addIngredientRow();
  }

  function addIngredientRow() {
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
      option.textContent = `${food.name} (${food.unit_default || 'g'})`;
      select.appendChild(option);
    });
    const amount = document.createElement('input');
    amount.type = 'number';
    amount.step = '0.1';
    amount.min = '0';
    amount.className = 'ingredient-amount';
    amount.placeholder = 'g/ml/pieces';
    const unit = document.createElement('input');
    unit.type = 'text';
    unit.className = 'ingredient-unit';
    unit.placeholder = 'Einheit';
    const remove = document.createElement('button');
    remove.type = 'button';
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

  async function fetchShoppingList() {
    const res = await fetch('/api/nutrition/export/shopping_list', { cache: 'no-store' });
    const data = await res.json();
    if (!res.ok || data.ok === false) {
      throw new Error(data.error || 'Einkaufsliste konnte nicht geladen werden.');
    }
    const text = data.shopping_list
      .map((item) => item.display_text || `${Math.round(item.total_grams)} g ${item.name}`)
      .join('\n');
    els.shoppingOutput.textContent = text;
  }

  async function fetchChecklist() {
    const res = await fetch('/api/nutrition/export/checklist', { cache: 'no-store' });
    const data = await res.json();
    if (!res.ok || data.ok === false) {
      throw new Error(data.error || 'Checklist konnte nicht geladen werden.');
    }
    const lines = [];
    (data.checklist || []).forEach((day) => {
      lines.push(`## ${day.label}`);
      day.slots.forEach((slot) => {
        lines.push(`- ${slot.slot_title}: ${slot.meal_title} (${slot.servings}x)`);
      });
      lines.push('');
    });
    els.checklistOutput.textContent = lines.join('\n');
  }

  function bindEvents() {
    els.mealSearch?.addEventListener('input', renderMeals);
    els.foodSearch?.addEventListener('input', renderFoods);

    els.slotForm?.addEventListener('submit', (event) => {
      event.preventDefault();
      const formData = new FormData(els.slotForm);
      const payload = {
        title: formData.get('title'),
        default_time: formData.get('default_time'),
        context_tag: formData.get('context_tag'),
        macro_intent: formData.get('macro_intent'),
      };
      sendSlotTemplate(payload).catch((err) => console.error(err));
      els.slotForm.reset();
    });

    els.mealBuilderForm?.addEventListener('submit', (event) => {
      event.preventDefault();
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
        tags: formData.get('tags'),
        mfp_alias: formData.get('mfp_alias'),
        notes: formData.get('notes'),
        default_servings: Number(formData.get('default_servings')) || 1,
        ingredients,
      };
      sendMealBuilder(payload).catch((err) => console.error(err));
      els.mealBuilderForm.reset();
      resetIngredientRows();
    });

    els.addIngredientBtn?.addEventListener('click', () => addIngredientRow());
    els.linkSelectedDays?.addEventListener('click', () => linkSelectedDays().catch((err) => console.error(err)));
    els.clearDaySelection?.addEventListener('click', () => { state.selectedWeekdays.clear(); state.focusedGroupId = null; renderWeekdayRibbon(); renderDayGroupFocus(); renderPlan(); });
    document.addEventListener('pointerup', () => { state.dragSelecting = false; });

    els.foodForm?.addEventListener('submit', (event) => {
      event.preventDefault();
      const formData = new FormData(els.foodForm);
      const payload = {
        name: formData.get('name'),
        brand: formData.get('brand'),
        unit_default: formData.get('unit_default') || 'g',
        mfp_search_hint: formData.get('mfp_search_hint'),
        category: formData.get('category'),
        kcal_per_100: Number(formData.get('kcal_per_100')) || 0,
        p_per_100: Number(formData.get('p_per_100')) || 0,
        c_per_100: Number(formData.get('c_per_100')) || 0,
        sugar_per_100: Number(formData.get('sugar_per_100')) || 0,
        f_per_100: Number(formData.get('f_per_100')) || 0,
      };
      sendFoodForm(payload).catch((err) => console.error(err));
      els.foodForm.reset();
    });

    els.applyBtn?.addEventListener('click', () => loadPlan());
    els.shoppingBtn?.addEventListener('click', () => fetchShoppingList().catch((err) => console.error(err)));
    els.checklistBtn?.addEventListener('click', () => fetchChecklist().catch((err) => console.error(err)));
    els.copyShoppingBtn?.addEventListener('click', () => copyText(els.shoppingOutput.textContent, els.copyShoppingBtn));
    els.copyChecklistBtn?.addEventListener('click', () => copyText(els.checklistOutput.textContent, els.copyChecklistBtn));
  }

  function init() {
    bindEvents();
    loadPlan();
  }

  init();
})();
