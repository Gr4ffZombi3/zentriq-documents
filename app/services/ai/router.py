"""Modell-Routing fuer ZentriqAI: Auswahl, automatisches Ausweichen, Kostengrenzen, Cache.

Reihenfolge der automatischen Auswahl: freigeschaltete kostenlose Modelle (nach Prioritaet),
danach kostenpflichtige Modelle mit hinterlegtem Preis (nach Prioritaet, dann Preis) - letztere
nur, solange Tages- und Monatsbudget nicht ausgeschoepft sind. Bei manueller Auswahl wird das
gewaehlte Modell zuerst versucht; faellt es aus, weicht der Router (abschaltbar ueber
ZENTRIQ_AI_MANUAL_FALLBACK) auf die automatische Reihenfolge aus und meldet das.

Nach Fehlern wird ein Modell bzw. Anbieter fuer eine Weile uebersprungen (state.cool_down),
damit nicht jede Anfrage erneut in dasselbe Limit laeuft. Bei einer Sicherheitsablehnung wird
bewusst NICHT auf einen anderen Anbieter ausgewichen.

Jeder Anbieter-Aufruf wird ohne Inhalte in ai_usage verbucht (Tokens, Kosten, Fehlerart)."""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from flask import current_app
from sqlalchemy import func, select

from app.extensions import db
from app.models.ai import AiModel, AiUsage
from app.services.ai import adapters, state
from app.services.ai.catalog import PROVIDERS, ensure_defaults, is_configured, provider_settings
from app.tenancy import bypass_tenant_scope

AUTO = "auto"
MIN_ATTEMPT_SECONDS = 4.0

# Abkuehlzeiten in Sekunden je Fehlerart; None = keine Sperre.
COOLDOWNS = {
    adapters.RATE_LIMIT: ("m", 60),
    adapters.AUTH: ("p", 3600),
    adapters.NOT_FOUND: ("m", 3600),
    adapters.UNAVAILABLE: ("p", 30),
    adapters.TIMEOUT: ("p", 30),
    adapters.EMPTY: ("m", 30),
    adapters.BAD_REQUEST: None,
    adapters.REFUSAL: None,
}

ERROR_LABELS = {
    adapters.RATE_LIMIT: "Limit erreicht",
    adapters.AUTH: "Zugang ungültig",
    adapters.NOT_FOUND: "Modell nicht gefunden",
    adapters.UNAVAILABLE: "nicht erreichbar",
    adapters.TIMEOUT: "Zeitüberschreitung",
    adapters.EMPTY: "leere Antwort",
    adapters.BAD_REQUEST: "Anfrage abgelehnt",
    "budget": "Kostengrenze erreicht",
    "cooldown": "pausiert",
}


class AiError(Exception):
    def __init__(self, message: str, status: int, error_type: str, attempts: list | None = None):
        super().__init__(message)
        self.status = status
        self.error_type = error_type
        self.attempts = attempts or []


@dataclass
class Completion:
    text: str
    provider: str
    model: str
    model_label: str
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: Decimal = Decimal("0")
    cached: bool = False
    fallback_used: bool = False
    attempts: list = field(default_factory=list)


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def model_cost(model: AiModel, input_tokens: int, output_tokens: int) -> Decimal:
    if model.is_free or model.input_price is None or model.output_price is None:
        return Decimal("0")
    cost = (Decimal(input_tokens) * Decimal(model.input_price) + Decimal(output_tokens) * Decimal(model.output_price)) / Decimal(1_000_000)
    return cost.quantize(Decimal("0.000001"))


# --- Budget ---------------------------------------------------------------------------------


def spend_since(since: datetime) -> Decimal:
    """Plattformweite Ausgaben (alle Bueros) seit `since` - fuer die Kostengrenzen."""
    with bypass_tenant_scope():
        total = db.session.execute(select(func.coalesce(func.sum(AiUsage.cost_usd), 0)).where(AiUsage.created_at >= since)).scalar()
    return Decimal(str(total or 0))


def budget_status() -> dict:
    now = _now()
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    month_start = day_start.replace(day=1)
    daily_limit = Decimal(str(current_app.config.get("ZENTRIQ_AI_DAILY_BUDGET_USD") or 0))
    monthly_limit = Decimal(str(current_app.config.get("ZENTRIQ_AI_MONTHLY_BUDGET_USD") or 0))
    today = spend_since(day_start)
    month = spend_since(month_start)
    return {
        "today": today,
        "month": month,
        "daily_limit": daily_limit,
        "monthly_limit": monthly_limit,
        "paid_allowed": today < daily_limit and month < monthly_limit,
    }


# --- Auswahl --------------------------------------------------------------------------------


def available_models(*, customer_data: bool = False) -> list[AiModel]:
    """Freigeschaltete Modelle konfigurierter, aktiver Anbieter in Auto-Reihenfolge."""
    ensure_defaults()
    settings = provider_settings()
    models = []
    for model in AiModel.query.filter_by(enabled=True).all():
        setting = settings.get(model.provider)
        if model.provider not in PROVIDERS or not is_configured(model.provider):
            continue
        if setting is not None and not setting.enabled:
            continue
        if customer_data and not (setting is not None and setting.customer_data_allowed):
            continue
        if not model.has_price:
            continue
        models.append(model)
    return sorted(
        models,
        key=lambda m: (not m.is_free, m.priority, Decimal(m.input_price or 0) + Decimal(m.output_price or 0), m.id),
    )


def resolve_choice(choice: str | None, *, customer_data: bool) -> tuple[list[AiModel], AiModel | None]:
    """Kandidatenliste fuer eine Anfrage und ggf. das ausdruecklich gewaehlte Modell."""
    candidates = available_models(customer_data=customer_data)
    if not choice or choice == AUTO:
        return candidates, None
    chosen = next((m for m in candidates if m.ref == choice), None)
    if chosen is None:
        raise AiError(
            "Das gewählte Modell ist nicht verfügbar (deaktiviert, ohne Zugang oder nicht für Kundendaten freigegeben).",
            400,
            "model_unavailable",
        )
    rest = [m for m in candidates if m.id != chosen.id] if current_app.config.get("ZENTRIQ_AI_MANUAL_FALLBACK", True) else []
    return [chosen, *rest], chosen


def _cache_key(user_id: int, choice: str, customer_data: bool, system: str, messages: list[dict]) -> str:
    payload = json.dumps([user_id, choice, customer_data, system, messages], ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()


def _record(model: AiModel, *, user_id, tenant_id, conversation_id, purpose, input_tokens=0, output_tokens=0, cost=Decimal("0"), ok=True, error_type=None, latency_ms=None) -> None:
    db.session.add(
        AiUsage(
            tenant_id=tenant_id,
            user_id=user_id,
            conversation_id=conversation_id,
            provider=model.provider,
            model=model.model_id,
            purpose=purpose,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=cost,
            ok=ok,
            error_type=error_type,
            latency_ms=latency_ms,
        )
    )
    db.session.commit()


def complete(
    *,
    system: str,
    messages: list[dict],
    user_id: int,
    tenant_id: int,
    choice: str | None = AUTO,
    customer_data: bool = False,
    conversation_id: int | None = None,
    purpose: str = "chat",
    max_tokens: int | None = None,
) -> Completion:
    choice = choice or AUTO
    candidates, chosen = resolve_choice(choice, customer_data=customer_data)
    if not candidates:
        message = (
            "Kein für Kundendaten freigegebenes Modell verfügbar."
            if customer_data
            else "Derzeit ist kein KI-Modell verfügbar. Bitte Anbieter und Modelle unter Einstellungen prüfen."
        )
        raise AiError(message, 503, "no_models")

    cache_ttl = int(current_app.config.get("ZENTRIQ_AI_CACHE_TTL_SECONDS") or 0)
    cache_key = _cache_key(user_id, choice, customer_data, system, messages) if purpose == "chat" and cache_ttl else None
    if cache_key:
        hit = state.cache_get(cache_key)
        if hit:
            return Completion(text=hit["text"], provider=hit["provider"], model=hit["model"], model_label=hit["label"], cached=True)

    max_tokens = max_tokens or int(current_app.config.get("ZENTRIQ_AI_MAX_OUTPUT_TOKENS") or 2000)
    deadline = time.monotonic() + float(current_app.config.get("ZENTRIQ_AI_TIMEOUT_SECONDS") or 50)
    per_call = float(current_app.config.get("ZENTRIQ_AI_CALL_TIMEOUT_SECONDS") or 25)
    paid_allowed = None
    attempts: list[dict] = []

    for model in candidates:
        cooling = state.cooling_reason(model.provider, model.model_id)
        if cooling:
            attempts.append({"model": model.label, "error": "cooldown"})
            continue
        if not model.is_free:
            if paid_allowed is None:
                paid_allowed = budget_status()["paid_allowed"]
            if not paid_allowed:
                attempts.append({"model": model.label, "error": "budget"})
                continue
        remaining = deadline - time.monotonic()
        if remaining < MIN_ATTEMPT_SECONDS:
            break

        started = time.monotonic()
        try:
            result = adapters.call(model.provider, model.model_id, system, messages, max_tokens, min(per_call, remaining))
        except adapters.ProviderError as exc:
            latency = int((time.monotonic() - started) * 1000)
            _record(model, user_id=user_id, tenant_id=tenant_id, conversation_id=conversation_id, purpose=purpose, ok=False, error_type=exc.kind, latency_ms=latency)
            current_app.logger.warning("zentriq_ai.provider_failed provider=%s model=%s kind=%s", model.provider, model.model_id, exc.kind)
            attempts.append({"model": model.label, "error": exc.kind})
            rule = COOLDOWNS.get(exc.kind)
            if rule:
                scope, seconds = rule
                if exc.retry_after:
                    seconds = min(max(seconds, exc.retry_after), 3600)
                key = f"p:{model.provider}" if scope == "p" else f"m:{model.provider}:{model.model_id}"
                state.cool_down(key, seconds, exc.kind)
            if exc.kind == adapters.REFUSAL:
                raise AiError("Zu dieser Anfrage kann ZentriqAI keine Antwort geben.", 422, "refusal", attempts) from exc
            continue

        latency = int((time.monotonic() - started) * 1000)
        cost = model_cost(model, result.input_tokens, result.output_tokens)
        _record(
            model, user_id=user_id, tenant_id=tenant_id, conversation_id=conversation_id, purpose=purpose,
            input_tokens=result.input_tokens, output_tokens=result.output_tokens, cost=cost, latency_ms=latency,
        )
        completion = Completion(
            text=result.text,
            provider=model.provider,
            model=model.model_id,
            model_label=model.label,
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            cost_usd=cost,
            fallback_used=chosen is not None and model.id != chosen.id,
            attempts=attempts,
        )
        if cache_key:
            state.cache_set(cache_key, {"text": result.text, "provider": model.provider, "model": model.model_id, "label": model.label}, cache_ttl)
        return completion

    if attempts and all(a["error"] == "budget" for a in attempts):
        raise AiError("Die Kostengrenze für kostenpflichtige Modelle ist erreicht.", 429, "budget", attempts)
    raise AiError(
        "Kein KI-Modell konnte die Anfrage gerade beantworten. Bitte in einer Minute erneut versuchen.",
        503,
        "all_failed",
        attempts,
    )


def describe_attempts(attempts: list[dict]) -> list[str]:
    return [f"{a['model']}: {ERROR_LABELS.get(a['error'], a['error'])}" for a in attempts]


def usage_overview(days: int = 30, *, user_id: int | None = None) -> dict:
    """Verbrauch je Anbieter/Modell (ohne Inhalte)."""
    since = _now() - timedelta(days=days)
    query = (
        select(
            AiUsage.provider,
            AiUsage.model,
            func.count(AiUsage.id),
            func.sum(func.cast(AiUsage.ok, db.Integer)),
            func.coalesce(func.sum(AiUsage.input_tokens), 0),
            func.coalesce(func.sum(AiUsage.output_tokens), 0),
            func.coalesce(func.sum(AiUsage.cost_usd), 0),
        )
        .where(AiUsage.created_at >= since)
        .group_by(AiUsage.provider, AiUsage.model)
        .order_by(func.count(AiUsage.id).desc())
    )
    if user_id is not None:
        query = query.where(AiUsage.user_id == user_id)
    with bypass_tenant_scope():
        rows = db.session.execute(query).all()
    labels = {(m.provider, m.model_id): m.label for m in AiModel.query.all()}
    return {
        "days": days,
        "rows": [
            {
                "provider": provider,
                "provider_label": PROVIDERS[provider].label if provider in PROVIDERS else provider,
                "model": labels.get((provider, model), model),
                "requests": int(count or 0),
                "errors": int(count or 0) - int(ok or 0),
                "input_tokens": int(tokens_in or 0),
                "output_tokens": int(tokens_out or 0),
                "cost_usd": float(cost or 0),
            }
            for provider, model, count, ok, tokens_in, tokens_out, cost in rows
        ],
    }
