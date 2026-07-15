from app.models import CustomerTimelineEvent, Document
from app.models.enums import Priority, TaskStatus, TimelineEventType
from app.services.documents import apply_leipziger_liste_extraction
from app.services.llm.schemas import ExtractedCustomer, LeipzigerListeExtraction, LeipzigerListeRow
from app.services.tasks import update_task_status


def test_leipziger_liste_flags_create_matching_timeline_events(app, db, tenant):
    document = Document(
        filename="liste.pdf", original_filename="liste.pdf", file_path="/tmp/liste.pdf", tenant_id=tenant.id
    )
    db.session.add(document)
    db.session.commit()

    extraction = LeipzigerListeExtraction(
        rows=[
            LeipzigerListeRow(
                customer=ExtractedCustomer(name="Voll-Flag Kunde"),
                is_neugeschaeft=True,
                is_fahrzeugwechsel=True,
                is_angebot=True,
                is_storno=True,
                priority=Priority.HIGH,
            )
        ]
    )
    apply_leipziger_liste_extraction(document, extraction)
    db.session.commit()

    events = CustomerTimelineEvent.query.all()
    event_types = {e.event_type for e in events}
    assert TimelineEventType.DOCUMENT_UPLOADED in event_types
    assert TimelineEventType.OFFER_DETECTED in event_types
    assert TimelineEventType.NEW_CONTRACT_DETECTED in event_types
    assert TimelineEventType.VEHICLE_CHANGE_DETECTED in event_types
    assert TimelineEventType.STORNO_DETECTED in event_types
    # TASK_CREATED fuer die aus is_neugeschaeft entstandene Recommendation->Task-Kette,
    # plus die beiden Angebots-Folgeaufgaben (fehlende Unterlagen, Abschluss).
    assert TimelineEventType.TASK_CREATED in event_types

    document_derived_types = {
        TimelineEventType.DOCUMENT_UPLOADED,
        TimelineEventType.OFFER_DETECTED,
        TimelineEventType.NEW_CONTRACT_DETECTED,
        TimelineEventType.VEHICLE_CHANGE_DETECTED,
        TimelineEventType.STORNO_DETECTED,
    }
    for event in events:
        assert event.customer.name == "Voll-Flag Kunde"
        if event.event_type in document_derived_types:
            assert event.occurred_at == document.uploaded_at


def test_task_status_change_logs_timeline_event(app, db, tenant):
    document = Document(
        filename="liste.pdf", original_filename="liste.pdf", file_path="/tmp/liste.pdf", tenant_id=tenant.id
    )
    db.session.add(document)
    db.session.commit()

    extraction = LeipzigerListeExtraction(
        rows=[LeipzigerListeRow(customer=ExtractedCustomer(name="Status Kunde"), is_neugeschaeft=True)]
    )
    apply_leipziger_liste_extraction(document, extraction)
    db.session.commit()

    from app.models import Task

    task = Task.query.first()
    events_before = CustomerTimelineEvent.query.filter_by(
        event_type=TimelineEventType.TASK_STATUS_CHANGED
    ).count()

    update_task_status(task, TaskStatus.DONE)
    db.session.commit()

    events_after = CustomerTimelineEvent.query.filter_by(
        event_type=TimelineEventType.TASK_STATUS_CHANGED
    ).count()
    assert events_after == events_before + 1
