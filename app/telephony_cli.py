"""CLI fuer die Anruferkennung (Webex Calling): Durchwahl zuordnen und fiktive Testanrufe.

  flask telefonie zuordnen --email laura@example.com --person-id <Webex-Person-ID> --label "Durchwahl 21"
  flask telefonie liste
  flask telefonie abschalten --email laura@example.com
  flask telefonie testanruf --email laura@example.com --nummer "0228 1234567" [--unterdrueckt] [--ende-nach 10]
  flask telefonie aufraeumen [--tage 7]

Der Testanruf laeuft durch denselben Webhook wie Webex (signiert mit WEBEX_WEBHOOK_SECRET) und
zeigt den Hinweis im geoeffneten Browser-Tab des Benutzers. Ausgegeben werden keine Secrets."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
import uuid
from datetime import datetime, timezone

import click
from flask import current_app
from flask.cli import AppGroup

from app.extensions import db
from app.models import TelephonyLine, User
from app.services import telephony
from app.tenancy import bypass_tenant_scope

telephony_cli = AppGroup("telefonie", help="Anruferkennung ueber Webex Calling (Test mit einer Durchwahl).")


def _user(email: str) -> User:
    with bypass_tenant_scope():
        user = User.query.filter(db.func.lower(User.email) == email.strip().lower()).first()
        if user is None or user.is_deleted:
            raise click.ClickException("Benutzer nicht gefunden.")
        _ = (user.id, user.tenant_id, user.role)
    return user


@telephony_cli.command("zuordnen")
@click.option("--email", required=True, help="Zentriq-Benutzer, der den Hinweis sieht.")
@click.option("--person-id", required=True, help="Webex-Person-ID (Base64 aus der API oder UUID aus Control Hub).")
@click.option("--label", default="", help='Anzeige, z. B. "Durchwahl 21".')
def assign_command(email, person_id, label):
    user = _user(email)
    if not (user.is_office_admin or user.is_employee):
        raise click.ClickException("Nur Buero-Benutzer (Admin oder Mitarbeiter) koennen Anruf-Hinweise erhalten.")
    try:
        line = telephony.assign_line(user, person_id, label)
    except telephony.LineError as exc:
        raise click.ClickException(str(exc)) from None
    click.echo(f"Zugeordnet: Leitung {line.id} -> Benutzer {user.id} ({line.label or 'ohne Bezeichnung'}).")


@telephony_cli.command("liste")
def list_command():
    with bypass_tenant_scope():
        for line in TelephonyLine.query.order_by(TelephonyLine.id).all():
            state = "aktiv" if line.active else "aus"
            click.echo(f"Leitung {line.id}: Benutzer {line.user_id} (Buero {line.tenant_id}) · {line.label or '-'} · {state}")


@telephony_cli.command("abschalten")
@click.option("--email", required=True)
def disable_command(email):
    user = _user(email)
    with bypass_tenant_scope():
        line = TelephonyLine.query.filter_by(user_id=user.id).first()
        if line is None:
            raise click.ClickException("Keine Durchwahl zugeordnet.")
        line.active = False
        db.session.commit()
    click.echo("Anruf-Hinweise fuer diesen Benutzer abgeschaltet (Zuordnung bleibt gespeichert).")


@telephony_cli.command("aufraeumen")
@click.option("--tage", type=int, default=None, help="Abweichende Frist in Tagen (Standard: TELEPHONY_CALL_RETENTION_DAYS).")
def purge_command(tage):
    days = tage if tage is not None else int(current_app.config.get("TELEPHONY_CALL_RETENTION_DAYS") or 0)
    if days <= 0:
        click.echo("Aufbewahrung unbegrenzt (0 Tage) - nichts geloescht.")
        return
    click.echo(f"{telephony.purge_old_calls(days)} Anrufe geloescht (aelter als {days} Tage).")


def _person_id(line: TelephonyLine) -> str:
    raw = f"ciscospark://us/PEOPLE/{line.webex_person_uuid}".encode()
    return base64.b64encode(raw).decode().rstrip("=")


def _send(client, secret: str, payload: dict) -> tuple[int, str]:
    body = json.dumps(payload).encode()
    signature = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    response = client.post(
        "/telefonie/webex/webhook", data=body, content_type="application/json", headers={"X-Webex-Signature": f"SHA-256={signature}"}
    )
    return response.status_code, (response.get_json(silent=True) or {}).get("result", "")


def fake_payload(person_id: str, call_id: str, *, state: str, number: str | None, privacy: bool, event: str) -> dict:
    """Meldung im Format der Cisco-Doku (telephony_calls, Beispiel-Payload)."""
    now = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    party = {"privacyEnabled": privacy, "callType": "external"}
    if not privacy and number:
        party.update({"number": number, "name": ""})
    return {
        "id": "zentriq-testwebhook",
        "name": "Zentriq Testanruf",
        "resource": "telephony_calls",
        "event": event,
        "orgId": current_app.config.get("WEBEX_ORG_ID") or "zentriq-test-org",
        "ownedBy": "org",
        "status": "active",
        "actorId": person_id,
        "data": {
            "actorType": "PEOPLE",
            "eventType": "received" if state == "alerting" else "disconnected",
            "eventTimestamp": now,
            "callId": call_id,
            "callSessionId": call_id,
            "personality": "terminator",
            "state": state,
            "remoteParty": party,
            "created": now,
        },
    }


@telephony_cli.command("testanruf")
@click.option("--email", required=True, help="Benutzer mit zugeordneter Durchwahl.")
@click.option("--nummer", default="", help="Fiktive Anrufernummer, z. B. 0228 1234567 oder +49 228 1234567.")
@click.option("--unterdrueckt", is_flag=True, help="Anruf mit unterdrueckter Nummer.")
@click.option("--ende-nach", type=int, default=0, help="Nach so vielen Sekunden 'aufgelegt' senden (0 = nicht).")
def test_call_command(email, nummer, unterdrueckt, ende_nach):
    secret = current_app.config.get("WEBEX_WEBHOOK_SECRET") or ""
    if not secret:
        raise click.ClickException("WEBEX_WEBHOOK_SECRET ist nicht gesetzt.")
    user = _user(email)
    with bypass_tenant_scope():
        line = TelephonyLine.query.filter_by(user_id=user.id, active=True).first()
        if line is None:
            raise click.ClickException("Diesem Benutzer ist keine aktive Durchwahl zugeordnet.")
        person_id = _person_id(line)
    if not unterdrueckt and not nummer.strip():
        raise click.ClickException("--nummer angeben oder --unterdrueckt setzen.")
    call_id = f"zentriq-test-{uuid.uuid4().hex}"
    client = current_app.test_client()
    status, result = _send(client, secret, fake_payload(person_id, call_id, state="alerting", number=nummer, privacy=unterdrueckt, event="created"))
    click.echo(f"Klingeln gemeldet: HTTP {status} {result}")
    # Doppelte Zustellung pruefen: dieselbe Meldung erneut.
    status, result = _send(client, secret, fake_payload(person_id, call_id, state="alerting", number=nummer, privacy=unterdrueckt, event="created"))
    click.echo(f"Wiederholung derselben Meldung: HTTP {status} {result}")
    if ende_nach > 0:
        time.sleep(ende_nach)
        status, result = _send(client, secret, fake_payload(person_id, call_id, state="disconnected", number=nummer, privacy=unterdrueckt, event="deleted"))
        click.echo(f"Aufgelegt gemeldet: HTTP {status} {result}")
