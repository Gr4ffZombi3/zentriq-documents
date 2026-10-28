"""Pruefung und Reparatur von Kunden und Notizen aus dem ZentriqAI-Eingabefeld
(CLI: `flask kunden-notizen-pruefen`, ohne --apply nur Bericht).

Wiederholt ausfuehrbar: repariert wird nur, was eindeutig rekonstruierbar ist, und nur in leeren
Feldern - vorhandene Stammdaten werden nie ueberschrieben, Kunden nie zusammengefuehrt.
- Vergleichsschluessel (name_key, phone_key, customer_number_key), die nicht zu den Stammdaten
  passen, werden neu berechnet (Suche/Zuordnung laufen ausschliesslich ueber diese Schluessel).
- Aus einer Notiz angelegte Kunden ohne Telefonnummer bzw. VN-Nummer: steht in der Notiz der
  Neuanlage genau eine Telefonnummer bzw. VN-Nummer, die keinem anderen Kunden des Bueros gehoert,
  wird sie in das Stammdatenfeld uebernommen (Herkunft "Notiz"). Der Notiztext bleibt unveraendert.
Nur gemeldet (keine Aenderung): Notizen/Vorgaenge mit Buero-Widerspruch zum Kunden, mehrdeutige
Nummern, moegliche Dubletten (gleiche Kennung bzw. gleicher Name) und doppelte Notizen."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import timedelta

from sqlalchemy import select

from app.extensions import db
from app.models import Customer, CustomerContact, CustomerNote, Tenant
from app.services import customer_sources
from app.services.customer_notes import _display_vn, customers_by_vn, parse
from app.tenancy import bypass_tenant_scope, use_tenant_id
from app.utils.customer_keys import customer_keys, format_phone

# Notiz der Neuanlage: vom anlegenden Benutzer, zeitgleich mit dem Kunden gespeichert.
CREATION_NOTE_WINDOW = timedelta(minutes=2)


@dataclass
class RepairReport:
    fixes: list[str] = field(default_factory=list)
    findings: list[str] = field(default_factory=list)
    checked_customers: int = 0
    checked_notes: int = 0


def _label(customer: Customer) -> str:
    return f"Kunde {customer.id} „{customer.name}“ (Büro {customer.tenant_id})"


def check_and_repair(apply: bool = False) -> RepairReport:
    report = RepairReport()
    with bypass_tenant_scope():
        tenant_ids = list(db.session.execute(select(Tenant.id).order_by(Tenant.id)).scalars())
        _check_tenant_consistency(report)
    for tenant_id in tenant_ids:
        with use_tenant_id(tenant_id):
            _check_tenant(report, apply)
    if apply:
        db.session.commit()
    else:
        db.session.rollback()
    return report


def _check_tenant_consistency(report: RepairReport) -> None:
    """Notizen/Vorgaenge, deren Buero nicht zum Buero des Kunden passt (nie automatisch geaendert)."""
    rows = db.session.execute(
        select(CustomerNote.id, CustomerNote.tenant_id, Customer.tenant_id)
        .join(Customer, Customer.id == CustomerNote.customer_id)
        .where(CustomerNote.tenant_id != Customer.tenant_id)
    ).all()
    for note_id, note_tenant, customer_tenant in rows:
        report.findings.append(f"Notiz {note_id}: Büro {note_tenant}, Kunde aber Büro {customer_tenant} – bitte prüfen.")
    rows = db.session.execute(
        select(CustomerContact.id, CustomerContact.tenant_id, Customer.tenant_id)
        .join(Customer, Customer.id == CustomerContact.customer_id)
        .where(CustomerContact.tenant_id != Customer.tenant_id)
    ).all()
    for contact_id, contact_tenant, customer_tenant in rows:
        report.findings.append(f"Vorgang {contact_id}: Büro {contact_tenant}, Kunde aber Büro {customer_tenant} – bitte prüfen.")


def _check_tenant(report: RepairReport, apply: bool) -> None:
    customers = Customer.query.order_by(Customer.id).all()
    report.checked_customers += len(customers)
    for customer in customers:
        expected = customer_keys(customer.name, customer.phone, customer.customer_number)
        wrong = sorted(column for column, value in expected.items() if getattr(customer, column) != value)
        if wrong:
            report.fixes.append(f"{_label(customer)}: Suchschlüssel {', '.join(wrong)} neu berechnet.")
            if apply:
                for column in wrong:
                    setattr(customer, column, expected[column])
    if apply:
        db.session.flush()

    for customer in customers:
        if customer.source == customer_sources.NOTIZ:
            _repair_from_creation_note(report, customer, apply)

    _report_duplicates(report, customers)
    _report_duplicate_notes(report)


def _creation_note(customer: Customer) -> CustomerNote | None:
    if customer.created_at is None:
        return None
    query = CustomerNote.query.filter(
        CustomerNote.customer_id == customer.id,
        CustomerNote.event.is_(None),
        CustomerNote.created_at <= customer.created_at + CREATION_NOTE_WINDOW,
    )
    if customer.assigned_user_id is not None:
        query = query.filter(CustomerNote.created_by_user_id == customer.assigned_user_id)
    return query.order_by(CustomerNote.created_at, CustomerNote.id).first()


def _repair_from_creation_note(report: RepairReport, customer: Customer, apply: bool) -> None:
    note = _creation_note(customer)
    if note is None:
        return
    report.checked_notes += 1
    parsed = parse(note.text)

    if not customer.phone and parsed.phones:
        if len(parsed.phones) > 1:
            report.findings.append(f"{_label(customer)}: mehrere Telefonnummern in Notiz {note.id} – nicht übernommen.")
        else:
            phone = parsed.phones[0]
            owners = [c for c in Customer.query.filter(Customer.phone_key == phone, Customer.id != customer.id).all()]
            if owners:
                others = ", ".join(str(c.id) for c in owners)
                report.findings.append(
                    f"{_label(customer)}: Telefonnummer aus Notiz {note.id} gehört bereits Kunde {others} – nicht übernommen."
                )
            else:
                report.fixes.append(f"{_label(customer)}: Telefonnummer {format_phone(phone)} aus Notiz {note.id} hinterlegt.")
                if apply:
                    customer_sources.apply_customer_values(customer, {"phone": format_phone(phone)}, customer_sources.NOTIZ)

    if not customer.customer_number and parsed.vn_keys:
        if len(parsed.vn_keys) > 1:
            report.findings.append(f"{_label(customer)}: mehrere VN-Nummern in Notiz {note.id} – nicht übernommen.")
            return
        owners = [cid for cid in customers_by_vn(parsed.vn_keys[0]) if cid != customer.id]
        vn = _display_vn(parsed, note.text)
        if owners:
            report.findings.append(
                f"{_label(customer)}: VN {vn} aus Notiz {note.id} gehört bereits Kunde {', '.join(map(str, owners))} – nicht übernommen."
            )
        else:
            report.fixes.append(f"{_label(customer)}: VN-Nummer {vn} aus Notiz {note.id} hinterlegt.")
            if apply:
                customer_sources.apply_customer_values(customer, {"customer_number": vn[:50]}, customer_sources.NOTIZ)
    if apply:
        db.session.flush()


def _report_duplicates(report: RepairReport, customers: list[Customer]) -> None:
    """Moegliche Dubletten - werden nie automatisch zusammengefuehrt (Kunden > Dubletten)."""
    by_number: dict[str, list[Customer]] = defaultdict(list)
    for customer in customers:
        if customer.customer_number_key:
            by_number[customer.customer_number_key].append(customer)
            for owner in customers_by_vn(customer.customer_number_key):
                if owner != customer.id and customer.source == customer_sources.NOTIZ:
                    report.findings.append(
                        f"{_label(customer)}: VN {customer.customer_number} ist auch Kunde {owner} (Leipziger Liste) zugeordnet – mögliche Dublette."
                    )
    for key, group in by_number.items():
        if len(group) > 1:
            report.findings.append(f"Kundennummer {key} bei mehreren Kunden: {', '.join(str(c.id) for c in group)} – mögliche Dublette.")
    by_name: dict[str, list[Customer]] = defaultdict(list)
    for customer in customers:
        if customer.name_key:
            by_name[customer.name_key].append(customer)
    for group in by_name.values():
        if len(group) > 1 and any(c.source == customer_sources.NOTIZ for c in group):
            details = "; ".join(f"{c.id}: {c.customer_number or 'ohne VN'}" for c in group)
            report.findings.append(f"Gleicher Name „{group[0].name}“ mehrfach ({details}) – mögliche Dublette, bitte prüfen.")


def _report_duplicate_notes(report: RepairReport) -> None:
    seen: dict[tuple, list[CustomerNote]] = defaultdict(list)
    for note in CustomerNote.query.filter(CustomerNote.event.is_(None)).order_by(CustomerNote.id).all():
        seen[(note.customer_id, note.created_by_user_id, note.text)].append(note)
    for (customer_id, _user_id, text), notes in seen.items():
        if len(notes) > 1:
            ids = ", ".join(str(n.id) for n in notes)
            report.findings.append(f"Kunde {customer_id}: Notiz „{text[:60]}“ mehrfach gespeichert (Notizen {ids}) – nur zur Kenntnis.")
