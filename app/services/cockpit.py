"""Datenquelle fuer das Tagescockpit - personenbezogen (auf current_user gescoped, wie
'Mein Bestand'), reine Abfragefunktionen ohne neuen State."""

from datetime import datetime, timedelta, timezone

from app.models import Document, Task
from app.models.enums import PRIORITY_ORDER, TaskStatus, TaskType
from app.services.kpis import get_sales_kpis


def get_daily_cockpit(user_id: int) -> dict:
    open_tasks_query = Task.query.filter(Task.assigned_user_id == user_id, Task.status == TaskStatus.OPEN)
    open_tasks = open_tasks_query.all()

    # Enum-Spalten sortieren in SQL alphabetisch nach Wert, nicht nach Dringlichkeit -
    # daher wie in app/services/stats.py in Python ueber PRIORITY_ORDER sortieren.
    top_priority_task = (
        max(open_tasks, key=lambda t: (PRIORITY_ORDER[t.priority], t.due_date is None)) if open_tasks else None
    )

    today = datetime.now(timezone.utc).date()
    call_today_tasks = [
        t for t in open_tasks if t.type == TaskType.CALL_TODAY and t.due_date is not None and t.due_date <= today
    ]

    overdue_tasks_count = sum(1 for t in open_tasks if t.due_date is not None and t.due_date < today)

    since = datetime.now(timezone.utc) - timedelta(hours=24)
    new_documents_count = Document.query.filter(
        Document.uploaded_by_user_id == user_id, Document.uploaded_at >= since
    ).count()

    kpis = get_sales_kpis(user_id=user_id)
    top_potential = kpis["top_customers"][0] if kpis["top_customers"] else None

    return {
        "top_priority_task": top_priority_task,
        "call_today_tasks": call_today_tasks,
        "top_potential": top_potential,
        "overdue_tasks_count": overdue_tasks_count,
        "new_documents_count": new_documents_count,
        "abschlussquote_percent": kpis["abschlussquote_percent"],
    }
