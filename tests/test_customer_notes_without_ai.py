# ruff: noqa: F811  (Fixtures werden aus anderen Testmodulen importiert)
"""Kundennotizen ohne verfuegbares KI-Modell und mit alternativen VN-Schreibweisen.

In der Testumgebung ist kein KI-Anbieter eingerichtet (keine API-Keys) - genau der Zustand der
Produktion bei der Fehlermeldung "Derzeit ist kein KI-Modell verfuegbar". Alle strukturierten
Eingaben muessen trotzdem funktionieren; ein externer Aufruf darf nie stattfinden."""

import pytest

from app.models import Customer
from app.services.ai import adapters, router
from app.services.customer_notes import _valid_vn, parse
from app.tenancy import bypass_tenant_scope
from tests.test_customer_notes import _contacts, _input, _notes, ai_available, office  # noqa: F401
from tests.test_permissions_security import login, world  # noqa: F401


@pytest.fixture(autouse=True)
def no_provider_calls(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Externer KI-Aufruf bei einer Kundeneingabe")

    monkeypatch.setattr(adapters, "call", forbidden, raising=False)
    monkeypatch.setattr(router, "complete", forbidden)


def _customers(tenant_id, name):
    with bypass_tenant_scope():
        return Customer.query.filter_by(tenant_id=tenant_id, name=name).all()


@pytest.mark.parametrize(
    "raw",
    ["808/123456-e", "808123456e", "808123456E", "808 123456 E", "808-123456-e", "808/123456e", "808 / 123456 - e"],
)
def test_alternative_vn_spellings_normalize_to_same_key(raw):
    assert parse(f"{raw} Max Mustermann HUK24 Wechsel").vn_keys == ["808123456E"]
    assert _valid_vn(raw) == "808/123456-E"


def test_leading_zeros_and_suffix_are_kept():
    assert parse("012/003456-a").vn_keys == ["012003456A"]
    assert _valid_vn("012003456a-07") == "012/003456-A-07"
    # Keine VN: Telefonnummern, zu kurze/lange Ziffernfolgen, fehlender Buchstabe.
    for text in ("0171 1234567", "808/12345-e", "8080/123456-e", "808/123456", "Termin 14 Uhr"):
        assert parse(text).vn_keys == [], text


def test_ai_unavailable_is_the_test_state():
    assert router.available_models() == [] and router.can_answer() is False


def test_both_spellings_find_the_same_existing_customer(app, office):
    client = login(app, "laura@example.com")
    first = _input(client, "808/123456-e Justin Heller HUK24 Wechsel")
    second = _input(client, "808123456e Justin Heller Kfz-Wechsel")
    assert first["kind"] == second["kind"] == "saved"
    assert first["card"]["id"] == second["card"]["id"] == office.ids["Justin Heller"]
    assert [n.text for n in _notes(office.ids["Justin Heller"])] == ["HUK24 Wechsel", "Kfz-Wechsel"]
    assert _input(client, "808123456E")["card"]["id"] == office.ids["Justin Heller"]


def test_create_search_card_and_callbacks_work_without_ai(app, office):
    client = login(app, "laura@example.com")
    created = _input(client, "808123456e Max Mustermann HUK24 Wechsel")
    # 808123456E gehoert bereits Justin Heller -> Widerspruch statt Neuanlage.
    assert created["kind"] == "conflict" or created.get("card", {}).get("id") == office.ids["Justin Heller"]

    created = _input(client, "808654321e Max Mustermann HUK24 Wechsel", client_ref="x1")
    assert created["kind"] == "saved" and created["created"] is True
    assert created["message"] == "Max Mustermann wurde angelegt. Notiz gespeichert: HUK24 Wechsel."
    [customer] = _customers(office.tenant_a.id, "Max Mustermann")
    assert customer.customer_number == "808/654321-E"
    # Andere Schreibweise derselben Nummer: derselbe Kunde, keine Dublette.
    again = _input(client, "808/654321-E Max Mustermann nicht erreicht")
    assert again["card"]["id"] == customer.id and not again.get("created")
    assert len(_customers(office.tenant_a.id, "Max Mustermann")) == 1
    assert len(_contacts(customer.id)) == 1
    assert client.get("/zentriq/api/rueckrufe").get_json()["callbacks"][0]["customer"]["name"] == "Max Mustermann"
    assert _input(client, "Max Mustermann")["card"]["id"] == customer.id
    assert _input(client, "Was ist bei Max Mustermann noch offen?")["answer"]["sources"]


def test_single_keyword_is_not_treated_as_creation_order(app, office):
    client = login(app, "laura@example.com")
    data = _input(client, "808654321e Max Mustermann HUK24")
    assert data["kind"] == "confirm_create" and data["create"]["name"] == "Max Mustermann"
    assert _customers(office.tenant_a.id, "Max Mustermann") == [] and _notes() == []
    # Ausdruecklich bestaetigt -> angelegt.
    confirmed = _input(client, data["create"]["text"], mode="create", create_new=True, new_name="Max Mustermann")
    assert confirmed["created"] is True and [n.text for n in _notes(confirmed["card"]["id"])] == ["HUK24"]


def test_unclear_name_boundary_asks_for_name(app, office):
    client = login(app, "laura@example.com")
    for text in ("808654321e Max Peter Mustermann HUK24 Wechsel", "808654321e Max Mustermann Schmidt angerufen"):
        data = _input(client, text)
        assert data["kind"] == "need_name", text
    assert data["suggestion"] == "Max Mustermann"
    assert Customer.query.filter(Customer.name.like("%Mustermann%")).count() == 0
    data = _input(client, "808654321e Max Peter Mustermann HUK24 Wechsel")
    saved = _input(client, data["pending_note"], new_name="Max Peter Mustermann", mode="create")
    assert saved["created"] is True and saved["card"]["name"] == "Max Peter Mustermann"
    assert [n.text for n in _notes(saved["card"]["id"])] == ["HUK24 Wechsel"]
    # Komma setzt die Grenze ausdruecklich.
    data = _input(client, "808111222e Erika Muster, Schmidt angerufen")
    assert data["created"] is True and data["card"]["name"] == "Erika Muster"
    # Bekannter Fachbegriff direkt nach dem Namen: Grenze eindeutig.
    data = _input(client, "808111333e Hans Probe Police verschickt")
    assert data["created"] is True and data["card"]["name"] == "Hans Probe"
    assert [n.text for n in _notes(data["card"]["id"])] == ["Police verschickt"]


def test_free_text_without_ai_gives_hint_or_asks_for_missing_vn(app, office):
    client = login(app, "laura@example.com")
    data = _input(client, "Erkläre mir Teilkasko")
    assert data["kind"] == "no_ai" and "Kundennotizen" in data["message"]
    data = _input(client, "Anna Beispiel HUK24 Wechsel")
    assert data["kind"] == "need_vn" and data["name"] == "Anna Beispiel"
    saved = _input(client, data["pending_note"], new_vn="808999111a", new_name=data["name"], mode="create")
    assert saved["created"] is True and saved["card"]["vn"] == "808/999111-A"
    assert _notes() and not _contacts()


def test_with_ai_free_text_goes_to_chat(app, office, ai_available):
    client = login(app, "laura@example.com")
    assert _input(client, "Erkläre mir Teilkasko")["kind"] == "chat"
    assert _input(client, "Anna Beispiel HUK24 Wechsel")["kind"] == "chat"
    # Strukturierte Eingaben bleiben lokal - auch mit KI.
    assert _input(client, "808123456e Justin Heller HUK24 Wechsel")["kind"] == "saved"


def test_router_message_explains_zero_budget(app, db):
    from app.models.ai import AiModel
    from app.services.ai import catalog

    app.config["OPENAI_API_KEY"] = "test-key"
    with app.test_request_context():
        catalog.ensure_defaults()
        assert [m.ref for m in router.available_models()] == [m.ref for m in AiModel.query.filter_by(provider="openai", enabled=True)]
        assert router.can_answer() is False  # nur kostenpflichtig, Kostengrenze 0
