"""Serialisiert das Anlegen von Kunden je Buero (und das Auswerten je Dokument).

Kundenerkennung und -anlage laufen als "erst suchen, dann anlegen". Zwei gleichzeitige
Vorgaenge (zwei Listen im Worker, Liste und Memo, doppelter Klick auf "Neu einlesen") sehen
dabei die jeweils noch nicht gespeicherten Kunden des anderen nicht und legen denselben Kunden
doppelt an. Ein Namens-Unique-Index ist keine Loesung (zwei Personen koennen gleich heissen),
deshalb eine benannte Datenbanksperre (MariaDB GET_LOCK) ueber eine eigene Verbindung:

- Die Sperre wird VOR dem ersten Lesen der Kunden geholt und erst NACH dem Commit freigegeben.
- Wer die Sperre haelt, muss danach eine neue Transaktion beginnen, damit er die Kunden des
  vorherigen Sperrinhabers sieht (REPEATABLE READ) - siehe fresh_transaction().
- Die Sperrverbindung wird beim Freigeben verworfen statt in den Pool zurueckgegeben; damit
  bleibt auch nach einem Fehler nie eine Sperre an einer gepoolten Verbindung haengen.

Auf Datenbanken ohne GET_LOCK (SQLite in den Tests) ist die Sperre wirkungslos."""

from __future__ import annotations

from sqlalchemy import text

from app.extensions import db


class CustomerLockTimeout(RuntimeError):
    """Die Sperre wurde innerhalb der Wartezeit nicht frei."""


class NamedLock:
    def __init__(self, name: str, timeout_seconds: int):
        self.name = name[:64]
        self.timeout_seconds = timeout_seconds
        self._connection = None

    @property
    def held(self) -> bool:
        return self._connection is not None

    def acquire(self) -> NamedLock:
        if self._connection is not None or db.engine.dialect.name not in ("mysql", "mariadb"):
            return self
        connection = db.engine.connect()
        try:
            acquired = connection.execute(
                text("SELECT GET_LOCK(:name, :timeout)"), {"name": self.name, "timeout": self.timeout_seconds}
            ).scalar()
        except Exception:
            connection.invalidate()
            connection.close()
            raise
        if acquired != 1:
            connection.invalidate()
            connection.close()
            raise CustomerLockTimeout(self.name)
        self._connection = connection
        return self

    def release(self) -> None:
        connection, self._connection = self._connection, None
        if connection is None:
            return
        try:
            connection.execute(text("SELECT RELEASE_LOCK(:name)"), {"name": self.name})
        except Exception:
            pass  # Verbindung wird ohnehin verworfen - der Server gibt die Sperre dann frei.
        finally:
            connection.invalidate()
            connection.close()

    def __enter__(self) -> NamedLock:
        return self.acquire()

    def __exit__(self, *exc) -> None:
        self.release()


def customer_creation_lock(tenant_id: int, timeout_seconds: int = 120) -> NamedLock:
    return NamedLock(f"zentriq-customers-{tenant_id}", timeout_seconds)


def document_processing_lock(document_id: int, timeout_seconds: int = 900) -> NamedLock:
    return NamedLock(f"zentriq-document-{document_id}", timeout_seconds)


def fresh_transaction() -> None:
    """Beendet die laufende Lese-Transaktion, damit die naechste Abfrage einen aktuellen
    Datenstand sieht (REPEATABLE READ). Nur aufrufen, wenn nichts Ungespeichertes ansteht."""
    db.session.commit()
