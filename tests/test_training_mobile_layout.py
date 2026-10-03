from pathlib import Path


def test_training_is_logging_only_and_forms_do_not_overflow():
    root = Path(__file__).parents[1]
    stylesheet = (root / "static" / "css" / "training.css").read_text(encoding="utf-8")
    template = (root / "templates" / "training.html").read_text(encoding="utf-8")

    mobile_invariants = stylesheet.split("Mobile invariants:", 1)[1]

    assert '_training_logging_content.html' in template
    assert '_training_history_rail.html' in template
    assert '_training_analysis_content.html' not in template
    assert 'training-tab-analysis' not in template
    assert 'training-tab-logging' not in template
    assert 'training_workspace.js' not in template
    assert "grid-template-columns: 28px repeat(3, minmax(0, 1fr)) 30px !important;" in mobile_invariants
    assert "#training-ui #exercise-list .set-row .set-input" in mobile_invariants
    assert "width: 100% !important;" in mobile_invariants
    assert "Phone polish:" in stylesheet
    assert "#training-ui #exercise-list .set-field-label" in stylesheet
    assert "#training-ui .parser-preview-panel" in stylesheet
    assert ".analysis-card-head > .analysis-card-heading" in stylesheet
    assert "20260809_mobile_workspace_v35_6" in template
    assert 'data-training-mobile-mode="session"' in template
    assert 'data-training-mobile-mode="parser"' in template
    assert 'data-training-mobile-mode="history"' in template
    assert "Dashboard design baseline for /training" in stylesheet
    assert "Training logger deck v17" in stylesheet
    assert "training-entry-grid" in stylesheet
    assert "v17 cascade lock" in stylesheet
    assert "#training-workspace #exercise-list .set-row.status-green .set-index" in stylesheet
    assert "Compact logger v19" in stylesheet
    assert "font-family: inherit !important;" in stylesheet
    assert "Defeat the persisted compact-mode card expansion explicitly" in stylesheet
    assert "#training-workspace #training-ui .session-header-columns > *" in stylesheet
    assert "Balanced logger v20" in stylesheet
    assert "grid-template-columns: repeat(2, minmax(0, 1fr));" in stylesheet
    assert "Session-first workspace v22" in stylesheet
    assert "Full-width session setup, parser left, exercise stack right" in stylesheet
    assert "Column alignment v25" in stylesheet
    assert "Header compaction v26" in stylesheet
    assert "Adaptive parser v27" in stylesheet
    assert "Direct parser v28" in stylesheet
    assert "Delete alignment v29" in stylesheet
    assert "Readable exercise notes v34" in stylesheet
    assert "clamp(300px, 42%, 360px)" in stylesheet
    assert "training-parser-log-head" in stylesheet
    assert "--training-page: #0e1013;" in stylesheet
    assert "body:has(#training-workspace) .top-nav" in stylesheet
    assert '<p class="liva-subpage-kicker">Logging</p>' in template
def test_training_mobile_set_fields_have_real_labels_and_chart_limits_ticks():
    root = Path(__file__).parents[1]
    script = (root / "static" / "js" / "training_vnext.js").read_text(encoding="utf-8")
    stylesheet = (root / "static" / "css" / "training.css").read_text(encoding="utf-8")
    analysis = (root / "templates" / "partials" / "_training_analysis_content.html").read_text(encoding="utf-8")
    logging = (root / "templates" / "partials" / "_training_logging_content.html").read_text(encoding="utf-8")

    assert "const setField = (label, input)" in script
    assert "setField('Reps', repsInput)" in script
    assert "setField('kg', weightInput)" in script
    assert "setField('RPE', rpeInput)" in script
    assert "resolveExerciseName(ex) || ex.variation || ex.notes" in script
    assert 'maxTicksLimit: window.matchMedia("(max-width: 720px)").matches ? 4 : 8' in analysis
    assert "hidden: false" in analysis
    assert "The editable draft is the UI source of truth" in script
    assert "const sets = computed.sets" in script
    assert "trainingState.currentProgress" in script
    assert "formatHistoryProgressionQuote({ progress: trainingState.currentProgress })" in script
    assert '<div class="stat-label">Progression</div>' in logging
    assert "trainingState.draft = result.draft;" not in script
    assert "sets-column-head" in script
    assert "one visual row stays one row" in stylesheet
    assert "grid-template-rows: 36px !important;" in stylesheet


def test_training_cards_render_embedded_progress_status_without_async_refresh():
    root = Path(__file__).parents[1]
    script = (root / "static" / "js" / "training_vnext.js").read_text(encoding="utf-8")

    assert "updateExerciseCardFromDraft(idx);" in script
    assert "['improved', 'progress'].includes(comparisonStatus)" in script
    assert "['worse', 'regress'].includes(comparisonStatus)" in script
    assert "'last_status', 'last_reason_code', 'last_reason_text'" in script
    assert "if (key !== JSON.stringify(buildLastSlotPayload())) return;" in script
    assert "A transient request must not blank the UI" in script
    assert "function formatLoadedParserText(text)" in script
    assert "formattedRawImport" in script
    assert "const progressionExcluded = Boolean" in script
    assert "progressionExcluded ? 'neutral'" in script
    assert "progression_exclusion_reason" in script
    assert "set-last-info is-empty" in script
    assert "lastInfo.classList.toggle('is-empty', !lastText)" in script


def test_training_history_hydrates_after_cold_snapshot_without_manual_reload():
    root = Path(__file__).parents[1]
    script = (root / "static" / "js" / "training_vnext.js").read_text(encoding="utf-8")
    template = (root / "templates" / "partials" / "_training_history_rail.html").read_text(encoding="utf-8")
    page = (root / "templates" / "training.html").read_text(encoding="utf-8")

    assert "hydrateHistoryFromSnapshot();" in script
    assert "fetch('/api/training_snapshot'" in script
    assert "payload?.loading || payload?.partial || payload?.refresh_triggered" in script
    assert "renderHistorySnapshot(payload?.workouts || [])" in script
    assert "function escapeHtml(value)" in script
    assert "history-list-status" in template
    assert "20260809_mobile_workspace_v24_3" in page
    assert "setMobileTrainingMode('session'" in script
    assert "formatHistoryProgressionQuote(data)" in script
    assert "data?.progress?.progress_rate ?? data?.progress?.progression_quote" in script
    assert "Math.round(rate * 100)" in script
    assert 'class="wc-progress"' in template
    assert 'class="wc-load"' not in template
    assert "total_load.toFixed" not in script
    logging = (root / "templates" / "partials" / "_training_logging_content.html").read_text(encoding="utf-8")
    assert '<summary class="parser-summary' not in logging
    assert '>Freitext<' not in logging


def test_training_response_chart_has_all_scopes_and_clear_model_semantics():
    root = Path(__file__).parents[1]
    analysis = (root / "templates" / "partials" / "_training_analysis_content.html").read_text(encoding="utf-8")
    stylesheet = (root / "static" / "css" / "training.css").read_text(encoding="utf-8")

    assert "Belastung &amp; Leistungsreaktion" in analysis
    assert "Superkompensation / Trend &amp; Patterns" not in analysis
    assert 'fetch(`/api/training/response?' in analysis
    assert 'liva:analysis-range-change' in analysis
    assert 'params.set("start", interval.start)' in analysis
    assert 'params.set("end", interval.end)' in analysis
    assert 'title: { display: true, text: "gewichtete Arbeitssätze"' in analysis
    assert 'text: "Leistungsindex"' in analysis
    assert 'metric: "measured"' in analysis
    assert 'metric: "trend"' in analysis
    assert "Leistungstrend aus echten Messungen" in analysis
    assert 'metric: "potential"' not in analysis
    assert 'metric: "baseline"' not in analysis
    assert 'borderColor: "rgba(226, 232, 240, 0.34)"' in analysis
    assert 'cubicInterpolationMode: "monotone"' in analysis
    assert "borderWidth: 4" in analysis
    assert "load_details?.[date]" not in analysis
    assert "setResponseScope(button.dataset.scope" in analysis
    for scope in ("overall", "chest", "back", "legs", "shoulders", "biceps", "triceps"):
        assert f'data-scope="{scope}"' in analysis
    assert "grid-template-columns: repeat(4, minmax(0, 1fr));" in stylesheet
