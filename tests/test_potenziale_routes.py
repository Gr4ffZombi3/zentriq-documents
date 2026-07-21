from app.models import Customer, Document, DocumentCustomer, Tenant
from app.models.enums import DocStatus, DocType, ListType
from app.tenancy import set_current_tenant_id


def make_document(db, tenant_id, filename="liste.pdf", list_type=ListType.OWN):
    document = Document(
        filename=filename,
        original_filename=filename,
        file_path=f"/tmp/{filename}",
        tenant_id=tenant_id,
        doc_type=DocType.LEIPZIGER_LISTE,
        status=DocStatus.DONE,
        list_type=list_type,
    )
    db.session.add(document)
    db.session.commit()
    return document


def make_doc_customer(db, tenant_id, document, customer_name, row):
    customer = Customer(tenant_id=tenant_id, name=customer_name)
    db.session.add(customer)
    db.session.commit()
    dc = DocumentCustomer(document=document, customer=customer, tenant_id=tenant_id, row_data=[row], field_confidence=[{}])
    db.session.add(dc)
    db.session.commit()
    return dc


def test_potenziale_requires_login(client):
    resp = client.get("/potenziale")
    assert resp.status_code == 302
    assert "/auth/login" in resp.headers["Location"]


def test_potenziale_renders_with_no_data(auth_client, db):
    resp = auth_client.get("/potenziale")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "Auswertungen" in body
    assert "Noch keine Leipziger Listen" in body


def test_potenziale_shows_document_selector_and_translated_status(auth_client, db, tenant):
    document = make_document(db, tenant.id)
    make_doc_customer(db, tenant.id, document, "Angebot Kunde", {"is_angebot": True, "broker_number": "VM-1001"})

    resp = auth_client.get("/potenziale")
    body = resp.get_data(as_text=True)

    assert "Leipziger Liste auswaehlen" in body
    assert "Angebot Kunde" in body
    assert "Angebot offen" in body
    assert document.original_filename in body


def test_potenziale_document_selector_limits_rows_to_selected_document(auth_client, db, tenant):
    first = make_document(db, tenant.id, "erste.pdf")
    second = make_document(db, tenant.id, "zweite.pdf")
    make_doc_customer(db, tenant.id, first, "Erster Kunde", {"is_angebot": True, "broker_number": "VM-1001"})
    make_doc_customer(db, tenant.id, second, "Zweiter Kunde", {"contract_start_date": "2026-01-01", "broker_number": "VM-1001"})

    resp = auth_client.get(f"/potenziale?document_id={second.id}")
    body = resp.get_data(as_text=True)

    assert "Zweiter Kunde" in body
    assert "Erster Kunde" not in body
    assert "zweite.pdf" in body


def test_potenziale_status_filter_shows_only_matching_rows(auth_client, db, tenant):
    document = make_document(db, tenant.id)
    make_doc_customer(db, tenant.id, document, "Neu Kunde", {"is_neugeschaeft": True, "broker_number": "VM-1001"})
    make_doc_customer(db, tenant.id, document, "Angebot Kunde", {"is_angebot": True, "broker_number": "VM-1001"})

    resp = auth_client.get(f"/potenziale?document_id={document.id}&status_filter=neugeschaeft")
    body = resp.get_data(as_text=True)

    assert "Neu Kunde" in body
    assert "Angebot Kunde" not in body
    assert 'value="neugeschaeft"' in body


def test_potenziale_tenant_isolation(auth_client, db, tenant):
    document = make_document(db, tenant.id)
    make_doc_customer(db, tenant.id, document, "Eigener Kunde", {"is_angebot": True, "broker_number": "VM-1001"})

    tenant_b = Tenant(name="Tenant B", slug="tenant-b-potenziale")
    db.session.add(tenant_b)
    db.session.commit()
    set_current_tenant_id(tenant_b.id)
    other_document = make_document(db, tenant_b.id, "andere.pdf")
    make_doc_customer(db, tenant_b.id, other_document, "Fremder Kunde", {"is_angebot": True, "broker_number": "VM-2002"})
    set_current_tenant_id(tenant.id)

    resp = auth_client.get("/potenziale")
    body = resp.get_data(as_text=True)
    assert "Eigener Kunde" in body
    assert "Fremder Kunde" not in body
