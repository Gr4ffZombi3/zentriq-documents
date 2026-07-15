from flask import Blueprint, render_template
from flask_login import current_user, login_required

from app.services.bestand import get_bestand
from app.services.wiedervorlagen import sweep_offer_wiedervorlagen

bestand_bp = Blueprint("bestand", __name__, url_prefix="/bestand")


@bestand_bp.route("")
@login_required
def index():
    sweep_offer_wiedervorlagen()
    bestand = get_bestand(current_user.id)
    return render_template("bestand/index.html", bestand=bestand)
