from datetime import datetime, timezone

from flask import Blueprint, flash, redirect, render_template, url_for
from flask_login import current_user, login_required, login_user, logout_user

from app.auth.forms import LoginForm, RegisterForm
from app.extensions import db
from app.models import Tenant, User
from app.models.audit_log import AuditEventType
from app.services.audit import log_audit_event
from app.tenancy import bypass_tenant_scope, set_current_tenant_id
from app.utils.slugs import unique_tenant_slug

auth_bp = Blueprint("auth", __name__, url_prefix="/auth")


@auth_bp.route("/register", methods=["GET", "POST"])
def register():
    if current_user.is_authenticated:
        return redirect(url_for("dashboard.index"))

    form = RegisterForm()
    if form.validate_on_submit():
        email = form.email.data.lower().strip()

        with bypass_tenant_scope():
            existing = User.query.filter_by(email=email).first()
        if existing is not None:
            flash("Diese E-Mail-Adresse ist bereits registriert.", "error")
            return render_template("auth/register.html", form=form)

        with bypass_tenant_scope():
            tenant = Tenant(name=form.company_name.data, slug=unique_tenant_slug(form.company_name.data))
            db.session.add(tenant)
            db.session.flush()

            user = User(tenant_id=tenant.id, email=email)
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
        email = form.email.data.lower().strip()

        with bypass_tenant_scope():
            user = User.query.filter_by(email=email).first()

        if user is None or not user.check_password(form.password.data):
            log_audit_event(AuditEventType.LOGIN_FAILED, actor_email=email, details={"reason": "invalid_credentials"})
            flash("E-Mail oder Passwort ist falsch.", "error")
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


@auth_bp.route("/logout", methods=["POST"])
@login_required
def logout():
    log_audit_event(AuditEventType.LOGOUT, tenant_id=current_user.tenant_id, user=current_user)
    logout_user()
    flash("Du wurdest abgemeldet.", "success")
    return redirect(url_for("auth.login"))
