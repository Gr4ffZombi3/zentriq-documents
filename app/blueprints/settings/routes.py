from flask import Blueprint, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from app.auth.permissions import admin_required
from app.blueprints.settings.forms import ChangePasswordForm, UserForm
from app.extensions import db
from app.models import Tenant, User
from app.services.user_admin import UserAdminError, create_user, update_user
from app.tenancy import get_or_404_scoped


def format_hours(minutes: int) -> str:
    hours = minutes / 60
    return f"{hours:g}".replace(".", ",")

settings_bp = Blueprint("settings", __name__, url_prefix="/settings")


@settings_bp.route("")
@login_required
def index():
    return redirect(url_for("settings.users" if current_user.is_admin else "settings.profile"))


@settings_bp.route("/users")
@login_required
@admin_required
def users():
    all_users = User.query.order_by(User.is_active.desc(), User.email).all()
    return render_template("settings/users.html", users=all_users)


@settings_bp.route("/users/new", methods=["GET", "POST"])
@login_required
@admin_required
def user_create():
    form = UserForm()
    if form.validate_on_submit():
        try:
            created = create_user(current_user, form)
        except UserAdminError as exc:
            db.session.rollback()
            flash(str(exc), "error")
        else:
            flash(f"Benutzer {created.email} wurde angelegt.", "success")
            return redirect(url_for("settings.users"))
    return render_template("settings/user_form.html", form=form, edit_user=None)


@settings_bp.route("/users/<int:user_id>", methods=["GET", "POST"])
@login_required
@admin_required
def user_edit(user_id):
    edit_user = get_or_404_scoped(User, user_id)
    profile = edit_user.employee_profile
    form = UserForm(
        data=None
        if request.method == "POST"
        else {
            "email": edit_user.email,
            "vermittlernummer": edit_user.vermittlernummer,
            "role": edit_user.role.value,
            "is_active": edit_user.is_active,
            "display_name": profile.display_name if profile else None,
            "personnel_number": profile.personnel_number if profile else None,
            "weekly_hours": format_hours(profile.weekly_target_minutes) if profile else "40",
            "workdays": list(profile.workdays) if profile else ["1", "2", "3", "4", "5"],
        }
    )
    if form.validate_on_submit():
        try:
            changes = update_user(current_user, edit_user, form)
        except UserAdminError as exc:
            db.session.rollback()
            flash(str(exc), "error")
        else:
            flash("Änderungen gespeichert." if changes else "Keine Änderungen.", "success")
            return redirect(url_for("settings.users"))
    return render_template("settings/user_form.html", form=form, edit_user=edit_user)


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
