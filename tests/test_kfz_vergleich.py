"""Kfz-Vergleich aus Versicherungsschein: Auslesen (lokal + pseudonymisierte KI), Pruefhinweise,
fehlende Pflichtangaben, "Bei HUK berechnen", Jahresvergleich, Abweichungen, manuelle Uebergabe,
Rechte und Datenschutz. Ausschliesslich fiktive Daten; der echte HUK-Rechner wird nicht aufgerufen."""

import io
import logging
from decimal import Decimal

import pytest

from app.models import Tenant, User, UserRole
from app.models.huk_quote import HukQuoteJob
from app.services.huk import policy, runner
from app.tenancy import use_tenant_id

SCHEIN = """Musterversicherung AG - Postfach 100 - 12345 Musterstadt
Herrn Erik Beispielmann
Lindenweg 7
53111 Bonn
Versicherungsschein Nr. KF-4711-0815 vom 02.01.2026
Versicherungsnehmer: Erik Beispielmann, geb. 14.06.1980
Fahrzeug: VW Golf 1.5 TSI, amtl. Kennzeichen BN-EB 123, FIN WVWZZZ1KZ6W000001
Herstellerschluessel (HSN) 0603 Typschluessel (TSN) BGUX12345
Erstzulassung 03/2019
Jaehrliche Fahrleistung bis 12.000 km
Fahrerkreis: Versicherungsnehmer und Ehepartner, alle Fahrer ab 25 Jahre
Kfz-Haftpflicht SF-Klasse SF 12 (35 %) 289,40 EUR
Vollkasko SF-Klasse SF 10 (38 %) mit 300 EUR Selbstbeteiligung, Teilkasko 150 EUR SB 241,20 EUR
Werkstattbindung: nein
Zahlweise: monatlich   Beitrag je Zahlungsabschnitt: 44,22 EUR
IBAN DE89 3704 0044 0532 0130 00"""

# Fehlende Pflichtangaben: keine HSN/TSN, keine Fahrleistung, kein Beitrag.
LUECKE = """Versicherungsschein
Fahrzeug VW Golf
Kfz-Haftpflicht SF 1/2
Teilkasko mit 150 EUR Selbstbeteiligung"""

RECHNUNG = """Beitragsrechnung 2027
Kfz-Haftpflicht SF 13 (33 %)
Vollkasko SF 11 (36 %)
Zahlweise: monatlich  Beitrag je Zahlungsabschnitt: 42,10 EUR"""

# Was der Schein nicht enthaelt (vom Benutzer gesammelt nachzutragen).
MISSING_INPUT = {
    "zulassung": "wechsel", "versicherungsbeginn": "01.01.2030", "halter": "ich", "plz": "96450",
    "halterzulassung": "05.2021", "kennzeichen": "normal", "finanzierung": "eigen", "geburtsdatum": "14.06.1980",
    "beschaeftigung": "nicht_oeffentlich", "ingenieur": "nein", "wohneigentum": "kein", "abstellplatz": "sonstiger",
    "privat": "ja", "fahrer_ueber_70": "nein", "zahlungsweise": "jährlich", "zahlungsart": "sepa",
}
RESULT = {
    "vehicle": "VW - GOLF 1.5 TSI", "page": "/tarifrechner/pkw/angebot",
    "coverage": "Vollkasko inkl. Kfz-Haftpflicht", "deductible": "Vollkasko 300 € / Teilkasko 150 €",
    "payment_interval": "jährlich", "payment_method": "SEPA-Lastschrift",
    "tariffs": [
        {"name": "Basis SELECT", "price": "431,13 €", "period": "jährlich", "selected": False, "recommended": False},
        {"name": "Classic SELECT", "price": "460,52 €", "period": "jährlich", "selected": True, "recommended": True},
        {"name": "Classic", "price": "477,38 €", "period": "jährlich", "selected": False, "recommended": False},
    ],
}
PERSONAL = ("Beispielmann", "Lindenweg", "53111", "KF-4711", "BN-EB", "WVWZZZ", "DE89", "14.06.1980")


def _pdf(text: str) -> bytes:
    import fitz

    doc = fitz.open()
    page = doc.new_page()
    y = 60
    for line in text.split("\n"):
        page.insert_text((40, y), line, fontsize=9)
        y += 14
    data = doc.tobytes()
    doc.close()
    return data


def _png() -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (1600, 1200), "white").save(buf, format="PNG")
    return buf.getvalue()


@pytest.fixture(autouse=True)
def setup(app, tmp_path, monkeypatch):
    app.config["HUK_STORAGE_DIR"] = str(tmp_path / "huk")
    app.config["HUK_BROWSER_TMP_DIR"] = str(tmp_path / "huk-tmp")
    app.config["HUK_AI_MAX_COST_USD"] = 0
    from app.services.ai import router

    monkeypatch.setattr(router, "can_answer", lambda: False)  # Standard: kein Modell


@pytest.fixture()
def fake_run(monkeypatch):
    calls = []

    def run(values, **kwargs):
        calls.append(values)
        if isinstance(run.behaviour, Exception):
            raise run.behaviour
        return dict(RESULT)

    run.behaviour = None
    run.calls = calls
    monkeypatch.setattr(runner, "run", run)
    return run


def _upload(client, schein=SCHEIN, rechnung=None, as_pdf=True, **extra):
    data = {"schein": (io.BytesIO(_pdf(schein) if as_pdf else schein), "schein.pdf" if as_pdf else "foto.png"), **extra}
    if rechnung:
        data["rechnung"] = (io.BytesIO(_pdf(rechnung)), "rechnung.pdf")
    return client.post("/kfz-angebot/versicherungsschein", data=data, content_type="multipart/form-data")


def _get(job_id):
    from app.extensions import db

    db.session.expire_all()
    return db.session.get(HukQuoteJob, job_id)


def _job_id(resp) -> int:
    assert resp.status_code == 302, resp.get_data(as_text=True)[:600]
    return int(resp.headers["Location"].split("/kfz-angebot/")[1].split("/")[0])


def _form_from(job, extra, aktion="berechnen", previous=None):
    values = {**(job.input_data or {}), **extra}
    data = {k: v for k, v in values.items() if k != "fahrer"}
    data["fahrer"] = values.get("fahrer", [])
    data["kunde"] = ""
    data["aktion"] = aktion
    data["client_ref"] = f"ref-{job.id}-{aktion}"
    prev = previous if previous is not None else (job.comparison or {}).get("previous") or {}
    data["bisher_beitrag"] = prev.get("beitrag") or ""
    data["bisher_zahlweise"] = prev.get("zahlweise") or ""
    data["bisher_werkstatt"] = prev.get("werkstatt") or ""
    return data


# --- Auslesen -------------------------------------------------------------------------------------


def test_local_extraction_reads_tariff_data_without_guessing(app):
    found, notes = policy.merge(policy.local_extract(SCHEIN), policy.Found(), policy.Found(), policy.Found())
    values, previous, flags = policy.build(found, notes)
    assert values == {
        "hsn": "0603", "tsn": "BGU", "erstzulassung": "03.2019", "fahrer": ["vn", "partner"], "alle_ueber_25": "ja",
        "sf_haftpflicht": "SF 12", "sf_vollkasko": "SF 10", "deckung": "vollkasko", "fahrleistung": "12000",
        "vollkasko_bisher": "ja", "selbstbeteiligung": "Vollkasko 300 € / Teilkasko 150 €",
    }
    assert previous["beitrag"] == "44,22" and previous["zahlweise"] == "monatlich" and previous["werkstatt"] == "nein"
    # HUK kennt keine monatliche Zahlung: nicht raten, sondern nachfragen.
    assert "zahlungsweise" not in values and "monatlich" in flags["zahlungsweise"]


def test_sf_half_class_and_missing_values_stay_empty(app):
    found, notes = policy.merge(policy.local_extract(LUECKE), policy.Found(), policy.Found(), policy.Found())
    values, previous, flags = policy.build(found, notes)
    assert values["sf_haftpflicht"] == "SF 1/2"
    assert values["deckung"] == "teilkasko" and values["selbstbeteiligung_tk"] == "Teilkasko 150 €"
    for key in ("hsn", "tsn", "fahrleistung", "fahrer", "zahlungsweise"):
        assert key not in values
    assert previous["beitrag"] is None and "bisher_beitrag" in flags


def test_contradictions_and_odd_values_are_flagged_not_filled(app):
    text = SCHEIN.replace("Erstzulassung 03/2019", "Erstzulassung 03/2019\nHSN 0588").replace("bis 12.000 km", "15.500 km")
    found, notes = policy.merge(policy.local_extract(text), policy.Found(), policy.Found(), policy.Found())
    values, _previous, flags = policy.build(found, notes)
    assert "hsn" not in values and "0603" in flags["hsn"] and "0588" in flags["hsn"]
    assert "fahrleistung" not in values and "1.000er" in flags["fahrleistung"]


def test_table_layout_with_ocr_currency_errors(app):
    # So liest Tesseract eine Spartentabelle aus einem Handyfoto (€ als "-" bzw. "+").
    ocr = """Sparte SF-Klasse Selbstbeteiligung Beitrag
Kfz-Haftpflicht SF 12 289,40 -
Vollkasko SF 10 300 - Selbstbeteiligung 196,20 -
Teilkasko 150 + Selbstbeteiligung 45, 00 -
Zahlweise: monatlich Beitrag je Zahlungsabschnitt: 44,22 -"""
    found, notes = policy.merge(policy.local_extract(ocr), policy.Found(), policy.Found(), policy.Found())
    values, previous, _flags = policy.build(found, notes)
    assert values["selbstbeteiligung"] == "Vollkasko 300 € / Teilkasko 150 €"
    assert values["sf_haftpflicht"] == "SF 12" and values["sf_vollkasko"] == "SF 10"
    assert previous["beitrag"] == "44,22" and previous["zahlweise"] == "monatlich"


def test_deductible_not_offered_by_huk_is_flagged(app):
    text = SCHEIN.replace("mit 300 EUR Selbstbeteiligung", "mit 250 EUR Selbstbeteiligung")
    found, notes = policy.merge(policy.local_extract(text), policy.Found(), policy.Found(), policy.Found())
    values, _previous, flags = policy.build(found, notes)
    assert "selbstbeteiligung" not in values and "250 €" in flags["selbstbeteiligung"]


def test_invoice_is_newer_for_sf_classes_and_premium(app):
    found, notes = policy.merge(policy.local_extract(SCHEIN), policy.Found(), policy.local_extract(RECHNUNG), policy.Found())
    values, previous, flags = policy.build(found, notes)
    assert values["sf_haftpflicht"] == "SF 13" and values["sf_vollkasko"] == "SF 11"
    assert previous["beitrag"] == "42,10"
    assert "Beitragsrechnung ist aktueller" in flags["info_sf_haftpflicht"]


def test_model_sees_only_pseudonymized_relevant_lines_and_conflicts_are_flagged(app, user, monkeypatch):
    from app.services.ai import router

    seen = {}

    def complete(**kwargs):
        seen.update(kwargs)
        # Modell liest eine andere SF-Klasse als die lokale Erkennung -> Widerspruch, kein Wert.
        return type("C", (), {"text": '{"hsn": "0603", "tsn": "BGU", "sf_haftpflicht": "SF 11", "werkstattbindung": "nein"}'})()

    monkeypatch.setattr(router, "can_answer", lambda: True)
    monkeypatch.setattr(router, "complete", complete)
    model = policy.model_extract(user, SCHEIN)
    prompt = seen["messages"][0]["content"]
    assert seen["customer_data"] is False and seen["purpose"] == "huk_policy"
    for value in PERSONAL:
        assert value not in prompt
    assert "0603" in prompt and "SF 12" in prompt and "44,22" in prompt
    found, notes = policy.merge(policy.local_extract(SCHEIN), model, policy.Found(), policy.Found())
    values, _previous, flags = policy.build(found, notes)
    assert "sf_haftpflicht" not in values and "SF 12 oder SF 11" in flags["sf_haftpflicht"]
    assert values["hsn"] == "0603"


def test_more_drivers_require_age_70_answer():
    from datetime import date

    from app.services.huk import schema

    base = {"fahrer": ["vn", "partner"], "alle_ueber_25": "ja"}
    assert "fahrer_ueber_70" in schema.validate(base, date(2026, 11, 11))[0]
    assert "fahrer_ueber_70" not in schema.validate({"fahrer": ["vn"]}, date(2026, 11, 11))[0]


def test_annual_conversion_is_traceable():
    assert policy.annual(Decimal("44.22"), "monatlich") == Decimal("530.64")
    assert policy.annual(Decimal("230.10"), "halbjährlich") == Decimal("460.20")
    assert policy._calc_text(Decimal("44.22"), "monatlich") == "12 × 44,22 € (monatlich) = 530,64 €"
    assert policy.parse_amount("1.044,22 €") == "1.044,22" and policy.parse_amount("44.2") == "44,20"
    assert policy.parse_amount("abc") is None and policy.parse_amount("0") is None


# --- Ablauf ---------------------------------------------------------------------------------------


def test_upload_creates_review_with_missing_fields_collected(app, auth_client, fake_run, tmp_path):
    resp = _upload(auth_client)
    job_id = _job_id(resp)
    job = _get(job_id)
    assert job.status == "draft" and job.comparison["source"] == "versicherungsschein"
    assert job.input_data["hsn"] == "0603" and job.input_data["sf_haftpflicht"] == "SF 12"
    html = auth_client.get(resp.headers["Location"]).get_data(as_text=True)
    assert "Kfz-Vergleich: Angaben prüfen" in html and "Noch anzugeben" in html and "Bei HUK berechnen" in html
    assert "Aus dem Dokument – bitte prüfen" in html and "monatlich" in html
    assert 'value="44,22"' in html
    # Datei wird nicht gespeichert.
    assert not any(tmp_path.rglob("*.pdf"))
    assert fake_run.calls == []


def test_missing_required_fields_block_calculation(app, auth_client, fake_run):
    job_id = _job_id(_upload(auth_client, schein=LUECKE))
    job = _get(job_id)
    html = auth_client.get(f"/kfz-angebot/{job_id}/angaben?pruefen=1").get_data(as_text=True)
    assert "Noch anzugeben" in html and "Bisheriger Beitrag nicht erkannt" in html
    resp = auth_client.post(f"/kfz-angebot/{job_id}/angaben", data=_form_from(job, {}))
    assert resp.status_code == 422
    assert "Bitte angeben." in resp.get_data(as_text=True)
    assert _get(job_id).status == "draft"
    assert fake_run.calls == []


def test_calculate_compares_annual_premiums_and_shows_deviations(app, auth_client, fake_run):
    job_id = _job_id(_upload(auth_client))
    job = _get(job_id)
    resp = auth_client.post(f"/kfz-angebot/{job_id}/angaben", data=_form_from(job, MISSING_INPUT))
    assert resp.status_code == 302
    job = _get(job_id)
    assert job.status == "done" and len(fake_run.calls) == 1
    # Nur Rechnerfelder gehen an den Rechner - kein bisheriger Beitrag, keine Personenangaben.
    sent = fake_run.calls[0]
    assert "bisher_beitrag" not in sent and "beitrag" not in sent and "comparison" not in sent
    html = auth_client.get(f"/kfz-angebot/{job_id}").get_data(as_text=True)
    assert "Vergleich pro Jahr" in html
    assert "12 × 44,22 € (monatlich) = 530,64 €" in html
    assert "460,52 €" in html and "70,12 € günstiger" in html
    assert "Werkstattbindung" in html and "ja (Tarif Classic SELECT)" in html
    assert "Ergebnis der Vergleichsberechnung" in html


def test_without_previous_premium_huk_result_is_shown_and_difference_open(app, auth_client, fake_run):
    job_id = _job_id(_upload(auth_client))
    job = _get(job_id)
    data = _form_from(job, MISSING_INPUT, previous={"beitrag": "", "zahlweise": "", "werkstatt": ""})
    assert auth_client.post(f"/kfz-angebot/{job_id}/angaben", data=data).status_code == 302
    html = auth_client.get(f"/kfz-angebot/{job_id}").get_data(as_text=True)
    assert "nicht angegeben" in html and "offen" in html and "460,52 €" in html


def test_changed_values_show_as_deviation(app, auth_client, fake_run):
    job_id = _job_id(_upload(auth_client))
    job = _get(job_id)
    data = _form_from(job, {**MISSING_INPUT, "fahrleistung": "15000", "selbstbeteiligung": "Vollkasko 500 € / Teilkasko 150 €"})
    auth_client.post(f"/kfz-angebot/{job_id}/angaben", data=data)
    html = auth_client.get(f"/kfz-angebot/{job_id}").get_data(as_text=True)
    assert "12.000 km/Jahr" in html and "15.000 km/Jahr" in html
    assert "Vollkasko 300 € / Teilkasko 150 €" in html and "Vollkasko 500 € / Teilkasko 150 €" in html


def test_blocked_calculator_offers_manual_continuation(app, auth_client, fake_run):
    fake_run.behaviour = runner.HukRunError("blocked", "Der Rechner verlangt eine Sicherheitsabfrage (Captcha). Bitte die Berechnung manuell auf huk.de durchführen.")
    job_id = _job_id(_upload(auth_client))
    job = _get(job_id)
    auth_client.post(f"/kfz-angebot/{job_id}/angaben", data=_form_from(job, MISSING_INPUT))
    job = _get(job_id)
    assert job.status == "failed" and job.error_code == "blocked"
    html = auth_client.get(f"/kfz-angebot/{job_id}").get_data(as_text=True)
    assert "Captcha" in html and "Manuell weiterrechnen" in html and "Rechner auf huk.de selbst öffnen" in html
    resp = auth_client.post(f"/kfz-angebot/{job_id}/huk-beitrag", data={"betrag": "230,10", "zahlweise": "halbjährlich", "werkstatt": "nein"})
    assert resp.status_code == 302
    html = auth_client.get(f"/kfz-angebot/{job_id}").get_data(as_text=True)
    assert "Manuell eingetragener HUK-Beitrag" in html and "2 × 230,10 € (halbjährlich) = 460,20 €" in html
    assert "70,44 € günstiger" in html
    assert "(manuell)" in auth_client.get("/kfz-angebot").get_data(as_text=True)


def test_manual_price_not_allowed_after_automatic_result(app, auth_client, fake_run):
    job_id = _job_id(_upload(auth_client))
    job = _get(job_id)
    auth_client.post(f"/kfz-angebot/{job_id}/angaben", data=_form_from(job, MISSING_INPUT))
    auth_client.post(f"/kfz-angebot/{job_id}/huk-beitrag", data={"betrag": "1,00", "zahlweise": "jährlich"})
    assert "manual" not in _get(job_id).comparison


def test_needs_input_message_names_offered_options(app, auth_client, fake_run):
    fake_run.behaviour = runner.HukRunError("needs_input", "„Teilkasko 300 €“ wird für „Selbstbeteiligung“ nicht angeboten.",
                                            {"field": "selbstbeteiligung", "options": ["Teilkasko ohne", "Teilkasko 150 €"]})
    job_id = _job_id(_upload(auth_client))
    job = _get(job_id)
    auth_client.post(f"/kfz-angebot/{job_id}/angaben", data=_form_from(job, MISSING_INPUT))
    assert "Angeboten: Teilkasko ohne, Teilkasko 150 €." in _get(job_id).message


def test_photo_upload_goes_through_local_ocr(app, auth_client, monkeypatch):
    calls = []

    def fake_lines(image):
        calls.append(image.size)
        return SCHEIN

    monkeypatch.setattr("app.services.ocr.tesseract_ocr.ocr_image_lines", fake_lines)
    job_id = _job_id(_upload(auth_client, schein=_png(), as_pdf=False))
    assert calls and _get(job_id).input_data["tsn"] == "BGU"


def test_unreadable_or_wrong_files_are_explained(app, auth_client, monkeypatch):
    monkeypatch.setattr("app.services.ocr.tesseract_ocr.ocr_image_lines", lambda image: "unscharf")
    resp = _upload(auth_client, schein=_png(), as_pdf=False)
    assert resp.status_code == 422 and "keine Tarifangaben erkannt" in resp.get_data(as_text=True)
    resp = auth_client.post("/kfz-angebot/versicherungsschein", data={"schein": (io.BytesIO(b"hallo"), "notiz.txt")},
                            content_type="multipart/form-data")
    assert resp.status_code == 422 and "Foto oder PDF" in resp.get_data(as_text=True)
    resp = auth_client.post("/kfz-angebot/versicherungsschein", data={}, content_type="multipart/form-data")
    assert resp.status_code == 422 and "Versicherungsschein" in resp.get_data(as_text=True)
    assert HukQuoteJob.query.count() == 0


def test_logs_contain_no_document_content(app, auth_client, fake_run, caplog):
    caplog.set_level(logging.DEBUG)
    job_id = _job_id(_upload(auth_client))
    job = _get(job_id)
    auth_client.post(f"/kfz-angebot/{job_id}/angaben", data=_form_from(job, MISSING_INPUT))
    text = caplog.text
    for value in PERSONAL + ("44,22", "0603", "SF 12"):
        assert value not in text


# --- Rechte -----------------------------------------------------------------------------------------


def test_office_and_role_rights(app, db, auth_client, employee_client, fake_run):
    job_id = _job_id(_upload(auth_client))
    # Mitarbeiter sehen nur eigene Auftraege.
    assert employee_client.get(f"/kfz-angebot/{job_id}").status_code == 404
    assert employee_client.post(f"/kfz-angebot/{job_id}/huk-beitrag", data={"betrag": "1,00", "zahlweise": "jährlich"}).status_code == 404
    # Mitarbeiter duerfen selbst vergleichen.
    own = _job_id(_upload(employee_client))
    assert own != job_id
    # Anderes Buero: nicht sichtbar.
    other = Tenant(name="Anderes Buero", slug="anderes-kfz")
    db.session.add(other)
    db.session.commit()
    stranger = User(tenant_id=other.id, email="fremd@example.com", role=UserRole.OFFICE_ADMIN, is_active=True)
    stranger.set_password("fremdespasswort123")
    db.session.add(stranger)
    db.session.commit()
    client = app.test_client()
    client.post("/auth/login", data={"login_type": "email", "identifier": "fremd@example.com", "password": "fremdespasswort123"})
    assert client.get(f"/kfz-angebot/{job_id}").status_code == 404
    assert client.post(f"/kfz-angebot/{job_id}/angaben", data={"aktion": "berechnen"}).status_code == 404
    with use_tenant_id(other.id):
        assert HukQuoteJob.query.count() == 0
