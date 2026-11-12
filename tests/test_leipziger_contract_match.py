# ruff: noqa: F811  (Fixture "world" wird aus test_permissions_security importiert)
"""Leipziger Liste: bestehende Kunden ueber die Vertragsnummer wiedererkennen. Fiktive Daten.

- Vertragsnummer eindeutig im eigenen Buero (VN an der Akte bzw. frueher verknuepfter Vertrag) ->
  vorhandene Akte; manuell erfasste Telefonnummer, Notizen und offene Rueckrufe bleiben.
- Mehrere Akten oder Widerspruch -> keine automatische Zuordnung, sondern manueller Fall
  ("gleiche Vertragsnummer - bitte zuordnen" im Dublettenvergleich).
- Gleicher Name allein ordnet nie zu. Wiederholter Import erzeugt keine doppelten Akten/Zuordnungen."""

from datetime import date

from app.models import Customer, CustomerContact, CustomerNote, DocumentCustomer, LeipzigerEntry
from app.services import customer_sources
from app.services.customer_duplicates import find_duplicates, merge_customers
from app.tenancy import use_tenant_id
from tests.test_kundenstamm import _import, _row
from tests.test_permissions_security import world  # noqa: F401


def _count(tenant):
    with use_tenant_id(tenant.id):
        return Customer.query.count()


def _manual_customer(db, tenant, user, name, vn=None, phone=None):
    with use_tenant_id(tenant.id):
        c = Customer(tenant_id=tenant.id, name=name, customer_number=vn, phone_private=phone, source=customer_sources.MANUELL,
                     field_sources={"name": "MANUELL", **({"phone_private": "MANUELL"} if phone else {})})
        db.session.add(c)
        db.session.flush()
        db.session.add(CustomerNote(tenant_id=tenant.id, customer_id=c.id, text="Will vor dem Termin angerufen werden",
                                    created_by_user_id=user.id))
        db.session.add(CustomerContact(tenant_id=tenant.id, customer_id=c.id, topic="HUK24 Wechsel", status="open",
                                       outcome="not_reached", created_by_user_id=user.id, updated_by_user_id=user.id))
        db.session.commit()
        return c.id


def test_existing_customer_with_manual_data_is_recognized_by_contract(db, world):
    tenant = world.tenant_a
    cid = _manual_customer(db, tenant, world.laura, "Max Mustermann", vn="808/100001-A", phone="0171 1112223")
    before = _count(tenant)
    # andere Schreibweise und andere Telefonnummer in der Liste - die Vertragsnummer entscheidet
    row = _row("808/100001-A-12", name="Max Mustermann-Beispiel")
    row.customer.phone = "0228 999999"
    _import(db, tenant, [row], "liste1.pdf")
    assert _count(tenant) == before
    with use_tenant_id(tenant.id):
        c = Customer.query.get(cid)
        assert c.phone_private == "0171 1112223"  # manuelle Nummer bleibt
        assert c.name == "Max Mustermann"
        assert CustomerNote.query.filter_by(customer_id=cid).count() == 1
        assert CustomerContact.query.filter_by(customer_id=cid, status="open").count() == 1
        assert LeipzigerEntry.query.filter_by(customer_id=cid).count() == 1


def test_manual_phone_field_is_not_overwritten_on_reimport(db, world):
    tenant = world.tenant_a
    _import(db, tenant, [_row("808/300001-C-12", name="Lena Liste")], "a.pdf", day=1)
    with use_tenant_id(tenant.id):
        c = Customer.query.filter_by(name="Lena Liste").one()
        c.phone = "0171 4444444"
        c.field_sources = {**(c.field_sources or {}), "phone": "MANUELL"}
        db.session.commit()
        cid = c.id
    row = _row("808/300001-C-12", name="Lena Liste")
    row.customer.phone = "0228 1111111"
    _import(db, tenant, [row], "b.pdf", day=2)
    with use_tenant_id(tenant.id):
        assert Customer.query.filter_by(name="Lena Liste").count() == 1
        assert Customer.query.get(cid).phone == "0171 4444444"


def test_same_name_alone_never_assigns(db, world):
    tenant = world.tenant_a
    first = _manual_customer(db, tenant, world.laura, "Peter Krause", vn="808/200001-B")
    second = _manual_customer(db, tenant, world.laura, "Peter Krause", vn="808/200002-C")
    _import(db, tenant, [_row("808/200002-C-12", name="Peter Krause", dob=None)], "krause.pdf")
    with use_tenant_id(tenant.id):
        assert LeipzigerEntry.query.filter_by(customer_id=second).count() == 1
        assert LeipzigerEntry.query.filter_by(customer_id=first).count() == 0
        assert Customer.query.filter_by(name="Peter Krause").count() == 2
    # unbekannte Vertragsnummer, nur derselbe Name: keine Zuordnung zu einer der beiden Akten
    _import(db, tenant, [_row("808/999999-Z-12", name="Peter Krause", dob=None, plz=None, city=None)], "krause2.pdf", day=2)
    with use_tenant_id(tenant.id):
        assert Customer.query.filter_by(name="Peter Krause").count() == 3


def test_ambiguous_contract_goes_to_manual_assignment(db, world):
    tenant = world.tenant_a
    a = _manual_customer(db, tenant, world.laura, "Sabine Kurz", vn="808/400001-D")
    _import(db, tenant, [_row("808/400001-D-12", name="Sabine Kurz", dob=date(1970, 1, 1))], "s1.pdf")
    with use_tenant_id(tenant.id):
        assert LeipzigerEntry.query.filter_by(customer_id=a).count() == 1
    # Widerspruch: gleiche Vertragsnummer, aber anderes, an der Akte von Hand gepflegtes Geburtsdatum
    with use_tenant_id(tenant.id):
        c = Customer.query.get(a)
        c.date_of_birth = date(1970, 1, 1)
        c.field_sources = {**(c.field_sources or {}), "date_of_birth": "MANUELL"}
        db.session.commit()
    before = _count(tenant)
    _import(db, tenant, [_row("808/400001-D-12", name="Sabine Kurz", dob=date(1985, 6, 6))], "s2.pdf", day=2)
    assert _count(tenant) == before + 1  # nicht automatisch zugeordnet
    with use_tenant_id(tenant.id):
        other = Customer.query.filter(Customer.name == "Sabine Kurz", Customer.id != a).one()
        reasons = {d.customer.id: d.reason for d in find_duplicates(Customer.query.get(a))}
        assert reasons.get(other.id) == "contract"  # manueller Fall im Dublettenvergleich
        result = merge_customers(Customer.query.get(a), other, world.laura)  # manuell zuordnen
        db.session.commit()
        assert result and CustomerNote.query.filter_by(customer_id=a).count() == 1
        assert LeipzigerEntry.query.filter_by(customer_id=a).count() == 2


def test_reimport_of_same_list_creates_no_duplicates(db, world):
    tenant = world.tenant_a
    rows = [_row("808/500001-E-12", name="Jonas Neu"), _row("808/500002-F-12", name="Mia Neu", dob=date(1990, 2, 2))]
    first = _import(db, tenant, rows, "woche.pdf", day=1)
    count = _count(tenant)
    second = _import(db, tenant, [_row("808/500001-E-12", name="Jonas Neu"), _row("808/500002-F-12", name="Mia Neu", dob=date(1990, 2, 2))],
                     "woche-erneut.pdf", day=2)
    assert _count(tenant) == count
    with use_tenant_id(tenant.id):
        for doc_id in (first, second):
            assert DocumentCustomer.query.filter_by(document_id=doc_id).count() == 2
            keys = [e.contract_key for e in LeipzigerEntry.query.filter_by(document_id=doc_id).all()]
            assert len(keys) == len(set(keys)) == 2
