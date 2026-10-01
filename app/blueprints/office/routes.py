"""Buero-Verwaltung fuer OFFICE_ADMIN: Mitarbeiter und Aktivitaetsprotokoll.

Default-Deny im Rollen-Hook (app/auth/permissions.py) sperrt EMPLOYEE und SUPER_ADMIN bereits
aus; @admin_required sichert jede View zusaetzlich ab. Mitarbeiter werden ausschliesslich ueber
get_office_member_or_404 (eigener Mandant, nie SUPER_ADMIN) geladen. Anlegen/Bearbeiten/
Loeschen nutzt die bestehenden Formulare und Services unter /settings/users."""

from datetime import time, timedelta

from flask import Blueprint, render_template, request
from flask_login import current_user, login_required
from sqlalchemy.orm import selectinload

from app.auth.permissions import admin_required
from app.models import AuditLog, User
from app.models.audit_log import OFFICE_ACTIVITY_EVENT_TYPES
from app.services import leipziger_todo
from app.services.activity import day_label, describe_activities
from app.services.timetracking import service as time_service
from app.services.timetracking.calc import month_bounds, week_bounds
from app.services.timetracking.clock import local_to_utc_naive, local_today, utcnow_naive
from app.services.user_admin import get_office_member_or_404, office_members_query
from app.utils.vermittlernummer import vermittlernummer_key

office_bp = Blueprint("office", __name__)

WEEKDAY_NAMES = {"1": "Mo", "2": "Di", "3": "Mi", "4": "Do", "5": "Fr", "6": "Sa", "7": "So"}
ACTIVITY_PAGE_SIZE = 50


@office_bp.get("/mitarbeiter")
@login_required
@admin_required
def staff():
    members = (
        office_members_query()
        .options(selectinload(User.employee_profile))
        .order_by(User.is_active.desc(), User.email)
        .all()
    )
    rows = {row.user.id: row for row in time_service.build_team_overview([m for m in members if m.is_active])}
    members.sort(key=lambda user: (not user.is_active, leipziger_todo.user_display_name(user).lower()))
    return render_template("office/staff.html", members=members, rows=rows)


@office_bp.get("/mitarbeiter/<int:user_id>")
@login_required
@admin_required
def staff_detail(user_id):
    member = get_office_member_or_404(user_id)
    now = utcnow_naive()
    today = local_today(now)
    state = time_service.get_stamp_state(member.id, now)
    week = time_service.summarize_user_period(member, *week_bounds(today), now=now)
    month = time_service.summarize_user_period(member, *month_bounds(today), now=now)
    today_summary = next(item for item in week.days if item.day == today)

    key = vermittlernummer_key(member.vermittlernummer)
    documents = leipziger_todo.list_documents()
    leipziger = None
    if documents and key:
        entries = leipziger_todo.visible_entries(leipziger_todo.load_entries(documents[0]), key, all_brokers=False)
        leipziger = {"document": documents[0], "counts": leipziger_todo.tab_counts(entries)}

    return render_template(
        "office/staff_detail.html",
        member=member,
        name=leipziger_todo.user_display_name(member),
        state=state,
        today=today,
        today_summary=today_summary,
        month=month,
        leipziger=leipziger,
        has_lists=bool(documents),
        weekday_names=WEEKDAY_NAMES,
    )


@office_bp.get("/aktivitaeten")
@login_required
@admin_required
def activities():
    """Aktivitaeten des eigenen Bueros, neueste zuerst, optional je Mitarbeiter. AuditLog ist
    nicht mandantengebunden - der Filter auf tenant_id ist deshalb hier explizit."""
    members = office_members_query(include_deleted=True).options(selectinload(User.employee_profile)).all()
    member_ids = {member.id for member in members}
    selected = request.args.get("mitarbeiter", type=int)
    if selected not in member_ids:
        selected = None
    days = request.args.get("tage", 7, type=int)
    days = days if days in (1, 7, 30, 90) else 7
    since = local_to_utc_naive(local_today() - timedelta(days=days - 1), time(0, 0))

    query = AuditLog.query.filter(
        AuditLog.tenant_id == current_user.tenant_id,
        AuditLog.event_type.in_(OFFICE_ACTIVITY_EVENT_TYPES),
        AuditLog.created_at >= since,
    )
    if selected:
        query = query.filter(AuditLog.actor_user_id == selected)
    page = max(request.args.get("seite", 1, type=int), 1)
    entries = (
        query.order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
        .offset((page - 1) * ACTIVITY_PAGE_SIZE)
        .limit(ACTIVITY_PAGE_SIZE + 1)
        .all()
    )
    has_more = len(entries) > ACTIVITY_PAGE_SIZE
    activities_ = describe_activities(entries[:ACTIVITY_PAGE_SIZE])
    groups = []
    for activity in activities_:
        if not groups or groups[-1]["day"] != activity.day:
            groups.append({"day": activity.day, "label": day_label(activity.day), "items": []})
        groups[-1]["items"].append(activity)
    members.sort(key=lambda user: leipziger_todo.user_display_name(user).lower())
    return render_template(
        "office/activities.html",
        groups=groups,
        members=members,
        selected=selected,
        days=days,
        page=page,
        has_more=has_more,
        name_of=leipziger_todo.user_display_name,
    )

