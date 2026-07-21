#!/usr/bin/env bash
# Einmalige Einrichtung eines frischen Ubuntu-24.04-Servers fuer Zentriq Documents.
# Nach erfolgreichem Lauf ist die Webseite vollstaendig online: MariaDB, Redis, Celery-
# Worker und Gunicorn hinter Nginx, systemd-Services, Firewall, optional SSL.
#
# Nutzung (Repo bereits geklont, dieses Skript befindet sich in deploy/ des Projekts):
#   chmod +x deploy/install.sh
#   ./deploy/install.sh
#
# Idempotent: kann nach einem fehlgeschlagenen Lauf gefahrlos erneut ausgefuehrt werden.
# Eine bereits vorhandene Datenbank/Nutzer/.env wird NIEMALS ueberschrieben oder geleert.
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
    echo "Bitte als root ausfuehren (z.B. direkt als root-Nutzer oder per 'sudo ./deploy/install.sh')." >&2
    exit 1
fi

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$APP_DIR"

# shellcheck disable=SC1091
source "$APP_DIR/deploy/lib.sh"

DB_NAME="zentriq_documents"
DB_USER="zentriq"
DEFAULT_DOMAIN="www.zentriqai.de"

read -rp "Domain fuer nginx/SSL [${DEFAULT_DOMAIN}, '-' = kein SSL]: " DOMAIN_INPUT || true
if [ "${DOMAIN_INPUT:-}" = "-" ]; then
    DOMAIN=""
else
    DOMAIN="${DOMAIN_INPUT:-$DEFAULT_DOMAIN}"
fi

echo ""
echo "==> [1/10] System-Pakete installieren..."
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq \
    python3 python3-venv python3-pip python3-dev build-essential pkg-config \
    mariadb-server mariadb-client \
    redis-server \
    nginx \
    tesseract-ocr tesseract-ocr-deu \
    git curl ufw

echo "==> [2/10] MariaDB & Redis aktivieren..."
systemctl enable --now mariadb
systemctl enable --now redis-server

echo "==> [3/10] Python-Umgebung..."
if [ ! -d ".venv" ]; then
    python3 -m venv .venv
fi
source .venv/bin/activate
pip install --upgrade pip -q
pip install -q -r requirements.txt
deactivate

echo "==> [4/10] .env einrichten..."
if [ ! -f ".env" ]; then
    DB_PASSWORD="$(python3 -c "import secrets; print(secrets.token_urlsafe(24))")"
    SECRET_KEY="$(python3 -c "import secrets; print(secrets.token_hex(32))")"

    mysql -e "CREATE DATABASE IF NOT EXISTS ${DB_NAME} CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;"
    mysql -e "CREATE USER IF NOT EXISTS '${DB_USER}'@'localhost' IDENTIFIED BY '${DB_PASSWORD}';"
    mysql -e "GRANT ALL PRIVILEGES ON ${DB_NAME}.* TO '${DB_USER}'@'localhost';"
    mysql -e "FLUSH PRIVILEGES;"

    cp .env.example .env
    sed -i "s#^SECRET_KEY=.*#SECRET_KEY=${SECRET_KEY}#" .env
    sed -i "s#^DATABASE_URL=.*#DATABASE_URL=mysql+pymysql://${DB_USER}:${DB_PASSWORD}@localhost:3306/${DB_NAME}#" .env
    sed -i "s#^MARIADB_DATABASE=.*#MARIADB_DATABASE=${DB_NAME}#" .env
    sed -i "s#^MARIADB_USER=.*#MARIADB_USER=${DB_USER}#" .env
    sed -i "s#^MARIADB_PASSWORD=.*#MARIADB_PASSWORD=${DB_PASSWORD}#" .env
    sed -i "s#^FLASK_ENV=.*#FLASK_ENV=production#" .env
    sed -i "s#^SESSION_COOKIE_SECURE=.*#SESSION_COOKIE_SECURE=false#" .env
    if [ -n "$DOMAIN" ]; then
        printf '\nPUBLIC_HOST=%s\nPUBLIC_URL=http://%s\n' "$DOMAIN" "$DOMAIN" >> .env
    fi

    echo "    .env erzeugt (SECRET_KEY und Datenbank-Zugangsdaten automatisch gesetzt)."
    echo "    WICHTIG: OPENAI_API_KEY ist noch leer - fuer KI-Auswertung in .env eintragen,"
    echo "    danach: systemctl restart zentriq-api zentriq-worker"
else
    echo "    .env existiert bereits - unveraendert gelassen."
fi

echo "==> [5/10] Datenbank-Migrationen..."
source .venv/bin/activate
set -a
source .env
set +a
flask db upgrade
deactivate

echo "==> [6/10] Ordner & Rechte..."
mkdir -p storage/uploads logs
chmod 750 storage/uploads

echo "==> [7/10] systemd-Services einrichten..."
sed -e "s#__APP_DIR__#${APP_DIR}#g" \
    "${APP_DIR}/deploy/systemd/zentriq-api.service.template" > /etc/systemd/system/zentriq-api.service
sed -e "s#__APP_DIR__#${APP_DIR}#g" \
    "${APP_DIR}/deploy/systemd/zentriq-worker.service.template" > /etc/systemd/system/zentriq-worker.service
chmod +x "${APP_DIR}/deploy/pre-start.sh" "${APP_DIR}/deploy/post-deploy.sh" "${APP_DIR}/deploy/repair-nginx.sh" "${APP_DIR}/deploy/update.sh"
systemctl daemon-reload
systemctl enable zentriq-api zentriq-worker
systemctl restart zentriq-api zentriq-worker

echo "==> [8/10] nginx einrichten..."
if [ ! -f /etc/nginx/sites-available/zentriq ]; then
    sed -e "s#__APP_DIR__#${APP_DIR}#g" -e "s#__DOMAIN__#${DOMAIN:-_}#g" \
        "${APP_DIR}/deploy/nginx/zentriq.conf.template" > /etc/nginx/sites-available/zentriq
fi
ln -sf /etc/nginx/sites-available/zentriq /etc/nginx/sites-enabled/zentriq
rm -f /etc/nginx/sites-enabled/default
"${APP_DIR}/deploy/repair-nginx.sh"

echo "==> [9/10] Firewall..."
ufw allow OpenSSH >/dev/null
ufw allow 'Nginx Full' >/dev/null
ufw --force enable >/dev/null

echo "==> [10/10] SSL..."
if [ -n "$DOMAIN" ]; then
    apt-get install -y -qq certbot python3-certbot-nginx
    CERTBOT_EMAIL="admin@${DOMAIN#www.}"
    if certbot --nginx -d "$DOMAIN" --non-interactive --agree-tos -m "$CERTBOT_EMAIL" --redirect; then
        # Erst NACH erfolgreichem Zertifikat auf HTTPS-only-Cookies umstellen - sonst waere
        # ein Login unmoeglich, falls certbot z.B. mangels DNS-Propagation fehlschlaegt und
        # die Seite vorerst nur ueber HTTP erreichbar bleibt.
        sed -i "s#^SESSION_COOKIE_SECURE=.*#SESSION_COOKIE_SECURE=true#" .env
        if grep -q '^PUBLIC_URL=' .env; then
            sed -i "s#^PUBLIC_URL=.*#PUBLIC_URL=https://${DOMAIN}#" .env
        else
            printf '\nPUBLIC_URL=https://%s\n' "$DOMAIN" >> .env
        fi
        systemctl restart zentriq-api zentriq-worker
        echo "    SSL aktiv, SESSION_COOKIE_SECURE=true gesetzt."
    else
        echo "    certbot fehlgeschlagen (zeigt die Domain schon auf diesen Server? DNS propagiert?)."
        echo "    Seite laeuft vorerst ueber HTTP. Spaeter manuell nachholen: certbot --nginx -d $DOMAIN"
    fi
else
    echo "    Keine Domain angegeben - SSL uebersprungen. Spaeter jederzeit: certbot --nginx -d <domain>"
fi

echo ""
echo "==> Health-Check..."
"${APP_DIR}/deploy/post-deploy.sh" || true

echo ""
echo "================================================================"
echo " Installation abgeschlossen."
echo ""
echo " Naechste Schritte:"
echo "  1. OPENAI_API_KEY in ${APP_DIR}/.env eintragen (fuer OCR-Fallback & KI-Auswertung)"
echo "  2. systemctl restart zentriq-api zentriq-worker"
echo ""
echo " Kuenftige Updates: lokal committen/pushen, dann auf dem Server:"
echo "   cd ${APP_DIR} && ./deploy.sh"
echo "================================================================"
