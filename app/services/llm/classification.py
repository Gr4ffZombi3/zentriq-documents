"""Rollup-Logik: aggregiert die Leipziger-Liste-Zeilenflags auf Dokumentenebene,
damit z.B. `is_neugeschaeft` und `priority` als durchsuchbare Document-Spalten existieren."""

from app.models.enums import Priority
from app.services.llm.schemas import LeipzigerListeExtraction

_PRIORITY_ORDER = {Priority.LOW: 0, Priority.MEDIUM: 1, Priority.HIGH: 2}


def compute_document_flags(extraction: LeipzigerListeExtraction) -> dict:
    rows = extraction.rows
    if not rows:
        return {
            "is_neugeschaeft": False,
            "is_fahrzeugwechsel": False,
            "is_angebot": False,
            "cross_sell_opportunity": False,
            "has_multiple_products": False,
            "priority": Priority.MEDIUM,
            "recommended_next_action": None,
        }

    top_row = max(rows, key=lambda r: _PRIORITY_ORDER[r.priority])
    return {
        "is_neugeschaeft": any(r.is_neugeschaeft for r in rows),
        "is_fahrzeugwechsel": any(r.is_fahrzeugwechsel for r in rows),
        "is_angebot": any(r.is_angebot for r in rows),
        "cross_sell_opportunity": any(r.cross_sell_opportunity for r in rows),
        "has_multiple_products": any(r.has_multiple_products for r in rows),
        "priority": top_row.priority,
        "recommended_next_action": top_row.recommended_next_action,
    }
