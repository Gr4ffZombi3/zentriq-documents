"""Memo: Sprachnachricht hochladen -> Transkript anzeigen -> Text kopieren -> Kunde zuordnen.

Fuer alle Buero-Rollen (OFFICE_ADMIN, EMPLOYEE); SUPER_ADMIN hat keinen Zugriff (Rollen-Hook in
app/auth/permissions.py und @office_member_required). Die Audiodatei wird nie gespeichert, das
Transkript nur, wenn es einem Kunden zugeordnet wird (app/services/memo_customers.py). Der
Kundenabgleich laeuft als eigene, nachgelagerte Anfrage, damit das Transkript sofort angezeigt
wird."""

from datetime import datetime, timezone
from functools import wraps

from flask import Blueprint, abort, current_app, jsonify, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from app.auth.permissions import admin_required
from app.extensions import db
from app.models import Customer
from app.models.audit_log import AuditEventType
from app.services.audit import log_audit_event
from app.services.memo import MemoError, transcribe_audio, validate_audio
from app.services.memo_customer_match import format_phone, match_customer
from app.services.memo_customers import (
    AUTO_MATCH_BASES,
    DuplicateFound,
    MemoAssignmentError,
    assign_memo,
    create_customer_from_memo,
    new_customer_suggestion,
    transcript_token,
    verify_transcript_token,
)
from app.tenancy import get_or_404_scoped

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
        return jsonify(
            {
                "transcript": transcript,
                "filename": filename,
                "uploaded_at": uploaded_at.isoformat(),
                # Berechtigt zum Zuordnen genau dieses Textes (siehe memo_customers.py).
                "token": transcript_token(transcript, current_user),
            }
        )
    # Ohne JavaScript gibt es keine Nachlade-Anfrage: Abgleich direkt mitliefern.
    result = {"transcript": transcript, "filename": filename, "uploaded_at": uploaded_at}
    return render_template("dashboard/index.html", result=result, match=customer_match_payload(transcript, trusted=True))


@dashboard_bp.post("/sprachnachrichten/kundenabgleich")
@login_required
@office_member_required
def match():
    payload = request.get_json(silent=True) or {}
    transcript = payload.get("transcript") if isinstance(payload, dict) else None
    if not isinstance(transcript, str) or not transcript.strip():
        return jsonify({"error": "Kein Text für den Abgleich übergeben."}), 400
    transcript = transcript[:MAX_TRANSCRIPT_CHARS]
    try:
        trusted = verify_transcript_token(payload.get("token"), transcript, current_user)
        return jsonify(customer_match_payload(transcript, trusted=trusted))
    except Exception:
        db.session.rollback()
        current_app.logger.exception("memo.customer_match.failed")
        return jsonify({"error": "Der Kundenabgleich ist derzeit nicht möglich."}), 500


def _verified_transcript() -> tuple[dict, str]:
    """JSON-Anfrage mit Transkript und gueltigem Token dieser Transkription - sonst 400."""
    payload = request.get_json(silent=True)
    payload = payload if isinstance(payload, dict) else {}
    transcript = payload.get("transcript")
    if not isinstance(transcript, str) or not transcript.strip() or len(transcript) > MAX_TRANSCRIPT_CHARS:
        abort(400)
    if not verify_transcript_token(payload.get("token"), transcript, current_user):
        abort(_json_error("Die Sprachnachricht ist abgelaufen. Bitte erneut hochladen.", 400))
    return payload, transcript


def _json_error(message: str, status: int):
    response = jsonify({"error": message})
    response.status_code = status
    return response


@dashboard_bp.post("/sprachnachrichten/zuordnen")
@login_required
@office_member_required
def assign():
    """Memo einem vom Benutzer ausgewaehlten Kunden des eigenen Bueros zuordnen."""
    payload, transcript = _verified_transcript()
    customer_id = payload.get("customer_id")
    if not isinstance(customer_id, int):
        return _json_error("Bitte einen Kunden auswählen.", 400)
    # Fremde oder unbekannte Kunden: 404 (Tenant-Filter).
    customer = get_or_404_scoped(Customer, customer_id)
    try:
        assign_memo(customer, transcript, "selected", current_user)
    except MemoAssignmentError as exc:
        db.session.rollback()
        return _json_error(str(exc), 400)
    return jsonify({"status": "assigned", "customer": _customer_view(customer)})


@dashboard_bp.post("/sprachnachrichten/kunde-anlegen")
@login_required
@office_member_required
def create_customer():
    """Neuen Kunden aus dem Memo anlegen - nur auf ausdrueckliche Bestaetigung und nie, wenn
    Kundennummer oder Telefonnummer schon im Bestand des Bueros vorkommen."""
    payload, transcript = _verified_transcript()

    def text(key):
        value = payload.get(key)
        return value if isinstance(value, str) else None

    try:
        customer, _memo = create_customer_from_memo(
            name=text("name"),
            phone=text("phone"),
            customer_number=text("customer_number"),
            transcript=transcript,
            user=current_user,
            confirm_same_name=payload.get("confirm_same_name") is True,
        )
    except DuplicateFound as conflict:
        db.session.rollback()
        return (
            jsonify(
                {
                    "status": "duplicate",
                    "blocking": conflict.blocking,
                    "customers": [_customer_view(customer) for customer in conflict.customers],
                }
            ),
            409,
        )
    except MemoAssignmentError as exc:
        db.session.rollback()
        return _json_error(str(exc), 400)
    return jsonify({"status": "created", "customer": _customer_view(customer)}), 201


def _customer_view(customer: Customer) -> dict:
    # Kundendetailseiten sind Buero-Admins vorbehalten - Mitarbeiter bekommen keinen Link.
    return {
        "id": customer.id,
        "name": customer.name,
        "customer_number": customer.customer_number,
        "phone": customer.phone,
        "city": customer.city,
        "url": url_for("customers.detail", customer_id=customer.id) if current_user.is_office_admin else None,
    }


def customer_match_payload(transcript: str, *, trusted: bool = False) -> dict:
    """Ergebnis des Kundenabgleichs. `trusted`: der Text stammt nachweislich aus einer
    Transkription - nur dann wird ein eindeutig erkannter Kunde automatisch verknuepft."""
    result = match_customer(transcript)
    assigned = False
    filled: list[str] = []
    if trusted and result.status == "unique" and result.matched_by in AUTO_MATCH_BASES:
        _memo, _created, filled = assign_memo(result.customers[0], transcript, result.matched_by, current_user)
        assigned = True
    return {
        "status": result.status,
        "matched_by": result.matched_by,
        "assigned": assigned,
        "filled_fields": filled,
        "detected_phones": [format_phone(phone) for phone in result.phones],
        "customers": [_customer_view(customer) for customer in result.customers],
        "suggestion": new_customer_suggestion(result, transcript),
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
