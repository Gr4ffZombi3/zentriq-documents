# ruff: noqa: F811  (Fixtures werden aus anderen Testmodulen importiert)
"""ZentriqAI-Orchestrierung: Anfrage -> Absicht -> berechtigte Daten -> kurze Antwort.

Deckt die Pruefszenarien des Auftrags ab: einfache Antwort, Direktantworten aus echten Daten
ohne KI-Aufruf, Dokumentanalyse mit dem vorhandenen Parser, Zusammenfassung von Transkripten
(pseudonymisiert), Buerotrennung, Fallback, keine Datenaenderung bei Fehlern, bestaetigte
Aktionen genau einmal, nicht blockierende Verarbeitung, Doppelversand, keine kostenpflichtigen
Aufrufe ohne Freigabe. Anbieter werden nie echt aufgerufen."""

import io
import json
from datetime import date

import pytest

from app.models import Customer, CustomerMemo, Task, User
from app.models.ai import AiAction, AiMessage, AiModel, AiUsage
from app.models.enums import Priority, TaskStatus, TaskType
from app.services.ai import adapters, orchestrator
from app.services.ai.privacy import Pseudonymizer
from app.tenancy import bypass_tenant_scope, use_tenant_id
from tests.test_leipziger_parser import PAGE_1, PAGE_2, make_list_pdf
from tests.test_permissions_security import login, world  # noqa: F401
from tests.test_zentriq_ai import FakeProviders, Sent, _approve, _new, _reset_state, _send, fake  # noqa: F401


def _task(db, tenant_id, title, status=TaskStatus.OPEN, priority=Priority.MEDIUM, customer=None, user=None):
    with use_tenant_id(tenant_id):
        task = Task(
            tenant_id=tenant_id, type=TaskType.CALL_TODAY, title=title, status=status, priority=priority,
            due_date=date.today(), customer=customer, assigned_user_id=user,
        )
        db.session.add(task)
        db.session.commit()
        return task.id


# --- 1./2. Direktantworten aus echten Daten, ohne KI-Aufruf -------------------------------


def test_simple_chat_message_is_answered(app, world, fake):
    client = login(app, "justin@example.com")
    reply = _send(client, _new(client), "Hallo, wer bist du?").reply
    assert reply["status"] == "done" and reply["content"] == "Antwort von gemini"


def test_open_tasks_answered_directly_from_real_data_without_ai(app, world, fake, db):
    _task(db, world.tenant_a.id, "Rückruf Herr Probe", priority=Priority.HIGH)
    _task(db, world.tenant_a.id, "Unterlagen anfordern")
    _task(db, world.tenant_a.id, "Längst erledigt", status=TaskStatus.DONE)
    _task(db, world.tenant_b.id, "Aufgabe aus Büro B")
    client = login(app, "admin-a@example.com")
    reply = _send(client, _new(client), "Welche Aufgaben sind noch offen?").reply
    assert reply["status"] == "done"
    assert fake.calls == []  # kein KI-Aufruf
    assert reply["model"] == "Direkt aus Zentriq" and reply["tools"] == ["Aufgaben"]
    assert reply["content"].startswith("**2 Aufgaben.**")
    assert "1. Rückruf Herr Probe" in reply["content"]  # hohe Prioritaet zuerst
    assert "Büro B" not in reply["content"] and "Längst erledigt" not in reply["content"]
    with bypass_tenant_scope():
        assert AiUsage.query.count() == 0


def test_day_overview_for_employee_uses_only_own_entries(app, world, fake):
    client = login(app, "dennis@example.com")
    reply = _send(client, _new(client), "Was ist heute noch offen?").reply
    assert fake.calls == []
    content = reply["content"]
    assert "Vorgänge** der Leipziger Liste offen" in content
    assert "DENNIS-OFFEN-1" in content
    assert "LAURA" not in content and "JUSTIN" not in content and "BOB" not in content


def test_work_times_and_presence_direct(app, world, fake):
    client = login(app, "dennis@example.com")
    reply = _send(client, _new(client), "Meine Arbeitszeiten heute").reply
    assert fake.calls == [] and "Std. netto" in reply["content"]
    # Team-Anwesenheit nur fuer Buero-Admins: beim Mitarbeiter keine Direktantwort -> Modell.
    _send(client, _new(client), "Wer ist heute da?")
    assert len(fake.calls) == 1
    admin = login(app, "admin-a@example.com")
    reply = _send(admin, _new(admin), "Wer ist heute da?").reply
    assert "Mitarbeitern sind anwesend" in reply["content"] and "Bob" not in reply["content"]


def test_direct_intent_only_for_clear_short_questions(app, world):
    with bypass_tenant_scope():
        admin = User.query.filter_by(email="admin-a@example.com").one()
    with use_tenant_id(admin.tenant_id):
        assert orchestrator.detect_intent(admin, "Welche Aufgaben sind offen?") == ("aufgaben", {"status": "offen"})
        assert orchestrator.detect_intent(admin, "Formuliere eine Mail zu den offenen Aufgaben") is None
        assert orchestrator.detect_intent(admin, "Warum sind so viele Aufgaben offen?") is None
        assert orchestrator.detect_intent(admin, "Arbeitszeiten diesen Monat") == ("meine_arbeitszeiten", {"zeitraum": "monat"})


def test_manual_model_choice_skips_direct_answer(app, world, fake, db):
    client = login(app, "admin-a@example.com")
    _send(client, _new(client, model="groq:llama-3.3-70b-versatile"), "Welche Aufgaben sind offen?")
    assert len(fake.calls) >= 1


# --- 3. Dokumentanalyse mit dem vorhandenen Parser ----------------------------------------


def _upload(client, conversation_id, filename, content, text="Analysiere diese Leipziger Liste.", mimetype="application/pdf"):
    resp = client.post(
        f"/zentriq/api/conversations/{conversation_id}/messages",
        data={"content": text, "files": (io.BytesIO(content), filename, mimetype)},
        content_type="multipart/form-data",
    )
    return Sent(client, conversation_id, resp)


def test_leipziger_list_upload_uses_existing_parser(app, world, fake):
    client = login(app, "admin-a@example.com")
    sent = _upload(client, _new(client), "liste.pdf", make_list_pdf([PAGE_1, PAGE_2]))
    assert sent.reply["status"] == "done"
    content = fake.calls[-1]["messages"][-1]["content"]
    assert "[Auswertung mit dem Zentriq-Parser für Leipziger Listen]" in content
    assert "Vorgänge: 10" in content and "ohne Beginn-Datum (offen): 4" in content and "Offen nach Status: ANG 4" in content
    # Ohne Kundendaten-Modus nur Zaehlungen - keine Namen verlassen den Server.
    assert "Anna Beispiel" not in content and "Berta Muster" not in content


def test_leipziger_list_details_only_in_customer_mode(app, world, fake, db):
    _approve(db, "groq")
    client = login(app, "admin-a@example.com")
    _upload(client, _new(client, customer_data=True), "liste.pdf", make_list_pdf([PAGE_1, PAGE_2]))
    content = fake.calls[-1]["messages"][-1]["content"]
    assert "720/307259-C-14 ANG PH – Anna Beispiel" in content
    assert fake.calls[-1]["provider"] == "groq"


# --- 4. Transkription zusammenfassen (pseudonymisiert) ------------------------------------


def test_voice_message_summary_pseudonymized_and_restored(app, world, fake, db):
    with use_tenant_id(world.tenant_a.id):
        customer = Customer(tenant_id=world.tenant_a.id, name="Herbert Musterkunde", phone="0171 9998887")
        db.session.add(customer)
        db.session.flush()
        db.session.add(
            CustomerMemo(
                tenant_id=world.tenant_a.id, customer_id=customer.id,
                transcript="Hallo, hier ist Herbert Musterkunde, bitte Rückruf unter 0171 9998887 wegen Kfz.",
                transcript_sha256="x" * 64, matched_by="phone",
            )
        )
        db.session.commit()
    fake.set(
        "gemini",
        adapters.ProviderResult("", 10, 5, tool_calls=[adapters.ToolCall("c1", "sprachnachrichten", {"anzahl": 1})]),
        adapters.ProviderResult("[Kunde 1] bittet um Rückruf unter [Telefon 1] (Kfz).", 20, 10),
    )
    client = login(app, "admin-a@example.com")
    reply = _send(client, _new(client), "Fasse die letzte Sprachnachricht zusammen.").reply
    tool_result = fake.calls[-1]["messages"][-1]["content"]
    assert "Musterkunde" not in tool_result and "9998887" not in tool_result
    assert "[Kunde 1]" in tool_result
    assert reply["content"] == "Herbert Musterkunde bittet um Rückruf unter 0171 9998887 (Kfz)."
    assert reply["tools"] == ["Sprachnachrichten"]


def test_pseudonymizer_roundtrip_and_tool_arguments():
    pseudo = Pseudonymizer()
    masked = pseudo.mask_data({"kunde": "Anna Beispiel", "anzahl": 3, "vorgaenge": [{"vertrag": "720/1-C", "kunde": "Anna Beispiel"}]})
    assert masked == {"kunde": "[Kunde 1]", "anzahl": 3, "vorgaenge": [{"vertrag": "[Vertrag 1]", "kunde": "[Kunde 1]"}]}
    assert pseudo.unmask({"suchbegriff": "[Kunde 1]"}) == {"suchbegriff": "Anna Beispiel"}
    assert pseudo.unmask("Unbekannt [Kunde 9] bleibt") == "Unbekannt [Kunde 9] bleibt"


# --- 5. Keine fremden Buerodaten ---------------------------------------------------------


def test_tools_never_return_other_office_data(app, world, fake, db):
    _task(db, world.tenant_a.id, "Geheime Aufgabe A")
    _approve(db, "groq")
    fake.set("groq", adapters.ProviderResult("", 1, 1, tool_calls=[adapters.ToolCall("c1", "aufgaben", {"status": "offen"})]), adapters.ProviderResult("ok", 1, 1))
    bob_admin = login(app, "admin-b@example.com")
    _send(bob_admin, _new(bob_admin), "Hilf mir bei der Planung der Aufgaben.")
    assert "Geheime Aufgabe A" not in fake.calls[-1]["messages"][-1]["content"]
    # Mitarbeiter: Werkzeug fuer Aufgaben ist gesperrt, auch wenn das Modell es anfordert.
    fake.set("groq", adapters.ProviderResult("", 1, 1, tool_calls=[adapters.ToolCall("c1", "aufgaben", {"status": "offen"})]), adapters.ProviderResult("ok", 1, 1))
    dennis = login(app, "dennis@example.com")
    _send(dennis, _new(dennis), "Hilf mir bei der Planung der Aufgaben.")
    assert "nicht verfügbar" in fake.calls[-1]["messages"][-1]["content"]


def test_member_activity_only_own_office(app, world, fake, db):
    from app.services.ai import tools

    with bypass_tenant_scope():
        admin_b = User.query.filter_by(email="admin-b@example.com").one()
    with use_tenant_id(admin_b.tenant_id):
        result = tools.run(admin_b, "mitarbeiter_aktivitaet", {"mitarbeiter": "dennis"})
    assert "Kein Mitarbeiter" in result["hinweis"]


# --- 6./7. Fallback; fehlgeschlagene Anfrage aendert keine Daten; Aktionen nach Bestaetigung ---


def test_failed_request_changes_no_data_and_action_runs_once(app, world, fake, db):
    task_id = _task(db, world.tenant_a.id, "Unterlagen anfordern")
    _approve(db, "groq")
    fake.set(
        "groq",
        adapters.ProviderResult("", 1, 1, tool_calls=[adapters.ToolCall("c1", "aufgabe_erledigen", {"aufgabe_id": task_id})]),
        adapters.ProviderError(adapters.UNAVAILABLE),
    )
    fake.set("gemini", adapters.ProviderError(adapters.UNAVAILABLE))
    admin = login(app, "admin-a@example.com")
    conversation_id = _new(admin)
    reply = _send(admin, conversation_id, "Bitte markiere die Unterlagen-Aufgabe als erledigt.").reply
    assert reply["status"] == "error"
    with use_tenant_id(world.tenant_a.id):
        assert db.session.get(Task, task_id).status == TaskStatus.OPEN  # nichts veraendert
        action = AiAction.query.one()
    assert action.status == "pending" and "Unterlagen anfordern" in action.summary

    # Andere Benutzer koennen die Aktion weder sehen noch ausfuehren.
    assert login(app, "dennis@example.com").post(f"/zentriq/api/actions/{action.id}/bestaetigen").status_code == 404
    first = admin.post(f"/zentriq/api/actions/{action.id}/bestaetigen").get_json()["action"]
    assert first["status"] == "done" and "erledigt" in first["result"]
    second = admin.post(f"/zentriq/api/actions/{action.id}/bestaetigen").get_json()["action"]
    assert second["status"] == "done"
    with use_tenant_id(world.tenant_a.id):
        task = db.session.get(Task, task_id)
        assert task.status == TaskStatus.DONE
    with bypass_tenant_scope():
        from app.models import AuditLog
        from app.models.audit_log import AuditEventType

        assert AuditLog.query.filter_by(event_type=AuditEventType.AI_ACTION_EXECUTED).count() == 1


def test_prepared_action_shown_with_answer_and_can_be_cancelled(app, world, fake, db):
    task_id = _task(db, world.tenant_a.id, "Rückruf vorbereiten")
    _approve(db, "groq")
    fake.set(
        "groq",
        adapters.ProviderResult("", 1, 1, tool_calls=[adapters.ToolCall("c1", "aufgabe_erledigen", {"aufgabe_id": task_id})]),
        adapters.ProviderResult("Die Aufgabe ist zum Erledigen vorbereitet und wartet auf Ihre Bestätigung.", 1, 1),
    )
    admin = login(app, "admin-a@example.com")
    conversation_id = _new(admin)
    reply = _send(admin, conversation_id, "Bitte markiere die Rückruf-Aufgabe als erledigt.").reply
    assert reply["actions"][0]["status"] == "pending"
    action_id = reply["actions"][0]["id"]
    assert admin.post(f"/zentriq/api/actions/{action_id}/abbrechen").get_json()["action"]["status"] == "cancelled"
    assert admin.post(f"/zentriq/api/actions/{action_id}/bestaetigen").get_json()["action"]["status"] == "cancelled"
    with use_tenant_id(world.tenant_a.id):
        assert db.session.get(Task, task_id).status == TaskStatus.OPEN
    data = admin.get(f"/zentriq/api/conversations/{conversation_id}").get_json()
    assert data["messages"][1]["actions"][0]["status"] == "cancelled"


# --- 8. Nicht blockierend; Doppelversand -------------------------------------------------


def test_request_returns_immediately_with_progress(app, world, fake, monkeypatch):
    from app.tasks import ai_tasks

    queued = []
    monkeypatch.setattr(ai_tasks.answer_message, "delay", lambda *args: queued.append(args))
    client = login(app, "justin@example.com")
    conversation_id = _new(client)
    resp = client.post(f"/zentriq/api/conversations/{conversation_id}/messages", json={"content": "Hallo"})
    assert resp.status_code == 202
    reply = resp.get_json()["reply"]
    assert reply["status"] == "pending" and reply["progress"] == "Analysiere Anfrage"
    assert len(queued) == 1 and fake.calls == []
    # Waehrend der Verarbeitung bleibt die Oberflaeche bedienbar (andere Endpunkte antworten).
    assert client.get("/zentriq/api/conversations").status_code == 200


def test_repeated_send_is_processed_once(app, world, fake):
    client = login(app, "justin@example.com")
    conversation_id = _new(client)
    body = {"content": "Hallo", "client_ref": "abc-123"}
    first = client.post(f"/zentriq/api/conversations/{conversation_id}/messages", json=body).get_json()
    second = client.post(f"/zentriq/api/conversations/{conversation_id}/messages", json=body).get_json()
    assert first["message"]["id"] == second["message"]["id"]
    assert first["reply"]["id"] == second["reply"]["id"]
    assert len(fake.calls) == 1
    with bypass_tenant_scope():
        assert AiMessage.query.filter_by(conversation_id=conversation_id).count() == 2


# --- 10. Keine kostenpflichtigen Aufrufe ohne Freigabe -----------------------------------


def test_no_paid_calls_by_default(app, world, fake, db):
    from app.services.ai import catalog

    app.config["ANTHROPIC_API_KEY"] = "sk-ant-test"
    client = login(app, "justin@example.com")
    catalog.ensure_defaults()
    for model in AiModel.query.filter_by(provider="gemini").all():
        model.enabled = False
    for model in AiModel.query.filter(AiModel.provider.in_(["groq", "mistral"])).all():
        model.enabled = False
    db.session.commit()
    reply = _send(client, _new(client), "Hallo").reply
    assert reply["status"] == "error"
    assert fake.calls == []  # Claude-Modelle sind ab Werk aus, Budget 0


def test_super_admin_gets_no_office_tools_or_direct_office_answers(app, world, fake):
    client = login(app, "justin@example.com")
    _send(client, _new(client), "Welche Aufgaben sind offen?")
    assert len(fake.calls) == 1 and fake.calls[0]["tools"] is None


@pytest.mark.parametrize("question", ["Zeige meine Arbeitszeiten dieser Woche", "Was ist heute noch offen?"])
def test_direct_answers_work_without_any_ai_provider(app, world, question):
    client = login(app, "dennis@example.com")
    reply = _send(client, _new(client), question).reply
    assert reply["status"] == "done" and reply["model"] == "Direkt aus Zentriq"
    assert json.dumps(reply)  # serialisierbar
