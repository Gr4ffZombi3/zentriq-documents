import json

from flask import current_app

from app.services.llm.client import get_openai_client
from app.services.llm.schemas import DocumentExtraction

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
  "special_notes": str|null
}

Erfinde keine Werte. Wenn eine Information im Text nicht vorhanden ist, setze das Feld auf null \
bzw. eine leere Liste. Antworte NUR mit dem JSON-Objekt, ohne zusätzlichen Text."""


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
