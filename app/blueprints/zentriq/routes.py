"""ZentriqAI - zentraler KI-Assistent und Startseite fuer alle Rollen.

Zugriff: jeder angemeldete Benutzer (Rollen-Hook in app/auth/permissions.py), Gespraeche sind je
Benutzer und Mandant getrennt (app/services/ai/chat.py). Werkzeuge mit Buerodaten pruefen die
bestehenden Rechte je Benutzer (app/services/ai/tools.py). KI-Einstellungen liegen getrennt im
Blueprint "zentriq_admin" (nur SUPER_ADMIN).

KI-Antworten entstehen asynchron im Celery-Worker: POST .../messages legt die Nachricht und einen
Platzhalter an (202), der Browser fragt GET .../messages/<id> ab."""

from decimal import Decimal

from flask import Blueprint, current_app, jsonify, render_template, request
from flask_login import current_user, login_required

from app.models.ai import AiModel
from app.models.audit_log import AuditEventType
from app.services.ai import catalog, chat, router, state, tools
from app.services.audit import log_audit_event

zentriq_bp = Blueprint("zentriq", __name__, url_prefix="/zentriq")


def _payload() -> dict:
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else {}


def _error(message: str, status: int, code: str, **extra):
    return jsonify({"error": message, "code": code, **extra}), status


@zentriq_bp.before_request
def _enabled():
    if not current_app.config.get("ZENTRIQ_AI_ENABLED", True):
        if request.path.startswith("/zentriq/api/"):
            return _error("ZentriqAI ist derzeit abgeschaltet.", 503, "disabled")
        return render_template("errors/error.html", title="ZentriqAI", message="ZentriqAI ist derzeit abgeschaltet."), 503
    return None


def _labels() -> dict:
    return {(m.provider, m.model_id): m.label for m in AiModel.query.all()}


def _model_options() -> list[dict]:
    settings = catalog.provider_settings()
    options = []
    for model in router.available_models():
        setting = settings.get(model.provider)
        options.append(
            {
                "ref": model.ref,
                "label": model.label,
                "provider": catalog.PROVIDERS[model.provider].label,
                "free": model.is_free,
                "customer_data": bool(setting and setting.customer_data_allowed),
                "paused": bool(state.cooldown_info(model.provider, model.model_id)),
            }
        )
    return options


# --- Seite ---------------------------------------------------------------------------------


@zentriq_bp.get("")
@zentriq_bp.get("/c/<int:conversation_id>")
@login_required
def index(conversation_id=None):
    return render_template(
        "zentriq/index.html",
        conversation_id=conversation_id,
        max_chars=int(current_app.config.get("ZENTRIQ_AI_MAX_INPUT_CHARS") or 12000),
        # Nur anzeigen, was tatsaechlich nutzbar ist: Werkzeuge brauchen einen fuer Kundendaten
        # freigegebenen Anbieter.
        tool_labels=[t.label for t in tools.available_for(current_user)] if router.available_models(customer_data=True) else [],
    )


# --- JSON-API ------------------------------------------------------------------------------


@zentriq_bp.get("/api/models")
@login_required
def api_models():
    return jsonify({"models": _model_options()})


@zentriq_bp.get("/api/conversations")
@login_required
def api_conversations():
    try:
        limit = max(1, min(100, int(request.args.get("limit") or 100)))
    except ValueError:
        limit = 100
    pinned = {"1": True, "0": False}.get(request.args.get("pinned") or "")
    items = chat.list_conversations(current_user, request.args.get("q"), limit=limit, pinned=pinned)
    return jsonify({"conversations": [chat.serialize_conversation(c) for c in items]})


@zentriq_bp.get("/api/projects")
@login_required
def api_projects():
    return jsonify({"projects": chat.projects(current_user)})


@zentriq_bp.post("/api/conversations")
@login_required
def api_create_conversation():
    data = _payload()
    conversation = chat.create_conversation(
        current_user,
        model_choice=str(data.get("model") or router.AUTO),
        customer_data=bool(data.get("customer_data")),
        project=data.get("project"),
    )
    return jsonify({"conversation": chat.serialize_conversation(conversation)}), 201


@zentriq_bp.get("/api/conversations/<int:conversation_id>")
@login_required
def api_conversation(conversation_id):
    try:
        conversation = chat.get_conversation(current_user, conversation_id)
    except chat.ChatError as exc:
        return _error(str(exc), exc.status, exc.error_type)
    labels = _labels()
    return jsonify(
        {
            "conversation": chat.serialize_conversation(conversation),
            "messages": [chat.serialize_message(m, labels) for m in conversation.messages],
        }
    )


@zentriq_bp.patch("/api/conversations/<int:conversation_id>")
@login_required
def api_update_conversation(conversation_id):
    data = _payload()
    try:
        conversation = chat.get_conversation(current_user, conversation_id)
        chat.update_conversation(
            conversation,
            title=data.get("title") if "title" in data else None,
            model_choice=data.get("model") if "model" in data else None,
            customer_data=data.get("customer_data") if "customer_data" in data else None,
            pinned=data.get("pinned") if "pinned" in data else None,
            project=(data.get("project") or "") if "project" in data else None,
        )
    except chat.ChatError as exc:
        return _error(str(exc), exc.status, exc.error_type)
    return jsonify({"conversation": chat.serialize_conversation(conversation)})


@zentriq_bp.delete("/api/conversations/<int:conversation_id>")
@login_required
def api_delete_conversation(conversation_id):
    try:
        conversation = chat.get_conversation(current_user, conversation_id)
    except chat.ChatError as exc:
        return _error(str(exc), exc.status, exc.error_type)
    chat.delete_conversation(conversation)
    log_audit_event(AuditEventType.AI_CONVERSATION_DELETED, user=current_user, details={"conversation_id": conversation_id})
    return jsonify({"ok": True})


@zentriq_bp.delete("/api/conversations")
@login_required
def api_delete_all():
    count = chat.delete_all(current_user)
    log_audit_event(AuditEventType.AI_CONVERSATION_DELETED, user=current_user, details={"all": True, "count": count})
    return jsonify({"ok": True, "deleted": count})


@zentriq_bp.post("/api/conversations/<int:conversation_id>/messages")
@login_required
def api_send(conversation_id):
    if request.mimetype == "multipart/form-data":
        content = request.form.get("content") or ""
        model = request.form.get("model")
        files = request.files.getlist("files")
    else:
        data = _payload()
        content = data.get("content") if isinstance(data.get("content"), str) else ""
        model = data.get("model") if isinstance(data.get("model"), str) else None
        files = []
    try:
        conversation = chat.get_conversation(current_user, conversation_id)
        user_message, pending = chat.submit_message(current_user, conversation, content, model, files)
    except chat.ChatError as exc:
        return _error(str(exc), exc.status, exc.error_type)
    labels = _labels()
    return (
        jsonify(
            {
                "message": chat.serialize_message(user_message, labels),
                "reply": chat.serialize_message(pending, labels),
                "conversation": chat.serialize_conversation(conversation),
            }
        ),
        202,
    )


@zentriq_bp.get("/api/conversations/<int:conversation_id>/messages/<int:message_id>")
@login_required
def api_message(conversation_id, message_id):
    try:
        conversation = chat.get_conversation(current_user, conversation_id)
        message = chat.get_message(current_user, conversation, message_id)
    except chat.ChatError as exc:
        return _error(str(exc), exc.status, exc.error_type)
    return jsonify({"message": chat.serialize_message(message, _labels()), "conversation": chat.serialize_conversation(conversation)})


@zentriq_bp.get("/api/usage")
@login_required
def api_usage():
    data = {"own": router.usage_overview(30, user_id=current_user.id)}
    if current_user.is_super_admin:
        budget = router.budget_status()
        data["budget"] = {key: float(value) if isinstance(value, Decimal) else value for key, value in budget.items()}
    return jsonify(data)
