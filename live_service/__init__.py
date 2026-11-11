"""zentriq-live: kleiner asyncio-SSE-Dienst fuer Anwesenheit und Live-Hinweise in Kundenakten.

Bewusst ohne Flask/SQLAlchemy - nur Standardbibliothek, itsdangerous und redis. Laeuft als eigener
Dienst (deploy/systemd/zentriq-live.service.template) auf 127.0.0.1:8001 hinter nginx /live/."""
