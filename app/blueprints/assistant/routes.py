"""KI-Assistent (Side-Panel in der Kopfleiste): Text absenden -> Ergebnis -> kopieren.

Fuer alle Buero-Rollen (OFFICE_ADMIN, EMPLOYEE); SUPER_ADMIN hat keinen Zugriff auf den
Assistenten (Rollen-Hook in app/auth/permissions.py und @office_member_required) und sieht im
Plattform-Panel nur technische Kennzahlen. Gesendet wird ausschliesslich der eingegebene Text
(app/services/assistant.py); gespeichert wird nichts davon."""

from flask import Blueprint, current_app, jsonify, request
from flask_login import current_user, login_required

from app.blueprints.dashboard.routes import office_member_required
from app.models.audit_log import AuditEventType
from app.services import assistant
from app.services.audit import log_audit_event

assistant_bp = Blueprint("assistant", __name__, url_prefix="/assistent")


@assistant_bp.post("/anfrage")
@login_required
@office_member_required
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
