from app.models import RecommendationFeedback, Task, Tenant, User
from app.models.enums import FeedbackRating, Priority, TaskStatus, TaskType
from app.services.feedback import get_accuracy_by_type, record_feedback
from app.tenancy import set_current_tenant_id


def make_task(db, tenant_id, task_type=TaskType.CALL_TODAY, title="Heute anrufen"):
    task = Task(
        tenant_id=tenant_id, type=task_type, title=title, priority=Priority.HIGH, status=TaskStatus.OPEN
    )
    db.session.add(task)
    db.session.commit()
    return task


def test_record_feedback_persists_rating(app, db, tenant, user):
    task = make_task(db, tenant.id)
    feedback = record_feedback(task, user.id, FeedbackRating.UP)

    assert feedback.id is not None
    assert feedback.task_id == task.id
    assert feedback.rated_by_user_id == user.id
    assert feedback.rating == FeedbackRating.UP
    assert RecommendationFeedback.query.count() == 1


def test_get_accuracy_by_type_computes_rate(app, db, tenant, user):
    call_task = make_task(db, tenant.id, TaskType.CALL_TODAY)
    followup_task = make_task(db, tenant.id, TaskType.FOLLOW_UP_OFFER)

    record_feedback(call_task, user.id, FeedbackRating.UP)
    record_feedback(call_task, user.id, FeedbackRating.UP)
    record_feedback(followup_task, user.id, FeedbackRating.DOWN)

    stats = get_accuracy_by_type()

    assert stats["call_today"] == {"up": 2, "down": 0, "accuracy_rate": 1.0}
    assert stats["follow_up_offer"] == {"up": 0, "down": 1, "accuracy_rate": 0.0}


def test_get_accuracy_by_type_is_tenant_scoped(app, db, tenant, user):
    task_a = make_task(db, tenant.id)
    record_feedback(task_a, user.id, FeedbackRating.UP)

    tenant_b = Tenant(name="Tenant B", slug="tenant-b-feedback")
    db.session.add(tenant_b)
    db.session.commit()
    set_current_tenant_id(tenant_b.id)

    user_b = User(tenant_id=tenant_b.id, email="userb@example.com")
    user_b.set_password("passwort123")
    db.session.add(user_b)
    db.session.commit()
    task_b = make_task(db, tenant_b.id)
    record_feedback(task_b, user_b.id, FeedbackRating.DOWN)

    stats_b = get_accuracy_by_type()
    assert stats_b["call_today"] == {"up": 0, "down": 1, "accuracy_rate": 0.0}

    set_current_tenant_id(tenant.id)
    stats_a = get_accuracy_by_type()
    assert stats_a["call_today"] == {"up": 1, "down": 0, "accuracy_rate": 1.0}


def test_feedback_route_requires_login(client, db, tenant):
    task = make_task(db, tenant.id)
    resp = client.post(f"/tasks/{task.id}/feedback", json={"rating": "up"})
    assert resp.status_code == 302


def test_feedback_route_records_rating(auth_client, db, tenant, user):
    task = make_task(db, tenant.id)
    resp = auth_client.post(f"/tasks/{task.id}/feedback", json={"rating": "up"})

    assert resp.status_code == 201
    feedback = RecommendationFeedback.query.filter_by(task_id=task.id).one()
    assert feedback.rating == FeedbackRating.UP
    assert feedback.rated_by_user_id == user.id


def test_feedback_route_rejects_invalid_rating(auth_client, db, tenant):
    task = make_task(db, tenant.id)
    resp = auth_client.post(f"/tasks/{task.id}/feedback", json={"rating": "sideways"})
    assert resp.status_code == 400


def test_feedback_route_404s_for_other_tenants_task(auth_client, db, tenant):
    tenant_b = Tenant(name="Tenant B", slug="tenant-b-feedback-404")
    db.session.add(tenant_b)
    db.session.commit()
    set_current_tenant_id(tenant_b.id)
    other_task = make_task(db, tenant_b.id)
    set_current_tenant_id(tenant.id)

    resp = auth_client.post(f"/tasks/{other_task.id}/feedback", json={"rating": "up"})
    assert resp.status_code == 404
