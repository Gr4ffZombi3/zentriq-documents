"""Zentrale Normalisierung von Vermittlernummern.

Dieselbe Nummer taucht in unterschiedlichen Schreibweisen auf, z. B. "08/0950-T" (Leipziger
Liste), "080950-T" oder "08 0950 t" (Benutzereingabe). Verglichen wird deshalb immer ueber
`vermittlernummer_key()`, angezeigt und gespeichert ueber `format_vermittlernummer()`."""

import re

# HUK-Format: zweistellige Geschaeftsstelle, vierstellige Vermittlernummer, Pruefbuchstabe.
_HUK_PATTERN = re.compile(r"^(\d{1,2})/(\d{4})-?([A-Z])$")
_HUK_COMPACT_PATTERN = re.compile(r"^(\d{2})(\d{4})([A-Z])$")


def _cleaned(value) -> str:
    if not isinstance(value, str):
        return ""
    return re.sub(r"\s+", "", value).upper()


def _huk_parts(cleaned: str) -> tuple[str, str, str] | None:
    match = _HUK_PATTERN.match(cleaned.replace("-/", "/"))
    if match:
        return match.group(1).zfill(2), match.group(2), match.group(3)
    match = _HUK_COMPACT_PATTERN.match(re.sub(r"[^0-9A-Z]", "", cleaned))
    if match:
        return match.group(1), match.group(2), match.group(3)
    return None


def vermittlernummer_key(value) -> str | None:
    """Vergleichsschluessel: nur Ziffern und Grossbuchstaben ("08/0950-T" -> "080950T").
    Leere/ungueltige Werte ergeben None und passen damit zu nichts."""
    cleaned = _cleaned(value)
    if not cleaned:
        return None
    parts = _huk_parts(cleaned)
    if parts is not None:
        return "".join(parts)
    key = re.sub(r"[^0-9A-Z]", "", cleaned)
    return key or None


def format_vermittlernummer(value) -> str | None:
    """Einheitliche Darstellung. Erkannte HUK-Nummern als "08/0950-T", alles andere
    unveraendert (nur Leerraum entfernt), damit keine Information verloren geht."""
    cleaned = _cleaned(value)
    if not cleaned:
        return None
    parts = _huk_parts(cleaned)
    if parts is not None:
        return f"{parts[0]}/{parts[1]}-{parts[2]}"
    return value.strip()


def same_vermittlernummer(left, right) -> bool:
    left_key = vermittlernummer_key(left)
    return left_key is not None and left_key == vermittlernummer_key(right)
