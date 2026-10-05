import logging

import pytest

from app.cli import create_user_command
from app.models import Tenant, User, UserRole
from app.tenancy import bypass_tenant_scope

PASSWORD = "sicheres-start-passwort"
REGISTER_DATA = {
    "company_name": "Offene Firma",
    "email": "offen@example.com",
    "vermittlernummer": "VM-9009",
    "password": "sicheres-passwort",
    "password_confirm": "sicheres-passwort",
}


@pytest.fixture()
def password_prompt(monkeypatch):
    """Ersetzt getpass durch vorgegebene Eingaben und merkt sich die Prompts."""
    prompts = []

    def configure(*answers):
        remaining = list(answers)

        def fake_getpass(prompt=""):
            prompts.append(prompt)
            return remaining.pop(0)

        monkeypatch.setattr("app.cli.getpass.getpass", fake_getpass)
        return prompts

    return configure


@pytest.fixture()
def fresh_install(db):
    """Ersteinrichtung: noch kein Mandant vorhanden (entfernt den Default-Mandanten der Test-DB)."""
    Tenant.query.delete()
    db.session.commit()


def _run(app, *args):
    return app.test_cli_runner().invoke(create_user_command, list(args))


def _find_user(email):
    with bypass_tenant_scope():
        return User.query.filter_by(email=email).first()


# --- Keine Selbstregistrierung ----------------------------------------------------------


@pytest.mark.parametrize("flag", [None, True])
def test_register_does_not_exist(app, client, flag):
    """Es gibt keine oeffentliche Registrierung - auch nicht ueber einen (frueheren) Schalter."""
    if flag is not None:
        app.config["REGISTRATION_ENABLED"] = flag
    assert "auth.register" not in app.view_functions
    assert client.get("/auth/register").status_code == 404
    assert client.post("/auth/register", data=REGISTER_DATA).status_code == 404
    assert _find_user(REGISTER_DATA["email"]) is None
    assert Tenant.query.filter_by(name=REGISTER_DATA["company_name"]).first() is None


def test_login_has_no_register_link(client):
    html = client.get("/auth/login").get_data(as_text=True)
    assert "/auth/register" not in html
    assert "registrieren" not in html.lower()


# --- flask create-user ---------------------------------------------------------------------


def test_create_user_creates_tenant_and_user(app, fresh_install, client, password_prompt):
    prompts = password_prompt(PASSWORD, PASSWORD)
    result = _run(app, "--email", " Chef@Firma.DE ", "--company", "Meine Firma GmbH", "--vermittlernummer", "VM-1")
    assert result.exit_code == 0, result.output
    assert prompts == ["Passwort: ", "Passwort wiederholen: "]

    user = _find_user("chef@firma.de")
    assert user is not None
    assert user.is_active
    assert user.vermittlernummer == "VM-1"
    assert user.check_password(PASSWORD)
    tenant = Tenant.query.filter_by(id=user.tenant_id).first()
    assert tenant.name == "Meine Firma GmbH"
    assert tenant.slug == "meine-firma-gmbh"

    login = client.post(
        "/auth/login", data={"login_type": "email", "identifier": "chef@firma.de", "password": PASSWORD}
    )
    assert login.status_code == 302


def test_create_user_never_outputs_or_logs_password(app, fresh_install, password_prompt, caplog):
    password_prompt(PASSWORD, PASSWORD)
    with caplog.at_level(logging.DEBUG):
        result = _run(app, "--email", "leise@example.com", "--company", "Leise AG")
    assert result.exit_code == 0, result.output
    assert PASSWORD not in result.output
    assert PASSWORD not in caplog.text
    assert _find_user("leise@example.com").password_hash != PASSWORD


def test_create_user_has_no_password_option(app):
    result = _run(app, "--email", "x@example.com", "--company", "X", "--password", PASSWORD)
    assert result.exit_code != 0
    assert _find_user("x@example.com") is None


def test_create_user_company_only_for_initial_setup(app, user, password_prompt):
    """Gibt es bereits einen Mandanten, legt die CLI kein weiteres Buero an - neue Bueros
    entstehen ausschliesslich ueber den SUPER_ADMIN (Plattform -> Bueros)."""
    prompts = password_prompt(PASSWORD, PASSWORD)
    result = _run(app, "--email", "neu@example.com", "--company", "Zweites Buero")
    assert result.exit_code != 0
    assert "Ersteinrichtung" in result.output
    assert prompts == []
    assert _find_user("neu@example.com") is None
    assert Tenant.query.count() == 1


def test_create_user_rejects_password_mismatch(app, fresh_install, password_prompt):
    password_prompt(PASSWORD, "etwas-anderes-123")
    result = _run(app, "--email", "mismatch@example.com", "--company", "Firma")
    assert result.exit_code != 0
    assert "stimmen nicht überein" in result.output
    assert _find_user("mismatch@example.com") is None
    assert Tenant.query.filter_by(name="Firma").first() is None


def test_create_user_rejects_short_password(app, fresh_install, password_prompt):
    password_prompt("kurz")
    result = _run(app, "--email", "kurz@example.com", "--company", "Firma")
    assert result.exit_code != 0
    assert "mindestens 8 Zeichen" in result.output
    assert _find_user("kurz@example.com") is None


def test_create_user_rejects_invalid_email(app, password_prompt):
    prompts = password_prompt(PASSWORD, PASSWORD)
    result = _run(app, "--email", "keine-mail", "--company", "Firma")
    assert result.exit_code != 0
    assert "Ungültige E-Mail-Adresse" in result.output
    assert prompts == []


def test_create_user_rejects_duplicate_email(app, user, password_prompt):
    prompts = password_prompt(PASSWORD, PASSWORD)
    result = _run(app, "--email", user.email.upper(), "--tenant", "default", "--role", "employee")
    assert result.exit_code != 0
    assert "bereits registriert" in result.output
    assert prompts == []


def test_create_user_rejects_duplicate_vermittlernummer(app, user, password_prompt):
    password_prompt(PASSWORD, PASSWORD)
    result = _run(
        app, "--email", "anders@example.com", "--tenant", "default", "--role", "employee",
        "--vermittlernummer", user.vermittlernummer,
    )
    assert result.exit_code != 0
    assert "bereits registriert" in result.output
    assert _find_user("anders@example.com") is None


def test_create_user_is_registered_as_flask_command(app):
    result = app.test_cli_runner().invoke(args=["create-user", "--help"])
    assert result.exit_code == 0
    assert "--email" in result.output
    assert "--company" in result.output


# --- Rollen / bestehender Mandant -------------------------------------------------------


def test_create_user_for_new_company_becomes_admin(app, fresh_install, password_prompt):
    password_prompt(PASSWORD, PASSWORD)
    result = _run(app, "--email", "gruender@example.com", "--company", "Gruender GmbH")
    assert result.exit_code == 0, result.output
    assert _find_user("gruender@example.com").role == UserRole.OFFICE_ADMIN


def test_create_user_in_existing_tenant_requires_role(app, tenant, password_prompt):
    password_prompt(PASSWORD, PASSWORD)
    result = _run(app, "--email", "ma@example.com", "--tenant", tenant.slug)
    assert result.exit_code != 0
    assert "--role" in result.output
    assert _find_user("ma@example.com") is None


def test_create_user_in_existing_tenant_with_role(app, tenant, password_prompt):
    password_prompt(PASSWORD, PASSWORD)
    result = _run(app, "--email", "ma@example.com", "--tenant", tenant.slug, "--role", "employee")
    assert result.exit_code == 0, result.output
    created = _find_user("ma@example.com")
    assert created.tenant_id == tenant.id
    assert created.role == UserRole.EMPLOYEE
    assert Tenant.query.count() == 1


def test_create_user_rejects_company_and_tenant_together(app, tenant, password_prompt):
    result = _run(app, "--email", "x@example.com", "--company", "X", "--tenant", tenant.slug, "--role", "office_admin")
    assert result.exit_code != 0
    assert "Genau eine" in result.output


def test_grant_super_admin_preview_and_execute(app, db, tenant, user):
    from app.cli import grant_super_admin_command
    from app.tenancy import set_current_tenant_id

    second_admin = User(tenant_id=tenant.id, email="zweiter@example.com", role=UserRole.OFFICE_ADMIN, password_hash="x")
    db.session.add(second_admin)
    db.session.commit()
    runner = app.test_cli_runner()

    # Wie auf dem Server: ohne gesetzten Tenant-Kontext.
    set_current_tenant_id(None)
    preview = runner.invoke(grant_super_admin_command, ["--vermittlernummer", "VM-1001"])
    assert preview.exit_code == 0, preview.output
    assert "Vorschau" in preview.output
    assert _find_user("test@example.com").role == UserRole.OFFICE_ADMIN

    result = runner.invoke(grant_super_admin_command, ["--email", "TEST@example.com", "--execute"])
    assert result.exit_code == 0, result.output
    assert "ist jetzt SUPER_ADMIN" in result.output
    promoted = _find_user("test@example.com")
    assert promoted.role == UserRole.SUPER_ADMIN
    assert promoted.check_password("testpassword123")
    set_current_tenant_id(tenant.id)
