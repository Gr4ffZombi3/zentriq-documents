from datetime import date

from app.models import Customer, Task
from app.models.enums import Priority, TaskStatus, TaskType


def test_cockpit_requires_login(client):
    resp = client.get("/cockpit")
    assert resp.status_code == 302
    assert "/auth/login" in resp.headers["Location"]


def test_cockpit_renders_with_no_data(auth_client, db):
    resp = auth_client.get("/cockpit")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "Tagescockpit" in body
    assert "Keine offenen Aufgaben." in body


def test_cockpit_shows_own_top_priority_task_and_call_today(auth_client, db, tenant, user):
    customer = Customer(tenant_id=tenant.id, name="Cockpit Kunde", assigned_user_id=user.id)
    db.session.add(customer)
    db.session.commit()

    task = Task(
        tenant_id=tenant.id,
        customer=customer,
        assigned_user_id=user.id,
        type=TaskType.CALL_TODAY,
        title="📞 Heute anrufen",
        priority=Priority.HIGH,
        status=TaskStatus.OPEN,
        due_date=date.today(),
    )
    db.session.add(task)
    db.session.commit()

    resp = auth_client.get("/cockpit")
    body = resp.get_data(as_text=True)
    assert "📞 Heute anrufen" in body
    assert "Cockpit Kunde" in body
