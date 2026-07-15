from flask import Blueprint, render_template
from flask_login import current_user, login_required

from app.services.cockpit import get_daily_cockpit
from app.services.wiedervorlagen import sweep_offer_wiedervorlagen

cockpit_bp = Blueprint("cockpit", __name__, url_prefix="/cockpit")


@cockpit_bp.route("")
@login_required
def index():
    sweep_offer_wiedervorlagen()
    cockpit = get_daily_cockpit(current_user.id)
    return render_template("cockpit/index.html", cockpit=cockpit)
