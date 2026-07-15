from datetime import date

from app.models import Task
from app.models.enums import Priority, TaskStatus, TaskType


def make_task(db, tenant_id, title="📞 Heute anrufen", status=TaskStatus.OPEN, priority=Priority.HIGH):
    task = Task(
        tenant_id=tenant_id,
        type=TaskType.CALL_TODAY,
        title=title,
        priority=priority,
        status=status,
        due_date=date.today(),
    )
    db.session.add(task)
    db.session.commit()
    return task


def test_tasks_list_requires_login(client):
    resp = client.get("/tasks")
    assert resp.status_code == 302
    assert "/auth/login" in resp.headers["Location"]


def test_tasks_list_shows_open_tasks_by_default(auth_client, db, tenant):
    task = make_task(db, tenant.id)
    resp = auth_client.get("/tasks")
    assert resp.status_code == 200
    assert task.title in resp.get_data(as_text=True)


def test_tasks_list_filters_by_status(auth_client, db, tenant):
    open_task = make_task(db, tenant.id, title="Offene Aufgabe", status=TaskStatus.OPEN)
    done_task = make_task(db, tenant.id, title="Erledigte Aufgabe", status=TaskStatus.DONE)

    open_resp = auth_client.get("/tasks?status=open")
    body = open_resp.get_data(as_text=True)
    assert open_task.title in body
    assert done_task.title not in body

    done_resp = auth_client.get("/tasks?status=done")
    body = done_resp.get_data(as_text=True)
    assert done_task.title in body
    assert open_task.title not in body


def test_update_task_status_marks_done_and_sets_resolved_at(auth_client, db, tenant):
    task = make_task(db, tenant.id)
    resp = auth_client.post(f"/tasks/{task.id}/status", data={"status": "done"})
    assert resp.status_code == 302

    db.session.refresh(task)
    assert task.status == TaskStatus.DONE
    assert task.resolved_at is not None


def test_update_task_status_via_htmx_returns_partial(auth_client, db, tenant):
    task = make_task(db, tenant.id)
    resp = auth_client.post(
        f"/tasks/{task.id}/status", data={"status": "dismissed"}, headers={"HX-Request": "true"}
    )
    assert resp.status_code == 200
    assert f"task-row-{task.id}" in resp.get_data(as_text=True)

    db.session.refresh(task)
    assert task.status == TaskStatus.DISMISSED
