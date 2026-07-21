from flask import Blueprint, render_template
from flask_login import current_user, login_required

from app.services.dashboard import build_dashboard_view

dashboard_bp = Blueprint("dashboard", __name__)


@dashboard_bp.route("/")
@login_required
def index():
    dashboard = build_dashboard_view(current_user)
    return render_template("dashboard/index.html", dashboard=dashboard)
