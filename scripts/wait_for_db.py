"""Wartet, bis die konfigurierte Datenbank Verbindungen annimmt (fuer Docker-Startreihenfolge)."""
import os
import sys
import time

from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError


def wait_for_db(timeout=60, interval=2):
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        print("DATABASE_URL ist nicht gesetzt.", file=sys.stderr)
        return False

    engine = create_engine(database_url)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            print("Datenbank erreichbar.")
            return True
        except OperationalError:
            time.sleep(interval)
    print("Datenbank nach Timeout nicht erreichbar.", file=sys.stderr)
    return False


if __name__ == "__main__":
    sys.exit(0 if wait_for_db() else 1)
