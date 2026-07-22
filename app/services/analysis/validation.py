"""Deterministische Plausibilitaetspruefung ("Datenvalidierung"-Stufe der Pipeline) statt
GPT-Selbsteinschaetzung: Modelle "raten" bei Selbsteinschaetzung oft hohe Werte, ein
regelbasierter Check ist zuverlaessiger. Jedes Textfeld bekommt einen 0-100-Score aus
nicht-leer (+40), Fuzzy-Match im OCR-Rohtext (+40) und einem Format-Check wo anwendbar
(+20, sonst automatisch gewaehrt). Boolesche Flags (is_storno etc.) werden separat ueber
Schluesselwort-Praesenz im Rohtext bewertet, da sie kein "im Text gefunden"-Konzept haben."""

import re
from difflib import SequenceMatcher

from flask import current_app

from app.services.llm.schemas import DocumentExtraction, ExtractedCustomer, LeipzigerListeRow

POSTAL_CODE_PATTERN = re.compile(r"^\d{5}$")
FUZZY_MATCH_THRESHOLD = 0.8

BOOLEAN_FLAG_KEYWORDS: dict[str, list[str]] = {
    "is_storno": ["storniert", "storno", "gekündigt", "gekuendigt"],
    "is_neugeschaeft": ["neugeschäft", "neugeschaeft", "neu abgeschlossen", "neuvertrag"],
    "is_fahrzeugwechsel": ["fahrzeugwechsel", "kennzeichenwechsel"],
    "is_angebot": ["angebot"],
    "has_antrag": ["antrag"],
}

CUSTOMER_FIELDS = ("name", "address", "city", "postal_code")
DOCUMENT_EXTRACTION_FIELDS = (
    "vehicle", "license_plate", "insurer", "contract_number", "case_number", "broker",
    "products", "special_notes", "broker_number", "product_line", "premium", "tariff",
)
LEIPZIGER_LISTE_ROW_FIELDS = (
    "vehicle", "license_plate", "insurer", "contract_number", "products", "special_notes",
    "broker_number", "product_line", "premium", "tariff", "contract_start_date", "status_code",
)


def _normalize(text: str) -> str:
    return " ".join(text.lower().split())


def _fuzzy_found_in(value: str, raw_text: str) -> bool:
    normalized_value = _normalize(value)
    normalized_raw = _normalize(raw_text)
    if not normalized_value:
        return False
    if normalized_value in normalized_raw:
        return True

    window = len(normalized_value)
    if window == 0 or len(normalized_raw) < window:
        return False
    step = max(1, window // 4)
    for start in range(0, len(normalized_raw) - window + 1, step):
        candidate = normalized_raw[start : start + window]
        if SequenceMatcher(None, normalized_value, candidate).ratio() >= FUZZY_MATCH_THRESHOLD:
            return True
    return False


def _format_check(field_name: str) -> str | None:
    """Name des Format-Checks fuer dieses Feld, oder None wenn keiner definiert ist
    (der Format-Bonus wird dann automatisch gewaehrt)."""
    if field_name in ("customer.postal_code", "postal_code"):
        return "postal_code"
    return None


def _passes_format_check(check_name: str, value: str) -> bool:
    if check_name == "postal_code":
        return bool(POSTAL_CODE_PATTERN.match(value.strip()))
    return True


def score_field(field_name: str, value, raw_text: str) -> dict:
    threshold = current_app.config["FIELD_CONFIDENCE_UNCERTAIN_THRESHOLD"]

    if value is None or (isinstance(value, str) and not value.strip()):
        return {"confidence": 0, "source": "ki", "original_text": None, "normalized_value": None, "uncertain": True}

    str_value = str(value)
    score = 40  # nicht leer

    found = _fuzzy_found_in(str_value, raw_text)
    if found:
        score += 40

    check_name = _format_check(field_name)
    format_ok = _passes_format_check(check_name, str_value) if check_name else True
    if format_ok:
        score += 20

    return {
        "confidence": score,
        "source": "ocr" if found else "ki",
        "original_text": str_value if found else None,
        "normalized_value": str_value.strip() if format_ok else None,
        "uncertain": score < threshold,
    }


def score_boolean_flag(value: bool, raw_text: str, keywords: list[str]) -> dict:
    threshold = current_app.config["FIELD_CONFIDENCE_UNCERTAIN_THRESHOLD"]
    normalized_raw = _normalize(raw_text)
    keyword_found = any(_normalize(kw) in normalized_raw for kw in keywords)

    if value and keyword_found:
        score = 90
    elif value and not keyword_found:
        score = 50  # KI meldet True, aber kein stuetzendes Schluesselwort im Rohtext
    elif not value and not keyword_found:
        score = 80
    else:
        score = 40  # False, aber Schluesselwort trotzdem vorhanden - potenziell falsch negativ

    return {
        "confidence": score,
        "source": "ki",
        "original_text": None,
        "normalized_value": value,
        "uncertain": score < threshold,
    }


def _score_customer(customer: ExtractedCustomer, raw_text: str) -> dict[str, dict]:
    result = {}
    for field_name in CUSTOMER_FIELDS:
        value = getattr(customer, field_name)
        if value:
            result[f"customer.{field_name}"] = score_field(f"customer.{field_name}", value, raw_text)
    return result


def _joined(value):
    return ", ".join(value) if isinstance(value, list) else value


def score_extraction(extraction: DocumentExtraction, raw_text: str) -> dict[str, dict]:
    result: dict[str, dict] = {}
    if extraction.customer is not None:
        result.update(_score_customer(extraction.customer, raw_text))
    for field_name in DOCUMENT_EXTRACTION_FIELDS:
        value = _joined(getattr(extraction, field_name))
        if value:
            result[field_name] = score_field(field_name, value, raw_text)
    return result


def score_leipziger_liste_row(row: LeipzigerListeRow, raw_text: str) -> dict[str, dict]:
    result = _score_customer(row.customer, raw_text)
    for field_name in LEIPZIGER_LISTE_ROW_FIELDS:
        value = _joined(getattr(row, field_name))
        if value:
            result[field_name] = score_field(field_name, value, raw_text)
    for flag_name, keywords in BOOLEAN_FLAG_KEYWORDS.items():
        result[flag_name] = score_boolean_flag(getattr(row, flag_name), raw_text, keywords)
    return result
