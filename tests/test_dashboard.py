from datetime import datetime, timezone

from app.models import Customer, DocStatus, Document, DocumentCustomer
from app.models.enums import DocType
from app.services.dashboard import build_dashboard_view


def make_document(db, tenant_id, filename, status=DocStatus.DONE, uploaded_at=None):
    document = Document(
        filename=filename,
        original_filename=filename,
        file_path=f"/tmp/{filename}",
        tenant_id=tenant_id,
        doc_type=DocType.LEIPZIGER_LISTE,
        status=status,
    )
    if uploaded_at is not None:
        document.uploaded_at = uploaded_at
    db.session.add(document)
    db.session.commit()
    return document


def make_doc_customer(db, tenant_id, document, customer_name, row_data, field_confidence=None):
    customer = Customer(tenant_id=tenant_id, name=customer_name)
    db.session.add(customer)
    db.session.commit()
    document_customer = DocumentCustomer(
        document=document,
        customer=customer,
        tenant_id=tenant_id,
        row_data=row_data,
        field_confidence=field_confidence or [{} for _ in row_data],
    )
    db.session.add(document_customer)
    db.session.commit()
    return document_customer


def metric_map(view):
    return {metric["label"]: metric["value"] for metric in view["metrics"]}


def test_build_dashboard_view_uses_real_leipziger_fields_and_vm_scope(db, tenant, user):
    document = make_document(
        db,
        tenant.id,
        "scope.pdf",
        uploaded_at=datetime(2026, 7, 21, 8, 30, tzinfo=timezone.utc),
    )
    make_doc_customer(
        db,
        tenant.id,
        document,
        "Anna Angebot",
        [{"broker_number": "VM-1001", "product_line": "KFZ", "is_angebot": True}],
    )
    make_doc_customer(
        db,
        tenant.id,
        document,
        "Peter Antrag",
        [{"broker_number": "VM-1001", "product_line": "Hausrat", "has_antrag": True}],
    )
    make_doc_customer(
        db,
        tenant.id,
        document,
        "Klara Abschluss",
        [{"broker_number": "VM-1001", "product_line": "Leben", "contract_start_date": "2026-01-15"}],
    )
    make_doc_customer(
        db,
        tenant.id,
        document,
        "Sven Storno",
        [{"broker_number": "VM-1001", "product_line": "KFZ", "is_storno": True}],
    )
    make_doc_customer(
        db,
        tenant.id,
        document,
        "Fremde Vermittlung",
        [{"broker_number": "VM-2002", "product_line": "KFZ", "is_angebot": True}],
    )

    view = build_dashboard_view(user)

    assert view["scope_note"] == "Fokus auf Vermittlernummer VM-1001."
    assert metric_map(view) == {
        "Antraege eingereicht": 1,
        "Angebote offen": 1,
        "Beginn vorhanden": 1,
        "Nacharbeit erforderlich": 1,
    }
    assert [case["customer_name"] for case in view["cases"]] == [
        "Sven Storno",
        "Peter Antrag",
        "Anna Angebot",
    ]
    assert all(case["status_key"] != "closed" for case in view["cases"])


def test_build_dashboard_view_falls_back_to_all_rows_without_vm_match(db, tenant, user):
    document = make_document(
        db,
        tenant.id,
        "fallback.pdf",
        uploaded_at=datetime(2026, 7, 20, 11, 0, tzinfo=timezone.utc),
    )
    make_doc_customer(
        db,
        tenant.id,
        document,
        "Geschaeftsstelle Angebot",
        [{"broker_number": "VM-9999", "product_line": "KFZ", "is_angebot": True}],
    )

    user.vermittlernummer = "08/0950-T"
    db.session.commit()

    view = build_dashboard_view(user)

    assert "Keine direkte Zuordnung fuer Vermittlernummer 08/0950-T gefunden." in view["scope_note"]
    assert metric_map(view)["Angebote offen"] == 1
    assert view["cases"][0]["customer_name"] == "Geschaeftsstelle Angebot"


def test_dashboard_route_renders_focused_sections(auth_client, db, tenant):
    document = make_document(
        db,
        tenant.id,
        "render.pdf",
        status=DocStatus.AI_PROCESSING,
        uploaded_at=datetime(2026, 7, 21, 9, 15, tzinfo=timezone.utc),
    )
    make_doc_customer(
        db,
        tenant.id,
        document,
        "Route Angebot",
        [{"broker_number": "VM-1001", "product_line": "KFZ", "is_angebot": True}],
    )

    response = auth_client.get("/")
    html = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "Auswertung deiner Leipziger Listen" in html
    assert "Aktuelle Vorgaenge" in html
    assert "Letzte Dokumente" in html
    assert "/documents#upload-widget" in html
    assert "Operations Workspace" not in html
    assert "Queue live" not in html
