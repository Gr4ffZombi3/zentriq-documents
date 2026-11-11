"""Live-Zusammenarbeit in Kundenakten (Flask-Seite des Dienstes zentriq-live, live_service/).

- token_for(): kurzlebiges, signiertes Token fuer GENAU eine Kundenakte - erst nach Pruefung von
  Anmeldung, Buero und Zugriff (der Live-Dienst selbst kennt keine Datenbank).
- Nach jedem ERFOLGREICHEN Commit werden die betroffenen Kundenakten gemeldet (Session-Hooks,
  unabhaengig davon, welche Route oder welcher Celery-Task gespeichert hat). Ein Rollback meldet
  nichts.
- publish(): best effort ueber das vorhandene Redis. Ist Redis/der Dienst nicht da, laeuft die App
  unveraendert weiter - Live-Hinweise sind Komfort, nie Voraussetzung fuers Speichern.

Gemeldet werden nur Art der Aenderung und Name des Bearbeiters - keine Kundendaten."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from flask import current_app, has_app_context, has_request_context
from sqlalchemy import event
from sqlalchemy.orm import Session

from live_service import tokens

log = logging.getLogger(__name__)

SESSION_KEY = "zq_live_changes"
ACTOR_KEY = "zq_live_actor"

# Modell -> Bezeichnung im Hinweis ("Laura hat gerade ... geaendert: Notiz, Termin").
# HUK-Rechner, Empfehlungen, Listenabgleiche usw. fehlen bewusst: sie aendern sich im Hintergrund
# laufend und wuerden nur Unruhe erzeugen.
KIND_LABELS = {
    "Customer": "Stammdaten",
    "CustomerNote": "Notiz",
    "CustomerContact": "Rückruf",
    "CalendarEvent": "Termin",
    "CustomerMemo": "Memo",
    "DocumentCustomer": "Dokument",
    "Task": "Aufgabe",
}


def enabled() -> bool:
    return bool(current_app.config.get("LIVE_ENABLED", True))


def channel_for_customer(customer_id: int) -> str:
    return f"customer:{int(customer_id)}"


def display_name(user) -> str:
    """Anzeigename fuer Kollegen - nie die E-Mail-Adresse."""
    profile = getattr(user, "employee_profile", None)
    name = (getattr(profile, "display_name", None) or "").strip()
    if not name or "@" in name:
        return "Ein Teammitglied"
    return name


def token_for(user, customer) -> str:
    return tokens.issue(
        current_app.config["SECRET_KEY"],
        user_id=user.id,
        tenant_id=customer.tenant_id,
        name=display_name(user),
        channel=channel_for_customer(customer.id),
    )


# --- Veroeffentlichen ------------------------------------------------------------------------


def _redis():
    client = current_app.extensions.get("zq_live_redis")
    if client is None:
        import redis

        url = current_app.config.get("LIVE_REDIS_URL") or current_app.config.get("CELERY_BROKER_URL")
        # Kurze Timeouts: ein haengendes Redis darf keine Speicheranfrage aufhalten.
        client = redis.Redis.from_url(url, socket_timeout=0.3, socket_connect_timeout=0.3)
        current_app.extensions["zq_live_redis"] = client
    return client


def publish(tenant_id: int, channel: str, message: dict) -> None:
    if not has_app_context() or not enabled():
        return
    payload = json.dumps(message, ensure_ascii=False, separators=(",", ":"))
    target = tokens.redis_channel(tenant_id, channel)
    if current_app.testing:
        # Tests: kein echtes Redis - Nachrichten zum Pruefen sammeln.
        current_app.extensions.setdefault("zq_live_sent", []).append((target, json.loads(payload)))
        return
    try:
        _redis().publish(target, payload)
    except Exception as exc:  # noqa: BLE001 - Redis weg: nur protokollieren, nie die Anfrage scheitern lassen
        log.warning("Live-Hinweis nicht gesendet (%s)", type(exc).__name__)


def _actor() -> dict | None:
    """Wer hat geaendert? In Celery/CLI ohne Anfrage: niemand (Hinweis "wurde aktualisiert").

    Laeuft im Flush - deshalb NUR einen bereits geladenen Benutzer verwenden (flask_login legt ihn
    in g._login_user ab). current_user wuerde sonst mitten im Flush den User-Loader starten, der
    selbst speichern kann (Sitzungs-Zeitstempel) und den laufenden Flush stoert."""
    if not has_request_context():
        return None
    from flask import g

    user = g.get("_login_user")
    if user is None or not getattr(user, "is_authenticated", False):
        return None
    try:
        return {"u": user.id, "n": display_name(user)}
    except Exception:  # noqa: BLE001 - Name nicht ladbar: Hinweis ohne Namen
        return None


def publish_editing(user, customer, active: bool, part: str) -> None:
    publish(
        customer.tenant_id,
        channel_for_customer(customer.id),
        {"type": "editing", "by": {"u": user.id, "n": display_name(user)}, "on": bool(active), "part": part[:40]},
    )


# --- Session-Hooks: betroffene Kundenakten nach dem Commit melden --------------------------------


def _touched(session: Session) -> dict:
    return session.info.setdefault(SESSION_KEY, {})


def _collect(session: Session, flush_context) -> None:
    changes = None
    for state, objs in (("new", session.new), ("dirty", session.dirty), ("deleted", session.deleted)):
        for obj in objs:
            label = KIND_LABELS.get(type(obj).__name__)
            if label is None:
                continue
            if label == "Stammdaten":
                if state == "dirty" and not session.is_modified(obj, include_collections=False):
                    continue
                customer_id = obj.id
            else:
                customer_id = getattr(obj, "customer_id", None)
            tenant_id = getattr(obj, "tenant_id", None)
            if not customer_id or not tenant_id:
                continue
            if changes is None:
                changes = _touched(session)
            changes.setdefault((tenant_id, customer_id), set()).add(label)


def _after_flush(session: Session, flush_context) -> None:
    # new/dirty/deleted zeigen hier noch den Stand vor dem Flush, IDs und Fremdschluessel
    # (auch ueber Beziehungen gesetzte) sind aber schon vergeben.
    _collect(session, flush_context)
    # Bearbeiter JETZT bestimmen: nach dem Commit sind alle Objekte abgelaufen und die Session darf
    # kein SQL mehr ausfuehren (Name aus dem Mitarbeiterprofil waere dann nicht mehr ladbar).
    if session.info.get(SESSION_KEY) and ACTOR_KEY not in session.info:
        session.info[ACTOR_KEY] = _actor()


def _after_commit(session: Session) -> None:
    changes = session.info.pop(SESSION_KEY, None)
    actor = session.info.pop(ACTOR_KEY, None)
    if not changes or not has_app_context():
        return
    at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    for (tenant_id, customer_id), kinds in changes.items():
        publish(tenant_id, channel_for_customer(customer_id), {"type": "changed", "kinds": sorted(kinds), "by": actor, "at": at})


def _after_rollback(session: Session) -> None:
    session.info.pop(SESSION_KEY, None)
    session.info.pop(ACTOR_KEY, None)


_installed = False


def install_hooks() -> None:
    """Einmal je Prozess (create_app kann in Tests mehrfach laufen)."""
    global _installed
    if _installed:
        return
    event.listen(Session, "after_flush", _after_flush)
    event.listen(Session, "after_commit", _after_commit)
    event.listen(Session, "after_soft_rollback", lambda session, previous: _after_rollback(session))
    _installed = True
