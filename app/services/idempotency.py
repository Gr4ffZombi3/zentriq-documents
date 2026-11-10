"""Wiederholte Anfragen ohne doppelte Wirkung (Termine, Notizen, Importe, ...).

Ablauf (gilt fuer jede schreibende Anfrage eines angemeldeten Benutzers mit Schluessel):

1. Der Browser (js/session-guard.js) schickt einen zufaelligen Schluessel im Kopf
   `Idempotency-Key` bzw. bei klassischen Formularen im Feld `_idem`. Dieselbe Aktion behaelt
   ihren Schluessel, bis eine eindeutige Antwort angekommen ist - also auch ueber einen
   Verbindungsabbruch, eine erneute Anmeldung oder einen Doppelklick hinweg.
2. Vor der Ausfuehrung legt der Server eine Quittung `pending` an (eindeutig je Benutzer und
   Schluessel). Kommt dieselbe Anfrage parallel ein zweites Mal, antwortet er 409 `in_progress`.
3. Nach der Ausfuehrung speichert er die Antwort (2xx/3xx). Jede Wiederholung bekommt genau diese
   Antwort mit dem Kopf `Idempotent-Replay: true`, ohne dass die Aktion erneut laeuft.
   Fehlerantworten (4xx/5xx) werden nicht gespeichert: Nach einer Korrektur darf erneut
   gespeichert werden.
4. `GET /auth/aktion/<key>` meldet, ob eine Aktion bereits gespeichert wurde.

Die CSRF-Pruefung laeuft vorher unveraendert; Rechte prueft jeder Endpunkt wie bisher selbst.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from flask import Response, current_app, g, request
from flask_login import current_user
from sqlalchemy.exc import IntegrityError

from app.extensions import db
from app.models.request_receipt import RequestReceipt

HEADER = "Idempotency-Key"
FORM_FIELD = "_idem"
UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
KEY_PATTERN = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
# Eine Anfrage, die so lange "pending" bleibt, ist abgebrochen (Worker-Neustart o. Ae.):
# Sie darf erneut ausgefuehrt werden. Deutlich ueber dem Gunicorn-Timeout (30 s).
STALE_PENDING = timedelta(minutes=2)
RETENTION = timedelta(hours=24)
MAX_BODY = 256 * 1024


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _request_key() -> str | None:
    key = request.headers.get(HEADER)
    if not key and request.mimetype in ("application/x-www-form-urlencoded", "multipart/form-data"):
        key = request.form.get(FORM_FIELD)
    if key and KEY_PATTERN.match(key):
        return key
    return None


def _replay(receipt: RequestReceipt) -> Response:
    body = receipt.body
    if body is None:
        # Antwort war zu gross zum Aufheben: nur den Erfolg bestaetigen.
        body = '{"ok": true, "replay": true}' if "json" in (receipt.content_type or "json") else ""
    response = Response(body, status=receipt.status_code or 200,
                        content_type=receipt.content_type or "application/json")
    if receipt.location:
        response.headers["Location"] = receipt.location
    response.headers["Idempotent-Replay"] = "true"
    return response


def _json_error(message: str, code: str, status: int) -> Response:
    response = current_app.response_class(
        current_app.json.dumps({"error": message, "code": code}), status=status, mimetype="application/json"
    )
    return response


def _purge_old(user_id: int) -> None:
    RequestReceipt.query.filter(RequestReceipt.user_id == user_id,
                                RequestReceipt.created_at < _now() - RETENTION).delete(synchronize_session="fetch")


def before_request():
    """Liefert bei einer Wiederholung die gespeicherte Antwort, sonst None (normal ausfuehren)."""
    if request.method not in UNSAFE_METHODS or not current_user.is_authenticated:
        return None
    key = _request_key()
    if key is None:
        return None
    user_id = current_user.id
    path = request.path[:255]
    receipt = RequestReceipt.query.filter_by(user_id=user_id, key=key).first()
    if receipt is not None:
        if receipt.method != request.method or receipt.path != path:
            return _json_error("Dieser Vorgang gehört zu einer anderen Aktion. Bitte die Seite neu laden.",
                               "idempotency_mismatch", 422)
        if receipt.state == "done":
            return _replay(receipt)
        if _now() - receipt.created_at < STALE_PENDING:
            return _json_error("Diese Aktion wird gerade gespeichert. Bitte einen Moment warten.", "in_progress", 409)
        # Abgebrochener Versuch: neu beginnen.
        db.session.delete(receipt)
        db.session.commit()
    try:
        _purge_old(user_id)
        receipt = RequestReceipt(user_id=user_id, key=key, method=request.method, path=path, state="pending")
        db.session.add(receipt)
        db.session.commit()
    except IntegrityError:
        # Dieselbe Anfrage kam in diesem Moment parallel an.
        db.session.rollback()
        return _json_error("Diese Aktion wird gerade gespeichert. Bitte einen Moment warten.", "in_progress", 409)
    g._receipt_id = receipt.id
    return None


def _is_text(mimetype: str | None) -> bool:
    # Nur Antworten, die sich verlustfrei als Text aufheben lassen (JSON, HTML, Text).
    # Dateien (PDF, Excel, ...) werden nicht gespeichert; dort entfaellt der Schutz.
    mimetype = mimetype or ""
    return mimetype.startswith("text/") or mimetype.endswith("json")


def after_request(response: Response) -> Response:
    receipt_id = g.pop("_receipt_id", None)
    if receipt_id is None:
        return response
    try:
        # Offene Aenderungen der Anfrage (Fehlerfall) nicht versehentlich mit speichern.
        if db.session.new or db.session.dirty or db.session.deleted:
            db.session.rollback()
        receipt = db.session.get(RequestReceipt, receipt_id)
        if receipt is None:
            return response
        storable = (
            response.status_code < 400
            and not response.is_streamed
            and not response.direct_passthrough
            and (300 <= response.status_code < 400 or _is_text(response.mimetype))
        )
        if storable:
            body = response.get_data(as_text=True)
            receipt.state = "done"
            receipt.status_code = response.status_code
            receipt.content_type = (response.content_type or "")[:100]
            receipt.location = (response.headers.get("Location") or "")[:500] or None
            receipt.body = body if len(body) <= MAX_BODY else None
            receipt.completed_at = _now()
        else:
            db.session.delete(receipt)
        db.session.commit()
    except Exception:  # pragma: no cover - die eigentliche Antwort darf nie daran scheitern
        db.session.rollback()
        current_app.logger.exception("idempotency.store_failed")
        try:  # keine haengende "pending"-Quittung zuruecklassen
            RequestReceipt.query.filter_by(id=receipt_id).delete(synchronize_session=False)
            db.session.commit()
        except Exception:
            db.session.rollback()
    return response


def status_for(user_id: int, key: str) -> dict:
    if not KEY_PATTERN.match(key or ""):
        return {"status": "unknown"}
    receipt = RequestReceipt.query.filter_by(user_id=user_id, key=key).first()
    if receipt is None:
        return {"status": "unknown"}
    if receipt.state == "done":
        return {"status": "done", "status_code": receipt.status_code}
    if _now() - receipt.created_at >= STALE_PENDING:
        return {"status": "unknown"}
    return {"status": "pending"}
