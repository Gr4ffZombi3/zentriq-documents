"""Interner 1:1-Teamchat des Bueros (app/services/team_chat.py).

- TeamChat: genau eine Unterhaltung je Paar im selben Buero (user_low_id < user_high_id, eindeutig).
  Lesestand je Teilnehmer als ID der zuletzt gelesenen Nachricht.
- TeamChatMessage: Textnachricht; client_ref verhindert doppeltes Speichern bei erneutem Senden."""

from datetime import datetime, timezone

from app.extensions import db
from app.tenancy import TenantScopedMixin


def _now():
    return datetime.now(timezone.utc)


class TeamChat(TenantScopedMixin, db.Model):
    __tablename__ = "team_chats"
    __table_args__ = (db.UniqueConstraint("tenant_id", "user_low_id", "user_high_id", name="uq_team_chats_pair"),)

    id = db.Column(db.Integer, primary_key=True)
    user_low_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    user_high_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    low_read_id = db.Column(db.Integer, nullable=False, default=0, server_default="0")
    high_read_id = db.Column(db.Integer, nullable=False, default=0, server_default="0")
    last_message_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=_now)

    def __repr__(self):
        return f"<TeamChat {self.id} {self.user_low_id}-{self.user_high_id}>"


class TeamChatMessage(TenantScopedMixin, db.Model):
    __tablename__ = "team_chat_messages"
    __table_args__ = (
        db.UniqueConstraint("sender_id", "client_ref", name="uq_team_chat_messages_client_ref"),
        db.Index("ix_team_chat_messages_chat_id_id", "chat_id", "id"),
    )

    id = db.Column(db.Integer, primary_key=True)
    chat_id = db.Column(db.Integer, db.ForeignKey("team_chats.id", ondelete="CASCADE"), nullable=False)
    sender_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    text = db.Column(db.Text, nullable=False)
    client_ref = db.Column(db.String(64), nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=_now)

    def __repr__(self):
        return f"<TeamChatMessage {self.id} chat={self.chat_id}>"
