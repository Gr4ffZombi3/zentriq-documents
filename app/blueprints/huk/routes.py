from flask import Blueprint

huk_bp = Blueprint("huk", __name__, url_prefix="/kfz-angebot")


@huk_bp.get("")
def index():
    return "in Arbeit"
