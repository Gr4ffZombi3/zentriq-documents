import fitz

from app.models import Customer, Document, DocumentCustomer, DocStatus, Recommendation
from app.models.enums import DocType, Priority
from app.services.documents import apply_leipziger_liste_extraction
from app.services.llm.classification import compute_document_flags
from app.services.llm.schemas import (
    DocumentExtraction,
    ExtractedCustomer,
    LeipzigerListeExtraction,
    LeipzigerListeRow,
)
from app.tasks.document_tasks import process_document


def make_pdf_file(path):
    doc = fitz.open()
    doc.new_page()
    doc.save(str(path))
    doc.close()


def make_extraction():
    return LeipzigerListeExtraction(
        rows=[
            LeipzigerListeRow(
                customer=ExtractedCustomer(name="Anna Kunde", city="Köln"),
                vehicle="VW Golf",
                products=["Kfz-Haftpflicht"],
                is_neugeschaeft=True,
                priority=Priority.HIGH,
                recommended_next_action="Heute anrufen",
            ),
            LeipzigerListeRow(
                customer=ExtractedCustomer(name="Bernd Kunde", city="Berlin"),
                vehicle="Audi A4",
                products=["Kfz-Haftpflicht", "Hausrat"],
                is_fahrzeugwechsel=True,
                cross_sell_opportunity=True,
                has_multiple_products=True,
                priority=Priority.MEDIUM,
            ),
            # Zweite Zeile fuer Anna Kunde (zweites Produkt, gleicher Kunde).
            LeipzigerListeRow(
                customer=ExtractedCustomer(name="Anna Kunde", city="Köln"),
                vehicle="VW Golf",
                products=["Rechtsschutz"],
                priority=Priority.LOW,
            ),
        ]
    )


def test_compute_document_flags_aggregates_rows_and_picks_highest_priority():
    flags = compute_document_flags(make_extraction())
    assert flags["is_neugeschaeft"] is True
    assert flags["is_fahrzeugwechsel"] is True
    assert flags["cross_sell_opportunity"] is True
    assert flags["has_multiple_products"] is True
    assert flags["priority"] == Priority.HIGH
    assert flags["recommended_next_action"] == "Heute anrufen"


def test_compute_document_flags_empty_rows_returns_defaults():
    flags = compute_document_flags(LeipzigerListeExtraction(rows=[]))
    assert flags["priority"] == Priority.MEDIUM
    assert flags["is_neugeschaeft"] is False


def test_apply_leipziger_liste_extraction_creates_customers_and_merges_duplicate_rows(app, db):
    document = Document(filename="liste.pdf", original_filename="liste.pdf", file_path="/tmp/liste.pdf")
    db.session.add(document)
    db.session.commit()

    apply_leipziger_liste_extraction(document, make_extraction())
    db.session.commit()

    assert document.doc_type == DocType.LEIPZIGER_LISTE
    assert document.is_neugeschaeft is True
    assert document.priority == Priority.HIGH

    # Zwei eindeutige Kunden trotz drei Zeilen (Anna Kunde erscheint zweimal).
    assert Customer.query.count() == 2
    assert DocumentCustomer.query.count() == 2

    anna_link = DocumentCustomer.query.join(Customer).filter(Customer.name == "Anna Kunde").one()
    assert len(anna_link.row_data) == 2

    # Empfehlungen: Neugeschaeft fuer Anna, Fahrzeugwechsel + Cross-Sell fuer Bernd.
    recommendations = Recommendation.query.all()
    types_by_customer = {}
    for rec in recommendations:
        types_by_customer.setdefault(rec.customer.name, set()).add(rec.type)

    assert "call_today" in {t.value for t in types_by_customer["Anna Kunde"]}
    assert "prioritize_vehicle_change" in {t.value for t in types_by_customer["Bernd Kunde"]}


def test_process_document_task_routes_leipziger_liste_through_multi_row_extraction(
    app, db, tmp_path, monkeypatch
):
    pdf_path = tmp_path / "leipziger.pdf"
    make_pdf_file(pdf_path)

    monkeypatch.setattr(
        "app.tasks.document_tasks.extract_document_data",
        lambda raw_text: DocumentExtraction(doc_type=DocType.LEIPZIGER_LISTE),
    )
    monkeypatch.setattr(
        "app.tasks.document_tasks.extract_leipziger_liste_rows",
        lambda raw_text: make_extraction(),
    )

    with app.app_context():
        document = Document(
            filename="leipziger.pdf",
            original_filename="leipziger.pdf",
            file_path=str(pdf_path),
            status=DocStatus.PENDING,
        )
        db.session.add(document)
        db.session.commit()

        process_document(document.id)

        db.session.refresh(document)
        assert document.status == DocStatus.DONE
        assert document.doc_type == DocType.LEIPZIGER_LISTE
        assert len(document.document_customers) == 2
        assert len(document.recommendations) >= 2

        # Reprocessing (z.B. via Retry-Button) darf nicht am Unique-Constraint auf
        # (document_id, customer_id) scheitern und muss wieder sauber bei DONE landen,
        # ohne doppelte document_customers/recommendations anzuhaeufen.
        process_document(document.id)

        db.session.refresh(document)
        assert document.status == DocStatus.DONE
        assert len(document.document_customers) == 2
        assert len(document.recommendations) >= 2
