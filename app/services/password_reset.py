"""Passwort-Reset per signiertem, zeitlich begrenztem Token.

Das Token ist zustandslos (itsdangerous, signiert mit SECRET_KEY) und enthaelt neben der
User-ID einen Fingerabdruck des aktuellen Passwort-Hashes. Sobald das Passwort geaendert
wurde - auch durch den Reset selbst - passt der Fingerabdruck nicht mehr, das Token ist also
faktisch einmalig verwendbar, ohne dass eine eigene Token-Tabelle noetig ist.
"""

import hashlib
import hmac
from datetime import datetime, timedelta, timezone

from flask import current_app
from itsdangerous import BadSignature, URLSafeTimedSerializer

from app.extensions import db
from app.models.audit_log import AuditEventType, AuditLog
from app.models.user import User
from app.tenancy import bypass_tenant_scope

_SALT = "zentriq-password-reset"


def _serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(current_app.config["SECRET_KEY"], salt=_SALT)


def _password_fingerprint(user: User) -> str:
    return hashlib.sha256(user.password_hash.encode("utf-8")).hexdigest()[:32]


def generate_reset_token(user: User) -> str:
    return _serializer().dumps({"uid": user.id, "pw": _password_fingerprint(user)})


def verify_reset_token(token: str) -> User | None:
    """Liefert den User zum Token oder None, wenn das Token ungueltig, abgelaufen, bereits
    verbraucht oder das Konto deaktiviert ist."""
    max_age = current_app.config["PASSWORD_RESET_TOKEN_MAX_AGE_SECONDS"]
    try:
        payload = _serializer().loads(token, max_age=max_age)
    except BadSignature:  # umfasst auch SignatureExpired
        return None
    if not isinstance(payload, dict) or not isinstance(payload.get("uid"), int):
        return None

    with bypass_tenant_scope():
        user = db.session.get(User, payload["uid"])
    if user is None or not user.is_active:
        return None
    if not hmac.compare_digest(str(payload.get("pw", "")), _password_fingerprint(user)):
        return None
    return user


def build_reset_url(token: str) -> str:
    base_url = current_app.config.get("PUBLIC_URL")
    if not base_url:
        raise RuntimeError("PUBLIC_URL ist nicht konfiguriert.")
    return f"{base_url}/auth/reset-password/{token}"


def is_reset_rate_limited(user: User) -> bool:
    """Drosselt Reset-Anfragen pro Konto anhand der Audit-Eintraege der letzten Stunde."""
    limit = current_app.config["PASSWORD_RESET_MAX_REQUESTS_PER_HOUR"]
    since = datetime.now(timezone.utc) - timedelta(hours=1)
    recent = AuditLog.query.filter(
        AuditLog.actor_user_id == user.id,
        AuditLog.event_type == AuditEventType.PASSWORD_RESET_REQUESTED,
        AuditLog.created_at >= since,
    ).count()
    return recent >= limit


def build_reset_email_body(reset_url: str) -> str:
    minutes = current_app.config["PASSWORD_RESET_TOKEN_MAX_AGE_SECONDS"] // 60
    return (
        "Hallo,\n\n"
        "für dein Konto bei Zentriq Documents wurde das Zurücksetzen des Passworts angefordert.\n"
        f"Über den folgenden Link kannst du innerhalb von {minutes} Minuten ein neues Passwort festlegen:\n\n"
        f"{reset_url}\n\n"
        "Der Link ist nur einmal gültig. Falls du das nicht angefordert hast, kannst du diese "
        "E-Mail ignorieren – dein bisheriges Passwort bleibt unverändert.\n\n"
        "Zentriq Documents\n"
    )
