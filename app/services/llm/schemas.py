from datetime import date

from pydantic import BaseModel, Field

from app.models.enums import DocType


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
