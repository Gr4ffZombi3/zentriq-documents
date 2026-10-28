"""Anbieter-Katalog fuer ZentriqAI.

Ein neuer OpenAI-kompatibler Anbieter ist ein weiterer Eintrag in PROVIDERS plus ein
API-Key in der .env - kein weiterer Code. Modelle stehen in der Tabelle ai_models: wenige
Startwerte (DEFAULT_MODELS) werden beim ersten Aufruf angelegt, weitere ruft der SUPER_ADMIN
unter ZentriqAI -> Einstellungen direkt beim Anbieter ab (/v1/models) und schaltet sie frei.

Kostenlose Kontingente sind begrenzt (Anfragen pro Minute/Tag) und koennen sich jederzeit
aendern - der Router behandelt sie deshalb als "verfuegbar, solange der Anbieter nicht mit
429 antwortet", nie als unbegrenzt.
"""

from __future__ import annotations

from dataclasses import dataclass

from flask import current_app
from sqlalchemy.exc import IntegrityError

from app.extensions import db
from app.models.ai import AiModel, AiProviderSetting


@dataclass(frozen=True)
class Provider:
    key: str
    label: str
    kind: str  # "openai" (OpenAI-kompatibel) | "anthropic"
    key_setting: str  # Name der Konfiguration/Umgebungsvariable mit dem API-Key
    base_url: str | None
    # Kostenloses Kontingent: neue/abgerufene Modelle gelten als kostenlos.
    free_tier: bool
    # Nur Modelle mit diesem Suffix gelten beim Abruf als kostenlos (OpenRouter: ":free").
    free_suffix: str | None = None
    note: str = ""


PROVIDERS: dict[str, Provider] = {
    p.key: p
    for p in (
        Provider(
            "gemini", "Google Gemini (AI Studio)", "openai", "GEMINI_API_KEY",
            "https://generativelanguage.googleapis.com/v1beta/openai/", True,
            note="Kostenloses Kontingent mit Tageslimit. EWR: es gelten die Datenregeln der kostenpflichtigen Dienste.",
        ),
        Provider(
            "groq", "Groq", "openai", "GROQ_API_KEY", "https://api.groq.com/openai/v1", True,
            note="Kostenloses Kontingent, Anbieter in den USA.",
        ),
        Provider(
            "cerebras", "Cerebras", "openai", "CEREBRAS_API_KEY", "https://api.cerebras.ai/v1", True,
            note="Kostenloses Kontingent, Anbieter in den USA.",
        ),
        Provider(
            "mistral", "Mistral AI", "openai", "MISTRAL_API_KEY", "https://api.mistral.ai/v1", True,
            note="EU-Anbieter. Im Gratis-Plan werden Eingaben ggf. zum Training genutzt (Opt-out in der Admin-Konsole).",
        ),
        Provider(
            "openrouter", "OpenRouter", "openai", "OPENROUTER_API_KEY", "https://openrouter.ai/api/v1", False,
            free_suffix=":free",
            note="Vermittler zu vielen Anbietern; nur Modelle mit ':free' sind kostenlos.",
        ),
        Provider(
            "omniroute", "OmniRoute (eigenes Gateway)", "openai", "OMNIROUTE_API_KEY", None, False,
            note="OpenAI-kompatibles Gateway (OMNIROUTE_BASE_URL). Derzeit nicht installiert (Arbeitsspeicher). "
            "Nur API-Key-Anbieter anbinden, keine privaten Abo-Logins. Kostenlose Modelle unter Modelle als kostenlos markieren.",
        ),
        Provider(
            "anthropic", "Anthropic (Claude)", "anthropic", "ANTHROPIC_API_KEY", None, False,
            note="Kostenpflichtig; offizielle API mit kommerziellen Bedingungen.",
        ),
        Provider(
            "openai", "OpenAI", "openai", "OPENAI_API_KEY", None, False,
            note="Kostenpflichtig. Preise je Modell unter Einstellungen eintragen.",
        ),
    )
}

# (provider, model_id, label, enabled, priority, is_free, input_price, output_price, tier)
# Preise in USD je 1 Mio. Tokens. Kostenpflichtige Modelle sind standardmaessig aus.
# Stufe: 1 = schnell/klein (einfache Anfragen), 2 = stark, 3 = sehr stark.
DEFAULT_MODELS = (
    ("gemini", "gemini-flash-latest", "Gemini Flash", True, 10, True, None, None, 2),
    ("gemini", "gemini-flash-lite-latest", "Gemini Flash-Lite", True, 20, True, None, None, 1),
    ("groq", "llama-3.3-70b-versatile", "Llama 3.3 70B (Groq)", True, 30, True, None, None, 2),
    ("mistral", "mistral-small-latest", "Mistral Small", True, 40, True, None, None, 1),
    ("mistral", "mistral-medium-latest", "Mistral Medium", True, 50, True, None, None, 2),
    ("anthropic", "claude-haiku-4-5", "Claude Haiku 4.5", False, 200, False, 1, 5, 1),
    ("anthropic", "claude-sonnet-5-5", "Claude Sonnet 5.5", False, 210, False, 2, 10, 2),
    ("anthropic", "claude-opus-5-5", "Claude Opus 5.5", False, 220, False, 4, 20, 3),
    # OpenAI: der Zugang (OPENAI_API_KEY) wird bereits fuer die Dokumentauswertung genutzt. Das
    # kleine Modell ist freigegeben; Kosten entstehen erst, wenn eine Kostengrenze > 0 gesetzt ist.
    ("openai", "gpt-4o-mini", "GPT-4o mini", True, 300, False, 0.15, 0.6, 1),
    ("openai", "gpt-4o", "GPT-4o", False, 310, False, 2.5, 10, 2),
)


def api_key(provider: Provider) -> str | None:
    return current_app.config.get(provider.key_setting) or None


def base_url(provider: Provider) -> str | None:
    if provider.key == "openai":
        # Bestehende globale Einstellung der Anwendung (app/services/llm/client.py).
        return current_app.config.get("OPENAI_BASE_URL") or None
    if provider.key == "omniroute":
        return current_app.config.get("OMNIROUTE_BASE_URL") or None
    return provider.base_url


def is_configured(provider_key: str) -> bool:
    provider = PROVIDERS.get(provider_key)
    if provider is None or not api_key(provider):
        return False
    # OmniRoute braucht zusaetzlich die Adresse des Gateways.
    return provider.key != "omniroute" or bool(base_url(provider))


def ensure_defaults() -> None:
    """Legt fehlende Startmodelle und Anbieter-Einstellungen an (idempotent)."""
    existing = {(m.provider, m.model_id) for m in AiModel.query.all()}
    known_providers = {s.provider for s in AiProviderSetting.query.all()}
    changed = False
    for key in PROVIDERS:
        if key not in known_providers:
            db.session.add(AiProviderSetting(provider=key))
            changed = True
    for provider, model_id, label, enabled, priority, is_free, price_in, price_out, tier in DEFAULT_MODELS:
        if (provider, model_id) not in existing:
            db.session.add(
                AiModel(
                    provider=provider, model_id=model_id, label=label, enabled=enabled, priority=priority,
                    is_free=is_free, input_price=price_in, output_price=price_out, tier=tier,
                )
            )
            changed = True
    if changed:
        try:
            db.session.commit()
        except IntegrityError:
            # Paralleler Request hat dieselben Zeilen angelegt.
            db.session.rollback()


def provider_settings() -> dict[str, AiProviderSetting]:
    return {s.provider: s for s in AiProviderSetting.query.all()}


def add_discovered_models(provider_key: str, model_ids: list[str]) -> int:
    """Uebernimmt abgerufene Modelle (deaktiviert) in den Katalog. Gibt die Anzahl neuer zurueck."""
    provider = PROVIDERS[provider_key]
    existing = {m.model_id for m in AiModel.query.filter_by(provider=provider_key).all()}
    added = 0
    for model_id in sorted(set(model_ids) - existing):
        if not model_id or len(model_id) > 150:
            continue
        is_free = provider.free_tier or bool(provider.free_suffix and model_id.endswith(provider.free_suffix))
        db.session.add(
            AiModel(provider=provider_key, model_id=model_id, label=model_id[:150], enabled=False, priority=100, is_free=is_free)
        )
        added += 1
    db.session.commit()
    return added
