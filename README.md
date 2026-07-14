# Zentriq Documents

KI-Dokumentenanalyse für Versicherungs- und Kfz-Dienstleister. PDF-Upload → OCR → LLM-Strukturextraktion → Dashboard.

## Voraussetzungen

- Python 3.11+ (getestet mit 3.14)
- [Docker Desktop](https://www.docker.com/products/docker-desktop/) (für MariaDB + Redis)
- [Tesseract OCR](https://github.com/UB-Mannheim/tesseract/wiki) (Windows-Installer, inkl. Sprachpaket `deu`) — Pfad zur `tesseract.exe` in `.env` als `TESSERACT_CMD` eintragen
- OpenAI API-Key

## Setup (Windows / PowerShell)

```powershell
copy .env.example .env
# .env ausfüllen: DATABASE_URL-Zugangsdaten, OPENAI_API_KEY, TESSERACT_CMD

docker compose up -d mariadb redis

.\manage.ps1 setup      # venv anlegen + Abhängigkeiten installieren
.\manage.ps1 upgrade    # Datenbank-Migrationen anwenden
.\manage.ps1 run        # Flask-Dev-Server starten -> http://127.0.0.1:5000
```

Für die Hintergrundverarbeitung (ab Meilenstein M3) zusätzlich in einem zweiten Terminal:

```powershell
.\manage.ps1 worker     # Celery-Worker (--pool=solo, Windows-kompatibel)
```

## Architektur

- **Backend**: Flask (Application-Factory-Pattern) + SQLAlchemy + MariaDB, Celery/Redis für asynchrone OCR-/KI-Verarbeitung
- **Frontend**: Flask/Jinja2 + HTMX + Alpine.js, dunkles Dashboard-Theme
- **OCR**: Tesseract (primär) mit OpenAI-Vision-Fallback bei schlechter Qualität
- **KI**: OpenAI GPT für Strukturextraktion, Klassifikation, Empfehlungen und natürlichsprachliche Suche

Details siehe Projektplan.

## Tests

```powershell
.\manage.ps1 test
```

Vor jedem Commit: `.\manage.ps1 check` (Lint + volle Testsuite).

## Deployment

Entwicklung erfolgt ausschließlich lokal; der Server ist reines Deployment-Ziel.

- **Frischer Server**: `chmod +x deploy/install.sh && ./deploy/install.sh` — richtet Python,
  MariaDB, Redis, Nginx, systemd-Services, Firewall und optional SSL vollautomatisch ein.
- **Laufendes Update**: `git pull && ./deploy.sh` — kein manueller Server-Zugriff auf Code
  oder Datenbank nötig.

Vollständige Dokumentation: [DEPLOYMENT.md](DEPLOYMENT.md).
