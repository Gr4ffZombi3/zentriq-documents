from datetime import datetime, timedelta, timezone

import fitz

from app.models import DocStatus, Document, ListComparison, ListComparisonEntry, ListScope
from app.models.enums import ComparisonKind, DocType
from app.services.list_comparison import compare_leipziger_liste, find_paired_gs_or_own_document
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


def test_own_list_alone_produces_no_own_vs_gs_comparison(app, db, tenant, tmp_path, monkeypatch):
    base_time = datetime.now(timezone.utc) - timedelta(days=1)
    document = upload_leipziger_liste(
        app, db, tenant, tmp_path, monkeypatch, "own1.pdf",
        rows=[LeipzigerListeRow(customer=ExtractedCustomer(name="Kunde A"), broker_number="VM-1001")],
        uploaded_at=base_time,
    )
    assert document.list_scope == ListScope.OWN
    assert ListComparison.query.filter_by(document_id=document.id, comparison_kind=ComparisonKind.OWN_VS_GS).count() == 0


def test_gs_list_after_own_list_produces_own_vs_gs_comparison(app, db, tenant, tmp_path, monkeypatch):
    base_time = datetime.now(timezone.utc) - timedelta(days=2)

    own_document = upload_leipziger_liste(
        app, db, tenant, tmp_path, monkeypatch, "own2.pdf",
        rows=[
            LeipzigerListeRow(customer=ExtractedCustomer(name="Kunde A"), broker_number="VM-1001", contract_number="C-100"),
            LeipzigerListeRow(customer=ExtractedCustomer(name="Kunde B"), broker_number="VM-1001"),
        ],
        uploaded_at=base_time,
    )
    assert own_document.list_scope == ListScope.OWN

    gs_document = upload_leipziger_liste(
        app, db, tenant, tmp_path, monkeypatch, "gs1.pdf",
        rows=[
            LeipzigerListeRow(customer=ExtractedCustomer(name="Kunde A"), broker_number="VM-1001", contract_number="C-100"),
            LeipzigerListeRow(customer=ExtractedCustomer(name="Kunde C"), broker_number="VM-2002"),
        ],
        uploaded_at=base_time + timedelta(days=1),
    )
    assert gs_document.list_scope == ListScope.GESCHAEFTSSTELLE

    own_vs_gs = ListComparison.query.filter_by(
        document_id=gs_document.id, comparison_kind=ComparisonKind.OWN_VS_GS
    ).one()
    assert own_vs_gs.previous_document_id == own_document.id

    entries_by_customer = {
        entry.customer.name: entry.change_type
        for entry in ListComparisonEntry.query.filter_by(list_comparison_id=own_vs_gs.id).all()
    }
    # Kunde A steht in beiden Listen unveraendert -> kein Eintrag. Kunde C ist neu in der
    # GS-Liste (aus eigener-Liste-Sicht fehlend). Kunde B fehlt in der GS-Liste (entfernt).
    assert "Kunde A" not in entries_by_customer
    assert entries_by_customer["Kunde C"] == "new_customer" or entries_by_customer["Kunde C"].value == "new_customer"
    assert entries_by_customer["Kunde B"].value == "removed_customer"


def test_temporal_and_own_vs_gs_comparisons_coexist_for_same_document(app, db, tenant, tmp_path, monkeypatch):
    # Kritischster Test: ein Dokument mit BEIDEN Vergleichsarten - keine darf die andere
    # ueberschreiben oder loeschen.
    base_time = datetime.now(timezone.utc) - timedelta(days=3)

    own_document_1 = upload_leipziger_liste(
        app, db, tenant, tmp_path, monkeypatch, "seq_own1.pdf",
        rows=[LeipzigerListeRow(customer=ExtractedCustomer(name="Kunde A"), broker_number="VM-1001")],
        uploaded_at=base_time,
    )
    gs_document = upload_leipziger_liste(
        app, db, tenant, tmp_path, monkeypatch, "seq_gs1.pdf",
        rows=[
            LeipzigerListeRow(customer=ExtractedCustomer(name="Kunde A"), broker_number="VM-1001"),
            LeipzigerListeRow(customer=ExtractedCustomer(name="Kunde D"), broker_number="VM-9999"),
        ],
        uploaded_at=base_time + timedelta(days=1),
    )
    own_document_2 = upload_leipziger_liste(
        app, db, tenant, tmp_path, monkeypatch, "seq_own2.pdf",
        rows=[
            LeipzigerListeRow(customer=ExtractedCustomer(name="Kunde A"), broker_number="VM-1001"),
            LeipzigerListeRow(customer=ExtractedCustomer(name="Kunde E"), broker_number="VM-1001"),
        ],
        uploaded_at=base_time + timedelta(days=2),
    )

    assert own_document_1.list_scope == ListScope.OWN
    assert gs_document.list_scope == ListScope.GESCHAEFTSSTELLE
    assert own_document_2.list_scope == ListScope.OWN

    # own_document_2 sollte sowohl einen TEMPORAL-Vergleich (gegen own_document_1, das
    # zeitlich letzte Leipziger-Liste-Dokument ueberhaupt vor own_document_2 - hier zufaellig
    # die GS-Liste, da diese zeitlich naeher liegt) als auch einen OWN_VS_GS-Vergleich
    # (gegen gs_document, das juengste Dokument des entgegengesetzten Scopes) haben.
    temporal = ListComparison.query.filter_by(
        document_id=own_document_2.id, comparison_kind=ComparisonKind.TEMPORAL
    ).one()
    own_vs_gs = ListComparison.query.filter_by(
        document_id=own_document_2.id, comparison_kind=ComparisonKind.OWN_VS_GS
    ).one()

    assert temporal.id != own_vs_gs.id
    assert own_vs_gs.previous_document_id == gs_document.id

    # Beide Vergleichslaeufe haben eigene, unabhaengige Eintraege.
    temporal_entries = ListComparisonEntry.query.filter_by(list_comparison_id=temporal.id).count()
    own_vs_gs_entries = ListComparisonEntry.query.filter_by(list_comparison_id=own_vs_gs.id).count()
    assert temporal_entries > 0
    assert own_vs_gs_entries > 0


def test_reprocessing_does_not_duplicate_or_wipe_either_comparison_kind(app, db, tenant, tmp_path, monkeypatch):
    base_time = datetime.now(timezone.utc) - timedelta(days=2)

    upload_leipziger_liste(
        app, db, tenant, tmp_path, monkeypatch, "reprocess_own.pdf",
        rows=[LeipzigerListeRow(customer=ExtractedCustomer(name="Kunde A"), broker_number="VM-1001")],
        uploaded_at=base_time,
    )
    gs_document = upload_leipziger_liste(
        app, db, tenant, tmp_path, monkeypatch, "reprocess_gs.pdf",
        rows=[
            LeipzigerListeRow(customer=ExtractedCustomer(name="Kunde A"), broker_number="VM-1001"),
            LeipzigerListeRow(customer=ExtractedCustomer(name="Kunde F"), broker_number="VM-3003"),
        ],
        uploaded_at=base_time + timedelta(days=1),
    )

    # Die eigene Liste existiert bereits vor der GS-Liste, deshalb bekommt die GS-Liste beim
    # ersten Verarbeiten bereits BEIDE Vergleichsarten: TEMPORAL (zeitbasiert) und OWN_VS_GS
    # (gegen die vorhandene eigene Liste).
    temporal_count_before = ListComparison.query.filter_by(
        document_id=gs_document.id, comparison_kind=ComparisonKind.TEMPORAL
    ).count()
    own_vs_gs_count_before = ListComparison.query.filter_by(
        document_id=gs_document.id, comparison_kind=ComparisonKind.OWN_VS_GS
    ).count()
    assert temporal_count_before == 1
    assert own_vs_gs_count_before == 1

    with app.app_context():
        process_document(gs_document.id)

    temporal_count_after = ListComparison.query.filter_by(
        document_id=gs_document.id, comparison_kind=ComparisonKind.TEMPORAL
    ).count()
    own_vs_gs_count_after = ListComparison.query.filter_by(
        document_id=gs_document.id, comparison_kind=ComparisonKind.OWN_VS_GS
    ).count()
    assert temporal_count_after == 1
    assert own_vs_gs_count_after == 1


def test_find_paired_gs_or_own_document_returns_none_without_list_scope(app, db, tenant):
    document = Document(
        filename="no_scope.pdf", original_filename="no_scope.pdf", file_path="/tmp/no_scope.pdf",
        tenant_id=tenant.id,
    )
    db.session.add(document)
    db.session.commit()
    assert find_paired_gs_or_own_document(document) is None


def test_compare_leipziger_liste_default_kind_is_temporal(app, db, tenant, tmp_path, monkeypatch):
    base_time = datetime.now(timezone.utc) - timedelta(days=1)
    document = upload_leipziger_liste(
        app, db, tenant, tmp_path, monkeypatch, "default_kind.pdf",
        rows=[LeipzigerListeRow(customer=ExtractedCustomer(name="Kunde A"))],
        uploaded_at=base_time,
    )
    # compare_leipziger_liste() ohne explizite Argumente (bestehendes Aufrufmuster) muss
    # weiterhin genau wie bisher funktionieren - kein previous_document vorhanden -> None.
    with app.app_context():
        result = compare_leipziger_liste(document)
    assert result is None
