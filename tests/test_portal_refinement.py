# ruff: noqa: F811  (Fixture "world" wird aus test_permissions_security importiert)
"""Portal-Ueberarbeitung: Startseite, Leipziger Suche/Reiter, Memo-Kundenabgleich, Export,
Mitarbeiterseite, Aktivitaetsprotokoll, aktive Sitzungen, Navigation und Fehlerseiten.

Nutzt das Zwei-Bueros-Szenario aus test_permissions_security (Buero A: Admin A, Dennis, Laura,
SUPER_ADMIN Justin; Buero B: Admin B, Bob). Alle Rechtepruefungen laufen ueber echte Requests."""

import io

import pytest

from app.models import AuditLog, Customer, UserSession
from app.models.audit_log import AuditEventType
from app.services.memo_customer_match import (
    extract_customer_numbers,
    extract_phone_numbers,
    normalize_phone,
)
from app.tenancy import bypass_tenant_scope, use_tenant_id
from tests.test_permissions_security import PASSWORD, _list, _row, login, world  # noqa: F401

TRANSCRIPT = (
    "Guten Tag, hier ist Max Mustermann. Sie erreichen mich unter 0171 1234567. "
    "Ich wollte wegen meiner Kfz-Versicherung anrufen."
)


def _customer(db, tenant, name, phone=None, number=None):
    with use_tenant_id(tenant.id):
        customer = Customer(tenant_id=tenant.id, name=name, phone=phone, customer_number=number)
        db.session.add(customer)
        db.session.commit()
        return customer.id


def _match(client, transcript=TRANSCRIPT):
    return client.post("/sprachnachrichten/kundenabgleich", json={"transcript": transcript})


def _audit(event_type):
    with bypass_tenant_scope():
        return AuditLog.query.filter_by(event_type=event_type).all()


# --- Telefonnummern und Kundennummern ------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    ["0171 1234567", "01711234567", "+49 171 1234567", "0049 171 1234567", "+49 (0)171 1234567", "0171/123 45 67"],
)
def test_german_phone_numbers_are_normalized_identically(raw):
    assert normalize_phone(raw) == "+491711234567"


@pytest.mark.parametrize("raw", ["1234567", "", None, "0171", "abc"])
def test_incomplete_phone_numbers_are_ignored(raw):
    assert normalize_phone(raw) is None


def test_phone_number_is_detected_in_transcript():
    assert extract_phone_numbers(TRANSCRIPT) == ["+491711234567"]
    # Datumsangaben sind keine Telefonnummern.
    assert extract_phone_numbers("Termin am 01.10.2026 um 14 Uhr") == []


def test_customer_number_is_detected_in_transcript():
    assert extract_customer_numbers("Meine Kundennummer ist 123 456 789, danke.") == ["123456789"]
    assert extract_customer_numbers("Kunden-Nr.: A-4711") == ["A4711"]
    assert extract_customer_numbers("Meine VN-Nr. lautet 12 345 678") == ["12345678"]
    assert extract_customer_numbers(TRANSCRIPT) == []


# --- Memo-Kundenabgleich -----------------------------------------------------------------


def test_unique_customer_found_by_phone_with_number_and_link(app, db, world):
    customer_id = _customer(db, world.tenant_a, "Max Mustermann", phone="+49 171 1234567", number="123456789")
    body = _match(login(app, "admin-a@example.com")).get_json()
    assert body["status"] == "unique"
    assert body["matched_by"] == "phone"
    assert body["customers"] == [
        {
            "id": customer_id,
            "name": "Max Mustermann",
            "customer_number": "123456789",
            "phone": "+49 171 1234567",
            "city": None,
            "url": f"/customers/{customer_id}",
        }
    ]


@pytest.mark.parametrize("stored", ["01711234567", "0049 171 1234567", "+491711234567"])
def test_phone_spellings_match_each_other(app, db, world, stored):
    _customer(db, world.tenant_a, "Kunde Telefon", phone=stored)
    body = _match(login(app, "admin-a@example.com"), "Rückruf bitte unter +49 171 1234567").get_json()
    assert body["status"] == "unique"
    assert body["customers"][0]["name"] == "Kunde Telefon"


def test_customer_number_is_used_when_no_phone_matches(app, db, world):
    _customer(db, world.tenant_a, "Erika Beispiel", number="KD-998877")
    body = _match(login(app, "admin-a@example.com"), "Hallo, meine Kundennummer ist KD 998877.").get_json()
    assert body["status"] == "unique"
    assert body["matched_by"] == "customer_number"
    assert body["customers"][0]["customer_number"] == "KD-998877"


def test_name_match_is_only_a_possible_match(app, db, world):
    _customer(db, world.tenant_a, "Mustermann, Max")
    body = _match(login(app, "admin-a@example.com"), "Hier ist Max Mustermann, bitte zurückrufen.").get_json()
    assert body["status"] == "possible"
    assert body["matched_by"] == "name"


def test_customer_number_has_priority_over_phone(app, db, world):
    _customer(db, world.tenant_a, "Telefon Kunde", phone="0171 1234567")
    _customer(db, world.tenant_a, "Nummer Kunde", number="555666777")
    body = _match(
        login(app, "admin-a@example.com"), "Kundennummer 555 666 777, erreichbar unter 0171 1234567."
    ).get_json()
    assert body["status"] == "unique"
    assert body["matched_by"] == "customer_number"
    assert body["customers"][0]["name"] == "Nummer Kunde"


def test_memo_page_shows_only_existing_customer_data(app, db, world, monkeypatch):
    _customer(db, world.tenant_a, "Max Mustermann", phone="0171 1234567")
    monkeypatch.setattr("app.blueprints.dashboard.routes.transcribe_audio", lambda filename, content: TRANSCRIPT)
    html = login(app, "admin-a@example.com").post(
        "/sprachnachrichten/transkribieren",
        data={"file": (io.BytesIO(b"ID3audio"), "anruf.mp3")},
        content_type="multipart/form-data",
    ).get_data(as_text=True)
    assert "Kunde erkannt" in html and "Telefon:" in html
    assert "Kundennummer</dt>" not in html and "nicht hinterlegt" not in html


def test_name_only_match_is_marked_as_possible_on_page(app, db, world, monkeypatch):
    _customer(db, world.tenant_a, "Max Mustermann")
    monkeypatch.setattr(
        "app.blueprints.dashboard.routes.transcribe_audio", lambda filename, content: "Hier ist Max Mustermann."
    )
    html = login(app, "dennis@example.com").post(
        "/sprachnachrichten/transkribieren",
        data={"file": (io.BytesIO(b"ID3audio"), "anruf.mp3")},
        content_type="multipart/form-data",
    ).get_data(as_text=True)
    assert "Möglicher Kunde" in html and "Kunde erkannt" not in html


def test_no_match_is_reported_without_inventing_data(app, db, world):
    _customer(db, world.tenant_a, "Jemand Anders", phone="0172 9999999")
    body = _match(login(app, "admin-a@example.com")).get_json()
    assert body["status"] == "none"
    assert body["customers"] == []
    assert body["detected_phones"] == ["0171 1234567"]


def test_multiple_candidates_are_never_assigned_automatically(app, db, world):
    _customer(db, world.tenant_a, "Max Mustermann", phone="0171 1234567")
    _customer(db, world.tenant_a, "Maria Mustermann", phone="+49 171 1234567")
    body = _match(login(app, "admin-a@example.com")).get_json()
    assert body["status"] == "multiple"
    assert sorted(c["name"] for c in body["customers"]) == ["Maria Mustermann", "Max Mustermann"]


def test_customer_match_never_crosses_tenants(app, db, world):
    _customer(db, world.tenant_b, "Max Mustermann", phone="0171 1234567", number="123456789")
    for email in ("admin-a@example.com", "dennis@example.com"):
        body = _match(login(app, email), TRANSCRIPT + " Kundennummer 123456789.").get_json()
        assert body["status"] == "none", email
        assert body["customers"] == []
    # Buero B findet seinen eigenen Kunden.
    assert _match(login(app, "admin-b@example.com")).get_json()["status"] == "unique"


def test_employee_gets_match_but_no_customer_detail_link(app, db, world):
    _customer(db, world.tenant_a, "Max Mustermann", phone="0171 1234567")
    body = _match(login(app, "dennis@example.com")).get_json()
    assert body["status"] == "unique"
    assert body["customers"][0]["url"] is None


def test_super_admin_cannot_use_memo_or_customer_match(app, db, world):
    _customer(db, world.tenant_a, "Max Mustermann", phone="0171 1234567")
    client = login(app, "justin@example.com")
    assert client.get("/sprachnachrichten").status_code == 403
    assert _match(client).status_code == 403
    assert client.post("/sprachnachrichten/transkribieren").status_code == 403


def test_match_rejects_empty_payload(app, world):
    client = login(app, "dennis@example.com")
    assert client.post("/sprachnachrichten/kundenabgleich", json={}).status_code == 400
    assert client.post("/sprachnachrichten/kundenabgleich", data="kein json").status_code == 400


def test_transcription_does_not_wait_for_customer_match(app, world, monkeypatch):
    """Das Transkript wird ohne Kundenabgleich ausgeliefert; der Abgleich ist eine eigene Anfrage."""
    monkeypatch.setattr("app.blueprints.dashboard.routes.transcribe_audio", lambda filename, content: TRANSCRIPT)

    def must_not_run(*args, **kwargs):
        raise AssertionError("Kundenabgleich darf die Transkription nicht blockieren")

    monkeypatch.setattr("app.blueprints.dashboard.routes.match_customer", must_not_run)
    resp = login(app, "dennis@example.com").post(
        "/sprachnachrichten/transkribieren",
        data={"file": (io.BytesIO(b"ID3audio"), "anruf.mp3")},
        content_type="multipart/form-data",
        headers={"Accept": "application/json"},
    )
    assert resp.status_code == 200
    # "token": berechtigt nur zum Zuordnen genau dieses Transkripts (app/services/memo_customers.py).
    assert set(resp.get_json()) == {"transcript", "filename", "uploaded_at", "token"}


def test_memo_transcription_is_logged_without_content(app, world, monkeypatch):
    monkeypatch.setattr("app.blueprints.dashboard.routes.transcribe_audio", lambda filename, content: TRANSCRIPT)
    login(app, "dennis@example.com").post(
        "/sprachnachrichten/transkribieren",
        data={"file": (io.BytesIO(b"ID3audio"), "anruf.mp3")},
        content_type="multipart/form-data",
        headers={"Accept": "application/json"},
    )
    entries = _audit(AuditEventType.MEMO_TRANSCRIBED)
    assert len(entries) == 1
    assert entries[0].tenant_id == world.tenant_a.id
    assert not entries[0].details
    assert "Mustermann" not in repr(entries[0].__dict__)


def test_memo_page_offers_drag_and_drop_and_file_selection(app, world):
    html = login(app, "dennis@example.com").get("/sprachnachrichten").get_data(as_text=True)
    assert "data-memo-drop" in html and "Datei hier ablegen" in html
    assert 'type="file"' in html and "Sprachnachricht auswählen" in html
    assert "/sprachnachrichten/kundenabgleich" in html
    assert "js/memo.js" in html


def test_memo_without_javascript_renders_match_inline(app, db, world, monkeypatch):
    _customer(db, world.tenant_a, "Max Mustermann", phone="0171 1234567", number="123456789")
    monkeypatch.setattr("app.blueprints.dashboard.routes.transcribe_audio", lambda filename, content: TRANSCRIPT)
    html = login(app, "admin-a@example.com").post(
        "/sprachnachrichten/transkribieren",
        data={"file": (io.BytesIO(b"ID3audio"), "anruf.mp3")},
        content_type="multipart/form-data",
    ).get_data(as_text=True)
    assert "Kunde erkannt" in html and "123456789" in html and "Kunde öffnen" in html


# --- Startseite ----------------------------------------------------------------------------


def test_employee_overview_shows_only_own_information(app, world):
    html = login(app, "dennis@example.com").get("/uebersicht").get_data(as_text=True)
    assert "Dennis" in html
    for section in ("Leipziger Liste", "Memo", "Zeiterfassung"):
        assert section in html
    assert "1 offener Vorgang" in html  # genau ein eigener offener Vorgang (DENNIS-OFFEN-1)
    assert "Büro heute" not in html and "Laura" not in html


def test_office_admin_overview_adds_office_information(app, world):
    html = login(app, "admin-a@example.com").get("/uebersicht").get_data(as_text=True)
    # Nur eine kompakte Bueroinformation - kein Aktivitaeten-Feed mehr auf der Startseite.
    assert "Büro heute" in html and "Letzte Aktivitäten" not in html
    assert "Bob" not in html and "Buero B" not in html


def test_super_admin_has_no_overview(app, world):
    assert login(app, "justin@example.com").get("/uebersicht").status_code == 403


def test_clock_in_from_overview_returns_to_overview(app, world):
    client = login(app, "dennis@example.com")
    resp = client.post("/zeiterfassung/einstempeln", data={"next": "/uebersicht"})
    assert resp.headers["Location"].endswith("/uebersicht")
    assert "Ausstempeln" in client.get("/uebersicht").get_data(as_text=True)
    # Fremde Ziele werden ignoriert.
    resp = client.post("/zeiterfassung/ausstempeln", data={"next": "https://evil.example/"})
    assert resp.headers["Location"].endswith("/zeiterfassung")


# --- Leipziger Liste: Suche und Reiter -----------------------------------------------------


def test_leipziger_tabs(app, world):
    client = login(app, "dennis@example.com")
    todo = client.get("/leipziger-liste").get_data(as_text=True)
    assert "DENNIS-OFFEN-1" in todo and "DENNIS-ERLEDIGT-1" not in todo
    done = client.get("/leipziger-liste?tab=mit-datum").get_data(as_text=True)
    assert "DENNIS-ERLEDIGT-1" in done and "DENNIS-OFFEN-1" not in done
    assert "01.07.2026" in done


def test_storno_rows_are_not_todo_but_without_date(app, db, world):
    row = _row("DENNIS-STORNO-1", "08/1234-A")
    row["is_storno"] = True
    row["is_angebot"] = False
    _list(db, world.tenant_a, [row], "liste-neu.pdf")
    client = login(app, "dennis@example.com")
    assert "DENNIS-STORNO-1" not in client.get("/leipziger-liste").get_data(as_text=True)
    assert "DENNIS-STORNO-1" in client.get("/leipziger-liste?tab=ohne-datum").get_data(as_text=True)


def test_leipziger_search_finds_contract_and_broker_numbers(app, world):
    client = login(app, "admin-a@example.com")
    html = client.get("/leipziger-liste?q=laura-offen").get_data(as_text=True)
    assert "LAURA-OFFEN-1" in html and "DENNIS-OFFEN-1" not in html
    html = client.get("/leipziger-liste?q=08 5555 B").get_data(as_text=True)
    assert "LAURA-OFFEN-1" in html and "DENNIS-OFFEN-1" not in html
    assert "Keine Treffer" in client.get("/leipziger-liste?q=GIBTESNICHT").get_data(as_text=True)


def test_employee_search_cannot_reveal_foreign_entries(app, world):
    client = login(app, "dennis@example.com")
    for query in ("LAURA-OFFEN-1", "08/5555-B", "BOB-OFFEN-1"):
        for tab in ("zu-erledigen", "mit-datum", "ohne-datum"):
            html = client.get(f"/leipziger-liste?q={query}&tab={tab}").get_data(as_text=True)
            # Der Suchbegriff selbst steht im Suchfeld - geprueft werden die Tabellenzeilen.
            assert '<td class="worklist-number">' not in html
            assert "Keine Treffer" in html


def test_leipziger_pagination(app, db, world):
    rows = [_row(f"PAGE-{index:04d}", "08/1234-A") for index in range(130)]
    _list(db, world.tenant_a, rows, "liste-gross.pdf")
    client = login(app, "dennis@example.com")
    first = client.get("/leipziger-liste").get_data(as_text=True)
    assert "PAGE-0000" in first and "PAGE-0099" in first and "PAGE-0100" not in first
    assert "1–100 von 130" in first
    second = client.get("/leipziger-liste?seite=2").get_data(as_text=True)
    assert "PAGE-0100" in second and "PAGE-0129" in second and "PAGE-0000" not in second


# --- Zeiterfassung: Monatsuebersicht und Export --------------------------------------------


def test_month_overview_shows_target_actual_and_difference(app, world):
    html = login(app, "dennis@example.com").get("/zeiterfassung/monat").get_data(as_text=True)
    for label in ("Soll", "Ist", "Differenz"):
        assert label in html


def test_month_without_entries_shows_empty_state(app, world):
    html = login(app, "dennis@example.com").get("/zeiterfassung/monat?monat=2025-01").get_data(as_text=True)
    assert "Noch keine Arbeitszeiten für Januar 2025 vorhanden." in html


def test_admin_exports_employee_month_as_csv_and_pdf(app, world):
    client = login(app, "admin-a@example.com")
    resp = client.get(f"/zeiterfassung/team/{world.dennis_id}/export.csv")
    assert resp.status_code == 200
    assert resp.mimetype == "text/csv"
    assert "attachment" in resp.headers["Content-Disposition"]
    text = resp.data.decode("utf-8-sig")
    assert "Mitarbeiter;Dennis" in text
    assert "Datum;Tag;Beginn;Ende;Pausen;Ist;Soll;Saldo;Hinweis" in text
    assert "Summe" in text
    pdf = client.get(f"/zeiterfassung/team/{world.dennis_id}/export.pdf")
    assert pdf.status_code == 200 and pdf.mimetype == "application/pdf"
    assert pdf.data.startswith(b"%PDF")
    team = client.get("/zeiterfassung/team/export.csv?monat=2026-10")
    assert team.status_code == 200
    team_text = team.data.decode("utf-8-sig")
    assert "Dennis" in team_text and "Laura" in team_text and "Bob" not in team_text


def test_export_is_limited_to_own_office(app, world):
    client = login(app, "admin-a@example.com")
    assert client.get(f"/zeiterfassung/team/{world.bob_id}/export.csv").status_code == 404
    assert client.get(f"/zeiterfassung/team/{world.root_id}/export.pdf").status_code == 404
    assert client.get(f"/zeiterfassung/team/{world.dennis_id}/export.xls").status_code == 404
    for email in ("dennis@example.com", "justin@example.com"):
        assert login(app, email).get(f"/zeiterfassung/team/{world.dennis_id}/export.csv").status_code == 403


# --- Mitarbeiterverwaltung -----------------------------------------------------------------


def test_staff_page_lists_own_office_members(app, world):
    html = login(app, "admin-a@example.com").get("/mitarbeiter").get_data(as_text=True)
    assert "Dennis" in html and "Laura" in html and "08/1234-A" in html
    assert "Bob" not in html and "Justin" not in html


def test_staff_detail_shows_master_data_time_and_leipziger(app, world):
    html = login(app, "admin-a@example.com").get(f"/mitarbeiter/{world.dennis_id}").get_data(as_text=True)
    for text in ("dennis@example.com", "08/1234-A", "Sollstunden", "Arbeitstage", "Accountstatus", "Zeiterfassung", "Zu erledigen"):
        assert text in html


def test_staff_detail_of_foreign_or_platform_account_is_404(app, world):
    client = login(app, "admin-a@example.com")
    assert client.get(f"/mitarbeiter/{world.bob_id}").status_code == 404
    assert client.get(f"/mitarbeiter/{world.root_id}").status_code == 404


def test_staff_create_and_edit_return_to_staff_pages(app, world):
    client = login(app, "admin-a@example.com")
    resp = client.post(
        "/settings/users/new",
        data={
            "von": "mitarbeiter",
            "email": "neu@example.com",
            "vermittlernummer": "08/4321-K",
            "role": "employee",
            "is_active": "y",
            "display_name": "Neue Person",
            "weekly_hours": "30",
            "workdays": ["1", "2", "3"],
            "password": "neues-passwort-1",
            "password_confirm": "neues-passwort-1",
        },
    )
    assert resp.status_code == 302 and "/mitarbeiter/" in resp.headers["Location"]
    detail = client.get(resp.headers["Location"]).get_data(as_text=True)
    assert "Neue Person" in detail and "08/4321-K" in detail and "30:00 Std. pro Woche" in detail
    resp = client.post(f"/settings/users/{world.laura_id}/loeschen", data={"von": "mitarbeiter"})
    assert resp.headers["Location"].endswith("/mitarbeiter")
    assert "Laura" not in client.get("/mitarbeiter").get_data(as_text=True)


def test_employee_and_super_admin_cannot_manage_staff(app, world):
    for email in ("dennis@example.com", "justin@example.com"):
        client = login(app, email)
        assert client.get("/mitarbeiter").status_code == 403
        assert client.get(f"/mitarbeiter/{world.laura_id}").status_code == 403


# --- Aktivitaetsprotokoll ------------------------------------------------------------------


def test_activity_log_shows_own_office_events(app, world):
    login(app, "dennis@example.com").post("/zeiterfassung/einstempeln")
    login(app, "bob@example.com").post("/zeiterfassung/einstempeln")
    html = login(app, "admin-a@example.com").get("/aktivitaeten").get_data(as_text=True)
    assert "Dennis" in html and "Zeiterfassung gestartet" in html
    assert "Bob" not in html
    html_b = login(app, "admin-b@example.com").get("/aktivitaeten").get_data(as_text=True)
    assert "Bob" in html_b and "Dennis" not in html_b


def test_activity_log_filter_by_member(app, world):
    login(app, "dennis@example.com").post("/zeiterfassung/einstempeln")
    login(app, "laura@example.com").post("/zeiterfassung/einstempeln")
    client = login(app, "admin-a@example.com")
    html = client.get(f"/aktivitaeten?mitarbeiter={world.laura_id}").get_data(as_text=True)
    assert "<strong>Laura</strong>" in html and "<strong>Dennis</strong>" not in html
    # Fremde Mitarbeiter-ID wird ignoriert statt fremde Daten zu zeigen.
    assert "Bob" not in client.get(f"/aktivitaeten?mitarbeiter={world.bob_id}").get_data(as_text=True)


def test_activity_log_access(app, world):
    for email in ("dennis@example.com", "justin@example.com"):
        assert login(app, email).get("/aktivitaeten").status_code == 403


def test_super_admin_security_log_excludes_business_activity(app, world):
    login(app, "dennis@example.com").post("/zeiterfassung/einstempeln")
    html = login(app, "justin@example.com").get("/plattform/sicherheit").get_data(as_text=True)
    assert "time_clock_in" not in html and "Zeiterfassung gestartet" not in html


# --- Mein Konto: aktive Sitzungen ----------------------------------------------------------


def _active_sessions(user_id):
    return UserSession.query.filter_by(user_id=user_id, revoked_at=None).count()


def test_login_creates_session_and_account_page_lists_it(app, world):
    client = login(app, "dennis@example.com")
    assert _active_sessions(world.dennis_id) == 1
    html = client.get("/settings/profile").get_data(as_text=True)
    for text in ("Mein Konto", "Kontodaten", "08/1234-A", "Passwort ändern", "Zwei-Faktor-Authentifizierung", "Aktive Sitzungen", "Diese Sitzung"):
        assert text in html


def test_logout_from_all_other_devices(app, world):
    laptop = login(app, "dennis@example.com")
    phone = login(app, "dennis@example.com")
    assert _active_sessions(world.dennis_id) == 2
    resp = laptop.post("/settings/sitzungen/abmelden")
    assert resp.status_code == 302
    assert laptop.get("/zeiterfassung").status_code == 200
    resp = phone.get("/zeiterfassung")
    assert resp.status_code == 302 and "/auth/login" in resp.headers["Location"]
    assert _active_sessions(world.dennis_id) == 1
    assert len(_audit(AuditEventType.SESSIONS_REVOKED)) == 1


def test_logout_ends_current_session(app, world):
    client = login(app, "dennis@example.com")
    client.post("/auth/logout")
    assert _active_sessions(world.dennis_id) == 0
    assert client.get("/zeiterfassung").status_code == 302


def test_password_change_keeps_current_session_and_ends_others(app, world):
    current = login(app, "dennis@example.com")
    other = login(app, "dennis@example.com")
    resp = current.post(
        "/settings/profile",
        data={"current_password": PASSWORD, "new_password": "ganz-neues-pw-1", "new_password_confirm": "ganz-neues-pw-1"},
    )
    assert resp.status_code == 302
    assert current.get("/zeiterfassung").status_code == 200
    assert other.get("/zeiterfassung").status_code == 302
    assert _active_sessions(world.dennis_id) == 1


# --- Navigation und Fehlerseiten -----------------------------------------------------------


def test_navigation_is_responsive_and_marks_active_area(app, world):
    html = login(app, "dennis@example.com").get("/leipziger-liste").get_data(as_text=True)
    assert 'name="viewport"' in html
    assert 'app-topbar-menu' in html and 'aria-controls="hauptnavigation"' in html
    assert 'id="hauptnavigation"' in html
    active = html.split('aria-current="page"')[0].rsplit("<a", 1)[1]
    assert "/leipziger-liste" in active
    for label in ("Start", "Leipziger Liste", "Memo", "Zeiterfassung", "Werkzeuge", "Mein Konto", "Abmelden"):
        assert label in html


def test_office_admin_navigation_has_office_areas(app, world):
    html = login(app, "admin-a@example.com").get("/uebersicht").get_data(as_text=True)
    for href in ('href="/mitarbeiter"', 'href="/aktivitaeten"', 'href="/settings/users"'):
        assert href in html


def test_unknown_page_shows_friendly_error(app, world):
    resp = login(app, "dennis@example.com").get("/gibt-es-nicht")
    html = resp.get_data(as_text=True)
    assert resp.status_code == 404
    assert "Seite nicht gefunden" in html and "Traceback" not in html


def test_server_error_shows_no_technical_details(app, world, monkeypatch):
    app.config["PROPAGATE_EXCEPTIONS"] = False

    def broken(*args, **kwargs):
        raise RuntimeError("geheime interne Details")

    monkeypatch.setattr("app.blueprints.portal.routes.time_service.get_stamp_state", broken)
    resp = login(app, "dennis@example.com").get("/uebersicht")
    html = resp.get_data(as_text=True)
    assert resp.status_code == 500
    assert "Es ist ein Fehler aufgetreten" in html
    assert "geheime interne Details" not in html and "RuntimeError" not in html


def test_super_admin_platform_status_without_office_data(app, world):
    html = login(app, "justin@example.com").get("/plattform/system").get_data(as_text=True)
    assert "Plattformstatus" in html and "Datenbank" in html and "erreichbar" in html
    for secret in ("DENNIS-OFFEN-1", "liste-a.pdf", "Zeiterfassung gestartet"):
        assert secret not in html
