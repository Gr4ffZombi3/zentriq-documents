# ruff: noqa: F811  (Fixture "world" wird aus test_permissions_security importiert)
"""Werkzeuge, Dokument anonymisieren, neue Navigation und Startseite.

Zwei-Bueros-Szenario aus test_permissions_security (Buero A: Admin A, Dennis, Laura,
SUPER_ADMIN Justin; Buero B: Admin B, Bob). Externe Dienste (OpenAI Vision, Anthropic) werden
so ersetzt, dass jeder Aufruf den Test scheitern laesst - die Anonymisierung muss ohne sie
auskommen."""

import io

import fitz
import pytest
from PIL import Image

from app.models import Customer
from app.services import anonymize as anon
from app.tenancy import use_tenant_id
from tests.test_permissions_security import login, world  # noqa: F401

EXAMPLE = """Max Mustermann
Musterstraße 12
53111 Bonn
Kundennummer 123456789
Kennzeichen EU-MM 123"""


@pytest.fixture(autouse=True)
def no_external_ai(monkeypatch):
    """Jeder Versuch, OpenAI Vision oder den Anthropic-Client zu nutzen, laesst den Test scheitern."""

    def forbidden(*args, **kwargs):
        raise AssertionError("Anonymisierung darf keinen externen KI-Dienst aufrufen")

    monkeypatch.setattr("app.services.ocr.vision_ocr.ocr_image", forbidden)
    monkeypatch.setattr("app.services.llm.client.get_openai_client", forbidden)
    monkeypatch.setattr("app.services.assistant.get_client", forbidden)
    monkeypatch.setattr("app.services.ocr.tesseract_ocr.ocr_image_lines", lambda image: "Herr Peter Lustig\nTel. 0171 2223334")


def _pdf(text: str | None) -> bytes:
    document = fitz.open()
    page = document.new_page()
    if text:
        page.insert_text((72, 72), text, fontsize=11)
    data = document.tobytes()
    document.close()
    return data


def _png() -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (40, 20), "white").save(buffer, format="PNG")
    return buffer.getvalue()


# --- Erkennung (lokal, deterministisch) ---------------------------------------------------


def test_example_from_the_brief():
    result = anon.anonymize(EXAMPLE)
    assert result.text == "[KUNDE]\n[ANSCHRIFT]\nKundennummer [KUNDENNUMMER]\nKennzeichen [KENNZEICHEN]"
    assert {(f.placeholder, f.original) for f in result.findings} == {
        ("[KUNDE]", "Max Mustermann"),
        ("[ANSCHRIFT]", "Musterstraße 12\n53111 Bonn"),
        ("[KUNDENNUMMER]", "123456789"),
        ("[KENNZEICHEN]", "EU-MM 123"),
    }


def test_detects_contact_and_identifiers():
    text = (
        "Telefon: 0171 1234567, Rückruf unter +49 (0)341 240891. E-Mail max.mueller@web.de, "
        "IBAN DE89 3704 0044 0532 0130 00. Geburtsdatum: 12.03.1980. Vertrag 720/4711037-A-14, "
        "Schaden-Nr. 2024-55871. Steuer-ID 12 345 678 901."
    )
    result = anon.anonymize(text)
    for original in ("0171 1234567", "+49 (0)341 240891", "max.mueller@web.de", "DE89 3704 0044 0532 0130 00",
                     "12.03.1980", "720/4711037-A-14", "2024-55871", "12 345 678 901"):
        assert original not in result.text, original
    for placeholder in ("[TELEFON]", "[TELEFON 2]", "[E-MAIL]", "[IBAN]", "[GEBURTSDATUM]", "[VERTRAGSNUMMER]", "[SCHADENNUMMER]", "[KENNUNG]"):
        assert placeholder in result.text, placeholder


def test_keeps_dates_amounts_and_broker_numbers():
    text = "Der Vertrag beginnt am 01.10.2026 und kostet 312,50 EUR. Vermittler 08/4205-M, PLZ 04109, Termin 10:30 Uhr."
    assert anon.anonymize(text).text == text


def test_same_person_same_placeholder_and_numbering():
    text = "Sehr geehrter Herr Müller,\nHerr Müller und Frau Anna Schmidt-Leutheusser haben angerufen. Mustermann, Max auch."
    result = anon.anonymize(text)
    assert result.text.count("[KUNDE]") == 2
    assert "Frau [KUNDE 2]" in result.text and "[KUNDE 3] auch" in result.text
    assert "Müller" not in result.text and "Schmidt" not in result.text


def test_invalid_iban_is_not_reported_as_iban():
    assert "[IBAN]" not in anon.anonymize("Referenz DE00 1234 5678 9012 3456 78").text


def test_known_names_only_from_own_office(app, world, db):
    """Der Kundenstamm hilft beim Erkennen - aber ausschliesslich der des eigenen Bueros."""
    with use_tenant_id(world.tenant_a.id):
        db.session.add(Customer(tenant_id=world.tenant_a.id, name="Yvonne Krawallski"))
        db.session.commit()
    with use_tenant_id(world.tenant_b.id):
        db.session.add(Customer(tenant_id=world.tenant_b.id, name="Xaver Quastenhuber"))
        db.session.commit()
    text = "Gespräch mit Yvonne Krawallski und Xaver Quastenhuber."
    resp = login(app, "admin-a@example.com").post("/werkzeuge/anonymisieren", data={"text": text})
    html = resp.get_data(as_text=True)
    assert resp.status_code == 200
    assert "Gespräch mit [KUNDE] und Xaver Quastenhuber." in html  # Buero B bleibt unbekannt
    assert ">[KUNDE]</span> Yvonne Krawallski" in html


# --- Seite, Rollen, keine Speicherung --------------------------------------------------------


def test_office_roles_can_anonymize_text(app, world):
    for email in ("admin-a@example.com", "dennis@example.com", "bob@example.com"):
        client = login(app, email)
        assert client.get("/werkzeuge/anonymisieren").status_code == 200
        resp = client.post("/werkzeuge/anonymisieren", data={"text": EXAMPLE})
        html = resp.get_data(as_text=True)
        assert resp.status_code == 200, email
        assert "[KUNDE]" in html and "Entfernt: 4 Angaben" in html
        assert resp.headers["Cache-Control"] == "no-store"


def test_super_admin_and_anonymous_have_no_access(app, world):
    assert login(app, "justin@example.com").get("/werkzeuge/anonymisieren").status_code == 403
    assert login(app, "justin@example.com").get("/werkzeuge").status_code == 403
    assert app.test_client().get("/werkzeuge/anonymisieren").status_code == 302


def test_pdf_uses_embedded_text(app, world):
    client = login(app, "dennis@example.com")
    resp = client.post(
        "/werkzeuge/anonymisieren",
        data={"file": (io.BytesIO(_pdf("Kunde: Erika Musterfrau, Tel. 0221 9876543")), "brief.pdf")},
        content_type="multipart/form-data",
    )
    html = resp.get_data(as_text=True)
    assert resp.status_code == 200
    assert "Kunde: [KUNDE], Tel. [TELEFON]" in html and "brief.pdf" in html


def test_scanned_pdf_and_image_use_local_tesseract_only(app, world):
    client = login(app, "dennis@example.com")
    for name, data in (("scan.pdf", _pdf(None)), ("foto.png", _png())):
        resp = client.post("/werkzeuge/anonymisieren", data={"file": (io.BytesIO(data), name)}, content_type="multipart/form-data")
        html = resp.get_data(as_text=True)
        assert resp.status_code == 200, name
        assert "Herr [KUNDE]" in html and "Tel. [TELEFON]" in html


def test_unsupported_file_and_empty_input(app, world):
    client = login(app, "dennis@example.com")
    resp = client.post("/werkzeuge/anonymisieren", data={"file": (io.BytesIO(b"x"), "tabelle.xlsx")}, content_type="multipart/form-data")
    assert resp.status_code == 400 and "PDF-Datei oder ein Bild" in resp.get_data(as_text=True)
    resp = client.post("/werkzeuge/anonymisieren", data={"text": "   "})
    assert resp.status_code == 400 and "Bitte eine Datei auswählen oder Text einfügen." in resp.get_data(as_text=True)


def test_result_offers_copy_and_assistant_without_sending(app, world):
    app.config["ANTHROPIC_API_KEY"] = "sk-ant-test"
    app.config["ASSISTANT_ENABLED"] = True
    html = login(app, "dennis@example.com").post("/werkzeuge/anonymisieren", data={"text": EXAMPLE}).get_data(as_text=True)
    # Beide Aktionen beziehen sich ausschliesslich auf das Feld mit der anonymisierten Fassung.
    assert 'data-copy="#anon-result"' in html
    assert 'data-assistant-insert="#anon-result"' in html
    assert 'id="anon-result" rows="14">[KUNDE]' in html
    app.config["ASSISTANT_ENABLED"] = False
    html = login(app, "dennis@example.com").post("/werkzeuge/anonymisieren", data={"text": EXAMPLE}).get_data(as_text=True)
    assert "data-assistant-insert" not in html


# --- Werkzeuge, Navigation, Startseite ------------------------------------------------------


def test_tools_page_per_role(app, world):
    employee = login(app, "dennis@example.com").get("/werkzeuge").get_data(as_text=True)
    assert "Dokument anonymisieren" in employee and "Assistent" in employee
    assert "Universal-Upload" not in employee  # fuer Mitarbeiter = Memo-Upload, keine Doppelung
    admin = login(app, "admin-a@example.com").get("/werkzeuge").get_data(as_text=True)
    assert "Universal-Upload" in admin and 'href="/hochladen"' in admin


def _sidebar(html):
    return html.split('id="hauptnavigation"', 1)[1].split("</nav>", 1)[0]


def test_employee_navigation_groups(app, world):
    nav = _sidebar(login(app, "dennis@example.com").get("/uebersicht").get_data(as_text=True))
    for label in ("Start", "Arbeit", "Leipziger Liste", "Memo", "Zeit", "Zeiterfassung", "Werkzeuge", "Dokument anonymisieren", "Mein Konto", "Abmelden"):
        assert label in nav, label
    for hidden in ("Büro", 'href="/customers"', 'href="/mitarbeiter"', 'href="/settings/users"', 'href="/aktivitaeten"', 'href="/hochladen"', "/plattform"):
        assert hidden not in nav, hidden


def test_office_admin_navigation_groups(app, world):
    nav = _sidebar(login(app, "admin-a@example.com").get("/uebersicht").get_data(as_text=True))
    for label in ("Arbeit", "Kunden", "Zeit", "Werkzeuge", "Universal-Upload", "Büro", "Mitarbeiter", "Benutzerverwaltung", "Aktivitäten"):
        assert label in nav, label
    assert "/plattform" not in nav


def test_navigation_has_at_most_two_levels_and_marks_active_entry(app, world):
    html = login(app, "admin-a@example.com").get("/zeiterfassung/woche").get_data(as_text=True)
    nav = _sidebar(html)
    assert nav.count('aria-current="page"') == 1
    assert 'href="/zeiterfassung" class="app-sidebar-link is-active"' in nav
    assert "<ul" not in nav.split("<ul", 1)[1].split("</ul>", 1)[0]  # keine verschachtelte Liste
    assert 'class="app-subnav"' in html and "Woche" in html  # zweite Ebene: Reiter ueber dem Inhalt


def test_start_page_is_reduced(app, world):
    html = login(app, "dennis@example.com").get("/uebersicht").get_data(as_text=True)
    main = html.split('id="inhalt"', 1)[1]
    for text in ("Leipziger Liste", "Sprachnachricht hochladen", "Einstempeln"):
        assert text in main
    for hidden in ("Büro heute", "Letzte Aktivitäten", "dash-card"):
        assert hidden not in main
    admin = login(app, "admin-a@example.com").get("/uebersicht").get_data(as_text=True).split('id="inhalt"', 1)[1]
    assert "Büro heute" in admin and "Letzte Aktivitäten" not in admin
