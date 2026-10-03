# Google Calendar Setup (Raspberry Pi)

Ziel: Der Pi kann Google Kalender lesen, erstellen, ändern und löschen.

## 1) Google Cloud vorbereiten

1. In Google Cloud ein Projekt anlegen (oder bestehendes nutzen).
2. API aktivieren: `Google Calendar API`.
3. OAuth Consent Screen konfigurieren.
4. OAuth Client erstellen:
   - Typ: `Desktop app`
5. JSON herunterladen und auf dem Pi speichern, z. B.:
   - `/opt/liva/var/gcalsync/google_credentials.json`

## 2) Python-Abhängigkeiten installieren

```bash
cd /opt/liva
source venv/bin/activate
pip install google-api-python-client google-auth google-auth-oauthlib
```

## 3) .env Variablen setzen

Beispielwerte:

```bash
GOOGLE_CALENDAR_CREDENTIALS_FILE=/opt/liva/var/gcalsync/google_credentials.json
GOOGLE_CALENDAR_TOKEN_FILE=/opt/liva/var/gcalsync/google_token.json
GOOGLE_CALENDAR_TIMEZONE=Europe/Berlin

GOOGLE_CALENDAR_DEFAULT_ID=primary
GOOGLE_CALENDAR_SCHOOL_ID=primary
GOOGLE_CALENDAR_FOOTBALL_ID=primary
GOOGLE_CALENDAR_TRAINING_ID=primary
GOOGLE_CALENDAR_WORK_ID=primary

GOOGLE_CALENDAR_SCHOOL_CLEANUP=true
```

Für getrennte Kalender die jeweiligen Calendar IDs statt `primary` eintragen.

## 4) OAuth am Pi initialisieren

```bash
cd /opt/liva
source venv/bin/activate
python scripts/google_calendar_auth.py --no-local-server
```

## 5) CLI benutzen (lesen / erstellen / ändern / löschen)

Kalender anzeigen:

```bash
python scripts/google_calendar_cli.py list-calendars
```

Events lesen:

```bash
python scripts/google_calendar_cli.py list-events --calendar school --time-min 2026-03-01T00:00:00+01:00 --time-max 2026-03-31T23:59:59+01:00
```

Event erstellen:

```bash
cat > /tmp/event.json <<'JSON'
{
  "summary": "Fußballtraining",
  "start": {"dateTime": "2026-03-10T18:30:00", "timeZone": "Europe/Berlin"},
  "end": {"dateTime": "2026-03-10T20:00:00", "timeZone": "Europe/Berlin"}
}
JSON

python scripts/google_calendar_cli.py create-event --calendar football --json-file /tmp/event.json
```

Event patchen:

```bash
cat > /tmp/patch.json <<'JSON'
{"summary": "Fußballtraining (Hauptplatz)"}
JSON

python scripts/google_calendar_cli.py patch-event --calendar football --event-id <EVENT_ID> --json-file /tmp/patch.json
```

Event löschen:

```bash
python scripts/google_calendar_cli.py delete-event --calendar football --event-id <EVENT_ID>
```

## 6) SchoolSync in Google Kalender spiegeln

```bash
python scripts/google_calendar_sync_schoolsync.py
```

Dry-Run:

```bash
python scripts/google_calendar_sync_schoolsync.py --dry-run
```

Hinweis: Der Sync arbeitet idempotent über `extendedProperties.private.liva_event_id`.
