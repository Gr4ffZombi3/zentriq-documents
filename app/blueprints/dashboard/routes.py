from flask import Blueprint, render_template

from app.services.stats import get_dashboard_stats, get_open_recommendations

dashboard_bp = Blueprint("dashboard", __name__)


@dashboard_bp.route("/")
def index():
    stats = get_dashboard_stats()
    recommendations = get_open_recommendations()
    return render_template("dashboard/index.html", stats=stats, recommendations=recommendations)
