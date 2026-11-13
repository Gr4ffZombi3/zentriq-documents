# ruff: noqa: F811  (Fixtures werden aus anderen Testmodulen importiert)
"""Termine aus Bildern: Bildpruefung, lokale Erkennung, Vorschau, Speichern. Synthetische Bilder."""

import io
from datetime import date

import pytest
from PIL import Image, ImageDraw, ImageFont

from app.extensions import db as _db
from app.models import CalendarEvent
from app.models.office_settings import OfficeSettings
from app.services import image_appointments as ia
from app.services.ai import adapters, router
from app.services.timetracking.clock import to_local
from app.tenancy import bypass_tenant_scope
from tests.test_customer_notes import API, office  # noqa: F401
from tests.test_permissions_security import login, world  # noqa: F401

FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"


def screenshot(lines, fmt="PNG", size=(1170, 1400)) -> bytes:
    im = Image.new("RGB", size, "white")
    d = ImageDraw.Draw(im)
    d.text((60, 40), "09:41", font=ImageFont.truetype(FONT, 44), fill="black")  # Statusleiste
    y = 200
    for line in lines:
        d.text((70, y), line, font=ImageFont.truetype(FONT, 52), fill=(30, 30, 30))
        y += 100
    buf = io.BytesIO()
    im.save(buf, format=fmt)
    return buf.getvalue()


@pytest.fixture()
def no_ai(monkeypatch):
    def forbidden(*a, **k):
        raise AssertionError("Modellaufruf ist hier nicht erlaubt")

    monkeypatch.setattr(adapters, "call", forbidden)
    assert not router.can_answer()


@pytest.fixture()
def hours(app, office):
    with bypass_tenant_scope():
        _db.session.add(OfficeSettings(tenant_id=office.tenant_a.id, hours={str(d): ["08:00", "18:00"] for d in range(1, 8)},
                                       default_minutes=45, lead_minutes=0))
        _db.session.commit()


def _day(n=2) -> date:
    from tests.conftest import next_workday

    return next_workday(n)


def _upload(client, *images):
    data = {"files": [(io.BytesIO(content), name) for name, content in images]}
    return client.post(API + "/termine/aus-bildern", data=data, content_type="multipart/form-data")


# --- Bildpruefung ---------------------------------------------------------------------------


def test_image_validation(app):
    with app.app_context():
        for fmt, name in (("JPEG", "a.jpg"), ("PNG", "a.png"), ("WEBP", "a.webp")):
            assert ia.load_image(name, screenshot(["x"], fmt=fmt)).mode == "RGB"
        with pytest.raises(ia.ImageError, match="kann nicht als Bild"):
            ia.load_image("fake.png", b"PK\x03\x04 zip statt Bild")
        with pytest.raises(ia.ImageError, match="Foto oder einen Screenshot"):
            ia.load_image("clip.mp4", b"\x00\x00\x00\x18ftypmp42")
        with pytest.raises(ia.ImageError, match="kein unterstütztes Bild"):
            buf = io.BytesIO()
            Image.new("RGB", (10, 10)).save(buf, format="GIF")
            ia.load_image("bild.png", buf.getvalue())  # Endung passt nicht zum Inhalt
        with pytest.raises(ia.ImageError, match="zu groß"):
            ia.load_image("big.jpg", b"\xff" * (ia.MAX_IMAGE_BYTES + 1))


def test_exif_orientation_is_applied(app):
    im = Image.new("RGB", (300, 100), "white")
    exif = Image.Exif()
    exif[0x0112] = 6
    buf = io.BytesIO()
    im.save(buf, format="JPEG", exif=exif)
    with app.app_context():
        assert ia.load_image("foto.jpg", buf.getvalue()).size == (100, 300)


def test_heic_supported_or_clear_message(app):
    with app.app_context():
        try:
            import pillow_heif

            pillow_heif.register_heif_opener()
        except ImportError:
            with pytest.raises(ia.ImageError, match="HEIC"):
                ia.load_image("IMG_0001.HEIC", b"\x00\x00\x00\x18ftypheic")
            return
        buf = io.BytesIO()
        Image.open(io.BytesIO(screenshot(["x"]))).save(buf, format="HEIF")
        assert ia.load_image("IMG_0001.HEIC", buf.getvalue()).mode == "RGB"


# --- Lokale Regeln --------------------------------------------------------------------------


def test_local_rules():
    today = date(2026, 11, 8)
    text = "09:41\nMontag, 09.11.2026\n10:00 Zahnarzt Dr. Weiß\n14:30 - 15:15 Beratung Hausrat\nDi, 10. Nov\n9.30 Uhr\nTeambesprechung\n16 Uhr Rückruf Klein\n12.11. Urlaub"
    rows = [(c.day, c.start, c.minutes, c.title, c.problems) for c in ia.extract(text, today)]
    assert rows == [
        ("2026-11-09", "10:00", None, "Zahnarzt Dr. Weiß", []),
        ("2026-11-09", "14:30", 45, "Beratung Hausrat", []),
        ("2026-11-10", "09:30", None, "Teambesprechung", []),
        ("2026-11-10", "16:00", None, "Rückruf Klein", []),
        ("2026-11-12", None, None, "Urlaub", ["Uhrzeit fehlt"]),
    ]
    assert [c.problems for c in ia.extract("10:00 Zahnarzt", today)] == [["Datum fehlt"]]
    assert ia.is_request("Termine übernehmen") and ia.is_request("bitte die Termine in den Kalender") and not ia.is_request("Was ist das?")


# --- Ablauf ueber die Endpunkte -------------------------------------------------------------


def test_screenshot_to_calendar_without_model(app, office, hours, no_ai):
    admin = login(app, "admin-a@example.com")
    day = _day()
    img = screenshot([day.strftime("Montag, %d.%m.%Y"), "16:30 Termin Schmidthofer Kfz", "17:30 - 18:00 Rückruf Klein"])
    r = _upload(admin, ("IMG_1234.PNG", img))
    assert r.status_code == 200, r.get_json()
    data = r.get_json()
    assert data["used_model"] is False
    items = data["items"]
    assert [(i["day"], i["start"], i["minutes"], i["title"], i["state"]) for i in items] == [
        (day.isoformat(), "16:30", 45, "Termin Schmidthofer Kfz", "ok"),
        (day.isoformat(), "17:30", 30, "Rückruf Klein", "ok"),
    ]
    with bypass_tenant_scope():
        assert CalendarEvent.query.count() == 0  # Vorschau speichert nichts
    # zweiten Termin abwaehlen, ersten speichern
    saved = admin.post(API + "/termine/uebernehmen", json={"items": [items[0]], "batch": "b1"}).get_json()["results"]
    assert saved[0]["status"] == "saved" and "16:30" in saved[0]["message"]
    again = admin.post(API + "/termine/uebernehmen", json={"items": [items[0]], "batch": "b2"}).get_json()["results"]
    assert again[0]["status"] == "duplicate"
    with bypass_tenant_scope():
        [event] = CalendarEvent.query.all()
        assert to_local(event.starts_at).strftime("%H:%M") == "16:30" and event.title == "Termin Schmidthofer Kfz"
    # Zweite berechtigte Person sieht den Termin
    events = login(app, "laura@example.com").get(f"/kalender/api/termine?von={day.isoformat()}&bis={day.isoformat()}").get_json()["events"]
    assert [e["title"] for e in events] == ["Termin Schmidthofer Kfz"]
    # Erneutes Einlesen: Dublette und Belegung markiert
    rows = _upload(admin, ("IMG_1234.PNG", img)).get_json()["items"]
    assert any("Dublette" in f for f in rows[0]["flags"]) and any("belegt" in f for f in rows[0]["flags"]) and rows[0]["state"] == "warn"


def test_busy_slot_reported_per_item_and_edit_resolves(app, office, hours, no_ai):
    admin = login(app, "admin-a@example.com")
    day = _day(3)
    first = {"day": day.isoformat(), "start": "11:00", "minutes": 30, "title": "Belegt", "assignee_id": office.admin_a_id, "ref": "x"}
    assert admin.post(API + "/termine/uebernehmen", json={"items": [first]}).get_json()["results"][0]["status"] == "saved"
    clash = dict(first, title="Anderer Termin", ref="y")
    moved = dict(first, title="Verschoben", start="12:00", ref="z")
    incomplete = {"day": "", "start": "12:00", "title": "Ohne Datum", "assignee_id": office.admin_a_id, "ref": "w"}
    results = {r["ref"]: r for r in admin.post(API + "/termine/uebernehmen", json={"items": [clash, moved, incomplete]}).get_json()["results"]}
    assert results["y"]["status"] == "conflict" and "Nicht übernommen" in results["y"]["message"]
    assert results["z"]["status"] == "saved" and results["w"]["status"] == "incomplete"


def test_unreadable_image_asks_back(app, office, no_ai):
    blank = io.BytesIO()
    Image.new("RGB", (800, 600), (200, 200, 200)).save(blank, format="JPEG")
    data = _upload(login(app, "admin-a@example.com"), ("leer.jpg", blank.getvalue())).get_json()
    assert data["items"] == [] and "Text nicht lesbar" in data["problems"][0] and data["image_status"][0]["status"] == "unreadable"


def test_permissions_and_foreign_assignee(app, office, hours, no_ai):
    day = _day(4)
    item = {"day": day.isoformat(), "start": "10:00", "minutes": 30, "title": "Fremd", "assignee_id": office.admin_b_id, "ref": "f"}
    r = login(app, "admin-a@example.com").post(API + "/termine/uebernehmen", json={"items": [item]}).get_json()["results"][0]
    assert r["status"] == "incomplete"  # Mitarbeiter eines anderen Bueros wird nicht angenommen
    assert app.test_client().post(API + "/termine/aus-bildern", headers={"Accept": "application/json"}).status_code == 401


def test_model_only_for_hard_text_and_only_relevant_lines(app, office, monkeypatch):
    sent = []

    class Done:
        text = '[{"datum": "2026-11-20", "uhrzeit": "09:15", "dauer_minuten": 30, "titel": "Gutachter"}]'

    monkeypatch.setattr(router, "can_answer", lambda: True)
    monkeypatch.setattr(router, "complete", lambda **k: sent.append(k["messages"][0]["content"]) or Done())
    text = "Rechnung Nr. 4711\nIhre Kundennummer 808/123456-E\nBesuch des Gutachters\nam Freitag den 20.11.2026\ngegen neun Uhr fünfzehn\nMit freundlichen Grüßen"
    with app.app_context():
        from app.models import User

        with bypass_tenant_scope():
            user = _db.session.get(User, office.admin_a_id)
        assert ia.extract(text, date(2026, 11, 8))[0].start is None  # lokal keine Uhrzeit
        lines = ia.relevant_lines(text)
        assert "Rechnung Nr. 4711" not in lines and "Mit freundlichen Grüßen" not in lines
        rows = ia._model_extract(user, lines, date(2026, 11, 8))
    assert [(r.day, r.start, r.minutes, r.title) for r in rows] == [("2026-11-20", "09:15", 30, "Gutachter")]
    assert "4711" not in sent[0] and "20.11.2026" in sent[0]


def test_chat_accepts_image_attachment_with_local_text(app, office, no_ai):
    client = login(app, "laura@example.com")
    conversation = client.post(API + "/conversations", json={}).get_json()["conversation"]["id"]
    img = screenshot(["Termin Montag 10:00"], fmt="JPEG")
    r = client.post(f"{API}/conversations/{conversation}/messages", data={"content": "Was steht hier?", "files": (io.BytesIO(img), "foto.jpg")},
                    content_type="multipart/form-data")
    assert r.status_code == 202, r.get_json()
    from app.models.ai import AiAttachment

    with bypass_tenant_scope():
        att = AiAttachment.query.one()
    assert att.kind == "image" and "10:00" in (att.text or "")


# --- Befehl im Hauptchat (mit Bild / Bild zuerst, Befehl danach) ----------------------------


@pytest.mark.parametrize("text, with_image, without_image", [
    ("Trage die Termine aus dem Bild ein", True, True),
    ("Übernimm die Termine vom Screenshot", True, True),
    ("Lies die Termine aus dem Foto aus", True, True),
    ("Termine übernehmen", True, False),
    ("Termin mit Max morgen um 10 Uhr eintragen", True, False),  # ohne Bild: normaler Kalenderbefehl
    ("Welche Termine habe ich morgen?", False, False),
    ("Schreib eine Mail mit Terminvorschlägen", False, False),
    ("Was steht auf dem Bild?", False, False),
])
def test_command_variants(text, with_image, without_image):
    assert ia.is_request(text) is with_image
    assert ia.is_chat_request(text) is without_image


def _conversation(client):
    return client.post(API + "/conversations", json={}).get_json()["conversation"]["id"]


def _upload_to_chat(client, conversation, *images, text="Trage die Termine aus dem Bild ein"):
    data = {"files": [(io.BytesIO(content), name) for name, content in images], "conversation_id": str(conversation), "text": text}
    return client.post(API + "/termine/aus-bildern", data=data, content_type="multipart/form-data")


def test_image_and_command_together_keeps_image_in_chat(app, office, hours, no_ai):
    admin = login(app, "admin-a@example.com")
    conv = _conversation(admin)
    day = _day()
    img = screenshot([day.strftime("Montag, %d.%m.%Y"), "09:30 Teambesprechung", "14:00 Beratung Klein"])
    r = _upload_to_chat(admin, conv, ("IMG_7.PNG", img))
    assert r.status_code == 200 and [i["title"] for i in r.get_json()["items"]] == ["Teambesprechung", "Beratung Klein"]
    messages = admin.get(f"{API}/conversations/{conv}").get_json()["messages"]
    assert messages[0]["role"] == "user" and messages[0]["content"].startswith("Trage die Termine")
    assert "2 Termin(e) aus dem Bild erkannt" in messages[1]["content"]
    # derselbe Befehl in der naechsten Eingabe (ohne Anhang) findet das gespeicherte Bild
    assert admin.post(API + "/kunden/eingabe", json={"text": "Trage die Termine aus dem Bild ein"}).get_json()["kind"] == "appointments_from_chat"


def test_image_first_command_afterwards(app, office, hours, no_ai):
    client = login(app, "laura@example.com")
    conv = _conversation(client)
    day = _day()
    img = screenshot([day.strftime("Dienstag, %d.%m.%Y"), "10:00 Zahnarzt", "16:30 - 17:00 Rückruf Herr Klein"], fmt="JPEG")
    sent = client.post(f"{API}/conversations/{conv}/messages", data={"content": "", "files": (io.BytesIO(img), "screenshot.jpg")},
                       content_type="multipart/form-data")
    assert sent.status_code == 202
    reply = client.get(f"{API}/conversations/{conv}/messages/{sent.get_json()['reply']['id']}").get_json()["message"]
    assert reply["status"] == "done" and "2 Termin(e) im Bild erkannt" in reply["content"]  # lokal, ohne Modell
    routed = client.post(API + "/kunden/eingabe", json={"text": "Übernimm die Termine vom Screenshot"}).get_json()
    assert routed["kind"] == "appointments_from_chat"
    data = client.post(API + "/termine/aus-chat", json={"conversation_id": conv}).get_json()
    items = data["items"]
    assert [(i["start"], i["title"], i["minutes"]) for i in items] == [("10:00", "Zahnarzt", 45), ("16:30", "Rückruf Herr Klein", 30)]
    saved = client.post(API + "/termine/uebernehmen", json={"items": items, "batch": "x"}).get_json()["results"]
    assert [r["status"] for r in saved] == ["saved", "saved"]
    with bypass_tenant_scope():
        assert CalendarEvent.query.count() == 2


def test_missing_multiple_and_foreign_images(app, office, hours, no_ai):
    client = login(app, "laura@example.com")
    conv = _conversation(client)
    r = client.post(API + "/termine/aus-chat", json={"conversation_id": conv})
    assert r.status_code == 400 and r.get_json()["code"] == "no_image" and r.get_json()["error"].startswith("Bild fehlt")
    day = _day()
    a = screenshot([day.strftime("%d.%m.%Y"), "10:00 Termin A"])
    b = screenshot([day.strftime("%d.%m.%Y"), "11:00 Termin B"])
    _upload_to_chat(client, conv, ("a.png", a), ("b.png", b), text="Bilder")
    choose = client.post(API + "/termine/aus-chat", json={"conversation_id": conv}).get_json()
    assert choose["kind"] == "choose_image" and [o["name"] for o in choose["options"]] == ["a.png", "b.png"]
    # Hinweis: die Notiz der KI-freien Auswertung ist die letzte Nachricht; das Bild gehoert zur letzten Benutzernachricht
    one = client.post(API + "/termine/aus-chat", json={"conversation_id": conv, "attachment_ids": [choose["options"][1]["id"]]}).get_json()
    assert [i["title"] for i in one["items"]] == ["Termin B"]
    # fremdes Gespraech (anderer Benutzer) -> nicht gefunden
    other = login(app, "dennis@example.com")
    assert other.post(API + "/termine/aus-chat", json={"conversation_id": conv}).status_code == 404


def test_error_kinds(app, office, no_ai):
    client = login(app, "admin-a@example.com")
    conv = _conversation(client)
    r = _upload_to_chat(client, conv, ("kaputt.png", b"PK\x03\x04 kein Bild"))
    assert r.status_code == 400 and r.get_json()["code"] == "upload_failed" and r.get_json()["error"].startswith("Upload fehlgeschlagen")
    blank = io.BytesIO()
    Image.new("RGB", (800, 600), (200, 200, 200)).save(blank, format="JPEG")
    unreadable = _upload_to_chat(client, conv, ("leer.jpg", blank.getvalue())).get_json()
    assert unreadable["image_status"][0]["status"] == "unreadable" and "Text nicht lesbar" in unreadable["problems"][0]
    text_only = _upload_to_chat(client, conv, ("brief.png", screenshot(["Sehr geehrte Damen und Herren", "vielen Dank für Ihr Schreiben"]))).get_json()
    assert text_only["image_status"][0]["status"] == "none" and "keine Termine" in text_only["problems"][0]
    assert client.post(API + "/termine/aus-bildern", data={}, content_type="multipart/form-data").get_json()["code"] == "no_image"
