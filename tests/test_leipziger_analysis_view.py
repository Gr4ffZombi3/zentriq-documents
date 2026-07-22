from datetime import date, datetime, timezone

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


def make_doc_customer(db, tenant_id, document, customer_name, row, confidence=None, customer_kwargs=None):
    customer = Customer(tenant_id=tenant_id, name=customer_name, **(customer_kwargs or {}))
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
    assert labels["Nina Neu"] == "Neugeschäft"
    assert labels["Fritz Wechsel"] == "Fahrzeugwechsel"


def test_build_document_analysis_treats_begin_as_completion_not_status_family(db, tenant):
    document = make_document(db, tenant.id, "abschluss.pdf")
    make_doc_customer(
        db,
        tenant.id,
        document,
        "Klara Beginn",
        {"contract_start_date": "2026-01-15", "broker_number": "VM-1001"},
    )

    analysis = build_document_analysis(document_id=document.id, current_broker_number="VM-1001")

    assert analysis["rows"][0]["status_label"] == "Unklar"
    assert analysis["rows"][0]["completion_label"] == "Abgeschlossen"
    assert analysis["summary"]["abgeschlossen"] == 1


def test_build_document_analysis_keeps_fahrzeugwechsel_status_when_begin_exists(db, tenant):
    document = make_document(db, tenant.id, "fzw.pdf")
    make_doc_customer(
        db,
        tenant.id,
        document,
        "Hans Kohlhammer",
        {
            "contract_number": "508/001164-L",
            "status_code": "FZW",
            "is_fahrzeugwechsel": True,
            "contract_start_date": "2026-07-13",
            "broker_number": "08/0950-T",
        },
    )

    analysis = build_document_analysis(document_id=document.id, current_broker_number="08/0950-T")
    row = analysis["rows"][0]

    assert row["status_label"] == "Fahrzeugwechsel"
    assert row["completion_label"] == "Abgeschlossen"
    assert row["result_label"] == "Beginn vorhanden"
    assert analysis["summary"]["fahrzeugwechsel"] == 1
    assert analysis["summary"]["abgeschlossen"] == 1


def test_build_document_analysis_groups_same_customer_but_keeps_contract_rows(db, tenant):
    document = make_document(db, tenant.id, "gruppe.pdf")
    customer_kwargs = {"date_of_birth": date(1985, 2, 17), "postal_code": "04109", "city": "Leipzig"}
    make_doc_customer(
        db,
        tenant.id,
        document,
        "Maria Beispiel",
        {"contract_number": "A-1", "is_angebot": True, "product_line": "PH", "source_page": 1, "source_row": 1},
        customer_kwargs=customer_kwargs,
    )
    make_doc_customer(
        db,
        tenant.id,
        document,
        "Maria Beispiel",
        {"contract_number": "B-2", "is_neugeschaeft": True, "product_line": "RS", "source_page": 2, "source_row": 1},
        customer_kwargs=customer_kwargs,
    )

    analysis = build_document_analysis(document_id=document.id, current_broker_number="VM-1001")

    assert len(analysis["grouped_customers"]) == 1
    group = analysis["grouped_customers"][0]
    assert group["record_count"] == 2
    assert {row["contract_number"] for row in group["rows"]} == {"A-1", "B-2"}


def test_build_document_analysis_does_not_merge_same_name_with_different_birth_dates(db, tenant):
    document = make_document(db, tenant.id, "dublette.pdf")
    make_doc_customer(
        db,
        tenant.id,
        document,
        "Chris Beispiel",
        {"contract_number": "A-1", "is_angebot": True},
        customer_kwargs={"date_of_birth": date(1980, 1, 1), "postal_code": "04109"},
    )
    make_doc_customer(
        db,
        tenant.id,
        document,
        "Chris Beispiel",
        {"contract_number": "B-2", "is_neugeschaeft": True},
        customer_kwargs={"date_of_birth": date(1992, 1, 1), "postal_code": "04109"},
    )

    analysis = build_document_analysis(document_id=document.id, current_broker_number="VM-1001")

    assert len(analysis["grouped_customers"]) == 2
    assert all(group["possible_duplicate"] is True for group in analysis["grouped_customers"])


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
