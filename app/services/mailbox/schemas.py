from typing import Literal

from pydantic import BaseModel, Field

HUK_DAMAGE_TYPES = (
    "Kfz-Versicherung",
    "Haftpflichtversicherung",
    "Hausrat- und Wohngebäudeversicherung",
    "Rechtsschutzversicherung",
    "Unfallversicherung",
    "Sonstiges",
)
HukDamageType = Literal[
    "Kfz-Versicherung",
    "Haftpflichtversicherung",
    "Hausrat- und Wohngebäudeversicherung",
    "Rechtsschutzversicherung",
    "Unfallversicherung",
    "Sonstiges",
]


class MailboxClassification(BaseModel):
    is_claim: bool = False
    concern: Literal["Schadenanliegen"] | None = None
    damage_type: HukDamageType | None = None
    damage_confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    callback_phone: str | None = None
    phone_confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    reason: str | None = None

