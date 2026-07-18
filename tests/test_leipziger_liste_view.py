from datetime import date, datetime, timezone

from app.models import Customer, Document, DocumentCustomer, ListScope, Tenant
from app.models.enums import DocType, PotentialCategory
from app.services.analysis.leipziger_liste_view import get_analysis_summary, get_potential_records
from app.tenancy import set_current_tenant_id


def make_document(db, tenant_id, filename="liste.pdf", list_scope=None, uploaded_at=None):
    document = Document(
        filename=filename, original_filename=filename, file_path=f"/tmp/{filename}",
        tenant_id=tenant_id, doc_type=DocType.LEIPZIGER_LISTE, list_scope=list_scope,
    )
    if uploaded_at is not None:
        document.uploaded_at = uploaded_at
    db.session.add(document)
    db.session.commit()
    return document


def make_doc_customer(db, tenant_id, document, customer_name, row):
    customer = Customer(tenant_id=tenant_id, name=customer_name)
    db.session.add(customer)
    db.session.commit()
    dc = DocumentCustomer(document=document, customer=customer, tenant_id=tenant_id, row_data=[row])
    db.session.add(dc)
    db.session.commit()
    return dc


def test_get_potential_records_excludes_abgeschlossen_by_default(app, db, tenant):
    document = make_document(db, tenant.id)
    make_doc_customer(db, tenant.id, document, "Abgeschlossen Kunde", {"contract_start_date": "2026-01-01"})
    make_doc_customer(db, tenant.id, document, "Angebot Kunde", {"is_angebot": True})

    records = get_potential_records()
    names = {r["customer_name"] for r in records}
    assert names == {"Angebot Kunde"}


def test_get_potential_records_include_closed_shows_everything(app, db, tenant):
    document = make_document(db, tenant.id)
    make_doc_customer(db, tenant.id, document, "Abgeschlossen Kunde", {"contract_start_date": "2026-01-01"})
    make_doc_customer(db, tenant.id, document, "Angebot Kunde", {"is_angebot": True})

    records = get_potential_records(include_closed=True)
    names = {r["customer_name"] for r in records}
    assert names == {"Abgeschlossen Kunde", "Angebot Kunde"}


def test_get_potential_records_filter_by_category(app, db, tenant):
    document = make_document(db, tenant.id)
    make_doc_customer(db, tenant.id, document, "Angebot Kunde", {"is_angebot": True})
    make_doc_customer(db, tenant.id, document, "Pruefen Kunde", {"has_antrag": True})

    records = get_potential_records(category=PotentialCategory.PRUEFEN)
    assert [r["customer_name"] for r in records] == ["Pruefen Kunde"]


def test_get_potential_records_explicit_abgeschlossen_filter_overrides_default_exclusion(app, db, tenant):
    document = make_document(db, tenant.id)
    make_doc_customer(db, tenant.id, document, "Abgeschlossen Kunde", {"contract_start_date": "2026-01-01"})

    records = get_potential_records(category=PotentialCategory.ABGESCHLOSSEN)
    assert [r["customer_name"] for r in records] == ["Abgeschlossen Kunde"]


def test_get_potential_records_filter_by_product_line(app, db, tenant):
    document = make_document(db, tenant.id)
    make_doc_customer(db, tenant.id, document, "KFZ Kunde", {"is_angebot": True, "product_line": "KFZ"})
    make_doc_customer(db, tenant.id, document, "Hausrat Kunde", {"is_angebot": True, "product_line": "Hausrat"})

    records = get_potential_records(product_line="KFZ")
    assert [r["customer_name"] for r in records] == ["KFZ Kunde"]


def test_get_potential_records_filter_by_broker_number(app, db, tenant):
    document = make_document(db, tenant.id)
    make_doc_customer(db, tenant.id, document, "VM1 Kunde", {"is_angebot": True, "broker_number": "VM-1001"})
    make_doc_customer(db, tenant.id, document, "VM2 Kunde", {"is_angebot": True, "broker_number": "VM-2002"})

    records = get_potential_records(broker_number="VM-1001")
    assert [r["customer_name"] for r in records] == ["VM1 Kunde"]


def test_get_potential_records_filter_by_date_range(app, db, tenant):
    old_document = make_document(db, tenant.id, "old.pdf", uploaded_at=datetime(2025, 1, 1, tzinfo=timezone.utc))
    new_document = make_document(db, tenant.id, "new.pdf", uploaded_at=datetime(2026, 6, 1, tzinfo=timezone.utc))
    make_doc_customer(db, tenant.id, old_document, "Alter Kunde", {"is_angebot": True})
    make_doc_customer(db, tenant.id, new_document, "Neuer Kunde", {"is_angebot": True})

    records = get_potential_records(date_from=date(2026, 1, 1))
    assert [r["customer_name"] for r in records] == ["Neuer Kunde"]


def test_get_potential_records_filter_by_list_scope(app, db, tenant):
    own_document = make_document(db, tenant.id, "own.pdf", list_scope=ListScope.OWN)
    gs_document = make_document(db, tenant.id, "gs.pdf", list_scope=ListScope.GESCHAEFTSSTELLE)
    make_doc_customer(db, tenant.id, own_document, "Own Kunde", {"is_angebot": True})
    make_doc_customer(db, tenant.id, gs_document, "GS Kunde", {"is_angebot": True})

    records = get_potential_records(list_scope=ListScope.OWN)
    assert [r["customer_name"] for r in records] == ["Own Kunde"]


def test_get_potential_records_includes_reason_and_document_id(app, db, tenant):
    document = make_document(db, tenant.id)
    make_doc_customer(db, tenant.id, document, "Angebot Kunde", {"is_angebot": True})

    records = get_potential_records()
    assert records[0]["reason"] == "Kunde erscheint in der Liste als Angebot. Es wurde kein Versicherungsbeginn gefunden."
    assert records[0]["document_id"] == document.id


def test_get_analysis_summary_counts_all_seven_metrics(app, db, tenant):
    document = make_document(db, tenant.id)
    make_doc_customer(db, tenant.id, document, "Abgeschlossen", {"contract_start_date": "2026-01-01"})
    make_doc_customer(db, tenant.id, document, "Storno", {"is_storno": True})
    make_doc_customer(db, tenant.id, document, "Angebot", {"is_angebot": True})
    make_doc_customer(db, tenant.id, document, "Antrag", {"has_antrag": True})
    make_doc_customer(db, tenant.id, document, "Offen", {})

    summary = get_analysis_summary()
    assert summary["total_records"] == 5
    assert summary["abgeschlossen"] == 1
    assert summary["stornos"] == 1
    assert summary["angebote"] == 1
    assert summary["offene_vorgaenge"] == 3  # Angebot, Antrag, Offen (nicht abgeschlossen/storniert)
    assert summary["ohne_beginn"] == 4
    assert summary["ohne_antrag"] == 4


def test_get_analysis_summary_scoped_to_single_document(app, db, tenant):
    document1 = make_document(db, tenant.id, "doc1.pdf")
    document2 = make_document(db, tenant.id, "doc2.pdf")
    make_doc_customer(db, tenant.id, document1, "In Doc1", {"is_angebot": True})
    make_doc_customer(db, tenant.id, document2, "In Doc2", {"is_angebot": True})

    summary = get_analysis_summary(document1)
    assert summary["total_records"] == 1


def test_get_potential_records_tenant_isolation(app, db, tenant):
    document = make_document(db, tenant.id)
    make_doc_customer(db, tenant.id, document, "Tenant A Kunde", {"is_angebot": True})

    tenant_b = Tenant(name="Tenant B", slug="tenant-b-ll-view")
    db.session.add(tenant_b)
    db.session.commit()
    set_current_tenant_id(tenant_b.id)

    records = get_potential_records()
    assert records == []

    set_current_tenant_id(tenant.id)
