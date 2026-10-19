"""Pseudonymisierung fuer KI-Anbieter ohne Kundendaten-Freigabe.

Personenbezogene Angaben werden vor der Modellanfrage durch stabile Platzhalter ersetzt
("[Kunde 1]", "[Vertrag 2]" ...) und nach der Antwort lokal wieder eingesetzt. Das Modell sieht
die Struktur (Anzahl, Status, Prioritaet), nie die Personen.

- Werkzeugergebnisse: feldgenau ueber PERSONAL_FIELDS (zuverlaessig, da strukturiert).
- Freitext (Benutzernachricht, Transkripte): ueber den bestehenden Anonymisierer
  (app/services/anonymize.py), dessen Fundstellen ebenfalls zurueckgesetzt werden.
- Werkzeug-Argumente des Modells (z. B. suche("[Kunde 1]")) werden vor der Ausfuehrung lokal
  wieder aufgeloest - der Anbieter erfaehrt den echten Wert nie.
"""

from __future__ import annotations

import re

# Feldname im Werkzeugergebnis -> Platzhalter-Kategorie
PERSONAL_FIELDS = {
    "kunde": "Kunde",
    "name": "Person",
    "mitarbeiter": "Person",
    "zustaendig": "Person",
    "von": "Person",
    "telefon": "Telefon",
    "kundennummer": "Kundennummer",
    "vertrag": "Vertrag",
    "ort": "Ort",
    "transkript": None,  # Freitext -> Anonymisierer
    "titel": None,
}

# Kategorien des bestehenden Anonymisierers -> einheitliche Platzhalter (ohne Leerzeichen).
ANON_LABELS = {
    "email": "E-Mail", "iban": "IBAN", "birthdate": "Geburtsdatum", "customer_number": "Kundennummer",
    "contract_number": "Vertrag", "claim_number": "Schaden", "identifier": "Kennung", "plate": "Kennzeichen",
    "phone": "Telefon", "address": "Anschrift", "employee": "Person", "name": "Kunde",
}

_PLACEHOLDER = re.compile(r"\[[A-Za-zÄÖÜäöüß\-]+(?: \d+)?\]")


class Pseudonymizer:
    def __init__(self, known_names: dict[str, str] | None = None):
        self._by_value: dict[tuple[str, str], str] = {}
        self._by_placeholder: dict[str, str] = {}
        self._counts: dict[str, int] = {}
        self._known_names = known_names or {}

    def _placeholder(self, category: str, value: str) -> str:
        key = (category, value.strip().casefold())
        if key not in self._by_value:
            self._counts[category] = self._counts.get(category, 0) + 1
            placeholder = f"[{category} {self._counts[category]}]"
            self._by_value[key] = placeholder
            self._by_placeholder[placeholder] = value
        return self._by_value[key]

    def mask_value(self, value, category: str):
        if value is None or value == "" or not isinstance(value, (str, int)):
            return value
        return self._placeholder(category, str(value))

    def mask_text(self, text: str) -> str:
        """Freitext ueber den bestehenden Anonymisierer; Fundstellen werden zuruecksetzbar."""
        if not text:
            return text
        from app.services.anonymize import anonymize

        result = anonymize(text, self._known_names)
        masked = result.text
        for finding in result.findings:
            ours = self._placeholder(ANON_LABELS.get(finding.category, "Angabe"), finding.original)
            masked = masked.replace(finding.placeholder, ours)
        return masked

    def mask_data(self, data):
        """Werkzeugergebnis rekursiv: bekannte Personenfelder ersetzen."""
        if isinstance(data, dict):
            out = {}
            for key, value in data.items():
                if key in PERSONAL_FIELDS and isinstance(value, str):
                    category = PERSONAL_FIELDS[key]
                    out[key] = self.mask_text(value) if category is None else self.mask_value(value, category)
                else:
                    out[key] = self.mask_data(value)
            return out
        if isinstance(data, list):
            return [self.mask_data(item) for item in data]
        return data

    def unmask(self, text):
        if isinstance(text, dict):
            return {k: self.unmask(v) for k, v in text.items()}
        if isinstance(text, list):
            return [self.unmask(v) for v in text]
        if not isinstance(text, str) or not self._by_placeholder:
            return text
        return _PLACEHOLDER.sub(lambda m: self._by_placeholder.get(m.group(0), m.group(0)), text)

    @property
    def active(self) -> bool:
        return bool(self._by_placeholder)
