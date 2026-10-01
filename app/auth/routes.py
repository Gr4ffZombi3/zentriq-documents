import logging
import time
from datetime import datetime, timedelta, timezone

from flask import (
    Blueprint,
    abort,
    current_app,
    flash,
    make_response,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from flask_login import current_user, login_required, login_user, logout_user

from app.auth.forms import ForgotPasswordForm, LoginForm, RegisterForm, ResetPasswordForm, TwoFactorForm
from app.extensions import db
from app.models import Tenant, User, UserRole
from app.models.audit_log import AuditEventType, AuditLog
from app.services import two_factor as two_factor_service
from app.services.account_state import account_login_block_reason
from app.services.audit import log_audit_event
from app.services.password_reset import (
    is_ip_reset_rate_limited,
    is_password_reset_available,
    is_reset_rate_limited,
    verify_reset_token,
)
from app.services.user_admin import find_user_by_vermittlernummer
from app.tasks.auth_tasks import send_password_reset_email
from app.tenancy import bypass_tenant_scope, set_current_tenant_id, use_tenant_id
from app.utils.slugs import unique_tenant_slug
from app.utils.vermittlernummer import format_vermittlernummer

logger = logging.getLogger(__name__)

auth_bp = Blueprint("auth", __name__, url_prefix="/auth")

PENDING_2FA_KEY = "_pending_2fa"

BLOCKED_LOGIN_MESSAGES = {
    "inactive": "Dieses Konto ist deaktiviert.",
    "deleted": "Dieses Konto ist deaktiviert.",
    "tenant_suspended": "Dieses Büro ist deaktiviert. Bitte wenden Sie sich an den Betreiber.",
}

RESET_REQUESTED_MESSAGE = (
    "Falls ein Konto mit dieser E-Mail-Adresse existiert, wurde eine Nachricht zum "
    "Zurücksetzen des Passworts versendet."
)


@auth_bp.route("/register", methods=["GET", "POST"])
def register():
    if not current_app.config.get("REGISTRATION_ENABLED"):
        abort(404)
    if current_user.is_authenticated:
        return redirect(url_for("portal.home"))

    form = RegisterForm()
    if form.validate_on_submit():
        email = form.email.data.lower().strip()
        vermittlernummer = format_vermittlernummer(form.vermittlernummer.data)

        with bypass_tenant_scope():
            existing = User.query.filter_by(email=email).first()
        if existing is not None:
            flash("Diese E-Mail-Adresse ist bereits registriert.", "error")
            return render_template("auth/register.html", form=form)

        existing_vm = find_user_by_vermittlernummer(vermittlernummer)
        if existing_vm is not None:
            flash("Diese Vermittlernummer ist bereits registriert.", "error")
            return render_template("auth/register.html", form=form)

        with bypass_tenant_scope():
            tenant = Tenant(name=form.company_name.data, slug=unique_tenant_slug(form.company_name.data))
            db.session.add(tenant)
            db.session.flush()

            user = User(
                tenant_id=tenant.id, email=email, vermittlernummer=vermittlernummer, role=UserRole.OFFICE_ADMIN
            )
            user.set_password(form.password.data)
            db.session.add(user)
            db.session.commit()

        set_current_tenant_id(tenant.id)
        login_user(user)
        log_audit_event(
            AuditEventType.LOGIN_SUCCESS, tenant_id=tenant.id, user=user, details={"reason": "registration"}
        )
        flash("Willkommen bei Zentriq Documents!", "success")
        return redirect(url_for("portal.home"))

    return render_template("auth/register.html", form=form)


def _is_login_rate_limited() -> bool:
    """Brute-Force-Schutz pro Client-IP anhand der fehlgeschlagenen Anmeldungen."""
    window = timedelta(minutes=current_app.config["LOGIN_FAILURE_WINDOW_MINUTES"])
    since = datetime.now(timezone.utc) - window
    failures = AuditLog.query.filter(
        AuditLog.ip_address == request.remote_addr,
        AuditLog.event_type.in_((AuditEventType.LOGIN_FAILED, AuditEventType.TWO_FACTOR_FAILED)),
        AuditLog.created_at >= since,
    ).count()
    return failures >= current_app.config["LOGIN_MAX_FAILURES_PER_IP"]


def _complete_login(user: User, details: dict | None = None):
    session.pop(PENDING_2FA_KEY, None)
    set_current_tenant_id(user.tenant_id)
    login_user(user)
    user.last_login_at = datetime.now(timezone.utc)
    db.session.commit()
    log_audit_event(AuditEventType.LOGIN_SUCCESS, tenant_id=user.tenant_id, user=user, details=details)
    return redirect(url_for("portal.home"))


@auth_bp.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("portal.home"))

    form = LoginForm()
    if form.validate_on_submit():
        login_type = form.login_type.data
        identifier = form.identifier.data.strip()

        if _is_login_rate_limited():
            flash("Zu viele fehlgeschlagene Anmeldeversuche. Bitte versuchen Sie es später erneut.", "error")
            return render_template("auth/login.html", form=form), 429

        with bypass_tenant_scope():
            if login_type == "vermittlernummer":
                user = find_user_by_vermittlernummer(identifier)
            else:
                user = User.query.filter_by(email=identifier.lower()).first()

        if user is None or not user.check_password(form.password.data):
            log_audit_event(
                AuditEventType.LOGIN_FAILED,
                actor_email=identifier if login_type == "email" else None,
                details={"reason": "invalid_credentials", "login_type": login_type},
            )
            flash("Anmeldedaten sind falsch.", "error")
            return render_template("auth/login.html", form=form)

        block_reason = account_login_block_reason(user)
        if block_reason is not None:
            log_audit_event(
                AuditEventType.LOGIN_FAILED, tenant_id=user.tenant_id, user=user, details={"reason": block_reason}
            )
            flash(BLOCKED_LOGIN_MESSAGES[block_reason], "error")
            return render_template("auth/login.html", form=form)

        if user.two_factor_enabled:
            # Passwort korrekt, aber noch NICHT angemeldet: erst nach gueltigem zweiten Faktor.
            session[PENDING_2FA_KEY] = {"uid": user.id, "v": user.auth_version or 0, "ts": int(time.time())}
            return redirect(url_for("auth.two_factor"))

        return _complete_login(user)

    return render_template("auth/login.html", form=form)


def _pending_two_factor_user() -> User | None:
    pending = session.get(PENDING_2FA_KEY)
    if not isinstance(pending, dict):
        return None
    max_age = current_app.config["TWO_FACTOR_PENDING_MAX_AGE_SECONDS"]
    if not isinstance(pending.get("ts"), int) or time.time() - pending["ts"] > max_age:
        session.pop(PENDING_2FA_KEY, None)
        return None
    with bypass_tenant_scope():
        user = db.session.get(User, pending.get("uid"))
        if user is None or (user.auth_version or 0) != pending.get("v") or not user.two_factor_enabled:
            session.pop(PENDING_2FA_KEY, None)
            return None
        if account_login_block_reason(user) is not None:
            session.pop(PENDING_2FA_KEY, None)
            return None
    return user


@auth_bp.route("/2fa", methods=["GET", "POST"])
def two_factor():
    if current_user.is_authenticated:
        return redirect(url_for("portal.home"))
    user = _pending_two_factor_user()
    if user is None:
        flash("Bitte melde dich erneut an.", "error")
        return redirect(url_for("auth.login"))

    form = TwoFactorForm()
    if form.validate_on_submit():
        with use_tenant_id(user.tenant_id):
            if two_factor_service.is_locked(user):
                flash("Zu viele ungültige Codes. Bitte versuche es später erneut.", "error")
                return render_template("auth/two_factor.html", form=form), 429
            if two_factor_service.verify_second_factor(user, form.code.data, "login"):
                return _complete_login(user, details={"two_factor": True})
        flash("Der Code ist ungültig.", "error")

    return render_template("auth/two_factor.html", form=form)


@auth_bp.app_context_processor
def _inject_password_reset_available():
    return {"password_reset_available": is_password_reset_available()}


@auth_bp.route("/forgot-password", methods=["GET", "POST"])
def forgot_password():
    if not is_password_reset_available():
        abort(404)
    if current_user.is_authenticated:
        return redirect(url_for("portal.home"))

    form = ForgotPasswordForm()
    if form.validate_on_submit():
        email = form.email.data.lower().strip()
        with bypass_tenant_scope():
            user = User.query.filter_by(email=email).first()

        # Antwort ist fuer existierende, unbekannte, deaktivierte und gedrosselte Konten
        # identisch, damit sich keine Konten per Reset-Formular ermitteln lassen.
        if is_ip_reset_rate_limited(request.remote_addr):
            logger.warning("Passwort-Reset fuer IP %s gedrosselt.", request.remote_addr)
        elif user is None or account_login_block_reason(user) is not None:
            # Auch unbekannte Adressen zaehlen fuer die IP-Drosselung (ohne Kontobezug).
            log_audit_event(AuditEventType.PASSWORD_RESET_REQUESTED, details={"matched": False})
        elif not user.two_factor_enabled:
            # Reset per E-Mail nur mit eingerichteter 2FA - die Identitaet muss zusaetzlich
            # bestaetigt werden koennen. Kein Versand, aber identische Antwort.
            with use_tenant_id(user.tenant_id):
                log_audit_event(
                    AuditEventType.PASSWORD_RESET_REQUESTED,
                    tenant_id=user.tenant_id,
                    user=user,
                    details={"sent": False, "reason": "two_factor_missing"},
                )
        elif not is_reset_rate_limited(user):
            user_id, tenant_id = user.id, user.tenant_id
            with use_tenant_id(tenant_id):
                log_audit_event(AuditEventType.PASSWORD_RESET_REQUESTED, tenant_id=tenant_id, user=user)
            try:
                send_password_reset_email.delay(user_id)
            except Exception:
                logger.exception("Passwort-Reset-Mail fuer User %s konnte nicht eingeplant werden.", user_id)

        flash(RESET_REQUESTED_MESSAGE, "success")
        return redirect(url_for("auth.login"))

    return render_template("auth/forgot_password.html", form=form)


@auth_bp.route("/reset-password", methods=["GET", "POST"])
def reset_password():
    if not is_password_reset_available():
        abort(404)
    if current_user.is_authenticated:
        return redirect(url_for("portal.home"))

    # Das Token kommt nur per POST-Body (aus dem URL-Fragment), nie ueber Pfad oder Query-String.
    form = ResetPasswordForm()
    if form.is_submitted():
        user = verify_reset_token(form.token.data)
        if user is None:
            flash("Der Link ist ungültig oder abgelaufen. Bitte fordere einen neuen an.", "error")
            return redirect(url_for("auth.forgot_password"))
        if not user.two_factor_enabled:
            # Ohne zweiten Faktor ist die Identitaet nicht ausreichend bestaetigt.
            flash(
                "Für dieses Konto ist keine Zwei-Faktor-Authentifizierung eingerichtet. Bitte wende "
                "dich an den Administrator deines Büros, um ein neues Passwort zu erhalten.",
                "error",
            )
            return redirect(url_for("auth.login"))

    if form.validate_on_submit():
        with use_tenant_id(user.tenant_id):
            if two_factor_service.is_locked(user):
                flash("Zu viele ungültige Codes. Bitte versuche es später erneut.", "error")
            elif not two_factor_service.verify_second_factor(user, form.code.data, "password_reset"):
                flash("Der Bestätigungscode ist ungültig.", "error")
            else:
                user.set_password(form.password.data)
                # Beendet alle bestehenden Sessions; das Token ist durch den neuen Passwort-
                # Hash ebenfalls sofort verbraucht.
                user.invalidate_sessions()
                db.session.commit()
                log_audit_event(AuditEventType.PASSWORD_RESET_COMPLETED, tenant_id=user.tenant_id, user=user)
                flash("Dein Passwort wurde geändert. Du kannst dich jetzt anmelden.", "success")
                return redirect(url_for("auth.login"))

    response = make_response(render_template("auth/reset_password.html", form=form))
    # Seite verarbeitet ein Token: nicht an Dritte weitergeben und nicht zwischenspeichern.
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Cache-Control"] = "no-store"
    return response


@auth_bp.route("/logout", methods=["POST"])
@login_required
def logout():
    log_audit_event(AuditEventType.LOGOUT, tenant_id=current_user.tenant_id, user=current_user)
    logout_user()
    session.pop(PENDING_2FA_KEY, None)
    flash("Du wurdest abgemeldet.", "success")
    return redirect(url_for("auth.login"))
