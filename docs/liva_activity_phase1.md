# LIVA Activity – Phase 1

## Architektur und Datenschutz

Die Chrome-/Edge-Extension sendet bei Fokus-, Tab- und Titeländerungen ausschließlich Browsername, Fokusstatus, normalisierte Domain, Seitentitel und Zeitstempel an `127.0.0.1`. Der Windows-Agent ist die Source of Truth: Er liest über Win32 das Foreground Window, PID, Prozessname und die Dauer seit der letzten Eingabe. Es werden keine Tasten, Mauskoordinaten, Zwischenablagen, Screenshots, Formulare oder vollständigen URLs erfasst.

Der Agent aggregiert Beobachtungen in UUID-stabile Segmente, checkpointed laufende Segmente und schreibt sie zuerst in einen lokalen SQLite-Spool. Bestätigte HTTPS-Batches werden anschließend als synchronisiert markiert. Das Backend upsertet nach Event-UUID in `database/digital_activity.sqlite3`; `/activity` liest Timeline und Summen in der Zeitzone Europe/Berlin.

## Backend auf dem LIVA-Mini-PC

Das Schema wird mit der bestehenden Flask-App automatisch initialisiert. Der Ingest verwendet einen eigenen, ausschließlich für LIVA Activity bestimmten Schlüssel. Er ist von Custom GPT, AI-Write und MCP getrennt:

```text
LIVA_ACTIVITY_INGEST_KEY=<starker-zufälliger-key>
```

Nach dem normalen Neustart der LIVA-Anwendung stehen diese Routen bereit:

- `POST /api/activity/ingest`
- `GET /api/activity/timeline?date=YYYY-MM-DD`
- `GET /api/activity/summary?date=YYYY-MM-DD`
- `GET /activity`

`digital_activity.sqlite3` wird vom bestehenden SQLite-Backup-Workflow berücksichtigt.

## Windows-Agent vorbereiten

Voraussetzung ist eine aktuelle 64-Bit-Python-Version. Im Repository-Verzeichnis unter Windows:

```powershell
py -m venv .venv-activity
.\.venv-activity\Scripts\Activate.ps1
pip install -r clients\liva_activity_windows\requirements.txt
```

Konfiguration als Benutzer-Umgebungsvariablen setzen. Der Server muss HTTPS verwenden; unverschlüsseltes HTTP wird nur für lokale Tests auf localhost akzeptiert.

```powershell
[Environment]::SetEnvironmentVariable("LIVA_ACTIVITY_SERVER_URL", "https://DEIN-LIVA-HOST", "User")
[Environment]::SetEnvironmentVariable("LIVA_ACTIVITY_API_KEY", "DEIN_ACTIVITY_INGEST_KEY", "User")
[Environment]::SetEnvironmentVariable("LIVA_ACTIVITY_LOCAL_TOKEN", "EIN_EIGENER_ZUFAELLIGER_PAIRING_TOKEN", "User")
[Environment]::SetEnvironmentVariable("LIVA_ACTIVITY_DEVICE_NAME", "Windows-PC", "User")
```

Ein Token lässt sich beispielsweise mit `py -c "import secrets; print(secrets.token_urlsafe(32))"` erzeugen. Agent starten:

```powershell
py -m clients.liva_activity_windows
```

Der Spool und die stabile Device-ID liegen standardmäßig unter `%USERPROFILE%\.liva-activity\`. Der Browser-Endpoint bindet ausschließlich an `127.0.0.1:8765`.

## Chrome- oder Edge-Extension laden

1. `chrome://extensions` beziehungsweise `edge://extensions` öffnen.
2. Entwicklermodus aktivieren.
3. „Entpackte Erweiterung laden“ wählen und `clients/liva_activity_browser_extension` auswählen.
4. Die Detailseite der Extension öffnen und „Erweiterungsoptionen“ wählen.
5. Denselben Wert wie `LIVA_ACTIVITY_LOCAL_TOKEN` eintragen.

Die Berechtigungen sind bewusst auf `tabs`, lokalen Extension-Speicher und `http://127.0.0.1/*` begrenzt. Hintergrundtabs werden zwar vom Browser verwaltet, aber niemals als Nutzungszeit übernommen: Der Agent akzeptiert Browserkontext nur, wenn Chrome beziehungsweise Edge wirklich das Windows-Foreground-Programm ist.

## Funktion prüfen

Agent und Extension starten, zwischen Anwendungen und Browser-Tabs wechseln, anschließend mindestens 15 Sekunden warten. Dann `/activity` auf LIVA öffnen.

Wenn ein Browser-Tab mit `/activity` geöffnet ist, meldet die Extension nur diesen Zustand an den lokalen Agenten. Der Agent synchronisiert dann im 1-Sekunden-Modus; ohne offenes Dashboard fällt er auf 10 Sekunden zurück. Die Seite selbst lädt sichtbar jede Sekunde, im Hintergrund nur alle 20 Sekunden. Vollständige Dashboard-URLs werden dabei weder an LIVA übertragen noch persistiert.

Ein Activity-Tag wird intern in `Europe/Berlin` von 04:00 Uhr bis 04:00 Uhr geführt. Timeline und Summary schneiden überlappende Segmente exakt an dieser Grenze; das Frontend wählt vor 04:00 Uhr automatisch den vorherigen Aktivitätstag.

Die Idle-Erkennung bleibt eingabebasiert und datensparsam. Bis zur Schwelle von standardmäßig 180 Sekunden läuft die Aktivzeit optimistisch weiter. Wird die Schwelle erreicht, setzt der Agent den Beginn der Inaktivität rückwirkend auf die letzte Windows-Eingabe; die zuvor vorläufig gezählten 180 Sekunden werden wieder aus der aktiven Nutzung entfernt. Auf einer begrenzten Liste bekannter Medienseiten hält ein frisches Boolean-Signal für laufendes Video/Audio die sichtbare Wiedergabe aktiv; zusätzlich wird das native `audible`-Signal des Tabs berücksichtigt. Das Signal verfällt nach 45 Sekunden ohne Extension-Heartbeat. Eine lediglich geöffnete App – einschließlich Spiel, Launcher oder Desktop – verhindert Idle niemals. Sperrbildschirm und Sleep überschreiben die Medienausnahme immer. Cursorpositionen, Tasten, Screenshots, Medieninhalte, Wiedergabepositionen und Formulardaten werden weiterhin nicht erfasst.

Website-Subdomains und bekannte App-/Spiele-Executables werden über explizite Aliaslisten auf kanonische Anzeigenamen gruppiert. Browser bleiben dabei Apps; die Website ist Metadatum desselben Segments und erhöht die aktive PC-Gesamtzeit nicht zusätzlich.

Ein isoliertes Backend-Testevent lässt sich in PowerShell so erzeugen (URL und Key ersetzen):

```powershell
$now = [DateTime]::UtcNow
$payload = @{
  batch_id = [guid]::NewGuid().ToString()
  device = @{ id = [guid]::NewGuid().ToString(); name = "Manual-Test"; platform = "windows"; agent_version = "manual" }
  events = @(@{
    id = [guid]::NewGuid().ToString()
    started_at = $now.AddMinutes(-5).ToString("o")
    ended_at = $now.ToString("o")
    app_exe = "manual-test.exe"; app_name = "Manueller Test"; window_title = "Phase 1"
    browser = $null; domain = $null; page_title = $null; is_idle = $false; source = "manual_test"
  })
} | ConvertTo-Json -Depth 5
Invoke-RestMethod -Method Post -Uri "https://DEIN-LIVA-HOST/api/activity/ingest" -Headers @{ Authorization = "Bearer DEIN_ACTIVITY_INGEST_KEY" } -ContentType "application/json" -Body $payload
```

Danach `/activity` neu laden. Das genaue JSON-Format und weitere Negativfälle stehen auch in `tests/test_activity_backend.py`.

Tests unter Linux/macOS:

```text
pytest tests/test_activity_backend.py tests/test_activity_windows_agent.py
node clients/liva_activity_browser_extension/test_domain.js
```

Diese Tests decken Backend, Aggregation, Idle, Browser-Merge, Spool und Retry ab. Die echten Win32-Aufrufe müssen auf Windows geprüft werden.

## Autostart vorbereiten (noch kein Installer)

Nach erfolgreichem manuellen Windows-Test kann über die Windows-Aufgabenplanung eine Aufgabe „Bei Anmeldung“ erstellt werden. Programm ist `...\.venv-activity\Scripts\pythonw.exe`, Argumente sind `-m clients.liva_activity_windows`, „Starten in“ zeigt auf das Repository. Zunächst `python.exe` verwenden, damit Logs sichtbar bleiben; erst nach stabiler Prüfung zu `pythonw.exe` wechseln. Ein Installer oder endgültiges Deployment ist nicht Teil von Phase 1.

Für die einfache dauerhafte Einrichtung steht `clients/liva_activity_windows/install_activity_agent.ps1` bereit. Es schreibt die drei benötigten Benutzer-Umgebungsvariablen, registriert die Aufgabe `LIVA Activity Agent` mit Neustart bei Prozessabbruch und startet sie sofort:

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\clients\liva_activity_windows\install_activity_agent.ps1 -ActivityKey $activityKey -PairingToken $pairingToken
```
