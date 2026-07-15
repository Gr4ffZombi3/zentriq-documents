import json

from flask import current_app

from app.services.llm.client import get_openai_client
from app.services.llm.schemas import DocumentExtraction, LeipzigerListeExtraction

EXTRACTION_SYSTEM_PROMPT = """Du analysierst deutsche Versicherungs- und Kfz-Dokumente \
(Rechnungen, Gutachten, Versicherungsunterlagen, Schadenakten, Briefe, HUK-Listen, Leipziger \
Listen). Extrahiere alle relevanten Informationen strukturiert und antworte AUSSCHLIESSLICH mit \
einem JSON-Objekt in genau diesem Format:

{
  "doc_type": "leipziger_liste" | "huk_liste" | "gutachten" | "rechnung" | "versicherungsunterlagen" | "schadenakte" | "brief" | "sonstiges",
  "customer": {"name": str, "address": str|null, "city": str|null, "postal_code": str|null, "date_of_birth": "YYYY-MM-DD"|null} | null,
  "vehicle": str|null,
  "license_plate": str|null,
  "insurer": str|null,
  "contract_number": str|null,
  "case_number": str|null,
  "broker": str|null,
  "contract_start_date": "YYYY-MM-DD"|null,
  "products": [str],
  "special_notes": str|null,
  "broker_number": str|null,
  "product_line": str|null,
  "premium": str|null,
  "tariff": str|null
}

"broker_number" ist die im Dokument aufgedruckte Vermittlernummer (VM-Nummer), NICHT der \
Vermittlername (das ist "broker"). "product_line" ist die Versicherungssparte (z.B. "KFZ", \
"Sach", "Leben"), nicht der konkrete Produktname. "premium" ist der Beitrag/die Praemie als \
Text genau wie im Dokument geschrieben (z.B. "123,45 EUR"), keine eigene Umrechnung. "tariff" \
ist die Tarifbezeichnung.

Erfinde keine Werte. Wenn eine Information im Text nicht vorhanden ist, setze das Feld auf null \
bzw. eine leere Liste. Antworte NUR mit dem JSON-Objekt, ohne zusätzlichen Text."""

LEIPZIGER_LISTE_SYSTEM_PROMPT = """Du analysierst eine deutsche "Leipziger Liste" - eine Tabelle \
mit mehreren Kundenzeilen (Bestandsuebersicht eines Versicherungsvermittlers). Extrahiere JEDE \
Zeile als eigenes Objekt und antworte AUSSCHLIESSLICH mit einem JSON-Objekt in genau diesem Format:

{
  "rows": [
    {
      "customer": {"name": str, "address": str|null, "city": str|null, "postal_code": str|null, "date_of_birth": "YYYY-MM-DD"|null},
      "vehicle": str|null,
      "license_plate": str|null,
      "insurer": str|null,
      "contract_number": str|null,
      "products": [str],
      "is_neugeschaeft": bool,
      "is_fahrzeugwechsel": bool,
      "is_angebot": bool,
      "is_storno": bool,
      "cross_sell_opportunity": bool,
      "has_multiple_products": bool,
      "priority": "low" | "medium" | "high",
      "recommended_next_action": str|null,
      "special_notes": str|null,
      "broker_number": str|null,
      "product_line": str|null,
      "premium": str|null,
      "tariff": str|null
    }
  ]
}

Setze "is_neugeschaeft" bei neu abgeschlossenen Vertraegen, "is_fahrzeugwechsel" bei erkennbarem \
Fahrzeugwechsel, "is_angebot" bei offenen Angeboten, "is_storno" bei erkennbar stornierten oder \
gekuendigten Vertraegen (z.B. Storno-Spalte, Vermerk "storniert"/"gekuendigt"), \
"cross_sell_opportunity" wenn der Kunde \
sinnvoll weitere Produkte angeboten bekommen koennte, und "has_multiple_products" wenn der Kunde \
bereits mehrere Produkte hat. "broker_number" ist die pro Zeile aufgedruckte Vermittlernummer, \
"product_line" die Versicherungssparte dieser Zeile, "premium" der Beitrag/die Praemie als Text \
genau wie im Dokument, "tariff" die Tarifbezeichnung. Erfinde keine Werte. Antworte NUR mit dem \
JSON-Objekt."""


def extract_document_data(raw_text: str) -> DocumentExtraction:
    client = get_openai_client()
    response = client.chat.completions.create(
        model=current_app.config["OPENAI_MODEL"],
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": EXTRACTION_SYSTEM_PROMPT},
            {"role": "user", "content": raw_text},
        ],
    )
    data = json.loads(response.choices[0].message.content)
    return DocumentExtraction.model_validate(data)


def extract_leipziger_liste_rows(raw_text: str) -> LeipzigerListeExtraction:
    client = get_openai_client()
    response = client.chat.completions.create(
        model=current_app.config["OPENAI_MODEL"],
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": LEIPZIGER_LISTE_SYSTEM_PROMPT},
            {"role": "user", "content": raw_text},
        ],
    )
    data = json.loads(response.choices[0].message.content)
    return LeipzigerListeExtraction.model_validate(data)
