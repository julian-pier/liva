(function () {
  const root = document.querySelector('.core-v2-page');
  if (!root) return;

  const coreKey = root.dataset.coreKey || '';
  const els = {
    generated: document.getElementById('core-v2-generated'),
    statusLine: document.getElementById('core-board-status-line'),
    recompute: document.getElementById('core-v2-recompute'),
    headline: document.getElementById('core-board-headline'),
    summary: document.getElementById('core-board-summary'),
    confidence: document.getElementById('core-board-confidence'),
    modeBadge: document.getElementById('core-board-mode-badge'),
    consistencyHint: document.getElementById('core-board-consistency-hint'),
    keyReasons: document.getElementById('core-board-key-reasons'),
    decisionRecommendation: document.getElementById('core-decision-recommendation'),
    decisionDataQuality: document.getElementById('core-decision-data-quality'),
    decisionUpdated: document.getElementById('core-decision-updated'),
    metrics: {
      recovery: {
        value: document.getElementById('metric-recovery-value'),
        badge: document.getElementById('metric-recovery-badge'),
        description: document.getElementById('metric-recovery-description'),
        meta: document.getElementById('metric-recovery-meta'),
      },
      sleep: {
        value: document.getElementById('metric-sleep-value'),
        badge: document.getElementById('metric-sleep-badge'),
        description: document.getElementById('metric-sleep-description'),
        meta: document.getElementById('metric-sleep-meta'),
      },
      ans: {
        value: document.getElementById('metric-ans-value'),
        badge: document.getElementById('metric-ans-badge'),
        description: document.getElementById('metric-ans-description'),
        meta: document.getElementById('metric-ans-meta'),
      },
      pulse: {
        value: document.getElementById('metric-pulse-value'),
        badge: document.getElementById('metric-pulse-badge'),
        description: document.getElementById('metric-pulse-description'),
        meta: document.getElementById('metric-pulse-meta'),
      },
      flags: {
        value: document.getElementById('metric-flags-value'),
        badge: document.getElementById('metric-flags-badge'),
        description: document.getElementById('metric-flags-description'),
        meta: document.getElementById('metric-flags-meta'),
      },
    },
    overviewHeadline: document.getElementById('core-overview-headline'),
    overviewSummary: document.getElementById('core-overview-summary'),
    overviewWatchouts: document.getElementById('core-overview-watchouts'),
    overviewMissingWrap: document.getElementById('core-overview-missing-wrap'),
    overviewMissing: document.getElementById('core-overview-missing'),
    filterRow: document.getElementById('core-board-filter-row'),
    signalsCount: document.getElementById('core-board-signals-count'),
    signalsLine: document.getElementById('core-board-signals-line'),
    signalsList: document.getElementById('core-board-signals-list'),
    signalDetail: document.getElementById('core-board-signal-detail'),
    dataBody: document.getElementById('core-data-body'),
    dataSleep: document.getElementById('core-data-sleep'),
    dataTraining: document.getElementById('core-data-training'),
    dataCheckin: document.getElementById('core-data-checkin'),
    dataCalendar: document.getElementById('core-data-calendar'),
    dataNutrition: document.getElementById('core-data-nutrition'),
    tabs: Array.from(document.querySelectorAll('.core-tab')),
    panels: Array.from(document.querySelectorAll('.core-tab-panel')),
    detailsTitle: document.getElementById('core-details-title'),
    detailsSubline: document.getElementById('core-details-subline'),
    historyChart: document.getElementById('core-history-chart'),
    historyList: document.getElementById('core-history-list'),
    historyEmpty: document.getElementById('core-history-empty'),
    historyCaption: document.getElementById('core-history-caption'),
  };

  const state = {
    payload: null,
    signals: [],
    currentFilter: 'all',
    selectedSignalId: null,
    activeTab: 'overview',
    historyRows: [],
    historyLoaded: false,
  };

  function withCore(url) {
    if (!coreKey) return url;
    return `${url}${url.includes('?') ? '&' : '?'}core_key=${encodeURIComponent(coreKey)}`;
  }

  function esc(value) {
    return String(value ?? '')
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;');
  }

  function text(value, fallback = '') {
    const out = String(value ?? '').replace(/\s+/g, ' ').trim();
    return out || fallback;
  }

  function missingLike(value) {
    const raw = text(value).toLowerCase();
    return !raw || ['-', '—', 'fehlt', 'offen', 'keine angabe', 'unbekannt'].includes(raw);
  }

  function clear(node) {
    if (node) node.replaceChildren();
  }

  function readBootstrap() {
    const script = document.getElementById('core-board-bootstrap');
    if (!script) return null;
    try {
      return JSON.parse(script.textContent || '{}');
    } catch (_error) {
      return null;
    }
  }

  function parsePct(value) {
    const n = Number(value);
    return Number.isFinite(n) ? Math.max(0, Math.min(100, Math.round(n))) : null;
  }

  function parseNum(value) {
    const n = Number(value);
    return Number.isFinite(n) ? n : null;
  }

  function fmtDateTime(value) {
    const raw = text(value);
    if (!raw) return '-';
    const dt = new Date(raw.length <= 10 ? `${raw}T00:00:00` : raw);
    if (Number.isNaN(dt.getTime())) return raw;
    return new Intl.DateTimeFormat('de-DE', { dateStyle: 'medium', timeStyle: 'short' }).format(dt);
  }

  function fmtRelative(value) {
    const raw = text(value);
    if (!raw) return 'fehlt';
    const dt = new Date(raw.length <= 10 ? `${raw}T00:00:00` : raw);
    if (Number.isNaN(dt.getTime())) return raw;
    const diffMs = Date.now() - dt.getTime();
    const hours = Math.round(diffMs / 3600000);
    if (hours <= 2) return 'heute';
    if (hours < 24) return `vor ${hours} h`;
    const days = Math.round(hours / 24);
    if (days === 1) return 'gestern';
    return `vor ${days} Tagen`;
  }

  function fmtSleepHours(minutes) {
    const total = parseNum(minutes);
    if (total == null || total <= 0) return '-';
    const hh = Math.floor(total / 60);
    const mm = Math.round(total % 60);
    return `${hh}:${String(mm).padStart(2, '0')}`;
  }

  function shortSentence(value, fallback = '-') {
    const line = text(value, fallback).replace(/[. ]*$/, '');
    return line ? `${line}.` : fallback;
  }

  function firstNumber(value) {
    const match = text(value).match(/-?\d+(?:[.,]\d+)?/);
    if (!match) return null;
    return Number(match[0].replace(',', '.'));
  }

  function valueOrFallback(value, fallback = 'fehlt') {
    return missingLike(value) ? fallback : text(value, fallback);
  }

  function pickFirstNonMissing(...values) {
    for (const value of values) {
      if (!missingLike(value)) return text(value);
    }
    return '';
  }

  function findSignal(signals, patterns) {
    return signals.find((item) => patterns.some((pattern) => item.id.includes(pattern) || item.label.toLowerCase().includes(pattern)));
  }

  function statusTone(kind) {
    return {
      good: 'good',
      supports: 'good',
      normal: 'neutral',
      neutral: 'neutral',
      medium: 'caution',
      caution: 'caution',
      warning: 'caution',
      stale: 'caution',
      bad: 'bad',
      limits: 'bad',
      critical: 'bad',
      missing: 'soft',
      soft: 'soft',
      heavy: 'good',
      light: 'caution',
      rest: 'bad',
      deload: 'caution',
      off: 'bad',
    }[text(kind).toLowerCase()] || 'soft';
  }

  function badgeClass(kind) {
    return `core-badge core-badge-${statusTone(kind)}`;
  }

  function setBadge(node, label, tone) {
    if (!node) return;
    node.className = badgeClass(tone);
    node.textContent = label;
  }

  function directionLabel(direction) {
    if (direction === 'supports') return 'trägt';
    if (direction === 'limits') return 'bremst';
    if (direction === 'missing') return 'fehlt';
    return 'neutral';
  }

  function impactLabel(impact) {
    if (impact === 'high') return 'hoch';
    if (impact === 'medium') return 'mittel';
    return 'niedrig';
  }

  function freshnessLabel(stateValue) {
    if (stateValue === 'fresh') return 'heute';
    if (stateValue === 'ok') return 'brauchbar';
    if (stateValue === 'stale') return 'alt';
    return 'fehlt';
  }

  function categoryLabel(category) {
    return {
      recovery: 'Recovery',
      checkin: 'Check-in',
      nutrition: 'Ernährung',
      calendar: 'Kalender',
      training: 'Training',
      system: 'Kontext',
    }[category] || 'Signal';
  }

  function cleanSignalLabel(label) {
    return text(label)
      .replace(/vorläufiges core board/gi, 'Heute')
      .replace(/recovery-konflikt/gi, 'Recovery')
      .replace(/protein-stabilität/gi, 'Protein')
      .replace(/entscheidungsdruck/gi, 'Training heute relevant')
      .replace(/alltag_kalender/gi, 'Abendlast')
      .replace(/training_card_status/gi, 'Trainingskarte')
      .replace(/sleep_duration/gi, 'Schlaf')
      .replace(/resting-hr-lage/gi, 'Puls')
      .replace(/schlafqualität/gi, 'Schlaf')
      .replace(/carb-timing/gi, 'Fueling')
      .replace(/\bhrv\b/gi, 'HRV')
      .replace(/\brhr\b/gi, 'Ruhepuls');
  }

  function shortSignalName(signal) {
    const label = cleanSignalLabel(signal.label);
    if (/protein/i.test(label)) return 'Protein lückenhaft';
    if (/recovery/i.test(label)) return 'Recovery bremst';
    if (/abend|alltag|kalender/i.test(label)) return 'Abendlast hoch';
    if (/training heute relevant/i.test(label)) return 'Training heute relevant';
    if (/motivation/i.test(label)) return 'Motivation fehlt';
    return label;
  }

  function humanSignalDescription(signal) {
    const evidence = text(signal.evidence || signal.headline || '—');
    if (/protein/i.test(signal.label)) return 'nicht als Tagesargument gewertet';
    if (/recovery/i.test(signal.label)) return 'HRV und Ruhepuls gemeinsam bewertet';
    if (/abend|alltag|kalender/i.test(signal.label)) return evidence || 'Nächster Abendtermin belastet';
    if (/trainingskarte/i.test(signal.label) || /training_card/i.test(signal.id)) return 'Trainingskarte für heute fehlt';
    return evidence || 'Keine klare Zusatzinfo';
  }

  function compactSignalValue(signal) {
    const value = text(signal.headline, '—');
    if (value.length <= 18) return value;
    if (/gemischt/i.test(value)) return 'gemischt';
    if (/lückenhaft/i.test(value)) return 'lückenhaft';
    if (/ruhig/i.test(value)) return 'ruhig';
    if (/stabil/i.test(value)) return 'stabil';
    return `${value.slice(0, 17).trim()}…`;
  }

  function sleepMainValue(signal) {
    const textValue = text(signal?.evidence || signal?.headline);
    const score = textValue.match(/(\d{2,3})\s*\/\s*100/) || textValue.match(/score\s*(\d{2,3})/i) || text(signal?.headline).match(/(\d{2,3})/);
    if (score) return `${score[1]} / 100`;
    return '—';
  }

  function sleepDurationLabel(signal) {
    const source = `${text(signal?.headline)} ${text(signal?.evidence)}`;
    const duration = source.match(/(\d+)\s*h\s*(\d{1,2})/) || source.match(/(\d+):(\d{2})/);
    if (!duration) return '—';
    if (source.includes(':')) return `${duration[1]} h ${duration[2]} min`;
    return `${duration[1]} h ${duration[2]} min`;
  }

  function sleepWindowLabel(signal) {
    const source = `${text(signal?.headline)} ${text(signal?.evidence)}`;
    const range = source.match(/(\d{2}:\d{2}).*?(\d{2}:\d{2})/);
    if (!range) return null;
    return `${range[1]}–${range[2]}`;
  }

  function pulseValueLabel(signal) {
    const n = firstNumber(signal?.headline || signal?.evidence);
    if (n == null) return '—';
    return `${n > 0 ? '+' : ''}${Math.round(n)} bpm`;
  }

  function hrvValueLabel(signal) {
    const n = firstNumber(signal?.headline || signal?.evidence);
    if (n == null) return '—';
    return `${n > 0 ? '+' : ''}${Math.round(n)} ms`;
  }

  function checkinFieldValue(signal) {
    if (!signal) return 'fehlt';
    return pickFirstNonMissing(signal.evidence, signal.headline) || 'fehlt';
  }

  function trainingLogLabel(signals, payload) {
    const signal = findSignal(signals, ['execution_risk', 'training']);
    const evidence = text(signal?.evidence);
    const match = evidence.match(/(\d+)\s*tag/i);
    if (match) {
      const n = firstNumber(match[1]);
      if (n != null) return n <= 1 ? 'heute' : `vor ${Math.round(n)} Tagen`;
    }
    return freshnessDetail(payload, ['training'], 'fehlt');
  }

  function compactDecisionMode(mode) {
    if (mode === 'REST') return 'Rest';
    if (mode === 'DELOAD') return 'Deload';
    if (mode === 'LIGHT') return 'LIGHT';
    if (mode === 'HEAVY') return 'Heavy';
    return 'Normal';
  }

  function severityScore(signal) {
    let score = 0;
    if (signal.freshnessState === 'missing') score += 400;
    if (signal.freshnessState === 'stale') score += 220;
    if (signal.direction === 'limits') score += 240;
    if (signal.direction === 'supports') score += 160;
    if (signal.impact === 'high') score += 180;
    if (signal.impact === 'medium') score += 90;
    if (signal.status === 'critical') score += 140;
    if (signal.status === 'warning') score += 80;
    if (signal.isCheckin) score += 70;
    return score;
  }

  function normalizeFreshness(rows) {
    const out = {};
    (rows || []).forEach((row) => {
      const id = text(row.id).toLowerCase();
      if (!id) return;
      const label = text(row.freshness).toLowerCase();
      let stateValue = 'ok';
      if (['heute', 'frisch'].includes(label)) stateValue = 'fresh';
      else if (['veraltet', 'alt'].includes(label)) stateValue = 'stale';
      else if (['fehlt', 'offen'].includes(label)) stateValue = 'missing';
      out[id] = {
        label: row.label || id,
        state: stateValue,
        detail: text(row.detail || row.secondary || row.freshness),
      };
    });
    return out;
  }

  function freshnessSources(payload) {
    return Array.isArray(payload?.ui?.data_freshness?.sources) ? payload.ui.data_freshness.sources : [];
  }

  function freshnessSource(payload, keys) {
    const lookup = freshnessSources(payload);
    return lookup.find((row) => keys.includes(text(row.id).toLowerCase()));
  }

  function freshnessDetail(payload, keys, fallback = 'fehlt') {
    const row = freshnessSource(payload, keys);
    return text(row?.detail || row?.secondary || row?.freshness, fallback);
  }

  function freshnessStateFromPayload(payload, keys, fallback = 'missing') {
    const row = freshnessSource(payload, keys);
    const label = text(row?.freshness).toLowerCase();
    if (label === 'heute') return 'fresh';
    if (['gestern', 'veraltet', 'alt'].includes(label)) return 'stale';
    if (['fehlt', 'offen'].includes(label)) return 'missing';
    if (label) return 'ok';
    return fallback;
  }

  function normalizeSignals(payload) {
    const ui = payload?.ui || {};
    const freshnessMap = normalizeFreshness(ui?.data_freshness?.sources || ui?.data_status_items || []);
    const items = Array.isArray(ui.today_signal_items) ? ui.today_signal_items : [];

    return items.map((item, index) => {
      const id = text(item.key, `signal_${index}`).toLowerCase();
      const label = cleanSignalLabel(item.label || item.name || item.headline || 'Signal');
      const headline = text(item.headline || item.value || item.badge || 'Keine Angabe');
      const evidence = text(item.evidence || item.detail || item.reason || '');
      const freshness = freshnessMap[id] || {};
      const freshnessState = freshness.state || 'ok';
      const badge = text(item.badge).toLowerCase();
      let direction = 'neutral';
      if (text(item.impact).toLowerCase() === 'push') direction = 'supports';
      else if (['limit', 'caution'].includes(text(item.impact).toLowerCase())) direction = 'limits';
      else if (freshnessState === 'missing') direction = 'missing';
      else if (freshnessState === 'stale') direction = 'limits';

      let impact = 'low';
      if (['push', 'limit', 'caution'].includes(text(item.impact).toLowerCase())) impact = 'high';
      else if (evidence || headline.length > 18) impact = 'medium';

      let status = 'neutral';
      if (freshnessState === 'missing') status = 'missing';
      else if (freshnessState === 'stale') status = 'stale';
      else if (['kritisch', 'unplausibel'].includes(badge)) status = 'critical';
      else if (['beobachten', 'erhöht', 'schwankend', 'gedrückt', 'gemischt'].includes(badge)) status = 'warning';
      else if (['positiv', 'gut', 'ruhig', 'okay', 'stabil', 'normal', 'planbar'].includes(badge)) status = 'good';

      const category = (() => {
        if (id.includes('sleep') || id.includes('hrv') || id.includes('rhr') || id.includes('recovery')) return 'recovery';
        if (id.includes('motivation') || id.includes('energy') || id.includes('pain')) return 'checkin';
        if (id.includes('weight') || id.includes('protein') || id.includes('carb') || id.includes('fuel')) return 'nutrition';
        if (id.includes('calendar') || id.includes('alltag') || id.includes('stress')) return 'calendar';
        if (id.includes('training') || id.includes('load') || id.includes('run') || id.includes('performance')) return 'training';
        return 'system';
      })();

      return {
        id,
        label,
        headline,
        evidence,
        category,
        direction,
        impact,
        status,
        freshnessState,
        freshnessDetail: freshness.detail || headline,
        isCheckin: category === 'checkin',
      };
    }).sort((a, b) => severityScore(b) - severityScore(a) || a.label.localeCompare(b.label, 'de'));
  }

  function deriveMode(payload, signals) {
    const headline = `${text(payload?.ui?.headline)} ${text(payload?.ui?.summary)}`.toLowerCase();
    if (headline.includes('rest') || headline.includes('erholung')) return 'REST';
    if (headline.includes('deload')) return 'DELOAD';
    if (headline.includes('light') || headline.includes('reduzier')) return 'LIGHT';
    if (headline.includes('heavy')) return 'HEAVY';
    const hardLimits = signals.filter((item) => item.direction === 'limits' && item.impact === 'high').length;
    if (hardLimits >= 3) return 'LIGHT';
    return 'NORMAL';
  }

  function modeBadgeTone(mode) {
    return { HEAVY: 'heavy', NORMAL: 'normal', LIGHT: 'light', REST: 'rest', DELOAD: 'deload' }[mode] || 'soft';
  }

  function recommendationText(mode, payload) {
    const next = text(payload?.ui?.next_step_text).toLowerCase();
    if (mode === 'REST') return 'Heute defensiv bleiben und Recovery priorisieren.';
    if (mode === 'LIGHT') return 'Heute defensiv trainieren. Kein PR-Fokus.';
    if (mode === 'DELOAD') return 'Deload sauber halten. Nicht hochziehen.';
    if (next.includes('karte')) return 'Plan halten, aber erst die Trainingskarte sauber bauen.';
    if (mode === 'HEAVY') return 'Plan halten. Mehr Druck ist möglich, wenn die Ausführung stabil bleibt.';
    return 'Plan halten. Sauber trainieren, aber nichts erzwingen.';
  }

  function heroSummaryText(mode, signals) {
    const limits = signals.filter((item) => item.direction === 'limits').slice(0, 2).map((item) => item.label.toLowerCase());
    const supports = signals.filter((item) => item.direction === 'supports').slice(0, 2).map((item) => item.label.toLowerCase());
    if (mode === 'REST') return 'Heute steht Erholung im Vordergrund. Training würde mehr Kosten als Nutzen bringen.';
    if (mode === 'LIGHT') return `Heute besser Druck rausnehmen: ${limits.join(' und ') || 'Recovery und Alltag'} bremsen klar.`;
    if (mode === 'HEAVY') return `Heute ist mehr Freigabe da: ${supports.join(' und ') || 'Recovery und Puls'} tragen.`;
    return `Heute sauber trainieren: ${supports.join(' und ') || 'Basisdaten'} tragen, ${limits.join(' und ') || 'einzelne Signale'} begrenzen aber den Druck.`;
  }

  function quickDataQuality(payload) {
    const ui = payload?.ui || {};
    const fresh = Number(ui?.freshness_counts?.fresh || 0);
    const missing = Number(ui?.data_freshness?.missing_critical || 0);
    if (missing >= 2) return 'kritisch';
    if (fresh >= 4) return 'gut';
    return 'mittel';
  }

  function fillList(node, lines, emptyText) {
    if (!node) return;
    clear(node);
    const clean = (lines || []).map((line) => text(line)).filter(Boolean);
    if (!clean.length) {
      const row = document.createElement('div');
      row.className = 'core-list-row is-empty';
      row.textContent = emptyText;
      node.append(row);
      return;
    }
    clean.forEach((line) => {
      const row = document.createElement('div');
      row.className = 'core-list-row';
      row.textContent = line;
      node.append(row);
    });
  }

  function renderHeader(payload, signals) {
    const ui = payload?.ui || {};
    const mode = deriveMode(payload, signals);
    const confidencePct = parsePct(ui.confidence_pct) || 0;
    const quality = quickDataQuality(payload);
    const modeLabel = compactDecisionMode(mode);

    els.headline.textContent = `Heute: ${modeLabel}`;
    els.summary.textContent = recommendationText(mode, payload).replace(/\.$/, '');
    els.generated.textContent = fmtDateTime(payload?.generated_at);
    els.statusLine.textContent = text(ui.board_status, 'Status noch offen');
    els.decisionUpdated.textContent = payload?.ui?.morning_checkin_done_today ? 'vollständige Tagesdaten' : 'bisherige Datenlage';
    els.decisionRecommendation.textContent = recommendationText(mode, payload).replace(/\.$/, '');
    els.decisionDataQuality.textContent = quality === 'gut' ? 'frisch' : quality === 'kritisch' ? 'lückenhaft' : 'brauchbar';

    setBadge(els.modeBadge, mode, modeBadgeTone(mode));
    setBadge(els.confidence, `${confidencePct} %`, confidencePct >= 80 ? 'good' : confidencePct >= 60 ? 'caution' : 'soft');

    const hint = text(ui.board_consistency_hint);
    els.consistencyHint.hidden = !hint;
    els.consistencyHint.textContent = hint;

    clear(els.keyReasons);
    signals.slice(0, 3).forEach((signal) => {
      const chip = document.createElement('span');
      chip.className = badgeClass(signal.direction === 'supports' ? 'good' : signal.direction === 'limits' ? 'bad' : signal.freshnessState === 'missing' ? 'soft' : 'caution');
      chip.textContent = shortSignalName(signal);
      els.keyReasons.append(chip);
    });
    if (signals.length > 3) {
      const chip = document.createElement('span');
      chip.className = 'core-badge core-badge-soft';
      chip.textContent = '+ weitere';
      els.keyReasons.append(chip);
    }
  }

  function renderMetrics(payload, signals) {
    const ui = payload?.ui || {};
    const recoverySignal = findSignal(signals, ['recovery_conflict', 'recovery', 'sleep_score', 'sleep_duration']);
    const sleepSignal = findSignal(signals, ['sleep_score', 'sleep', 'schlaf']);
    const hrvSignal = findSignal(signals, ['hrv']);
    const pulseSignal = findSignal(signals, ['rhr', 'resting', 'pulse']);
    const missingCount = signals.filter((item) => item.freshnessState === 'missing').length;
    const limitCount = signals.filter((item) => item.direction === 'limits').length;

    const recoveryPct = parsePct(ui.confidence_pct ? Number(ui.confidence_pct) * 0.9 : null) || parsePct(firstNumber(recoverySignal?.evidence));
    els.metrics.recovery.value.textContent = recoveryPct != null ? `${recoveryPct}` : 'fehlt';
    setBadge(
      els.metrics.recovery.badge,
      recoverySignal?.status === 'good' ? 'gut' : recoverySignal?.status === 'critical' ? 'kritisch' : 'okay',
      recoverySignal?.status === 'good' ? 'good' : recoverySignal?.status === 'critical' ? 'bad' : 'caution',
    );
    els.metrics.recovery.description.textContent = compactSignalValue(recoverySignal || {}) || 'Recovery unklar';
    els.metrics.recovery.meta.textContent = 'HRV und RHR gemeinsam bewertet';

    els.metrics.sleep.value.textContent = sleepMainValue(sleepSignal);
    setBadge(
      els.metrics.sleep.badge,
      sleepSignal?.status === 'good' ? 'gut' : sleepSignal ? 'brauchbar' : 'fehlt',
      sleepSignal?.status === 'good' ? 'good' : sleepSignal ? 'soft' : 'missing',
    );
    els.metrics.sleep.description.textContent = sleepDurationLabel(sleepSignal);
    els.metrics.sleep.meta.textContent = sleepWindowLabel(sleepSignal) || 'Polar Sleep Score';

    const hrvValue = hrvValueLabel(hrvSignal);
    els.metrics.ans.value.textContent = hrvValue !== '—' ? hrvValue : compactSignalValue(hrvSignal || {}) || 'fehlt';
    setBadge(
      els.metrics.ans.badge,
      hrvSignal?.status === 'good' ? 'nah an Basis' : hrvSignal ? 'brauchbar' : 'fehlt',
      hrvSignal?.status === 'good' ? 'good' : hrvSignal ? 'soft' : 'missing',
    );
    els.metrics.ans.description.textContent = hrvSignal?.status === 'good' ? 'keine starke Abweichung' : humanSignalDescription(hrvSignal || { label: 'HRV' });
    els.metrics.ans.meta.textContent = 'vs. Baseline';

    const pulseValue = pulseValueLabel(pulseSignal);
    els.metrics.pulse.value.textContent = pulseValue !== '—' ? pulseValue : compactSignalValue(pulseSignal || {}) || 'fehlt';
    setBadge(
      els.metrics.pulse.badge,
      pulseSignal?.status === 'good' ? 'ruhig' : pulseSignal ? 'brauchbar' : 'fehlt',
      pulseSignal?.status === 'good' ? 'good' : pulseSignal ? 'soft' : 'missing',
    );
    els.metrics.pulse.description.textContent = pulseSignal?.status === 'good' ? 'unter Baseline' : humanSignalDescription(pulseSignal || { label: 'Puls' });
    els.metrics.pulse.meta.textContent = 'Schlafpuls / Ruhepuls';

    els.metrics.flags.value.textContent = `${limitCount}`;
    setBadge(els.metrics.flags.badge, limitCount ? 'beachten' : 'klar', limitCount ? 'caution' : 'good');
    els.metrics.flags.description.textContent = `${limitCount} Bremsen aktiv`;
    els.metrics.flags.meta.textContent = `${signals.filter((item) => item.status === 'critical').length} harte Stopps · ${missingCount} fehlen`;
  }

  function renderOverview(payload, signals) {
    const ui = payload?.ui || {};
    const mode = deriveMode(payload, signals);
    els.overviewHeadline.textContent = recommendationText(mode, payload).replace(/\.$/, '');
    fillList(
      els.overviewSummary,
      [
        heroSummaryText(mode, signals),
        text(ui.summary),
        text(ui.next_step_text),
      ].filter(Boolean).slice(0, 3),
      'Keine Zusammenfassung verfügbar.',
    );
    const watchouts = signals
      .filter((item) => item.direction === 'limits' || item.freshnessState === 'missing')
      .slice(0, 4)
      .map((item) => `${shortSignalName(item)}: ${humanSignalDescription(item)}`);
    fillList(els.overviewWatchouts, watchouts, 'Keine harten Warnhinweise.');
    const missing = [];
    if (!payload?.ui?.morning_checkin_done_today) missing.push('Morning Check-in fehlt');
    if (!payload?.ui?.training_card_ready) missing.push('Trainingskarte noch nicht finalisiert');
    els.overviewMissingWrap.hidden = !missing.length;
    if (missing.length) fillList(els.overviewMissing, missing, '');
  }

  function signalMatchesFilter(signal, filter) {
    if (filter === 'all') return true;
    if (filter === 'supports') return signal.direction === 'supports';
    if (filter === 'limits') return signal.direction === 'limits';
    if (filter === 'missing') return signal.freshnessState === 'missing';
    if (filter === 'stale') return signal.freshnessState === 'stale';
    return true;
  }

  function renderSignals(signals) {
    const filtered = signals.filter((item) => signalMatchesFilter(item, state.currentFilter));
    const visible = filtered;
    const selected = visible.find((item) => item.id === state.selectedSignalId) || filtered[0] || null;
    state.selectedSignalId = selected ? selected.id : null;

    els.signalsCount.textContent = 'Signale';
    els.signalsLine.textContent = `${signals.length} geprüft · ${signals.filter((item) => item.direction === 'limits').length} bremsen · ${signals.filter((item) => item.direction === 'supports').length} tragen · ${signals.filter((item) => item.direction !== 'limits' && item.direction !== 'supports').length} neutral`;

    clear(els.signalsList);
    visible.forEach((signal) => {
      const row = document.createElement('button');
      row.type = 'button';
      row.className = `core-signal-row ${signal.id === state.selectedSignalId ? 'is-active' : ''}`;
      row.innerHTML = `
        <span class="core-signal-main">
          <strong>${esc(shortSignalName(signal))}</strong>
          <small>${esc(humanSignalDescription(signal))}</small>
        </span>
        <span class="core-signal-value">${esc(compactSignalValue(signal))}</span>
        <span class="${badgeClass(signal.direction === 'neutral' && signal.freshnessState === 'stale' ? 'soft' : signal.direction)}">${esc(directionLabel(signal.direction === 'neutral' && signal.freshnessState === 'stale' ? 'missing' : signal.direction))}</span>
      `;
      row.addEventListener('click', () => {
        state.selectedSignalId = signal.id;
        renderSignals(signals);
      });
      els.signalsList.append(row);
    });

    if (!selected) {
      els.signalDetail.hidden = true;
      els.signalDetail.innerHTML = '';
      return;
    }

    els.signalDetail.hidden = false;
    els.signalDetail.innerHTML = `
      <div class="core-signal-detail-head">
        <strong>${esc(shortSignalName(selected))}</strong>
        <span>${esc(categoryLabel(selected.category))} · ${esc(impactLabel(selected.impact))} Einfluss</span>
      </div>
      <div class="core-signal-detail-grid">
        <div class="core-detail-cell"><span>Wert</span><strong>${esc(compactSignalValue(selected) || '—')}</strong></div>
        <div class="core-detail-cell"><span>Richtung</span><strong>${esc(directionLabel(selected.direction) || '—')}</strong></div>
        <div class="core-detail-cell"><span>Datenstand</span><strong>${esc(selected.freshnessDetail || freshnessLabel(selected.freshnessState) || '—')}</strong></div>
        <div class="core-detail-cell"><span>Einordnung</span><strong>${esc(humanSignalDescription(selected) || '—')}</strong></div>
      </div>
    `;
  }

  function fillDataStack(node, rows) {
    clear(node);
    rows.forEach((row) => {
      const item = document.createElement('div');
      item.className = 'core-data-item';
      item.innerHTML = `
        <div class="core-data-item-copy">
          <strong>${esc(row.label)}</strong>
          <span>${esc(row.value)}</span>
        </div>
        <span class="${badgeClass(row.tone || row.state || 'soft')}">${esc(row.status)}</span>
      `;
      node.append(item);
    });
  }

  function renderDataTab(payload, signals) {
    const sleepSignal = findSignal(signals, ['sleep_score', 'sleep', 'schlaf']);
    const hrvSignal = findSignal(signals, ['hrv']);
    const pulseSignal = findSignal(signals, ['rhr', 'resting', 'pulse']);
    const energySignal = findSignal(signals, ['energy']);
    const motivationSignal = findSignal(signals, ['motivation']);
    const painSignal = findSignal(signals, ['pain']);
    const calendarSignal = findSignal(signals, ['evening_load', 'calendar', 'alltag']);
    const proteinSignal = findSignal(signals, ['protein']);
    const carbSignal = findSignal(signals, ['carb']);
    const bodyState = freshnessStateFromPayload(payload, ['weight', 'hrv', 'resting_hr', 'recovery'], 'ok');
    const sleepState = freshnessStateFromPayload(payload, ['sleep_score', 'sleep_duration'], sleepSignal?.freshnessState || 'missing');
    const trainingState = freshnessStateFromPayload(payload, ['training', 'training_card'], payload?.ui?.training_card_ready ? 'ok' : 'missing');
    const checkinState = payload?.ui?.morning_checkin_done_today ? 'fresh' : 'missing';
    const calendarState = freshnessStateFromPayload(payload, ['calendar'], calendarSignal?.freshnessState || 'ok');
    const nutritionState = proteinSignal?.freshnessState || carbSignal?.freshnessState || 'missing';
    const trainingCardTitle = text(payload?.ui?.training_card?.title || payload?.ui?.training_card_title || '');
    const lastTrainingLog = trainingLogLabel(signals, payload);
    const sleepWindow = sleepWindowLabel(sleepSignal);
    const sleepDuration = sleepDurationLabel(sleepSignal);
    const sleepScore = sleepMainValue(sleepSignal);
    const calendarDetail = valueOrFallback(calendarSignal?.evidence || freshnessDetail(payload, ['calendar'], ''), 'fehlt');
    const proteinValue = compactSignalValue(proteinSignal || {}) || 'fehlt';
    const carbValue = valueOrFallback(carbSignal?.evidence || carbSignal?.headline, 'fehlt');

    fillDataStack(els.dataBody, [
      { label: 'Gewicht', value: freshnessDetail(payload, ['weight'], 'fehlt'), status: freshnessLabel(freshnessStateFromPayload(payload, ['weight'], bodyState)), tone: freshnessStateFromPayload(payload, ['weight'], bodyState) },
      { label: 'HRV', value: hrvValueLabel(hrvSignal) !== '—' ? hrvValueLabel(hrvSignal) : compactSignalValue(hrvSignal || {}) || 'fehlt', status: freshnessLabel(freshnessStateFromPayload(payload, ['hrv'], hrvSignal?.freshnessState || bodyState)), tone: freshnessStateFromPayload(payload, ['hrv'], hrvSignal?.freshnessState || bodyState) },
      { label: 'Ruhepuls', value: pulseValueLabel(pulseSignal) !== '—' ? pulseValueLabel(pulseSignal) : compactSignalValue(pulseSignal || {}) || 'fehlt', status: freshnessLabel(freshnessStateFromPayload(payload, ['resting_hr'], pulseSignal?.freshnessState || bodyState)), tone: freshnessStateFromPayload(payload, ['resting_hr'], pulseSignal?.freshnessState || bodyState) },
      { label: 'Recovery', value: compactSignalValue(findSignal(signals, ['recovery_conflict']) || {}) || 'fehlt', status: freshnessLabel(freshnessStateFromPayload(payload, ['recovery'], bodyState)), tone: freshnessStateFromPayload(payload, ['recovery'], bodyState) },
    ]);

    fillDataStack(els.dataSleep, [
      { label: 'Sleep Score', value: sleepScore !== '—' ? sleepScore : 'fehlt', status: freshnessLabel(sleepState), tone: sleepState },
      { label: 'Schlafdauer', value: sleepDuration !== '—' ? sleepDuration : 'fehlt', status: freshnessLabel(sleepState), tone: sleepState },
      { label: 'Schlaffenster', value: sleepWindow || 'fehlt', status: sleepWindow ? 'brauchbar' : 'fehlt', tone: sleepWindow ? 'soft' : 'missing' },
    ]);

    fillDataStack(els.dataTraining, [
      { label: 'Letzter Log', value: lastTrainingLog, status: freshnessLabel(freshnessStateFromPayload(payload, ['training'], trainingState)), tone: freshnessStateFromPayload(payload, ['training'], trainingState) },
      { label: 'Heutige Einheit', value: trainingCardTitle || 'noch offen', status: trainingCardTitle ? 'geplant' : 'fehlt', tone: trainingCardTitle ? 'soft' : 'missing' },
      { label: 'Trainingskarte', value: payload?.ui?.training_card_ready ? 'vorhanden' : 'fehlt', status: payload?.ui?.training_card_ready ? 'bereit' : 'fehlt', tone: payload?.ui?.training_card_ready ? 'good' : 'missing' },
    ]);

    fillDataStack(els.dataCheckin, [
      { label: 'Morning Check-in', value: payload?.ui?.morning_checkin_done_today ? 'vorhanden' : 'fehlt', status: freshnessLabel(checkinState), tone: checkinState },
      { label: 'Motivation', value: checkinFieldValue(motivationSignal), status: motivationSignal ? freshnessLabel(motivationSignal.freshnessState) : freshnessLabel(checkinState), tone: motivationSignal?.freshnessState || checkinState },
      { label: 'Energie', value: checkinFieldValue(energySignal), status: energySignal ? freshnessLabel(energySignal.freshnessState) : freshnessLabel(checkinState), tone: energySignal?.freshnessState || checkinState },
      { label: 'Schmerz', value: checkinFieldValue(painSignal), status: painSignal ? freshnessLabel(painSignal.freshnessState) : freshnessLabel(checkinState), tone: painSignal?.freshnessState || checkinState },
    ]);

    fillDataStack(els.dataCalendar, [
      { label: 'Termine', value: freshnessDetail(payload, ['calendar'], 'fehlt'), status: freshnessLabel(calendarState), tone: calendarState },
      { label: 'Nächster Abendtermin', value: calendarDetail, status: freshnessLabel(calendarState), tone: calendarState },
      { label: 'Abendlast', value: compactSignalValue(calendarSignal || {}) || 'fehlt', status: calendarSignal?.direction === 'limits' ? 'hoch' : calendarSignal ? 'brauchbar' : 'fehlt', tone: calendarSignal?.direction === 'limits' ? 'bad' : calendarSignal ? 'soft' : 'missing' },
    ]);

    fillDataStack(els.dataNutrition, [
      { label: 'Proteinstatus', value: proteinValue, status: proteinSignal ? freshnessLabel(proteinSignal.freshnessState) : 'fehlt', tone: proteinSignal?.freshnessState || 'missing' },
      { label: 'Carbs vor Training', value: carbValue, status: carbSignal ? freshnessLabel(carbSignal.freshnessState) : 'fehlt', tone: carbSignal?.freshnessState || 'missing' },
      { label: 'Tageslog', value: proteinSignal || carbSignal ? 'vorhanden' : 'fehlt', status: proteinSignal?.direction === 'limits' ? 'lückenhaft' : freshnessLabel(nutritionState), tone: proteinSignal?.direction === 'limits' ? 'caution' : nutritionState },
    ]);
  }

  function renderDetailsHeader(signals) {
    const titles = {
      overview: 'Heute im Überblick',
      signals: 'Signale',
      data: 'Datenlage',
      history: 'Verlauf',
    };
    els.detailsTitle.textContent = titles[state.activeTab] || 'Heute im Überblick';
    els.detailsSubline.textContent = {
      overview: 'Kurzfassung der heutigen Entscheidung.',
      signals: `${signals.length} geprüft · ${signals.filter((item) => item.direction === 'limits').length} bremsen · ${signals.filter((item) => item.direction === 'supports').length} tragen · ${signals.filter((item) => item.direction !== 'limits' && item.direction !== 'supports').length} neutral`,
      data: 'Welche Quellen CORE heute verwendet.',
      history: 'Entwicklung der letzten Tage.',
    }[state.activeTab] || 'Kurzfassung der heutigen Entscheidung.';
  }

  function setActiveTab(tab) {
    state.activeTab = tab;
    els.tabs.forEach((button) => {
      const active = button.dataset.tab === tab;
      button.classList.toggle('is-active', active);
      button.setAttribute('aria-selected', active ? 'true' : 'false');
    });
    els.panels.forEach((panel) => {
      const active = panel.dataset.panel === tab;
      panel.classList.toggle('is-active', active);
      panel.hidden = !active;
    });
    if (state.signals.length) renderDetailsHeader(state.signals);
  }

  async function loadHistory(payload) {
    const baseDate = text(payload?.date);
    if (!baseDate) return;
    const dates = [];
    for (let i = 0; i < 7; i += 1) {
      const dt = new Date(`${baseDate}T00:00:00`);
      dt.setDate(dt.getDate() - i);
      dates.push(dt.toISOString().slice(0, 10));
    }
    const results = await Promise.all(dates.map(async (day) => {
      try {
        const response = await fetch(withCore(`/api/core/board?date=${encodeURIComponent(day)}`), { credentials: 'same-origin' });
        if (!response.ok) return null;
        const data = await response.json();
        return data?.ok ? data : null;
      } catch (_error) {
        return null;
      }
    }));
    state.historyRows = results.filter(Boolean).map((row) => {
      const signals = normalizeSignals(row);
      return {
        date: row.date,
        mode: deriveMode(row, signals),
        confidence: parsePct(row?.ui?.confidence_pct) || 0,
        limits: signals.filter((item) => item.direction === 'limits').length,
        supports: signals.filter((item) => item.direction === 'supports').length,
      };
    }).reverse();
    state.historyLoaded = true;
    renderHistory();
  }

  function renderHistory() {
    const rows = state.historyRows || [];
    clear(els.historyChart);
    clear(els.historyList);
    if (!rows.length) {
      els.historyEmpty.hidden = false;
      els.historyCaption.textContent = 'Keine Historie geladen';
      return;
    }
    els.historyEmpty.hidden = true;
    els.historyCaption.textContent = 'Letzte 7 Tage';
    rows.forEach((row) => {
      const pill = document.createElement('div');
      pill.className = 'core-history-pill';
      pill.innerHTML = `<span>${esc(row.date.slice(5))}</span><strong>${esc(row.mode)}</strong>`;
      els.historyChart.append(pill);

      const item = document.createElement('div');
      item.className = 'core-history-row';
      item.innerHTML = `
        <strong>${esc(row.date)}</strong>
        <span>${esc(row.mode)}</span>
        <span>${esc(`${row.confidence} %`)}</span>
        <span>${esc(`${row.supports} trägt · ${row.limits} bremst`)}</span>
      `;
      els.historyList.append(item);
    });
  }

  function render(payload) {
    state.payload = payload;
    state.signals = normalizeSignals(payload);
    renderHeader(payload, state.signals);
    renderMetrics(payload, state.signals);
    renderOverview(payload, state.signals);
    renderSignals(state.signals);
    renderDataTab(payload, state.signals);
    renderDetailsHeader(state.signals);
    if (!state.historyLoaded) loadHistory(payload);
  }

  async function recompute() {
    if (!els.recompute) return;
    const old = els.recompute.textContent;
    els.recompute.disabled = true;
    els.recompute.textContent = 'Berechne…';
    try {
      const day = encodeURIComponent(text(state.payload?.date));
      const response = await fetch(withCore(`/api/core/board?date=${day}`), { credentials: 'same-origin', cache: 'no-store' });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const data = await response.json();
      if (data?.ok) {
        state.historyLoaded = false;
        state.historyRows = [];
        render(data);
      }
    } catch (error) {
      console.error('CORE konnte nicht neu berechnet werden', error);
    } finally {
      els.recompute.disabled = false;
      els.recompute.textContent = old;
    }
  }

  els.filterRow?.addEventListener('click', (event) => {
    const button = event.target.closest('[data-filter]');
    if (!button) return;
    state.currentFilter = button.dataset.filter || 'all';
    Array.from(els.filterRow.querySelectorAll('[data-filter]')).forEach((node) => node.classList.toggle('is-active', node === button));
    renderSignals(state.signals);
  });

  els.tabs.forEach((button) => {
    button.addEventListener('click', () => setActiveTab(button.dataset.tab || 'overview'));
  });

  els.recompute?.addEventListener('click', recompute);

  const payload = readBootstrap();
  if (!payload) return;
  setActiveTab('overview');
  render(payload);
})();
