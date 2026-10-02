import logging

from celery import shared_task

from app.extensions import db
from app.models import User
from app.services.account_state import account_login_block_reason
from app.services.mailer import send_email
from app.services.password_reset import (
    build_reset_email_body,
    build_reset_url,
    generate_reset_token,
    missing_reset_settings,
)
from app.services.system_errors import record_system_error
from app.tenancy import bypass_tenant_scope

logger = logging.getLogger(__name__)


@shared_task(name="app.tasks.auth_tasks.send_password_reset_email")
def send_password_reset_email(user_id: int):
    """Das Token wird erst hier erzeugt, damit es nie im Celery-Broker (Redis) landet.

    Empfaenger ist ausschliesslich die im Konto hinterlegte Login-E-Mail (aus der Datenbank
    geladen, nie aus dem Request). MAIL_FROM ist nur der Absender."""
    with bypass_tenant_scope():
        user = db.session.get(User, user_id)
        if user is None or account_login_block_reason(user) is not None:
            return {"sent": False, "reason": "user_unavailable"}
        if not user.two_factor_enabled:
            logger.warning("Passwort-Reset fuer User %s nicht versendet: keine 2FA eingerichtet.", user_id)
            return {"sent": False, "reason": "two_factor_missing"}
        recipient = user.email
        tenant_id = user.tenant_id
        reset_url = None
        if not missing_reset_settings():
            reset_url = build_reset_url(generate_reset_token(user))
    if reset_url is None:
        logger.error(
            "Passwort-Reset fuer User %s nicht versendet: Mailversand nicht konfiguriert (fehlend: %s).",
            user_id,
            ", ".join(missing_reset_settings()),
        )
        record_system_error("mail", "Reset-Mail nicht konfiguriert", location="Passwort-Reset", tenant_id=tenant_id)
        return {"sent": False, "reason": "not_configured"}

    try:
        send_email(recipient, "Zurücksetzen Ihres Passworts – Zentriq", build_reset_email_body(reset_url))
    except Exception as exc:
        # Keine Details der SMTP-Antwort/Zugangsdaten loggen - nur Fehlerklasse, SMTP-Statuscode
        # und Konto-ID.
        smtp_code = getattr(exc, "smtp_code", None)
        logger.error(
            "Passwort-Reset-Mail fuer User %s fehlgeschlagen (%s%s).",
            user_id,
            type(exc).__name__,
            f", SMTP {smtp_code}" if smtp_code else "",
        )
        record_system_error("mail", f"Reset-Mail: {type(exc).__name__}", location="Passwort-Reset", tenant_id=tenant_id)
        return {"sent": False, "reason": "smtp_error"}
    logger.info("Passwort-Reset-Mail fuer User %s versendet.", user_id)
    return {"sent": True}
