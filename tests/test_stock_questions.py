# ruff: noqa: F811  (Fixtures werden aus anderen Testmodulen importiert)
"""Bestandsfragen ohne KI: Zaehlung "zurueckzurufen" laut Leipziger Liste und offene Rueckrufe.

KI-Anbieter sind hier nicht verfuegbar (router.can_answer liefert False) - die Antworten
kommen ausschliesslich aus der Datenbank."""

from app.models import CustomerContact
from app.services import leipziger_todo, stock_questions
from app.tenancy import bypass_tenant_scope
from tests.test_customer_notes import API, _input, office  # noqa: F401
from tests.test_permissions_security import login, world  # noqa: F401

Q = "Wie viele Kunden sind laut Leipziger Liste noch zurückzurufen?"


def _expected(office, broker=None):
    from app.models import Document

    with bypass_tenant_scope():
        doc = Document.query.get(office.doc_list_id)
        entries = leipziger_todo.load_entries(doc)
    todo = [e for e in entries if e.is_todo and (broker is None or e.broker_number == broker)]
    return len({(e.customer or e.number).strip().lower() for e in todo}), len(todo)


def test_detects_only_count_questions():
    assert stock_questions.detect(Q) == "leipziger"
    assert stock_questions.detect("Wie viele Rückrufe sind offen?") == "callbacks"
    assert stock_questions.detect("Was ist in der Leipziger Liste noch zu erledigen?") == "leipziger"
    assert stock_questions.detect("Justin Heller") is None
    assert stock_questions.detect("Schreib eine Mail zum Rückruf") is None


def test_leipziger_count_without_ai_admin_sees_office(app, office):
    from app.services.ai import router

    assert not router.can_answer()  # kein KI-Anbieter
    data = _input(login(app, "admin-a@example.com"), Q)
    customers, entries = _expected(office)
    assert data["kind"] == "facts" and data["count"] == customers and data["entries"] == entries
    assert f"{customers} Kunden mit {entries} offenen Vorgängen" in data["message"] or customers == 1
    assert data["link"]["url"].endswith("tab=zu-erledigen") and data["rule"].startswith("Gezählt:")


def test_leipziger_count_employee_only_own_broker(app, office):
    data = _input(login(app, "laura@example.com"), Q)
    customers, entries = _expected(office, broker="08/5555-B")
    assert data["count"] == customers and data["entries"] == entries
    assert "Ihrer Vermittlernummer" in data["message"]


def test_callback_count_and_repeated_questions(app, office):
    client = login(app, "laura@example.com")
    _input(client, "808/123456-E Justin Heller bitte zurückrufen wegen Kfz")
    with bypass_tenant_scope():
        open_count = CustomerContact.query.filter_by(tenant_id=office.tenant_a.id, status="open").count()
    for _ in range(4):  # mehrere aufeinanderfolgende Anfragen
        data = _input(client, "Wie viele Rückrufe sind offen?")
        assert data["kind"] == "facts" and data["count"] == open_count >= 1
    # Andere Bueros zaehlen nicht mit
    bob = login(app, "bob@example.com")
    assert _input(bob, "Wie viele Rückrufe sind offen?")["count"] == 0


def test_customer_specific_question_is_not_intercepted(app, office):
    data = _input(login(app, "admin-a@example.com"), "Wie viele Rückrufe hat Justin Heller offen?")
    assert data["kind"] != "facts"


def test_chat_answers_count_directly_without_model(app, office):
    client = login(app, "admin-a@example.com")
    cid = client.post(f"{API}/conversations", json={}).get_json()["conversation"]["id"]
    resp = client.post(f"{API}/conversations/{cid}/messages", json={"content": Q}).get_json()
    from app.models.ai import AiMessage

    with bypass_tenant_scope():
        reply = AiMessage.query.get(resp["reply"]["id"])
    customers, entries = _expected(office)
    assert reply.status == "done" and f"{customers} Kunde" in reply.content and f"{entries} offene" in reply.content


def test_direct_standard_question_reaches_chat_without_model(app, office):
    client = login(app, "laura@example.com")
    data = _input(client, "Meine Arbeitszeiten diese Woche")
    assert data["kind"] == "chat"  # Chat beantwortet direkt (ohne Modell)
    assert _input(client, "Erkläre mir die Hausratversicherung")["kind"] == "no_ai"
