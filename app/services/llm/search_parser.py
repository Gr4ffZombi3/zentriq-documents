"""Wandelt eine natuerlichsprachliche Suchanfrage per OpenAI Function Calling in eine
FilterSpec um. Die KI waehlt nur aus einer festen Funktionssignatur aus - es gibt keinen
Text-to-SQL-Pfad. Bei Fehlern oder wenn die KI keine Funktion aufruft, gibt diese Funktion
None zurueck; der Aufrufer faellt dann auf eine einfache Textsuche zurueck."""

import json

from flask import current_app

from app.models.enums import DocType, Priority
from app.search.query_builder import FilterSpec
from app.services.llm.client import get_openai_client

SEARCH_FUNCTION_SCHEMA = {
    "type": "function",
    "function": {
        "name": "build_document_filter",
        "description": "Baut einen Filter fuer die Dokumentensuche aus einer natuerlichsprachlichen Anfrage.",
        "parameters": {
            "type": "object",
            "properties": {
                "doc_type": {"type": "string", "enum": [t.value for t in DocType]},
                "city": {"type": "string", "description": "Stadt des Kunden"},
                "postal_code": {"type": "string", "description": "Postleitzahl des Kunden"},
                "has_product": {"type": "string", "description": "Produkt, das der Kunde hat, z.B. Hausrat"},
                "missing_product": {"type": "string", "description": "Produkt, das dem Kunden fehlt, z.B. Rechtsschutz"},
                "is_neugeschaeft": {"type": "boolean"},
                "is_fahrzeugwechsel": {"type": "boolean"},
                "priority": {"type": "string", "enum": [p.value for p in Priority]},
                "customer_name_contains": {"type": "string"},
                "date_from": {"type": "string", "description": "YYYY-MM-DD"},
                "date_to": {"type": "string", "description": "YYYY-MM-DD"},
            },
            "additionalProperties": False,
        },
    },
}

SEARCH_SYSTEM_PROMPT = (
    "Uebersetze die Suchanfrage in einen strukturierten Filter ueber die Funktion "
    "build_document_filter. Setze nur Felder, die eindeutig aus der Anfrage hervorgehen. "
    "Erfinde keine Werte."
)


def parse_search_query(query: str) -> FilterSpec | None:
    try:
        client = get_openai_client()
        response = client.chat.completions.create(
            model=current_app.config["OPENAI_MODEL"],
            messages=[
                {"role": "system", "content": SEARCH_SYSTEM_PROMPT},
                {"role": "user", "content": query},
            ],
            tools=[SEARCH_FUNCTION_SCHEMA],
            tool_choice="auto",
        )
        message = response.choices[0].message
        if not message.tool_calls:
            return None
        arguments = json.loads(message.tool_calls[0].function.arguments)
        return FilterSpec.model_validate(arguments)
    except Exception:
        return None
