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
    # HMAC statt reinem Hash: Der Token-Payload ist nur signiert, nicht verschluesselt, und
    # soll nichts vom Passwort-Hash preisgeben.
    key = current_app.config["SECRET_KEY"].encode("utf-8")
    return hmac.new(key, user.password_hash.encode("utf-8"), hashlib.sha256).hexdigest()[:32]


def generate_reset_token(user: User) -> str:
    return _serializer().dumps({"uid": user.id, "pw": _password_fingerprint(user)})


def verify_reset_token(token: str | None) -> User | None:
    """Liefert den User zum Token oder None, wenn das Token ungueltig, abgelaufen, bereits
    verbraucht oder das Konto deaktiviert ist."""
    if not token:
        return None
    max_age = current_app.config["PASSWORD_RESET_TOKEN_MAX_AGE_SECONDS"]
    try:
        payload = _serializer().loads(token, max_age=max_age)
    except BadSignature:  # umfasst auch SignatureExpired
        return None
    if not isinstance(payload, dict) or not isinstance(payload.get("uid"), int):
        return None

    with bypass_tenant_scope():
        user = db.session.get(User, payload["uid"])
        if user is None:
            return None
        from app.services.account_state import account_login_block_reason

        # Deaktivierte/geloeschte Konten und Konten deaktivierter Bueros: Token wertlos.
        if account_login_block_reason(user) is not None:
            return None
    if not hmac.compare_digest(str(payload.get("pw", "")), _password_fingerprint(user)):
        return None
    return user


def missing_reset_settings() -> list[str]:
    """Namen der Einstellungen, ohne die keine Reset-Mail versendet werden kann (nur Namen,
    nie Werte - fuer Server-Log und Fehlerprotokoll)."""
    config = current_app.config
    missing = [name for name in ("SMTP_HOST", "MAIL_FROM", "PUBLIC_URL") if not config.get(name)]
    public_url = config.get("PUBLIC_URL") or ""
    if public_url and not public_url.startswith(("https://", "http://")):
        missing.append("PUBLIC_URL (mit https:// angeben)")
    return missing


def is_password_reset_available() -> bool:
    """Der Reset wird nur angeboten, wenn Reset-Mails tatsaechlich versendet werden koennen."""
    return not missing_reset_settings()


def build_reset_url(token: str) -> str:
    base_url = current_app.config.get("PUBLIC_URL")
    if not base_url:
        raise RuntimeError("PUBLIC_URL ist nicht konfiguriert.")
    # Token im Fragment (#): Browser senden es nie an den Server, dadurch taucht es weder in
    # Gunicorn-/nginx-Access-Logs noch im Referer auf. Die Seite uebertraegt es per POST.
    return f"{base_url}/auth/reset-password#token={token}"


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


def is_ip_reset_rate_limited(ip_address: str | None) -> bool:
    """Drosselt Reset-Anfragen pro Client-IP - unabhaengig davon, ob die Adresse existiert."""
    if not ip_address:
        return False
    limit = current_app.config["PASSWORD_RESET_MAX_REQUESTS_PER_IP_PER_HOUR"]
    since = datetime.now(timezone.utc) - timedelta(hours=1)
    recent = AuditLog.query.filter(
        AuditLog.ip_address == ip_address,
        AuditLog.event_type == AuditEventType.PASSWORD_RESET_REQUESTED,
        AuditLog.created_at >= since,
    ).count()
    return recent >= limit


def build_reset_email_body(reset_url: str) -> str:
    minutes = current_app.config["PASSWORD_RESET_TOKEN_MAX_AGE_SECONDS"] // 60
    return (
        "Guten Tag,\n\n"
        "für Ihr Konto bei Zentriq wurde das Zurücksetzen des Passworts angefordert.\n\n"
        "Über den folgenden Link können Sie ein neues Passwort festlegen:\n\n"
        f"{reset_url}\n\n"
        f"Der Link ist {minutes} Minuten gültig und kann nur einmal verwendet werden. Zur "
        "Bestätigung Ihrer Identität benötigen Sie zusätzlich den Code aus Ihrer "
        "Authenticator-App oder einen Ihrer Wiederherstellungscodes.\n\n"
        "Falls Sie diese Anfrage nicht gestellt haben, können Sie diese E-Mail ignorieren. "
        "Ihr bisheriges Passwort bleibt dann unverändert.\n\n"
        "Aus Sicherheitsgründen enthält diese E-Mail kein Passwort. Bitte geben Sie den Link "
        "nicht weiter.\n\n"
        "Mit freundlichen Grüßen\n"
        "Zentriq\n\n"
        "Diese Nachricht wurde automatisch erstellt. Bitte antworten Sie nicht auf diese E-Mail.\n"
    )
