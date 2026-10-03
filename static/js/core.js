(() => {
  const root = document.querySelector('.core-room-page');
  if (!root) return;

  const coreKey = root.dataset.coreKey || '';
  const DEFAULT_TYPES = ['Training', 'Run', 'Recovery', 'Nutrition', 'System'];
  const DEFAULT_SEVERITIES = ['Daily micro', 'Major'];

  const state = {
    payload: null,
    selectedMemory: null,
    focus: 'heute',
    loadRequestId: 0,
    heavyRenderFrame: 0,
    filters: {
      range: 30,
      types: new Set(DEFAULT_TYPES),
      severities: new Set(DEFAULT_SEVERITIES),
      memoryOnly: false,
      overrideOnly: false,
      memoryActiveOnly: false,
      memoryImpactOnly: false,
    },
  };

  const el = {
    shell: document.getElementById('core-room-shell'),
    generatedAt: document.getElementById('core-generated-at'),
    tabs: Array.from(document.querySelectorAll('.core-tab')),
    openHeute: document.getElementById('core-open-heute'),
    statusPill: document.getElementById('core-status-pill'),
    statusPlan: document.getElementById('core-status-plan'),
    statusIntervening: document.getElementById('core-status-intervening'),
    statusDataQuality: document.getElementById('core-status-data-quality'),
    statusWhy: document.getElementById('core-status-why'),
    todayProblem: document.getElementById('core-today-problem'),
    todayDecision: document.getElementById('core-today-decision'),
    todayRules: document.getElementById('core-today-rules'),
    todayWhy: document.getElementById('core-today-why'),
    todayMemory: document.getElementById('core-today-memory'),
    todayCheck: document.getElementById('core-today-check'),
    healthBars: document.getElementById('core-health-bars'),
    healthMetrics: document.getElementById('core-health-metrics'),
    healthNote: document.getElementById('core-health-note'),
    healthPrinciples: document.getElementById('core-health-principles'),
    decisionList: document.getElementById('core-decision-list'),
    memoryList: document.getElementById('core-memory-list'),
    rangeControl: document.getElementById('core-filter-range'),
    typeControl: document.getElementById('core-filter-type'),
    severityControl: document.getElementById('core-filter-severity'),
    filterMemoryOnly: document.getElementById('core-filter-memory-only'),
    filterOverrideOnly: document.getElementById('core-filter-override-only'),
    memorySearch: document.getElementById('core-memory-search'),
    memoryType: document.getElementById('core-memory-type'),
    memorySort: document.getElementById('core-memory-sort'),
    memoryActiveOnly: document.getElementById('core-memory-active-only'),
    memoryImpactOnly: document.getElementById('core-memory-impact-only'),
    sidebar: document.getElementById('core-memory-sidebar'),
    drawerClose: document.getElementById('core-drawer-close'),
    drawerType: document.getElementById('core-drawer-type'),
    drawerTitle: document.getElementById('core-drawer-title'),
    drawerMeta: document.getElementById('core-drawer-meta'),
    drawerWhy: document.getElementById('core-drawer-why'),
    drawerScope: document.getElementById('core-drawer-scope'),
    drawerAction: document.getElementById('core-drawer-action'),
    drawerEvidence: document.getElementById('core-drawer-evidence'),
    drawerImpactLink: document.getElementById('core-drawer-impact-link'),
    drawerActions: Array.from(document.querySelectorAll('.core-memory-action')),
  };

  function readBootstrapPayload() {
    const node = document.getElementById('core-bootstrap');
    if (!node?.textContent) return null;
    try {
      return JSON.parse(node.textContent);
    } catch (_error) {
      return null;
    }
  }

  function withCore(url) {
    if (!coreKey) return url;
    return `${url}${url.includes('?') ? '&' : '?'}core_key=${encodeURIComponent(coreKey)}`;
  }

  async function authFetch(url, options = {}) {
    return fetch(withCore(url), { ...options, credentials: 'same-origin' });
  }

  function esc(value) {
    return String(value ?? '')
      .replaceAll('&', '&amp;')
      .replaceAll('<', '&lt;')
      .replaceAll('>', '&gt;')
      .replaceAll('"', '&quot;');
  }

  function fmtDate(value, weekday = false) {
    if (!value) return '-';
    const parsed = value.length <= 10 ? new Date(`${value}T00:00:00`) : new Date(value);
    if (Number.isNaN(parsed.getTime())) return value;
    return new Intl.DateTimeFormat(
      'de-DE',
      weekday ? { weekday: 'short', day: '2-digit', month: '2-digit' } : { dateStyle: 'medium', timeStyle: 'short' },
    ).format(parsed);
  }

  function splitLines(text) {
    return String(text || '')
      .split(/[\n•]+/)
      .map((line) => line.trim())
      .filter(Boolean);
  }

  function titleCase(value) {
    const text = String(value || '').trim();
    return text ? text.charAt(0).toUpperCase() + text.slice(1) : '-';
  }

  function badgeClass(kind, value) {
    const safe = String(value || '').toLowerCase().replace(/[^a-z0-9]+/g, '-');
    return `core-badge is-${kind} is-${safe}`;
  }

  function severityLabel(value) {
    return value === 'Daily micro' ? 'Daily' : String(value || '-');
  }

  function memoryChip(memory, mode = 'default') {
    const live = mode === 'live' ? ' is-live' : '';
    return `<button type="button" class="core-chip memory-chip${live}" data-memory-key="${esc(memory?.memory_key)}">${esc(memory?.title || memory?.label || 'Memory')}</button>`;
  }

  function principleChip(principle, mode = 'default') {
    const live = mode === 'live' ? ' is-live' : '';
    const topic = String(principle?.topic || '').trim();
    const label = String(principle?.label || topic || 'Principle').trim();
    return `<button type="button" class="core-chip memory-chip${live}" data-memory-key="principle:${esc(topic)}">${esc(label)}</button>`;
  }

  function usedInsightEntries(item) {
    const memoryEntries = Array.isArray(item?.used_memory) ? item.used_memory : [];
    const principleEntries = Array.isArray(item?.used_principles) ? item.used_principles : [];
    const merged = [];
    const seen = new Set();
    memoryEntries.forEach((entry) => {
      const key = String(entry?.memory_key || '');
      if (!key || seen.has(key)) return;
      seen.add(key);
      merged.push({ kind: 'memory', entry });
    });
    principleEntries.forEach((entry) => {
      const key = `principle:${String(entry?.topic || '').trim()}`;
      if (key === 'principle:' || seen.has(key)) return;
      seen.add(key);
      merged.push({ kind: 'principle', entry });
    });
    return merged;
  }

  function cleanReason(line) {
    const text = String(line || '').trim();
    if (!text) return '';
    const replacements = [
      ['genug Exposures ohne klaren Vorwärtsschritt', 'Seit 8 Einheiten kein sauberer Fortschritt.'],
      ['gleiches Muster nicht endlos wiederholen', 'Das gleiche Muster bringt dich gerade nicht weiter'],
      ['Erholung ernster gewichten', 'Erholung braucht gerade Vorrang'],
      ['RPE vorsichtiger deuten', 'RPE ist gerade nur begrenzt belastbar'],
      ['Datenlage konservativer lesen', 'Die Datenlage ist gerade nicht ganz sauber'],
      ['Qualitätsläufe früher absichern', 'Qualität im Run braucht gerade mehr Kontrolle'],
      ['Stagnation früher sauber benennen', 'Das Plateau soll früh sauber erkannt werden'],
      ['näher am Plan bleiben', 'Der Plan bleibt gerade die bessere Leitplanke'],
      ['Ruhepuls erhöht', 'Ruhepuls hoch'],
      ['Neu bewerten nach 4-6 Exposures oder sobald Leistung wieder sauber anzieht.', 'Check nach 4 Einheiten: Trend muss wieder anziehen.'],
      ['3-5 Wochen andere Variation, Rep-Range oder Struktur', 'Ansatzwechsel: 4 Wochen neu ansetzen.'],
      ['Ansatzwechsel testen', 'Ansatzwechsel: 4 Wochen neu ansetzen.'],
    ];
    let out = text;
    replacements.forEach(([from, to]) => {
      out = out.replace(from, to);
    });
    return out;
  }

  function problemFromReasons(reasons, item = null) {
    const cleaned = reasons.map(cleanReason).filter(Boolean).map((line) => line.replace(/[.]$/, ''));
    if (cleaned.length >= 2) return `${cleaned[0]} + ${cleaned[1]}`;
    if (cleaned.length === 1) return cleaned[0].endsWith('.') ? cleaned[0] : `${cleaned[0]}.`;
    if (item?.type === 'Run') return 'Laufqualität und Erholung ziehen heute nicht sauber zusammen';
    if (item?.type === 'Recovery') return 'Erholung braucht heute Vorrang';
    if (item?.type === 'Training') return 'Der Tag braucht eine klarere Maßnahme';
    return 'CORE sieht eine Lage, die geklärt werden muss';
  }

  function whySentence(reasons) {
    const cleaned = reasons.map(cleanReason).filter(Boolean).slice(0, 2);
    if (!cleaned.length) return 'CORE hält die Maßnahme aktuell für die sauberste Option.';
    const sentence = cleaned.join(' und ');
    return sentence.endsWith('.') ? sentence : `${sentence}.`;
  }

  function buildRules(item) {
    const rules = splitLines(cleanReason(item?.impact_text));
    if (rules.length) return rules.slice(0, 3);
    const decision = cleanReason(String(item?.decision_text || '').trim());
    if (decision) return [decision];
    return ['Heute gilt die aktuelle CORE-Maßnahme ohne Zusatzregel.'];
  }

  function principleStatus(item) {
    const strength = Number(item?.strength || 0);
    const risk = Number(item?.risk || 0);
    if (risk >= 66) return 'kritisch';
    if (strength >= 60 && risk <= 40) return 'sitzt';
    return 'wacklig';
  }

  function setFocus(nextFocus) {
    state.focus = nextFocus;
    root.dataset.focus = nextFocus;
    el.tabs.forEach((button) => {
      button.classList.toggle('is-active', button.dataset.coreFocus === nextFocus);
    });
    const targets = {
      heute: document.getElementById('core-today-panel'),
      entscheidungen: document.getElementById('core-decisions-panel'),
      memory: document.getElementById('core-memory-panel'),
      health: document.getElementById('core-health-panel'),
    };
    targets[nextFocus]?.scrollIntoView({ behavior: 'smooth', block: 'start' });
  }

  function syncControls() {
    el.rangeControl?.querySelectorAll('[data-range]').forEach((button) => {
      button.classList.toggle('is-active', Number(button.dataset.range) === Number(state.filters.range));
    });
    el.typeControl?.querySelectorAll('[data-type]').forEach((button) => {
      button.classList.toggle('is-active', state.filters.types.has(button.dataset.type));
    });
    el.severityControl?.querySelectorAll('[data-severity]').forEach((button) => {
      button.classList.toggle('is-active', state.filters.severities.has(button.dataset.severity));
    });
    el.filterMemoryOnly?.classList.toggle('is-active', state.filters.memoryOnly);
    el.filterOverrideOnly?.classList.toggle('is-active', state.filters.overrideOnly);
    el.memoryActiveOnly?.classList.toggle('is-active', state.filters.memoryActiveOnly);
    el.memoryImpactOnly?.classList.toggle('is-active', state.filters.memoryImpactOnly);
  }

  function renderStatus(today) {
    el.statusPill.textContent = today?.status || '-';
    el.statusPlan.textContent = today?.today || today?.planned || '-';
    el.statusIntervening.textContent = today?.intervening ? 'Ja' : 'Nein';
    el.statusDataQuality.textContent = today?.data_quality?.label || '-';
    el.statusWhy.innerHTML = (today?.why_short || []).slice(0, 2).map((line) => `<div class="core-reason">${esc(cleanReason(line))}</div>`).join('');
  }

  function renderToday(today) {
    const detail = today?.detail || {};
    const reasons = Array.isArray(detail.why) ? detail.why : [];
    el.todayProblem.textContent = problemFromReasons(reasons, { type: 'Training' });
    el.todayDecision.textContent = detail.decision || '-';
    el.todayRules.innerHTML = buildRules({ impact_text: detail.decision, decision_text: detail.decision })
      .map((line) => `<div class="core-rule-item">${esc(line)}</div>`)
      .join('');
    el.todayWhy.textContent = whySentence(reasons);
    const todayMemory = (detail.used_memory || []).slice(0, 3);
    el.todayMemory.innerHTML = todayMemory.length
      ? todayMemory.map((memory) => memoryChip(memory, 'live')).join('')
      : '<span class="core-muted">Keine Erkenntnis aktiv.</span>';
    el.todayCheck.textContent = detail.tomorrow_check || '-';
  }

  function renderHealth(health, principles) {
    const coverage = health?.coverage?.coverage || {};
    el.healthBars.innerHTML = Object.entries(coverage).map(([key, ratio]) => `
      <div class="core-bar-row">
        <span>${esc(titleCase(key))}</span>
        <div class="core-mini-bar"><i style="width:${Math.round(Number(ratio || 0) * 100)}%"></i></div>
      </div>
    `).join('');
    el.healthMetrics.innerHTML = `
      <div class="core-metric"><span>Decision Rate</span><b>${esc(health?.decision_rate)}</b></div>
      <div class="core-metric"><span>Override Rate</span><b>${esc(health?.override_rate)}%</b></div>
      <div class="core-metric"><span>Erkenntnis-Nutzung</span><b>${esc(health?.memory_utilization)}%</b></div>
      <div class="core-metric"><span>Principle-Nutzung</span><b>${esc(health?.principle_utilization || 0)}%</b></div>
      <div class="core-metric"><span>Confidence aktiv</span><b>${esc(health?.confidence_overview?.avg_active_confidence || 0)}%</b></div>
    `;
    el.healthNote.textContent = health?.note || '';
    const principleEntries = Object.values(principles || {})
      .filter((item) => Number(item?.exposures || 0) > 0)
      .sort((a, b) => (Number(b.risk || 0) + Number(b.strength || 0)) - (Number(a.risk || 0) + Number(a.strength || 0)))
      .slice(0, 6);
    el.healthPrinciples.innerHTML = principleEntries.length
      ? principleEntries.map((item) => {
        const status = principleStatus(item);
        const strength = Math.max(0, Math.min(100, Number(item.strength || 0)));
        const risk = Math.max(0, Math.min(100, Number(item.risk || 0)));
        const riskStart = Math.max(0, Math.min(100, strength));
        return `
          <button type="button" class="core-principle-tile is-${status}" data-memory-key="principle:${esc(item.topic)}">
            <div class="core-principle-top">
              <span>${esc(item.label || item.topic)}</span>
              <b class="status-pill is-${status}">${esc(status)}</b>
            </div>
            <div class="srbar" aria-hidden="true">
              <span class="srbar__strength" style="width:${strength}%"></span>
              <span class="srbar__risk" style="left:${riskStart}%; width:${Math.max(0, Math.min(100 - riskStart, risk))}%"></span>
              <span class="srbar__marker" style="left:${riskStart}%"></span>
            </div>
            <div class="core-principle-meta">S ${esc(strength)} / R ${esc(risk)}</div>
          </button>
        `;
      }).join('')
      : '<div class="core-muted">Noch keine Principles aus dem Drill.</div>';
  }

  function filteredDecisions() {
    const decisions = Array.isArray(state.payload?.decisions) ? state.payload.decisions : [];
    const sinceTs = Date.now() - (Number(state.filters.range || 30) * 86400000);
    return decisions.filter((item) => {
      const itemTs = Date.parse(`${item.day_iso}T00:00:00`);
      if (Number.isFinite(itemTs) && itemTs < sinceTs) return false;
      if (!state.filters.types.has(item.type)) return false;
      if (!state.filters.severities.has(item.severity)) return false;
      if (state.filters.memoryOnly && !usedInsightEntries(item).length) return false;
      if (state.filters.overrideOnly && !item.override_flag) return false;
      return true;
    });
  }

  function renderDecisions() {
    const items = filteredDecisions();
    if (!items.length) {
      el.decisionList.innerHTML = '<div class="core-empty">Keine passenden Decisions.</div>';
      return;
    }
    const todayIso = new Date().toISOString().slice(0, 10);
    el.decisionList.innerHTML = items.map((item) => {
      const reasons = Array.isArray(item.why) ? item.why : [];
      const insights = usedInsightEntries(item).slice(0, 3);
      const activeBadge = item.day_iso >= todayIso ? `<span class="${badgeClass('status', 'aktiv')}">Aktiv</span>` : '';
      return `
        <article class="core-decision-item ${item.major_flag ? 'is-major' : ''}">
          <div class="core-decision-head">
            <div class="core-decision-date">${esc(fmtDate(item.day_iso, true))}</div>
            <div class="core-decision-badges">
              <span class="${badgeClass('type', item.type)}">${esc(titleCase(item.type))}</span>
              <span class="${badgeClass('severity', item.severity)}">${esc(severityLabel(item.severity))}</span>
              ${activeBadge}
              ${item.override_flag ? `<span class="${badgeClass('status', 'override')}">Override</span>` : ''}
            </div>
          </div>
          <div class="core-decision-structure">
            <section class="core-decision-block">
              <div class="core-decision-label">Problem</div>
              <div class="core-decision-value is-problem">${esc(problemFromReasons(reasons, item))}</div>
            </section>
            <section class="core-decision-block">
              <div class="core-decision-label">Heute gilt</div>
              <div class="core-decision-value is-decision">${esc(item.decision_text || '-')}</div>
              <div class="core-rule-list">${buildRules(item).map((line) => `<div class="core-rule-item">${esc(line)}</div>`).join('')}</div>
            </section>
            <section class="core-decision-block">
              <div class="core-decision-label">Warum</div>
              <div class="core-decision-copy">${esc(whySentence(reasons))}</div>
            </section>
            <section class="core-decision-block">
              <div class="core-decision-label">Neubewertung</div>
              <div class="core-decision-value">${esc(item.exit_criterion || 'Neu bewerten, sobald die Lage wieder sauber aussieht.')}</div>
            </section>
            <section class="core-decision-block">
              <div class="core-decision-label">Genutzte Erkenntnisse</div>
              <div class="core-chip-row">${insights.length ? insights.map((itemEntry) => itemEntry.kind === 'principle' ? principleChip(itemEntry.entry, item.day_iso >= todayIso ? 'live' : 'default') : memoryChip(itemEntry.entry, item.day_iso >= todayIso ? 'live' : 'default')).join('') : '<span class="core-muted">Keine direkte Erkenntnis verknüpft.</span>'}</div>
            </section>
          </div>
        </article>
      `;
    }).join('');
  }

  function filteredMemories() {
    let items = Array.isArray(state.payload?.memories) ? [...state.payload.memories] : [];
    const search = String(el.memorySearch.value || '').trim().toLowerCase();
    if (search) items = items.filter((item) => `${item.title} ${item.type}`.toLowerCase().includes(search));
    if (el.memoryType.value !== 'all') items = items.filter((item) => item.type === el.memoryType.value);
    if (state.filters.memoryActiveOnly) items = items.filter((item) => !['ignored', 'archived'].includes(String(item.status || 'active')));
    if (state.filters.memoryImpactOnly) items = items.filter((item) => item.is_impacting);
    const sorter = el.memorySort.value;
    items.sort((a, b) => {
      if (sorter === 'updated') return String(b.last_updated || '').localeCompare(String(a.last_updated || ''));
      if (sorter === 'impact') return Number(b.impact_count || 0) - Number(a.impact_count || 0);
      return Number(b.confidence || 0) - Number(a.confidence || 0);
    });
    return items;
  }

  function renderMemories() {
    const items = filteredMemories();
    if (!items.length) {
      el.memoryList.innerHTML = '<div class="core-empty">Keine passenden Memory-Statements.</div>';
      return;
    }
    el.memoryList.innerHTML = items.map((item) => `
      <button type="button" class="core-memory-row ${state.selectedMemory?.memory_key === item.memory_key ? 'is-selected' : ''}" data-memory-key="${esc(item.memory_key)}">
        <div class="core-memory-main">
          <div class="core-memory-title">${esc(item.title)}</div>
          <div class="core-memory-sub">${esc(item.type)} · ${esc(item.status || 'active')}</div>
        </div>
        <div class="core-memory-meta">
          <span>${esc(item.confidence)}%</span>
          <span>n=${esc(item.n)}</span>
          ${item.is_impacting ? '<span class="core-memory-live">Aktiv</span>' : ''}
        </div>
      </button>
    `).join('');
  }

  function closeSidebar() {
    state.selectedMemory = null;
    el.sidebar.classList.add('is-hidden');
    el.sidebar.setAttribute('aria-hidden', 'true');
    el.shell.classList.remove('has-sidebar');
    renderMemories();
  }

  function openSidebar(memoryKey) {
    const memory = (state.payload?.memories || []).find((item) => item.memory_key === memoryKey);
    if (!memory) {
      closeSidebar();
      return;
    }
    if (state.selectedMemory?.memory_key === memoryKey) {
      closeSidebar();
      return;
    }
    state.selectedMemory = memory;
    el.drawerType.textContent = memory.type || 'Memory';
    el.drawerTitle.textContent = memory.title || '-';
    el.drawerMeta.innerHTML = `
      <span>Confidence ${esc(memory.confidence)}%</span>
      <span>n=${esc(memory.n)}</span>
      <span>Status ${esc(memory.status || 'active')}</span>
      ${memory.test_until ? `<span>Test bis ${esc(memory.test_until)}</span>` : ''}
    `;
    el.drawerWhy.textContent = memory.why || '-';
    el.drawerScope.textContent = memory.scope || '-';
    el.drawerAction.textContent = memory.action || '-';
    el.drawerEvidence.innerHTML = (memory.evidence || []).map((item) => `<span class="core-chip">${esc(item.label || item.ref)}</span>`).join('');
    el.drawerImpactLink.textContent = `${Number(memory.impact_count || 0)} Decisions anzeigen`;
    el.sidebar.classList.remove('is-hidden');
    el.sidebar.setAttribute('aria-hidden', 'false');
    el.shell.classList.add('has-sidebar');
    renderMemories();
  }

  async function updateMemory(action) {
    if (!state.selectedMemory?.memory_key) return;
    const res = await authFetch(`/api/core/memory/${encodeURIComponent(state.selectedMemory.memory_key)}/state`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ action }),
    });
    if (!res.ok) return;
    const currentKey = state.selectedMemory.memory_key;
    await loadPayload(state.filters.range);
    openSidebar(currentKey);
  }

  function applyPayload(nextPayload, requestId = state.loadRequestId) {
    state.payload = nextPayload;
    el.generatedAt.textContent = fmtDate(state.payload?.generated_at);
    renderStatus(state.payload?.today);
    renderToday(state.payload?.today);
    renderHealth(state.payload?.health, state.payload?.principles);
    if (state.heavyRenderFrame) cancelAnimationFrame(state.heavyRenderFrame);
    state.heavyRenderFrame = requestAnimationFrame(() => {
      if (requestId !== state.loadRequestId) return;
      renderDecisions();
      renderMemories();
      if (state.selectedMemory?.memory_key) openSidebar(state.selectedMemory.memory_key);
      state.heavyRenderFrame = 0;
    });
  }

  async function loadPayload(days = state.filters.range) {
    const requestId = ++state.loadRequestId;
    state.filters.range = Number(days || state.filters.range || 30);
    syncControls();
    const res = await authFetch(`/api/core/control-room?days=${encodeURIComponent(state.filters.range)}`, { cache: 'no-store' });
    if (!res.ok) return;
    const nextPayload = await res.json();
    if (requestId !== state.loadRequestId) return;
    applyPayload(nextPayload, requestId);
  }

  el.tabs.forEach((button) => button.addEventListener('click', () => setFocus(button.dataset.coreFocus || 'heute')));
  el.openHeute?.addEventListener('click', () => setFocus('heute'));

  el.rangeControl?.addEventListener('click', async (event) => {
    const button = event.target.closest('[data-range]');
    if (!button) return;
    await loadPayload(Number(button.dataset.range || 30));
  });

  el.typeControl?.addEventListener('click', (event) => {
    const button = event.target.closest('[data-type]');
    if (!button) return;
    const key = button.dataset.type;
    if (state.filters.types.has(key) && state.filters.types.size > 1) state.filters.types.delete(key);
    else state.filters.types.add(key);
    syncControls();
    renderDecisions();
  });

  el.severityControl?.addEventListener('click', (event) => {
    const button = event.target.closest('[data-severity]');
    if (!button) return;
    const key = button.dataset.severity;
    if (state.filters.severities.has(key) && state.filters.severities.size > 1) state.filters.severities.delete(key);
    else state.filters.severities.add(key);
    syncControls();
    renderDecisions();
  });

  el.filterMemoryOnly?.addEventListener('click', () => {
    state.filters.memoryOnly = !state.filters.memoryOnly;
    syncControls();
    renderDecisions();
  });

  el.filterOverrideOnly?.addEventListener('click', () => {
    state.filters.overrideOnly = !state.filters.overrideOnly;
    syncControls();
    renderDecisions();
  });

  el.memoryActiveOnly?.addEventListener('click', () => {
    state.filters.memoryActiveOnly = !state.filters.memoryActiveOnly;
    syncControls();
    renderMemories();
  });

  el.memoryImpactOnly?.addEventListener('click', () => {
    state.filters.memoryImpactOnly = !state.filters.memoryImpactOnly;
    syncControls();
    renderMemories();
  });

  [el.memorySearch, el.memoryType, el.memorySort].forEach((input) => {
    input?.addEventListener('input', renderMemories);
    input?.addEventListener('change', renderMemories);
  });

  el.memoryList?.addEventListener('click', (event) => {
    const row = event.target.closest('[data-memory-key]');
    if (!row) return;
    openSidebar(row.dataset.memoryKey || '');
  });

  el.todayMemory?.addEventListener('click', (event) => {
    const chip = event.target.closest('[data-memory-key]');
    if (!chip) return;
    openSidebar(chip.dataset.memoryKey || '');
  });

  el.decisionList?.addEventListener('click', (event) => {
    const chip = event.target.closest('[data-memory-key]');
    if (!chip) return;
    openSidebar(chip.dataset.memoryKey || '');
  });

  el.healthPrinciples?.addEventListener('click', (event) => {
    const tile = event.target.closest('[data-memory-key]');
    if (!tile) return;
    openSidebar(tile.dataset.memoryKey || '');
  });

  el.drawerClose?.addEventListener('click', closeSidebar);
  el.drawerImpactLink?.addEventListener('click', () => {
    if (!state.selectedMemory) return;
    state.filters.memoryOnly = true;
    syncControls();
    setFocus('entscheidungen');
    renderDecisions();
  });

  el.drawerActions.forEach((button) => button.addEventListener('click', () => updateMemory(button.dataset.memoryAction || 'active')));

  window.addEventListener('keydown', (event) => {
    if (event.key === 'Escape' && state.selectedMemory) closeSidebar();
  });

  syncControls();
  const bootstrapPayload = readBootstrapPayload();
  if (bootstrapPayload) {
    applyPayload(bootstrapPayload, state.loadRequestId);
    window.requestAnimationFrame(() => {
      loadPayload(state.filters.range);
    });
  } else {
    loadPayload(state.filters.range);
  }
})();
