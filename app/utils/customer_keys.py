"""Vergleichsschluessel fuer Kundendaten (Telefon, Kundennummer, Name).

Werden als indizierte Spalten am Kunden gespeichert (siehe app/models/customer.py) und von der
Dubletten-Erkennung und der Memo-Kundenerkennung gleichermassen verwendet - so muss fuer einen
Abgleich nie der gesamte Kundenbestand geladen werden."""

from __future__ import annotations

import re

from app.services.customer_normalization import normalize_customer_name


def normalize_phone(raw: str | None) -> str | None:
    """Einheitliche Form "+49..." fuer deutsche Schreibweisen:
    0171 1234567, 01711234567, +49 171 1234567, 0049 171 1234567, +49 (0)171 1234567."""
    if not raw:
        return None
    text = raw.strip()
    has_plus = text.startswith("+")
    digits = re.sub(r"\D", "", text)
    if has_plus:
        pass
    elif digits.startswith("00"):
        digits = digits[2:]
    elif digits.startswith("0"):
        digits = "49" + digits[1:]
    else:
        # Ohne Vorwahl ist eine Nummer nicht eindeutig zuzuordnen.
        return None
    if digits.startswith("490"):
        digits = "49" + digits[3:]
    if not 9 <= len(digits) <= 15:
        return None
    return "+" + digits


def format_phone(normalized: str) -> str:
    """Lesbare Anzeige deutscher Nummern als 0171 1234567."""
    if normalized.startswith("+49"):
        national = "0" + normalized[3:]
        return f"{national[:4]} {national[4:]}" if national.startswith("01") else national
    return normalized


def normalize_customer_number(raw: str | None) -> str:
    return re.sub(r"[^0-9A-Z]", "", (raw or "").upper())


_UMLAUT_SPELLING = re.compile(r"([aou])e")


def name_key(raw: str | None) -> str:
    """Name ohne Akzente/Satzzeichen, Bestandteile sortiert: "Mustermann, Max" und
    "Max Mustermann" ergeben denselben Schluessel. Einzelbuchstaben zaehlen nicht.

    Umlaute werden auf den Grundbuchstaben gefaltet: "Löwenstein", "Loewenstein" und
    "Lowenstein" (OCR verliert die Punkte) ergeben denselben Schluessel. Ein gleicher Name
    allein fuehrt nirgends zu einer Zuordnung - dafuer braucht es immer weitere Merkmale."""
    return " ".join(sorted(name_parts(raw)))


def name_parts(raw: str | None) -> list[str]:
    """Bestandteile eines Namens bzw. Textes in derselben Form wie im name_key."""
    return [_UMLAUT_SPELLING.sub(r"\1", part) for part in normalize_customer_name(raw).split() if len(part) >= 2]


def customer_keys(name: str | None, phone: str | None, customer_number: str | None) -> dict:
    return {
        "name_key": name_key(name)[:255] or None,
        "phone_key": normalize_phone(phone),
        "customer_number_key": normalize_customer_number(customer_number)[:50] or None,
    }
