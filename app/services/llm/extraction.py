import json
from collections import defaultdict
from collections.abc import Iterable

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
bzw. eine leere Liste. Antworte NUR mit dem JSON-Objekt, ohne zusaetzlichen Text."""

LEIPZIGER_LISTE_SYSTEM_PROMPT = """Du analysierst eine deutsche "Leipziger Liste" - eine Tabelle \
mit mehreren Kunden- und Vertragszeilen. Extrahiere JEDE relevante Vertragszeile als eigenes \
Objekt. WICHTIG:

- KEINE kuenstliche Obergrenze. Gib ALLE erkennbaren Vertragszeilen zurueck, nicht nur die ersten 5.
- Wenn derselbe Kunde mehrere Vertraege oder Sparten hat, gib fuer JEDE Vertragszeile ein eigenes Objekt aus.
- Wenn ein Beginn-Datum vorhanden ist, ist das KEIN offener Vorgang ohne Beginn.
- "status_code" soll den rohen Tabellenstatus liefern, falls er direkt lesbar ist (z.B. ANG, NEU, FZW).
- "source_page" muss die absolute Seitenzahl aus dem bereitgestellten Seitenmarker uebernehmen.
- "source_row" ist die Laufnummer der Vertragszeile innerhalb dieser Seite.

Antworte AUSSCHLIESSLICH mit einem JSON-Objekt in genau diesem Format:

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
      "tariff": str|null,
      "contract_start_date": "YYYY-MM-DD"|null,
      "has_antrag": bool,
      "source_page": int|null,
      "source_row": int|null,
      "status_code": str|null
    }
  ]
}

Setze "is_neugeschaeft" bei neu abgeschlossenen oder klar als NEU markierten Vertraegen, \
"is_fahrzeugwechsel" bei erkennbarem Fahrzeugwechsel oder Status FZW, "is_angebot" bei offenen \
Angeboten oder Status ANG, "is_storno" bei erkennbar stornierten oder gekuendigten Vertraegen, \
"cross_sell_opportunity" wenn der Kunde sinnvoll weitere Produkte angeboten bekommen koennte, \
und "has_multiple_products" wenn der Kunde bereits mehrere Produkte hat. "broker_number" ist \
die pro Zeile aufgedruckte Vermittlernummer, "product_line" die Versicherungssparte dieser \
Zeile, "premium" der Beitrag/die Praemie als Text genau wie im Dokument, "tariff" die \
Tarifbezeichnung. "contract_start_date" ist das Versicherungsbeginn-Datum ("Beginn") dieser \
Zeile, NICHT das Antragsdatum - nur setzen, wenn ein konkretes Beginn-Datum im Dokument \
erkennbar ist. "has_antrag" ist true, wenn fuer diese Zeile erkennbar ein Versicherungsantrag \
vorliegt. Erfinde keine Werte. Antworte NUR mit dem JSON-Objekt."""


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


def extract_leipziger_liste_rows(raw_text: str | list[str]) -> LeipzigerListeExtraction:
    if isinstance(raw_text, str):
        extraction = _extract_leipziger_liste_batch(raw_text)
        extraction.analysis_meta = {
            "total_pages": 1,
            "processed_pages": 1,
            "processed_page_numbers": [1],
            "failed_pages": [],
            "failed_page_count": 0,
            "raw_row_count": len(extraction.rows),
            "batch_size": 1,
        }
        return extraction

    page_texts = list(raw_text)
    total_pages = len(page_texts)
    batch_size = max(1, int(current_app.config.get("LEIPZIGER_LISTE_PAGE_BATCH_SIZE", 1)))

    combined_rows = []
    failed_pages: list[int] = []
    processed_pages: list[int] = []
    row_counters_by_page: dict[int, int] = defaultdict(int)

    for batch_start in range(0, total_pages, batch_size):
        batch_page_numbers = list(range(batch_start + 1, min(total_pages, batch_start + batch_size) + 1))
        batch_pages = [
            (page_number, page_texts[page_number - 1])
            for page_number in batch_page_numbers
            if page_texts[page_number - 1].strip()
        ]
        if not batch_pages:
            processed_pages.extend(batch_page_numbers)
            continue

        try:
            extraction = _extract_leipziger_liste_batch(_format_page_batch(batch_pages))
        except Exception:
            failed_pages.extend(batch_page_numbers)
            continue

        processed_pages.extend(batch_page_numbers)
        for row in extraction.rows:
            page_number = _coerce_absolute_page(row.source_page, batch_page_numbers)
            row.source_page = page_number
            row_counters_by_page[page_number] += 1
            row.source_row = row.source_row or row_counters_by_page[page_number]
            combined_rows.append(row)

    return LeipzigerListeExtraction(
        rows=combined_rows,
        analysis_meta={
            "total_pages": total_pages,
            "processed_pages": len(processed_pages),
            "processed_page_numbers": sorted(set(processed_pages)),
            "failed_pages": sorted(set(failed_pages)),
            "failed_page_count": len(set(failed_pages)),
            "raw_row_count": len(combined_rows),
            "batch_size": batch_size,
        },
    )


def _extract_leipziger_liste_batch(raw_text: str) -> LeipzigerListeExtraction:
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


def _format_page_batch(batch_pages: Iterable[tuple[int, str]]) -> str:
    return "\n\n".join(
        f"[SEITE {page_number}]\n{text.strip()}"
        for page_number, text in batch_pages
    )


def _coerce_absolute_page(source_page: int | None, batch_page_numbers: list[int]) -> int:
    if not batch_page_numbers:
        return 1
    if source_page is None:
        return batch_page_numbers[0]
    if source_page in batch_page_numbers:
        return source_page
    if 1 <= source_page <= len(batch_page_numbers):
        return batch_page_numbers[source_page - 1]
    return batch_page_numbers[0]
