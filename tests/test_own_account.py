# ruff: noqa: F811  (Fixtures werden aus anderen Testmodulen importiert)
"""Mein Konto -> Kontodaten bearbeiten (nur Buero-Admin, nur das eigene Konto)."""

import re

import pytest

from app.extensions import db as _db
from app.models import LeipzigerEntry, User, UserRole
from app.services import mailer
from app.tenancy import bypass_tenant_scope
from tests.test_permissions_security import PASSWORD, login, world  # noqa: F401

URL = "/settings/profile/kontodaten"


@pytest.fixture()
def mails(app, monkeypatch):
    app.config.update(SMTP_HOST="smtp.test", MAIL_FROM="noreply@example.com", PUBLIC_URL="https://zentriq.test")
    sent = []
    monkeypatch.setattr(mailer, "send_email", lambda to, subject, body: sent.append((to, subject, body)))
    return sent


def _user(user_id):
    with bypass_tenant_scope():
        _db.session.expire_all()
        return _db.session.get(User, user_id)


def _post(client, **data):
    page = client.get(URL)
    assert page.status_code == 200
    token = re.search(r'name="csrf_token" type="hidden" value="([^"]+)"', page.get_data(as_text=True))
    payload = {"csrf_token": token.group(1) if token else ""}
    payload.update(data)
    return client.post(URL, data=payload, follow_redirects=True)


def _form(user, **overrides):
    profile = user.employee_profile
    data = {"display_name": profile.display_name if profile else "", "email": user.email, "vermittlernummer": user.vermittlernummer or ""}
    data.update(overrides)
    return data


def test_profile_shows_edit_action_only_for_office_admin(app, world):
    admin_page = login(app, "admin-a@example.com").get("/settings/profile").get_data(as_text=True)
    assert "Kontodaten bearbeiten" in admin_page and "pflegen Sie als Büro-Admin selbst" in admin_page
    employee = login(app, "laura@example.com")
    page = employee.get("/settings/profile").get_data(as_text=True)
    assert "Kontodaten bearbeiten" not in page and "pflegt Ihr Büro-Admin" in page
    assert employee.get(URL).status_code == 403
    assert employee.post(URL, data={"display_name": "Hack"}).status_code == 403


def test_name_can_be_set_and_is_shown_after_reload(app, world):
    admin = login(app, "admin-a@example.com")
    r = _post(admin, **_form(_user(world.admin_a_id), display_name="Anna Admin-Neu"))
    assert "Kontodaten gespeichert." in r.get_data(as_text=True)
    assert "Anna Admin-Neu" in admin.get("/settings/profile").get_data(as_text=True)


def test_vermittlernummer_first_time_with_password_and_impact_hint(app, world):
    admin = login(app, "admin-a@example.com")
    with bypass_tenant_scope():
        u = _db.session.get(User, world.admin_a_id)
        u.vermittlernummer = None
        _db.session.commit()
    # ohne Passwort: Hinweis auf Auswirkungen, nichts gespeichert
    r = _post(admin, **_form(_user(world.admin_a_id), vermittlernummer="080777z"))
    html = r.get_data(as_text=True)
    assert "Auswirkungen der neuen Vermittlernummer" in html and "Leipziger Liste" in html and "Abrechnungen" in html
    assert _user(world.admin_a_id).vermittlernummer is None
    # bestaetigt, aber ohne Passwort: abgewiesen
    r = _post(admin, **_form(_user(world.admin_a_id), vermittlernummer="080777z", confirm_impact="y"))
    assert "aktuelles Passwort" in r.get_data(as_text=True) and _user(world.admin_a_id).vermittlernummer is None
    r = _post(admin, **_form(_user(world.admin_a_id), vermittlernummer="080777z", confirm_impact="y", current_password=PASSWORD))
    assert "Kontodaten gespeichert." in r.get_data(as_text=True)
    assert _user(world.admin_a_id).vermittlernummer == "08/0777-Z"  # gleiche Normalisierung wie Bueroverwaltung
    assert "08/0777-Z" in admin.get("/settings/profile").get_data(as_text=True)


def test_vermittlernummer_change_names_concrete_assignment_changes(app, world):
    admin = login(app, "admin-a@example.com")
    r = _post(admin, **_form(_user(world.admin_a_id), vermittlernummer="08/5555-B"))
    assert "bereits vergeben" in r.get_data(as_text=True)  # Laura hat sie
    with bypass_tenant_scope():
        n_dennis = LeipzigerEntry.query.filter(LeipzigerEntry.broker_key == "081234A").count()
    with bypass_tenant_scope():
        dennis = _db.session.get(User, world.dennis_id)
        dennis.vermittlernummer = None  # Nummer wird frei
        _db.session.commit()
    r = _post(admin, **_form(_user(world.admin_a_id), vermittlernummer="08/1234-A"))
    html = r.get_data(as_text=True)
    assert f"künftig {n_dennis} statt bisher" in html and "nicht automatisch umgeordnet" in html
    assert _user(world.admin_a_id).vermittlernummer == "08/0001-A"


def test_email_change_requires_reauth_and_confirmation(app, world, mails):
    admin = login(app, "admin-a@example.com")
    r = _post(admin, **_form(_user(world.admin_a_id), email="kein-format"))
    assert "gültige E-Mail" in r.get_data(as_text=True)
    r = _post(admin, **_form(_user(world.admin_a_id), email="laura@example.com", current_password=PASSWORD))
    assert "bereits vergeben" in r.get_data(as_text=True)
    r = _post(admin, **_form(_user(world.admin_a_id), email="neu-admin@example.com"))
    assert "aktuelles Passwort" in r.get_data(as_text=True) and not mails
    r = _post(admin, **_form(_user(world.admin_a_id), email="neu-admin@example.com", current_password=PASSWORD))
    page = r.get_data(as_text=True)
    assert "Bestätigungslink an neu-admin@example.com" in page and "wartet auf Bestätigung" in page
    assert _user(world.admin_a_id).email == "admin-a@example.com"  # alte Adresse bleibt aktiv
    [(to, _subject, body)] = mails
    assert to == "neu-admin@example.com"
    token = re.search(r"#token=(\S+)", body).group(1)
    assert "/settings/profile/email-bestaetigen#token=" in body  # Token nur im Fragment

    # fremdes Konto kann das Token nicht verwenden
    other = login(app, "admin-b@example.com")
    csrf = re.search(r'name="csrf_token" value="([^"]+)"', other.get("/settings/profile/email-bestaetigen").get_data(as_text=True)).group(1)
    r = other.post("/settings/profile/email-bestaetigen", data={"csrf_token": csrf, "token": token}, follow_redirects=True)
    assert "anderen Konto" in r.get_data(as_text=True) and _user(world.admin_a_id).email == "admin-a@example.com"

    csrf = re.search(r'name="csrf_token" value="([^"]+)"', admin.get("/settings/profile/email-bestaetigen").get_data(as_text=True)).group(1)
    r = admin.post("/settings/profile/email-bestaetigen", data={"csrf_token": csrf, "token": token}, follow_redirects=True)
    assert "neu-admin@example.com" in r.get_data(as_text=True)
    assert _user(world.admin_a_id).email == "neu-admin@example.com"
    assert mails[-1][0] == "admin-a@example.com"  # Hinweis an die bisherige Adresse
    # Token ist verbraucht (an die alte Adresse gebunden)
    r = admin.post("/settings/profile/email-bestaetigen", data={"csrf_token": csrf, "token": token}, follow_redirects=True)
    assert "nicht mehr gültig" in r.get_data(as_text=True)
    # Anmeldung mit neuer Adresse
    fresh = app.test_client()
    assert fresh.post("/auth/login", data={"login_type": "email", "identifier": "neu-admin@example.com", "password": PASSWORD}).status_code == 302


def test_office_role_and_foreign_account_cannot_be_changed(app, world):
    admin = login(app, "admin-a@example.com")
    before_b = _user(world.admin_b_id)
    b_email, b_name = before_b.email, before_b.employee_profile.display_name if before_b.employee_profile else None
    r = _post(admin, **_form(_user(world.admin_a_id), display_name="Anna X", role="super_admin", tenant_id=str(world.tenant_b.id),
                             is_active="", user_id=str(world.admin_b_id)))
    assert r.status_code == 200
    me = _user(world.admin_a_id)
    assert me.role == UserRole.OFFICE_ADMIN and me.tenant_id == world.tenant_a.id and me.is_active
    assert me.employee_profile.display_name == "Anna X"
    other = _user(world.admin_b_id)
    assert other.email == b_email and (other.employee_profile.display_name if other.employee_profile else None) == b_name
    assert admin.get(URL + "?user_id=%d" % world.admin_b_id).status_code == 200
    assert "admin-b@example.com" not in admin.get(URL + "?user_id=%d" % world.admin_b_id).get_data(as_text=True)


def test_email_change_refused_when_mail_not_configured(app, world):
    app.config.update(SMTP_HOST=None)
    admin = login(app, "admin-a@example.com")
    r = _post(admin, **_form(_user(world.admin_a_id), email="neu@example.com", current_password=PASSWORD))
    assert "E-Mail-Versand ist nicht eingerichtet" in r.get_data(as_text=True)
    assert _user(world.admin_a_id).email == "admin-a@example.com"
