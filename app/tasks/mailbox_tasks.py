from datetime import datetime, timedelta, timezone

from celery import shared_task
from flask import current_app

from app.extensions import db
from app.models import (
    CallbackAttemptStatus,
    MailboxCallbackAttempt,
    MailboxCase,
    MailboxStatus,
    MailboxSyncCursor,
    Tenant,
)
from app.services.mailbox.huk import (
    build_huk_form_data,
    is_huk_service_open,
    run_huk_automation,
    seconds_until_huk_service_open,
)
from app.services.mailbox.imap import ImapMailboxReader
from app.services.mailbox.pipeline import add_case_event, process_raw_message
from app.tenancy import bypass_tenant_scope, use_tenant_id


@shared_task(name="app.tasks.mailbox_tasks.poll_placetel_mailbox")
def poll_placetel_mailbox():
    if not current_app.config.get("PLACETEL_MAILBOX_ENABLED"):
        return {"enabled": False, "checked": 0, "created": 0}
    tenant_id = current_app.config.get("PLACETEL_TENANT_ID")
    if tenant_id is None:
        raise RuntimeError("PLACETEL_TENANT_ID ist nicht konfiguriert.")
    with bypass_tenant_scope():
        tenant = db.session.get(Tenant, tenant_id)
    if tenant is None:
        raise RuntimeError("Der konfigurierte PLACETEL_TENANT_ID existiert nicht.")

    with use_tenant_id(tenant_id):
        folder = current_app.config["PLACETEL_IMAP_FOLDER"]
        cursor = MailboxSyncCursor.query.filter_by(mailbox_name=folder).first()
        if cursor is None:
            cursor = MailboxSyncCursor(tenant_id=tenant_id, mailbox_name=folder)
            db.session.add(cursor)
            db.session.commit()

        try:
            messages = ImapMailboxReader(current_app.config).fetch_since(cursor.last_uid)
            created = 0
            matched = 0
            for message in messages:
                result = process_raw_message(
                    tenant_id,
                    message.raw_message,
                    source_uid=message.uid,
                    source_mailbox=folder,
                )
                cursor.last_uid = message.uid
                cursor.last_checked_at = datetime.now(timezone.utc)
                cursor.last_error = None
                db.session.commit()
                matched += int(result.matched)
                created += int(result.created)
                if result.created and result.mailbox_case and result.mailbox_case.status == MailboxStatus.NEW:
                    if _live_submission_enabled():
                        submit_mailbox_case.delay(result.mailbox_case.id)
            if not messages:
                cursor.last_checked_at = datetime.now(timezone.utc)
                cursor.last_error = None
                db.session.commit()
            return {"enabled": True, "checked": len(messages), "matched": matched, "created": created}
        except Exception as exc:
            db.session.rollback()
            cursor = MailboxSyncCursor.query.filter_by(mailbox_name=folder).first()
            if cursor is not None:
                cursor.last_checked_at = datetime.now(timezone.utc)
                cursor.last_error = str(exc)
                db.session.commit()
            current_app.logger.exception("mailbox.poll.failed tenant_id=%s", tenant_id)
            raise


@shared_task(bind=True, name="app.tasks.mailbox_tasks.submit_mailbox_case")
def submit_mailbox_case(self, mailbox_case_id: int, force: bool = False):
    with bypass_tenant_scope():
        unscoped_case = db.session.get(MailboxCase, mailbox_case_id)
        tenant_id = unscoped_case.tenant_id if unscoped_case is not None else None
    if tenant_id is None:
        return {"status": "missing"}

    with use_tenant_id(tenant_id):
        mailbox_case = MailboxCase.query.filter_by(id=mailbox_case_id).with_for_update().first()
        if mailbox_case is None:
            return {"status": "missing"}
        if mailbox_case.status == MailboxStatus.CALLBACK_REQUESTED:
            return {"status": "already_submitted"}
        if mailbox_case.processing_started_at and not force:
            return {"status": "already_running"}
        if not _case_is_ready(mailbox_case):
            mailbox_case.status = MailboxStatus.REVIEW
            mailbox_case.review_reason = "Rufnummer und Schadenart müssen vor der Ausführung eindeutig sein."
            add_case_event(mailbox_case, "submission_blocked", {"reason": mailbox_case.review_reason})
            db.session.commit()
            return {"status": "review"}

        request_data = build_huk_form_data(mailbox_case.callback_phone, mailbox_case.damage_type)
        if not _live_submission_enabled():
            attempt = MailboxCallbackAttempt(
                tenant_id=tenant_id,
                mailbox_case=mailbox_case,
                status=CallbackAttemptStatus.PREPARED,
                dry_run=True,
                form_url=current_app.config["HUK_FORM_URL"],
                request_data=request_data,
                result_data={"prepared": True, "submitted": False, "reason": "dry_run"},
                completed_at=datetime.now(timezone.utc),
            )
            db.session.add(attempt)
            mailbox_case.status = MailboxStatus.NEW
            mailbox_case.dry_run = True
            mailbox_case.last_error = None
            add_case_event(mailbox_case, "dry_run_prepared", {"damage_type": mailbox_case.damage_type})
            db.session.commit()
            return {"status": "prepared", "submitted": False}

        if not is_huk_service_open():
            wait_seconds = seconds_until_huk_service_open()
            mailbox_case.next_attempt_at = datetime.now(timezone.utc) + timedelta(seconds=wait_seconds)
            mailbox_case.last_error = None
            add_case_event(
                mailbox_case,
                "scheduled_for_service_window",
                {"next_attempt_at": mailbox_case.next_attempt_at.isoformat()},
            )
            db.session.commit()
            self.apply_async(args=[mailbox_case_id], countdown=wait_seconds)
            return {"status": "scheduled", "countdown": wait_seconds}

        mailbox_case.processing_started_at = datetime.now(timezone.utc)
        mailbox_case.next_attempt_at = None
        attempt = MailboxCallbackAttempt(
            tenant_id=tenant_id,
            mailbox_case=mailbox_case,
            status=CallbackAttemptStatus.PREPARED,
            dry_run=False,
            form_url=current_app.config["HUK_FORM_URL"],
            request_data=request_data,
        )
        db.session.add(attempt)
        db.session.commit()

        try:
            result = run_huk_automation(mailbox_case.callback_phone, mailbox_case.damage_type, submit=True)
            attempt.status = CallbackAttemptStatus.SUBMITTED
            attempt.result_data = result
            attempt.completed_at = datetime.now(timezone.utc)
            mailbox_case.status = MailboxStatus.CALLBACK_REQUESTED
            mailbox_case.submitted_at = attempt.completed_at
            mailbox_case.dry_run = False
            mailbox_case.last_error = None
            add_case_event(mailbox_case, "callback_requested", {"damage_type": mailbox_case.damage_type})
            outcome = {"status": "submitted"}
        except Exception as exc:
            attempt.status = CallbackAttemptStatus.FAILED
            attempt.error_message = str(exc)
            attempt.completed_at = datetime.now(timezone.utc)
            mailbox_case.status = MailboxStatus.FAILED
            mailbox_case.last_error = str(exc)
            add_case_event(mailbox_case, "submission_failed", {"error": str(exc)})
            current_app.logger.exception(
                "mailbox.huk_submission.failed tenant_id=%s mailbox_case_id=%s",
                tenant_id,
                mailbox_case.id,
            )
            outcome = {"status": "failed"}
        finally:
            mailbox_case.processing_started_at = None
            db.session.commit()
        return outcome


def _case_is_ready(mailbox_case: MailboxCase) -> bool:
    return bool(
        mailbox_case.callback_phone
        and mailbox_case.concern == "Schadenanliegen"
        and mailbox_case.damage_type
        and mailbox_case.damage_type != "Sonstiges"
        and not mailbox_case.review_reason
    )


def _live_submission_enabled() -> bool:
    return bool(
        current_app.config.get("HUK_AUTOMATION_ENABLED")
        and not current_app.config.get("MAILBOX_DRY_RUN")
    )
