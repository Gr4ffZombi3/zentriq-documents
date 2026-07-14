from flask import Blueprint, render_template
from flask_login import login_required

from app.services.stats import get_open_recommendations

recommendations_bp = Blueprint("recommendations", __name__, url_prefix="/recommendations")


@recommendations_bp.route("")
@login_required
def list_recommendations():
    recommendations = get_open_recommendations(limit=None)
    return render_template("recommendations/list.html", recommendations=recommendations)
