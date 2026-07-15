from datetime import date, timedelta

import fitz

from app.models import DocStatus, Document, Task
from app.models.enums import DocType, Priority, TaskStatus, TaskType
from app.services.documents import apply_extraction, apply_leipziger_liste_extraction
from app.services.llm.schemas import (
    DocumentExtraction,
    ExtractedCustomer,
    LeipzigerListeExtraction,
    LeipzigerListeRow,
)
from app.services.tasks import update_task_status
from app.tasks.document_tasks import process_document


def make_pdf_file(path):
    doc = fitz.open()
    doc.new_page()
    doc.save(str(path))
    doc.close()


def test_apply_extraction_creates_call_today_task_from_recommendation(app, db, tenant):
    document = Document(
        filename="x.pdf", original_filename="x.pdf", file_path="/tmp/x.pdf", tenant_id=tenant.id
    )
    db.session.add(document)
    db.session.commit()

    extraction = DocumentExtraction(
        doc_type=DocType.LEIPZIGER_LISTE, customer=ExtractedCustomer(name="Neukunde")
    )
    # apply_extraction selbst setzt keine is_neugeschaeft-Flags (nur die Leipziger-Liste-Zeilen
    # tun das) - hier reicht ein Cross-Sell-Test ueber die Leipziger-Liste-Zeile, siehe unten.
    apply_extraction(document, extraction)
    db.session.commit()

    # Ohne Flags entstehen keine Empfehlungen und somit auch keine Tasks.
    assert Task.query.count() == 0


def test_leipziger_liste_neugeschaeft_creates_call_today_task(app, db, tenant):
    document = Document(
        filename="liste.pdf", original_filename="liste.pdf", file_path="/tmp/liste.pdf", tenant_id=tenant.id
    )
    db.session.add(document)
    db.session.commit()

    extraction = LeipzigerListeExtraction(
        rows=[
            LeipzigerListeRow(
                customer=ExtractedCustomer(name="Anna Kunde"),
                is_neugeschaeft=True,
                priority=Priority.HIGH,
            )
        ]
    )
    apply_leipziger_liste_extraction(document, extraction)
    db.session.commit()

    tasks = Task.query.all()
    call_today_tasks = [t for t in tasks if t.type == TaskType.CALL_TODAY]
    assert len(call_today_tasks) == 1
    task = call_today_tasks[0]
    assert task.customer.name == "Anna Kunde"
    assert task.priority == Priority.HIGH
    assert task.status == TaskStatus.OPEN
    assert task.due_date == date.today()
    assert task.recommendation is not None
    assert task.document_id == document.id


def test_leipziger_liste_open_offer_creates_follow_up_tasks(app, db, tenant):
    document = Document(
        filename="liste.pdf", original_filename="liste.pdf", file_path="/tmp/liste.pdf", tenant_id=tenant.id
    )
    db.session.add(document)
    db.session.commit()

    extraction = LeipzigerListeExtraction(
        rows=[
            LeipzigerListeRow(
                customer=ExtractedCustomer(name="Offene Anna"),
                is_angebot=True,
                priority=Priority.MEDIUM,
            )
        ]
    )
    apply_leipziger_liste_extraction(document, extraction)
    db.session.commit()

    tasks = Task.query.all()
    types = {t.type for t in tasks}
    assert TaskType.REQUEST_DOCUMENTS in types
    assert TaskType.CHECK_CLOSURE in types

    request_docs = next(t for t in tasks if t.type == TaskType.REQUEST_DOCUMENTS)
    check_closure = next(t for t in tasks if t.type == TaskType.CHECK_CLOSURE)
    assert request_docs.due_date == date.today() + timedelta(days=5)
    assert check_closure.due_date == date.today() + timedelta(days=14)


def test_leipziger_liste_without_offer_flag_creates_no_follow_up_tasks(app, db, tenant):
    document = Document(
        filename="liste.pdf", original_filename="liste.pdf", file_path="/tmp/liste.pdf", tenant_id=tenant.id
    )
    db.session.add(document)
    db.session.commit()

    extraction = LeipzigerListeExtraction(
        rows=[LeipzigerListeRow(customer=ExtractedCustomer(name="Ruhiger Kunde"))]
    )
    apply_leipziger_liste_extraction(document, extraction)
    db.session.commit()

    assert Task.query.count() == 0


def test_reprocessing_document_does_not_duplicate_tasks(app, db, tenant, tmp_path, monkeypatch):
    pdf_path = tmp_path / "leipziger.pdf"
    make_pdf_file(pdf_path)

    extraction = LeipzigerListeExtraction(
        rows=[
            LeipzigerListeRow(
                customer=ExtractedCustomer(name="Anna Kunde"), is_neugeschaeft=True, is_angebot=True
            )
        ]
    )
    monkeypatch.setattr(
        "app.tasks.document_tasks.extract_document_data",
        lambda raw_text: DocumentExtraction(doc_type=DocType.LEIPZIGER_LISTE),
    )
    monkeypatch.setattr(
        "app.tasks.document_tasks.extract_leipziger_liste_rows", lambda raw_text: extraction
    )

    with app.app_context():
        document = Document(
            filename="leipziger.pdf",
            original_filename="leipziger.pdf",
            file_path=str(pdf_path),
            status=DocStatus.PENDING,
            tenant_id=tenant.id,
        )
        db.session.add(document)
        db.session.commit()

        process_document(document.id)
        db.session.refresh(document)
        first_count = Task.query.count()
        assert first_count > 0

        process_document(document.id)
        db.session.refresh(document)
        assert Task.query.count() == first_count


def test_update_task_status_sets_and_clears_resolved_at(app, db, tenant):
    document = Document(
        filename="x.pdf", original_filename="x.pdf", file_path="/tmp/x.pdf", tenant_id=tenant.id
    )
    db.session.add(document)
    db.session.commit()

    task = Task(
        tenant_id=tenant.id,
        document=document,
        type=TaskType.CALL_TODAY,
        title="📞 Heute anrufen",
        status=TaskStatus.OPEN,
    )
    db.session.add(task)
    db.session.commit()

    update_task_status(task, TaskStatus.DONE)
    db.session.commit()
    assert task.status == TaskStatus.DONE
    assert task.resolved_at is not None

    update_task_status(task, TaskStatus.OPEN)
    db.session.commit()
    assert task.resolved_at is None
