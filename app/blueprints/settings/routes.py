from flask import Blueprint, flash, redirect, render_template, url_for
from flask_login import current_user, login_required

from app.blueprints.settings.forms import ChangePasswordForm
from app.extensions import db
from app.models import Tenant

settings_bp = Blueprint("settings", __name__, url_prefix="/settings")


@settings_bp.route("")
@login_required
def index():
    return render_template(
        "settings/coming_soon.html",
        title="Einstellungen",
        description="Weitere Einstellungen (Benachrichtigungen, Team, Abrechnung) folgen in Kürze.",
    )


@settings_bp.route("/users")
@login_required
def users():
    return render_template(
        "settings/coming_soon.html",
        title="Benutzerverwaltung",
        description="Die Verwaltung von Team-Mitgliedern und Rollen folgt in Kürze.",
    )


@settings_bp.route("/profile", methods=["GET", "POST"])
@login_required
def profile():
    # Tenant ist nicht TenantScopedMixin (hat selbst keinen Tenant), daher ist dieser
    # direkte Lookup per ID sicher und braucht keinen bypass_tenant_scope().
    tenant = db.session.get(Tenant, current_user.tenant_id)

    form = ChangePasswordForm()
    if form.validate_on_submit():
        if not current_user.check_password(form.current_password.data):
            flash("Aktuelles Passwort ist falsch.", "error")
        else:
            current_user.set_password(form.new_password.data)
            db.session.commit()
            flash("Passwort wurde erfolgreich geändert.", "success")
            return redirect(url_for("settings.profile"))

    return render_template("settings/profile.html", tenant=tenant, form=form)
