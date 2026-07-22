from __future__ import annotations

import re
import unicodedata


def normalize_customer_name(value: str | None) -> str:
    if not value:
        return ""
    replacements = {
        "Ã¤": "ae",
        "Ã¶": "oe",
        "Ã¼": "ue",
        "ÃŸ": "ss",
        "Ã„": "ae",
        "Ã–": "oe",
        "Ãœ": "ue",
    }
    for source, target in replacements.items():
        value = value.replace(source, target)
    normalized = unicodedata.normalize("NFKD", value)
    without_accents = "".join(character for character in normalized if not unicodedata.combining(character))
    lowered = without_accents.lower()
    lowered = re.sub(r"[^a-z0-9\s]", " ", lowered)
    lowered = re.sub(r"\s+", " ", lowered)
    return lowered.strip()


def normalize_postal_code(value: str | None) -> str:
    if not value:
        return ""
    return re.sub(r"\s+", "", value).lower()
