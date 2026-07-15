from datetime import date, datetime, timedelta, timezone

from app.models import Customer, Document, DocumentCustomer, Task, User
from app.models.enums import DocStatus, Priority, TaskStatus, TaskType
from app.services.bestand import get_bestand
from app.services.cockpit import get_daily_cockpit
from app.services.kpis import get_sales_kpis
from app.services.potential_score import compute_potential_score


def make_user(db, tenant_id, email):
    user = User(tenant_id=tenant_id, email=email)
    user.set_password("passwort123")
    db.session.add(user)
    db.session.commit()
    return user


def make_customer(db, tenant_id, name, assigned_user_id=None):
    customer = Customer(tenant_id=tenant_id, name=name, assigned_user_id=assigned_user_id)
    db.session.add(customer)
    db.session.commit()
    return customer


def make_document_with_row(
    db,
    tenant_id,
    customer,
    row_data,
    uploaded_by_user_id=None,
    uploaded_at=None,
    processed_at=None,
    cross_sell_opportunity=False,
):
    uploaded_at = uploaded_at or datetime.now(timezone.utc)
    document = Document(
        tenant_id=tenant_id,
        filename=f"{customer.name}.pdf",
        original_filename=f"{customer.name}.pdf",
        file_path=f"/tmp/{customer.name}.pdf",
        status=DocStatus.DONE,
        uploaded_at=uploaded_at,
        processed_at=processed_at or (uploaded_at + timedelta(minutes=5)),
        uploaded_by_user_id=uploaded_by_user_id,
        customer_id=customer.id,
        cross_sell_opportunity=cross_sell_opportunity,
    )
    db.session.add(document)
    db.session.flush()

    doc_customer = DocumentCustomer(
        tenant_id=tenant_id, document_id=document.id, customer_id=customer.id, row_data=[row_data]
    )
    db.session.add(doc_customer)
    db.session.commit()
    return document


def test_compute_potential_score_formula():
    base = compute_potential_score(priority=Priority.LOW, products=[], cross_sell_opportunity=False, has_multiple_products=False)
    assert base == 0

    high_full = compute_potential_score(
        priority=Priority.HIGH, products=["A", "B"], cross_sell_opportunity=True, has_multiple_products=True
    )
    # PRIORITY_ORDER[HIGH]=2 -> 20, + 2 Produkte*5=10, +15, +10 = 55
    assert high_full == 55


def test_kpis_are_scoped_per_user(app, db, tenant):
    broker_a = make_user(db, tenant.id, "a@example.com")
    broker_b = make_user(db, tenant.id, "b@example.com")

    customer_a_closed = make_customer(db, tenant.id, "Kunde A Closed", assigned_user_id=broker_a.id)
    make_document_with_row(
        db,
        tenant.id,
        customer_a_closed,
        {"is_angebot": True, "is_neugeschaeft": True, "products": ["Kfz-Haftpflicht"], "priority": "high"},
        uploaded_by_user_id=broker_a.id,
    )

    customer_a_open = make_customer(db, tenant.id, "Kunde A Open", assigned_user_id=broker_a.id)
    make_document_with_row(
        db,
        tenant.id,
        customer_a_open,
        {"is_angebot": True, "products": ["Hausrat"], "priority": "medium"},
        uploaded_by_user_id=broker_a.id,
    )

    customer_b = make_customer(db, tenant.id, "Kunde B", assigned_user_id=broker_b.id)
    make_document_with_row(
        db,
        tenant.id,
        customer_b,
        {"is_angebot": True, "products": ["Rechtsschutz"], "priority": "low"},
        uploaded_by_user_id=broker_b.id,
    )

    kpis_a = get_sales_kpis(user_id=broker_a.id)
    assert kpis_a["open_offers_count"] == 1  # nur "Kunde A Open" (der andere ist geschlossen)
    assert kpis_a["abschlussquote_percent"] == 50.0  # 1 von 2 Angeboten geschlossen
    assert "Kfz-Haftpflicht" in kpis_a["vertraege_pro_sparte"]
    assert "Rechtsschutz" not in kpis_a["vertraege_pro_sparte"]

    kpis_b = get_sales_kpis(user_id=broker_b.id)
    assert kpis_b["open_offers_count"] == 1
    assert "Rechtsschutz" in kpis_b["vertraege_pro_sparte"]
    assert "Kfz-Haftpflicht" not in kpis_b["vertraege_pro_sparte"]

    kpis_tenant_wide = get_sales_kpis(user_id=None)
    assert kpis_tenant_wide["open_offers_count"] == 2


def test_cockpit_only_shows_current_users_tasks(app, db, tenant):
    broker_a = make_user(db, tenant.id, "cockpit-a@example.com")
    broker_b = make_user(db, tenant.id, "cockpit-b@example.com")
    customer_a = make_customer(db, tenant.id, "Cockpit Kunde A", assigned_user_id=broker_a.id)
    customer_b = make_customer(db, tenant.id, "Cockpit Kunde B", assigned_user_id=broker_b.id)

    task_a = Task(
        tenant_id=tenant.id,
        customer=customer_a,
        assigned_user_id=broker_a.id,
        type=TaskType.CALL_TODAY,
        title="📞 Heute anrufen",
        priority=Priority.HIGH,
        status=TaskStatus.OPEN,
        due_date=date.today(),
    )
    task_b = Task(
        tenant_id=tenant.id,
        customer=customer_b,
        assigned_user_id=broker_b.id,
        type=TaskType.CALL_TODAY,
        title="📞 Heute anrufen",
        priority=Priority.HIGH,
        status=TaskStatus.OPEN,
        due_date=date.today(),
    )
    db.session.add_all([task_a, task_b])
    db.session.commit()

    cockpit_a = get_daily_cockpit(broker_a.id)
    assert cockpit_a["top_priority_task"].id == task_a.id
    assert [t.id for t in cockpit_a["call_today_tasks"]] == [task_a.id]

    cockpit_b = get_daily_cockpit(broker_b.id)
    assert cockpit_b["top_priority_task"].id == task_b.id


def test_cockpit_counts_overdue_and_new_documents(app, db, tenant):
    broker = make_user(db, tenant.id, "overdue@example.com")
    customer = make_customer(db, tenant.id, "Ueberfaellig Kunde", assigned_user_id=broker.id)

    overdue_task = Task(
        tenant_id=tenant.id,
        customer=customer,
        assigned_user_id=broker.id,
        type=TaskType.FOLLOW_UP_OFFER,
        title="📧 Angebot nachfassen",
        priority=Priority.MEDIUM,
        status=TaskStatus.OPEN,
        due_date=date.today() - timedelta(days=3),
    )
    db.session.add(overdue_task)
    db.session.commit()

    make_document_with_row(
        db,
        tenant.id,
        customer,
        {"products": []},
        uploaded_by_user_id=broker.id,
        uploaded_at=datetime.now(timezone.utc) - timedelta(hours=2),
    )

    cockpit = get_daily_cockpit(broker.id)
    assert cockpit["overdue_tasks_count"] == 1
    assert cockpit["new_documents_count"] == 1


def test_bestand_only_returns_own_customers_and_tasks(app, db, tenant):
    broker_a = make_user(db, tenant.id, "bestand-a@example.com")
    broker_b = make_user(db, tenant.id, "bestand-b@example.com")
    customer_a = make_customer(db, tenant.id, "Bestand Kunde A", assigned_user_id=broker_a.id)
    make_customer(db, tenant.id, "Bestand Kunde B", assigned_user_id=broker_b.id)

    task_a = Task(
        tenant_id=tenant.id,
        customer=customer_a,
        assigned_user_id=broker_a.id,
        type=TaskType.FOLLOW_UP_OFFER,
        wiedervorlage_reason=None,
        title="📧 Angebot nachfassen",
        priority=Priority.MEDIUM,
        status=TaskStatus.OPEN,
        due_date=date.today(),
    )
    db.session.add(task_a)
    db.session.commit()

    bestand_a = get_bestand(broker_a.id)
    assert [c.name for c in bestand_a["customers"]] == ["Bestand Kunde A"]
    assert [t.id for t in bestand_a["open_tasks"]] == [task_a.id]

    bestand_b = get_bestand(broker_b.id)
    assert [c.name for c in bestand_b["customers"]] == ["Bestand Kunde B"]
    assert bestand_b["open_tasks"] == []
