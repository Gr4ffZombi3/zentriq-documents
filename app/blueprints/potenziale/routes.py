from datetime import date

from flask import Blueprint, render_template, request
from flask_login import login_required

from app.models.enums import ListScope, PotentialCategory
from app.services.analysis.leipziger_liste_view import get_analysis_summary, get_potential_records

potenziale_bp = Blueprint("potenziale", __name__, url_prefix="/potenziale")


def _parse_enum(enum_cls, raw_value):
    try:
        return enum_cls(raw_value) if raw_value else None
    except ValueError:
        return None


def _parse_date(raw_value):
    try:
        return date.fromisoformat(raw_value) if raw_value else None
    except ValueError:
        return None


def _parse_filters(args) -> dict:
    return {
        "category": _parse_enum(PotentialCategory, args.get("category", "")),
        "include_closed": args.get("include_closed") == "1",
        "product_line": args.get("product_line") or None,
        "broker_number": args.get("broker_number") or None,
        "date_from": _parse_date(args.get("date_from", "")),
        "date_to": _parse_date(args.get("date_to", "")),
        "list_scope": _parse_enum(ListScope, args.get("list_scope", "")),
    }


@potenziale_bp.route("")
@login_required
def index():
    filters = _parse_filters(request.args)
    records = get_potential_records(**filters)
    summary = get_analysis_summary()
    return render_template(
        "potenziale/index.html",
        records=records,
        summary=summary,
        filters=filters,
        categories=PotentialCategory,
    )
