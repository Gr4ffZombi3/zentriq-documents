#!/usr/bin/env bash
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$APP_DIR"

# shellcheck disable=SC1091
source "$APP_DIR/deploy/lib.sh"

TARGET_SITE_FILE="${1:-}"
PUBLIC_HOST_VALUE="${PUBLIC_HOST:-$(resolve_public_host 2>/dev/null || true)}"

if [ -z "$TARGET_SITE_FILE" ]; then
    TARGET_SITE_FILE="$(resolve_nginx_site_file 2>/dev/null || true)"
fi

if [ -z "$TARGET_SITE_FILE" ]; then
    if [ -z "$PUBLIC_HOST_VALUE" ]; then
        echo "[repair-nginx] FEHLER: Zentriq-Nginx-Site konnte nicht eindeutig gefunden werden." >&2
        echo "[repair-nginx] Setze PUBLIC_HOST/PUBLIC_URL oder pruefe server_name der aktiven Site." >&2
        exit 1
    fi
    TARGET_SITE_FILE="${NGINX_SITES_AVAILABLE_DIR}/zentriq"
    echo "[repair-nginx] Keine bestehende Zentriq-Site gefunden - erzeuge ${TARGET_SITE_FILE}."
    TMP_NEW_FILE="$(mktemp)"
    sed -e "s#__APP_DIR__#${APP_DIR}#g" -e "s#__DOMAIN__#${PUBLIC_HOST_VALUE}#g" \
        "$APP_DIR/deploy/nginx/zentriq.conf.template" > "$TMP_NEW_FILE"
    sudo mkdir -p "$NGINX_SITES_AVAILABLE_DIR" "$NGINX_SITES_ENABLED_DIR"
    sudo cp "$TMP_NEW_FILE" "$TARGET_SITE_FILE"
    sudo ln -sfn "$TARGET_SITE_FILE" "$NGINX_SITES_ENABLED_DIR/zentriq"
    rm -f "$TMP_NEW_FILE"
else
    echo "[repair-nginx] Nutze Zentriq-Site ${TARGET_SITE_FILE}."
fi

TMP_FILE="$(mktemp)"
python3 "$APP_DIR/deploy/nginx/repair_site.py" --input "$TARGET_SITE_FILE" --output "$TMP_FILE"

CHANGED=0
if cmp -s "$TARGET_SITE_FILE" "$TMP_FILE"; then
    echo "[repair-nginx] Keine inhaltlichen nginx-Aenderungen noetig."
    rm -f "$TMP_FILE"
else
    CHANGED=1
    BACKUP_PATH="${TARGET_SITE_FILE}.bak.$(date +%Y%m%d%H%M%S)"
    echo "[repair-nginx] Sicherung -> ${BACKUP_PATH}"
    sudo cp "$TARGET_SITE_FILE" "$BACKUP_PATH"
    sudo cp "$TMP_FILE" "$TARGET_SITE_FILE"
    rm -f "$TMP_FILE"
fi

echo "[repair-nginx] Pruefe nginx-Konfiguration..."
if ! sudo nginx -t; then
    if [ "$CHANGED" -eq 1 ] && [ -n "${BACKUP_PATH:-}" ] && [ -f "${BACKUP_PATH:-}" ]; then
        echo "[repair-nginx] nginx -t fehlgeschlagen - stelle Backup wieder her."
        sudo cp "$BACKUP_PATH" "$TARGET_SITE_FILE"
    fi
    exit 1
fi
echo "[repair-nginx] Lade nginx neu..."
sudo systemctl reload nginx

if [ -n "$PUBLIC_HOST_VALUE" ]; then
    echo "[repair-nginx] Verwendeter Public Host: ${PUBLIC_HOST_VALUE}"
fi
