"""Universal-Upload (Dateityp-Erkennung, Weitergabe an bestehende Importe), Memo-Kunden-
erkennung (Ergaenzungen), rollenabhaengige Startseiten und kompakte Benutzerverwaltung."""

import io

import fitz
import pytest
from sqlalchemy import event

from app.extensions import db as _db
from app.models import Customer, Document, EmployeeProfile, UserRole
from app.template_filters import hours_short, short_dt, workdays_short
from app.tenancy import use_tenant_id
from tests.test_global_search import _login, _make_user
from tests.test_leipziger_parser import PAGE_1, make_list_pdf, no_llm  # noqa: F401


@pytest.fixture()
def office(app, db, tenant):
    admin = _make_user(db, tenant.id, "admin@example.org", UserRole.OFFICE_ADMIN, "08/0001-A")
    employee = _make_user(db, tenant.id, "dennis@example.org", UserRole.EMPLOYEE, "08/4205-M")
    _make_user(db, tenant.id, "plattform@example.org", UserRole.SUPER_ADMIN, None)
    with use_tenant_id(tenant.id):
        db.session.add_all([
            EmployeeProfile(tenant_id=tenant.id, user_id=admin.id, display_name="Julia Heller"),
            EmployeeProfile(tenant_id=tenant.id, user_id=employee.id, display_name="Dennis Muster"),
        ])
        db.session.commit()
    return tenant


def _plain_pdf(text="Rechnung Nr. 4711"):
    doc = fitz.open()
    doc.new_page().insert_text((50, 50), text)
    data = doc.tobytes()
    doc.close()
    return data


def _check(client, name, content):
    resp = client.post("/hochladen/pruefen", data={"file": (io.BytesIO(content), name)}, content_type="multipart/form-data")
    assert resp.status_code == 200
    return resp.get_json()


# --- Universal-Upload ------------------------------------------------------------------------


def test_leipziger_pdf_is_detected(app, office):
    result = _check(_login(app, "admin@example.org"), "liste.pdf", make_list_pdf([PAGE_1]))
    assert result == {"kind": "leipziger", "label": "Leipziger Liste erkannt", "allowed": True}


@pytest.mark.parametrize("name", ["memo.m4a", "anruf.MP3", "nachricht.ogg", "aufnahme.webm"])
def test_audio_is_detected(app, office, name):
    result = _check(_login(app, "dennis@example.org"), name, b"\x00" * 128)
    assert result == {"kind": "memo", "label": "Sprachnachricht erkannt", "allowed": True}


@pytest.mark.parametrize(
    "name, content",
    [("notiz.txt", b"hallo"), ("bild.png", b"\x89PNG"), ("rechnung.pdf", None), ("kaputt.pdf", b"%PDF-kaputt"), ("ohne_endung", b"x")],
)
def test_unknown_or_invalid_files_are_rejected(app, office, name, content):
    result = _check(_login(app, "admin@example.org"), name, _plain_pdf() if content is None else content)
    assert result["allowed"] is False
    assert result["label"] == "Diese Datei kann Zentriq nicht verarbeiten."


def test_employee_cannot_import_lists_via_universal_upload(app, office):
    client = _login(app, "dennis@example.org")
    result = _check(client, "liste.pdf", make_list_pdf([PAGE_1]))
    assert result["allowed"] is False and "Büro-Admin" in result["label"]
    # Der eigentliche Import bleibt serverseitig gesperrt.
    resp = client.post("/upload", data={"file": (io.BytesIO(make_list_pdf([PAGE_1])), "liste.pdf")},
                       content_type="multipart/form-data", headers={"X-Requested-With": "XMLHttpRequest"})
    assert resp.status_code == 403


def test_detected_pdf_is_imported_with_existing_leipziger_import(app, db, office, tmp_path, no_llm):  # noqa: F811
    app.config["UPLOAD_FOLDER"] = str(tmp_path)
    client = _login(app, "admin@example.org")
    content = make_list_pdf([PAGE_1])
    assert _check(client, "liste.pdf", content)["kind"] == "leipziger"
    resp = client.post("/upload", data={"file": (io.BytesIO(content), "liste.pdf")},
                       content_type="multipart/form-data", headers={"X-Requested-With": "XMLHttpRequest"})
    assert resp.status_code == 201
    with use_tenant_id(office.id):
        document = db.session.get(Document, resp.get_json()["document_id"])
        assert document.extra_data["leipziger_analysis"]["parser"]["records"] == 7


def test_detected_audio_is_transcribed_with_existing_memo(app, office, monkeypatch):
    monkeypatch.setattr("app.services.memo.transcribe_file", lambda path: "Hallo, hier ist Max.")
    client = _login(app, "dennis@example.org")
    assert _check(client, "memo.m4a", b"\x00" * 128)["kind"] == "memo"
    resp = client.post("/sprachnachrichten/transkribieren", data={"file": (io.BytesIO(b"\x00" * 128), "memo.m4a")},
                       content_type="multipart/form-data", headers={"Accept": "application/json"})
    assert resp.get_json()["transcript"] == "Hallo, hier ist Max."


def test_upload_page_and_header_entry(app, office):
    for email in ("admin@example.org", "dennis@example.org"):
        client = _login(app, email)
        html = client.get("/hochladen").get_data(as_text=True)
        assert "Datei hier ablegen" in html and "Datei auswählen" in html
    # In der Navigation (Werkzeuge) nur fuer Buero-Admins: Mitarbeiter koennen dort nur
    # Sprachnachrichten verarbeiten - das deckt "Memo" bereits ab (keine Doppelung).
    assert 'href="/hochladen"' in _login(app, "admin@example.org").get("/uebersicht").get_data(as_text=True)
    assert 'href="/hochladen"' not in _login(app, "dennis@example.org").get("/uebersicht").get_data(as_text=True)
    assert "PDF (Leipziger Liste)" not in _login(app, "dennis@example.org").get("/hochladen").get_data(as_text=True)


def test_super_admin_cannot_use_universal_upload(app, office):
    client = _login(app, "plattform@example.org")
    assert client.get("/hochladen").status_code == 403
    assert client.post("/hochladen/pruefen", data={"file": (io.BytesIO(b"x"), "a.m4a")}, content_type="multipart/form-data").status_code == 403
    assert 'href="/hochladen"' not in client.get("/plattform").get_data(as_text=True)


# --- Memo-Kundenerkennung --------------------------------------------------------------------


def _match(client, transcript):
    return client.post("/sprachnachrichten/kundenabgleich", json={"transcript": transcript}).get_json()


def test_memo_page_shows_multiple_candidates_compactly(app, db, office, monkeypatch):
    with use_tenant_id(office.id):
        db.session.add_all([
            Customer(tenant_id=office.id, name="Max Mustermann", phone="0171 1234567", customer_number="K-1"),
            Customer(tenant_id=office.id, name="Erika Mustermann", phone="0171 1234567", customer_number="K-2"),
        ])
        db.session.commit()
    monkeypatch.setattr("app.services.memo.transcribe_file", lambda path: "Rückruf an 0171 1234567 bitte.")
    client = _login(app, "admin@example.org")
    html = client.post("/sprachnachrichten/transkribieren", data={"file": (io.BytesIO(b"\x00" * 64), "a.m4a")},
                       content_type="multipart/form-data").get_data(as_text=True)
    assert "Mehrere mögliche Kunden gefunden" in html
    assert "Max Mustermann" in html and "Erika Mustermann" in html
    assert "%" not in html.split("Kundenzuordnung")[1]


def test_memo_match_finds_customer_by_spoken_customer_number(app, db, office):
    with use_tenant_id(office.id):
        db.session.add(Customer(tenant_id=office.id, name="Max Mustermann", customer_number="123456"))
        db.session.commit()
    result = _match(_login(app, "admin@example.org"), "Meine Kundennummer ist 123 456, bitte zurückrufen.")
    assert result["status"] == "unique" and result["matched_by"] == "customer_number"


def test_memo_match_does_not_scan_whole_customer_base(app, db, office):
    """Abgleich ueber indizierte Schluessel: die Zahl der Abfragen ist unabhaengig vom Bestand,
    und es werden keine vollstaendigen Kundenlisten geladen."""
    client = _login(app, "admin@example.org")
    transcript = "Hallo, hier ist Max Mustermann, Telefon 0171 1234567, Kundennummer K-99."

    def run():
        statements = []
        listener = lambda conn, cursor, statement, *args: statements.append(statement)  # noqa: E731
        event.listen(_db.engine, "before_cursor_execute", listener)
        try:
            assert _match(client, transcript)["status"] == "none"
        finally:
            event.remove(_db.engine, "before_cursor_execute", listener)
        return statements

    before = run()
    with use_tenant_id(office.id):
        db.session.add_all(Customer(tenant_id=office.id, name=f"Kunde Nummer{i}", phone=f"0341 {100000 + i}") for i in range(300))
        db.session.commit()
    client.get("/uebersicht")
    after = run()
    assert len(after) <= len(before)
    customer_queries = [s for s in after if "FROM customers" in s]
    assert customer_queries and all("WHERE" in s for s in customer_queries)


# --- Startseiten nach Rolle --------------------------------------------------------------------


def test_employee_start_page(app, office):
    client = _login(app, "dennis@example.org")
    assert client.get("/").headers["Location"].endswith("/uebersicht")
    html = client.get("/uebersicht").get_data(as_text=True)
    assert "Dennis" in html
    for href in ('href="/leipziger-liste"', 'href="/sprachnachrichten"', 'href="/zeiterfassung"'):
        assert href in html
    for hidden in ('href="/mitarbeiter"', 'href="/settings/users"', "Büro heute", 'href="/plattform', "Aktivitäten"):
        assert hidden not in html


def test_office_admin_start_page(app, office):
    html = _login(app, "admin@example.org").get("/uebersicht").get_data(as_text=True)
    assert "Julia" in html and "Büro heute" in html
    for href in ('href="/leipziger-liste"', 'href="/sprachnachrichten"', 'href="/zeiterfassung"', 'href="/mitarbeiter"', 'href="/settings/users"'):
        assert href in html
    assert 'href="/plattform' not in html


def test_super_admin_start_page(app, db, office):
    with use_tenant_id(office.id):
        db.session.add(Customer(tenant_id=office.id, name="Geheimkunde Müller", phone="0171 1234567"))
        db.session.commit()
    client = _login(app, "plattform@example.org")
    assert client.get("/").headers["Location"].endswith("/plattform")
    html = client.get("/plattform").get_data(as_text=True)
    for label in ("Büros", "Benutzer", "System", "Sicherheit"):
        assert label in html
    for href in ('href="/plattform/bueros"', 'href="/plattform/benutzer"', 'href="/plattform/system"', 'href="/plattform/sicherheit"'):
        assert href in html
    for hidden in ("Geheimkunde", 'href="/leipziger-liste"', 'href="/sprachnachrichten"', 'href="/zeiterfassung"', 'href="/customers'):
        assert hidden not in html
    assert client.get("/uebersicht").status_code == 403


def test_office_roles_cannot_open_platform_start_page(app, office):
    for email in ("admin@example.org", "dennis@example.org"):
        assert _login(app, email).get("/plattform").status_code == 403


# --- Benutzerverwaltung kompakt ------------------------------------------------------------------


def test_compact_formats():
    assert workdays_short("12345") == "Mo–Fr"
    assert workdays_short("124") == "Mo, Di, Do"
    assert workdays_short("") == "–"
    assert hours_short(720) == "12 h" and hours_short(2250) == "37,5 h"
    assert short_dt(None, default="noch nie") == "noch nie"


def test_user_table_uses_compact_columns_and_card_layout(app, office):
    html = _login(app, "admin@example.org").get("/settings/users").get_data(as_text=True)
    assert "table-stack-md" in html and "data-table-fit" in html
    assert "40 h" in html and "Mo–Fr" in html
    assert html.count('class="row-actions') >= 1 and "Bearbeiten" in html and "Löschen" in html
