"""Anruferkennung ueber Webex Calling (Webhook "telephony_calls").

Ablauf (Cisco-Doku: developer.webex.com/docs/call-control#webhook-payload-schemas):
1. Webex sendet je Anrufzustand ein POST an /telefonie/webex/webhook. Geprueft werden die Signatur
   (X-Webex-Signature HMAC-SHA256/512, sonst X-Spark-Signature HMAC-SHA1 ueber den unveraenderten
   Rohtext) und - falls konfiguriert - die Org-ID.
2. Ein eingehender Anruf ist data.personality == "terminator" mit data.state == "alerting". Wer
   angerufen wird, steht in actorId (Webex-Person-ID); der Anrufer in data.remoteParty
   (number/name, privacyEnabled bei unterdrueckter Nummer).
3. actorId wird ueber TelephonyLine genau einem Zentriq-Benutzer zugeordnet. Unbekannte Durchwahlen
   werden still quittiert (200), damit Webex den Webhook nicht deaktiviert.
4. Jeder Anruf wird einmal gespeichert (call_key = SHA-256 der callId, eindeutig) - doppelte
   Meldungen loesen keinen zweiten Hinweis aus. Danach geht ueber den Live-Dienst nur die
   Anruf-ID an den Benutzerkanal; der Browser holt die Treffer selbst (angemeldet, eigenes Buero).
5. Angenommen/aufgelegt (state connected/disconnected oder event deleted): Hinweis ausblenden.
6. Aufbewahrung: Anrufe aelter als TELEPHONY_CALL_RETENTION_DAYS (Standard 7) werden bei jedem neuen
   Anruf geloescht (zusaetzlich flask telefonie aufraeumen).

Placetel Notify (github.com/Placetel/call-control-notify-api, "Anruf an externe API melden" im Reiter
"Sonstiges" der Rufnummer): POST application/x-www-form-urlencoded an /telefonie/placetel/notify mit
event (IncomingCall | OutgoingCall | CallAccepted | HungUp), from, to, call_id, direction, peer.
Signatur: X-PLACETEL-SIGNATURE = HMAC-SHA256 (hex) ueber den Rohtext mit dem Secret aus
Einstellungen > Externe APIs. Reine Benachrichtigung - die Antwort wird ignoriert, das Routing
(Sprachmenue) bleibt unveraendert. Zugeordnet wird ueber die angerufene Nummer ("to") und/oder den
annehmenden SIP-Benutzer ("peer"); welche Durchwahl hinter einem Sprachmenue klingelt, meldet
Placetel nicht.

Logs enthalten nie Rufnummern, Namen, Secrets oder Signaturen - nur gekuerzte Hash-Kennungen."""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from flask import current_app, url_for
from sqlalchemy.exc import IntegrityError

from app.extensions import db
from app.models import Customer, TelephonyCall, TelephonyLine, User
from app.services import live
from app.tenancy import bypass_tenant_scope, use_tenant_id
from app.utils.customer_keys import format_phone, normalize_phone
from live_service import tokens

log = logging.getLogger(__name__)

OFFICE_TZ = ZoneInfo("Europe/Berlin")
MAX_BODY_BYTES = 64 * 1024
# Spaet zugestellte Klingel-Meldung (Wiederholung nach Stoerung): kein Hinweis mehr.
STALE_AFTER = timedelta(minutes=2)
# "Aktueller Anruf" beim Laden einer neuen Seite waehrend es klingelt.
RINGING_WINDOW = timedelta(seconds=90)
MAX_MATCHES = 8
INTERNAL_CALL_TYPES = {"location", "organization"}
_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


class LineError(ValueError):
    pass


# --- Kennungen ---------------------------------------------------------------------------------


def person_uuid(value: str | None) -> str | None:
    """Webex-Person-ID in jeder Form -> UUID in Kleinbuchstaben.

    Die API liefert Base64 von "ciscospark://us/PEOPLE/<uuid>", Control Hub zeigt die UUID selbst."""
    text = (value or "").strip()
    if not text:
        return None
    if _UUID.match(text.lower()):
        return text.lower()
    try:
        decoded = base64.urlsafe_b64decode(text + "=" * (-len(text) % 4)).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return None
    tail = decoded.rstrip("/").rsplit("/", 1)[-1].lower()
    return tail if _UUID.match(tail) else None


def call_key(call_id: str) -> str:
    return hashlib.sha256(call_id.encode("utf-8")).hexdigest()


def _ref(key: str) -> str:
    """Gekuerzte Kennung fuer Logs (kein Rueckschluss auf Anruf oder Nummer)."""
    return key[:8]


# --- Signatur ----------------------------------------------------------------------------------

_WEBEX_ALGORITHMS = {"SHA-256": hashlib.sha256, "SHA-512": hashlib.sha512}


def verify_signature(secret: str, body: bytes, headers) -> bool:
    """X-Webex-Signature ("SHA-256=<hex>, SHA-512=<hex>") bevorzugt, sonst X-Spark-Signature (SHA1)."""
    if not secret:
        return False
    key = secret.encode("utf-8")
    webex = headers.get("X-Webex-Signature")
    if webex:
        for pair in webex.split(","):
            name, _, digest = pair.strip().partition("=")
            algorithm = _WEBEX_ALGORITHMS.get(name.strip().upper())
            if algorithm and digest:
                expected = hmac.new(key, body, algorithm).hexdigest()
                if hmac.compare_digest(expected, digest.strip().lower()):
                    return True
        return False
    spark = headers.get("X-Spark-Signature")
    if spark:
        expected = hmac.new(key, body, hashlib.sha1).hexdigest()
        return hmac.compare_digest(expected, spark.strip().lower())
    return False


# --- Rufnummer -----------------------------------------------------------------------------------


def clean_number(raw: str | None) -> str | None:
    """Rufnummer aus remoteParty.number ("+492281234567", "02281234567", "tel:+49...", "sip:0228...@...")."""
    text = (raw or "").strip()
    if not text:
        return None
    lowered = text.lower()
    for prefix in ("tel:", "sip:", "sips:"):
        if lowered.startswith(prefix):
            text = text[len(prefix):]
            break
    text = text.split("@", 1)[0].split(";", 1)[0].strip()
    if not re.search(r"\d", text) or re.search(r"[A-Za-z]", text):
        return None  # "anonymous", "Unavailable" o. ae.
    return text[:50]


def number_keys(number: str | None) -> list[str]:
    """Vergleichsschluessel wie an den Kundenakten (normalize_phone): 0228 ... und +49 228 ... gleich."""
    key = normalize_phone(number)
    return [key] if key else []


def display_number(number: str | None) -> str | None:
    key = normalize_phone(number)
    return format_phone(key) if key else number


# --- Webhook-Verarbeitung ------------------------------------------------------------------------


@dataclass
class Outcome:
    status: int
    result: str  # fuer Logs und Antwort: ringing, duplicate, ended, ignored:<grund>, ...


def _parse_time(value) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _naive_utc(moment: datetime) -> datetime:
    return moment.astimezone(timezone.utc).replace(tzinfo=None)


def _find_line(actor_id) -> TelephonyLine | None:
    uuid_value = person_uuid(actor_id if isinstance(actor_id, str) else None)
    if not uuid_value:
        return None
    with bypass_tenant_scope():
        line = TelephonyLine.query.filter_by(webex_person_uuid=uuid_value, active=True).first()
        if line is not None:
            # Benutzer im selben Block laden (danach fehlt der Tenant-Kontext fuer Lazy-Loads).
            user = db.session.get(User, line.user_id)
            if user is None or not user.is_active or user.is_deleted:
                return None
            _ = line.tenant_id
    return line


def user_channel(user_id: int) -> str:
    return f"user:{int(user_id)}"


def _publish(call: TelephonyCall, kind: str) -> None:
    live.publish(call.tenant_id, user_channel(call.user_id), {"type": kind, "id": call.id})


def handle_event(payload) -> Outcome:
    """Verarbeitet eine (bereits signaturgepruefte) Meldung. Wiederholbar ohne Doppelwirkung."""
    if not isinstance(payload, dict):
        return Outcome(400, "invalid")
    if payload.get("resource") != "telephony_calls":
        return Outcome(200, "ignored:resource")
    org_id = current_app.config.get("WEBEX_ORG_ID") or ""
    if org_id and payload.get("orgId") != org_id:
        return Outcome(200, "ignored:org")
    data = payload.get("data")
    if not isinstance(data, dict):
        return Outcome(400, "invalid")
    call_id = data.get("callId")
    if not isinstance(call_id, str) or not call_id:
        return Outcome(400, "invalid")
    if data.get("personality") != "terminator":
        return Outcome(200, "ignored:outgoing")
    line = _find_line(payload.get("actorId"))
    if line is None:
        return Outcome(200, "ignored:line")

    key = call_key(call_id)
    state = data.get("state")
    event = payload.get("event")
    with use_tenant_id(line.tenant_id):
        if state == "alerting" and event != "deleted":
            return _ringing(line, key, data)
        if event == "deleted" or state in ("connected", "disconnected", "held", "remoteHeld"):
            return _finished(key, "answered" if state in ("connected", "held", "remoteHeld") else "ended")
    return Outcome(200, "ignored:state")


def _ringing(line: TelephonyLine, key: str, data: dict) -> Outcome:
    if TelephonyCall.query.filter_by(call_key=key).first() is not None:
        log.info("telefonie.webhook duplicate call=%s", _ref(key))
        return Outcome(200, "duplicate")
    emitted = _parse_time(data.get("eventTimestamp")) or _parse_time(data.get("created"))
    if emitted is not None and datetime.now(timezone.utc) - emitted > STALE_AFTER:
        log.info("telefonie.webhook stale call=%s", _ref(key))
        return Outcome(200, "ignored:stale")
    party = data.get("remoteParty") if isinstance(data.get("remoteParty"), dict) else {}
    number = None if party.get("privacyEnabled") else clean_number(party.get("number"))
    return _store_ringing(line, key, number=number, privacy=bool(party.get("privacyEnabled")) or number is None,
                          call_type=str(party.get("callType") or "")[:20] or None)


def _store_ringing(line, key: str, *, number: str | None, privacy: bool, call_type: str | None,
                   external_ref: str | None = None) -> Outcome:
    call = TelephonyCall(
        tenant_id=line.tenant_id,
        call_key=key,
        external_ref=external_ref,
        line_id=line.id,
        user_id=line.user_id,
        remote_number=number,
        privacy=privacy,
        call_type=call_type,
        status="ringing",
    )
    db.session.add(call)
    try:
        db.session.commit()
    except IntegrityError:
        # Dieselbe Meldung gleichzeitig ein zweites Mal: der andere Aufruf hat schon gespeichert.
        db.session.rollback()
        log.info("telefonie.webhook duplicate call=%s", _ref(key))
        return Outcome(200, "duplicate")
    _publish(call, "call")
    log.info("telefonie.webhook ringing call=%s line=%s", _ref(key), line.id)
    try:
        purge_old_calls()
    except Exception as exc:  # Aufraeumen darf den Anruf-Hinweis nie verhindern.
        db.session.rollback()
        log.warning("telefonie.purge failed: %s", type(exc).__name__)
    return Outcome(200, "ringing")


def _finished(key: str, status: str) -> Outcome:
    call = TelephonyCall.query.filter_by(call_key=key).first()
    if call is None:
        return Outcome(200, "ignored:unknown_call")
    if call.status != "ringing":
        return Outcome(200, "duplicate")
    call.status = status
    call.ended_at = _naive_utc(datetime.now(timezone.utc))
    db.session.commit()
    _publish(call, "call_end")
    log.info("telefonie.webhook %s call=%s", status, _ref(key))
    return Outcome(200, status)


# --- Placetel Notify ----------------------------------------------------------------------------

PLACETEL_EVENTS = {"IncomingCall", "OutgoingCall", "CallAccepted", "HungUp"}


def verify_placetel_signature(secret: str, body: bytes, headers) -> bool:
    """X-PLACETEL-SIGNATURE: HMAC-SHA256 (hex) des unveraenderten Rohtexts mit dem Secret."""
    signature = (headers.get("X-PLACETEL-SIGNATURE") or "").strip().lower()
    if not secret or not signature:
        return False
    expected = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def placetel_peer(value: str | None) -> str | None:
    """SIP-Benutzer wie von Placetel geliefert ("7777abcdefg@fpbx.de"), klein geschrieben."""
    text = (value or "").strip().lower()
    if text.startswith("sip:"):
        text = text[4:]
    return text[:120] or None


@dataclass(frozen=True)
class _LineRef:
    """Feste Werte einer Zuordnung - ein Commit (Hinweis fuer den ersten Benutzer) laesst ORM-Objekte
    ablaufen, ein Nachladen ohne Mandantenkontext waere nicht erlaubt."""

    id: int
    tenant_id: int
    user_id: int


def _active_lines(**filters) -> list[_LineRef]:
    """Aktive Zuordnungen aller Bueros (Placetel meldet ohne Mandant) - nur aktive Benutzer."""
    with bypass_tenant_scope():
        lines = TelephonyLine.query.filter_by(active=True, **filters).order_by(TelephonyLine.id).all()
        result = []
        for line in lines:
            user = db.session.get(User, line.user_id)
            if user is not None and user.is_active and not user.is_deleted:
                result.append(_LineRef(line.id, line.tenant_id, line.user_id))
    return result


def handle_placetel(form) -> Outcome:
    """Verarbeitet eine (bereits signaturgepruefte) Placetel-Meldung. Wiederholbar ohne Doppelwirkung.

    IncomingCall -> Hinweis fuer alle Benutzer, denen die angerufene Nummer zugeordnet ist.
    CallAccepted -> Hinweis bleibt beim annehmenden SIP-Benutzer (bzw. erscheint dort, falls nur der
                    SIP-Benutzer zugeordnet ist); bei den uebrigen "Anruf beendet".
    HungUp       -> alle Hinweise dieses Anrufs ausblenden."""
    event = (form.get("event") or "").strip()
    call_id = (form.get("call_id") or "").strip()
    if event not in PLACETEL_EVENTS or not call_id or len(call_id) > 128:
        return Outcome(400, "invalid")
    if event == "OutgoingCall" or (form.get("direction") or "in") != "in":
        return Outcome(200, "ignored:outgoing")
    ref = call_key(f"placetel:{call_id}")
    raw_from = (form.get("from") or "").strip()
    number = None if raw_from.lower() in ("", "anonymous") else clean_number(raw_from)
    privacy = number is None
    if event == "IncomingCall":
        to_keys = number_keys(form.get("to"))
        lines = _active_lines(placetel_number_key=to_keys[0]) if to_keys else []
        if not lines:
            return Outcome(200, "ignored:line")
        results = []
        for line in lines:
            with use_tenant_id(line.tenant_id):
                results.append(_store_ringing(line, call_key(f"placetel:{call_id}:{line.id}"), number=number, privacy=privacy,
                                              call_type="placetel", external_ref=ref).result)
        return Outcome(200, "ringing" if "ringing" in results else results[0])
    if event == "CallAccepted":
        peer = placetel_peer(form.get("peer"))
        answering = {line.id: line for line in _active_lines(placetel_peer=peer)} if peer else {}
        outcome = _placetel_finish(ref, "answered", keep_lines=set(answering))
        for line in answering.values():
            key = call_key(f"placetel:{call_id}:{line.id}")
            with use_tenant_id(line.tenant_id):
                if TelephonyCall.query.filter_by(call_key=key).first() is None:
                    # Nur der SIP-Benutzer ist zugeordnet (Sprachmenue davor): Hinweis bei Annahme.
                    _store_ringing(line, key, number=number, privacy=privacy, call_type="placetel", external_ref=ref)
        return Outcome(200, "accepted" if answering else outcome.result)
    return _placetel_finish(ref, "ended")


def _placetel_finish(ref: str, status: str, keep_lines: set | None = None) -> Outcome:
    with bypass_tenant_scope():
        calls = TelephonyCall.query.filter_by(external_ref=ref, status="ringing").all()
        if not calls:
            return Outcome(200, "ignored:unknown_call")
        changed = []
        for call in calls:
            if keep_lines and call.line_id in keep_lines:
                continue  # der annehmende Benutzer behaelt den Hinweis bis zum Auflegen
            call.status = status
            call.ended_at = _naive_utc(datetime.now(timezone.utc))
            changed.append(call)
        db.session.commit()
        for call in changed:
            _publish(call, "call_end")
    log.info("telefonie.placetel %s call=%s count=%s", status, _ref(ref), len(changed))
    return Outcome(200, status if changed else "duplicate")


def assign_placetel(user: User, *, number: str | None, peer: str | None, label: str | None) -> TelephonyLine:
    """Placetel-Zuordnung fuer den Benutzer: angerufene Nummer und/oder SIP-Benutzer."""
    number_key = normalize_phone(number) if number else None
    peer_value = placetel_peer(peer)
    if number and not number_key:
        raise LineError("Rufnummer nicht erkannt (z. B. 0228 1234567 oder +49 228 1234567).")
    if not number_key and not peer_value:
        raise LineError("Bitte die angerufene Rufnummer und/oder den SIP-Benutzer angeben.")
    with bypass_tenant_scope():
        if peer_value:
            taken = TelephonyLine.query.filter_by(placetel_peer=peer_value).first()
            if taken is not None and taken.user_id != user.id:
                raise LineError("Dieser SIP-Benutzer ist bereits einem anderen Benutzer zugeordnet.")
        line = TelephonyLine.query.filter_by(user_id=user.id).first()
        if line is None:
            line = TelephonyLine(tenant_id=user.tenant_id, user_id=user.id)
            db.session.add(line)
        line.tenant_id = user.tenant_id
        line.placetel_number_key = number_key
        line.placetel_peer = peer_value
        line.label = (label or "").strip()[:60] or line.label
        line.active = True
        db.session.commit()
    return line


def purge_old_calls(days: int | None = None) -> int:
    """Loescht Anrufe aller Bueros, die aelter als die Aufbewahrungsfrist sind. 0 Tage = nie."""
    if days is None:
        days = int(current_app.config.get("TELEPHONY_CALL_RETENTION_DAYS") or 0)
    if days <= 0:
        return 0
    cutoff = _naive_utc(datetime.now(timezone.utc) - timedelta(days=days))
    with bypass_tenant_scope():
        removed = TelephonyCall.query.filter(TelephonyCall.created_at < cutoff).delete(synchronize_session=False)
    db.session.commit()
    if removed:
        log.info("telefonie.purge removed=%s days=%s", removed, days)
    return removed


# --- Browser: Anruf anzeigen -----------------------------------------------------------------------


def line_for(user) -> TelephonyLine | None:
    if not getattr(user, "is_authenticated", False):
        return None
    return TelephonyLine.query.filter_by(user_id=user.id, active=True).first()


def token_for(user) -> str:
    return tokens.issue(
        current_app.config["SECRET_KEY"], user_id=user.id, tenant_id=user.tenant_id, name=live.display_name(user), channel=user_channel(user.id)
    )


def own_call(user, call_id: int) -> TelephonyCall | None:
    # Tenant-Filter greift automatisch; zusaetzlich nur Anrufe der eigenen Durchwahl.
    return TelephonyCall.query.filter_by(id=call_id, user_id=user.id).first()


def current_call(user) -> TelephonyCall | None:
    since = _naive_utc(datetime.now(timezone.utc) - RINGING_WINDOW)
    return (
        TelephonyCall.query.filter(TelephonyCall.user_id == user.id, TelephonyCall.status == "ringing", TelephonyCall.created_at >= since)
        .order_by(TelephonyCall.created_at.desc())
        .first()
    )


def find_customers(number: str | None) -> list[Customer]:
    """Kunden des eigenen Bueros (Tenant-Filter) mit dieser Nummer - bisherige, Festnetz oder privat."""
    keys = number_keys(number)
    if not keys:
        return []
    return Customer.query.filter(Customer.phone_key_in(keys)).order_by(Customer.name).limit(MAX_MATCHES).all()


def _customer_link(user, customer: Customer) -> dict:
    # Wie die Schnellsuche: Akte im Seitenfenster (die geoeffnete Akte bleibt) - Mitarbeiter lesend.
    url, panel = url_for("customers.detail", customer_id=customer.id), True
    detail = " · ".join(v for v in (customer.customer_number, " ".join(p for p in (customer.postal_code, customer.city) if p)) if v)
    return {"id": customer.id, "name": customer.name, "detail": detail, "url": url, "panel": panel}


def describe(user, call: TelephonyCall) -> dict:
    base = {"id": call.id, "status": call.status, "at": call.created_at.replace(tzinfo=timezone.utc).isoformat(timespec="seconds")}
    if call.privacy or not call.remote_number:
        return {**base, "kind": "unknown", "title": "Unbekannter Anrufer"}
    number = display_number(call.remote_number)
    if call.call_type in INTERNAL_CALL_TYPES and not number_keys(call.remote_number):
        return {**base, "kind": "internal", "title": "Interner Anruf", "number": number}
    customers = find_customers(call.remote_number)
    if len(customers) == 1:
        hit = _customer_link(user, customers[0])
        return {**base, "kind": "single", "title": f"{customers[0].name} ruft an", "number": number, "customers": [hit]}
    if customers:
        return {**base, "kind": "multiple", "title": f"Anruf von {number}", "number": number,
                "customers": [_customer_link(user, c) for c in customers]}
    return {**base, "kind": "none", "title": f"Anruf von {number}", "number": number,
            "callback_url": url_for("workspace.call_callback_note", call_id=call.id)}


def callback_note_text(call: TelephonyCall) -> str:
    at = call.created_at.replace(tzinfo=timezone.utc).astimezone(OFFICE_TZ)
    number = display_number(call.remote_number) or "unbekannt"
    return f"Rückruf erbeten – Anruf von {number} am {at:%d.%m.%Y} um {at:%H:%M} Uhr.\n"


# --- Verwaltung (CLI) -------------------------------------------------------------------------------


def assign_line(user: User, person_id: str, label: str | None) -> TelephonyLine:
    """Ordnet eine Webex-Person (Durchwahl) dem Benutzer zu; ersetzt dessen bisherige Zuordnung."""
    uuid_value = person_uuid(person_id)
    if not uuid_value:
        raise LineError("Webex-Person-ID nicht erkannt (erwartet: UUID oder Base64-ID aus der Webex-API).")
    with bypass_tenant_scope():
        taken = TelephonyLine.query.filter_by(webex_person_uuid=uuid_value).first()
        if taken is not None and taken.user_id != user.id:
            raise LineError("Diese Webex-Person ist bereits einem anderen Benutzer zugeordnet.")
        line = TelephonyLine.query.filter_by(user_id=user.id).first()
        if line is None:
            line = TelephonyLine(tenant_id=user.tenant_id, user_id=user.id, webex_person_uuid=uuid_value)
            db.session.add(line)
        line.tenant_id = user.tenant_id
        line.webex_person_uuid = uuid_value
        line.label = (label or "").strip()[:60] or None
        line.active = True
        db.session.commit()
    return line
