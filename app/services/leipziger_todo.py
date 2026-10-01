"""Arbeitslisten der Leipziger Liste: "Zu erledigen" und "Mitarbeiter".

Grundregel des Arbeitsablaufs: Eine Zeile MIT Datum (Beginn-Datum) ist erledigt, eine Zeile
OHNE Datum ist offen. Die Zuordnung zu Mitarbeitern laeuft ausschliesslich ueber
Benutzer -> Vermittlernummer -> normalisierte Vermittlernummer der Zeile. Es werden nur die
persistierten Analyseergebnisse einer einzigen Liste gelesen - keine Neuberechnung, keine
KI-Aufrufe."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from sqlalchemy.orm import load_only, selectinload

from app.models import Document, DocumentCustomer, User
from app.models.enums import DocStatus, DocType
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

    @property
    def has_date(self) -> bool:
        return self.date is not None


def list_documents() -> list[Document]:
    """Auswaehlbare Listen (neueste zuerst), ohne die Zeilendaten zu laden."""
    return (
        Document.query.options(load_only(Document.id, Document.original_filename, Document.uploaded_at))
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


def load_entries(document: Document | None) -> list[Entry]:
    if document is None:
        return []
    document_customers = (
        DocumentCustomer.query.options(selectinload(DocumentCustomer.customer))
        .filter(DocumentCustomer.document_id == document.id)
        .all()
    )
    entries = []
    for doc_customer in document_customers:
        for row in doc_customer.row_data or []:
            if not isinstance(row, dict):
                continue
            status_key = row_status_key(row)
            row_date = _parse_date(row.get("contract_start_date"))
            number = (row.get("contract_number") or "").strip()
            if not number:
                customer = doc_customer.customer
                number = customer.name if customer is not None else "Ohne Vertragsnummer"
            raw_broker = row.get("broker_number")
            entries.append(
                Entry(
                    number=number,
                    label=(DONE_LABELS if row_date else OPEN_LABELS)[status_key],
                    date=row_date,
                    broker_number=format_vermittlernummer(raw_broker),
                    broker_key=vermittlernummer_key(raw_broker),
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
    """Alle aktiven Benutzer des eigenen Mandanten mit Vermittlernummer (der Tenant-Filter
    greift automatisch)."""
    users = (
        User.query.options(selectinload(User.employee_profile))
        .filter(User.is_active.is_(True), User.vermittlernummer.isnot(None))
        .all()
    )
    users = [user for user in users if vermittlernummer_key(user.vermittlernummer)]
    return sorted(users, key=lambda user: user_display_name(user).lower())


def broker_names(users: list[User]) -> dict[str, str]:
    return {vermittlernummer_key(user.vermittlernummer): user_display_name(user) for user in users}
