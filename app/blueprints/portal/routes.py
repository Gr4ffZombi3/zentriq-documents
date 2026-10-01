"""Startseite: "/" leitet rollenabhaengig weiter; /uebersicht ist die persoenliche Uebersicht
fuer Buero-Rollen (SUPER_ADMIN erreicht sie nicht - Rollen-Hook in app/auth/permissions.py)."""

from flask import Blueprint, abort, redirect, render_template, url_for
from flask_login import current_user, login_required

from app.models import AuditLog, CorrectionRequestStatus, TimeCorrectionRequest, User
from app.models.audit_log import OFFICE_ACTIVITY_EVENT_TYPES
from app.navigation import display_name_for, home_endpoint_for
from app.services import leipziger_todo
from app.services.activity import describe_activities
from app.services.timetracking import service as time_service
from app.services.timetracking.calc import month_bounds, week_bounds
from app.services.timetracking.clock import local_today, to_local, utcnow_naive
from app.services.user_admin import office_members_query
from app.utils.vermittlernummer import vermittlernummer_key

portal_bp = Blueprint("portal", __name__)


@portal_bp.route("/")
@login_required
def home():
    return redirect(url_for(home_endpoint_for(current_user)))


def _greeting(now) -> str:
    hour = to_local(now).hour
    if hour < 11:
        return "Guten Morgen"
    if hour < 18:
        return "Guten Tag"
    return "Guten Abend"


def _first_name(user) -> str:
    name = display_name_for(user)
    return name.split("@")[0].split()[0] if name else ""


@portal_bp.get("/uebersicht")
@login_required
def overview():
    if current_user.is_super_admin:
        abort(403)
    now = utcnow_naive()
    today = local_today(now)
    own_key = vermittlernummer_key(current_user.vermittlernummer)

    state = time_service.get_stamp_state(current_user.id, now)
    week = time_service.summarize_user_period(current_user, *week_bounds(today), now=now)
    today_summary = next(item for item in week.days if item.day == today)
    month = time_service.summarize_user_period(current_user, *month_bounds(today), now=now)

    documents = leipziger_todo.list_documents()
    entries = leipziger_todo.load_entries(documents[0]) if documents else []
    own_todo = sum(1 for e in leipziger_todo.visible_entries(entries, own_key, all_brokers=False) if e.is_todo)

    office = None
    if current_user.is_office_admin:
        members = office_members_query().filter(User.is_active.is_(True)).all()
        rows = time_service.build_team_overview(members, now)
        recent = (
            AuditLog.query.filter(
                AuditLog.tenant_id == current_user.tenant_id,
                AuditLog.event_type.in_(OFFICE_ACTIVITY_EVENT_TYPES),
            )
            .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
            .limit(6)
            .all()
        )
        office = {
            "todo_total": sum(1 for e in entries if e.is_todo),
            "members": len(members),
            "present": sum(1 for row in rows if row.state == "in"),
            "on_break": sum(1 for row in rows if row.state == "break"),
            "pending_requests": TimeCorrectionRequest.query.filter_by(status=CorrectionRequestStatus.PENDING).count(),
            "activities": describe_activities(recent),
        }

    return render_template(
        "portal/overview.html",
        greeting=_greeting(now),
        first_name=_first_name(current_user),
        today=today,
        own_todo=own_todo,
        has_own_number=own_key is not None,
        has_list=bool(documents),
        state=state,
        today_summary=today_summary,
        month=month,
        office=office,
    )
