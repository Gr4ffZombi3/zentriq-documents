"""Benutzerverwaltung durch Admins des eigenen Mandanten.

Neue Benutzer werden immer im Mandanten des handelnden Admins angelegt; bestehende werden
ausschliesslich per get_or_404_scoped geladen. E-Mail und Vermittlernummer sind global
eindeutig (Login ohne Mandantenauswahl), daher erfolgt die Eindeutigkeitspruefung bewusst
mandantenuebergreifend - ohne dabei Daten fremder Mandanten preiszugeben."""

from app.extensions import db
from app.models import EmployeeProfile, User, UserRole
from app.models.audit_log import AuditEventType
from app.services.audit import log_audit_event
from app.tenancy import bypass_tenant_scope
from app.utils.vermittlernummer import format_vermittlernummer, vermittlernummer_key


class UserAdminError(Exception):
    """Fachlicher Fehler mit einer fuer Anwender verstaendlichen Meldung."""


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


def _ensure_unique(email: str, vermittlernummer: str | None, exclude_user_id: int | None = None) -> None:
    with bypass_tenant_scope():
        existing = User.query.filter(User.email == email).first()
        if existing is not None and existing.id != exclude_user_id:
            raise UserAdminError("Diese E-Mail-Adresse ist bereits vergeben.")
    if vermittlernummer:
        existing = find_user_by_vermittlernummer(vermittlernummer)
        if existing is not None and existing.id != exclude_user_id:
            raise UserAdminError("Diese Vermittlernummer ist bereits vergeben.")


def _active_admin_count() -> int:
    return User.query.filter_by(role=UserRole.ADMIN, is_active=True).count()


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


def create_user(actor: User, form) -> User:
    email = form.email.data.strip().lower()
    vermittlernummer = format_vermittlernummer(form.vermittlernummer.data)
    if not form.password.data:
        raise UserAdminError("Bitte ein Startpasswort vergeben.")
    _ensure_unique(email, vermittlernummer)

    user = User(
        tenant_id=actor.tenant_id,
        email=email,
        vermittlernummer=vermittlernummer,
        role=UserRole(form.role.data),
        is_active=bool(form.is_active.data),
    )
    user.set_password(form.password.data)
    db.session.add(user)
    db.session.flush()
    _apply_profile(user, form)
    log_audit_event(
        AuditEventType.USER_CREATED,
        user=actor,
        details={"target_user_id": user.id, "email": email, "role": user.role.value, "is_active": user.is_active},
    )
    return user


def update_user(actor: User, user: User, form) -> dict:
    email = form.email.data.strip().lower()
    vermittlernummer = format_vermittlernummer(form.vermittlernummer.data)
    new_role = UserRole(form.role.data)
    new_active = bool(form.is_active.data)
    _ensure_unique(email, vermittlernummer, exclude_user_id=user.id)

    if user.id == actor.id and (new_role != UserRole.ADMIN or not new_active):
        raise UserAdminError("Sie können sich nicht selbst die Admin-Rolle entziehen oder sich deaktivieren.")
    losing_admin = user.role == UserRole.ADMIN and user.is_active and (new_role != UserRole.ADMIN or not new_active)
    if losing_admin and _active_admin_count() <= 1:
        raise UserAdminError("Es muss mindestens ein aktiver Admin im Mandanten bleiben.")

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
    if form.password.data:
        user.set_password(form.password.data)
        changes["password"] = "neu gesetzt"
    changes.update(_apply_profile(user, form))

    if changes:
        log_audit_event(
            AuditEventType.USER_UPDATED, user=actor, details={"target_user_id": user.id, "changes": changes}
        )
    else:
        db.session.rollback()
    return changes
