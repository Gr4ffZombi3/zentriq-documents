"""Memo: Sprachnachricht hochladen -> Transkript anzeigen -> Text kopieren.

Nur fuer Buero-Admins (Rollen-Hook in app/auth/permissions.py und @admin_required). Es wird
nichts gespeichert und keine Folgeaktion ausgeloest."""

from datetime import datetime, timezone

from flask import Blueprint, current_app, jsonify, redirect, render_template, request, url_for
from flask_login import login_required

from app.auth.permissions import admin_required
from app.services.memo import MemoError, transcribe_audio, validate_audio

dashboard_bp = Blueprint("dashboard", __name__)


@dashboard_bp.get("/sprachnachrichten")
@login_required
@admin_required
def index():
    return render_template("dashboard/index.html")


@dashboard_bp.post("/sprachnachrichten/transkribieren")
@login_required
@admin_required
def transcribe():
    wants_json = request.accept_mimetypes.best == "application/json"
    upload = request.files.get("file")
    filename = upload.filename if upload else None
    try:
        content = upload.read() if upload else b""
        filename = validate_audio(filename, content)
        transcript = transcribe_audio(filename, content)
        if not transcript:
            raise MemoError("In der Aufnahme wurde keine Sprache erkannt.")
    except MemoError as exc:
        return _error(str(exc), 400, wants_json, filename)
    except Exception:
        current_app.logger.exception("memo.transcription.failed")
        return _error("Die Transkription ist fehlgeschlagen. Bitte erneut versuchen.", 502, wants_json, filename)

    uploaded_at = datetime.now(timezone.utc)
    if wants_json:
        return jsonify({"transcript": transcript, "filename": filename, "uploaded_at": uploaded_at.isoformat()})
    result = {"transcript": transcript, "filename": filename, "uploaded_at": uploaded_at}
    return render_template("dashboard/index.html", result=result)


def _error(message: str, status: int, wants_json: bool, filename: str | None):
    if wants_json:
        return jsonify({"error": message}), status
    return render_template("dashboard/index.html", error=message, filename=filename), status


@dashboard_bp.get("/mailbox")
@login_required
@admin_required
def legacy_mailbox():
    # Alte Lesezeichen auf die fruehere Mailbox-Uebersicht.
    return redirect(url_for("dashboard.index"))
