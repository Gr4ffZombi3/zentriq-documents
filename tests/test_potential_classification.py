from app.models.enums import PotentialCategory
from app.services.analysis.potential_classification import classify_row, explain_category


def test_storno_wins_even_with_start_date():
    # Laut Briefing soll Storno "separat angezeigt" werden, nicht in "erledigt" verschwinden.
    row = {"is_storno": True, "contract_start_date": "2026-01-15"}
    assert classify_row(row) == PotentialCategory.STORNIERT


def test_start_date_without_storno_is_abgeschlossen():
    row = {"is_storno": False, "contract_start_date": "2026-01-15"}
    assert classify_row(row) == PotentialCategory.ABGESCHLOSSEN


def test_antrag_without_start_date_is_pruefen():
    row = {"contract_start_date": None, "has_antrag": True}
    assert classify_row(row) == PotentialCategory.PRUEFEN


def test_angebot_without_start_date_or_antrag_is_nur_angebot():
    row = {"contract_start_date": None, "has_antrag": False, "is_angebot": True}
    assert classify_row(row) == PotentialCategory.NUR_ANGEBOT


def test_nothing_present_is_offener_vorgang():
    row = {"contract_start_date": None, "has_antrag": False, "is_angebot": False}
    assert classify_row(row) == PotentialCategory.OFFENER_VORGANG


def test_empty_row_does_not_crash_and_is_offener_vorgang():
    assert classify_row({}) == PotentialCategory.OFFENER_VORGANG


def test_antrag_and_angebot_both_true_pruefen_wins_tie_break():
    # Ein Antrag ist im Verkaufsprozess weiter fortgeschritten als ein reines Angebot -
    # bewusster Tie-Break zwischen zwei explizit im Briefing genannten Regeln.
    row = {"contract_start_date": None, "has_antrag": True, "is_angebot": True}
    assert classify_row(row) == PotentialCategory.PRUEFEN


def test_explain_nur_angebot_matches_briefing_example():
    row = {"is_angebot": True}
    text = explain_category(row, PotentialCategory.NUR_ANGEBOT)
    assert text == "Kunde erscheint in der Liste als Angebot. Es wurde kein Versicherungsbeginn gefunden."


def test_explain_pruefen_matches_briefing_example():
    row = {"has_antrag": True}
    text = explain_category(row, PotentialCategory.PRUEFEN)
    assert text == "Antrag vorhanden, aber kein Beginn-Datum erkannt."


def test_explain_offener_vorgang_matches_briefing_example():
    text = explain_category({}, PotentialCategory.OFFENER_VORGANG)
    assert text == "Datensatz enthält keinen Hinweis auf einen Abschluss."


def test_explain_abgeschlossen_mentions_the_actual_date():
    row = {"contract_start_date": "2026-03-01"}
    text = explain_category(row, PotentialCategory.ABGESCHLOSSEN)
    assert "2026-03-01" in text


def test_explain_storniert_is_sensible():
    text = explain_category({"is_storno": True}, PotentialCategory.STORNIERT)
    assert "storniert" in text.lower() or "gekündigt" in text.lower()
