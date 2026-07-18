"""M13.9: Verifiziert SAMPLE_EIGENE_LISTE_ROWS/SAMPLE_GS_LISTE_ROWS einmal end-to-end durch
process_document() - von der (gemockten) Extraktion bis zu list_scope-Erkennung, Own-vs-GS-
Vergleich und der /potenziale-Abfrageschicht."""

from datetime import datetime, timedelta, timezone

import fitz

from app.models import DocStatus, Document, ListComparison, ListComparisonEntry, ListScope
from app.models.enums import ComparisonKind, DocType, PotentialCategory
from app.services.analysis.leipziger_liste_view import get_potential_records
from app.services.llm.schemas import DocumentExtraction, LeipzigerListeExtraction
from app.tasks.document_tasks import process_document
from tests.fixtures.leipziger_liste_samples import SAMPLE_EIGENE_LISTE_ROWS, SAMPLE_GS_LISTE_ROWS


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


def test_sample_eigene_liste_is_detected_as_own(app, db, tenant, tmp_path, monkeypatch):
    base_time = datetime.now(timezone.utc) - timedelta(days=1)
    document = upload_leipziger_liste(
        app, db, tenant, tmp_path, monkeypatch, "sample_eigene.pdf", SAMPLE_EIGENE_LISTE_ROWS, base_time
    )
    assert document.list_scope == ListScope.OWN


def test_sample_gs_liste_is_detected_as_geschaeftsstelle(app, db, tenant, tmp_path, monkeypatch):
    base_time = datetime.now(timezone.utc) - timedelta(days=1)
    document = upload_leipziger_liste(
        app, db, tenant, tmp_path, monkeypatch, "sample_gs.pdf", SAMPLE_GS_LISTE_ROWS, base_time
    )
    assert document.list_scope == ListScope.GESCHAEFTSSTELLE


def test_sample_pair_produces_expected_potential_categories(app, db, tenant, tmp_path, monkeypatch):
    base_time = datetime.now(timezone.utc) - timedelta(days=1)
    own_document = upload_leipziger_liste(
        app, db, tenant, tmp_path, monkeypatch, "sample_pair_eigene.pdf", SAMPLE_EIGENE_LISTE_ROWS, base_time
    )

    records = get_potential_records(document_id=own_document.id)
    categories_by_name = {r["customer_name"]: r["category"] for r in records}
    assert categories_by_name["Anna Angebot"] == PotentialCategory.NUR_ANGEBOT
    assert categories_by_name["Peter Pruefen"] == PotentialCategory.PRUEFEN
    assert categories_by_name["Otto Offen"] == PotentialCategory.OFFENER_VORGANG
    assert categories_by_name["Sabine Storno"] == PotentialCategory.STORNIERT
    # Klaus Kunde ist abgeschlossen und wird von get_potential_records() standardmaessig
    # (include_closed=False) ausgeblendet.
    assert "Klaus Kunde" not in categories_by_name


def test_sample_pair_produces_own_vs_gs_comparison_with_expected_entries(app, db, tenant, tmp_path, monkeypatch):
    base_time = datetime.now(timezone.utc) - timedelta(days=1)
    upload_leipziger_liste(
        app, db, tenant, tmp_path, monkeypatch, "sample_pair_eigene2.pdf", SAMPLE_EIGENE_LISTE_ROWS, base_time
    )
    gs_document = upload_leipziger_liste(
        app, db, tenant, tmp_path, monkeypatch, "sample_pair_gs2.pdf", SAMPLE_GS_LISTE_ROWS,
        base_time + timedelta(hours=1),
    )

    own_vs_gs = ListComparison.query.filter_by(
        document_id=gs_document.id, comparison_kind=ComparisonKind.OWN_VS_GS
    ).one()
    entries_by_customer = {
        entry.customer.name: entry.change_type
        for entry in ListComparisonEntry.query.filter_by(list_comparison_id=own_vs_gs.id).all()
    }
    # Anna Angebot & Peter Pruefen sind unveraendert in beiden Listen -> kein Eintrag.
    assert "Anna Angebot" not in entries_by_customer
    assert "Peter Pruefen" not in entries_by_customer
    # Frank/Gisela sind neu in der GS-Liste (andere Vermittler, nicht in der eigenen Liste).
    assert entries_by_customer["Frank Fremdvermittler"].value == "new_customer"
    assert entries_by_customer["Gisela Geschaeftsstelle"].value == "new_customer"
    # Otto/Sabine/Klaus aus der eigenen Liste fehlen in der (schlankeren) GS-Liste-Fixture.
    assert entries_by_customer["Otto Offen"].value == "removed_customer"
    assert entries_by_customer["Sabine Storno"].value == "removed_customer"
    assert entries_by_customer["Klaus Kunde"].value == "removed_customer"
