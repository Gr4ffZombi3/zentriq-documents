# CLAUDE.md – Verbindliche Arbeitsregeln

Dieses Verzeichnis (`/root/Zentriqai`) liegt auf dem **Produktionsserver**. Die folgenden
Regeln gelten ausnahmslos für jede Sitzung.

## Grundregeln

- Vor jeder Änderung zuerst `git status` prüfen.
- `.env` niemals ändern, löschen oder ausgeben (auch nicht auszugsweise, z. B. per `cat`,
  `grep`, `source` mit Ausgabe o. Ä.). Dasselbe gilt für Kopien wie `.env.save`.
- Keine Secrets, Tokens oder Passwörter ausgeben – weder aus Dateien noch aus Umgebungs-
  variablen, Logs oder Datenbank.
- Keine Produktionsdaten löschen (Datenbankinhalte, `storage/uploads/`, `logs/`).
- Keine Datenbanktabellen manuell verändern (kein manuelles `ALTER`, `DROP`, `DELETE`,
  `UPDATE`, `INSERT` o. Ä. per SQL-Client).
- Datenbankänderungen ausschließlich über Flask-Migrate (`flask db migrate` /
  `flask db upgrade`), mit Migrationsdatei in `migrations/versions/` unter Git.
- Vor riskanten Änderungen an systemd, nginx, Datenbank oder Produktionsdaten **zuerst
  nachfragen** – das gilt auch für Neustarts der Services und das Ausführen von
  `deploy.sh`, `deploy/update.sh`, `deploy/install.sh` oder `deploy/repair-nginx.sh`.
- Änderungen müssen über Git nachvollziehbar bleiben.
- Nach Änderungen Tests ausführen: `.venv/bin/python -m pytest` (Tests nutzen eine
  temporäre SQLite-DB, nicht die Produktionsdatenbank). Zusätzlich `.venv/bin/ruff check .`.
- Danach `git diff` prüfen.
- GitHub (`origin`) bleibt die Versionssicherung.
- Arbeite nur innerhalb von `/root/Zentriqai`, außer der Nutzer erlaubt ausdrücklich etwas
  anderes.

## Bekannte Services

| Service          | Zweck                                         |
|------------------|-----------------------------------------------|
| `zentriq-api`    | Gunicorn / Flask-App (127.0.0.1:8000)         |
| `zentriq-worker` | Celery-Worker (OCR/KI, Mailbox, HUK)          |
| `zentriq-beat`   | Celery Beat (periodischer Postfach-Abruf)     |
| `nginx`          | Reverse Proxy                                 |
| `mariadb`        | Datenbank                                     |
| `redis-server`   | Celery-Broker                                 |

Lesende Befehle (`systemctl status`, `journalctl -u … --no-pager`) sind unkritisch;
`restart`, `stop`, `enable`, `disable`, `daemon-reload` und Änderungen an Unit- oder
nginx-Dateien nur nach Rückfrage.

## Relevante Dateien

- `deploy.sh` → `deploy/update.sh` – laufendes Deployment (git pull, pip, `flask db upgrade`,
  nginx-Reparatur, Service-Neustart, Verifikation)
- `deploy/install.sh`, `deploy/lib.sh` – Ersteinrichtung
- `deploy/pre-start.sh`, `deploy/post-deploy.sh` – ExecStartPre bzw. Verifikation
- `deploy/systemd/*.service.template`, `deploy/nginx/zentriq.conf.template`
- `DEPLOYMENT.md` – Deployment-Dokumentation
