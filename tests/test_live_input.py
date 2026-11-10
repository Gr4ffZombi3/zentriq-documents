"""Live-Vorschau im Hauptchat: Kundensuche, Notiz, Termin - lokal erkannt, rein lesend; Speichern
ueber die bestehenden Wege mit erneuter Pruefung (Belegung, Rechte)."""

from datetime import timedelta

import pytest

from app.models import CalendarEvent, Customer, CustomerNote, EmployeeProfile, OfficeSettings, User, UserRole
from app.services import live_input
from app.services.timetracking.clock import local_to_utc_naive, local_today

PASSWORD = "geheimespasswort123"


@pytest.fixture()
def office(app, db, tenant):
    laura = User(tenant_id=tenant.id, email="laura@example.com", role=UserRole.OFFICE_ADMIN, is_active=True, vermittlernummer="VM-1")
    laura.set_password(PASSWORD)
    mia = User(tenant_id=tenant.id, email="mia@example.com", role=UserRole.EMPLOYEE, is_active=True, vermittlernummer="VM-2")
    mia.set_password(PASSWORD)
    db.session.add_all([laura, mia])
    db.session.flush()
    db.session.add_all([
        EmployeeProfile(tenant_id=tenant.id, user_id=laura.id, display_name="Laura Beispiel"),
        EmployeeProfile(tenant_id=tenant.id, user_id=mia.id, display_name="Mia Muster"),
        OfficeSettings(tenant_id=tenant.id, hours={str(d): ["08:00", "18:00"] for d in range(1, 8)}, default_minutes=45, lead_minutes=0),
        Customer(tenant_id=tenant.id, name="Hans Müller", city="Bonn"),
        Customer(tenant_id=tenant.id, name="Erika Schmitz", city="Köln"),
    ])
    db.session.commit()
    return {"laura": laura, "mia": mia}


def _login(app, user):
    client = app.test_client()
    assert client.post("/auth/login", data={"login_type": "email", "identifier": user.email, "password": PASSWORD}).status_code == 302
    return client


def _thursday():
    today = local_today()
    return today + timedelta(days=((3 - today.weekday()) % 7) or 7)


def _preview(client, text, **params):
    return client.get("/zentriq/api/vorschau", query_string={"text": text, **params}).get_json()


def test_classify_is_local_and_leaves_chat_questions_alone():
    assert live_input.classify("Müller") == "customer"
    assert live_input.classify("Notiz Müller: Fahrzeugschein fehlt") == "note"
    assert live_input.classify("Termin Müller Donnerstag 11:30 bei Laura") == "appointment"
    for text in ("Wie hoch ist die Versicherungssumme?", "Hallo", "Schreib eine Mail an Herrn Müller wegen des Angebots", "Ist Donnerstag 11:30 frei?"):
        assert live_input.classify(text) is None, text


def test_appointment_preview_fields_free_and_saved_in_calendar(app, db, office):
    client = _login(app, office["mia"])
    day = _thursday()
    d = _preview(client, "Termin Müller Donnerstag 11:30 bei Laura")
    f = d["fields"]
    assert d["kind"] == "appointment"
    assert f["customer"]["value"]["name"] == "Hans Müller"
    assert (f["day"]["value"], f["time"]["value"]) == (day.isoformat(), "11:30")
    assert f["assignee"]["value"]["name"] == "Laura Beispiel"
    assert (f["minutes"]["value"], f["minutes"]["source"]) == (45, "Büro-Standard")
    assert d["availability"]["status"] == "free" and d["ready"] is True
    assert CalendarEvent.query.count() == 0  # Vorschau liest nur

    # Uhrzeit anklicken: freie Zeiten aus Lauras Kalender; 12:00 waehlen
    slots = _preview(client, "Termin Müller Donnerstag 11:30 bei Laura", slots="1")["slots"]
    assert f"{day.isoformat()}T12:00" in [s["start"] for s in slots]
    chosen = _preview(client, "Termin Müller Donnerstag 11:30 bei Laura", start=f"{day.isoformat()}T12:00")
    assert chosen["fields"]["time"]["value"] == "12:00" and chosen["ready"]

    saved = client.post("/zentriq/api/kunden/eingabe", json={"text": "Termin Müller Donnerstag 11:30 bei Laura", "client_ref": "r1",
                                                               "calendar": {**chosen["choice"], "book": True}}).get_json()
    assert saved["kind"] == "calendar_saved", saved
    event = CalendarEvent.query.one()
    assert (event.assignee_user_id, event.customer.name) == (office["laura"].id, "Hans Müller")
    assert event.starts_at == local_to_utc_naive(day, __import__("datetime").time(12, 0))
    assert event.ends_at - event.starts_at == timedelta(minutes=45)
    # Im Kalender sichtbar
    api = client.get(f"/kalender/api/termine?von={day}&bis={day}").get_json()
    assert [e["title"] for e in api["events"]] == ["Termin Hans Müller"]


def test_two_customers_same_name_only_customer_choice_opens(app, db, office, tenant):
    db.session.add(Customer(tenant_id=tenant.id, name="Petra Müller", city="Bonn"))
    db.session.commit()
    client = _login(app, office["laura"])
    d = _preview(client, "Termin Müller Donnerstag 11:30 bei Laura")
    f = d["fields"]
    assert f["customer"]["status"] == "ambiguous" and len(f["customer"]["options"]) == 2
    assert f["day"]["status"] == f["time"]["status"] == f["assignee"]["status"] == "ok"
    assert d["ready"] is False
    petra = Customer.query.filter_by(name="Petra Müller").one()
    picked = _preview(client, "Termin Müller Donnerstag 11:30 bei Laura", customer_id=petra.id)
    assert picked["fields"]["customer"]["value"]["name"] == "Petra Müller"
    assert picked["fields"]["time"]["value"] == "11:30" and picked["fields"]["assignee"]["value"]["name"] == "Laura Beispiel"
    assert picked["ready"]
    # Kundensuche: beide zur Auswahl
    names = [c["name"] for c in _preview(client, "Müller")["customers"]]
    assert sorted(names) == ["Hans Müller", "Petra Müller"]


def test_busy_slot_offers_alternatives_and_save_rechecks(app, db, office, tenant):
    client = _login(app, office["laura"])
    day = _thursday()
    preview = _preview(client, "Termin Müller Donnerstag 11:30 bei Laura")
    assert preview["ready"]
    # Inzwischen bucht jemand anderes genau diese Zeit
    start = local_to_utc_naive(day, __import__("datetime").time(11, 30))
    db.session.add(CalendarEvent(tenant_id=tenant.id, title="Teambesprechung", starts_at=start, ends_at=start + timedelta(minutes=60),
                                 assignee_user_id=office["laura"].id, created_by_user_id=office["mia"].id))
    db.session.commit()
    result = client.post("/zentriq/api/kunden/eingabe", json={"text": "Termin Müller Donnerstag 11:30 bei Laura", "client_ref": "r2",
                                                                "calendar": {**preview["choice"], "book": True}}).get_json()
    assert result["kind"] == "calendar_question" and "Teambesprechung" in result["message"]
    assert CalendarEvent.query.count() == 1  # nicht gebucht
    again = _preview(client, "Termin Müller Donnerstag 11:30 bei Laura")
    assert again["availability"]["status"] == "busy" and again["ready"] is False
    assert "Teambesprechung" in again["availability"]["reason"]
    assert again["availability"]["alternatives"]
    slots = [s["label"] for s in _preview(client, "Termin Müller Donnerstag 11:30 bei Laura", slots="1")["slots"]]
    assert "11:30" not in slots and "12:00" not in slots


def test_missing_parts_are_marked(app, db, office):
    client = _login(app, office["laura"])
    d = _preview(client, "Termin Müller Donnerstag")
    assert d["fields"]["time"]["status"] == "missing" and d["ready"] is False and d["availability"] is None
    d = _preview(client, "Termin Donnerstag 10 Uhr bei Laura")
    assert d["fields"]["customer"]["status"] == "missing" and d["ready"] is False
    d = _preview(client, "Termin Müller Donnerstag 10 Uhr bei Gustav")
    assert d["fields"]["assignee"]["status"] == "unknown"


def test_note_preview_and_save_after_confirmation(app, db, office):
    client = _login(app, office["mia"])
    d = _preview(client, "Notiz Müller: Fahrzeugschein fehlt")
    assert d["kind"] == "note" and d["customer"]["value"]["name"] == "Hans Müller" and d["text"]["value"] == "Fahrzeugschein fehlt"
    assert d["ready"] and CustomerNote.query.count() == 0
    customer = Customer.query.filter_by(name="Hans Müller").one()
    r = client.post("/zentriq/api/kunden/eingabe", json={"text": "Fahrzeugschein fehlt (Kopie folgt)", "customer_id": customer.id, "mode": "note", "client_ref": "n1"}).get_json()
    assert r["kind"] == "saved", r
    note = CustomerNote.query.one()
    assert (note.customer_id, note.text, note.created_by_user_id) == (customer.id, "Fahrzeugschein fehlt (Kopie folgt)", office["mia"].id)


def test_preview_is_office_scoped_and_needs_office_role(app, db, office, tenant):
    from app.models import Tenant

    other = Tenant(name="Anderes Büro", slug="anderes")
    db.session.add(other)
    db.session.flush()
    stranger = User(tenant_id=other.id, email="fremd@example.com", role=UserRole.OFFICE_ADMIN, is_active=True)
    stranger.set_password(PASSWORD)
    db.session.add(stranger)
    db.session.commit()
    assert _preview(_login(app, stranger), "Müller") == {"kind": None, "seq": None}
    assert app.test_client().get("/zentriq/api/vorschau?text=Müller").status_code in (302, 401)
