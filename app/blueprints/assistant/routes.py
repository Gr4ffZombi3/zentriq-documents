"""Textassistent (Werkzeuge -> Assistent): Auftrag bzw. Text eingeben -> fertiger Text ->
kopieren oder ueberarbeiten (kuerzer, freundlicher, professioneller, neu formulieren).

Zugriff nur fuer Buero-Admin und Mitarbeiter eines Bueros, fuer das der Assistent freigegeben
ist (Tenant.assistant_enabled, app.navigation.assistant_allowed). Geprueft wird serverseitig fuer
Seite UND API - alle anderen (fremde Bueros, SUPER_ADMIN) erhalten 403. Reines Textwerkzeug:
gesendet wird ausschliesslich der eingegebene Text (app/services/assistant.py), gespeichert wird
nichts davon, und es werden keine Aktionen in Zentriq ausgefuehrt."""

from functools import wraps

from flask import Blueprint, abort, current_app, jsonify, render_template, request
from flask_login import current_user, login_required

from app.models.audit_log import AuditEventType
from app.navigation import assistant_allowed
from app.services import assistant
from app.services.audit import log_audit_event

assistant_bp = Blueprint("assistant", __name__, url_prefix="/assistent")


def assistant_access_required(view):
    @wraps(view)
    @login_required
    def wrapped(*args, **kwargs):
        if not assistant_allowed(current_user):
            abort(403)
        return view(*args, **kwargs)

    return wrapped


@assistant_bp.get("")
@assistant_access_required
def index():
    return render_template(
        "assistant/index.html",
        available=assistant.is_enabled(),
        unavailable_message=assistant.UNAVAILABLE_MESSAGE,
        refine_actions=[(key, assistant.ACTIONS[key].label) for key in assistant.REFINE_ACTIONS],
        max_chars=assistant.max_input_chars(),
    )


@assistant_bp.post("/anfrage")
@assistant_access_required
def generate():
    payload = request.get_json(silent=True)
    payload = payload if isinstance(payload, dict) else {}
    action = payload.get("action") if isinstance(payload.get("action"), str) else None
    text = payload.get("text") if isinstance(payload.get("text"), str) else None
    try:
        result = assistant.generate(action, text)
    except assistant.AssistantError as exc:
        # Nur die Fehlerart - nie Eingabe oder Antwort.
        if exc.status >= 500 or exc.error_type == "refusal":
            current_app.logger.warning("assistant.failed type=%s", exc.error_type)
            log_audit_event(
                AuditEventType.ASSISTANT_USED,
                user=current_user,
                details={"action": action if action in assistant.ACTIONS else None, "ok": False, "error": exc.error_type},
            )
        return jsonify({"error": str(exc)}), exc.status
    log_audit_event(AuditEventType.ASSISTANT_USED, user=current_user, details={"action": action, "ok": True})
    return jsonify({"result": result})
