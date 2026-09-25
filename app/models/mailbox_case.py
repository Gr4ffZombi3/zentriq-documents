from datetime import datetime, timezone

from app.extensions import db
from app.models.enums import CallbackAttemptStatus, MailboxStatus
from app.tenancy import TenantScopedMixin


def utcnow():
    return datetime.now(timezone.utc)


class MailboxCase(TenantScopedMixin, db.Model):
    __tablename__ = "mailbox_cases"
    __table_args__ = (
        db.UniqueConstraint("tenant_id", "source_key", name="uq_mailbox_cases_tenant_source_key"),
    )

    id = db.Column(db.Integer, primary_key=True)
    source_key = db.Column(db.String(128), nullable=False)
    source_message_id = db.Column(db.String(512), nullable=True)
    source_uid = db.Column(db.String(128), nullable=True)
    source_mailbox = db.Column(db.String(255), nullable=True)
    source_sender = db.Column(db.String(512), nullable=True)
    source_subject = db.Column(db.String(998), nullable=True)
    received_at = db.Column(db.DateTime, nullable=True, index=True)

    audio_filename = db.Column(db.String(255), nullable=True)
    audio_content_type = db.Column(db.String(128), nullable=True)
    audio_sha256 = db.Column(db.String(64), nullable=True, index=True)
    audio_size_bytes = db.Column(db.Integer, nullable=True)

    transcript = db.Column(db.Text, nullable=True)
    caller_phone = db.Column(db.String(32), nullable=True)
    callback_phone = db.Column(db.String(32), nullable=True)
    phone_source = db.Column(db.String(32), nullable=True)
    phone_confidence = db.Column(db.Float, nullable=True)
    concern = db.Column(db.String(64), nullable=True)
    damage_type = db.Column(db.String(128), nullable=True)
    damage_confidence = db.Column(db.Float, nullable=True)
    classification_reason = db.Column(db.Text, nullable=True)

    status = db.Column(db.Enum(MailboxStatus), nullable=False, default=MailboxStatus.NEW, index=True)
    review_reason = db.Column(db.Text, nullable=True)
    dry_run = db.Column(db.Boolean, nullable=False, default=True)
    processing_started_at = db.Column(db.DateTime, nullable=True)
    processed_at = db.Column(db.DateTime, nullable=True)
    submitted_at = db.Column(db.DateTime, nullable=True)
    next_attempt_at = db.Column(db.DateTime, nullable=True)
    last_error = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow, index=True)
    updated_at = db.Column(db.DateTime, nullable=False, default=utcnow, onupdate=utcnow)

    attempts = db.relationship(
        "MailboxCallbackAttempt",
        back_populates="mailbox_case",
        cascade="all, delete-orphan",
        order_by="MailboxCallbackAttempt.started_at.desc()",
    )
    events = db.relationship(
        "MailboxCaseEvent",
        back_populates="mailbox_case",
        cascade="all, delete-orphan",
        order_by="MailboxCaseEvent.created_at.desc()",
    )


class MailboxCallbackAttempt(TenantScopedMixin, db.Model):
    __tablename__ = "mailbox_callback_attempts"

    id = db.Column(db.Integer, primary_key=True)
    mailbox_case_id = db.Column(
        db.Integer,
        db.ForeignKey("mailbox_cases.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    status = db.Column(db.Enum(CallbackAttemptStatus), nullable=False, index=True)
    dry_run = db.Column(db.Boolean, nullable=False, default=True)
    form_url = db.Column(db.String(1024), nullable=False)
    request_data = db.Column(db.JSON, nullable=False)
    result_data = db.Column(db.JSON, nullable=True)
    error_message = db.Column(db.Text, nullable=True)
    started_at = db.Column(db.DateTime, nullable=False, default=utcnow)
    completed_at = db.Column(db.DateTime, nullable=True)

    mailbox_case = db.relationship("MailboxCase", back_populates="attempts")


class MailboxCaseEvent(TenantScopedMixin, db.Model):
    __tablename__ = "mailbox_case_events"

    id = db.Column(db.Integer, primary_key=True)
    mailbox_case_id = db.Column(
        db.Integer,
        db.ForeignKey("mailbox_cases.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    event_type = db.Column(db.String(64), nullable=False, index=True)
    actor_user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    details = db.Column(db.JSON, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow, index=True)

    mailbox_case = db.relationship("MailboxCase", back_populates="events")
    actor_user = db.relationship("User")


class MailboxSyncCursor(TenantScopedMixin, db.Model):
    __tablename__ = "mailbox_sync_cursors"
    __table_args__ = (
        db.UniqueConstraint("tenant_id", "mailbox_name", name="uq_mailbox_sync_cursor_tenant_name"),
    )

    id = db.Column(db.Integer, primary_key=True)
    mailbox_name = db.Column(db.String(255), nullable=False)
    last_uid = db.Column(db.String(128), nullable=True)
    last_checked_at = db.Column(db.DateTime, nullable=True)
    last_error = db.Column(db.Text, nullable=True)
    updated_at = db.Column(db.DateTime, nullable=False, default=utcnow, onupdate=utcnow)
