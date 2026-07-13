import json
from types import SimpleNamespace

from app.models import Customer, DocStatus, Document
from app.models.enums import DocType, Priority
from app.search.query_builder import FilterSpec, fallback_text_search, search_documents
from app.services.llm.search_parser import parse_search_query


def seed_documents(db):
    koeln_customer = Customer(name="Anna Kunde", city="Köln", postal_code="50667")
    berlin_customer = Customer(name="Bernd Kunde", city="Berlin", postal_code="10115")
    db.session.add_all([koeln_customer, berlin_customer])
    db.session.commit()

    doc1 = Document(
        filename="a.pdf", original_filename="a.pdf", file_path="/tmp/a.pdf",
        status=DocStatus.DONE, doc_type=DocType.LEIPZIGER_LISTE,
        customer=koeln_customer, products=["Kfz-Haftpflicht"], is_neugeschaeft=True,
        priority=Priority.HIGH, raw_text="Vertrag fuer Anna Kunde in Koeln",
    )
    doc2 = Document(
        filename="b.pdf", original_filename="b.pdf", file_path="/tmp/b.pdf",
        status=DocStatus.DONE, doc_type=DocType.RECHNUNG,
        customer=berlin_customer, products=["Kfz-Haftpflicht", "Hausrat"],
        raw_text="Rechnung fuer Bernd Kunde in Berlin",
    )
    db.session.add_all([doc1, doc2])
    db.session.commit()
    return doc1, doc2


def test_search_documents_filters_by_city(db):
    doc1, doc2 = seed_documents(db)
    results = search_documents(FilterSpec(city="Köln"))
    assert results == [doc1]


def test_search_documents_filters_by_doc_type_and_flag(db):
    doc1, doc2 = seed_documents(db)
    results = search_documents(FilterSpec(doc_type=DocType.LEIPZIGER_LISTE, is_neugeschaeft=True))
    assert results == [doc1]


def test_search_documents_missing_product(db):
    doc1, doc2 = seed_documents(db)
    results = search_documents(FilterSpec(missing_product="Hausrat"))
    assert doc1 in results
    assert doc2 not in results


def test_search_documents_has_product(db):
    doc1, doc2 = seed_documents(db)
    results = search_documents(FilterSpec(has_product="Hausrat"))
    assert results == [doc2]


def test_fallback_text_search_matches_raw_text(db):
    doc1, doc2 = seed_documents(db)
    results = fallback_text_search("Berlin")
    assert results == [doc2]


class FakeToolCallClient:
    def __init__(self, tool_calls):
        self._tool_calls = tool_calls
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        message = SimpleNamespace(tool_calls=self._tool_calls)
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])


def make_tool_call(arguments: dict):
    function = SimpleNamespace(arguments=json.dumps(arguments))
    return SimpleNamespace(function=function)


def test_parse_search_query_returns_filter_spec_from_tool_call(app, monkeypatch):
    fake_client = FakeToolCallClient([make_tool_call({"city": "Köln"})])
    monkeypatch.setattr(
        "app.services.llm.search_parser.get_openai_client", lambda: fake_client
    )

    with app.app_context():
        result = parse_search_query("Zeige alle Kunden aus Köln")

    assert isinstance(result, FilterSpec)
    assert result.city == "Köln"


def test_parse_search_query_returns_none_without_tool_call(app, monkeypatch):
    fake_client = FakeToolCallClient([])
    monkeypatch.setattr(
        "app.services.llm.search_parser.get_openai_client", lambda: fake_client
    )

    with app.app_context():
        result = parse_search_query("irgendwas Unklares")

    assert result is None


def test_parse_search_query_falls_back_gracefully_on_invalid_enum(app, monkeypatch):
    fake_client = FakeToolCallClient([make_tool_call({"doc_type": "not_a_real_type"})])
    monkeypatch.setattr(
        "app.services.llm.search_parser.get_openai_client", lambda: fake_client
    )

    with app.app_context():
        result = parse_search_query("Zeige irgendwas")

    assert result is None


def test_search_endpoint_uses_filter_spec_when_available(client, db, monkeypatch):
    doc1, doc2 = seed_documents(db)
    monkeypatch.setattr(
        "app.blueprints.search.routes.parse_search_query",
        lambda query: FilterSpec(city="Köln"),
    )

    resp = client.get("/search?q=Zeige alle Kunden aus Köln")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "a.pdf" in body
    assert "b.pdf" not in body


def test_search_endpoint_falls_back_to_text_search(client, db, monkeypatch):
    doc1, doc2 = seed_documents(db)
    monkeypatch.setattr(
        "app.blueprints.search.routes.parse_search_query", lambda query: None
    )

    resp = client.get("/search?q=Berlin")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "b.pdf" in body
    assert "a.pdf" not in body
    assert "einfache Textsuche" in body
