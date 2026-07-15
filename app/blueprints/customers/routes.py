from flask import Blueprint, render_template
from flask_login import login_required

from app.models import Customer, DocumentCustomer, Task
from app.models.enums import TaskStatus
from app.tenancy import get_or_404_scoped

customers_bp = Blueprint("customers", __name__, url_prefix="/customers")


@customers_bp.route("")
@login_required
def list_customers():
    customers = Customer.query.order_by(Customer.name.asc()).all()
    return render_template("customers/list.html", customers=customers)


@customers_bp.route("/<int:customer_id>")
@login_required
def detail(customer_id):
    customer = get_or_404_scoped(Customer, customer_id)

    timeline_events = sorted(customer.timeline_events, key=lambda e: e.occurred_at)

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
        document_customers=document_customers,
        open_tasks=open_tasks,
    )
