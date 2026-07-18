from datetime import date

from pydantic import BaseModel, Field

from app.models.enums import DocType, Priority


class ExtractedCustomer(BaseModel):
    name: str
    address: str | None = None
    city: str | None = None
    postal_code: str | None = None
    date_of_birth: date | None = None


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


class LeipzigerListeExtraction(BaseModel):
    rows: list[LeipzigerListeRow] = Field(default_factory=list)
