"""ZentriqAI: Modell-/Anbieterkonfiguration, Gespraeche, Nachrichten und Verbrauch.

- AiProviderSetting / AiModel sind Plattform-Einstellungen (nicht mandantengebunden) und nur
  ueber den SUPER_ADMIN aenderbar. API-Schluessel liegen NIE in der Datenbank, sondern
  ausschliesslich in der Server-Umgebung (.env).
- AiConversation / AiMessage / AiUsage sind mandanten- und benutzergebunden. AiUsage enthaelt
  keine Inhalte und bleibt fuer die Kostenrechnung erhalten, wenn ein Gespraech geloescht wird.
"""

from datetime import datetime, timezone

from app.extensions import db
from app.tenancy import TenantScopedMixin


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


class AiProviderSetting(db.Model):
    __tablename__ = "ai_provider_settings"

    provider = db.Column(db.String(40), primary_key=True)
    enabled = db.Column(db.Boolean, nullable=False, default=True, server_default=db.true())
    # Ausdrueckliche Freigabe fuer Kundendaten (Auftragsverarbeitung geprueft). Standard: aus.
    customer_data_allowed = db.Column(db.Boolean, nullable=False, default=False, server_default=db.false())
    updated_at = db.Column(db.DateTime, nullable=False, default=_now, onupdate=_now)


class AiModel(db.Model):
    __tablename__ = "ai_models"
    __table_args__ = (db.UniqueConstraint("provider", "model_id", name="uq_ai_models_provider_model"),)

    id = db.Column(db.Integer, primary_key=True)
    provider = db.Column(db.String(40), nullable=False, index=True)
    model_id = db.Column(db.String(150), nullable=False)
    label = db.Column(db.String(150), nullable=False)
    enabled = db.Column(db.Boolean, nullable=False, default=False, server_default=db.false())
    # Kleiner Wert = frueher in der automatischen Auswahl.
    priority = db.Column(db.Integer, nullable=False, default=100, server_default="100")
    # Kostenloses Kontingent des Anbieters (Kosten werden mit 0 verbucht, Tokens trotzdem gezaehlt).
    is_free = db.Column(db.Boolean, nullable=False, default=False, server_default=db.false())
    # USD je 1 Mio. Tokens. Kostenpflichtige Modelle ohne Preis werden nie automatisch gewaehlt.
    input_price = db.Column(db.Numeric(10, 4), nullable=True)
    output_price = db.Column(db.Numeric(10, 4), nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=_now)

    @property
    def ref(self) -> str:
        return f"{self.provider}:{self.model_id}"

    @property
    def has_price(self) -> bool:
        return self.is_free or (self.input_price is not None and self.output_price is not None)


class AiConversation(TenantScopedMixin, db.Model):
    __tablename__ = "ai_conversations"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    title = db.Column(db.String(200), nullable=False, default="Neue Unterhaltung")
    # "auto" oder "<anbieter>:<modell>"
    model_choice = db.Column(db.String(200), nullable=False, default="auto", server_default="auto")
    # Kundendaten-Modus: nur fuer Kundendaten freigegebene Anbieter.
    customer_data = db.Column(db.Boolean, nullable=False, default=False, server_default=db.false())
    # In der Seitenleiste unter "Gespeichert" angeheftet.
    pinned = db.Column(db.Boolean, nullable=False, default=False, server_default=db.false())
    # Optionaler Projektname: Zusammenfassungen anderer Gespraeche desselben Projekts dienen als Kontext.
    project = db.Column(db.String(100), nullable=True, index=True)
    # Laufende Zusammenfassung aelterer Nachrichten (spart Tokens bei langen Gespraechen).
    summary = db.Column(db.Text, nullable=True)
    summary_upto_id = db.Column(db.Integer, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=_now)
    updated_at = db.Column(db.DateTime, nullable=False, default=_now, index=True)

    messages = db.relationship(
        "AiMessage",
        backref="conversation",
        # Loeschen ueber das ORM (nicht nur per ON DELETE CASCADE), damit Nachrichten auch ohne
        # erzwungene Fremdschluessel sicher mitgeloescht werden.
        cascade="all, delete-orphan",
        order_by="AiMessage.id",
    )


class AiMessage(TenantScopedMixin, db.Model):
    __tablename__ = "ai_messages"

    id = db.Column(db.Integer, primary_key=True)
    conversation_id = db.Column(
        db.Integer, db.ForeignKey("ai_conversations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    role = db.Column(db.String(20), nullable=False)  # "user" | "assistant"
    content = db.Column(db.Text, nullable=False, default="")
    # Antworten entstehen asynchron im Celery-Worker: pending -> done | error.
    status = db.Column(db.String(20), nullable=False, default="done", server_default="done")
    error = db.Column(db.String(500), nullable=True)
    # Ausgefuehrte Werkzeuge (nur Namen), z. B. ["meine_arbeitszeiten"].
    tools_used = db.Column(db.JSON, nullable=True)
    provider = db.Column(db.String(40), nullable=True)
    model = db.Column(db.String(150), nullable=True)
    input_tokens = db.Column(db.Integer, nullable=True)
    output_tokens = db.Column(db.Integer, nullable=True)
    cost_usd = db.Column(db.Numeric(12, 6), nullable=True)
    cached = db.Column(db.Boolean, nullable=False, default=False, server_default=db.false())
    created_at = db.Column(db.DateTime, nullable=False, default=_now)


class AiAttachment(TenantScopedMixin, db.Model):
    """Datei zu einer Benutzernachricht. Gespeichert wird nur der extrahierte Text (bzw. das
    Transkript), nie die Datei selbst; geloescht mit der Nachricht."""

    __tablename__ = "ai_attachments"

    id = db.Column(db.Integer, primary_key=True)
    message_id = db.Column(db.Integer, db.ForeignKey("ai_messages.id", ondelete="CASCADE"), nullable=False, index=True)
    filename = db.Column(db.String(255), nullable=False)
    kind = db.Column(db.String(20), nullable=False)  # document | audio
    size_bytes = db.Column(db.Integer, nullable=False, default=0)
    text = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=_now)

    message = db.relationship("AiMessage", backref=db.backref("attachments", cascade="all, delete-orphan", order_by="AiAttachment.id"))


class AiUsage(TenantScopedMixin, db.Model):
    """Ein Eintrag je Anbieter-Aufruf (auch fehlgeschlagene) - ohne Inhalte."""

    __tablename__ = "ai_usage"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    conversation_id = db.Column(
        db.Integer, db.ForeignKey("ai_conversations.id", ondelete="SET NULL"), nullable=True, index=True
    )
    provider = db.Column(db.String(40), nullable=False)
    model = db.Column(db.String(150), nullable=False)
    purpose = db.Column(db.String(20), nullable=False, default="chat")  # chat | summary
    input_tokens = db.Column(db.Integer, nullable=False, default=0)
    output_tokens = db.Column(db.Integer, nullable=False, default=0)
    cost_usd = db.Column(db.Numeric(12, 6), nullable=False, default=0)
    ok = db.Column(db.Boolean, nullable=False, default=True)
    error_type = db.Column(db.String(40), nullable=True)
    latency_ms = db.Column(db.Integer, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=_now, index=True)
