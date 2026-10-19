"""ZentriqAI - KI-Einstellungen (Anbieter, Modelle, Kostengrenzen, Verbrauch).

Ausschliesslich SUPER_ADMIN: Blueprint steht in SUPER_ADMIN_ONLY_BLUEPRINTS
(app/auth/permissions.py) und jede Route traegt zusaetzlich @super_admin_required. API-Schluessel
bleiben in der Server-Umgebung; angezeigt wird nur, OB ein Anbieter konfiguriert ist."""

from decimal import Decimal, InvalidOperation

from flask import Blueprint, flash, redirect, render_template, request, url_for
from flask_login import current_user

from app.auth.permissions import super_admin_required
from app.extensions import db
from app.models.ai import AiModel
from app.models.audit_log import AuditEventType
from app.services.ai import adapters, catalog, router, state
from app.services.audit import log_audit_event

zentriq_admin_bp = Blueprint("zentriq_admin", __name__, url_prefix="/zentriq/einstellungen")


# --- Einstellungen (Anbieter, Modelle, Verbrauch) ------------------------------------------


@zentriq_admin_bp.get("")
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


@zentriq_admin_bp.post("/anbieter/<provider_key>")
@super_admin_required
def settings_provider(provider_key):
    setting = catalog.provider_settings().get(provider_key)
    if provider_key not in catalog.PROVIDERS or setting is None:
        return redirect(url_for("zentriq_admin.settings"))
    setting.enabled = request.form.get("enabled") == "1"
    setting.customer_data_allowed = request.form.get("customer_data_allowed") == "1"
    db.session.commit()
    _audit_settings({"provider": provider_key, "enabled": setting.enabled, "customer_data_allowed": setting.customer_data_allowed})
    flash("Anbieter gespeichert.", "success")
    return redirect(url_for("zentriq_admin.settings") + f"#anbieter-{provider_key}")


def _price(value: str | None) -> Decimal | None:
    value = (value or "").strip().replace(",", ".")
    if not value:
        return None
    try:
        price = Decimal(value)
    except InvalidOperation:
        return None
    return price if price >= 0 else None


@zentriq_admin_bp.post("/modelle/<int:model_id>")
@super_admin_required
def settings_model(model_id):
    model = db.session.get(AiModel, model_id)
    if model is None:
        return redirect(url_for("zentriq_admin.settings"))
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
    return redirect(url_for("zentriq_admin.settings") + f"#anbieter-{model.provider}")


@zentriq_admin_bp.post("/anbieter/<provider_key>/abrufen")
@super_admin_required
def settings_discover(provider_key):
    if provider_key not in catalog.PROVIDERS:
        return redirect(url_for("zentriq_admin.settings"))
    try:
        ids = adapters.list_models(provider_key)
    except adapters.ProviderError as exc:
        message = "Kein gültiger API-Schlüssel hinterlegt." if exc.kind == adapters.AUTH else "Anbieter nicht erreichbar."
        flash(f"Modelle konnten nicht abgerufen werden: {message}", "error")
        return redirect(url_for("zentriq_admin.settings") + f"#anbieter-{provider_key}")
    added = catalog.add_discovered_models(provider_key, ids)
    _audit_settings({"provider": provider_key, "discovered": added})
    flash(f"{len(ids)} Modelle gefunden, {added} neu übernommen (deaktiviert).", "success")
    return redirect(url_for("zentriq_admin.settings") + f"#anbieter-{provider_key}")
