"""Arbeitsplatz: Tagesbegruessung/Startansicht, Was ist neu?, gespeicherte Ansichten,
Formularentwuerfe, Schnellsuche, Seitenfenster, Feldbearbeitung, Storno-Filter und
Fortschritt nach Fehlern - inklusive Rechtepruefung je Rolle."""

from datetime import date, timedelta

import pytest

from app.models import (
    Customer,
    DocStatus,
    DocType,
    Document,
    DocumentCustomer,
    EmployeeProfile,
    FormDraft,
    OfficeNews,
    SavedView,
    User,
    UserPreference,
    UserRole,
)
from app.services import workspace

PASSWORD = "geheimespasswort123"


def _make_user(db, tenant, email, role=UserRole.EMPLOYEE, name=None, vm=None):
    user = User(tenant_id=tenant.id, email=email, role=role, is_active=True, vermittlernummer=vm)
    user.set_password(PASSWORD)
    db.session.add(user)
    db.session.flush()
    if name:
        db.session.add(EmployeeProfile(tenant_id=tenant.id, user_id=user.id, display_name=name))
    db.session.commit()
    return user


def _login(app, user):
    client = app.test_client()
    response = client.post("/auth/login", data={"login_type": "email", "identifier": user.email, "password": PASSWORD})
    assert response.status_code == 302
    return client


@pytest.fixture()
def laura(db, tenant):
    return _make_user(db, tenant, "laura@example.com", UserRole.OFFICE_ADMIN, name="Laura Beispiel", vm="VM-2001")


@pytest.fixture()
def mitarbeiter(db, tenant):
    return _make_user(db, tenant, "mia@example.com", UserRole.EMPLOYEE, name="Mia Muster", vm="VM-2002")


@pytest.fixture()
def super_admin(db, tenant):
    return _make_user(db, tenant, "root@example.com", UserRole.SUPER_ADMIN)


def _start_page(client):
    response = client.get("/")
    assert response.status_code == 302
    return client.get(response.headers["Location"])


# --- Tagesbegruessung ------------------------------------------------------------------------


def test_greeting_once_per_day_on_start_view(app, db, laura):
    client = _login(app, laura)
    page = _start_page(client).get_data(as_text=True)
    assert "Hallo Laura, wie geht’s dir heute?" in page
    assert "data-zq-greeting" in page
    # Neuladen und weiterer Tab: nicht noch einmal
    assert "data-zq-greeting" not in _start_page(client).get_data(as_text=True)
    other_tab = app.test_client()
    with client.session_transaction() as session:
        cookie = dict(session)
    with other_tab.session_transaction() as session:
        session.update(cookie)
    assert "data-zq-greeting" not in _start_page(other_tab).get_data(as_text=True)
    # Zweiter Login am selben Tag: nicht noch einmal
    second = _login(app, laura)
    assert "data-zq-greeting" not in _start_page(second).get_data(as_text=True)


def test_greeting_returns_after_day_change(app, db, laura):
    client = _login(app, laura)
    _start_page(client)
    pref = UserPreference.query.filter_by(user_id=laura.id).one()
    pref.last_login_day = workspace.office_today() - timedelta(days=1)
    db.session.commit()
    again = _login(app, laura)
    assert "Hallo Laura" in _start_page(again).get_data(as_text=True)


def test_day_boundary_uses_berlin_time(app, db, laura):
    from datetime import datetime, timezone

    # 23:30 UTC am 10.11. ist in Berlin bereits der 11.11.
    late = datetime(2026, 11, 10, 23, 30, tzinfo=timezone.utc)
    assert workspace.office_today(late) == date(2026, 11, 11)
    workspace.record_login(laura, datetime(2026, 11, 10, 22, 0, tzinfo=timezone.utc))
    db.session.commit()
    workspace.record_login(laura, late)
    db.session.commit()
    pref = UserPreference.query.filter_by(user_id=laura.id).one()
    assert pref.greeting_due_on == date(2026, 11, 11)


def test_greeting_without_display_name_and_no_news(app, db, tenant):
    user = _make_user(db, tenant, "ohne-name@example.com", UserRole.EMPLOYEE, vm="VM-2010")
    client = _login(app, user)
    client.get("/arbeitsplatz/neuigkeiten")  # alles gelesen
    pref = UserPreference.query.filter_by(user_id=user.id).one()
    pref.last_login_day = None
    db.session.commit()
    page = _start_page(_login(app, user)).get_data(as_text=True)
    assert "Hallo, wie geht’s dir heute?" in page
    assert "ohne-name@example.com, wie" not in page
    assert "Heute gibt es keine Neuerungen." in page


def test_greeting_shows_news_since_last_visit(app, db, laura):
    client = _login(app, laura)
    _start_page(client)
    db.session.add(OfficeNews(tenant_id=laura.tenant_id, published_on=workspace.office_today(), title="Neue Bürozeiten", body="Ab Montag 8 Uhr.", audience="alle"))
    pref = UserPreference.query.filter_by(user_id=laura.id).one()
    pref.last_login_day = None
    pref.news_seen_at = pref.news_seen_at - timedelta(days=2)
    db.session.commit()
    page = _start_page(_login(app, laura)).get_data(as_text=True)
    assert "Neue Bürozeiten" in page


def test_start_view_choice(app, db, laura, mitarbeiter):
    client = _login(app, laura)
    client.get("/")
    response = client.post("/arbeitsplatz/startansicht", data={"start_view": "calendar.index"})
    assert response.status_code == 302
    assert _login(app, laura).get("/").headers["Location"].endswith("/kalender")
    # Mitarbeiter: Kundenstamm nicht waehlbar
    staff = _login(app, mitarbeiter)
    staff.post("/arbeitsplatz/startansicht", data={"start_view": "customers.list_customers"})
    assert UserPreference.query.filter_by(user_id=mitarbeiter.id).one().start_view is None
    assert "customers.list_customers" not in dict(workspace.start_view_choices(mitarbeiter))


# --- Was ist neu? ----------------------------------------------------------------------------


def test_news_history_filtered_by_role(app, db, laura, mitarbeiter):
    admin = _login(app, laura)
    admin.post("/arbeitsplatz/neuigkeiten", data={"title": "Nur Admins", "body": "Interna", "audience": "admin", "published_on": date.today().isoformat()})
    admin.post("/arbeitsplatz/neuigkeiten", data={"title": "Für alle", "body": "Neu im Büro", "audience": "alle", "published_on": date.today().isoformat()})
    admin.post("/arbeitsplatz/neuigkeiten", data={"title": "Zukunft", "body": "Später", "audience": "alle", "published_on": (date.today() + timedelta(days=5)).isoformat()})
    admin_page = admin.get("/arbeitsplatz/neuigkeiten").get_data(as_text=True)
    assert "Nur Admins" in admin_page and "Für alle" in admin_page
    assert "Kundendaten direkt in der Akte ändern" in admin_page  # Release-Eintrag fuer Admins

    staff = _login(app, mitarbeiter)
    page = staff.get("/arbeitsplatz/neuigkeiten").get_data(as_text=True)
    assert "Für alle" in page
    assert "Nur Admins" not in page
    assert "Kundendaten direkt in der Akte ändern" not in page
    assert "Eintrag hinzufügen" not in page
    news_item = OfficeNews.query.filter_by(title="Für alle").one()
    assert staff.post("/arbeitsplatz/neuigkeiten", data={"title": "x", "body": "y"}).status_code == 403
    assert staff.post(f"/arbeitsplatz/neuigkeiten/{news_item.id}/loeschen").status_code == 403
    assert OfficeNews.query.count() == 3


def test_news_admin_edit_and_delete(app, db, laura):
    admin = _login(app, laura)
    admin.post("/arbeitsplatz/neuigkeiten", data={"title": "Alt", "body": "Text", "audience": "alle"})
    item = OfficeNews.query.one()
    admin.post("/arbeitsplatz/neuigkeiten", data={"id": item.id, "title": "Neu", "body": "Text 2", "audience": "mitarbeiter"})
    db.session.refresh(item)
    assert (item.title, item.audience) == ("Neu", "mitarbeiter")
    admin.post(f"/arbeitsplatz/neuigkeiten/{item.id}/loeschen")
    assert OfficeNews.query.count() == 0


def test_release_notes_have_no_technical_details():
    from app.release_notes import RELEASE_NOTES

    for note in RELEASE_NOTES:
        text = note["title"] + note["text"]
        assert note["audience"] in workspace.AUDIENCES
        for word in (".py", "Endpoint", "Migration", "SQL", "API", "Celery", "Commit"):
            assert word not in text


# --- Gespeicherte Ansichten ------------------------------------------------------------------


def test_saved_views_per_user_and_sanitized(app, db, laura, mitarbeiter):
    admin = _login(app, laura)
    response = admin.post(
        "/arbeitsplatz/ansichten",
        data={"page": "leipziger", "name": "Leipziger Liste · ohne Datum · ohne Storno", "tab": "ohne-datum", "storno": "ohne", "evil": "1", "q": "x" * 100},
    )
    assert response.status_code == 302
    view = SavedView.query.one()
    assert view.params == {"tab": "ohne-datum", "storno": "ohne", "q": "x" * 60}
    opened = admin.get(f"/arbeitsplatz/ansichten/{view.id}")
    assert "tab=ohne-datum" in opened.headers["Location"] and "storno=ohne" in opened.headers["Location"]
    assert "Leipziger Liste · ohne Datum · ohne Storno" in admin.get("/leipziger-liste").get_data(as_text=True)

    staff = _login(app, mitarbeiter)
    assert staff.get(f"/arbeitsplatz/ansichten/{view.id}").status_code == 404
    assert staff.post(f"/arbeitsplatz/ansichten/{view.id}/loeschen").status_code == 404
    assert "ohne Datum · ohne Storno" not in staff.get("/leipziger-liste").get_data(as_text=True)
    # Kundenstamm ist fuer Mitarbeiter gesperrt - auch keine Ansicht dafuer
    staff.post("/arbeitsplatz/ansichten", data={"page": "kunden", "name": "Alle Kunden"})
    assert SavedView.query.filter_by(user_id=mitarbeiter.id).count() == 0


# --- Formularentwuerfe -----------------------------------------------------------------------


@pytest.fixture()
def huk_enabled(app):
    app.config["HUK_ENABLED"] = True
    return app


def test_draft_roundtrip_is_private(app, db, laura, mitarbeiter, super_admin):
    admin = _login(app, laura)
    data = {"kunde": "", "plz": "04109", "fahrer": ["vn"], "csrf_token": "x"}
    response = admin.put("/arbeitsplatz/entwuerfe/huk:neu", json={"data": data})
    assert response.status_code == 200
    draft = admin.get("/arbeitsplatz/entwuerfe/huk:neu").get_json()["draft"]
    assert draft["data"] == {"kunde": "", "plz": "04109", "fahrer": ["vn"]}

    staff = _login(app, mitarbeiter)
    assert staff.get("/arbeitsplatz/entwuerfe/huk:neu").get_json()["draft"] is None
    staff.delete("/arbeitsplatz/entwuerfe/huk:neu")
    assert FormDraft.query.filter_by(user_id=laura.id).count() == 1
    assert _login(app, super_admin).get("/arbeitsplatz/entwuerfe/huk:neu").status_code == 403
    assert admin.put("/arbeitsplatz/entwuerfe/../etwas", json={"data": {}}).status_code == 404
    assert admin.put("/arbeitsplatz/entwuerfe/huk:999999", json={"data": {}}).status_code == 404

    admin.delete("/arbeitsplatz/entwuerfe/huk:neu")
    assert FormDraft.query.count() == 0


def test_draft_does_not_start_anything_and_is_dropped_after_save(app, db, laura, huk_enabled, monkeypatch):
    from app.models import HukQuoteJob

    started = []
    monkeypatch.setattr("app.services.huk.jobs.start", lambda *a, **k: started.append(a), raising=False)
    admin = _login(app, laura)
    admin.put("/arbeitsplatz/entwuerfe/huk:neu", json={"data": {"plz": "04109"}})
    assert HukQuoteJob.query.count() == 0 and not started
    page = admin.get("/kfz-angebot/neu").get_data(as_text=True)
    assert 'data-zq-draft="huk:neu"' in page
    # Absenden (auch unvollstaendig) uebernimmt die Angaben in den Auftrag; vollstaendig -> Entwurf weg
    admin.post("/kfz-angebot/neu", data={"plz": "04109", "aktion": "speichern"})
    assert not started


def test_draft_removed_after_successful_save(app, db, laura, monkeypatch):
    from app.blueprints.huk import routes as huk_routes

    class Job:
        id = 42

    monkeypatch.setattr(huk_routes.jobs, "save_draft", lambda user, job, values, customer: (Job(), [], {}))
    monkeypatch.setattr(huk_routes, "_member", lambda: None)
    monkeypatch.setattr("app.services.workspace._check_key", lambda user, key: None)
    admin = _login(app, laura)
    admin.put("/arbeitsplatz/entwuerfe/huk:neu", json={"data": {"plz": "04109"}})
    response = admin.post("/kfz-angebot/neu", data={"aktion": "speichern"})
    assert response.status_code == 302
    assert FormDraft.query.count() == 0


# --- Schnellsuche ----------------------------------------------------------------------------


def test_quick_search_pages_and_customers(app, db, laura, mitarbeiter, super_admin):
    customer = Customer(tenant_id=laura.tenant_id, name="Erika Mustermann", city="Leipzig")
    db.session.add(customer)
    db.session.commit()
    admin = _login(app, laura)
    labels = [p["label"] for p in admin.get("/arbeitsplatz/suche?q=Kalender").get_json()["pages"]]
    assert "Kalender" in labels
    assert "Abrechnungen" in [p["label"] for p in admin.get("/arbeitsplatz/suche?q=Abrechnung").get_json()["pages"]]
    hits = admin.get("/arbeitsplatz/suche?q=Mustermann").get_json()["customers"]
    assert hits[0]["url"] == f"/customers/{customer.id}"

    staff = _login(app, mitarbeiter)
    staff_hits = staff.get("/arbeitsplatz/suche?q=Mustermann").get_json()["customers"]
    assert "/customers/" not in staff_hits[0]["url"]  # Mitarbeiter: keine Kundenakte
    staff_pages = [p["url"] for p in staff.get("/arbeitsplatz/suche?q=kunden").get_json()["pages"]]
    assert "/customers" not in staff_pages
    assert _login(app, super_admin).get("/arbeitsplatz/suche?q=Kalender").status_code == 403


# --- Seitenfenster und Feldbearbeitung --------------------------------------------------------


def test_customer_panel_and_inline_edit(app, db, laura, mitarbeiter):
    customer = Customer(tenant_id=laura.tenant_id, name="Erika Mustermann")
    db.session.add(customer)
    db.session.commit()
    admin = _login(app, laura)
    panel = admin.get(f"/customers/{customer.id}?panel=1").get_data(as_text=True)
    assert "data-zq-panel-title" in panel
    assert "hauptnavigation" not in panel
    full = admin.get(f"/customers/{customer.id}").get_data(as_text=True)
    assert "hauptnavigation" in full and "data-zq-inline" in full and 'data-zq-tab-kind="kunde"' in full

    version = admin.get(f"/zentriq/api/kunden/{customer.id}").get_json()["card"]["edit"]["version"]
    bad = admin.patch(f"/zentriq/api/kunden/{customer.id}", json={"version": version, "email": "kein-mail"})
    assert bad.status_code == 400 and "E-Mail" in bad.get_json()["error"]
    ok = admin.patch(f"/zentriq/api/kunden/{customer.id}", json={"version": version, "phone_landline": "0341 1234567"})
    assert ok.status_code == 200
    assert ok.get_json()["card"]["edit"]["values"]["phone_landline"]

    staff = _login(app, mitarbeiter)
    assert staff.get(f"/customers/{customer.id}?panel=1").status_code == 403


# --- Leipziger Liste: Storno-Filter und Kundenlink -------------------------------------------


def _row(number, status="ANG", start=None, broker="VM-2002"):
    return {
        "customer": {"name": "Kunde"},
        "contract_number": number,
        "broker_number": broker,
        "contract_start_date": start,
        "status_code": status,
        "is_angebot": status == "ANG",
        "is_storno": status == "STO",
    }


def test_leipziger_without_storno_and_customer_link(app, db, tenant, laura, mitarbeiter):
    from app.services import leipziger_todo

    document = Document(tenant_id=tenant.id, filename="l.pdf", original_filename="l.pdf", file_path="/tmp/l.pdf", status=DocStatus.DONE, doc_type=DocType.LEIPZIGER_LISTE)
    customer = Customer(tenant_id=tenant.id, name="Kunde Panel")
    db.session.add_all([document, customer])
    db.session.flush()
    db.session.add(DocumentCustomer(tenant_id=tenant.id, document_id=document.id, customer_id=customer.id, row_data=[_row("720/1"), _row("720/2", "STO")]))
    db.session.commit()
    leipziger_todo._ENTRY_CACHE.clear()
    entries = leipziger_todo.load_entries(document)
    assert {e.status_key for e in entries} >= {"storno"}
    assert all(e.customer_id == customer.id for e in entries)
    assert all(e.status_key != "storno" for e in leipziger_todo.without_storno(entries))

    admin = _login(app, laura)
    page = admin.get("/leipziger-liste?tab=ohne-datum&storno=ohne").get_data(as_text=True)
    assert "720/1" in page and "720/2" not in page
    assert f'href="/customers/{customer.id}" data-zq-panel' in page
    staff_page = _login(app, mitarbeiter).get("/leipziger-liste?tab=ohne-datum").get_data(as_text=True)
    assert f"/customers/{customer.id}" not in staff_page


# --- Fortschritt ----------------------------------------------------------------------------


def test_failed_progress_explains_upload_and_retry(app, db, tenant, tmp_path):
    from app.services.document_progress import build_document_progress

    stored = tmp_path / "uploads" / "a.pdf"
    stored.parent.mkdir(parents=True, exist_ok=True)
    stored.write_bytes(b"%PDF-1.4")
    document = Document(tenant_id=tenant.id, filename="a.pdf", original_filename="a.pdf", file_path=str(stored), status=DocStatus.FAILED, error_message="OCR fehlgeschlagen: x")
    db.session.add(document)
    db.session.commit()
    recovery = build_document_progress(document)["recovery"]
    assert recovery["uploaded"] is True and recovery["can_retry"]
    assert "angekommen" in recovery["upload_text"]
    document.file_path = str(stored.parent / "fehlt.pdf")
    assert build_document_progress(document)["recovery"]["uploaded"] is False


def test_ocr_reports_real_page_numbers(app, monkeypatch):
    from app.services.ocr import pipeline

    monkeypatch.setattr(pipeline, "_native_page_texts", lambda path: ["Text " * 50, "", ""])
    monkeypatch.setattr(pipeline, "_render_pages", lambda path: [None, None, None])
    monkeypatch.setattr(pipeline.tesseract_ocr, "ocr_image", lambda image: ("erkannter Text " * 20, 95.0))
    seen = []
    pipeline.extract_text("egal.pdf", on_page=lambda n, total, ocr: seen.append((n, total)))
    assert seen == [(2, 3), (3, 3)]


def test_pages_render_workspace_elements(app, db, laura, mitarbeiter):
    admin = _login(app, laura)
    profile = admin.get("/settings/profile").get_data(as_text=True)
    assert 'id="startansicht"' in profile and "Kunden" in profile
    calendar = admin.get("/kalender").get_data(as_text=True)
    assert 'data-zq-tab-kind="kalender"' in calendar and "data-zq-search-open" in calendar
    customers = admin.get("/customers").get_data(as_text=True)
    assert "Ansicht speichern" in customers
    assert "Was ist neu?" in customers
    staff_profile = _login(app, mitarbeiter).get("/settings/profile").get_data(as_text=True)
    assert 'value="customers.list_customers"' not in staff_profile
