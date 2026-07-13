from app.models import Customer, Document, DocStatus, DocType, Recommendation, RecommendationType


def test_index_returns_200(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert "Zentriq Documents" in resp.get_data(as_text=True)


def test_create_document_with_customer(db, tenant):
    customer = Customer(name="Max Mustermann", city="Köln", postal_code="50667", tenant_id=tenant.id)
    document = Document(
        filename="abc.pdf",
        original_filename="Rechnung.pdf",
        file_path="/storage/uploads/abc.pdf",
        doc_type=DocType.RECHNUNG,
        status=DocStatus.DONE,
        customer=customer,
        tenant_id=tenant.id,
    )
    db.session.add(document)
    db.session.commit()

    fetched = Document.query.first()
    assert fetched.customer.name == "Max Mustermann"
    assert fetched.doc_type == DocType.RECHNUNG


def test_recommendation_linked_to_document(db, tenant):
    customer = Customer(name="Erika Musterfrau", city="Berlin", tenant_id=tenant.id)
    document = Document(
        filename="liste.pdf",
        original_filename="Leipziger Liste.pdf",
        file_path="/storage/uploads/liste.pdf",
        doc_type=DocType.LEIPZIGER_LISTE,
        status=DocStatus.DONE,
        customer=customer,
        tenant_id=tenant.id,
    )
    recommendation = Recommendation(
        document=document,
        customer=customer,
        type=RecommendationType.CALL_TODAY,
        label="Heute anrufen",
        tenant_id=tenant.id,
    )
    db.session.add(recommendation)
    db.session.commit()

    fetched = Recommendation.query.first()
    assert fetched.document.original_filename == "Leipziger Liste.pdf"
    assert fetched.customer.name == "Erika Musterfrau"
