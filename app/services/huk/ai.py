"""Claude-Anbindung fuer die Kfz-Berechnung (eigene, serverseitige Anthropic-API-Anbindung -
unabhaengig vom ZentriqAI-Router/Gemini).

Einsatz nur gezielt: Angaben aus einem eingefuegten Freitext (z. B. Kundenmail, Notiz) in die
Formularfelder uebernehmen. Ergebnis sind ausschliesslich *Vorschlaege*, die im Formular markiert
und vom Benutzer geprueft werden; nichts wird erfunden (unbekannt -> leer). Den HUK-Rechner bedient
das Modell nie, und es liefert keine Beitraege.

Kosten: HUK_AI_MAX_COST_USD je Auftrag (0 = Funktion aus). Vor jedem Aufruf wird der ungünstigste
Fall (alle Eingabezeichen als Token + volle Ausgabegrenze) gegen das Restbudget geprueft; danach
werden die tatsaechlichen Kosten aus `usage` verbucht. Hoechstens HUK_AI_MAX_CALLS Aufrufe je
Auftrag, keine automatischen Wiederholungen."""

from __future__ import annotations

import json
import logging
from decimal import Decimal

import anthropic
from flask import current_app

from app.extensions import db
from app.models.huk_quote import HukQuoteJob
from app.services.huk import schema

log = logging.getLogger(__name__)

SYSTEM = (
    "Du überträgst Angaben für einen Kfz-Tarifrechner aus einem deutschen Text in Formularfelder. "
    "Übernimm nur, was im Text ausdrücklich steht. Erfinde, schätze oder ergänze nichts: Fehlt eine "
    "Angabe oder ist sie unklar, gib einen leeren String zurück (beim Fahrerkreis eine leere Liste). "
    "Bei Auswahlfeldern verwende ausschließlich einen der erlaubten Werte. Datumsformate: TT.MM.JJJJ, "
    "Monat/Jahr als MM.JJJJ, Fahrleistung als ganze Kilometerzahl ohne Punkte."
)


class HukAiError(Exception):
    def __init__(self, message: str, code: str):
        super().__init__(message)
        self.message = message
        self.code = code


def budget_usd() -> float:
    return float(current_app.config.get("HUK_AI_MAX_COST_USD") or 0)


def is_available() -> bool:
    return budget_usd() > 0 and bool(current_app.config.get("ANTHROPIC_API_KEY"))


def unavailable_reason() -> str | None:
    if budget_usd() <= 0:
        return "Die KI-Übernahme ist ausgeschaltet (kein Budget festgelegt: HUK_AI_MAX_COST_USD)."
    if not current_app.config.get("ANTHROPIC_API_KEY"):
        return "Die KI-Übernahme ist nicht eingerichtet (ANTHROPIC_API_KEY fehlt)."
    return None


def _price(tokens_in: int, tokens_out: int) -> float:
    cfg = current_app.config
    return tokens_in * cfg["HUK_AI_PRICE_INPUT_PER_MTOK"] / 1e6 + tokens_out * cfg["HUK_AI_PRICE_OUTPUT_PER_MTOK"] / 1e6


def _output_schema() -> dict:
    props = {}
    for f in schema.FIELDS:
        if f.kind == "multi":
            props[f.key] = {"type": "array", "items": {"type": "string", "enum": [o.value for o in f.options]}}
        elif f.options:
            props[f.key] = {"type": "string", "enum": [""] + [o.value for o in f.options]}
        else:
            props[f.key] = {"type": "string"}
    return {"type": "object", "properties": props, "required": list(props), "additionalProperties": False}


def _field_guide() -> str:
    lines = []
    for f in schema.FIELDS:
        if f.options and f.kind != "sf":
            opts = "; ".join(f"{o.value} = {o.label}" for o in f.options)
            lines.append(f"- {f.key}: {f.question} [{opts}]")
        elif f.kind == "sf":
            lines.append(f"- {f.key}: {f.question} [Werte wie 'SF 10', 'SF 1/2', 'Klasse 0', 'Klasse M']")
        else:
            lines.append(f"- {f.key}: {f.question}" + (f" (Format {f.placeholder})" if f.placeholder else ""))
    return "\n".join(lines)


def get_client() -> anthropic.Anthropic:
    return anthropic.Anthropic(
        api_key=current_app.config["ANTHROPIC_API_KEY"],
        timeout=float(current_app.config.get("HUK_AI_TIMEOUT_SECONDS") or 40),
        max_retries=0,
    )


def extract(job: HukQuoteJob, text: str) -> dict:
    """Gibt Vorschlaege {feld: wert} zurueck (nur gueltige, bekannte Werte). Verbucht Kosten am Auftrag."""
    reason = unavailable_reason()
    if reason:
        raise HukAiError(reason, "disabled")
    text = (text or "").strip()
    if not text:
        raise HukAiError("Bitte einen Text einfügen.", "empty")
    max_chars = int(current_app.config.get("HUK_AI_MAX_INPUT_CHARS") or 3000)
    if len(text) > max_chars:
        raise HukAiError(f"Der Text ist zu lang (höchstens {max_chars} Zeichen).", "too_long")
    if job.ai_calls >= int(current_app.config.get("HUK_AI_MAX_CALLS") or 3):
        raise HukAiError("Für diesen Auftrag wurde die KI-Übernahme bereits so oft genutzt wie erlaubt.", "max_calls")

    guide = _field_guide()
    max_out = int(current_app.config.get("HUK_AI_MAX_OUTPUT_TOKENS") or 2000)
    # Ungünstigster Fall: ein Token je Zeichen (deutlich mehr als real) plus Schema-Aufschlag.
    worst_in = len(SYSTEM) + len(guide) + len(text) + 3000
    spent = float(job.ai_cost_usd or 0)
    if spent + _price(worst_in, max_out) > budget_usd():
        raise HukAiError("Das KI-Budget für diesen Auftrag reicht für einen weiteren Aufruf nicht aus.", "budget")

    prompt = f"Felder:\n{guide}\n\nText:\n<<<\n{text}\n>>>"
    try:
        response = get_client().messages.create(
            model=current_app.config["HUK_AI_MODEL"],
            max_tokens=max_out,
            system=SYSTEM,
            messages=[{"role": "user", "content": prompt}],
            output_config={"effort": "low", "format": {"type": "json_schema", "schema": _output_schema()}},
        )
    except anthropic.APITimeoutError as exc:
        raise HukAiError("Die KI hat nicht rechtzeitig geantwortet.", "timeout") from exc
    except anthropic.RateLimitError as exc:
        raise HukAiError("Die KI ist gerade ausgelastet. Bitte später erneut versuchen.", "rate_limit") from exc
    except (anthropic.AuthenticationError, anthropic.PermissionDeniedError, anthropic.NotFoundError) as exc:
        raise HukAiError("Die KI-Anbindung ist technisch nicht verfügbar.", type(exc).__name__) from exc
    except anthropic.APIStatusError as exc:
        raise HukAiError("Der KI-Dienst ist vorübergehend nicht erreichbar.", f"status_{exc.status_code}") from exc
    except anthropic.APIConnectionError as exc:
        raise HukAiError("Der KI-Dienst ist nicht erreichbar.", "connection") from exc
    finally:
        job.ai_calls = (job.ai_calls or 0) + 1
        db.session.commit()

    usage = getattr(response, "usage", None)
    cost = _price(getattr(usage, "input_tokens", 0) or 0, getattr(usage, "output_tokens", 0) or 0)
    job.ai_cost_usd = Decimal(str(round(spent + cost, 4)))
    db.session.commit()
    log.info("huk.ai.extract job_id=%s cost_usd=%.4f stop=%s", job.id, cost, response.stop_reason)

    if response.stop_reason == "refusal":
        raise HukAiError("Die KI konnte den Text nicht verarbeiten.", "refusal")
    text_out = next((b.text for b in response.content if b.type == "text"), "")
    try:
        data = json.loads(text_out)
    except (TypeError, ValueError) as exc:
        raise HukAiError("Die KI-Antwort war unvollständig. Bitte die Angaben manuell eintragen.", "invalid_json") from exc
    if not isinstance(data, dict):
        raise HukAiError("Die KI-Antwort war unvollständig.", "invalid_json")
    suggested = schema.normalize({k: v for k, v in data.items() if v not in ("", None, [])})
    # Nur Werte, die das Feldformat bzw. die Auswahl erfuellen.
    _missing, errors = schema.validate(suggested, schema_today())
    return {k: v for k, v in suggested.items() if k not in errors and v not in ("", [])}


def schema_today():
    from app.services.timetracking.clock import local_today

    return local_today()
