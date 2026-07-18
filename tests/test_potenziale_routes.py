from app.models import Customer, Document, DocumentCustomer, Tenant
from app.models.enums import DocType
from app.tenancy import set_current_tenant_id


def make_document(db, tenant_id, filename="liste.pdf"):
    document = Document(
        filename=filename,
        original_filename=filename,
        file_path=f"/tmp/{filename}",
        tenant_id=tenant_id,
        doc_type=DocType.LEIPZIGER_LISTE,
    )
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


def test_potenziale_requires_login(client):
    resp = client.get("/potenziale")
    assert resp.status_code == 302
    assert "/auth/login" in resp.headers["Location"]


def test_potenziale_renders_with_no_data(auth_client, db):
    resp = auth_client.get("/potenziale")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "Potenziale" in body
    assert "Keine passenden Datensätze" in body


def test_potenziale_shows_open_records_with_reason(auth_client, db, tenant):
    document = make_document(db, tenant.id)
    make_doc_customer(db, tenant.id, document, "Angebot Kunde", {"is_angebot": True})

    resp = auth_client.get("/potenziale")
    body = resp.get_data(as_text=True)
    assert "Angebot Kunde" in body
    assert "Kunde erscheint in der Liste als Angebot. Es wurde kein Versicherungsbeginn gefunden." in body


def test_potenziale_excludes_abgeschlossen_by_default(auth_client, db, tenant):
    document = make_document(db, tenant.id)
    make_doc_customer(db, tenant.id, document, "Abgeschlossen Kunde", {"contract_start_date": "2026-01-01"})

    resp = auth_client.get("/potenziale")
    body = resp.get_data(as_text=True)
    assert "Abgeschlossen Kunde" not in body


def test_potenziale_include_closed_filter_shows_abgeschlossen(auth_client, db, tenant):
    document = make_document(db, tenant.id)
    make_doc_customer(db, tenant.id, document, "Abgeschlossen Kunde", {"contract_start_date": "2026-01-01"})

    resp = auth_client.get("/potenziale?include_closed=1")
    body = resp.get_data(as_text=True)
    assert "Abgeschlossen Kunde" in body
    assert 'checked' in body


def test_potenziale_category_filter_reflected_in_form(auth_client, db, tenant):
    document = make_document(db, tenant.id)
    make_doc_customer(db, tenant.id, document, "Pruefen Kunde", {"has_antrag": True})
    make_doc_customer(db, tenant.id, document, "Angebot Kunde", {"is_angebot": True})

    resp = auth_client.get("/potenziale?category=pruefen")
    body = resp.get_data(as_text=True)
    assert "Pruefen Kunde" in body
    assert "Angebot Kunde" not in body
    assert 'value="pruefen" selected' in body


def test_potenziale_product_line_filter_reflected_in_input(auth_client, db, tenant):
    document = make_document(db, tenant.id)
    make_doc_customer(db, tenant.id, document, "KFZ Kunde", {"is_angebot": True, "product_line": "KFZ"})

    resp = auth_client.get("/potenziale?product_line=KFZ")
    body = resp.get_data(as_text=True)
    assert "KFZ Kunde" in body
    assert 'value="KFZ"' in body


def test_potenziale_tenant_isolation(auth_client, db, tenant):
    document = make_document(db, tenant.id)
    make_doc_customer(db, tenant.id, document, "Eigener Kunde", {"is_angebot": True})

    tenant_b = Tenant(name="Tenant B", slug="tenant-b-potenziale")
    db.session.add(tenant_b)
    db.session.commit()
    set_current_tenant_id(tenant_b.id)
    other_document = make_document(db, tenant_b.id, "andere.pdf")
    make_doc_customer(db, tenant_b.id, other_document, "Fremder Kunde", {"is_angebot": True})
    set_current_tenant_id(tenant.id)

    resp = auth_client.get("/potenziale")
    body = resp.get_data(as_text=True)
    assert "Eigener Kunde" in body
    assert "Fremder Kunde" not in body
