# LIVA Actions API V2

LIVA Actions V2 ist eine neue, parallele GPT-Actions-Schicht fuer LIVA. Sie ersetzt keine bestehende API, schreibt keine alten GPT-Actions-Configs um und ist nicht als Standard aktiviert.

## Zweck

V2 ist fuer Custom GPT Actions optimiert:

- kompakte, strukturierte Read-Endpunkte fuer Tageszustand, Kontext, Rohdaten und Datenfrische
- kleine, streng validierte Command-Endpunkte
- keine versteckte Stagnations- oder Progressionsdiagnose im Backend
- GPT soll Muster aus Rohdaten erkennen; die API liefert Daten, Zustand und praezise Befehlsflaechen

## Unterschiede zu V1

- V1 bleibt unter den bestehenden Routen wie `/api/ai` und `/api/aiw` aktiv. Die alte lokale `/api/memory`-Markdown-API ist entfernt; Memory laeuft ueber die LIVA/usememos-Bridge.
- V2 nutzt ausschliesslich `/api/v2/actions/...`.
- V2 hat eine eigene OpenAPI-Datei: `openapi/liva-actions-v2.json`.
- V2-Commands werden ueber eigene `actions_v2_*` Tabellen protokolliert, wenn es fuer die jeweilige Domaene keinen sicheren bestehenden Write-Service gibt.
- Remote-Control ist standardmaessig `dry_run`. Reale Ausfuehrung passiert nur mit `dry_run=false` und nur fuer bekannte Devices, Szenen und PC-Aktionen.
- V2 resolved Trainings-Displaynamen wie `Schrägbankdrücken (Smith)` zentral auf die DB-Normalform `name + variation/device`.
- V2 unterscheidet leere Zeitfenster, nicht aufgeloeste Entities und fehlende Datenquellen ueber `data_status` und `resolution`.
- V2 fuehrt stabile `exercise_key`-Werte fuer aufgeloeste Uebungen mit.
- `training/plan-state` ist standardmaessig kompakt; `?detail=full` liefert die volle Planstruktur.

## Route-Basis

Alle 30 Endpoints liegen unter:

```text
/api/v2/actions
```

## Auth

V2 nutzt dieselbe AI-Key-Logik wie die bestehende Actions API:

```bash
Authorization: Bearer $LIVA_AI_READ_KEY
Authorization: Bearer $LIVA_AI_WRITE_KEY
```

Read-Endpunkte akzeptieren Read- oder Write-Key. Command-Endpunkte nutzen `require_ai_write` und benoetigen zusaetzlich `AI_WRITE_ENABLED=1`, genau wie die bestehende Write-Schicht.

Fehler sind strukturiert:

```json
{
  "ok": false,
  "error": {
    "code": "invalid_command",
    "message": "Unsupported command for /core/control"
  }
}
```

Command-Responses haben zusaetzlich eine einheitliche Ausfuehrungssemantik. Fuer die Aussendarstellung gelten die Klassen `read_only`, `applied_sidecar`, `logged_only`, `dry_run`, `live_mutation`.

```json
{
  "ok": true,
  "command": "change_sets",
  "execution": {
    "mode": "logged_only",
    "live_state_changed": false,
    "affected_resources": ["training.actions_v2_command_log"]
  },
  "result": {}
}
```

`execution.mode` kann nur diese Werte haben:

- `applied_live`: Response-Mode fuer `live_mutation`; echte Markdown-Dateien oder reale Geraetezustaende wurden geaendert
- `applied_sidecar`: nur in V2-Sidecar-State angewendet, nicht im kanonischen V1-State; `live_state_changed=false`
- `logged_only`: validiert und protokolliert, aber keine Live-Mutation
- `dry_run`: validiert, aber nicht real ausgefuehrt

## Command-Aktivierung

V2 trennt Live-Writes, Sidecar-Writes und reine Logs bewusst:

| Endpoint | Commands | Execution |
| --- | --- | --- |
| `/core/control` | `set_override`, `clear_override` | `live_mutation`; schreibt `training.core_override_choices` und spiegelt den Zustand in `core.actions_v2_state`. |
| `/training/adjust` | `log_gym_session` | `live_mutation`; parst OCR/Text und schreibt echte `workouts`, `exercises`, `sets`. |
| `/training/adjust` | `replace_exercise`, `change_sets`, `change_rep_range`, `change_rpe`, `change_variation`, `update_exercise_target` | `live_mutation`; mutiert nur den aktiven, nicht archivierten `gym_plans.plan_json`, setzt `updated_at` und liefert Resolution plus alten/neuen Target-Stand zurueck. |
| `/training/adjust` | `add_exercise`, `remove_exercise`, `reorder_exercise` | `logged_only`; noch keine Live-Planmutation. |
| `/training/plan-control` | `create_training_plan` | `live_mutation`; erstellt einen kompletten neuen Trainingsplan in `gym_plans` oder fallback `plans`, kann den neuen Plan aktiv setzen und macht ihn danach sofort in der LIVA-UI sichtbar. |
| `/training/plan-control` | alle anderen Plan-Control-Commands | `logged_only`; aktive produktive Plaene werden nicht umgeschaltet. |
| `/recovery/control` | `set_alcohol_flag`, `set_sickness_flag` | `live_mutation`; schreibt echte HRV-Flags in `hrv_measurements`. |
| `/recovery/control` | `set_sleep_flag`, `annotate_day` | `live_mutation`; schreibt `recovery_day_annotations` in der HRV-DB. |
| `/nutrition/control` | `adjust_targets` | `live_mutation`; schreibt echte `nutrition_settings` und aktive Week-Template-Targets, falls vorhanden. |
| `/nutrition/control` | `log_planned_meal`, `quick_log_meal`, `edit_logged_meal`, `delete_logged_meal` | `live_mutation`; `log_planned_meal` bestätigt geplante Slots und nutzt standardmäßig exakt die Plan-Items. `edit_logged_meal` kann mit `append_items=true` echte Foods zu einem bestehenden Meal hinzufügen. Alle Meal-Commands schreiben echte `nutrition_logged_meals` und synchronisieren Tageswerte via `weight_logs`. |
| `/nutrition/control` | `create_nutrition_plan` | `live_mutation`; erstellt eine komplette neue aktive Week-Template-Struktur mit echten Meals, Foods und Slots. Nicht auflösbare Foods brechen den Write ab, außer `allow_partial=true`. |
| `/weight/control` | `log_weight`, `edit_weight`, `delete_weight` | `live_mutation` mit Response-Mode `applied_live`; kanonische `weight_logs` werden mutiert und sind danach in `/weight/state` und `/weight/data` sichtbar. |
| `/weight/control` | `set_checkin_note` | `logged_only`; kein kanonischer Check-in-Note-Store vorhanden. |
| `/runs/control` | `create_run`, `delete_run` | `live_mutation`; legt echte Runs in `runs.runs` an oder loescht sie nach `run_id`. |
| `/runs/control` | `set_run_type`, `update_run_block` | `applied_sidecar`; Sidecar-Steuerung wird in `/runs/state` als `next_run_type_source=v2_sidecar` und in `sidecar_controls` sichtbar. |
| `/runs/control` | `mark_run_skipped` | `logged_only`. |
| `/liva/act domain=memory` | `store_fact`, `store_preference`, `store_pattern`, `append_log_entry`, `append_section_note` | `live_mutation`; erstellt private usememos-Eintraege mit `#gpt` als erstem Text. |
| `/liva/act domain=memory` | `update_entry`, `remove_entry`, `archive`, `mark_stale` | `live_mutation`; darf ausschliesslich usememos-Eintraege mutieren, deren Content mit `#gpt` beginnt. |
| `/liva/act domain=memory` | `cleanup_expired` | `dry_run` per Default; echte TTL-Loeschung nur mit `dry_run=false` und `confirm=true`. |
| `/access/control` | `create_guest_key` | `live_mutation`; erzeugt genau einen read-only Gast-Key in `auth.access_keys`, gibt den Klartext-Key nur einmal zurueck und niemals mit Schreibrechten. |
| `/memory/link` | alle Link-Commands | `logged_only`; noch kein kanonischer Link-Store. |
| `/remote/control` | alle Remote-Commands mit `dry_run=true` oder ohne Feld | `dry_run`. |
| `/remote/control` | `device_set_power`, `device_toggle`, `set_brightness`, `set_color`, `activate_scene`, `pc_action=wake/shutdown` mit `dry_run=false` | `live_mutation` mit Response-Mode `applied_live`, wenn der bekannte Adapter erfolgreich ausfuehrt; sonst Fehler. Unsupported Target/Aktion-Kombinationen werden auch im Dry-Run abgelehnt. |
| `/remote/control` | `pc_action=status` mit `dry_run=false` | `logged_only`; Status ist eine Read-artige Aktion. |

Remote erlaubt nur bekannte Ziele:

- Tapo/Kasa Devices aus `smart_home.config.TAPO_DEVICES`
- Govee Devices aus `smart_home.config.GOVEE_DEVICES`
- optional begrenzt per `LIVA_ACTIONS_V2_REMOTE_DEVICES`
- Szenen: `desk_on`, `desk_off`, `focus_mode`, `chill_mode`, `shutdown_room`
- PC-Aktionen: `wake`, `shutdown`, `status`

Erlaubte Live-Aktionen pro Target-Typ:

| Target-Typ | Erlaubte Actions |
| --- | --- |
| Tapo/Kasa Device | `device_set_power`, `device_toggle` |
| Govee Device | `device_set_power`, `device_toggle`, `set_brightness`, `set_color` |
| Scene | `activate_scene` mit bekannter Szene |
| PC | `pc_action=status`, `pc_action=wake`, `pc_action=shutdown`; nutzt die bestehenden `/api/remote/pc/status`, `/api/remote/pc/wake` und `/api/remote/pc/shutdown`-Helper. Nur `wake` und `shutdown` sind Mutationen. |

Remote fuehrt keine freien Shell-Kommandos, freien Pfade oder beliebige Device-Namen aus. PC-Wake nutzt nur den fest konfigurierten Wake-Befehl aus Host, Interface und MAC; Shutdown nutzt nur den konfigurierten Windows-HTTP-Listener mit Token.

## Entity Resolution

Trainings- und Progression-Endpunkte nutzen einen zentralen V2-Resolver fuer Uebungen. Unterstuetzt werden:

- exakter DB-Name, z. B. `Latzug`
- Display-Name mit Klammer, z. B. `Latzug (eGym)`
- Kombination aus Grundname und Variation/Device
- case-insensitive Matching
- Normalisierung von Leerzeichen, Bindestrichen und Klammern

Relevante Responses enthalten ein `resolution`-Objekt:

```json
{
  "requested": "Latzug (eGym)",
  "status": "resolved",
  "resolved_to": {
    "exercise_key": "latzug__egym__bilateral",
    "name": "Latzug",
    "variation": "eGym",
    "device": "eGym",
    "display_name": "Latzug (eGym)"
  },
  "alternatives": []
}
```

Leere Daten werden differenziert:

- `ok`: Daten vorhanden
- `no_matching_points`: Entity geloest, aber keine Punkte im Filter
- `unresolved`: Entity konnte nicht geloest werden
- `no_rows_in_range`: Datenquelle vorhanden, Zeitraum leer
- `source_missing`: Datenquelle fehlt

## Targets Und Domain-Status

`nutrition/state` liefert aktive Targets mit Quelle:

```json
{
  "active_targets": {"kcal": 2650, "protein": 180, "carbs": 300, "fat": 65},
  "targets_source": "live_plan",
  "targets_source_detail": {"live_plan_changed": true}
}
```

Moegliche `targets_source`-Werte:

- `live_plan`: aus einem aktiven Nutrition-Plan oder Template
- `command_log`: V2-Sidecar-Override aus `/nutrition/control`
- `fallback`: Settings/Fallback-Quelle

`nutrition/control.adjust_targets` mutiert jetzt echte Nutrition-Ziele: `nutrition_settings` wird aktualisiert, und aktive Week-Template-Day-Targets werden mitgezogen, wenn eine aktive Vorlage vorhanden ist. Die Response meldet `execution.mode = applied_live`.

`domain_status.status` bedeutet:

- `fresh`: aktuelle Tages- oder ausreichend dichte Wochenlogs sind vorhanden
- `partial`: Struktur/Targets/Foods/Meals sind vorhanden, aber aktuelle Logs sind unvollstaendig
- `stale`: nutzbare Struktur existiert, letzte Logs sind zu alt
- `missing`: kaum verwertbare Nutrition-Struktur vorhanden

## Runs State

`runs/state` trennt aktiven Run-Block und naechsten Run-Typ. Ein V2-Sidecar-Override aus `/runs/control set_run_type` hat Vorrang vor dem Weekly-Plan und erscheint als `next_run_type_source = "v2_sidecar"` plus `next_run_type_source_detail`. Ohne Override faellt V2 auf `weekly_plan` oder `null` zurueck.

## Endpoints

1. `GET /api/v2/actions/liva/today`  
   Kompakter Tagessnapshot fuer GPT.

2. `GET /api/v2/actions/liva/context`  
   Langfristiger Ziel-, Plan- und Memory-Kontext.

3. `GET /api/v2/actions/liva/state`  
   Datenfrische und technische Verfuegbarkeit je Domaene.

4. `GET /api/v2/actions/core/state`  
   Aktueller CORE-Zustand aus der echten LIVA-CORE-Decision inklusive HEAVY/NORMAL/LIGHT und Override-Status. V2-Override hat Vorrang; kein blinder NORMAL-Fallback.

5. `POST /api/v2/actions/core/control`  
   Commands: `set_override`, `clear_override`.

6. `GET /api/v2/actions/training/state`  
   Aktiver Trainingsrahmen und letzte Sessions.

7. `POST /api/v2/actions/training/data`  
   Scopes: `sessions`, `plan_days`, `recent_summary`.

8. `POST /api/v2/actions/training/exercise-data`  
   Gezielte Uebungshistorie mit zentraler Exercise-Resolution, optionalen Best Sets und e1RM.

9. `POST /api/v2/actions/training/adjust`  
   Commands: `log_gym_session`, `replace_exercise`, `add_exercise`, `remove_exercise`, `change_sets`, `change_rep_range`, `change_rpe`, `change_variation`, `reorder_exercise`, `update_exercise_target`. `log_gym_session` ist live und schreibt OCR/Text-Einheiten in `workouts`, `exercises`, `sets`. `replace_exercise`, `change_sets`, `change_rep_range`, `change_rpe`, `change_variation` und `update_exercise_target` mutieren live den aktiven `gym_plans.plan_json`; unaufloesbare Tage oder Uebungen liefern strukturierte `400`-Fehler ohne Mutation.

10. `GET /api/v2/actions/training/plan-state`  
    Aktive Planversion, nutzbare `day -> label/events/exercises` Struktur und `current_exercise_map`. Default ist `?detail=compact`; `?detail=full` liefert Event- und Exercise-Objekte.

11. `POST /api/v2/actions/training/plan-control`  
    Commands: `activate_plan_version`, `rename_plan`, `duplicate_plan`, `archive_plan`, `set_active_days`, `create_training_plan`. `create_training_plan` unterstützt `dry_run`, verlangt für Live-Writes `confirm=true` und schreibt den neuen aktiven Plan in `gym_plans`, falls vorhanden, sonst fallback in `plans`.

12. `POST /api/v2/actions/progression/data`  
    Modes: `by_exercise`, `by_group`. Liefert Serien fuer GPT-Mustererkennung und nutzt dieselbe Exercise-Resolution wie Training.

13. `POST /api/v2/actions/progression/compare`  
    Vergleich von Zeitraeumen, aktuell `time_ranges`.

14. `GET /api/v2/actions/recovery/state`  
    Latest HRV, Baselines und Flags plus Polar-Schlaf, ANS- und Continuous-HR-Felder in einer GPT-tauglichen Struktur mit Legacy-Fallback.

15. `POST /api/v2/actions/recovery/data`  
    HRV-/Recovery-Zeitreihe inklusive Recovery-Annotationen und relevanter Polar-Felder pro Tag, wenn vorhanden.

16. `POST /api/v2/actions/recovery/control`  
    Commands: `set_sleep_flag`, `set_sickness_flag`, `set_alcohol_flag`, `annotate_day`. Alcohol/Sickness schreiben HRV-Flags; Sleep/Annotation schreiben Recovery-Annotationen.

17. `GET /api/v2/actions/nutrition/state`  
    Targets, heutige Actuals, Adherence, Logging-Streak und `domain_status` (`fresh`, `partial`, `stale`, `missing`).

18. `POST /api/v2/actions/nutrition/data`  
    Modes: `daily_totals`, `meals`, `planned_meals`. `meals` liest fuer einen Tag geloggte Meals mit Items/Makros plus Targets, Remaining und offene geplante Meals. `planned_meals` liest den konkreten Tagesplan mit Slots, Items und Status, damit GPT geplante Meals direkt loggen kann.

19. `POST /api/v2/actions/nutrition/control`  
    Commands: `adjust_targets`, `log_planned_meal`, `quick_log_meal`, `edit_logged_meal`, `delete_logged_meal`, `create_nutrition_plan`. `log_planned_meal` loggt geplante Slots nach `meal_number`, `meal`, `slot_id`, Text-Hint (`query`, `meal_name`, `title`) oder Uhrzeit, z. B. `{"command":"log_planned_meal","query":"Reis Hähnchen","logged_at":"2026-04-18T21:00:00"}`. Standardmäßig werden exakt die Plan-Items genutzt; freie `items` werden ignoriert, außer `override_items=true`. `edit_logged_meal` ersetzt Items standardmäßig; mit `append_items=true` werden neue Foods an das bestehende Meal angehängt. `create_nutrition_plan` unterstützt `dry_run`, verlangt für Live-Writes `confirm=true` und schreibt echte aktive Week-Templates mit Meals und Foods statt nur Logs.

20. `GET /api/v2/actions/weight/state`  
    Latest Weight, 7d Average, Trend und Check-in-Status.

21. `POST /api/v2/actions/weight/data`  
    Gewichtsdaten im Datumsbereich.

22. `POST /api/v2/actions/weight/control`  
    Commands: `log_weight`, `edit_weight`, `delete_weight`, `set_checkin_note`. `log_weight`, `edit_weight` und `delete_weight` mutieren echte `weight_logs`; `set_checkin_note` ist nur `logged_only`.

23. `GET /api/v2/actions/runs/state`  
    Run-Block, letzter Run im 14d-Fenster, letzter historischer Run und naechster Run-Typ.

24. `POST /api/v2/actions/runs/data`  
    Run-Sessions im Datumsbereich.

25. `POST /api/v2/actions/runs/control`  
    Commands: `create_run`, `delete_run`, `update_run_block`, `mark_run_skipped`, `set_run_type`. `create_run` legt einen echten Run an; `delete_run` loescht einen echten Run nach `run_id`; `set_run_type` und `update_run_block` bleiben V2-Sidecar-Hinweise.

26. `GET /api/v2/actions/memory/state`  
    Memory-Status, Topics, Prioritaeten, Domains und strukturierte Entry-Metadaten.

27. `POST /api/v2/actions/memory/data`  
    Modes: `status`, `context`, `file`, `all_files`, `by_topic`, `by_type`. `file` kann eine logische Memory-Datei im Volltext lesen; `all_files` liefert standardmäßig Metadaten ohne Rohtext, außer `include_raw=true`.

28. `POST /api/v2/actions/memory/store`  
    Commands: `propose`, `apply_write_plan`, `archive`, `replace_section`, `append_section_note`, `append_log_entry`, `mark_stale`, `store_fact`, `store_preference`, `store_pattern`, `update_entry`, `remove_entry`. `propose` schreibt nicht. Alle anderen Commands können echte Memory-Markdown-Dateien verändern; ohne explizites `logical_file` wird ueber die bestehenden logischen Routing-Regeln auf passende Memory-Dateien geleitet.

29. `POST /api/v2/actions/memory/link`  
    Commands: `link_entry_to_exercise`, `link_entry_to_phase`, `link_entry_to_goal`, `link_entry_to_domain`.

30. `POST /api/v2/actions/remote/control`  
    Commands: `device_set_power`, `device_toggle`, `set_brightness`, `set_color`, `activate_scene`, `pc_action`. Default ist `dry_run=true`; das ist keine Geraetesteuerung. Echte Ausfuehrung nur mit `dry_run=false` und bekanntem Adapter.

31. `POST /api/v2/actions/access/control`  
    Commands: `create_guest_key`. Erzeugt read-only Gast-Keys mit Default-Ablaufzeit 24h und maximal 7 Tagen.

## Beispiel-Requests

```bash
curl -H "Authorization: Bearer $LIVA_AI_READ_KEY" \
  http://localhost:5000/api/v2/actions/liva/today
```

```bash
curl -X POST http://localhost:5000/api/v2/actions/training/data \
  -H "Authorization: Bearer $LIVA_AI_READ_KEY" \
  -H "Content-Type: application/json" \
  -d '{"scope":"sessions","date_from":"2026-03-01","date_to":"2026-04-17","include_exercises":true,"include_sets":true}'
```

```bash
curl -X POST http://localhost:5000/api/v2/actions/core/control \
  -H "Authorization: Bearer $LIVA_AI_WRITE_KEY" \
  -H "Content-Type: application/json" \
  -d '{"command":"set_override","mode":"LIGHT","reason":"manual override for today"}'
```

## Lokal Testen

```bash
.venv/bin/python -m pytest tests/test_actions_v2_api.py -q
.venv/bin/python -m pytest tests/test_ai_read_api.py tests/test_core_api_guards.py tests/test_memos_client.py tests/test_remote_devices_api.py -q
```

Die Tests pruefen:

- Smoke-Test fuer alle 31 V2-Endpoints
- Auth-Verhalten
- strukturierte Validierungsfehler fuer ungueltige Commands
- Exercise-Resolution fuer Display-Namen und unresolved Cases
- stabile `exercise_key` Werte
- Konsistenz zwischen `liva/today`, `training/state`, `training/data` und `training/plan-state`
- Nutrition-Target-Quelle und Domainstatus
- Command-Execution-Metadaten mit den kanonischen Modi `applied_live`, `applied_sidecar`, `logged_only`, `dry_run`
- Remote-Control `dry_run`, gated `live_mutation` und ungueltige Ziele
- Memory-Serialisierung
- Memory-Store `store_*` als gated `live_mutation` und Memory-Link als ehrliches `logged_only`
- Runs-State mit historischen, aber nicht aktuellen Runs und sauberer `next_run_type_source`
- Sichtbarkeit von Runs-Sidecar-Steuerung in `/runs/state.sidecar_controls`
- Compact vs Full Plan-State
- konservative Trend-/Status-Ableitung bei Minimaldaten
- V2-OpenAPI mit exakt 30 Operationen
- V1 laeuft weiter neben V2

## OpenAPI

Die V2-Spezifikation liegt hier:

```text
openapi/liva-actions-v2.json
```

Die bestehende V1-Datei `openapi/openapi.yaml` bleibt unveraendert und wird nicht ueberschrieben.

## Aktivierungsstrategie

1. V2 lokal und im Staging mit `openapi/liva-actions-v2.json` testen.
2. Einen neuen Custom GPT Action Connector gegen `/api/v2/actions/...` anlegen.
3. V1-Connector unveraendert lassen, bis alle V2-Flows validiert sind.
4. Write-Commands einzeln freigeben: `AI_WRITE_ENABLED=1` ist weiterhin erforderlich.
5. Remote-Control nur nach separater Freigabe real ausfuehren. Standard ist `dry_run` und damit keine Geraetesteuerung.

## Risiken und offene Punkte

- Viele Command-Flows sind aktuell bewusst nur `logged_only`, wenn die bestehende Domaene keinen klaren Write-Service anbietet.
- Progression/Stagnation wird absichtlich nicht im Backend diagnostiziert.
- Memory-Store `store_*` kann Markdown live schreiben. Memory-Link bleibt `logged_only`; eine kanonische Link-Struktur fehlt.
- Remote-Control erlaubt nur bekannte Commands und Ziele. Keine freien Shell-Befehle, Pfade oder Arbitrary-Execution-Felder.
