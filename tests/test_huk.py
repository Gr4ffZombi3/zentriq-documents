"""Kfz-Berechnung ueber den HUK-Webrechner: Vollstaendigkeit, Bestaetigung, Hintergrundauftrag,
Abbruch/Fehler/Zeitueberschreitung, Doppelstart, Buerotrennung, Ergebnisspeicherung, KI-Budget.

Der echte Rechner wird hier NICHT aufgerufen (runner.run ist ersetzt); die echte Bedienung von
huk.de ist manuell mit synthetischen Daten geprueft (siehe Commit-Beschreibung)."""

from datetime import date
from types import SimpleNamespace

import pytest

from app.models import Customer, Tenant, User, UserRole
from app.models.huk_quote import HukQuoteJob
from app.services.huk import ai, runner, schema
from app.tenancy import use_tenant_id

VALID = {
    "hsn": "0603", "tsn": "BGU", "zulassung": "wechsel", "versicherungsbeginn": "01.01.2030", "erstzulassung": "05.2016",
    "halter": "ich", "plz": "96450", "halterzulassung": "05.2016", "kennzeichen": "normal", "finanzierung": "eigen",
    "geburtsdatum": "15.03.1985", "beschaeftigung": "nicht_oeffentlich", "ingenieur": "nein", "wohneigentum": "kein",
    "fahrleistung": "12000", "abstellplatz": "sonstiger", "privat": "ja", "fahrer": ["vn"],
    "sf_haftpflicht": "SF 10", "vollkasko_bisher": "ja", "sf_vollkasko": "SF 10",
    "deckung": "vollkasko", "selbstbeteiligung": "Vollkasko 500 € / Teilkasko 150 €",
    "zahlungsweise": "jährlich", "zahlungsart": "sepa",
}
RESULT = {
    "vehicle": "VW Golf 1.4 TSI Leistung 92 kW", "page": "/tarifrechner/pkw/angebot",
    "coverage": "Vollkasko inkl. Kfz-Haftpflicht", "deductible": "Vollkasko 500 € / Teilkasko 150 €",
    "payment_interval": "jährlich", "payment_method": "SEPA-Lastschrift",
    "tariffs": [
        {"name": "Basis SELECT", "price": "431,90 €", "period": "jährlich", "selected": False, "recommended": False},
        {"name": "Classic SELECT", "price": "460,52 €", "period": "jährlich", "selected": True, "recommended": True},
    ],
}


def _login(app, email, password):
    client = app.test_client()
    client.post("/auth/login", data={"login_type": "email", "identifier": email, "password": password})
    return client


@pytest.fixture(autouse=True)
def huk_dirs(app, tmp_path):
    app.config["HUK_STORAGE_DIR"] = str(tmp_path / "huk")
    app.config["HUK_BROWSER_TMP_DIR"] = str(tmp_path / "huk-tmp")
    app.config["HUK_AI_MAX_COST_USD"] = 0


@pytest.fixture()
def fake_run(monkeypatch):
    calls = []

    def run(values, **kwargs):
        calls.append({"values": values, **kwargs})
        behaviour = run.behaviour
        if isinstance(behaviour, Exception):
            raise behaviour
        if callable(behaviour):
            return behaviour(values, **kwargs)
        return dict(RESULT)

    run.behaviour = None
    monkeypatch.setattr(runner, "run", run)
    run.calls = calls
    return run


@pytest.fixture()
def laura(app, auth_client, employee):
    return _login(app, "mitarbeiter@example.com", "mitarbeiterpass123")


def _form(values):
    data = {k: v for k, v in values.items() if k != "fahrer"}
    data["fahrer"] = values.get("fahrer", [])
    data["kunde"] = values.get("kunde", "")
    return data


def _draft(client, values=VALID, **extra):
    resp = client.post("/kfz-angebot/neu", data=_form({**values, **extra}))
    assert resp.status_code == 302, resp.get_data(as_text=True)[:500]
    return int(resp.headers["Location"].rstrip("/").split("/")[-1])


def _start(client, job_id, ref="ref-1"):
    return client.post(f"/kfz-angebot/{job_id}/starten", data={"bestaetigt": "ja", "client_ref": ref})


# --- Angaben ------------------------------------------------------------------------------------


def test_schema_requires_all_tariff_relevant_fields():
    missing, errors = schema.validate({}, date(2026, 11, 7))
    assert {"hsn", "geburtsdatum", "sf_haftpflicht", "deckung", "zahlungsweise"} <= set(missing)
    missing, errors = schema.validate(VALID, date(2026, 11, 7))
    assert (missing, errors) == ([], {})
    # Folgefragen nur, wenn der Rechner sie stellt.
    partial = {**VALID, "fahrer": ["vn", "partner"]}
    assert "alle_ueber_25" in schema.validate(partial, date(2026, 11, 7))[0]
    no_vk = {k: v for k, v in VALID.items() if k != "sf_vollkasko"} | {"vollkasko_bisher": "nein"}
    assert schema.validate(no_vk, date(2026, 11, 7)) == ([], {})


def test_no_silent_defaults_and_proposals_are_marked(auth_client):
    html = auth_client.get("/kfz-angebot/neu").get_data(as_text=True)
    assert "Vorschlag:" in html  # Versicherungsbeginn nur als Vorschlag
    assert 'name="versicherungsbeginn" value=""' in html
    assert "checked" not in html  # keine vorbelegten Antworten


def test_incomplete_input_is_rejected_and_cannot_start(auth_client, fake_run):
    values = {k: v for k, v in VALID.items() if k not in ("geburtsdatum", "sf_haftpflicht")}
    resp = auth_client.post("/kfz-angebot/neu", data=_form(values))
    assert resp.status_code == 422
    html = resp.get_data(as_text=True)
    assert "Bitte angeben." in html
    job = HukQuoteJob.query.one()
    assert job.status == "draft"
    _start(auth_client, job.id)
    assert HukQuoteJob.query.one().status == "draft"
    assert fake_run.calls == []


def test_summary_before_start_and_explicit_confirmation(auth_client, fake_run):
    job_id = _draft(auth_client)
    html = auth_client.get(f"/kfz-angebot/{job_id}").get_data(as_text=True)
    assert "Angebot auf huk.de berechnen" in html and "Zusammenfassung der Angaben" in html
    assert "15.03.1985" in html and "SF 10" in html
    # Ohne Bestaetigung keine Uebermittlung.
    auth_client.post(f"/kfz-angebot/{job_id}/starten", data={"client_ref": "x"})
    assert fake_run.calls == []


# --- Ablauf -------------------------------------------------------------------------------------


def test_successful_calculation_stores_exact_result_at_customer(app, auth_client, fake_run, tenant):
    customer = Customer(tenant_id=tenant.id, name="Max Mustermann")
    from app.extensions import db

    db.session.add(customer)
    db.session.commit()
    job_id = _draft(auth_client, kunde=str(customer.id))
    resp = _start(auth_client, job_id)
    assert resp.status_code == 302
    job = db.session.get(HukQuoteJob, job_id)
    assert job.status == "done" and job.customer_id == customer.id
    # Uebertragung: genau die bestaetigten Angaben gehen an den Rechner.
    assert fake_run.calls[0]["values"] == job.input_data
    assert fake_run.calls[0]["values"]["selbstbeteiligung"] == VALID["selbstbeteiligung"]
    # Gespeichert wird exakt das Rechnerergebnis.
    assert job.result["tariffs"] == RESULT["tariffs"] and job.result["source"] == "huk.de"
    html = auth_client.get(f"/kfz-angebot/{job_id}").get_data(as_text=True)
    assert "460,52 €" in html and "431,90 €" in html and "Unverbindliche Beitragsberechnung" in html
    assert "Unvollständiges Ergebnis" not in html
    detail = auth_client.get(f"/customers/{customer.id}").get_data(as_text=True)
    assert "Kfz-Berechnungen (HUK)" in detail and "460,52 €" in detail
    # Nach einem Seitenwechsel weiterhin auffindbar.
    assert f"/kfz-angebot/{job_id}" in auth_client.get("/kfz-angebot").get_data(as_text=True)


def test_result_without_selected_tariff_is_marked_incomplete(auth_client, fake_run):
    fake_run.behaviour = lambda v, **k: {**RESULT, "tariffs": [{**t, "selected": False} for t in RESULT["tariffs"]]}
    job_id = _draft(auth_client)
    _start(auth_client, job_id)
    assert "Unvollständiges Ergebnis" in auth_client.get(f"/kfz-angebot/{job_id}").get_data(as_text=True)


def test_double_start_creates_one_run(auth_client, fake_run, db):
    job_id = _draft(auth_client)
    _start(auth_client, job_id, "same")
    _start(auth_client, job_id, "same")
    _start(auth_client, job_id, "other")
    assert len(fake_run.calls) == 1
    assert HukQuoteJob.query.count() == 1


def test_only_one_active_job_per_user(auth_client, fake_run, db, user):
    first = _draft(auth_client)
    job = db.session.get(HukQuoteJob, first)
    job.status = "running"
    db.session.commit()
    second = _draft(auth_client)
    _start(auth_client, second)
    assert db.session.get(HukQuoteJob, second).status == "draft"
    assert fake_run.calls == []


@pytest.mark.parametrize(
    ("error", "status", "text"),
    [
        (runner.HukRunError("blocked", "Der Rechner verlangt eine Sicherheitsabfrage (Captcha)."), "failed", "Captcha"),
        (runner.HukRunError("needs_input", "„Teilkasko 2.500 €“ wird nicht angeboten.", {"field": "selbstbeteiligung_tk"}), "needs_input", "nicht angeboten"),
        (runner.HukRunError("site_changed", "Der Rechner ist unerwartet in den Antragsbereich gewechselt – abgebrochen, nichts beantragt."), "failed", "nichts beantragt"),
    ],
)
def test_external_errors_are_reported_without_result(auth_client, fake_run, db, error, status, text):
    fake_run.behaviour = error
    job_id = _draft(auth_client)
    _start(auth_client, job_id)
    job = db.session.get(HukQuoteJob, job_id)
    assert job.status == status and job.result is None
    assert len(fake_run.calls) == 1  # keine Wiederholung bei nicht voruebergehenden Fehlern
    html = auth_client.get(f"/kfz-angebot/{job_id}").get_data(as_text=True)
    assert text in html and "Es wurde kein Beitrag gespeichert." in html


def test_timeout_is_retried_a_limited_number_of_times(auth_client, fake_run, db, app):
    app.config["HUK_MAX_ATTEMPTS"] = 2
    fake_run.behaviour = runner.HukRunError("timeout", "Der Rechner hat nicht rechtzeitig reagiert.", retryable=True)
    job_id = _draft(auth_client)
    _start(auth_client, job_id)
    job = db.session.get(HukQuoteJob, job_id)
    assert len(fake_run.calls) == 2
    assert job.status == "failed" and job.error_code == "timeout" and job.result is None


def test_each_run_gets_own_browser_profile_which_is_removed(auth_client, fake_run, app):
    import os

    seen = []
    fake_run.behaviour = lambda v, **k: (seen.append((k["profile_dir"], os.path.isdir(k["profile_dir"]))), dict(RESULT))[1]
    first = _draft(auth_client)
    _start(auth_client, first, "a")
    second = _draft(auth_client)
    _start(auth_client, second, "b")
    assert len(seen) == 2 and seen[0][0] != seen[1][0] and all(exists for _p, exists in seen)
    assert not any(os.path.exists(p) for p, _e in seen)


def test_cancel_running_job(auth_client, fake_run, db):
    job_id = _draft(auth_client)

    def cancelled_midway(values, should_cancel, **kwargs):
        job = db.session.get(HukQuoteJob, job_id)
        job.cancel_requested = True
        db.session.commit()
        import time

        time.sleep(2.1)  # Abbruchpruefung fragt hoechstens alle 2 s die DB
        if should_cancel():
            raise runner.HukRunError("cancelled", "Die Berechnung wurde abgebrochen.")
        return dict(RESULT)

    fake_run.behaviour = cancelled_midway
    _start(auth_client, job_id)
    job = db.session.get(HukQuoteJob, job_id)
    assert job.status == "cancelled" and job.result is None


def test_cancel_queued_job_and_status_endpoint(auth_client, db):
    job_id = _draft(auth_client)
    job = db.session.get(HukQuoteJob, job_id)
    job.status = "queued"
    db.session.commit()
    assert auth_client.get(f"/kfz-angebot/{job_id}/status").get_json()["label"] == "In Bearbeitung"
    auth_client.post(f"/kfz-angebot/{job_id}/abbrechen")
    data = auth_client.get(f"/kfz-angebot/{job_id}/status").get_json()
    assert data["status"] == "cancelled" and data["finished"] is True


# --- Rechte und Buerotrennung -------------------------------------------------------------------


def test_employee_sees_only_own_jobs_admin_sees_office(auth_client, laura, fake_run):
    admin_job = _draft(auth_client)
    laura_job = _draft(laura)
    assert laura.get(f"/kfz-angebot/{admin_job}").status_code == 404
    assert laura.get(f"/kfz-angebot/{laura_job}").status_code == 200
    assert auth_client.get(f"/kfz-angebot/{laura_job}").status_code == 200


def test_employee_gets_no_customer_master_data_prefill(auth_client, laura, db, tenant):
    customer = Customer(tenant_id=tenant.id, name="Erika Muster", date_of_birth=date(1980, 2, 3), postal_code="96450")
    db.session.add(customer)
    db.session.commit()
    assert "03.02.1980" in auth_client.get(f"/kfz-angebot/neu?kunde={customer.id}").get_data(as_text=True)
    assert "03.02.1980" not in laura.get(f"/kfz-angebot/neu?kunde={customer.id}").get_data(as_text=True)


def test_other_office_is_isolated(app, db, auth_client, fake_run):
    job_id = _draft(auth_client)
    _start(auth_client, job_id)
    other = Tenant(name="Fremdbüro", slug="fremd-huk")
    db.session.add(other)
    db.session.commit()
    with use_tenant_id(other.id):
        stranger = User(tenant_id=other.id, email="fremd-huk@example.com", is_active=True, role=UserRole.OFFICE_ADMIN)
        stranger.set_password("fremdpasswort123")
        foreign_customer = Customer(tenant_id=other.id, name="Fremdkunde")
        db.session.add_all([stranger, foreign_customer])
        db.session.commit()
        foreign_customer_id = foreign_customer.id
    bob = _login(app, "fremd-huk@example.com", "fremdpasswort123")
    for url in (f"/kfz-angebot/{job_id}", f"/kfz-angebot/{job_id}/status", f"/kfz-angebot/{job_id}/beleg.png"):
        assert bob.get(url).status_code == 404
    assert bob.post(f"/kfz-angebot/{job_id}/abbrechen").status_code == 404
    assert f"/kfz-angebot/{job_id}" not in bob.get("/kfz-angebot").get_data(as_text=True)
    # Fremde Kunden lassen sich nicht zuordnen.
    own = _draft(auth_client, kunde=str(foreign_customer_id))
    assert db.session.get(HukQuoteJob, own).customer_id is None


def test_super_admin_has_no_access(app, db, tenant):
    admin = User(tenant_id=tenant.id, email="root@example.com", is_active=True, role=UserRole.SUPER_ADMIN)
    admin.set_password("superpasswort123")
    db.session.add(admin)
    db.session.commit()
    client = _login(app, "root@example.com", "superpasswort123")
    assert client.get("/kfz-angebot").status_code in (302, 403, 404)


# --- Claude-Anbindung ---------------------------------------------------------------------------


def test_ai_is_off_without_budget(auth_client, app):
    app.config["ANTHROPIC_API_KEY"] = "test"
    app.config["HUK_AI_MAX_COST_USD"] = 0
    html = auth_client.get("/kfz-angebot/neu").get_data(as_text=True)
    assert "kein Budget festgelegt" in html
    resp = auth_client.post("/kfz-angebot/neu", data={**_form({}), "aktion": "ki", "ki_text": "SF 10"})
    assert resp.status_code == 422


class _FakeMessages:
    def __init__(self, payload, usage=(1000, 200)):
        self.payload = payload
        self.usage = usage
        self.calls = 0

    def create(self, **kwargs):
        self.calls += 1
        assert kwargs["output_config"]["format"]["type"] == "json_schema"
        return SimpleNamespace(
            stop_reason="end_turn",
            content=[SimpleNamespace(type="text", text=self.payload)],
            usage=SimpleNamespace(input_tokens=self.usage[0], output_tokens=self.usage[1]),
        )


def test_ai_suggestions_are_marked_and_budget_enforced(auth_client, app, monkeypatch, db):
    import json

    app.config.update(ANTHROPIC_API_KEY="test", HUK_AI_MAX_COST_USD=0.2, HUK_AI_MAX_CALLS=5)
    payload = {f.key: ([] if f.kind == "multi" else "") for f in schema.FIELDS}
    payload.update({"sf_haftpflicht": "SF 12", "hsn": "0603", "geburtsdatum": "31.02.1990"})
    fake = _FakeMessages(json.dumps(payload))
    monkeypatch.setattr(ai, "get_client", lambda: SimpleNamespace(messages=fake))
    resp = auth_client.post("/kfz-angebot/neu", data={**_form({"tsn": "BGU"}), "aktion": "ki", "ki_text": "Golf, HSN 0603, SF 12"})
    html = resp.get_data(as_text=True)
    assert resp.status_code == 200 and "Vorschlag – bitte prüfen" in html
    assert 'value="SF 12" selected' in html and 'value="0603"' in html
    assert "31.02.1990" not in html  # ungueltige Werte werden verworfen
    job = HukQuoteJob.query.one()
    assert job.status == "draft" and job.ai_calls == 1 and float(job.ai_cost_usd) > 0
    # Budget: der ungünstigste Fall eines weiteren Aufrufs passt nicht mehr hinein.
    app.config["HUK_AI_MAX_COST_USD"] = float(job.ai_cost_usd) + 0.001
    resp = auth_client.post(f"/kfz-angebot/{job.id}/angaben", data={**_form({}), "aktion": "ki", "ki_text": "SF 12"})
    assert resp.status_code == 422 and "Budget" in resp.get_data(as_text=True)
    assert fake.calls == 1
