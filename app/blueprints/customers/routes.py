from flask import Blueprint, abort, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from app.auth.permissions import admin_required
from app.models import Customer, DocumentCustomer, Task
from app.models.enums import TaskStatus
from app.navigation import display_name_for
from app.services import customer_sources
from app.services.customer_duplicates import (
    MergeError,
    compare_rows,
    duplicate_reason,
    link_counts,
    merge_customers,
)
from app.services.customer_overview import customer_entries, customer_memos
from app.services.customers import (
    DEFAULT_CUSTOMER_PAGE_SIZE,
    MAX_CUSTOMER_PAGE_SIZE,
    build_customer_detail_context,
    build_customer_directory,
)
from app.tenancy import get_or_404_scoped

MEMO_BASIS_LABELS = {
    "customer_number": "über die Kundennummer erkannt",
    "phone": "über die Telefonnummer erkannt",
    "selected": "manuell zugeordnet",
    "new_customer": "Kunde aus Memo angelegt",
}

customers_bp = Blueprint("customers", __name__, url_prefix="/customers")


@customers_bp.route("")
@login_required
def list_customers():
    page = request.args.get("page", 1, type=int)
    per_page = request.args.get("per_page", DEFAULT_CUSTOMER_PAGE_SIZE, type=int)
    directory = build_customer_directory(page=page, per_page=per_page, query=request.args.get("q", ""))
    return render_template(
        "customers/list.html",
        customer_directory=directory,
        customers=[item["customer"] for item in directory["items"]],
        max_per_page=MAX_CUSTOMER_PAGE_SIZE,
    )


@customers_bp.route("/<int:customer_id>")
@login_required
def detail(customer_id):
    customer = get_or_404_scoped(Customer, customer_id)

    timeline_events = sorted(customer.timeline_events, key=lambda e: e.occurred_at)
    customer_view = build_customer_detail_context(customer)

    document_customers = (
        DocumentCustomer.query.join(DocumentCustomer.document)
        .filter(DocumentCustomer.customer_id == customer.id)
        .all()
    )
    document_customers.sort(key=lambda dc: dc.document.uploaded_at)

    open_tasks = (
        Task.query.filter(Task.customer_id == customer.id, Task.status == TaskStatus.OPEN)
        .order_by(Task.due_date.asc())
        .all()
    )

    return render_template(
        "customers/detail.html",
        customer=customer,
        timeline_events=timeline_events,
        customer_view=customer_view,
        case_rows=customer_view["case_rows"],
        customer_summary=customer_view["summary"],
        possible_duplicates=customer_view["possible_duplicates"],
        document_customers=document_customers,
        open_tasks=open_tasks,
        leipziger_entries=customer_entries(customer),
        memos=customer_memos(customer),
        memo_basis=MEMO_BASIS_LABELS,
        source_labels=customer_sources.LABELS,
        display_name=display_name_for,
    )


def _duplicate_pair(customer_id: int, other_id: int):
    """Beide Datensaetze aus dem eigenen Buero (sonst 404) und als Dublette erkannt."""
    customer = get_or_404_scoped(Customer, customer_id)
    other = get_or_404_scoped(Customer, other_id)
    reason = duplicate_reason(customer, other)
    if reason is None:
        abort(404)
    return customer, other, reason


@customers_bp.get("/<int:customer_id>/dublette/<int:other_id>")
@login_required
@admin_required
def compare(customer_id, other_id):
    customer, other, reason = _duplicate_pair(customer_id, other_id)
    return render_template(
        "customers/compare.html",
        customer=customer,
        other=other,
        reason=reason,
        rows=compare_rows(customer, other),
        counts=(link_counts(customer), link_counts(other)),
    )


@customers_bp.post("/<int:customer_id>/zusammenfuehren/<int:other_id>")
@login_required
@admin_required
def merge(customer_id, other_id):
    """Fuehrt `other` in `customer` zusammen - nur nach ausdruecklicher Bestaetigung."""
    customer, other, _reason = _duplicate_pair(customer_id, other_id)
    if request.form.get("bestaetigt") != "ja":
        flash("Bitte bestätigen, dass es sich eindeutig um denselben Kunden handelt.", "error")
        return redirect(url_for("customers.compare", customer_id=customer.id, other_id=other.id))
    try:
        merge_customers(customer, other, current_user)
    except MergeError as exc:
        flash(str(exc), "error")
        return redirect(url_for("customers.compare", customer_id=customer.id, other_id=other.id))
    flash("Die Datensätze wurden zusammengeführt. Alle Verknüpfungen sind erhalten.", "success")
    return redirect(url_for("customers.detail", customer_id=customer_id))
