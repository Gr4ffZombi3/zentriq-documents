from datetime import datetime, timezone

from app.models import Customer, Document, DocumentCustomer
from app.models.enums import DocStatus, DocType


def make_document(db, tenant_id, filename="liste.pdf"):
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


def test_customer_list_renders_empty_state(auth_client, db):
    resp = auth_client.get("/customers")
    assert resp.status_code == 200

    body = resp.get_data(as_text=True)
    assert "Kunden" in body
    assert "Noch keine Kunden" in body
    assert "Uebersicht" not in body


def test_customer_list_renders_table_with_umlauts(auth_client, db, tenant):
    customer = Customer(tenant_id=tenant.id, name="Anna Beispiel", city="Köln", postal_code="50667")
    document = make_document(db, tenant.id, "kundenliste.pdf")
    db.session.add(customer)
    db.session.commit()

    db.session.add(
        DocumentCustomer(
            tenant_id=tenant.id,
            document=document,
            customer=customer,
            row_data=[{"is_angebot": True, "broker_number": "VM-1001", "product_line": "KFZ"}],
            field_confidence=[{}],
        )
    )
    db.session.commit()

    resp = auth_client.get("/customers")
    assert resp.status_code == 200

    body = resp.get_data(as_text=True)
    assert "Anna Beispiel" in body
    assert "Vorgänge" in body
    assert "Kundenakte öffnen" in body
    assert "Vorgaenge" not in body
