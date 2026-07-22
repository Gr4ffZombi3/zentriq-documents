from flask import Blueprint, render_template, request
from flask_login import current_user, login_required

from app.models import ListComparison
from app.models.enums import ComparisonKind
from app.services.analysis.leipziger_liste_view import build_document_analysis

potenziale_bp = Blueprint("potenziale", __name__, url_prefix="/potenziale")


@potenziale_bp.route("")
@login_required
def index():
    document_id = request.args.get("document_id", type=int)
    status_filter = request.args.get("status_filter", "alle")
    search_query = request.args.get("search", "").strip() or None
    product_line_filter = request.args.get("product_line", "").strip() or None
    group_by_customer = request.args.get("group_by", "1") != "0"
    analysis = build_document_analysis(
        document_id=document_id,
        status_filter=status_filter,
        current_broker_number=getattr(current_user, "vermittlernummer", None),
        search_query=search_query,
        product_line_filter=product_line_filter,
        group_by_customer=group_by_customer,
    )
    return render_template(
        "potenziale/index.html",
        analysis=analysis,
    )


@potenziale_bp.route("/vergleich")
@login_required
def vergleich():
    comparisons = (
        ListComparison.query.filter_by(comparison_kind=ComparisonKind.OWN_VS_GS)
        .order_by(ListComparison.compared_at.desc())
        .all()
    )

    selected_id = request.args.get("document_id", type=int)
    selected = None
    if selected_id is not None:
        selected = next((c for c in comparisons if c.document_id == selected_id), None)
    if selected is None and comparisons:
        selected = comparisons[0]

    return render_template("potenziale/vergleich.html", comparisons=comparisons, selected=selected)
