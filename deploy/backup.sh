#!/usr/bin/env bash
# Taegliche Sicherung von Zentriq: Datenbank (konsistenter Dump), Uploads und .env.
#
#   deploy/backup.sh            -> neue Sicherung unter backups/auto/<Zeitstempel>/
#
# - Datenbank per mariadb-dump --single-transaction (keine Sperre fuer den Bueroalltag).
#   Zugangsdaten aus DATABASE_URL in .env, uebergeben per temporaerer Optionsdatei (0600),
#   nie auf der Kommandozeile oder in Logs.
# - Aufbewahrung: BACKUP_KEEP_DAYS (Standard 14) Tage; aeltere automatische Sicherungen
#   werden entfernt. Manuelle Sicherungen ausserhalb von backups/auto bleiben unberuehrt.
# - Optional Kopie auf einen anderen Rechner: BACKUP_REMOTE (rsync-Ziel, z. B.
#   backup@host:/pfad) - ohne externe Kopie schuetzt die Sicherung nicht vor Serververlust.
# - Fehler -> Exit-Code != 0; der systemd-Dienst zentriq-backup erscheint dann als
#   fehlgeschlagen (systemctl --failed, journalctl -u zentriq-backup).
set -euo pipefail

APP_DIR="${APP_DIR:-$(cd "$(dirname "$0")/.." && pwd)}"
DEST="${BACKUP_DIR:-$APP_DIR/backups/auto}"
KEEP_DAYS="${BACKUP_KEEP_DAYS:-14}"
umask 077

stamp="$(date -u +%Y%m%d-%H%M%S)"
target="$DEST/$stamp"
work="$DEST/.$stamp.partial"
mkdir -p "$work"
cnf="$(mktemp)"
trap 'rm -f "$cnf"; rm -rf "$work"' EXIT

db_name="$("$APP_DIR/.venv/bin/python" - "$APP_DIR/.env" "$cnf" <<'PY'
import sys

from dotenv import dotenv_values
from sqlalchemy.engine import make_url

url = make_url(dotenv_values(sys.argv[1])["DATABASE_URL"])
with open(sys.argv[2], "w", encoding="utf-8") as fh:
    fh.write("[client]\n")
    fh.write(f"user={url.username}\n")
    fh.write(f"password={url.password or ''}\n")
    fh.write(f"host={url.host or 'localhost'}\n")
    fh.write(f"port={url.port or 3306}\n")
print(url.database)
PY
)"

echo "==> Datenbank $db_name"
mariadb-dump --defaults-extra-file="$cnf" --single-transaction --quick --routines --triggers \
    --default-character-set=utf8mb4 "$db_name" | gzip -6 > "$work/database.sql.gz"
# Ein vollstaendiger Dump endet mit "-- Dump completed".
zcat "$work/database.sql.gz" | tail -n 1 | grep -q "Dump completed" || { echo "FEHLER: Dump unvollstaendig" >&2; exit 1; }

echo "==> Uploads"
tar -C "$APP_DIR/storage" -czf "$work/uploads.tar.gz" uploads

echo "==> Konfiguration"
cp "$APP_DIR/.env" "$work/env"
git -C "$APP_DIR" rev-parse HEAD > "$work/git-revision.txt" 2>/dev/null || true

(cd "$work" && sha256sum database.sql.gz uploads.tar.gz env > SHA256SUMS)
mv "$work" "$target"
echo "==> Gespeichert: $target ($(du -sh "$target" | cut -f1))"

if [ -n "${BACKUP_REMOTE:-}" ]; then
    echo "==> Externe Kopie nach $BACKUP_REMOTE"
    rsync -a "$target" "$BACKUP_REMOTE/"
fi

# Aufbewahrung: nur vollstaendige automatische Sicherungen (Verzeichnisname = Zeitstempel).
find "$DEST" -mindepth 1 -maxdepth 1 -type d -name '20*' -mtime +"$KEEP_DAYS" -print -exec rm -rf {} +
