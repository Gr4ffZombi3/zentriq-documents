"""Globale Suche: Gruppen, Rechte je Rolle und strikte Mandantentrennung.

Buero A: Buero-Admin, Mitarbeiter Dennis (08/4205-M) und Laura (08/0950-T), Plattformbetreiber
(SUPER_ADMIN). Buero B: eigener Buero-Admin. Beide Bueros haben Kunden und eine Liste."""

import pytest
from sqlalchemy import event

from app.extensions import db as _db
from app.models import Customer, DocStatus, DocType, Document, DocumentCustomer, Tenant, User, UserRole
from app.services.leipziger_entries import rebuild_entries
from app.tenancy import use_tenant_id

PASSWORD = "sicheres-passwort-1"


def _row(number, broker, customer, start=None, status="ANG"):
    return {
        "customer": {"name": customer},
        "contract_number": number,
        "broker_number": broker,
        "contract_start_date": start,
        "status_code": status,
        "product_line": "PH",
    }


def _make_user(db, tenant_id, email, role, vm=None):
    with use_tenant_id(tenant_id):
        user = User(tenant_id=tenant_id, email=email, vermittlernummer=vm, role=role, is_active=True)
        user.set_password(PASSWORD)
        db.session.add(user)
        db.session.commit()
        return user


def _make_list(db, tenant_id, rows, name="liste.pdf"):
    with use_tenant_id(tenant_id):
        document = Document(
            tenant_id=tenant_id,
            filename=name,
            original_filename=name,
            file_path=f"/tmp/{name}",
            status=DocStatus.DONE,
            doc_type=DocType.LEIPZIGER_LISTE,
        )
        holder = Customer(tenant_id=tenant_id, name="Listeninhaber")
        db.session.add_all([document, holder])
        db.session.flush()
        db.session.add(DocumentCustomer(tenant_id=tenant_id, document_id=document.id, customer_id=holder.id, row_data=rows))
        db.session.flush()
        rebuild_entries(document)
        db.session.commit()
        return document


def _login(app, email):
    client = app.test_client()
    resp = client.post("/auth/login", data={"login_type": "email", "identifier": email, "password": PASSWORD})
    assert resp.status_code == 302
    return client


@pytest.fixture()
def world(app, db, tenant):
    office_b = Tenant(name="Büro B", slug="buero-b")
    db.session.add(office_b)
    db.session.commit()

    _make_user(db, tenant.id, "admin-a@example.com", UserRole.OFFICE_ADMIN, "08/0001-A")
    _make_user(db, tenant.id, "dennis@example.com", UserRole.EMPLOYEE, "08/4205-M")
    _make_user(db, tenant.id, "laura@example.com", UserRole.EMPLOYEE, "08/0950-T")
    _make_user(db, tenant.id, "plattform@example.com", UserRole.SUPER_ADMIN, "08/0950-X")
    _make_user(db, office_b.id, "admin-b@example.com", UserRole.OFFICE_ADMIN, "09/1111-B")

    with use_tenant_id(tenant.id):
        db.session.add_all(
            [
                Customer(tenant_id=tenant.id, name="Müller Hans", phone="0341 / 123 456-7", customer_number="K-10042"),
                Customer(tenant_id=tenant.id, name="Schmidt Anna", phone="+49 351 99887766"),
            ]
        )
        db.session.commit()
    with use_tenant_id(office_b.id):
        db.session.add(Customer(tenant_id=office_b.id, name="Müller Fremdbüro", phone="0341 1234567", customer_number="K-10043"))
        db.session.commit()

    # Schreibweisen der Vermittlernummer wie sie in Listen vorkommen.
    _make_list(
        db,
        tenant.id,
        [
            _row("720/111111-A-14", "08/4205-M", "Müller Hans"),
            _row("720/222222-B-14", "084205 m", "Müller Hans", start="2026-07-15", status="NEU"),
            _row("720/333333-C-14", "08/0950-T", "Müller Gerda"),
        ],
    )
    _make_list(db, office_b.id, [_row("720/999999-Z-14", "08/4205-M", "Müller Fremdbüro")], name="b.pdf")
    return office_b


def test_office_admin_finds_customers_and_entries_of_own_office_only(app, world):
    client = _login(app, "admin-a@example.com")
    html = client.get("/search?q=Müller").get_data(as_text=True)
    assert "Müller Hans" in html and "Müller Gerda" in html
    assert "720/111111-A-14" in html and "720/333333-C-14" in html
    assert "Fremdbüro" not in html and "720/999999-Z-14" not in html


def test_search_by_contract_number_ignores_separators(app, world):
    client = _login(app, "admin-a@example.com")
    html = client.get("/search?q=720 222222 b").get_data(as_text=True)
    assert "720/222222-B-14" in html
    assert "720/111111-A-14" not in html


def test_search_by_phone_matches_differently_formatted_numbers(app, world):
    client = _login(app, "admin-a@example.com")
    html = client.get("/search?q=%2B49 341 1234567").get_data(as_text=True)
    assert "Müller Hans" in html
    assert "Fremdbüro" not in html
    assert "Schmidt Anna" in client.get("/search?q=0351/99887766").get_data(as_text=True)


def test_search_by_customer_number(app, world):
    client = _login(app, "admin-a@example.com")
    html = client.get("/search?q=K-1004").get_data(as_text=True)
    assert "Müller Hans" in html
    assert "Fremdbüro" not in html


def test_employee_sees_only_own_entries_and_no_customer_master_data(app, world):
    client = _login(app, "dennis@example.com")
    resp = client.get("/search?q=Müller")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    # Beide Vorgaenge mit Dennis' Nummer (unterschiedliche Schreibweisen) ...
    assert "720/111111-A-14" in html and "720/222222-B-14" in html
    # ... aber weder Lauras Vorgang noch Kundenstammdaten (Telefon, Kundennummer) noch Buero B.
    assert "720/333333-C-14" not in html
    assert "K-10042" not in html and "0341" not in html
    assert "720/999999-Z-14" not in html
    assert 'id="treffer-kunden"' not in html


def test_employee_cannot_find_colleagues_entries_by_number(app, world):
    client = _login(app, "laura@example.com")
    html = client.get("/search?q=720/111111").get_data(as_text=True)
    assert "720/111111-A-14" not in html


def test_employee_without_broker_number_gets_no_entries(app, db, world, tenant):
    _make_user(db, tenant.id, "ohne@example.com", UserRole.EMPLOYEE, None)
    client = _login(app, "ohne@example.com")
    html = client.get("/search?q=720").get_data(as_text=True)
    assert "720/" not in html


def test_super_admin_has_no_access_to_search(app, world):
    client = _login(app, "plattform@example.com")
    assert client.get("/search?q=Müller").status_code == 403
    # Auch kein Suchfeld in der Kopfleiste.
    html = client.get("/plattform/bueros").get_data(as_text=True)
    assert 'class="app-sidebar-search"' not in html


def test_search_field_in_header_for_office_members(app, world):
    for email in ("admin-a@example.com", "dennis@example.com"):
        html = _login(app, email).get("/uebersicht").get_data(as_text=True)
        assert 'class="app-sidebar-search"' in html


def test_short_query_runs_no_search(app, world):
    client = _login(app, "admin-a@example.com")
    html = client.get("/search?q=M").get_data(as_text=True)
    assert "Mindestens 2 Zeichen" in html
    assert "Müller Hans" not in html


def test_search_uses_constant_number_of_queries(app, db, world, tenant):
    """Keine N+1-Abfragen: die Zahl der Queries haengt nicht von der Trefferzahl ab."""
    client = _login(app, "admin-a@example.com")
    client.get("/search?q=Müller")  # Aufwaermen (Sitzung, Navigation)

    def count_queries(query):
        statements = []

        def before(conn, cursor, statement, *args):
            statements.append(statement)

        event.listen(_db.engine, "before_cursor_execute", before)
        try:
            assert client.get(f"/search?q={query}").status_code == 200
        finally:
            event.remove(_db.engine, "before_cursor_execute", before)
        return len(statements)

    few = count_queries("Müller")
    _make_list(db, tenant.id, [_row(f"720/5{i:05d}-X-14", "08/0950-T", "Müller Viele") for i in range(15)], name="neu.pdf")
    client.get("/search?q=Müller")  # Konto/Mandant nach dem Commit neu laden (nicht Teil der Suche)
    many = count_queries("Müller")
    assert many == few, (few, many)
