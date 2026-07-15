import io

import fitz
import pytest

from app.models import (
    Customer,
    CustomerTimelineEvent,
    Document,
    DocumentCustomer,
    ListComparison,
    ListComparisonEntry,
    Task,
    Tenant,
    User,
)
from app.models.enums import DocType, TaskStatus, TaskType
from app.services.llm.schemas import (
    DocumentExtraction,
    ExtractedCustomer,
    LeipzigerListeExtraction,
    LeipzigerListeRow,
)
from app.tenancy import MissingTenantContextError, set_current_tenant_id


def make_pdf_bytes() -> bytes:
    doc = fitz.open()
    doc.new_page()
    data = doc.tobytes()
    doc.close()
    return data


def test_full_upload_pipeline_creates_task_and_timeline_event(auth_client, db, user, monkeypatch):
    """End-to-End ueber die echte /upload-Route (nicht direkt apply_leipziger_liste_extraction):
    Upload -> Recommendation -> Task -> CustomerTimelineEvent, alles in einer Kette."""
    extraction = LeipzigerListeExtraction(
        rows=[LeipzigerListeRow(customer=ExtractedCustomer(name="E2E Kunde"), is_neugeschaeft=True, is_angebot=True)]
    )
    monkeypatch.setattr(
        "app.tasks.document_tasks.extract_document_data",
        lambda raw_text: DocumentExtraction(doc_type=DocType.LEIPZIGER_LISTE),
    )
    monkeypatch.setattr("app.tasks.document_tasks.extract_leipziger_liste_rows", lambda raw_text: extraction)

    resp = auth_client.post(
        "/upload",
        data={"file": (io.BytesIO(make_pdf_bytes()), "e2e.pdf")},
        content_type="multipart/form-data",
    )
    assert resp.status_code == 302

    document = Document.query.filter_by(original_filename="e2e.pdf").one()
    assert document.status.value == "done"
    assert document.uploaded_by_user_id == user.id

    customer = Customer.query.filter_by(name="E2E Kunde").one()
    assert customer.assigned_user_id == user.id

    tasks = Task.query.filter_by(customer_id=customer.id).all()
    assert len(tasks) >= 1  # call_today (aus Recommendation) + Angebots-Folgeaufgaben

    events = CustomerTimelineEvent.query.filter_by(customer_id=customer.id).all()
    event_types = {e.event_type.value for e in events}
    assert "document_uploaded" in event_types
    assert "task_created" in event_types


def test_second_leipziger_liste_upload_via_route_creates_list_comparison(auth_client, db, user, monkeypatch):
    def patch_and_upload(filename, rows):
        extraction = LeipzigerListeExtraction(rows=rows)
        monkeypatch.setattr(
            "app.tasks.document_tasks.extract_document_data",
            lambda raw_text: DocumentExtraction(doc_type=DocType.LEIPZIGER_LISTE),
        )
        monkeypatch.setattr("app.tasks.document_tasks.extract_leipziger_liste_rows", lambda raw_text: extraction)
        resp = auth_client.post(
            "/upload",
            data={"file": (io.BytesIO(make_pdf_bytes()), filename)},
            content_type="multipart/form-data",
        )
        assert resp.status_code == 302
        return Document.query.filter_by(original_filename=filename).one()

    patch_and_upload("liste_v1.pdf", [LeipzigerListeRow(customer=ExtractedCustomer(name="Vergleichskunde"))])
    document2 = patch_and_upload(
        "liste_v2.pdf",
        [
            LeipzigerListeRow(customer=ExtractedCustomer(name="Vergleichskunde")),
            LeipzigerListeRow(customer=ExtractedCustomer(name="Ganz neuer Kunde")),
        ],
    )

    comparison = ListComparison.query.filter_by(document_id=document2.id).one()
    assert comparison.new_customer_count == 1


def test_tasks_are_isolated_between_tenants_via_route(auth_client, db, tenant, user):
    tenant_b = Tenant(name="Tenant B", slug="tenant-b-m11-integration")
    db.session.add(tenant_b)
    db.session.commit()

    set_current_tenant_id(tenant_b.id)
    other_user = User(tenant_id=tenant_b.id, email="other-tenant@example.com")
    other_user.set_password("passwort123")
    db.session.add(other_user)
    db.session.flush()

    foreign_task = Task(
        tenant_id=tenant_b.id,
        type=TaskType.OTHER,
        title="Fremde Aufgabe",
        status=TaskStatus.OPEN,
    )
    db.session.add(foreign_task)
    db.session.commit()
    set_current_tenant_id(tenant.id)

    # Tenant A's eingeloggter User darf die Aufgabe eines anderen Tenants weder sehen...
    list_resp = auth_client.get("/tasks?status=all")
    assert foreign_task.title not in list_resp.get_data(as_text=True)

    # ...noch ueber die Status-Route veraendern (get_or_404_scoped -> 404).
    status_resp = auth_client.post(f"/tasks/{foreign_task.id}/status", data={"status": "done"})
    assert status_resp.status_code == 404


def test_document_customer_and_list_comparison_entry_require_tenant_context(db):
    set_current_tenant_id(None)

    with pytest.raises(MissingTenantContextError):
        DocumentCustomer.query.all()
    with pytest.raises(MissingTenantContextError):
        ListComparisonEntry.query.all()
