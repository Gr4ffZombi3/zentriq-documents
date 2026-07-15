"""Datenquelle fuer 'Mein Bestand' - filtert ausschliesslich ueber Customer.assigned_user_id
(gesetzt beim ersten Upload, siehe find_or_create_customer), nicht ueber Tenant-weite Daten."""

from app.models import Customer, Task
from app.models.enums import TaskStatus
from app.services.kpis import get_sales_kpis


def get_bestand(user_id: int) -> dict:
    customers = Customer.query.filter_by(assigned_user_id=user_id).order_by(Customer.name.asc()).all()
    open_tasks = (
        Task.query.filter(Task.assigned_user_id == user_id, Task.status == TaskStatus.OPEN)
        .order_by(Task.due_date.asc())
        .all()
    )
    wiedervorlagen = [t for t in open_tasks if t.wiedervorlage_reason is not None]

    return {
        "customers": customers,
        "open_tasks": open_tasks,
        "wiedervorlagen": wiedervorlagen,
        "kpis": get_sales_kpis(user_id=user_id),
    }
