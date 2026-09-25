import logging

import pytest

from app.cli import create_user_command
from app.models import Tenant, User
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


def _run(app, *args):
    return app.test_cli_runner().invoke(create_user_command, list(args))


def _find_user(email):
    with bypass_tenant_scope():
        return User.query.filter_by(email=email).first()


# --- Registrierung abgeschaltet (Standard) -----------------------------------------------


def test_registration_disabled_by_default(app):
    assert app.config["REGISTRATION_ENABLED"] is False


def test_register_returns_404_when_disabled(client):
    assert client.get("/auth/register").status_code == 404
    resp = client.post("/auth/register", data=REGISTER_DATA)
    assert resp.status_code == 404
    assert _find_user(REGISTER_DATA["email"]) is None
    assert Tenant.query.filter_by(name=REGISTER_DATA["company_name"]).first() is None


def test_login_hides_register_link_when_disabled(client):
    html = client.get("/auth/login").get_data(as_text=True)
    assert "/auth/register" not in html
    assert "Jetzt registrieren" not in html


def test_register_available_when_enabled(app, client):
    app.config["REGISTRATION_ENABLED"] = True
    assert client.get("/auth/register").status_code == 200
    assert "/auth/register" in client.get("/auth/login").get_data(as_text=True)


# --- flask create-user ---------------------------------------------------------------------


def test_create_user_creates_tenant_and_user(app, client, password_prompt):
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


def test_create_user_never_outputs_or_logs_password(app, password_prompt, caplog):
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


def test_create_user_uses_separate_tenant_per_company(app, user, password_prompt):
    password_prompt(PASSWORD, PASSWORD)
    result = _run(app, "--email", "neu@example.com", "--company", "Default Tenant")
    assert result.exit_code == 0, result.output
    created = _find_user("neu@example.com")
    assert created.tenant_id != user.tenant_id


def test_create_user_rejects_password_mismatch(app, password_prompt):
    password_prompt(PASSWORD, "etwas-anderes-123")
    result = _run(app, "--email", "mismatch@example.com", "--company", "Firma")
    assert result.exit_code != 0
    assert "stimmen nicht überein" in result.output
    assert _find_user("mismatch@example.com") is None
    assert Tenant.query.filter_by(name="Firma").first() is None


def test_create_user_rejects_short_password(app, password_prompt):
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
    result = _run(app, "--email", user.email.upper(), "--company", "Zweite Firma")
    assert result.exit_code != 0
    assert "bereits registriert" in result.output
    assert prompts == []
    assert Tenant.query.filter_by(name="Zweite Firma").first() is None


def test_create_user_rejects_duplicate_vermittlernummer(app, user, password_prompt):
    password_prompt(PASSWORD, PASSWORD)
    result = _run(
        app, "--email", "anders@example.com", "--company", "Firma", "--vermittlernummer", user.vermittlernummer
    )
    assert result.exit_code != 0
    assert "bereits registriert" in result.output
    assert _find_user("anders@example.com") is None


def test_create_user_is_registered_as_flask_command(app):
    result = app.test_cli_runner().invoke(args=["create-user", "--help"])
    assert result.exit_code == 0
    assert "--email" in result.output
    assert "--company" in result.output
