"""Vorgang ohne Kundenakte (Notiz/Termin) und automatische Zuordnung beim Import der Leipziger
Liste; Feierabendgruss und Abmelden bei laufender Zeiterfassung."""

from datetime import date, timedelta

from app.models import CalendarEvent, Customer, CustomerNote, PendingCase, Tenant, User, UserRole, WorkSession
from app.services import pending_cases, workspace
from tests.test_leipziger_parser import PAGE_1, PAGE_2, _upload, make_list_pdf, no_llm  # noqa: F401


def _create(client, **data):
    tomorrow = (date.today() + timedelta(days=1)).isoformat()
    form = {"name": "Berta Muster", "vn": "808/255635-L", "notiz": "Ruft wegen Hausrat zurück, Angebot vorbereiten.",
            "termin_datum": tomorrow, "termin_zeit": "10:30", "termin_dauer": "30", "termin_titel": "Beratung Hausrat"}
    form.update(data)
    return client.post("/arbeitsplatz/vorgaenge/neu", data=form)


def _import(app, client, db, tmp_path, pages):
    app.config["UPLOAD_FOLDER"] = str(tmp_path)
    response = _upload(client, make_list_pdf(pages))
    assert response.status_code == 201, response.get_json()


def test_case_without_file_note_appointment_and_auto_link_on_import(app, auth_client, user, db, tmp_path, no_llm):  # noqa: F811
    response = _create(auth_client)
    assert response.status_code == 302
    case = PendingCase.query.one()
    assert (case.name, case.vn_number, case.vn_key, case.status) == ("Berta Muster", "808/255635-L", "808255635L", "open")
    note = case.notes[0]
    event = CalendarEvent.query.one()
    booked = (event.starts_at, event.ends_at)
    assert event.pending_case_id == case.id and event.customer_id is None

    # Sofort im normalen Buerokalender, gekennzeichnet
    tomorrow = date.today() + timedelta(days=1)
    api = auth_client.get(f"/kalender/api/termine?von={tomorrow}&bis={tomorrow}").get_json()
    assert api["events"][0]["title"] == "Beratung Hausrat"
    assert api["events"][0]["pending_case"]["name"] == "Berta Muster"
    # Auffindbar ueber Name oder Nummer, Kennzeichnung sichtbar
    detail = auth_client.get(f"/arbeitsplatz/vorgaenge/{case.id}").get_data(as_text=True)
    assert "Noch keiner Kundenakte zugeordnet" in detail and "Ruft wegen Hausrat zurück" in detail
    for q in ("Muster", "berta", "808/255635", "808255635L"):
        assert [c.id for c in pending_cases.search(q)] == [case.id], q
    hits = auth_client.get("/arbeitsplatz/suche?q=Muster").get_json()
    assert hits["cases"][0]["label"] == "Berta Muster"
    assert "Berta Muster" in auth_client.get("/search?q=808255635").get_data(as_text=True)

    # Import der Testliste mit derselben (VM-unabhaengigen) VN -> automatisch verknuepft
    _import(app, auth_client, db, tmp_path, [PAGE_1, PAGE_2])
    db.session.expire_all()
    customer = Customer.query.filter_by(name="Berta Muster").one()
    case = db.session.get(PendingCase, case.id)
    assert (case.status, case.customer_id, case.link_basis) == ("linked", customer.id, "import")
    notes = CustomerNote.query.filter_by(customer_id=customer.id).all()
    assert len(notes) == 1
    assert (notes[0].text, notes[0].created_by_user_id) == (note.text, user.id)
    assert notes[0].created_at.replace(tzinfo=None) == note.created_at
    event = db.session.get(CalendarEvent, event.id)
    assert event.customer_id == customer.id and (event.starts_at, event.ends_at) == booked
    page = auth_client.get(f"/customers/{customer.id}").get_data(as_text=True)
    assert "Ruft wegen Hausrat zurück" in page

    # Erneuter Import: keine Kopien
    _import(app, auth_client, db, tmp_path, [PAGE_1, PAGE_2])
    db.session.expire_all()
    assert CustomerNote.query.filter_by(customer_id=customer.id).count() == 1
    assert CalendarEvent.query.count() == 1
    assert PendingCase.query.count() == 1
    # Neue Notiz am zugeordneten Vorgang landet direkt in der Akte
    auth_client.post(f"/arbeitsplatz/vorgaenge/{case.id}/notiz", data={"notiz": "Nachtrag"})
    assert CustomerNote.query.filter_by(customer_id=customer.id).count() == 2


def test_conflicting_or_missing_number_stays_open_with_candidates(app, auth_client, user, db, tmp_path, no_llm):  # noqa: F811
    # Gleiche VN wie "Anna Beispiel", aber anderer Name -> keine automatische Zuordnung
    _create(auth_client, name="Anna Meier", vn="720/307259-C", termin_datum="", termin_zeit="")
    # Ohne Nummer -> nur Auswahl ueber den Namen
    _create(auth_client, name="Carl Probe", vn="", termin_datum="", termin_zeit="")
    _import(app, auth_client, db, tmp_path, [PAGE_1, PAGE_2])
    db.session.expire_all()
    meier = PendingCase.query.filter_by(name="Anna Meier").one()
    carl = PendingCase.query.filter_by(name="Carl Probe").one()
    assert meier.status == "open" and carl.status == "open"
    reasons = {c["customer"].name: c["reasons"] for c in pending_cases.candidates(meier)}
    assert "Name weicht ab" in reasons["Anna Beispiel"]
    page = auth_client.get(f"/arbeitsplatz/vorgaenge/{carl.id}").get_data(as_text=True)
    assert "Mit dieser Akte verknüpfen" in page and "Gleicher Name" in page
    # Bis dahin nutzbar: weitere Notiz und Termin
    assert auth_client.post(f"/arbeitsplatz/vorgaenge/{carl.id}/notiz", data={"notiz": "Zweite Notiz"}).status_code == 302
    target = Customer.query.filter_by(name="Carl Probe").one()
    assert auth_client.post(f"/arbeitsplatz/vorgaenge/{carl.id}/zuordnen", data={"customer_id": target.id}).status_code == 302
    assert CustomerNote.query.filter_by(customer_id=target.id).count() == 2
    auth_client.post(f"/arbeitsplatz/vorgaenge/{carl.id}/zuordnen", data={"customer_id": target.id})  # doppelt
    assert CustomerNote.query.filter_by(customer_id=target.id).count() == 2


def test_validation_keeps_input_and_reuses_open_case(app, auth_client, employee_client, db):
    response = _create(auth_client, vn="12345", notiz="Bitte zurückrufen")
    assert response.status_code == 400
    page = response.get_data(as_text=True)
    assert "Format 808/123456-E" in page and "Bitte zurückrufen" in page and 'value="12345"' in page
    assert _create(auth_client, termin_datum="", termin_zeit="", notiz="").status_code == 400
    assert PendingCase.query.count() == 0
    # Mitarbeiter darf erfassen; gleiche VN verwendet den offenen Vorgang weiter
    assert _create(employee_client, termin_datum="", termin_zeit="").status_code == 302
    assert _create(auth_client, name="Berta  Muster", termin_datum="", termin_zeit="", notiz="Zweiter Anruf").status_code == 302
    assert PendingCase.query.count() == 1 and len(PendingCase.query.one().notes) == 2


def test_cases_are_office_scoped(app, auth_client, db, user):
    _create(auth_client, termin_datum="", termin_zeit="")
    case = PendingCase.query.one()
    other_tenant = Tenant(name="Anderes Büro", slug="anderes")
    db.session.add(other_tenant)
    db.session.flush()
    stranger = User(tenant_id=other_tenant.id, email="fremd@example.com", role=UserRole.OFFICE_ADMIN, is_active=True)
    stranger.set_password("fremdpasswort123")
    db.session.add(stranger)
    db.session.commit()
    client = app.test_client()
    client.post("/auth/login", data={"login_type": "email", "identifier": stranger.email, "password": "fremdpasswort123"})
    assert client.get(f"/arbeitsplatz/vorgaenge/{case.id}").status_code == 404
    assert client.get("/arbeitsplatz/suche?q=Muster").get_json()["cases"] == []


def test_empty_search_offers_case_capture(app, auth_client, employee_client, db):
    assert "Vorgang ohne Kundenakte erfassen" in auth_client.get("/customers?q=Niemand").get_data(as_text=True)
    assert "Vorgang ohne Kundenakte erfassen" in employee_client.get("/search?q=Niemand").get_data(as_text=True)
    hits = auth_client.get("/arbeitsplatz/suche?q=Niemand").get_json()
    assert hits["create"].endswith("/arbeitsplatz/vorgaenge/neu?name=Niemand")


# --- Feierabendgruss und Abmelden ------------------------------------------------------------


def test_format_worked():
    assert workspace.format_worked(4 * 3600 + 2 * 60 + 59) == "4 Std. 02 Min."
    assert workspace.format_worked(0) == "0 Std. 00 Min."


def _clock_in(client):
    assert client.post("/zeiterfassung/einstempeln", headers={"Accept": "application/json"}).status_code == 200


def test_clock_out_returns_farewell_with_today(app, auth_client, db, user):
    _clock_in(auth_client)
    data = auth_client.post("/zeiterfassung/ausstempeln", headers={"Accept": "application/json"}).get_json()
    assert data["status"] == "out"
    assert data["farewell"] == {"title": "Schönen Feierabend!", "detail": "Heute erfasst: " + workspace.format_worked(data["today_net_seconds"])}
    # Formular ohne JavaScript: Karte auf der naechsten Seite, genau einmal
    _clock_in(auth_client)
    auth_client.post("/zeiterfassung/ausstempeln")
    page = auth_client.get("/zeiterfassung").get_data(as_text=True)
    assert "data-zq-farewell" in page and "Heute erfasst:" in page
    assert "data-zq-farewell" not in auth_client.get("/zeiterfassung").get_data(as_text=True)
    # Fehlgeschlagen (schon ausgestempelt): kein Gruss
    failed = auth_client.post("/zeiterfassung/ausstempeln", headers={"Accept": "application/json"})
    assert failed.status_code == 409 and "farewell" not in failed.get_json()


def test_logout_clock_out_and_logout(app, auth_client, db, user):
    _clock_in(auth_client)
    assert 'data-zs-logout-dialog' in auth_client.get("/zeiterfassung").get_data(as_text=True)
    response = auth_client.post("/auth/logout", data={"stempel": "aus"})
    assert response.status_code == 302 and "/auth/login" in response.headers["Location"]
    db.session.expire_all()
    assert WorkSession.query.filter_by(user_id=user.id).one().ended_at is not None
    login = auth_client.get("/auth/login").get_data(as_text=True)
    assert "Du bist ausgestempelt (heute erfasst:" in login
    assert "Schönen Feierabend!" in login and "data-zq-farewell" in login
    assert auth_client.get("/zeiterfassung").status_code == 302  # abgemeldet


def test_logout_keep_running(app, auth_client, db, user):
    _clock_in(auth_client)
    auth_client.post("/auth/logout", data={"stempel": "weiter"})
    db.session.expire_all()
    assert WorkSession.query.filter_by(user_id=user.id).one().ended_at is None
    login = auth_client.get("/auth/login").get_data(as_text=True)
    assert "Deine Zeiterfassung läuft weiter" in login and "data-zq-farewell" not in login


def test_logout_stays_logged_in_when_clock_out_fails(app, auth_client, db, user, monkeypatch):
    from app.services.timetracking import service

    _clock_in(auth_client)

    def boom(_user):
        raise service.TimeTrackingError("Die Buchung wurde bereits geändert. Bitte Seite neu laden.")

    monkeypatch.setattr(service, "clock_out", boom)
    response = auth_client.post("/auth/logout", data={"stempel": "aus"}, headers={"Referer": "http://localhost/zeiterfassung"})
    assert response.status_code == 302 and response.headers["Location"] == "/zeiterfassung"
    page = auth_client.get("/zeiterfassung").get_data(as_text=True)
    assert "Ausstempeln nicht möglich" in page and "weiterhin angemeldet" in page
    db.session.expire_all()
    assert WorkSession.query.filter_by(user_id=user.id).one().ended_at is None
