from flask import has_request_context, request

from app.extensions import db
from app.models.audit_log import AuditEventType, AuditLog
from app.models.user import User


def log_audit_event(
    event_type: AuditEventType,
    *,
    tenant_id: int | None = None,
    user: User | None = None,
    actor_email: str | None = None,
    details: dict | None = None,
) -> AuditLog:
    """Append-only: es gibt bewusst keine Update-/Delete-Funktion fuer Audit-Eintraege."""
    entry = AuditLog(
        tenant_id=tenant_id if tenant_id is not None else (user.tenant_id if user else None),
        actor_user_id=user.id if user else None,
        actor_email_snapshot=user.email if user else actor_email,
        event_type=event_type,
        details=details,
        ip_address=request.remote_addr if has_request_context() else None,
        user_agent=request.headers.get("User-Agent") if has_request_context() else None,
    )
    db.session.add(entry)
    db.session.commit()
    return entry
