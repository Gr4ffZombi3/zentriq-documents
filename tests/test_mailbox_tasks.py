from app.extensions import db
from app.models import CallbackAttemptStatus, MailboxCallbackAttempt, MailboxCase
from app.models.enums import MailboxStatus
from app.tasks import mailbox_tasks
from app.tasks.mailbox_tasks import submit_mailbox_case


def make_ready_case(tenant_id, **overrides):
    defaults = dict(
        tenant_id=tenant_id,
        source_key="ready-case",
        callback_phone="+495211234567",
        concern="Schadenanliegen",
        damage_type="Kfz-Versicherung",
        status=MailboxStatus.NEW,
    )
    defaults.update(overrides)
    case = MailboxCase(**defaults)
    db.session.add(case)
    db.session.commit()
    return case


def test_submit_mailbox_case_dry_run_prepares_without_submitting(app, tenant):
    app.config["MAILBOX_DRY_RUN"] = True
    app.config["HUK_AUTOMATION_ENABLED"] = False
    with app.app_context():
        case = make_ready_case(tenant.id)
        result = submit_mailbox_case(case.id)

    assert result == {"status": "prepared", "submitted": False}
    case = db.session.get(MailboxCase, case.id)
    assert case.status == MailboxStatus.NEW
    attempts = MailboxCallbackAttempt.query.filter_by(mailbox_case_id=case.id).all()
    assert len(attempts) == 1
    assert attempts[0].status == CallbackAttemptStatus.PREPARED
    assert attempts[0].dry_run is True


def test_submit_mailbox_case_already_submitted_short_circuits(app, tenant):
    app.config["MAILBOX_DRY_RUN"] = True
    with app.app_context():
        case = make_ready_case(tenant.id, status=MailboxStatus.CALLBACK_REQUESTED)
        result = submit_mailbox_case(case.id)

    assert result == {"status": "already_submitted"}
    assert MailboxCallbackAttempt.query.filter_by(mailbox_case_id=case.id).count() == 0


def test_submit_mailbox_case_blocks_incomplete_data(app, tenant):
    app.config["MAILBOX_DRY_RUN"] = True
    with app.app_context():
        case = make_ready_case(tenant.id, damage_type=None)
        result = submit_mailbox_case(case.id)

    assert result == {"status": "review"}
    case = db.session.get(MailboxCase, case.id)
    assert case.status == MailboxStatus.REVIEW


def test_submit_mailbox_case_live_submission_succeeds(app, tenant, monkeypatch):
    app.config["MAILBOX_DRY_RUN"] = False
    app.config["HUK_AUTOMATION_ENABLED"] = True
    monkeypatch.setattr(mailbox_tasks, "is_huk_service_open", lambda: True)
    monkeypatch.setattr(
        mailbox_tasks,
        "run_huk_automation",
        lambda phone, damage_type, submit: {"prepared": True, "submitted": True, "confirmation": "Vielen Dank"},
    )

    with app.app_context():
        case = make_ready_case(tenant.id)
        result = submit_mailbox_case(case.id)

    assert result == {"status": "submitted"}
    case = db.session.get(MailboxCase, case.id)
    assert case.status == MailboxStatus.CALLBACK_REQUESTED
    assert case.submitted_at is not None
    attempt = MailboxCallbackAttempt.query.filter_by(mailbox_case_id=case.id).first()
    assert attempt.status == CallbackAttemptStatus.SUBMITTED
    assert attempt.dry_run is False


def test_submit_mailbox_case_live_submission_failure_marks_case_failed(app, tenant, monkeypatch):
    app.config["MAILBOX_DRY_RUN"] = False
    app.config["HUK_AUTOMATION_ENABLED"] = True
    monkeypatch.setattr(mailbox_tasks, "is_huk_service_open", lambda: True)

    def failing_automation(*args, **kwargs):
        raise RuntimeError("Captcha erkannt")

    monkeypatch.setattr(mailbox_tasks, "run_huk_automation", failing_automation)

    with app.app_context():
        case = make_ready_case(tenant.id)
        result = submit_mailbox_case(case.id)

    assert result == {"status": "failed"}
    case = db.session.get(MailboxCase, case.id)
    assert case.status == MailboxStatus.FAILED
    assert "Captcha" in case.last_error
    attempt = MailboxCallbackAttempt.query.filter_by(mailbox_case_id=case.id).first()
    assert attempt.status == CallbackAttemptStatus.FAILED


def test_submit_mailbox_case_outside_business_hours_reschedules_without_looping(app, tenant, monkeypatch):
    app.config["MAILBOX_DRY_RUN"] = False
    app.config["HUK_AUTOMATION_ENABLED"] = True
    monkeypatch.setattr(mailbox_tasks, "is_huk_service_open", lambda: False)
    monkeypatch.setattr(mailbox_tasks, "seconds_until_huk_service_open", lambda: 3600)

    rescheduled = []
    monkeypatch.setattr(
        submit_mailbox_case, "apply_async", lambda args, countdown: rescheduled.append((args, countdown))
    )

    with app.app_context():
        case = make_ready_case(tenant.id)
        result = submit_mailbox_case(case.id)

    assert result == {"status": "scheduled", "countdown": 3600}
    assert rescheduled == [([case.id], 3600)]
    case = db.session.get(MailboxCase, case.id)
    assert case.next_attempt_at is not None
    assert MailboxCallbackAttempt.query.filter_by(mailbox_case_id=case.id).count() == 0
