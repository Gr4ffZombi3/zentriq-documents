"""Normalisierte Vorgaenge der Leipziger Liste (Tabelle leipziger_entries) und Importvergleich.

- rebuild_entries(): schreibt nach jedem (Neu-)Import die Vorgaenge eines Dokuments neu.
- compare_with_previous(): vergleicht die Vorgaenge mit der vorherigen Liste desselben Bueros
  ueber die Vertrags-/Vorgangsnummer (plus Sparte) und liefert die Kennzahlen
  "Neu", "Bereits vorhanden" und "Geaendert" samt der geaenderten Felder.

Mehrere Vorgaenge mit gleicher Nummer und Sparte (z. B. zwei Zeilen eines Kunden) werden in
PDF-Reihenfolge paarweise verglichen, damit keiner verloren geht."""

from __future__ import annotations

from collections import defaultdict

from sqlalchemy import select

from app.extensions import db
from app.models import Document, LeipzigerEntry
from app.utils.vermittlernummer import format_vermittlernummer, vermittlernummer_key

# Felder, deren Aenderung zwischen zwei Importen angezeigt wird (Feld -> Bezeichnung).
COMPARED_FIELDS = {
    "status_code": "Status",
    "start_date": "Datum",
    "broker_key": "Vermittler",
    "customer_key": "Kunde",
}
# Obergrenze der gespeicherten Einzelaenderungen (die Kennzahlen zaehlen immer vollstaendig).
MAX_STORED_CHANGES = 300


def search_key(value) -> str:
    """Nur Buchstaben und Ziffern, gross geschrieben: "KH 47-11" -> "KH4711"."""
    if not isinstance(value, str):
        return ""
    return "".join(ch for ch in value.upper() if ch.isalnum())


def _text(value, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    value = " ".join(value.split())
    return value[:limit] or None


def _position(row: dict, fallback: int) -> int:
    page, line = row.get("source_page"), row.get("source_row")
    if isinstance(page, int) and isinstance(line, int):
        return page * 100000 + line
    return 2 * 10**9 - 10**6 + fallback


def entry_values(row: dict, fallback_customer: str | None, fallback_position: int) -> dict | None:
    """Uebersetzt eine gespeicherte Zeile (row_data) in die Spalten von LeipzigerEntry."""
    if not isinstance(row, dict):
        return None
    row_customer = row.get("customer") if isinstance(row.get("customer"), dict) else {}
    customer_name = _text(row_customer.get("name"), 255) or _text(fallback_customer, 255)
    contract_number = _text(row.get("contract_number"), 100)
    raw_broker = row.get("broker_number")
    start = row.get("contract_start_date")
    return {
        "position": _position(row, fallback_position),
        "contract_number": contract_number,
        "contract_key": search_key(contract_number)[:100] or None,
        "customer_name": customer_name,
        "customer_key": search_key(customer_name)[:255] or None,
        "broker_number": (format_vermittlernummer(raw_broker) or "")[:50] or None,
        "broker_key": (vermittlernummer_key(raw_broker) or "")[:50] or None,
        "status_code": (_text(row.get("status_code"), 20) or "").upper() or None,
        "product_line": _text(row.get("product_line"), 100),
        "start_date": start.strip()[:10] if isinstance(start, str) and start.strip() else None,
    }


def rebuild_entries(document: Document) -> int:
    """Ersetzt die Vorgaenge des Dokuments durch die aktuell gespeicherten Zeilen."""
    LeipzigerEntry.query.filter(LeipzigerEntry.document_id == document.id).delete(synchronize_session=False)
    values = []
    for doc_customer in document.document_customers:
        customer_name = doc_customer.customer.name if doc_customer.customer is not None else None
        for row in doc_customer.row_data or []:
            item = entry_values(row, customer_name, len(values))
            if item is not None:
                # Jeder Vorgang bleibt eigenstaendig, gehoert aber genau einem Zentriq-Kunden.
                item["customer_id"] = doc_customer.customer_id
                values.append(item)
    db.session.add_all(
        LeipzigerEntry(tenant_id=document.tenant_id, document_id=document.id, **item) for item in values
    )
    return len(values)


def find_previous_document(document: Document) -> Document | None:
    """Vorangehende, fertig ausgewertete Liste desselben Bueros - nach Berichtsdatum
    (Kalenderwoche), nicht nach Upload-Zeitpunkt (siehe list_comparison.find_previous_list)."""
    from app.services.list_comparison import find_previous_list

    return find_previous_list(document)


_COMPARE_COLUMNS = (
    LeipzigerEntry.position,
    LeipzigerEntry.contract_number,
    LeipzigerEntry.contract_key,
    LeipzigerEntry.customer_name,
    LeipzigerEntry.customer_key,
    LeipzigerEntry.broker_number,
    LeipzigerEntry.broker_key,
    LeipzigerEntry.status_code,
    LeipzigerEntry.product_line,
    LeipzigerEntry.start_date,
)


def _load_rows(document_id: int, tenant_id: int) -> list:
    return db.session.execute(
        select(*_COMPARE_COLUMNS)
        .where(LeipzigerEntry.document_id == document_id, LeipzigerEntry.tenant_id == tenant_id)
        .order_by(LeipzigerEntry.position, LeipzigerEntry.id)
    ).all()


def _identity(row) -> tuple:
    """Vorgang primaer ueber die Vertrags-/Vorgangsnummer; ohne Nummer ueber den Kunden."""
    product = search_key(row.product_line)
    if row.contract_key:
        return ("nr", row.contract_key, product)
    return ("kunde", row.customer_key or "", product)


def _display(row, field: str):
    if field == "broker_key":
        return row.broker_number
    if field == "customer_key":
        return row.customer_name
    return getattr(row, field)


def compare_entries(current_rows: list, previous_rows: list) -> dict:
    previous_by_identity: dict[tuple, list] = defaultdict(list)
    for row in previous_rows:
        previous_by_identity[_identity(row)].append(row)

    counts = {"new": 0, "unchanged": 0, "changed": 0}
    changes = []
    for row in current_rows:
        candidates = previous_by_identity.get(_identity(row))
        if not candidates:
            counts["new"] += 1
            continue
        previous = candidates.pop(0)
        changed_fields = {
            label: [_display(previous, field), _display(row, field)]
            for field, label in COMPARED_FIELDS.items()
            if getattr(previous, field) != getattr(row, field)
        }
        if not changed_fields:
            counts["unchanged"] += 1
            continue
        counts["changed"] += 1
        if len(changes) < MAX_STORED_CHANGES:
            changes.append(
                {
                    "contract_number": row.contract_number,
                    "customer": row.customer_name,
                    "broker_key": row.broker_key,
                    "fields": changed_fields,
                }
            )
    return {**counts, "changes": changes}


def compare_with_previous(document: Document, previous: Document | None = None) -> dict | None:
    """Kennzahlen gegenueber der vorherigen Liste; None, wenn es keine vorherige Liste gibt."""
    previous = previous or find_previous_document(document)
    if previous is None:
        return None
    result = compare_entries(
        _load_rows(document.id, document.tenant_id), _load_rows(previous.id, document.tenant_id)
    )
    return {
        "previous_document_id": previous.id,
        "previous_filename": previous.original_filename,
        **result,
    }



def stored_comparison(document: Document | None) -> dict | None:
    """Beim Import gespeicherter Vergleich (nur fuer Buero-Admins angezeigt: die Kennzahlen
    umfassen das ganze Buero)."""
    if document is None:
        return None
    meta = (document.extra_data or {}).get("leipziger_analysis") or {}
    return meta.get("import_comparison") or None
