from datetime import datetime, timezone

from app.models import Customer, Document, DocumentCustomer
from app.models.enums import DocStatus, DocType
from app.services.customers import CustomerMatcher, build_customer_detail_context, build_customer_directory
from app.services.llm.schemas import ExtractedCustomer


def make_document(db, tenant_id, filename):
    document = Document(
        filename=filename,
        original_filename=filename,
        file_path=f"/tmp/{filename}",
        tenant_id=tenant_id,
        doc_type=DocType.LEIPZIGER_LISTE,
        status=DocStatus.DONE,
        uploaded_at=datetime(2026, 7, 21, 9, 0, tzinfo=timezone.utc),
    )
    db.session.add(document)
    db.session.commit()
    return document


def add_doc_customer(db, tenant_id, document, customer, row_data, field_confidence=None):
    db.session.add(
        DocumentCustomer(
            tenant_id=tenant_id,
            document=document,
            customer=customer,
            row_data=row_data,
            field_confidence=field_confidence or [{} for _ in row_data],
        )
    )
    db.session.commit()


def test_customer_matcher_merges_same_normalized_name_and_date_of_birth(db, tenant, user):
    matcher = CustomerMatcher()

    first = matcher.get_or_create(
        ExtractedCustomer(name="Anna Mueller", date_of_birth=datetime(1990, 5, 14).date()),
        uploaded_by_user_id=user.id,
    )
    db.session.flush()
    second = matcher.get_or_create(
        ExtractedCustomer(name=" anna  müller ", date_of_birth=datetime(1990, 5, 14).date()),
        uploaded_by_user_id=user.id,
    )
    db.session.commit()

    assert first.id == second.id
    assert Customer.query.count() == 1


def test_customer_matcher_keeps_same_name_with_different_date_of_birth_separate(db, tenant, user):
    matcher = CustomerMatcher()

    matcher.get_or_create(
        ExtractedCustomer(name="Chris Beispiel", date_of_birth=datetime(1988, 1, 1).date()),
        uploaded_by_user_id=user.id,
    )
    matcher.get_or_create(
        ExtractedCustomer(name="Chris Beispiel", date_of_birth=datetime(1991, 2, 2).date()),
        uploaded_by_user_id=user.id,
    )
    db.session.commit()

    assert Customer.query.count() == 2


def test_customer_directory_marks_possible_duplicates(db, tenant):
    customer_a = Customer(tenant_id=tenant.id, name="Mara Beispiel")
    customer_b = Customer(tenant_id=tenant.id, name="mara   beispiel", postal_code="50667")
    db.session.add_all([customer_a, customer_b])
    db.session.commit()

    directory = build_customer_directory()
    mara_rows = [item for item in directory["items"] if item["customer"].id in {customer_a.id, customer_b.id}]

    assert len(mara_rows) == 2
    assert all(item["possible_duplicates"] for item in mara_rows)


def test_customer_detail_context_shows_multiple_cases_for_same_customer(db, tenant):
    customer = Customer(tenant_id=tenant.id, name="Sammelkunde", postal_code="04109")
    document = make_document(db, tenant.id, "kunde.pdf")
    db.session.add(customer)
    db.session.commit()

    add_doc_customer(
        db,
        tenant.id,
        document,
        customer,
        [
            {"product_line": "KFZ", "is_angebot": True, "broker_number": "VM-1001"},
            {"product_line": "Hausrat", "contract_start_date": "2026-01-15", "broker_number": "VM-1001"},
        ],
    )

    detail = build_customer_detail_context(customer)

    assert detail["summary"]["cases"] == 2
    assert detail["summary"]["offers"] == 1
    assert detail["summary"]["closures"] == 1
    assert {row["status_label"] for row in detail["case_rows"]} == {"Angebot", "Abgeschlossen"}
