#!/usr/bin/env bash
# Deployment-Skript fuer den Produktionsserver. Wird NACH einem `git push` von lokal aus
# auf dem Server im Projektverzeichnis ausgefuehrt: `./deploy.sh`
#
# Voraussetzung (einmalig, siehe docs/DEPLOYMENT.md): .venv existiert bereits, .env ist
# manuell mit den Produktionswerten befuellt, die systemd-Units zentriq/zentriq-worker
# sind installiert und aktiviert.
set -euo pipefail
cd "$(dirname "$0")"

echo "==> Hole neuesten Code..."
git pull

echo "==> Aktiviere virtuelle Umgebung..."
source .venv/bin/activate

echo "==> Installiere Abhaengigkeiten..."
pip install -r requirements.txt

echo "==> Wende Datenbank-Migrationen an..."
flask db upgrade

echo "==> Starte Dienste neu..."
sudo systemctl restart zentriq
sudo systemctl restart zentriq-worker
sudo systemctl restart nginx

echo "==> Pruefe Dienststatus..."
sudo systemctl is-active --quiet zentriq && echo "    zentriq: aktiv" || echo "    WARNUNG: zentriq laeuft nicht"
sudo systemctl is-active --quiet zentriq-worker && echo "    zentriq-worker: aktiv" || echo "    WARNUNG: zentriq-worker laeuft nicht"
sudo systemctl is-active --quiet nginx && echo "    nginx: aktiv" || echo "    WARNUNG: nginx laeuft nicht"

echo "==> Deployment abgeschlossen."
