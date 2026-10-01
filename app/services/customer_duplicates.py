"""Dubletten-Erkennung und Zusammenfuehren von Kunden - ausschliesslich innerhalb des eigenen
Bueros (globaler Tenant-Filter, zusaetzlich explizite tenant_id-Bedingungen).

Abgleich in dieser Reihenfolge ueber die indizierten Schluessel (app/utils/customer_keys.py):
1. Kundennummer, 2. Telefonnummer, 3. Name. Telefon- und Namenstreffer gelten nicht, wenn beide
Datensaetze unterschiedliche Kundennummern (bzw. beim Namen: unterschiedliche Geburtsdaten)
haben - dann handelt es sich erkennbar um verschiedene Personen.

Es wird nie automatisch zusammengefuehrt. Beim Zusammenfuehren bleiben alle Verknuepfungen
erhalten, leere Werte ueberschreiben nie gefuellte, und abweichende Werte des aufgeloesten
Datensatzes werden im Kundenverlauf festgehalten."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import or_, update

from app.extensions import db
from app.models import (
    Customer,
    CustomerTimelineEvent,
    Document,
    DocumentCustomer,
    ListComparisonEntry,
    Recommendation,
    Task,
)
from app.models.audit_log import AuditEventType
from app.models.enums import TimelineEventType
from app.services.audit import log_audit_event
from app.services.timeline import log_timeline_event

MAX_DUPLICATES = 10

REASON_LABELS = {
    "customer_number": "gleiche Kundennummer",
    "phone": "gleiche Telefonnummer",
    "name": "gleicher Name",
}

# Felder, die beim Zusammenfuehren uebernommen werden (Feld -> Bezeichnung).
MERGE_FIELDS = {
    "name": "Name",
    "customer_number": "Kundennummer",
    "phone": "Telefon",
    "email": "E-Mail",
    "date_of_birth": "Geburtsdatum",
    "address": "Adresse",
    "postal_code": "PLZ",
    "city": "Ort",
    "assigned_user_id": "Zuständig",
}


class MergeError(ValueError):
    """Fehler mit einer fuer den Nutzer verstaendlichen Meldung."""


@dataclass(frozen=True)
class Duplicate:
    customer: Customer
    reason: str

    @property
    def reason_label(self) -> str:
        return REASON_LABELS[self.reason]


def _conflict(left, right) -> bool:
    return left is not None and right is not None and left != right


def duplicate_reason(customer: Customer, other: Customer) -> str | None:
    """Warum `other` eine moegliche Dublette von `customer` ist - oder None."""
    if customer.id == other.id or customer.tenant_id != other.tenant_id:
        return None
    if customer.customer_number_key and customer.customer_number_key == other.customer_number_key:
        return "customer_number"
    numbers_conflict = _conflict(customer.customer_number_key, other.customer_number_key)
    if customer.phone_key and customer.phone_key == other.phone_key and not numbers_conflict:
        return "phone"
    if (
        customer.name_key
        and customer.name_key == other.name_key
        and not numbers_conflict
        and not _conflict(customer.date_of_birth, other.date_of_birth)
    ):
        return "name"
    return None


def _candidates_query(customers: list[Customer]):
    conditions = []
    for column in ("customer_number_key", "phone_key", "name_key"):
        values = {getattr(customer, column) for customer in customers if getattr(customer, column)}
        if values:
            conditions.append(getattr(Customer, column).in_(values))
    if not conditions:
        return None
    tenant_ids = {customer.tenant_id for customer in customers}
    return Customer.query.filter(Customer.tenant_id.in_(tenant_ids), or_(*conditions))


def find_duplicates(customer: Customer) -> list[Duplicate]:
    query = _candidates_query([customer])
    if query is None:
        return []
    found = []
    for other in query.filter(Customer.id != customer.id).order_by(Customer.id).limit(MAX_DUPLICATES * 5):
        reason = duplicate_reason(customer, other)
        if reason:
            found.append(Duplicate(other, reason))
    order = list(REASON_LABELS)
    return sorted(found, key=lambda item: (order.index(item.reason), item.customer.id))[:MAX_DUPLICATES]


def duplicate_map(customers: list[Customer]) -> dict[int, list[Duplicate]]:
    """Dubletten fuer eine Seite von Kunden mit einer einzigen Abfrage."""
    query = _candidates_query(customers)
    if query is None:
        return {}
    candidates = query.all()
    result: dict[int, list[Duplicate]] = {}
    for customer in customers:
        matches = [Duplicate(other, reason) for other in candidates if (reason := duplicate_reason(customer, other))]
        if matches:
            result[customer.id] = matches
    return result


def link_counts(customer: Customer) -> dict[str, int]:
    """Anzahl der Verknuepfungen (fuer den Vergleich vor dem Zusammenfuehren)."""
    return {
        "Listen/Dokumente": DocumentCustomer.query.filter_by(customer_id=customer.id).count()
        + Document.query.filter_by(customer_id=customer.id).count(),
        "Verlaufseinträge": CustomerTimelineEvent.query.filter_by(customer_id=customer.id).count(),
        "Aufgaben": Task.query.filter_by(customer_id=customer.id).count(),
        "Empfehlungen": Recommendation.query.filter_by(customer_id=customer.id).count(),
    }


# Felder, deren Werte normalisiert verglichen werden ("0341 1" == "+49 341 1").
_KEYED_FIELDS = {"name": "name_key", "phone": "phone_key", "customer_number": "customer_number_key"}


def _same(field: str, target: Customer, source: Customer) -> bool:
    if field in _KEYED_FIELDS:
        key = _KEYED_FIELDS[field]
        if getattr(target, key) and getattr(target, key) == getattr(source, key):
            return True
    return getattr(target, field) == getattr(source, field)


def compare_rows(target: Customer, source: Customer) -> list[dict]:
    """Gegenueberstellung fuer die Vergleichsseite: je Feld beide Werte und was passiert
    ("same", "fill" = wird ergaenzt, "differ" = abweichend, bleibt im Verlauf, "empty")."""
    rows = []
    for field, label in MERGE_FIELDS.items():
        if field == "assigned_user_id":
            left = target.assigned_user.email if target.assigned_user else None
            right = source.assigned_user.email if source.assigned_user else None
        else:
            left, right = _display(getattr(target, field)) or None, _display(getattr(source, field)) or None
        if not right:
            state = "same" if left else "empty"
        elif not left:
            state = "fill"
        elif _same(field, target, source) or left == right:
            state = "same"
        else:
            state = "differ"
        rows.append({"label": label, "target": left, "source": right, "state": state})
    return rows


def _display(value) -> str:
    if value is None:
        return ""
    if hasattr(value, "strftime"):
        return value.strftime("%d.%m.%Y")
    return str(value)


def merge_customers(target: Customer, source: Customer, actor) -> dict:
    """Fuehrt `source` in `target` zusammen und entfernt danach `source`.

    - Leere Felder von `target` werden aus `source` gefuellt, gefuellte nie ueberschrieben.
    - Abweichende Werte von `source` werden im Kundenverlauf von `target` festgehalten.
    - Alle Verknuepfungen (Listenzeilen, Dokumente, Verlauf, Aufgaben, Empfehlungen,
      Listenvergleiche) werden auf `target` umgehaengt."""
    if target.id == source.id:
        raise MergeError("Ein Datensatz kann nicht mit sich selbst zusammengeführt werden.")
    if target.tenant_id != source.tenant_id or target.tenant_id != actor.tenant_id:
        raise MergeError("Die Datensätze gehören nicht zu Ihrem Büro.")
    if duplicate_reason(target, source) is None:
        raise MergeError("Die Datensätze sind keine erkannte Dublette und werden nicht zusammengeführt.")

    tenant_id, target_id = target.tenant_id, target.id
    filled, differing = [], {}
    for field, label in MERGE_FIELDS.items():
        source_value, target_value = getattr(source, field), getattr(target, field)
        if source_value in (None, ""):
            continue
        if target_value in (None, ""):
            setattr(target, field, source_value)
            filled.append(label)
        elif field != "assigned_user_id" and not _same(field, target, source):
            differing[label] = _display(source_value)

    # Listenzeilen: je Dokument darf ein Kunde nur einmal verknuepft sein - Zeilen zusammenlegen.
    target_links = {link.document_id: link for link in DocumentCustomer.query.filter_by(customer_id=target.id)}
    for link in DocumentCustomer.query.filter_by(customer_id=source.id).all():
        existing = target_links.get(link.document_id)
        if existing is None:
            link.customer_id = target.id
        else:
            existing.row_data = [*(existing.row_data or []), *(link.row_data or [])]
            existing.field_confidence = [*(existing.field_confidence or []), *(link.field_confidence or [])]
            db.session.delete(link)
    db.session.flush()

    for model in (Document, CustomerTimelineEvent, Task, Recommendation, ListComparisonEntry):
        db.session.execute(
            update(model)
            .where(model.customer_id == source.id, model.tenant_id == tenant_id)
            .values(customer_id=target.id)
            .execution_options(synchronize_session=False)
        )

    log_timeline_event(
        target,
        TimelineEventType.CUSTOMER_MERGED,
        "Kundendatensatz zusammengeführt",
        extra_data={"merged_customer_id": source.id, "filled_fields": filled, "differing_values": differing},
    )
    source_id = source.id
    # Erst schreiben, dann neu laden: die Beziehungen von `source` sind danach leer.
    db.session.flush()
    db.session.expire_all()
    db.session.delete(db.session.get(Customer, source_id))
    # Der Audit-Eintrag committet - damit wird das Zusammenfuehren als Ganzes gespeichert.
    log_audit_event(
        AuditEventType.CUSTOMER_MERGED,
        user=actor,
        details={
            "customer_id": target_id,
            "merged_customer_id": source_id,
            "filled_fields": filled,
            "differing_fields": sorted(differing),
        },
    )
    return {"filled": filled, "differing": differing}
