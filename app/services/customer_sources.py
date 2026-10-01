"""Datenherkunft im Zentriq-Kundenstamm.

Jeder Kunde merkt sich, woher er stammt (Customer.source) und je Feld, woher der aktuelle Wert
kommt (Customer.field_sources, z. B. {"phone": "MEMO"}). Bewusst keine Versionierung: es zaehlt
nur die Herkunft des aktuell gespeicherten Werts."""

from __future__ import annotations

LEIPZIGER_LISTE = "LEIPZIGER_LISTE"
MEMO = "MEMO"
MANUELL = "MANUELL"
# Einzeldokumente der fruehen Analyse-Pipeline (keine Leipziger Liste).
DOKUMENT = "DOKUMENT"

LABELS = {LEIPZIGER_LISTE: "Leipziger Liste", MEMO: "Memo", MANUELL: "Manuell", DOKUMENT: "Dokument"}


def _empty(value) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def apply_customer_values(customer, values: dict, source: str, *, overwrite: bool = False) -> list[str]:
    """Uebernimmt `values` in den Kunden und vermerkt die Herkunft je Feld.

    Leere Werte ueberschreiben nie vorhandene Daten. Gefuellte Felder werden nur mit
    `overwrite=True` ersetzt (Leipziger Liste: die neueste Liste ist der aktuelle Stand).
    Liefert die geaenderten Felder."""
    changed = []
    sources = dict(customer.field_sources or {})
    for field, value in values.items():
        if _empty(value):
            continue
        current = getattr(customer, field)
        if current == value or (not _empty(current) and not overwrite):
            continue
        setattr(customer, field, value)
        sources[field] = source
        changed.append(field)
    if changed:
        # Neues dict: JSON-Spalten erkennen nur Zuweisungen als Aenderung.
        customer.field_sources = sources
    if not customer.source:
        customer.source = source
    return changed
