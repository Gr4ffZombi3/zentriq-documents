"""Kundensuche im Hauptchat: eindeutige Sucheingaben (Name, Telefon, VN-Nummer) beantwortet
Zentriq direkt aus der Kundensuche - mit und ohne Treffer, ohne KI-Modell. Jeder Modellaufruf
waere ein Testfehler (adapters.call ist gesperrt, das Modell gilt aber als verfuegbar wie in
Produktion). Synthetische Daten."""

from datetime import timedelta

import pytest

from app.models import CalendarEvent, Customer, CustomerNote, EmployeeProfile
from app.models.ai import AiConversation, AiMessage
from app.models.office_settings import OfficeSettings
from app.services import customer_notes
from app.services.ai import adapters, router
from app.services.timetracking.clock import local_today
from app.tenancy import bypass_tenant_scope
from tests.test_customer_notes import _customer_list, _row
from tests.test_permissions_security import login, world  # noqa: F401

API = "/zentriq/api/kunden/eingabe"
HOURS = {str(d): ["08:00", "18:00"] for d in range(1, 8)}


@pytest.fixture()
def no_model(monkeypatch):
    """KI-Modell verfuegbar (wie Produktion), aber jeder Aufruf ist verboten."""

    def forbidden(*args, **kwargs):
        raise AssertionError("Modellaufruf ist hier nicht erlaubt")

    monkeypatch.setattr(router, "can_answer", lambda: True)
    monkeypatch.setattr(adapters, "call", forbidden)
    monkeypatch.setattr(router, "complete", forbidden)


@pytest.fixture()
def office(app, db, world):  # noqa: F811
    _doc, ids = _customer_list(
        db,
        world.tenant_a.id,
        [
            ("Erika Schmidthofer", "+49 221 9876543", [_row("808/100004-D", "08/5555-B")]),
            ("Max Mustermann", "0228 1234567", [_row("808/100001-A", "08/5555-B")]),
            ("Peter Krause", "0171 2223334", [_row("808/222222-C", "08/5555-B")]),
            ("Peter Krause", "0172 5556667", [_row("808/333333-D", "08/5555-B")]),
        ],
    )
    _customer_list(db, world.tenant_b.id, [("Olga Fremdbuero", "0160 1112223", [_row("808/999999-Z", "08/7777-C")])], "b.pdf")
    world.ids = ids
    return world


def _say(client, text, **extra):
    resp = client.post(API, json={"text": text, **extra})
    assert resp.status_code == 200, resp.get_json()
    return resp.get_json()


def _no_chat_traffic():
    with bypass_tenant_scope():
        return AiConversation.query.count() == 0 and AiMessage.query.count() == 0


def _notes():
    with bypass_tenant_scope():
        return CustomerNote.query.count()


@pytest.mark.parametrize("text", ["schmidthofer", "Schmidthofer", "Erika Schmidthofer", "erika schmidthofer",
                                  "Frau Schmidthofer", "0221 9876543", "+49 221 9876543", "0049 221 9876543",
                                  "808/100004-D", "808100004D"])
def test_unique_inputs_show_the_card_without_model(app, office, no_model, text):
    client = login(app, "laura@example.com")
    r = _say(client, text)
    assert r["kind"] == "card" and r["card"]["name"] == "Erika Schmidthofer", (text, r)
    assert _no_chat_traffic() and _notes() == 0


@pytest.mark.parametrize("text,label", [("zuckerberg", "zuckerberg"), ("Hanna Zuckerberg", "Hanna Zuckerberg"),
                                        ("hanna zuckerberg", "hanna zuckerberg"), ("Herr Zuckerberg", "Zuckerberg")])
def test_name_without_match_is_answered_directly(app, office, no_model, text, label):
    client = login(app, "laura@example.com")
    r = _say(client, text)
    assert r["kind"] == "not_found" and r["search"] is True and r["ask_ai"] is True, r
    # Vollstaendiger Name: Suche oder Anlage? -> kurze Rueckfrage mit Anlage-Knopf (nichts angelegt)
    expected = f"Kein Kunde „{label}“ in Ihrem Büro gefunden."
    if len(label.split()) >= 2:
        expected += " Kunden suchen oder neu anlegen?"
        assert r["create"]["last_name"] == "Zuckerberg"
    assert r["message"] == expected
    assert _no_chat_traffic() and _notes() == 0


def test_phone_and_vn_without_match_are_answered_directly(app, office, no_model):
    client = login(app, "laura@example.com")
    assert _say(client, "0170 0000000")["kind"] == "not_found"
    assert _say(client, "808/555555")["kind"] == "not_found"
    assert _no_chat_traffic() and _notes() == 0


def test_typo_offers_suggestion_and_same_names_offer_choice(app, office, no_model):
    client = login(app, "laura@example.com")
    r = _say(client, "schmidhofer")
    assert r["kind"] == "choose" and [c["name"] for c in r["candidates"]] == ["Erika Schmidthofer"] and r["ask_ai"] is True
    r = _say(client, "Peter Krause")
    assert r["kind"] == "choose" and len(r["candidates"]) == 2 and all(c["same_name"] for c in r["candidates"])
    assert r["pending_note"] is None
    assert _no_chat_traffic()


def test_selected_card_does_not_turn_a_search_into_a_note(app, office, no_model):
    """Nach einer geoeffneten Karte ist der naechste Nachname eine Suche, keine Notiz beim
    ausgewaehlten Kunden; ein unbekannter Name wird nachgefragt statt gespeichert."""
    client = login(app, "laura@example.com")
    selected = office.ids["Max Mustermann"]
    r = _say(client, "schmidthofer", selected_id=selected)
    assert r["kind"] == "card" and r["card"]["name"] == "Erika Schmidthofer"
    r = _say(client, "zuckerberg", selected_id=selected)
    assert r["kind"] == "clarify" and r["customer"]["id"] == selected and r["options"] == ["note", "chat"]
    assert _notes() == 0
    # Eine echte Folgenotiz bleibt eine Notiz beim ausgewaehlten Kunden.
    assert _say(client, "nicht erreicht", selected_id=selected)["kind"] == "saved"
    assert _notes() == 1


def test_recently_created_customer_is_found_at_once(app, office, no_model):
    client = login(app, "laura@example.com")
    assert _say(client, "Lena Neubauer")["kind"] == "not_found"
    created = _say(client, "Lege Kunden Lena Neubauer an, Telefon 0151 4445556", first_name="Lena", last_name="Neubauer", phone_kinds=["privat"])
    assert created["kind"] in ("card", "saved"), created
    for text in ("neubauer", "Lena Neubauer", "0151 4445556", "+49 151 4445556"):
        r = _say(client, text)
        assert r["kind"] == "card" and r["card"]["name"] == "Lena Neubauer", (text, r)


def test_search_stays_in_own_office(app, office, no_model):
    client = login(app, "laura@example.com")
    for text in ("Olga Fremdbuero", "fremdbuero", "0160 1112223", "808/999999-Z"):
        assert _say(client, text)["kind"] in ("not_found", "need_name"), text
    other = login(app, "bob@example.com")
    assert _say(other, "Olga Fremdbuero")["kind"] == "card"
    assert _say(other, "schmidthofer")["kind"] == "not_found"


def test_card_keeps_notes_and_leipziger_list(app, office, no_model):
    client = login(app, "laura@example.com")
    _say(client, "Max Mustermann HUK24 Wechsel besprochen")
    r = _say(client, "mustermann")
    card = r["card"]
    assert r["kind"] == "card" and card["latest"]["text"].startswith("HUK24 Wechsel")
    assert card["leipziger"]["state"] == "found" and card["leipziger"]["entries"][0]["number"] == "808/100001-A"


@pytest.mark.parametrize("text", ["Hallo", "hallo", "Danke", "Teilkasko", "Erkläre mir Teilkasko",
                                  "Was ist eine Teilkaskoversicherung?", "Wie spät ist es in New York?",
                                  "Formuliere eine freundliche Terminerinnerung"])
def test_chat_inputs_still_go_to_the_chat(app, office, monkeypatch, text):
    monkeypatch.setattr(router, "can_answer", lambda: True)
    client = login(app, "laura@example.com")
    assert _say(client, text)["kind"] == "chat", text


def test_name_search_text():
    assert customer_notes.name_search_text("Frau Krause") == "Krause"
    assert customer_notes.name_search_text("max mustermann") == "max mustermann"
    assert customer_notes.name_search_text("Müller-Lüdenscheidt") == "Müller-Lüdenscheidt"
    for text in ("erkläre mir kasko", "nicht erreicht", "Hallo", "0171 1234567", "Wer ist Krause?", "Krause HUK24", "ab"):
        assert customer_notes.name_search_text(text) is None, text


# --- Offener Termin wartet auf den Kundennamen ---------------------------------------------------


@pytest.fixture()
def calendar_office(db, tenant, user, employee):
    db.session.add_all([
        EmployeeProfile(tenant_id=tenant.id, user_id=employee.id, display_name="Laura Test"),
        EmployeeProfile(tenant_id=tenant.id, user_id=user.id, display_name="Anna Admin"),
        OfficeSettings(tenant_id=tenant.id, hours=HOURS, default_minutes=45, buffer_minutes=0, lead_minutes=0, horizon_days=14),
    ])
    karl = Customer(tenant_id=tenant.id, name="Karl Schmidthofer", assigned_user_id=employee.id)
    db.session.add_all([karl, Customer(tenant_id=tenant.id, name="Ute Bergmann"), Customer(tenant_id=tenant.id, name="Udo Bergmann")])
    db.session.commit()
    return karl


def test_pending_appointment_continues_with_typed_name(auth_client, calendar_office, no_model):
    day = local_today() + timedelta(days=2)
    while day.weekday() >= 5:
        day += timedelta(days=1)
    day = day.strftime("%d.%m.%y")
    q = _say(auth_client, f"{day} Termin um 16.30")
    assert q["kind"] == "calendar_question" and q["message"].startswith("Für welchen Kunden ist der Termin"), q
    pending = q["await_customer"]
    r = _say(auth_client, "schmidthofer", calendar_pending=pending, client_ref="p1")
    assert r["kind"] == "calendar_saved", r["message"]
    event = CalendarEvent.query.one()
    assert event.customer_id == calendar_office.id
    # gleiche Namen -> Auswahl, Tippfehler -> Vorschlag; jeweils noch nichts gebucht
    r = _say(auth_client, "Bergmann", calendar_pending=pending)
    assert r["kind"] == "calendar_question" and len(r["options"]) == 3
    r = _say(auth_client, "Zuckerberg", calendar_pending=pending)
    assert r["kind"] == "calendar_question" and r["await_customer"]
    assert CalendarEvent.query.count() == 1
    # ohne offene Rueckfrage bleibt der Name eine normale Suche
    assert _say(auth_client, "schmidthofer")["kind"] == "card"
    # andere Eingabe trotz offener Rueckfrage: normale Verarbeitung, kein Termin
    assert _say(auth_client, "Max Mustermann HUK24 Wechsel", calendar_pending=pending)["kind"] != "calendar_saved"
    assert _no_chat_traffic()
