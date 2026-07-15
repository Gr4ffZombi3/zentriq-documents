"""Erzeugt Task-Zeilen aus den bereits bestehenden Recommendation- und
Leipziger-Liste-Flag-Quellen. Die Recommendation-Engine selbst (build_recommendations/
create_recommendations) bleibt unveraendert - hier wird nur ein bereits erzeugter
KI-Vorschlag zu einem faelligkeitsdatierten, zuweisbaren Task "promoted"."""

from datetime import date, datetime, timedelta, timezone

from app.extensions import db
from app.models import Customer, Document, Recommendation, Task
from app.models.enums import RecommendationType, TaskStatus, TaskType, TimelineEventType, WiedervorlageReason
from app.services.timeline import log_timeline_event

TASK_TYPE_LABELS: dict[TaskType, str] = {
    TaskType.CALL_TODAY: "📞 Heute anrufen",
    TaskType.FOLLOW_UP_OFFER: "📧 Angebot nachfassen",
    TaskType.REQUEST_DOCUMENTS: "📄 Fehlende Unterlagen anfordern",
    TaskType.PREPARE_CONTRACT: "📝 Vertrag vorbereiten",
    TaskType.CHECK_CLOSURE: "✅ Abschluss kontrollieren",
    TaskType.SCHEDULE_APPOINTMENT: "📅 Termin vereinbaren",
    TaskType.OTHER: "Aufgabe",
}

TASK_STATUS_LABELS: dict[TaskStatus, str] = {
    TaskStatus.OPEN: "Offen",
    TaskStatus.DONE: "Erledigt",
    TaskStatus.DISMISSED: "Verworfen",
}

RECOMMENDATION_TYPE_TO_TASK_TYPE: dict[RecommendationType, TaskType] = {
    RecommendationType.CALL_TODAY: TaskType.CALL_TODAY,
    RecommendationType.PRIORITIZE_VEHICLE_CHANGE: TaskType.PREPARE_CONTRACT,
    RecommendationType.OFFER_LEGAL_PROTECTION: TaskType.FOLLOW_UP_OFFER,
    RecommendationType.OFFER_HOUSEHOLD_INSURANCE: TaskType.FOLLOW_UP_OFFER,
    RecommendationType.CHECK_ACCIDENT_INSURANCE: TaskType.FOLLOW_UP_OFFER,
    RecommendationType.CHECK_SUPPLEMENTARY_HEALTH: TaskType.FOLLOW_UP_OFFER,
    RecommendationType.OTHER: TaskType.OTHER,
}

FLAG_TASK_WIEDERVORLAGE_REASON: dict[TaskType, WiedervorlageReason] = {
    TaskType.REQUEST_DOCUMENTS: WiedervorlageReason.MISSING_DOCUMENTS,
    TaskType.CHECK_CLOSURE: WiedervorlageReason.OPEN_CLOSURE,
}

DUE_OFFSET_DAYS: dict[TaskType, int] = {
    TaskType.CALL_TODAY: 0,
    TaskType.PREPARE_CONTRACT: 1,
    TaskType.SCHEDULE_APPOINTMENT: 2,
    TaskType.FOLLOW_UP_OFFER: 3,
    TaskType.OTHER: 3,
    TaskType.REQUEST_DOCUMENTS: 5,
    TaskType.CHECK_CLOSURE: 14,
}


def _due_date(task_type: TaskType) -> date:
    offset = DUE_OFFSET_DAYS.get(task_type, 3)
    return (datetime.now(timezone.utc) + timedelta(days=offset)).date()


def _assigned_user_id(document: Document, customer: Customer | None) -> int | None:
    if customer is not None and customer.assigned_user_id is not None:
        return customer.assigned_user_id
    return document.uploaded_by_user_id


def create_tasks_from_recommendations(
    document: Document, customer: Customer | None, recommendations: list[Recommendation]
) -> list[Task]:
    assigned_user_id = _assigned_user_id(document, customer)
    created = []
    for recommendation in recommendations:
        task_type = RECOMMENDATION_TYPE_TO_TASK_TYPE.get(recommendation.type, TaskType.OTHER)
        task = Task(
            tenant_id=document.tenant_id,
            document=document,
            customer=customer,
            recommendation=recommendation,
            assigned_user_id=assigned_user_id,
            type=task_type,
            title=TASK_TYPE_LABELS.get(task_type, recommendation.label),
            priority=recommendation.priority,
            status=TaskStatus.OPEN,
            due_date=_due_date(task_type),
        )
        db.session.add(task)
        if customer is not None:
            log_timeline_event(
                customer, TimelineEventType.TASK_CREATED, f"Aufgabe erstellt: {task.title}", task=task
            )
        created.append(task)
    return created


def create_flag_based_tasks(document: Document, customer: Customer | None, row) -> list[Task]:
    """`row` ist eine LeipzigerListeRow. Bei einem offenen Angebot entstehen automatisch
    Folgeaufgaben (fehlende Unterlagen anfordern, Abschluss kontrollieren)."""
    if not row.is_angebot:
        return []

    assigned_user_id = _assigned_user_id(document, customer)
    created = []
    for task_type in (TaskType.REQUEST_DOCUMENTS, TaskType.CHECK_CLOSURE):
        task = Task(
            tenant_id=document.tenant_id,
            document=document,
            customer=customer,
            assigned_user_id=assigned_user_id,
            type=task_type,
            wiedervorlage_reason=FLAG_TASK_WIEDERVORLAGE_REASON.get(task_type),
            title=TASK_TYPE_LABELS[task_type],
            priority=row.priority,
            status=TaskStatus.OPEN,
            due_date=_due_date(task_type),
        )
        db.session.add(task)
        if customer is not None:
            log_timeline_event(
                customer, TimelineEventType.TASK_CREATED, f"Aufgabe erstellt: {task.title}", task=task
            )
        created.append(task)
    return created


def update_task_status(task: Task, new_status: TaskStatus) -> Task:
    task.status = new_status
    task.resolved_at = datetime.now(timezone.utc) if new_status != TaskStatus.OPEN else None
    if task.customer is not None:
        log_timeline_event(
            task.customer,
            TimelineEventType.TASK_STATUS_CHANGED,
            f"Aufgabe aktualisiert: {task.title} → {TASK_STATUS_LABELS[new_status]}",
            task=task,
        )
    return task
