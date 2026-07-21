from datetime import datetime, timezone

from app.models import Customer, Document, DocumentCustomer, ListType
from app.models.enums import DocStatus, DocType
from app.services.analysis.leipziger_liste_view import build_document_analysis


def make_document(db, tenant_id, filename="liste.pdf"):
    document = Document(
        filename=filename,
        original_filename=filename,
        file_path=f"/tmp/{filename}",
        tenant_id=tenant_id,
        doc_type=DocType.LEIPZIGER_LISTE,
        status=DocStatus.DONE,
        list_type=ListType.OWN,
        uploaded_at=datetime(2026, 7, 21, 12, 0, tzinfo=timezone.utc),
    )
    db.session.add(document)
    db.session.commit()
    return document


def make_doc_customer(db, tenant_id, document, customer_name, row, confidence=None):
    customer = Customer(tenant_id=tenant_id, name=customer_name)
    db.session.add(customer)
    db.session.commit()
    db.session.add(
        DocumentCustomer(
            document=document,
            customer=customer,
            tenant_id=tenant_id,
            row_data=[row],
            field_confidence=[confidence or {}],
        )
    )
    db.session.commit()


def test_build_document_analysis_translates_offer_new_business_and_vehicle_change(db, tenant):
    document = make_document(db, tenant.id)
    make_doc_customer(db, tenant.id, document, "Anna Angebot", {"is_angebot": True, "broker_number": "VM-1001"})
    make_doc_customer(db, tenant.id, document, "Nina Neu", {"is_neugeschaeft": True, "broker_number": "VM-1001"})
    make_doc_customer(db, tenant.id, document, "Fritz Wechsel", {"is_fahrzeugwechsel": True, "broker_number": "VM-1001"})

    analysis = build_document_analysis(document_id=document.id, current_broker_number="VM-1001")
    labels = {row["customer_name"]: row["status_label"] for row in analysis["rows"]}

    assert labels["Anna Angebot"] == "Angebot"
    assert labels["Nina Neu"] == "Neugeschaeft"
    assert labels["Fritz Wechsel"] == "Fahrzeugwechsel"


def test_build_document_analysis_marks_contract_start_as_abgeschlossen(db, tenant):
    document = make_document(db, tenant.id, "abschluss.pdf")
    make_doc_customer(
        db,
        tenant.id,
        document,
        "Klara Beginn",
        {"contract_start_date": "2026-01-15", "broker_number": "VM-1001"},
    )

    analysis = build_document_analysis(document_id=document.id, current_broker_number="VM-1001")

    assert analysis["rows"][0]["status_label"] == "Abgeschlossen"
    assert analysis["summary"]["abgeschlossen"] == 1


def test_build_document_analysis_uses_document_selector_and_uncertain_status(db, tenant):
    first = make_document(db, tenant.id, "erste.pdf")
    second = make_document(db, tenant.id, "zweite.pdf")
    make_doc_customer(db, tenant.id, first, "Erster Kunde", {"is_angebot": True, "broker_number": "VM-1001"})
    make_doc_customer(
        db,
        tenant.id,
        second,
        "Unsicher Kunde",
        {"has_antrag": False, "broker_number": "VM-1001"},
        confidence={"has_antrag": {"uncertain": True}},
    )

    analysis = build_document_analysis(document_id=second.id, current_broker_number="VM-1001")

    assert analysis["selected_document"]["id"] == second.id
    assert any(option["id"] == first.id for option in analysis["document_options"])
    assert analysis["rows"][0]["safety_label"] == "Unklar"
    assert analysis["summary"]["ohne_antrag"] == 0
