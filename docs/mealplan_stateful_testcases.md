# Mealplan Stateful CORE - Structured Test Cases

Date baseline: `2026-03-11`  
Scope: Active Day Plan, Event History, Telegram Decision Layer

## 1. Contradictory Suggestions
- Setup: Two core proposals for the same slot (`shift_meal` 16:01 and 17:15).
- Expectation:
  - Exactly one active `suggestion_created` event per slot.
  - Telegram sends one suggestion only.
  - Second proposal supersedes previous internal suggestion state, no duplicate prompt.

## 2. Skip + Replan
- Setup: `meal 4 raus` on an open meal.
- Expectation:
  - `meal_skipped` event persisted.
  - `plan_rebuilt` event persisted.
  - Active plan marks meal as non-remindable.
  - Rest-of-day output recalculates from remaining open meals.

## 3. Log + Replacement Meal Reality
- Setup: meal switched/replaced and then logged with the replacement variant.
- Expectation:
  - `meal_logged` event persisted with replacement payload.
  - Active state for slot is `logged`.
  - Logged reality beats further switch/replace events.

## 4. Training Time Changed
- Setup: context signal `training spaeter`.
- Expectation:
  - Event captured (`training_time_changed`).
  - Next core proposal aligns pre/post timing to new training window.
  - Old timing reminder is suppressed.

## 5. Telegram Message Dedupe
- Setup: same reminder tick runs twice for same slot/time/title.
- Expectation:
  - second send blocked by `telegram_meal_message_log.dedupe_key`.
  - no repeated user-facing reminder.

## 6. Late-day Salvage Mode
- Setup: multiple open meals remain near late evening with large macro gap.
- Expectation:
  - day mode transitions to practical salvage behavior.
  - suggestions prefer merge/trim over snack-fragmentation.
  - max one active suggestion at a time.

## 7. Switch Persists After Reload
- Setup: `switch meal 2 to ...`, then rebuild state from DB.
- Expectation:
  - Active day plan still uses switched meal.
  - base meal title remains historical only.
  - no fallback to base variant in reminder/status.

## 8. Reminder After Switch Uses New Variant
- Setup: switched meal is still open and reminder window is reached.
- Expectation:
  - reminder shows effective switched title/items/macros.
  - original base title does not appear.

## 9. Snapshot After Replace Does Not Show Original as Open Future
- Setup: meal replaced before logging.
- Expectation:
  - snapshot shows replacement as active node.
  - original may exist only as history reference, not as open future meal.

## 10. Stale Base Guard
- Setup: switched/replaced meal plus core evaluation re-run.
- Expectation:
  - `stale_base_reference_blocked` true for slot.
  - renderer/reminder/status never reintroduce base title for active meal.
