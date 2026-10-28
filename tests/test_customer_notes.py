# ruff: noqa: F811  (Fixtures werden aus anderen Testmodulen importiert)
"""Kundennotizen und offene Rueckrufe ueber das ZentriqAI-Eingabefeld.

Szenario wie in test_permissions_security (Buero A mit Admin, Dennis und Laura, Buero B mit Bob,
SUPER_ADMIN Justin). Kunden stammen wie im Bestand aus der Leipziger Liste: die
Versicherungsnehmernummer steht dort als Vorgangsnummer ("808/123456-E-12"), die Kundennummer
am Kunden ist leer. Alle Pruefungen laufen ueber echte HTTP-Requests."""

from datetime import timedelta

import pytest

from app.models import (
    Customer,
    CustomerContact,
    CustomerNote,
    DocStatus,
    DocType,
    Document,
    DocumentCustomer,
    LeipzigerEntry,
)
from app.services import leipziger_entries
from app.tenancy import bypass_tenant_scope, use_tenant_id
from tests.test_permissions_security import login, world  # noqa: F401

API = "/zentriq/api"


def _row(number, broker, start=None, status="FZW"):
    return {
        "customer": {},
        "contract_number": number,
        "broker_number": broker,
        "contract_start_date": start,
        "status_code": status,
        "product_line": "KH",
    }


def _customer_list(db, tenant_id, customers, name="liste.pdf"):
    """Leipziger Liste mit Kunden [(Name, Telefon, [Zeilen])] - wie beim Import zugeordnet."""
    with use_tenant_id(tenant_id):
        document = Document(
            tenant_id=tenant_id, filename=name, original_filename=name, file_path=f"/tmp/{name}",
            status=DocStatus.DONE, doc_type=DocType.LEIPZIGER_LISTE,
        )
        db.session.add(document)
        db.session.flush()
        ids = {}
        for customer_name, phone, rows in customers:
            customer = Customer(tenant_id=tenant_id, name=customer_name, phone=phone)
            db.session.add(customer)
            db.session.flush()
            db.session.add(DocumentCustomer(tenant_id=tenant_id, document_id=document.id, customer_id=customer.id, row_data=rows))
            ids[customer_name if customer_name not in ids else f"{customer_name} 2"] = customer.id
        db.session.flush()
        leipziger_entries.rebuild_entries(document)
        db.session.commit()
        return document.id, ids


@pytest.fixture()
def office(app, db, world):
    doc_id, ids = _customer_list(
        db,
        world.tenant_a.id,
        [
            ("Justin Heller", "0171 1234567", [_row("808/123456-E-12", "08/5555-B"), _row("808/123456-E", "08/1234-A", start="2026-07-01")]),
            ("Maria Schulz", None, [_row("808/111111-B", "08/5555-B")]),
            ("Peter Krause", None, [_row("808/222222-C", "08/5555-B")]),
            ("Peter Krause", None, [_row("808/333333-D", "08/5555-B")]),
            ("Anna Fischer", None, []),
        ],
    )
    _doc_b, ids_b = _customer_list(db, world.tenant_b.id, [("Justin Heller", "0171 1234567", [_row("808/123456-E", "08/7777-C")])], "b.pdf")
    world.doc_list_id = doc_id
    world.ids = ids
    world.heller_b = ids_b["Justin Heller"]
    with bypass_tenant_scope():
        world.customer_count = Customer.query.filter_by(tenant_id=world.tenant_a.id).count()
    return world


def _post(client, path, **body):
    resp = client.post(API + path, json=body)
    return resp.status_code, resp.get_json()


def _input(client, text, **extra):
    status, data = _post(client, "/kunden/eingabe", text=text, **extra)
    assert status == 200, data
    return data


def _notes(customer_id=None):
    with bypass_tenant_scope():
        query = CustomerNote.query
        if customer_id is not None:
            query = query.filter_by(customer_id=customer_id)
        return query.order_by(CustomerNote.id).all()


def _contacts(customer_id=None):
    with bypass_tenant_scope():
        query = CustomerContact.query
        if customer_id is not None:
            query = query.filter_by(customer_id=customer_id)
        return query.order_by(CustomerContact.id).all()


# --- 1. Beispieleingabe ------------------------------------------------------------------------


def test_example_input_saves_exactly_one_note_at_existing_customer(app, office):
    client = login(app, "laura@example.com")
    data = _input(client, "808/123456-e Justin Heller huk 24 Wechsel", client_ref="r1")
    heller = office.ids["Justin Heller"]
    assert data["kind"] == "saved"
    assert data["message"] == "Bei Justin Heller gespeichert: HUK24 Wechsel"
    notes = _notes()
    assert len(notes) == 1
    note = notes[0]
    assert note.customer_id == heller and note.text == "HUK24 Wechsel"
    assert note.created_by_user_id == office.laura_id and note.created_at is not None
    assert note.tenant_id == office.tenant_a.id
    card = data["card"]
    assert card["name"] == "Justin Heller" and card["vn"] == "808/123456-E"
    assert card["latest"]["author"] == "Laura" and card["latest"]["when"].startswith("heute, ")
    # Keine neue Kundenakte, Stammdaten unveraendert.
    with bypass_tenant_scope():
        assert Customer.query.filter_by(tenant_id=office.tenant_a.id).count() == office.customer_count
        customer = Customer.query.get(heller)
        assert customer.customer_number is None and customer.phone == "0171 1234567"


@pytest.mark.parametrize(
    "text, expected",
    [
        ("808/123456-e HUK24 Wechsel, nicht erreicht", "HUK24 Wechsel, nicht erreicht"),
        ("Justin Heller wegen Kfz-Wechsel angerufen, nicht erreicht", "Wegen Kfz-Wechsel angerufen, nicht erreicht"),
        ("808/123456-e Kunde ruft morgen zurück", "Kunde ruft morgen zurück"),
        ("0171 1234567 Unterlagen per Post geschickt", "Unterlagen per Post geschickt"),
    ],
)
def test_further_examples_keep_content(app, office, text, expected):
    data = _input(login(app, "laura@example.com"), text)
    assert data["kind"] == "saved"
    assert [n.text for n in _notes(office.ids["Justin Heller"])] == [expected]


# --- 2./3. Offene Kontaktvorgaenge -------------------------------------------------------------


def test_plain_note_creates_no_open_callback(app, office):
    client = login(app, "laura@example.com")
    _input(client, "808/123456-e HUK24 Wechsel")
    assert _contacts() == []
    _status, data = 200, client.get(API + "/rueckrufe").get_json()
    assert data["callbacks"] == []
    assert _notes()[0].event is None


def test_not_reached_creates_and_then_updates_the_same_contact(app, office):
    laura = login(app, "laura@example.com")
    heller = office.ids["Justin Heller"]
    first = _input(laura, "808/123456-e HUK24 Wechsel, nicht erreicht")
    contacts = _contacts(heller)
    assert len(contacts) == 1
    assert contacts[0].topic == "HUK24 Wechsel" and contacts[0].status == "open" and contacts[0].outcome == "not_reached"
    assert first["card"]["open_contacts"][0]["status_label"] == "Nicht erreicht"

    # Zweiter Versuch (anderer Mitarbeiter, andere Schreibweise) ergaenzt den Vorgang.
    dennis = login(app, "dennis@example.com")
    _input(dennis, "Justin Heller huk 24 Wechsel – wieder nicht erreicht")
    contacts = _contacts(heller)
    assert len(contacts) == 1 and contacts[0].attempts == 2 and contacts[0].updated_by_user_id == office.dennis_id
    callbacks = dennis.get(API + "/rueckrufe").get_json()["callbacks"]
    assert len(callbacks) == 1 and callbacks[0]["customer"]["name"] == "Justin Heller" and callbacks[0]["by"] == "Dennis"
    assert len(_notes(heller)) == 2  # fruehere Notizen bleiben erhalten

    # Anderer Anlass desselben Kunden -> eigener Vorgang.
    _input(laura, "808/123456-e wegen Hausrat angerufen, bitte zurückrufen")
    contacts = _contacts(heller)
    assert [c.topic for c in contacts] == ["HUK24 Wechsel", "Hausrat"]
    assert contacts[1].outcome == "callback_requested"


def test_customer_calls_back_is_open_and_topicless_note_joins_single_open_contact(app, office):
    laura = login(app, "laura@example.com")
    heller = office.ids["Justin Heller"]
    _input(laura, "808/123456-e HUK24 Wechsel, nicht erreicht")
    _input(laura, "808/123456-e Kunde ruft morgen zurück")
    contacts = _contacts(heller)
    assert len(contacts) == 1 and contacts[0].outcome == "customer_calls_back" and contacts[0].attempts == 2


def test_topicless_contact_with_several_open_asks_which_one(app, office):
    laura = login(app, "laura@example.com")
    heller = office.ids["Justin Heller"]
    _input(laura, "808/123456-e HUK24 Wechsel, nicht erreicht")
    _input(laura, "808/123456-e Hausrat, nicht erreicht")
    data = _input(laura, "808/123456-e nicht erreicht")
    assert data["kind"] == "choose_contact" and len(data["contacts"]) == 2
    assert len(_notes(heller)) == 2  # nichts gespeichert, bevor geklaert ist
    target = [c for c in data["contacts"] if c["topic"] == "Hausrat"][0]
    saved = _input(laura, "808/123456-e nicht erreicht", customer_id=heller, contact_id=target["id"])
    assert saved["kind"] == "saved"
    attempts = {c.topic: c.attempts for c in _contacts(heller)}
    assert attempts == {"HUK24 Wechsel": 1, "Hausrat": 2}


# --- 4./5. Suche -------------------------------------------------------------------------------


@pytest.mark.parametrize("query", ["Justin Heller", "808/123456-e", "808/123456-E-12", "+49 171 1234567", "0171/1234567", "Heller"])
def test_dennis_finds_the_reason_by_name_number_or_phone_without_saving(app, office, query):
    _input(login(app, "laura@example.com"), "808/123456-e Justin Heller huk 24 Wechsel")
    data = _input(login(app, "dennis@example.com"), query)
    assert data["kind"] == "card"
    assert data["card"]["id"] == office.ids["Justin Heller"]
    assert data["card"]["latest"]["text"] == "HUK24 Wechsel" and data["card"]["latest"]["author"] == "Laura"
    assert len(_notes()) == 1  # Suche speichert nichts


def test_questions_are_answered_from_stored_notes_with_source(app, office):
    laura = login(app, "laura@example.com")
    admin = login(app, "admin-a@example.com")
    data = _input(admin, "Warum hat Laura Justin Heller angerufen?")
    assert data["kind"] == "card"
    assert data["answer"]["text"] == "Zu Justin Heller ist kein Anlass von Laura dokumentiert."

    _input(laura, "808/123456-e Justin Heller huk 24 Wechsel, nicht erreicht")
    data = _input(admin, "Warum hat Laura Justin Heller angerufen?")
    source = data["answer"]["sources"][0]
    assert source["text"] == "HUK24 Wechsel, nicht erreicht" and source["author"] == "Laura" and source["source"] == "Notiz"
    assert source["when"].startswith("heute, ")

    data = _input(admin, "Was ist bei Justin Heller noch offen?")
    assert data["answer"]["sources"][0]["text"] == "HUK24 Wechsel – Nicht erreicht"
    assert len(_notes()) == 1


def test_pure_search_and_general_chat_save_nothing(app, office):
    client = login(app, "laura@example.com")
    assert _input(client, "Formuliere eine freundliche Terminerinnerung")["kind"] == "chat"
    assert _input(client, "Was ist heute noch offen?")["kind"] == "chat"
    assert _input(client, "Schreib eine Mail an Justin Heller wegen Termin")["kind"] == "chat"
    assert _input(client, "Formuliere eine SMS an 0151 9999999")["kind"] == "chat"
    assert _input(client, "Formuliere eine Mail an 808/123456-E")["kind"] == "clarify"
    assert _input(client, "Justin Heller")["kind"] == "card"
    assert _notes() == [] and _contacts() == []


# --- 6. Folgenotizen ---------------------------------------------------------------------------


def test_follow_up_note_goes_only_to_visibly_selected_customer(app, office):
    client = login(app, "laura@example.com")
    heller, schulz = office.ids["Justin Heller"], office.ids["Maria Schulz"]
    data = _input(client, "Termin Donnerstag 14 Uhr", selected_id=heller)
    assert data["kind"] == "saved" and data["message"] == "Bei Justin Heller gespeichert: Termin Donnerstag 14 Uhr"
    # Ohne Auswahl: kein unsichtbarer Kontext -> nichts gespeichert, Eingabe geht an den Chat.
    assert _input(client, "Termin Freitag 9 Uhr")["kind"] == "chat"
    # Neue Kennung ersetzt die Auswahl.
    data = _input(client, "808/111111-b Angebot zugeschickt", selected_id=heller)
    assert data["card"]["id"] == schulz
    assert [n.text for n in _notes(heller)] == ["Termin Donnerstag 14 Uhr"]
    assert [n.text for n in _notes(schulz)] == ["Angebot zugeschickt"]
    # Arbeitsauftrag an die KI bei ausgewaehltem Kunden -> kurze Rueckfrage.
    data = _input(client, "Formuliere eine Erinnerung an den Termin", selected_id=heller)
    assert data["kind"] == "clarify"
    assert len(_notes()) == 2
    # Kein Termin/keine Aufgabe aus einer Terminerwaehnung.
    assert _contacts() == []


# --- 7. Mehrdeutig / widerspruechlich -----------------------------------------------------------


def test_ambiguous_name_asks_and_saves_nothing(app, office):
    client = login(app, "laura@example.com")
    data = _input(client, "Peter Krause HUK24 Wechsel")
    assert data["kind"] == "choose" and len(data["candidates"]) == 2
    assert {c["vn"] for c in data["candidates"]} == {"808/222222-C", "808/333333-D"}
    assert data["pending_note"] == "Peter Krause HUK24 Wechsel"
    assert _notes() == []
    chosen = data["candidates"][1]["id"]
    saved = _input(client, data["pending_note"], customer_id=chosen)
    assert saved["kind"] == "saved" and [n.customer_id for n in _notes()] == [chosen]


def test_number_and_name_of_other_customer_conflict(app, office):
    client = login(app, "laura@example.com")
    data = _input(client, "808/123456-e Maria Schulz HUK24 Wechsel")
    assert data["kind"] == "conflict"
    assert {c["name"] for c in data["candidates"]} == {"Justin Heller", "Maria Schulz"}
    assert _notes() == []


def test_unknown_number_or_name_gives_hint_and_creates_nothing(app, office):
    client = login(app, "laura@example.com")
    assert _input(client, "808/999999-z HUK24 Wechsel")["kind"] == "not_found"
    assert _input(client, "Müllermann nicht erreicht")["kind"] == "not_found"
    assert _notes() == []
    with bypass_tenant_scope():
        assert Customer.query.filter_by(tenant_id=office.tenant_a.id).count() == office.customer_count


# --- 8. Leipziger Liste -----------------------------------------------------------------------


def test_card_shows_assigned_leipziger_entries(app, office):
    admin = login(app, "admin-a@example.com")
    card = admin.get(f"{API}/kunden/{office.ids['Justin Heller']}").get_json()["card"]
    leipziger = card["leipziger"]
    assert leipziger["state"] == "found"
    assert [e["number"] for e in leipziger["entries"]] == ["808/123456-E-12", "808/123456-E"]
    assert "Beginn 01.07.2026" in leipziger["entries"][1]["status"]
    assert f"document_id={office.doc_list_id}" in leipziger["entries"][0]["url"]
    assert "/leipziger-liste" in leipziger["entries"][0]["url"]

    # Mitarbeiter sehen wie in der Liste nur Vorgaenge ihrer Vermittlernummer.
    laura_card = login(app, "laura@example.com").get(f"{API}/kunden/{office.ids['Justin Heller']}").get_json()["card"]
    assert [e["number"] for e in laura_card["leipziger"]["entries"]] == ["808/123456-E-12"]
    assert laura_card["leipziger"]["hidden"] == 1

    # Kein zugeordneter Eintrag != Ladefehler.
    fischer = admin.get(f"{API}/kunden/{office.ids['Anna Fischer']}").get_json()["card"]
    assert fischer["leipziger"]["state"] == "none"


def test_leipziger_load_error_is_reported_as_error(app, office, monkeypatch):
    from app.services import customer_notes

    def broken():
        raise RuntimeError("DB weg")

    monkeypatch.setattr(customer_notes, "current_list", broken)
    card = login(app, "admin-a@example.com").get(f"{API}/kunden/{office.ids['Justin Heller']}").get_json()["card"]
    assert card["leipziger"]["state"] == "error"


def test_note_does_not_change_list_entries(app, office):
    with bypass_tenant_scope():
        before = [(e.id, e.contract_number, e.status_code, e.start_date, e.customer_id) for e in LeipzigerEntry.query.order_by(LeipzigerEntry.id)]
    _input(login(app, "laura@example.com"), "808/123456-e HUK24 Wechsel, nicht erreicht")
    with bypass_tenant_scope():
        after = [(e.id, e.contract_number, e.status_code, e.start_date, e.customer_id) for e in LeipzigerEntry.query.order_by(LeipzigerEntry.id)]
    assert before == after


# --- 9. Doppelte Uebermittlung -----------------------------------------------------------------


def test_double_submission_saves_one_note(app, office):
    client = login(app, "laura@example.com")
    first = _input(client, "808/123456-e HUK24 Wechsel, nicht erreicht", client_ref="abc")
    second = _input(client, "808/123456-e HUK24 Wechsel, nicht erreicht", client_ref="abc")
    third = _input(client, "808/123456-e HUK24 Wechsel, nicht erreicht", client_ref="other")  # Doppelklick, neue ref
    assert first["duplicate"] is False and second["duplicate"] is True and third["duplicate"] is True
    assert len(_notes()) == 1
    assert len(_contacts()) == 1 and _contacts()[0].attempts == 1


# --- 10. Erledigt / Nicht erreicht ------------------------------------------------------------


def test_done_closes_only_the_chosen_contact(app, office):
    laura = login(app, "laura@example.com")
    heller = office.ids["Justin Heller"]
    _input(laura, "808/123456-e HUK24 Wechsel, nicht erreicht")
    _input(laura, "808/123456-e Hausrat, bitte zurückrufen")
    _input(laura, "808/111111-b Rechtsschutz, nicht erreicht")
    huk = [c for c in _contacts(heller) if c.topic == "HUK24 Wechsel"][0]
    dennis = login(app, "dennis@example.com")
    status, data = _post(dennis, f"/rueckrufe/{huk.id}/erledigt", client_ref="d1")
    assert status == 200 and data["kind"] == "closed"
    status, again = _post(dennis, f"/rueckrufe/{huk.id}/erledigt", client_ref="d2")
    assert again["duplicate"] is True
    states = {c.topic: (c.status, c.closed_by_user_id) for c in _contacts()}
    assert states == {
        "HUK24 Wechsel": ("done", office.dennis_id),
        "Hausrat": ("open", None),
        "Rechtsschutz": ("open", None),
    }
    done_notes = [n for n in _notes(heller) if n.event == "done"]
    assert len(done_notes) == 1 and done_notes[0].created_by_user_id == office.dennis_id
    assert len(_notes(heller)) == 3  # fruehere Notizen bleiben erhalten
    remaining = dennis.get(API + "/rueckrufe").get_json()["callbacks"]
    assert {c["topic"] for c in remaining} == {"Hausrat", "Rechtsschutz"}


def test_not_reached_quick_action_opens_contact_for_shown_note(app, office):
    laura = login(app, "laura@example.com")
    heller = office.ids["Justin Heller"]
    data = _input(laura, "808/123456-e HUK24 Wechsel")
    note_id = data["card"]["latest"]["id"]
    status, result = _post(laura, f"/kunden/{heller}/nicht-erreicht", note_id=note_id, client_ref="q1")
    assert status == 200
    contacts = _contacts(heller)
    assert len(contacts) == 1 and contacts[0].topic == "HUK24 Wechsel" and contacts[0].status == "open"
    status, result = _post(laura, f"/kunden/{heller}/nicht-erreicht", contact_id=contacts[0].id, client_ref="q2")
    assert result["card"]["open_contacts"][0]["attempts"] == 2
    _post(laura, f"/kunden/{heller}/nicht-erreicht", contact_id=contacts[0].id, client_ref="q2")  # Doppelklick
    assert _contacts(heller)[0].attempts == 2


def test_callbacks_sorted_by_last_update(app, office, db):
    laura = login(app, "laura@example.com")
    _input(laura, "808/123456-e HUK24 Wechsel, nicht erreicht")
    _input(laura, "808/111111-b Rechtsschutz, nicht erreicht")
    with bypass_tenant_scope():
        first = CustomerContact.query.order_by(CustomerContact.id).first()
        first.updated_at = first.updated_at + timedelta(minutes=5)
        db.session.commit()
    callbacks = laura.get(API + "/rueckrufe").get_json()["callbacks"]
    assert [c["topic"] for c in callbacks] == ["HUK24 Wechsel", "Rechtsschutz"]


# --- 11. Buerogrenzen ---------------------------------------------------------------------------


def test_other_office_is_unreachable(app, office):
    laura = login(app, "laura@example.com")
    _input(laura, "808/123456-e HUK24 Wechsel, nicht erreicht")
    contact_a = _contacts(office.ids["Justin Heller"])[0]

    bob = login(app, "bob@example.com")
    # Suche nach Nummer/Name findet nur den Kunden aus Buero B.
    data = _input(bob, "808/123456-e")
    assert data["card"]["id"] == office.heller_b and data["card"]["notes"] == []
    assert bob.get(API + "/rueckrufe").get_json()["callbacks"] == []
    # Direkte Backend-Aufrufe mit IDs aus Buero A.
    assert bob.get(f"{API}/kunden/{office.ids['Justin Heller']}").status_code == 404
    assert bob.post(f"{API}/rueckrufe/{contact_a.id}/erledigt", json={}).status_code == 404
    assert bob.post(f"{API}/kunden/{office.ids['Justin Heller']}/nicht-erreicht", json={}).status_code == 404
    status, _data = _post(bob, "/kunden/eingabe", text="Notiz", customer_id=office.ids["Justin Heller"])
    assert status == 404
    status, _data = _post(bob, "/kunden/eingabe", text="Notiz für A", selected_id=office.ids["Justin Heller"])
    assert _notes(office.ids["Justin Heller"])[0].text == "HUK24 Wechsel, nicht erreicht" and len(_notes(office.ids["Justin Heller"])) == 1
    assert _contacts(office.ids["Justin Heller"])[0].status == "open"


def test_super_admin_has_no_access(app, office):
    root = login(app, "justin@example.com")
    assert root.post(API + "/kunden/eingabe", json={"text": "Justin Heller"}).status_code == 403
    assert root.get(API + "/rueckrufe").status_code == 403
    assert root.get(f"{API}/kunden/{office.ids['Justin Heller']}").status_code == 403


# --- 12. Bestehende Funktionen ------------------------------------------------------------------


def test_start_page_shows_notes_area_and_keeps_chat(app, office):
    html = login(app, "laura@example.com").get("/zentriq").get_data(as_text=True)
    assert "data-zn" in html and "Offene Rückrufe" in html and "data-zc-form" in html
    root_html = login(app, "justin@example.com").get("/zentriq").get_data(as_text=True)
    assert "data-zn" not in root_html and "data-zc-form" in root_html


def test_merge_moves_notes_and_contacts(app, office, db):
    from app.services.customer_duplicates import merge_customers

    laura = login(app, "laura@example.com")
    _input(laura, "Anna Fischer Kfz-Wechsel, nicht erreicht")
    with use_tenant_id(office.tenant_a.id):
        target = Customer(tenant_id=office.tenant_a.id, name="Anna Fischer")
        db.session.add(target)
        db.session.commit()
        source = db.session.get(Customer, office.ids["Anna Fischer"])
        admin = db.session.get(type(office.admin_a), office.admin_a_id)
        merge_customers(target, source, admin)
        assert [n.customer_id for n in CustomerNote.query.all()] == [target.id]
        assert [c.customer_id for c in CustomerContact.query.all()] == [target.id]


def test_follow_up_not_reached_takes_topic_of_last_note(app, office):
    laura = login(app, "laura@example.com")
    heller = office.ids["Justin Heller"]
    _input(laura, "808/123456-e Justin Heller huk 24 Wechsel")
    data = _input(laura, "nicht erreicht", selected_id=heller)
    assert data["kind"] == "saved" and data["message"] == "Bei Justin Heller gespeichert: Nicht erreicht"
    contacts = _contacts(heller)
    assert len(contacts) == 1 and contacts[0].topic == "HUK24 Wechsel"
    assert [n.text for n in _notes(heller)] == ["HUK24 Wechsel", "Nicht erreicht"]
