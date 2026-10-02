"""Memo: Sprachnachricht hochladen -> Transkript anzeigen -> Text kopieren -> Kunde zuordnen.

Fuer alle Buero-Rollen (OFFICE_ADMIN, EMPLOYEE); SUPER_ADMIN hat keinen Zugriff (Rollen-Hook in
app/auth/permissions.py und @office_member_required). Die Audiodatei wird nie gespeichert, das
Transkript nur, wenn es einem Kunden zugeordnet wird (app/services/memo_customers.py). Der
Kundenabgleich laeuft als eigene, nachgelagerte Anfrage, damit das Transkript sofort angezeigt
wird.

Transkription (lokal, app/services/memo.py): Mit JavaScript wird die Datei an den Celery-Worker
uebergeben (202 + signierte Status-URL), der Browser fragt den Stand ab - so scheitern auch
laengere Aufnahmen nicht am Proxy-Timeout. Ohne JavaScript wird synchron transkribiert."""

import time
from datetime import datetime, timezone
from functools import wraps

from flask import Blueprint, abort, current_app, jsonify, redirect, render_template, request, url_for
from flask_login import current_user, login_required
from itsdangerous import BadSignature, URLSafeTimedSerializer

from app.auth.permissions import admin_required
from app.extensions import db
from app.models import Customer
from app.models.audit_log import AuditEventType
from app.services.audit import log_audit_event
from app.services.memo import (
    ALLOWED_AUDIO_EXTENSIONS,
    MemoError,
    remove_temp_audio,
    store_temp_audio,
    transcribe_audio,
    validate_audio,
)
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


@dashboard_bp.context_processor
def _audio_extensions():
    # Dieselbe Liste wie die serverseitige Pruefung (Dateiauswahl, Hinweis, memo.js).
    return {"audio_extensions": sorted(ALLOWED_AUDIO_EXTENSIONS)}

JOB_SALT = "zentriq-memo-job"
# Laenger als jede Transkription dauern darf (TRANSCRIPTION_TIMEOUT_SECONDS + Warteschlange).
JOB_MAX_AGE_SECONDS = 3600
TRANSCRIPTION_FAILED = "Die Transkription ist auf dem Server fehlgeschlagen. Bitte erneut versuchen."
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
    content = upload.read() if upload else b""
    try:
        filename = validate_audio(filename, content)
    except MemoError as exc:
        return _error(str(exc), 400, wants_json, filename)
    try:
        if not wants_json:
            # Ohne JavaScript gibt es keine Statusabfrage: direkt transkribieren.
            transcript = transcribe_audio(filename, content)
            log_audit_event(AuditEventType.MEMO_TRANSCRIBED, user=current_user)
            result = {"transcript": transcript, "filename": filename, "uploaded_at": datetime.now(timezone.utc)}
            # Abgleich direkt mitliefern.
            return render_template("dashboard/index.html", result=result, match=customer_match_payload(transcript, trusted=True))
        job = _enqueue_transcription(filename, content)
    except MemoError as exc:
        return _error(str(exc), 422, wants_json, filename)
    except Exception:
        current_app.logger.exception("memo.transcription.failed")
        return _error(TRANSCRIPTION_FAILED, 502, wants_json, filename)

    if job.ready():
        # Eager-Modus (Tests/Entwicklung): Ergebnis liegt bereits vor.
        return _job_response(job, filename)
    token = _job_serializer().dumps({"t": job.id, "u": current_user.id, "f": filename, "s": int(time.time())})
    return jsonify({"status": "pending", "status_url": url_for("dashboard.transcription_status", job=token)}), 202


@dashboard_bp.post("/sprachnachrichten/diagnose")
@login_required
@office_member_required
def upload_diagnosis():
    """Protokolliert, welche Antwort der Browser auf den Upload erhalten hat, wenn sie nicht
    von diesem Server stammt (z. B. Proxy, Firewall, Virenscanner). Nur Metadaten, kein Inhalt."""
    data = request.get_json(silent=True) or {}

    def clean(key, limit=120):
        value = data.get(key)
        return "".join(ch for ch in str("" if value is None else value)[:limit] if ch.isprintable())

    current_app.logger.warning(
        "memo.upload.foreign_response status=%s redirected=%s url=%s content_type=%s server=%s via=%s size=%s",
        clean("status", 5), clean("redirected", 5), clean("url", 200), clean("content_type"),
        clean("server"), clean("via"), clean("size", 12),
    )
    return "", 204


def _job_serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(current_app.config["SECRET_KEY"], salt=JOB_SALT)


def _enqueue_transcription(filename: str, content: bytes):
    from app.tasks.memo_tasks import transcribe_memo

    path = store_temp_audio(filename, content)
    try:
        return transcribe_memo.delay(path)
    except Exception as exc:
        remove_temp_audio(path)
        current_app.logger.exception("memo.transcription.enqueue_failed")
        raise MemoError("Der Transkriptionsdienst ist gerade nicht erreichbar. Bitte in einer Minute erneut versuchen.") from exc


@dashboard_bp.get("/sprachnachrichten/transkription/<job>")
@login_required
@office_member_required
def transcription_status(job):
    """Stand einer Transkription (nur fuer den Benutzer, der sie gestartet hat)."""
    try:
        data = _job_serializer().loads(job, max_age=JOB_MAX_AGE_SECONDS)
    except BadSignature:
        return _json_error("Diese Transkription ist abgelaufen. Bitte die Datei erneut hochladen.", 404)
    if not isinstance(data, dict) or data.get("u") != current_user.id:
        return _json_error("Diese Transkription ist abgelaufen. Bitte die Datei erneut hochladen.", 404)
    result = current_app.extensions["celery"].AsyncResult(data["t"])
    if not result.ready():
        waited = time.time() - int(data.get("s") or 0)
        limit = int(current_app.config.get("TRANSCRIPTION_TIMEOUT_SECONDS") or 900) + 300
        if waited > limit:
            current_app.logger.error("memo.transcription.stuck seconds=%d", waited)
            return _json_error("Die Transkription wurde nicht abgeschlossen (Hintergrunddienst reagiert nicht). Bitte erneut versuchen.", 504)
        return jsonify({"status": "pending", "waited": int(waited)}), 202
    return _job_response(result, data.get("f") or "")


def _job_response(result, filename: str):
    payload = result.get(timeout=5, propagate=False)
    failed = result.failed() or not isinstance(payload, dict)
    try:
        # Ergebnis nicht laenger als noetig im Result-Backend aufbewahren.
        result.forget()
    except Exception:  # noqa: BLE001 - z. B. EagerResult ohne Backend
        pass
    if failed:
        current_app.logger.error("memo.transcription.task_failed type=%s", type(payload).__name__)
        return _json_error(TRANSCRIPTION_FAILED, 502)
    if payload.get("error"):
        return _json_error(payload["error"], 422)
    transcript = payload["transcript"]
    # Aktivitaetsprotokoll: nur das Ereignis - weder Text noch Dateiname.
    log_audit_event(AuditEventType.MEMO_TRANSCRIBED, user=current_user)
    return jsonify(
        {
            "status": "done",
            "transcript": transcript,
            "filename": filename,
            "uploaded_at": datetime.now(timezone.utc).isoformat(),
            # Berechtigt zum Zuordnen genau dieses Textes (siehe memo_customers.py).
            "token": transcript_token(transcript, current_user),
        }
    )


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
