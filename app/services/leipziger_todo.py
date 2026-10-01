"""Arbeitslisten der Leipziger Liste: "Zu erledigen" und "Mitarbeiter".

Grundregel des Arbeitsablaufs: Eine Zeile MIT Datum (Beginn-Datum) ist erledigt, eine Zeile
OHNE Datum ist offen. Die Zuordnung zu Mitarbeitern laeuft ausschliesslich ueber
Benutzer -> Vermittlernummer -> normalisierte Vermittlernummer der Zeile. Es werden nur die
persistierten Analyseergebnisse einer einzigen Liste gelesen - keine Neuberechnung, keine
KI-Aufrufe."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from time import monotonic

from sqlalchemy import select
from sqlalchemy.orm import load_only, selectinload

from app.extensions import db
from app.models import Customer, Document, DocumentCustomer, User
from app.models.enums import DocStatus, DocType, UserRole
from app.services.analysis.leipziger_liste_view import row_status_key
from app.utils.vermittlernummer import format_vermittlernummer, vermittlernummer_key

OPEN_LABELS = {
    "angebot": "Angebot offen",
    "neugeschaeft": "Neugeschäft ohne Beginn",
    "fahrzeugwechsel": "Fahrzeugwechsel offen",
    "storno": "Storno",
    "unklar": "Klärungsbedarf",
}
DONE_LABELS = {
    "angebot": "Angebot",
    "neugeschaeft": "Neugeschäft",
    "fahrzeugwechsel": "Fahrzeugwechsel",
    "storno": "Storno",
    "unklar": "Sonstiges",
}


@dataclass(frozen=True)
class Entry:
    number: str
    label: str
    date: date | None
    broker_number: str | None
    broker_key: str | None
    status_key: str = "unklar"

    @property
    def has_date(self) -> bool:
        return self.date is not None

    @property
    def is_todo(self) -> bool:
        """"Zu erledigen": offen (ohne Datum) und nicht storniert."""
        return self.date is None and self.status_key != "storno"


def list_documents() -> list[Document]:
    """Auswaehlbare Listen (neueste zuerst), ohne die Zeilendaten zu laden."""
    return (
        Document.query.options(
            load_only(Document.id, Document.tenant_id, Document.original_filename, Document.uploaded_at, Document.processed_at)
        )
        .filter(Document.doc_type == DocType.LEIPZIGER_LISTE, Document.status == DocStatus.DONE)
        .order_by(Document.uploaded_at.desc(), Document.id.desc())
        .all()
    )


def select_document(documents: list[Document], document_id: int | None) -> Document | None:
    if document_id is not None:
        for document in documents:
            if document.id == document_id:
                return document
    return documents[0] if documents else None


def _parse_date(value) -> date | None:
    if isinstance(value, date):
        return value
    if isinstance(value, str) and value.strip():
        try:
            return date.fromisoformat(value.strip()[:10])
        except ValueError:
            # Ein vorhandener, aber unlesbarer Datumswert zaehlt fachlich trotzdem als "Datum".
            return date.min
    return None


# Kurzlebiger Zwischenspeicher der aufbereiteten Zeilen je Liste: Uebersicht, Reiter und Suche
# lesen dieselbe (unveraenderliche) Auswertung mehrfach kurz hintereinander. Schluessel enthaelt
# Mandant, Dokument und Verarbeitungszeitpunkt - eine neu ausgewertete Liste wird neu gelesen.
_ENTRY_CACHE: dict[tuple, tuple[float, list]] = {}
_ENTRY_CACHE_TTL_SECONDS = 60.0
_ENTRY_CACHE_MAX = 32


def load_entries(document: Document | None) -> list[Entry]:
    if document is None:
        return []
    key = (document.tenant_id, document.id, document.uploaded_at, document.processed_at)
    cached = _ENTRY_CACHE.get(key)
    now = monotonic()
    if cached is not None and now - cached[0] < _ENTRY_CACHE_TTL_SECONDS:
        return cached[1]
    entries = _read_entries(document)
    if len(_ENTRY_CACHE) >= _ENTRY_CACHE_MAX:
        _ENTRY_CACHE.clear()
    _ENTRY_CACHE[key] = (now, entries)
    return entries


def _read_entries(document: Document) -> list[Entry]:
    # Nur die benoetigten Spalten statt vollstaendiger ORM-Objekte (inkl. Kunden) - bei Listen
    # mit mehreren tausend Zeilen der wesentliche Zeitanteil. Der Tenant-Filter greift ueber
    # den globalen Listener; der explizite Filter ist zusaetzliche Absicherung.
    rows = db.session.execute(
        select(DocumentCustomer.row_data, Customer.name)
        .join(Customer, Customer.id == DocumentCustomer.customer_id, isouter=True)
        .where(DocumentCustomer.document_id == document.id, DocumentCustomer.tenant_id == document.tenant_id)
    ).all()
    entries = []
    for row_data, customer_name in rows:
        for row in row_data or []:
            if not isinstance(row, dict):
                continue
            status_key = row_status_key(row)
            row_date = _parse_date(row.get("contract_start_date"))
            number = (row.get("contract_number") or "").strip()
            if not number:
                number = customer_name or "Ohne Vertragsnummer"
            raw_broker = row.get("broker_number")
            entries.append(
                Entry(
                    number=number,
                    label=(DONE_LABELS if row_date else OPEN_LABELS)[status_key],
                    date=row_date,
                    broker_number=format_vermittlernummer(raw_broker),
                    broker_key=vermittlernummer_key(raw_broker),
                    status_key=status_key,
                )
            )
    return entries


def open_entries(entries: list[Entry], broker_key: str | None = None, *, all_brokers: bool = False) -> list[Entry]:
    """Offene Zeilen (ohne Datum). Ohne all_brokers nur die der angegebenen Vermittlernummer -
    fehlt diese, gibt es bewusst keine Treffer statt aller Zeilen."""
    return sorted(
        (e for e in entries if not e.has_date and (all_brokers or (broker_key and e.broker_key == broker_key))),
        key=lambda e: e.number,
    )


def done_entries(entries: list[Entry], broker_key: str | None) -> list[Entry]:
    return sorted(
        (e for e in entries if e.has_date and broker_key and e.broker_key == broker_key),
        key=lambda e: (e.date, e.number),
        reverse=True,
    )


def user_display_name(user: User) -> str:
    profile = user.employee_profile
    if profile is not None and profile.display_name:
        return profile.display_name
    return user.email


def team_members() -> list[User]:
    """Alle aktiven Buero-Mitglieder des eigenen Mandanten mit Vermittlernummer (der Tenant-
    Filter greift automatisch; SUPER_ADMIN-Konten gehoeren nicht zum Buero)."""
    users = (
        User.query.options(selectinload(User.employee_profile))
        .filter(
            User.is_active.is_(True),
            User.deleted_at.is_(None),
            User.role != UserRole.SUPER_ADMIN,
            User.vermittlernummer.isnot(None),
        )
        .all()
    )
    users = [user for user in users if vermittlernummer_key(user.vermittlernummer)]
    return sorted(users, key=lambda user: user_display_name(user).lower())


def broker_names(users: list[User]) -> dict[str, str]:
    return {vermittlernummer_key(user.vermittlernummer): user_display_name(user) for user in users}


TABS = {
    "zu-erledigen": "Zu erledigen",
    "mit-datum": "Mit Datum",
    "ohne-datum": "Ohne Datum",
}


def visible_entries(entries: list[Entry], broker_key: str | None, *, all_brokers: bool) -> list[Entry]:
    """Mitarbeiter: nur Zeilen der eigenen Vermittlernummer (ohne Nummer: keine). Buero-Admin:
    alle Zeilen der Liste des eigenen Bueros."""
    if all_brokers:
        return list(entries)
    if not broker_key:
        return []
    return [entry for entry in entries if entry.broker_key == broker_key]


def search_entries(entries: list[Entry], query: str | None) -> list[Entry]:
    """Suche nach Vertrags- oder Vermittlernummer, unabhaengig von Leerzeichen, Strichen und
    Gross-/Kleinschreibung."""
    needle = _search_key(query)
    if not needle:
        return entries
    return [
        entry
        for entry in entries
        if needle in _search_key(entry.number) or (entry.broker_key and needle in entry.broker_key)
    ]


def _search_key(value: str | None) -> str:
    return "".join(ch for ch in (value or "").upper() if ch.isalnum())


def tab_entries(entries: list[Entry], tab: str) -> list[Entry]:
    if tab == "mit-datum":
        return sorted((e for e in entries if e.has_date), key=lambda e: (e.date, e.number), reverse=True)
    if tab == "ohne-datum":
        return sorted((e for e in entries if not e.has_date), key=lambda e: e.number)
    return sorted((e for e in entries if e.is_todo), key=lambda e: e.number)


def tab_counts(entries: list[Entry]) -> dict[str, int]:
    return {
        "zu-erledigen": sum(1 for e in entries if e.is_todo),
        "mit-datum": sum(1 for e in entries if e.has_date),
        "ohne-datum": sum(1 for e in entries if not e.has_date),
    }

