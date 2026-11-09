# ruff: noqa: F811  (Fixtures werden aus anderen Testmodulen importiert)
"""Abrechnungen: Einreichen, Zugriff nur Buero-Admin, Doppel-Upload, mehrere Buchungen je Vertrag,
Stornos, 10-EUR-Auswertung, Filter "Ohne Kfz", unklare Zeilen, interne Frage ohne KI/Web.
Alle Belege sind synthetisch und werden im Test erzeugt."""

import io
import re
from decimal import Decimal

import fitz
import pytest

from app.models import Settlement, SettlementLine
from app.services import settlements as svc
from app.tenancy import bypass_tenant_scope
from tests.test_customer_notes import API, _input, office  # noqa: F401
from tests.test_permissions_security import login, world  # noqa: F401

LAURA_ROWS = [
    "02.10.2026 808/123456-E-12 Kfz Abschlussprovision 85,40",
    "02.10.2026 808/123456-E-12 Kfz Abschlussprovision Nachtrag 12,10",  # gleicher Vertrag -> 1 Abschluss
    "05.10.2026 808/222222-C-01 Hausrat Abschlussprovision 40,00",
    "06.10.2026 808/333333-D-02 Wohngebäude Bestandsprovision 4,20",
    "07.10.2026 808/444444-F-01 Kfz Wechselprämie 10,00",  # 10-EUR-Position, Abschluss belegt
    "08.10.2026 808/555555-G-01 Haftpflicht Bestandsprovision 10,00",  # 10 EUR, kein Abschluss
    "09.10.2026 808/666666-H-01 Kfz Abschlussprovision 60,00",
    "20.10.2026 808/666666-H-01 Kfz Storno Abschlussprovision -60,00",  # storniert
    "21.10.2026 808/777777-K-01 Hausrat Storno Bestandsprovision -10,00",  # negative 10 EUR
    "22.10.2026 808/222222-C-01 Hausrat Korrektur -3,10",
    "23.10.2026 Sonderzahlung Bonus 25,00",  # unklar: keine Referenz/Sparte/Art
    "Summe 163,60",
]


def make_pdf(rows, broker="08/5555-B", period="01.10.2026 - 31.10.2026", scanned=False) -> bytes:
    doc = fitz.open()
    page = doc.new_page(width=595, height=842)
    lines = ["HUK-COBURG Provisionsabrechnung", f"Abrechnungszeitraum: {period}", f"Vermittlernummer: {broker}", ""] + rows
    y = 60
    for line in lines:
        page.insert_text((40, y), line, fontsize=10 if not scanned else 13, fontname="helv")
        y += 18 if not scanned else 24
    if scanned:
        pix = page.get_pixmap(matrix=fitz.Matrix(3, 3))
        scan = fitz.open()
        p2 = scan.new_page(width=595, height=842)
        p2.insert_image(p2.rect, stream=pix.tobytes("png"))
        data = scan.tobytes()
        scan.close()
    else:
        data = doc.tobytes()
    doc.close()
    return data


@pytest.fixture(autouse=True)
def storage(app, tmp_path):
    app.config["SETTLEMENT_STORAGE_DIR"] = str(tmp_path / "settlements")


def _upload(client, data, name="abrechnung-oktober.pdf"):
    return client.post("/abrechnungen/einreichen", data={"dateien": (io.BytesIO(data), name)}, content_type="multipart/form-data", follow_redirects=True)


def _records():
    with bypass_tenant_scope():
        return Settlement.query.order_by(Settlement.id).all()


def test_parse_rules():
    parsed = svc.parse(["Abrechnungszeitraum: 01.10.2026 - 31.10.2026\nVermittlernummer: 08/5555-B\n" + "\n".join(LAURA_ROWS)])
    assert str(parsed.period_start) == "2026-10-01" and parsed.broker_number == "08/5555-B"
    assert len(parsed.lines) == 11  # "Summe" ignoriert
    by_text = {line.raw[:40]: line for line in parsed.lines}
    storno = next(line for line in parsed.lines if "Storno Abschluss" in line.raw)
    assert storno.booking_type == "storno" and storno.amount == Decimal("-60.00")
    assert next(line for line in parsed.lines if "Wechselprämie" in line.raw).is_ten
    bonus = next(line for line in parsed.lines if "Bonus" in line.raw)
    assert bonus.reasons and "keine Vertrags-/Auftragsreferenz" in bonus.reasons[0]
    assert by_text  # noqa


def test_employee_submits_gets_confirmation_but_no_contents(app, office):
    laura = login(app, "laura@example.com")
    resp = _upload(laura, make_pdf(LAURA_ROWS))
    html = resp.get_data(as_text=True)
    assert "Eingang bestätigt" in html and "abrechnung-oktober.pdf" in html
    assert "85,40" not in html and "Verdient" not in html  # keine Inhalte fuer Mitarbeiter
    [record] = _records()
    assert record.status == "needs_review" and record.employee_user_id == office.laura_id
    for url in (f"/abrechnungen/beleg/{record.id}", f"/abrechnungen/beleg/{record.id}/original", "/abrechnungen/buchungen?monat=2026-10",
                "/abrechnungen/export.csv?monat=2026-10"):
        assert laura.get(url).status_code == 404, url


def test_only_own_office_admin_sees_contents(app, office):
    _upload(login(app, "laura@example.com"), make_pdf(LAURA_ROWS))
    [record] = _records()
    admin = login(app, "admin-a@example.com")
    assert admin.get(f"/abrechnungen/beleg/{record.id}/original").headers["Content-Type"] == "application/pdf"
    bob_admin = login(app, "admin-b@example.com")
    assert bob_admin.get(f"/abrechnungen/beleg/{record.id}").status_code == 404
    assert bob_admin.get(f"/abrechnungen/beleg/{record.id}/original").status_code == 404
    assert "Laura" not in bob_admin.get("/abrechnungen?monat=2026-10").get_data(as_text=True)


def test_summary_amounts_orders_sparten_and_ten_block(app, office):
    _upload(login(app, "laura@example.com"), make_pdf(LAURA_ROWS))
    with bypass_tenant_scope():
        [row] = svc.summary(2026, 10)
    # Regulaer (ohne 10er, ohne unklare Bonus-Zeile): 85,40+12,10+40,00+4,20+60,00-60,00-3,10
    assert row.regular == Decimal("138.60")
    assert (row.ten_pos_count, row.ten_pos_sum, row.ten_neg_count, row.ten_neg_sum) == (2, Decimal("20.00"), 1, Decimal("-10.00"))
    assert row.ten_sum == Decimal("10.00") and row.total == Decimal("148.60")
    # Korrekturen: "Nachtrag" +12,10 und "Korrektur" -3,10
    assert row.storno_sum == Decimal("-70.00") and row.korrektur_sum == Decimal("9.00")
    # Auftraege: Abschluss belegt fuer 123456 (2 Zeilen = 1), 222222, 444444 (10er-Wechselpraemie), 666666 (storniert)
    assert row.orders == 4 and row.orders_cancelled == 1
    assert row.sales_by_sparte == {"Hausrat": 1, "Kfz": 2}
    assert row.unclear == 1
    # Filter
    with bypass_tenant_scope():
        [no_ten] = svc.summary(2026, 10, exclude_ten=True)
        [no_kfz] = svc.summary(2026, 10, exclude_sparten=("Kfz",))
    assert no_ten.total == Decimal("138.60") and no_ten.ten_sum == 0
    assert "Kfz" not in no_kfz.sales_by_sparte and no_kfz.regular == Decimal("41.10")
    # Nachvollziehbar: Summe der Buchungen = angezeigter Wert
    admin = login(app, "admin-a@example.com")
    html = admin.get(f"/abrechnungen/buchungen?monat=2026-10&mitarbeiter={office.laura_id}&art=ten").get_data(as_text=True)
    assert "3 Buchung(en), Summe <strong>10,00 €</strong>" in html
    page = admin.get("/abrechnungen?monat=2026-10&ohne=Kfz").get_data(as_text=True)
    assert "Ohne Kfz" in page and "41,10 €" in page


def test_unclear_line_counts_only_after_review(app, office):
    _upload(login(app, "laura@example.com"), make_pdf(LAURA_ROWS))
    admin = login(app, "admin-a@example.com")
    with bypass_tenant_scope():
        line = SettlementLine.query.filter(SettlementLine.raw_text.like("%Bonus%")).one()
    detail = admin.get(f"/abrechnungen/beleg/{line.settlement_id}").get_data(as_text=True)
    assert "Unklare Zeilen (1)" in detail and "Unklar:" not in detail  # Zeilen erst beim Aufklappen
    lazy = admin.get(f"/abrechnungen/beleg/{line.settlement_id}/zeilen?nur=unklar").get_data(as_text=True)
    assert "Unklar:" in lazy and "Seite 1" in lazy
    admin.post(f"/abrechnungen/zeile/{line.id}", data={"aktion": "ok", "sparte": "Kfz", "buchungsart": "sonstig", "referenz": "BONUS-10", "betrag": "25,00"})
    with bypass_tenant_scope():
        [row] = svc.summary(2026, 10)
        assert row.regular == Decimal("163.60") and row.unclear == 0
        assert Settlement.query.one().status == "done"


def test_duplicate_upload_and_rescan_are_not_counted_twice(app, office):
    laura = login(app, "laura@example.com")
    data = make_pdf(LAURA_ROWS)
    _upload(laura, data)
    again = _upload(laura, data, "nochmal.pdf").get_data(as_text=True)
    assert "bereits eingereicht" in again and len(_records()) == 1
    # Gleicher Inhalt, andere Datei (z. B. neu erzeugt/gescannt)
    _upload(login(app, "admin-a@example.com"), make_pdf(LAURA_ROWS + [""]), "kopie.pdf")
    records = _records()
    assert len(records) == 2 and records[1].status == "duplicate" and records[1].duplicate_of_id == records[0].id
    with bypass_tenant_scope():
        [row] = svc.summary(2026, 10)
    assert row.total == Decimal("148.60")


def test_orders_not_determinable_without_closing_bookings(app, office):
    rows = ["03.10.2026 808/333333-D-02 Wohngebäude Bestandsprovision 4,20", "04.10.2026 808/555555-G-01 Haftpflicht Bestandsprovision 10,00"]
    _upload(login(app, "laura@example.com"), make_pdf(rows))
    page = login(app, "admin-a@example.com").get("/abrechnungen?monat=2026-10").get_data(as_text=True)
    assert "data-abr-orders>–</dd>" in page


def test_scanned_pdf_is_read_locally_with_ocr(app, office):
    rows = ["02.10.2026 808/123456-E-12 Kfz Abschlussprovision 85,40", "07.10.2026 808/444444-F-01 Kfz Wechselprämie 10,00"]
    _upload(login(app, "laura@example.com"), make_pdf(rows, scanned=True))
    [record] = _records()
    assert record.text_method == "ocr" and record.broker_number == "08/5555-B"
    amounts = sorted(str(line.amount) for line in record.lines if line.amount is not None)
    assert amounts == ["10.00", "85.40"]


def test_internal_question_without_model_or_web(app, office, monkeypatch):
    from app.services import web_research
    from app.services.ai import router

    def boom(*a, **k):
        raise AssertionError("kein Modell-/Webaufruf erlaubt")

    monkeypatch.setattr(router, "complete", boom)
    monkeypatch.setattr(web_research, "_client", boom)
    _upload(login(app, "laura@example.com"), make_pdf(LAURA_ROWS))
    admin = login(app, "admin-a@example.com")
    data = _input(admin, "Wie viel hat Laura im Oktober verdient?")
    assert data["kind"] == "facts" and "148,60 €" in data["message"] and "Oktober 2026" in data["message"]
    assert "4 Aufträge" in data["lines"][0]
    # Mitarbeiter: keine Abrechnungsdaten
    denied = _input(login(app, "laura@example.com"), "Wie viel hat Laura im Oktober verdient?")
    assert denied["kind"] == "facts" and "ausschließlich der Büro-Admin" in denied["message"]


# --- HUK-Werbegebuehrenabrechnung (Einzelübersicht, Spalte "Betrag in Euro") --------------------


def make_huk_pdf() -> bytes:
    """Synthetischer Beleg im Aufbau der HUK-Werbegebuehrenabrechnung (Querformat)."""
    doc = fitz.open()
    cover = doc.new_page(width=842, height=595)
    for y, text in ((60, "Ihre Werbegebührenabrechnung für den Monat Oktober 2026"), (80, "Abrechnungsmonat: Oktober 2026"),
                    (100, "Betrag 9.256,19 €"), (120, "Übertrag 0,00 €"), (140, "Werbegebühr 01.11.2026 10,00"), (160, "VM-Nr.: 085555B")):
        cover.insert_text((40, y), text, fontsize=10, fontname="helv")
    page = doc.new_page(width=842, height=595)

    def put(x, y, text, size=7):
        page.insert_text((x, y), text, fontsize=size, fontname="helv")

    put(30, 60, "Einzelübersicht der Werbegebührenabrechnung vom 01.11.2026", 9)
    for x, t in ((145, "Vertragsnummer"), (337, "Risiko-/"), (476, "Rolle"), (573, "Bemessungs-"), (795, "Betrag")):
        put(x, 98, t)
    put(794, 105, "in"), put(801, 105, "Euro"), put(572, 112, "Werbegebühr")
    y = 130

    def row(*cells):
        nonlocal y
        for x, t in cells:
            put(x, y, t)
        y += 10

    def contract(name, ref, code, amount, extra=()):
        row((26, name), (145, ref), (224, "01.10.26"), (264, "01.11.26"), (303, "11"), (337, code), (476, "EVM"), *extra, (794, amount))

    row((26, "VM-Nr."), (53, "085555B,"), (88, "Laura"), (113, "Beispiel"))
    contract("Muster Max", "808/123456-E-12", "KH", "85,40", ((595, "99,00"), (689, "12,00"), (753, "1,00")))
    row((337, "FAB"), (476, "EVM"), (798, "10,00"))
    contract("Probe Erika", "808/222222-C-01", "HC", "40,00")
    row((14, "VM"), (337, "FAB"), (476, "EVM"), (798, "10,00"))  # Randtext links in derselben Zeile
    contract("Storno Paul", "808/666666-H-01", "KH", "-60,00")
    row((26, "Produktion"), (69, "in"), (77, "Kranken"), (656, "123,45"))
    row((26, "VM-Nr."), (53, "081234A,"), (88, "Dennis"))
    contract("Test Tina", "808/888888-B", "FV", "30,00")
    row((26, "VM-Nr."), (53, "089999Z,"), (88, "Fremd"))
    contract("Fremd Fritz", "808/999999-U", "UC", "5,00")
    row((26, "VM-Nr."), (53, "080950T,"), (88, "Justin"))  # Nummer haengt (nur) am SUPER_ADMIN-Konto
    contract("Plattform Paul", "808/121212-K", "KH", "7,00")
    row((26, "Ihre"), (42, "Werbegebühren"), (783, "170,40"))
    data = doc.tobytes()
    doc.close()
    return data


def test_huk_uses_only_detail_rows_amount_column_and_block_broker(app, office):
    parsed = svc.parse_huk(make_huk_pdf())
    assert parsed is not None and str(parsed.period_start) == "2026-10-01"
    amounts = [str(line.amount) for line in parsed.lines]
    assert amounts == ["85.40", "10.00", "40.00", "10.00", "-60.00", "30.00", "5.00", "7.00"]  # ohne Deckblatt/Uebertrag/Summen
    assert all(line.page == 2 for line in parsed.lines)
    fab = parsed.lines[1]
    assert fab.contract_ref == "808/123456-E-12" and fab.is_ten and fab.sparte == "Kfz" and fab.broker_number == "085555B"
    assert [line.broker_number for line in parsed.lines][-3:] == ["081234A", "089999Z", "080950T"]

    _upload(login(app, "laura@example.com"), make_huk_pdf(), "huk-oktober.pdf")
    [record] = _records()
    with bypass_tenant_scope():
        assert not any(line.amount in (Decimal("9256.19"), Decimal("0.00")) for line in record.lines)
        rows = {r.name: r for r in svc.summary(2026, 10)}
    laura, dennis, unknown = rows["Laura"], rows["Dennis"], rows["Nicht zugeordnet"]
    assert laura.regular == Decimal("65.40") and laura.ten_pos_count == 2 and laura.ten_sum == Decimal("20.00")
    assert laura.orders == 2 and laura.sales_by_sparte == {"Hausrat": 1, "Kfz": 1}
    assert dennis.regular == Decimal("30.00") and dennis.sales_by_sparte == {"Kfz": 1} and dennis.ten_sum == 0
    assert unknown.unclear == 2  # fremde Nummer + Nummer des Plattformkontos -> erst nach Zuordnung
    with bypass_tenant_scope():
        platform = next(line for line in record.lines if line.broker_number == "080950T")
        assert platform.employee_user_id is None and "keinem Mitarbeiter zugeordnet" in platform.review_reason
    page = login(app, "admin-a@example.com").get("/abrechnungen?monat=2026-10").get_data(as_text=True)
    assert "Vergütung ohne 10-€-Positionen" in page and "65,40 €" in page and "Hausrat: 1" in page
    assert "808/123456-E-12" not in page  # Einzelbuchungen erst in den Details
    lazy = login(app, "admin-a@example.com").get(f"/abrechnungen/buchungen?monat=2026-10&mitarbeiter={office.laura_id}&teil=1").get_data(as_text=True)
    assert "808/123456-E-12" in lazy and "<html" not in lazy


def test_huk_reevaluation_checked_without_duplicates(app, office, monkeypatch):
    # Fehlerhafter Import wie bisher (Zeilen-Parser auch fuer HUK)
    real = svc.parse_huk
    monkeypatch.setattr(svc, "parse_huk", lambda data: None)
    _upload(login(app, "laura@example.com"), make_huk_pdf(), "huk-oktober.pdf")
    [record] = _records()
    with bypass_tenant_scope():
        wrong = [line for line in record.lines if line.page == 1 and line.amount is not None]
        assert {str(line.amount) for line in wrong} >= {"9256.19", "10.00"}
        cover = next(line for line in wrong if line.amount == Decimal("9256.19"))
    admin = login(app, "admin-a@example.com")
    admin.post(f"/abrechnungen/zeile/{cover.id}", data={"aktion": "ignorieren"})  # manuelle Korrektur
    monkeypatch.setattr(svc, "parse_huk", real)

    preview = admin.get(f"/abrechnungen/beleg/{record.id}/neu-auswerten").get_data(as_text=True)
    assert "Vorschau – noch nichts gespeichert" in preview and "9.256,19 €" in preview and "Geprüfte Zeilen ohne Gegenstück" in preview
    with bypass_tenant_scope():
        assert SettlementLine.query.filter(SettlementLine.settlement_id == record.id, SettlementLine.page == 1).count() == len(wrong)
    token = re.search(r'name="token" value="([^"]+)"', preview).group(1)
    # Ohne Bestaetigung wird die manuelle Pruefung nicht still verworfen
    r = admin.post(f"/abrechnungen/beleg/{record.id}/neu-auswerten", data={"token": token}, follow_redirects=True)
    assert "ausdrücklich bestätigen" in r.get_data(as_text=True)
    assert admin.post(f"/abrechnungen/beleg/{record.id}/neu-auswerten", data={"token": "falsch", "verwerfen": "1"}, follow_redirects=True).status_code == 200
    r = admin.post(f"/abrechnungen/beleg/{record.id}/neu-auswerten", data={"token": token, "verwerfen": "1"}, follow_redirects=True)
    assert "Neu ausgewertet: 8 Buchungen" in r.get_data(as_text=True)
    records = _records()
    assert len(records) == 1  # derselbe Beleg, keine Dublette
    with bypass_tenant_scope():
        lines = SettlementLine.query.filter(SettlementLine.settlement_id == record.id).all()
        assert len(lines) == 8 and all(line.page == 2 for line in lines)
        assert {r.name: r.regular for r in svc.summary(2026, 10)}["Laura"] == Decimal("65.40")
        # Pruefung einer neuen Zeile bleibt bei erneuter Neuauswertung erhalten
        unknown = next(line for line in lines if line.broker_number == "089999Z")
    admin.post(f"/abrechnungen/zeile/{unknown.id}", data={"aktion": "ignorieren"})
    preview = admin.get(f"/abrechnungen/beleg/{record.id}/neu-auswerten").get_data(as_text=True)
    assert "1 werden übernommen" in preview and "ohne Gegenstück" not in preview
    token = re.search(r'name="token" value="([^"]+)"', preview).group(1)
    admin.post(f"/abrechnungen/beleg/{record.id}/neu-auswerten", data={"token": token})
    with bypass_tenant_scope():
        again = SettlementLine.query.filter(SettlementLine.settlement_id == record.id, SettlementLine.broker_number == "089999Z").one()
        assert again.review_state == "ignored" and Settlement.query.count() == 1



def test_broker_block_assignment_is_manual_and_survives_reevaluation(app, office):
    _upload(login(app, "laura@example.com"), make_huk_pdf(), "huk-oktober.pdf")
    [record] = _records()
    admin = login(app, "admin-a@example.com")
    page = admin.get(f"/abrechnungen/beleg/{record.id}").get_data(as_text=True)
    assert "VM 080950T" in page and "data-abr-blocks" in page
    # SUPER_ADMIN und fremde Bueros sind nicht waehlbar
    assert admin.post(f"/abrechnungen/beleg/{record.id}/vermittler", data={"vm": "080950T", "mitarbeiter": office.root_id}).status_code == 404
    assert admin.post(f"/abrechnungen/beleg/{record.id}/vermittler", data={"vm": "080950T", "mitarbeiter": office.bob_id}).status_code == 404
    r = admin.post(f"/abrechnungen/beleg/{record.id}/vermittler", data={"vm": "080950T", "mitarbeiter": office.admin_a_id}, follow_redirects=True)
    assert "VM 080950T: 1 Buchungen" in r.get_data(as_text=True)
    with bypass_tenant_scope():
        rows = {row.name: row for row in svc.summary(2026, 10)}
        assert rows["Admin A"].regular == Decimal("7.00") and rows["Nicht zugeordnet"].unclear == 1
    preview = admin.get(f"/abrechnungen/beleg/{record.id}/neu-auswerten").get_data(as_text=True)
    assert "VM 080950T → Admin A" in preview
    token = re.search(r'name="token" value="([^"]+)"', preview).group(1)
    admin.post(f"/abrechnungen/beleg/{record.id}/neu-auswerten", data={"token": token})
    with bypass_tenant_scope():
        line = SettlementLine.query.filter(SettlementLine.broker_number == "080950T").one()
        assert line.employee_user_id == office.admin_a_id and not line.needs_review and Settlement.query.count() == 1
        # Das Mitarbeiterfeld am Beleg ueberschreibt die Block-Zuordnung nicht
    admin.post(f"/abrechnungen/beleg/{record.id}/mitarbeiter", data={"mitarbeiter": office.dennis_id})
    with bypass_tenant_scope():
        rows = {row.name: row for row in svc.summary(2026, 10)}
        assert rows["Laura"].regular == Decimal("65.40") and rows["Admin A"].regular == Decimal("7.00")
