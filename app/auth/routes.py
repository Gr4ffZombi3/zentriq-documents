import logging
from datetime import datetime, timezone

from flask import Blueprint, abort, current_app, flash, make_response, redirect, render_template, url_for
from flask_login import current_user, login_required, login_user, logout_user

from app.auth.forms import ForgotPasswordForm, LoginForm, RegisterForm, ResetPasswordForm
from app.extensions import db
from app.models import Tenant, User
from app.models.audit_log import AuditEventType
from app.services.audit import log_audit_event
from app.services.password_reset import is_reset_rate_limited, verify_reset_token
from app.tasks.auth_tasks import send_password_reset_email
from app.tenancy import bypass_tenant_scope, set_current_tenant_id, use_tenant_id
from app.utils.slugs import unique_tenant_slug

logger = logging.getLogger(__name__)

auth_bp = Blueprint("auth", __name__, url_prefix="/auth")

RESET_REQUESTED_MESSAGE = (
    "Falls ein Konto mit dieser E-Mail-Adresse existiert, wurde eine Nachricht zum "
    "Zurücksetzen des Passworts versendet."
)


@auth_bp.route("/register", methods=["GET", "POST"])
def register():
    if not current_app.config.get("REGISTRATION_ENABLED"):
        abort(404)
    if current_user.is_authenticated:
        return redirect(url_for("dashboard.index"))

    form = RegisterForm()
    if form.validate_on_submit():
        email = form.email.data.lower().strip()
        vermittlernummer = form.vermittlernummer.data.strip()

        with bypass_tenant_scope():
            existing = User.query.filter_by(email=email).first()
        if existing is not None:
            flash("Diese E-Mail-Adresse ist bereits registriert.", "error")
            return render_template("auth/register.html", form=form)

        with bypass_tenant_scope():
            existing_vm = User.query.filter_by(vermittlernummer=vermittlernummer).first()
        if existing_vm is not None:
            flash("Diese Vermittlernummer ist bereits registriert.", "error")
            return render_template("auth/register.html", form=form)

        with bypass_tenant_scope():
            tenant = Tenant(name=form.company_name.data, slug=unique_tenant_slug(form.company_name.data))
            db.session.add(tenant)
            db.session.flush()

            user = User(tenant_id=tenant.id, email=email, vermittlernummer=vermittlernummer)
            user.set_password(form.password.data)
            db.session.add(user)
            db.session.commit()

        set_current_tenant_id(tenant.id)
        login_user(user)
        log_audit_event(
            AuditEventType.LOGIN_SUCCESS, tenant_id=tenant.id, user=user, details={"reason": "registration"}
        )
        flash("Willkommen bei Zentriq Documents!", "success")
        return redirect(url_for("dashboard.index"))

    return render_template("auth/register.html", form=form)


@auth_bp.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("dashboard.index"))

    form = LoginForm()
    if form.validate_on_submit():
        login_type = form.login_type.data
        identifier = form.identifier.data.strip()

        with bypass_tenant_scope():
            if login_type == "vermittlernummer":
                user = User.query.filter_by(vermittlernummer=identifier).first()
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

        if not user.is_active:
            log_audit_event(
                AuditEventType.LOGIN_FAILED, tenant_id=user.tenant_id, user=user, details={"reason": "inactive"}
            )
            flash("Dieses Konto ist deaktiviert.", "error")
            return render_template("auth/login.html", form=form)

        set_current_tenant_id(user.tenant_id)
        login_user(user)
        user.last_login_at = datetime.now(timezone.utc)
        db.session.commit()
        log_audit_event(AuditEventType.LOGIN_SUCCESS, tenant_id=user.tenant_id, user=user)
        return redirect(url_for("dashboard.index"))

    return render_template("auth/login.html", form=form)


@auth_bp.route("/forgot-password", methods=["GET", "POST"])
def forgot_password():
    if current_user.is_authenticated:
        return redirect(url_for("dashboard.index"))

    form = ForgotPasswordForm()
    if form.validate_on_submit():
        email = form.email.data.lower().strip()
        with bypass_tenant_scope():
            user = User.query.filter_by(email=email).first()

        # Antwort ist fuer existierende, unbekannte, deaktivierte und gedrosselte Konten
        # identisch, damit sich keine Konten per Reset-Formular ermitteln lassen.
        if user is not None and user.is_active and not is_reset_rate_limited(user):
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
    if current_user.is_authenticated:
        return redirect(url_for("dashboard.index"))

    # Das Token kommt nur per POST-Body (aus dem URL-Fragment), nie ueber Pfad oder Query-String.
    form = ResetPasswordForm()
    if form.is_submitted():
        user = verify_reset_token(form.token.data)
        if user is None:
            flash("Der Link ist ungültig oder abgelaufen. Bitte fordere einen neuen an.", "error")
            return redirect(url_for("auth.forgot_password"))

    if form.validate_on_submit():
        with use_tenant_id(user.tenant_id):
            user.set_password(form.password.data)
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
    flash("Du wurdest abgemeldet.", "success")
    return redirect(url_for("auth.login"))
