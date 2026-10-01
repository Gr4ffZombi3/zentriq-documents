from datetime import date

from pydantic import BaseModel, Field, field_validator

from app.models.enums import DocType, Priority


class ExtractedCustomer(BaseModel):
    name: str
    address: str | None = None
    city: str | None = None
    postal_code: str | None = None
    date_of_birth: date | None = None
    # Nur wenn ausdruecklich im Dokument aufgedruckt (siehe Prompts). Unbrauchbare Werte werden
    # verworfen statt gespeichert - es wird nichts ergaenzt oder geraten.
    phone: str | None = None
    customer_number: str | None = None

    @field_validator("phone")
    @classmethod
    def _valid_phone(cls, value: str | None) -> str | None:
        from app.services.memo_customer_match import normalize_phone

        value = " ".join((value or "").split())
        return value[:50] if value and normalize_phone(value) else None

    @field_validator("customer_number")
    @classmethod
    def _valid_customer_number(cls, value: str | None) -> str | None:
        value = " ".join((value or "").split())
        # Mindestens drei Ziffern; reine Platzhalter wie "-" oder "X" zaehlen nicht.
        return value[:50] if sum(ch.isdigit() for ch in value) >= 3 else None


class DocumentExtraction(BaseModel):
    doc_type: DocType
    customer: ExtractedCustomer | None = None
    vehicle: str | None = None
    license_plate: str | None = None
    insurer: str | None = None
    contract_number: str | None = None
    case_number: str | None = None
    broker: str | None = None
    contract_start_date: date | None = None
    products: list[str] = Field(default_factory=list)
    special_notes: str | None = None
    # M12: zusaetzliche Erkennungsfelder
    broker_number: str | None = None
    product_line: str | None = None
    premium: str | None = None
    tariff: str | None = None


class LeipzigerListeRow(BaseModel):
    """Eine Kundenzeile aus einer Leipziger Liste (ein PDF enthaelt typischerweise mehrere)."""

    customer: ExtractedCustomer
    vehicle: str | None = None
    license_plate: str | None = None
    insurer: str | None = None
    contract_number: str | None = None
    products: list[str] = Field(default_factory=list)
    is_neugeschaeft: bool = False
    is_fahrzeugwechsel: bool = False
    is_angebot: bool = False
    is_storno: bool = False
    cross_sell_opportunity: bool = False
    has_multiple_products: bool = False
    priority: Priority = Priority.MEDIUM
    recommended_next_action: str | None = None
    special_notes: str | None = None
    # M12: zusaetzliche Erkennungsfelder
    broker_number: str | None = None
    product_line: str | None = None
    premium: str | None = None
    tariff: str | None = None
    # M13: Beginn-Datum je Zeile (bewusst NICHT auf DocumentExtraction dupliziert - das ist
    # ein anderes, unabhaengiges Feld fuer generische Einzeldokumente) und Antrag-Signal,
    # getrennt von is_angebot: ein Antrag ist im Verkaufsprozess weiter fortgeschritten als
    # ein reines Angebot, aber noch kein Abschluss ohne Beginn-Datum.
    contract_start_date: date | None = None
    has_antrag: bool = False
    source_page: int | None = None
    source_row: int | None = None
    status_code: str | None = None


class LeipzigerListeExtraction(BaseModel):
    rows: list[LeipzigerListeRow] = Field(default_factory=list)
    analysis_meta: dict = Field(default_factory=dict)
