"""Memo -> Zentriq-Kundenstamm: Memo zuordnen, fehlende Daten ergaenzen, Kunden anlegen.

Regeln (zusaetzlich zur Erkennung in app/services/memo_customer_match.py):
- Automatisch zugeordnet wird nur bei einem eindeutigen Treffer ueber Kundennummer oder
  Telefonnummer. Ein Treffer nur ueber den Namen oder mehrere Treffer werden nie automatisch
  zugeordnet - der Benutzer waehlt selbst.
- Gespeichert wird ausschliesslich das Transkript eines zugeordneten Memos; der Text muss aus
  einer Transkription dieser Sitzung stammen (signiertes Token, siehe transcript_token()).
- Fehlende Kundendaten (Telefon, Kundennummer) werden nur ergaenzt, wenn das Transkript genau
  einen Wert nennt, das Feld leer ist und kein anderer Kunde diesen Wert bereits hat.
- Ein neuer Kunde wird nur auf ausdrueckliche Bestaetigung angelegt und nie, wenn Kundennummer
  oder Telefonnummer schon im Bestand vorkommen. Ein gleicher Name verlangt eine zusaetzliche
  Bestaetigung (zwei Personen koennen gleich heissen).
Alle Abfragen laufen ueber den globalen Tenant-Filter: nie bueroubergreifend."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field

from flask import current_app
from itsdangerous import BadSignature, URLSafeTimedSerializer

from app.extensions import db
from app.models import Customer, CustomerMemo
from app.models.audit_log import AuditEventType
from app.services import customer_sources
from app.services.audit import log_audit_event
from app.services.memo_customer_match import (
    MatchResult,
    extract_customer_numbers,
    extract_phone_numbers,
    format_phone,
)
from app.utils.customer_keys import customer_keys

TOKEN_SALT = "zentriq-memo-transcript"
# Zuordnen ist bis zu zwei Stunden nach der Transkription moeglich.
TOKEN_MAX_AGE_SECONDS = 2 * 60 * 60
MAX_NAME_LENGTH = 120
AUTO_MATCH_BASES = ("customer_number", "phone")

# "Hallo, hier ist Peter Mueller" / "mein Name ist ..." / "hier spricht ..." / "ich bin ...".
_NAME_INTRO = re.compile(
    r"\b(?:hier\s+ist|hier\s+spricht|mein\s+name\s+ist|ich\s+bin|ich\s+hei(?:ss|ß)e)\s+(?:(?:der|die|frau|herr)\s+)?"
    r"((?:[A-ZÄÖÜ][a-zäöüß]+(?:-[A-ZÄÖÜ][a-zäöüß]+)?\s*){2,4})",
    re.IGNORECASE,
)
_NAME_WORD = re.compile(r"^[A-ZÄÖÜ][a-zäöüß]+(?:-[A-ZÄÖÜ][a-zäöüß]+)?$")
# Gross geschriebene Woerter, die kein Namensbestandteil sind (Satzanfang, Anrede).
_NOT_A_NAME = frozenset(
    {
        "sie", "ich", "ihr", "ihnen", "wir", "und", "oder", "von", "vom", "der", "die", "das", "dem", "den",
        "bitte", "danke", "hallo", "guten", "tag", "morgen", "abend", "wegen", "mit", "aus", "bei", "ein",
        "eine", "es", "zu", "zur", "zum", "rufen", "rueckruf", "rückruf", "meine", "mein", "nummer", "telefon",
        "kundennummer", "frau", "herr", "ist", "hier", "spricht", "versicherung", "huk", "coburg",
    }
)


class MemoAssignmentError(ValueError):
    """Fehler mit einer fuer den Nutzer verstaendlichen Meldung."""


@dataclass
class DuplicateFound(Exception):
    """Vor dem Anlegen gefundene moegliche Dubletten."""

    customers: list[Customer] = field(default_factory=list)
    # True: gleiche Kundennummer/Telefonnummer - Anlegen ausgeschlossen. False: nur gleicher Name.
    blocking: bool = True


def transcript_hash(transcript: str) -> str:
    return hashlib.sha256(transcript.strip().encode("utf-8")).hexdigest()


def _serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(current_app.config["SECRET_KEY"], salt=TOKEN_SALT)


def transcript_token(transcript: str, user) -> str:
    """Belegt, dass `transcript` von der Transkription fuer diesen Benutzer erzeugt wurde."""
    return _serializer().dumps({"h": transcript_hash(transcript), "u": user.id, "t": user.tenant_id})


def verify_transcript_token(token: str | None, transcript: str, user) -> bool:
    if not token or not isinstance(token, str):
        return False
    try:
        data = _serializer().loads(token, max_age=TOKEN_MAX_AGE_SECONDS)
    except BadSignature:
        return False
    return (
        isinstance(data, dict)
        and data.get("h") == transcript_hash(transcript)
        and data.get("u") == user.id
        and data.get("t") == user.tenant_id
    )


def detect_caller_name(text: str | None) -> str | None:
    """Name des Anrufers, wenn er sich ausdruecklich vorstellt - sonst None (nichts raten)."""
    for match in _NAME_INTRO.finditer(text or ""):
        words = []
        for word in match.group(1).split():
            if word.lower() in _NOT_A_NAME or not _NAME_WORD.match(word):
                break
            words.append(word)
        if len(words) >= 2:
            return " ".join(words[:3])
    return None


def new_customer_suggestion(result: MatchResult, transcript: str) -> dict | None:
    """Vorschlag "Neuer Kunde erkannt": nur ohne jeden Treffer im Bestand, mit genanntem Namen
    und genau einer Telefonnummer bzw. Kundennummer."""
    if result.status != "none":
        return None
    name = detect_caller_name(transcript)
    phone = result.phones[0] if len(result.phones) == 1 else None
    number = result.customer_numbers[0] if len(result.customer_numbers) == 1 else None
    if not name or not (phone or number):
        return None
    return {"name": name, "phone": format_phone(phone) if phone else None, "customer_number": number}


def _taken(column: str, value: str | None, customer: Customer | None = None) -> bool:
    if not value:
        return False
    query = Customer.query.filter(getattr(Customer, column) == value)
    if customer is not None and customer.id is not None:
        query = query.filter(Customer.id != customer.id)
    return db.session.query(query.exists()).scalar()


def complete_customer(customer: Customer, transcript: str) -> list[str]:
    """Ergaenzt leere Telefon-/Kundennummer-Felder aus dem Transkript (siehe Modul-Doku)."""
    values = {}
    phones = extract_phone_numbers(transcript)
    numbers = extract_customer_numbers(transcript)
    if not customer.phone and len(phones) == 1 and not _taken("phone_key", phones[0], customer):
        values["phone"] = format_phone(phones[0])
    if not customer.customer_number and len(numbers) == 1 and not _taken("customer_number_key", numbers[0], customer):
        values["customer_number"] = numbers[0]
    return customer_sources.apply_customer_values(customer, values, customer_sources.MEMO)


def assign_memo(customer: Customer, transcript: str, matched_by: str, user) -> tuple[CustomerMemo, bool, list[str]]:
    """Ordnet das Transkript dem Kunden zu (idempotent). Liefert (Memo, neu angelegt, ergaenzte Felder)."""
    if customer.tenant_id != user.tenant_id:
        raise MemoAssignmentError("Der Kunde gehört nicht zu Ihrem Büro.")
    text = transcript.strip()
    digest = transcript_hash(text)
    existing = CustomerMemo.query.filter_by(customer_id=customer.id, transcript_sha256=digest).first()
    if existing is not None:
        return existing, False, []
    filled = complete_customer(customer, text)
    memo = CustomerMemo(
        tenant_id=customer.tenant_id,
        customer=customer,
        transcript=text,
        transcript_sha256=digest,
        matched_by=matched_by,
        created_by_user_id=user.id,
    )
    db.session.add(memo)
    db.session.flush()
    # Der Audit-Eintrag committet; er enthaelt nur IDs und die Grundlage, nie Text.
    log_audit_event(
        AuditEventType.MEMO_ASSIGNED,
        user=user,
        details={"customer_id": customer.id, "memo_id": memo.id, "matched_by": matched_by, "filled_fields": filled},
    )
    return memo, True, filled


def find_conflicts(name: str, phone: str | None, customer_number: str | None) -> DuplicateFound | None:
    keys = customer_keys(name, phone, customer_number)
    hard = []
    if keys["customer_number_key"]:
        hard += Customer.query.filter(Customer.customer_number_key == keys["customer_number_key"]).limit(5).all()
    if keys["phone_key"]:
        hard += Customer.query.filter(Customer.phone_key == keys["phone_key"]).limit(5).all()
    if hard:
        unique = list({customer.id: customer for customer in hard}.values())
        return DuplicateFound(unique, blocking=True)
    if keys["name_key"]:
        same_name = Customer.query.filter(Customer.name_key == keys["name_key"]).order_by(Customer.id).limit(5).all()
        if same_name:
            return DuplicateFound(same_name, blocking=False)
    return None


def create_customer_from_memo(
    *,
    name: str,
    phone: str | None,
    customer_number: str | None,
    transcript: str,
    user,
    confirm_same_name: bool = False,
) -> tuple[Customer, CustomerMemo]:
    name = " ".join((name or "").split())[:MAX_NAME_LENGTH]
    phone = " ".join((phone or "").split())[:50] or None
    customer_number = " ".join((customer_number or "").split())[:50] or None
    keys = customer_keys(name, phone, customer_number)
    if not keys["name_key"]:
        raise MemoAssignmentError("Bitte einen Namen angeben.")
    if phone and not keys["phone_key"]:
        raise MemoAssignmentError("Die Telefonnummer ist ungültig (bitte mit Vorwahl angeben).")
    if not (keys["phone_key"] or keys["customer_number_key"]):
        raise MemoAssignmentError("Bitte eine Telefonnummer oder Kundennummer angeben.")

    conflict = find_conflicts(name, phone, customer_number)
    if conflict is not None and (conflict.blocking or not confirm_same_name):
        raise conflict

    customer = Customer(
        tenant_id=user.tenant_id,
        name=name,
        assigned_user_id=user.id,
        source=customer_sources.MEMO,
        field_sources={"name": customer_sources.MEMO},
    )
    customer_sources.apply_customer_values(
        customer,
        {"phone": format_phone(keys["phone_key"]) if keys["phone_key"] else None, "customer_number": customer_number},
        customer_sources.MEMO,
    )
    db.session.add(customer)
    db.session.flush()
    log_audit_event(AuditEventType.CUSTOMER_CREATED, user=user, details={"customer_id": customer.id, "source": customer_sources.MEMO})
    memo, _created, _filled = assign_memo(customer, transcript, "new_customer", user)
    return customer, memo
