# ruff: noqa: F811  (Fixture "world" wird aus test_permissions_security importiert)
"""Doppelte Kundenakten und Listenreihenfolge (Regression KW29/KW37/KW38_Heller.pdf).

- Umlaute: OCR liest "Löwenstein" als "Lowenstein" - derselbe Kunde.
- OCR-Lesefehler im Geburtsdatum: gleicher Name + dieselbe Vorgangsnummer = derselbe Kunde.
- Unklare Faelle (gleicher Name und PLZ, anderes Geburtsdatum) werden nicht zusammengelegt,
  sondern zur Pruefung angezeigt.
- Listen werden nach Berichtsdatum (Kalenderwoche) verglichen, nicht nach Upload-Zeitpunkt.
- Kundenanlage laeuft unter der Kundensperre des Bueros."""

from datetime import date, datetime, timedelta, timezone

import fitz
import pytest

from app.models import (
    Customer,
    CustomerTimelineEvent,
    DocStatus,
    Document,
    ListComparison,
    ListComparisonEntry,
)
from app.models.enums import ComparisonKind, DocType, TimelineEventType
from app.services import customer_lock
from app.services.customer_duplicates import duplicate_reason, find_duplicates, merge_customers
from app.services.leipziger_parser import detect_report_date
from app.services.llm.schemas import (
    DocumentExtraction,
    ExtractedCustomer,
    LeipzigerListeExtraction,
    LeipzigerListeRow,
)
from app.services.memo_customers import MemoAssignmentError, create_customer_from_memo
from app.tasks.document_tasks import process_document
from app.tenancy import use_tenant_id
from app.utils.customer_keys import name_key
from tests.test_kundenstamm import _customers, _import, _row
from tests.test_permissions_security import world  # noqa: F401

HEADER = "*WM312-L* NEUGESCHAEFT  - FAHRZEUGWECHSEL - ANGEBOTE DER VORWOCHE    GS 08     COBURG, {date}     SEITE  64"


# --- Berichtsdatum --------------------------------------------------------------------------


def test_report_date_is_read_from_list_header_only():
    pages = [
        HEADER.format(date="19.07.2026")
        + "\n720/307259-C-14 ANG PH  Max Mustermann 04109 Leipzig GEB.-DAT.: 01.05.1980 BEGINN: 01.08.2026"
    ]
    assert detect_report_date(pages) == date(2026, 7, 19)
    assert date(2026, 7, 19).isocalendar()[1] == 29
    # Ohne Kopfzeile wird kein Datum aus einer Vorgangszeile uebernommen.
    assert detect_report_date([pages[0].split("\n", 1)[1]]) is None


# --- Kundenzuordnung ------------------------------------------------------------------------


def test_umlaut_folding_matches_ocr_spelling():
    assert name_key("Dennis Löwenstein") == name_key("Dennis Loewenstein") == name_key("Dennis Lowenstein")
    assert name_key("Jürgen Müller") == name_key("Jurgen Muller")


def test_ocr_spelling_without_umlaut_reuses_existing_customer(app, world, db):
    _import(db, world.tenant_a, [_row("708/100001-W-14", name="Dennis Lowenstein", dob=None, plz="53474")], "kw29.pdf", day=1)
    _import(db, world.tenant_a, [_row("708/100002-W-14", name="Dennis Löwenstein", dob=date(1993, 5, 31), plz="53474")], "kw37.pdf", day=8)
    matching = [c for c in _customers(world.tenant_a) if name_key(c.name) == name_key("Dennis Löwenstein")]
    assert len(matching) == 1
    assert matching[0].date_of_birth == date(1993, 5, 31)


def test_ocr_birth_date_error_matches_via_same_contract_number(app, world, db):
    _import(db, world.tenant_a, [_row("720/200001-C-14", name="Jochen Leske", dob=date(1968, 3, 14), plz="53506")], "kw29-ocr.pdf", day=1)
    _import(db, world.tenant_a, [_row("720/200001-C-14", name="Jochen Leske", dob=date(1968, 5, 13), plz="53506")], "kw29.pdf", day=2)
    matching = [c for c in _customers(world.tenant_a) if c.name == "Jochen Leske"]
    assert len(matching) == 1
    # Die neueste Liste ist der aktuelle Stand.
    assert matching[0].date_of_birth == date(1968, 5, 13)


def test_same_contract_number_with_other_postcode_stays_separate(app, world, db):
    _import(db, world.tenant_a, [_row("720/200002-C-14", name="Anna Weber", dob=date(1970, 1, 1), plz="53474")], "a.pdf", day=1)
    _import(db, world.tenant_a, [_row("720/200002-C-14", name="Anna Weber", dob=date(1980, 1, 1), plz="10115")], "b.pdf", day=2)
    assert len([c for c in _customers(world.tenant_a) if c.name == "Anna Weber"]) == 2


def test_unclear_birth_date_conflict_is_flagged_for_review_and_can_be_merged(app, world, db):
    _import(db, world.tenant_a, [_row("720/300001-C-14", name="Silvia Beutgen", dob=date(1962, 6, 20), plz="53501")], "a.pdf", day=1)
    _import(db, world.tenant_a, [_row("720/300002-C-14", name="Silvia Beutgen", dob=date(1964, 6, 14), plz="53501")], "b.pdf", day=2)
    first, second = [c for c in _customers(world.tenant_a) if c.name == "Silvia Beutgen"]
    with use_tenant_id(world.tenant_a.id):
        first, second = db.session.get(Customer, first.id), db.session.get(Customer, second.id)
        # Nie automatisch zusammengelegt, aber zur Pruefung angezeigt ...
        assert duplicate_reason(first, second) == "name_postal"
        assert [d.customer.id for d in find_duplicates(first)] == [second.id]
        # ... und nach Pruefung ohne Datenverlust zusammenfuehrbar.
        merge_customers(first, second, world.admin_a)
        assert len([c for c in Customer.query.all() if c.name == "Silvia Beutgen"]) == 1


def test_different_postcode_and_birth_date_is_no_duplicate(app, world, db):
    _import(db, world.tenant_a, [_row("720/300003-C-14", name="Jan Treffer", dob=date(1998, 5, 12), plz="56651")], "a.pdf", day=1)
    _import(db, world.tenant_a, [_row("720/300004-C-14", name="Jan Treffer", dob=date(1960, 1, 1), plz="10115")], "b.pdf", day=2)
    first, second = [c for c in _customers(world.tenant_a) if c.name == "Jan Treffer"]
    with use_tenant_id(world.tenant_a.id):
        assert duplicate_reason(db.session.get(Customer, first.id), db.session.get(Customer, second.id)) is None


# --- Listenvergleich nach Kalenderwoche ----------------------------------------------------


def _upload_week(app, db, tenant, tmp_path, monkeypatch, filename, report_date, rows, uploaded_at):
    pdf_path = tmp_path / filename
    doc = fitz.open()
    page = doc.new_page(width=842, height=595)
    page.insert_text((20, 40), HEADER.format(date=report_date), fontsize=7)
    doc.save(str(pdf_path))
    doc.close()

    extraction = LeipzigerListeExtraction(rows=rows)
    monkeypatch.setattr(
        "app.tasks.document_tasks.extract_document_data",
        lambda raw_text: DocumentExtraction(doc_type=DocType.LEIPZIGER_LISTE),
    )
    monkeypatch.setattr("app.tasks.document_tasks.extract_leipziger_liste_rows", lambda raw_text: extraction)
    document = Document(
        filename=filename,
        original_filename=filename,
        file_path=str(pdf_path),
        status=DocStatus.PENDING,
        tenant_id=tenant.id,
        uploaded_at=uploaded_at,
    )
    db.session.add(document)
    db.session.commit()
    process_document(document.id)
    db.session.expire_all()
    return db.session.get(Document, document.id)


def _row_for(name, contract, plz="53474"):
    return LeipzigerListeRow(customer=ExtractedCustomer(name=name, postal_code=plz), contract_number=contract)


def _temporal(document):
    return ListComparison.query.filter_by(document_id=document.id, comparison_kind=ComparisonKind.TEMPORAL).one_or_none()


def test_lists_are_compared_by_report_week_not_upload_order(app, db, tenant, tmp_path, monkeypatch):
    now = datetime.now(timezone.utc)
    kw38 = _upload_week(
        app, db, tenant, tmp_path, monkeypatch, "KW38_Heller.pdf", "20.09.2026",
        [_row_for("Anna Kunde", "C-1"), _row_for("Clara Kunde", "C-3")], now - timedelta(hours=3),
    )
    kw37 = _upload_week(
        app, db, tenant, tmp_path, monkeypatch, "KW37_Heller.pdf", "13.09.2026",
        [_row_for("Anna Kunde", "C-1"), _row_for("Bernd Kunde", "C-2")], now - timedelta(hours=2),
    )
    assert kw38.status == DocStatus.DONE and kw37.status == DocStatus.DONE
    assert kw37.extra_data["leipziger_analysis"]["report_week"] == 37

    # KW37 wurde spaeter hochgeladen, ist aber die aeltere Liste: kein Vergleich gegen KW38 ...
    assert _temporal(kw37) is None
    # ... stattdessen wird KW38 neu gegen KW37 verglichen.
    comparison = _temporal(kw38)
    assert comparison.previous_document_id == kw37.id
    assert (comparison.new_customer_count, comparison.removed_customer_count) == (1, 1)
    assert kw38.extra_data["leipziger_analysis"]["import_comparison"]["previous_document_id"] == kw37.id

    kw29 = _upload_week(
        app, db, tenant, tmp_path, monkeypatch, "KW29_Heller.pdf", "19.07.2026",
        [_row_for("Anna Kunde", "C-1")], now - timedelta(hours=1),
    )
    db.session.expire_all()
    assert _temporal(kw29) is None
    assert _temporal(kw37).previous_document_id == kw29.id
    assert _temporal(kw38).previous_document_id == kw37.id

    # Neu vergleichen ersetzt die Verlaufseintraege des alten Vergleichs, statt sie zu verdoppeln.
    for document in (kw37, kw38):
        events = CustomerTimelineEvent.query.filter_by(
            document_id=document.id, event_type=TimelineEventType.LIST_COMPARISON_CHANGE
        ).count()
        entries = ListComparisonEntry.query.filter_by(list_comparison_id=_temporal(document).id).count()
        assert events == entries

    # Wiederholtes Einlesen legt keine Kunden doppelt an.
    customers_before = Customer.query.count()
    process_document(kw37.id)
    process_document(kw38.id)
    db.session.expire_all()
    assert Customer.query.count() == customers_before == 3
    assert _temporal(kw38).previous_document_id == kw37.id


# --- Parallele Anlage -----------------------------------------------------------------------


def test_pipeline_matches_customers_only_while_holding_the_customer_lock(app, db, tenant, tmp_path, monkeypatch):
    events = []

    class RecordingLock(customer_lock.NamedLock):
        def acquire(self):
            if not self.held:
                events.append(("acquire", self.name))
            self._connection = object()
            return self

        def release(self):
            if self.held:
                events.append(("release", self.name))
            self._connection = None

    from app.services import customers as customers_module

    original_init = customers_module.CustomerMatcher.__init__

    def recording_init(self, *args, **kwargs):
        events.append(("match", None))
        original_init(self, *args, **kwargs)

    monkeypatch.setattr(customer_lock, "NamedLock", RecordingLock)
    monkeypatch.setattr(
        "app.tasks.document_tasks.customer_creation_lock",
        lambda tenant_id: RecordingLock(f"zentriq-customers-{tenant_id}", 1),
    )
    monkeypatch.setattr(
        "app.tasks.document_tasks.document_processing_lock",
        lambda document_id: RecordingLock(f"zentriq-document-{document_id}", 1),
    )
    monkeypatch.setattr(customers_module.CustomerMatcher, "__init__", recording_init)

    _upload_week(
        app, db, tenant, tmp_path, monkeypatch, "KW37_Heller.pdf", "13.09.2026",
        [_row_for("Anna Kunde", "C-1")], datetime.now(timezone.utc),
    )
    names = [event for event in events if event[0] != "match"]
    customers_lock = f"zentriq-customers-{tenant.id}"
    assert ("acquire", customers_lock) in names and ("release", customers_lock) in names
    acquire_at = events.index(("acquire", customers_lock))
    release_at = events.index(("release", customers_lock))
    match_at = events.index(("match", None))
    assert acquire_at < match_at < release_at
    assert events[0][0] == "acquire" and events[0][1].startswith("zentriq-document-")


def test_memo_customer_creation_reports_busy_lock(app, world, db, monkeypatch):
    class BusyLock:
        def acquire(self):
            raise customer_lock.CustomerLockTimeout("busy")

        def release(self):
            pass

    monkeypatch.setattr("app.services.memo_customers.customer_creation_lock", lambda tenant_id, timeout_seconds: BusyLock())
    with use_tenant_id(world.tenant_a.id):
        with pytest.raises(MemoAssignmentError, match="erneut versuchen"):
            create_customer_from_memo(
                name="Neu Kunde", phone="0171 9998887", customer_number=None, transcript="Test", user=world.admin_a
            )
        assert Customer.query.filter_by(name="Neu Kunde").count() == 0


def test_named_lock_is_noop_without_mysql(app, db):
    lock = customer_lock.customer_creation_lock(1)
    with lock:
        assert lock.held is False


def test_cli_recompares_lists_in_report_order(app, db, tenant, tmp_path, monkeypatch):
    now = datetime.now(timezone.utc)
    kw38 = _upload_week(app, db, tenant, tmp_path, monkeypatch, "KW38.pdf", "20.09.2026", [_row_for("Anna Kunde", "C-1")], now - timedelta(hours=2))
    kw37 = _upload_week(app, db, tenant, tmp_path, monkeypatch, "KW37.pdf", "13.09.2026", [_row_for("Anna Kunde", "C-1")], now - timedelta(hours=1))
    db.session.commit()
    result = app.test_cli_runner().invoke(args=["leipziger-vergleiche-neu", "--tenant", tenant.slug])
    assert result.exit_code == 0, result.output
    assert f"Dokument {kw38.id} (KW38/2026): verglichen mit Dokument {kw37.id}" in result.output
    assert f"Dokument {kw37.id} (KW37/2026): kein Vorgaenger" in result.output
    db.session.expire_all()
    assert _temporal(kw38).previous_document_id == kw37.id and _temporal(kw37) is None
