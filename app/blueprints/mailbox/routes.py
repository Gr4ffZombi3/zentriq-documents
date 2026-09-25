from flask import Blueprint, current_app, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from app.extensions import db
from app.models import MailboxCase, MailboxStatus
from app.services.mailbox.phone import normalize_callback_phone
from app.services.mailbox.pipeline import add_case_event
from app.services.mailbox.schemas import HUK_DAMAGE_TYPES
from app.tasks.mailbox_tasks import poll_placetel_mailbox, submit_mailbox_case
from app.tenancy import get_or_404_scoped

mailbox_bp = Blueprint("mailbox", __name__, url_prefix="/mailbox")


@mailbox_bp.get("")
@login_required
def index():
    return redirect(url_for("dashboard.index", **request.args))


@mailbox_bp.get("/<int:case_id>")
@login_required
def detail(case_id):
    mailbox_case = get_or_404_scoped(MailboxCase, case_id)
    return render_template(
        "mailbox/detail.html",
        mailbox_case=mailbox_case,
        dry_run=current_app.config["MAILBOX_DRY_RUN"],
        automation_enabled=current_app.config["HUK_AUTOMATION_ENABLED"],
        allowed_damage_types=HUK_DAMAGE_TYPES,
    )


@mailbox_bp.post("/<int:case_id>/update")
@login_required
def update(case_id):
    mailbox_case = get_or_404_scoped(MailboxCase, case_id)
    if mailbox_case.status == MailboxStatus.CALLBACK_REQUESTED:
        flash("Bereits übermittelte Rückrufanforderungen sind unveränderlich.", "error")
        return redirect(url_for("mailbox.detail", case_id=case_id))

    callback_phone = normalize_callback_phone(request.form.get("callback_phone"))
    damage_type = request.form.get("damage_type", "").strip()
    if not callback_phone:
        flash("Bitte gib eine gültige deutsche Rückrufnummer ein.", "error")
        return redirect(url_for("mailbox.detail", case_id=case_id))
    if damage_type not in HUK_DAMAGE_TYPES:
        flash("Bitte wähle ausschließlich eine im HUK-Formular vorhandene Schadenart.", "error")
        return redirect(url_for("mailbox.detail", case_id=case_id))

    before = {
        "callback_phone": mailbox_case.callback_phone,
        "damage_type": mailbox_case.damage_type,
        "status": mailbox_case.status.value,
    }
    mailbox_case.callback_phone = callback_phone
    mailbox_case.phone_source = "manual"
    mailbox_case.phone_confidence = 1.0
    mailbox_case.concern = "Schadenanliegen"
    mailbox_case.damage_type = damage_type
    mailbox_case.damage_confidence = 1.0
    mailbox_case.last_error = None
    if damage_type == "Sonstiges":
        mailbox_case.status = MailboxStatus.REVIEW
        mailbox_case.review_reason = "‚Sonstiges‘ wird aus Sicherheitsgründen nicht automatisch abgesendet."
    else:
        mailbox_case.status = MailboxStatus.NEW
        mailbox_case.review_reason = None
    add_case_event(
        mailbox_case,
        "manually_corrected",
        {
            "before": before,
            "after": {
                "callback_phone": mailbox_case.callback_phone,
                "damage_type": mailbox_case.damage_type,
                "status": mailbox_case.status.value,
            },
        },
        actor_user_id=current_user.id,
    )
    db.session.commit()
    flash("Die erkannten Formulardaten wurden korrigiert.", "success")
    return redirect(url_for("mailbox.detail", case_id=case_id))


@mailbox_bp.post("/<int:case_id>/retry")
@login_required
def retry(case_id):
    mailbox_case = get_or_404_scoped(MailboxCase, case_id)
    if mailbox_case.status == MailboxStatus.CALLBACK_REQUESTED:
        flash("Für diese Mailbox-Nachricht wurde bereits ein Rückruf angefordert.", "error")
        return redirect(url_for("mailbox.detail", case_id=case_id))
    if not mailbox_case.callback_phone or not mailbox_case.damage_type or mailbox_case.review_reason:
        flash("Korrigiere zuerst Rufnummer und Schadenart eindeutig.", "error")
        return redirect(url_for("mailbox.detail", case_id=case_id))

    mailbox_case.status = MailboxStatus.NEW
    mailbox_case.last_error = None
    add_case_event(mailbox_case, "retry_requested", actor_user_id=current_user.id)
    db.session.commit()
    submit_mailbox_case.delay(mailbox_case.id)
    if current_app.config["MAILBOX_DRY_RUN"] or not current_app.config["HUK_AUTOMATION_ENABLED"]:
        flash("Dry Run ausgeführt: Formulardaten vorbereitet, nichts abgesendet.", "success")
    else:
        flash("Die erneute Ausführung wurde gestartet.", "success")
    return redirect(url_for("mailbox.detail", case_id=case_id))


@mailbox_bp.post("/sync")
@login_required
def sync():
    if not current_app.config["PLACETEL_MAILBOX_ENABLED"]:
        flash("Die Placetel-Mailbox ist noch nicht konfiguriert.", "error")
        return redirect(url_for("dashboard.index"))
    poll_placetel_mailbox.delay()
    flash("Das Postfach wird geprüft.", "success")
    return redirect(url_for("dashboard.index"))
