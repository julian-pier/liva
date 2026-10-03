# LIVA StayFree iOS Bridge

Die Bridge importiert ausschließlich abgeschlossene, von StayFree synchronisierte
iPhone-Tageswerte. Sie ist kein Live-Tracker und läuft getrennt vom bestehenden
Windows-Foreground-Agenten.

Auf Windows installiert das Setup bei Bedarf automatisch das kleine Python-Paket
`tzdata`, damit die Europe/Berlin-Tagesgrenze inklusive Sommerzeit korrekt ist.

## Sicherheit und Datenfluss

- StayFree SQLite wird über SQLite URI `mode=ro` und `PRAGMA query_only=ON`
  ausschließlich lesend geöffnet.
- Die belegte Quelle ist die StayFree-Windows-App `3.5.10.0`, Datei
  `LocalCache\Roaming\StayFree\config.db`, Schlüssel
  `api-sessions-repo-find-sessions-cache`.
- Nur importierte Datensätze mit `platform = ios` werden berücksichtigt.
  Die getrennte lokale `usage.db` mit Windows-Sessions wird nie gelesen.
- Der Server akzeptiert zusätzlich ausschließlich `platform=ios` und
  `source=stayfree_ios`.
- Der vorhandene LIVA Activity API Key wird verwendet. Pairing-, AI- oder
  RemoteXPC-Schlüssel gehören nicht in diese Konfiguration.
- Der Key steht nie in den Argumenten der geplanten Aufgabe. Die lokale
  Konfigurationsdatei wird per Windows-ACL auf den Benutzer und SYSTEM begrenzt.

## Installation auf Windows

Im PowerShell-Fenster im Repository:

```powershell
cd C:\LIVA-Activity
Set-ExecutionPolicy -Scope Process Bypass
.\clients\liva_stayfree_ios_bridge\install_bridge.ps1
```

Der Installer fragt verdeckt nach dem vorhandenen **Activity API Key**. Wenn die
automatische Cache-Suche den Cache nicht eindeutig findet, kann der bereits im
Probe bestätigte SQLite-Pfad einmalig mitgegeben werden:

```powershell
.\clients\liva_stayfree_ios_bridge\install_bridge.ps1 `
  -StayFreeDbPath "C:\voller\Pfad\zu\StayFree\config.db"
```

Die geplante Aufgabe läuft ohne sichtbares Terminal um 04:15, 04:30, 05:00,
06:00 sowie als leichte Sicherheitsprüfung um 12:00, 18:00 und 23:00. Zusätzlich
läuft sie beim Login als Recovery. Ein bereits mit identischem Hash
importierter Tag wird nicht erneut übertragen. Ist StayFree noch nicht
synchronisiert oder LIVA nicht erreichbar, probiert die unsichtbare Bridge bis
zu drei Stunden lang alle fünf Minuten erneut. Ist der PC nachts ausgeschaltet,
holt Windows den Lauf beim nächsten Login nach. Netzwerkfehler bleiben außerdem
im lokalen Retry-State.

Bei der Erstinstallation prüft die Bridge den vorhandenen StayFree-Cache
read-only und importiert ausschließlich vollständig abgeschlossene Tage.
StayFree speichert Start und Ende in Unix-Millisekunden; die Cache-Grenzen bilden
den Nutzungstag exakt von 04:00 bis 04:00 Europe/Berlin ab.

## Prüfung und Backfill

```powershell
$config = "$env:LOCALAPPDATA\LIVA\StayFreeIOSBridge\config.json"
.\.venv-activity\Scripts\python.exe -m clients.liva_stayfree_ios_bridge --config $config --dry-run
.\.venv-activity\Scripts\python.exe -m clients.liva_stayfree_ios_bridge --config $config --day 2026-08-16
.\.venv-activity\Scripts\python.exe -m clients.liva_stayfree_ios_bridge --config $config --backfill
```

Der Trockenlauf meldet verfügbare Tage, frühesten und letzten abgeschlossenen Tag
sowie die Zahl der App-Datensätze. Er schreibt weder StayFree noch LIVA. Der
lokale Status liegt unter `%LOCALAPPDATA%\LIVA\StayFreeIOSBridge`.

Fehlende Daten werden niemals als `0 Sekunden` importiert. Für den laufenden
04:00–04:00-Nutzungstag erfolgt grundsätzlich kein Daily-Import.
