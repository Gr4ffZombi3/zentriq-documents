#!/usr/bin/env bash
# Laufendes Deployment auf einem bereits eingerichteten Server (nach deploy/install.sh).
# Wird von /deploy.sh im Projektroot aufgerufen: `git pull && ./deploy.sh`
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$APP_DIR"

echo "==> [1/5] Hole neuesten Code..."
git pull

echo "==> [2/5] Aktiviere virtuelle Umgebung & installiere Abhaengigkeiten..."
source .venv/bin/activate
pip install -q -r requirements.txt

echo "==> [3/5] Wende Datenbank-Migrationen an..."
set -a
source .env
set +a
flask db upgrade

echo "==> [4/5] Statische Assets..."
echo "    Kein Build-/Collect-Schritt noetig: Flask liefert app/static/ direkt aus,"
echo "    es wird kein JS-Bundler eingesetzt (htmx/Alpine per CDN)."

echo "==> [5/5] Starte Dienste neu..."
sudo systemctl restart zentriq-api
sudo systemctl restart zentriq-worker

echo "==> Verifikation..."
"$APP_DIR/deploy/post-deploy.sh"

echo "==> Update abgeschlossen."
