from app.models import Customer, Document
from app.services.documents import apply_leipziger_liste_extraction
from app.services.llm.schemas import ExtractedCustomer, LeipzigerListeExtraction, LeipzigerListeRow


def test_customer_detail_requires_login(client, db, tenant):
    customer = Customer(tenant_id=tenant.id, name="Detail Kunde")
    db.session.add(customer)
    db.session.commit()

    resp = client.get(f"/customers/{customer.id}")
    assert resp.status_code == 302
    assert "/auth/login" in resp.headers["Location"]


def test_customer_detail_shows_empty_state_without_history(auth_client, db, tenant):
    customer = Customer(tenant_id=tenant.id, name="Leere Historie")
    db.session.add(customer)
    db.session.commit()

    resp = auth_client.get(f"/customers/{customer.id}")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "Leere Historie" in body
    assert "Noch keine Ereignisse erfasst." in body
    assert "Dieser Kunde wurde noch in keiner Leipziger Liste erfasst." in body


def test_customer_detail_shows_merged_timeline_and_leipziger_liste_history(auth_client, db, tenant):
    document = Document(
        filename="liste.pdf", original_filename="liste.pdf", file_path="/tmp/liste.pdf", tenant_id=tenant.id
    )
    db.session.add(document)
    db.session.commit()

    extraction = LeipzigerListeExtraction(
        rows=[
            LeipzigerListeRow(
                customer=ExtractedCustomer(name="Chronik Kunde"), is_angebot=True, is_neugeschaeft=True
            )
        ]
    )
    apply_leipziger_liste_extraction(document, extraction)
    db.session.commit()

    customer = Customer.query.filter_by(name="Chronik Kunde").one()
    resp = auth_client.get(f"/customers/{customer.id}")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "Dokument hochgeladen" in body
    assert "Angebot erkannt" in body
    assert "Neuer Vertrag erkannt" in body
    assert "liste.pdf" in body


def test_customer_detail_returns_404_for_other_tenant(auth_client, db, tenant):
    from app.models import Tenant
    from app.tenancy import set_current_tenant_id

    other_tenant = Tenant(name="Andere Firma", slug="andere-firma-detail")
    db.session.add(other_tenant)
    db.session.commit()

    set_current_tenant_id(other_tenant.id)
    other_customer = Customer(tenant_id=other_tenant.id, name="Fremder Kunde")
    db.session.add(other_customer)
    db.session.commit()
    set_current_tenant_id(tenant.id)

    resp = auth_client.get(f"/customers/{other_customer.id}")
    assert resp.status_code == 404
