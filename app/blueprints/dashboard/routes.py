from flask import Blueprint, current_app, render_template, request
from flask_login import login_required
from sqlalchemy.orm import selectinload

from app.models import MailboxCase, MailboxStatus
from app.services.mailbox.schemas import HUK_DAMAGE_TYPES
from app.services.voice_messages_view import STATUS_FILTERS, build_voice_message_view

dashboard_bp = Blueprint("dashboard", __name__)


@dashboard_bp.route("/sprachnachrichten")
@login_required
def index():
    allowed_filters = {"all", *(status.value for status in MailboxStatus)}
    status_filter = request.args.get("status", "all")
    if status_filter not in allowed_filters:
        status_filter = "all"

    counts = {
        status.value: MailboxCase.query.filter_by(status=status).count()
        for status in MailboxStatus
    }
    query = MailboxCase.query.options(selectinload(MailboxCase.attempts)).order_by(
        MailboxCase.received_at.desc(), MailboxCase.created_at.desc()
    )
    if status_filter != "all":
        query = query.filter(MailboxCase.status == MailboxStatus(status_filter))

    return render_template(
        "dashboard/index.html",
        cases=query.limit(100).all(),
        counts=counts,
        status_filter=status_filter,
        dry_run=current_app.config["MAILBOX_DRY_RUN"],
        automation_enabled=current_app.config["HUK_AUTOMATION_ENABLED"],
        mailbox_enabled=current_app.config["PLACETEL_MAILBOX_ENABLED"],
        allowed_damage_types=HUK_DAMAGE_TYPES,
        status_filters=STATUS_FILTERS,
        build_view=build_voice_message_view,
    )
