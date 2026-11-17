# ruff: noqa: F811  (Fixtures werden aus anderen Testmodulen importiert)
"""Vorfuehrbare Oberflaeche (2026-11): Erste Schritte fuer neue Bueros, Tagesauswahl auf der
Buchungsseite, freie Zeiten mit einer einzigen Kalenderabfrage, Schrift/Designsystem."""

import re
from pathlib import Path

from sqlalchemy import event

from app.extensions import db as _db
from app.models import OfficeBookingPage
from app.models.office_settings import OfficeSettings
from app.tenancy import bypass_tenant_scope
from tests.test_customer_notes import office  # noqa: F401
from tests.test_office_booking import _enable, setup  # noqa: F401
from tests.test_permissions_security import login, world  # noqa: F401

STATIC = Path(__file__).resolve().parents[1] / "app" / "static"


def test_first_steps_only_for_what_is_missing(app, office):
    admin = login(app, "admin-a@example.com")
    html = admin.get("/zentriq").get_data(as_text=True)
    assert "data-zq-first-steps" in html
    assert "Bürozeiten festlegen" in html and "Buchungslink einrichten" in html
    with bypass_tenant_scope():
        _db.session.add(OfficeSettings(tenant_id=office.tenant_a.id, hours={"1": ["09:00", "12:00"]}))
        _db.session.add(OfficeBookingPage(tenant_id=office.tenant_a.id, slug="erste-schritte", enabled=True, assignee_user_id=office.laura_id))
        _db.session.commit()
    html = admin.get("/zentriq").get_data(as_text=True)
    assert "Bürozeiten festlegen" not in html and "Buchungslink einrichten" not in html
    # Mitarbeiter sehen keine Einrichtungsaufgaben des Admins.
    assert "data-zq-first-steps" not in login(app, "laura@example.com").get("/zentriq").get_data(as_text=True)


def test_booking_page_day_picker_keeps_radio_fallback_and_office_branding(app, setup):
    _enable(app, setup)
    html = app.test_client().get("/termin/heller").get_data(as_text=True)
    days = re.findall(r'data-day="(\d+)"', html)
    panels = re.findall(r'data-panel="(\d+)"', html)
    assert days and days == panels  # je Tag ein Kalenderfeld und eine Uhrzeitgruppe
    assert re.search(r'class="cal-day is-empty"[^>]*disabled', html)  # Tage ohne Zeiten sichtbar, nicht waehlbar
    assert 'name="slot"' in html and "Keine buchbaren Zeiten" in html  # ohne JS weiter buchbar
    assert "<h1>Agentur Heller</h1>" in html and "data-booking-mono>AH<" not in html
    assert 'data-booking-mono>H<' in html  # "Agentur" ist Gattungswort
    assert "InterVariable-latin.woff2" in html
    assert "Tag wählen" in html and "Uhrzeit wählen" in html and "Ihre Kontaktdaten" in html


def test_free_times_need_one_calendar_query_not_one_per_slot(app, setup):
    _enable(app, setup)
    client = app.test_client()
    client.get("/termin/heller")
    statements = []

    def count(conn, cursor, statement, *args):
        if "calendar_events" in statement:
            statements.append(statement)

    with app.app_context():
        engine = _db.engine
    event.listen(engine, "before_cursor_execute", count)
    try:
        html = client.get("/termin/heller").get_data(as_text=True)
    finally:
        event.remove(engine, "before_cursor_execute", count)
    assert len(re.findall(r'name="slot"', html)) > 10
    assert len(statements) <= 2, len(statements)  # vorher eine Abfrage je moeglicher Startzeit


def test_design_tokens_font_subset_and_progress_feedback():
    css = (STATIC / "css" / "tailwind_source.css").read_text(encoding="utf-8")
    assert "--color-surface: #ffffff;" in css and "--color-bg: #f5f6f8;" in css
    assert 'url("/static/fonts/InterVariable-latin.woff2")' in css
    assert (STATIC / "fonts" / "InterVariable-latin.woff2").stat().st_size < 150_000
    built = (STATIC / "css" / "app.css").read_text(encoding="utf-8")
    assert "InterVariable-latin.woff2" in built and ".zq-progress" in built and ".cust-head" in built  # Build aktuell
    js = (STATIC / "js" / "app.js").read_text(encoding="utf-8")
    assert "zq-loading" in js and "data-zq-submitting" in js
