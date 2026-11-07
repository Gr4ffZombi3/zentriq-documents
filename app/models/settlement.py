"""Abrechnungen (Provisions-/Verguetungsabrechnungen) eines Bueros.

Einreichen duerfen Buero-Mitglieder; Originaldatei, Buchungen und Auswertungen sieht
ausschliesslich der Buero-Admin des eigenen Bueros (app/blueprints/settlements/routes.py).
Doppelt eingereichte Belege: gleiche Datei (sha256) wird abgewiesen; gleicher Inhalt (z. B. neu
gescannt) wird als "duplicate" gespeichert und nie gezaehlt."""

from datetime import datetime, timezone

from app.extensions import db
from app.tenancy import TenantScopedMixin


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Settlement(TenantScopedMixin, db.Model):
    __tablename__ = "settlements"
    __table_args__ = (db.UniqueConstraint("tenant_id", "file_sha256", name="uq_settlements_file"),)

    id = db.Column(db.Integer, primary_key=True)
    # processing | done | needs_review | duplicate | failed
    status = db.Column(db.String(20), nullable=False, default="processing", server_default="processing")
    original_filename = db.Column(db.String(255), nullable=False)
    stored_path = db.Column(db.String(255), nullable=False)
    file_sha256 = db.Column(db.String(64), nullable=False)
    content_fingerprint = db.Column(db.String(64), nullable=True, index=True)
    duplicate_of_id = db.Column(db.Integer, db.ForeignKey("settlements.id", ondelete="SET NULL"), nullable=True)
    period_start = db.Column(db.Date, nullable=True, index=True)
    period_end = db.Column(db.Date, nullable=True)
    broker_number = db.Column(db.String(50), nullable=True)
    employee_user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    text_method = db.Column(db.String(20), nullable=True)  # text | ocr | mixed
    page_count = db.Column(db.Integer, nullable=True)
    message = db.Column(db.String(500), nullable=True)
    uploaded_by_user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    created_at = db.Column(db.DateTime, nullable=False, default=_now)
    processed_at = db.Column(db.DateTime, nullable=True)

    employee = db.relationship("User", foreign_keys=[employee_user_id])
    uploaded_by = db.relationship("User", foreign_keys=[uploaded_by_user_id])
    lines = db.relationship("SettlementLine", back_populates="settlement", cascade="all, delete-orphan", order_by="SettlementLine.position")

    @property
    def counts(self) -> bool:
        return self.status in ("done", "needs_review")


class SettlementLine(TenantScopedMixin, db.Model):
    __tablename__ = "settlement_lines"

    id = db.Column(db.Integer, primary_key=True)
    settlement_id = db.Column(db.Integer, db.ForeignKey("settlements.id", ondelete="CASCADE"), nullable=False, index=True)
    position = db.Column(db.Integer, nullable=False, default=0)
    page = db.Column(db.Integer, nullable=False, default=1)
    raw_text = db.Column(db.String(500), nullable=False)
    booking_date = db.Column(db.Date, nullable=True)
    contract_ref = db.Column(db.String(60), nullable=True, index=True)
    sparte = db.Column(db.String(40), nullable=True)
    # abschluss | bestand | storno | korrektur | sonstig
    booking_type = db.Column(db.String(20), nullable=True)
    booking_label = db.Column(db.String(120), nullable=True)
    amount = db.Column(db.Numeric(12, 2), nullable=True)
    is_ten = db.Column(db.Boolean, nullable=False, default=False, server_default=db.false())
    needs_review = db.Column(db.Boolean, nullable=False, default=False, server_default=db.false())
    review_reason = db.Column(db.String(200), nullable=True)
    # Vom Buero-Admin geprueft: "ok" (Angaben bestaetigt/korrigiert) oder "ignored" (zaehlt nicht)
    review_state = db.Column(db.String(20), nullable=True)
    reviewed_by_user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    reviewed_at = db.Column(db.DateTime, nullable=True)

    settlement = db.relationship("Settlement", back_populates="lines")

    @property
    def counts(self) -> bool:
        return self.amount is not None and self.review_state != "ignored" and (not self.needs_review or self.review_state == "ok")
