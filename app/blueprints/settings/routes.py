from flask import Blueprint, current_app, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required, login_user

from app.auth.permissions import admin_required
from app.blueprints.settings.forms import ChangePasswordForm, TwoFactorCodeForm, UserForm
from app.extensions import db
from app.models import Tenant, User
from app.models.audit_log import AuditEventType
from app.services import two_factor
from app.services.audit import log_audit_event
from app.services.password_reset import is_password_reset_available
from app.services.user_admin import (
    UserAdminError,
    create_user,
    delete_user,
    get_office_member_or_404,
    office_members_query,
    trigger_password_reset,
    update_user,
)


def format_hours(minutes: int) -> str:
    hours = minutes / 60
    return f"{hours:g}".replace(".", ",")


settings_bp = Blueprint("settings", __name__, url_prefix="/settings")


@settings_bp.after_request
def _no_store_for_secrets(response):
    # Die Sicherheitsseite zeigt TOTP-Secret bzw. Recovery Codes: nie zwischenspeichern.
    if request.endpoint == "settings.security":
        response.headers["Cache-Control"] = "no-store"
    return response


@settings_bp.route("")
@login_required
def index():
    return redirect(url_for("settings.users" if current_user.is_office_admin else "settings.profile"))


def user_form_data(edit_user) -> dict:
    profile = edit_user.employee_profile
    return {
        "email": edit_user.email,
        "vermittlernummer": edit_user.vermittlernummer,
        "role": edit_user.role.value,
        "is_active": edit_user.is_active,
        "display_name": profile.display_name if profile else None,
        "personnel_number": profile.personnel_number if profile else None,
        "weekly_hours": format_hours(profile.weekly_target_minutes) if profile else "40",
        "workdays": list(profile.workdays) if profile else ["1", "2", "3", "4", "5"],
    }


@settings_bp.route("/users")
@login_required
@admin_required
def users():
    all_users = office_members_query().order_by(User.is_active.desc(), User.email).all()
    return render_template(
        "settings/users.html", users=all_users, password_reset_available=is_password_reset_available()
    )


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
    edit_user = get_office_member_or_404(user_id)
    form = UserForm(data=None if request.method == "POST" else user_form_data(edit_user))
    if form.validate_on_submit():
        try:
            changes = update_user(current_user, edit_user, form)
        except UserAdminError as exc:
            db.session.rollback()
            flash(str(exc), "error")
        else:
            if edit_user.id == current_user.id and "password" in changes:
                # Eigene Session mit neuer auth_version weiterfuehren.
                login_user(edit_user)
            flash("Änderungen gespeichert." if changes else "Keine Änderungen.", "success")
            return redirect(url_for("settings.users"))
    return render_template("settings/user_form.html", form=form, edit_user=edit_user)


@settings_bp.post("/users/<int:user_id>/loeschen")
@login_required
@admin_required
def user_delete(user_id):
    target = get_office_member_or_404(user_id)
    try:
        delete_user(current_user, target)
    except UserAdminError as exc:
        db.session.rollback()
        flash(str(exc), "error")
    else:
        flash("Mitarbeiter wurde gelöscht. Arbeitszeit- und Protokolldaten bleiben erhalten.", "success")
    return redirect(url_for("settings.users"))


@settings_bp.post("/users/<int:user_id>/2fa-zuruecksetzen")
@login_required
@admin_required
def user_reset_two_factor(user_id):
    target = get_office_member_or_404(user_id)
    if target.id == current_user.id:
        flash("Die eigene Zwei-Faktor-Authentifizierung kann hier nicht zurückgesetzt werden.", "error")
    else:
        two_factor.reset_two_factor(target, current_user)
        flash("Zwei-Faktor-Authentifizierung wurde zurückgesetzt. Sie wird bei der nächsten Anmeldung neu eingerichtet.", "success")
    return redirect(url_for("settings.user_edit", user_id=target.id))


@settings_bp.post("/users/<int:user_id>/passwort-reset")
@login_required
@admin_required
def user_send_password_reset(user_id):
    target = get_office_member_or_404(user_id)
    try:
        trigger_password_reset(current_user, target)
    except UserAdminError as exc:
        flash(str(exc), "error")
    else:
        flash(f"Ein Link zum Zurücksetzen des Passworts wurde an {target.email} gesendet.", "success")
    return redirect(url_for("settings.user_edit", user_id=target.id))


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
            user = current_user._get_current_object()
            user.set_password(form.new_password.data)
            # Beendet alle anderen Sessions; die aktuelle laeuft mit neuer Version weiter.
            user.invalidate_sessions()
            db.session.commit()
            login_user(user)
            log_audit_event(AuditEventType.PASSWORD_CHANGED, user=user)
            flash("Passwort wurde erfolgreich geändert. Andere Sitzungen wurden abgemeldet.", "success")
            return redirect(url_for("settings.profile"))

    return render_template("settings/profile.html", tenant=tenant, form=form)


@settings_bp.route("/sicherheit", methods=["GET", "POST"])
@login_required
def security():
    """Zwei-Faktor-Authentifizierung einrichten bzw. Status/Recovery Codes verwalten."""
    user = current_user._get_current_object()
    form = TwoFactorCodeForm()
    new_codes = None

    if user.two_factor_enabled:
        if form.validate_on_submit():
            new_codes = two_factor.regenerate_recovery_codes(user, form.code.data)
            if new_codes is None:
                flash("Der Code ist ungültig.", "error")
        return render_template(
            "settings/security.html",
            form=form,
            enabled=True,
            new_codes=new_codes,
            remaining=two_factor.remaining_recovery_codes(user),
        )

    secret = two_factor.begin_setup(user)
    if form.validate_on_submit():
        new_codes = two_factor.confirm_setup(user, form.code.data)
        if new_codes is not None:
            # Die Session bleibt gueltig; ab jetzt verlangt jede Anmeldung den zweiten Faktor.
            return render_template(
                "settings/security.html",
                form=TwoFactorCodeForm(formdata=None),
                enabled=True,
                new_codes=new_codes,
                remaining=len(new_codes),
                just_enabled=True,
            )
        flash("Der Code ist ungültig. Bitte prüfe die Uhrzeit deines Geräts und versuche es erneut.", "error")

    uri = two_factor.provisioning_uri(user, secret)
    return render_template(
        "settings/security.html",
        form=form,
        enabled=False,
        qr_svg=two_factor.qr_svg(uri),
        secret=two_factor.format_secret(secret),
        enforced=current_app.config.get("TWO_FACTOR_ENFORCED"),
    )
