# ruff: noqa: F811  (Fixtures werden aus anderen Testmodulen importiert)
"""Abrechnungen: automatische Mitarbeiteruebersicht aus der HUK-Werbegebuehrenabrechnung.

Geprueft: Zuordnung ueber die VM-Nummer der Tabellenbloecke (Blockwechsel mitten auf der Seite,
Fortsetzung auf der Folgeseite), Zaehlregeln (Neuabschluss einmal je Vertrag, Folgezeilen/Stornos/
10-€-Positionen zaehlen nicht), Betrag nur aus "Betrag in Euro", Deckblatt/Uebertraege/Summen nie,
"Auswertung unvollständig" ohne Pruefliste und ohne Null-Werte, Neuauswertung im selben Beleg
(erst nach Erfolg, keine Dubletten), Zugriff nur fuer den Buero-Admin des eigenen Bueros.
Alle Belege sind synthetisch und werden im Test erzeugt."""

import io
from decimal import Decimal

import fitz
import pytest

from app.models import Settlement, SettlementLine
from app.services import settlements as svc
from app.tenancy import bypass_tenant_scope
from tests.test_customer_notes import _input, office  # noqa: F401
from tests.test_permissions_security import login, world  # noqa: F401


def make_huk_pdf(total="131,40", cover_amount="9.256,19") -> bytes:
    """Synthetischer Beleg im Aufbau der HUK-Werbegebuehrenabrechnung (Querformat)."""
    doc = fitz.open()
    cover = doc.new_page(width=842, height=595)
    for y, text in ((40, "Laura Beispiel"),  # Empfaenger der Gesamtabrechnung - nicht zur Zuordnung
                    (60, "Ihre Werbegebührenabrechnung für den Monat Oktober 2026"), (80, "Abrechnungsmonat: Oktober 2026"),
                    (100, f"Betrag {cover_amount} €"), (120, "Übertrag 444,44 €"), (140, "Zielerreichung 777,77 €"), (160, "VM-Nr.: 085555B")):
        cover.insert_text((40, y), text, fontsize=10, fontname="helv")
    state = {"page": None, "y": 0}

    def new_page():
        page = doc.new_page(width=842, height=595)
        state["page"], state["y"] = page, 130
        put(30, 40, "Laura Beispiel", 9)
        put(30, 60, "Einzelübersicht der Werbegebührenabrechnung vom 01.11.2026", 9)
        for x, t in ((145, "Vertragsnummer"), (224, "Antrags-"), (264, "Beginn-/"), (337, "Risiko-/"), (476, "Rolle"), (573, "Bemessungs-"), (795, "Betrag")):
            put(x, 98, t)
        put(794, 105, "in"), put(801, 105, "Euro"), put(572, 112, "Werbegebühr")

    def put(x, y, text, size=7):
        state["page"].insert_text((x, y), text, fontsize=size, fontname="helv")

    def row(*cells):
        for x, t in cells:
            put(x, state["y"], t)
        state["y"] += 10

    def contract(name, ref, code, amount=None, applied="01.10.26"):
        cells = [(26, name), (145, ref), (224, applied), (264, "01.11.26"), (303, "11"), (337, code), (476, "EVM")]
        row(*cells, *([(794, amount)] if amount else []))

    new_page()
    row((26, "VM-Nr."), (53, "085555B,"), (88, "Laura"), (113, "Beispiel"))
    contract("Muster Max", "808/123456-E-12", "ASS", applied="02.10.26")  # Hauptzeile ohne Betrag
    row((337, "FAB"), (476, "EVM"), (798, "2,00"))
    row((337, "KH"), (476, "EVM"), (595, "99,00"), (753, "3,00"), (794, "85,40"))
    row((337, "FV"), (476, "EVM"), (794, "10,00"))  # echte 10-€-Position
    contract("Probe Erika", "808/222222-C-01", "HC", "40,00", applied="05.10.26")
    contract("Storno Paul", "808/666666-H-01", "KH", "-60,00")
    row((26, "VM-Nr."), (53, "081234A,"), (88, "Dennis"))  # Blockwechsel mitten auf der Seite
    contract("Test Tina", "808/888888-B", "FV", "30,00", applied="07.10.26")
    new_page()  # Block laeuft auf der naechsten Seite weiter (Folgezeile ohne Vertragsnummer)
    row((14, "VM"), (337, "FAB"), (476, "EVM"), (798, "2,00"))
    contract("Zehner Zora", "808/777777-P", "PHPL", "10,00")  # nur 10-€-Position -> kein Neuabschluss
    row((26, "Produktion"), (69, "in"), (77, "Kranken"), (656, "123,45"))
    row((26, "VM-Nr."), (53, "089999Z,"), (88, "Fremd"), (113, "Fritz"))
    contract("Fremd Kunde", "808/999999-U", "UC", "5,00")
    row((26, "VM-Nr."), (53, "080950T,"), (88, "Plattform"))  # Nummer haengt nur am SUPER_ADMIN-Konto
    contract("Plattform Paul", "808/121212-K", "KH", "7,00")
    row((26, "Ihre"), (42, "Werbegebühren"), (783, total))
    data = doc.tobytes()
    doc.close()
    return data


def make_other_pdf() -> bytes:
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((40, 60), "Provisionsabrechnung Oktober 2026   808/123456-E Kfz Abschluss 85,40", fontsize=10)
    data = doc.tobytes()
    doc.close()
    return data


@pytest.fixture(autouse=True)
def storage(app, tmp_path):
    app.config["SETTLEMENT_STORAGE_DIR"] = str(tmp_path / "settlements")


def _upload(client, data, name="huk-oktober.pdf"):
    return client.post("/abrechnungen/einreichen", data={"dateien": (io.BytesIO(data), name)}, content_type="multipart/form-data", follow_redirects=True)


def _records():
    with bypass_tenant_scope():
        return Settlement.query.order_by(Settlement.id).all()


def _rows():
    with bypass_tenant_scope():
        return {r.name: r for r in svc.overview(2026, 10).rows}


def test_parse_blocks_amount_column_and_skipped_totals():
    parsed = svc.parse_huk(make_huk_pdf())
    assert parsed is not None and str(parsed.period_start) == "2026-10-01" and parsed.incomplete == []
    assert [str(line.amount) for line in parsed.lines] == ["2.00", "85.40", "10.00", "40.00", "-60.00", "30.00", "2.00", "10.00", "5.00", "7.00"]
    assert parsed.document_total == Decimal("131.40")
    assert parsed.blocks == {"085555B": "Laura Beispiel", "081234A": "Dennis", "089999Z": "Fremd Fritz", "080950T": "Plattform"}
    assert {line.page for line in parsed.lines} == {2, 3}  # Deckblatt nie
    follow = parsed.lines[6]  # Folgezeile auf Seite 3 gehoert zu Dennis' Vertrag von Seite 2
    assert (follow.page, follow.broker_number, follow.contract_ref) == (3, "081234A", "808/888888-B")
    assert str(follow.booking_date) == "2026-10-07"  # Antragsdatum der Hauptzeile
    first = parsed.lines[0]
    assert first.contract_ref == "808/123456-E-12" and first.sparte == "Kfz" and str(first.booking_date) == "2026-10-02"


def test_overview_per_employee_without_manual_review(app, office):
    _upload(login(app, "laura@example.com"), make_huk_pdf())
    [record] = _records()
    assert record.status == "done" and record.employee_user_id is None
    with bypass_tenant_scope():
        assert not any(line.needs_review for line in record.lines)
        assert not any(line.amount in (Decimal("9256.19"), Decimal("444.44"), Decimal("777.77"), Decimal("123.45")) for line in record.lines)
    rows = _rows()
    laura, dennis, admin = rows["Laura"], rows["Dennis"], rows["Admin A"]
    assert laura.vm == "085555B" and laura.count == 2 and laura.by_sparte == {"Hausrat": 1, "Kfz": 1}
    assert [(c.ref, str(c.amount), str(c.applied_on)) for c in laura.closings] == [("808/123456-E-12", "87.40", "2026-10-02"), ("808/222222-C-01", "40.00", "2026-10-05")]
    assert laura.regular == Decimal("67.40")  # 87,40 + 40,00 - 60,00 (Storno), ohne 10 €
    assert (laura.ten_count, laura.ten_sum) == (1, Decimal("10.00")) and len(laura.cancellations) == 1
    assert dennis.count == 1 and dennis.regular == Decimal("32.00") and (dennis.ten_count, dennis.ten_sum) == (1, Decimal("10.00"))
    assert admin.count == 0 and not admin.has_data  # Buero-Mitglied ohne Abschluesse erscheint
    assert "Justin" not in rows  # SUPER_ADMIN nie als Mitarbeiter
    assert rows["Fremd Fritz"].unassigned and rows["Plattform"].unassigned and rows["Plattform"].regular == Decimal("7.00")

    page = login(app, "admin-a@example.com").get("/abrechnungen?monat=2026-10").get_data(as_text=True)
    for text in ("VM-Nr. 085555B", "Abschlüsse anzeigen (2)", "808/123456-E-12", "87,40 €", "67,40 €", "Kfz: 1", "02.10.2026", "Admin A"):
        assert text in page, text
    for text in ("Bestätigen", "Nicht zählen", "unklare Zeile", "9.256,19", "Auswertung unvollständig"):
        assert text not in page, text


def test_incomplete_shows_hint_and_no_zero_for_missing_data(app, office):
    _upload(login(app, "admin-a@example.com"), make_huk_pdf(total="999,99"))
    [record] = _records()
    assert record.status == "incomplete" and "weicht vom Gesamtbetrag" in record.message
    rows = _rows()
    assert rows["Laura"].count == 2  # gelesene Daten bleiben sichtbar
    page = login(app, "admin-a@example.com").get("/abrechnungen?monat=2026-10").get_data(as_text=True)
    assert "Auswertung unvollständig" in page and "Bestätigen" not in page
    admin_card = page.split('data-abr-row="080001A"')[1].split("</section>")[0]
    assert 'data-abr-count>–<' in admin_card and "Keine vollständigen Daten" in admin_card


def test_non_huk_pdf_is_not_evaluated(app, office):
    _upload(login(app, "admin-a@example.com"), make_other_pdf(), "andere.pdf")
    [record] = _records()
    assert record.status == "failed" and "Keine HUK-Einzelübersicht" in record.message and not record.lines


def test_access_only_own_office_admin(app, office):
    laura = login(app, "laura@example.com")
    html = _upload(laura, make_huk_pdf()).get_data(as_text=True)
    assert "Eingang bestätigt" in html and "85,40" not in html
    [record] = _records()
    for url in ("/abrechnungen?monat=2026-10", f"/abrechnungen/beleg/{record.id}", f"/abrechnungen/beleg/{record.id}/original",
                "/abrechnungen/export.csv?monat=2026-10"):
        resp = laura.get(url)
        assert resp.status_code == 404 or "808/123456" not in resp.get_data(as_text=True), url
    assert laura.post(f"/abrechnungen/beleg/{record.id}/neu-auswerten").status_code == 404
    other = login(app, "admin-b@example.com")
    assert other.get(f"/abrechnungen/beleg/{record.id}").status_code == 404
    assert "808/123456" not in other.get("/abrechnungen?monat=2026-10").get_data(as_text=True)
    admin = login(app, "admin-a@example.com")
    csv = admin.get("/abrechnungen/export.csv?monat=2026-10").get_data(as_text=True)
    assert "808/123456-E-12" in csv and "Neuabschluss" in csv and "10-€-Position" in csv


def test_reevaluation_replaces_legacy_result_in_place(app, office):
    _upload(login(app, "admin-a@example.com"), make_huk_pdf())
    [record] = _records()
    with bypass_tenant_scope():
        # Zustand wie nach dem alten Import: Pruefliste, Deckblattzeilen, keine Details
        record.status, record.details = "needs_review", None
        record.lines = [SettlementLine(tenant_id=record.tenant_id, position=i, page=1, raw_text="alt", amount=Decimal("9256.19"),
                                       needs_review=True) for i in range(5)]
        from app.extensions import db

        db.session.commit()
        record_id = record.id
    assert _rows()["Laura"].has_data is False  # alte Auswertung zaehlt nicht
    admin = login(app, "admin-a@example.com")
    page = admin.get("/abrechnungen").get_data(as_text=True)
    assert "Alte Auswertung – bitte neu auswerten" in page
    resp = admin.post(f"/abrechnungen/beleg/{record_id}/neu-auswerten", follow_redirects=True)
    assert "Neu ausgewertet" in resp.get_data(as_text=True)
    records = _records()
    assert len(records) == 1 and records[0].status == "done"
    with bypass_tenant_scope():
        lines = SettlementLine.query.filter(SettlementLine.settlement_id == record_id).all()
        assert len(lines) == 10 and not any(line.amount == Decimal("9256.19") for line in lines)
    assert _rows()["Laura"].count == 2
    admin.post(f"/abrechnungen/beleg/{record_id}/neu-auswerten")  # erneut: keine Dubletten
    assert len(_records()) == 1
    with bypass_tenant_scope():
        assert SettlementLine.query.filter(SettlementLine.settlement_id == record_id).count() == 10


def test_failed_reevaluation_keeps_previous_result(app, office):
    _upload(login(app, "admin-a@example.com"), make_huk_pdf())
    [record] = _records()
    with bypass_tenant_scope():
        svc.file_path(record).unlink()  # Datei fehlt -> Lesen schlaegt fehl
    admin = login(app, "admin-a@example.com")
    resp = admin.post(f"/abrechnungen/beleg/{record.id}/neu-auswerten", follow_redirects=True)
    assert "bisherige Ergebnis bleibt unverändert" in resp.get_data(as_text=True)
    [same] = _records()
    assert same.status == "done"
    with bypass_tenant_scope():
        assert SettlementLine.query.filter(SettlementLine.settlement_id == record.id).count() == 10


def test_duplicate_content_is_not_counted_twice(app, office):
    laura = login(app, "laura@example.com")
    data = make_huk_pdf()
    _upload(laura, data)
    assert "bereits eingereicht" in _upload(laura, data, "nochmal.pdf").get_data(as_text=True)
    _upload(login(app, "admin-a@example.com"), make_huk_pdf(cover_amount="1,00"), "kopie.pdf")  # andere Datei, gleicher Inhalt
    records = _records()
    assert len(records) == 2 and records[1].status == "duplicate"
    assert _rows()["Laura"].count == 2


def test_chat_question_without_model(app, office, monkeypatch):
    from app.services.ai import router

    monkeypatch.setattr(router, "complete", lambda *a, **k: (_ for _ in ()).throw(AssertionError("kein Modellaufruf")))
    _upload(login(app, "laura@example.com"), make_huk_pdf())
    data = _input(login(app, "admin-a@example.com"), "Wie viel hat Laura im Oktober verdient?")
    assert data["kind"] == "facts" and "77,40 €" in data["message"] and "2 Neuabschlüsse" in data["lines"][0]
    denied = _input(login(app, "laura@example.com"), "Wie viel hat Laura im Oktober verdient?")
    assert "ausschließlich der Büro-Admin" in denied["message"]
