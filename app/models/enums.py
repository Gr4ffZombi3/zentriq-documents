import enum


class TenantStatus(enum.Enum):
    ACTIVE = "active"
    SUSPENDED = "suspended"


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


PRIORITY_ORDER = {Priority.LOW: 0, Priority.MEDIUM: 1, Priority.HIGH: 2}


class RecommendationType(enum.Enum):
    CALL_TODAY = "call_today"
    PRIORITIZE_VEHICLE_CHANGE = "prioritize_vehicle_change"
    OFFER_LEGAL_PROTECTION = "offer_legal_protection"
    OFFER_HOUSEHOLD_INSURANCE = "offer_household_insurance"
    CHECK_ACCIDENT_INSURANCE = "check_accident_insurance"
    CHECK_SUPPLEMENTARY_HEALTH = "check_supplementary_health"
    OTHER = "other"
    # M12: erweiterte Business-Regeln (app/services/analysis/business_rules.py)
    CROSS_SELL_HOUSEHOLD_FROM_BUILDING = "cross_sell_household_from_building"
    CROSS_SELL_LIABILITY_FROM_VEHICLE = "cross_sell_liability_from_vehicle"
    SALES_RISK_MULTIPLE_OFFERS_NO_CLOSURE = "sales_risk_multiple_offers_no_closure"
    HIGH_PRIORITY_STORNO = "high_priority_storno"


class RecommendationStatus(enum.Enum):
    OPEN = "open"
    DISMISSED = "dismissed"
    DONE = "done"


class TaskType(enum.Enum):
    CALL_TODAY = "call_today"
    FOLLOW_UP_OFFER = "follow_up_offer"
    REQUEST_DOCUMENTS = "request_documents"
    PREPARE_CONTRACT = "prepare_contract"
    CHECK_CLOSURE = "check_closure"
    SCHEDULE_APPOINTMENT = "schedule_appointment"
    OTHER = "other"


class TaskStatus(enum.Enum):
    OPEN = "open"
    DONE = "done"
    DISMISSED = "dismissed"


class WiedervorlageReason(enum.Enum):
    OFFER_OLDER_THAN_7_DAYS = "offer_older_than_7_days"
    OFFER_OLDER_THAN_14_DAYS = "offer_older_than_14_days"
    NO_RESPONSE = "no_response"
    MISSING_DOCUMENTS = "missing_documents"
    OPEN_CLOSURE = "open_closure"


class ListChangeType(enum.Enum):
    NEW_CUSTOMER = "new_customer"
    NEW_CONTRACT = "new_contract"
    NEW_OFFER = "new_offer"
    STATUS_CHANGE = "status_change"
    STORNO = "storno"
    REMOVED_CUSTOMER = "removed_customer"
    NEW_PRODUCT_LINE = "new_product_line"


class TimelineEventType(enum.Enum):
    DOCUMENT_UPLOADED = "document_uploaded"
    OFFER_DETECTED = "offer_detected"
    NEW_CONTRACT_DETECTED = "new_contract_detected"
    VEHICLE_CHANGE_DETECTED = "vehicle_change_detected"
    STORNO_DETECTED = "storno_detected"
    TASK_CREATED = "task_created"
    TASK_STATUS_CHANGED = "task_status_changed"
    LIST_COMPARISON_CHANGE = "list_comparison_change"


class AnalysisRunStatus(enum.Enum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class FeedbackRating(enum.Enum):
    UP = "up"
    DOWN = "down"
