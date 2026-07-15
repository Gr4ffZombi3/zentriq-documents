from datetime import date

from flask import Blueprint, redirect, render_template, request, url_for
from flask_login import login_required

from app.extensions import db
from app.models import Task
from app.models.enums import PRIORITY_ORDER, TaskStatus
from app.services.tasks import update_task_status
from app.tenancy import get_or_404_scoped

tasks_bp = Blueprint("tasks", __name__, url_prefix="/tasks")


@tasks_bp.route("")
@login_required
def list_tasks():
    status_filter = request.args.get("status", "open")
    query = Task.query
    if status_filter != "all":
        try:
            query = query.filter_by(status=TaskStatus(status_filter))
        except ValueError:
            status_filter = "open"
            query = Task.query.filter_by(status=TaskStatus.OPEN)

    # Enum-Spalten sortieren in SQL alphabetisch nach Wert, nicht nach Dringlichkeit -
    # daher wie in app/services/stats.py in Python ueber PRIORITY_ORDER sortieren.
    tasks = query.all()
    tasks.sort(key=lambda t: (-PRIORITY_ORDER[t.priority], t.due_date or date.max))
    return render_template("tasks/list.html", tasks=tasks, status_filter=status_filter)


@tasks_bp.route("/<int:task_id>/status", methods=["POST"])
@login_required
def update_status(task_id):
    task = get_or_404_scoped(Task, task_id)
    try:
        status_enum = TaskStatus(request.form.get("status", ""))
    except ValueError:
        return redirect(url_for("tasks.list_tasks"))

    update_task_status(task, status_enum)
    db.session.commit()

    if request.headers.get("HX-Request"):
        return render_template("tasks/_task_row.html", task=task)
    return redirect(url_for("tasks.list_tasks"))
