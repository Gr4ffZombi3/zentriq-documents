from datetime import datetime, timezone

from app.extensions import db
from app.models.enums import AnalysisRunStatus
from app.tenancy import TenantScopedMixin


class AnalysisRun(TenantScopedMixin, db.Model):
    """Ein Verarbeitungsversuch eines Dokuments. Anders als die Document-Spalten (die bei
    jedem Reprocessing an Ort und Stelle ueberschrieben werden) bleibt hier JEDER Versuch als
    eigene Zeile erhalten - das ist die "Analyse-Historie". `summary` (der Analysebericht)
    ist 1:1 an einen abgeschlossenen Lauf gebunden, deshalb keine eigene Tabelle dafuer."""

    __tablename__ = "analysis_runs"

    id = db.Column(db.Integer, primary_key=True)
    document_id = db.Column(db.Integer, db.ForeignKey("documents.id"), nullable=False, index=True)

    engine_version = db.Column(db.String(50), nullable=False)
    prompt_version = db.Column(db.String(50), nullable=False)
    openai_model = db.Column(db.String(100), nullable=True)

    status = db.Column(db.Enum(AnalysisRunStatus), nullable=False, default=AnalysisRunStatus.RUNNING, index=True)
    started_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    finished_at = db.Column(db.DateTime, nullable=True)
    duration_ms = db.Column(db.Integer, nullable=True)
    stage_durations = db.Column(db.JSON, nullable=True)
    error_message = db.Column(db.Text, nullable=True)

    overall_confidence = db.Column(db.Float, nullable=True)
    summary = db.Column(db.JSON, nullable=True)

    document = db.relationship("Document", back_populates="analysis_runs")

    def __repr__(self):
        return f"<AnalysisRun {self.id} document_id={self.document_id} status={self.status}>"
