import enum


class DocType(enum.Enum):
    LEIPZIGER_LISTE = "leipziger_liste"
    HUK_LISTE = "huk_liste"
    GUTACHTEN = "gutachten"
    RECHNUNG = "rechnung"
    VERSICHERUNGSUNTERLAGEN = "versicherungsunterlagen"
    SCHADENAKTE = "schadenakte"
    BRIEF = "brief"
    SONSTIGES = "sonstiges"


class DocStatus(enum.Enum):
    PENDING = "pending"
    OCR_PROCESSING = "ocr_processing"
    OCR_DONE = "ocr_done"
    AI_PROCESSING = "ai_processing"
    DONE = "done"
    FAILED = "failed"


class OcrEngine(enum.Enum):
    NONE = "none"
    TESSERACT = "tesseract"
    VISION = "vision"


class Priority(enum.Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class RecommendationType(enum.Enum):
    CALL_TODAY = "call_today"
    PRIORITIZE_VEHICLE_CHANGE = "prioritize_vehicle_change"
    OFFER_LEGAL_PROTECTION = "offer_legal_protection"
    OFFER_HOUSEHOLD_INSURANCE = "offer_household_insurance"
    CHECK_ACCIDENT_INSURANCE = "check_accident_insurance"
    CHECK_SUPPLEMENTARY_HEALTH = "check_supplementary_health"
    OTHER = "other"


class RecommendationStatus(enum.Enum):
    OPEN = "open"
    DISMISSED = "dismissed"
    DONE = "done"
