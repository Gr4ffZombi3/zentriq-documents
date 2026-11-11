"""Webhook fuer Webex Calling (Anruferkennung). Oeffentlich erreichbar, ohne Anmeldung und ohne
CSRF - abgesichert ueber die HMAC-Signatur der Webhook-Registrierung (app/services/telephony.py)."""

import json
import logging

from flask import Blueprint, current_app, jsonify, request

from app.extensions import csrf, db
from app.services import telephony

log = logging.getLogger(__name__)

telephony_bp = Blueprint("telephony", __name__, url_prefix="/telefonie")


@telephony_bp.post("/webex/webhook")
@csrf.exempt
def webex_webhook():
    secret = current_app.config.get("WEBEX_WEBHOOK_SECRET") or ""
    if not secret:
        return jsonify({"error": "not_configured"}), 503
    if (request.content_length or 0) > telephony.MAX_BODY_BYTES:
        return jsonify({"error": "too_large"}), 413
    body = request.get_data(cache=False)
    if len(body) > telephony.MAX_BODY_BYTES:
        return jsonify({"error": "too_large"}), 413
    if not telephony.verify_signature(secret, body, request.headers):
        log.warning("telefonie.webhook signature_rejected")
        return jsonify({"error": "signature"}), 401
    try:
        payload = json.loads(body)
    except (UnicodeDecodeError, ValueError):
        return jsonify({"error": "invalid"}), 400
    try:
        outcome = telephony.handle_event(payload)
    except Exception as exc:  # noqa: BLE001
        db.session.rollback()
        # Nur der Fehlertyp: Meldungstexte (z. B. SQL-Parameter) koennten die Rufnummer enthalten.
        # Webex wiederholt die Zustellung.
        log.error("telefonie.webhook failed error=%s", type(exc).__name__)
        return jsonify({"error": "failed"}), 500
    if outcome.result.startswith("ignored:") or outcome.result == "invalid":
        log.info("telefonie.webhook %s", outcome.result)
    return jsonify({"result": outcome.result}), outcome.status
