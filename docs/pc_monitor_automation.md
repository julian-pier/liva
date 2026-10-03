# PC Monitor Automation

Die neue Automatik läuft als eigener Worker:

- Modul: `smart_home.pc_monitor_automation`
- Service: `systemd/liva-pc-monitor-guard.service`
- Zustand: `smart_home/data/pc_state.json`
- Zusatzsignale: `smart_home/data/pc_presence_signal.json`

## Erkennungslogik

Jeder Poll bewertet mehrere Signale gemeinsam:

1. TCP auf RDP-Port `3389`
2. zusätzliche TCP-Ports aus `LIVA_PC_MONITOR_EXTRA_TCP_PORTS`
3. optionaler HTTP-Listener-Ping zum Windows-PC
4. Ping nur als sekundäres Signal
5. frische `/api/pc-status/online` / `/offline`-Signale nur als Zusatzsignal

Regeln:

- Ein TCP- oder Listener-Treffer ist ein stark positives Signal.
- Ping allein schaltet einen bestätigten Offline-Zustand nicht auf AN.
- Ping plus frisches Online-Zusatzsignal reicht als robuste Kombi.
- Wenn der PC schon als AN gilt, darf ein einzelnes Zusatzsignal den Zustand stabil halten.
- Erst mehrere negative Polls hintereinander bestätigen `PC_CONFIRMED_OFF`.

## Zustände

- `PC_ON`
- `PC_MAYBE_OFF`
- `OFF_TIMER_RUNNING`
- `PC_CONFIRMED_OFF`

## Nachtlogik

- Vor `LIVA_PC_MONITOR_NIGHT_START_HOUR` gibt es kein Auto-Off.
- Nach bestätigtem Offline im Nachtfenster startet ein 60s-Timer.
- Kommt der PC in der Wartezeit zurück, wird der Timer abgebrochen.
- Läuft der Timer ab und der PC ist weiter aus, gehen die Monitore aus.

## Schonender Steckdosenbetrieb

Die Steckdosen werden nicht als dauerhafte Health-Checks verwendet. Eine
Verbindung erfolgt nur beim Wechsel `PC aus -> an`, beim Nacht-Ausschalten und
bei einem fehlgeschlagenen Schaltvorgang. Ein solcher Fehler wird gezielt nur
für die betroffene Steckdose und standardmäßig erst nach 15 Minuten erneut
versucht (`LIVA_PC_MONITOR_PLUG_FAILURE_RETRY_SECONDS`).

## Service

```bash
sudo cp /opt/liva/systemd/liva-pc-monitor-guard.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now liva-pc-monitor-guard.service
sudo systemctl restart liva-pc-monitor-guard.service
sudo journalctl -u liva-pc-monitor-guard.service -f
```

## Konfiguration

- Der Service lädt optional `/opt/liva/.env.pc-monitor`.
- Eine Vorlage liegt unter `docs/pc_monitor_automation.env.example`.

## Diagnose

1. `curl -s http://127.0.0.1:5000/api/pc-status/state | jq`
2. `sudo journalctl -u liva-pc-monitor-guard.service -n 200`
3. Prüfen, ob `last_evaluation.checks` die erwarteten TCP-/Ping-/Signal-Ergebnisse zeigt.
4. Prüfen, ob `pending_shutdown_deadline` nur nachts gesetzt wird.
