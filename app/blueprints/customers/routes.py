from flask import Blueprint, render_template
from flask_login import login_required

from app.models import Customer

customers_bp = Blueprint("customers", __name__, url_prefix="/customers")


@customers_bp.route("")
@login_required
def list_customers():
    customers = Customer.query.order_by(Customer.name.asc()).all()
    return render_template("customers/list.html", customers=customers)
