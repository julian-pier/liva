# Einheitlicher Trainingsfortschritt (`progression_rules_v3`)

## Zentrale Architektur

- Persistierte Wahrheit: `workouts` -> `exercises` -> `sets`.
- Optionale Quelleingabe: `workouts.raw_import_text`.
- Reproduzierbare Textansicht: `analysis.training_parser.normalized_training_text`.
  Fehlt ein echter Rohtext, wird dieser Text beim Schreiben persistiert und bei
  alten Datensätzen beim Lesen aus der strukturierten Wahrheit erzeugt.
- Einzige Session-Progress-Engine: `analysis.training_progress`.
- Einzige Low-Level-Regel: `analysis.progression_rules` mit
  `RULE_VERSION = progression_rules_v3`.
- Analyse, Trainingsdetail, Historienkarten, Kalender, Actions/GPT und
  Wochenaggregation konsumieren den fertigen Payload; sie klassifizieren keine
  Sätze selbst.

Die Engine sucht in der vollständigen Übungshistorie. Identität besteht aus
kanonischem Namen (einschließlich `exercise_aliases`), Gerät, Variante und
Laterality. Referenzqualität hat Vorrang vor Aktualität. Das Match ist
one-to-one und auf die Satzanzahl der vorherigen tatsächlichen Ausführung
begrenzt. Zusätzliche Sätze sind `unmatched_known_exercise`, nicht automatisch
eine neue Übung.

Jeder Session-Payload enthält `rule_version`, Eligible-/Comparable-Counts,
`improved`, `same`, `worse`, `new_exercise`,
`unmatched_known_exercise`, `unknown`, Coverage, Raten, Net Progress,
Matchqualität, Confidence, Status, Gründe und die einzelnen Setmatches.

Es werden keine Progresswerte persistiert. Änderungen und Löschungen werden
deshalb aus Rohdaten neu berechnet. Write-Flows invalidieren zusätzlich
Training-, Dashboard-, Today- und Next-Session-Snapshots; die App leert ihre
zugehörigen In-Memory-Caches.

## Read-only-Diagnose vom 2026-07-14

Ausgeführt ausschließlich auf `/tmp/training_progress_v3_diagnostic.sqlite3`,
einer SQLite-Backupkopie von `database/training.sqlite3`:

| Kennzahl | Vorher (rekonstruierte V2-Wege) | V3 |
|---|---:|---:|
| Durchschnittliche Coverage | 77,52 % | 85,95 % |
| Ø vergleichbare Sätze/Session | 8,39 | 9,25 |
| Sessions ohne sichtbares Ergebnis | 59 | 3 |
| widersprüchliche alte Consumer-Sessions | 231 | 0 (gemeinsamer Payload) |

V3-Verteilung: 142 positiv, 117 stabil, 24 negativ, 3 ohne Vergleich.
31 Sessions haben niedrige, sehr niedrige oder keine Confidence. Es wurden
keine Snapshot-Referenzen auf gelöschte Session-IDs gefunden.

Die noch vorhandene Session 322 `FB Pull A` vom 14.07.2026 hat 8 Eligible Sets.
Die rekonstruierte positionsbasierte V2-Analyse liefert 4/0/4, der alte
Kalenderweg 4 verbessert/4 schlechter. V3 findet anhand Vollhistorie und
qualitätspriorisiertem one-to-one Matching 7 verbessert/1 gleich/0 schlechter
bei 8/8 Coverage und hoher Confidence. Die zuvor beobachteten 6/0/2 und
4/1/3 stammen damit aus den unterschiedlichen Last-Slot-, Positions- und
Consumerwegen beziehungsweise deren damaligem Daten-/Snapshotstand; die
Rohdaten reichen nicht aus, den bereits überschriebenen exakten Snapshot
nachträglich wiederherzustellen.

## Diagnose ausführen

```bash
sqlite3 database/training.sqlite3 ".backup /tmp/training-progress-copy.sqlite3"
.venv/bin/python scripts/diagnose_training_progress_v3.py \
  --database-copy /tmp/training-progress-copy.sqlite3
```

Das Skript verweigert die produktive Standard-Datenbank und öffnet die Kopie
mit SQLite `mode=ro`.

## Deploy und Restart

Es ist keine Datenbankmigration nötig und es werden keine Rohdaten verändert.
Nach Deployment und erfolgreichen Tests reicht der normale App-Restart:

```bash
sudo systemctl restart liva.service
sudo systemctl status liva.service --no-pager
```

Falls die Unit auf diesem Host anders heißt, zuerst read-only ermitteln:

```bash
systemctl list-units --type=service | grep -i liva
```

Vor Abschluss der Tests und Diagnose wurde kein produktiver Restart ausgeführt.
