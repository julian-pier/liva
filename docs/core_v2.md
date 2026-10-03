# CORE-v2 Developer Notes

## Neue Dateien
- `core/core_v2_schema.py`: v2-Tabellen + Indizes.
- `core/core_learning.py`: Daily Interpretation Pass (Raw Signals -> Patterns).
- `core/core_review.py`: Outcome-Review-Pass für frühere Decisions.
- `core/core_model.py`: Personalisierte Hypothesen, Blind Spots, Modellupdates.
- `core/core_explain.py`: Explainability-Helper (`explain_decision_v2`, `explain_pattern_v2`, `explain_hypothesis_v2`, `summarize_model_change_v2`).
- `core/core_v2_engine.py`: Orchestrierung, Snapshots, API-Payload-Building.
- `templates/core_v2.html`: neues `/core` Frontend mit Tabs `Heute`, `Gelernt`, `Reviews`, `Modell`, `Timeline`.
- `static/js/core_board.js`: aktueller Frontend-Renderer + API-Loading + Recompute.
- `static/css/core_board.css`: aktuelle CORE-v2-Designsprache.

## Angepasste Datei
- `core/core_api.py`
  - `/core` rendert jetzt `core_v2.html`.
  - Legacy-Control-Room bleibt unter `/core/control-room`.
  - neue Endpunkte unter `/api/core/v2/*`.

## Neue Tabellen (SQLite `database/core.sqlite3`)
- `core_observations`
- `core_patterns`
- `core_hypotheses`
- `core_decision_reviews`
- `core_model_updates`
- `core_blind_spots`
- `core_daily_snapshot_v2`

## Neue Endpunkte
- `GET /api/core/v2/today`
- `GET /api/core/v2/learned`
- `GET /api/core/v2/reviews`
- `GET /api/core/v2/model`
- `GET /api/core/v2/timeline`
- `GET /api/core/v2/decision/<id>/explain`
- `GET /api/core/v2/hypothesis/<id>`
- `GET /api/core/v2/pattern/<id>`
- `POST /api/core/v2/recompute`
- `POST /api/core/v2/review/run`

## Wie CORE-v2 intern lernt
1. **Daily Interpretation Pass** (`core_learning.run_daily_interpretation_pass`)
   - sammelt Rohsignale aus Training, Runs, HRV, Nutrition, Overrides und Decision-History.
   - speichert sie als `core_observations`.
   - erzeugt interpretierte Pattern-Objekte (inkl. Evidenz, Confidence, Stabilität) in `core_patterns`.

2. **Review Pass** (`core_review.run_review_pass`)
   - nimmt relevante frühere Decisions.
   - vergleicht Erwartung vs. Folgeentwicklung (Recovery, Umsetzung, Nutrition-Logging).
   - schreibt Outcome-Labels + Learnings in `core_decision_reviews`.

3. **Personal Learning Pass** (`core_model.run_personal_learning_pass`)
   - erzeugt/aktualisiert personenspezifische Hypothesen in `core_hypotheses`.
   - markiert unklare Bereiche als `core_blind_spots`.
   - protokolliert Modelländerungen in `core_model_updates`.

4. **Snapshot** (`core_v2_engine._store_snapshot`)
   - schreibt täglichen Modellzustand in `core_daily_snapshot_v2`.

## Explainability
- `explain_decision_v2`: zeigt Top-Signale, Pattern, Hypothesen, Unsicherheiten und Falsifizierungschecks.
- `explain_pattern_v2`: erklärt Evidenz + Stabilität eines Patterns in natürlichem Deutsch.
- `explain_hypothesis_v2`: erklärt Support/Widerspruch, Relevanz und Status einer Hypothese.
- `summarize_model_change_v2`: beschreibt, was sich im Modell konkret geändert hat.

## Tuning / Anpassungspunkte
- Pattern-Logik und Schwellen: `core/core_learning.py`
  - `_build_patterns`
  - `_pattern_status`
- Review-Bewertung (good/mixed/bad/too_...): `core/core_review.py`
  - `review_decision_v2`
- Hypothesenbildung und Status-Übergänge: `core/core_model.py`
  - `_build_hypothesis_candidates`
  - `_status_from_stats`
  - `_upsert_hypothesis`
- Gewichtete Einflussfaktoren: `core/core_model.py`
  - `_factor_weights`
- API-Payload-Zuschnitt fürs Frontend: `core/core_v2_engine.py`
  - `get_today_payload`
  - `get_learned_payload`
  - `get_reviews_payload`
  - `get_timeline_payload`

## Fallback-Verhalten
- Wenn v2-Datenlage zu schwach ist, liefert `today` ein Fallback-Objekt (`fallback.active=true`) und zieht Legacy-`core_control_room`-Kontext mit ein.
