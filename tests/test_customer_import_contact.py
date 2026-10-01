"""Kundenimport: Telefonnummer und Kundennummer aus der bestehenden Extraktion uebernehmen -
nur vorhandene, plausible Werte; leere Quellen ueberschreiben nichts."""

from app.models import Customer
from app.services.customers import CustomerMatcher
from app.services.llm.schemas import ExtractedCustomer, LeipzigerListeExtraction


def test_phone_and_customer_number_are_taken_over(db, tenant):
    customer = CustomerMatcher().get_or_create(
        ExtractedCustomer(name="Max Mustermann", phone="0171 1234567", customer_number="KD 123456")
    )
    db.session.commit()
    stored = db.session.get(Customer, customer.id)
    assert stored.phone == "0171 1234567"
    assert stored.customer_number == "KD 123456"


def test_empty_source_does_not_overwrite_existing_values(db, tenant):
    existing = Customer(tenant_id=tenant.id, name="Max Mustermann", postal_code="50667", phone="0171 1234567", customer_number="KD 1")
    db.session.add(existing)
    db.session.commit()
    matched = CustomerMatcher().get_or_create(ExtractedCustomer(name="Max Mustermann", postal_code="50667"))
    assert matched.id == existing.id
    assert matched.phone == "0171 1234567" and matched.customer_number == "KD 1"


def test_newer_source_value_replaces_old_value(db, tenant):
    existing = Customer(tenant_id=tenant.id, name="Max Mustermann", postal_code="50667", phone="0171 1234567")
    db.session.add(existing)
    db.session.commit()
    matched = CustomerMatcher().get_or_create(ExtractedCustomer(name="Max Mustermann", postal_code="50667", phone="0221 998877"))
    assert matched.phone == "0221 998877"


def test_implausible_values_are_discarded_not_invented():
    data = ExtractedCustomer(name="X", phone="TEL", customer_number="-")
    assert data.phone is None and data.customer_number is None
    assert ExtractedCustomer(name="X", phone="12345").phone is None  # ohne Vorwahl nicht zuordenbar
    assert ExtractedCustomer(name="X").phone is None


def test_leipziger_rows_carry_contact_fields(db, tenant):
    extraction = LeipzigerListeExtraction.model_validate(
        {"rows": [{"customer": {"name": "Erika Muster", "phone": "+49 171 7654321", "customer_number": "VN 445566"}}]}
    )
    customer = CustomerMatcher().get_or_create(extraction.rows[0].customer)
    assert customer.phone == "+49 171 7654321" and customer.customer_number == "VN 445566"
