#!/usr/bin/env bash
# Laufendes Deployment auf einem bereits eingerichteten Server (nach deploy/install.sh).
# Wird von /deploy.sh im Projektroot aufgerufen: `git pull && ./deploy.sh`
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$APP_DIR"

echo "==> [1/6] Hole neuesten Code..."
git pull

echo "==> [2/6] Aktiviere virtuelle Umgebung & installiere Abhaengigkeiten..."
source .venv/bin/activate
pip install -q -r requirements.txt

echo "==> [3/6] Wende Datenbank-Migrationen an..."
set -a
source .env
set +a
flask db upgrade

echo "==> [4/6] Statische Assets..."
CSS_FILE="app/static/css/app.css"
if [ ! -f "$CSS_FILE" ]; then
    echo "    FEHLER: $CSS_FILE fehlt." >&2
    exit 1
fi
CSS_BYTES="$(wc -c < "$CSS_FILE" | tr -d ' ')"
if [ "$CSS_BYTES" -lt 10000 ]; then
    echo "    FEHLER: $CSS_FILE wirkt unvollstaendig (${CSS_BYTES} Bytes)." >&2
    exit 1
fi
if ! grep -q "InterVariable" "$CSS_FILE"; then
    echo "    FEHLER: $CSS_FILE enthaelt nicht die erwarteten Font-/Design-Regeln." >&2
    exit 1
fi
if ! grep -q ".auth-shell" "$CSS_FILE"; then
    echo "    FEHLER: $CSS_FILE enthaelt nicht die erwarteten Auth-Layout-Regeln." >&2
    exit 1
fi
echo "    app.css vorhanden und plausibel (${CSS_BYTES} Bytes)."

echo "==> [5/6] Repariere nginx-Konfiguration..."
"$APP_DIR/deploy/repair-nginx.sh"

echo "==> [6/6] Starte Dienste neu..."
sudo systemctl restart zentriq-api
sudo systemctl restart zentriq-worker

echo "==> Verifikation..."
"$APP_DIR/deploy/post-deploy.sh"

echo "==> Update abgeschlossen."
