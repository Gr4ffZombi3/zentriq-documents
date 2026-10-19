"""Einheitlicher Aufruf der Anbieter. Zwei Adapter decken alle Anbieter ab:

- OpenAI-kompatibel (Gemini, Groq, Cerebras, Mistral, OpenRouter, OpenAI) ueber das bereits
  installierte `openai`-SDK mit eigener base_url,
- Anthropic ueber das offizielle `anthropic`-SDK.

Fehler werden auf wenige Arten abgebildet (ProviderError.kind), nach denen der Router
entscheidet, ob er auf ein anderes Modell ausweicht und wie lange er den Anbieter meidet.
Keine automatischen SDK-Wiederholungen (max_retries=0): der Router steuert das Ausweichen
selbst und muss unter dem nginx-Timeout (60 s) bleiben."""

from __future__ import annotations

from dataclasses import dataclass

import anthropic
import openai

from app.services.ai.catalog import PROVIDERS, api_key, base_url

# Fehlerarten
RATE_LIMIT = "rate_limit"  # 429 / Kontingent erschoepft -> anderes Modell, Abkuehlzeit
AUTH = "auth"  # Key ungueltig/fehlt -> Anbieter laenger meiden
NOT_FOUND = "not_found"  # Modell existiert nicht (mehr)
BAD_REQUEST = "bad_request"  # z. B. Kontext zu lang -> naechstes Modell, keine Sperre
UNAVAILABLE = "unavailable"  # 5xx, Verbindungsfehler
TIMEOUT = "timeout"
REFUSAL = "refusal"  # Sicherheitsablehnung -> kein Ausweichen auf andere Anbieter
EMPTY = "empty"


class ProviderError(Exception):
    def __init__(self, kind: str, retry_after: float | None = None, status: int | None = None):
        super().__init__(kind)
        self.kind = kind
        self.retry_after = retry_after
        self.status = status


@dataclass
class ProviderResult:
    text: str
    input_tokens: int
    output_tokens: int


def _retry_after(exc) -> float | None:
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None) or {}
    try:
        value = headers.get("retry-after")
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _openai_client(provider, timeout: float) -> openai.OpenAI:
    kwargs = {"api_key": api_key(provider), "timeout": timeout, "max_retries": 0}
    url = base_url(provider)
    if url:
        kwargs["base_url"] = url
    return openai.OpenAI(**kwargs)


def _anthropic_client(provider, timeout: float) -> anthropic.Anthropic:
    return anthropic.Anthropic(api_key=api_key(provider), timeout=timeout, max_retries=0)


def _call_openai(provider, model_id, system, messages, max_tokens, timeout) -> ProviderResult:
    client = _openai_client(provider, timeout)
    token_param = "max_completion_tokens" if provider.key == "openai" else "max_tokens"
    try:
        response = client.chat.completions.create(
            model=model_id,
            messages=[{"role": "system", "content": system}, *messages],
            **{token_param: max_tokens},
        )
    except openai.APITimeoutError as exc:
        raise ProviderError(TIMEOUT) from exc
    except openai.RateLimitError as exc:
        raise ProviderError(RATE_LIMIT, _retry_after(exc), 429) from exc
    except (openai.AuthenticationError, openai.PermissionDeniedError) as exc:
        raise ProviderError(AUTH, status=exc.status_code) from exc
    except openai.NotFoundError as exc:
        raise ProviderError(NOT_FOUND, status=404) from exc
    except (openai.BadRequestError, openai.UnprocessableEntityError) as exc:
        raise ProviderError(BAD_REQUEST, status=exc.status_code) from exc
    except openai.APIStatusError as exc:
        kind = RATE_LIMIT if exc.status_code == 429 else UNAVAILABLE
        raise ProviderError(kind, _retry_after(exc), exc.status_code) from exc
    except openai.APIConnectionError as exc:
        raise ProviderError(UNAVAILABLE) from exc

    choice = response.choices[0] if response.choices else None
    text = ((choice.message.content if choice and choice.message else None) or "").strip()
    if not text:
        raise ProviderError(EMPTY)
    usage = response.usage
    return ProviderResult(
        text=text,
        input_tokens=int(getattr(usage, "prompt_tokens", 0) or 0),
        output_tokens=int(getattr(usage, "completion_tokens", 0) or 0),
    )


def _call_anthropic(provider, model_id, system, messages, max_tokens, timeout) -> ProviderResult:
    client = _anthropic_client(provider, timeout)
    try:
        if model_id.startswith("claude-haiku"):
            # Haiku 4.5 kennt weder effort noch den serverseitigen Fallback.
            response = client.messages.create(model=model_id, max_tokens=max_tokens, system=system, messages=messages)
        else:
            response = client.beta.messages.create(
                model=model_id,
                max_tokens=max_tokens,
                system=system,
                messages=messages,
                output_config={"effort": "low"},
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
            )
    except anthropic.APITimeoutError as exc:
        raise ProviderError(TIMEOUT) from exc
    except anthropic.RateLimitError as exc:
        raise ProviderError(RATE_LIMIT, _retry_after(exc), 429) from exc
    except (anthropic.AuthenticationError, anthropic.PermissionDeniedError) as exc:
        raise ProviderError(AUTH, status=exc.status_code) from exc
    except anthropic.NotFoundError as exc:
        raise ProviderError(NOT_FOUND, status=404) from exc
    except anthropic.BadRequestError as exc:
        raise ProviderError(BAD_REQUEST, status=400) from exc
    except anthropic.APIStatusError as exc:
        kind = RATE_LIMIT if exc.status_code == 429 else UNAVAILABLE
        raise ProviderError(kind, _retry_after(exc), exc.status_code) from exc
    except anthropic.APIConnectionError as exc:
        raise ProviderError(UNAVAILABLE) from exc

    if response.stop_reason == "refusal":
        raise ProviderError(REFUSAL)
    text = "\n".join(block.text for block in response.content if block.type == "text").strip()
    if not text:
        raise ProviderError(EMPTY)
    return ProviderResult(
        text=text,
        input_tokens=int(getattr(response.usage, "input_tokens", 0) or 0),
        output_tokens=int(getattr(response.usage, "output_tokens", 0) or 0),
    )


def call(provider_key: str, model_id: str, system: str, messages: list[dict], max_tokens: int, timeout: float) -> ProviderResult:
    provider = PROVIDERS[provider_key]
    if not api_key(provider):
        raise ProviderError(AUTH)
    if provider.kind == "anthropic":
        return _call_anthropic(provider, model_id, system, messages, max_tokens, timeout)
    return _call_openai(provider, model_id, system, messages, max_tokens, timeout)


def list_models(provider_key: str, timeout: float = 15) -> list[str]:
    """Modell-IDs direkt beim Anbieter abrufen (Einstellungen -> Modelle abrufen)."""
    provider = PROVIDERS[provider_key]
    if not api_key(provider):
        raise ProviderError(AUTH)
    try:
        if provider.kind == "anthropic":
            return [m.id for m in _anthropic_client(provider, timeout).models.list()]
        return [m.id.removeprefix("models/") for m in _openai_client(provider, timeout).models.list()]
    except (openai.AuthenticationError, openai.PermissionDeniedError, anthropic.AuthenticationError, anthropic.PermissionDeniedError) as exc:
        raise ProviderError(AUTH) from exc
    except (openai.APIError, anthropic.APIError) as exc:
        raise ProviderError(UNAVAILABLE) from exc
