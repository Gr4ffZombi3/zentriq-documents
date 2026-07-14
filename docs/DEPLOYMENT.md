# Deployment

## Entwicklungsstrategie

Es wird ausschließlich lokal entwickelt. Der Produktionsserver ist reines Deployment-Ziel:

- Keine manuellen Codeänderungen auf dem Server.
- Keine manuellen Datenbankänderungen auf dem Server — ausschließlich über Flask-Migrate-Migrationen, die lokal erzeugt und per Git übertragen werden.
- Jede Änderung entsteht lokal, wird getestet, committet und per `git push` übertragen.
- Der Server führt ausschließlich `deploy.sh` aus.

## Einmalige Server-Einrichtung

Diese Schritte sind nur beim ersten Einrichten eines neuen Servers nötig, nicht bei jedem Deployment.

```bash
# Repository klonen
sudo mkdir -p /opt/zentriq-documents
sudo chown $USER:$USER /opt/zentriq-documents
git clone <repo-url> /opt/zentriq-documents
cd /opt/zentriq-documents

# Virtuelle Umgebung anlegen
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# .env aus .env.example erzeugen und mit echten Produktionswerten befuellen
cp .env.example .env
nano .env   # FLASK_ENV=production, echte DATABASE_URL, SECRET_KEY, OPENAI_API_KEY, ...

# Erste Migration anwenden
flask db upgrade

# systemd-Units installieren
sudo cp deploy/systemd/zentriq.service /etc/systemd/system/
sudo cp deploy/systemd/zentriq-worker.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now zentriq
sudo systemctl enable --now zentriq-worker

# nginx (Beispielkonfiguration anpassen, siehe deploy/nginx/zentriq.conf.example)
sudo cp deploy/nginx/zentriq.conf.example /etc/nginx/sites-available/zentriq
sudo ln -s /etc/nginx/sites-available/zentriq /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
```

`zentriq` und `zentriq-worker` laufen unter einem dedizierten `zentriq`-Systembenutzer (nicht root) — vor der Aktivierung anlegen: `sudo useradd -r -s /usr/sbin/nologin zentriq` und Verzeichnisrechte entsprechend setzen.

## Laufendes Deployment

Nach jedem `git push` von lokal:

```bash
ssh <server>
cd /opt/zentriq-documents
./deploy.sh
```

`deploy.sh` übernimmt: `git pull`, Abhängigkeiten installieren, Migrationen anwenden, alle drei Dienste neu starten und deren Status prüfen.

## Lokale Entwicklung vor jedem Commit

```powershell
.\manage.ps1 check
```

Führt `ruff check .` (Syntax-/Importfehler, ungenutzte Importe) und die volle Testsuite aus. Vor jedem Commit sollten außerdem manuell geprüft werden:

- `requirements.txt` aktuell (neue Abhängigkeiten ergänzt, ungenutzte entfernt)
- `.env.example` aktuell (neue Config-Keys ergänzt)
- Migration vorhanden für jede Modelländerung (`flask db migrate`)
- Keine Debug-Ausgaben (`print()`, `pdb`) im committeten Code

## Verzeichnisstruktur (Server)

```
/opt/zentriq-documents/
├── .venv/              # lokal auf dem Server erzeugt, nicht in Git
├── .env                # Produktions-Secrets, nicht in Git
├── storage/uploads/     # hochgeladene PDFs, nicht in Git
└── ...                  # restlicher Code, per Git verwaltet
```
