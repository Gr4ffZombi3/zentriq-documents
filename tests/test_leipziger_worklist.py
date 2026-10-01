"""Leipziger Liste: "Zu erledigen" (Zeilen ohne Datum) und "Mitarbeiter" (Zuordnung ueber die
Vermittlernummer, getrennt nach ohne/mit Datum)."""

import pytest

from app.models import (
    Customer,
    DocStatus,
    DocType,
    Document,
    DocumentCustomer,
    EmployeeProfile,
    User,
    UserRole,
)


def _row(number, broker, start=None, status="ANG"):
    return {
        "customer": {"name": "Kunde"},
        "contract_number": number,
        "broker_number": broker,
        "contract_start_date": start,
        "status_code": status,
        "is_angebot": status == "ANG",
        "is_neugeschaeft": status == "NEU",
        "is_fahrzeugwechsel": status == "FZW",
        "is_storno": False,
    }


def _make_list(db, tenant, rows, name="liste.pdf", status=DocStatus.DONE):
    document = Document(
        tenant_id=tenant.id,
        filename=name,
        original_filename=name,
        file_path=f"/tmp/{name}",
        status=status,
        doc_type=DocType.LEIPZIGER_LISTE,
    )
    customer = Customer(tenant_id=tenant.id, name=f"Kunde {name}")
    db.session.add_all([document, customer])
    db.session.flush()
    db.session.add(DocumentCustomer(tenant_id=tenant.id, document_id=document.id, customer_id=customer.id, row_data=rows))
    db.session.commit()
    return document


def _make_user(db, tenant, email, vm, name, role=UserRole.MITARBEITER, password="mitarbeiterpass123"):
    user = User(tenant_id=tenant.id, email=email, vermittlernummer=vm, role=role, is_active=True)
    user.set_password(password)
    db.session.add(user)
    db.session.flush()
    db.session.add(EmployeeProfile(tenant_id=tenant.id, user_id=user.id, display_name=name))
    db.session.commit()
    return user


def _login(app, email, password="mitarbeiterpass123"):
    client = app.test_client()
    client.post("/auth/login", data={"login_type": "email", "identifier": email, "password": password})
    return client


ROWS = [
    _row("808/19442-J", "08/0950-T", status="KLÄ"),
    _row("608/585466-A", "08/0950-T", start="2026-07-10"),
    _row("708/101630-X", "08/1234-A", status="ANG"),
    _row("508/570074-X", "081234-A", start="2026-07-07", status="FZW"),
    _row("408/130322-L", "08/9999-Z", status="NEU"),
]


@pytest.fixture()
def team(db, tenant):
    dennis = _make_user(db, tenant, "dennis@example.com", "081234-A", "Dennis")
    laura = _make_user(db, tenant, "laura@example.com", "08/5555-B", "Laura")
    _make_list(db, tenant, ROWS)
    return dennis, laura


def test_todo_shows_only_rows_without_date_for_admin(auth_client, team):
    html = auth_client.get("/leipziger-liste").get_data(as_text=True)
    assert "808/19442-J" in html and "Klärungsbedarf" in html
    assert "708/101630-X" in html and "Angebot offen" in html
    assert "408/130322-L" in html  # keinem Benutzer zugeordnet, aber offen
    assert "608/585466-A" not in html  # mit Datum -> erledigt
    assert "508/570074-X" not in html


def test_employee_sees_only_own_open_rows(app, team):
    client = _login(app, "dennis@example.com")
    resp = client.get("/leipziger-liste")
    html = resp.get_data(as_text=True)
    assert resp.status_code == 200
    assert "708/101630-X" in html
    assert "808/19442-J" not in html  # gehoert 08/0950-T
    assert "408/130322-L" not in html
    assert "508/570074-X" not in html  # eigene Zeile, aber mit Datum


def test_employee_without_number_sees_nothing(app, db, tenant, team):
    _make_user(db, tenant, "ohne@example.com", None, "Ohne Nummer")
    html = _login(app, "ohne@example.com").get("/leipziger-liste").get_data(as_text=True)
    assert "keine Vermittlernummer hinterlegt" in html
    assert "708/101630-X" not in html and "808/19442-J" not in html


def test_employee_cannot_open_team_view(app, team):
    assert _login(app, "dennis@example.com").get("/leipziger-liste/mitarbeiter").status_code == 403


def test_team_view_splits_by_date(auth_client, team):
    dennis, laura = team
    html = auth_client.get(f"/leipziger-liste/mitarbeiter?mitarbeiter={dennis.id}").get_data(as_text=True)
    assert "Dennis" in html and "Laura" in html
    assert "708/101630-X" in html
    assert "508/570074-X" not in html

    html = auth_client.get(f"/leipziger-liste/mitarbeiter?mitarbeiter={dennis.id}&ansicht=mit").get_data(as_text=True)
    assert "508/570074-X" in html and "07.07.2026" in html
    assert "708/101630-X" not in html

    html = auth_client.get(f"/leipziger-liste/mitarbeiter?mitarbeiter={laura.id}").get_data(as_text=True)
    assert "Keine offenen Einträge" in html


def test_team_view_rejects_foreign_tenant_user(auth_client, db, team):
    from app.models import Tenant
    from app.tenancy import use_tenant_id

    other = Tenant(name="Fremd", slug="fremd-team")
    db.session.add(other)
    db.session.commit()
    with use_tenant_id(other.id):
        stranger = _make_user(db, other, "fremd@example.com", "08/7777-C", "Fremd")
        _make_list(db, other, [_row("999/00001-A", "08/1234-A")], name="fremd.pdf")
    assert auth_client.get(f"/leipziger-liste/mitarbeiter?mitarbeiter={stranger.id}").status_code == 404
    html = auth_client.get("/leipziger-liste/mitarbeiter").get_data(as_text=True)
    assert "Fremd" not in html


def test_lists_of_other_tenants_are_never_shown(app, db, tenant, team):
    from app.models import Tenant
    from app.tenancy import use_tenant_id

    other = Tenant(name="Fremd", slug="fremd-ll")
    db.session.add(other)
    db.session.commit()
    with use_tenant_id(other.id):
        _make_list(db, other, [_row("999/00001-A", "08/1234-A")], name="fremd.pdf")
    html = _login(app, "dennis@example.com").get("/leipziger-liste").get_data(as_text=True)
    assert "999/00001-A" not in html and "fremd.pdf" not in html


def test_latest_list_is_default_and_older_selectable(auth_client, db, tenant, team):
    from datetime import datetime, timedelta, timezone

    newest = _make_list(db, tenant, [_row("111/11111-A", "08/1234-A")], name="neu.pdf")
    newest.uploaded_at = datetime.now(timezone.utc) + timedelta(hours=1)
    db.session.commit()
    html = auth_client.get("/leipziger-liste").get_data(as_text=True)
    assert "111/11111-A" in html and "808/19442-J" not in html

    older = Document.query.filter_by(original_filename="liste.pdf").one()
    html = auth_client.get(f"/leipziger-liste?document_id={older.id}").get_data(as_text=True)
    assert "808/19442-J" in html and "111/11111-A" not in html


def test_unprocessed_lists_are_ignored(auth_client, db, tenant):
    _make_list(db, tenant, [_row("222/22222-B", "08/1234-A")], name="laeuft.pdf", status=DocStatus.AI_PROCESSING)
    html = auth_client.get("/leipziger-liste").get_data(as_text=True)
    assert "222/22222-B" not in html
    assert "noch keine ausgewertete Leipziger Liste" in html


def test_set_admin_updates_existing_user_without_duplicate(app, db, tenant, employee):
    runner = app.test_cli_runner()
    result = runner.invoke(
        args=["set-admin", "--email", employee.email.upper(), "--vermittlernummer", "080950-T", "--password-stdin"],
        input="neuesSicheresPw1\n",
    )
    assert result.exit_code == 0, result.output
    assert "neuesSicheresPw1" not in result.output
    users = User.query.filter(User.email == employee.email).all()
    assert len(users) == 1
    user = users[0]
    assert user.role == UserRole.ADMIN and user.is_active
    assert user.vermittlernummer == "08/0950-T"
    assert user.password_hash.startswith("scrypt:") and "neuesSicheresPw1" not in user.password_hash
    assert user.check_password("neuesSicheresPw1")


def test_set_admin_creates_user_in_given_tenant(app, db, tenant):
    runner = app.test_cli_runner()
    result = runner.invoke(
        args=["set-admin", "--email", "chef@example.com", "--tenant", tenant.slug, "--password-stdin"],
        input="neuesSicheresPw1\n",
    )
    assert result.exit_code == 0, result.output
    user = User.query.filter_by(email="chef@example.com").one()
    assert user.is_admin and user.tenant_id == tenant.id


def test_set_admin_rejects_number_of_other_user(app, db, tenant, team):
    runner = app.test_cli_runner()
    result = runner.invoke(
        args=["set-admin", "--email", "chef@example.com", "--tenant", tenant.slug, "--vermittlernummer", "08/1234-A", "--password-stdin"],
        input="neuesSicheresPw1\n",
    )
    assert result.exit_code != 0
    assert User.query.filter_by(email="chef@example.com").first() is None
