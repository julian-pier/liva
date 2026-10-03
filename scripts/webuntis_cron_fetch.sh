#!/usr/bin/env bash
set -euo pipefail

cd /opt/liva
source venv/bin/activate

# Prevent overlapping runs if a previous fetch is still active.
/usr/bin/flock -n /tmp/webuntis_fetch_today.lock \
  python scripts/webuntis_fetch_today.py
