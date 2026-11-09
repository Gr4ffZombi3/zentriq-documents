from flask import (
    Blueprint,
    abort,
    current_app,
    flash,
    make_response,
    redirect,
    render_template,
    request,
    url_for,
)
from flask_login import current_user, login_required, login_user

from app.auth.permissions import admin_required
from app.blueprints.settings.forms import ChangePasswordForm, OwnAccountForm, TwoFactorCodeForm, UserForm
from app.extensions import db
from app.models import Tenant, User
from app.models.audit_log import AuditEventType
from app.services import account_profile, trusted_browsers, two_factor, user_sessions
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


def _from_staff() -> bool:
    """Rueckkehr zur Mitarbeiterseite statt zur Benutzertabelle (feste Zielseiten, keine URL)."""
    return request.values.get("von") == "mitarbeiter"


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
            return redirect(url_for("office.staff_detail", user_id=created.id) if _from_staff() else url_for("settings.users"))
    return render_template("settings/user_form.html", form=form, edit_user=None, from_staff=_from_staff())


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
                user_sessions.start_session(edit_user)
                db.session.commit()
            flash("Änderungen gespeichert." if changes else "Keine Änderungen.", "success")
            return redirect(url_for("office.staff_detail", user_id=edit_user.id) if _from_staff() else url_for("settings.users"))
    return render_template("settings/user_form.html", form=form, edit_user=edit_user, from_staff=_from_staff())


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
    return redirect(url_for("office.staff") if _from_staff() else url_for("settings.users"))


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
            user_sessions.start_session(user)
            db.session.commit()
            login_user(user)
            log_audit_event(AuditEventType.PASSWORD_CHANGED, user=user)
            flash("Passwort wurde erfolgreich geändert. Andere Sitzungen wurden abgemeldet.", "success")
            return redirect(url_for("settings.profile"))

    sessions = user_sessions.active_sessions(current_user.id)
    current_record = user_sessions.current_session_record(current_user.id)
    return render_template(
        "settings/profile.html",
        tenant=tenant,
        form=form,
        sessions=sessions,
        current_session_id=current_record.id if current_record else None,
        trusted_browser_count=trusted_browsers.active_count(current_user.id),
        describe_user_agent=user_sessions.describe_user_agent,
        can_edit_account=account_profile.can_edit_own_account(current_user),
        pending_email=account_profile.pending_email(current_user) if account_profile.can_edit_own_account(current_user) else None,
    )


@settings_bp.route("/profile/kontodaten", methods=["GET", "POST"])
@login_required
def account_edit():
    """Eigene Kontodaten (nur Buero-Admin, nur das angemeldete Konto - keine Benutzer-ID im
    Request). Buero/Rolle sind keine Formularfelder; uebermittelte Werte dafuer werden ignoriert."""
    if not account_profile.can_edit_own_account(current_user):
        abort(403)
    user = current_user._get_current_object()
    tenant = db.session.get(Tenant, user.tenant_id)
    profile = user.employee_profile
    form = OwnAccountForm(data=None if request.method == "POST" else {
        "display_name": profile.display_name if profile else "", "email": user.email, "vermittlernummer": user.vermittlernummer or "",
    })
    impact = []
    if form.validate_on_submit():
        try:
            number = account_profile.validate_vermittlernummer(user, form.vermittlernummer.data)
            new_email = account_profile.validate_new_email(user, form.email.data)
            login_change = account_profile.vermittlernummer_changes_login(user, number)
            impact = account_profile.impact_of_vermittlernummer(user, number) if login_change else []
            if impact and not form.confirm_impact.data:
                flash("Bitte die Auswirkungen der neuen Vermittlernummer prüfen und bestätigen. Es wurde noch nichts gespeichert.", "warning")
            else:
                reauth = False
                if login_change or new_email:
                    account_profile.check_password_and_code(user, form.current_password.data, form.code.data)
                    reauth = True
                changes = account_profile.update_name_and_vermittlernummer(user, form.display_name.data, number, reauthenticated=reauth)
                messages = ["Kontodaten gespeichert." if changes else ("Keine Änderungen." if not new_email else "")]
                if new_email:
                    account_profile.request_email_change(user, new_email, reauthenticated=reauth)
                    messages.append(f"Bestätigungslink an {new_email} gesendet. Bis zur Bestätigung bleibt {user.email} Ihre Anmeldeadresse.")
                flash(" ".join(m for m in messages if m), "success")
                return redirect(url_for("settings.profile"))
        except account_profile.AccountError as exc:
            db.session.rollback()
            flash(str(exc), "error")
    response = make_response(render_template("settings/account_edit.html", form=form, tenant=tenant, impact=impact,
                                             needs_code=user.two_factor_enabled))
    response.headers["Cache-Control"] = "no-store"
    return response


@settings_bp.route("/profile/email-bestaetigen", methods=["GET", "POST"])
@login_required
def account_email_confirm():
    """Bestaetigung der neuen Adresse. Das Token steht im URL-Fragment und kommt nur per POST."""
    if request.method == "POST":  # CSRF-Schutz global (CSRFProtect)
        try:
            email = account_profile.confirm_email_change(current_user._get_current_object(), request.form.get("token"))
        except account_profile.AccountError as exc:
            db.session.rollback()
            flash(str(exc), "error")
        else:
            login_user(current_user._get_current_object())
            flash(f"Ihre Anmelde-E-Mail lautet jetzt {email}. Andere Sitzungen wurden abgemeldet.", "success")
        return redirect(url_for("settings.profile"))
    response = make_response(render_template("settings/account_email_confirm.html"))
    response.headers["Referrer-Policy"] = "same-origin"  # Flask-WTF prueft bei HTTPS den Referer
    response.headers["Cache-Control"] = "no-store"
    return response


@settings_bp.post("/sitzungen/abmelden")
@login_required
def revoke_other_sessions():
    """Meldet alle anderen Geraete ab; die aktuelle Sitzung bleibt angemeldet. Alle vertrauten
    Browser (auch dieser) verlangen danach wieder den 2FA-Code."""
    trusted_browsers.revoke_all(current_user.id)
    count = user_sessions.revoke_other_sessions(current_user.id)
    log_audit_event(AuditEventType.SESSIONS_REVOKED, user=current_user, details={"count": count})
    flash(
        "Alle anderen Sitzungen wurden abgemeldet." if count else "Es gab keine weiteren aktiven Sitzungen.",
        "success",
    )
    return redirect(url_for("settings.profile") + "#sitzungen")


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
