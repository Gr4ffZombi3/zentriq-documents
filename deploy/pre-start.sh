#!/usr/bin/env bash
# ExecStartPre fuer zentriq-api.service UND zentriq-worker.service: Vorbereitungen und
# Sicherheitschecks vor jedem Start. Nur echte Blocker (Upload-Ordner nicht beschreibbar)
# fuehren zu einem Abbruch (exit != 0) - alles andere ist ein Hinweis in den Logs
# (`journalctl -u zentriq-api`), damit ein weiches Problem (z.B. Migration noch nicht
# angewendet) nicht sofort eine Neustart-Schleife wie den urspruenglichen Bug ausloest.
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$APP_DIR"

echo "[pre-start] Projektverzeichnis: $APP_DIR"

# 1) Verwaiste Gunicorn-/Celery-Prozesse DIESES Projekts sauber beenden (z.B. nach einem
#    harten Kill, bevor systemd den PID-Stand aktualisieren konnte). Es werden ausschliesslich
#    Prozesse mit passendem Arbeitsverzeichnis beendet - nie fremde Prozesse auf dem Server.
for pattern in "gunicorn.*wsgi:app" "celery.*celery_worker.celery"; do
    STALE_PIDS=$(pgrep -f "$pattern" || true)
    for pid in $STALE_PIDS; do
        PID_CWD="$(readlink -f "/proc/$pid/cwd" 2>/dev/null || true)"
        if [ "$PID_CWD" = "$APP_DIR" ]; then
            echo "[pre-start] Beende verwaisten Prozess $pid ($pattern)"
            kill "$pid" 2>/dev/null || true
        fi
    done
done

# 2) Gunicorn-Port frei? (informativ - der eigentliche Fehler wird beim Start sichtbar,
#    hier nur ein fruehzeitiger, deutlicher Hinweis in den Logs)
GUNICORN_PORT="8000"
if ss -ltn 2>/dev/null | awk '{print $4}' | grep -q ":${GUNICORN_PORT}\$"; then
    echo "[pre-start] Hinweis: Port ${GUNICORN_PORT} ist bereits belegt."
fi

# 3) Migrationen aktuell? (nur ein Hinweis - angewendet werden Migrationen ausschliesslich
#    explizit durch deploy/update.sh, nie implizit beim Service-Start)
if [ -x ".venv/bin/flask" ]; then
    CURRENT="$(.venv/bin/flask db current 2>/dev/null | head -1 || echo "unbekannt")"
    HEAD="$(.venv/bin/flask db heads 2>/dev/null | head -1 || echo "unbekannt")"
    if [ "$CURRENT" != "$HEAD" ]; then
        echo "[pre-start] Hinweis: DB-Migration evtl. nicht aktuell (current: '${CURRENT}', head: '${HEAD}')."
    fi
fi

# 4) Upload-Ordner vorhanden und beschreibbar? (harter Blocker - die App kann ohne
#    funktionieren nicht sinnvoll starten)
UPLOAD_DIR="${UPLOAD_FOLDER:-$APP_DIR/storage/uploads}"
mkdir -p "$UPLOAD_DIR"
if [ ! -w "$UPLOAD_DIR" ]; then
    echo "[pre-start] FEHLER: Upload-Ordner $UPLOAD_DIR ist nicht beschreibbar." >&2
    exit 1
fi

# 5) Log-Verzeichnis vorhanden?
mkdir -p "$APP_DIR/logs"

echo "[pre-start] Alle Pruefungen abgeschlossen."
