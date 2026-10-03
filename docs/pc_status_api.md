# PC Status API

Die Endpunkte unter `/api/pc-status/*` schalten keine Monitore mehr direkt.
Sie schreiben nur noch Zusatzsignale, die vom Hintergrunddienst `smart_home.pc_monitor_automation` in die kombinierte PC-Erkennung einbezogen werden.

## Endpunkte
- `POST /api/pc-status/online`
- `POST /api/pc-status/offline`
- `GET /api/pc-status/state`

## Verhalten
- `online` setzt ein frisches Online-Zusatzsignal.
- `offline` setzt ein frisches Offline-Zusatzsignal.
- `state` liest den zuletzt persistierten Worker-Zustand aus `smart_home/data/pc_state.json`.

## Curl-Tests
```bash
curl -s -X POST http://127.0.0.1:5000/api/pc-status/online | jq
curl -s -X POST http://127.0.0.1:5000/api/pc-status/offline | jq
curl -s http://127.0.0.1:5000/api/pc-status/state | jq
```

## Wichtige Felder in `state`
- `state`: `PC_ON`, `PC_MAYBE_OFF`, `OFF_TIMER_RUNNING` oder `PC_CONFIRMED_OFF`
- `is_online`: kompatibles Online-Flag für andere Projektteile
- `pending_shutdown_deadline`: aktiver Ausschalt-Timer nach 22:00 Uhr
- `shutdown_executed_for_cycle`: Monitore wurden in diesem Offline-Zyklus automatisch ausgeschaltet
- `last_evaluation`: letzte kombinierte Bewertung inklusive Check-Details
