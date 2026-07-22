from app.models import Customer, Document, DocumentCustomer
from app.models.enums import DocStatus, DocType, ListType


def test_document_detail_shows_partial_leipziger_analysis(auth_client, db, tenant):
    document = Document(
        filename="kw29_heller.pdf",
        original_filename="kw29_heller.pdf",
        file_path="/tmp/kw29_heller.pdf",
        tenant_id=tenant.id,
        doc_type=DocType.LEIPZIGER_LISTE,
        status=DocStatus.FAILED,
        error_message="Teilweise ausgewertet - 3 von 4 Seiten verarbeitet",
        list_type=ListType.OWN,
        raw_json={"rows": [{"contract_number": "508/001164-L"}]},
        extra_data={
            "leipziger_analysis": {
                "total_pages": 4,
                "processed_pages": 3,
                "failed_pages": [4],
                "failed_page_count": 1,
                "raw_row_count": 1,
                "stored_row_count": 1,
                "discarded_duplicate_count": 0,
                "uncertain_row_count": 0,
                "is_complete": False,
                "completion_label": "Teilweise ausgewertet - 3 von 4 Seiten verarbeitet",
            }
        },
    )
    customer = Customer(tenant_id=tenant.id, name="Hans Kohlhammer", city="Leipzig", postal_code="04109")
    db.session.add_all([document, customer])
    db.session.commit()
    db.session.add(
        DocumentCustomer(
            document=document,
            customer=customer,
            tenant_id=tenant.id,
            row_data=[
                {
                    "contract_number": "508/001164-L",
                    "status_code": "FZW",
                    "is_fahrzeugwechsel": True,
                    "contract_start_date": "2026-07-13",
                    "source_page": 1,
                    "source_row": 1,
                }
            ],
            field_confidence=[{}],
        )
    )
    db.session.commit()

    resp = auth_client.get(f"/documents/{document.id}")
    body = resp.get_data(as_text=True)

    assert resp.status_code == 200
    assert "Teilweise ausgewertet - 3 von 4 Seiten verarbeitet" in body
    assert "Hans Kohlhammer" in body
    assert "508/001164-L" in body
