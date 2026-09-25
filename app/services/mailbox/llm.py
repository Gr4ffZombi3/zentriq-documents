import io
import json

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


def transcribe_audio(filename: str, content_type: str, content: bytes) -> str:
    audio = io.BytesIO(content)
    audio.name = filename
    response = get_openai_client().audio.transcriptions.create(
        model=current_app.config["OPENAI_TRANSCRIPTION_MODEL"],
        file=audio,
        language="de",
        response_format="json",
    )
    return response.text.strip()


def classify_transcript(transcript: str) -> MailboxClassification:
    response = get_openai_client().chat.completions.create(
        model=current_app.config["MAILBOX_CLASSIFICATION_MODEL"],
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": CLASSIFICATION_SYSTEM_PROMPT},
            {"role": "user", "content": transcript},
        ],
    )
    data = json.loads(response.choices[0].message.content)
    classification = MailboxClassification.model_validate(data)
    if classification.damage_type not in HUK_DAMAGE_TYPES:
        classification.damage_type = None
        classification.damage_confidence = 0.0
    return classification

