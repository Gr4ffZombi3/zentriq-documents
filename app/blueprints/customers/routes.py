from flask import Blueprint, render_template, request
from flask_login import login_required

from app.models import Customer, DocumentCustomer, Task
from app.models.enums import TaskStatus
from app.services.customers import DEFAULT_CUSTOMER_PAGE_SIZE, MAX_CUSTOMER_PAGE_SIZE, build_customer_detail_context, build_customer_directory
from app.tenancy import get_or_404_scoped

customers_bp = Blueprint("customers", __name__, url_prefix="/customers")


@customers_bp.route("")
@login_required
def list_customers():
    page = request.args.get("page", 1, type=int)
    per_page = request.args.get("per_page", DEFAULT_CUSTOMER_PAGE_SIZE, type=int)
    directory = build_customer_directory(page=page, per_page=per_page)
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
    )
