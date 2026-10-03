# Windows PC Status Setup (LIVA)

## 1) Raspberry-Pi URL anpassen
1. Öffne `pc_online.bat` und `pc_offline.bat`.
2. Standard ist bereits gesetzt auf `http://192.0.2.1:5000` (eth0).
   Alternative je nach Netzwerk:
   - WLAN: `http://192.0.2.10:5000`
   - Tailscale: `http://192.0.2.11:5000`

## 2) Online-Signal beim Windows-Start senden
1. Öffne **Task Scheduler**.
2. Erstelle eine neue Aufgabe (nicht nur "Basic Task").
3. Reiter **General**:
   - Name: `LIVA PC Online`
   - Optional: `Run with highest privileges` aktivieren.
4. Reiter **Triggers**:
   - Neuer Trigger: **At startup**
5. Reiter **Actions**:
   - Action: **Start a program**
   - Program/script: voller Pfad zu `pc_online.bat`
6. Reiter **Conditions**:
   - `Start the task only if the following network connection is available` deaktivieren, wenn Windows damit zickt.
7. Reiter **Settings**:
   - `Run task as soon as possible after a scheduled start is missed` aktivieren.
   - Falls nötig: `Delay task for` auf `30 seconds`, damit Netzwerk/Flask schon stehen.
8. Aufgabe speichern.

## 3) Offline-Signal beim Shutdown senden
1. Öffne `gpedit.msc`.
2. Gehe zu:
   - `Computer Configuration > Windows Settings > Scripts (Startup/Shutdown)`
3. Öffne **Shutdown**.
4. Klicke **Add...** und hinterlege `pc_offline.bat`.
5. Übernehmen und schließen.

## 4) Funktion testen
1. `pc_online.bat` manuell ausführen.
2. Auf dem Pi prüfen:
   ```bash
   curl -s http://127.0.0.1:5000/api/pc-status/state
   ```
3. `pc_offline.bat` manuell ausführen.
4. State erneut prüfen. Das Signal landet jetzt zusätzlich in `signal`.
5. Nach 22:00 Uhr prüfen, dass der Worker bei bestätigtem Offline `pending_shutdown_deadline` setzt.
6. Optional: nach Ablauf von ~1 Minute erneut State prüfen, ob `shutdown_executed_for_cycle=true` und `pending_shutdown_deadline=null` ist.

## Hinweise
- `pc_online.bat` und `pc_offline.bat` liefern nur Zusatzsignale. Die eigentliche Entscheidung trifft jetzt der LIVA-Worker über TCP, Ping und Zusatzsignale gemeinsam.
- `pc_online.bat` retried jetzt bis zu ca. 2 Minuten, falls beim Boot das Netzwerk oder Flask noch nicht bereit ist.
- `pc_offline.bat` retried kurz beim Herunterfahren, blockiert den Shutdown aber nicht lange.
- Bei Firewall-Problemen auf Windows oder LIVA zuerst lokale API-Erreichbarkeit und die RDP-/Zusatzports prüfen.
