from flask import Blueprint, redirect, url_for
from flask_login import current_user, login_required

from app.navigation import home_endpoint_for

portal_bp = Blueprint("portal", __name__)


@portal_bp.route("/")
@login_required
def home():
    return redirect(url_for(home_endpoint_for(current_user)))
