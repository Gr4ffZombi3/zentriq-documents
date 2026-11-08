"""Eigene Kontodaten des Buero-Admins (Mein Konto -> Kontodaten bearbeiten).

- Name: sofort gespeichert (EmployeeProfile.display_name).
- Vermittlernummer: dieselbe Normalisierung/Eindeutigkeit wie in der Bueroverwaltung
  (user_admin). Sie ist alternativer Login und steuert Zuordnungen - vor dem Speichern werden die
  Auswirkungen benannt (impact_of_vermittlernummer), bestehende Vorgaenge werden NICHT umgeordnet.
- E-Mail (Login, Passwort-Reset): erst nach erneuter Authentifizierung (Passwort, ggf. 2FA-Code)
  wird ein Bestaetigungslink an die NEUE Adresse gesendet. Bis zur Bestaetigung bleibt die
  bisherige Adresse aktiv. Das Token ist zustandslos (itsdangerous, wie password_reset) und an die
  bisherige Adresse gebunden - nach jeder Aenderung der Adresse ist es wertlos.

Buero und Rolle sind hier nie aenderbar: die Funktionen kennen diese Felder nicht.
Audit ueber den vorhandenen Typ USER_UPDATED (details.action), ohne neue Enum-Werte."""

from __future__ import annotations

import hashlib
import hmac
from datetime import datetime, timedelta, timezone

from flask import current_app
from itsdangerous import BadSignature, URLSafeTimedSerializer

from app.extensions import db
from app.models import EmployeeProfile, User
from app.models.audit_log import AuditEventType, AuditLog
from app.services.audit import log_audit_event
from app.services.user_admin import UserAdminError, _ensure_unique
from app.utils.vermittlernummer import format_vermittlernummer, vermittlernummer_key

_SALT = "zentriq-email-change"
TOKEN_MAX_AGE_SECONDS = 24 * 3600
MAX_REQUESTS_PER_HOUR = 5


class AccountError(Exception):
    pass


def can_edit_own_account(user: User) -> bool:
    """Nur Buero-Admins pflegen ihre Kontodaten selbst. Mitarbeiter nicht: ihre Vermittlernummer
    steuert, welche Vorgaenge der Leipziger Liste sie sehen (pflegt der Buero-Admin)."""
    return bool(user.is_authenticated and user.is_office_admin and not user.is_super_admin and user.deleted_at is None)


def _audit(user: User, action: str, **details) -> None:
    log_audit_event(AuditEventType.USER_UPDATED, tenant_id=user.tenant_id, user=user,
                    details={"target_user_id": user.id, "action": action, **details})


# --- Name und Vermittlernummer ---------------------------------------------------------------


def impact_of_vermittlernummer(user: User, new_value: str | None) -> list[str]:
    """Konkrete Auswirkungen einer geaenderten Vermittlernummer (leer = keine Aenderung)."""
    from app.models import Customer, LeipzigerEntry, Settlement

    old_key, new_key = vermittlernummer_key(user.vermittlernummer), vermittlernummer_key(new_value)
    if old_key == new_key:
        return []
    old_label = format_vermittlernummer(user.vermittlernummer) or "keine"
    new_label = format_vermittlernummer(new_value) or "keine"
    hints = []

    def entries(key):
        return LeipzigerEntry.query.filter(LeipzigerEntry.broker_key == key).count() if key else 0

    old_n, new_n = entries(old_key), entries(new_key)
    hints.append(
        "Leipziger Liste: Ihr Zugriff bleibt unverändert (Büro-Admins sehen alle Vorgänge des Büros). "
        f"Als Ihre eigenen Vorgänge gelten künftig {new_n} statt bisher {old_n} (Vermittlernummer {new_label} statt {old_label})."
    )
    mine = Settlement.query.filter(Settlement.employee_user_id == user.id).count()
    others_new = 0
    if new_key:
        others_new = sum(
            1 for s in Settlement.query.filter(Settlement.broker_number.isnot(None)).all()
            if vermittlernummer_key(s.broker_number) == new_key and s.employee_user_id != user.id
        )
    line = (f"Abrechnungen: {mine} Ihnen bereits zugeordnete Beleg(e) bleiben bei Ihnen. Neu eingereichte Belege werden "
            f"künftig über {new_label} zugeordnet" + (f", nicht mehr über {old_label}" if old_key else "") + ".")
    if others_new:
        line += (f" {others_new} bereits eingereichte(r) Beleg(e) mit {new_label} sind bisher nicht Ihnen zugeordnet und "
                 "werden NICHT automatisch umgeordnet (Zuordnung im Beleg durch den Büro-Admin).")
    hints.append(line)
    without_owner = [c for c in Customer.query.filter(Customer.assigned_user_id.is_(None), Customer.broker_number.isnot(None)).all()]
    old_c = sum(1 for c in without_owner if old_key and vermittlernummer_key(c.broker_number) == old_key)
    new_c = sum(1 for c in without_owner if new_key and vermittlernummer_key(c.broker_number) == new_key)
    if old_c or new_c:
        hints.append(f"Kalender: Für Kunden ohne hinterlegten Betreuer gilt die Vermittlernummer als Zuständigkeit – "
                     f"künftig {new_c} statt {old_c} Kunden. Bestehende Termine bleiben unverändert.")
    hints.append(f"Anmeldung per Vermittlernummer künftig mit {new_label}." if new_key else "Die Anmeldung per Vermittlernummer entfällt.")
    return hints


def check_password_and_code(user: User, password: str | None, code: str | None) -> None:
    """Erneute Authentifizierung fuer Aenderungen an Login-Kennungen."""
    from app.services import two_factor

    if not password or not user.check_password(password):
        raise AccountError("Zur Bestätigung bitte Ihr aktuelles Passwort angeben.")
    if user.two_factor_enabled:
        if two_factor.is_locked(user):
            raise AccountError("Zu viele ungültige Codes. Bitte später erneut versuchen.")
        if not two_factor.verify_second_factor(user, code, "account_change"):
            raise AccountError("Bitte den aktuellen Code aus Ihrer Authenticator-App angeben.")


def validate_vermittlernummer(user: User, value: str | None) -> str | None:
    """Gleiche Regeln wie in der Bueroverwaltung: Normalisierung, Laenge, Eindeutigkeit."""
    number = format_vermittlernummer(value)
    if number and len(number) > 50:
        raise AccountError("Die Vermittlernummer ist zu lang (höchstens 50 Zeichen).")
    try:
        _ensure_unique(user.email, number, exclude_user_id=user.id)
    except UserAdminError as exc:
        raise AccountError(str(exc)) from None
    return number


def vermittlernummer_changes_login(user: User, number: str | None) -> bool:
    return vermittlernummer_key(number) != vermittlernummer_key(user.vermittlernummer)


def validate_new_email(user: User, new_email: str | None) -> str | None:
    """Neue Adresse oder None (unveraendert). Prueft Eindeutigkeit und ob bestaetigt werden kann."""
    from app.services.password_reset import missing_reset_settings

    email = (new_email or "").strip().lower()
    if not email or email == (user.email or "").lower():
        return None
    try:
        _ensure_unique(email, None, exclude_user_id=user.id)
    except UserAdminError as exc:
        raise AccountError(str(exc)) from None
    if missing_reset_settings():
        raise AccountError("Der E-Mail-Versand ist nicht eingerichtet – die neue Adresse kann nicht bestätigt werden. "
                           "Bitte den Plattformbetreiber kontaktieren.")
    if sum(1 for r in _recent_requests(user) if (r.details or {}).get("action") == "email_change_requested") >= MAX_REQUESTS_PER_HOUR:
        raise AccountError("Zu viele Anfragen zur E-Mail-Änderung. Bitte in einer Stunde erneut versuchen.")
    return email


def update_name_and_vermittlernummer(user: User, display_name: str | None, number: str | None, *, reauthenticated: bool) -> dict:
    """`number` bereits durch validate_vermittlernummer geprueft. Eine geaenderte Vermittlernummer
    (alternative Login-Kennung) setzt eine erneute Authentifizierung voraus."""
    if not can_edit_own_account(user):
        raise AccountError("Ihre Kontodaten pflegt Ihr Büro-Admin.")
    changes = {}
    name = " ".join((display_name or "").split())[:120] or None
    profile = user.employee_profile
    if (profile.display_name if profile else None) != name:
        if profile is None:
            profile = EmployeeProfile(tenant_id=user.tenant_id, user=user)
            db.session.add(profile)
        changes["display_name"] = {"old": profile.display_name, "new": name}
        profile.display_name = name
    if (number or None) != (user.vermittlernummer or None):
        if vermittlernummer_changes_login(user, number) and not reauthenticated:
            raise AccountError("Zur Bestätigung bitte Ihr aktuelles Passwort angeben.")
        changes["vermittlernummer"] = {"old": user.vermittlernummer, "new": number}
        user.vermittlernummer = number
    if changes:
        _audit(user, "self_profile", changes=changes)
        db.session.commit()
    return changes


# --- E-Mail-Adresse ----------------------------------------------------------------------------


def _serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(current_app.config["SECRET_KEY"], salt=_SALT)


def _email_fingerprint(email: str) -> str:
    key = current_app.config["SECRET_KEY"].encode("utf-8")
    return hmac.new(key, (email or "").lower().encode("utf-8"), hashlib.sha256).hexdigest()[:32]


def _recent_requests(user: User, hours: int = 1) -> list[AuditLog]:
    since = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=hours)
    rows = (AuditLog.query.filter(AuditLog.actor_user_id == user.id, AuditLog.event_type == AuditEventType.USER_UPDATED,
                                  AuditLog.created_at >= since).order_by(AuditLog.id.desc()).limit(50).all())
    return [r for r in rows if (r.details or {}).get("action") in ("email_change_requested", "email_changed")]


def pending_email(user: User) -> str | None:
    """Zuletzt angeforderte, noch nicht bestaetigte neue Adresse (nur Anzeige)."""
    for row in _recent_requests(user, hours=TOKEN_MAX_AGE_SECONDS // 3600):
        details = row.details or {}
        if details.get("action") == "email_changed":
            return None
        if details.get("action") == "email_change_requested" and details.get("for_email") == user.email:
            return details.get("new_email")
    return None


def request_email_change(user: User, email: str, *, reauthenticated: bool) -> str:
    """`email` bereits durch validate_new_email geprueft. Sendet den Bestaetigungslink an die
    neue Adresse; die bisherige Adresse bleibt bis zur Bestaetigung aktiv."""
    from app.services import mailer

    if not can_edit_own_account(user):
        raise AccountError("Ihre Kontodaten pflegt Ihr Büro-Admin.")
    if not reauthenticated:
        raise AccountError("Zur Bestätigung bitte Ihr aktuelles Passwort angeben.")
    token = _serializer().dumps({"uid": user.id, "new": email, "old": _email_fingerprint(user.email)})
    url = f"{current_app.config['PUBLIC_URL'].rstrip('/')}/settings/profile/email-bestaetigen#token={token}"
    body = (
        "Guten Tag,\n\n"
        "für Ihr Zentriq-Konto wurde diese Adresse als neue Anmelde-E-Mail angegeben.\n\n"
        "Bitte bestätigen Sie die Änderung über den folgenden Link (Sie müssen dabei angemeldet sein):\n\n"
        f"{url}\n\n"
        f"Der Link ist {TOKEN_MAX_AGE_SECONDS // 3600} Stunden gültig. Bis zur Bestätigung bleibt Ihre bisherige Adresse aktiv.\n\n"
        "Falls Sie die Änderung nicht veranlasst haben, ignorieren Sie diese E-Mail.\n\n"
        "Mit freundlichen Grüßen\nZentriq\n"
    )
    try:
        mailer.send_email(email, "Neue E-Mail-Adresse bestätigen – Zentriq", body)
    except Exception:  # noqa: BLE001 - Versandfehler ohne Details an den Benutzer
        current_app.logger.exception("account.email_change_mail_failed user=%s", user.id)
        raise AccountError("Die Bestätigungs-E-Mail konnte nicht gesendet werden. Bitte später erneut versuchen.") from None
    _audit(user, "email_change_requested", new_email=email, for_email=user.email)
    db.session.commit()
    return email


def confirm_email_change(user: User, token: str | None) -> str:
    """Aktiviert die neue Adresse - nur fuer das angemeldete Konto, fuer das das Token erstellt
    wurde, und nur solange die bisherige Adresse unveraendert ist."""
    from app.services import mailer, user_sessions

    try:
        payload = _serializer().loads(token or "", max_age=TOKEN_MAX_AGE_SECONDS)
    except BadSignature:
        raise AccountError("Der Bestätigungslink ist ungültig oder abgelaufen.") from None
    if not isinstance(payload, dict) or payload.get("uid") != user.id:
        raise AccountError("Dieser Bestätigungslink gehört zu einem anderen Konto. Bitte mit dem richtigen Konto anmelden.")
    if not can_edit_own_account(user):
        raise AccountError("Ihre Kontodaten pflegt Ihr Büro-Admin.")
    if not hmac.compare_digest(str(payload.get("old", "")), _email_fingerprint(user.email)):
        raise AccountError("Der Bestätigungslink ist nicht mehr gültig (die E-Mail-Adresse wurde inzwischen geändert).")
    email = str(payload.get("new") or "").lower()
    try:
        _ensure_unique(email, None, exclude_user_id=user.id)
    except UserAdminError as exc:
        raise AccountError(str(exc)) from None
    old = user.email
    user.email = email
    # Login-Kennung geaendert: andere Sitzungen abmelden, diese mit neuer Version weiterfuehren.
    user.invalidate_sessions()
    user_sessions.start_session(user)
    _audit(user, "email_changed", changes={"email": {"old": old, "new": email}})
    db.session.commit()
    try:
        mailer.send_email(old, "Ihre Anmelde-E-Mail wurde geändert – Zentriq",
                          "Guten Tag,\n\ndie Anmelde-E-Mail Ihres Zentriq-Kontos wurde soeben auf eine neue Adresse geändert.\n"
                          "Falls Sie das nicht waren, wenden Sie sich bitte umgehend an Ihr Büro bzw. den Plattformbetreiber.\n\n"
                          "Mit freundlichen Grüßen\nZentriq\n")
    except Exception:  # noqa: BLE001 - Hinweis an die alte Adresse ist nur ergaenzend
        current_app.logger.warning("account.email_changed_notice_failed user=%s", user.id)
    return email
