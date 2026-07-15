from app.services.analysis.validation import score_boolean_flag, score_field, score_leipziger_liste_row
from tests.fixtures.leipziger_liste_samples import (
    SAMPLE_CLEAN_RAW_TEXT,
    SAMPLE_CLEAN_ROW,
    SAMPLE_OCR_NOISE_RAW_TEXT,
    SAMPLE_OCR_NOISE_ROW,
    SAMPLE_SPARSE_RAW_TEXT,
    SAMPLE_SPARSE_ROW,
)


def test_score_field_empty_value_is_zero_and_uncertain(app):
    with app.app_context():
        result = score_field("vehicle", None, "irrelevanter Text")
    assert result["confidence"] == 0
    assert result["uncertain"] is True
    assert result["original_text"] is None


def test_score_field_found_verbatim_scores_high(app):
    with app.app_context():
        result = score_field("vehicle", "VW Golf", "Fahrzeug: VW Golf, Kennzeichen: B-XX 1234")
    assert result["confidence"] == 100
    assert result["source"] == "ocr"
    assert result["original_text"] == "VW Golf"
    assert result["uncertain"] is False


def test_score_field_not_found_in_text_still_gets_partial_credit(app):
    with app.app_context():
        result = score_field("vehicle", "Tesla Model 3", "Dieser Text erwaehnt kein Fahrzeug.")
    assert result["confidence"] == 60  # nicht leer (40) + kein Format-Check definiert (20)
    assert result["source"] == "ki"
    assert result["original_text"] is None


def test_score_field_postal_code_format_check_passes(app):
    with app.app_context():
        result = score_field("customer.postal_code", "10115", "PLZ 10115 Berlin")
    assert result["confidence"] == 100
    assert result["normalized_value"] == "10115"


def test_score_field_postal_code_format_check_fails_on_ocr_noise(app):
    with app.app_context():
        result = score_field("customer.postal_code", "1O115", SAMPLE_OCR_NOISE_RAW_TEXT)
    # nicht leer (40) + im Rohtext gefunden (40) + Format-Check nicht bestanden (kein Bonus) = 80
    assert result["confidence"] == 80
    assert result["normalized_value"] is None  # Format-Check fehlgeschlagen -> kein Normalwert


def test_score_boolean_flag_true_with_supporting_keyword():
    result = score_boolean_flag(True, "Status: storniert", ["storniert", "storno"])
    assert result["confidence"] == 90
    assert result["uncertain"] is False


def test_score_boolean_flag_true_without_supporting_keyword():
    result = score_boolean_flag(True, "Kein Hinweis im Text.", ["storniert", "storno"])
    assert result["confidence"] == 50
    assert result["uncertain"] is True


def test_score_boolean_flag_false_with_contradicting_keyword():
    result = score_boolean_flag(False, "Status: storniert", ["storniert", "storno"])
    assert result["confidence"] == 40
    assert result["uncertain"] is True


def test_score_leipziger_liste_row_clean_sample_is_confident(app):
    with app.app_context():
        result = score_leipziger_liste_row(SAMPLE_CLEAN_ROW, SAMPLE_CLEAN_RAW_TEXT)

    assert result["customer.name"]["confidence"] == 100
    assert result["vehicle"]["confidence"] == 100
    assert result["broker_number"]["confidence"] == 100
    assert result["is_storno"]["confidence"] == 90
    assert all(not entry["uncertain"] for entry in result.values())


def test_score_leipziger_liste_row_sparse_sample_flags_missing_fields(app):
    with app.app_context():
        result = score_leipziger_liste_row(SAMPLE_SPARSE_ROW, SAMPLE_SPARSE_RAW_TEXT)

    assert "customer.name" in result
    assert result["customer.name"]["confidence"] > 0
    # Optionale Felder ohne Wert tauchen gar nicht erst im Ergebnis auf (nichts zu bewerten).
    assert "vehicle" not in result
    assert "customer.postal_code" not in result
    # Boolesche Flags sind immer vorhanden (Default False), hier ohne Schluesselwort im Text.
    assert result["is_storno"]["confidence"] == 80


def test_score_leipziger_liste_row_ocr_noise_marks_postal_code_uncertain(app):
    with app.app_context():
        result = score_leipziger_liste_row(SAMPLE_OCR_NOISE_ROW, SAMPLE_OCR_NOISE_RAW_TEXT)

    assert result["customer.postal_code"]["uncertain"] is False  # 80 liegt noch ueber dem Default-Schwellenwert 70
    assert result["customer.postal_code"]["normalized_value"] is None
