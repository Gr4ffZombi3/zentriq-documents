import pytest

from app.models import Document, DocStatus, Tenant
from app.tenancy import (
    MissingTenantContextError,
    bypass_tenant_scope,
    get_or_404_scoped,
    set_current_tenant_id,
)


def make_document(db, tenant_id, filename):
    document = Document(
        filename=filename,
        original_filename=filename,
        file_path=f"/tmp/{filename}",
        status=DocStatus.PENDING,
        tenant_id=tenant_id,
    )
    db.session.add(document)
    db.session.commit()
    return document


def test_query_without_tenant_context_raises(db):
    set_current_tenant_id(None)
    with pytest.raises(MissingTenantContextError):
        Document.query.all()


def test_documents_are_isolated_between_tenants(app, db, tenant):
    tenant_b = Tenant(name="Tenant B", slug="tenant-b")
    db.session.add(tenant_b)
    db.session.commit()

    doc_a = make_document(db, tenant.id, "a.pdf")

    set_current_tenant_id(tenant_b.id)
    doc_b = make_document(db, tenant_b.id, "b.pdf")

    set_current_tenant_id(tenant.id)
    visible = Document.query.all()
    assert visible == [doc_a]

    set_current_tenant_id(tenant_b.id)
    visible = Document.query.all()
    assert visible == [doc_b]


def test_get_or_404_scoped_blocks_cross_tenant_access(app, db, tenant, client):
    tenant_b = Tenant(name="Tenant B", slug="tenant-b-2")
    db.session.add(tenant_b)
    db.session.commit()

    set_current_tenant_id(tenant_b.id)
    doc_b = make_document(db, tenant_b.id, "b.pdf")

    set_current_tenant_id(tenant.id)
    with app.test_request_context():
        with pytest.raises(Exception) as exc_info:
            get_or_404_scoped(Document, doc_b.id)
        # Flask's abort(404) raises a Werkzeug HTTPException with code 404.
        assert getattr(exc_info.value, "code", None) == 404


def test_bypass_tenant_scope_sees_all_tenants(app, db, tenant):
    tenant_b = Tenant(name="Tenant B", slug="tenant-b-3")
    db.session.add(tenant_b)
    db.session.commit()

    doc_a = make_document(db, tenant.id, "a.pdf")
    set_current_tenant_id(tenant_b.id)
    doc_b = make_document(db, tenant_b.id, "b.pdf")
    set_current_tenant_id(tenant.id)

    with bypass_tenant_scope():
        all_documents = Document.query.order_by(Document.filename).all()

    assert all_documents == [doc_a, doc_b]


def test_tenant_queries_never_require_tenant_context():
    set_current_tenant_id(None)
    # Tenant selbst ist nicht mandantengebunden - darf ohne Kontext abgefragt werden.
    Tenant.query.all()
