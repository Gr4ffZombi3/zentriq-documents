"""Werkzeuge: Hilfsfunktionen, die keine taeglichen Hauptbereiche sind - Assistent, Dokument
anonymisieren, Universal-Upload. Nur fuer Buero-Rollen (SUPER_ADMIN: Default-Deny in
app/auth/permissions.py, zusaetzlich hier abgelehnt).

Dokument anonymisieren laeuft vollstaendig auf dem Server (app/services/anonymize.py): Weder
die Datei noch der Text werden gespeichert oder an einen KI-Dienst uebertragen. Erst wenn der
Benutzer die anonymisierte Fassung geprueft hat, kann er sie kopieren oder selbst in den
Assistenten uebernehmen."""

from flask import Blueprint, abort, render_template, request
from flask_login import current_user, login_required

from app.navigation import assistant_allowed, assistant_available
from app.services.anonymize import (
    IMAGE_EXTENSIONS,
    MAX_INPUT_CHARS,
    PDF_EXTENSIONS,
    UnreadableFileError,
    anonymize,
    extract_text_locally,
    office_known_names,
)

tools_bp = Blueprint("tools", __name__, url_prefix="/werkzeuge")


@tools_bp.before_request
@login_required
def _office_members_only():
    if not (current_user.is_office_admin or current_user.is_employee):
        abort(403)
    return None


@tools_bp.get("")
def index():
    return render_template("tools/index.html", assistant_allowed=assistant_allowed(current_user))


@tools_bp.route("/anonymisieren", methods=["GET", "POST"])
def anonymize_document():
    context = {
        "accept": ",".join(f".{ext}" for ext in sorted(PDF_EXTENSIONS | IMAGE_EXTENSIONS)),
        "max_chars": MAX_INPUT_CHARS,
        "assistant_enabled": assistant_available(current_user),
        "result": None,
        "error": None,
        "source": "",
    }
    if request.method == "POST":
        upload = request.files.get("file")
        text = request.form.get("text", "")
        if upload is not None and upload.filename:
            try:
                text = extract_text_locally(upload.filename, upload.read())
            except UnreadableFileError as exc:
                context["error"] = str(exc)
                return render_template("tools/anonymize.html", **context), 400
            context["source"] = upload.filename
            if not text.strip():
                context["error"] = "In der Datei wurde kein lesbarer Text gefunden."
                return render_template("tools/anonymize.html", **context), 400
        elif not text.strip():
            context["error"] = "Bitte eine Datei auswählen oder Text einfügen."
            return render_template("tools/anonymize.html", **context), 400
        if len(text) > MAX_INPUT_CHARS:
            context["error"] = f"Der Text ist zu lang (höchstens {MAX_INPUT_CHARS:,} Zeichen).".replace(",", ".")
            return render_template("tools/anonymize.html", **context), 400
        context["result"] = anonymize(text, office_known_names(text))
    return render_template("tools/anonymize.html", **context)


@tools_bp.after_request
def _no_store(response):
    # Vorschau enthaelt die erkannten Originalangaben: nicht im Browser-/Proxy-Cache ablegen.
    response.headers["Cache-Control"] = "no-store"
    return response
