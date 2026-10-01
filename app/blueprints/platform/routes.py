"""Super-Admin-Panel: Verwaltung von Bueros (Mandanten) und Benutzerkonten.

Bewusste, eng begrenzte Cross-Tenant-Zone: Hier werden mandantenuebergreifend AUSSCHLIESSLICH
Tenant-, User- und EmployeeProfile-Daten (Kontoebene) sowie sicherheitsrelevante Audit-
Ereignisse ohne Details gelesen. Fachliche Daten (Leipziger Liste, Dokumente, Kunden, Memo,
Zeiterfassung) werden in diesem Blueprint nie abgefragt; die Fach-Blueprints selbst sind fuer
SUPER_ADMIN per Default-Deny (app/auth/permissions.py) gesperrt.

Templates werden innerhalb von bypass_tenant_scope() gerendert, damit Lazy-Loads der
Konto-Relationen (employee_profile) mandantenuebergreifend korrekt funktionieren."""

import platform as python_platform
import subprocess
from functools import lru_cache
from importlib.metadata import PackageNotFoundError, version

from flask import Blueprint, abort, current_app, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required
from sqlalchemy import func, text
from sqlalchemy.orm import selectinload

from app.auth.permissions import super_admin_required
from app.blueprints.settings.forms import PlatformUserForm, TenantCreateForm, TenantForm
from app.blueprints.settings.routes import user_form_data
from app.extensions import db
from app.models import Tenant, TenantStatus, User, UserRole
from app.models.audit_log import SECURITY_EVENT_TYPES, AuditEventType, AuditLog
from app.services import two_factor
from app.services.audit import log_audit_event
from app.services.mailer import is_mail_configured
from app.services.password_reset import is_password_reset_available
from app.services.system_errors import latest_errors
from app.services.user_admin import (
    UserAdminError,
    create_user,
    delete_user,
    trigger_password_reset,
    update_user,
)
from app.tenancy import bypass_tenant_scope
from app.utils.slugs import unique_tenant_slug

platform_bp = Blueprint("platform", __name__, url_prefix="/plattform")

ALL_ROLES = tuple(UserRole)


@platform_bp.before_request
@login_required
@super_admin_required
def _require_super_admin():
    """Zweite Absicherung zusaetzlich zur Default-Deny-Pruefung: jeder Endpunkt dieses
    Blueprints ist ausschliesslich fuer SUPER_ADMIN."""
    return None


def _tenant_or_404(tenant_id: int) -> Tenant:
    tenant = db.session.get(Tenant, tenant_id)
    if tenant is None:
        abort(404)
    return tenant


def _user_or_404(user_id: int) -> User:
    with bypass_tenant_scope():
        user = User.query.options(selectinload(User.employee_profile)).filter(User.id == user_id).first()
    if user is None or user.deleted_at is not None:
        abort(404)
    return user


def _user_counts() -> dict[int, dict]:
    counts: dict[int, dict] = {}
    with bypass_tenant_scope():
        rows = (
            db.session.query(User.tenant_id, User.role, User.is_active, func.count(User.id))
            .filter(User.deleted_at.is_(None))
            .group_by(User.tenant_id, User.role, User.is_active)
            .all()
        )
    for tenant_id, role, is_active, count in rows:
        entry = counts.setdefault(tenant_id, {"total": 0, "active": 0, "office_admins": 0})
        entry["total"] += count
        if is_active:
            entry["active"] += count
            if role == UserRole.OFFICE_ADMIN:
                entry["office_admins"] += count
    return counts


@platform_bp.get("")
def index():
    return redirect(url_for("platform.offices"))


# --- Bueros ---------------------------------------------------------------------------------


@platform_bp.get("/bueros")
def offices():
    tenants = Tenant.query.order_by(Tenant.status, Tenant.name).all()
    return render_template("platform/offices.html", tenants=tenants, counts=_user_counts())


@platform_bp.route("/bueros/neu", methods=["GET", "POST"])
def office_create():
    form = TenantCreateForm()
    if form.validate_on_submit():
        name = form.tenant_name.data.strip()
        with bypass_tenant_scope():
            tenant = Tenant(name=name, slug=unique_tenant_slug(name), status=TenantStatus.ACTIVE)
            db.session.add(tenant)
            db.session.flush()
            try:
                admin = create_user(
                    current_user, form, tenant_id=tenant.id, allowed_roles=(UserRole.OFFICE_ADMIN,)
                )
            except UserAdminError as exc:
                db.session.rollback()
                flash(str(exc), "error")
                return render_template("platform/office_create.html", form=form)
            log_audit_event(
                AuditEventType.TENANT_CREATED,
                tenant_id=tenant.id,
                user=current_user,
                details={"tenant_name": name, "office_admin_user_id": admin.id},
            )
        flash(f"Büro „{name}“ mit Büro-Admin {admin.email} wurde angelegt.", "success")
        return redirect(url_for("platform.office_detail", tenant_id=tenant.id))
    return render_template("platform/office_create.html", form=form)


@platform_bp.route("/bueros/<int:tenant_id>", methods=["GET", "POST"])
def office_detail(tenant_id):
    tenant = _tenant_or_404(tenant_id)
    form = TenantForm(
        data=None if request.method == "POST" else {"name": tenant.name, "is_active": tenant.status == TenantStatus.ACTIVE}
    )
    if form.validate_on_submit():
        new_status = TenantStatus.ACTIVE if form.is_active.data else TenantStatus.SUSPENDED
        changes = {}
        if form.name.data.strip() != tenant.name:
            changes["name"] = {"old": tenant.name, "new": form.name.data.strip()}
            tenant.name = form.name.data.strip()
        if new_status != tenant.status:
            changes["status"] = {"old": tenant.status.value, "new": new_status.value}
            tenant.status = new_status
        if changes:
            log_audit_event(
                AuditEventType.TENANT_UPDATED, tenant_id=tenant.id, user=current_user, details={"changes": changes}
            )
            flash("Büro gespeichert.", "success")
        else:
            flash("Keine Änderungen.", "success")
        return redirect(url_for("platform.office_detail", tenant_id=tenant.id))

    with bypass_tenant_scope():
        members = (
            User.query.options(selectinload(User.employee_profile))
            .filter(User.tenant_id == tenant.id, User.deleted_at.is_(None))
            .order_by(User.is_active.desc(), User.role, User.email)
            .all()
        )
        return render_template("platform/office_detail.html", tenant=tenant, form=form, members=members)


@platform_bp.route("/bueros/<int:tenant_id>/benutzer/neu", methods=["GET", "POST"])
def office_user_create(tenant_id):
    tenant = _tenant_or_404(tenant_id)
    form = PlatformUserForm()
    if form.validate_on_submit():
        with bypass_tenant_scope():
            try:
                created = create_user(current_user, form, tenant_id=tenant.id, allowed_roles=ALL_ROLES)
            except UserAdminError as exc:
                db.session.rollback()
                flash(str(exc), "error")
            else:
                flash(f"Benutzer {created.email} wurde angelegt.", "success")
                return redirect(url_for("platform.office_detail", tenant_id=tenant.id))
    return render_template("platform/user_form.html", form=form, edit_user=None, tenant=tenant)


# --- Benutzer -------------------------------------------------------------------------------


@platform_bp.get("/benutzer")
def users():
    tenant_filter = request.args.get("buero", type=int)
    tenants = {tenant.id: tenant for tenant in Tenant.query.all()}
    with bypass_tenant_scope():
        query = User.query.options(selectinload(User.employee_profile)).filter(User.deleted_at.is_(None))
        if tenant_filter is not None:
            query = query.filter(User.tenant_id == tenant_filter)
        all_users = query.order_by(User.tenant_id, User.is_active.desc(), User.email).all()
        return render_template(
            "platform/users.html", users=all_users, tenants=tenants, tenant_filter=tenant_filter
        )


@platform_bp.route("/benutzer/<int:user_id>", methods=["GET", "POST"])
def user_edit(user_id):
    edit_user = _user_or_404(user_id)
    tenant = _tenant_or_404(edit_user.tenant_id)
    with bypass_tenant_scope():
        form = PlatformUserForm(data=None if request.method == "POST" else user_form_data(edit_user))
        # Der Super-Admin vergibt keine Passwoerter fuer bestehende Konten (sonst koennte er
        # sich als Buero-Benutzer anmelden) - nur Reset per E-Mail an den Benutzer selbst.
        del form.password
        del form.password_confirm
        if form.validate_on_submit():
            try:
                changes = update_user(
                    current_user, edit_user, form, allowed_roles=ALL_ROLES, allow_password=False
                )
            except UserAdminError as exc:
                db.session.rollback()
                flash(str(exc), "error")
            else:
                flash("Änderungen gespeichert." if changes else "Keine Änderungen.", "success")
                return redirect(url_for("platform.office_detail", tenant_id=tenant.id))
        return render_template(
            "platform/user_form.html",
            form=form,
            edit_user=edit_user,
            tenant=tenant,
            action_urls={
                "reset_2fa": url_for("platform.user_reset_two_factor", user_id=edit_user.id),
                "password_reset": url_for("platform.user_send_password_reset", user_id=edit_user.id),
                "delete": url_for("platform.user_delete", user_id=edit_user.id),
            },
        )


@platform_bp.post("/benutzer/<int:user_id>/buero-admin")
def user_make_office_admin(user_id):
    """Legt ein aktives Konto eines Bueros als Buero-Admin fest."""
    target = _user_or_404(user_id)
    if target.role != UserRole.EMPLOYEE or not target.is_active:
        flash("Nur aktive Mitarbeiter können als Büro-Admin festgelegt werden.", "error")
        return redirect(url_for("platform.office_detail", tenant_id=target.tenant_id))
    with bypass_tenant_scope():
        target.role = UserRole.OFFICE_ADMIN
        # Neue Rechte gelten erst nach erneuter Anmeldung (wie bei jeder Rollenaenderung).
        target.invalidate_sessions()
        log_audit_event(
            AuditEventType.USER_UPDATED,
            tenant_id=target.tenant_id,
            user=current_user,
            details={"target_user_id": target.id, "changes": {"role": {"old": "employee", "new": "office_admin"}}},
        )
        db.session.commit()
    flash(f"{target.email} ist jetzt Büro-Admin.", "success")
    return redirect(url_for("platform.office_detail", tenant_id=target.tenant_id))


@platform_bp.post("/benutzer/<int:user_id>/loeschen")
def user_delete(user_id):
    target = _user_or_404(user_id)
    with bypass_tenant_scope():
        try:
            delete_user(current_user, target)
        except UserAdminError as exc:
            db.session.rollback()
            flash(str(exc), "error")
            return redirect(url_for("platform.user_edit", user_id=target.id))
    flash("Benutzer wurde gelöscht. Historische Daten bleiben erhalten.", "success")
    return redirect(url_for("platform.office_detail", tenant_id=target.tenant_id))


@platform_bp.post("/benutzer/<int:user_id>/2fa-zuruecksetzen")
def user_reset_two_factor(user_id):
    target = _user_or_404(user_id)
    if target.id == current_user.id:
        flash("Die eigene Zwei-Faktor-Authentifizierung kann hier nicht zurückgesetzt werden.", "error")
    else:
        with bypass_tenant_scope():
            two_factor.reset_two_factor(target, current_user)
        flash("Zwei-Faktor-Authentifizierung wurde zurückgesetzt.", "success")
    return redirect(url_for("platform.user_edit", user_id=target.id))


@platform_bp.post("/benutzer/<int:user_id>/passwort-reset")
def user_send_password_reset(user_id):
    target = _user_or_404(user_id)
    with bypass_tenant_scope():
        try:
            trigger_password_reset(current_user, target)
        except UserAdminError as exc:
            flash(str(exc), "error")
        else:
            flash(f"Ein Link zum Zurücksetzen des Passworts wurde an {target.email} gesendet.", "success")
    return redirect(url_for("platform.user_edit", user_id=target.id))


# --- System und Sicherheit ------------------------------------------------------------------


@platform_bp.get("/system")
def system():
    config = current_app.config
    settings = [
        ("Offene Registrierung", "aktiv" if config.get("REGISTRATION_ENABLED") else "aus"),
        ("E-Mail-Versand", "konfiguriert" if is_mail_configured() else "nicht konfiguriert"),
        ("Passwort vergessen", "verfügbar" if is_password_reset_available() else "nicht verfügbar"),
        ("Öffentliche URL", config.get("PUBLIC_URL") or "–"),
        ("Zwei-Faktor-Pflicht", "aktiv" if config.get("TWO_FACTOR_ENFORCED") else "aus"),
        ("Gültigkeit Reset-Link", f"{config['PASSWORD_RESET_TOKEN_MAX_AGE_SECONDS'] // 60} Minuten"),
        ("Reset-Anfragen je Konto/Stunde", config["PASSWORD_RESET_MAX_REQUESTS_PER_HOUR"]),
        ("Reset-Anfragen je IP/Stunde", config["PASSWORD_RESET_MAX_REQUESTS_PER_IP_PER_HOUR"]),
        (
            "2FA-Sperre",
            f"nach {config['TWO_FACTOR_MAX_FAILURES']} Fehlversuchen für {config['TWO_FACTOR_LOCK_MINUTES']} Minuten",
        ),
        (
            "Anmeldesperre je IP",
            f"nach {config['LOGIN_MAX_FAILURES_PER_IP']} Fehlversuchen in {config['LOGIN_FAILURE_WINDOW_MINUTES']} Minuten",
        ),
        ("Sitzungsdauer", f"{int(config['PERMANENT_SESSION_LIFETIME'].total_seconds() // 3600)} Stunden"),
        ("Zeitzone", config.get("APP_TIMEZONE")),
    ]
    tenants = {tenant.id: tenant for tenant in Tenant.query.all()}
    return render_template(
        "platform/system.html",
        settings=settings,
        status=_platform_status(),
        versions=_versions(),
        user_stats=_user_stats(),
        errors=latest_errors(20),
        tenants=tenants,
    )


@lru_cache(maxsize=1)
def _git_revision() -> str:
    try:
        output = subprocess.run(
            ["git", "-C", current_app.root_path, "log", "-1", "--format=%h · %cd", "--date=format:%d.%m.%Y %H:%M"],
            capture_output=True,
            text=True,
            timeout=2,
            check=True,
        ).stdout.strip()
        return output or "unbekannt"
    except Exception:
        return "unbekannt"


def _package_version(name: str) -> str:
    try:
        return version(name)
    except PackageNotFoundError:
        return "–"


def _versions() -> list[tuple[str, str]]:
    try:
        schema = db.session.execute(text("SELECT version_num FROM alembic_version")).scalar() or "–"
    except Exception:
        db.session.rollback()
        schema = "–"
    return [
        ("Anwendung (Git-Stand)", _git_revision()),
        ("Datenbankschema", schema),
        ("Python", python_platform.python_version()),
        ("Flask", _package_version("flask")),
        ("SQLAlchemy", _package_version("sqlalchemy")),
        ("Celery", _package_version("celery")),
    ]


def _user_stats() -> list[tuple[str, int]]:
    """Kontenzahlen je Rolle (nur Kontoebene, keine Buerodaten)."""
    with bypass_tenant_scope():
        rows = (
            db.session.query(User.role, User.is_active, func.count(User.id))
            .filter(User.deleted_at.is_(None))
            .group_by(User.role, User.is_active)
            .all()
        )
    total = sum(count for _, _, count in rows)
    active = sum(count for _, is_active, count in rows if is_active)

    def by_role(role):
        return sum(count for row_role, is_active, count in rows if row_role == role and is_active)

    return [
        ("Benutzer gesamt", total),
        ("davon aktiv", active),
        ("Büro-Admins (aktiv)", by_role(UserRole.OFFICE_ADMIN)),
        ("Mitarbeiter (aktiv)", by_role(UserRole.EMPLOYEE)),
        ("Super-Admins (aktiv)", by_role(UserRole.SUPER_ADMIN)),
    ]


def _platform_status() -> list[tuple[str, str, bool | None]]:
    """Technischer Plattformstatus (ohne Buerodaten): (Bezeichnung, Wert, ok)."""
    status = []
    try:
        db.session.execute(text("SELECT 1"))
        status.append(("Datenbank", "erreichbar", True))
    except Exception:
        db.session.rollback()
        status.append(("Datenbank", "nicht erreichbar", False))

    if current_app.config.get("CELERY_TASK_ALWAYS_EAGER"):
        status.append(("Hintergrunddienste (Redis)", "nicht verwendet (synchroner Modus)", None))
    else:
        try:
            import redis

            client = redis.Redis.from_url(current_app.config["CELERY_BROKER_URL"], socket_connect_timeout=1, socket_timeout=1)
            client.ping()
            status.append(("Hintergrunddienste (Redis)", "erreichbar", True))
        except Exception:
            status.append(("Hintergrunddienste (Redis)", "nicht erreichbar", False))

    tenants = Tenant.query.all()
    active = sum(1 for tenant in tenants if tenant.status == TenantStatus.ACTIVE)
    status.append(("Büros", f"{active} aktiv, {len(tenants) - active} deaktiviert", None))
    with bypass_tenant_scope():
        users = User.query.filter(User.is_active.is_(True), User.deleted_at.is_(None)).count()
    status.append(("Aktive Benutzerkonten", str(users), None))
    return status


@platform_bp.get("/sicherheit")
def security():
    tenants = {tenant.id: tenant for tenant in Tenant.query.all()}
    with bypass_tenant_scope():
        active_users = User.query.filter(User.is_active.is_(True), User.deleted_at.is_(None)).all()
        without_2fa = [user for user in active_users if not user.two_factor_enabled]
    # Nur sicherheitsrelevante Ereignisse, ohne Details (keine fachlichen Inhalte).
    events = (
        AuditLog.query.filter(AuditLog.event_type.in_(SECURITY_EVENT_TYPES))
        .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
        .limit(100)
        .all()
    )
    return render_template(
        "platform/security.html",
        active_count=len(active_users),
        without_2fa=without_2fa,
        events=events,
        tenants=tenants,
    )
