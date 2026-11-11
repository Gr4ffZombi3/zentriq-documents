#!/usr/bin/env bash
# Rollt den Live-Dienst zentriq-live einmalig aus (NUR nach Freigabe, siehe DEPLOYMENT.md):
#   1. systemd-Unit aus deploy/systemd/zentriq-live.service.template installieren und starten
#   2. Gesundheitscheck http://127.0.0.1:8001/live/health
#   3. nginx-Location /live/ in die bestehende Site einfuegen (Sicherung, nginx -t, Ruecksicherung)
#   4. Pruefung ueber nginx
# Wiederholbar: bereits vorhandene Teile bleiben unveraendert.
# Rueckbau: deploy/enable-live.sh --disable (Dienst stoppen; /live/ liefert dann 502, die
# Oberflaeche bleibt still).
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$APP_DIR"
# shellcheck disable=SC1091
source "$APP_DIR/deploy/lib.sh"

UNIT=/etc/systemd/system/zentriq-live.service

if [ "${1:-}" = "--disable" ]; then
    echo "[live] Stoppe und deaktiviere zentriq-live..."
    sudo systemctl disable --now zentriq-live || true
    echo "[live] Erledigt. nginx bleibt unveraendert (/live/ antwortet mit 502, die App merkt das still)."
    exit 0
fi

echo "[live] 1/4 systemd-Unit..."
TMP_UNIT="$(mktemp)"
sed -e "s#__APP_DIR__#${APP_DIR}#g" "$APP_DIR/deploy/systemd/zentriq-live.service.template" > "$TMP_UNIT"
if [ -f "$UNIT" ] && cmp -s "$TMP_UNIT" "$UNIT"; then
    echo "    Unit unveraendert."
else
    sudo cp "$TMP_UNIT" "$UNIT"
    sudo systemctl daemon-reload
    echo "    Unit installiert: $UNIT"
fi
rm -f "$TMP_UNIT"
sudo systemctl enable zentriq-live >/dev/null
sudo systemctl restart zentriq-live

echo "[live] 2/4 Gesundheitscheck..."
OK=0
for _ in $(seq 1 10); do
    if [ "$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8001/live/health)" = "200" ]; then OK=1; break; fi
    sleep 1
done
if [ "$OK" -ne 1 ]; then
    echo "    FEHLER: zentriq-live antwortet nicht. Log: journalctl -u zentriq-live --no-pager -n 50" >&2
    exit 1
fi
echo "    zentriq-live: aktiv (127.0.0.1:8001)"

echo "[live] 3/4 nginx-Location /live/..."
SITE="$(resolve_nginx_site_file 2>/dev/null || true)"
if [ -z "$SITE" ]; then
    echo "    FEHLER: Zentriq-nginx-Site nicht gefunden." >&2
    exit 1
fi
TMP_SITE="$(mktemp)"
python3 "$APP_DIR/deploy/nginx/live_location.py" --input "$SITE" --output "$TMP_SITE"
if cmp -s "$SITE" "$TMP_SITE"; then
    echo "    /live/ bereits vorhanden ($SITE)."
    rm -f "$TMP_SITE"
else
    BACKUP="${SITE}.bak.$(date +%Y%m%d%H%M%S)"
    sudo cp "$SITE" "$BACKUP"
    sudo cp "$TMP_SITE" "$SITE"
    rm -f "$TMP_SITE"
    echo "    Sicherung -> $BACKUP"
    if ! sudo nginx -t; then
        echo "    nginx -t fehlgeschlagen - stelle Sicherung wieder her." >&2
        sudo cp "$BACKUP" "$SITE"
        exit 1
    fi
    sudo systemctl reload nginx
    echo "    nginx neu geladen."
fi

echo "[live] 4/4 Pruefung ueber nginx..."
HOST="$(resolve_public_host 2>/dev/null || echo localhost)"
STATUS="$(curl -s -o /dev/null -w '%{http_code}' -H "Host: ${HOST}" http://127.0.0.1/live/health || true)"
case "$STATUS" in
    200) echo "    nginx -> zentriq-live OK" ;;
    301|302|308) echo "    nginx leitet auf HTTPS um ($STATUS) - pruefe oeffentlich: https://${HOST}/live/health" ;;
    *) echo "    WARNUNG: nginx /live/health liefert $STATUS" ;;
esac
echo "[live] Fertig."
