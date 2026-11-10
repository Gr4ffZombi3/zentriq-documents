"""ZentriqAI - zentraler KI-Assistent und Startseite fuer alle Rollen.

Zugriff: jeder angemeldete Benutzer (Rollen-Hook in app/auth/permissions.py), Gespraeche sind je
Benutzer und Mandant getrennt (app/services/ai/chat.py). Werkzeuge mit Buerodaten pruefen die
bestehenden Rechte je Benutzer (app/services/ai/tools.py). KI-Einstellungen liegen getrennt im
Blueprint "zentriq_admin" (nur SUPER_ADMIN).

KI-Antworten entstehen asynchron im Celery-Worker: POST .../messages legt die Nachricht und einen
Platzhalter an (202), der Browser fragt GET .../messages/<id> ab."""

import json
import os
from decimal import Decimal

from flask import Blueprint, current_app, g, jsonify, render_template, request, url_for
from flask_login import current_user, login_required

from app.extensions import db
from app.models import Customer, CustomerContact
from app.models.ai import AiModel
from app.models.audit_log import AuditEventType
from app.services import (
    calendar_intent,
    chat_followup,
    chat_task,
    customer_create,
    customer_notes,
    email_context,
    email_reply,
    stock_questions,
)
from app.services.ai import actions, catalog, chat, router, state
from app.services.audit import log_audit_event
from app.tenancy import get_or_404_scoped

zentriq_bp = Blueprint("zentriq", __name__, url_prefix="/zentriq")


def _payload() -> dict:
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else {}


def _error(message: str, status: int, code: str, **extra):
    return jsonify({"error": message, "code": code, **extra}), status


@zentriq_bp.before_request
def _enabled():
    if not current_app.config.get("ZENTRIQ_AI_ENABLED", True):
        if request.path.startswith("/zentriq/api/"):
            return _error("ZentriqAI ist derzeit abgeschaltet.", 503, "disabled")
        return render_template("errors/error.html", title="ZentriqAI", message="ZentriqAI ist derzeit abgeschaltet."), 503
    return None


def _labels() -> dict:
    return {(m.provider, m.model_id): m.label for m in AiModel.query.all()}


def _model_options() -> list[dict]:
    settings = catalog.provider_settings()
    options = []
    for model in router.available_models():
        setting = settings.get(model.provider)
        options.append(
            {
                "ref": model.ref,
                "label": model.label,
                "provider": catalog.PROVIDERS[model.provider].label,
                "free": model.is_free,
                "customer_data": bool(setting and setting.customer_data_allowed),
                "paused": bool(state.cooldown_info(model.provider, model.model_id)),
            }
        )
    return options


# --- Seite ---------------------------------------------------------------------------------


def _greeting() -> str:
    """Begruessung mit Vornamen aus dem Mitarbeiterprofil - nie aus der E-Mail-Adresse."""
    profile = None if current_user.is_super_admin else current_user.employee_profile
    name = (profile.display_name or "").strip() if profile is not None else ""
    first = name.split()[0] if name and "@" not in name else ""
    return f"Womit kann ich helfen, {first}?" if first else "Womit kann ich helfen?"


@zentriq_bp.get("")
@zentriq_bp.get("/c/<int:conversation_id>")
@login_required
def index(conversation_id=None):
    return render_template(
        "zentriq/index.html",
        appt_pattern=__import__("app.services.image_appointments", fromlist=["PATTERN"]).PATTERN,
        conversation_id=conversation_id,
        greeting=_greeting(),
        max_chars=int(current_app.config.get("ZENTRIQ_AI_MAX_INPUT_CHARS") or 12000),
        customer_notes_enabled=bool(current_user.is_office_admin or current_user.is_employee),
    )


# --- JSON-API ------------------------------------------------------------------------------


@zentriq_bp.get("/api/models")
@login_required
def api_models():
    return jsonify({"models": _model_options()})


@zentriq_bp.get("/api/conversations")
@login_required
def api_conversations():
    try:
        limit = max(1, min(100, int(request.args.get("limit") or 100)))
    except ValueError:
        limit = 100
    pinned = {"1": True, "0": False}.get(request.args.get("pinned") or "")
    items = chat.list_conversations(current_user, request.args.get("q"), limit=limit, pinned=pinned)
    return jsonify({"conversations": [chat.serialize_conversation(c) for c in items]})


@zentriq_bp.get("/api/projects")
@login_required
def api_projects():
    return jsonify({"projects": chat.projects(current_user)})


@zentriq_bp.post("/api/conversations")
@login_required
def api_create_conversation():
    data = _payload()
    conversation = chat.create_conversation(
        current_user,
        model_choice=str(data.get("model") or router.AUTO),
        customer_data=bool(data.get("customer_data")),
        project=data.get("project"),
    )
    return jsonify({"conversation": chat.serialize_conversation(conversation)}), 201


@zentriq_bp.get("/api/conversations/<int:conversation_id>")
@login_required
def api_conversation(conversation_id):
    try:
        conversation = chat.get_conversation(current_user, conversation_id)
    except chat.ChatError as exc:
        return _error(str(exc), exc.status, exc.error_type)
    labels = _labels()
    messages = conversation.messages
    by_message = chat.actions_for(messages)
    return jsonify(
        {
            "conversation": chat.serialize_conversation(conversation),
            "messages": [chat.serialize_message(m, labels, by_message.get(m.id)) for m in messages],
        }
    )


@zentriq_bp.patch("/api/conversations/<int:conversation_id>")
@login_required
def api_update_conversation(conversation_id):
    data = _payload()
    try:
        conversation = chat.get_conversation(current_user, conversation_id)
        chat.update_conversation(
            conversation,
            title=data.get("title") if "title" in data else None,
            model_choice=data.get("model") if "model" in data else None,
            customer_data=data.get("customer_data") if "customer_data" in data else None,
            pinned=data.get("pinned") if "pinned" in data else None,
            project=(data.get("project") or "") if "project" in data else None,
        )
    except chat.ChatError as exc:
        return _error(str(exc), exc.status, exc.error_type)
    return jsonify({"conversation": chat.serialize_conversation(conversation)})


@zentriq_bp.delete("/api/conversations/<int:conversation_id>")
@login_required
def api_delete_conversation(conversation_id):
    try:
        conversation = chat.get_conversation(current_user, conversation_id)
    except chat.ChatError as exc:
        return _error(str(exc), exc.status, exc.error_type)
    chat.delete_conversation(conversation)
    log_audit_event(AuditEventType.AI_CONVERSATION_DELETED, user=current_user, details={"conversation_id": conversation_id})
    return jsonify({"ok": True})


@zentriq_bp.delete("/api/conversations")
@login_required
def api_delete_all():
    count = chat.delete_all(current_user)
    log_audit_event(AuditEventType.AI_CONVERSATION_DELETED, user=current_user, details={"all": True, "count": count})
    return jsonify({"ok": True, "deleted": count})


@zentriq_bp.post("/api/conversations/<int:conversation_id>/messages")
@login_required
def api_send(conversation_id):
    if request.mimetype == "multipart/form-data":
        content = request.form.get("content") or ""
        model = request.form.get("model")
        client_ref = request.form.get("client_ref")
        files = request.files.getlist("files")
    else:
        data = _payload()
        content = data.get("content") if isinstance(data.get("content"), str) else ""
        model = data.get("model") if isinstance(data.get("model"), str) else None
        client_ref = data.get("client_ref") if isinstance(data.get("client_ref"), str) else None
        files = []
    raw_customer = (request.form.get("customer_id") if request.mimetype == "multipart/form-data" else _payload().get("customer_id"))
    context_customer_id = int(raw_customer) if str(raw_customer or "").isdigit() else None
    if context_customer_id is not None and Customer.query.filter(Customer.id == context_customer_id).first() is None:
        context_customer_id = None  # fremdes Buero / geloescht: wird ignoriert
    try:
        conversation = chat.get_conversation(current_user, conversation_id)
        user_message, pending = chat.submit_message(current_user, conversation, content, model, files, client_ref,
                                                    context_customer_id=context_customer_id)
    except chat.ChatError as exc:
        return _error(str(exc), exc.status, exc.error_type)
    labels = _labels()
    return (
        jsonify(
            {
                "message": chat.serialize_message(user_message, labels),
                "reply": chat.serialize_message(pending, labels),
                "conversation": chat.serialize_conversation(conversation),
            }
        ),
        202,
    )


@zentriq_bp.get("/api/conversations/<int:conversation_id>/messages/<int:message_id>")
@login_required
def api_message(conversation_id, message_id):
    try:
        conversation = chat.get_conversation(current_user, conversation_id)
        message = chat.get_message(current_user, conversation, message_id)
    except chat.ChatError as exc:
        return _error(str(exc), exc.status, exc.error_type)
    message_actions = chat.actions_for([message]).get(message.id)
    return jsonify(
        {"message": chat.serialize_message(message, _labels(), message_actions), "conversation": chat.serialize_conversation(conversation)}
    )


@zentriq_bp.post("/api/actions/<int:action_id>/<decision>")
@login_required
def api_action(action_id, decision):
    """Vorbereitete Aktion bestaetigen oder abbrechen - nur durch den Benutzer, der sie angefordert hat."""
    if decision not in ("bestaetigen", "abbrechen"):
        return _error("Unbekannte Entscheidung.", 400, "invalid")
    try:
        action = actions.confirm(current_user, action_id) if decision == "bestaetigen" else actions.cancel(current_user, action_id)
    except actions.ActionError as exc:
        return _error(str(exc), exc.status, "action")
    return jsonify({"action": actions.serialize(action)})


@zentriq_bp.get("/api/usage")
@login_required
def api_usage():
    data = {"own": router.usage_overview(30, user_id=current_user.id)}
    if current_user.is_super_admin:
        budget = router.budget_status()
        data["budget"] = {key: float(value) if isinstance(value, Decimal) else value for key, value in budget.items()}
    return jsonify(data)


# --- Kundennotizen und offene Rueckrufe -------------------------------------------------------
# Gemeinsame Notizen des Bueros: Buero-Admins und Mitarbeiter desselben Bueros lesen und
# ergaenzen sie (nur diese Endpunkte, keine weiteren Kundenmodule). Der SUPER_ADMIN hat wie
# ueberall keinen Zugriff auf Buerodaten. Kunden und Vorgaenge werden ausschliesslich im
# Mandanten des Benutzers gesucht (globaler Tenant-Filter + get_or_404_scoped).


def _office_user_required():
    if not (current_user.is_office_admin or current_user.is_employee):
        return _error("Kundennotizen stehen nur Büro-Konten zur Verfügung.", 403, "forbidden")
    return None


def _client_ref(data: dict) -> str | None:
    value = data.get("client_ref")
    return value.strip()[:64] or None if isinstance(value, str) else None


@zentriq_bp.get("/api/vorschau")
@login_required
def api_live_preview():
    """Live-Vorschau beim Tippen (app/services/live_input.py): liest nur, speichert nie."""
    if (denied := _office_user_required()) is not None:
        return denied
    from app.services import live_input

    args = request.args
    overrides = {key: args.get(key) for key in ("customer_id", "assignee_id", "start", "minutes") if args.get(key)}
    if args.get("no_customer") == "1":
        overrides["no_customer"] = True
    try:
        result = live_input.preview(current_user, (args.get("text") or "")[:300], overrides, want_slots=args.get("slots") == "1")
    except Exception:  # noqa: BLE001 - Vorschau ist Komfort: im Fehlerfall keine Vorschau, Absenden bleibt
        current_app.logger.exception("live_input.preview_failed user=%s", current_user.id)
        db.session.rollback()
        return jsonify({"kind": None})
    return jsonify({"seq": args.get("seq"), **result})


@zentriq_bp.post("/api/kunden/eingabe")
@login_required
def api_customer_input():
    if (denied := _office_user_required()) is not None:
        return denied
    data = _payload()
    text = data.get("text") if isinstance(data.get("text"), str) else ""
    mode = data.get("mode") if data.get("mode") in ("note", "search", "create") else None
    # "Trage die Termine aus dem Bild ein" ohne neuen Anhang: Bild der vorherigen Chat-Nachricht
    # auswerten (api_appointments_from_chat) - vor allen anderen Weichen und vor dem KI-Router.
    from app.services import image_appointments

    if mode is None and image_appointments.is_chat_request(text):
        return jsonify({"kind": "appointments_from_chat"})
    # Offener Auftrag des Chats ("Fuer welchen Kunden?" -> nur der Name) bzw. neuer Auftrag
    # "Termin bestaetigen": im Backend, ohne Modell (app/services/chat_task.py). Ohne offenen
    # Auftrag bleibt alles wie bisher (ein allein eingegebener Name ist die direkte Kundensuche).
    if mode is None and not data.get("customer_id"):
        task_result, task_ended = _chat_task(data, text)
        if task_result is not None:
            return task_result
        g.task_ended = task_ended
    # "Schreibe eine Antwort, um einen Termin zu machen": Auftrag zu einer E-Mail im Gespraech ->
    # Chat (dort app/services/email_reply.py, funktioniert auch ohne KI-Modell). Kein Termin,
    # keine Notiz.
    if mode is None and not data.get("customer_id") and email_reply.is_reply_instruction(text) and not customer_create.is_command(text):
        # Ausgewaehlter bzw. im Auftrag eindeutig genannter Kunde geht mit (Entwurf mit freien Terminen).
        customer = None
        if str(data.get("selected_id") or "").isdigit():
            customer = Customer.query.filter(Customer.id == int(data["selected_id"])).first()
        customer = customer or email_reply.customer_from_instruction(text)
        return jsonify({"kind": "chat", **({"customer_id": customer.id, "customer_name": customer.name} if customer else {})})
    # Eingefuegte E-Mail: gehoert ins Gespraech (Kontext fuer "Antwort schreiben"), ist keine
    # Kurznotiz - auch wenn sie Nummern oder Namen enthaelt.
    if mode is None and not data.get("customer_id") and email_context.looks_like_email(text):
        return jsonify({"kind": "chat"})
    # Kurzer Buchungsauftrag ("09.11.26 Schmidthofer Termin um 16.30") und reine Verfuegbarkeits-
    # fragen ("Ist am 09.11. um 16:30 noch frei?") gehen direkt an den Kalender - lokal erkannt,
    # ohne KI-Modell und vor allen anderen Weichen.
    selected = data.get("selected_id") not in (None, "")
    direct_calendar = mode is None and not data.get("customer_id") and (
        calendar_intent.is_availability_question(text)
        or (calendar_intent.is_booking_command(text) and calendar_intent.is_calendar_request(text, customer_selected=selected))
    )
    # Bestandsfragen ("Wie viele Kunden sind laut Leipziger Liste noch zurueckzurufen?"): direkt
    # aus der Datenbank, unabhaengig von einem KI-Modell (app/services/stock_questions.py).
    if mode is None and not data.get("customer_id") and not direct_calendar:
        facts = stock_questions.answer(current_user, text, selected_id=data.get("selected_id"))
        if facts is not None:
            return jsonify(facts)
    # Fachfrage mit Internetbedarf -> Chat (Webrecherche, funktioniert auch ohne KI-Modell).
    if mode is None and not data.get("customer_id") and not data.get("selected_id") and not direct_calendar:
        from app.services import web_research

        if web_research.is_question(text) and not customer_notes._by_name(text):
            return jsonify({"kind": "chat"})
    # Termine ("Termin mit ... Donnerstag um 14 Uhr bei Laura") gehen an den Kalender.
    if isinstance(data.get("calendar"), dict) or direct_calendar or (mode is None and calendar_intent.is_calendar_request(text, customer_selected=selected)):
        try:
            return jsonify(
                calendar_intent.handle(
                    current_user, text, choice=data.get("calendar"), client_ref=_client_ref(data),
                    selected_customer_id=data.get("selected_id"),
                )
            )
        except Exception:  # noqa: BLE001
            current_app.logger.exception("calendar_intent.failed user=%s", current_user.id)
            db.session.rollback()
            return _error("Technischer Fehler beim Termin. Er wurde nicht gespeichert – bitte erneut versuchen.", 500, "technical")
    try:
        result = customer_notes.handle_input(
            current_user,
            text,
            selected_id=data.get("selected_id"),
            customer_id=data.get("customer_id"),
            client_ref=_client_ref(data),
            mode=mode,
            contact_choice=data.get("contact_id"),
            new_name=data.get("new_name") if isinstance(data.get("new_name"), str) else None,
            new_vn=data.get("new_vn") if isinstance(data.get("new_vn"), str) else None,
            create_new=data.get("create_new") is True,
            first_name=data.get("first_name") if isinstance(data.get("first_name"), str) else None,
            last_name=data.get("last_name") if isinstance(data.get("last_name"), str) else None,
            phone_kinds=data.get("phone_kinds") if isinstance(data.get("phone_kinds"), list) else None,
        )
        if result.get("kind") == "chat" and not router.can_answer():
            # Ohne KI-Modell nicht an den Chat weiterreichen (der koennte nur einen Fehler melden) -
            # ausser die Frage ist eine Standardabfrage, die der Chat direkt aus Zentriq beantwortet
            # (orchestrator.direct_answer, ohne Modell).
            from app.services.ai import orchestrator

            if orchestrator.detect_intent(current_user, text) is None:
                result = customer_notes.without_ai(current_user, text)
    except customer_notes.NoteError as exc:
        return _error(str(exc), exc.status, "note")
    except Exception:  # noqa: BLE001 - technischer Fehler wird als solcher gemeldet, nie als "kein Treffer".
        current_app.logger.exception("customer_notes.input_failed user=%s", current_user.id)
        db.session.rollback()
        return _error(
            "Technischer Fehler bei der Suche bzw. Speicherung. Bitte erneut versuchen – das Ergebnis ist nicht bestätigt.",
            500,
            "technical",
        )
    return jsonify(result)


@zentriq_bp.after_request
def _mark_task_ended(response):
    """Andere Eingabe hat den offenen Auftrag beendet: der Browser blendet die Wartezeile aus."""
    if g.get("task_ended") and response.is_json:
        body = response.get_json(silent=True)
        if isinstance(body, dict):
            body["task_ended"] = True
            response.set_data(json.dumps(body))
    return response


def _task_conversation(data: dict):
    raw = data.get("conversation_id")
    if not str(raw or "").isdigit():
        return None
    try:
        return chat.get_conversation(current_user, int(raw))
    except chat.ChatError:
        return None


def _chat_task(data: dict, text: str):
    """(JSON-Antwort | None, Auftrag beendet). Ein Chat wird nur angelegt, wenn ein neuer Auftrag
    eine Rueckfrage bzw. einen Entwurf erzeugt - die normale Suche legt keinen an."""
    conversation = _task_conversation(data)
    choice = data.get("task_choice") if isinstance(data.get("task_choice"), dict) else None
    if conversation is None and (choice is not None or not chat_task.is_confirm_request(text)):
        if choice is not None:
            return _error("Diese Auswahl ist nicht mehr aktuell.", 409, "task"), False
        kind = chat_followup.detect(text)
        return (_followup(data, text, kind, None), False) if kind else (None, False)
    if conversation is None:
        conversation = chat.create_conversation(current_user)
    try:
        reply, ended = chat_task.dispatch(current_user, conversation, text, selected_id=data.get("selected_id"), choice=choice)
    except chat_task.TaskError as exc:
        db.session.rollback()
        return _error(str(exc), 409, "task"), False
    except Exception:  # noqa: BLE001
        current_app.logger.exception("chat_task.failed user=%s", current_user.id)
        db.session.rollback()
        return _error("Technischer Fehler beim offenen Auftrag. Es wurde nichts erstellt – bitte erneut versuchen.", 500, "technical"), False
    if reply is None:
        # Kurze Folgeeingabe ("Kuerzer", "Doch um 16:30", "Die Strasse ist falsch") - vor Notiz/Suche.
        kind = chat_followup.detect(text)
        if kind is None or choice is not None:
            return None, ended
        return _followup(data, text, kind, conversation), ended
    shown = (data.get("choice_label") if choice is not None and isinstance(data.get("choice_label"), str) else None) or text
    return _task_response(conversation, shown, reply), ended


def _task_response(conversation, shown: str, reply, tool: str = "Terminbestätigung"):
    _asked, answer = chat.add_direct_turn(conversation, shown, chat_task.render(reply), tool)
    body = {
        "kind": "task",
        "message": reply.text,
        "status": reply.status,
        "options": reply.options,
        "draft": reply.draft,
        "used_model": reply.used_model,
        "assistant": chat.serialize_message(answer, _labels()),
        "task": chat_task.public(conversation),
        "conversation": chat.serialize_conversation(conversation),
    }
    if reply.edit:
        customer = Customer.query.filter(Customer.id == reply.edit["customer_id"]).first()
        if customer is not None:
            body["edit"] = {"field": reply.edit["field"], "card": customer_notes.customer_card(current_user, customer)}
    return jsonify(body)


def _followup(data: dict, text: str, kind: str, conversation):
    form_open = data.get("form_open") is True
    if kind == "time" and form_open:
        return jsonify(chat_followup.handle(current_user, None, text, kind, form_open=True))
    if conversation is None:
        conversation = chat.create_conversation(current_user)
    try:
        reply = chat_followup.handle(current_user, conversation, text, kind, selected_id=data.get("selected_id"),
                                     last_event_id=data.get("last_event_id"))
    except Exception:  # noqa: BLE001
        current_app.logger.exception("chat_followup.failed user=%s", current_user.id)
        db.session.rollback()
        return _error("Technischer Fehler. Es wurde nichts geändert – bitte erneut versuchen.", 500, "technical")
    return _task_response(conversation, text, reply, {"shorter": "Terminbestätigung", "time": "Kalender", "field": "Kundendaten"}[kind])


@zentriq_bp.post("/api/kunden/<int:customer_id>/termin")
@login_required
def api_customer_book(customer_id):
    """"Termin buchen" auf der Kundenkarte: vorhandene Buchung (Sperre je Mitarbeiter, Verfuegbarkeit,
    Doppeltipp-Schutz ueber client_ref). Belegt -> Grund + freie Alternativen, nichts gespeichert."""
    from datetime import date as _date
    from datetime import time as _time

    from app.services import booking
    from app.services import calendar as cal
    from app.services.timetracking.clock import local_to_utc_naive

    if (denied := _office_user_required()) is not None:
        return denied
    if not chat_task.calendar_allowed(current_user):
        return _error("Kein Zugriff auf den Bürokalender.", 403, "forbidden")
    customer = get_or_404_scoped(Customer, customer_id)
    data = _payload()
    try:
        day = _date.fromisoformat(str(data.get("day") or ""))
        hh, mm = (int(x) for x in str(data.get("time") or "").split(":")[:2])
        start_utc = local_to_utc_naive(day, _time(hh, mm))
        minutes = int(data.get("minutes") or 30)
        if not 5 <= minutes <= 480:
            raise ValueError
    except (TypeError, ValueError):
        return _error("Bitte Datum, Uhrzeit und Dauer angeben.", 400, "invalid")
    members = {u.id: u for u in cal.office_members()}
    assignee = members.get(int(data["assignee_id"])) if str(data.get("assignee_id") or "").isdigit() else members.get(current_user.id)
    if assignee is None:
        return _error("Bitte eine zuständige Person auswählen.", 400, "invalid")
    kind = data.get("location_kind") if data.get("location_kind") in cal.LOCATION_KINDS else ""
    result = booking.book(current_user, assignee=assignee, start_utc=start_utc, minutes=minutes, customer=customer,
                          title=f"Termin {customer.name}", location_kind=kind, client_ref=_client_ref(data))
    if result.event is None:
        return jsonify({"error": result.reason or "Der Termin konnte nicht gebucht werden.", "code": "busy",
                        "alternatives": [{"label": a.label(), "day": cal.local_iso(a.start_utc)[:10], "time": cal.local_iso(a.start_utc)[11:16]}
                                         for a in result.alternatives]}), 409
    conversation = _task_conversation(data)
    chat_task.remember(conversation, "event", {"event_id": result.event.id})
    label = chat_task._when_label(cal.to_local(result.event.starts_at), cal.to_local(result.event.ends_at))
    return jsonify({
        "message": ("Termin gebucht: " if result.created else "Termin war bereits gebucht: ") + f"{customer.name}, {label}, bei {cal.member_name(assignee)}.",
        "event": cal.serialize(result.event, current_user), "created": result.created,
        "calendar_url": url_for("calendar.index", tag=cal.local_iso(result.event.starts_at)[:10]),
        "card": customer_notes.customer_card(current_user, customer),
    }), (201 if result.created else 200)


@zentriq_bp.post("/api/kunden/<int:customer_id>/bestaetigung")
@login_required
def api_customer_confirmation(customer_id):
    """"Bestaetigung kopieren": Entwurf aus der Vorlage (0 Modellaufrufe), im Chat festgehalten -
    kein Versand, keine Kalenderaenderung."""
    if (denied := _office_user_required()) is not None:
        return denied
    customer = get_or_404_scoped(Customer, customer_id)
    data = _payload()
    events = chat_task.upcoming_events(customer) if chat_task.calendar_allowed(current_user) else []
    event = next((e for e in events if str(e.id) == str(data.get("event_id"))), None)
    if event is None:
        return _error("Dieser Termin ist nicht (mehr) als bevorstehender Termin des Kunden eingetragen.", 404, "not_found")
    conversation = _task_conversation(data) or chat.create_conversation(current_user)
    chat_task.clear(conversation)
    reply = chat_task.compose(current_user, conversation, customer, "", event=event)
    shown = f"Bestätigung für {customer.name}: {chat_task._event_option(event)['label']}"
    return _task_response(conversation, shown, reply)


@zentriq_bp.post("/api/conversations/<int:conversation_id>/auftrag/abbrechen")
@login_required
def api_task_cancel(conversation_id):
    try:
        conversation = chat.get_conversation(current_user, conversation_id)
    except chat.ChatError as exc:
        return _error(str(exc), exc.status, exc.error_type)
    had = chat_task.clear(conversation)
    if had:
        chat.add_assistant_note(conversation, "Offener Auftrag abgebrochen – es wurde nichts erstellt.", "Terminbestätigung")
    return jsonify({"ok": True, "cancelled": had, "conversation": chat.serialize_conversation(conversation)})


@zentriq_bp.patch("/api/kunden/<int:customer_id>")
@login_required
def api_customer_update(customer_id):
    """Stammdaten ueber die Kundenkarte bearbeiten (eigenes Buero; Rechte: customer_create.can_edit)."""
    if (denied := _office_user_required()) is not None:
        return denied
    customer = get_or_404_scoped(Customer, customer_id)
    try:
        customer_create.update_fields(current_user, customer, _payload())
    except customer_create.CreateError as exc:
        db.session.rollback()
        return _error(str(exc), exc.status, "customer")
    return jsonify({"card": customer_notes.customer_card(current_user, customer), "message": "Gespeichert."})


@zentriq_bp.get("/api/kunden/<int:customer_id>")
@login_required
def api_customer_card(customer_id):
    if (denied := _office_user_required()) is not None:
        return denied
    customer = get_or_404_scoped(Customer, customer_id)
    return jsonify({"card": customer_notes.customer_card(current_user, customer)})


@zentriq_bp.post("/api/kunden/<int:customer_id>/nicht-erreicht")
@login_required
def api_customer_not_reached(customer_id):
    if (denied := _office_user_required()) is not None:
        return denied
    customer = get_or_404_scoped(Customer, customer_id)
    data = _payload()
    try:
        result = customer_notes.mark_not_reached(
            current_user, customer, contact_id=data.get("contact_id"), note_id=data.get("note_id"), client_ref=_client_ref(data)
        )
    except customer_notes.NoteError as exc:
        return _error(str(exc), exc.status, "note")
    return jsonify(result)


@zentriq_bp.post("/api/rueckrufe/<int:contact_id>/erledigt")
@login_required
def api_callback_done(contact_id):
    if (denied := _office_user_required()) is not None:
        return denied
    contact = get_or_404_scoped(CustomerContact, contact_id)
    return jsonify(customer_notes.close_contact(current_user, contact, client_ref=_client_ref(_payload())))


@zentriq_bp.post("/api/rueckrufe/<int:contact_id>/wieder-oeffnen")
@login_required
def api_callback_reopen(contact_id):
    if (denied := _office_user_required()) is not None:
        return denied
    contact = get_or_404_scoped(CustomerContact, contact_id)
    return jsonify(customer_notes.reopen_contact(current_user, contact, client_ref=_client_ref(_payload())))


@zentriq_bp.post("/api/termine/aus-bildern")
@login_required
def api_appointments_from_images():
    """Bilder (Fotos/Screenshots) lesen und erkannte Termine zur Pruefung liefern - nichts wird
    gespeichert. Gleiche Berechtigung wie der Buerokalender."""
    from app.services import image_appointments
    from app.services.email_reply import _calendar_allowed
    from app.services.timetracking.clock import local_today

    if (denied := _office_user_required()) is not None:
        return denied
    if not _calendar_allowed(current_user):
        return _error("Kein Zugriff auf den Bürokalender.", 403, "forbidden")
    uploads = [(os.path.basename(f.filename or "bild"), f.read()) for f in request.files.getlist("files") if f and f.filename]
    if not uploads:
        return _error("Bild fehlt: Bitte ein Foto oder einen Screenshot anhängen.", 400, "no_image")
    if len(uploads) > image_appointments.MAX_IMAGES:
        return _error(f"Höchstens {image_appointments.MAX_IMAGES} Bilder auf einmal.", 400, "too_many")
    conversation = None
    if str(request.form.get("conversation_id") or "").isdigit():
        try:
            conversation = chat.get_conversation(current_user, int(request.form["conversation_id"]))
        except chat.ChatError as exc:
            return _error(str(exc), exc.status, exc.error_type)
    texts = []
    try:
        for name, data in uploads:
            texts.append((name, len(data), image_appointments.read_text(image_appointments.load_image(name, data))))
    except image_appointments.ImageError as exc:
        return _error(f"Upload fehlgeschlagen: {exc}", 400, "upload_failed")
    if conversation is not None:
        # Bild im Chat behalten - ein spaeterer Befehl ("Trage die Termine aus dem Bild ein") findet es.
        chat.store_image_message(current_user, conversation, request.form.get("text") or "Termine aus Bild", texts)
    result = image_appointments.analyse_texts(current_user, [(n, t) for n, _s, t in texts], local_today())
    if conversation is not None:
        chat.add_assistant_note(conversation, _appointments_note(result))
    return jsonify(result)


def _appointments_note(result: dict) -> str:
    items = result.get("items") or []
    if not items:
        return "Keine Termine übernommen. " + " ".join(result.get("problems") or [])
    from app.services import image_appointments

    lines = image_appointments.summary_lines(items)
    return f"{len(items)} Termin(e) aus dem Bild erkannt (Vorschau zum Prüfen und Eintragen):\n" + "\n".join(lines)


@zentriq_bp.post("/api/termine/aus-chat")
@login_required
def api_appointments_from_chat():
    """Befehl ohne Anhang: Bilder der direkt vorangehenden Nachricht dieses Chats (nur eigene
    Gespraeche) auswerten. Mehrere Bilder -> Rueckfrage; ohne Bild -> "Bild fehlt"."""
    from app.services import image_appointments
    from app.services.email_reply import _calendar_allowed
    from app.services.timetracking.clock import local_today

    if (denied := _office_user_required()) is not None:
        return denied
    if not _calendar_allowed(current_user):
        return _error("Kein Zugriff auf den Bürokalender.", 403, "forbidden")
    data = _payload()
    if not str(data.get("conversation_id") or "").isdigit():
        return _error("Bild fehlt: In diesem Chat wurde noch kein Bild hochgeladen. Bitte über „+ › Fotos und Screenshots“ anhängen.", 400, "no_image")
    try:
        conversation = chat.get_conversation(current_user, int(data["conversation_id"]))
    except chat.ChatError as exc:
        return _error(str(exc), exc.status, exc.error_type)
    images, older = chat.latest_image_attachments(conversation)
    if not images:
        hint = " Ältere Bilder in diesem Chat bitte erneut anhängen." if older else ""
        return _error("Bild fehlt: In der vorherigen Nachricht ist kein Bild. Bitte das Bild anhängen und den Befehl erneut senden." + hint, 400, "no_image")
    wanted = {int(x) for x in data.get("attachment_ids") or [] if str(x).isdigit()}
    if len(images) > 1 and not wanted:
        return jsonify({"kind": "choose_image", "message": "Mehrere Bilder kommen infrage – welches soll ausgewertet werden?",
                        "options": [{"id": a.id, "name": a.filename} for a in images]})
    chosen = [a for a in images if not wanted or a.id in wanted]
    if not chosen:
        return _error("Das gewählte Bild gehört nicht zur vorherigen Nachricht.", 400, "no_image")
    result = image_appointments.analyse_texts(current_user, [(a.filename, a.text or "") for a in chosen], local_today())
    chat.add_assistant_note(conversation, _appointments_note(result))
    return jsonify(result)


@zentriq_bp.post("/api/termine/uebernehmen")
@login_required
def api_appointments_save():
    """Ausgewaehlte Termine aus der Vorschau speichern (je Termin erneute Pruefung)."""
    from app.services import image_appointments
    from app.services.email_reply import _calendar_allowed

    if (denied := _office_user_required()) is not None:
        return denied
    if not _calendar_allowed(current_user):
        return _error("Kein Zugriff auf den Bürokalender.", 403, "forbidden")
    data = _payload()
    items = data.get("items") if isinstance(data.get("items"), list) else []
    if not items:
        return _error("Keine Termine ausgewählt.", 400, "empty")
    batch = str(data.get("batch") or "")[:40] or "bild"
    return jsonify({"results": image_appointments.save(current_user, [i for i in items if isinstance(i, dict)], batch)})


@zentriq_bp.get("/api/rueckrufe")
@login_required
def api_callbacks():
    if (denied := _office_user_required()) is not None:
        return denied
    return jsonify({"callbacks": customer_notes.open_callbacks(), "total": customer_notes.open_callback_count()})
