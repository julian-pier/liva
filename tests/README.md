Run block-status scenario tests:

```bash
pytest -q tests/test_block_status_scenarios.py
```

Covered edge-cases:
- HRV missing data -> `hrv_observed="keine Daten"`, `hrv_verdict="neutral"`, no drift.
- Partial plan payload (missing phase/week in cycle) -> endpoint stays stable (200), preview still renders.
- Override events with missing/unknown `decision_type` are ignored and do not trigger drift.
- RPE threshold boundary at exactly `0.5` breach is pinned (off-by-one guard).
- German grammar is pinned for deload timing strings: `in 1 Tag` vs `in 2 Tagen`.

Run autopilot decision scenario tests:

```bash
pytest -q tests/test_autopilot_scenarios.py
```

Real-data context helper tests:

```bash
pytest -q tests/test_autopilot_explain_context.py
```

Late logging semantics:

```bash
pytest -q tests/test_autopilot_late_logging.py
```

Override metric whitelist:

```bash
pytest -q tests/test_autopilot_override_metrics.py
```

Cardio debt reintro policy:

```bash
pytest -q tests/test_autopilot_cardio_debt.py
```

Recovery-missing semantics:

```bash
pytest -q tests/test_autopilot_recovery_missing_semantics.py
```

What is pinned:
- deterministic decision (`train|light|rest|off`) for 30+ policy scenarios
- stable explain check order
- rule-trigger expectations (plan dominance, sickness window, plan-fit mismatch, gym-priority, late logging)
- boundary behavior (exact threshold for high RHR and low HRV)
- rest-as-last-resort behavior (rest stays rare)
- one-line non-technical user explanation (`user_one_liner`)
- cardio re-intro gates (`CARDIO_DEBT`, `RECOVERY_GOOD`, `NO_GYM_CONFLICT`, `PLAN_REST_Z2_ALLOWED`)
- expected-fatigue mapping (`days_to_deload<=2 => high`, late-week fallback)
- missing gym logs default assumption (`assume_completed_if_missing=True`) does not penalize decisions
- wording snapshot: `PLAN_REST_Z2_ALLOWED` failed case uses `plan-rest override disabled`
- compact CLI includes `Plan-Fit: expected=... · observed=... · passt|mismatch`

CLI explain output (no frontend):

```bash
PYTHONPATH=. ./venv/bin/python tools/autopilot_explain.py --day 2026-02-15 --scenario s1_plan_rest_forces_rest
```

Optional real data loader path:

```bash
PYTHONPATH=. ./venv/bin/python tools/autopilot_explain.py --day 2026-02-15 --use-real-data
```

Compact output:

```bash
PYTHONPATH=. ./venv/bin/python tools/autopilot_explain.py --day 2026-02-15 --scenario s10_build_high_pulse_and_down_light --explain-compact
```

Cardio re-intro example:

```bash
PYTHONPATH=. ./venv/bin/python tools/autopilot_explain.py --day 2026-02-15 --scenario s27_cardio_debt_plan_rest_allowed_reintro --explain-compact
```
