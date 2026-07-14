"""Produktions-Entrypoint fuer Gunicorn (`gunicorn wsgi:app`). Getrennt von `run.py`, das
den lokalen Flask-Entwicklungsserver startet - Gunicorn soll niemals ueber `run.py` laufen
und der Werkzeug-Dev-Server niemals in Produktion."""

from dotenv import load_dotenv

load_dotenv()

from app import create_app  # noqa: E402

app = create_app()
