# LIVA

**A self-hosted home for training, recovery, nutrition, and activity data.**

LIVA combines strength training, endurance planning, recovery signals,
nutrition workflows, personal analytics, and optional assistant integrations in
one system you operate yourself. Your databases and provider credentials stay
on infrastructure you control.

## Highlights

- Training plans, session logging, progression analysis, and coaching context
- Recovery and sleep data with optional Polar integration
- Nutrition planning, meal logging, and historical targets
- Endurance planning with optional Garmin, Strava, and Intervals.icu workflows
- Google Calendar, Telegram, ntfy, and WebUntis integrations
- Optional MCP server with scoped read/write capabilities
- Built-in health checks, backups, operational tooling, and privacy gates

All integrations are optional. An empty installation starts without personal
profiles, provider accounts, smart-home devices, or example health data.

## Run with Docker Compose

Requirements: Docker Engine with the Compose plugin.

```bash
cp .env.example .env
openssl rand -hex 32
```

Put independent generated values into `LIVA_FLASK_SECRET`,
`LIVA_MASTER_SECRET`, and `LIVA_AUTH_PEPPER`, then start LIVA:

```bash
docker compose up --build -d
curl --fail http://127.0.0.1:5000/healthz
```

Open `http://127.0.0.1:5000`. The Compose configuration binds only to
localhost. Put a TLS-enabled reverse proxy in front of LIVA before allowing
remote access, and set `LIVA_SESSION_COOKIE_SECURE=1` once HTTPS is active.

Application state is stored in named Docker volumes. Back up the
`liva_database`, `liva_data`, `liva_uploads`, and `liva_var` volumes together.

## Local development

LIVA requires Python 3.12:

```bash
python3.12 -m venv .venv
.venv/bin/pip install -e '.[test]'
cp .env.example .env
set -a; . ./.env; set +a
.venv/bin/python -m pytest -m "not live and not hardware"
.venv/bin/flask --app app run --host 127.0.0.1 --port 5000
```

Frontend sources live in `frontend/`; use `scripts/frontend.sh` for the pinned
build workflow. Tests marked `live` or `hardware` can contact external systems
or physical devices and are never part of the default CI gate.

## Configuration

Start with [.env.example](.env.example). Empty optional values disable their
integration. Never commit `.env`, OAuth tokens, exports, databases, uploads, or
provider credentials.

Smart-home devices have no built-in defaults. Configure them explicitly using
`LIVA_TAPO_DEVICES_JSON` and `LIVA_GOVEE_DEVICES_JSON`. Documentation-specific
IP addresses use the reserved `192.0.2.0/24` test range and are not real hosts.

## Architecture

| Area | Purpose |
| --- | --- |
| `app.py`, `app_support/` | Flask application and HTTP composition |
| `core/`, `training/`, `nutrition/`, `analysis/` | Domain logic and analytics |
| `integrations/`, `gcalsync/`, `strava_sync/`, `schoolsync/` | Optional providers |
| `liva_mcp/` | Separately deployable MCP server |
| `database/` | SQLite access and forward-only migrations |
| `jobs/`, `scripts/`, `systemd/` | Scheduling and self-hosted operations |
| `tests/` | Unit, integration, live, and hardware tests |

## Privacy and release safety

Every commit and pull request runs a release audit that rejects runtime data,
database variants, populated credential files, personal home paths, legacy
identifiers, non-example email addresses, and common secret formats.

```bash
python scripts/public_release_audit.py
```

The repository has a fresh history and contains no production databases or
personal runtime state. See [SECURITY.md](SECURITY.md) for deployment and
reporting guidance.

## Production notes

The bundled container intentionally runs one Gunicorn worker with multiple
threads. LIVA uses SQLite by default, so multiple application processes must
not initialize or write the same database files concurrently. Put a reverse
proxy in front of the container for TLS and keep the published application port
bound to localhost unless you have configured authentication and network
controls.

## Project status

LIVA is pre-1.0 software. Back up your volumes before upgrades and review
migrations in a staging copy when the release notes call them out.

## License

[MIT](LICENSE) — use, modify, and share LIVA freely.
