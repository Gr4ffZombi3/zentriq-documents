"""ZentriqAI - KI-Assistent (Phase 5: zunaechst nur SUPER_ADMIN).

Zugriff serverseitig doppelt abgesichert: app/auth/permissions.py (Blueprint "zentriq" nur
fuer SUPER_ADMIN, fuer alle anderen Rollen 403) und @super_admin_required an jeder Route.
Gespraeche sind je Benutzer und Mandant getrennt (app/services/ai/chat.py). API-Schluessel
bleiben in der Server-Umgebung; die Seite zeigt nur, OB ein Anbieter konfiguriert ist."""

from decimal import Decimal, InvalidOperation

from flask import Blueprint, current_app, flash, jsonify, redirect, render_template, request, url_for
from flask_login import current_user

from app.auth.permissions import super_admin_required
from app.extensions import db
from app.models.ai import AiModel
from app.models.audit_log import AuditEventType
from app.services.ai import adapters, catalog, chat, router, state
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


def _model_options() -> list[dict]:
    options = []
    settings = catalog.provider_settings()
    for model in router.available_models():
        cooldown = state.cooldown_info(model.provider, model.model_id)
        options.append(
            {
                "ref": model.ref,
                "label": model.label,
                "provider": catalog.PROVIDERS[model.provider].label,
                "free": model.is_free,
                "customer_data": bool(settings.get(model.provider) and settings[model.provider].customer_data_allowed),
                "paused": bool(cooldown),
            }
        )
    return options


def _labels() -> dict:
    return {(m.provider, m.model_id): m.label for m in AiModel.query.all()}


# --- Seite ---------------------------------------------------------------------------------


@zentriq_bp.get("")
@super_admin_required
def index():
    return render_template(
        "zentriq/index.html",
        max_chars=int(current_app.config.get("ZENTRIQ_AI_MAX_INPUT_CHARS") or 12000),
    )


# --- JSON-API ------------------------------------------------------------------------------


@zentriq_bp.get("/api/models")
@super_admin_required
def api_models():
    return jsonify({"models": _model_options()})


@zentriq_bp.get("/api/conversations")
@super_admin_required
def api_conversations():
    items = chat.list_conversations(current_user, request.args.get("q"))
    return jsonify({"conversations": [chat.serialize_conversation(c) for c in items]})


@zentriq_bp.post("/api/conversations")
@super_admin_required
def api_create_conversation():
    data = _payload()
    conversation = chat.create_conversation(
        current_user, model_choice=str(data.get("model") or router.AUTO), customer_data=bool(data.get("customer_data"))
    )
    return jsonify({"conversation": chat.serialize_conversation(conversation)}), 201


@zentriq_bp.get("/api/conversations/<int:conversation_id>")
@super_admin_required
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
@super_admin_required
def api_update_conversation(conversation_id):
    data = _payload()
    try:
        conversation = chat.get_conversation(current_user, conversation_id)
        chat.update_conversation(
            conversation,
            title=data.get("title") if "title" in data else None,
            model_choice=data.get("model") if "model" in data else None,
            customer_data=data.get("customer_data") if "customer_data" in data else None,
        )
    except chat.ChatError as exc:
        return _error(str(exc), exc.status, exc.error_type)
    return jsonify({"conversation": chat.serialize_conversation(conversation)})


@zentriq_bp.delete("/api/conversations/<int:conversation_id>")
@super_admin_required
def api_delete_conversation(conversation_id):
    try:
        conversation = chat.get_conversation(current_user, conversation_id)
    except chat.ChatError as exc:
        return _error(str(exc), exc.status, exc.error_type)
    chat.delete_conversation(conversation)
    log_audit_event(AuditEventType.AI_CONVERSATION_DELETED, user=current_user, details={"conversation_id": conversation_id})
    return jsonify({"ok": True})


@zentriq_bp.delete("/api/conversations")
@super_admin_required
def api_delete_all():
    count = chat.delete_all(current_user)
    log_audit_event(AuditEventType.AI_CONVERSATION_DELETED, user=current_user, details={"all": True, "count": count})
    return jsonify({"ok": True, "deleted": count})


@zentriq_bp.post("/api/conversations/<int:conversation_id>/messages")
@super_admin_required
def api_send(conversation_id):
    data = _payload()
    content = data.get("content") if isinstance(data.get("content"), str) else ""
    model = data.get("model") if isinstance(data.get("model"), str) else None
    try:
        conversation = chat.get_conversation(current_user, conversation_id)
        reply, completion = chat.send_message(current_user, conversation, content, model)
    except chat.ChatError as exc:
        return _error(str(exc), exc.status, exc.error_type)
    except router.AiError as exc:
        current_app.logger.warning("zentriq_ai.failed type=%s", exc.error_type)
        return _error(str(exc), exc.status, exc.error_type, attempts=router.describe_attempts(exc.attempts))
    return jsonify(
        {
            "message": chat.serialize_message(reply, _labels()),
            "conversation": chat.serialize_conversation(conversation),
            "fallback_used": completion.fallback_used,
            "attempts": router.describe_attempts(completion.attempts),
        }
    )


@zentriq_bp.get("/api/usage")
@super_admin_required
def api_usage():
    budget = router.budget_status()
    return jsonify(
        {
            "budget": {key: float(value) if isinstance(value, Decimal) else value for key, value in budget.items()},
            "own": router.usage_overview(30, user_id=current_user.id),
        }
    )


# --- Einstellungen (Anbieter, Modelle, Verbrauch) ------------------------------------------


@zentriq_bp.get("/einstellungen")
@super_admin_required
def settings():
    catalog.ensure_defaults()
    provider_settings = catalog.provider_settings()
    models = AiModel.query.order_by(AiModel.provider, AiModel.priority, AiModel.model_id).all()
    providers = [
        {
            "key": p.key,
            "label": p.label,
            "note": p.note,
            "key_setting": p.key_setting,
            "configured": catalog.is_configured(p.key),
            "setting": provider_settings.get(p.key),
            "models": [m for m in models if m.provider == p.key],
        }
        for p in catalog.PROVIDERS.values()
    ]
    cooldowns = {m.id: state.cooldown_info(m.provider, m.model_id) for m in models}
    return render_template(
        "zentriq/settings.html",
        providers=providers,
        cooldowns=cooldowns,
        budget=router.budget_status(),
        usage=router.usage_overview(30),
    )


def _audit_settings(details: dict) -> None:
    log_audit_event(AuditEventType.AI_SETTINGS_CHANGED, user=current_user, details=details)


@zentriq_bp.post("/einstellungen/anbieter/<provider_key>")
@super_admin_required
def settings_provider(provider_key):
    setting = catalog.provider_settings().get(provider_key)
    if provider_key not in catalog.PROVIDERS or setting is None:
        return redirect(url_for("zentriq.settings"))
    setting.enabled = request.form.get("enabled") == "1"
    setting.customer_data_allowed = request.form.get("customer_data_allowed") == "1"
    db.session.commit()
    _audit_settings({"provider": provider_key, "enabled": setting.enabled, "customer_data_allowed": setting.customer_data_allowed})
    flash("Anbieter gespeichert.", "success")
    return redirect(url_for("zentriq.settings") + f"#anbieter-{provider_key}")


def _price(value: str | None) -> Decimal | None:
    value = (value or "").strip().replace(",", ".")
    if not value:
        return None
    try:
        price = Decimal(value)
    except InvalidOperation:
        return None
    return price if price >= 0 else None


@zentriq_bp.post("/einstellungen/modelle/<int:model_id>")
@super_admin_required
def settings_model(model_id):
    model = db.session.get(AiModel, model_id)
    if model is None:
        return redirect(url_for("zentriq.settings"))
    model.enabled = request.form.get("enabled") == "1"
    model.is_free = request.form.get("is_free") == "1"
    label = " ".join((request.form.get("label") or "").split())[:150]
    model.label = label or model.model_id[:150]
    try:
        model.priority = max(0, min(10000, int(request.form.get("priority") or model.priority)))
    except ValueError:
        pass
    model.input_price = _price(request.form.get("input_price"))
    model.output_price = _price(request.form.get("output_price"))
    db.session.commit()
    _audit_settings({"model": model.ref, "enabled": model.enabled, "is_free": model.is_free})
    if model.enabled and not model.has_price:
        flash("Gespeichert. Ohne Preis wird dieses kostenpflichtige Modell nicht verwendet.", "error")
    else:
        flash("Modell gespeichert.", "success")
    return redirect(url_for("zentriq.settings") + f"#anbieter-{model.provider}")


@zentriq_bp.post("/einstellungen/anbieter/<provider_key>/abrufen")
@super_admin_required
def settings_discover(provider_key):
    if provider_key not in catalog.PROVIDERS:
        return redirect(url_for("zentriq.settings"))
    try:
        ids = adapters.list_models(provider_key)
    except adapters.ProviderError as exc:
        message = "Kein gültiger API-Schlüssel hinterlegt." if exc.kind == adapters.AUTH else "Anbieter nicht erreichbar."
        flash(f"Modelle konnten nicht abgerufen werden: {message}", "error")
        return redirect(url_for("zentriq.settings") + f"#anbieter-{provider_key}")
    added = catalog.add_discovered_models(provider_key, ids)
    _audit_settings({"provider": provider_key, "discovered": added})
    flash(f"{len(ids)} Modelle gefunden, {added} neu übernommen (deaktiviert).", "success")
    return redirect(url_for("zentriq.settings") + f"#anbieter-{provider_key}")
