"""Quittung fuer eine schreibende Anfrage mit Idempotenz-Schluessel (app/services/idempotency.py).

Der Browser schickt bei jedem Speichern einen zufaelligen Schluessel mit. Wiederholt er dieselbe
Anfrage (Verbindung abgebrochen, Doppelklick, erneutes Senden nach neuer Anmeldung), liefert der
Server die gespeicherte Antwort statt die Aktion ein zweites Mal auszufuehren. Gespeichert wird
nur, was der Server ohnehin an denselben Benutzer zurueckgeschickt hat; nach 24 Stunden wird die
Quittung verworfen.
"""

from datetime import datetime, timezone

from app.extensions import db


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


class RequestReceipt(db.Model):
    __tablename__ = "request_receipts"
    __table_args__ = (db.UniqueConstraint("user_id", "key", name="uq_request_receipts_user_key"),)

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    key = db.Column(db.String(64), nullable=False)
    method = db.Column(db.String(8), nullable=False)
    path = db.Column(db.String(255), nullable=False)
    # pending = wird gerade ausgefuehrt; done = Antwort gespeichert.
    state = db.Column(db.String(10), nullable=False, default="pending")
    status_code = db.Column(db.Integer, nullable=True)
    content_type = db.Column(db.String(100), nullable=True)
    location = db.Column(db.String(500), nullable=True)
    body = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=_now, index=True)
    completed_at = db.Column(db.DateTime, nullable=True)
