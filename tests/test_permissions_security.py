"""Rechte- und Mandantentrennung fuer das Rollenmodell SUPER_ADMIN / OFFICE_ADMIN / EMPLOYEE.

Szenario: Buero A (Default-Tenant) mit Buero-Admin, den Mitarbeitern Dennis und Laura und dem
Plattformbetreiber (SUPER_ADMIN), Buero B mit eigenem Buero-Admin und Mitarbeiter Bob. Beide
Bueros haben eine Leipziger Liste und Arbeitszeitbuchungen. Alle Pruefungen laufen ueber echte
HTTP-Requests (direkte URLs, manipulierte IDs/Formulare)."""

import re
from datetime import timedelta

import pytest

from app.models import (
    AuditLog,
    Customer,
    DocStatus,
    DocType,
    Document,
    DocumentCustomer,
    EmployeeProfile,
    Tenant,
    TenantStatus,
    User,
    UserRole,
    WorkSession,
)
from app.models.audit_log import AuditEventType
from app.models.timetracking import utcnow_naive
from app.tenancy import bypass_tenant_scope, use_tenant_id

PASSWORD = "sicheres-passwort-1"


def _row(number, broker, start=None):
    return {
        "customer": {"name": f"Kunde {number}"},
        "contract_number": number,
        "broker_number": broker,
        "contract_start_date": start,
        "status_code": "ANG",
        "is_angebot": True,
        "is_neugeschaeft": False,
        "is_fahrzeugwechsel": False,
        "is_storno": False,
    }


def _user(db, tenant, email, vm, name, role=UserRole.EMPLOYEE):
    with use_tenant_id(tenant.id):
        user = User(tenant_id=tenant.id, email=email, vermittlernummer=vm, role=role, is_active=True)
        user.set_password(PASSWORD)
        db.session.add(user)
        db.session.flush()
        db.session.add(EmployeeProfile(tenant_id=tenant.id, user_id=user.id, display_name=name))
        db.session.commit()
        return user


def _list(db, tenant, rows, name):
    with use_tenant_id(tenant.id):
        document = Document(
            tenant_id=tenant.id,
            filename=name,
            original_filename=name,
            file_path=f"/tmp/{name}",
            status=DocStatus.DONE,
            doc_type=DocType.LEIPZIGER_LISTE,
        )
        customer = Customer(tenant_id=tenant.id, name=f"Kunde {name}")
        db.session.add_all([document, customer])
        db.session.flush()
        db.session.add(
            DocumentCustomer(tenant_id=tenant.id, document_id=document.id, customer_id=customer.id, row_data=rows)
        )
        db.session.commit()
        return document


def _session(db, user):
    with use_tenant_id(user.tenant_id):
        start = utcnow_naive() - timedelta(hours=3)
        work_session = WorkSession(
            tenant_id=user.tenant_id, user_id=user.id, work_date=start.date(), started_at=start,
            ended_at=start + timedelta(hours=2),
        )
        db.session.add(work_session)
        db.session.commit()
        return work_session


class World:
    pass


@pytest.fixture()
def world(app, db, tenant):
    w = World()
    w.tenant_a = tenant
    w.tenant_b = Tenant(name="Buero B", slug="buero-b")
    db.session.add(w.tenant_b)
    db.session.commit()

    w.admin_a = _user(db, tenant, "admin-a@example.com", "08/0001-A", "Admin A", UserRole.OFFICE_ADMIN)
    w.dennis = _user(db, tenant, "dennis@example.com", "08/1234-A", "Dennis")
    w.laura = _user(db, tenant, "laura@example.com", "08/5555-B", "Laura")
    w.root = _user(db, tenant, "justin@example.com", "08/0950-T", "Justin", UserRole.SUPER_ADMIN)
    w.admin_b = _user(db, w.tenant_b, "admin-b@example.com", "08/0002-B", "Admin B", UserRole.OFFICE_ADMIN)
    w.bob = _user(db, w.tenant_b, "bob@example.com", "08/7777-C", "Bob")

    w.doc_a = _list(
        db,
        tenant,
        [
            _row("DENNIS-OFFEN-1", "081234-A"),
            _row("DENNIS-ERLEDIGT-1", "08/1234-A", start="2026-07-01"),
            _row("LAURA-OFFEN-1", "08/5555-B"),
            _row("JUSTIN-OFFEN-1", "08/0950-T"),
        ],
        "liste-a.pdf",
    )
    w.doc_b = _list(db, w.tenant_b, [_row("BOB-OFFEN-1", "08/7777-C"), _row("B-DENNIS-1", "08/1234-A")], "liste-b.pdf")

    w.session_dennis = _session(db, w.dennis)
    w.session_laura = _session(db, w.laura)
    w.session_bob = _session(db, w.bob)
    for name in ("admin_a", "dennis", "laura", "root", "admin_b", "bob", "session_dennis", "session_laura", "session_bob", "doc_a", "doc_b"):
        setattr(w, f"{name}_id", getattr(w, name).id)
    return w


def login(app, email, password=PASSWORD):
    client = app.test_client()
    resp = client.post("/auth/login", data={"login_type": "email", "identifier": email, "password": password})
    assert resp.status_code == 302, f"Login fehlgeschlagen fuer {email}"
    return client


def _reload(user_id):
    with bypass_tenant_scope():
        from app.extensions import db

        db.session.expire_all()
        return db.session.get(User, user_id)


# --- EMPLOYEE: nur eigene Daten ------------------------------------------------------------


def test_employee_sees_only_own_leipziger_entries(app, world):
    html = login(app, "dennis@example.com").get("/leipziger-liste").get_data(as_text=True)
    assert "DENNIS-OFFEN-1" in html
    for foreign in ("LAURA-OFFEN-1", "JUSTIN-OFFEN-1", "BOB-OFFEN-1", "B-DENNIS-1"):
        assert foreign not in html


def test_employee_cannot_load_foreign_tenant_list_by_document_id(app, world):
    """Manipulierte document_id einer Liste aus Buero B liefert keine Daten aus B - auch
    nicht die Zeile, die zufaellig Dennis' Vermittlernummer traegt."""
    html = login(app, "dennis@example.com").get(f"/leipziger-liste?document_id={world.doc_b_id}").get_data(as_text=True)
    assert "B-DENNIS-1" not in html
    assert "BOB-OFFEN-1" not in html
    assert "liste-b.pdf" not in html


@pytest.mark.parametrize(
    "path",
    [
        "/leipziger-liste/mitarbeiter",
        "/leipziger-liste/mitarbeiter?mitarbeiter={laura_id}",
        "/documents/{doc_a_id}",
        "/documents/{doc_a_id}/file",
        "/documents/{doc_a_id}/row?index=0",
        "/potenziale",
        "/customers",
        "/zeiterfassung/team",
        "/zeiterfassung/team/{laura_id}",
        "/zeiterfassung/buchungen/{session_laura_id}/bearbeiten",
        "/zeiterfassung/protokoll?mitarbeiter={laura_id}",
        "/zeiterfassung/antraege",
        "/settings/users",
        "/settings/users/{laura_id}",
        "/sprachnachrichten",
        "/plattform/bueros",
    ],
)
def test_employee_cannot_open_foreign_data_by_url(app, world, path):
    client = login(app, "dennis@example.com")
    assert client.get(path.format(**vars(world))).status_code == 403


@pytest.mark.parametrize(
    "path",
    [
        "/zeiterfassung/buchungen/{session_laura_id}/stornieren",
        "/zeiterfassung/team/{laura_id}/buchungen/neu",
        "/settings/users/{laura_id}/loeschen",
        "/settings/users/{laura_id}/2fa-zuruecksetzen",
        "/settings/users/new",
        "/upload",
    ],
)
def test_employee_cannot_trigger_admin_actions(app, world, path):
    client = login(app, "dennis@example.com")
    assert client.post(path.format(**vars(world)), data={"reason": "x"}).status_code == 403
    assert _reload(world.laura_id).is_active is True


def test_employee_cannot_file_request_for_foreign_session(app, world):
    client = login(app, "dennis@example.com")
    for session_id in (world.session_laura_id, world.session_bob_id):
        resp = client.get(f"/zeiterfassung/korrekturantraege/neu?buchung={session_id}")
        assert resp.status_code in (403, 404)


def test_employee_timetracking_shows_only_own_data(app, world):
    client = login(app, "dennis@example.com")
    for path in ("/zeiterfassung", "/zeiterfassung/woche", "/zeiterfassung/monat", "/zeiterfassung/korrekturantraege"):
        html = client.get(path).get_data(as_text=True)
        assert "laura@example.com" not in html and "Laura" not in html
        assert "bob@example.com" not in html


def test_employee_navigation_has_no_admin_areas(app, world):
    html = login(app, "dennis@example.com").get("/zeiterfassung").get_data(as_text=True)
    assert "Leipziger Liste" in html and "Zeiterfassung" in html and "Konto" in html
    for hidden in ('href="/sprachnachrichten"', 'href="/settings/users"', 'href="/zeiterfassung/team"', 'href="/plattform'):
        assert hidden not in html


# --- OFFICE_ADMIN: nur eigener Mandant -------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "/settings/users/{bob_id}",
        "/settings/users/{admin_b_id}",
        "/zeiterfassung/team/{bob_id}",
        "/zeiterfassung/buchungen/{session_bob_id}/bearbeiten",
        "/documents/{doc_b_id}",
        "/leipziger-liste/mitarbeiter?mitarbeiter={bob_id}",
    ],
)
def test_office_admin_cannot_open_other_office_data(app, world, path):
    assert login(app, "admin-a@example.com").get(path.format(**vars(world))).status_code == 404


def test_office_admin_lists_contain_no_other_office(app, world):
    client = login(app, "admin-a@example.com")
    for path in ("/settings/users", "/zeiterfassung/team", "/zeiterfassung/protokoll", "/leipziger-liste", "/leipziger-liste/mitarbeiter"):
        html = client.get(path).get_data(as_text=True)
        assert "bob@example.com" not in html and "admin-b@example.com" not in html and "BOB-OFFEN-1" not in html
    html = client.get(f"/leipziger-liste?document_id={world.doc_b_id}").get_data(as_text=True)
    assert "BOB-OFFEN-1" not in html


@pytest.mark.parametrize(
    "path",
    [
        "/settings/users/{bob_id}/loeschen",
        "/settings/users/{bob_id}/2fa-zuruecksetzen",
        "/settings/users/{bob_id}/passwort-reset",
        "/zeiterfassung/buchungen/{session_bob_id}/stornieren",
    ],
)
def test_office_admin_cannot_modify_other_office(app, world, path):
    client = login(app, "admin-a@example.com")
    assert client.post(path.format(**vars(world)), data={"reason": "x"}).status_code == 404
    bob = _reload(world.bob_id)
    assert bob.is_active and bob.deleted_at is None


def test_office_admin_cannot_access_platform(app, world):
    client = login(app, "admin-a@example.com")
    for path in ("/plattform", "/plattform/bueros", "/plattform/benutzer", f"/plattform/benutzer/{world.bob_id}", "/plattform/sicherheit", "/plattform/system"):
        assert client.get(path).status_code == 403
    assert client.post(f"/plattform/benutzer/{world.bob_id}/loeschen").status_code == 403
    assert client.post(f"/plattform/bueros/{world.tenant_b.id}", data={"name": "gekapert"}).status_code == 403
    assert db_tenant_name(world.tenant_b.id) == "Buero B"


def db_tenant_name(tenant_id):
    from app.extensions import db

    db.session.expire_all()
    return db.session.get(Tenant, tenant_id).name


USER_FORM = {
    "email": "neu@example.com",
    "display_name": "Neu",
    "weekly_hours": "40",
    "workdays": ["1", "2", "3", "4", "5"],
    "is_active": "y",
    "password": "startpasswort1",
    "password_confirm": "startpasswort1",
}


def test_office_admin_cannot_create_super_admin(app, world):
    client = login(app, "admin-a@example.com")
    resp = client.post("/settings/users/new", data={**USER_FORM, "role": "super_admin"})
    assert resp.status_code == 200
    with bypass_tenant_scope():
        assert User.query.filter_by(email="neu@example.com").first() is None


def test_office_admin_cannot_promote_to_super_admin(app, world):
    client = login(app, "admin-a@example.com")
    for target in (world.dennis_id, world.admin_a_id):
        client.post(f"/settings/users/{target}", data={**USER_FORM, "email": _reload(target).email, "role": "super_admin", "password": "", "password_confirm": ""})
        assert _reload(target).role != UserRole.SUPER_ADMIN


def test_office_admin_cannot_see_or_manage_super_admin_of_same_tenant(app, world):
    client = login(app, "admin-a@example.com")
    assert "justin@example.com" not in client.get("/settings/users").get_data(as_text=True)
    assert "justin@example.com" not in client.get("/zeiterfassung/team").get_data(as_text=True)
    assert client.get(f"/settings/users/{world.root_id}").status_code == 404
    assert client.get(f"/zeiterfassung/team/{world.root_id}").status_code == 404
    for action in ("loeschen", "2fa-zuruecksetzen", "passwort-reset"):
        assert client.post(f"/settings/users/{world.root_id}/{action}").status_code == 404
    root = _reload(world.root_id)
    assert root.is_active and root.role == UserRole.SUPER_ADMIN


def test_office_admin_cannot_assign_user_to_other_office(app, world):
    """Ein eingeschleustes tenant_id-Feld wird ignoriert - Anlage immer im eigenen Buero."""
    client = login(app, "admin-a@example.com")
    resp = client.post("/settings/users/new", data={**USER_FORM, "role": "employee", "tenant_id": str(world.tenant_b.id)})
    assert resp.status_code == 302
    with bypass_tenant_scope():
        created = User.query.filter_by(email="neu@example.com").one()
        assert created.tenant_id == world.tenant_a.id


def test_office_admin_creates_employee_and_sees_team_times(app, world):
    client = login(app, "admin-a@example.com")
    assert client.post("/settings/users/new", data={**USER_FORM, "role": "employee", "vermittlernummer": "08/4242-X"}).status_code == 302
    html = client.get("/zeiterfassung/team").get_data(as_text=True)
    assert "Dennis" in html and "Laura" in html
    assert client.get(f"/zeiterfassung/team/{world.laura_id}").status_code == 200


# --- SUPER_ADMIN: Plattform ja, Fachdaten nein -----------------------------------------------

SUPER_ADMIN_ALLOWED = {"static", "portal.home", "settings.index", "settings.profile", "settings.security"}


def _concrete_url(rule, world):
    url = rule.rule
    url = re.sub(r"<int:(tenant_id)>", str(world.tenant_b.id), url)
    url = re.sub(r"<int:user_id>", str(world.bob_id), url)
    url = re.sub(r"<int:document_id>", str(world.doc_b_id), url)
    url = re.sub(r"<int:session_id>", str(world.session_bob_id), url)
    return re.sub(r"<int:[a-z_]+>", "1", url)


def test_super_admin_is_denied_every_business_endpoint(app, world):
    """Generisch ueber ALLE registrierten Routen: alles ausser Plattform, Anmeldung und
    eigenem Konto endet fuer den SUPER_ADMIN mit 403 - auch neue Blueprints automatisch."""
    client = login(app, "justin@example.com")
    checked = 0
    for rule in app.url_map.iter_rules():
        if rule.endpoint == "static" or rule.endpoint.startswith(("platform.", "auth.")) or rule.endpoint in SUPER_ADMIN_ALLOWED:
            continue
        url = _concrete_url(rule, world)
        for method in sorted(rule.methods - {"HEAD", "OPTIONS"}):
            resp = client.open(url, method=method, data={})
            assert resp.status_code == 403, f"{method} {url} -> {resp.status_code}"
            checked += 1
    assert checked > 40


@pytest.mark.parametrize(
    "path",
    [
        "/leipziger-liste",
        "/leipziger-liste?document_id={doc_a_id}",
        "/leipziger-liste/mitarbeiter",
        "/documents/{doc_a_id}",
        "/documents/{doc_a_id}/file",
        "/zeiterfassung",
        "/zeiterfassung/team/{dennis_id}",
        "/zeiterfassung/protokoll",
        "/sprachnachrichten",
        "/mailbox",
        "/settings/users",
    ],
)
def test_super_admin_cannot_read_office_data(app, world, path):
    assert login(app, "justin@example.com").get(path.format(**vars(world))).status_code == 403


def test_super_admin_platform_pages_show_no_business_data(app, world):
    client = login(app, "justin@example.com")
    pages = [
        "/plattform/bueros",
        f"/plattform/bueros/{world.tenant_a.id}",
        f"/plattform/bueros/{world.tenant_b.id}",
        "/plattform/benutzer",
        f"/plattform/benutzer/{world.dennis_id}",
        "/plattform/sicherheit",
        "/plattform/system",
    ]
    for path in pages:
        resp = client.get(path)
        assert resp.status_code == 200, path
        html = resp.get_data(as_text=True)
        for secret in ("DENNIS-OFFEN-1", "BOB-OFFEN-1", "liste-a.pdf", "liste-b.pdf"):
            assert secret not in html, (path, secret)
    offices = client.get("/plattform/bueros").get_data(as_text=True)
    assert "Buero B" in offices and "Default Tenant" in offices
    detail_b = client.get(f"/plattform/bueros/{world.tenant_b.id}").get_data(as_text=True)
    assert "bob@example.com" in detail_b and "admin-b@example.com" in detail_b


def test_super_admin_navigation(app, world):
    html = login(app, "justin@example.com").get("/plattform/bueros").get_data(as_text=True)
    for label in ("Büros", "Benutzer", "Systemeinstellungen", "Sicherheit"):
        assert label in html
    assert 'href="/leipziger-liste"' not in html
    assert 'href="/zeiterfassung"' not in html
    assert 'href="/sprachnachrichten"' not in html


def test_super_admin_home_is_platform(app, world):
    resp = login(app, "justin@example.com").get("/")
    assert resp.headers["Location"].endswith("/plattform/bueros")


def test_super_admin_creates_office_with_office_admin(app, world):
    client = login(app, "justin@example.com")
    resp = client.post(
        "/plattform/bueros/neu",
        data={**USER_FORM, "tenant_name": "Buero C", "email": "chefin-c@example.com", "role": "office_admin"},
    )
    assert resp.status_code == 302
    with bypass_tenant_scope():
        tenant_c = Tenant.query.filter_by(name="Buero C").one()
        chefin = User.query.filter_by(email="chefin-c@example.com").one()
    assert chefin.tenant_id == tenant_c.id and chefin.role == UserRole.OFFICE_ADMIN
    assert AuditLog.query.filter_by(event_type=AuditEventType.TENANT_CREATED).count() == 1

    chefin_client = login(app, "chefin-c@example.com", "startpasswort1")
    html = chefin_client.get("/settings/users").get_data(as_text=True)
    assert "chefin-c@example.com" in html and "dennis@example.com" not in html


def test_super_admin_creates_employee_in_office(app, world):
    client = login(app, "justin@example.com")
    resp = client.post(
        f"/plattform/bueros/{world.tenant_b.id}/benutzer/neu",
        data={**USER_FORM, "email": "neu-b@example.com", "role": "employee"},
    )
    assert resp.status_code == 302
    with bypass_tenant_scope():
        assert User.query.filter_by(email="neu-b@example.com").one().tenant_id == world.tenant_b.id


def test_super_admin_deactivates_office(app, world):
    bob_client = login(app, "bob@example.com")
    assert bob_client.get("/zeiterfassung").status_code == 200

    client = login(app, "justin@example.com")
    assert client.post(f"/plattform/bueros/{world.tenant_b.id}", data={"name": "Buero B"}).status_code == 302
    from app.extensions import db

    db.session.expire_all()
    assert db.session.get(Tenant, world.tenant_b.id).status == TenantStatus.SUSPENDED
    # Laufende Session endet sofort, neue Anmeldung scheitert.
    assert bob_client.get("/zeiterfassung").status_code == 302
    resp = app.test_client().post("/auth/login", data={"login_type": "email", "identifier": "bob@example.com", "password": PASSWORD})
    assert resp.status_code == 200
    assert "Büro ist deaktiviert" in resp.get_data(as_text=True)


def test_super_admin_cannot_set_passwords(app, world):
    client = login(app, "justin@example.com")
    resp = client.post(
        f"/plattform/benutzer/{world.dennis_id}",
        data={**USER_FORM, "email": "dennis@example.com", "role": "employee", "password": "uebernommen1", "password_confirm": "uebernommen1"},
    )
    assert resp.status_code == 302
    dennis = _reload(world.dennis_id)
    assert dennis.check_password(PASSWORD)
    assert not dennis.check_password("uebernommen1")


def test_super_admin_manages_roles_and_status(app, world):
    client = login(app, "justin@example.com")
    data = {**USER_FORM, "email": "laura@example.com", "role": "office_admin", "password": "", "password_confirm": ""}
    assert client.post(f"/plattform/benutzer/{world.laura_id}", data=data).status_code == 302
    assert _reload(world.laura_id).role == UserRole.OFFICE_ADMIN

    # Eigene Rolle/Status kann er nicht entziehen, letzter Super-Admin bleibt bestehen.
    data = {**USER_FORM, "email": "justin@example.com", "role": "employee"}
    client.post(f"/plattform/benutzer/{world.root_id}", data=data)
    assert _reload(world.root_id).role == UserRole.SUPER_ADMIN


def test_super_admin_deletes_and_resets_two_factor(app, world):
    from tests.two_factor_helpers import enable_two_factor

    enable_two_factor(_reload(world.laura_id))
    client = login(app, "justin@example.com")
    assert client.post(f"/plattform/benutzer/{world.laura_id}/2fa-zuruecksetzen").status_code == 302
    assert _reload(world.laura_id).two_factor_enabled is False
    assert client.post(f"/plattform/benutzer/{world.dennis_id}/loeschen").status_code == 302
    assert _reload(world.dennis_id).deleted_at is not None


# --- Mitarbeiter loeschen (Soft-Delete) ------------------------------------------------------


def test_office_admin_deletes_employee_softly(app, world, db):
    dennis_client = login(app, "dennis@example.com")
    assert dennis_client.get("/zeiterfassung").status_code == 200

    admin = login(app, "admin-a@example.com")
    assert "Mitarbeiter wirklich löschen?" in admin.get("/settings/users").get_data(as_text=True)
    assert admin.post(f"/settings/users/{world.dennis_id}/loeschen").status_code == 302

    dennis = _reload(world.dennis_id)
    assert dennis.deleted_at is not None and dennis.is_active is False
    # Historische Daten bleiben erhalten.
    with use_tenant_id(world.tenant_a.id):
        assert WorkSession.query.filter_by(user_id=world.dennis_id).count() == 1
        assert db.session.get(EmployeeProfile, dennis.employee_profile.id) is not None
    assert AuditLog.query.filter_by(event_type=AuditEventType.USER_DELETED).count() == 1
    # Aus der Verwaltung verschwunden, Session beendet, Login gesperrt.
    assert "dennis@example.com" not in admin.get("/settings/users").get_data(as_text=True)
    assert admin.get(f"/settings/users/{world.dennis_id}").status_code == 404
    assert dennis_client.get("/zeiterfassung").status_code == 302
    resp = app.test_client().post("/auth/login", data={"login_type": "email", "identifier": "dennis@example.com", "password": PASSWORD})
    assert resp.status_code == 200 and "deaktiviert" in resp.get_data(as_text=True)
    resp = app.test_client().post("/auth/login", data={"login_type": "vermittlernummer", "identifier": "08/1234-A", "password": PASSWORD})
    assert resp.status_code == 200
    # Die Zeiterfassungs-Historie bleibt fuer den Buero-Admin einsehbar.
    assert admin.get(f"/zeiterfassung/team/{world.dennis_id}").status_code == 200


def test_office_admin_cannot_delete_self_or_last_admin(app, world):
    admin = login(app, "admin-a@example.com")
    admin.post(f"/settings/users/{world.admin_a_id}/loeschen")
    assert _reload(world.admin_a_id).deleted_at is None


def test_deactivated_employee_cannot_login(app, world, db):
    with bypass_tenant_scope():
        db.session.get(User, world.laura_id).is_active = False
        db.session.commit()
    resp = app.test_client().post("/auth/login", data={"login_type": "email", "identifier": "laura@example.com", "password": PASSWORD})
    assert resp.status_code == 200
    assert "deaktiviert" in resp.get_data(as_text=True)


# --- Sessions ----------------------------------------------------------------------------------


def test_password_change_ends_other_sessions(app, world):
    first = login(app, "dennis@example.com")
    second = login(app, "dennis@example.com")
    resp = second.post(
        "/settings/profile",
        data={"current_password": PASSWORD, "new_password": "neues-passwort-9", "new_password_confirm": "neues-passwort-9"},
    )
    assert resp.status_code == 302
    assert second.get("/zeiterfassung").status_code == 200
    assert first.get("/zeiterfassung").status_code == 302


def test_legacy_session_format_is_rejected(app, world):
    client = app.test_client()
    with client.session_transaction() as sess:
        sess["_user_id"] = str(world.dennis_id)
        sess["_fresh"] = True
    assert client.get("/zeiterfassung").status_code == 302


def test_login_is_rate_limited_per_ip(app, world):
    app.config["LOGIN_MAX_FAILURES_PER_IP"] = 3
    client = app.test_client()
    for _ in range(3):
        client.post("/auth/login", data={"login_type": "email", "identifier": "dennis@example.com", "password": "falsch"})
    resp = client.post("/auth/login", data={"login_type": "email", "identifier": "dennis@example.com", "password": PASSWORD})
    assert resp.status_code == 429
