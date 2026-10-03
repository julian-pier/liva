# GPT Actions Surface

## Exposed To GPT

- `getLivaToday`
- `getLivaState`
- `getCoreBoard`
- `getCoreMorningContext`
- `buildCoreBoard`
- `getCoreTrainingCard`
- `getTrainingState`
- `getTrainingPlanState`
- `getTrainingData`
- `logGymSession`
- `getTrainingExerciseData`
- `getProgressionData`
- `compareProgression`
- `getRecoveryState`
- `getRecoveryData`
- `writeRecoveryFlag`
- `getNutritionState`
- `getNutritionData`
- `completePlannedMeal`
- `getWeightState`
- `getWeightData`
- `writeWeight`
- `getRunsState`
- `getRunsData`
- `writeCardio`
- `readCoreMemory`
- `buildCoreMemoryArtifacts`
- `controlCoreMemory`

## Kept Internal

- `buildCoreTrainingCard`
  Reason: Wird indirekt über `buildCoreBoard` ausgelöst.
- `getCoreMemoryLayers`
  Reason: Im GPT-Flow durch `readCoreMemory(resource="events"|"summary")` ersetzt.
- `promoteCoreMemoryEvent`
  Reason: Im GPT-Flow durch `controlCoreMemory(entity_type="event", action="promote")` ersetzt.
- `getCoreMemoryConsolidations`
  Reason: Im GPT-Flow durch `readCoreMemory(resource="consolidations")` ersetzt.
- `buildCoreMemoryConsolidations`
  Reason: Im GPT-Flow durch `buildCoreMemoryArtifacts(artifact="consolidations")` ersetzt.
- `controlCoreMemoryConsolidation`
  Reason: Im GPT-Flow durch `controlCoreMemory(entity_type="consolidation")` ersetzt.
- `batchCoreMemoryConsolidations`
  Reason: UI- und Review-spezifisch, nicht Kern-Flow für Custom GPT.
- `getCoreMemoryFilePatches`
  Reason: Im GPT-Flow durch `readCoreMemory(resource="file_patches")` ersetzt.
- `buildCoreMemoryFilePatches`
  Reason: Im GPT-Flow durch `buildCoreMemoryArtifacts(artifact="file_patches")` ersetzt.
- `controlCoreMemoryFilePatch`
  Reason: Im GPT-Flow durch `controlCoreMemory(entity_type="file_patch")` ersetzt.
- `getCoreState`
  Reason: Debug/Legacy.
- `setCoreOverride`
  Reason: Debug/Manual Override, nicht Standard-Coaching-Flow.
- `getLivaContext`
  Reason: Für Custom GPT zunächst zu breit; kann später gezielt ergänzt werden.
- `writeNutrition`, `getMemoryState`, `getMemoryData`, `writeMemory`, `controlRemote`, `normalizeTrainingImport`, `getTrainingImportCatalog`
  Reason: Nützlich intern oder in Spezial-Flows, aber nicht Teil der kuratierten Core-GPT-Fläche.

## Notes

- Die vollständige V2-API hat genau eine kanonische Quelle: `openapi/liva-actions-v2.json`.
- Das Custom-GPT-Schema lebt separat in `openapi/liva-actions-gpt.json` und `openapi/liva-actions-gpt.yaml`.
- Es kombiniert `readLiva`/`actLiva` als breite Fassade mit typisierten Kernrouten für Morning Context/Board/Card, Gym-Logging und historische Edits sowie Trainings-/Ernährungsplan-Details und Patches.
- Direkte Kernrouten verwenden die tatsächlich registrierten `/api/v2/actions/...`-Pfade; die öffentlichen `/actions/...`-Aliases bleiben die interne Kompatibilitätsschicht.
- Aktueller GPT-Schema-Umfang: 20 Operations.
- Read-only Actions sind non-consequential.
- Mutierende Actions bleiben grundsätzlich consequential, außer klar begrenzte GPT-Logging-Flows und `buildCoreBoard`.
- `buildCoreBoard` ist im GPT-Schema approval-freundlich markiert, weil es der erwartete Morning-Write ist.
- `completePlannedMeal`, `writeWeight`, `writeRecoveryFlag` und `writeCardio` sind im GPT-Schema approval-freundlich, weil sie enge, explizite Logging-Flows sind.
- `controlCoreMemory` bleibt confirmation-sensibel, weil darüber auch Apply-/Memory-File-Schritte laufen.
- Bei "Wie sieht meine Training-Card heute aus?" soll GPT `getCoreTrainingCard` nutzen, nicht `getTrainingPlanState(detail=full)`.
- Bei "Gewicht ist drin" soll GPT lesen, nicht schreiben. `writeWeight` nur mit explizitem Zahlenwert.
- Bei Recovery-Flags und Cardio-Logs nur auf ausdrückliche Logging-Intention schreiben.
- `writeCardio` ist für Ergo/Run/Cardio-Logs gedacht.
- `logGymSession` ist für Gym-Session-Logs aus Text oder Foto/OCR gedacht.
- Foto selbst wird nicht an LIVA gesendet; GPT extrahiert Text oder strukturierte Übungen und sendet nur JSON.
- Bei "logge Frühstück" soll GPT erst `getNutritionData(mode="planned_meals")` lesen und dann `completePlannedMeal` für genau einen geplanten Slot nutzen.
- Bei "Training fertig", "Gym loggen" oder "Foto vom Notizbuch loggen" soll GPT zuerst `logGymSession` mit `dry_run=true` verwenden und erst nach Bestätigung mit `dry_run=false` schreiben.
- `buildCoreMemoryArtifacts(artifact="events")` und `controlCoreMemory` bleiben approval-pflichtig.
- `buildCoreMemoryArtifacts(artifact="events")` bleibt für GPT ein langsamer Deferred-Pfad und soll nicht blind im normalen Chat ausgelöst werden.
