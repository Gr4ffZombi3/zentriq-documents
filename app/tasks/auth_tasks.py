import logging

from celery import shared_task
from flask import current_app

from app.extensions import db
from app.models import User
from app.services.mailer import is_mail_configured, send_email
from app.services.password_reset import build_reset_email_body, build_reset_url, generate_reset_token
from app.tenancy import bypass_tenant_scope

logger = logging.getLogger(__name__)


@shared_task(name="app.tasks.auth_tasks.send_password_reset_email")
def send_password_reset_email(user_id: int):
    """Das Token wird erst hier erzeugt, damit es nie im Celery-Broker (Redis) landet."""
    with bypass_tenant_scope():
        user = db.session.get(User, user_id)
    if user is None or not user.is_active:
        return {"sent": False, "reason": "user_unavailable"}
    if not is_mail_configured() or not current_app.config.get("PUBLIC_URL"):
        logger.warning("Passwort-Reset fuer User %s nicht versendet: Mailversand nicht konfiguriert.", user_id)
        return {"sent": False, "reason": "not_configured"}

    reset_url = build_reset_url(generate_reset_token(user))
    send_email(user.email, "Passwort zurücksetzen – Zentriq Documents", build_reset_email_body(reset_url))
    return {"sent": True}
