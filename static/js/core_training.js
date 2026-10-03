(() => {
  const root = document.querySelector('.core-training-page');
  if (!root) return;

  const csrfToken = root.dataset.csrfToken || '';
  const stackEl = document.getElementById('core-card-stack');
  const liveStatsEl = document.getElementById('core-training-live-stats');
  const predictionLabelEl = document.getElementById('core-prediction-label');
  const predictionMarkerEl = document.getElementById('core-prediction-marker');
  const errorEl = document.getElementById('core-deck-error');
  const retryBtn = document.getElementById('core-deck-retry');
  const noBtn = document.getElementById('core-answer-no');
  const yesBtn = document.getElementById('core-answer-yes');
  const hintEl = document.getElementById('core-deck-hint');

  const state = {
    queue: [],
    answering: false,
    current: null,
    recentSignatures: [],
    pointer: null,
    loadedAt: 0,
    undoAvailable: false,
    menuOpen: false,
  };

  function authFetch(url, options = {}) {
    const opts = { ...options };
    const method = (opts.method || 'GET').toUpperCase();
    opts.headers = { ...(options.headers || {}) };
    if (method !== 'GET' && csrfToken) {
      opts.headers['X-CSRF-Token'] = csrfToken;
    }
    opts.credentials = opts.credentials || 'same-origin';
    return fetch(url, opts);
  }

  function setError(visible) {
    errorEl?.classList.toggle('is-hidden', !visible);
  }

  function rememberSignature(signature) {
    if (!signature) return;
    state.recentSignatures = [signature, ...state.recentSignatures.filter((item) => item !== signature)].slice(0, 24);
  }

  function queueSignatures() {
    return state.queue.map((entry) => String(entry?.card?.signature || '')).filter(Boolean);
  }

  function normalizePrompt(text) {
    return String(text || '').trim().toLowerCase().replace(/\s+/g, ' ');
  }

  function queuePromptKeys() {
    return state.queue
      .map((entry) => `${entry?.card?.meta?.topic || ''}::${normalizePrompt(entry?.card?.text)}`)
      .filter(Boolean);
  }

  function blockedSignatures(extra = []) {
    return [...new Set([...state.recentSignatures, ...queueSignatures(), ...extra.filter(Boolean)])].slice(0, 24);
  }

  function renderPrediction(prediction) {
    const answer = String(prediction?.answer || 'yes').toUpperCase();
    const confidence = Math.round(Number(prediction?.confidence || 0) * 100);
    const pos = Math.max(0, Math.min(1, Number(prediction?.bar_position || 0.5)));
    const scope = String(prediction?.scope || 'question_text');
    predictionLabelEl.textContent = `CORE zur Frage würde: ${answer} (${confidence}%)`;
    predictionLabelEl.title = scope.startsWith('question')
      ? 'Fragenbezogene CORE-Prognose'
      : 'CORE-Prognose';
    predictionMarkerEl.style.left = `${pos * 100}%`;
  }

  function renderStats(stats) {
    if (!liveStatsEl) return;
    liveStatsEl.textContent = String(stats?.summary || 'Noch keine Antworten · CORE baut gerade dein Profil');
  }

  function currentPrediction(fallback = null) {
    return state.queue[0]?.prediction || fallback || { answer: 'yes', confidence: 0, bar_position: 0.5 };
  }

  function syncVisibleCard(fallbackPrediction = null) {
    renderStack();
    renderPrediction(currentPrediction(fallbackPrediction));
  }

  function makeCardNode(entry, index) {
    const card = document.createElement('article');
    card.className = 'core-card';
    if (index === 1) card.classList.add('is-back-1');
    if (index >= 2) card.classList.add('is-back-2');
    card.dataset.cardId = entry.card.id;
    card.dataset.signature = entry.card.signature;
    card.dataset.answer = entry.prediction.answer;
    const menuMarkup = index === 0 ? `
      <div class="core-card-menu">
        <button type="button" class="core-card-menu-toggle" aria-label="Kartenaktionen öffnen" aria-expanded="false">⋯</button>
        <div class="core-card-menu-panel" hidden>
          <button type="button" class="core-card-menu-item" data-menu-action="undo" ${state.undoAvailable ? '' : 'disabled'}>Undo</button>
          <button type="button" class="core-card-menu-item is-danger" data-menu-action="delete">Karte löschen</button>
        </div>
      </div>
    ` : '';
    card.innerHTML = `
      <div class="core-card-pill">${entry.card.meta?.pill || 'CORE'}</div>
      ${menuMarkup}
      <h2>${entry.card.text || '—'}</h2>
    `;
    return card;
  }

  function closeCardMenu() {
    const menu = stackEl?.querySelector('.core-card-menu');
    if (!menu) return;
    const toggle = menu.querySelector('.core-card-menu-toggle');
    const panel = menu.querySelector('.core-card-menu-panel');
    menu.classList.remove('is-open');
    toggle?.setAttribute('aria-expanded', 'false');
    if (panel) panel.hidden = true;
    state.menuOpen = false;
  }

  function focusTopCard(options = {}) {
    const top = getTopCardNode();
    if (!top) return;
    if (document.activeElement === top) return;
    const shouldSkip = options.preserveControls && document.activeElement && (
      document.activeElement === retryBtn ||
      document.activeElement === noBtn ||
      document.activeElement === yesBtn ||
      document.activeElement?.classList?.contains('core-card-menu-toggle') ||
      document.activeElement?.classList?.contains('core-card-menu-item')
    );
    if (shouldSkip) return;
    window.requestAnimationFrame(() => {
      top.focus({ preventScroll: true });
    });
  }

  function bindTopCard() {
    const top = stackEl?.querySelector('.core-card:not(.is-back-1):not(.is-back-2)');
    if (!top) return;
    const menu = top.querySelector('.core-card-menu');
    const menuToggle = top.querySelector('.core-card-menu-toggle');
    const menuPanel = top.querySelector('.core-card-menu-panel');
    menu?.addEventListener('pointerdown', (ev) => ev.stopPropagation());
    menuToggle?.addEventListener('click', (ev) => {
      ev.preventDefault();
      ev.stopPropagation();
      const open = !menu?.classList.contains('is-open');
      if (!menu || !menuPanel) return;
      if (open) {
        menu.classList.add('is-open');
        menuToggle.setAttribute('aria-expanded', 'true');
        menuPanel.hidden = false;
        state.menuOpen = true;
      } else {
        closeCardMenu();
        focusTopCard();
      }
    });
    top.querySelectorAll('[data-menu-action]').forEach((button) => {
      button.addEventListener('click', (ev) => {
        ev.preventDefault();
        ev.stopPropagation();
        const action = button.dataset.menuAction;
        closeCardMenu();
        if (action === 'undo') triggerUndo();
        if (action === 'delete') triggerDeleteCurrentCard();
      });
    });
    top.addEventListener('pointerdown', onPointerDown);
    top.addEventListener('click', () => focusTopCard());
    top.addEventListener('blur', () => {
      window.setTimeout(() => focusTopCard(), 0);
    });
    top.tabIndex = -1;
  }

  function renderStack() {
    if (!stackEl) return;
    stackEl.innerHTML = '';
    state.queue.slice(0, 3).forEach((entry, index) => {
      stackEl.appendChild(makeCardNode(entry, index));
    });
    bindTopCard();
    focusTopCard({ preserveControls: true });
  }

  function addToQueueIfEligible(payload) {
    const promptKey = `${payload?.card?.meta?.topic || ''}::${normalizePrompt(payload?.card?.text)}`;
    if (!payload?.card) return false;
    if (
      queueSignatures().includes(payload.card.signature) ||
      state.recentSignatures.includes(payload.card.signature) ||
      queuePromptKeys().includes(promptKey)
    ) {
      return false;
    }
    state.queue.push(payload);
    return true;
  }

  function shiftWithPrefill(nextPayload) {
    const blockedBeforeShift = new Set(blockedSignatures());
    const promptKeysBeforeShift = new Set(queuePromptKeys());
    if (state.queue.length) {
      rememberSignature(state.queue[0]?.card?.signature);
      state.queue.shift();
    }
    const nextPromptKey = `${nextPayload?.card?.meta?.topic || ''}::${normalizePrompt(nextPayload?.card?.text)}`;
    if (
      nextPayload?.card &&
      !blockedBeforeShift.has(nextPayload.card.signature) &&
      !queueSignatures().includes(nextPayload.card.signature) &&
      !promptKeysBeforeShift.has(nextPromptKey)
    ) {
      state.queue.push(nextPayload);
    }
    syncVisibleCard(nextPayload?.prediction);
  }

  async function fetchNext(extraBlocked = []) {
    const qs = new URLSearchParams();
    const blocked = blockedSignatures(extraBlocked);
    if (blocked.length) qs.set('recent_signatures', blocked.join(','));
    const res = await authFetch(`/api/core_training/next${qs.toString() ? `?${qs}` : ''}`, { cache: 'no-store' });
    if (!res.ok) throw new Error('next_failed');
    return res.json();
  }

  async function ensureQueue() {
    let attempts = 0;
    const maxAttempts = 8;
    while (state.queue.length < 3 && attempts < maxAttempts) {
      attempts += 1;
      const payload = await fetchNext();
      const added = addToQueueIfEligible(payload);
      if (!added && state.queue.length === 0 && payload?.card) {
        state.queue.push(payload);
      }
      if (state.queue.length === 1) {
        syncVisibleCard();
      }
    }
    if (!state.queue.length) {
      throw new Error('queue_empty');
    }
    syncVisibleCard();
  }

  async function submit(answer, optimisticNode = null) {
    if (state.answering || !state.queue[0]) return;
    state.answering = true;
    root.classList.add('is-answering');
    setError(false);
    const current = state.queue[0];
    const startedAt = state.loadedAt || performance.now();
    const hadBufferedNext = state.queue.length > 1;
    if (optimisticNode) {
      optimisticNode.classList.add(answer === 'yes' ? 'is-fly-out-right' : 'is-fly-out-left');
    }
    if (hadBufferedNext) {
      rememberSignature(current.card.signature);
      state.queue.shift();
      syncVisibleCard();
      state.loadedAt = performance.now();
      ensureQueue().catch(() => setError(true));
    }
    try {
      const res = await authFetch('/api/core_training/answer', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          card_id: current.card.id,
          signature: current.card.signature,
          answer,
          client_ms: Math.max(0, Math.round(performance.now() - startedAt)),
          recent_signatures: blockedSignatures([current.card.signature]),
        }),
      });
      if (!res.ok) throw new Error('answer_failed');
      const payload = await res.json();
      state.undoAvailable = true;
      renderStats(payload.stats || payload.next?.stats);
      if (hadBufferedNext) {
        addToQueueIfEligible(payload.next);
        syncVisibleCard(payload.next?.prediction);
        ensureQueue().catch(() => setError(true));
      } else {
        shiftWithPrefill(payload.next);
        state.loadedAt = performance.now();
        ensureQueue().catch(() => setError(true));
      }
    } catch (err) {
      console.error(err);
      setError(true);
      syncVisibleCard();
    } finally {
      state.answering = false;
      root.classList.remove('is-answering');
    }
  }

  function onPointerDown(ev) {
    const node = ev.currentTarget;
    state.pointer = {
      id: ev.pointerId,
      node,
      startX: ev.clientX,
      startY: ev.clientY,
      dx: 0,
    };
    node.setPointerCapture?.(ev.pointerId);
    node.classList.add('is-dragging');
    node.classList.add('is-swiping');
    node.addEventListener('pointermove', onPointerMove);
    node.addEventListener('pointerup', onPointerUp);
    node.addEventListener('pointercancel', onPointerUp);
  }

  function onPointerMove(ev) {
    if (!state.pointer || ev.pointerId !== state.pointer.id) return;
    const dx = ev.clientX - state.pointer.startX;
    state.pointer.dx = dx;
    const rotate = Math.max(-10, Math.min(10, dx / 18));
    const strength = Math.max(0, Math.min(1, Math.abs(dx) / 140));
    state.pointer.node.classList.toggle('is-swipe-yes', dx > 10);
    state.pointer.node.classList.toggle('is-swipe-no', dx < -10);
    state.pointer.node.style.setProperty('--core-swipe-strength', strength.toFixed(3));
    state.pointer.node.style.transform = `translateX(${dx}px) rotate(${rotate}deg)`;
    if (hintEl) {
      hintEl.textContent = dx > 18 ? 'JA' : dx < -18 ? 'NEIN' : 'Links = Nein, rechts = Ja';
    }
    noBtn?.classList.toggle('is-live', dx < -18);
    yesBtn?.classList.toggle('is-live', dx > 18);
  }

  function onPointerUp(ev) {
    if (!state.pointer || ev.pointerId !== state.pointer.id) return;
    const { node, dx } = state.pointer;
    node.classList.remove('is-dragging');
    node.classList.remove('is-swiping');
    node.classList.remove('is-swipe-yes', 'is-swipe-no');
    node.style.removeProperty('--core-swipe-strength');
    node.removeEventListener('pointermove', onPointerMove);
    node.removeEventListener('pointerup', onPointerUp);
    node.removeEventListener('pointercancel', onPointerUp);
    state.pointer = null;
    if (hintEl) hintEl.textContent = 'Links = Nein, rechts = Ja';
    noBtn?.classList.remove('is-live');
    yesBtn?.classList.remove('is-live');
    if (Math.abs(dx) > 90) {
      submit(dx > 0 ? 'yes' : 'no', node);
      return;
    }
    node.style.transform = '';
  }

  function getTopCardNode() {
    return stackEl?.querySelector('.core-card:not(.is-back-1):not(.is-back-2)') || null;
  }

  function triggerKeyboardAnswer(answer) {
    if (state.answering || !state.queue[0]) return;
    const node = getTopCardNode();
    if (!node) {
      submit(answer);
      return;
    }
    const isYes = answer === 'yes';
    node.classList.add(isYes ? 'is-swipe-yes' : 'is-swipe-no');
    node.style.setProperty('--core-swipe-strength', '0.9');
    node.style.transform = isYes ? 'translateX(56px) rotate(5deg)' : 'translateX(-56px) rotate(-5deg)';
    if (hintEl) hintEl.textContent = isYes ? 'JA' : 'NEIN';
    noBtn?.classList.toggle('is-live', !isYes);
    yesBtn?.classList.toggle('is-live', isYes);
    window.setTimeout(() => {
      submit(answer, node);
      if (hintEl) hintEl.textContent = 'Links = Nein, rechts = Ja';
      noBtn?.classList.remove('is-live');
      yesBtn?.classList.remove('is-live');
    }, 70);
  }

  async function triggerUndo() {
    if (state.answering || !state.undoAvailable) return;
    state.answering = true;
    root.classList.add('is-answering');
    setError(false);
    try {
      const res = await authFetch('/api/core_training/undo', { method: 'POST' });
      if (!res.ok) throw new Error('undo_failed');
      const payload = await res.json();
      const restored = { card: payload.card, prediction: payload.prediction };
      if (!restored?.card?.text) throw new Error('undo_invalid_card');
      renderStats(payload.stats);
      state.recentSignatures = state.recentSignatures.filter((item) => item !== restored.card.signature);
      state.queue = [restored, ...state.queue.filter((entry) => entry?.card?.signature !== restored.card.signature)].slice(0, 3);
      state.undoAvailable = false;
      syncVisibleCard(restored.prediction);
      state.loadedAt = performance.now();
      ensureQueue().catch(() => setError(true));
    } catch (err) {
      console.error(err);
      setError(true);
    } finally {
      state.answering = false;
      root.classList.remove('is-answering');
    }
  }

  async function triggerDeleteCurrentCard() {
    const current = state.queue[0];
    if (state.answering || !current?.card?.signature) return;
    state.answering = true;
    root.classList.add('is-answering');
    setError(false);
    try {
      const res = await authFetch('/api/core_training/delete', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ signature: current.card.signature }),
      });
      if (!res.ok) throw new Error('delete_failed');
      const payload = await res.json();
      renderStats(payload.stats);
      state.recentSignatures = state.recentSignatures.filter((item) => item !== current.card.signature);
      state.queue = state.queue.slice(1);
      syncVisibleCard();
      await ensureQueue();
      state.loadedAt = performance.now();
    } catch (err) {
      console.error(err);
      setError(true);
    } finally {
      state.answering = false;
      root.classList.remove('is-answering');
      focusTopCard();
    }
  }

  document.addEventListener('keydown', (ev) => {
    if (ev.target && ['INPUT', 'TEXTAREA'].includes(ev.target.tagName)) return;
    if (ev.key === 'Escape' && state.menuOpen) {
      closeCardMenu();
      focusTopCard();
      return;
    }
    if (ev.key === 'ArrowLeft' || ev.key === 'a' || ev.key === 'A') triggerKeyboardAnswer('no');
    if (ev.key === 'ArrowRight' || ev.key === 'd' || ev.key === 'D') triggerKeyboardAnswer('yes');
  });

  noBtn?.addEventListener('click', () => submit('no'));
  yesBtn?.addEventListener('click', () => submit('yes'));
  retryBtn?.addEventListener('click', () => ensureQueue().then(() => setError(false)).catch(() => setError(true)));

  stackEl?.addEventListener('click', () => focusTopCard());
  document.addEventListener('click', (ev) => {
    if (!state.menuOpen) return;
    const menu = stackEl?.querySelector('.core-card-menu');
    if (menu && !menu.contains(ev.target)) {
      closeCardMenu();
      focusTopCard({ preserveControls: true });
    }
  });
  window.addEventListener('focus', () => focusTopCard({ preserveControls: true }));
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible') focusTopCard({ preserveControls: true });
  });

  ensureQueue().then(() => {
    state.loadedAt = performance.now();
    renderStats(state.queue[0]?.stats || state.queue[0]?.card?.stats || null);
    renderPrediction(currentPrediction());
    focusTopCard();
  }).catch((err) => {
    console.error(err);
    setError(true);
  });
})();
