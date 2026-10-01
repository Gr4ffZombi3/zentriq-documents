"""Leipziger Liste: normalisierte Vorgaenge (leipziger_entries) und Vergleich mit der vorherigen
Liste beim Import ("Neue Eintraege", "Bereits vorhanden", "Geaendert")."""

from collections import namedtuple
from datetime import datetime, timedelta, timezone

from app.models import Document, LeipzigerEntry
from app.services.leipziger_entries import compare_entries
from tests.test_leipziger_parser import (  # noqa: F401
    PAGE_1,
    PAGE_2,
    _upload,
    make_list_pdf,
    no_llm,
    page,
    rec,
)

Row = namedtuple(
    "Row",
    "position contract_number contract_key customer_name customer_key broker_number broker_key status_code product_line start_date",
)


def _row(number, art="PH", status="ANG", start=None, broker="080950T", customer="ANNA", position=0):
    key = "".join(ch for ch in number.upper() if ch.isalnum())
    return Row(position, number, key, customer.title(), customer, broker, broker, status, art, start)


def _upload_list(auth_client, db, pages, name, uploaded_at=None):
    resp = _upload(auth_client, make_list_pdf(pages), name=name)
    assert resp.status_code == 201, resp.get_json()
    document = db.session.get(Document, resp.get_json()["document_id"])
    if uploaded_at is not None:
        document.uploaded_at = uploaded_at
        db.session.commit()
    return document


def test_import_writes_one_entry_per_record_across_pages(app, auth_client, user, db, tmp_path, no_llm):  # noqa: F811
    app.config["UPLOAD_FOLDER"] = str(tmp_path)
    document = _upload_list(auth_client, db, [PAGE_1, PAGE_2], "woche1.pdf")
    entries = LeipzigerEntry.query.filter_by(document_id=document.id).order_by(LeipzigerEntry.position).all()
    assert len(entries) == 10
    # Seitenuebergreifend in PDF-Reihenfolge; drei Vorgaenge desselben Kunden bleiben einzeln.
    assert [e.contract_number for e in entries[:3]] == ["720/307259-C-14", "420/233697-F-01", "720/307259-C-63"]
    assert entries[-1].contract_number == "708/111111-A-14"
    anna = [e for e in entries if e.customer_name == "Anna Beispiel"]
    assert len(anna) == 4
    first = entries[0]
    assert (first.contract_key, first.broker_number, first.broker_key) == ("720307259C14", "08/0950-T", "080950T")
    # Erste Liste: kein Vergleich moeglich.
    assert document.extra_data["leipziger_analysis"]["import_comparison"] is None


def test_second_import_reports_new_unchanged_and_changed(app, auth_client, user, db, tmp_path, no_llm):  # noqa: F811
    app.config["UPLOAD_FOLDER"] = str(tmp_path)
    first = _upload_list(
        auth_client, db, [PAGE_1, PAGE_2], "woche1.pdf", uploaded_at=datetime.now(timezone.utc) - timedelta(days=7)
    )
    page_2_next_week = page(
        65,
        # Unveraendert:
        rec("708/181706-Q-14", "NEU", "PH", "Carl Probe", "53343", "Wachtberg", "19.03.1940", "15.07.2026", "08/0951-A"),
        rec("608/475130-N", "FZW", "KPKW", "Dora Test", "53424", "Remagen", "23.02.1980", "13.07.2026", "08/0951-A"),
        # Geaendert: jetzt mit Beginn-Datum und anderem Status.
        rec("708/111111-A-14", "NEU", "PH", "Anna Beispiel", "50181", "Bedburg", "18.05.1979", "01.08.2026"),
        # Neu:
        rec("999/000001-Z-14", "ANG", "PH", "Emil Neu", "50181", "Bedburg", "01.01.1990", None),
    )
    second = _upload_list(auth_client, db, [PAGE_1, page_2_next_week], "woche2.pdf")

    comparison = second.extra_data["leipziger_analysis"]["import_comparison"]
    assert comparison["previous_document_id"] == first.id
    assert (comparison["new"], comparison["unchanged"], comparison["changed"]) == (1, 9, 1)
    change = comparison["changes"][0]
    assert change["contract_number"] == "708/111111-A-14"
    assert change["fields"]["Status"] == ["ANG", "NEU"]
    assert change["fields"]["Datum"] == [None, "2026-08-01"]

    html = auth_client.get("/leipziger-liste").get_data(as_text=True)
    assert "Neue Einträge" in html and "Bereits vorhanden" in html and "Geändert" in html
    assert "708/111111-A-14" in html


def test_comparison_is_not_shown_to_employees(app, auth_client, employee_client, user, db, tmp_path, no_llm):  # noqa: F811
    app.config["UPLOAD_FOLDER"] = str(tmp_path)
    _upload_list(auth_client, db, [PAGE_1], "woche1.pdf", uploaded_at=datetime.now(timezone.utc) - timedelta(days=7))
    _upload_list(auth_client, db, [PAGE_1, PAGE_2], "woche2.pdf")
    html = employee_client.get("/leipziger-liste").get_data(as_text=True)
    assert "Neue Einträge" not in html


def test_reimport_rebuilds_entries_without_duplicates(app, auth_client, user, db, tmp_path, no_llm):  # noqa: F811
    app.config["UPLOAD_FOLDER"] = str(tmp_path)
    document = _upload_list(auth_client, db, [PAGE_1, PAGE_2], "woche1.pdf")
    assert auth_client.post(f"/leipziger-liste/{document.id}/neu-einlesen").status_code == 302
    assert LeipzigerEntry.query.filter_by(document_id=document.id).count() == 10


def test_compare_pairs_same_number_and_product_line_in_order():
    previous = [_row("1/1", art="PH", position=1), _row("1/1", art="PH", position=2, status="NEU"), _row("2/2", art="RS")]
    current = [
        _row("1/1", art="PH", position=1),
        _row("1/1", art="PH", position=2, status="FZW"),
        _row("1/1", art="WG", position=3),  # gleiche Nummer, andere Sparte: eigener Vorgang
        _row("2/2", art="RS", broker="080951A"),
    ]
    result = compare_entries(current, previous)
    assert (result["new"], result["unchanged"], result["changed"]) == (1, 1, 2)
    fields = [change["fields"] for change in result["changes"]]
    assert {"Status": ["NEU", "FZW"]} in fields
    assert {"Vermittler": ["080950T", "080951A"]} in fields


def test_compare_identifies_rows_without_number_by_customer():
    previous = [_row("", customer="BERTA")]
    current = [_row("", customer="BERTA", start="2026-08-01"), _row("", customer="CARL")]
    result = compare_entries(current, previous)
    assert (result["new"], result["unchanged"], result["changed"]) == (1, 0, 1)
