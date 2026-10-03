# LIVA GPT Actions Endpoint Guide

Stand: GPT-Slim-Schema `openapi/liva-actions-gpt.yaml`

Server:

```text
https://liva.example.com
```

Der Custom GPT sieht die beiden breiten Fassaden plus die typisierten Kernoperationen im Schema:

- `readLiva`
- `actLiva`

Die breite Fallback-Fassade bleibt:

```text
POST /api/v2/actions/liva/act
```

Neuer Trainings-Flow:

1. `getDailyContext`
2. Wenn `training.needs_gpt_decision=true` oder `training.ai_decision.status` in `missing|stale|failed`: `getTrainingDecisionContext`
3. Entscheidung als JSON bauen
4. `saveTrainingDecision`
5. `getTrainingTodayCard` zur Kontrolle

`daily/context.training.ai_decision` ist die Statusquelle fuer den Training-Flow. Der Block liefert `status`, `decision_id`, `decision`, `headline`, `global_rule`, `items_count`, `created_at`, `saved_context_hash`, `current_context_hash`, `stale_reason` und optional `stale_reasons`.
`getTrainingTodayCard` nutzt dieselbe Statuslogik wie `daily/context`. Eine gespeicherte Entscheidung ist nur `fresh`, wenn `saved_context_hash == current_context_hash`; sonst ist sie `stale`.
`getTrainingDecisionContext` ist die eigentliche Entscheidungsgrundlage. Dort werden `recovery_context`, `bodyweight_context`, `calendar_context` und `user_checkin` kompakt aus denselben Tagesquellen wie `daily/context` aufgebaut. Diese Felder liefern Kontext und Interpretation, aber keine CORE-Trainingsentscheidung.
Im `readLiva`-Facade liefert `mode=training_today_decision_context` standardmaessig einen kompakten Payload fuer Custom GPT. Der volle Debug-Payload bleibt ueber den direkten GET-Endpoint oder `mode=training_today_decision_context_full` verfuegbar.
`mode=daily_context` im `readLiva`-Facade ist die Trigger-Quelle. Wenn `result.training` fehlt oder leer ist, darf GPT nicht still weitermachen.

Die im GPT-Schema enthaltenen direkten Write-Routen sind nur die typisierten Kernflüsse (Gym-Logging, historische Workout-/Set-Edits, CORE-Board-Build sowie Plan- und Nutrition-Patches). Nicht ausgewählte Spezial- und Sicherheitsrouten bleiben intern.

## Harte Regeln

- Keine alten direkten GPT-Write-Tools.
- Keine erfundenen Routen.
- Keine Sprache wie "LIVA kann das noch nicht offiziell", wenn der Command in der offiziellen Matrix steht.
- Bei Writes zuerst bei Bedarf lesen, dann `actLiva`, danach mit `readLiva` kontrollieren.
- Bei Änderungen an einem bestehenden Ernährungsplan: zuerst `getNutritionPlanDetailDirect(plan_id)`, dann `patchNutritionPlanDirect` mit `dry_run=true`, nach Freigabe mit `dry_run=false`, danach erneut `getNutritionPlanDetailDirect(plan_id).`
- Bei Nutrition-Operationen: `replace_slot_food` verwendet `replacement.food_id`; `add_slot` verwendet `food_id` plus `target.day_index`/`target.meal_index`. Food-Namen oder IDs niemals in `note` übertragen.
- Für bestehende Ernährungspläne niemals `liva_operator_execute`, einen alten Phase-4.1-Operator oder `replace_nutrition_plan` als Workaround verwenden. `slot_path` aus dem Detail-Read ist bindend.
- Fuer komplette neue Trainings- oder Ernährungspläne: erst `readLiva`, dann strukturiertes JSON bauen, dann `actLiva` mit `dry_run=true`, erst nach Nutzerfreigabe `confirm=true` für den Live-Write.
- Keine Plan-Writes ohne `confirm=true`.
- Target-Write Contract: Wenn ein Nutzer eine konkrete Ziel-ID, einen konkreten Plan oder eine konkrete Änderung nennt, ist dieses Ziel bindend. Wenn genau dieser Write fehlschlägt, sofort stoppen. Niemals Ersatzplan, Kopie, Replace, Activate, Archive, Delete oder neuen Plan als Workaround erzeugen ohne neue ausdrückliche Freigabe.
- Bei `create_nutrition_plan` zaehlt nur strukturierte `days` als echter Plan-Write. `raw_text` ist hoechstens Preview oder Notes und darf nie als gespeicherter Plan ausgegeben werden.
- Bei `create_nutrition_plan` muessen Summary und Readback dieselbe relationale Struktur treffen: neue Meal-Slots laufen ueber `nutrition_week_day_slots.meal_template_id` plus `nutrition_meal_ingredients`, nicht nur ueber Legacy-`nutrition_slot_meals`.
- Wenn `target_kcal/target_p/target_c/target_f` gesendet werden, muessen diese Werte im Template-Read sichtbar sein und duerfen nicht still durch alte Macro-Defaults ersetzt werden.
- Wenn ein Nutrition-Write fehlschlaegt: `Ich konnte den Plan nicht in LIVA speichern. Hier ist nur die Preview.`
- Bei Notizänderungen, Satz-/RPE-/Übungsänderungen an einem bestehenden Trainingsplan: immer `patch_training_plan`.
- Rolling-Sequence-Trainingspläne werden über `getTrainingPlanDetailDirect` gelesen; der Readback liefert `day_id="rotation"` und stabile `exercise_path`-Werte. Änderungen laufen über `patchTrainingPlanDirect` mit `match.exercise_path`, nicht über `/training/adjust` mit `day="rotation"`.
- Fuer Note-Edits an bestehenden Plaenen gilt: immer patch_training_plan.
- `replace_training_plan` nur verwenden, wenn der Nutzer ausdruecklich den gesamten Plan ersetzen will.
- Fuer solche Aenderungen gilt auch in Klartext: replace_training_plan nur verwenden, wenn der Nutzer ausdruecklich den gesamten Plan ersetzen will.
- Wenn `patch_training_plan` scheitert: STOPPEN. Nicht auf `replace_training_plan` ausweichen.
- Klartextregel: Nicht auf replace_training_plan ausweichen.
- Dann klar sagen: `Ich konnte den Patch nicht speichern. Kein Live-State wurde verändert.`
- Nach Live-Writes mit `patch_training_plan` immer Ruecklese-Check machen.
- Wenn Write-Response und Readback alte Werte zeigen: `Write response and read-back disagree.`
- Dann stoppen und keinen zweiten kreativen Patch, Replace oder Workaround versuchen.
- Bei `training_plan` Readback nach einem Patch nicht nur Mo/Di/Mi/Do pruefen, sondern auch `rotation`, falls vorhanden.
- Wenn `rotation` noch alte Werte spiegelt, gilt der Patch als nicht vollstaendig bestaetigt.
- Fuer `update_exercise_note` ist ein Patch erst dann sauber bestaetigt, wenn sowohl `detail="full"` als auch die persistente `gym_plans.plan_json`-Struktur keine alten Note-Spiegel mehr enthalten.
- Wenn die Action `failed_operations` oder `stale_paths` meldet, gilt der Patch als fehlgeschlagen und darf nicht als erfolgreicher Live-Write dargestellt werden.

## Official Command Matrix

```text
core: build_board, set_override, clear_override
training: log_gym_session, replace_exercise, add_exercise, remove_exercise, change_sets, change_rep_range, change_rpe, change_variation, reorder_exercise, update_exercise_target, create_training_plan
nutrition: adjust_targets, quick_log_meal, log_planned_meal, edit_logged_meal, delete_logged_meal, create_nutrition_plan, patch_nutrition_plan
weight: log_weight, edit_weight, delete_weight
cardio: create_cardio_session, delete_cardio_session, create_run, delete_run
recovery: annotate_day, set_sleep_flag, set_sickness_flag, set_alcohol_flag
endurance: set_execution_mode
memory: store_fact, store_preference, store_pattern, update_entry, remove_entry, replace_section, append_log_entry, append_section_note, apply_write_plan, archive, mark_stale
remote: device_set_power, device_toggle, set_brightness, set_color, activate_scene, pc_action
access: create_guest_key
```

Interne Routen:

- `domain=cardio` routet intern auf `/runs/control`
- `domain=weight` routet intern auf `/weight/control`
- `domain=nutrition` routet intern auf `/nutrition/control`
- `domain=training` routet intern auf `/training/adjust` oder `/training/plan-control`
- `domain=core` routet intern auf `/core/board/build` oder `/core/control`
- `domain=endurance` routet intern auf `/endurance/control`
- `domain=recovery` routet intern auf `/recovery/control`
- `domain=memory` routet intern auf Memory-Store/Control
- `domain=remote` routet intern auf `/remote/control`
- `domain=access` routet intern auf `/access/control`

Recovery:

- `readLiva` mit `mode=recovery_state` zeigt Polar-Schlaf, ANS, Continuous-HR und Legacy-HRV-Fallback in einer GPT-tauglichen Struktur.
- `actLiva` mit `set_sickness_flag` und `set_alcohol_flag` schreibt echte HRV-Flags.

Training-AI:

- `readLiva` mit `mode=training_today_decision_context` liefert den kompletten GPT-Entscheidungskontext fuer heute.
  Wichtige Trainingsfelder:
  - `session_type_context`: erkannter geplanter Session-Type und Matching-Regeln
  - `same_session_type_history`: letzte Workouts desselben Session-Types
  - `set_slot_reference_map`: echte Satz-fuer-Satz-Referenzen pro geplanter Uebung
  - `exercise_session_context`: gleiche vs. andere Session-Types fuer dieselbe Uebung
  - `related_session_family_work_sets`: verwandte, aber nicht identische Session-Familie wie Push vs. Push B
  - `context_quality`: Warnungen, ob alle geplanten Uebungen und Satz-Slots solide Referenzen haben
  - `reference_quality`: Einordnung wie `primary_exact`, `secondary_related`, `secondary_other`, `low_confidence`, `suspicious`
  - `last_top_reference` und `last_backoff_reference`: vollstaendige Herleitung fuer Top-/Backoff-Referenzen
  - `planned_sets_source`: Quelle der geplanten Satzanzahl je Uebung
  - In `exercise_trends.*.set_slot_reference_summary` gilt:
    `slots_without_primary_reference` = keine gleiche Session-Type-Referenz.
    `slots_without_any_reference` = ueberhaupt keine echte Referenz vorhanden.
- `readLiva` mit `mode=training_today_card` liefert die kompakte Dashboard-Card auf Basis der gespeicherten GPT-Entscheidung.
- `readLiva` mit `mode=daily_context` liefert im `training`-Block die Trigger-Felder `needs_gpt_decision`, `decision_reason` und `ai_decision.status`. Nutze diese Werte als Quelle, ob eine neue Entscheidung gebaut werden muss.
- `readLiva` mit `mode=training_today_decision_context` liefert die kompakte Tagesbasis fuer die Entscheidung. Verwende `training_today_decision_context_full` nur fuer Debug oder tiefe Audits.
- `actLiva` mit `domain=training` und `command=save_training_decision` speichert die konkrete Tagesentscheidung.
- Wenn `best_reference.use_for_prescription=false`, behandle die Referenz nur als Kontextsignal. Nutze sie nicht direkt als Gewichts-/Lastvorgabe.

## Request-Form

Top-Level-Felder und `payload` sind gleichwertig. Bei Plan-Patches wird auch `value` akzeptiert.

Diese beiden Requests muessen gleich behandelt werden:

```json
{
  "domain": "weight",
  "command": "log_weight",
  "date": "2026-05-04",
  "payload": {
    "weight": 70.2
  }
}
```

```json
{
  "domain": "weight",
  "command": "log_weight",
  "date": "2026-05-04",
  "weight": 70.2
}
```

## Read-Beispiele

Daily snapshot:

```json
{
  "mode": "daily_snapshot"
}
```

Gewicht lesen:

```json
{
  "mode": "weight_state"
}
```

Cardio kontrollieren:

```json
{
  "mode": "cardio_data",
  "date_from": "2026-05-04",
  "date_to": "2026-05-04"
}
```

Nutrition lesen:

- `mode=nutrition_state` fuer aktive Targets und Status
- `mode=nutrition_foods` fuer Food-Suche
- `mode=nutrition_plans` fuer bestehende Plan-Templates
- `mode=nutrition_timing` fuer Timing/Meal-Verteilung
- `mode=nutrition_data` nur mit zusaetzlichem Submodus in `payload.mode`

Gueltige `nutrition_data`-Submodi:

- `daily_totals`
- `meals`
- `planned_meals`

Beispiel fuer geplante Meals:

```json
{
  "mode": "nutrition_data",
  "date_from": "2026-05-04",
  "date_to": "2026-05-04",
  "payload": {
    "mode": "planned_meals"
  }
}
```

## Write-Beispiele Nur Ueber `actLiva`

Gewicht loggen:

```json
{
  "domain": "weight",
  "command": "log_weight",
  "date": "2026-05-04",
  "payload": {
    "weight": 70.2
  }
}
```

Cardio Ergo loggen:

```json
{
  "domain": "cardio",
  "command": "create_cardio_session",
  "date": "2026-05-04",
  "payload": {
    "sport_type": "ergo",
    "duration": "17:20",
    "avg_hr": 123
  }
}
```

Geplantes Meal loggen:

```json
{
  "domain": "nutrition",
  "command": "log_planned_meal",
  "date": "2026-05-04",
  "payload": {
    "meal_number": 1
  }
}
```

Freies Meal loggen:

```json
{
  "domain": "nutrition",
  "command": "quick_log_meal",
  "date": "2026-05-04",
  "payload": {
    "meal_name": "Snack",
    "logged_at": "2026-05-04T16:30:00",
    "items": [
      {
        "food_name": "Whey",
        "amount": 30,
        "unit": "g"
      }
    ]
  }
}
```

Kompletten Trainingsplan vorbereiten:

```json
{
  "domain": "training",
  "command": "create_training_plan",
  "dry_run": true,
  "payload": {
    "name": "Hybrid Build 6W",
    "block_length": 6,
    "set_active": true,
    "days": [
      {
        "day": "Mo",
        "events": [
          {
            "kind": "gym",
            "title": "Push",
            "time": "18:30",
            "items": [
              {
                "kind": "exercise",
                "name": "Schrägbankdrücken",
                "variation": "Smith",
                "sets": 3,
                "reps": {"min": 6, "max": 10}
              }
            ]
          }
        ]
      }
    ],
    "weeks": [{"week": 1, "phase": "Build"}]
  }
}
```

Bestehenden Trainingsplan gezielt patchen:

```json
{
  "domain": "training",
  "command": "patch_training_plan",
  "plan_id": 28,
  "dry_run": true,
  "operations": [
    {
      "op": "update_exercise_note",
      "match": {
        "day": "Mo",
        "exercise": "Schrägbankdrücken",
        "variation": "KH"
      },
      "note": "Hauptmission Road to 40s"
    }
  ]
}
```

Fuer `patch_training_plan` sind diese Shapes gueltig:

- `operations`
- `value.operations`
- `payload.operations`

Fuer gezielte Änderungen an bestehenden Trainingsplänen immer `patch_training_plan` verwenden, nicht `replace_training_plan`.

Kompletten Ernährungsplan vorbereiten:

```json
{
  "domain": "nutrition",
  "command": "create_nutrition_plan",
  "dry_run": true,
  "payload": {
    "name": "Standard Cut Woche",
    "set_active": true,
    "days": [
      {
        "day": "Mo",
        "meals": [
          {
            "time": "06:40",
            "title": "Brot + Marmelade",
            "items": [
              {"food_name": "Brot", "amount": 1, "unit": "pcs"},
              {"food_name": "Marmelade", "amount": 15, "unit": "g"}
            ]
          }
        ]
      }
    ]
  }
}
```

Top-Level `days` ist ebenfalls gueltig:

```json
{
  "domain": "nutrition",
  "command": "create_nutrition_plan",
  "dry_run": true,
  "name": "Standard Cut Woche",
  "target_kcal": 2700,
  "target_p": 180,
  "target_c": 360,
  "target_f": 60,
  "days": [
    {
      "day": "Mo",
      "meals": [
        {
          "time": "06:40",
          "title": "Brot + Marmelade",
          "items": [
            {"food_name": "Brot", "amount": 1, "unit": "pcs"},
            {"food_name": "Marmelade", "amount": 15, "unit": "g"}
          ]
        }
      ]
    }
  ]
}
```

Fuer echte Pläne immer strukturierte `days` verwenden:

- erlaubt: `days`
- erlaubt: `value.days`
- erlaubt: `payload.days`
- nicht als echter Write-Pfad: nur `raw_text`

Recovery annotieren:

```json
{
  "domain": "recovery",
  "command": "annotate_day",
  "date": "2026-05-04",
  "payload": {
    "note": "schlecht geschlafen"
  }
}
```

Gym Dry-Run:

```json
{
  "domain": "training",
  "command": "log_gym_session",
  "dry_run": true,
  "payload": {
    "date": "2026-05-04",
    "session_name": "Push",
    "raw_text": "..."
  }
}
```

Gym bestaetigt schreiben:

```json
{
  "domain": "training",
  "command": "log_gym_session",
  "dry_run": false,
  "confirm": true,
  "payload": {
    "date": "2026-05-04",
    "session_name": "Push",
    "raw_text": "..."
  }
}
```

CORE Board bauen:

```json
{
  "domain": "core",
  "command": "build_board",
  "confirm": true,
  "payload": {
    "date": "2026-05-04",
    "mode": "gpt_fast",
    "memory_mode": "skip",
    "build_training_card": true,
    "persist": true,
    "source_message": "Gewicht und HRV sind drin."
  }
}
```

## Sprachregel Fuer GPT

Wenn `actLiva` existiert und der Command in der offiziellen Matrix steht, darf GPT nie sagen:

- "LIVA kann das noch nicht offiziell"
- "ich kann das nicht direkt eintragen"
- "dafuer gibt es keinen offiziellen Write"

Stattdessen:

- bei Erfolg: `Ist eingetragen.`
- bei `confirmation_required`: `Geht, ich brauche dafuer kurz deine Bestaetigung.`
- bei `unsupported_command`: `Dieser konkrete Write ist noch nicht unterstuetzt.`
- bei Backendfehler: `Der Write ist vorgesehen, aber der Backend-Call ist gerade fehlgeschlagen: ...`

Bei einem fehlgeschlagenen Ziel-Patch gilt immer:

- Fehler nennen.
- Keine Speicherung behaupten.
- Keine Ausweich-Kopie oder Ersatzversion schreiben.
- Keine neue Plan-ID als angeblich "korrigierte Version" ausgeben.
- Stattdessen korrigierte Payload oder nächste Diagnose vorschlagen.
