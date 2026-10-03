'use strict';

const parserDebounceMs = 200;
const tableDebounceMs = 150;
const MOBILE_QUERY = window.LIVA?.mobileQuery || "(max-width: 640px)";
const isMobile = () => (window.LIVA?.isMobile ? window.LIVA.isMobile() : window.matchMedia(MOBILE_QUERY).matches);

const trainingState = {
    currentWorkoutId: null,
    currentProgress: null,
    draft: {
        session: {
            date_iso: '',
            session_name: '',
            plan_id: null,
            notes: '',
            raw_import_text: '',
        },
        exercises: [],
    },
    planContext: null,
    validation: null,
    activePlanId: null,
    thresholds: {
        top_threshold_pct: 0.05,
        top_threshold_abs: 5,
    },
    lastExposureCache: new Map(),
    lastExposureInflight: new Map(),
    validationRequestId: 0,
    lastSlotRequestId: 0,
    lastSlotKey: '',
    lastSlotExerciseTimers: new Map(),
    lastSlotExerciseRequestId: new Map(),
    lastSlotExerciseKey: new Map(),
    variationRequestId: 0,
    parseAutocomplete: {
        key: '',
        suggestion: '',
        lineStart: 0,
        lineEnd: 0,
    },
    timers: {
        parser: null,
        table: null,
        lastSlot: null,
        variation: null,
        parseAutocomplete: null,
    }
};

const refs = {};
const mobileWorkoutCache = new Map();
const historyHydration = {
    timer: null,
    request: null,
};

const draftStore = {
    get state() {
        return trainingState.draft;
    },
    setSessionField(field, value) {
        if (!trainingState.draft.session) trainingState.draft.session = {};
        trainingState.draft.session[field] = value;
    },
    setRawPaste(value) {
        draftStore.setSessionField('raw_import_text', value || '');
    },
    setExerciseName(index, value) {
        const ex = draftStore.ensureExercise(index);
        ex.name = value || '';
    },
    setExerciseVariation(index, value, opts = {}) {
        const ex = draftStore.ensureExercise(index);
        const prev = (ex.variation || '').trim();
        const next = (value || '').trim();
        ex.variation = next;
        if (opts.userInput) {
            ex.variation_user_locked = next.length > 0;
            if (!next) {
                ex.variation_user_locked = false;
                ex.variation_auto_value = null;
            }
        }
        if (opts.auto) {
            ex.variation_auto_value = next || ex.variation_auto_value || null;
            ex.variation_meta = opts.meta || ex.variation_meta || null;
        }
        if (prev !== next) {
            if (window.__TRAINING_DEBUG_VARIATION__) {
                console.debug('[VAR-CHANGED]', index, { prev, next });
            }
            if (next) {
                refreshExerciseDerived(index);
            } else {
                clearLastRefsForExercise(index);
                refreshAmpelForExercise(index);
            }
        }
    },
    setExerciseNotes(index, value) {
        const ex = draftStore.ensureExercise(index);
        ex.notes = value || '';
    },
    removeExercise(index) {
        if (!trainingState.draft.exercises) return;
        if (index < 0 || index >= trainingState.draft.exercises.length) return;
        trainingState.draft.exercises.splice(index, 1);
        if (trainingState.draft.exercises.length === 0) {
            trainingState.draft.exercises.push({ name: '', variation: '', notes: '', sets: [{ reps: null, weight: null, rpe: null }] });
        }
    },
    setSetField(exIndex, setIndex, field, value) {
        const ex = draftStore.ensureExercise(exIndex);
        const set = draftStore.ensureSet(ex, setIndex);
        set[field] = value;
    },
    ensureSet(ex, setIndex) {
        if (!ex.sets) ex.sets = [];
        while (ex.sets.length <= setIndex) {
            ex.sets.push({ reps: null, weight: null, rpe: null });
        }
        if (!ex.sets[setIndex]) ex.sets[setIndex] = { reps: null, weight: null, rpe: null };
        return ex.sets[setIndex];
    },
    removeSet(exIndex, setIndex) {
        const ex = draftStore.ensureExercise(exIndex);
        if (!ex.sets || ex.sets.length <= setIndex) return;
        ex.sets.splice(setIndex, 1);
        if (ex.sets.length === 0) {
            ex.sets.push({ reps: null, weight: null, rpe: null });
        }
    },
    applyParsedDraft(nextDraft) {
        if (!nextDraft) return;
        const raw = trainingState.draft.session?.raw_import_text || '';
        const prevDraft = trainingState.draft;
        trainingState.draft = normalizeDraft(nextDraft);
        preserveTransientFields(prevDraft, trainingState.draft);
        if (!trainingState.draft.session) trainingState.draft.session = {};
        trainingState.draft.session.raw_import_text = raw;
    },
    ensureExercise(index) {
        if (!trainingState.draft.exercises) trainingState.draft.exercises = [];
        while (trainingState.draft.exercises.length <= index) {
            trainingState.draft.exercises.push({
                name: '',
                variation: '',
                notes: '',
                sets: [{ reps: null, weight: null, rpe: null }],
                variation_user_locked: false,
                variation_auto_value: null,
                variation_meta: null,
                variation_resolve_key: '',
            });
        }
        if (!trainingState.draft.exercises[index].sets) {
            trainingState.draft.exercises[index].sets = [{ reps: null, weight: null, rpe: null }];
        }
        if (trainingState.draft.exercises[index].variation_user_locked == null) {
            trainingState.draft.exercises[index].variation_user_locked = false;
        }
        if (trainingState.draft.exercises[index].variation_auto_value == null) {
            trainingState.draft.exercises[index].variation_auto_value = null;
        }
        if (trainingState.draft.exercises[index].variation_meta == null) {
            trainingState.draft.exercises[index].variation_meta = null;
        }
        if (trainingState.draft.exercises[index].variation_resolve_key == null) {
            trainingState.draft.exercises[index].variation_resolve_key = '';
        }
        return trainingState.draft.exercises[index];
    }
};

function normalizeDraft(draft) {
    if (!draft) return draft;
    const next = { ...draft };
    next.session = next.session || {};
    next.exercises = (next.exercises || []).map(ex => ({
        ...ex,
        name: ex.name || ex.parsed_label || '',
        variation: ex.variation || '',
        notes: ex.notes || '',
        sets: ex.sets || [{ reps: null, weight: null, rpe: null }],
        variation_user_locked: ex.variation_user_locked || false,
        variation_auto_value: ex.variation_auto_value || null,
        variation_meta: ex.variation_meta || null,
        variation_resolve_key: ex.variation_resolve_key || '',
    }));
    return next;
}

function preserveTransientFields(prevDraft, nextDraft) {
    if (!prevDraft || !nextDraft) return;
    const prevExercises = prevDraft.exercises || [];
    const nextExercises = nextDraft.exercises || [];
    nextExercises.forEach((ex, exIdx) => {
        const prevEx = prevExercises[exIdx];
        if (!prevEx) return;
        if (prevEx.variation_meta && !ex.variation_meta) {
            ex.variation_meta = prevEx.variation_meta;
        }
        const prevSets = prevEx.sets || [];
        const nextSets = ex.sets || [];
        nextSets.forEach((set, setIdx) => {
            const prevSet = prevSets[setIdx];
            if (!prevSet) return;
            ['last_ref', 'last_reason', '_last_ref', 'last_status', 'last_reason_code', 'last_reason_text'].forEach((field) => {
                if (set[field] == null && prevSet[field] != null) set[field] = prevSet[field];
            });
        });
    });
}

function $(selector) {
    return document.querySelector(selector);
}

function escapeHtml(value) {
    return String(value ?? '')
        .replaceAll('&', '&amp;')
        .replaceAll('<', '&lt;')
        .replaceAll('>', '&gt;')
        .replaceAll('"', '&quot;')
        .replaceAll("'", '&#039;');
}

function createElement(tag, className, attrs = {}) {
    const el = document.createElement(tag);
    if (className) el.className = className;
    Object.entries(attrs).forEach(([key, value]) => {
        el.setAttribute(key, value);
    });
    return el;
}

function initTraining() {
    refs.exerciseList = $('#exercise-list');
    refs.addExerciseBtn = $('#add-exercise-btn');
    refs.saveSessionBtn = $('#save-session-btn');
    refs.sessionSets = $('#session-sets');
    refs.sessionLoad = $('#session-load');
    refs.saveStatus = $('#save-status');
    refs.validationToggle = $('#validation-toggle');
    refs.validationList = $('#validation-list');
    refs.validationCount = $('#validation-count');
    refs.summaryExercises = $('#summary-exercises');
    refs.summarySets = $('#summary-sets');
    refs.planCoverage = $('#plan-coverage');
    refs.parserTextarea = $('#parser-input');
    refs.parserErrors = $('#parser-errors');
    refs.parserStatus = $('#parser-status');
    refs.parserPreviewBody = $('#parser-preview-body');
    refs.planLabel = $('#plan-label');
    refs.planCoverageWrap = $('#plan-coverage-wrap');
    refs.planCoverageDivider = $('#plan-coverage-divider');
    refs.sessionNameInput = $('#session-name');
    refs.sessionDateInput = $('#session-date');
    refs.sessionDateDisplay = $('#session-date-display');
    refs.newSessionBtn = $('#new-session-btn');
    refs.historyList = document.querySelector('.history-list');
    refs.workspace = $('#training-workspace');
    refs.mobileModeButtons = Array.from(document.querySelectorAll('[data-training-mobile-mode]'));
    const params = new URLSearchParams(window.location.search || '');
    const deepLinkRaw = params.get('workout_id') || params.get('workout');
    const deepLinkWorkoutId = deepLinkRaw ? parseInt(deepLinkRaw, 10) : null;

    bindEvents();
    setMobileTrainingMode('session', { scroll: false });
    syncSessionDateDisplay();
    window.addEventListener('resize', autoResizeParser);
    hydrateHistoryFromSnapshot();
    syncDraftSessionFromInputs();
    ensureInitialCard();
    syncParserTextarea(trainingState.draft.session.raw_import_text);
    resolveActivePlan();
    queueValidation({ fromParser: false });
    if (Number.isFinite(deepLinkWorkoutId) && deepLinkWorkoutId > 0) {
        loadWorkoutById(deepLinkWorkoutId);
    }
}

function setHistoryStatus(text) {
    if (!refs.historyList) return;
    let status = refs.historyList.querySelector('.history-list-status');
    if (!text) {
        if (status) status.remove();
        return;
    }
    if (!status) {
        status = createElement('div', 'history-list-status muted', { role: 'status' });
        refs.historyList.appendChild(status);
    }
    status.textContent = text;
}

function renderHistorySnapshot(workouts) {
    if (!refs.historyList || !Array.isArray(workouts) || !workouts.length) return false;
    if (refs.historyList.querySelector('.workout-card')) return true;
    setHistoryStatus('');
    const fragment = document.createDocumentFragment();
    workouts.forEach((workout) => fragment.appendChild(createHistoryCard(workout)));
    refs.historyList.appendChild(fragment);
    sortHistoryCardsByDate();
    return true;
}

function hydrateHistoryFromSnapshot(attempt = 0) {
    if (!refs.historyList || refs.historyList.querySelector('.workout-card')) {
        setHistoryStatus('');
        return Promise.resolve(true);
    }
    if (historyHydration.request) return historyHydration.request;

    setHistoryStatus('Einheiten werden geladen…');
    historyHydration.request = fetch('/api/training_snapshot', {
        headers: { Accept: 'application/json' },
        cache: 'no-store',
    })
        .then((response) => {
            if (!response.ok) throw new Error(`Training snapshot ${response.status}`);
            return response.json();
        })
        .then((payload) => {
            if (renderHistorySnapshot(payload?.workouts || [])) return true;
            const isBuilding = Boolean(payload?.loading || payload?.partial || payload?.refresh_triggered);
            if (isBuilding && attempt < 8) {
                const delay = Math.min(2000, 250 * (2 ** attempt));
                clearTimeout(historyHydration.timer);
                historyHydration.timer = window.setTimeout(() => hydrateHistoryFromSnapshot(attempt + 1), delay);
                return false;
            }
            setHistoryStatus('Noch keine Einheiten vorhanden.');
            return false;
        })
        .catch(() => {
            if (attempt < 3) {
                clearTimeout(historyHydration.timer);
                historyHydration.timer = window.setTimeout(() => hydrateHistoryFromSnapshot(attempt + 1), 500 * (attempt + 1));
            } else {
                setHistoryStatus('Einheiten konnten nicht geladen werden.');
            }
            return false;
        })
        .finally(() => {
            historyHydration.request = null;
        });
    return historyHydration.request;
}

function bindEvents() {
    refs.mobileModeButtons?.forEach((button) => {
        button.addEventListener('click', () => {
            setMobileTrainingMode(button.dataset.trainingMobileMode || 'session');
        });
    });

    if (refs.addExerciseBtn) {
        refs.addExerciseBtn.addEventListener('click', () => {
            appendExerciseCard();
            queueValidation({ fromParser: false });
            scheduleVariationResolve();
        });
    }

    if (refs.saveSessionBtn) {
        refs.saveSessionBtn.addEventListener('click', saveSession);
    }

    if (refs.validationToggle) {
        refs.validationToggle.addEventListener('click', () => {
            refs.validationList.classList.toggle('is-open');
        });
    }

    if (refs.parserTextarea) {
        refs.parserTextarea.addEventListener('input', () => {
            const value = refs.parserTextarea.value || '';
            draftStore.setRawPaste(value);
            autoResizeParser();
            queueValidation({ fromParser: true });
            scheduleParseAutocomplete();
        });
        refs.parserTextarea.addEventListener('keydown', (e) => {
            if (e.key !== 'Tab') return;
            e.preventDefault();
            handleParsingTab(e);
        });
        refs.parserTextarea.addEventListener('focus', () => {
            refs.parserTextarea.classList.add('is-focused');
        });
        refs.parserTextarea.addEventListener('blur', () => {
            refs.parserTextarea.classList.remove('is-focused');
        });
    }

    if (refs.newSessionBtn) {
        refs.newSessionBtn.addEventListener('click', () => {
            startNewSessionDraft();
            setMobileTrainingMode('session');
        });
    }

    if (refs.sessionNameInput) {
        refs.sessionNameInput.addEventListener('input', () => {
            syncDraftSessionFromInputs();
            resolveActivePlan();
            queueValidation({ fromParser: false });
            scheduleLastSlotRefs();
            scheduleVariationResolve();
        });
    }
    if (refs.sessionDateInput) {
        refs.sessionDateInput.addEventListener('change', () => {
            syncSessionDateDisplay();
            syncDraftSessionFromInputs();
            resolveActivePlan();
            queueValidation({ fromParser: false });
            scheduleLastSlotRefs();
            scheduleVariationResolve();
        });
    }

    if (refs.historyList) {
        refs.historyList.addEventListener('click', (event) => {
            if (event.target.closest('.delete-btn')) return;
            const card = event.target.closest('.workout-card');
            if (!card) return;
            const id = parseInt(card.dataset.id || '', 10);
            if (!Number.isFinite(id)) return;
            handleWorkoutClick(id, event);
            if (isMobile()) setMobileTrainingMode('session');
        });
    }
}

function setMobileTrainingMode(mode, options = {}) {
    if (!refs.workspace) return;
    const allowed = new Set(['session', 'parser', 'history']);
    const nextMode = allowed.has(mode) ? mode : 'session';
    refs.workspace.dataset.mobilePanel = nextMode;
    refs.mobileModeButtons?.forEach((button) => {
        const active = button.dataset.trainingMobileMode === nextMode;
        button.classList.toggle('is-active', active);
        button.setAttribute('aria-selected', active ? 'true' : 'false');
        button.tabIndex = active ? 0 : -1;
    });
    if (options.scroll !== false && isMobile()) {
        const top = refs.workspace.getBoundingClientRect().top + window.scrollY - 8;
        window.scrollTo({ top: Math.max(0, top), behavior: 'smooth' });
    }
    if (nextMode === 'parser' && isMobile()) {
        window.setTimeout(() => refs.parserTextarea?.focus({ preventScroll: true }), 180);
    }
}

function formatGermanDate(isoDate) {
    const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(isoDate || '');
    return match ? `${match[3]}.${match[2]}.${match[1]}` : 'Datum wählen';
}

function syncSessionDateDisplay() {
    if (!refs.sessionDateDisplay) return;
    refs.sessionDateDisplay.textContent = formatGermanDate(refs.sessionDateInput?.value || '');
}

function startNewSessionDraft() {
    trainingState.currentWorkoutId = null;
    trainingState.currentProgress = null;
    if (refs.sessionNameInput) refs.sessionNameInput.value = '';
    if (refs.parserTextarea) refs.parserTextarea.value = '';
    document.querySelectorAll('.workout-card.open, .workout-card.is-active').forEach((card) => {
        card.classList.remove('open', 'is-active');
    });

    trainingState.draft = {
        session: {
            date_iso: refs.sessionDateInput ? refs.sessionDateInput.value : '',
            session_name: '',
            plan_id: trainingState.activePlanId,
            notes: '',
            raw_import_text: '',
        },
        exercises: [{ sets: [{ reps: null, weight: null, rpe: null }] }],
    };
    trainingState.draft = normalizeDraft(trainingState.draft);
    if (refs.exerciseList) refs.exerciseList.innerHTML = '';
    renderDraftExercises();
    syncParserTextarea('');
    resolveActivePlan();
    queueValidation({ fromParser: false });
    scheduleLastSlotRefs();
    scheduleVariationResolve();
    if (window.TrainingWorkspace?.setNewSessionView) {
        window.TrainingWorkspace.setNewSessionView();
    }
}

function scheduleParseAutocomplete() {
    clearTimeout(trainingState.timers.parseAutocomplete);
    trainingState.timers.parseAutocomplete = setTimeout(() => {
        updateParseAutocomplete();
    }, 200);
}

function normalizeAutocompleteText(value) {
    return (value || '')
        .toLowerCase()
        .replace(/ä/g, 'ae')
        .replace(/ö/g, 'oe')
        .replace(/ü/g, 'ue')
        .replace(/ß/g, 'ss')
        .normalize('NFD')
        .replace(/[\u0300-\u036f]/g, '')
        .trim();
}

function getExerciseAutocompleteSuggestion(prefix) {
    const list = window.exerciseNameSuggestions || [];
    const qRaw = (prefix || '').trim();
    const q = normalizeAutocompleteText(qRaw);
    if (!q || q.length < 3) return '';
    let best = '';
    let bestScore = Infinity;
    for (const name of list) {
        if (!name) continue;
        const normalized = normalizeAutocompleteText(name);
        if (!normalized) continue;

        let score = Infinity;
        if (normalized === q) {
            score = 0;
        } else if (normalized.startsWith(q)) {
            score = 100 + (normalized.length - q.length);
        } else {
            const tokens = normalized.split(/[^a-z0-9]+/).filter(Boolean);
            const tokenIdx = tokens.findIndex((token) => token.startsWith(q));
            if (tokenIdx >= 0) {
                score = 300 + tokenIdx * 50 + (normalized.length - q.length);
            }
        }
        if (!Number.isFinite(score)) continue;
        if (score < bestScore) {
            bestScore = score;
            best = name;
        }
    }
    return best;
}

function getCurrentLineInfo(text, cursor) {
    const lineStart = text.lastIndexOf('\n', cursor - 1) + 1;
    const lineEnd = (() => {
        const idx = text.indexOf('\n', cursor);
        return idx === -1 ? text.length : idx;
    })();
    const lineText = text.slice(lineStart, lineEnd);
    return { lineStart, lineEnd, lineText };
}

function isNameLine(lineText) {
    const trim = (lineText || '').trim();
    if (!trim) return false;
    if (/[0-9]/.test(trim)) return false;
    if (/[x@]/i.test(trim)) return false;
    return true;
}

function isSessionHeaderLine(lineText) {
    const text = (lineText || '').trim();
    if (!text) return false;
    return /^.+\s-\s(\d{4}-\d{2}-\d{2}|\d{1,2}[./-]\d{1,2}[./-]\d{2,4})$/.test(text);
}

function updateParseAutocomplete() {
    if (!refs.parserTextarea) return;
    const ta = refs.parserTextarea;
    const value = ta.value || '';
    const cursor = ta.selectionStart || 0;
    const { lineStart, lineEnd, lineText } = getCurrentLineInfo(value, cursor);
    if (!isNameLine(lineText)) {
        trainingState.parseAutocomplete = { key: '', suggestion: '', lineStart: 0, lineEnd: 0 };
        return;
    }
    const key = `${lineStart}:${lineEnd}:${lineText.trim().toLowerCase()}`;
    if (trainingState.parseAutocomplete.key === key) return;
    const suggestion = getExerciseAutocompleteSuggestion(lineText.trim());
    trainingState.parseAutocomplete = {
        key,
        suggestion: suggestion || '',
        lineStart,
        lineEnd,
    };
}

function handleParsingTab(event) {
    if (!refs.parserTextarea) return;
    const ta = refs.parserTextarea;
    const value = ta.value || '';
    const start = ta.selectionStart || 0;
    const end = ta.selectionEnd || 0;
    if (start !== end) return;

    if (event.shiftKey) {
        event.preventDefault();
        const nextValue = value.slice(0, start) + ' ()' + value.slice(end);
        ta.value = nextValue;
        const newPos = start + 2; // place caret inside the parentheses
        ta.selectionStart = newPos;
        ta.selectionEnd = newPos;
        draftStore.setRawPaste(nextValue);
        autoResizeParser();
        queueValidation({ fromParser: true });
        scheduleParseAutocomplete();
        return;
    }

    const { lineStart, lineEnd, lineText } = getCurrentLineInfo(value, start);
    const lineTrim = lineText.trim();
    if (isSessionHeaderLine(lineText)) {
        const nextValue = value.slice(0, start) + '\n\n' + value.slice(end);
        ta.value = nextValue;
        const newPos = start + 2;
        ta.selectionStart = newPos;
        ta.selectionEnd = newPos;
        draftStore.setRawPaste(nextValue);
        autoResizeParser();
        queueValidation({ fromParser: true });
        scheduleParseAutocomplete();
        return;
    }
    const nameLine = isNameLine(lineText);
    let insert = '';

    if (nameLine) {
        const suggestion = getExerciseAutocompleteSuggestion(lineTrim) || trainingState.parseAutocomplete?.suggestion || '';
        if (suggestion && suggestion.trim().toLowerCase() !== lineTrim.toLowerCase()) {
            const nextValue = value.slice(0, lineStart) + suggestion + value.slice(lineEnd);
            ta.value = nextValue;
            const newPos = lineStart + suggestion.length;
            ta.selectionStart = newPos;
            ta.selectionEnd = newPos;
            draftStore.setRawPaste(nextValue);
            autoResizeParser();
            queueValidation({ fromParser: true });
            scheduleParseAutocomplete();
            return;
        }
        insert = '\n';
    } else {
        // token boundaries around cursor
        const left = value.slice(0, start);
        const right = value.slice(start);
        const leftBoundary = Math.max(left.lastIndexOf(' '), left.lastIndexOf('\n'), left.lastIndexOf('\t'));
        const rightBoundarySpace = right.search(/[ \n\t]/);
        const tokenStart = leftBoundary === -1 ? 0 : leftBoundary + 1;
        const tokenEnd = rightBoundarySpace === -1 ? value.length : start + rightBoundarySpace;
        const token = value.slice(tokenStart, tokenEnd);

        const repsOnly = /^\d+$/.test(token);
        const hasX = /x/i.test(token);
        const hasRpeSeparator = /[@;]/.test(token);
        const weightComplete = /^\d+x[+-]?\d+([.,]\d+)?$/i.test(token);
        const rpeComplete = /^\d+x[+-]?\d+([.,]\d+)?[@;]\d+([.,]\d+)?$/i.test(token);

        if (repsOnly) {
            insert = 'x';
        } else if (!hasRpeSeparator && weightComplete) {
            insert = '@';
        } else if (rpeComplete) {
            insert = '\n\n';
        } else {
            insert = ' ';
        }
    }

    if (!insert) return;
    const nextValue = value.slice(0, start) + insert + value.slice(end);
    ta.value = nextValue;
    const newPos = start + insert.length;
    ta.selectionStart = newPos;
    ta.selectionEnd = newPos;
    draftStore.setRawPaste(nextValue);
    autoResizeParser();
    queueValidation({ fromParser: true });
    scheduleParseAutocomplete();
}

function ensureInitialCard() {
    if (!trainingState.draft.exercises.length) {
        trainingState.draft.exercises.push({ sets: [{ reps: null, weight: null, rpe: null }] });
    }
    if (refs.exerciseList && refs.exerciseList.childElementCount === 0) {
        renderDraftExercises();
    }
    syncParserTextarea(trainingState.draft.session.raw_import_text);
    autoResizeParser();
    renderParserPreview(trainingState.draft);
}

function resolveActivePlan() {
    const dateIso = trainingState.draft?.session?.date_iso || '';
    const sessionName = trainingState.draft?.session?.session_name || '';
    if (!dateIso) {
        trainingState.activePlanId = null;
        trainingState.planContext = null;
        updatePlanUI(null);
        return;
    }
    const qs = new URLSearchParams({ date: dateIso, session_name: sessionName });
    fetch(`/api/plans/active?${qs.toString()}`)
        .then(r => r.json())
        .then(payload => {
            if (!payload || !payload.plan_id) {
                trainingState.activePlanId = null;
                trainingState.planContext = null;
                trainingState.draft.session.plan_id = null;
                updatePlanUI(null);
                return;
            }
            trainingState.activePlanId = payload.plan_id;
            trainingState.planContext = payload.plan_context || null;
            trainingState.draft.session.plan_id = payload.plan_id;
            updatePlanUI(payload);
        })
        .catch(() => updatePlanUI(null));
}

function updatePlanUI(payload) {
    if (!refs.planLabel) return;
    if (!payload || !payload.plan_id) {
        refs.planLabel.textContent = 'Off';
        refs.planLabel.classList.add('is-muted');
        document.getElementById('training-ui')?.classList.add('plan-disabled');
    } else {
        refs.planLabel.textContent = payload.plan_name || 'Plan';
        refs.planLabel.classList.remove('is-muted');
        document.getElementById('training-ui')?.classList.remove('plan-disabled');
    }
}

function appendExerciseCard(data = {}) {
    const card = createExerciseCard(data, refs.exerciseList.childElementCount);
    refs.exerciseList.appendChild(card);
    card.scrollIntoView({ behavior: 'smooth', block: 'center' });
}

function createExerciseCard(data, index) {
    const card = createElement('div', 'exercise-card', { 'data-testid': 'training-exercise-row' });
    const exerciseIndex = Number.isFinite(index) ? index : refs.exerciseList.childElementCount;
    card.dataset.exerciseIndex = exerciseIndex;
    card.dataset.confirmed = data.meta?.confirmed_variation ? '1' : '0';

    const header = createElement('div', 'exercise-header');
    const left = createElement('div', 'exercise-header-left');
    const right = createElement('div', 'exercise-header-right');

    const nameInput = createElement('input', 'exercise-name-input', { type: 'text', placeholder: 'Bench / RDLs / ...' });
    const variationInput = createElement('input', 'exercise-variation-input', { type: 'text', placeholder: 'Variation' });
    let displayName = resolveExerciseName(data);
    if (!displayName && hasExerciseData(data)) displayName = 'Übung';
    const variationLabel = resolveVariationLabel(data);
    nameInput.value = displayName || '';
    variationInput.value = variationLabel || '';

    const titleRow = createElement('div', 'exercise-title-row');
    const nameWrap = createElement('div', 'exercise-title');
    const variationWrap = createElement('div', 'exercise-variation');
    const variationHint = createElement('span', 'exercise-variation-hint hidden');
    nameWrap.appendChild(nameInput);
    variationWrap.appendChild(variationInput);
    variationWrap.appendChild(variationHint);
    titleRow.appendChild(nameWrap);
    titleRow.appendChild(variationWrap);
    left.appendChild(titleRow);

    const metaRow = createElement('div', 'exercise-meta');
    left.appendChild(metaRow);

    const stats = createElement('div', 'exercise-header-stats');
    stats.innerHTML = '<span class="exercise-chip">Sets <span class="exercise-sets-count">0</span></span><span class="exercise-chip">Load <span class="exercise-load-total">0 kg</span></span>';
    const confirmBtn = createElement('button', 'confirm-variation-btn hidden');
    confirmBtn.type = 'button';
    confirmBtn.textContent = 'Confirm Variation';
    const delBtn = createElement('button', 'exercise-delete-btn');
    delBtn.textContent = '✕';
    delBtn.type = 'button';
    delBtn.addEventListener('click', (e) => {
        e.stopPropagation();
        card.remove();
        const idx = parseInt(card.dataset.exerciseIndex, 10) || 0;
        draftStore.removeExercise(idx);
        renumberCards();
        queueValidation({ fromParser: false });
    });

    right.appendChild(stats);
    right.appendChild(confirmBtn);
    right.appendChild(delBtn);

    header.appendChild(left);
    header.appendChild(right);
    card.appendChild(header);

    const grid = createElement('div', 'exercise-grid');
    const leftCol = createElement('div', 'exercise-left-col');
    const setsContainer = createElement('div', 'sets-container');
    const setsColumnHead = createElement('div', 'sets-column-head', { 'aria-hidden': 'true' });
    setsColumnHead.innerHTML = '<span></span><span>Reps</span><span>kg</span><span>RPE</span><span>Typ</span><span>Vorher</span><span></span>';

    const footer = createElement('div', 'exercise-footer');
    const addSetBtn = createElement('button', 'add-set-btn');
    addSetBtn.type = 'button';
    addSetBtn.textContent = '+ Satz';
    footer.appendChild(addSetBtn);

    leftCol.appendChild(setsColumnHead);
    leftCol.appendChild(setsContainer);
    leftCol.appendChild(footer);

    const rightCol = createElement('div', 'exercise-right-col');
    const notesArea = createElement('textarea', 'exercise-notes', { placeholder: 'Notiz…' });
    notesArea.value = data.notes || '';
    rightCol.appendChild(notesArea);

    grid.appendChild(leftCol);
    grid.appendChild(rightCol);
    card.appendChild(grid);

    const setData = data.sets || [];
    if (setData.length === 0) setData.push({ reps: null, weight: null, rpe: null });
    setData.forEach((setItem, idx) => {
        setsContainer.appendChild(createSetRow(card, idx + 1, setItem));
    });

    addSetBtn.addEventListener('click', () => {
        setsContainer.appendChild(createSetRow(card, setsContainer.childElementCount + 1, {}));
        updateSessionTotals();
        const idx = parseInt(card.dataset.exerciseIndex, 10) || 0;
        const setIndex = Math.max(setsContainer.childElementCount - 1, 0);
        draftStore.setSetField(idx, setIndex, 'reps', null);
        draftStore.setSetField(idx, setIndex, 'weight', null);
        draftStore.setSetField(idx, setIndex, 'rpe', null);
        queueValidation({ fromParser: false });
        scheduleLastSlotRefs();
    });

    nameInput.addEventListener('input', () => {
        const idx = parseInt(card.dataset.exerciseIndex, 10) || 0;
        draftStore.setExerciseName(idx, nameInput.value);
        queueValidation({ fromParser: false });
        scheduleVariationResolve();
        if ((trainingState.draft.exercises?.[idx]?.variation || '').trim()) {
            refreshExerciseDerived(idx);
        }
    });
    variationInput.addEventListener('input', () => {
        const idx = parseInt(card.dataset.exerciseIndex, 10) || 0;
        draftStore.setExerciseVariation(idx, variationInput.value, { userInput: true });
        queueValidation({ fromParser: false });
    });
    notesArea.addEventListener('input', () => {
        const idx = parseInt(card.dataset.exerciseIndex, 10) || 0;
        draftStore.setExerciseNotes(idx, notesArea.value);
        queueValidation({ fromParser: false });
    });

    confirmBtn.addEventListener('click', () => {
        card.dataset.confirmed = '1';
        queueValidation({ fromParser: false });
    });

    return card;
}

function createSetRow(card, index, setItem) {
    const row = createElement('div', 'set-row');
    row.dataset.setIndex = index;
    const getExerciseIndex = () => parseInt(card.dataset.exerciseIndex, 10) || 0;

    const idx = createElement('span', 'set-index');
    idx.textContent = index;

    const repsInput = createElement('input', 'set-input set-reps-input', { type: 'number', placeholder: 'Reps', 'aria-label': 'Wiederholungen' });
    repsInput.value = setItem.reps == null ? '' : setItem.reps;
    const weightInput = createElement('input', 'set-input set-weight-input', { type: 'number', step: '0.5', placeholder: 'kg', 'aria-label': 'Gewicht in Kilogramm' });
    weightInput.value = setItem.weight == null ? '' : setItem.weight;
    const rpeInput = createElement('input', 'set-input set-rpe-input', { type: 'number', step: '0.5', placeholder: 'RPE', 'aria-label': 'RPE' });
    rpeInput.value = setItem.rpe == null ? '' : setItem.rpe;

    const setField = (label, input) => {
        const field = createElement('label', 'set-field');
        const caption = createElement('span', 'set-field-label');
        caption.textContent = label;
        field.appendChild(caption);
        field.appendChild(input);
        return field;
    };

    const labelWrap = createElement('div', 'set-label-wrap');
    const tbLabel = createElement('span', 'top-backoff-label');
    const warnIcon = createElement('span', 'set-warning-icon hidden');
    warnIcon.textContent = '!';
    labelWrap.appendChild(tbLabel);
    labelWrap.appendChild(warnIcon);

    // Keep the comparison track mounted even when there is no reference so
    // the delete action never slides into the "Vorher" column.
    const setInfo = createElement('span', 'set-last-info is-empty');
    setInfo.textContent = '';

    const delBtn = createElement('button', 'set-delete-btn');
    delBtn.type = 'button';
    delBtn.textContent = '✕';
    delBtn.addEventListener('click', () => {
        row.remove();
        const currentIndex = Math.max(parseInt(row.dataset.setIndex, 10) || 1, 1) - 1;
        draftStore.removeSet(getExerciseIndex(), currentIndex);
        renumberSets(card);
        updateSessionTotals();
        queueValidation({ fromParser: false });
    });

    [repsInput, weightInput, rpeInput].forEach(input => {
        input.addEventListener('input', () => {
            const reps = parseInt(repsInput.value, 10);
            const weight = parseFloat((weightInput.value || '').replace(',', '.'));
            const rpe = parseFloat((rpeInput.value || '').replace(',', '.'));
            const currentIndex = Math.max(parseInt(row.dataset.setIndex, 10) || 1, 1) - 1;
            const exIndex = getExerciseIndex();
            draftStore.setSetField(exIndex, currentIndex, 'reps', Number.isFinite(reps) ? reps : null);
            draftStore.setSetField(exIndex, currentIndex, 'weight', Number.isFinite(weight) ? weight : null);
            draftStore.setSetField(exIndex, currentIndex, 'rpe', Number.isFinite(rpe) ? rpe : null);
            updateSessionTotals();
            queueValidation({ fromParser: false });
        });
        input.addEventListener('keydown', (e) => {
            if (e.key === 'Enter' && input === rpeInput) {
                e.preventDefault();
                const setsContainer = card.querySelector('.sets-container');
                const newRow = createSetRow(card, setsContainer.childElementCount + 1, {});
                setsContainer.appendChild(newRow);
                const target = newRow.querySelector('.set-reps-input');
                if (target) target.focus();
                const newIndex = Math.max(setsContainer.childElementCount - 1, 0);
                const exIndex = getExerciseIndex();
                draftStore.setSetField(exIndex, newIndex, 'reps', null);
                draftStore.setSetField(exIndex, newIndex, 'weight', null);
                draftStore.setSetField(exIndex, newIndex, 'rpe', null);
                queueValidation({ fromParser: false });
            }
        });
    });

    row.appendChild(idx);
    row.appendChild(setField('Reps', repsInput));
    row.appendChild(setField('kg', weightInput));
    row.appendChild(setField('RPE', rpeInput));
    row.appendChild(labelWrap);
    row.appendChild(setInfo);
    row.appendChild(delBtn);
    return row;
}

function renumberSets(card) {
    card.querySelectorAll('.set-row').forEach((row, idx) => {
        row.dataset.setIndex = idx + 1;
        row.querySelector('.set-index').textContent = idx + 1;
    });
}

function renumberCards() {
    refs.exerciseList.querySelectorAll('.exercise-card').forEach((card, idx) => {
        card.dataset.exerciseIndex = idx;
    });
}

function gatherSessionInfo() {
    const sessionId = refs.sessionDateInput ? refs.sessionDateInput.value : '';
    const sessionName = refs.sessionNameInput ? refs.sessionNameInput.value.trim() : '';
    return {
        date_iso: sessionId,
        session_name: sessionName,
        plan_id: trainingState.activePlanId,
    };
}


function queueValidation({ fromParser }) {
    const timerKey = fromParser ? 'parser' : 'table';
    clearTimeout(trainingState.timers[timerKey]);
    trainingState.timers[timerKey] = setTimeout(() => {
        runValidation({ fromParser });
    }, fromParser ? parserDebounceMs : tableDebounceMs);
}

function runValidation({ fromParser }) {
    const requestId = ++trainingState.validationRequestId;
    syncDraftSessionFromInputs();
    const payload = {
        session: gatherSessionInfo(),
        thresholds: trainingState.thresholds,
    };
    if (fromParser) {
        payload.raw_text = refs.parserTextarea.value;
    } else {
        payload.draft = { exercises: trainingState.draft.exercises || [] };
    }

    fetch('/api/training/validate_draft', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
    })
        .then(resp => resp.json())
        .then(result => {
            if (requestId !== trainingState.validationRequestId) return;
            if (!result || typeof result !== 'object' || !result.draft || !result.validation) {
                updateSaveStatus('Validierung im Testmodus nicht verfuegbar.');
                return;
            }
            trainingState.planContext = result.plan_context;
            trainingState.validation = result.validation;
            applyValidationResult(result, { fromParser });
        })
        .catch(err => {
            console.error('Validation failed', err);
        });
}

function applyValidationResult(result, { fromParser }) {
    if (fromParser && result.draft) {
        draftStore.applyParsedDraft(ensureParsedLabels(result.draft));
        const parsedSession = result.draft.session || {};
        if (refs.sessionNameInput && parsedSession.session_name) {
            refs.sessionNameInput.value = parsedSession.session_name;
        }
        if (refs.sessionDateInput && parsedSession.date_iso) {
            refs.sessionDateInput.value = parsedSession.date_iso;
            syncSessionDateDisplay();
        }
        syncDraftSessionFromInputs();
        resolveActivePlan();
        const rawText = refs.parserTextarea ? refs.parserTextarea.value : '';
        draftStore.setRawPaste(rawText || trainingState.draft.session.raw_import_text || '');
    } else if (result.draft && trainingState.draft) {
        trainingState.draft = mergeDraftWithMeta(trainingState.draft, result.draft);
    }
    updateTopSummary(result.draft.summary, result.plan_coverage, result.validation);
    updateValidationList(result.validation);
    updateParserStatus(result.draft, result.validation);
    updateParserIssues(result.validation);
    renderParserPreview(trainingState.draft);
    if (fromParser) {
        renderDraftExercises();
    }
    (trainingState.draft.exercises || []).forEach((_, idx) => markExerciseDirty(idx));
    syncParserTextarea(trainingState.draft.session.raw_import_text || '');
    updateSessionTotals(result.draft.summary);
    updateSaveStatus('');
    scheduleLastSlotRefs();
    scheduleVariationResolve();
}

function updateCardsFromMeta(exercises) {
    refs.exerciseList.querySelectorAll('.exercise-card').forEach((card, idx) => {
        updateExerciseCardFromDraft(idx);
    });
}

function updateExerciseCardFromDraft(exIdx) {
    const exercises = trainingState.draft.exercises || [];
    const ex = exercises[exIdx];
    const card = refs.exerciseList.querySelector(`.exercise-card[data-exercise-index="${exIdx}"]`);
    if (!ex || !card) return;
    const planEnabled = !!trainingState.activePlanId;
    const meta = ex.meta || {};
    const setsMeta = ex.sets || [];
    card.dataset.confirmed = meta.confirmed_variation ? '1' : '0';
    const resolvedName = resolveExerciseName(ex) || (hasExerciseData(ex) ? 'Übung' : '');
    const resolvedVariation = resolveVariationLabel(ex);
    if (!resolvedName) return;

    const nameInput = card.querySelector('.exercise-name-input');
    const variationInput = card.querySelector('.exercise-variation-input');
    if (nameInput && document.activeElement !== nameInput) {
        nameInput.value = resolvedName || '';
    }
    if (variationInput && document.activeElement !== variationInput) {
        variationInput.value = resolvedVariation || '';
    }
    const hint = card.querySelector('.exercise-variation-hint');
    const metaHint = ex.variation_meta?.note;
    if (hint) {
        if (metaHint) {
            hint.textContent = metaHint;
            hint.classList.remove('hidden');
        } else {
            hint.textContent = '';
            hint.classList.add('hidden');
        }
    }
    const variationWrap = card.querySelector('.exercise-variation');
    if (variationWrap) variationWrap.classList.remove('hidden');
    const confirmBtn = card.querySelector('.confirm-variation-btn');
    if (planEnabled && meta.requires_confirmation && !meta.confirmed_variation) {
        confirmBtn.classList.remove('hidden');
    } else {
        confirmBtn.classList.add('hidden');
    }

        const currentSets = (ex.sets || []).map(set => ({
            reps: toNumber(set.reps),
            weight: toNumber(set.weight),
            rpe: toNumber(set.rpe),
            progression_excluded: Boolean(set.progression_excluded),
            progression_exclusion_reason: set.progression_exclusion_reason || '',
        }));

        card.querySelectorAll('.set-row').forEach((row, setIdx) => {
            const setMeta = setsMeta[setIdx] || {};
            const label = row.querySelector('.top-backoff-label');
            if (label) label.textContent = setMeta.is_top ? 'T' : setMeta.is_backoff ? 'B' : '';
            const warnIcon = row.querySelector('.set-warning-icon');
            if (warnIcon) {
                if (setMeta.warning) {
                    warnIcon.classList.remove('hidden');
                } else {
                    warnIcon.classList.add('hidden');
                }
            }

            const setData = currentSets[setIdx] || {};
            const progressionExcluded = Boolean(setMeta.progression_excluded || setData.progression_excluded);
            const exclusionReason = setMeta.progression_exclusion_reason || setData.progression_exclusion_reason || '';
            const lastRef = progressionExcluded ? null : (setMeta.last_ref || setMeta._last_ref || null);
            const lastInfo = row.querySelector('.set-last-info');
            if (lastInfo) {
                let lastText = '';
                if (lastRef) {
                    lastText = formatLastRef(lastRef);
                    setMeta._last_ref = lastRef;
                }
                lastInfo.textContent = lastText;
                lastInfo.classList.toggle('is-empty', !lastText);
            }

            const comparisonStatus = progressionExcluded ? null : (setMeta.last_status || setData.last_status || null);
            const comparisonReason = setMeta.last_reason_text || setData.last_reason_text || setMeta.last_reason_code || setData.last_reason_code || setMeta.last_reason || setData.last_reason || '';
            const mappedStatus = ['improved', 'progress'].includes(comparisonStatus)
                ? 'green'
                : ['worse', 'regress'].includes(comparisonStatus)
                    ? 'red'
                    : 'neutral';
            const status = progressionExcluded ? 'neutral' : (comparisonStatus ? mappedStatus : 'neutral');
            const reason = progressionExcluded ? exclusionReason : comparisonReason;

        row.classList.remove('status-green', 'status-red', 'status-neutral', 'status-excluded');
        row.classList.add(`status-${status}`);
        row.classList.toggle('status-excluded', progressionExcluded);
        row.title = reason || '';
    });

    const cardTotals = computeCardTotals(card);
    const setsEl = card.querySelector('.exercise-sets-count');
    const loadEl = card.querySelector('.exercise-load-total');
    if (setsEl) setsEl.textContent = cardTotals.sets.toString();
    if (loadEl) loadEl.textContent = `${cardTotals.load} kg`;
}

function markExerciseDirty(exIdx) {
    if (trainingState.uiDirtyExercises == null) trainingState.uiDirtyExercises = new Set();
    trainingState.uiDirtyExercises.add(exIdx);
    clearTimeout(trainingState.uiDirtyTimer);
    trainingState.uiDirtyTimer = setTimeout(() => {
        const pending = Array.from(trainingState.uiDirtyExercises);
        trainingState.uiDirtyExercises.clear();
        pending.forEach(updateExerciseCardFromDraft);
    }, 16);
}

function computeCardTotals(card) {
    let sets = 0;
    let load = 0;
    card.querySelectorAll('.set-row').forEach(row => {
        const reps = parseFloat(row.querySelector('.set-reps-input')?.value || '');
        const weight = parseFloat((row.querySelector('.set-weight-input')?.value || '').replace(',', '.'));
        if (Number.isFinite(reps) && Number.isFinite(weight)) {
            sets += 1;
            load += reps * weight;
        }
    });
    return { sets, load: Math.round(load * 10) / 10 };
}
function updateTopSummary(summary = {}, coverage = { logged: 0, planned: 0 }, validation = { errors: [], warnings: [] }) {
    if (refs.summaryExercises) refs.summaryExercises.textContent = summary.exercise_count || 0;
    if (refs.summarySets) refs.summarySets.textContent = summary.set_count || 0;
    const planEnabled = !!trainingState.activePlanId;
    if (refs.planCoverage) refs.planCoverage.textContent = `${coverage.logged}/${coverage.planned}`;
    if (refs.planCoverageWrap) refs.planCoverageWrap.classList.toggle('hidden', !planEnabled);
    if (refs.planCoverageDivider) refs.planCoverageDivider.classList.toggle('hidden', !planEnabled);
    const totalIssues = (validation.errors?.length || 0) + (validation.warnings?.length || 0);
    if (refs.validationCount) refs.validationCount.textContent = totalIssues.toString();
    if (refs.validationToggle) refs.validationToggle.classList.toggle('hidden', totalIssues === 0);
    if (totalIssues === 0 && refs.validationList) refs.validationList.classList.remove('is-open');
}

function updateSessionTotals(summary = {}) {
    const computed = computeTotalsFromDraft();
    // The editable draft is the UI source of truth. Async validation responses
    // can contain an older summary while a freshly parsed draft is already on
    // screen, which previously reset the set KPI while parsing was still in flight.
    const sets = computed.sets;
    if (refs.sessionSets) refs.sessionSets.textContent = sets || 0;
    if (refs.sessionLoad) refs.sessionLoad.textContent = formatHistoryProgressionQuote({ progress: trainingState.currentProgress });
}

function computeTotalsFromDraft() {
    if (!trainingState.draft || !trainingState.draft.exercises) return { sets: 0, load: 0 };
    let sets = 0;
    let load = 0;
    trainingState.draft.exercises.forEach(ex => {
        (ex.sets || []).forEach(set => {
            const reps = toNumber(set.reps);
            const weight = toNumber(set.weight);
            if (reps != null && weight != null) {
                sets += 1;
                load += reps * weight;
            }
        });
    });
    return { sets, load: Math.round(load * 10) / 10 };
}

function updateValidationList(validation) {
    refs.validationList.innerHTML = '';
    if (!validation || !validation.warnings) return;
    validation.warnings.forEach((warn, idx) => {
        const item = createElement('button', 'validation-item', { type: 'button' });
        item.textContent = `${idx + 1}. ${warn.message}`;
        item.addEventListener('click', () => {
            if (warn.exercise_index != null) scrollToExercise(warn.exercise_index);
        });
        refs.validationList.appendChild(item);
    });
}

function updateParserStatus(draft, validation) {
    if (!refs.parserStatus) return;
    const exercises = draft?.summary?.exercise_count || 0;
    const sets = draft?.summary?.set_count || 0;
    const errors = validation?.errors?.length || 0;
    refs.parserStatus.textContent = `${exercises} Übungen · ${sets} Sets erkannt · ${errors} Fehler`;
}

function updateParserIssues(validation) {
    if (!refs.parserErrors) return;
    const parserIsEmpty = !String(refs.parserTextarea?.value || '').trim();
    const draftIsEmpty = !(trainingState.draft?.exercises || []).some(hasExerciseData);
    if (parserIsEmpty && draftIsEmpty) {
        refs.parserErrors.replaceChildren();
        refs.parserErrors.classList.remove('has-issues');
        return;
    }
    const issues = [...(validation?.errors || []), ...(validation?.warnings || [])]
        .map((issue) => issue?.message || issue?.detail || String(issue || ''))
        .filter(Boolean)
        .slice(0, 5);
    refs.parserErrors.replaceChildren();
    issues.forEach((message) => {
        const item = createElement('div', 'parser-issue');
        item.textContent = message;
        refs.parserErrors.appendChild(item);
    });
    refs.parserErrors.classList.toggle('has-issues', issues.length > 0);
}

function renderParserPreview(draft) {
    if (!refs.parserPreviewBody) return;
    const exercises = Array.isArray(draft?.exercises) ? draft.exercises.filter(hasExerciseData) : [];
    if (!exercises.length) {
        refs.parserPreviewBody.innerHTML = '<div class="parser-preview-empty">Noch kein Parsing.</div>';
        return;
    }
    refs.parserPreviewBody.innerHTML = exercises.map((ex, idx) => {
        const name = resolveExerciseName(ex) || `Übung ${idx + 1}`;
        const variation = resolveVariationLabel(ex);
        const setLine = (Array.isArray(ex.sets) ? ex.sets : []).map((set) => {
            const reps = toNumber(set?.reps);
            const weight = toNumber(set?.weight);
            const rpe = toNumber(set?.rpe);
            let text = '';
            if (reps != null && weight != null) text = `${reps}x${String(weight).replace('.', ',')}`;
            else if (reps != null) text = `${reps} reps`;
            else if (weight != null) text = `${String(weight).replace('.', ',')} kg`;
            if (rpe != null) text = `${text}@${String(rpe).replace('.', ',')}`;
            return text || 'offen';
        }).join(' · ');
        return `
            <article class="parser-preview-item">
                <div class="parser-preview-title">${escapeHtml(name)}${variation ? `<span class="parser-preview-variation"> · ${escapeHtml(variation)}</span>` : ''}</div>
                <div class="parser-preview-sets">${escapeHtml(setLine)}</div>
            </article>
        `;
    }).join('');
}

function syncParserTextarea(text) {
    if (!refs.parserTextarea) return;
    if (document.activeElement === refs.parserTextarea) return;
    refs.parserTextarea.value = text || '';
    autoResizeParser();
}

function formatLoadedParserText(text) {
    const source = String(text || '').replace(/\r\n?/g, '\n').trim();
    if (!source) return '';

    const compactLines = [];
    source.split('\n').forEach((line) => {
        const cleaned = line.replace(/\s+$/g, '');
        if (!cleaned.trim()) {
            if (compactLines.length && compactLines[compactLines.length - 1] !== '') compactLines.push('');
            return;
        }
        compactLines.push(cleaned);
    });

    const formatted = [];
    compactLines.forEach((line, index) => {
        const trimmed = line.trim();
        if (!trimmed) return;
        const isSessionTitle = index === 0 && isSessionHeaderLine(trimmed);
        const isExerciseTitle = /:$/.test(trimmed) && isNameLine(trimmed.slice(0, -1));

        if (isExerciseTitle && formatted.length && formatted[formatted.length - 1] !== '') {
            formatted.push('');
        }
        formatted.push(line);
        if (isSessionTitle) formatted.push('');
    });

    return formatted.join('\n').replace(/\n{3,}/g, '\n\n').trim();
}

function autoResizeParser() {
    if (!refs.parserTextarea) return;
    const isPhone = window.matchMedia('(max-width: 720px)').matches;
    const isDesktopRail = window.matchMedia('(min-width: 1300px)').matches;
    const minHeight = isPhone ? 260 : (isDesktopRail ? 168 : 146);
    const maxHeight = isPhone ? Math.max(260, Math.round(window.innerHeight * 0.54)) : (isDesktopRail ? 430 : 220);

    refs.parserTextarea.style.setProperty('height', 'auto', 'important');
    const contentHeight = refs.parserTextarea.scrollHeight;
    const nextHeight = Math.max(minHeight, Math.min(contentHeight, maxHeight));
    refs.parserTextarea.style.setProperty('height', `${nextHeight}px`, 'important');
    refs.parserTextarea.style.setProperty('overflow-y', contentHeight > maxHeight ? 'auto' : 'hidden', 'important');
}

function syncDraftSessionFromInputs() {
    if (!trainingState.draft || !trainingState.draft.session) return;
    draftStore.setSessionField('date_iso', refs.sessionDateInput ? refs.sessionDateInput.value : '');
    draftStore.setSessionField('session_name', refs.sessionNameInput ? refs.sessionNameInput.value.trim() : '');
    draftStore.setSessionField('plan_id', trainingState.activePlanId);
}


function renderDraftExercises() {
    if (!refs.exerciseList) return;
    refs.exerciseList.innerHTML = '';
    (trainingState.draft.exercises || []).forEach((ex, idx) => {
        const card = createExerciseCard(ex, idx);
        refs.exerciseList.appendChild(card);
        // Loaded workouts already carry their previous-set comparison. Apply it
        // in the same render pass instead of waiting for a follow-up request.
        updateExerciseCardFromDraft(idx);
    });
}

function mergeDraftWithMeta(currentDraft, validatedDraft) {
    const merged = { ...currentDraft, session: { ...currentDraft.session } };
    const rawImportText = currentDraft.session?.raw_import_text || '';
    merged.exercises = (currentDraft.exercises || []).map((ex, idx) => {
        const src = (validatedDraft.exercises || [])[idx] || {};
        const mergedSets = (ex.sets || []).map((set, setIdx) => {
            const srcSet = (src.sets || [])[setIdx] || {};
            return { ...set, ...srcSet };
        });
        const parsedLabel = ex.parsed_label || src.parsed_label || src.name || ex.name || '';
        return { ...ex, ...src, parsed_label: parsedLabel, sets: mergedSets };
    });
    merged.session.raw_import_text = rawImportText;
    return merged;
}

function resolveExerciseName(ex) {
    if (!ex) return '';
    return ex.display_name || ex.name || ex.parsed_label || ex.parsed_label_text || '';
}

function resolveVariationLabel(ex) {
    if (!ex) return '';
    return ex.variation_label || ex.variation || '';
}

function ensureParsedLabels(draft) {
    if (!draft || !draft.exercises) return draft;
    const next = { ...draft };
    next.exercises = draft.exercises.map(ex => ({
        ...ex,
        parsed_label: ex.parsed_label || ex.name || '',
        name: ex.name || ex.parsed_label || '',
        variation_user_locked: ex.variation_user_locked || false,
        variation_auto_value: ex.variation_auto_value || null,
        variation_meta: ex.variation_meta || null,
        variation_resolve_key: ex.variation_resolve_key || '',
    }));
    return next;
}

function hasExerciseData(ex) {
    if (!ex) return false;
    if (resolveExerciseName(ex) || ex.variation || ex.notes) return true;
    const sets = ex.sets || [];
    return sets.some(set => set && (set.reps || set.weight || set.rpe));
}

function planVariationForName(name) {
    if (!name || !trainingState.planContext) return '';
    const normalized = name.trim().toLowerCase();
    const match = (trainingState.planContext.exercises || []).find(entry => {
        const entryName = (entry.exercise_name || '').trim().toLowerCase();
        return entryName === normalized;
    });
    return match ? (match.variation || '') : '';
}

function formatLastRef(lastRef) {
    if (!lastRef) return '';
    const reps = lastRef.reps != null ? lastRef.reps : '';
    const weight = lastRef.weight != null ? lastRef.weight : '';
    const rpe = lastRef.rpe != null ? lastRef.rpe : '';
    let text = '';
    if (reps && weight) {
        text = `${reps}x${weight}`;
    } else if (weight) {
        text = `${weight}kg`;
    } else if (reps) {
        text = `${reps} reps`;
    }
    if (rpe) text = `${text}@${rpe}`;
    return text;
}

function getLastExposureSnapshot(name, variation, before, sessionName) {
    if (!name || !before) return null;
    const key = `${name.toLowerCase()}|${(variation || '').toLowerCase()}|${before}|${(sessionName || '').toLowerCase()}`;
    if (trainingState.lastExposureCache.has(key)) {
        return trainingState.lastExposureCache.get(key);
    }
    if (!trainingState.lastExposureInflight.has(key)) {
        const qs = new URLSearchParams({ name, variation: variation || '', before, session_name: sessionName || '' });
        const fetchPromise = fetch(`/api/training/last_exposure?${qs.toString()}`)
            .then(r => r.json())
            .then(payload => {
                if (payload && payload.ok) {
                    trainingState.lastExposureCache.set(key, payload);
                }
                trainingState.lastExposureInflight.delete(key);
            })
            .catch(() => trainingState.lastExposureInflight.delete(key));
        trainingState.lastExposureInflight.set(key, fetchPromise);
    }
    return null;
}

function scheduleLastSlotRefs() {
    clearTimeout(trainingState.timers.lastSlot);
    trainingState.timers.lastSlot = setTimeout(() => {
        fetchLastSlotRefs();
    }, 300);
}

function buildLastSlotPayload() {
    const session = trainingState.draft?.session || {};
    const dateIso = session.date_iso || '';
    const sessionName = session.session_name || '';
    const exercises = (trainingState.draft?.exercises || []).map((ex, exIdx) => {
        const exName = (ex.name || ex.parsed_label || '').trim();
        const sets = (ex.sets || []).map((set, setIdx) => ({
            set_index: setIdx,
            set_number: set?.set_number ?? (setIdx + 1),
            weight: set?.weight ?? null,
            reps: set?.reps ?? null,
            rpe: set?.rpe ?? null,
            role: set?.is_top ? 'T' : set?.is_backoff ? 'B' : '',
            variation: (ex.variation || '').trim(),
            progression_excluded: Boolean(set?.progression_excluded),
            progression_exclusion_reason: set?.progression_exclusion_reason || null,
        }));
        return {
            exercise_index: exIdx,
            exercise_name: exName,
            variation: (ex.variation || '').trim(),
            sets,
        };
    });
    return { date_iso: dateIso, session_name: sessionName, exercises };
}

function clearLastSlotRefs() {
    (trainingState.draft?.exercises || []).forEach(ex => {
        (ex.sets || []).forEach(set => {
            set.last_ref = null;
            set.last_reason = null;
            set.last_status = null;
            set.last_reason_code = null;
            set.last_reason_text = null;
        });
    });
}

function fetchLastSlotRefs() {
    const payload = buildLastSlotPayload();
    if (!payload.date_iso) {
        clearLastSlotRefs();
        (trainingState.draft.exercises || []).forEach((_, idx) => markExerciseDirty(idx));
        return;
    }
    const key = JSON.stringify(payload);
    if (key === trainingState.lastSlotKey) return;
    trainingState.lastSlotKey = key;
    const requestId = ++trainingState.lastSlotRequestId;
    fetch('/api/training/last-slot-references', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
    })
        .then(r => r.json())
        .then(result => {
            if (requestId !== trainingState.lastSlotRequestId) return;
            if (key !== JSON.stringify(buildLastSlotPayload())) return;
            clearLastSlotRefs();
            const touched = new Set();
            (result?.refs || []).forEach(entry => {
                const exIdx = entry.exercise_index;
                const setIdx = entry.set_index;
                if (exIdx == null || setIdx == null) return;
                const ex = trainingState.draft.exercises?.[exIdx];
                if (!ex || !ex.sets || !ex.sets[setIdx]) return;
                ex.sets[setIdx].last_ref = entry.ref || null;
                ex.sets[setIdx].last_reason = entry.reason || null;
                ex.sets[setIdx].last_status = entry.status || null;
                ex.sets[setIdx].last_reason_code = entry.reason_code || null;
                ex.sets[setIdx].last_reason_text = entry.reason_text || null;
                ex.sets[setIdx].progression_excluded = Boolean(entry.progression_excluded || ex.sets[setIdx].progression_excluded);
                ex.sets[setIdx].progression_exclusion_reason = entry.progression_exclusion_reason || ex.sets[setIdx].progression_exclusion_reason || null;
                touched.add(exIdx);
            });
            touched.forEach(idx => markExerciseDirty(idx));
        })
        // Keep the comparison embedded in the primary workout response when a
        // background refresh fails. A transient request must not blank the UI.
        .catch(() => {});
}

function clearLastRefsForExercise(exIdx) {
    const ex = trainingState.draft?.exercises?.[exIdx];
    if (!ex) return;
    (ex.sets || []).forEach(set => {
        set.last_ref = null;
        set.last_reason = null;
    });
}

function refreshExerciseDerived(exIdx) {
    refreshLastSlotRefsForExercise(exIdx);
    refreshAmpelForExercise(exIdx);
}

function refreshLastSlotRefsForExercise(exIdx) {
    const ex = trainingState.draft?.exercises?.[exIdx];
    if (!ex) return;
    const dateIso = trainingState.draft?.session?.date_iso || '';
    const sessionName = trainingState.draft?.session?.session_name || '';
    const exerciseName = (ex.name || ex.parsed_label || '').trim();
    const variation = (ex.variation || '').trim();
    if (!dateIso || !exerciseName) {
        clearLastRefsForExercise(exIdx);
        return;
    }
    if (!trainingState.lastSlotExerciseSignature) trainingState.lastSlotExerciseSignature = new Map();
    const signature = `${exerciseName.toLowerCase()}||${variation.toLowerCase()}||${(ex.sets || []).map(s => [s.reps, s.weight, s.rpe].join(':')).join('|')}`;
    trainingState.lastSlotExerciseSignature.set(exIdx, signature);
    const rolesSig = (ex.sets || []).map(s => (s?.is_top ? 'T' : s?.is_backoff ? 'B' : '')).join('');
    const key = `${dateIso}||${sessionName.toLowerCase()}||${exerciseName.toLowerCase()}||${variation.toLowerCase()}||${(ex.sets || []).length}||${rolesSig}`;
    if (trainingState.lastSlotExerciseKey.get(exIdx) === key) return;
    trainingState.lastSlotExerciseKey.set(exIdx, key);

    clearTimeout(trainingState.lastSlotExerciseTimers.get(exIdx));
    const timer = setTimeout(() => {
        const requestId = (trainingState.lastSlotExerciseRequestId.get(exIdx) || 0) + 1;
        trainingState.lastSlotExerciseRequestId.set(exIdx, requestId);
        const payload = {
            date_iso: dateIso,
            session_name: sessionName,
            exercises: [
                {
                    exercise_index: exIdx,
                    exercise_name: exerciseName,
                    variation,
                    sets: (ex.sets || []).map((set, setIdx) => ({
                        set_index: setIdx,
                        set_number: set?.set_number ?? (setIdx + 1),
                        weight: set?.weight ?? null,
                        reps: set?.reps ?? null,
                        rpe: set?.rpe ?? null,
                        role: set?.is_top ? 'T' : set?.is_backoff ? 'B' : '',
                        variation,
                        progression_excluded: Boolean(set?.progression_excluded),
                        progression_exclusion_reason: set?.progression_exclusion_reason || null,
                    })),
                },
            ],
        };
        fetch('/api/training/last-slot-references', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload),
        })
            .then(r => r.json())
            .then(result => {
                if ((trainingState.lastSlotExerciseRequestId.get(exIdx) || 0) !== requestId) return;
                if (trainingState.lastSlotExerciseSignature.get(exIdx) !== signature) {
                    return;
                }
                (result?.refs || []).forEach(entry => {
                    if (entry.exercise_index !== exIdx) return;
                    const setIdx = entry.set_index;
                    if (setIdx == null) return;
                    const exNow = trainingState.draft.exercises?.[exIdx];
                    if (!exNow || !exNow.sets || !exNow.sets[setIdx]) return;
                    exNow.sets[setIdx].last_ref = entry.ref || null;
                    exNow.sets[setIdx].last_reason = entry.reason || null;
                    exNow.sets[setIdx].last_status = entry.status || null;
                    exNow.sets[setIdx].last_reason_code = entry.reason_code || null;
                    exNow.sets[setIdx].last_reason_text = entry.reason_text || null;
                    exNow.sets[setIdx].progression_excluded = Boolean(entry.progression_excluded || exNow.sets[setIdx].progression_excluded);
                    exNow.sets[setIdx].progression_exclusion_reason = entry.progression_exclusion_reason || exNow.sets[setIdx].progression_exclusion_reason || null;
                });
                if (window.__TRAINING_DEBUG_VARIATION__) {
                    console.debug('[LAST-SLOT]', exIdx, (result?.refs || []).length);
                }
                markExerciseDirty(exIdx);
            })
            .catch(() => {});
    }, 200);
    trainingState.lastSlotExerciseTimers.set(exIdx, timer);
}

function refreshAmpelForExercise(exIdx) {
    if (!trainingState.draft?.exercises?.[exIdx]) return;
    markExerciseDirty(exIdx);
}

function scheduleVariationResolve() {
    clearTimeout(trainingState.timers.variation);
    trainingState.timers.variation = setTimeout(() => {
        resolveVariationsForDraft();
    }, 200);
}

function canAutoFillVariation(ex) {
    if (!ex) return false;
    const name = (ex.name || ex.parsed_label || '').trim();
    if (!name) return false;
    if (ex.variation_user_locked) return false;
    const current = (ex.variation || '').trim();
    const lastAuto = (ex.variation_auto_value || '').trim();
    return !current || current === lastAuto;
}

function resolveVariationsForDraft() {
    const session = trainingState.draft?.session || {};
    const dateIso = (session.date_iso || '').trim();
    const sessionName = (session.session_name || '').trim();
    if (!dateIso) return;
    (trainingState.draft?.exercises || []).forEach((ex, idx) => {
        if (!canAutoFillVariation(ex)) return;
        const name = (ex.name || ex.parsed_label || '').trim();
        if (!name) return;
        const setsPayload = (ex.sets || []).map((set) => ({
            reps: set?.reps ?? null,
            weight: set?.weight ?? null,
            rpe: set?.rpe ?? null,
            role: set?.is_top ? 'T' : set?.is_backoff ? 'B' : '',
        }));
        const setSig = setsPayload.map((set) => `${set.reps ?? ''}:${set.weight ?? ''}:${set.rpe ?? ''}:${set.role || ''}`).join('|');
        const key = `${dateIso}||${sessionName.toLowerCase()}||${name.toLowerCase()}||${setSig}`;
        if (ex.variation_resolve_key === key) return;
        ex.variation_resolve_key = key;
        const requestId = ++trainingState.variationRequestId;
        ex.variation_request_id = requestId;
        fetch('/api/training/resolve-variation', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                date_iso: dateIso,
                session_name: sessionName,
                exercise_name: name,
                set_index: 0,
                role: (ex.sets?.[0]?.is_top ? 'T' : ex.sets?.[0]?.is_backoff ? 'B' : ''),
                sets: setsPayload,
            }),
        })
            .then(r => r.json())
            .then(result => {
                if (ex.variation_request_id !== requestId) return;
                if (!canAutoFillVariation(ex)) return;
                const variation = (result?.variation || '').trim();
                const meta = {
                    source: result?.source || null,
                    note: result?.note || null,
                };
                if (variation) {
                    draftStore.setExerciseVariation(idx, variation, { auto: true, meta });
                    queueValidation({ fromParser: false });
                } else {
                    ex.variation_meta = meta;
                }
                markExerciseDirty(idx);
            })
            .catch(() => {});
    });
}

function toNumber(value) {
    if (value == null || value === '') return null;
    const num = Number(value);
    return Number.isFinite(num) ? num : null;
}

function scrollToExercise(index) {
    const card = refs.exerciseList.querySelector(`.exercise-card[data-exercise-index="${index}"]`);
    if (!card) return;
    card.scrollIntoView({ behavior: 'smooth', block: 'center' });
    card.classList.add('highlight');
    setTimeout(() => card.classList.remove('highlight'), 1200);
}

function saveSession() {
    const payload = {
        workout_id: trainingState.currentWorkoutId,
        date_iso: refs.sessionDateInput ? refs.sessionDateInput.value : '',
        session_name: refs.sessionNameInput ? refs.sessionNameInput.value.trim() : '',
        plan_id: trainingState.activePlanId,
        raw_import_text: trainingState.draft.session.raw_import_text || '',
        exercises: trainingState.draft.exercises || [],
    };
    fetch('/training/save', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
    })
        .then(resp => resp.json())
        .then(data => {
            if (data?.success) {
                updateSaveStatus('Gespeichert');
                trainingState.currentWorkoutId = data.workout_id;
                refreshHistoryCard(data.progress || null);
                startNewSessionDraft();
            } else {
                updateSaveStatus('Speichern fehlgeschlagen', true);
            }
        })
        .catch(() => updateSaveStatus('Speichern fehlgeschlagen', true));
}

function refreshHistoryCard(progress = null) {
    if (!refs.historyList || !trainingState.currentWorkoutId) return;
    const totals = computeTotalsFromDraft();
    const workoutId = trainingState.currentWorkoutId;
    const name = trainingState.draft?.session?.session_name || '';
    const dateIso = trainingState.draft?.session?.date_iso || '';
    const totalSets = totals.sets;

    const existing = refs.historyList.querySelector(`.workout-card[data-id="${workoutId}"]`);
    const card = existing || createHistoryCard({
        id: workoutId,
        name,
        date: dateIso,
        total_sets: totalSets,
        session_key: '',
        progress,
    });
    updateHistoryCard(card, { name, date: dateIso, total_sets: totalSets, progress });
    if (!existing) {
        refs.historyList.appendChild(card);
    }
    sortHistoryCardsByDate();
}

function formatHistoryProgressionQuote(data) {
    const rawRate = data?.progress?.progress_rate ?? data?.progress?.progression_quote;
    if (rawRate === null || rawRate === undefined || rawRate === '') return '—';
    const rate = Number(rawRate);
    return Number.isFinite(rate) ? `${Math.round(rate * 100)} %` : '—';
}

function createHistoryCard(data) {
    const card = createElement('div', 'workout-card', { 'data-testid': 'training-exercise-row' });
    card.dataset.id = data.id;
    card.dataset.sessionKey = data.session_key || '';
    card.dataset.dateIso = (data.date_iso || data.date || '').trim();

    const row = createElement('div', 'wc-row');
    const left = createElement('div', 'wc-left');
    const nameEl = createElement('div', 'wc-name');
    nameEl.textContent = data.name || '';
    const dateEl = createElement('div', 'wc-date');
    dateEl.textContent = formatDateShort(data.date_iso || data.date || '');
    left.appendChild(nameEl);
    left.appendChild(dateEl);

    const stats = createElement('div', 'wc-stats');
    const setsEl = createElement('span', 'wc-sets');
    setsEl.textContent = `${data.total_sets || 0} Sets`;
    const dot = createElement('span', 'dot');
    dot.textContent = '·';
    const progressEl = createElement('span', 'wc-progress');
    progressEl.textContent = formatHistoryProgressionQuote(data);
    stats.appendChild(setsEl);
    stats.appendChild(dot);
    stats.appendChild(progressEl);

    row.appendChild(left);
    row.appendChild(stats);
    card.appendChild(row);

    const delBtn = createElement('button', 'delete-btn', { 'aria-label': 'Einheit löschen' });
    delBtn.textContent = '✕';
    delBtn.addEventListener('click', (event) => deleteWorkout(event, data.id));
    card.appendChild(delBtn);

    const details = createElement('div', 'wc-details');
    details.id = `wc-details-${data.id}`;
    card.appendChild(details);
    return card;
}

function updateHistoryCard(card, data) {
    if (!card) return;
    card.dataset.dateIso = (data.date_iso || data.date || '').trim();
    const nameEl = card.querySelector('.wc-name');
    const dateEl = card.querySelector('.wc-date');
    const setsEl = card.querySelector('.wc-sets');
    const progressEl = card.querySelector('.wc-progress');
    if (nameEl) nameEl.textContent = data.name || '';
    if (dateEl) dateEl.textContent = formatDateShort(data.date_iso || data.date || '');
    if (setsEl) setsEl.textContent = `${data.total_sets || 0} Sets`;
    if (progressEl) progressEl.textContent = formatHistoryProgressionQuote(data);
}

function sortHistoryCardsByDate() {
    if (!refs.historyList) return;
    const cards = Array.from(refs.historyList.querySelectorAll('.workout-card'));
    cards.sort((a, b) => {
        const dateA = (a.dataset.dateIso || '').trim();
        const dateB = (b.dataset.dateIso || '').trim();
        if (dateA !== dateB) return dateB.localeCompare(dateA);
        const idA = parseInt(a.dataset.id || '0', 10) || 0;
        const idB = parseInt(b.dataset.id || '0', 10) || 0;
        return idB - idA;
    });
    cards.forEach((card) => refs.historyList.appendChild(card));
}

function formatDateShort(iso) {
    const parts = (iso || '').split('-');
    if (parts.length !== 3) return iso || '';
    const yy = parts[0].slice(-2);
    return `${parts[2]}.${parts[1]}.${yy}`;
}

function formatWeight(value) {
    if (value == null) return '';
    const num = Number(value);
    if (!Number.isFinite(num)) return '';
    return num.toFixed(1).replace(/\.0$/, '');
}

function buildWorkoutText(payload) {
    if (!payload?.workout) return 'Keine Details verfügbar.';
    const w = payload.workout || {};
    const lines = [];
    const header = [w.name || 'Training', formatDateShort(w.date_iso || '')].filter(Boolean).join(' · ');
    lines.push(header);
    if (w.notes) lines.push(`Notiz: ${w.notes}`);
    const progress = payload.progress || null;
    if (progress) {
        lines.push(`Fortschritt (${progress.rule_version}): ${progress.improved || 0} verbessert, ${progress.same || 0} gleich, ${progress.worse || 0} schlechter; ${progress.comparable_sets || 0}/${progress.total_eligible_sets || 0} vergleichbar; Sicherheit ${progress.comparison_confidence || 'none'}.`);
    }
    lines.push('');

    const exercises = payload.exercises || [];
    if (!exercises.length) {
        lines.push('Keine Übungen gespeichert.');
        return lines.join('\n');
    }

    exercises.forEach((ex) => {
        const title = `${ex.name || 'Übung'}${ex.variation ? ` (${ex.variation})` : ''}`;
        lines.push(title);
        if (ex.notes) lines.push(`  Notiz: ${ex.notes}`);
        const sets = ex.sets || [];
        if (!sets.length) {
            lines.push('  (keine Sets)');
        } else {
            sets.forEach((s, idx) => {
                const parts = [];
                if (s.reps != null) parts.push(`${s.reps}x`);
                const weight = formatWeight(s.weight);
                if (weight) parts.push(`${weight}kg`);
                if (s.rpe != null) parts.push(`RPE ${s.rpe}`);
                lines.push(`  ${idx + 1}) ${parts.join(' ') || '-'}`);
            });
        }
        lines.push('');
    });
    return lines.join('\n').trim();
}

async function toggleMobileWorkout(card, id) {
    if (!card) return;
    const details = card.querySelector('.wc-details');
    if (!details) return;

    if (card.classList.contains('open')) {
        card.classList.remove('open');
        return;
    }

    document.querySelectorAll('.workout-card.open').forEach((el) => {
        if (el !== card) el.classList.remove('open');
    });

    if (!details.textContent.trim()) {
        try {
            if (!mobileWorkoutCache.has(id)) {
                const resp = await fetch(`/api/workout/${id}`);
                const data = await resp.json();
                mobileWorkoutCache.set(id, data);
            }
            details.textContent = buildWorkoutText(mobileWorkoutCache.get(id));
        } catch (_) {
            details.textContent = 'Details konnten nicht geladen werden.';
        }
    }
    card.classList.add('open');
}

function initTrainingMobile() {
    if (!refs.historyList) return;
    refs.historyList.addEventListener('click', (event) => {
        event.preventDefault();
        event.stopPropagation();
        if (event.target.closest('.delete-btn')) return;
        const card = event.target.closest('.workout-card');
        if (!card) return;
        const id = parseInt(card.dataset.id || '', 10);
        if (!Number.isFinite(id)) return;
        toggleMobileWorkout(card, id);
    });
}

function updateSaveStatus(text, isError = false) {
    if (!refs.saveStatus) return;
    refs.saveStatus.textContent = text;
    refs.saveStatus.classList.toggle('error', isError);
}

function loadWorkoutById(id) {
    if (!id) return;
    const sidebar = document.querySelector('.training-sidebar');
    const pageScrollX = window.scrollX;
    const pageScrollY = window.scrollY;
    const historyScrollTop = refs.historyList ? refs.historyList.scrollTop : null;
    const sidebarScrollTop = sidebar ? sidebar.scrollTop : null;
    const sidebarViewportTop = sidebar ? sidebar.getBoundingClientRect().top : null;
    const restoreScroll = () => {
        if (refs.historyList && historyScrollTop != null) {
            refs.historyList.scrollTop = historyScrollTop;
        }
        if (sidebar && sidebarScrollTop != null) {
            sidebar.scrollTop = sidebarScrollTop;
        }
        window.scrollTo(pageScrollX, pageScrollY);
        document.documentElement.scrollTop = pageScrollY;
        document.body.scrollTop = pageScrollY;
        if (sidebar && sidebarViewportTop != null) {
            const currentTop = sidebar.getBoundingClientRect().top;
            const delta = currentTop - sidebarViewportTop;
            if (Math.abs(delta) > 1) {
                window.scrollBy(0, delta);
            }
        }
    };
    fetch(`/api/workout/${id}`)
        .then(r => r.json())
        .then(payload => {
            if (!payload?.success || !payload.workout) return;
            trainingState.currentWorkoutId = payload.workout.id;
            trainingState.currentProgress = payload.progress || null;
            if (refs.sessionNameInput) refs.sessionNameInput.value = payload.workout.name || '';
            if (refs.sessionDateInput) {
                refs.sessionDateInput.value = payload.workout.date_iso || '';
                syncSessionDateDisplay();
            }
            const formattedRawImport = formatLoadedParserText(payload.workout.raw_import_text || '');
            trainingState.draft = {
                session: {
                    date_iso: payload.workout.date_iso || '',
                    session_name: payload.workout.name || '',
                    plan_id: trainingState.activePlanId,
                    notes: payload.workout.notes || '',
                    raw_import_text: formattedRawImport,
                },
                exercises: payload.exercises.map(ex => ({
                    name: ex.name,
                    variation: ex.variation,
                    notes: ex.notes,
                    sets: ex.sets,
                    meta: {},
                    variation_user_locked: false,
                    variation_auto_value: null,
                    variation_meta: null,
                    variation_resolve_key: '',
                })),
            };
            syncParserTextarea(trainingState.draft.session.raw_import_text || '');
            renderDraftExercises();
            resolveActivePlan();
            queueValidation({ fromParser: false });
            scheduleLastSlotRefs();
            if (window.TrainingWorkspace?.setExistingSessionView) {
                window.TrainingWorkspace.setExistingSessionView(payload.workout.id);
            }
            window.requestAnimationFrame(restoreScroll);
            window.setTimeout(restoreScroll, 0);
            window.setTimeout(restoreScroll, 32);
            window.setTimeout(restoreScroll, 120);
            window.setTimeout(restoreScroll, 300);
        })
        .catch(console.error);
}

window.loadWorkout = function (id) {
    loadWorkoutById(id);
};

window.deleteWorkout = function (event, id) {
    event.stopPropagation();
    if (event.preventDefault) event.preventDefault();
    if (!confirm('Training wirklich löschen?')) return;
    fetch(`/api/workout/delete/${id}`, { method: 'DELETE' })
        .then(r => r.json())
        .then(result => {
            if (result?.success) {
                const card = event.target.closest('.workout-card') || document.querySelector(`.workout-card[data-id="${id}"]`);
                if (card) card.remove();
                if (trainingState.currentWorkoutId === id) {
                    startNewSessionDraft();
                }
                if (refs.historyList && !refs.historyList.querySelector('.workout-card')) {
                    trainingState.currentWorkoutId = null;
                }
            } else {
                alert(result?.message || 'Löschen fehlgeschlagen');
            }
        })
        .catch(() => alert('Löschen fehlgeschlagen'));
};

window.handleWorkoutClick = function (id, event) {
    if (event && event.target.closest('.delete-btn')) return;
    loadWorkoutById(id);
};

window.addEventListener('DOMContentLoaded', initTraining);
