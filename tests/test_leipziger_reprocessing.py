"""Wiederholte Auswertung einer Leipziger Liste: keine Dubletten, saubere Rollbacks und keine
rohen Datenbankfehler in der Oberflaeche (Regression KW37_Heller.pdf)."""

import importlib.util
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import text

from app.models import (
    AnalysisRun,
    Customer,
    CustomerTimelineEvent,
    DocStatus,
    Document,
    DocumentCustomer,
    ListComparison,
    ListComparisonEntry,
    Task,
)
from app.models.enums import AnalysisRunStatus, ListChangeType
from app.services.document_progress import (
    SAVE_FAILED_MESSAGE,
    UNEXPECTED_FAILURE_MESSAGE,
    build_document_progress,
    public_error_message,
)
from app.services.llm.schemas import ExtractedCustomer, LeipzigerListeRow
from app.tasks.document_tasks import process_document
from tests.test_list_comparison import upload_leipziger_liste

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations" / "versions"


@pytest.fixture()
def enforce_foreign_keys(app, db):
    """SQLite prueft Fremdschluessel nur mit PRAGMA foreign_keys=ON - MariaDB immer. Die
    Test-DB nutzt eine einzige Verbindung, das PRAGMA gilt damit fuer den ganzen Test."""
    db.session.commit()
    db.session.execute(text("PRAGMA foreign_keys=ON"))
    assert db.session.execute(text("PRAGMA foreign_keys")).scalar() == 1
    yield
    db.session.rollback()
    db.session.execute(text("PRAGMA foreign_keys=OFF"))


def _first_rows():
    return [
        LeipzigerListeRow(
            customer=ExtractedCustomer(name="Anna Kunde", postal_code="04109"),
            contract_number="C-100",
            products=["Kfz"],
            is_neugeschaeft=True,
        ),
        LeipzigerListeRow(
            customer=ExtractedCustomer(name="Bernd Kunde", postal_code="04103"), contract_number="C-200"
        ),
    ]


def _second_rows():
    return [
        LeipzigerListeRow(
            customer=ExtractedCustomer(name="Anna Kunde", postal_code="04109"),
            contract_number="C-100",
            products=["Kfz", "Hausrat"],
            is_neugeschaeft=True,
        ),
        LeipzigerListeRow(
            customer=ExtractedCustomer(name="Clara Kunde", postal_code="04105"), contract_number="C-300"
        ),
    ]


def _snapshot():
    return {
        "customers": Customer.query.count(),
        "document_customers": DocumentCustomer.query.count(),
        "comparisons": ListComparison.query.count(),
        "entries": ListComparisonEntry.query.count(),
        "tasks": Task.query.count(),
        "timeline": CustomerTimelineEvent.query.count(),
    }


def _reload(db, document):
    # Der Task laeuft in einem eigenen App-Kontext (eigene Session): veralteten Stand verwerfen.
    db.session.expire_all()
    return db.session.get(Document, document.id)


def _upload_both(app, db, tenant, tmp_path, monkeypatch):
    base_time = datetime.now(timezone.utc) - timedelta(days=1)
    first = upload_leipziger_liste(
        app, db, tenant, tmp_path, monkeypatch, "kw36.pdf", rows=_first_rows(), uploaded_at=base_time
    )
    second = upload_leipziger_liste(
        app,
        db,
        tenant,
        tmp_path,
        monkeypatch,
        "kw37.pdf",
        rows=_second_rows(),
        uploaded_at=base_time + timedelta(hours=1),
    )
    return first, second


def test_new_product_line_value_is_covered_by_migration():
    path = next(MIGRATIONS_DIR.glob("e8f9a0b1c2d3_*.py"))
    spec = importlib.util.spec_from_file_location("listchangetype_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert set(module.NEW_VALUES) == {member.name for member in ListChangeType}


def test_reprocessing_leipziger_liste_creates_no_duplicates(
    app, db, tenant, tmp_path, monkeypatch, enforce_foreign_keys
):
    _first, second = _upload_both(app, db, tenant, tmp_path, monkeypatch)
    assert second.status == DocStatus.DONE

    entries = ListComparisonEntry.query.all()
    change_types = sorted(entry.change_type.value for entry in entries)
    assert change_types == ["new_customer", "new_product_line", "removed_customer", "status_change"]
    before = _snapshot()
    assert before["tasks"] > 0  # Aufgaben samt "Aufgabe erstellt"-Verlauf vorhanden (FK-Fall)

    # Zweimal erneut auswerten (Retry/Neu einlesen) - Ergebnis bleibt identisch.
    for _ in range(2):
        process_document(second.id)
        second = _reload(db, second)
        assert second.status == DocStatus.DONE
        assert _snapshot() == before

    runs = AnalysisRun.query.filter_by(document_id=second.id).all()
    assert [run.status for run in runs] == [AnalysisRunStatus.SUCCEEDED] * 3


def test_failed_comparison_rolls_back_everything(
    app, db, tenant, tmp_path, monkeypatch, enforce_foreign_keys
):
    _first, second = _upload_both(app, db, tenant, tmp_path, monkeypatch)
    customers_before = Customer.query.count()

    from app.services import list_comparison as list_comparison_service

    def _failing_compare(document, **kwargs):
        list_comparison_service.compare_leipziger_liste(document, **kwargs)
        db.session.flush()
        raise RuntimeError("(pymysql.err.DataError) [SQL: INSERT ...] [parameters: ('Clara Kunde', ...)]")

    monkeypatch.setattr("app.tasks.document_tasks.compare_leipziger_liste", _failing_compare)
    process_document(second.id)
    second = _reload(db, second)

    assert second.status == DocStatus.FAILED
    assert second.error_message == SAVE_FAILED_MESSAGE
    progress = build_document_progress(second)
    assert "Clara" not in progress["detail"] and "SQL" not in progress["detail"]
    # Nichts Halbfertiges: keine Zuordnungen, kein Vergleich, kein "Kunde nicht mehr in der Liste".
    assert DocumentCustomer.query.filter_by(document_id=second.id).count() == 0
    assert ListComparison.query.filter_by(document_id=second.id).count() == 0
    assert ListComparisonEntry.query.filter_by(change_type=ListChangeType.REMOVED_CUSTOMER).count() == 0
    assert Customer.query.count() == customers_before

    # Danach erfolgreich wiederholen: Kunden werden wiedererkannt, nicht doppelt angelegt.
    monkeypatch.setattr(
        "app.tasks.document_tasks.compare_leipziger_liste", list_comparison_service.compare_leipziger_liste
    )
    process_document(second.id)
    second = _reload(db, second)
    assert second.status == DocStatus.DONE
    assert Customer.query.count() == customers_before
    assert ListComparison.query.filter_by(document_id=second.id).count() == 1


def test_unexpected_error_marks_document_failed(app, db, tenant, tmp_path, monkeypatch):
    _first, second = _upload_both(app, db, tenant, tmp_path, monkeypatch)

    def _boom(document):
        raise RuntimeError("Cannot delete or update a parent row: tasks_ibfk_4")

    monkeypatch.setattr("app.tasks.document_tasks._reset_previous_results", _boom)
    process_document(second.id)
    second = _reload(db, second)

    assert second.status == DocStatus.FAILED
    assert second.error_message == UNEXPECTED_FAILURE_MESSAGE
    assert build_document_progress(second)["state"] == "failed"
    assert AnalysisRun.query.filter_by(document_id=second.id, status=AnalysisRunStatus.RUNNING).count() == 0


def test_public_error_message_hides_legacy_raw_errors():
    raw = (
        'Speichern der Analyse fehlgeschlagen: (pymysql.err.DataError) (1265, "Data truncated for '
        "column 'change_type' at row 11\") [SQL: INSERT INTO list_comparison_entries ...]"
    )
    assert public_error_message(raw) == SAVE_FAILED_MESSAGE
    partial = "Teilweise ausgewertet - 2 von 3 Seiten verarbeitet"
    assert public_error_message(partial) == partial
    assert public_error_message(None) is None
