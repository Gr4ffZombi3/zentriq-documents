import io
import json
import math

from flask import current_app

from app.services.llm.client import get_openai_client
from app.services.mailbox.schemas import HUK_DAMAGE_TYPES, MailboxClassification

CLASSIFICATION_SYSTEM_PROMPT = """Du analysierst das Transkript einer deutschen Mailbox-Nachricht.
Entscheide ausschließlich anhand des Transkripts, ob die anrufende Person wegen eines bereits
eingetretenen Versicherungsschadens einen Rückruf benötigt. Ein Angebot, eine allgemeine
Vertragsfrage oder eine bloße Risiko-/Vorsorgefrage ist kein Schadenanliegen.

Erlaubte Schadenarten sind ausschließlich:
- Kfz-Versicherung
- Haftpflichtversicherung
- Hausrat- und Wohngebäudeversicherung
- Rechtsschutzversicherung
- Unfallversicherung
- Sonstiges

Erfinde keine Telefonnummer oder Schadenart. callback_phone darf nur gesetzt werden, wenn die
Nummer in der gesprochenen Nachricht ausdrücklich als Erreichbarkeits- oder Rückrufnummer genannt
wird. Bei Mehrdeutigkeit setze das betreffende Feld auf null und eine niedrige Confidence.
"Sonstiges" ist nur zulässig, wenn eindeutig ein Schaden vorliegt, der keiner spezifischeren
Formularauswahl zugeordnet werden kann.

Antworte ausschließlich als JSON mit:
{
  "is_claim": bool,
  "concern": "Schadenanliegen" | null,
  "damage_type": string | null,
  "damage_confidence": number,
  "callback_phone": string | null,
  "phone_confidence": number,
  "reason": string | null
}
"""

UNPARSEABLE_REASON = "Die KI-Antwort war nicht auswertbar."


def _mailbox_openai_client():
    return get_openai_client(base_url=current_app.config.get("MAILBOX_OPENAI_BASE_URL"))


def transcribe_audio(filename: str, content_type: str, content: bytes) -> str:
    audio = io.BytesIO(content)
    audio.name = filename
    response = _mailbox_openai_client().audio.transcriptions.create(
        model=current_app.config["OPENAI_TRANSCRIPTION_MODEL"],
        file=audio,
        language="de",
        response_format="json",
    )
    return response.text.strip()


def classify_transcript(transcript: str) -> MailboxClassification:
    response = _mailbox_openai_client().chat.completions.create(
        model=current_app.config["MAILBOX_CLASSIFICATION_MODEL"],
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": CLASSIFICATION_SYSTEM_PROMPT},
            {"role": "user", "content": transcript},
        ],
    )
    try:
        data = json.loads(response.choices[0].message.content or "")
    except (TypeError, ValueError):
        data = None
    return MailboxClassification.model_validate(normalize_classification_payload(data))


def normalize_classification_payload(data) -> dict:
    """Bringt die KI-Antwort vor der Schema-Pruefung in eine gueltige Form. Unklare oder
    ungueltige Werte werden auf den sicheren Wert (None bzw. 0.0) gesetzt - der Fall landet
    dadurch in REVIEW statt als FAILED zu enden. Es wird nie etwas "geraten"."""
    if not isinstance(data, dict):
        return {"is_claim": False, "reason": UNPARSEABLE_REASON}

    return {
        "is_claim": data.get("is_claim") is True,
        "concern": "Schadenanliegen" if _clean_str(data.get("concern")).lower() == "schadenanliegen" else None,
        "damage_type": _normalize_damage_type(data.get("damage_type")),
        "damage_confidence": _normalize_confidence(data.get("damage_confidence")),
        "callback_phone": _clean_str(data.get("callback_phone")) or None,
        "phone_confidence": _normalize_confidence(data.get("phone_confidence")),
        "reason": _clean_str(data.get("reason"))[:2000] or None,
    }


def _clean_str(value) -> str:
    if isinstance(value, bool) or value is None:
        return ""
    if isinstance(value, (str, int)):
        return str(value).strip()
    return ""


def _normalize_damage_type(value) -> str | None:
    """Nur exakte Formularwerte (Gross-/Kleinschreibung egal) - "Kfz" o. Ae. wird bewusst
    NICHT auf "Kfz-Versicherung" gemappt, sondern fuehrt in die manuelle Pruefung."""
    cleaned = _clean_str(value).lower()
    return next((damage_type for damage_type in HUK_DAMAGE_TYPES if damage_type.lower() == cleaned), None)


def _normalize_confidence(value) -> float:
    """Werte ausserhalb 0..1 (z. B. Prozentangaben wie 95) werden NICHT umgerechnet, sondern auf
    0.0 gesetzt: eine uneindeutige Angabe darf nie eine Live-Einreichung freischalten."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0
    number = float(value)
    if math.isnan(number) or not 0.0 <= number <= 1.0:
        return 0.0
    return number
