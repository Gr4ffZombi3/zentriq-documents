"""Zwei-Faktor-Authentifizierung per TOTP (RFC 6238, Authenticator-Apps) plus Einmal-
Wiederherstellungscodes.

- Das TOTP-Secret wird mit Fernet (AES-128-CBC + HMAC) verschluesselt gespeichert. Der
  Schluessel wird aus SECRET_KEY abgeleitet - wird SECRET_KEY rotiert, muessen alle Konten
  2FA neu einrichten.
- Ein Code wird nur einmal akzeptiert (totp_last_counter), Toleranz +-1 Zeitschritt (30 s).
- Recovery Codes werden nur einmal im Klartext angezeigt und als HMAC gespeichert; jeder
  Code ist genau einmal verwendbar.
- Fehlversuche werden im Audit-Log gezaehlt; nach TWO_FACTOR_MAX_FAILURES Fehlversuchen im
  Zeitfenster wird jede weitere Pruefung (auch mit korrektem Code) abgelehnt.
"""

import base64
import hashlib
import hmac
import secrets
import time
from datetime import datetime, timedelta, timezone

import pyotp
import segno
from cryptography.fernet import Fernet, InvalidToken
from flask import current_app

from app.extensions import db
from app.models.audit_log import AuditEventType, AuditLog
from app.models.user import RecoveryCode, User
from app.services.audit import log_audit_event

TOTP_INTERVAL = 30
TOTP_DIGITS = 6
RECOVERY_CODE_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"  # ohne leicht verwechselbare Zeichen


def _utcnow_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _fernet() -> Fernet:
    digest = hashlib.sha256(b"zentriq-totp-v1:" + current_app.config["SECRET_KEY"].encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def _encrypt(secret: str) -> str:
    return _fernet().encrypt(secret.encode("ascii")).decode("ascii")


def _decrypt(token: str | None) -> str | None:
    if not token:
        return None
    try:
        return _fernet().decrypt(token.encode("ascii")).decode("ascii")
    except InvalidToken:
        return None


# --- Einrichtung ----------------------------------------------------------------------------


def begin_setup(user: User) -> str:
    """Erzeugt (oder verwendet weiter) ein noch unbestaetigtes Secret. Ein bereits aktives
    2FA wird hier nie ueberschrieben."""
    if user.two_factor_enabled:
        raise ValueError("2FA ist bereits aktiv.")
    secret = _decrypt(user.totp_secret_encrypted)
    if secret is None:
        secret = pyotp.random_base32()
        user.totp_secret_encrypted = _encrypt(secret)
        user.totp_last_counter = None
        db.session.commit()
    return secret


def provisioning_uri(user: User, secret: str) -> str:
    return pyotp.TOTP(secret, interval=TOTP_INTERVAL, digits=TOTP_DIGITS).provisioning_uri(
        name=user.email, issuer_name=current_app.config["TWO_FACTOR_ISSUER"]
    )


def qr_svg(uri: str) -> str:
    """QR-Code als inline-SVG (kein externer Dienst, das Secret verlaesst den Server nicht)."""
    return segno.make(uri, error="m").svg_inline(scale=5, dark="#111827", light="#ffffff", border=2)


def format_secret(secret: str) -> str:
    return " ".join(secret[i : i + 4] for i in range(0, len(secret), 4))


def confirm_setup(user: User, code: str) -> list[str] | None:
    """Aktiviert 2FA, wenn der Code zum ausstehenden Secret passt. Liefert die neuen Recovery
    Codes (einmalige Klartextanzeige) oder None bei falschem Code."""
    if user.two_factor_enabled or not user.totp_secret_encrypted:
        return None
    if not _verify_totp(user, code):
        return None
    user.totp_enabled_at = _utcnow_naive()
    codes = _replace_recovery_codes(user)
    db.session.commit()
    log_audit_event(AuditEventType.TWO_FACTOR_ENABLED, user=user)
    return codes


def reset_two_factor(target: User, actor: User) -> None:
    """Administrativer Reset: Secret und Recovery Codes werden entfernt, alle Sessions des
    Kontos beendet. Der Benutzer richtet 2FA beim naechsten Login neu ein. Gibt dem Akteur
    keinerlei Zugang zum Konto (Passwort bleibt unveraendert und unbekannt)."""
    target.totp_secret_encrypted = None
    target.totp_enabled_at = None
    target.totp_last_counter = None
    RecoveryCode.query.filter_by(user_id=target.id).delete()
    target.invalidate_sessions()
    db.session.commit()
    log_audit_event(
        AuditEventType.TWO_FACTOR_RESET,
        tenant_id=target.tenant_id,
        user=actor,
        details={"target_user_id": target.id},
    )


# --- Pruefung ---------------------------------------------------------------------------------


def _normalize_code(code: str | None) -> str:
    return "".join((code or "").split()).replace("-", "").lower()


def _verify_totp(user: User, code: str | None) -> bool:
    code = _normalize_code(code)
    if len(code) != TOTP_DIGITS or not code.isdigit():
        return False
    secret = _decrypt(user.totp_secret_encrypted)
    if secret is None:
        return False
    totp = pyotp.TOTP(secret, interval=TOTP_INTERVAL, digits=TOTP_DIGITS)
    current = int(time.time()) // TOTP_INTERVAL
    for counter in (current - 1, current, current + 1):
        if user.totp_last_counter is not None and counter <= user.totp_last_counter:
            continue
        if hmac.compare_digest(totp.at(counter * TOTP_INTERVAL), code):
            user.totp_last_counter = counter
            return True
    return False


def _hash_recovery_code(code: str) -> str:
    key = current_app.config["SECRET_KEY"].encode("utf-8")
    return hmac.new(key, b"zentriq-recovery-v1:" + code.encode("utf-8"), hashlib.sha256).hexdigest()


def _generate_recovery_code() -> str:
    raw = "".join(secrets.choice(RECOVERY_CODE_ALPHABET) for _ in range(10))
    return f"{raw[:5]}-{raw[5:]}"


def _replace_recovery_codes(user: User) -> list[str]:
    RecoveryCode.query.filter_by(user_id=user.id).delete()
    codes = [_generate_recovery_code() for _ in range(current_app.config["TWO_FACTOR_RECOVERY_CODE_COUNT"])]
    for code in codes:
        db.session.add(RecoveryCode(user_id=user.id, code_hash=_hash_recovery_code(_normalize_code(code))))
    return codes


def regenerate_recovery_codes(user: User, code: str) -> list[str] | None:
    """Neue Recovery Codes nur gegen einen gueltigen aktuellen TOTP-Code."""
    if not user.two_factor_enabled or is_locked(user) or not _verify_totp(user, code):
        if user.two_factor_enabled:
            record_failure(user, "regenerate_recovery_codes")
        return None
    codes = _replace_recovery_codes(user)
    db.session.commit()
    log_audit_event(AuditEventType.RECOVERY_CODES_REGENERATED, user=user)
    return codes


def _use_recovery_code(user: User, code: str | None) -> bool:
    normalized = _normalize_code(code)
    if len(normalized) != 10:
        return False
    expected = _hash_recovery_code(normalized)
    for candidate in RecoveryCode.query.filter_by(user_id=user.id, used_at=None).all():
        if hmac.compare_digest(candidate.code_hash, expected):
            # Bedingtes UPDATE: bei parallelen Requests gewinnt genau einer.
            updated = (
                RecoveryCode.query.filter_by(id=candidate.id, used_at=None)
                .update({"used_at": _utcnow_naive()}, synchronize_session=False)
            )
            return updated == 1
    return False


def remaining_recovery_codes(user: User) -> int:
    return RecoveryCode.query.filter_by(user_id=user.id, used_at=None).count()


def is_locked(user: User) -> bool:
    window = timedelta(minutes=current_app.config["TWO_FACTOR_LOCK_MINUTES"])
    since = datetime.now(timezone.utc) - window
    failures = AuditLog.query.filter(
        AuditLog.actor_user_id == user.id,
        AuditLog.event_type == AuditEventType.TWO_FACTOR_FAILED,
        AuditLog.created_at >= since,
    ).count()
    return failures >= current_app.config["TWO_FACTOR_MAX_FAILURES"]


def record_failure(user: User, context: str) -> None:
    db.session.rollback()
    log_audit_event(AuditEventType.TWO_FACTOR_FAILED, user=user, details={"context": context})


def verify_second_factor(user: User, code: str | None, context: str) -> bool:
    """Prueft einen TOTP- oder Recovery-Code fuer ein Konto mit aktivem 2FA. Commit bei
    Erfolg (verbrauchter Zeitschritt/Recovery Code), Audit-Eintrag bei Fehlschlag."""
    if not user.two_factor_enabled or is_locked(user):
        return False
    if _verify_totp(user, code):
        db.session.commit()
        return True
    if _use_recovery_code(user, code):
        db.session.commit()
        log_audit_event(
            AuditEventType.RECOVERY_CODE_USED,
            user=user,
            details={"context": context, "remaining": remaining_recovery_codes(user)},
        )
        return True
    record_failure(user, context)
    return False
