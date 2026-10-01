# ruff: noqa: F811  (Fixture "world" wird aus test_permissions_security importiert)
"""Zentriq-Kundenstamm: Kunden aus der Leipziger Liste und aus Memos, Dubletten-Schutz,
Datenherkunft, Kundenansicht, globale Suche und strikte Mandantentrennung.

Zwei-Bueros-Szenario aus test_permissions_security (Buero A: Admin A, Dennis, Laura,
SUPER_ADMIN Justin; Buero B: Admin B, Bob). Rechte werden ueber echte Requests geprueft."""

import io
from datetime import date, datetime, timezone

import pytest

from app.models import Customer, CustomerMemo, Document, LeipzigerEntry
from app.models.enums import DocStatus, DocType
from app.services import customer_sources
from app.services.customer_overview import customer_entries, customer_link_counts
from app.services.documents import apply_leipziger_liste_extraction
from app.services.leipziger_entries import rebuild_entries
from app.services.llm.schemas import ExtractedCustomer, LeipzigerListeExtraction, LeipzigerListeRow
from app.tenancy import bypass_tenant_scope, use_tenant_id
from app.utils.customer_keys import name_key
from tests.test_permissions_security import login, world  # noqa: F401

PETER = "Hallo, hier ist Peter Müller. Sie erreichen mich unter 0176 12345678. Es geht um meine Hausratversicherung."


# --- Hilfen ---------------------------------------------------------------------------------


def _row(contract, name="Max Mustermann", dob=date(1980, 5, 1), plz="04109", city="Leipzig", broker="08/4205-M", status="ANG"):
    return LeipzigerListeRow(
        customer=ExtractedCustomer(name=name, postal_code=plz, city=city, date_of_birth=dob),
        contract_number=contract,
        status_code=status,
        product_line="KPKW",
        broker_number=broker,
    )


def _import(db, tenant, rows, filename, day=1):
    """Importiert eine Leipziger Liste wie der Hintergrund-Task (Auswertung + Vorgaenge)."""
    with use_tenant_id(tenant.id):
        document = Document(
            tenant_id=tenant.id,
            filename=filename,
            original_filename=filename,
            file_path=f"/tmp/{filename}",
            status=DocStatus.DONE,
            doc_type=DocType.LEIPZIGER_LISTE,
            uploaded_at=datetime(2026, 9, day, 8, 0, tzinfo=timezone.utc),
        )
        db.session.add(document)
        db.session.flush()
        apply_leipziger_liste_extraction(document, LeipzigerListeExtraction(rows=rows))
        db.session.flush()
        rebuild_entries(document)
        db.session.commit()
        return document.id


def _customers(tenant):
    with use_tenant_id(tenant.id):
        return Customer.query.order_by(Customer.id).all()


def _customer(db, tenant, name, phone=None, number=None, dob=None):
    with use_tenant_id(tenant.id):
        customer = Customer(tenant_id=tenant.id, name=name, phone=phone, customer_number=number, date_of_birth=dob)
        db.session.add(customer)
        db.session.commit()
        return customer.id


def _memos(tenant):
    with use_tenant_id(tenant.id):
        return CustomerMemo.query.all()


def _transcribe(client, monkeypatch, transcript):
    monkeypatch.setattr("app.blueprints.dashboard.routes.transcribe_audio", lambda filename, content: transcript)
    resp = client.post(
        "/sprachnachrichten/transkribieren",
        data={"file": (io.BytesIO(b"ID3audio"), "anruf.mp3")},
        content_type="multipart/form-data",
        headers={"Accept": "application/json"},
    )
    assert resp.status_code == 200
    return resp.get_json()["token"]


def _recognize(client, monkeypatch, transcript):
    """Upload -> Transkript -> Kundenabgleich, wie memo.js."""
    token = _transcribe(client, monkeypatch, transcript)
    resp = client.post("/sprachnachrichten/kundenabgleich", json={"transcript": transcript, "token": token})
    assert resp.status_code == 200
    return token, resp.get_json()


# --- Leipziger Liste ------------------------------------------------------------------------


def test_customer_created_from_leipziger_list_with_all_present_fields(app, world, db):
    _import(db, world.tenant_a, [_row("720/307259-C-14")], "kw36.pdf")
    created = [c for c in _customers(world.tenant_a) if c.name == "Max Mustermann"]
    assert len(created) == 1
    customer = created[0]
    assert (customer.postal_code, customer.city, customer.date_of_birth) == ("04109", "Leipzig", date(1980, 5, 1))
    assert customer.broker_number == "08/4205-M"
    assert customer.source == customer_sources.LEIPZIGER_LISTE
    assert customer.field_sources["broker_number"] == customer_sources.LEIPZIGER_LISTE
    with use_tenant_id(world.tenant_a.id):
        entry = LeipzigerEntry.query.filter_by(contract_number="720/307259-C-14").one()
    assert entry.customer_id == customer.id


def test_weekly_reimport_creates_no_duplicate(app, world, db):
    before = len(_customers(world.tenant_a))
    _import(db, world.tenant_a, [_row("720/307259-C-14"), _row("608/475130-N", name="Erika Beispiel", dob=date(1975, 1, 2))], "kw36.pdf", day=1)
    _import(db, world.tenant_a, [_row("720/307259-C-14", status="NEU"), _row("608/475130-N", name="Erika Beispiel", dob=date(1975, 1, 2))], "kw37.pdf", day=8)
    customers = _customers(world.tenant_a)
    assert len(customers) == before + 2
    max_customer = next(c for c in customers if c.name == "Max Mustermann")
    # Derselbe Vorgang aus zwei Wochenlisten erscheint einmal - mit dem Stand der neuesten Liste.
    with use_tenant_id(world.tenant_a.id):
        entries = customer_entries(max_customer)
        counts = customer_link_counts([max_customer.id])
    assert len(entries) == 1
    assert entries[0]["entry"].status_code == "NEU"
    assert entries[0]["document_name"] == "kw37.pdf"
    assert counts[max_customer.id]["leipziger"] == 1


def test_multiple_leipziger_entries_belong_to_one_customer(app, world, db):
    rows = [_row(f"720/30725{i}-C-14") for i in range(4)]
    _import(db, world.tenant_a, rows, "kw36.pdf")
    max_customers = [c for c in _customers(world.tenant_a) if c.name == "Max Mustermann"]
    assert len(max_customers) == 1
    with use_tenant_id(world.tenant_a.id):
        entries = LeipzigerEntry.query.filter(LeipzigerEntry.contract_number.like("720/30725%")).all()
        assert {entry.customer_id for entry in entries} == {max_customers[0].id}
        assert len(customer_entries(max_customers[0])) == 4


def test_reimport_without_birth_date_or_postcode_matches_via_contract_number(app, world, db):
    row = dict(name="Lisa Ohnedaten", dob=None, plz=None, city=None)
    _import(db, world.tenant_a, [_row("720/111111-C-14", **row)], "kw36.pdf", day=1)
    _import(db, world.tenant_a, [_row("720/111111-C-14", **row)], "kw37.pdf", day=8)
    assert len([c for c in _customers(world.tenant_a) if c.name == "Lisa Ohnedaten"]) == 1


def test_same_name_with_different_birth_dates_stays_separate(app, world, db):
    _import(
        db,
        world.tenant_a,
        [_row("720/222222-C-14", name="Michael Müller", dob=date(1970, 1, 1)), _row("720/333333-C-14", name="Michael Müller", dob=date(1990, 2, 2))],
        "kw36.pdf",
    )
    assert len([c for c in _customers(world.tenant_a) if c.name == "Michael Müller"]) == 2


def test_leipziger_import_never_matches_customers_of_other_office(app, world, db):
    _import(db, world.tenant_b, [_row("720/307259-C-14")], "b.pdf")
    _import(db, world.tenant_a, [_row("720/307259-C-14")], "a.pdf")
    a = [c for c in _customers(world.tenant_a) if c.name == "Max Mustermann"]
    b = [c for c in _customers(world.tenant_b) if c.name == "Max Mustermann"]
    assert len(a) == 1 and len(b) == 1 and a[0].id != b[0].id


def test_umlaut_spellings_share_one_name_key():
    assert name_key("Anna Mueller") == name_key(" anna  müller ") == name_key("Müller, Anna")
    assert name_key("Groß") == name_key("Gross")


# --- Memo -----------------------------------------------------------------------------------


def test_memo_recognizes_existing_customer_by_phone_and_links_memo(app, world, db, monkeypatch):
    customer_id = _customer(db, world.tenant_a, "Max Mustermann", phone="+49 171 1234567")
    transcript = "Guten Tag, hier ist Max Mustermann, bitte um Rückruf unter 0171-1234567."
    _token, body = _recognize(login(app, "dennis@example.com"), monkeypatch, transcript)
    assert body["status"] == "unique" and body["matched_by"] == "phone"
    assert body["assigned"] is True
    memos = _memos(world.tenant_a)
    assert [(memo.customer_id, memo.matched_by, memo.transcript) for memo in memos] == [(customer_id, "phone", transcript)]
    assert len(_customers(world.tenant_a)) == len({c.id for c in _customers(world.tenant_a)})


def test_memo_recognizes_existing_customer_by_customer_number_and_fills_missing_phone(app, world, db, monkeypatch):
    customer_id = _customer(db, world.tenant_a, "Erika Beispiel", number="KD-998877")
    transcript = "Hallo, meine Kundennummer ist KD 998877, erreichbar unter 0151 2223334."
    _token, body = _recognize(login(app, "admin-a@example.com"), monkeypatch, transcript)
    assert body["status"] == "unique" and body["matched_by"] == "customer_number" and body["assigned"]
    assert body["filled_fields"] == ["phone"]
    with use_tenant_id(world.tenant_a.id):
        customer = db.session.get(Customer, customer_id)
        assert customer.phone == "0151 2223334"
        assert customer.customer_number == "KD-998877"  # vorhandene Daten bleiben
        assert customer.field_sources["phone"] == customer_sources.MEMO


def test_name_alone_never_links_or_merges_automatically(app, world, db, monkeypatch):
    _customer(db, world.tenant_a, "Peter Müller", phone="0341 111111")
    _customer(db, world.tenant_a, "Peter Müller", phone="0341 222222")
    _token, body = _recognize(login(app, "dennis@example.com"), monkeypatch, "Hier ist Peter Müller, bitte zurückrufen.")
    assert body["status"] in ("possible", "multiple")
    assert body["assigned"] is False
    assert _memos(world.tenant_a) == []
    assert len([c for c in _customers(world.tenant_a) if c.name == "Peter Müller"]) == 2


def test_untrusted_text_is_never_linked(app, world, db):
    """Ohne Token aus einer Transkription (z. B. selbst eingegebener Text) wird nichts gespeichert."""
    _customer(db, world.tenant_a, "Max Mustermann", phone="0171 1234567")
    client = login(app, "dennis@example.com")
    body = client.post("/sprachnachrichten/kundenabgleich", json={"transcript": "Rückruf unter 0171 1234567"}).get_json()
    assert body["status"] == "unique" and body["assigned"] is False
    resp = client.post("/sprachnachrichten/zuordnen", json={"transcript": "Rückruf unter 0171 1234567", "token": "gefälscht", "customer_id": 1})
    assert resp.status_code == 400
    assert _memos(world.tenant_a) == []


def test_new_memo_caller_can_be_saved_as_customer(app, world, db, monkeypatch):
    client = login(app, "dennis@example.com")
    token, body = _recognize(client, monkeypatch, PETER)
    assert body["status"] == "none"
    assert body["suggestion"] == {"name": "Peter Müller", "phone": "0176 12345678", "customer_number": None}
    assert not [c for c in _customers(world.tenant_a) if c.name == "Peter Müller"]  # nie automatisch

    resp = client.post("/sprachnachrichten/kunde-anlegen", json={"transcript": PETER, "token": token, **body["suggestion"]})
    assert resp.status_code == 201
    created = [c for c in _customers(world.tenant_a) if c.name == "Peter Müller"]
    assert len(created) == 1
    assert created[0].source == customer_sources.MEMO and created[0].phone == "0176 12345678"
    assert [memo.customer_id for memo in _memos(world.tenant_a)] == [created[0].id]
    # Mitarbeiter erhalten keinen Link zur Kundenakte.
    assert resp.get_json()["customer"]["url"] is None

    # Zweites Anlegen mit derselben Nummer: blockiert, kein zweiter Kunde.
    again = client.post("/sprachnachrichten/kunde-anlegen", json={"transcript": PETER, "token": token, "name": "P. Müller", "phone": "+49176 12345678"})
    assert again.status_code == 409 and again.get_json()["blocking"] is True
    assert len([c for c in _customers(world.tenant_a) if "Müller" in c.name]) == 1


def test_same_name_requires_explicit_confirmation(app, world, db, monkeypatch):
    _customer(db, world.tenant_a, "Peter Müller", phone="0341 111111")
    client = login(app, "dennis@example.com")
    token = _transcribe(client, monkeypatch, PETER)
    payload = {"transcript": PETER, "token": token, "name": "Peter Müller", "phone": "0176 12345678"}
    resp = client.post("/sprachnachrichten/kunde-anlegen", json=payload)
    assert resp.status_code == 409 and resp.get_json()["blocking"] is False
    resp = client.post("/sprachnachrichten/kunde-anlegen", json={**payload, "confirm_same_name": True})
    assert resp.status_code == 201
    assert len([c for c in _customers(world.tenant_a) if c.name == "Peter Müller"]) == 2


def test_memo_can_be_assigned_to_selected_customer(app, world, db, monkeypatch):
    customer_id = _customer(db, world.tenant_a, "Peter Müller", phone="0341 111111")
    client = login(app, "dennis@example.com")
    token = _transcribe(client, monkeypatch, "Hier ist Peter Müller, bitte zurückrufen.")
    payload = {"transcript": "Hier ist Peter Müller, bitte zurückrufen.", "token": token, "customer_id": customer_id}
    assert client.post("/sprachnachrichten/zuordnen", json=payload).status_code == 200
    assert client.post("/sprachnachrichten/zuordnen", json=payload).status_code == 200  # idempotent
    assert [(m.customer_id, m.matched_by) for m in _memos(world.tenant_a)] == [(customer_id, "selected")]


# --- Mandantentrennung ----------------------------------------------------------------------


def test_no_cross_tenant_memo_recognition(app, world, db, monkeypatch):
    _customer(db, world.tenant_b, "Max Mustermann", phone="0171 1234567", number="123456789")
    _token, body = _recognize(login(app, "admin-a@example.com"), monkeypatch, "Hier ist Max Mustermann, Kundennummer 123456789, Telefon 0171 1234567.")
    assert body["customers"] == [] and body["assigned"] is False
    assert _memos(world.tenant_b) == []


def test_no_cross_tenant_duplicates_block_or_merge(app, world, db, monkeypatch):
    """Gleiche Telefonnummer in Buero B verhindert nicht das Anlegen in Buero A."""
    _customer(db, world.tenant_b, "Peter Müller", phone="0176 12345678")
    client = login(app, "dennis@example.com")
    token, body = _recognize(client, monkeypatch, PETER)
    assert body["suggestion"]["name"] == "Peter Müller"
    resp = client.post("/sprachnachrichten/kunde-anlegen", json={"transcript": PETER, "token": token, **body["suggestion"]})
    assert resp.status_code == 201
    assert len([c for c in _customers(world.tenant_b) if c.name == "Peter Müller"]) == 1


def test_memo_cannot_be_assigned_to_customer_of_other_office(app, world, db, monkeypatch):
    foreign_id = _customer(db, world.tenant_b, "Fremder Kunde", phone="0341 999999")
    client = login(app, "dennis@example.com")
    token = _transcribe(client, monkeypatch, "Bitte Rückruf.")
    resp = client.post("/sprachnachrichten/zuordnen", json={"transcript": "Bitte Rückruf.", "token": token, "customer_id": foreign_id})
    assert resp.status_code == 404
    assert _memos(world.tenant_b) == []


def test_transcript_token_is_bound_to_user(app, world, db, monkeypatch):
    customer_id = _customer(db, world.tenant_a, "Max Mustermann", phone="0171 1234567")
    token = _transcribe(login(app, "dennis@example.com"), monkeypatch, "Bitte Rückruf.")
    resp = login(app, "laura@example.com").post(
        "/sprachnachrichten/zuordnen", json={"transcript": "Bitte Rückruf.", "token": token, "customer_id": customer_id}
    )
    assert resp.status_code == 400


def test_no_cross_tenant_customer_search(app, world, db):
    _customer(db, world.tenant_b, "Bettina Fremd", phone="0171 5556667", number="FREMD-1")
    client = login(app, "admin-a@example.com")
    for query in ("Bettina", "0171 5556667", "FREMD-1"):
        assert "Bettina Fremd" not in client.get("/customers", query_string={"q": query}).get_data(as_text=True)
        assert "Bettina Fremd" not in client.get("/search", query_string={"q": query}).get_data(as_text=True)
    assert "Bettina Fremd" in login(app, "admin-b@example.com").get("/customers?q=Bettina").get_data(as_text=True)


@pytest.mark.parametrize("path", ["/customers", "/customers/{id}", "/search?q=Max"])
def test_super_admin_cannot_read_office_customer_data(app, world, db, path):
    customer_id = _customer(db, world.tenant_a, "Max Mustermann", phone="0171 1234567")
    resp = login(app, "justin@example.com").get(path.format(id=customer_id))
    assert resp.status_code == 403
    assert "Max Mustermann" not in resp.get_data(as_text=True)


def test_super_admin_cannot_use_memo_customer_endpoints(app, world, db):
    client = login(app, "justin@example.com")
    for path in ("/sprachnachrichten/zuordnen", "/sprachnachrichten/kunde-anlegen", "/sprachnachrichten/kundenabgleich"):
        assert client.post(path, json={"transcript": "x", "token": "x"}).status_code == 403


def test_employee_has_no_customer_directory(app, world, db):
    customer_id = _customer(db, world.tenant_a, "Max Mustermann")
    client = login(app, "dennis@example.com")
    assert client.get("/customers").status_code == 403
    assert client.get(f"/customers/{customer_id}").status_code == 403


# --- Kundenansicht und globale Suche --------------------------------------------------------


def test_customer_list_search_by_phone_number_and_contract(app, world, db):
    _import(db, world.tenant_a, [_row("720/307259-C-14"), _row("720/307260-C-14")], "kw36.pdf")
    with use_tenant_id(world.tenant_a.id):
        customer = Customer.query.filter_by(name="Max Mustermann").one()
        customer.phone = "0171 1234567"
        customer.customer_number = "123456"
        db.session.add(CustomerMemo(tenant_id=world.tenant_a.id, customer_id=customer.id, transcript="Text", transcript_sha256="x" * 64, matched_by="phone"))
        db.session.commit()
    client = login(app, "admin-a@example.com")
    for query in ("Mustermann", "+49 171 1234567", "00491711234567", "123456", "307259"):
        html = client.get("/customers", query_string={"q": query}).get_data(as_text=True)
        assert "Max Mustermann" in html, query
    html = client.get("/customers?q=Mustermann").get_data(as_text=True)
    assert "08/4205-M" in html
    assert 'data-label="Leipziger-Vorgänge">2<' in html and 'data-label="Memos">1<' in html
    assert "Max Mustermann" not in client.get("/customers?q=Niemand").get_data(as_text=True)


def test_global_search_uses_customer_master_with_counts(app, world, db):
    _import(db, world.tenant_a, [_row("720/307259-C-14"), _row("720/307260-C-14"), _row("720/307261-C-14")], "kw36.pdf")
    with use_tenant_id(world.tenant_a.id):
        customer = Customer.query.filter_by(name="Max Mustermann").one()
        customer.phone = "0171 1234567"
        db.session.commit()
    html = login(app, "admin-a@example.com").get("/search?q=0171 1234567").get_data(as_text=True)
    assert "Max Mustermann" in html
    assert 'data-label="Leipziger">3<' in html and 'data-label="Memos">0<' in html
    assert f"/customers/{customer.id}" in html


def test_customer_detail_shows_master_data_entries_memos_and_sources(app, world, db, monkeypatch):
    _import(db, world.tenant_a, [_row("720/307259-C-14")], "kw36.pdf")
    transcript = "Hallo, Max Mustermann hier, Kundennummer 123456, bitte Rückruf."
    with use_tenant_id(world.tenant_a.id):
        customer = Customer.query.filter_by(name="Max Mustermann").one()
        customer.customer_number = "123456"
        db.session.commit()
        customer_id = customer.id
    client = login(app, "admin-a@example.com")
    _token, body = _recognize(client, monkeypatch, transcript)
    assert body["assigned"] is True
    html = client.get(f"/customers/{customer_id}").get_data(as_text=True)
    for text in ("Stammdaten", "Kundennummer", "123456", "Vermittlernummer", "08/4205-M", "04109 Leipzig", "01.05.1980",
                 "Leipziger Liste", "720/307259-C-14", "Memos", transcript, "Text kopieren", "Angelegt aus: Leipziger Liste"):
        assert text in html, text


def test_merge_moves_memos_and_leipziger_entries(app, world, db):
    from app.services.customer_duplicates import merge_customers

    _import(db, world.tenant_a, [_row("720/307259-C-14", name="Max Mustermann", dob=date(1980, 5, 1))], "a.pdf")
    with use_tenant_id(world.tenant_a.id):
        source = Customer.query.filter_by(name="Max Mustermann").one()
        target = Customer(tenant_id=world.tenant_a.id, name="Max Mustermann", phone="0171 1234567")
        db.session.add(target)
        db.session.flush()
        db.session.add(CustomerMemo(tenant_id=world.tenant_a.id, customer_id=source.id, transcript="T", transcript_sha256="a" * 64, matched_by="phone"))
        db.session.commit()
        admin = db.session.get(type(world.admin_a), world.admin_a_id)
        merge_customers(target, source, admin)
        db.session.commit()
        assert CustomerMemo.query.one().customer_id == target.id
        assert {entry.customer_id for entry in LeipzigerEntry.query.filter_by(contract_number="720/307259-C-14")} == {target.id}
    with bypass_tenant_scope():
        assert db.session.get(Customer, source.id) is None
