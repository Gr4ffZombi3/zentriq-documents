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

from app.extensions import db
from app.models import Customer, CustomerContact
from app.models.ai import AiModel
from app.models.audit_log import AuditEventType
from app.services import customer_notes
from app.services.ai import actions, catalog, chat, router, state, tools
from app.services.audit import log_audit_event
from app.tenancy import get_or_404_scoped

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


def _greeting() -> str:
    """Begruessung mit Vornamen aus dem Mitarbeiterprofil - nie aus der E-Mail-Adresse."""
    profile = None if current_user.is_super_admin else current_user.employee_profile
    name = (profile.display_name or "").strip() if profile is not None else ""
    first = name.split()[0] if name and "@" not in name else ""
    return f"Womit kann ich helfen, {first}?" if first else "Womit kann ich helfen?"


@zentriq_bp.get("")
@zentriq_bp.get("/c/<int:conversation_id>")
@login_required
def index(conversation_id=None):
    return render_template(
        "zentriq/index.html",
        conversation_id=conversation_id,
        greeting=_greeting(),
        max_chars=int(current_app.config.get("ZENTRIQ_AI_MAX_INPUT_CHARS") or 12000),
        tool_labels=[t.label for t in tools.available_for(current_user) if not t.write],
        customer_notes_enabled=bool(current_user.is_office_admin or current_user.is_employee),
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
    messages = conversation.messages
    by_message = chat.actions_for(messages)
    return jsonify(
        {
            "conversation": chat.serialize_conversation(conversation),
            "messages": [chat.serialize_message(m, labels, by_message.get(m.id)) for m in messages],
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
        client_ref = request.form.get("client_ref")
        files = request.files.getlist("files")
    else:
        data = _payload()
        content = data.get("content") if isinstance(data.get("content"), str) else ""
        model = data.get("model") if isinstance(data.get("model"), str) else None
        client_ref = data.get("client_ref") if isinstance(data.get("client_ref"), str) else None
        files = []
    try:
        conversation = chat.get_conversation(current_user, conversation_id)
        user_message, pending = chat.submit_message(current_user, conversation, content, model, files, client_ref)
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
    message_actions = chat.actions_for([message]).get(message.id)
    return jsonify(
        {"message": chat.serialize_message(message, _labels(), message_actions), "conversation": chat.serialize_conversation(conversation)}
    )


@zentriq_bp.post("/api/actions/<int:action_id>/<decision>")
@login_required
def api_action(action_id, decision):
    """Vorbereitete Aktion bestaetigen oder abbrechen - nur durch den Benutzer, der sie angefordert hat."""
    if decision not in ("bestaetigen", "abbrechen"):
        return _error("Unbekannte Entscheidung.", 400, "invalid")
    try:
        action = actions.confirm(current_user, action_id) if decision == "bestaetigen" else actions.cancel(current_user, action_id)
    except actions.ActionError as exc:
        return _error(str(exc), exc.status, "action")
    return jsonify({"action": actions.serialize(action)})


@zentriq_bp.get("/api/usage")
@login_required
def api_usage():
    data = {"own": router.usage_overview(30, user_id=current_user.id)}
    if current_user.is_super_admin:
        budget = router.budget_status()
        data["budget"] = {key: float(value) if isinstance(value, Decimal) else value for key, value in budget.items()}
    return jsonify(data)


# --- Kundennotizen und offene Rueckrufe -------------------------------------------------------
# Gemeinsame Notizen des Bueros: Buero-Admins und Mitarbeiter desselben Bueros lesen und
# ergaenzen sie (nur diese Endpunkte, keine weiteren Kundenmodule). Der SUPER_ADMIN hat wie
# ueberall keinen Zugriff auf Buerodaten. Kunden und Vorgaenge werden ausschliesslich im
# Mandanten des Benutzers gesucht (globaler Tenant-Filter + get_or_404_scoped).


def _office_user_required():
    if not (current_user.is_office_admin or current_user.is_employee):
        return _error("Kundennotizen stehen nur Büro-Konten zur Verfügung.", 403, "forbidden")
    return None


def _client_ref(data: dict) -> str | None:
    value = data.get("client_ref")
    return value.strip()[:64] or None if isinstance(value, str) else None


@zentriq_bp.post("/api/kunden/eingabe")
@login_required
def api_customer_input():
    if (denied := _office_user_required()) is not None:
        return denied
    data = _payload()
    text = data.get("text") if isinstance(data.get("text"), str) else ""
    mode = data.get("mode") if data.get("mode") in ("note", "search", "create") else None
    try:
        result = customer_notes.handle_input(
            current_user,
            text,
            selected_id=data.get("selected_id"),
            customer_id=data.get("customer_id"),
            client_ref=_client_ref(data),
            mode=mode,
            contact_choice=data.get("contact_id"),
            new_name=data.get("new_name") if isinstance(data.get("new_name"), str) else None,
            new_vn=data.get("new_vn") if isinstance(data.get("new_vn"), str) else None,
            create_new=data.get("create_new") is True,
        )
        if result.get("kind") == "chat" and not router.can_answer():
            # Ohne KI-Modell nicht an den Chat weiterreichen (der koennte nur einen Fehler melden).
            result = customer_notes.without_ai(current_user, text)
    except customer_notes.NoteError as exc:
        return _error(str(exc), exc.status, "note")
    except Exception:  # noqa: BLE001 - technischer Fehler wird als solcher gemeldet, nie als "kein Treffer".
        current_app.logger.exception("customer_notes.input_failed user=%s", current_user.id)
        db.session.rollback()
        return _error(
            "Technischer Fehler bei der Suche bzw. Speicherung. Bitte erneut versuchen – das Ergebnis ist nicht bestätigt.",
            500,
            "technical",
        )
    return jsonify(result)


@zentriq_bp.get("/api/kunden/<int:customer_id>")
@login_required
def api_customer_card(customer_id):
    if (denied := _office_user_required()) is not None:
        return denied
    customer = get_or_404_scoped(Customer, customer_id)
    return jsonify({"card": customer_notes.customer_card(current_user, customer)})


@zentriq_bp.post("/api/kunden/<int:customer_id>/nicht-erreicht")
@login_required
def api_customer_not_reached(customer_id):
    if (denied := _office_user_required()) is not None:
        return denied
    customer = get_or_404_scoped(Customer, customer_id)
    data = _payload()
    try:
        result = customer_notes.mark_not_reached(
            current_user, customer, contact_id=data.get("contact_id"), note_id=data.get("note_id"), client_ref=_client_ref(data)
        )
    except customer_notes.NoteError as exc:
        return _error(str(exc), exc.status, "note")
    return jsonify(result)


@zentriq_bp.post("/api/rueckrufe/<int:contact_id>/erledigt")
@login_required
def api_callback_done(contact_id):
    if (denied := _office_user_required()) is not None:
        return denied
    contact = get_or_404_scoped(CustomerContact, contact_id)
    return jsonify(customer_notes.close_contact(current_user, contact, client_ref=_client_ref(_payload())))


@zentriq_bp.get("/api/rueckrufe")
@login_required
def api_callbacks():
    if (denied := _office_user_required()) is not None:
        return denied
    return jsonify({"callbacks": customer_notes.open_callbacks()})
