# Canonical LIVA parity – MCP Phase 2A

This document records source-level parity. The MCP code does not import the
Flask application or the canonical Actions module, because several canonical
read paths call schema initializers, refresh-capable integrations, or builders.

## Daily context

1. **Canonical routes/functions:** `daily_context`, `life_today`, `liva_today`,
   `_calendar_range_payload`, `_training_ai_decision_state`,
   `_training_today_card_payload`, `_core_state_payload` in `ai/actions_v2.py`.
2. **Modules:** Actions v2, calendar read models, CORE, training plan management,
   Polar, endurance approval, nutrition planning.
3. **Canonical sources:** training and plans SQLite databases, calendar snapshot
   tables plus Google calendar cache, CORE state, persisted training cards,
   training AI decisions, Polar, HRV, nutrition and weight data.
4. **Potential side effects:** canonical CORE board/card getters can call schema
   initializers or build missing artifacts; AI-decision state recalculates a
   context hash through a large resolver graph; endurance context can refresh;
   nutrition planning helpers call `ensure_nutrition_planning_schema`.
5. **Safe MCP alternative:** direct `mode=ro` reads of the latest school snapshot,
   active plan, already persisted training card and AI decision, CORE phase and
   persisted mode candidates, plus the other snapshot services.
6. **Parity:** partial.
7. **Missing:** Google calendar events, canonical planned-session resolver,
   live context-hash comparison, live CORE decision, endurance approval, full
   calendar pressure/free-block merging and coaching summary.

## Calendar today

1. **Canonical route/function:** `calendar_today`, `_calendar_range_payload`.
2. **Modules:** Actions v2 calendar helpers and Google calendar read cache.
3. **Sources:** `training.school_schedule_snapshots`,
   `training.school_schedule_entries`, and the Google calendar cache.
4. **Potential side effects:** Google helper reads an external/cache-backed
   integration; importing the full Actions module is outside the isolation
   boundary.
5. **Safe MCP alternative:** select the newest school snapshot for the local
   date and at most 25 entries belonging to that exact snapshot.
6. **Parity:** partial.
7. **Missing:** Google calendars, canonical free blocks, merged warnings,
   calendar scope and complete day-pressure calculation.

## Recovery state and data

1. **Canonical functions:** `_recovery_summary`,
   `_compact_recovery_summary_payload`, `_gpt_recovery_state_payload`,
   `recovery_state`, `recovery_data`.
2. **Modules:** Actions v2, `integrations.polar_sync`, Polar read API.
3. **Sources:** `hrv_measurements`, `recovery_day_flags`, `hrv_day_flags`,
   `recovery_day_annotations`, `polar_sleep`, `polar_nightly_recharge`, and
   `polar_continuous_samples`.
4. **Potential side effects:** the canonical function calls Polar state helpers;
   write-side recovery helpers create/alter annotation and flag schemas. The
   canonical baseline/readiness function is embedded in the large Actions
   module rather than exposed as an isolated pure service.
5. **Safe MCP alternative:** fixed direct reads of raw recovery components,
   source dates, source-level freshness and at most 30 days of flags/annotations.
6. **Parity:** partial.
7. **Missing:** canonical 28-day baselines, readiness classification, exact
   recent-flag resolution and canonical heart-rate semantic projection.

`canonical_readiness_available` remains `false`; Phase 2A invents no formula.

## Nutrition state, data and timing

1. **Canonical functions:** `_nutrition_target_state`, `_nutrition_daily`,
   `_nutrition_state`, `_nutrition_domain_status`, `_nutrition_logging_streak`,
   `_nutrition_planned_meals_for_date`, plus `get_live_macro_targets` and
   `get_logging_day_payload` in `nutrition/nutrition_planning_db.py`.
2. **Modules:** Actions v2 and nutrition planning database/service modules.
3. **Sources:** `actions_v2_state`, `nutrition_settings`, active week templates
   and template days, `nutrition_day_actuals`, macro-bearing `weight_logs`,
   `nutrition_daily`, `nutrition_week_plans`, and `nutrition_active_day_plan`.
4. **Potential side effects:** `get_live_macro_targets` and logging payload
   helpers invoke `ensure_nutrition_planning_schema`; importing them is therefore
   not acceptable for a guaranteed read-only process.
5. **Safe MCP alternative:** reproduce the documented canonical target priority
   exactly: valid `active_targets_override`, then active template day, then
   settings. Actuals follow canonical daily precedence: macro-bearing
   `weight_logs`, then `nutrition_day_actuals`, then the explicitly separate
   legacy `nutrition_daily` snapshot. Planned meals come from the already
   persisted, size-bounded active-day payload.
6. **Parity:** partial.
7. **Missing:** full canonical meal items/totals, logging streak, 7-day
   adherence, complete timing analysis and canonical domain-status reasons.

## CORE state and phase

1. **Canonical function:** `_core_state_payload` and application-backed
   `_core_decision_mode_state`.
2. **Modules:** Actions v2, Flask application CORE payload, endurance approval.
3. **Sources:** CORE `actions_v2_state`, training `core_override_choices`,
   `autopilot_day`, live application decision payload and endurance state.
4. **Potential side effects:** the canonical path depends on the already loaded
   Flask app and calls endurance context with `refresh=True`.
5. **Safe MCP alternative:** read persisted phase and override candidates only.
6. **Parity:** low.
7. **Missing:** live CORE decision, confidence, objections, hard stops, recent
   modes and endurance gate. `canonical_live_decision_available` is false.

## Training card and planned unit

1. **Canonical functions:** `get_persisted_core_training_card`,
   `_training_today_card_payload`, `_plan_session_for_date`.
2. **Modules:** CORE training card, daily board and Actions v2 plan resolver.
3. **Sources:** `core_training_cards`, active `gym_plans`/legacy `plans`, workout
   rotation anchors and training decisions.
4. **Potential side effects:** persisted card getters call schema initializers;
   non-persisted getters can build a card or missing board; plan resolution has a
   wide dependency graph.
5. **Safe MCP alternative:** direct read of an existing active card for today.
   A persisted AI decision can identify its already resolved planned session.
6. **Parity:** partial when a decision/card exists, otherwise low.
7. **Missing:** canonical rotation resolver and synthesized card read model.

## AI-decision readback

1. **Canonical functions:** `_latest_training_ai_decision`,
   `_training_ai_decision_state`.
2. **Modules:** Actions v2 training decision context and persistence code.
3. **Source:** `core.training_ai_decisions`.
4. **Potential side effects:** the canonical getter calls
   `ensure_training_ai_decisions_schema`; state calculation rebuilds the current
   context and hash.
5. **Safe MCP alternative:** direct bounded read of the newest non-superseded
   persisted decision for the local date.
6. **Parity:** partial.
7. **Missing:** current context hash, dynamically detected stale reasons and
   planned-session applicability recalculation.

The MCP never inserts, updates, invalidates or generates an AI decision.

## Domain freshness and stale reasons

1. **Canonical functions:** `_domain_freshness`,
   `_nutrition_domain_status`, recovery freshness fields and AI-decision state.
2. **Modules:** Actions v2 domain helpers.
3. **Sources:** latest domain dates plus domain-specific logs and status fields.
4. **Potential side effects:** no side effect in the small freshness helper, but
   most complete domain-status callers use broader modules with schema checks.
5. **Safe MCP alternative:** explicit source dates and deterministic age values;
   generic training/weight/runs freshness follows the canonical three-day rule.
6. **Parity:** partial.
7. **Missing:** canonical nutrition reason codes, live AI context-hash stale
   reasons and complete calendar/CORE freshness semantics.
