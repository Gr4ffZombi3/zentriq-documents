"""Benutzerverwaltung durch OFFICE_ADMINs (eigener Mandant) und den SUPER_ADMIN (Plattform).

OFFICE_ADMIN: Neue Benutzer werden immer im Mandanten des handelnden Admins angelegt;
bestehende werden ausschliesslich per get_office_member_or_404 geladen (Tenant-Filter +
Ausschluss von SUPER_ADMIN-Konten und geloeschten Konten). Vergebbare Rollen sind auf
OFFICE_ASSIGNABLE_ROLES begrenzt - ein OFFICE_ADMIN kann niemals einen SUPER_ADMIN erzeugen.

SUPER_ADMIN: arbeitet mandantenuebergreifend (explizit per tenant_id), aber nur auf Konto-
und Bueroebene - fachliche Daten werden hier nie gelesen.

E-Mail und Vermittlernummer sind global eindeutig (Login ohne Mandantenauswahl), daher erfolgt
die Eindeutigkeitspruefung bewusst mandantenuebergreifend - ohne dabei Daten fremder
Mandanten preiszugeben."""

from datetime import datetime, timezone

from flask import abort

from app.extensions import db
from app.models import EmployeeProfile, User, UserRole
from app.models.audit_log import AuditEventType
from app.models.enums import OFFICE_ASSIGNABLE_ROLES
from app.services.audit import log_audit_event
from app.tenancy import bypass_tenant_scope, get_or_404_scoped
from app.utils.vermittlernummer import format_vermittlernummer, vermittlernummer_key


class UserAdminError(Exception):
    """Fachlicher Fehler mit einer fuer Anwender verstaendlichen Meldung."""


def _utcnow_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _normalize_optional(value: str | None) -> str | None:
    value = (value or "").strip()
    return value or None


def find_user_by_vermittlernummer(value: str | None) -> User | None:
    """Mandantenuebergreifende Suche ueber die normalisierte Vermittlernummer ("080950-T"
    findet auch "08/0950-T"). Nur fuer Login und Eindeutigkeitspruefung gedacht."""
    key = vermittlernummer_key(value)
    if key is None:
        return None
    with bypass_tenant_scope():
        exact = User.query.filter(User.vermittlernummer == format_vermittlernummer(value)).first()
        if exact is not None:
            return exact
        # Aeltere, noch nicht einheitlich gespeicherte Schreibweisen.
        for candidate in User.query.filter(User.vermittlernummer.isnot(None)).all():
            if vermittlernummer_key(candidate.vermittlernummer) == key:
                return candidate
    return None


# --- Buero-Mitglieder (Sicht eines OFFICE_ADMIN) ---------------------------------------------


def office_members_query(include_deleted: bool = False):
    """Benutzer des aktuellen Mandanten (Tenant-Filter greift automatisch) ohne SUPER_ADMIN-
    Konten - der Plattformbetreiber ist fuer Bueros weder sichtbar noch verwaltbar."""
    query = User.query.filter(User.role != UserRole.SUPER_ADMIN)
    if not include_deleted:
        query = query.filter(User.deleted_at.is_(None))
    return query


def get_office_member_or_404(user_id: int, include_deleted: bool = False) -> User:
    user = get_or_404_scoped(User, user_id)
    if user.is_super_admin or (user.deleted_at is not None and not include_deleted):
        abort(404)
    return user


def _ensure_unique(email: str, vermittlernummer: str | None, exclude_user_id: int | None = None) -> None:
    with bypass_tenant_scope():
        existing = User.query.filter(User.email == email).first()
        if existing is not None and existing.id != exclude_user_id:
            if existing.deleted_at is not None:
                raise UserAdminError("Diese E-Mail-Adresse gehört zu einem gelöschten Konto und ist weiterhin belegt.")
            raise UserAdminError("Diese E-Mail-Adresse ist bereits vergeben.")
    if vermittlernummer:
        existing = find_user_by_vermittlernummer(vermittlernummer)
        if existing is not None and existing.id != exclude_user_id:
            raise UserAdminError("Diese Vermittlernummer ist bereits vergeben.")


def _active_role_count(tenant_id: int | None, role: UserRole) -> int:
    with bypass_tenant_scope():
        query = User.query.filter(User.role == role, User.is_active.is_(True), User.deleted_at.is_(None))
        if tenant_id is not None:
            query = query.filter(User.tenant_id == tenant_id)
        return query.count()


def _check_role_change(actor: User, user: User, new_role: UserRole, new_active: bool) -> None:
    if user.id == actor.id and (new_role != actor.role or not new_active):
        raise UserAdminError("Sie können sich nicht selbst eine Rolle entziehen oder sich deaktivieren.")
    losing_office_admin = user.role == UserRole.OFFICE_ADMIN and user.is_active and (
        new_role != UserRole.OFFICE_ADMIN or not new_active
    )
    if losing_office_admin and _active_role_count(user.tenant_id, UserRole.OFFICE_ADMIN) <= 1:
        raise UserAdminError("Es muss mindestens ein aktiver Büro-Admin im Büro bleiben.")
    losing_super_admin = user.role == UserRole.SUPER_ADMIN and user.is_active and (
        new_role != UserRole.SUPER_ADMIN or not new_active
    )
    if losing_super_admin and _active_role_count(None, UserRole.SUPER_ADMIN) <= 1:
        raise UserAdminError("Es muss mindestens ein aktiver Super-Admin bleiben.")


def _parse_role(value: str | None, allowed_roles) -> UserRole:
    try:
        role = UserRole(value)
    except ValueError as exc:
        raise UserAdminError("Ungültige Rolle.") from exc
    if role not in allowed_roles:
        raise UserAdminError("Diese Rolle darf hier nicht vergeben werden.")
    return role


def _apply_profile(user: User, form) -> dict:
    profile = user.employee_profile
    if profile is None:
        profile = EmployeeProfile(tenant_id=user.tenant_id, user=user)
        db.session.add(profile)
    new_values = {
        "display_name": _normalize_optional(form.display_name.data),
        "personnel_number": _normalize_optional(form.personnel_number.data),
        "weekly_target_minutes": form.weekly_target_minutes,
        "workdays": "".join(sorted(form.workdays.data)),
    }
    changes = {}
    for field, value in new_values.items():
        if getattr(profile, field) != value:
            changes[field] = {"old": getattr(profile, field), "new": value}
            setattr(profile, field, value)
    return changes


def create_user(actor: User, form, *, tenant_id: int | None = None, allowed_roles=OFFICE_ASSIGNABLE_ROLES) -> User:
    """Legt einen Benutzer an. Ohne tenant_id im Mandanten des Akteurs (OFFICE_ADMIN); eine
    abweichende tenant_id darf nur der SUPER_ADMIN uebergeben."""
    if tenant_id is None:
        tenant_id = actor.tenant_id
    elif tenant_id != actor.tenant_id and not actor.is_super_admin:
        raise UserAdminError("Benutzer können nur im eigenen Büro angelegt werden.")
    role = _parse_role(form.role.data, allowed_roles)
    email = form.email.data.strip().lower()
    vermittlernummer = format_vermittlernummer(form.vermittlernummer.data)
    if not form.password.data:
        raise UserAdminError("Bitte ein Startpasswort vergeben.")
    _ensure_unique(email, vermittlernummer)

    user = User(
        tenant_id=tenant_id,
        email=email,
        vermittlernummer=vermittlernummer,
        role=role,
        is_active=bool(form.is_active.data),
    )
    user.set_password(form.password.data)
    db.session.add(user)
    db.session.flush()
    _apply_profile(user, form)
    log_audit_event(
        AuditEventType.USER_CREATED,
        tenant_id=tenant_id,
        user=actor,
        details={"target_user_id": user.id, "email": email, "role": user.role.value, "is_active": user.is_active},
    )
    return user


def update_user(actor: User, user: User, form, *, allowed_roles=OFFICE_ASSIGNABLE_ROLES, allow_password: bool = True) -> dict:
    if user.deleted_at is not None:
        raise UserAdminError("Gelöschte Konten können nicht bearbeitet werden.")
    if user.role not in allowed_roles:
        raise UserAdminError("Dieses Konto darf hier nicht bearbeitet werden.")
    email = form.email.data.strip().lower()
    vermittlernummer = format_vermittlernummer(form.vermittlernummer.data)
    new_role = _parse_role(form.role.data, allowed_roles)
    new_active = bool(form.is_active.data)
    _ensure_unique(email, vermittlernummer, exclude_user_id=user.id)
    _check_role_change(actor, user, new_role, new_active)

    changes = {}
    for field, value in (
        ("email", email),
        ("vermittlernummer", vermittlernummer),
        ("role", new_role),
        ("is_active", new_active),
    ):
        old = getattr(user, field)
        if old != value:
            changes[field] = {
                "old": old.value if isinstance(old, UserRole) else old,
                "new": value.value if isinstance(value, UserRole) else value,
            }
            setattr(user, field, value)
    if allow_password and form.password.data:
        user.set_password(form.password.data)
        changes["password"] = "neu gesetzt"
    changes.update(_apply_profile(user, form))
    if any(key in changes for key in ("password", "is_active", "role", "email")):
        # Rechte-/Zugangsaenderungen wirken sofort, nicht erst beim naechsten Login.
        user.invalidate_sessions()

    if changes:
        log_audit_event(
            AuditEventType.USER_UPDATED,
            tenant_id=user.tenant_id,
            user=actor,
            details={"target_user_id": user.id, "changes": changes},
        )
    else:
        db.session.rollback()
    return changes


def delete_user(actor: User, user: User) -> None:
    """Soft-Delete: Konto wird dauerhaft deaktiviert und aus allen Verwaltungslisten
    ausgeblendet. Zeiterfassungs-, Korrektur- und Audit-Daten bleiben unveraendert erhalten."""
    if user.id == actor.id:
        raise UserAdminError("Sie können Ihr eigenes Konto nicht löschen.")
    if user.deleted_at is not None:
        raise UserAdminError("Dieses Konto wurde bereits gelöscht.")
    _check_role_change(actor, user, user.role, new_active=False)
    user.is_active = False
    user.deleted_at = _utcnow_naive()
    user.deleted_by_user_id = actor.id
    user.invalidate_sessions()
    log_audit_event(
        AuditEventType.USER_DELETED,
        tenant_id=user.tenant_id,
        user=actor,
        details={"target_user_id": user.id, "email": user.email, "role": user.role.value},
    )


def trigger_password_reset(actor: User, user: User) -> None:
    """Administrativ angestossener Passwort-Reset: der Benutzer erhaelt den regulaeren,
    zeitlich begrenzten Reset-Link per E-Mail (inkl. 2FA-Bestaetigung). Der Akteur erfaehrt
    weder Token noch Passwort."""
    from app.services.password_reset import is_password_reset_available
    from app.tasks.auth_tasks import send_password_reset_email

    if not is_password_reset_available():
        raise UserAdminError("Der E-Mail-Versand ist nicht konfiguriert.")
    if user.deleted_at is not None or not user.is_active:
        raise UserAdminError("Für deaktivierte Konten kann kein Passwort-Reset angestoßen werden.")
    if not user.two_factor_enabled:
        raise UserAdminError(
            "Das Konto hat keine Zwei-Faktor-Authentifizierung eingerichtet - ein Reset per E-Mail "
            "ist dann nicht möglich."
        )
    log_audit_event(
        AuditEventType.PASSWORD_RESET_TRIGGERED,
        tenant_id=user.tenant_id,
        user=actor,
        details={"target_user_id": user.id},
    )
    send_password_reset_email.delay(user.id)
