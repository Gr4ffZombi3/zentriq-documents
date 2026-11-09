"""Antwort auf Kunden-E-Mails: Kontext, Kundenzuordnung/-anlage, freie Termine, Buchung.

Kein echter KI-Anbieter: ohne Modell greift die feste Vorlage; wo das Modell geprueft wird, ist
router.complete ersetzt (Antwort enthaelt bewusst Fehler, die das Backend korrigieren muss)."""

from datetime import date, datetime, time, timedelta
from types import SimpleNamespace

import pytest

from app.extensions import db as _db
from app.models import CalendarEvent, Customer, CustomerNote, OfficeSettings, Tenant, User, UserRole
from app.models.ai import AiMessage
from app.services import availability, email_context
from app.services.timetracking.clock import local_to_utc_naive, local_today, to_local, utcnow_naive
from app.tenancy import bypass_tenant_scope, use_tenant_id

API = "/zentriq/api"

EMAIL = """Von: Thomas Horna <t.horna@example.de>
An: info@buero-test.de
Betreff: Beratungstermin Kfz

Sehr geehrte Damen und Herren,

ich bin Herr Horna
Geburtsdatum: 03.04.1971
Vertragsnummer: 808/654321-E-12
Meine Vermittlernummer laut Schreiben: 08/1234-A

ich hätte gern einen Termin zur Beratung, am liebsten nachmittags.

Mit freundlichen Grüßen
Thomas Horna
Tel. 0171 2223334

Am 01.11.2026 schrieb Büro Test <info@buero-test.de>:
> Sehr geehrter Herr Mustermann, Ihre Vertragsnummer 808/999999-Z-01 ...
> Telefon 0228 999999
"""


@pytest.fixture()
def office(app, tenant, user, employee):
    _db.session.add(OfficeSettings(
        tenant_id=tenant.id, office_name="Büro Test", email="info@buero-test.de", phone="0228 123450",
        address="Hauptstraße 1, 53111 Bonn", hours={str(d): ["09:00", "17:00"] for d in range(1, 8)},
        default_minutes=30, buffer_minutes=15, lead_minutes=0, horizon_days=14,
    ))
    _db.session.commit()
    return tenant


def _conversation(client, customer_data=True):
    resp = client.post(f"{API}/conversations", json={"customer_data": customer_data})
    assert resp.status_code == 201, resp.get_json()
    return resp.get_json()["conversation"]["id"]


def _send(client, conversation_id, content):
    resp = client.post(f"{API}/conversations/{conversation_id}/messages", json={"content": content})
    assert resp.status_code == 202, resp.get_json()
    reply_id = resp.get_json()["reply"]["id"]
    return AiMessage.query.filter(AiMessage.id == reply_id).first()


def _reply_for_appointment(client):
    cid = _conversation(client)
    _send(client, cid, EMAIL)
    return _send(client, cid, "Antwort für einen Termin schreiben")


# --- Zerlegung ---------------------------------------------------------------------------------


def test_parse_separates_sender_office_fields_signature_and_quote():
    info = email_context.parse(EMAIL, {"info@buero-test.de"})
    assert (info.first_name, info.last_name, info.sender_email) == ("Thomas", "Horna", "t.horna@example.de")
    assert info.recipients == ["info@buero-test.de"]
    assert info.birthdate == date(1971, 4, 3)
    assert info.contracts == ["808/654321-E-12"] and info.vn == "808/654321-E"
    assert info.broker_numbers == ["08/1234-A"]
    assert info.phone == "+491712223334"
    # Zitat: weder fremde Vertragsnummer noch fremde Telefonnummer
    assert "808/999999-Z-01" in info.quoted and "808/999999-Z-01" not in info.body
    assert "Geburtsdatum" not in (info.last_name or "")


def test_own_office_message_makes_recipient_the_customer():
    text = "Von: info@buero-test.de\nAn: kunde@example.de\nBetreff: Termin\n\nGuten Tag,\nanbei ...\n\nViele Grüße\nBüro"
    info = email_context.parse(text, {"info@buero-test.de"})
    assert info.sender_email == "kunde@example.de"


# --- Ablauf ueber die Chat-API -------------------------------------------------------------------


def test_reply_creates_customer_and_offers_real_free_slots(auth_client, office, user):
    reply = _reply_for_appointment(auth_client)
    content = reply.content
    assert reply.status == "done", content
    # Status ausserhalb des Entwurfs, nach erfolgreicher Anlage
    assert "✓ Kunde angelegt: Thomas Horna" in content
    assert content.index("Kunde angelegt") < content.index("```E-Mail-Entwurf")
    draft = content.split("```E-Mail-Entwurf\n", 1)[1]
    # Anrede ohne Feldbezeichnung, Kunden-E-Mail nur als Empfaenger, Buero-Kontakt aus dem Profil
    assert "Guten Tag Thomas Horna," in draft and "Geburtsdatum" not in draft.split("\n\n")[1]
    assert draft.startswith("An: t.horna@example.de") and draft.count("t.horna@example.de") == 1
    assert "info@buero-test.de" in draft and "Büro Test" in draft
    assert "Entwurf – nicht gesendet, kein Termin gebucht." in content
    with bypass_tenant_scope():
        [customer] = Customer.query.filter_by(name="Thomas Horna").all()
        assert customer.email == "t.horna@example.de" and customer.phone == "0171 2223334"
        assert customer.customer_number == "808/654321-E"  # VN aus Vertragsnummer, nie Vermittlernummer
        assert customer.date_of_birth == date(1971, 4, 3) and customer.field_sources["email"] == "E_MAIL"
        assert CalendarEvent.query.count() == 0 and CustomerNote.query.count() == 0  # nichts gebucht, keine Notiz
    # Vorschlaege: nachmittags (Kundenwunsch), innerhalb der Buerozeiten, verschiedene Tage
    lines = [line for line in draft.splitlines() if line.startswith("- ")]
    assert 1 <= len(lines) <= 3
    for line in lines:
        start = line.split(", ")[2].split("–")[0]
        assert "12:00" <= start <= "16:30"


def test_repeat_assigns_existing_customer_without_duplicate(auth_client, office):
    _reply_for_appointment(auth_client)
    second = _reply_for_appointment(auth_client)
    assert "✓ Kunde zugeordnet: Thomas Horna" in second.content
    with bypass_tenant_scope():
        assert Customer.query.filter_by(name="Thomas Horna").count() == 1


def test_existing_customer_found_by_vn_and_not_overwritten(auth_client, office, tenant):
    existing = Customer(tenant_id=tenant.id, name="Thomas Horna", customer_number="808/654321-E", phone="0171 0000000")
    _db.session.add(existing)
    _db.session.commit()
    reply = _reply_for_appointment(auth_client)
    assert "Kunde zugeordnet: Thomas Horna (abweichend in der Nachricht: Telefon – nicht übernommen)" in reply.content
    _db.session.refresh(existing)
    assert existing.phone == "0171 0000000" and existing.email is None


def test_slots_avoid_events_and_buffer(auth_client, office, user, tenant):
    # Morgen 12:00-16:00 belegt (+15 Min Puffer) -> morgen ist nachmittags nichts frei.
    tomorrow = local_today() + timedelta(days=1)
    _db.session.add(CalendarEvent(tenant_id=tenant.id, title="Belegt", starts_at=local_to_utc_naive(tomorrow, time(12, 0)),
                                  ends_at=local_to_utc_naive(tomorrow, time(16, 50)), assignee_user_id=user.id, created_by_user_id=user.id))
    _db.session.commit()
    draft = _reply_for_appointment(auth_client).content
    assert tomorrow.strftime("%d.%m.%Y") not in draft


def test_missing_planning_rules_are_reported_not_invented(auth_client, tenant, user):
    reply = _reply_for_appointment(auth_client)
    assert "fehlen die Bürozeiten" in reply.content and "Es wurden keine freien Termine geprüft" in reply.content
    draft = reply.content.split("```E-Mail-Entwurf\n", 1)[1]
    assert "Folgende Termine" not in draft and "nennen Sie mir zwei bis drei Zeiten" in draft


def test_neutral_salutation_when_name_unclear(auth_client, office):
    cid = _conversation(auth_client)
    _send(auth_client, cid, "Hallo,\nbitte um Rückruf wegen eines Termins.\nDanke\nKfz-Team <info@firma.de>")
    reply = _send(auth_client, cid, "Schreibe eine Antwort, um einen Termin zu machen")
    assert "Guten Tag,\n" in reply.content
    with bypass_tenant_scope():
        assert Customer.query.count() == 0


def test_model_text_is_cleaned(auth_client, office, monkeypatch):
    from app.services.ai import router

    body = ("Sehr geehrter Herr Horna Geburtsdatum,\nvielen Dank für Ihre Anfrage.\n[[TERMINVORSCHLAEGE]]\n"
            "Sie erreichen uns unter t.horna@example.de.\nMit freundlichen Grüßen\nIhr Team")
    monkeypatch.setattr(router, "can_answer", lambda: True)
    monkeypatch.setattr(router, "available_models", lambda **k: [object()])
    fake = SimpleNamespace(text=body, provider="test", model="test", tools_used=[], input_tokens=0, output_tokens=0, cost_usd=0,
                           cached=False, fallback_used=False, model_label="Test")
    monkeypatch.setattr(router, "complete", lambda **k: fake)
    draft = _reply_for_appointment(auth_client).content.split("```E-Mail-Entwurf\n", 1)[1]
    assert "Horna Geburtsdatum" not in draft and draft.count("t.horna@example.de") == 1
    assert "vielen Dank für Ihre Anfrage." in draft and "- " in draft and "Ihr Team" not in draft


def test_pasted_email_alone_is_no_instruction():
    from app.services import email_reply

    assert not email_reply.is_request(EMAIL.replace("einen Termin", "einen Termin, bitte um Rückmeldung"))
    assert email_reply.is_request("Antwort für einen Termin schreiben")
    assert email_reply.is_request("Bitte Antwort mit Terminvorschlägen:\n\n" + EMAIL)
    # Vorgaben an den Chat sind kein E-Mail-Auftrag (Produktionsfall 09.11.2026)
    for text in ("Was ist der Unterschied zwischen Teilkasko und Vollkasko? Antworte in zwei Sätzen.",
                 "Antworte kurz: Was bedeutet Selbstbeteiligung?", "Beantworte bitte die Frage knapp"):
        assert not email_reply.is_request(text), text
    assert email_reply.is_request("Antwort an Herrn Meier wegen der Kündigung")


def test_input_field_hands_reply_instruction_to_chat(auth_client, office):
    data = auth_client.post(f"{API}/kunden/eingabe", json={"text": "Schreibe eine Antwort, um einen Termin zu machen"}).get_json()
    assert data["kind"] == "chat"
    with bypass_tenant_scope():
        assert CalendarEvent.query.count() == 0


# --- Verfuegbarkeit ------------------------------------------------------------------------------


def test_dst_change_keeps_local_office_hours(app, office, user):
    s = OfficeSettings.query.first()
    now = datetime(2027, 3, 26, 18, 0)  # Fr; So 28.03.2027 Zeitumstellung
    slots, _ = availability.free_slots(user, 30, availability.Preferences(from_day=date(2027, 3, 29)), count=1, s=s, now=now)
    assert to_local(slots[0].start_utc).strftime("%Y-%m-%d %H:%M") == "2027-03-29 09:00"
    assert slots[0].start_utc == datetime(2027, 3, 29, 7, 0)  # MESZ = UTC+2


def test_employee_workdays_respected(app, office, employee):
    from app.models import EmployeeProfile

    _db.session.add(EmployeeProfile(tenant_id=employee.tenant_id, user_id=employee.id, workdays="2"))  # nur Dienstag
    _db.session.commit()
    slots, _ = availability.free_slots(employee, 30, count=3)
    assert slots and all(to_local(x.start_utc).isoweekday() == 2 for x in slots)


# --- Buchung -------------------------------------------------------------------------------------


def _say(client, text, **extra):
    return client.post(f"{API}/kunden/eingabe", json={"text": text, **extra}).get_json()


def test_explicit_booking_rechecks_and_prevents_double_booking(auth_client, office, tenant, user, employee):
    from app.models import EmployeeProfile

    _db.session.add(EmployeeProfile(tenant_id=tenant.id, user_id=employee.id, display_name="Laura Test"))
    customer = Customer(tenant_id=tenant.id, name="Max Mustermann")
    _db.session.add(customer)
    _db.session.commit()
    day = local_today() + timedelta(days=2)
    text = f"Buche {day.strftime('%d.%m.%Y')} um 14 Uhr für Max Mustermann bei Laura"
    first = _say(auth_client, text, client_ref="b1")
    assert first["kind"] == "calendar_saved" and "Termin gebucht (bestätigt)" in first["message"], first
    event = CalendarEvent.query.one()
    assert event.customer_id == customer.id and event.assignee_user_id == employee.id
    # Wiederholung: kein zweiter Termin
    again = _say(auth_client, text, client_ref="b2")
    assert again["duplicate"] is True and CalendarEvent.query.count() == 1
    # Geaenderte Verfuegbarkeit: 14:15 kollidiert (Puffer) -> nicht gebucht, Alternativen
    clash = _say(auth_client, f"Buche {day.strftime('%d.%m.%Y')} um 14:30 Uhr für Max Mustermann bei Laura", client_ref="b3")
    assert clash["kind"] == "calendar_question" and clash["message"].startswith("Nicht gebucht:")
    assert "Freie Alternativen" in clash["message"] and CalendarEvent.query.count() == 1


def test_booking_outside_office_hours_is_refused(auth_client, office):
    day = local_today() + timedelta(days=2)
    data = _say(auth_client, f"Buche {day.strftime('%d.%m.%Y')} um 20 Uhr")
    assert data["kind"] == "calendar_question" and "Außerhalb der Bürozeiten" in data["message"]
    assert CalendarEvent.query.count() == 0


def test_booking_tool_requires_confirmation_and_rechecks(app, auth_client, office, user, tenant):
    from app.services.ai import actions, tools

    day = (local_today() + timedelta(days=3)).isoformat()
    prepared = tools.run(user, "termin_buchen", {"datum": day, "uhrzeit": "10:00", "dauer_minuten": 30})
    assert prepared["aktion"]["tool"] == "termin_buchen" and CalendarEvent.query.count() == 0
    action = actions.prepare(user, None, "termin_buchen", prepared["aktion"]["arguments"], prepared["aktion"]["summary"])
    # Zwischen Vorschlag und Bestaetigung wird die Zeit belegt.
    start = local_to_utc_naive(date.fromisoformat(day), time(10, 0))
    _db.session.add(CalendarEvent(tenant_id=tenant.id, title="Dazwischen", starts_at=start, ends_at=start + timedelta(minutes=30),
                                  assignee_user_id=user.id, created_by_user_id=user.id))
    _db.session.commit()
    done = actions.confirm(user, action.id)
    assert done.status == "failed" and done.result.startswith("Nicht gebucht:")
    assert CalendarEvent.query.count() == 1
    # Freie Zeit: Bestaetigung bucht genau einmal.
    ok = tools.run(user, "termin_buchen", {"datum": day, "uhrzeit": "15:00", "dauer_minuten": 30})
    action = actions.prepare(user, None, "termin_buchen", ok["aktion"]["arguments"], ok["aktion"]["summary"])
    assert actions.confirm(user, action.id).status == "done"
    assert actions.confirm(user, action.id).status == "done" and CalendarEvent.query.count() == 2


def test_tools_respect_office_separation(app, auth_client, office, tenant):
    from app.services.ai import tools

    other = Tenant(name="Fremd", slug="fremd-mail")
    _db.session.add(other)
    _db.session.commit()
    with use_tenant_id(other.id):
        stranger = User(tenant_id=other.id, email="fremd-mail@example.com", is_active=True, role=UserRole.OFFICE_ADMIN)
        stranger.set_password("fremdpasswort123")
        _db.session.add_all([stranger, Customer(tenant_id=other.id, name="Fremd Kunde", email="f@example.de")])
        _db.session.commit()
    with use_tenant_id(tenant.id):
        user = User.query.filter_by(email="test@example.com").one()
        assert tools.run(user, "kunden_suchen", {"suchbegriff": "f@example.de"})["anzahl"] == 0
    with use_tenant_id(other.id):
        result = tools.run(stranger, "bueroprofil", {})
        assert result["buero"] is None and result["terminplanung_eingerichtet"] is False
    assert utcnow_naive()  # Hilfsimport genutzt


def test_pasted_email_goes_to_conversation_not_notes(auth_client, office):
    data = auth_client.post(f"{API}/kunden/eingabe", json={"text": EMAIL[:900]}).get_json()
    assert data["kind"] == "chat"
    with bypass_tenant_scope():
        assert Customer.query.count() == 0 and CustomerNote.query.count() == 0
