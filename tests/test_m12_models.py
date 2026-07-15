import pytest

from app.models import (
    AnalysisRun,
    AnalysisRunStatus,
    DocStatus,
    Document,
    FeedbackRating,
    Priority,
    Recommendation,
    RecommendationFeedback,
    RecommendationStatus,
    RecommendationType,
    Task,
    TaskStatus,
    TaskType,
    Tenant,
    User,
)
from app.tenancy import MissingTenantContextError, set_current_tenant_id


def make_document(db, tenant_id, filename="a.pdf"):
    document = Document(
        tenant_id=tenant_id,
        filename=filename,
        original_filename=filename,
        file_path=f"/tmp/{filename}",
        status=DocStatus.DONE,
    )
    db.session.add(document)
    db.session.commit()
    return document


def make_analysis_run(db, tenant_id, document_id, status=AnalysisRunStatus.SUCCEEDED):
    run = AnalysisRun(
        tenant_id=tenant_id,
        document_id=document_id,
        engine_version="m12.1",
        prompt_version="v1",
        status=status,
    )
    db.session.add(run)
    db.session.commit()
    return run


def make_task(db, tenant_id, title="Heute anrufen"):
    task = Task(
        tenant_id=tenant_id,
        type=TaskType.CALL_TODAY,
        title=title,
        priority=Priority.HIGH,
        status=TaskStatus.OPEN,
    )
    db.session.add(task)
    db.session.commit()
    return task


def make_user(db, tenant_id, email):
    user = User(tenant_id=tenant_id, email=email)
    user.set_password("passwort123")
    db.session.add(user)
    db.session.commit()
    return user


def make_feedback(db, tenant_id, task_id, rated_by_user_id, rating=FeedbackRating.UP):
    feedback = RecommendationFeedback(
        tenant_id=tenant_id, task_id=task_id, rated_by_user_id=rated_by_user_id, rating=rating
    )
    db.session.add(feedback)
    db.session.commit()
    return feedback


@pytest.mark.parametrize(
    "query_fn",
    [
        lambda: AnalysisRun.query.all(),
        lambda: RecommendationFeedback.query.all(),
    ],
)
def test_new_m12_tables_require_tenant_context(db, query_fn):
    set_current_tenant_id(None)
    with pytest.raises(MissingTenantContextError):
        query_fn()


def test_analysis_runs_are_isolated_between_tenants(app, db, tenant):
    tenant_b = Tenant(name="Tenant B", slug="tenant-b-m12-runs")
    db.session.add(tenant_b)
    db.session.commit()

    document_a = make_document(db, tenant.id, "a.pdf")
    run_a = make_analysis_run(db, tenant.id, document_a.id)

    set_current_tenant_id(tenant_b.id)
    document_b = make_document(db, tenant_b.id, "b.pdf")
    run_b = make_analysis_run(db, tenant_b.id, document_b.id)

    set_current_tenant_id(tenant.id)
    assert AnalysisRun.query.all() == [run_a]

    set_current_tenant_id(tenant_b.id)
    assert AnalysisRun.query.all() == [run_b]


def test_analysis_run_relates_to_its_document(app, db, tenant):
    document = make_document(db, tenant.id)
    run = make_analysis_run(db, tenant.id, document.id)

    assert document.analysis_runs == [run]
    assert run.document_id == document.id


def test_recommendation_feedback_is_isolated_between_tenants(app, db, tenant):
    tenant_b = Tenant(name="Tenant B", slug="tenant-b-m12-feedback")
    db.session.add(tenant_b)
    db.session.commit()

    user_a = make_user(db, tenant.id, "a@example.com")
    task_a = make_task(db, tenant.id)
    feedback_a = make_feedback(db, tenant.id, task_a.id, user_a.id)

    set_current_tenant_id(tenant_b.id)
    user_b = make_user(db, tenant_b.id, "b@example.com")
    task_b = make_task(db, tenant_b.id)
    feedback_b = make_feedback(db, tenant_b.id, task_b.id, user_b.id, rating=FeedbackRating.DOWN)

    set_current_tenant_id(tenant.id)
    assert RecommendationFeedback.query.all() == [feedback_a]

    set_current_tenant_id(tenant_b.id)
    assert RecommendationFeedback.query.all() == [feedback_b]


def test_task_feedback_relationship(app, db, tenant):
    user = make_user(db, tenant.id, "rater@example.com")
    task = make_task(db, tenant.id)
    feedback = make_feedback(db, tenant.id, task.id, user.id)

    assert task.feedback_entries == [feedback]
    assert feedback.task_id == task.id
    assert feedback.rated_by_user_id == user.id


def test_new_document_and_recommendation_columns_default_to_none(app, db, tenant):
    document = make_document(db, tenant.id)
    assert document.broker_number is None
    assert document.product_line is None
    assert document.premium is None
    assert document.tariff is None
    assert document.field_confidence is None

    recommendation = Recommendation(
        tenant_id=tenant.id,
        document_id=document.id,
        type=RecommendationType.CALL_TODAY,
        label="Heute anrufen",
        status=RecommendationStatus.OPEN,
    )
    db.session.add(recommendation)
    db.session.commit()
    assert recommendation.explanation is None
