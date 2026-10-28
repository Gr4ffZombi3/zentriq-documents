# ruff: noqa: F811  (Fixtures werden aus anderen Testmodulen importiert)
"""Auffindbarkeit von Kunden, die ueber eine Kurznotiz im ZentriqAI-Feld angelegt wurden.

Ablauf wie im Buero: Laura erfasst "808/765432-e Max Mustermann HUK24 Wechsel", danach suchen
Laura, Dennis (Mitarbeiter) und der Buero-Admin ueber VN-Nummer (alle Schreibweisen), Name und
Telefonnummer; Bob aus einem anderen Buero darf nichts finden. Alles ueber echte HTTP-Requests,
ohne KI-Modell (externe Aufrufe sind verboten), mit isolierten Testdaten (temporaere SQLite-DB)."""

from datetime import timedelta

import pytest

from app.extensions import db as _db
from app.models import Customer, CustomerContact, CustomerNote
from app.services import customer_notes, customer_sources
from app.services.customer_note_repair import check_and_repair
from app.services.timetracking.clock import utcnow_naive
from app.tenancy import bypass_tenant_scope, use_tenant_id
from tests.test_customer_notes import API, _input, _post, office  # noqa: F401
from tests.test_customer_notes_without_ai import no_provider_calls  # noqa: F401
from tests.test_permissions_security import login, world  # noqa: F401

VN_SPELLINGS = ["808/765432-e", "808/765432-E", "808765432e", "808 765432 e", "808-765432-E", "808765432E"]


def _customers(tenant_id, **filters):
    with bypass_tenant_scope():
        return Customer.query.filter_by(tenant_id=tenant_id, **filters).order_by(Customer.id).all()


def _snapshot():
    """Gesamter Datenstand (fuer "Suche aendert nichts")."""
    with bypass_tenant_scope():
        _db.session.expire_all()
        return (
            [(c.id, c.name, c.customer_number, c.phone, c.updated_at) for c in Customer.query.order_by(Customer.id)],
            [(n.id, n.customer_id, n.text) for n in CustomerNote.query.order_by(CustomerNote.id)],
            [(c.id, c.status, c.attempts) for c in CustomerContact.query.order_by(CustomerContact.id)],
        )


@pytest.fixture()
def created(app, office):
    """1. Laura erfasst einen bisher nicht vorhandenen Kunden samt Notiz."""
    before = utcnow_naive()
    data = _input(login(app, "laura@example.com"), "808/765432-e Max Mustermann HUK24 Wechsel", client_ref="laura-1")
    assert data["kind"] == "saved" and data["created"] is True, data
    [customer] = _customers(office.tenant_a.id, name="Max Mustermann")
    office.customer = customer
    office.created_before = before
    return office


def test_creation_stores_structured_fields_and_linked_note(created):
    customer = created.customer
    assert customer.customer_number == "808/765432-E" and customer.customer_number_key == "808765432E"
    assert customer.name_key == "max mustermann" and customer.phone is None
    assert customer.tenant_id == created.tenant_a.id and customer.assigned_user_id == created.laura.id
    assert customer.source == customer_sources.NOTIZ
    with bypass_tenant_scope():
        [note] = CustomerNote.query.filter_by(customer_id=customer.id).all()
    # Nummer und Name stehen in den Stammdaten, nicht im Notiztext.
    assert note.text == "HUK24 Wechsel"
    assert note.tenant_id == created.tenant_a.id and note.created_by_user_id == created.laura.id
    assert created.created_before - timedelta(seconds=1) <= note.created_at <= utcnow_naive() + timedelta(seconds=1)


@pytest.mark.parametrize("who", ["laura@example.com", "dennis@example.com", "admin-a@example.com"])
def test_every_office_member_finds_customer_by_all_spellings_name_and_question(app, created, who):
    """2./4./5. Direkt nach dem Speichern, fuer Laura, Dennis und den Admin, alle Schreibweisen."""
    client = login(app, who)
    before = _snapshot()
    for query in [*VN_SPELLINGS, "Suche 808/765432-e", "Max Mustermann", "Mustermann", "Suche Mustermann", "808765432"]:
        data = _input(client, query)
        assert data["kind"] == "card", (query, data)
        assert data["card"]["id"] == created.customer.id
        assert data["card"]["vn"] == "808/765432-E"
        [note] = data["card"]["notes"]
        assert note["text"] == "HUK24 Wechsel" and note["author"] == "Laura" and note["when"].startswith("heute")
    answer = _input(client, "Was wurde bei 808/765432-e notiert?")
    assert answer["card"]["id"] == created.customer.id
    assert [(s["text"], s["author"]) for s in answer["answer"]["sources"]] == [("HUK24 Wechsel", "Laura")]
    # 8. Reine Suchanfragen veraendern keine Daten.
    assert _snapshot() == before


def test_customer_stays_findable_after_reload_and_new_login(app, created):
    """3. Neue Sitzung (erneute Anmeldung) und Neuladen der Seite."""
    first = login(app, "laura@example.com")
    assert first.post("/auth/logout").status_code == 302
    client = login(app, "laura@example.com")
    assert client.get("/zentriq").status_code == 200
    assert _input(client, "808765432e")["card"]["id"] == created.customer.id
    assert client.get(f"{API}/kunden/{created.customer.id}").get_json()["card"]["notes"][0]["text"] == "HUK24 Wechsel"


def test_admin_customer_search_uses_same_stored_data(app, created):
    """Vorhandene Kundensuche (Kundenliste/Kopfzeile des Admins) findet dieselbe Kundenakte."""
    client = login(app, "admin-a@example.com")
    for query in ["808/765432-e", "808765432E", "808 765432 e", "Max Mustermann"]:
        html = client.get("/search", query_string={"q": query}).get_data(as_text=True)
        assert f"/customers/{created.customer.id}\"" in html, query


def test_employee_header_search_finds_note_customer_and_opens_card(app, created):
    """Mitarbeiter finden den aus einer Notiz angelegten Kunden auch in der Kopfzeilensuche."""
    dennis = login(app, "dennis@example.com")
    for query in ["808/765432-e", "808765432E", "Max Mustermann"]:
        html = dennis.get("/search", query_string={"q": query}).get_data(as_text=True)
        assert f"/zentriq?kunde={created.customer.id}\"" in html, query
    assert dennis.get(f"/zentriq?kunde={created.customer.id}").status_code == 200
    assert dennis.get(f"/customers/{created.customer.id}").status_code in (302, 403, 404)
    assert dennis.get(f"{API}/kunden/{created.customer.id}").get_json()["card"]["notes"][0]["author"] == "Laura"
    # Anderes Buero: kein Treffer in der Kopfzeilensuche.
    html = login(app, "bob@example.com").get("/search", query_string={"q": "808/765432-e"}).get_data(as_text=True)
    assert f"kunde={created.customer.id}\"" not in html and "Max Mustermann" not in html.split("</form>", 1)[1]


def test_other_office_has_no_access(app, created):
    """6. Bob (anderes Buero): kein Treffer, kein Zugriff auf Karte oder Notiz."""
    bob = login(app, "bob@example.com")
    before = _snapshot()
    for query in ["808/765432-e", "Max Mustermann", "Mustermann"]:
        data = _input(bob, query)
        assert data["kind"] != "card" or data["card"]["id"] != created.customer.id, query
        assert created.customer.id not in [c["id"] for c in data.get("candidates", [])]
    assert bob.get(f"{API}/kunden/{created.customer.id}").status_code == 404
    status, _data = _post(bob, "/kunden/eingabe", text="HUK24", customer_id=created.customer.id, mode="note")
    assert status == 404
    assert _snapshot() == before


def test_follow_up_note_uses_same_customer(app, created):
    """7. Erneute Notiz (anderer Mitarbeiter, andere Schreibweise) -> dieselbe Kundenakte."""
    data = _input(login(app, "dennis@example.com"), "808 765432 E Max Mustermann nicht erreicht", client_ref="dennis-1")
    assert data["kind"] == "saved" and not data.get("created")
    assert data["card"]["id"] == created.customer.id
    assert len(_customers(created.tenant_a.id, name="Max Mustermann")) == 1
    with bypass_tenant_scope():
        notes = CustomerNote.query.filter_by(customer_id=created.customer.id).order_by(CustomerNote.id).all()
    assert [(n.text, n.created_by_user_id) for n in notes] == [("HUK24 Wechsel", created.laura.id), ("Nicht erreicht", created.dennis.id)]


def test_repeated_and_parallel_submissions_create_one_customer(app, office):
    """Doppelklick (gleiche client_ref) und zweite Anfrage mit neuer client_ref: ein Kunde, eine Notiz."""
    laura = login(app, "laura@example.com")
    first = _input(laura, "808/700001-k Erika Beispiel Termin", client_ref="r1")
    again = _input(laura, "808/700001-k Erika Beispiel Termin", client_ref="r1")
    third = _input(laura, "808700001K Erika Beispiel Termin", client_ref="r2")
    [customer] = _customers(office.tenant_a.id, name="Erika Beispiel")
    assert first["created"] is True and again["duplicate"] is True and third["duplicate"] is True
    assert third["card"]["id"] == customer.id
    with bypass_tenant_scope():
        assert CustomerNote.query.filter_by(customer_id=customer.id).count() == 1


def test_phone_from_creation_is_stored_and_searchable(app, office):
    """Telefonnummer der Neuanlage steht im Telefonfeld (nicht nur im Notiztext); die VN-Nummer nie."""
    laura = login(app, "laura@example.com")
    data = _input(laura, "808/700002-m Erika Muster 0176 1234870 HUK24 Wechsel", client_ref="p1")
    assert data["created"] is True
    assert data["message"] == "Erika Muster wurde angelegt. Telefon 0176 1234870 hinterlegt. Notiz gespeichert: HUK24 Wechsel."
    [customer] = _customers(office.tenant_a.id, name="Erika Muster")
    assert customer.phone == "0176 1234870" and customer.phone_key == "+491761234870"
    assert customer.customer_number_key == "808700002M"
    assert customer.field_sources["phone"] == customer_sources.NOTIZ
    dennis = login(app, "dennis@example.com")
    for query in ["01761234870", "0176/1234870", "+49 176 1234870", "Suche 0176 1234870"]:
        assert _input(dennis, query)["card"]["id"] == customer.id, query
    # VN ohne Telefonnummer: nichts im Telefonfeld.
    _input(laura, "808/700003-n Hans Probe Termin", client_ref="p2")
    [other] = _customers(office.tenant_a.id, name="Hans Probe")
    assert other.phone is None and other.phone_key is None


def test_unknown_number_next_to_known_name_finds_customer_and_keeps_number(app, created):
    """Eine (noch) nirgends hinterlegte Telefonnummer verdraengt den Namenstreffer nicht und geht
    in einer Notiz nicht verloren - die Stammdaten des vorhandenen Kunden bleiben unveraendert."""
    dennis = login(app, "dennis@example.com")
    search = _input(dennis, "Max Mustermann 0176 5550001")
    assert search["kind"] == "card" and search["card"]["id"] == created.customer.id
    note = _input(dennis, "Max Mustermann 0176 5550001 neue Nummer, bitte zurückrufen", client_ref="n-phone")
    assert note["kind"] == "saved"
    assert note["note"]["text"] == "0176 5550001 neue Nummer, bitte zurückrufen"
    assert _customers(created.tenant_a.id, name="Max Mustermann")[0].phone is None


def test_digit_sequence_matching_vn_and_phone_of_different_customers_asks(app, office):
    """Eine reine Ziffernfolge wird nicht nach ihrer Laenge eingeordnet: passt sie als VN-Nummer
    (ohne Buchstaben) zu einem und als Telefonnummer zu einem anderen Kunden -> Auswahl."""
    with use_tenant_id(office.tenant_a.id):
        a = Customer(tenant_id=office.tenant_a.id, name="Vera Nummer", customer_number="017/612348-K")
        b = Customer(tenant_id=office.tenant_a.id, name="Tom Telefon", phone="017612348")
        _db.session.add_all([a, b])
        _db.session.commit()
        ids = {a.id, b.id}
    data = _input(login(app, "dennis@example.com"), "017612348")
    assert data["kind"] == "choose" and {c["id"] for c in data["candidates"]} == ids
    assert data["message"].startswith("Mehrere Kunden passen zu dieser Nummer")


def test_vn_letter_is_relevant(app, created):
    """Der Buchstabe gehoert zur Nummer: 808/765432-F ist ein anderer (unbekannter) Kunde."""
    data = _input(login(app, "dennis@example.com"), "808/765432-f")
    assert data["kind"] == "not_found" and data["search"] is True


def test_no_hit_is_reported_as_such_and_closes_old_card(app, created):
    dennis = login(app, "dennis@example.com")
    for query in ["808/799999-z", "808799999", "Suche Zacharias"]:
        data = _input(dennis, query, selected_id=created.customer.id)
        assert data["kind"] == "not_found" and data["search"] is True, (query, data)
    # Ohne KI-Modell: Namenssuche ohne Treffer ist "kein Treffer", kein KI-Hinweis.
    data = _input(dennis, "Zacharias Unbekannt")
    assert data["kind"] == "not_found" and "Zacharias Unbekannt" in data["message"]


def test_technical_error_is_not_reported_as_no_hit(app, created, monkeypatch):
    def broken(*args, **kwargs):
        raise RuntimeError("Datenbank weg")

    monkeypatch.setattr(customer_notes, "find_by_identifiers", broken)
    before = _snapshot()
    status, data = _post(login(app, "dennis@example.com"), "/kunden/eingabe", text="808/765432-e Max Mustermann HUK24")
    assert status == 500 and data["code"] == "technical"
    assert "Technischer Fehler" in data["error"]
    assert _snapshot() == before


def test_works_without_ai_model(app, created):
    """9. Kein KI-Modell verfuegbar (Testzustand) - externe Aufrufe sind per Fixture verboten."""
    from app.services.ai import router

    assert router.can_answer() is False
    assert _input(login(app, "dennis@example.com"), "Was wurde bei Mustermann notiert?")["card"]["id"] == created.customer.id


# --- 10. Bereits betroffene Datensaetze ----------------------------------------------------------


def _legacy_customer(office, *, phone=None, number="808/766666-H", note="Mustermann 0176 6543210 HUK24", name="Lea Altfall"):
    """Kunde wie aus der bisherigen Funktion: Telefonnummer nur im Notiztext."""
    with use_tenant_id(office.tenant_a.id):
        customer = Customer(
            tenant_id=office.tenant_a.id, name=name, customer_number=number, phone=phone,
            assigned_user_id=office.laura.id, source=customer_sources.NOTIZ,
            field_sources={"name": customer_sources.NOTIZ, "customer_number": customer_sources.NOTIZ},
        )
        _db.session.add(customer)
        _db.session.flush()
        _db.session.add(CustomerNote(tenant_id=office.tenant_a.id, customer_id=customer.id, text=note,
                                     created_by_user_id=office.laura.id, created_at=utcnow_naive()))
        _db.session.commit()
        return customer.id


def test_repair_restores_phone_from_creation_note_and_is_repeatable(app, office):
    customer_id = _legacy_customer(office)
    dennis = login(app, "dennis@example.com")
    assert _input(dennis, "0176 6543210")["kind"] == "not_found"

    dry = check_and_repair(apply=False)
    assert any(f"Kunde {customer_id} " in line and "0176 6543210" in line for line in dry.fixes)
    assert _customers(office.tenant_a.id, id=customer_id)[0].phone is None  # Bericht aendert nichts

    applied = check_and_repair(apply=True)
    assert applied.fixes == dry.fixes
    [customer] = _customers(office.tenant_a.id, id=customer_id)
    assert customer.phone == "0176 6543210" and customer.field_sources["phone"] == customer_sources.NOTIZ
    with bypass_tenant_scope():
        assert CustomerNote.query.filter_by(customer_id=customer_id).one().text == "Mustermann 0176 6543210 HUK24"
    assert _input(dennis, "0176 6543210")["card"]["id"] == customer_id
    assert _input(dennis, "808766666h")["card"]["id"] == customer_id

    assert check_and_repair(apply=True).fixes == []  # wiederholt ausfuehrbar


def test_repair_never_overwrites_and_never_merges(app, office):
    keep = _legacy_customer(office, phone="0171 9999999", name="Lea Altfall")
    dup = _legacy_customer(office, number="808/767777-J", name="Lea Altfall", note="0176 1112223 Termin")
    taken = _legacy_customer(office, number="808/768888-K", name="Udo Vorhanden", note="0171 9999999 Termin")
    report = check_and_repair(apply=True)
    assert _customers(office.tenant_a.id, id=keep)[0].phone == "0171 9999999"
    assert _customers(office.tenant_a.id, id=dup)[0].phone == "0176 1112223"  # eindeutig -> repariert
    assert _customers(office.tenant_a.id, id=taken)[0].phone is None  # Nummer gehoert schon Kunde `keep`
    assert any(f"Kunde {taken} " in line and "gehört bereits" in line for line in report.findings)
    assert any("Gleicher Name „Lea Altfall“" in line for line in report.findings)
    assert len(_customers(office.tenant_a.id, name="Lea Altfall")) == 2


def test_repair_recomputes_stale_search_keys(app, office):
    customer_id = _legacy_customer(office)
    with bypass_tenant_scope():
        _db.session.execute(Customer.__table__.update().where(Customer.id == customer_id).values(customer_number_key=None))
        _db.session.commit()
    dennis = login(app, "dennis@example.com")
    assert _input(dennis, "808/766666-h")["kind"] == "not_found"
    report = check_and_repair(apply=True)
    assert any(f"Kunde {customer_id} " in line and "customer_number_key" in line for line in report.fixes)
    assert _input(dennis, "808/766666-h")["card"]["id"] == customer_id
