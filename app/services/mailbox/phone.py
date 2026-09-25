import re

import phonenumbers
from phonenumbers import PhoneNumberType

PHONE_PATTERN = re.compile(r"(?<!\d)(?:\+49|0049|0)[\d\s()/.\-]{6,24}\d(?!\d)")
CALLBACK_MARKERS = (
    "rückrufnummer",
    "rueckrufnummer",
    "zurückrufen",
    "zurueckrufen",
    "rufen sie mich",
    "erreichen sie mich",
    "erreichbar unter",
    "meine nummer",
)
ALLOWED_PHONE_TYPES = {
    PhoneNumberType.FIXED_LINE,
    PhoneNumberType.MOBILE,
    PhoneNumberType.FIXED_LINE_OR_MOBILE,
    PhoneNumberType.VOIP,
}


def normalize_callback_phone(value: str | None) -> str | None:
    if not value:
        return None
    candidate = value.strip()
    if candidate.startswith("00"):
        candidate = "+" + candidate[2:]
    try:
        parsed = phonenumbers.parse(candidate, "DE")
    except phonenumbers.NumberParseException:
        return None
    if parsed.country_code != 49:
        return None
    if not phonenumbers.is_possible_number(parsed) or not phonenumbers.is_valid_number(parsed):
        return None
    if phonenumbers.number_type(parsed) not in ALLOWED_PHONE_TYPES:
        return None
    return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)


def extract_phone_candidates(text: str | None) -> list[str]:
    if not text:
        return []
    results: list[str] = []
    for match in PHONE_PATTERN.finditer(text):
        normalized = normalize_callback_phone(match.group(0))
        if normalized and normalized not in results:
            results.append(normalized)
    return results


def extract_explicit_callback_phone(transcript: str | None) -> str | None:
    if not transcript:
        return None
    lowered = transcript.lower()
    for match in PHONE_PATTERN.finditer(transcript):
        context_start = max(0, match.start() - 90)
        context_end = min(len(transcript), match.end() + 30)
        context = lowered[context_start:context_end]
        if any(marker in context for marker in CALLBACK_MARKERS):
            normalized = normalize_callback_phone(match.group(0))
            if normalized:
                return normalized
    return None


# Zusammenhaengende Ziffernfolge inkl. typischer Trennzeichen ("0521 / 12 34-567", "+49 (0) 521 ...").
DIGIT_RUN_PATTERN = re.compile(r"\+?\d[\d\s()/.\-]*\d")
# Erlaubte Praefixe vor der nationalen Rufnummer: "0", "+49"/"0049", "+49 (0)".
_NATIONAL_PREFIXES = {"0", "49", "0049", "490", "00490"}


def phone_appears_in_text(phone: str | None, text: str | None) -> bool:
    """Prueft, ob eine (z. B. von der KI gelieferte) Rufnummer tatsaechlich woertlich im Text
    vorkommt. Verglichen werden nur Ziffern innerhalb EINER zusammenhaengenden Nummernangabe -
    so koennen keine Ziffern aus verschiedenen Zahlen zu einer Treffernummer zusammengesetzt
    werden."""
    normalized = normalize_callback_phone(phone)
    if not normalized or not text:
        return False
    national_number = str(phonenumbers.parse(normalized, None).national_number)
    for match in DIGIT_RUN_PATTERN.finditer(text):
        digits = re.sub(r"\D", "", match.group(0))
        if digits.endswith(national_number) and digits[: -len(national_number)] in _NATIONAL_PREFIXES:
            return True
    return False


def split_huk_phone(value: str) -> tuple[str, str]:
    normalized = normalize_callback_phone(value)
    if not normalized:
        raise ValueError("Keine gültige deutsche Rückrufnummer.")
    parsed = phonenumbers.parse(normalized, None)
    national_number = str(parsed.national_number)
    area_length = phonenumbers.length_of_geographical_area_code(parsed)
    if not area_length:
        area_length = phonenumbers.length_of_national_destination_code(parsed)
    if not area_length or area_length >= len(national_number):
        raise ValueError("Vorwahl und Rufnummer konnten nicht eindeutig getrennt werden.")
    return "0" + national_number[:area_length], national_number[area_length:]

