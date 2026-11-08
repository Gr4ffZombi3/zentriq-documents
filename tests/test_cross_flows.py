# ruff: noqa: F811  (Fixtures werden aus anderen Testmodulen importiert)
"""Zusammenspiel der Einstiege: Eingabefeld/Assistent und normale Oberflaeche zeigen dieselben
Daten. Synthetische Daten, KI-Anbieter aus."""

import re
from datetime import datetime

from app.extensions import db as _db
from app.models import Customer
from app.tenancy import bypass_tenant_scope
from tests.test_customer_notes import API, _input, office  # noqa: F401
from tests.test_permissions_security import login, world  # noqa: F401

MAIL = """Von: Erika Neumann <erika.neumann@example.org>
Betreff: Beratungstermin Hausrat

Guten Tag,
ich würde gern einen Termin zur Hausratversicherung vereinbaren.
Telefon: 0170 9876543

Viele Grüße
Erika Neumann"""


def _flat(html: str) -> str:
    return " ".join(re.sub(r"<[^>]+>", " ", html).split())


def test_admin_customer_detail_shows_field_notes_and_callbacks(app, office):
    laura = login(app, "laura@example.com")
    _input(laura, "Neuer Kunde Max Probe 0171 5551234")
    _input(laura, "Max Probe wegen Kfz-Wechsel nicht erreicht")
    with bypass_tenant_scope():
        customer = Customer.query.filter_by(name="Max Probe").one()
    page = _flat(login(app, "admin-a@example.com").get(f"/customers/{customer.id}").get_data(as_text=True))
    assert "Notizen & Rückrufe 1 offen" in page.replace("&amp;", "&")
    assert "Rückruf offen · Kfz-Wechsel · Nicht erreicht · Laura" in page
    assert "Wegen Kfz-Wechsel nicht erreicht" in page
    # Nach dem Schliessen ueber den Assistenten zeigt auch die Detailseite den neuen Stand.
    dennis = login(app, "dennis@example.com")
    contact_id = _input(dennis, "0171/555 12 34")["card"]["open_contacts"][0]["id"]
    assert dennis.post(f"{API}/rueckrufe/{contact_id}/erledigt", json={}).get_json()["kind"] == "closed"
    page = _flat(login(app, "admin-a@example.com").get(f"/customers/{customer.id}").get_data(as_text=True))
    assert "0 offen" in page and "Rückruf offen" not in page
    # Zweites Buero: kein Zugriff
    assert login(app, "admin-b@example.com").get(f"/customers/{customer.id}").status_code == 404


def test_customer_detail_last_change_in_local_time(app, office):
    with bypass_tenant_scope():
        c = Customer(tenant_id=office.tenant_a.id, name="Zeit Test", name_key="zeit test", updated_at=datetime(2026, 7, 1, 10, 0))
        _db.session.add(c)
        _db.session.commit()
        cid = c.id
    page = login(app, "admin-a@example.com").get(f"/customers/{cid}").get_data(as_text=True)
    assert "01.07.2026 12:00" in page  # 10:00 UTC = 12:00 Sommerzeit Europe/Berlin


def test_pasted_mail_without_model_is_recognized_locally(app, office):
    client = login(app, "laura@example.com")
    conversation = client.post(API + "/conversations", json={}).get_json()["conversation"]["id"]
    sent = client.post(f"{API}/conversations/{conversation}/messages", json={"content": MAIL}).get_json()
    reply = client.get(f"{API}/conversations/{conversation}/messages/{sent['reply']['id']}").get_json()["message"]
    assert reply["status"] == "done" and reply["content"].startswith("Kundenmail erkannt von Erika Neumann")
    assert "Schreibe eine Antwort mit Terminvorschlägen" in reply["content"]
    with bypass_tenant_scope():
        assert Customer.query.filter(Customer.name == "Erika Neumann").count() == 0  # nur gelesen
