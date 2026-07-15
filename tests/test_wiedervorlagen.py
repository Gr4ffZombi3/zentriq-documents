from datetime import datetime, timedelta, timezone

from app.models import Customer, Document, DocumentCustomer, Task
from app.models.enums import DocStatus, Priority, TaskStatus, WiedervorlageReason
from app.services.wiedervorlagen import sweep_offer_wiedervorlagen


def make_offer_document_customer(db, tenant_id, customer_name, days_old, is_neugeschaeft=False):
    customer = Customer.query.filter_by(name=customer_name).first()
    if customer is None:
        customer = Customer(name=customer_name, tenant_id=tenant_id)
        db.session.add(customer)
        db.session.flush()

    uploaded_at = datetime.now(timezone.utc) - timedelta(days=days_old)
    document = Document(
        filename=f"{customer_name}.pdf",
        original_filename=f"{customer_name}.pdf",
        file_path=f"/tmp/{customer_name}.pdf",
        tenant_id=tenant_id,
        status=DocStatus.DONE,
        uploaded_at=uploaded_at,
    )
    db.session.add(document)
    db.session.flush()

    doc_customer = DocumentCustomer(
        tenant_id=tenant_id,
        document_id=document.id,
        customer_id=customer.id,
        row_data=[{"is_angebot": True, "is_neugeschaeft": is_neugeschaeft, "priority": "medium"}],
    )
    db.session.add(doc_customer)
    db.session.commit()
    return customer, document


def test_sweep_ignores_offers_younger_than_7_days(app, db, tenant):
    make_offer_document_customer(db, tenant.id, "Junges Angebot", days_old=2)

    created = sweep_offer_wiedervorlagen()

    assert created == []
    assert Task.query.count() == 0


def test_sweep_creates_medium_priority_task_for_7_day_offer(app, db, tenant):
    make_offer_document_customer(db, tenant.id, "Sieben Tage Kunde", days_old=8)

    created = sweep_offer_wiedervorlagen()

    assert len(created) == 1
    task = created[0]
    assert task.wiedervorlage_reason == WiedervorlageReason.OFFER_OLDER_THAN_7_DAYS
    assert task.priority == Priority.MEDIUM
    assert task.status == TaskStatus.OPEN


def test_sweep_creates_high_priority_task_for_14_day_offer(app, db, tenant):
    make_offer_document_customer(db, tenant.id, "Vierzehn Tage Kunde", days_old=15)

    created = sweep_offer_wiedervorlagen()

    assert len(created) == 1
    task = created[0]
    assert task.wiedervorlage_reason == WiedervorlageReason.OFFER_OLDER_THAN_14_DAYS
    assert task.priority == Priority.HIGH


def test_sweep_escalates_existing_7_day_task_to_14_days(app, db, tenant):
    customer, _ = make_offer_document_customer(db, tenant.id, "Eskalations Kunde", days_old=8)
    first_run = sweep_offer_wiedervorlagen()
    assert first_run[0].wiedervorlage_reason == WiedervorlageReason.OFFER_OLDER_THAN_7_DAYS
    task_id = first_run[0].id

    # Dokument nachtraeglich "aelter machen" (simuliert Zeitablauf).
    doc_customer = DocumentCustomer.query.filter_by(customer_id=customer.id).one()
    doc_customer.document.uploaded_at = datetime.now(timezone.utc) - timedelta(days=15)
    db.session.commit()

    second_run = sweep_offer_wiedervorlagen()

    assert second_run == []  # kein NEUER Task, bestehender wird eskaliert statt dupliziert
    assert Task.query.count() == 1
    escalated = db.session.get(Task, task_id)
    assert escalated.wiedervorlage_reason == WiedervorlageReason.OFFER_OLDER_THAN_14_DAYS
    assert escalated.priority == Priority.HIGH


def test_sweep_is_idempotent_across_repeated_runs(app, db, tenant):
    make_offer_document_customer(db, tenant.id, "Idempotenz Kunde", days_old=10)

    sweep_offer_wiedervorlagen()
    sweep_offer_wiedervorlagen()
    sweep_offer_wiedervorlagen()

    assert Task.query.count() == 1


def test_sweep_skips_customers_already_closed_as_neugeschaeft(app, db, tenant):
    make_offer_document_customer(db, tenant.id, "Bereits Abgeschlossen", days_old=10, is_neugeschaeft=True)

    created = sweep_offer_wiedervorlagen()

    assert created == []
    assert Task.query.count() == 0
