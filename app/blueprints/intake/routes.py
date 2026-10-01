"""Universal-Upload: eine Ablage fuer Leipziger Listen und Sprachnachrichten.

Die Seite erkennt den Dateityp (app/services/file_intake.py) und uebergibt die Datei danach an
die bestehenden Endpunkte (/upload bzw. Memo-Transkription) - inklusive deren Rechtepruefung.
Nur fuer Buero-Mitglieder; Mitarbeiter koennen nur Sprachnachrichten verarbeiten."""

from flask import Blueprint, abort, jsonify, render_template, request
from flask_login import current_user, login_required

from app.services.file_intake import detect
from app.services.memo import ALLOWED_AUDIO_EXTENSIONS

intake_bp = Blueprint("intake", __name__, url_prefix="/hochladen")


@intake_bp.before_request
@login_required
def _office_members_only():
    if not (current_user.is_office_admin or current_user.is_employee):
        abort(403)
    return None


@intake_bp.get("")
def index():
    return render_template("intake/index.html", audio_extensions=sorted(ALLOWED_AUDIO_EXTENSIONS))


@intake_bp.post("/pruefen")
def check():
    upload = request.files.get("file")
    if upload is None or not upload.filename:
        return jsonify({"error": "Bitte eine Datei auswählen."}), 400
    result = detect(upload.filename, upload.read(), can_import_lists=current_user.is_office_admin)
    return jsonify({"kind": result.kind, "label": result.label, "allowed": result.allowed})
