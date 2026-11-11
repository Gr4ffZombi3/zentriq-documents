"""Anfrage von der Willkommensseite: neues Buero einrichten oder Demo anfragen.

Es gibt keine Selbstregistrierung und keine oeffentliche Demo - Interessenten hinterlassen eine
Anfrage, der Plattformbetreiber (SUPER_ADMIN) sieht sie unter Plattform -> Anfragen und meldet
sich. Kein Mandant: die Anfrage gehoert zu keinem Buero. Die IP-Adresse dient nur der Begrenzung
wiederholter Absendungen."""

from datetime import datetime, timezone

from app.extensions import db


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


class AccessRequest(db.Model):
    __tablename__ = "access_requests"

    KIND_LABELS = {"buero": "Neues Büro", "demo": "Demo"}

    id = db.Column(db.Integer, primary_key=True)
    kind = db.Column(db.String(10), nullable=False)
    office_name = db.Column(db.String(200), nullable=False)
    contact_name = db.Column(db.String(200), nullable=False)
    email = db.Column(db.String(255), nullable=False)
    phone = db.Column(db.String(50), nullable=True)
    message = db.Column(db.Text, nullable=True)
    ip_address = db.Column(db.String(64), nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=_now, index=True)

    @property
    def kind_label(self) -> str:
        return self.KIND_LABELS.get(self.kind, self.kind)
