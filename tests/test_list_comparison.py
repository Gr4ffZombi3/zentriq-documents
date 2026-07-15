from datetime import datetime, timedelta, timezone

import fitz

from app.models import DocStatus, Document, ListComparison, ListComparisonEntry
from app.models.enums import DocType, ListChangeType
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


def upload_leipziger_liste(app, db, tenant, tmp_path, monkeypatch, filename, rows, uploaded_at):
    pdf_path = tmp_path / filename
    make_pdf_file(pdf_path)

    extraction = LeipzigerListeExtraction(rows=rows)
    monkeypatch.setattr(
        "app.tasks.document_tasks.extract_document_data",
        lambda raw_text: DocumentExtraction(doc_type=DocType.LEIPZIGER_LISTE),
    )
    monkeypatch.setattr("app.tasks.document_tasks.extract_leipziger_liste_rows", lambda raw_text: extraction)

    with app.app_context():
        document = Document(
            filename=filename,
            original_filename=filename,
            file_path=str(pdf_path),
            status=DocStatus.PENDING,
            tenant_id=tenant.id,
            uploaded_at=uploaded_at,
        )
        db.session.add(document)
        db.session.commit()

        process_document(document.id)
        db.session.refresh(document)
        return document


def test_second_leipziger_liste_upload_detects_new_removed_and_contract_changes(
    app, db, tenant, tmp_path, monkeypatch
):
    base_time = datetime.now(timezone.utc) - timedelta(days=1)

    document1 = upload_leipziger_liste(
        app,
        db,
        tenant,
        tmp_path,
        monkeypatch,
        "liste1.pdf",
        rows=[
            LeipzigerListeRow(customer=ExtractedCustomer(name="Anna Kunde"), is_angebot=True),
            LeipzigerListeRow(customer=ExtractedCustomer(name="Bernd Kunde"), contract_number="C-100"),
        ],
        uploaded_at=base_time,
    )
    assert ListComparison.query.count() == 0  # kein Vorgaenger vorhanden -> kein Vergleich

    document2 = upload_leipziger_liste(
        app,
        db,
        tenant,
        tmp_path,
        monkeypatch,
        "liste2.pdf",
        rows=[
            LeipzigerListeRow(
                customer=ExtractedCustomer(name="Anna Kunde"), is_angebot=False, contract_number="C-200"
            ),
            LeipzigerListeRow(customer=ExtractedCustomer(name="Clara Kunde")),
            # Bernd Kunde fehlt in dieser Liste -> REMOVED_CUSTOMER
        ],
        uploaded_at=base_time + timedelta(days=7),
    )

    comparison = ListComparison.query.filter_by(document_id=document2.id).one()
    assert comparison.previous_document_id == document1.id

    entries_by_type = {}
    for entry in ListComparisonEntry.query.filter_by(list_comparison_id=comparison.id).all():
        entries_by_type.setdefault(entry.change_type, set()).add(entry.customer.name)

    assert entries_by_type[ListChangeType.NEW_CUSTOMER] == {"Clara Kunde"}
    assert entries_by_type[ListChangeType.NEW_CONTRACT] == {"Anna Kunde"}
    assert entries_by_type[ListChangeType.REMOVED_CUSTOMER] == {"Bernd Kunde"}

    assert comparison.new_customer_count == 1
    assert comparison.new_contract_count == 1
    assert comparison.removed_customer_count == 1


def test_storno_flag_produces_storno_entry(app, db, tenant, tmp_path, monkeypatch):
    base_time = datetime.now(timezone.utc) - timedelta(days=1)

    upload_leipziger_liste(
        app,
        db,
        tenant,
        tmp_path,
        monkeypatch,
        "storno1.pdf",
        rows=[LeipzigerListeRow(customer=ExtractedCustomer(name="Storno Kunde"))],
        uploaded_at=base_time,
    )
    document2 = upload_leipziger_liste(
        app,
        db,
        tenant,
        tmp_path,
        monkeypatch,
        "storno2.pdf",
        rows=[LeipzigerListeRow(customer=ExtractedCustomer(name="Storno Kunde"), is_storno=True)],
        uploaded_at=base_time + timedelta(days=1),
    )

    comparison = ListComparison.query.filter_by(document_id=document2.id).one()
    assert comparison.storno_count == 1
    entry = ListComparisonEntry.query.filter_by(list_comparison_id=comparison.id).one()
    assert entry.change_type == ListChangeType.STORNO


def test_unchanged_customer_produces_no_entry(app, db, tenant, tmp_path, monkeypatch):
    base_time = datetime.now(timezone.utc) - timedelta(days=1)
    row = LeipzigerListeRow(customer=ExtractedCustomer(name="Stabiler Kunde"), products=["Kfz-Haftpflicht"])

    upload_leipziger_liste(app, db, tenant, tmp_path, monkeypatch, "stabil1.pdf", rows=[row], uploaded_at=base_time)
    document2 = upload_leipziger_liste(
        app, db, tenant, tmp_path, monkeypatch, "stabil2.pdf", rows=[row], uploaded_at=base_time + timedelta(days=1)
    )

    comparison = ListComparison.query.filter_by(document_id=document2.id).one()
    assert ListComparisonEntry.query.filter_by(list_comparison_id=comparison.id).count() == 0


def test_new_product_line_produces_dedicated_entry(app, db, tenant, tmp_path, monkeypatch):
    base_time = datetime.now(timezone.utc) - timedelta(days=1)

    upload_leipziger_liste(
        app, db, tenant, tmp_path, monkeypatch, "sparte1.pdf",
        rows=[LeipzigerListeRow(customer=ExtractedCustomer(name="Sparten Kunde"), product_line="KFZ")],
        uploaded_at=base_time,
    )
    document2 = upload_leipziger_liste(
        app, db, tenant, tmp_path, monkeypatch, "sparte2.pdf",
        rows=[
            LeipzigerListeRow(customer=ExtractedCustomer(name="Sparten Kunde"), product_line="KFZ"),
            LeipzigerListeRow(customer=ExtractedCustomer(name="Sparten Kunde"), product_line="Hausrat"),
        ],
        uploaded_at=base_time + timedelta(days=1),
    )

    comparison = ListComparison.query.filter_by(document_id=document2.id).one()
    assert comparison.new_product_line_count == 1
    entries_by_type = {}
    for entry in ListComparisonEntry.query.filter_by(list_comparison_id=comparison.id).all():
        entries_by_type.setdefault(entry.change_type, set()).add(entry.customer.name)
    assert entries_by_type[ListChangeType.NEW_PRODUCT_LINE] == {"Sparten Kunde"}


def test_new_product_line_can_co_occur_with_new_contract(app, db, tenant, tmp_path, monkeypatch):
    # Eine neue Sparte soll zusaetzlich zu, nicht anstelle von, einer anderen Aenderung
    # erkannt werden koennen - beide Eintraege muessen fuer denselben Kunden entstehen.
    base_time = datetime.now(timezone.utc) - timedelta(days=1)

    upload_leipziger_liste(
        app, db, tenant, tmp_path, monkeypatch, "co1.pdf",
        rows=[LeipzigerListeRow(customer=ExtractedCustomer(name="Doppel Kunde"), product_line="KFZ")],
        uploaded_at=base_time,
    )
    document2 = upload_leipziger_liste(
        app, db, tenant, tmp_path, monkeypatch, "co2.pdf",
        rows=[
            LeipzigerListeRow(
                customer=ExtractedCustomer(name="Doppel Kunde"), product_line="Hausrat", contract_number="C-999"
            )
        ],
        uploaded_at=base_time + timedelta(days=1),
    )

    comparison = ListComparison.query.filter_by(document_id=document2.id).one()
    change_types = {
        entry.change_type
        for entry in ListComparisonEntry.query.filter_by(list_comparison_id=comparison.id).all()
    }
    assert ListChangeType.NEW_CONTRACT in change_types
    assert ListChangeType.NEW_PRODUCT_LINE in change_types
    assert comparison.new_contract_count == 1
    assert comparison.new_product_line_count == 1


def test_reprocessing_document_does_not_duplicate_comparison(app, db, tenant, tmp_path, monkeypatch):
    base_time = datetime.now(timezone.utc) - timedelta(days=1)
    upload_leipziger_liste(
        app,
        db,
        tenant,
        tmp_path,
        monkeypatch,
        "reprocess1.pdf",
        rows=[LeipzigerListeRow(customer=ExtractedCustomer(name="Kunde A"))],
        uploaded_at=base_time,
    )
    document2 = upload_leipziger_liste(
        app,
        db,
        tenant,
        tmp_path,
        monkeypatch,
        "reprocess2.pdf",
        rows=[LeipzigerListeRow(customer=ExtractedCustomer(name="Kunde B"))],
        uploaded_at=base_time + timedelta(days=1),
    )

    assert ListComparison.query.filter_by(document_id=document2.id).count() == 1

    with app.app_context():
        process_document(document2.id)

    assert ListComparison.query.filter_by(document_id=document2.id).count() == 1
