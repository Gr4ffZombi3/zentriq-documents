"""Memo: Sprachnachricht hochladen -> Transkript anzeigen -> Text kopieren.

Fuer alle Buero-Rollen (OFFICE_ADMIN, EMPLOYEE); SUPER_ADMIN hat keinen Zugriff (Rollen-Hook in
app/auth/permissions.py und @office_member_required). Audiodatei und Transkript werden nicht
gespeichert. Der Kundenabgleich laeuft als eigene, nachgelagerte Anfrage, damit das Transkript
sofort angezeigt wird."""

from datetime import datetime, timezone
from functools import wraps

from flask import Blueprint, abort, current_app, jsonify, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from app.auth.permissions import admin_required
from app.models.audit_log import AuditEventType
from app.services.audit import log_audit_event
from app.services.memo import MemoError, transcribe_audio, validate_audio
from app.services.memo_customer_match import format_phone, match_customer

dashboard_bp = Blueprint("dashboard", __name__)

# Obergrenze fuer den Text des Kundenabgleichs (ein Transkript von 25 MB Audio bleibt deutlich darunter).
MAX_TRANSCRIPT_CHARS = 20000


def office_member_required(view):
    """Nur Mitglieder eines Bueros - der Plattformbetreiber sieht keine Buero-Inhalte."""

    @wraps(view)
    def wrapped(*args, **kwargs):
        if not (current_user.is_office_admin or current_user.is_employee):
            abort(403)
        return view(*args, **kwargs)

    return wrapped


@dashboard_bp.get("/sprachnachrichten")
@login_required
@office_member_required
def index():
    return render_template("dashboard/index.html")


@dashboard_bp.post("/sprachnachrichten/transkribieren")
@login_required
@office_member_required
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

    # Aktivitaetsprotokoll: nur das Ereignis - weder Text noch Dateiname.
    log_audit_event(AuditEventType.MEMO_TRANSCRIBED, user=current_user)
    uploaded_at = datetime.now(timezone.utc)
    if wants_json:
        return jsonify({"transcript": transcript, "filename": filename, "uploaded_at": uploaded_at.isoformat()})
    # Ohne JavaScript gibt es keine Nachlade-Anfrage: Abgleich direkt mitliefern.
    result = {"transcript": transcript, "filename": filename, "uploaded_at": uploaded_at}
    return render_template("dashboard/index.html", result=result, match=customer_match_payload(transcript))


@dashboard_bp.post("/sprachnachrichten/kundenabgleich")
@login_required
@office_member_required
def match():
    payload = request.get_json(silent=True) or {}
    transcript = payload.get("transcript") if isinstance(payload, dict) else None
    if not isinstance(transcript, str) or not transcript.strip():
        return jsonify({"error": "Kein Text für den Abgleich übergeben."}), 400
    try:
        return jsonify(customer_match_payload(transcript[:MAX_TRANSCRIPT_CHARS]))
    except Exception:
        current_app.logger.exception("memo.customer_match.failed")
        return jsonify({"error": "Der Kundenabgleich ist derzeit nicht möglich."}), 500


def customer_match_payload(transcript: str) -> dict:
    result = match_customer(transcript)
    # Kundendetailseiten sind Buero-Admins vorbehalten - Mitarbeiter bekommen keinen Link.
    can_open = current_user.is_office_admin
    return {
        "status": result.status,
        "matched_by": result.matched_by,
        "detected_phones": [format_phone(phone) for phone in result.phones],
        "customers": [
            {
                "id": customer.id,
                "name": customer.name,
                "customer_number": customer.customer_number,
                "phone": customer.phone,
                "city": customer.city,
                "url": url_for("customers.detail", customer_id=customer.id) if can_open else None,
            }
            for customer in result.customers
        ],
    }


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
