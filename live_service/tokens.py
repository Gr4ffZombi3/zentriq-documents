"""Gemeinsame Zugangstokens zwischen Flask (app/services/live.py) und dem Live-Dienst.

Flask prueft Anmeldung, Buero und Zugriff auf die Kundenakte und stellt dann ein kurzlebiges,
signiertes Token fuer GENAU einen Kanal aus. Der Live-Dienst kennt keine Datenbank - er vertraut
nur der Signatur (SECRET_KEY) und dem Ausstellungszeitpunkt."""

from __future__ import annotations

import re

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

SALT = "zentriq-live-v1"
# Nur zum Aufbau der Verbindung - die Verbindung selbst endet spaetestens nach MAX_STREAM_SECONDS
# (server.py) und holt sich dann ueber Flask ein neues Token (Abmeldung wirkt so zeitnah).
TOKEN_MAX_AGE = 120
CHANNEL = re.compile(r"^customer:[1-9][0-9]{0,11}$")


def _serializer(secret: str) -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(secret, salt=SALT)


def issue(secret: str, *, user_id: int, tenant_id: int, name: str, channel: str) -> str:
    if not CHANNEL.fullmatch(channel):
        raise ValueError("unbekannter Kanal")
    return _serializer(secret).dumps({"u": int(user_id), "t": int(tenant_id), "n": str(name)[:80], "c": channel})


def verify(secret: str, token: str, max_age: int = TOKEN_MAX_AGE) -> dict | None:
    """Payload {u, t, n, c} oder None (falsch signiert, abgelaufen, unbekannter Kanal)."""
    if not token or len(token) > 2048:
        return None
    try:
        data = _serializer(secret).loads(token, max_age=max_age)
    except (BadSignature, SignatureExpired):
        return None
    if not isinstance(data, dict) or not CHANNEL.fullmatch(str(data.get("c", ""))):
        return None
    if not isinstance(data.get("u"), int) or not isinstance(data.get("t"), int):
        return None
    return data


def redis_channel(tenant_id: int, channel: str) -> str:
    """Pub/Sub-Kanal je Buero - Buero-Grenzen gelten auch hier."""
    return f"zq:live:{int(tenant_id)}:{channel}"


def presence_key(tenant_id: int, channel: str) -> str:
    return f"zq:presence:{int(tenant_id)}:{channel}"
