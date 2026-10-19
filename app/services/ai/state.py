"""Kurzlebiger Zustand des Routers: Abkuehlzeiten nach Fehlern und Antwort-Cache.

Liegt im bereits laufenden Redis (Schluessel-Praefix "zentriq-ai:"), damit beide Gunicorn-
Worker denselben Stand sehen. Ist Redis nicht erreichbar oder ZENTRIQ_AI_STATE_BACKEND=memory
(Tests), wird ein prozesslokaler Speicher genutzt - der Assistent funktioniert weiter, nur
ohne geteilten Stand."""

from __future__ import annotations

import json
import time

import redis
from flask import current_app

PREFIX = "zentriq-ai:"
_memory: dict[str, tuple[float, str]] = {}


def _redis():
    if current_app.config.get("ZENTRIQ_AI_STATE_BACKEND") == "memory":
        return None
    client = current_app.extensions.get("zentriq_ai_redis")
    if client is None:
        client = redis.Redis.from_url(
            current_app.config["CELERY_BROKER_URL"], socket_timeout=0.5, socket_connect_timeout=0.5
        )
        current_app.extensions["zentriq_ai_redis"] = client
    return client


def _set(key: str, value: str, ttl: float) -> None:
    ttl = max(1, int(ttl))
    client = _redis()
    if client is not None:
        try:
            client.set(PREFIX + key, value, ex=ttl)
            return
        except redis.RedisError:
            pass
    _memory[key] = (time.monotonic() + ttl, value)


def _get(key: str) -> str | None:
    client = _redis()
    if client is not None:
        try:
            raw = client.get(PREFIX + key)
            return raw.decode() if raw is not None else None
        except redis.RedisError:
            pass
    entry = _memory.get(key)
    if entry is None:
        return None
    if entry[0] < time.monotonic():
        _memory.pop(key, None)
        return None
    return entry[1]


def _ttl(key: str) -> int:
    client = _redis()
    if client is not None:
        try:
            return max(0, int(client.ttl(PREFIX + key)))
        except redis.RedisError:
            pass
    entry = _memory.get(key)
    return max(0, int(entry[0] - time.monotonic())) if entry else 0


# --- Abkuehlzeiten -------------------------------------------------------------------------


def cool_down(scope: str, seconds: float, reason: str) -> None:
    """scope: "p:<anbieter>" (ganzer Anbieter) oder "m:<anbieter>:<modell>"."""
    _set(f"cool:{scope}", reason, seconds)


def cooling_reason(provider: str, model_id: str) -> str | None:
    return _get(f"cool:p:{provider}") or _get(f"cool:m:{provider}:{model_id}")


def cooldown_info(provider: str, model_id: str) -> dict | None:
    for scope in (f"p:{provider}", f"m:{provider}:{model_id}"):
        reason = _get(f"cool:{scope}")
        if reason:
            return {"reason": reason, "seconds": _ttl(f"cool:{scope}")}
    return None


# --- Antwort-Cache -------------------------------------------------------------------------


def cache_get(key: str) -> dict | None:
    raw = _get(f"cache:{key}")
    if not raw:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return None


def cache_set(key: str, value: dict, ttl: int) -> None:
    if ttl > 0:
        _set(f"cache:{key}", json.dumps(value), ttl)


def reset_memory() -> None:
    _memory.clear()
