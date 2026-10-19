"""Von ZentriqAI vorbereitete schreibende Aktionen - Ausfuehrung nur nach Bestaetigung.

Ablauf: Das Modell ruft ein schreibendes Werkzeug auf -> es entsteht eine AiAction (pending) mit
geprueften Parametern und einer lesbaren Beschreibung. Im Chat erscheint "Bestätigen /
Abbrechen". Erst die Bestaetigung durch DENSELBEN Benutzer fuehrt die Aktion aus:
- Rechte werden erneut geprueft (Werkzeug-Endpunkt, Rolle, Mandant),
- ausgefuehrt wird die BESTEHENDE Geschaeftslogik (z. B. services/tasks.update_task_status),
- genau einmal: Status pending -> done/failed in derselben Transaktion; erneutes Bestaetigen
  (Doppelklick, Wiederholung) aendert nichts mehr,
- Audit-Eintrag ohne Inhalte.
Eine fehlgeschlagene KI-Anfrage legt hoechstens eine pending-Aktion an und veraendert keine Daten.
"""

from __future__ import annotations

from app.extensions import db
from app.models.ai import AiAction, _now
from app.models.audit_log import AuditEventType
from app.services.ai import tools
from app.services.audit import log_audit_event


class ActionError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def prepare(user, message_id: int | None, tool: str, arguments: dict, summary: str) -> AiAction:
    # Dieselbe Aktion nicht doppelt vorschlagen (z. B. zweimal im selben Ablauf angefordert).
    for existing in AiAction.query.filter(AiAction.user_id == user.id, AiAction.tool == tool, AiAction.status == "pending").all():
        if existing.arguments == arguments:
            return existing
    action = AiAction(
        tenant_id=user.tenant_id, user_id=user.id, message_id=message_id, tool=tool,
        arguments=arguments, summary=summary[:300], status="pending",
    )
    db.session.add(action)
    db.session.commit()
    return action


def _get_own(user, action_id: int) -> AiAction:
    action = AiAction.query.filter(AiAction.id == action_id, AiAction.user_id == user.id).first()
    if action is None:
        raise ActionError("Aktion nicht gefunden.", 404)
    return action


def _task_done(user, arguments: dict) -> str:
    from app.models import Task
    from app.models.enums import TaskStatus
    from app.services.tasks import update_task_status

    task = Task.query.filter(Task.id == int(arguments["aufgabe_id"])).first()
    if task is None:
        raise ActionError("Die Aufgabe existiert nicht mehr.", 409)
    if task.status != TaskStatus.OPEN:
        return "Die Aufgabe war bereits erledigt."
    update_task_status(task, TaskStatus.DONE)
    return f"Aufgabe „{task.title}“ ist als erledigt markiert."


EXECUTORS = {"aufgabe_erledigen": _task_done}


def confirm(user, action_id: int) -> AiAction:
    action = _get_own(user, action_id)
    if action.status != "pending":
        return action  # bereits entschieden: nichts erneut ausfuehren
    tool = tools.TOOLS.get(action.tool)
    executor = EXECUTORS.get(action.tool)
    if tool is None or executor is None or not tool.write or not tools.is_allowed(user, tool):
        raise ActionError("Für diese Aktion fehlt die Berechtigung.", 403)
    # Sperre gegen gleichzeitiges Bestaetigen: nur ein Request setzt pending -> running.
    claimed = AiAction.query.filter(AiAction.id == action.id, AiAction.status == "pending").update(
        {AiAction.status: "running"}, synchronize_session=False
    )
    db.session.commit()
    if not claimed:
        db.session.refresh(action)
        return action
    try:
        result = executor(user, action.arguments or {})
    except ActionError as exc:
        db.session.rollback()
        action.status, action.result = "failed", str(exc)[:300]
    except Exception:  # noqa: BLE001
        db.session.rollback()
        action.status, action.result = "failed", "Die Aktion konnte nicht ausgeführt werden."
    else:
        action.status, action.result = "done", result[:300]
    action.decided_at = _now()
    db.session.commit()
    log_audit_event(AuditEventType.AI_ACTION_EXECUTED, user=user, details={"action_id": action.id, "tool": action.tool, "status": action.status})
    return action


def cancel(user, action_id: int) -> AiAction:
    action = _get_own(user, action_id)
    if action.status == "pending":
        action.status, action.decided_at = "cancelled", _now()
        db.session.commit()
    return action


def serialize(action: AiAction) -> dict:
    return {"id": action.id, "summary": action.summary, "status": action.status, "result": action.result}
