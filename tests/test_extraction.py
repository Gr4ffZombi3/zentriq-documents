import json
from types import SimpleNamespace

from app.models import Customer, Document
from app.models.enums import DocType
from app.services.documents import apply_extraction
from app.services.llm.extraction import extract_document_data
from app.services.llm.schemas import DocumentExtraction, ExtractedCustomer


class FakeOpenAIClient:
    def __init__(self, content: str):
        self._content = content
        self.chat = SimpleNamespace(
            completions=SimpleNamespace(create=self._create)
        )

    def _create(self, **kwargs):
        message = SimpleNamespace(content=self._content)
        choice = SimpleNamespace(message=message)
        return SimpleNamespace(choices=[choice])


def test_extract_document_data_parses_valid_json_response(app, monkeypatch):
    payload = {
        "doc_type": "rechnung",
        "customer": {"name": "Erika Musterfrau", "city": "Berlin", "postal_code": "10115"},
        "vehicle": None,
        "license_plate": None,
        "insurer": "Musterversicherung",
        "contract_number": "V-123",
        "case_number": None,
        "broker": None,
        "contract_start_date": None,
        "products": ["Kfz-Kasko"],
        "special_notes": None,
    }
    fake_client = FakeOpenAIClient(json.dumps(payload))
    monkeypatch.setattr(
        "app.services.llm.extraction.get_openai_client", lambda: fake_client
    )

    with app.app_context():
        result = extract_document_data("irrelevanter Rohtext")

    assert isinstance(result, DocumentExtraction)
    assert result.doc_type == DocType.RECHNUNG
    assert result.customer.name == "Erika Musterfrau"
    assert result.insurer == "Musterversicherung"
    assert result.products == ["Kfz-Kasko"]


def test_apply_extraction_maps_fields_and_upserts_customer(app, db):
    document = Document(
        filename="x.pdf", original_filename="x.pdf", file_path="/tmp/x.pdf"
    )
    db.session.add(document)
    db.session.commit()

    extraction = DocumentExtraction(
        doc_type=DocType.RECHNUNG,
        customer=ExtractedCustomer(name="Erika Musterfrau", city="Berlin"),
        insurer="Musterversicherung",
        products=["Kfz-Kasko"],
    )

    apply_extraction(document, extraction)
    db.session.commit()

    assert document.doc_type == DocType.RECHNUNG
    assert document.insurer == "Musterversicherung"
    assert document.products == ["Kfz-Kasko"]
    assert document.customer.name == "Erika Musterfrau"
    assert document.raw_json["doc_type"] == "rechnung"

    # Zweites Dokument mit gleichem Kundennamen soll den bestehenden Kunden wiederverwenden.
    document2 = Document(
        filename="y.pdf", original_filename="y.pdf", file_path="/tmp/y.pdf"
    )
    db.session.add(document2)
    db.session.commit()
    extraction2 = DocumentExtraction(
        doc_type=DocType.GUTACHTEN,
        customer=ExtractedCustomer(name="Erika Musterfrau"),
    )
    apply_extraction(document2, extraction2)
    db.session.commit()

    assert Customer.query.count() == 1
    assert document2.customer_id == document.customer_id
