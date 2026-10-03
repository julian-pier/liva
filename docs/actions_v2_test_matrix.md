# Actions V2 Test Matrix

Basis: `/api/v2/actions`  
Quelle: `ai/actions_v2.py`  
Testlauf: `scripts/test_actions_v2.sh` mit Default-Gates (`ACTIONS_V2_MEMORY_WRITE=0`, `ACTIONS_V2_REMOTE_LIVE=0`)

| # | Methode | Endpoint | Zweck | Klasse | Ressourcen / Wirkung | Im Default-Testlauf |
|---:|---|---|---|---|---|---|
| 1 | GET | `/liva/today` | Tages-Snapshot | `read_only` | Liest Core, Recovery, Nutrition, Weight, Training, Memory. | bestätigt |
| 2 | GET | `/liva/context` | Langfristiger Kontext | `read_only` | Liest Plan, Core-Sidecar, Nutrition-Sidecar, Memory. | bestätigt |
| 3 | GET | `/liva/state` | Domain-Frische | `read_only` | Liest Status je Domain. | bestätigt |
| 4 | GET | `/core/state` | CORE-Zustand | `read_only` | Liest `core.actions_v2_state`. | bestätigt |
| 5 | POST | `/core/control` | CORE-Override | `live_mutation` | Schreibt `training.core_override_choices`, spiegelt in `core.actions_v2_state`; `live_state_changed=true`, wenn gesetzt/gelöscht wurde. | bestätigt |
| 6 | GET | `/training/state` | Trainingsrahmen | `read_only` | Liest Planstruktur und letzte Sessions. | bestätigt |
| 7 | POST | `/training/data` | Trainingsdaten | `read_only` | Scopes: `sessions`, `plan_days`, `recent_summary`. | bestätigt |
| 8 | POST | `/training/exercise-data` | Übungshistorie | `read_only` | Liest echte Trainingsdaten mit Exercise-Resolution. | bestätigt |
| 9 | POST | `/training/adjust` | Trainingsänderungs-Intent | `logged_only` | Schreibt `training.actions_v2_command_log`; kein Plan-Write. | bestätigt |
| 10 | GET | `/training/plan-state` | Aktiver Plan | `read_only` | Query `detail=compact|full`; Default `compact`. | bestätigt |
| 11 | POST | `/training/plan-control` | Plansteuerungs-Intent | `logged_only` | Schreibt `plans.actions_v2_command_log`; kein Planwechsel. | bestätigt |
| 12 | POST | `/progression/data` | Progressionsreihen | `read_only` | `by_exercise` implementiert; `by_group` liefert `unsupported_mode_not_implemented`. | bestätigt für `by_exercise` |
| 13 | POST | `/progression/compare` | Zeiträume vergleichen | `read_only` | Nur `compare_type=time_ranges`. | bestätigt |
| 14 | GET | `/recovery/state` | Recovery-Status | `read_only` | Liest HRV/Recovery-Zusammenfassung. | bestätigt |
| 15 | POST | `/recovery/data` | Recovery-Zeitreihe | `read_only` | Filtert HRV-Zeilen nach Datum. | bestätigt |
| 16 | POST | `/recovery/control` | Recovery-Writes | `live_mutation` | Alcohol/Sickness schreiben `hrv.hrv_measurements`; Sleep/Annotation schreiben `hrv.recovery_day_annotations`. | bestätigt |
| 17 | GET | `/nutrition/state` | Nutrition-Status | `read_only` | Liest Live-Targets oder V2-Sidecar-Targets. | bestätigt |
| 17a | GET | `/nutrition/timing` | Nutrition-Timing | `read_only` | Kompakter Coach-Read für Meals, Foods und Carb-/Protein-Timing. | neu |
| 17b | GET | `/nutrition/foods` | Food-Suche | `read_only` | Liest konkrete Foods aus `nutrition_foods`. | neu |
| 18 | POST | `/nutrition/data` | Nutrition-Daten | `read_only` | Modes: `daily_totals`, `meals`, `planned_meals`; `meals` liefert Tages-Foods, Makros, Targets, Remaining und offene geplante Meals. | bestätigt |
| 19 | POST | `/nutrition/control` | Nutrition-Steuerung | `live_mutation` | `adjust_targets` schreibt echte Targets; `log_planned_meal` bestaetigt einzelne oder alle offenen geplanten Meals optional mit Substitutions; freie Meal-Commands schreiben echte Logged-Meals. | bestätigt |
| 19a | GET | `/bodycomp/cut-status` | Cut-/Formstatus | `read_only` | Fuehrt Gewicht, Nutrition, Training und Recovery kompakt zusammen. | neu |
| 20 | GET | `/weight/state` | Gewichtszustand | `read_only` | Liest Weight-Logs und Trendstatus. | bestätigt |
| 21 | POST | `/weight/data` | Gewichtsdaten | `read_only` | Filtert Weight-Logs nach Datum. | bestätigt |
| 22 | POST | `/weight/control` | Weight-Writes/Note-Intent | `live_mutation` / `logged_only` | `log_weight`, `edit_weight`, `delete_weight` mutieren `nutrition.weight_logs`; `set_checkin_note` nur Log. | bestätigt |
| 23 | GET | `/runs/state` | Run-Zustand | `read_only` | Liest Runs plus V2-Sidecar-Override `next_run_type`. | bestätigt |
| 23a | GET | `/cardio/summary` | Cardio-Zusammenfassung | `read_only` | Saubere Run-/Ergo-Rollups mit konservativer Z2-Schätzung. | neu |
| 24 | POST | `/runs/data` | Run-Daten | `read_only` | Filtert Run-Sessions nach Datum. | bestätigt |
| 25 | POST | `/runs/control` | Run-Steuerung | `live_mutation` / `applied_sidecar` / `logged_only` | `create_run` schreibt `runs.runs`; `set_run_type`, `update_run_block` schreiben `runs.actions_v2_state`; `mark_run_skipped` nur Log. | bestätigt |
| 25a | GET | `/training/today-loads` | Tagesgewichte | `read_only` | Liest Plan + Historie und liefert konkrete, gerundete Lastvorschläge. | neu |
| 25b | GET | `/life/today` | Kalender / Alltag | `read_only` | Liest Kalender-, Schul- und Tagesfenster kompakt. | neu |
| 25c | GET | `/checkin/today` | Subjektiver Check-in | `read_only` | Liest subjektive Tagesform, wenn vorhanden. | neu |
| 25d | GET | `/endurance/week` | Endurance-Woche | `read_only` | Zeigt Athletica/Intervals-Wochenkontext, falls verbunden. | neu |
| 26 | POST | `/liva/read` `mode=memory` | Memory lesen | `read_only` | Liest usememos via LIVA-Bridge, inkl. Query-/Tag-Filter. | bestätigt |
| 27 | POST | `/liva/act` `domain=memory` | Memory speichern | `dry_run` / `live_mutation` / `logged_only` | Schreibt GPT-Memos nach usememos; Mutationen nur fuer `#gpt`-Memos. | gated für echte Writes |
| 28 | POST | `/liva/act` `cleanup_expired` | Memory TTL-Cleanup | `dry_run` / `live_mutation` | Loescht nur kurzfristige `#gpt`-Memos nach TTL. | gated für echte Writes |
| 29 | POST | `/memory/link` | Memory-Link-Intent | `logged_only` | Schreibt `core.actions_v2_command_log`; kein Link-Store. | bestätigt |
| 30 | POST | `/remote/control` | Remote-Geräte/PC | `dry_run` / `live_mutation` / `logged_only` | Default `dry_run=true`; `dry_run=false` nutzt echte Adapter oder liefert Fehler; nur erlaubte Aktionen pro Target-Typ; `pc_action=status` ist `logged_only`. | bestätigt als `dry_run` |

## Command-Klassen

| Endpoint | Commands | Klasse |
|---|---|---|
| `/core/control` | `set_override`, `clear_override` | `live_mutation` |
| `/training/adjust` | alle erlaubten Commands | `logged_only` |
| `/training/plan-control` | alle erlaubten Commands | `logged_only` |
| `/recovery/control` | alle erlaubten Commands | `live_mutation` |
| `/nutrition/control` | `adjust_targets` | `live_mutation` |
| `/nutrition/control` | `log_planned_meal` | `live_mutation`; loggt geplante Slots nach `meal_number`, `meal` oder `slot_id`; mit `all_planned=true` alle offenen geplanten Meals; optional mit `substitutions`; syncet Tageswerte via `weight_logs` |
| `/nutrition/control` | `quick_log_meal`, `edit_logged_meal`, `delete_logged_meal` | `live_mutation`; resolved bekannte Food-Namen und syncet Tageswerte via `weight_logs` |
| `/weight/control` | `log_weight`, `edit_weight`, `delete_weight` | `live_mutation` |
| `/weight/control` | `set_checkin_note` | `logged_only` |
| `/runs/control` | `create_run` | `live_mutation` |
| `/runs/control` | `set_run_type`, `update_run_block` | `applied_sidecar` |
| `/runs/control` | `mark_run_skipped` | `logged_only` |
| `/liva/act domain=memory` | `store_fact`, `store_preference`, `store_pattern`, `append_log_entry`, `append_section_note` | `live_mutation`, wenn usememos erfolgreich schreibt |
| `/liva/act domain=memory` | `update_entry`, `remove_entry`, `archive`, `mark_stale` | `live_mutation`, nur fuer Memos mit `#gpt` am Anfang |
| `/liva/act domain=memory` | `cleanup_expired` | `dry_run` oder `live_mutation` mit `confirm=true` |
| `/memory/link` | alle erlaubten Commands | `logged_only` |
| `/remote/control` | Feld fehlt oder `dry_run=true` | `dry_run` |
| `/remote/control` | Device/Scene/PC `wake`/`shutdown` mit `dry_run=false` | `live_mutation`, wenn Adapter erfolgreich ist |
| `/remote/control` | `pc_action=status` mit `dry_run=false` | `logged_only` |
