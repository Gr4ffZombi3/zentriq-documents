"""SUPER_ADMIN-Panel: Buero-Admin festlegen, Systemuebersicht, Fehlerprotokoll - und kein
Zugriff auf fachliche Daten fremder (oder eigener) Bueros."""

import pytest

from app.models import SystemErrorEvent, Tenant, User, UserRole
from app.services.system_errors import record_system_error
from app.tenancy import bypass_tenant_scope
from tests.test_global_search import _login, _make_user, world  # noqa: F401


@pytest.fixture()
def office_b(world):  # noqa: F811
    return world


def _user(email):
    with bypass_tenant_scope():
        return User.query.filter_by(email=email).one()


def test_super_admin_can_make_employee_office_admin(app, db, office_b):
    client = _login(app, "plattform@example.com")
    dennis = _user("dennis@example.com")
    resp = client.post(f"/plattform/benutzer/{dennis.id}/buero-admin")
    assert resp.status_code == 302
    dennis = _user("dennis@example.com")
    assert dennis.role == UserRole.OFFICE_ADMIN
    assert "Zum Büro-Admin" not in client.get(f"/plattform/bueros/{dennis.tenant_id}").get_data(as_text=True).split("dennis@example.com")[1].split("</tr>")[0]


def test_office_admin_cannot_use_platform_actions(app, db, office_b):
    client = _login(app, "admin-a@example.com")
    laura = _user("laura@example.com")
    assert client.post(f"/plattform/benutzer/{laura.id}/buero-admin").status_code == 403
    assert _user("laura@example.com").role == UserRole.EMPLOYEE
    assert client.get("/plattform/system").status_code == 403


def test_system_page_shows_counts_versions_and_errors(app, db, office_b):
    record_system_error("task", "TimeoutError", location="app.tasks.document_tasks.process_document", tenant_id=office_b.id)
    html = _login(app, "plattform@example.com").get("/plattform/system").get_data(as_text=True)
    assert "Benutzer gesamt" in html and "Büro-Admins (aktiv)" in html
    assert "Datenbankschema" in html and "Python" in html
    assert "TimeoutError" in html and "Büro B" in html


def test_unhandled_request_error_is_recorded_without_content(app, db, tenant):
    @app.get("/_test-fehler")
    def boom():
        raise RuntimeError("Geheimer Kundenname Müller in der Fehlermeldung")

    app.config["PROPAGATE_EXCEPTIONS"] = False
    resp = app.test_client().get("/_test-fehler?kunde=Mueller")
    assert resp.status_code == 500
    event = SystemErrorEvent.query.order_by(SystemErrorEvent.id.desc()).first()
    assert (event.source, event.error_type, event.location) == ("request", "RuntimeError", "GET /_test-fehler")
    stored = " ".join(str(value) for value in (event.source, event.error_type, event.location))
    assert "Müller" not in stored and "Mueller" not in stored


def test_http_errors_are_not_recorded(app, db, tenant):
    app.test_client().get("/gibt-es-nicht")
    assert SystemErrorEvent.query.count() == 0


@pytest.mark.parametrize(
    "url",
    [
        "/search?q=Müller",
        "/leipziger-liste",
        "/leipziger-liste/mitarbeiter",
        "/customers",
        "/sprachnachrichten",
        "/zeiterfassung",
        "/zeiterfassung/team",
        "/uebersicht",
        "/mitarbeiter",
        "/aktivitaeten",
        "/documents",
    ],
)
def test_super_admin_never_reaches_office_data(app, office_b, url):
    assert _login(app, "plattform@example.com").get(url).status_code == 403


def test_platform_pages_contain_no_office_business_data(app, db, office_b):
    client = _login(app, "plattform@example.com")
    for url in ("/plattform/bueros", f"/plattform/bueros/{office_b.id}", "/plattform/benutzer", "/plattform/system", "/plattform/sicherheit"):
        html = client.get(url).get_data(as_text=True)
        # Kundennamen, Telefon und Vertragsnummern aus den Listen beider Bueros.
        for secret in ("Müller Hans", "Fremdbüro", "0341", "720/111111-A-14", "720/999999-Z-14"):
            assert secret not in html, (url, secret)
    assert Tenant.query.count() == 2


def test_failed_import_is_recorded_with_stage_label_only(app, auth_client, db, tmp_path, monkeypatch):
    from tests.test_leipziger_parser import PAGE_1, _upload, make_list_pdf

    def fail(*args, **kwargs):
        raise RuntimeError("Kunde Müller, Vertrag 720/111111-A-14")

    monkeypatch.setattr("app.tasks.document_tasks.extract_text", fail)
    app.config["UPLOAD_FOLDER"] = str(tmp_path)
    _upload(auth_client, make_list_pdf([PAGE_1]))
    event = SystemErrorEvent.query.filter_by(source="import").one()
    assert event.error_type == "OCR fehlgeschlagen"
    assert "Müller" not in f"{event.location} {event.error_type}"
