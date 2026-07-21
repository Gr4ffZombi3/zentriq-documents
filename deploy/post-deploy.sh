#!/usr/bin/env bash
# Verifikation nach einem Deployment: Dienststatus + HTTP-Health-Check. Wird von
# deploy/update.sh am Ende aufgerufen, kann aber auch einzeln laufen:
# `./deploy/post-deploy.sh`
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$APP_DIR"

# shellcheck disable=SC1091
source "$APP_DIR/deploy/lib.sh"

CSS_MIN_BYTES=60000
PUBLIC_HOST_VALUE="$(resolve_public_host 2>/dev/null || true)"
PUBLIC_BASE_URL="$(resolve_public_base_url 2>/dev/null || true)"

curl_status() {
    curl -sS -L --connect-timeout 10 -o /dev/null -w "%{http_code}" "$@" || echo "000"
}

curl_headers() {
    curl -sS -I -L --connect-timeout 10 "$@" || true
}

curl_body() {
    curl -sS -L --connect-timeout 10 "$@" || true
}

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

echo "[post-deploy] Flask-Direktcheck..."
sleep 2
FLASK_LOGIN_STATUS="$(curl_status http://127.0.0.1:8000/auth/login)"
FLASK_CSS_STATUS="$(curl_status http://127.0.0.1:8000/static/css/app.css)"
FLASK_CSS_HEADERS="$(curl_headers http://127.0.0.1:8000/static/css/app.css)"
FLASK_CSS_TYPE="$(printf '%s' "$FLASK_CSS_HEADERS" | awk -F': ' 'BEGIN{IGNORECASE=1} /^Content-Type:/{gsub("\r","",$2); print $2; exit}')"
FLASK_CSS_BYTES="$(curl_body http://127.0.0.1:8000/static/css/app.css | wc -c | tr -d ' ')"
if [ "$FLASK_LOGIN_STATUS" = "200" ]; then
    echo "    Flask Login OK (HTTP 200)"
else
    echo "    FEHLER: Flask-Login fehlgeschlagen (HTTP ${FLASK_LOGIN_STATUS})."
    echo "    Logs pruefen: journalctl -u zentriq-api -n 50 --no-pager"
    FAILED=1
fi
if [ "$FLASK_CSS_STATUS" = "200" ] && printf '%s' "$FLASK_CSS_TYPE" | grep -qi '^text/css'; then
    echo "    Flask CSS OK (${FLASK_CSS_BYTES} Bytes, ${FLASK_CSS_TYPE})"
else
    echo "    FEHLER: Flask-CSS fehlerhaft (HTTP ${FLASK_CSS_STATUS}, Content-Type '${FLASK_CSS_TYPE:-unbekannt}')."
    FAILED=1
fi
if [ "$FLASK_CSS_BYTES" -lt "$CSS_MIN_BYTES" ]; then
    echo "    FEHLER: Flask-CSS ist zu klein (${FLASK_CSS_BYTES} Bytes)."
    FAILED=1
fi

echo "[post-deploy] Nginx-/Static-Check..."
if [ -z "$PUBLIC_HOST_VALUE" ]; then
    echo "    FEHLER: PUBLIC_HOST/PUBLIC_URL konnte nicht eindeutig ermittelt werden."
    echo "    Hinweis: Setze PUBLIC_HOST oder PUBLIC_URL in .env oder pruefe server_name der Zentriq-Site."
    FAILED=1
else
    LOCAL_RESOLVE_ARGS=(--resolve "${PUBLIC_HOST_VALUE}:80:127.0.0.1" --resolve "${PUBLIC_HOST_VALUE}:443:127.0.0.1")
    LOCAL_LOGIN_URL="http://${PUBLIC_HOST_VALUE}/auth/login"
    LOCAL_LOGIN_STATUS="$(curl_status "${LOCAL_RESOLVE_ARGS[@]}" "$LOCAL_LOGIN_URL")"
    LOCAL_LOGIN_HTML="$(curl_body "${LOCAL_RESOLVE_ARGS[@]}" "$LOCAL_LOGIN_URL")"
    CSS_PATH="$(printf '%s' "$LOCAL_LOGIN_HTML" | grep -oE '/static/css/app\.css[^"[:space:]]*' | head -n 1 || true)"
    if [ -z "$CSS_PATH" ]; then
        CSS_PATH="/static/css/app.css"
    fi
    LOCAL_CSS_URL="http://${PUBLIC_HOST_VALUE}${CSS_PATH}"
    LOCAL_CSS_STATUS="$(curl_status "${LOCAL_RESOLVE_ARGS[@]}" "$LOCAL_CSS_URL")"
    LOCAL_CSS_HEADERS="$(curl_headers "${LOCAL_RESOLVE_ARGS[@]}" "$LOCAL_CSS_URL")"
    LOCAL_CSS_TYPE="$(printf '%s' "$LOCAL_CSS_HEADERS" | awk -F': ' 'BEGIN{IGNORECASE=1} /^Content-Type:/{gsub("\r","",$2); print $2; exit}')"
    LOCAL_CSS_BYTES="$(curl_body "${LOCAL_RESOLVE_ARGS[@]}" "$LOCAL_CSS_URL" | wc -c | tr -d ' ')"
    if [ "$LOCAL_LOGIN_STATUS" = "200" ]; then
        echo "    Nginx Login OK (${PUBLIC_HOST_VALUE}, HTTP 200)"
    else
        echo "    FEHLER: Nginx-Login fuer ${PUBLIC_HOST_VALUE} fehlgeschlagen (HTTP ${LOCAL_LOGIN_STATUS})."
        FAILED=1
    fi
    if [ "$LOCAL_CSS_STATUS" = "200" ] && printf '%s' "$LOCAL_CSS_TYPE" | grep -qi '^text/css'; then
        echo "    Nginx CSS OK (${LOCAL_CSS_BYTES} Bytes, ${LOCAL_CSS_TYPE})"
    else
        echo "    FEHLER: Nginx-CSS fehlerhaft (HTTP ${LOCAL_CSS_STATUS}, Content-Type '${LOCAL_CSS_TYPE:-unbekannt}')."
        FAILED=1
    fi
    if [ "$LOCAL_CSS_BYTES" -lt "$CSS_MIN_BYTES" ]; then
        echo "    FEHLER: Nginx-CSS ist zu klein (${LOCAL_CSS_BYTES} Bytes)."
        FAILED=1
    fi
    if ! printf '%s' "$LOCAL_LOGIN_HTML" | grep -q '/static/css/app.css'; then
        echo "    FEHLER: Login-Seite bindet app.css ueber nginx nicht ein."
        FAILED=1
    fi
fi

echo "[post-deploy] Oeffentliche URL pruefen..."
if [ -z "$PUBLIC_BASE_URL" ]; then
    echo "    FEHLER: PUBLIC_URL/PUBLIC_HOST konnte nicht fuer den Public-Check ermittelt werden."
    FAILED=1
else
    PUBLIC_LOGIN_URL="${PUBLIC_BASE_URL}/auth/login"
    PUBLIC_LOGIN_STATUS="$(curl_status "$PUBLIC_LOGIN_URL")"
    if [ -n "${CSS_PATH:-}" ]; then
        PUBLIC_CSS_URL="${PUBLIC_BASE_URL}${CSS_PATH}"
    else
        PUBLIC_CSS_URL="${PUBLIC_BASE_URL}/static/css/app.css"
    fi
    PUBLIC_CSS_STATUS="$(curl_status "$PUBLIC_CSS_URL")"
    PUBLIC_CSS_HEADERS="$(curl_headers "$PUBLIC_CSS_URL")"
    PUBLIC_CSS_TYPE="$(printf '%s' "$PUBLIC_CSS_HEADERS" | awk -F': ' 'BEGIN{IGNORECASE=1} /^Content-Type:/{gsub("\r","",$2); print $2; exit}')"
    PUBLIC_CSS_BYTES="$(curl_body "$PUBLIC_CSS_URL" | wc -c | tr -d ' ')"
    PUBLIC_LOGIN_HTML="$(curl_body "$PUBLIC_LOGIN_URL")"
    if [ "$PUBLIC_LOGIN_STATUS" = "200" ]; then
        echo "    Public Login OK (${PUBLIC_BASE_URL}, HTTP 200)"
    else
        echo "    FEHLER: Public Login fehlgeschlagen (${PUBLIC_BASE_URL}, HTTP ${PUBLIC_LOGIN_STATUS})."
        FAILED=1
    fi
    if [ "$PUBLIC_CSS_STATUS" = "200" ] && printf '%s' "$PUBLIC_CSS_TYPE" | grep -qi '^text/css'; then
        echo "    Public CSS OK (${PUBLIC_CSS_BYTES} Bytes, ${PUBLIC_CSS_TYPE})"
    else
        echo "    FEHLER: Public CSS fehlerhaft (HTTP ${PUBLIC_CSS_STATUS}, Content-Type '${PUBLIC_CSS_TYPE:-unbekannt}')."
        FAILED=1
    fi
    if [ "$PUBLIC_CSS_BYTES" -lt "$CSS_MIN_BYTES" ]; then
        echo "    FEHLER: Public CSS ist zu klein (${PUBLIC_CSS_BYTES} Bytes)."
        FAILED=1
    fi
    if ! printf '%s' "$PUBLIC_LOGIN_HTML" | grep -q '/static/css/app.css'; then
        echo "    FEHLER: Public Login bindet app.css nicht ein."
        FAILED=1
    fi
fi

if [ "$FAILED" -ne 0 ]; then
    echo "[post-deploy] Es gab Probleme - siehe Warnungen/Fehler oben."
    exit 1
fi

echo "[post-deploy] Alles laeuft."
