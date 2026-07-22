from datetime import date

import fitz

from app.models import Customer, DocStatus, Document, DocumentCustomer, Recommendation
from app.models.enums import DocType, OcrEngine, Priority
from app.services.documents import apply_leipziger_liste_extraction
from app.services.llm.classification import compute_document_flags
from app.services.llm.extraction import extract_leipziger_liste_rows
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


def test_apply_leipziger_liste_extraction_creates_customers_and_merges_duplicate_rows(app, db, tenant):
    document = Document(
        filename="liste.pdf", original_filename="liste.pdf", file_path="/tmp/liste.pdf", tenant_id=tenant.id
    )
    db.session.add(document)
    db.session.commit()

    apply_leipziger_liste_extraction(document, make_extraction())
    db.session.commit()

    assert document.doc_type == DocType.LEIPZIGER_LISTE
    assert document.is_neugeschaeft is True
    assert document.priority == Priority.HIGH

    # Ohne starken Schluessel (DOB oder PLZ) werden gleichnamige Kunden nicht automatisch
    # zusammengefuehrt. Entscheidend ist, dass alle Vertragszeilen erhalten bleiben.
    assert Customer.query.count() == 3
    assert DocumentCustomer.query.count() == 3
    anna_links = DocumentCustomer.query.join(Customer).filter(Customer.name == "Anna Kunde").all()
    assert len(anna_links) == 2
    assert sum(len(link.row_data or []) for link in anna_links) == 2

    # Empfehlungen: Neugeschaeft fuer Anna, Fahrzeugwechsel + Cross-Sell fuer Bernd.
    recommendations = Recommendation.query.all()
    types_by_customer = {}
    for rec in recommendations:
        types_by_customer.setdefault(rec.customer.name, set()).add(rec.type)

    assert "call_today" in {t.value for t in types_by_customer["Anna Kunde"]}
    assert "prioritize_vehicle_change" in {t.value for t in types_by_customer["Bernd Kunde"]}


def test_new_m12_fields_stay_per_row_not_on_document(app, db, tenant):
    document = Document(
        filename="liste.pdf", original_filename="liste.pdf", file_path="/tmp/liste.pdf", tenant_id=tenant.id
    )
    db.session.add(document)
    db.session.commit()

    extraction = LeipzigerListeExtraction(
        rows=[
            LeipzigerListeRow(
                customer=ExtractedCustomer(name="Sparten Kunde"),
                broker_number="VM-1001",
                product_line="KFZ",
                premium="99,90 EUR",
                tariff="Basis",
            )
        ]
    )
    apply_leipziger_liste_extraction(document, extraction)
    db.session.commit()

    # Dokumentebene bleibt bewusst leer - waere bei mehrzeiligen Listen irrefuehrend.
    assert document.broker_number is None
    assert document.product_line is None
    assert document.premium is None
    assert document.tariff is None

    doc_customer = DocumentCustomer.query.join(Customer).filter(Customer.name == "Sparten Kunde").one()
    row = doc_customer.row_data[0]
    assert row["broker_number"] == "VM-1001"
    assert row["product_line"] == "KFZ"
    assert row["premium"] == "99,90 EUR"
    assert row["tariff"] == "Basis"


def test_leipziger_liste_row_defaults_for_m13_fields():
    row = LeipzigerListeRow(customer=ExtractedCustomer(name="Ohne Beginn"))
    assert row.contract_start_date is None
    assert row.has_antrag is False


def test_m13_fields_flow_into_row_data(app, db, tenant):
    document = Document(
        filename="liste2.pdf", original_filename="liste2.pdf", file_path="/tmp/liste2.pdf", tenant_id=tenant.id
    )
    db.session.add(document)
    db.session.commit()

    extraction = LeipzigerListeExtraction(
        rows=[
            LeipzigerListeRow(
                customer=ExtractedCustomer(name="Beginn Kunde"),
                contract_start_date=date(2026, 1, 15),
                has_antrag=True,
            )
        ]
    )
    apply_leipziger_liste_extraction(document, extraction)
    db.session.commit()

    doc_customer = DocumentCustomer.query.join(Customer).filter(Customer.name == "Beginn Kunde").one()
    row = doc_customer.row_data[0]
    assert row["contract_start_date"] == "2026-01-15"
    assert row["has_antrag"] is True

    # Wie bei den M12-Feldern: bewusst keine Dokumentebene-Aggregation fuer diese Zeilenfelder.
    assert document.contract_start_date is None


def test_apply_leipziger_liste_extraction_keeps_multiple_contract_rows_for_same_customer(app, db, tenant):
    document = Document(
        filename="mehrfach.pdf", original_filename="mehrfach.pdf", file_path="/tmp/mehrfach.pdf", tenant_id=tenant.id
    )
    db.session.add(document)
    db.session.commit()

    extraction = LeipzigerListeExtraction(
        rows=[
            LeipzigerListeRow(
                customer=ExtractedCustomer(name="Mehrfach Kunde"),
                contract_number="A-100",
                product_line="PH",
                is_angebot=True,
            ),
            LeipzigerListeRow(
                customer=ExtractedCustomer(name="Mehrfach Kunde"),
                contract_number="B-200",
                product_line="RS",
                is_neugeschaeft=True,
            ),
        ]
    )

    apply_stats = apply_leipziger_liste_extraction(document, extraction)
    db.session.commit()

    doc_customers = DocumentCustomer.query.join(Customer).filter(Customer.name == "Mehrfach Kunde").all()
    assert len(doc_customers) == 2
    assert {
        row["contract_number"]
        for doc_customer in doc_customers
        for row in (doc_customer.row_data or [])
    } == {"A-100", "B-200"}
    assert apply_stats["discarded_duplicates"] == 0


def test_apply_leipziger_liste_extraction_merges_exact_duplicate_contract_rows(app, db, tenant):
    document = Document(
        filename="dedupe.pdf", original_filename="dedupe.pdf", file_path="/tmp/dedupe.pdf", tenant_id=tenant.id
    )
    db.session.add(document)
    db.session.commit()

    extraction = LeipzigerListeExtraction(
        rows=[
            LeipzigerListeRow(
                customer=ExtractedCustomer(name="Dubletten Kunde"),
                contract_number="508/001164-L",
                status_code="FZW",
                product_line="KFZ",
                contract_start_date=date(2026, 7, 13),
                source_page=1,
                source_row=3,
            ),
            LeipzigerListeRow(
                customer=ExtractedCustomer(name="Dubletten Kunde"),
                contract_number="508/001164-L",
                status_code="FZW",
                product_line="KFZ",
                contract_start_date=date(2026, 7, 13),
                broker_number="08/0950-T",
                source_page=2,
                source_row=1,
            ),
        ]
    )

    apply_stats = apply_leipziger_liste_extraction(document, extraction)
    db.session.commit()

    doc_customer = DocumentCustomer.query.join(Customer).filter(Customer.name == "Dubletten Kunde").one()
    assert len(doc_customer.row_data) == 1
    assert doc_customer.row_data[0]["broker_number"] == "08/0950-T"
    assert doc_customer.row_data[0]["source_page"] == 1
    assert apply_stats["discarded_duplicates"] == 1


def test_extract_leipziger_liste_rows_processes_all_pages_without_five_row_limit(app, monkeypatch):
    batch_inputs = []

    def fake_batch_extract(raw_text):
        batch_inputs.append(raw_text)
        page_numbers = [int(line.removeprefix("[SEITE ").removesuffix("]")) for line in raw_text.splitlines() if line.startswith("[SEITE ")]
        rows = []
        for page_number in page_numbers:
            for offset in range(2):
                rows.append(
                    LeipzigerListeRow(
                        customer=ExtractedCustomer(name=f"Kunde {page_number}-{offset}"),
                        contract_number=f"{page_number}-{offset}",
                        source_page=page_number,
                    )
                )
        return LeipzigerListeExtraction(rows=rows)

    monkeypatch.setattr("app.services.llm.extraction._extract_leipziger_liste_batch", fake_batch_extract)

    with app.app_context():
        app.config["LEIPZIGER_LISTE_PAGE_BATCH_SIZE"] = 1
        extraction = extract_leipziger_liste_rows(["Seite 1", "Seite 2", "Seite 3", "Seite 4"])

    assert len(batch_inputs) == 4
    assert len(extraction.rows) == 8
    assert extraction.analysis_meta["processed_pages"] == 4
    assert extraction.analysis_meta["processed_page_numbers"] == [1, 2, 3, 4]
    assert extraction.analysis_meta["raw_row_count"] == 8


def test_process_document_task_uses_page_texts_and_stores_analysis_meta(app, db, tenant, tmp_path, monkeypatch):
    pdf_path = tmp_path / "mehrseitig.pdf"
    make_pdf_file(pdf_path)

    captured_page_texts = {}

    monkeypatch.setattr(
        "app.tasks.document_tasks.extract_text",
        lambda file_path: (
            "Seite 1\nSeite 2\nSeite 3\nSeite 4",
            OcrEngine.TESSERACT,
            96.0,
            ["Seite 1", "Seite 2", "Seite 3", "Seite 4"],
        ),
    )
    monkeypatch.setattr(
        "app.tasks.document_tasks.extract_document_data",
        lambda raw_text: DocumentExtraction(doc_type=DocType.LEIPZIGER_LISTE),
    )

    def fake_extract_rows(page_texts):
        captured_page_texts["value"] = list(page_texts)
        return LeipzigerListeExtraction(
            rows=[
                LeipzigerListeRow(
                    customer=ExtractedCustomer(name="Alpha Kunde"),
                    contract_number="A-1",
                    status_code="ANG",
                    is_angebot=True,
                    source_page=1,
                    source_row=1,
                ),
                LeipzigerListeRow(
                    customer=ExtractedCustomer(name="Beta Kunde"),
                    contract_number="B-2",
                    status_code="FZW",
                    is_fahrzeugwechsel=True,
                    contract_start_date=date(2026, 7, 13),
                    source_page=4,
                    source_row=2,
                ),
                LeipzigerListeRow(
                    customer=ExtractedCustomer(name="Gamma Kunde"),
                    contract_number="C-3",
                    status_code="NEU",
                    is_neugeschaeft=True,
                    source_page=4,
                    source_row=3,
                ),
                LeipzigerListeRow(
                    customer=ExtractedCustomer(name="Delta Kunde"),
                    contract_number="D-4",
                    source_page=2,
                    source_row=4,
                ),
                LeipzigerListeRow(
                    customer=ExtractedCustomer(name="Epsilon Kunde"),
                    contract_number="E-5",
                    source_page=3,
                    source_row=5,
                ),
                LeipzigerListeRow(
                    customer=ExtractedCustomer(name="Zeta Kunde"),
                    contract_number="F-6",
                    source_page=4,
                    source_row=6,
                ),
            ],
            analysis_meta={
                "total_pages": 4,
                "processed_pages": 4,
                "processed_page_numbers": [1, 2, 3, 4],
                "failed_pages": [],
                "raw_row_count": 6,
                "batch_size": 1,
            },
        )

    monkeypatch.setattr("app.tasks.document_tasks.extract_leipziger_liste_rows", fake_extract_rows)

    with app.app_context():
        document = Document(
            filename="mehrseitig.pdf",
            original_filename="mehrseitig.pdf",
            file_path=str(pdf_path),
            status=DocStatus.PENDING,
            tenant_id=tenant.id,
        )
        db.session.add(document)
        db.session.commit()

        process_document(document.id)

        db.session.refresh(document)
        assert captured_page_texts["value"] == ["Seite 1", "Seite 2", "Seite 3", "Seite 4"]
        assert document.status == DocStatus.DONE
        assert document.extra_data["leipziger_analysis"]["total_pages"] == 4
        assert document.extra_data["leipziger_analysis"]["processed_pages"] == 4
        assert document.extra_data["leipziger_analysis"]["stored_row_count"] == 6
        assert len(document.document_customers) == 6


def test_process_document_task_routes_leipziger_liste_through_multi_row_extraction(
    app, db, tenant, tmp_path, monkeypatch
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
            tenant_id=tenant.id,
        )
        db.session.add(document)
        db.session.commit()

        process_document(document.id)

        db.session.refresh(document)
        assert document.status == DocStatus.DONE
        assert document.doc_type == DocType.LEIPZIGER_LISTE
        assert len(document.document_customers) == 3
        assert len(document.recommendations) >= 2

        # Reprocessing (z.B. via Retry-Button) darf nicht am Unique-Constraint auf
        # (document_id, customer_id) scheitern und muss wieder sauber bei DONE landen,
        # ohne doppelte document_customers/recommendations anzuhaeufen.
        process_document(document.id)

        db.session.refresh(document)
        assert document.status == DocStatus.DONE
        assert len(document.document_customers) == 3
        assert len(document.recommendations) >= 2
