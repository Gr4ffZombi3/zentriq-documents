"""Einheitlicher Aufruf der Anbieter. Zwei Adapter decken alle Anbieter ab:

- OpenAI-kompatibel (Gemini, Groq, Cerebras, Mistral, OpenRouter, OpenAI) ueber das bereits
  installierte `openai`-SDK mit eigener base_url,
- Anthropic ueber das offizielle `anthropic`-SDK.

Fehler werden auf wenige Arten abgebildet (ProviderError.kind), nach denen der Router
entscheidet, ob er auf ein anderes Modell ausweicht und wie lange er den Anbieter meidet.
Keine automatischen SDK-Wiederholungen (max_retries=0): der Router steuert das Ausweichen
selbst und muss unter dem nginx-Timeout (60 s) bleiben."""

from __future__ import annotations

import json
from dataclasses import dataclass, field

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
class ToolCall:
    id: str
    name: str
    arguments: dict


@dataclass
class ProviderResult:
    text: str
    input_tokens: int
    output_tokens: int
    tool_calls: list[ToolCall] = field(default_factory=list)


# Neutrales Nachrichtenformat (unabhaengig vom Anbieter, damit der Router auch mitten in einem
# Werkzeug-Ablauf auf einen anderen Anbieter ausweichen kann):
#   {"role": "user", "content": str}
#   {"role": "assistant", "content": str, "tool_calls": [ToolCall-Dict ...]}
#   {"role": "tool", "tool_call_id": str, "name": str, "content": str}
# Werkzeuge: [{"name": str, "description": str, "parameters": JSON-Schema}]


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


def _openai_messages(system: str, messages: list[dict]) -> list[dict]:
    out = [{"role": "system", "content": system}]
    for m in messages:
        if m["role"] == "tool":
            out.append({"role": "tool", "tool_call_id": m["tool_call_id"], "content": m["content"]})
        elif m["role"] == "assistant" and m.get("tool_calls"):
            out.append(
                {
                    "role": "assistant",
                    "content": m.get("content") or None,
                    "tool_calls": [
                        {"id": c["id"], "type": "function", "function": {"name": c["name"], "arguments": json.dumps(c["arguments"], ensure_ascii=False)}}
                        for c in m["tool_calls"]
                    ],
                }
            )
        else:
            out.append({"role": m["role"], "content": m["content"]})
    return out


def _anthropic_messages(messages: list[dict]) -> list[dict]:
    out: list[dict] = []
    for m in messages:
        if m["role"] == "tool":
            block = {"type": "tool_result", "tool_use_id": m["tool_call_id"], "content": m["content"]}
            # Mehrere Werkzeugergebnisse gehoeren in EINE Benutzernachricht.
            if out and out[-1]["role"] == "user" and isinstance(out[-1]["content"], list):
                out[-1]["content"].append(block)
            else:
                out.append({"role": "user", "content": [block]})
        elif m["role"] == "assistant" and m.get("tool_calls"):
            blocks = [{"type": "text", "text": m["content"]}] if m.get("content") else []
            blocks += [{"type": "tool_use", "id": c["id"], "name": c["name"], "input": c["arguments"]} for c in m["tool_calls"]]
            out.append({"role": "assistant", "content": blocks})
        else:
            out.append({"role": m["role"], "content": m["content"]})
    return out


def _parse_arguments(raw) -> dict:
    if isinstance(raw, dict):
        return raw
    try:
        value = json.loads(raw or "{}")
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def _call_openai(provider, model_id, system, messages, max_tokens, timeout, tools) -> ProviderResult:
    client = _openai_client(provider, timeout)
    token_param = "max_completion_tokens" if provider.key == "openai" else "max_tokens"
    kwargs = {token_param: max_tokens}
    if tools:
        kwargs["tools"] = [{"type": "function", "function": t} for t in tools]
        kwargs["tool_choice"] = "auto"
    try:
        response = client.chat.completions.create(model=model_id, messages=_openai_messages(system, messages), **kwargs)
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
    message = choice.message if choice else None
    text = ((message.content if message else None) or "").strip()
    calls = [
        ToolCall(id=c.id or f"call_{i}", name=c.function.name, arguments=_parse_arguments(c.function.arguments))
        for i, c in enumerate((message.tool_calls if message else None) or [])
        if getattr(c, "function", None)
    ]
    if not text and not calls:
        raise ProviderError(EMPTY)
    usage = response.usage
    return ProviderResult(
        text=text,
        input_tokens=int(getattr(usage, "prompt_tokens", 0) or 0),
        output_tokens=int(getattr(usage, "completion_tokens", 0) or 0),
        tool_calls=calls,
    )


def _call_anthropic(provider, model_id, system, messages, max_tokens, timeout, tools) -> ProviderResult:
    client = _anthropic_client(provider, timeout)
    kwargs = {"model": model_id, "max_tokens": max_tokens, "system": system, "messages": _anthropic_messages(messages)}
    if tools:
        kwargs["tools"] = [{"name": t["name"], "description": t["description"], "input_schema": t["parameters"]} for t in tools]
    try:
        if model_id.startswith("claude-haiku"):
            # Haiku 4.5 kennt weder effort noch den serverseitigen Fallback.
            response = client.messages.create(**kwargs)
        else:
            response = client.beta.messages.create(
                **kwargs,
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
    calls = [
        ToolCall(id=block.id, name=block.name, arguments=_parse_arguments(block.input))
        for block in response.content
        if block.type == "tool_use"
    ]
    if not text and not calls:
        raise ProviderError(EMPTY)
    return ProviderResult(
        text=text,
        input_tokens=int(getattr(response.usage, "input_tokens", 0) or 0),
        output_tokens=int(getattr(response.usage, "output_tokens", 0) or 0),
        tool_calls=calls,
    )


def call(
    provider_key: str,
    model_id: str,
    system: str,
    messages: list[dict],
    max_tokens: int,
    timeout: float,
    tools: list[dict] | None = None,
) -> ProviderResult:
    provider = PROVIDERS[provider_key]
    if not api_key(provider):
        raise ProviderError(AUTH)
    if provider.kind == "anthropic":
        return _call_anthropic(provider, model_id, system, messages, max_tokens, timeout, tools)
    return _call_openai(provider, model_id, system, messages, max_tokens, timeout, tools)


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
