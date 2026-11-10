"""Persoenlicher Arbeitsplatz je Benutzer (alle Tabellen mandantengebunden):

- UserPreference: Startansicht, Tagesbegruessung (einmal je Kalendertag nach dem ersten Login)
  und Zeitpunkt, bis zu dem "Was ist neu?" gelesen wurde.
- SavedView: benannte Filterkombination einer Liste. Gespeichert werden nur erlaubte
  Parameter; beim Oeffnen prueft die Zielseite wie immer selbst die Rechte.
- FormDraft: Zwischenstand eines Formulars (zuerst Kfz-Angebot) - nur Eingaben, startet nichts.
- OfficeNews: vom Buero-Admin gepflegte Eintraege fuer "Was ist neu?" (zusaetzlich zu den
  Release-Eintraegen aus app/release_notes.py)."""

from datetime import datetime, timezone

from app.extensions import db
from app.tenancy import TenantScopedMixin


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


class UserPreference(TenantScopedMixin, db.Model):
    __tablename__ = "user_preferences"
    __table_args__ = (db.UniqueConstraint("user_id", name="uq_user_preferences_user"),)

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    # Endpoint aus app/services/workspace.START_VIEWS; leer = Standard (Assistent).
    start_view = db.Column(db.String(64), nullable=True)
    # Kalendertag (Europe/Berlin) des letzten erfolgreichen Logins.
    last_login_day = db.Column(db.Date, nullable=True)
    # Gesetzt beim ersten Login eines Tages; beim Anzeigen atomar geleert (nur einmal sichtbar).
    greeting_due_on = db.Column(db.Date, nullable=True)
    news_seen_at = db.Column(db.DateTime, nullable=True)
    updated_at = db.Column(db.DateTime, nullable=False, default=_now, onupdate=_now)


class SavedView(TenantScopedMixin, db.Model):
    __tablename__ = "saved_views"
    __table_args__ = (db.UniqueConstraint("user_id", "page", "name", name="uq_saved_views_user_page_name"),)

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    page = db.Column(db.String(40), nullable=False)
    name = db.Column(db.String(120), nullable=False)
    params = db.Column(db.JSON, nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=_now)


class FormDraft(TenantScopedMixin, db.Model):
    __tablename__ = "form_drafts"
    __table_args__ = (db.UniqueConstraint("user_id", "form_key", name="uq_form_drafts_user_key"),)

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    form_key = db.Column(db.String(40), nullable=False)
    data = db.Column(db.JSON, nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=_now)
    updated_at = db.Column(db.DateTime, nullable=False, default=_now, onupdate=_now)


class OfficeNews(TenantScopedMixin, db.Model):
    __tablename__ = "office_news"

    id = db.Column(db.Integer, primary_key=True)
    published_on = db.Column(db.Date, nullable=False)
    title = db.Column(db.String(160), nullable=False)
    body = db.Column(db.Text, nullable=False)
    # "alle", "admin" (nur Buero-Admins) oder "mitarbeiter"
    audience = db.Column(db.String(20), nullable=False, default="alle", server_default="alle")
    created_by_user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=_now)
