"""Leipziger Liste (*WM312-L*): deterministischer Import aus dem nativen PDF-Text.

Alle Namen und Nummern sind erfunden; das Zeilenformat entspricht der echten Liste."""

import io
from datetime import date

import fitz
import pytest

from app.models import DocStatus, Document, OcrEngine
from app.models.enums import DocType
from app.services import leipziger_todo
from app.services.leipziger_parser import is_wm312_list, parse_pages
from app.services.ocr.pipeline import extract_text

HEADER = [
    "*WM312-L* NEUGESCHAEFT  - FAHRZEUGWECHSEL - ANGEBOTE DER VORWOCHE    GS 08               COBURG, 19.07.2026         SEITE  {page}",
    "MM  HAT BERUF  STAT. BG-Z-L ARBG. FAM ANZ. PABEZ PABEZ KIND KIND  MM  MM MM MM MM  MM  MM  MM  MM  MM MM  MM MM  MM   MM",
    "OUT TEL IN     IN           IN    IN  KIND  E/L  KIND  <=15 16-24 KFZ RS HR PH WG WOHN KLV RLV BUZ KV UNF BS HKB WERB KFZ",
    "        ZAD    ZAD          ZAD   ZAD ZAD                                          RA  REN                            SOLO",
    " 1   2   3      4      5     6     7   8     9    10    11   12   13  14 15 16 17  18  19  20  21  22 23  24 25  26   27",
]
FLAGS = " J   J   N     RP            N     V   *     J     N     N    N    B   B  B  B  B  1                          1   J    N"


def rec(number, status, art, name, plz, city, birth, start, broker="08/0950-T"):
    return (
        f"{number:<15} {status} {art:<4} {name:<18} {plz} {city:<15} GEB.-DAT.: {birth} "
        f"BEGINN: {start or '':<10} ABO: X VM-NR.: {broker}"
    )


def page(number, *lines):
    return "\n".join([line.format(page=number) for line in HEADER] + list(lines))


PAGE_1 = page(
    64,
    rec("720/307259-C-14", "ANG", "PH", "Anna Beispiel", "50181", "Bedburg", "18.05.1979", None),
    rec("420/233697-F-01", "ANG", "RS", "Anna Beispiel", "50181", "Bedburg", "18.05.1979", None),
    rec("720/307259-C-63", "ANG", "WG", "Anna Beispiel", "50181", "Bedburg", "18.05.1979", None),
    "     J   N                   N         0     N     N     N    N                                                   J    N",
    rec("808/255635-L-15", "NEU", "HR", "Berta Muster", "53343", "Wachtberg", "27.06.1960", "16.07.2026"),
    rec("808/255635-L-16", "NEU", "GL", "Berta Muster", "53343", "Wachtberg", "27.06.1960", "16.07.2026"),
    rec("808/255635-L-63", "NEU", "WG", "Berta Muster", "53343", "Wachtberg", "27.06.1960", "16.07.2026"),
    rec("808/255635-L-14", "NEU", "PH", "Berta Muster", "53343", "Wachtberg", "27.06.1960", "16.07.2026"),
    FLAGS,
)
PAGE_2 = page(
    65,
    # Folgezeile oben auf der Seite gehoert zum letzten Vorgang der Vorseite.
    FLAGS,
    rec("708/181706-Q-14", "NEU", "PH", "Carl Probe", "53343", "Wachtberg", "19.03.1940", "15.07.2026", "08/0951-A"),
    rec("608/475130-N", "FZW", "KPKW", "Dora Test", "53424", "Remagen", "23.02.1980", "13.07.2026", "08/0951-A"),
    # Gleicher Name wie auf Seite 1, andere Vertragsnummer: eigener Vorgang.
    rec("708/111111-A-14", "ANG", "PH", "Anna Beispiel", "50181", "Bedburg", "18.05.1979", None),
)


def test_detects_list_format():
    assert is_wm312_list([PAGE_1, PAGE_2])
    assert not is_wm312_list(["Rechnung Nr. 4711\nBetrag: 120,00 EUR"])


def test_all_pages_every_number_is_one_record():
    result = parse_pages([PAGE_1, PAGE_2])
    assert result.page_count == 2
    assert result.pages_with_records == [1, 2]
    assert [r.number for r in result.records] == [
        "720/307259-C-14",
        "420/233697-F-01",
        "720/307259-C-63",
        "808/255635-L-15",
        "808/255635-L-16",
        "808/255635-L-63",
        "808/255635-L-14",
        "708/181706-Q-14",
        "608/475130-N",
        "708/111111-A-14",
    ]
    assert result.unreadable_count == 0
    assert result.flag_line_count == 3


def test_customer_with_one_and_with_several_records():
    records = parse_pages([PAGE_1, PAGE_2]).records
    by_name = {}
    for record in records:
        by_name.setdefault(record.customer_name, []).append(record)
    assert len(by_name["Carl Probe"]) == 1
    assert [r.art for r in by_name["Berta Muster"]] == ["HR", "GL", "WG", "PH"]
    # Gleicher Kundenname, unterschiedliche Vertragsnummern - nicht zusammengefasst.
    assert len(by_name["Anna Beispiel"]) == 4
    assert len({r.number for r in by_name["Anna Beispiel"]}) == 4


def test_fields_of_a_record():
    record = parse_pages([PAGE_1]).records[3]
    assert (record.number, record.status, record.art) == ("808/255635-L-15", "NEU", "HR")
    assert (record.customer_name, record.postal_code, record.city) == ("Berta Muster", "53343", "Wachtberg")
    assert record.date_of_birth == date(1960, 6, 27)
    assert record.start_date == date(2026, 7, 16)
    assert record.broker_number == "08/0950-T"
    assert (record.page, record.row) == (1, 4)
    assert not record.uncertain


def test_start_date_only_from_own_beginn_field():
    records = parse_pages([PAGE_1]).records
    # Ohne Datum hinter BEGINN: bleibt leer - trotz Geburts- und Beginndaten in Nachbarzeilen.
    assert records[0].start_date is None and records[0].date_of_birth == date(1979, 5, 18)
    assert all(r.start_date == date(2026, 7, 16) for r in records[3:7])
    result = parse_pages([PAGE_1, PAGE_2])
    assert (result.with_date, result.without_date) == (6, 4)


def test_flag_lines_are_not_customers():
    records = parse_pages([PAGE_1, PAGE_2]).records
    assert all(r.customer_name and not r.customer_name.startswith(("J", "N")) for r in records)
    assert all(r.number[0].isdigit() for r in records)


def test_header_on_every_page_is_skipped():
    three_pages = [PAGE_1, PAGE_2, page(66, rec("101/000001-A-01", "NEU", "WG", "Emil Fall", "12345", "Ort", "01.01.1990", "01.08.2026"))]
    result = parse_pages(three_pages)
    assert result.page_count == 3 and len(result.records) == 11
    assert result.unreadable_count == 0


def test_exact_duplicate_line_is_counted_once():
    line = rec("720/307259-C-14", "ANG", "PH", "Anna Beispiel", "50181", "Bedburg", "18.05.1979", None)
    result = parse_pages([page(1, line, line)])
    assert len(result.records) == 1 and result.duplicate_count == 1


def test_same_number_other_art_is_not_a_duplicate():
    result = parse_pages(
        [
            page(
                1,
                rec("708/177651-T-00", "ANG", "HR", "Fritz Doppel", "50667", "Köln", "01.02.1970", None),
                rec("708/177651-T-00", "ANG", "GL", "Fritz Doppel", "50667", "Köln", "01.02.1970", None),
            )
        ]
    )
    assert len(result.records) == 2 and result.duplicate_count == 0


def test_empty_and_badly_extracted_lines():
    broken = page(
        1,
        "",
        "   ",
        rec("720/307259-C-14", "ANG", "PH", "Anna Beispiel", "50181", "Bedburg", "18.05.1979", None),
        "Seitenrest mit unerwartetem Fliesstext",
        # Nummer erkannt, Rest unvollstaendig: Vorgang bleibt erhalten, unsichere Felder leer.
        "999/123456-Z-01 ANG PH   BEGINN: 1x.07.2026",
    )
    result = parse_pages([broken])
    assert len(result.records) == 2
    assert result.unreadable_lines == [(1, 9)]
    uncertain = result.records[1]
    assert uncertain.uncertain and uncertain.start_date is None and uncertain.customer_name is None
    assert "beginn_unlesbar" in uncertain.issues
    assert result.unreadable_count == 2


# --- PDF-Ende-zu-Ende ------------------------------------------------------------------------


def make_list_pdf(pages: list[str]) -> bytes:
    doc = fitz.open()
    for text in pages:
        pdf_page = doc.new_page(width=1000, height=842)
        pdf_page.insert_text((20, 30), text, fontname="cour", fontsize=7)
    data = doc.tobytes()
    doc.close()
    return data


def test_native_text_is_used_without_ocr(app, tmp_path, monkeypatch):
    def fail_ocr(image):
        raise AssertionError("OCR darf bei maschinenlesbarem Text nicht laufen")

    monkeypatch.setattr("app.services.ocr.tesseract_ocr.ocr_image", fail_ocr)
    path = tmp_path / "liste.pdf"
    path.write_bytes(make_list_pdf([PAGE_1, PAGE_2]))
    text, engine, confidence, page_texts = extract_text(str(path))
    assert engine == OcrEngine.NONE and confidence is None
    assert len(page_texts) == 2
    assert len(parse_pages(page_texts).records) == 10


def _upload(client, data: bytes, name="liste.pdf"):
    return client.post(
        "/upload",
        data={"file": (io.BytesIO(data), name), "list_type": "own"},
        content_type="multipart/form-data",
        headers={"X-Requested-With": "XMLHttpRequest"},
    )


@pytest.fixture()
def no_llm(monkeypatch):
    def fail(*args, **kwargs):
        raise AssertionError("Die WM312-Liste darf keine KI-Extraktion ausloesen")

    monkeypatch.setattr("app.tasks.document_tasks.extract_document_data", fail)
    monkeypatch.setattr("app.tasks.document_tasks.extract_leipziger_liste_rows", fail)


def test_upload_imports_every_single_record_and_tabs_add_up(app, auth_client, user, db, tmp_path, no_llm):
    app.config["UPLOAD_FOLDER"] = str(tmp_path)
    resp = _upload(auth_client, make_list_pdf([PAGE_1, PAGE_2]))
    assert resp.status_code == 201
    document = db.session.get(Document, resp.get_json()["document_id"])
    assert document.status == DocStatus.DONE and document.doc_type == DocType.LEIPZIGER_LISTE
    assert document.ocr_engine_used == OcrEngine.NONE

    stats = document.extra_data["leipziger_analysis"]["parser"]
    assert stats["pdf_pages"] == 2 and stats["records"] == 10
    assert (stats["with_date"], stats["without_date"], stats["duplicates"], stats["unreadable"]) == (6, 4, 0, 0)

    entries = leipziger_todo.load_entries(document)
    assert len(entries) == 10
    counts = leipziger_todo.tab_counts(entries)
    assert counts["mit-datum"] + counts["ohne-datum"] == len(entries)
    assert counts == {"zu-erledigen": 4, "mit-datum": 6, "ohne-datum": 4}

    html = auth_client.get("/leipziger-liste?tab=ohne-datum").get_data(as_text=True)
    for header in ("Nummer", "Status", "Kunde", "Art", "Datum", "Vermittler"):
        assert f">{header}</th>" in html
    # Drei Vorgaenge desselben Kunden stehen in PDF-Reihenfolge untereinander.
    positions = [html.index(number) for number in ("720/307259-C-14", "420/233697-F-01", "720/307259-C-63")]
    assert positions == sorted(positions)
    assert html.count("Anna Beispiel") == 4
    assert "10 Vorgänge" in html


def test_reupload_of_same_pdf_is_rejected(app, auth_client, user, tmp_path, no_llm):
    app.config["UPLOAD_FOLDER"] = str(tmp_path)
    data = make_list_pdf([PAGE_1, PAGE_2])
    assert _upload(auth_client, data).status_code == 201
    again = _upload(auth_client, data)
    assert again.status_code == 409
    assert "bereits importiert" in again.get_json()["error"]
    assert Document.query.count() == 1


def test_reimport_replaces_rows_without_duplicates(app, auth_client, user, db, tmp_path, no_llm):
    app.config["UPLOAD_FOLDER"] = str(tmp_path)
    document_id = _upload(auth_client, make_list_pdf([PAGE_1, PAGE_2])).get_json()["document_id"]
    resp = auth_client.post(f"/leipziger-liste/{document_id}/neu-einlesen")
    assert resp.status_code == 302
    document = db.session.get(Document, document_id)
    assert document.status == DocStatus.DONE
    assert len(leipziger_todo.load_entries(document)) == 10
    assert Document.query.count() == 1


def test_reimport_is_admin_only(app, employee_client, auth_client, user, tmp_path, no_llm):
    app.config["UPLOAD_FOLDER"] = str(tmp_path)
    document_id = _upload(auth_client, make_list_pdf([PAGE_1])).get_json()["document_id"]
    assert employee_client.post(f"/leipziger-liste/{document_id}/neu-einlesen").status_code == 403
