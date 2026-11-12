"""zentriq-live: Server-Sent Events fuer Kundenakten (Anwesenheit, "bearbeitet gerade", Aenderungen).

Ablauf:
- Browser holt bei Flask (/arbeitsplatz/live/token) ein Token fuer genau einen Kanal (Kundenakte).
- Browser oeffnet GET /live/stream?token=... (nginx -> 127.0.0.1:8001, ohne Pufferung).
- Der Dienst traegt die Verbindung in die Anwesenheitsliste ein (Redis-Hash je Kanal, mit
  Zeitstempel - abgestuerzte Verbindungen fallen nach STALE_SECONDS heraus) und leitet alles weiter,
  was Flask nach einem erfolgreichen Commit auf zq:live:<buero>:<kanal> veroeffentlicht.

Start: python -m live_service.server  (Umgebung: SECRET_KEY, LIVE_REDIS_URL bzw. CELERY_BROKER_URL,
LIVE_HOST, LIVE_PORT). Keine Datenbank, keine Kundendaten - nur Namen der Anwesenden und Art der
Aenderung."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import time
import uuid
from urllib.parse import parse_qs, urlsplit

from live_service import tokens

log = logging.getLogger("zentriq-live")

PING_SECONDS = 20  # Kommentarzeile gegen Proxy-/Mobilfunk-Timeouts, zugleich Anwesenheits-Auffrischung
STALE_SECONDS = 60  # Anwesenheit ohne Auffrischung gilt als weg (z. B. nach Absturz des Dienstes)
MAX_STREAM_SECONDS = 30 * 60  # danach neues Token ueber Flask (Abmeldung/Rechte wirken zeitnah)
MAX_CONNECTIONS_PER_USER = 12
MAX_HEADER_BYTES = 8192
QUEUE_SIZE = 100
FORWARD_TYPES = {"changed", "editing", "call", "call_end", "chat", "chat_read"}


# --- Broker: Redis im Betrieb, Speicher in Tests ------------------------------------------------


class MemoryBroker:
    """Fuer Tests und lokale Pruefung - gleiche Schnittstelle wie RedisBroker."""

    def __init__(self):
        self.hashes: dict[str, dict[str, str]] = {}
        self._queue: asyncio.Queue = asyncio.Queue()

    async def publish(self, channel: str, data: str) -> None:
        await self._queue.put((channel, data))

    async def listen(self):
        while True:
            yield await self._queue.get()

    async def hset(self, key, field, value):
        self.hashes.setdefault(key, {})[field] = value

    async def hdel(self, key, *fields):
        for field in fields:
            self.hashes.get(key, {}).pop(field, None)

    async def hgetall(self, key):
        return dict(self.hashes.get(key, {}))

    async def expire(self, key, seconds):
        return None


class RedisBroker:
    PATTERN = "zq:live:*"

    def __init__(self, url: str):
        import redis.asyncio as aioredis

        self.redis = aioredis.from_url(url, decode_responses=True, socket_connect_timeout=3, health_check_interval=30)

    async def publish(self, channel, data):
        await self.redis.publish(channel, data)

    async def listen(self):
        while True:
            pubsub = self.redis.pubsub()
            try:
                await pubsub.psubscribe(self.PATTERN)
                async for message in pubsub.listen():
                    if message.get("type") == "pmessage":
                        yield message["channel"], message["data"]
            except (OSError, ConnectionError, asyncio.TimeoutError) as exc:  # Redis-Neustart: erneut verbinden
                log.warning("Redis-Verbindung unterbrochen (%s) - neuer Versuch in 2 s", type(exc).__name__)
                await asyncio.sleep(2)
            except Exception as exc:  # noqa: BLE001 - redis.exceptions.* ohne harten Import
                log.warning("Redis-Fehler (%s) - neuer Versuch in 2 s", type(exc).__name__)
                await asyncio.sleep(2)
            finally:
                try:
                    await pubsub.aclose()
                except Exception:  # noqa: BLE001
                    pass

    async def hset(self, key, field, value):
        await self.redis.hset(key, field, value)

    async def hdel(self, key, *fields):
        if fields:
            await self.redis.hdel(key, *fields)

    async def hgetall(self, key):
        return await self.redis.hgetall(key)

    async def expire(self, key, seconds):
        await self.redis.expire(key, seconds)


# --- HTTP (bewusst minimal: nur GET, keine Keep-Alive-Verbindungen) -------------------------------


def parse_request(head: bytes) -> tuple[str, str, dict] | None:
    """(method, path, query) aus der Anfragezeile oder None."""
    try:
        line = head.split(b"\r\n", 1)[0].decode("latin-1")
        method, target, _version = line.split(" ", 2)
    except ValueError:
        return None
    parts = urlsplit(target)
    query = {k: v[0] for k, v in parse_qs(parts.query, keep_blank_values=False).items()}
    return method.upper(), parts.path, query


def _response(status: str, body: str = "", content_type: str = "text/plain; charset=utf-8") -> bytes:
    data = body.encode("utf-8")
    return (
        f"HTTP/1.1 {status}\r\nContent-Type: {content_type}\r\nContent-Length: {len(data)}\r\n"
        "Cache-Control: no-store\r\nConnection: close\r\n\r\n"
    ).encode("latin-1") + data


SSE_HEADERS = (
    "HTTP/1.1 200 OK\r\n"
    "Content-Type: text/event-stream; charset=utf-8\r\n"
    "Cache-Control: no-cache, no-store, no-transform\r\n"
    "X-Accel-Buffering: no\r\n"
    "Connection: close\r\n\r\n"
).encode("latin-1")


def sse(event: str, data: dict) -> bytes:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False, separators=(',', ':'))}\n\n".encode("utf-8")


class Connection:
    def __init__(self, claims: dict):
        self.id = uuid.uuid4().hex
        self.user_id = claims["u"]
        self.tenant_id = claims["t"]
        self.name = claims.get("n") or ""
        self.channel = claims["c"]
        self.redis_channel = tokens.redis_channel(self.tenant_id, self.channel)
        self.presence_key = tokens.presence_key(self.tenant_id, self.channel)
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=QUEUE_SIZE)


class Hub:
    def __init__(self, broker, secret: str, *, ping=PING_SECONDS, stale=STALE_SECONDS, max_stream=MAX_STREAM_SECONDS, clock=time.time):
        self.broker = broker
        self.secret = secret
        self.ping = ping
        self.stale = stale
        self.max_stream = max_stream
        self.clock = clock
        self.subscribers: dict[str, set[Connection]] = {}
        self.per_user: dict[tuple[int, int], int] = {}
        self.writers: set[asyncio.StreamWriter] = set()

    def close_all(self) -> None:
        """Beim Herunterfahren: alle Verbindungen schliessen (Browser verbinden sich neu)."""
        for writer in list(self.writers):
            writer.close()

    # Verteilung: EIN Abonnement fuer alle Kanaele, Verteilung an lokale Verbindungen.
    async def dispatch(self) -> None:
        async for channel, data in self.broker.listen():
            for conn in list(self.subscribers.get(channel, ())):
                try:
                    conn.queue.put_nowait(data)
                except asyncio.QueueFull:  # Browser liest nicht mehr: Verbindung wird beendet
                    log.info("Warteschlange voll - Verbindung %s wird beendet", conn.id[:8])
                    conn.queue = None  # type: ignore[assignment]

    async def _presence_entry(self, conn: Connection) -> None:
        if tokens.is_personal(conn.channel):
            return  # persoenlicher Kanal: keine Anwesenheit
        value = json.dumps({"u": conn.user_id, "n": conn.name, "ts": int(self.clock())})
        await self.broker.hset(conn.presence_key, conn.id, value)
        await self.broker.expire(conn.presence_key, self.stale * 3)

    async def viewers(self, key: str) -> list[dict]:
        now = self.clock()
        seen: dict[int, str] = {}
        stale = []
        for field, raw in (await self.broker.hgetall(key)).items():
            try:
                entry = json.loads(raw)
            except (TypeError, ValueError):
                stale.append(field)
                continue
            if now - entry.get("ts", 0) > self.stale:
                stale.append(field)
                continue
            seen.setdefault(int(entry["u"]), entry.get("n") or "")
        if stale:
            await self.broker.hdel(key, *stale)
        return sorted(({"u": u, "n": n} for u, n in seen.items()), key=lambda v: (v["n"].lower(), v["u"]))

    async def _announce(self, conn: Connection) -> None:
        if tokens.is_personal(conn.channel):
            return
        await self.broker.publish(conn.redis_channel, json.dumps({"type": "presence"}))

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.writers.add(writer)
        try:
            await self._handle(reader, writer)
        finally:
            self.writers.discard(writer)

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=5)
        except (asyncio.TimeoutError, asyncio.IncompleteReadError, asyncio.LimitOverrunError, ConnectionError):
            writer.close()
            return
        parsed = parse_request(head)
        if parsed is None or parsed[0] != "GET":
            await self._reply(writer, _response("405 Method Not Allowed", "Nur GET."))
            return
        _method, path, query = parsed
        if path == "/live/health":
            await self._reply(writer, _response("200 OK", "ok"))
            return
        if path != "/live/stream":
            await self._reply(writer, _response("404 Not Found", "Unbekannt."))
            return
        claims = tokens.verify(self.secret, query.get("token", ""))
        if claims is None:
            await self._reply(writer, _response("403 Forbidden", "Token ungueltig oder abgelaufen."))
            return
        user_key = (claims["t"], claims["u"])
        if self.per_user.get(user_key, 0) >= MAX_CONNECTIONS_PER_USER:
            await self._reply(writer, _response("429 Too Many Requests", "Zu viele Verbindungen."))
            return
        conn = Connection(claims)
        self.per_user[user_key] = self.per_user.get(user_key, 0) + 1
        self.subscribers.setdefault(conn.redis_channel, set()).add(conn)
        try:
            await self._stream(conn, reader, writer)
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            self.subscribers.get(conn.redis_channel, set()).discard(conn)
            if not self.subscribers.get(conn.redis_channel):
                self.subscribers.pop(conn.redis_channel, None)
            self.per_user[user_key] -= 1
            if self.per_user[user_key] <= 0:
                self.per_user.pop(user_key, None)
            try:
                await self.broker.hdel(conn.presence_key, conn.id)
                await self._announce(conn)
            except Exception:  # noqa: BLE001 - Redis weg: Eintrag verfaellt ueber STALE_SECONDS
                pass
            writer.close()

    async def _reply(self, writer, data: bytes) -> None:
        try:
            writer.write(data)
            await writer.drain()
        except ConnectionError:
            pass
        writer.close()

    async def _stream(self, conn: Connection, reader, writer) -> None:
        writer.write(SSE_HEADERS + b"retry: 5000\n\n" + sse("hello", {"conn": conn.id}))
        await writer.drain()
        await self._presence_entry(conn)
        await self._announce(conn)
        started = self.clock()
        closed = asyncio.ensure_future(reader.read(1))  # Browser schliesst -> EOF
        try:
            while True:
                if conn.queue is None or closed.done():
                    return
                if self.clock() - started > self.max_stream:
                    writer.write(sse("renew", {}))
                    await writer.drain()
                    return
                getter = asyncio.ensure_future(conn.queue.get())
                done, _ = await asyncio.wait({getter, closed}, timeout=self.ping, return_when=asyncio.FIRST_COMPLETED)
                if getter not in done:
                    getter.cancel()
                    if closed in done:
                        return
                    writer.write(b": ping\n\n")
                    await writer.drain()
                    await self._presence_entry(conn)
                    continue
                await self._forward(conn, writer, getter.result())
        finally:
            closed.cancel()

    async def _forward(self, conn: Connection, writer, raw: str) -> None:
        try:
            message = json.loads(raw)
        except (TypeError, ValueError):
            return
        kind = message.get("type")
        if kind == "presence":
            writer.write(sse("presence", {"viewers": await self.viewers(conn.presence_key)}))
        elif kind in FORWARD_TYPES:
            writer.write(sse(kind, {k: v for k, v in message.items() if k != "type"}))
        else:
            return
        await writer.drain()


async def serve(hub: Hub, host: str, port: int) -> None:
    server = await asyncio.start_server(hub.handle, host, port, limit=MAX_HEADER_BYTES)
    dispatcher = asyncio.create_task(hub.dispatch())
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # pragma: no cover
            pass
    log.info("zentriq-live hoert auf %s:%s", host, port)
    await stop.wait()
    # Seit Python 3.12 wartet wait_closed() auf alle offenen Verbindungen - SSE-Verbindungen enden
    # aber nie von selbst. Deshalb aktiv schliessen und hoechstens 5 s warten (systemd-Neustart).
    server.close()
    hub.close_all()
    dispatcher.cancel()
    try:
        await asyncio.wait_for(server.wait_closed(), timeout=5)
    except asyncio.TimeoutError:
        log.warning("Nicht alle Verbindungen rechtzeitig geschlossen - beende trotzdem")
    log.info("zentriq-live beendet")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    secret = os.environ.get("SECRET_KEY") or ""
    if not secret or secret == "dev-secret-key-change-me":
        raise SystemExit("zentriq-live: SECRET_KEY fehlt (gleicher Wert wie die Flask-App).")
    url = os.environ.get("LIVE_REDIS_URL") or os.environ.get("CELERY_BROKER_URL") or "redis://localhost:6379/0"
    host = os.environ.get("LIVE_HOST", "127.0.0.1")
    port = int(os.environ.get("LIVE_PORT", "8001"))
    asyncio.run(serve(Hub(RedisBroker(url), secret), host, port))


if __name__ == "__main__":
    main()
