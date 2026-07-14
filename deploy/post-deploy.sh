#!/usr/bin/env bash
# Verifikation nach einem Deployment: Dienststatus + HTTP-Health-Check. Wird von
# deploy/update.sh am Ende aufgerufen, kann aber auch einzeln laufen:
# `./deploy/post-deploy.sh`
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$APP_DIR"

echo "[post-deploy] Pruefe nginx-Konfiguration..."
if command -v nginx >/dev/null 2>&1; then
    sudo nginx -t
    sudo systemctl reload nginx
fi

echo "[post-deploy] Pruefe Dienststatus..."
FAILED=0
for svc in zentriq-api zentriq-worker nginx mariadb redis-server; do
    if systemctl is-active --quiet "$svc" 2>/dev/null; then
        echo "    $svc: aktiv"
    else
        echo "    WARNUNG: $svc laeuft nicht"
        FAILED=1
    fi
done

echo "[post-deploy] HTTP-Health-Check..."
sleep 2
STATUS="$(curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:8000/auth/login || echo "000")"
if [ "$STATUS" = "200" ]; then
    echo "    Health-Check OK (Login-Seite antwortet mit 200)"
else
    echo "    FEHLER: Health-Check fehlgeschlagen (HTTP ${STATUS})."
    echo "    Logs pruefen: journalctl -u zentriq-api -n 50 --no-pager"
    FAILED=1
fi

if [ "$FAILED" -ne 0 ]; then
    echo "[post-deploy] Es gab Probleme - siehe Warnungen/Fehler oben."
    exit 1
fi

echo "[post-deploy] Alles laeuft."
