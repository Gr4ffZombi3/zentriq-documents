"""Rein deterministische Einstufung eines Leipziger-Liste-Datensatzes (M13). KEINE
KI-Entscheidung, KEINE Prognose, KEINE Vermutung - ausschliesslich die im Dokument tatsaechlich
erkannten Felder (is_storno, contract_start_date, has_antrag, is_angebot). Beide Funktionen
arbeiten auf einem plain dict (ein Eintrag aus DocumentCustomer.row_data), nicht auf dem
Pydantic-Modell, da der Konsument (leipziger_liste_view.py) immer mit bereits persistiertem
JSON arbeitet.

Prioritaetsreihenfolge (deckt jede Regel aus dem Briefing ab):
    is_storno=True                                   -> STORNIERT (Briefing: "separat anzeigen",
                                                         nicht in "erledigt" versteckt - deshalb
                                                         vor ABGESCHLOSSEN geprueft)
    contract_start_date vorhanden                     -> ABGESCHLOSSEN
    has_antrag=True (kein Beginn)                     -> PRUEFEN
    is_angebot=True (kein Beginn, kein Antrag)         -> NUR_ANGEBOT
    sonst                                              -> OFFENER_VORGANG

Drei der acht Beispielkategorien aus dem Briefing ("Unterlagen fehlen", "Bearbeitung offen",
"Rueckfrage erforderlich") haben kein verlaessliches, bereits extrahiertes Signal in der
aktuellen Datenlage - sie werden bewusst in OFFENER_VORGANG zusammengefasst statt eine
unzuverlaessige Freitext-Heuristik zu erfinden (siehe docs/M13_COMPLETION_REPORT.md)."""

from app.models.enums import PotentialCategory


def classify_row(row: dict) -> PotentialCategory:
    if row.get("is_storno"):
        return PotentialCategory.STORNIERT
    if row.get("contract_start_date"):
        return PotentialCategory.ABGESCHLOSSEN
    if row.get("has_antrag"):
        return PotentialCategory.PRUEFEN
    if row.get("is_angebot"):
        return PotentialCategory.NUR_ANGEBOT
    return PotentialCategory.OFFENER_VORGANG


def explain_category(row: dict, category: PotentialCategory) -> str:
    """Feste Begruendungs-Templates, teilweise woertlich aus dem Briefing uebernommen -
    kein GPT-Call, ausschliesslich Felder, die tatsaechlich extrahiert wurden."""
    if category == PotentialCategory.STORNIERT:
        return "Datensatz wurde als storniert oder gekündigt erkannt."
    if category == PotentialCategory.ABGESCHLOSSEN:
        start_date = row.get("contract_start_date")
        if start_date:
            return f"Versicherungsbeginn am {start_date} erkannt. Datensatz gilt als abgeschlossen."
        return "Versicherungsbeginn erkannt. Datensatz gilt als abgeschlossen."
    if category == PotentialCategory.PRUEFEN:
        return "Antrag vorhanden, aber kein Beginn-Datum erkannt."
    if category == PotentialCategory.NUR_ANGEBOT:
        return "Kunde erscheint in der Liste als Angebot. Es wurde kein Versicherungsbeginn gefunden."
    return "Datensatz enthält keinen Hinweis auf einen Abschluss."
