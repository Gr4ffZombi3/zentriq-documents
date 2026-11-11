"""Freier Schreibauftrag im Hauptchat ("Schreib eine E-Mail-Antwort: ...") ohne Kundenakte.

Ablauf wie im Browser: Eingabefeld (/kunden/eingabe) -> bei "chat" die Nachricht an den Chat
(Worker-Verarbeitung laeuft in den Tests synchron). Ohne Modell greift die Vorlage; wo das Modell
geprueft wird, ist router.complete ersetzt. Nichts wird gesendet oder angelegt."""

from datetime import time, timedelta

import pytest

from app.extensions import db as _db
from app.models import CalendarEvent, Customer, OfficeSettings
from app.models.ai import AiConversation, AiMessage
from app.services import chat_task, email_reply
from app.services.ai import router
from app.services.timetracking.clock import local_to_utc_naive, local_today
from app.tenancy import bypass_tenant_scope

API = "/zentriq/api"
ORDER = ("Schreib eine E-Mail-Antwort: Entschuldige dich für die vorherige automatische Nachricht. Das Angebot habe ich "
         "bereits versendet. Ein Leihwagen kommt grundsätzlich infrage, wird aber im Einzelfall geprüft. Als Werkstatt in "
         "Bad Neuenahr-Ahrweiler nenne die Huther GmbH.")
HORNA_MAIL = """Von: Thomas Horna <t.horna@example.de>
An: info@buero-test.de
Betreff: Kfz-Schaden

Sehr geehrte Damen und Herren,

wie geht es mit meinem Schaden weiter?

Mit freundlichen Grüßen
Thomas Horna
"""


@pytest.fixture()
def office(app, tenant, user):
    _db.session.add(OfficeSettings(tenant_id=tenant.id, office_name="Büro Test", email="info@buero-test.de", phone="0228 123450",
                                   address="Hauptstraße 1, 53111 Bonn"))
    _db.session.commit()
    return tenant


@pytest.fixture()
def no_model(monkeypatch):
    monkeypatch.setattr(router, "can_answer", lambda: False)


def _customer(tenant, name, **kw):
    c = Customer(tenant_id=tenant.id, name=name, **kw)
    _db.session.add(c)
    _db.session.commit()
    return c


def _conversation(client):
    resp = client.post(f"{API}/conversations", json={"customer_data": True})
    assert resp.status_code == 201
    return resp.get_json()["conversation"]["id"]


def _say(client, text, cid=None, **extra):
    body = {"text": text, **extra}
    if cid:
        body["conversation_id"] = cid
    resp = client.post(f"{API}/kunden/eingabe", json=body)
    assert resp.status_code == 200, resp.get_json()
    return resp.get_json()


def _chat(client, cid, text):
    """Wie der Browser: erst das Eingabefeld, bei "chat" an den Chat."""
    r = _say(client, text, cid)
    if r["kind"] != "chat":
        return r, None
    resp = client.post(f"{API}/conversations/{cid}/messages", json={"content": text})
    assert resp.status_code == 202, resp.get_json()
    return r, AiMessage.query.filter(AiMessage.id == resp.get_json()["reply"]["id"]).first().content


def _draft(content: str) -> str:
    assert "```E-Mail-Entwurf" in content, content
    return content.split("```E-Mail-Entwurf\n", 1)[1].rsplit("```", 1)[0]


def _assert_order_draft(draft: str):
    assert draft.startswith("Betreff: ") and "\n\nGuten Tag,\n\n" in draft
    assert "Bitte entschuldigen Sie die vorherige automatische Nachricht." in draft
    assert "Das Angebot habe ich bereits versendet." in draft
    assert "wird aber im Einzelfall geprüft" in draft
    assert "Als Werkstatt in Bad Neuenahr-Ahrweiler empfehle ich Ihnen die Huther GmbH." in draft
    assert "Mit freundlichen Grüßen" in draft and "Büro Test" in draft  # Buerosignatur
    for wrong in ("Geburtsdatum", "VN-Nummer", "An:", "Für welchen Kunden"):
        assert wrong not in draft


# --- Erkennung ---------------------------------------------------------------------------------


def test_recipient_and_content_detection():
    assert email_reply.has_content(ORDER)
    assert email_reply.named_recipient(ORDER) is None  # "Huther GmbH" im Inhalt ist kein Empfaenger
    for text in ("Schreib eine Mail", "Schreib dem Kunden eine Antwort", "Formuliere eine E-Mail an Frau Becker"):
        assert not email_reply.has_content(text), text
    becker = email_reply.named_recipient("Formuliere eine E-Mail an Frau Becker: Das Angebot ist versendet.")
    assert (becker.salutation, becker.first_name, becker.last_name) == ("Frau", None, "Becker")
    weber = email_reply.named_recipient("Schreib eine Mail an Max Weber, dass das Angebot versendet ist.")
    assert (weber.salutation, weber.first_name, weber.last_name) == (None, "Max", "Weber")
    assert email_reply._greeting(weber) == "Guten Tag Max Weber,"
    # Vorlage ohne Modell: alle Aussagen, auch ohne Doppelpunkt
    assert email_reply.statements_of("Schreib eine Mail, dass das Angebot versendet ist. Der Termin bleibt bestehen.") == [
        "Gern teile ich Ihnen mit, dass das Angebot versendet ist.", "Der Termin bleibt bestehen."]


# --- Ablauf im Hauptchat -------------------------------------------------------------------------


def test_order_without_name_gives_draft_with_empty_customer_db(auth_client, office, no_model):
    with bypass_tenant_scope():
        assert Customer.query.count() == 0
    cid = _conversation(auth_client)
    r, content = _chat(auth_client, cid, ORDER)
    assert r["kind"] == "chat"
    _assert_order_draft(_draft(content))
    assert "nicht gesendet" in content
    conversation = AiConversation.query.filter(AiConversation.id == cid).first()
    assert chat_task.get(conversation) is None  # keine Rueckfrage offen
    with bypass_tenant_scope():
        assert Customer.query.count() == 0 and CalendarEvent.query.count() == 0


def test_model_gets_full_order_and_rules(auth_client, office, monkeypatch):
    calls = []

    class Fake:
        text = ("Guten Tag,\n\nbitte entschuldigen Sie die vorherige automatische Nachricht. Das Angebot habe ich Ihnen bereits "
                "zugesendet. Ein Leihwagen ist grundsätzlich möglich, wir prüfen das im Einzelfall. Als Werkstatt in "
                "Bad Neuenahr-Ahrweiler empfehle ich die Huther GmbH.\n\nMit freundlichen Grüßen\nX")
        provider, model, model_label, tools_used = "fake", "fake", "Fake", []
        input_tokens = output_tokens = 0
        cost_usd, cached, fallback_used = 0, False, False

    monkeypatch.setattr(router, "can_answer", lambda: True)
    monkeypatch.setattr(router, "complete", lambda **kw: calls.append(kw) or Fake())
    cid = _conversation(auth_client)
    _r, content = _chat(auth_client, cid, ORDER)
    draft = _draft(content)
    assert len(calls) == 1
    prompt = calls[0]["messages"][0]["content"]
    assert "nenne die Huther GmbH." in prompt  # Auftrag vollstaendig (frueher nach 300 Zeichen abgeschnitten)
    assert "Einzelfall" in calls[0]["system"] and "Geburtsdatum" in calls[0]["system"]
    assert draft.count("Guten Tag,") == 1 and "\nX" not in draft and "Büro Test" in draft


def test_explicit_name_without_customer_file(auth_client, office, no_model):
    _customer(office, "Klaus Huther")  # Name aus dem Inhalt darf nicht als Empfaenger gelten
    cid = _conversation(auth_client)
    _r, content = _chat(auth_client, cid, "Schreib eine E-Mail-Antwort an Herrn Weber: Das Angebot habe ich bereits versendet.")
    draft = _draft(content)
    assert "Sehr geehrter Herr Weber," in draft and "Das Angebot habe ich bereits versendet." in draft
    assert "An:" not in draft and "Ohne Kundenakte" in content
    _r, content = _chat(auth_client, cid, ORDER)
    draft = _draft(content)
    assert "Guten Tag," in draft and "Huther GmbH" in draft and "Klaus" not in draft and "Weber" not in draft


def test_no_name_from_older_unrelated_draft(auth_client, office, no_model):
    _customer(office, "Thomas Horna", first_name="Thomas", last_name="Horna", email="t.horna@example.de")
    cid = _conversation(auth_client)
    _chat(auth_client, cid, HORNA_MAIL)
    _r, content = _chat(auth_client, cid, "Schreibe eine Antwort")
    assert "Horna" in _draft(content)  # der aeltere Entwurf fuer Horna
    _r, content = _chat(auth_client, cid, ORDER)
    draft = _draft(content)
    _assert_order_draft(draft)
    assert "Horna" not in draft and "t.horna" not in draft and "AW:" not in draft


def test_older_confirmation_draft_is_not_reused(auth_client, office, user, no_model):
    horna = _customer(office, "Thomas Horna", first_name="Thomas", last_name="Horna", email="t.horna@example.de")
    start = local_to_utc_naive(local_today() + timedelta(days=2), time(14, 0))
    _db.session.add(CalendarEvent(tenant_id=office.id, title="Beratung", starts_at=start, ends_at=start + timedelta(minutes=45),
                                  assignee_user_id=user.id, customer_id=horna.id))
    _db.session.commit()
    cid = _conversation(auth_client)
    r = _say(auth_client, "Bestätige den bereits gebuchten Termin", cid)
    assert r["kind"] == "task" and r["message"] == chat_task.ASK_CUSTOMER  # braucht Kalenderdaten -> Rueckfrage
    r = _say(auth_client, "Horna", cid)
    assert r["draft"] and "Thomas Horna" in r["draft"]
    _r, content = _chat(auth_client, cid, ORDER)
    _assert_order_draft(_draft(content))
    assert "Horna" not in content
    r = _say(auth_client, "Kürzer", cid)  # bezieht sich nicht mehr auf die alte Terminbestaetigung
    assert not r.get("draft") or "Horna" not in r["draft"]


def test_question_only_without_content_then_name_continues(auth_client, office, no_model):
    cid = _conversation(auth_client)
    _r, content = _chat(auth_client, cid, "Schreib eine Mail")
    assert content == email_reply.CLARIFY and "Geburtsdatum" not in content
    conversation = AiConversation.query.filter(AiConversation.id == cid).first()
    assert chat_task.public(conversation)["label"] == "Inhalt oder Empfänger für den E-Mail-Entwurf"
    r = _say(auth_client, "Frau Becker", cid)  # keine Akte "Becker" - Name wird trotzdem verwendet
    assert r["kind"] == "task" and "Sehr geehrte Frau Becker," in r["draft"] and r["task"] is None
    assert "Becker ist noch nicht angelegt" not in str(r)
    with bypass_tenant_scope():
        assert Customer.query.count() == 0


def test_question_answered_with_content(auth_client, office, no_model):
    cid = _conversation(auth_client)
    _chat(auth_client, cid, "Schreib eine Mail")
    r = _say(auth_client, "Das Angebot habe ich bereits versendet.", cid)
    assert r["kind"] == "task" and "Guten Tag," in r["draft"] and "Das Angebot habe ich bereits versendet." in r["draft"]


def test_name_alone_without_open_order_stays_search(auth_client, office, no_model):
    _customer(office, "Thomas Horna", first_name="Thomas", last_name="Horna")
    cid = _conversation(auth_client)
    _chat(auth_client, cid, ORDER)  # fertiger Entwurf, keine offene Rueckfrage
    r = _say(auth_client, "Horna", cid)
    assert r["kind"] == "card" and r["card"]["name"] == "Thomas Horna"


def test_input_field_sends_writing_order_to_chat_without_model(auth_client, office, no_model):
    r = _say(auth_client, "Formuliere eine E-Mail: Das Angebot habe ich bereits versendet.")
    assert r["kind"] == "chat"
