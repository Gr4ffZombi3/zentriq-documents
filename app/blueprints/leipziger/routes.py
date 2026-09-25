from flask import Blueprint, render_template
from flask_login import login_required

from app.services.leipziger_overview import build_leipziger_overview

leipziger_bp = Blueprint("leipziger", __name__, url_prefix="/leipziger-liste")


@leipziger_bp.route("")
@login_required
def index():
    return render_template("leipziger/index.html", overview=build_leipziger_overview())
