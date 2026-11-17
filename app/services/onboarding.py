"""Erste Schritte fuer ein frisch eingerichtetes Buero (Startseite des Assistenten).

Zeigt nur, was noch fehlt - Kunden, Buerozeiten, aktiver Buchungslink - jeweils mit dem direkten
Weg dorthin. Sind alle Punkte erledigt, erscheint nichts. Abfragen laufen im Buero-Kontext
(TenantScopedMixin) und sind auf Existenz beschraenkt (LIMIT 1)."""

from flask import url_for

from app.models import Customer
from app.models.office_booking import OfficeBookingPage
from app.models.office_settings import OfficeSettings


def first_steps(user) -> list[dict]:
    if not (user.is_office_admin or user.is_employee):
        return []
    steps: list[dict] = []
    admin = user.is_office_admin
    if admin and Customer.query.with_entities(Customer.id).limit(1).first() is None:
        steps.append({"key": "kunden", "title": "Kunden anlegen", "text": "Ersten Kunden erfassen oder eine Leipziger Liste importieren – danach finden Sie jeden Kunden über die Suche.",
                      "url": url_for("customers.new"), "action": "Kunde anlegen", "icon": "user-plus"})
    settings = OfficeSettings.query.first()
    if admin and not (settings and settings.hours):
        steps.append({"key": "zeiten", "title": "Bürozeiten festlegen", "text": "Aus den Bürozeiten entstehen freie Termine für Kalender, Terminvorschläge und Ihren Buchungslink.",
                      "url": url_for("calendar.settings") + "#s-planung", "action": "Bürozeiten eintragen", "icon": "clock"})
    page = OfficeBookingPage.query.first()
    if admin and not (page and page.enabled):
        steps.append({"key": "buchung", "title": "Buchungslink einrichten", "text": "Ein fester Link für Signatur und Website: Kunden buchen selbst einen freien Termin, ohne Anmeldung.",
                      "url": url_for("calendar.settings") + "#online-buchung", "action": "Online-Buchung einrichten", "icon": "link"})
    return steps
