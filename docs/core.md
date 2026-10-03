# CORE Graph (/core)

## Ziel
Ein einziger `/core` Screen mit:
- Finder (Suche, Pins, Heute, letzte Aktionen)
- Local-Graph/Global-Graph (Node/Edge aus SQL)
- Bundle-Panels (Training, Recovery, Run, Ernaehrung, Plan, Autopilot, Insights)
- Backlinks (Incoming/Outgoing)

Kein Seitenwechsel: Fokuswechsel passiert innerhalb derselben Ansicht.

## API
- `GET /core`
- `GET /api/core/graph?focus=<node_id>&depth=2&max_nodes=250&max_edges=600&global=0|1`
- `GET /api/core/search?q=<term>`
- `GET /api/core/recent`
- `GET /api/core/stream` (SSE, Event `new_data`)
- `GET /api/core/rebuild` (manueller Rebuild)

Legacy-Endpoints (`/api/core/state`, `/api/core/snapshot`, ...) bleiben kompatibel vorhanden.

## Node IDs
- `day:YYYY-MM-DD`
- `gym_session:<id>`
- `exercise:<id>`
- `run:<id>`
- `hrv:YYYY-MM-DD`
- `hr:YYYY-MM-DD`
- `meal:YYYY-MM-DD`
- `planblock:<id>`
- `autopilot:YYYY-MM-DD`
- `insight:<id>`

## Index-Tabellen
In `database/core.sqlite3`:
- `core_node_index(id, type, title, subtitle, ts, weight, json)`
- `core_edge_index(source, target, type, weight, ts)`
- `core_index_meta(key, value)`

Indizes:
- `idx_edge_source`, `idx_edge_target`, `idx_edge_type`
- `idx_node_type`, `idx_node_ts`

## Rebuild
Manuell:
```bash
./scripts/rebuild_core_index.py
```

Oder per API (auth/CORE-Zugriff erforderlich):
```bash
curl -s "http://localhost:5000/api/core/rebuild"
```

Empfohlen: hourly Cron/systemd-Timer auf `scripts/rebuild_core_index.py`.

## Neue Node-Typen erweitern
1. In `core/core_graph.py` Node-Type + ID-Konvention ergänzen.
2. Im Rebuild neue SQL-Quelle lesen und Nodes/Edges erzeugen.
3. Optional Farbe in `static/js/core.js` (`TYPE_COLORS`) ergänzen.
4. Optional Panel-Builder (`_build_panels`) um domänenspezifische Ansicht erweitern.

## Performance-Defaults
- Local Graph Default: `depth=2`, `max_nodes=250`, `max_edges=600`
- Global Graph: nur bei explizitem Toggle und weiterhin hart limitiert
- In-Memory TTL/LRU Cache für Graph-Payloads
- SSE statt Dauer-Polling; zusätzlich 30s Refresh nur bei aktivem Tab
