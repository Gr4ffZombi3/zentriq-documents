import json
from datetime import date, timedelta
from types import SimpleNamespace

from app.models import Customer, Document, DocumentCustomer, Recommendation, Task, User
from app.models.enums import Priority, RecommendationStatus, RecommendationType, TaskStatus, TaskType
from app.services.analysis.chat_assistant import (
    answer_chat_query,
    tool_list_customers_by_cross_sell_potential,
    tool_list_customers_by_sales_risk,
    tool_list_customers_missing_product,
    tool_list_customers_to_call_today,
    tool_list_customers_with_no_response,
    tool_list_customers_with_only_product,
    tool_list_customers_with_single_offer_only,
    tool_list_stale_tasks,
)


def make_user(db, tenant_id, email):
    user = User(tenant_id=tenant_id, email=email)
    user.set_password("passwort123")
    db.session.add(user)
    db.session.commit()
    return user


def make_customer(db, tenant_id, name, assigned_user_id=None):
    customer = Customer(tenant_id=tenant_id, name=name, assigned_user_id=assigned_user_id)
    db.session.add(customer)
    db.session.commit()
    return customer


def make_document(db, tenant_id, filename="liste.pdf"):
    document = Document(
        filename=filename, original_filename=filename, file_path=f"/tmp/{filename}", tenant_id=tenant_id
    )
    db.session.add(document)
    db.session.commit()
    return document


def link_rows(db, tenant_id, document, customer, row_data):
    dc = DocumentCustomer(document=document, customer=customer, tenant_id=tenant_id, row_data=row_data)
    db.session.add(dc)
    db.session.commit()
    return dc


def test_list_customers_to_call_today(app, db, tenant):
    user = make_user(db, tenant.id, "a@example.com")
    customer = make_customer(db, tenant.id, "Anzurufen", assigned_user_id=user.id)
    db.session.add(
        Task(
            tenant_id=tenant.id, customer=customer, assigned_user_id=user.id, type=TaskType.CALL_TODAY,
            title="📞 Heute anrufen", priority=Priority.HIGH, status=TaskStatus.OPEN, due_date=date.today(),
        )
    )
    db.session.commit()

    results = tool_list_customers_to_call_today(user.id)
    assert [r["customer_name"] for r in results] == ["Anzurufen"]


def test_list_customers_with_single_offer_only(app, db, tenant):
    user = make_user(db, tenant.id, "b@example.com")
    document = make_document(db, tenant.id)
    single = make_customer(db, tenant.id, "Ein Angebot", assigned_user_id=user.id)
    multiple = make_customer(db, tenant.id, "Mehrere Angebote", assigned_user_id=user.id)
    link_rows(db, tenant.id, document, single, [{"is_angebot": True}])
    link_rows(db, tenant.id, document, multiple, [{"is_angebot": True}, {"is_angebot": True}])

    results = tool_list_customers_with_single_offer_only(user.id)
    assert [r["customer_name"] for r in results] == ["Ein Angebot"]


def test_list_customers_with_no_response(app, db, tenant):
    user = make_user(db, tenant.id, "c@example.com")
    document = make_document(db, tenant.id)
    customer = make_customer(db, tenant.id, "Keine Rueckmeldung", assigned_user_id=user.id)
    document.uploaded_at = date.today() - timedelta(days=10)
    db.session.commit()
    link_rows(db, tenant.id, document, customer, [{"is_angebot": True}])

    results = tool_list_customers_with_no_response(user.id)
    assert [r["customer_name"] for r in results] == ["Keine Rueckmeldung"]


def test_list_customers_with_only_product(app, db, tenant):
    user = make_user(db, tenant.id, "d@example.com")
    document = make_document(db, tenant.id)
    only_kfz = make_customer(db, tenant.id, "Nur KFZ", assigned_user_id=user.id)
    kfz_and_hausrat = make_customer(db, tenant.id, "KFZ und Hausrat", assigned_user_id=user.id)
    link_rows(db, tenant.id, document, only_kfz, [{"products": ["KFZ"]}])
    link_rows(db, tenant.id, document, kfz_and_hausrat, [{"products": ["KFZ", "Hausrat"]}])

    results = tool_list_customers_with_only_product(user.id, "KFZ")
    assert [r["customer_name"] for r in results] == ["Nur KFZ"]


def test_list_customers_missing_product(app, db, tenant):
    user = make_user(db, tenant.id, "e@example.com")
    document = make_document(db, tenant.id)
    missing = make_customer(db, tenant.id, "Ohne Haftpflicht", assigned_user_id=user.id)
    has_it = make_customer(db, tenant.id, "Mit Haftpflicht", assigned_user_id=user.id)
    link_rows(db, tenant.id, document, missing, [{"products": ["KFZ"]}])
    link_rows(db, tenant.id, document, has_it, [{"products": ["KFZ", "Privathaftpflicht"]}])

    results = tool_list_customers_missing_product(user.id, "Privathaftpflicht")
    assert [r["customer_name"] for r in results] == ["Ohne Haftpflicht"]


def test_list_customers_by_sales_risk(app, db, tenant):
    user = make_user(db, tenant.id, "f@example.com")
    document = make_document(db, tenant.id)
    at_risk = make_customer(db, tenant.id, "Risiko Kunde", assigned_user_id=user.id)
    db.session.add(
        Recommendation(
            tenant_id=tenant.id, document=document, customer=at_risk,
            type=RecommendationType.SALES_RISK_MULTIPLE_OFFERS_NO_CLOSURE,
            label="Vertriebsrisiko", priority=Priority.HIGH, status=RecommendationStatus.OPEN,
            explanation="3 Angebote ohne Abschluss erkannt. Vertriebsrisiko.",
        )
    )
    db.session.commit()

    results = tool_list_customers_by_sales_risk(user.id)
    assert [r["customer_name"] for r in results] == ["Risiko Kunde"]


def test_list_customers_by_cross_sell_potential(app, db, tenant):
    user = make_user(db, tenant.id, "g@example.com")
    document = make_document(db, tenant.id)
    high = make_customer(db, tenant.id, "Hohes Potenzial", assigned_user_id=user.id)
    none_customer = make_customer(db, tenant.id, "Kein Potenzial", assigned_user_id=user.id)
    link_rows(
        db, tenant.id, document, high,
        [{"products": ["KFZ", "Hausrat"], "cross_sell_opportunity": True, "priority": "high"}],
    )
    link_rows(db, tenant.id, document, none_customer, [{"products": []}])

    results = tool_list_customers_by_cross_sell_potential(user.id)
    names = [r["customer_name"] for r in results]
    assert names[0] == "Hohes Potenzial"
    assert "Kein Potenzial" not in names


def test_list_stale_tasks(app, db, tenant):
    user = make_user(db, tenant.id, "h@example.com")
    customer = make_customer(db, tenant.id, "Alter Vorgang", assigned_user_id=user.id)
    db.session.add(
        Task(
            tenant_id=tenant.id, customer=customer, assigned_user_id=user.id, type=TaskType.OTHER,
            title="Alt", priority=Priority.LOW, status=TaskStatus.OPEN,
            due_date=date.today() - timedelta(days=40),
        )
    )
    db.session.commit()

    results = tool_list_stale_tasks(user.id)
    assert [r["customer_name"] for r in results] == ["Alter Vorgang"]


def test_tools_never_leak_other_users_customers(app, db, tenant):
    user_a = make_user(db, tenant.id, "user-a@example.com")
    user_b = make_user(db, tenant.id, "user-b@example.com")
    make_customer(db, tenant.id, "Kunde von A", assigned_user_id=user_a.id)
    make_customer(db, tenant.id, "Kunde von B", assigned_user_id=user_b.id)

    document = make_document(db, tenant.id)
    customer_a = Customer.query.filter_by(name="Kunde von A").one()
    customer_b = Customer.query.filter_by(name="Kunde von B").one()
    link_rows(db, tenant.id, document, customer_a, [{"products": ["KFZ"]}])
    link_rows(db, tenant.id, document, customer_b, [{"products": ["KFZ"]}])

    results_a = tool_list_customers_with_only_product(user_a.id, "KFZ")
    assert [r["customer_name"] for r in results_a] == ["Kunde von A"]


class FakeToolCallClient:
    def __init__(self, tool_calls):
        self._tool_calls = tool_calls
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        message = SimpleNamespace(tool_calls=self._tool_calls)
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])


def make_tool_call(name: str, arguments: dict):
    function = SimpleNamespace(name=name, arguments=json.dumps(arguments))
    return SimpleNamespace(function=function)


def test_answer_chat_query_dispatches_to_matched_tool(app, db, tenant, monkeypatch):
    user = make_user(db, tenant.id, "chat-a@example.com")
    customer = make_customer(db, tenant.id, "Chat Kunde", assigned_user_id=user.id)
    db.session.add(
        Task(
            tenant_id=tenant.id, customer=customer, assigned_user_id=user.id, type=TaskType.CALL_TODAY,
            title="📞 Heute anrufen", priority=Priority.HIGH, status=TaskStatus.OPEN, due_date=date.today(),
        )
    )
    db.session.commit()

    fake_client = FakeToolCallClient([make_tool_call("list_customers_to_call_today", {})])
    monkeypatch.setattr("app.services.analysis.chat_assistant.get_openai_client", lambda: fake_client)

    with app.app_context():
        result = answer_chat_query(user.id, "Welche Kunden sollte ich heute anrufen?")

    assert result["tool_used"] == "list_customers_to_call_today"
    assert "Chat Kunde" in result["answer"]
    assert result["results"][0]["customer_name"] == "Chat Kunde"


def test_answer_chat_query_passes_tool_arguments(app, db, tenant, monkeypatch):
    user = make_user(db, tenant.id, "chat-b@example.com")
    document = make_document(db, tenant.id)
    customer = make_customer(db, tenant.id, "Nur KFZ Kunde", assigned_user_id=user.id)
    link_rows(db, tenant.id, document, customer, [{"products": ["KFZ"]}])

    fake_client = FakeToolCallClient([make_tool_call("list_customers_with_only_product", {"product": "KFZ"})])
    monkeypatch.setattr("app.services.analysis.chat_assistant.get_openai_client", lambda: fake_client)

    with app.app_context():
        result = answer_chat_query(user.id, "Welche Kunden besitzen nur KFZ?")

    assert result["tool_used"] == "list_customers_with_only_product"
    assert result["results"][0]["customer_name"] == "Nur KFZ Kunde"


def test_answer_chat_query_falls_back_without_tool_call(app, monkeypatch):
    fake_client = FakeToolCallClient([])
    monkeypatch.setattr("app.services.analysis.chat_assistant.get_openai_client", lambda: fake_client)

    with app.app_context():
        result = answer_chat_query(1, "Irgendwas Unklares")

    assert result["tool_used"] is None
    assert result["results"] == []


def test_answer_chat_query_falls_back_on_exception(app, monkeypatch):
    def raise_error():
        raise RuntimeError("OpenAI nicht erreichbar")

    monkeypatch.setattr("app.services.analysis.chat_assistant.get_openai_client", raise_error)

    with app.app_context():
        result = answer_chat_query(1, "Welche Kunden sollte ich anrufen?")

    assert result["tool_used"] is None
    assert "Fehler" in result["answer"]


def test_chat_route_requires_login(client):
    resp = client.post("/api/chat", json={"question": "Wer soll heute angerufen werden?"})
    assert resp.status_code == 302


def test_chat_route_returns_answer_for_logged_in_user(auth_client, db, tenant, user, monkeypatch):
    customer = make_customer(db, tenant.id, "Routen Kunde", assigned_user_id=user.id)
    db.session.add(
        Task(
            tenant_id=tenant.id, customer=customer, assigned_user_id=user.id, type=TaskType.CALL_TODAY,
            title="📞 Heute anrufen", priority=Priority.HIGH, status=TaskStatus.OPEN, due_date=date.today(),
        )
    )
    db.session.commit()

    fake_client = FakeToolCallClient([make_tool_call("list_customers_to_call_today", {})])
    monkeypatch.setattr("app.services.analysis.chat_assistant.get_openai_client", lambda: fake_client)

    resp = auth_client.post("/api/chat", json={"question": "Wer soll heute angerufen werden?"})
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["tool_used"] == "list_customers_to_call_today"
    assert "Routen Kunde" in body["answer"]


def test_chat_route_rejects_empty_question(auth_client):
    resp = auth_client.post("/api/chat", json={"question": ""})
    assert resp.status_code == 400
