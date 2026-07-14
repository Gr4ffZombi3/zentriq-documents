# Deployment

## Entwicklungsstrategie

Es wird **ausschließlich lokal entwickelt**. Der Produktionsserver ist reines Deployment-Ziel:

- Keine manuellen Codeänderungen auf dem Server.
- Keine manuellen Datenbankänderungen auf dem Server — ausschließlich über Flask-Migrate-Migrationen, die lokal erzeugt und per Git übertragen werden.
- Jede Änderung entsteht lokal, wird getestet, committet und per `git push` übertragen.
- Der Server führt ausschließlich Skripte aus diesem `deploy/`-Verzeichnis aus.

```
Lokal entwickeln
      ↓
git add . && git commit && git push
      ↓
Server: git pull && ./deploy.sh
```

## Frischer Server (einmalig)

Repository klonen und einrichten:

```bash
git clone <repo-url> /opt/zentriq-documents
cd /opt/zentriq-documents
chmod +x deploy/install.sh
./deploy/install.sh
```

`deploy/install.sh` richtet **alles** automatisch ein:

1. System-Pakete (Python, MariaDB, Redis, Nginx, Tesseract OCR, Build-Tools)
2. MariaDB- und Redis-Dienst aktivieren
3. Virtuelle Umgebung + Python-Abhängigkeiten
4. `.env` erzeugen (SECRET_KEY und Datenbank-Zugangsdaten werden automatisch generiert)
5. Datenbank-Migrationen anwenden
6. Upload-/Log-Ordner mit passenden Rechten
7. systemd-Services `zentriq-api` und `zentriq-worker` installieren und starten
8. Nginx als Reverse Proxy konfigurieren
9. Firewall (ufw: nur SSH + HTTP/HTTPS)
10. SSL-Zertifikat per Let's Encrypt/certbot (falls eine Domain angegeben wird)

Das Skript ist **idempotent** — ein erneuter Lauf nach einem Fehler überschreibt keine bereits vorhandene Datenbank oder `.env`.

Danach ist die Seite vollständig online. Einziger manueller Schritt: den `OPENAI_API_KEY` in `.env` eintragen (für KI-Auswertung/OCR-Fallback — kann nicht automatisiert werden, da ein echter Account nötig ist), danach:

```bash
systemctl restart zentriq-api zentriq-worker
```

## Laufendes Deployment

Nach jedem `git push` von lokal:

```bash
ssh <server>
cd /opt/zentriq-documents
./deploy.sh
```

`deploy.sh` ruft lediglich `deploy/update.sh` auf, welches:

1. `git pull`
2. venv aktivieren, `pip install -r requirements.txt`
3. `flask db upgrade`
4. (kein Static-Build nötig — Flask liefert `app/static/` direkt aus)
5. `zentriq-api` und `zentriq-worker` neu starten
6. `deploy/post-deploy.sh` als Verifikation: nginx-Konfig-Check, Dienststatus, HTTP-Health-Check gegen `/auth/login`

Schlägt der Health-Check fehl, bricht das Skript mit Exit-Code ≠ 0 ab — sichtbar bevor man denkt, das Deployment sei erfolgreich gewesen.

## Architekturkomponenten

| Datei | Zweck |
|---|---|
| `deploy/install.sh` | Einmalige Ersteinrichtung eines frischen Servers |
| `deploy/update.sh` | Laufendes Update (von `deploy.sh` aufgerufen) |
| `deploy/pre-start.sh` | `ExecStartPre` für beide systemd-Services: räumt verwaiste Prozesse auf, prüft Upload-Ordner/Migrationsstand |
| `deploy/post-deploy.sh` | Verifikation nach jedem Deployment: Dienststatus + HTTP-Health-Check |
| `deploy/systemd/*.template` | Vorlagen für die systemd-Units, von `install.sh` mit dem tatsächlichen Pfad befüllt |
| `deploy/nginx/*.template` | Vorlage für die Nginx-Reverse-Proxy-Konfiguration |
| `wsgi.py` | Produktions-Entrypoint für Gunicorn (`gunicorn wsgi:app`) — getrennt von `run.py`, das den lokalen Dev-Server startet |

## Verzeichnisstruktur (Server)

```
/opt/zentriq-documents/
├── .venv/              # lokal auf dem Server erzeugt, nicht in Git
├── .env                # Produktions-Secrets, automatisch von install.sh erzeugt, nicht in Git
├── storage/uploads/     # hochgeladene PDFs, nicht in Git
├── logs/                # Gunicorn-/Celery-Logs, nicht in Git
└── ...                  # restlicher Code, per Git verwaltet
```

## systemd-Services

```bash
systemctl status zentriq-api       # Gunicorn (Flask-App)
systemctl status zentriq-worker    # Celery-Worker (OCR/KI-Verarbeitung)
journalctl -u zentriq-api -n 100 --no-pager
journalctl -u zentriq-worker -n 100 --no-pager
```

Beide laufen aktuell unter `root` (bewusste Entscheidung, um die Server-Einrichtung so einfach wie möglich zu halten — siehe Abschlussbericht im Commit-Verlauf für die Sicherheitsabwägung). Eine spätere Umstellung auf einen dedizierten Service-User ist möglich, erfordert dann aber eine Migration der Datei-/Verzeichnisrechte (Uploads, venv, Logs).

## Lokale Entwicklung vor jedem Commit

```powershell
.\manage.ps1 check
```

Führt `ruff check .` (Syntax-/Importfehler, ungenutzte Importe) und die volle Testsuite aus. Vor jedem Commit sollten außerdem manuell geprüft werden:

- `requirements.txt` aktuell (neue Abhängigkeiten ergänzt, ungenutzte entfernt)
- `.env.example` aktuell (neue Config-Keys ergänzt)
- Migration vorhanden für jede Modelländerung (`flask db migrate`)
- Keine Debug-Ausgaben (`print()`, `pdb`) im committeten Code

## Troubleshooting

**Service startet nicht / Neustart-Schleife**: `journalctl -u zentriq-api -n 50 --no-pager` — `pre-start.sh` bricht nur bei einem nicht beschreibbaren Upload-Ordner hart ab, alles andere sind Hinweise, keine Blocker.

**502 Bad Gateway**: Gunicorn läuft nicht oder lauscht nicht auf Port 8000 — `systemctl status zentriq-api` prüfen.

**Health-Check schlägt fehl**: `curl http://127.0.0.1:8000/auth/login` manuell ausführen und die Fehlermeldung/den Log direkt prüfen.

**SSL nachträglich einrichten**: `certbot --nginx -d <domain>`, danach `SESSION_COOKIE_SECURE=true` in `.env` setzen und `systemctl restart zentriq-api zentriq-worker`.
