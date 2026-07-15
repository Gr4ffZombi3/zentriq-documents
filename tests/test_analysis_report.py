from app.models import Customer, Document, DocumentCustomer, Task
from app.models.enums import DocStatus, DocType, Priority, TaskStatus, TaskType
from app.services.analysis.report import build_analysis_report


def make_document(db, tenant_id, filename="liste.pdf", doc_type=DocType.LEIPZIGER_LISTE):
    document = Document(
        filename=filename,
        original_filename=filename,
        file_path=f"/tmp/{filename}",
        tenant_id=tenant_id,
        doc_type=doc_type,
        status=DocStatus.DONE,
    )
    db.session.add(document)
    db.session.commit()
    return document


def make_customer(db, tenant_id, name):
    customer = Customer(tenant_id=tenant_id, name=name)
    db.session.add(customer)
    db.session.commit()
    return customer


def test_build_analysis_report_computes_deterministic_stats(app, db, tenant):
    document = make_document(db, tenant.id)
    closed = make_customer(db, tenant.id, "Abgeschlossen")
    offer = make_customer(db, tenant.id, "Nur Angebot")
    storno = make_customer(db, tenant.id, "Storno Kunde")

    db.session.add_all(
        [
            DocumentCustomer(
                document=document, customer=closed, tenant_id=tenant.id,
                row_data=[{"is_neugeschaeft": True, "products": ["KFZ"]}],
            ),
            DocumentCustomer(
                document=document, customer=offer, tenant_id=tenant.id,
                row_data=[{"is_angebot": True, "cross_sell_opportunity": True, "products": ["Hausrat"]}],
            ),
            DocumentCustomer(
                document=document, customer=storno, tenant_id=tenant.id,
                row_data=[{"is_storno": True}],
            ),
        ]
    )
    db.session.commit()

    with app.app_context():
        report = build_analysis_report(document)

    assert report["total_customers"] == 3
    assert report["neue_abschluesse"] == 1
    assert report["neue_angebote"] == 1
    assert report["stornos"] == 1
    assert report["abschlussquote"] == round(1 / 3, 2)
    assert report["gesamtbewertung"] == "kritisch"  # Storno vorhanden
    assert report["nicht_bearbeitet"] == 3  # keine Empfehlungen/Aufgaben in diesem Test angelegt
    assert "3 Kunden" in report["kurzfassung"]
    assert report["executive_summary"].startswith(report["kurzfassung"])


def test_build_analysis_report_top_chancen_ranks_by_potential_score(app, db, tenant):
    document = make_document(db, tenant.id)
    high_potential = make_customer(db, tenant.id, "Viel Potenzial")
    low_potential = make_customer(db, tenant.id, "Wenig Potenzial")

    db.session.add_all(
        [
            DocumentCustomer(
                document=document, customer=high_potential, tenant_id=tenant.id,
                row_data=[{
                    "products": ["KFZ", "Hausrat", "Rechtsschutz"],
                    "cross_sell_opportunity": True,
                    "has_multiple_products": True,
                    "priority": "high",
                }],
            ),
            DocumentCustomer(
                document=document, customer=low_potential, tenant_id=tenant.id,
                row_data=[{"products": [], "priority": "low"}],
            ),
        ]
    )
    db.session.commit()

    with app.app_context():
        report = build_analysis_report(document)

    top_names = [entry["customer_name"] for entry in report["top_chancen"]]
    assert top_names[0] == "Viel Potenzial"
    assert "Wenig Potenzial" not in top_names  # score == 0 wird ausgelassen


def test_build_analysis_report_top_risiken_from_advanced_recommendations(app, db, tenant):
    from app.services.documents import apply_leipziger_liste_extraction
    from app.services.llm.schemas import ExtractedCustomer, LeipzigerListeExtraction, LeipzigerListeRow

    document = make_document(db, tenant.id)
    extraction = LeipzigerListeExtraction(
        rows=[LeipzigerListeRow(customer=ExtractedCustomer(name="Risiko Kunde"), is_storno=True)]
    )
    apply_leipziger_liste_extraction(document, extraction)
    db.session.commit()

    with app.app_context():
        report = build_analysis_report(document)

    assert len(report["top_risiken"]) == 1
    assert report["top_risiken"][0]["customer_name"] == "Risiko Kunde"
    assert "Storno" in report["top_risiken"][0]["reason"]


def test_build_analysis_report_non_leipziger_liste_doc_has_zero_stats(app, db, tenant):
    document = make_document(db, tenant.id, filename="rechnung.pdf", doc_type=DocType.RECHNUNG)

    with app.app_context():
        report = build_analysis_report(document)

    assert report["total_customers"] == 0
    assert report["neue_abschluesse"] == 0
    assert report["abschlussquote"] == 0.0
    assert report["top_chancen"] == []


def test_build_analysis_report_counts_open_tasks_and_untouched_customers(app, db, tenant):
    document = make_document(db, tenant.id)
    handled = make_customer(db, tenant.id, "Bearbeitet")
    untouched = make_customer(db, tenant.id, "Unbearbeitet")

    db.session.add_all(
        [
            DocumentCustomer(document=document, customer=handled, tenant_id=tenant.id, row_data=[{}]),
            DocumentCustomer(document=document, customer=untouched, tenant_id=tenant.id, row_data=[{}]),
        ]
    )
    db.session.add(
        Task(
            tenant_id=tenant.id, document=document, customer=handled, type=TaskType.CALL_TODAY,
            title="Anrufen", priority=Priority.HIGH, status=TaskStatus.OPEN,
        )
    )
    db.session.commit()

    with app.app_context():
        report = build_analysis_report(document)

    assert report["offene_vorgaenge"] == 1
    assert report["nicht_bearbeitet"] == 1


def test_narrative_path_used_when_enabled(app, db, tenant, monkeypatch):
    document = make_document(db, tenant.id, doc_type=DocType.RECHNUNG)

    monkeypatch.setattr(
        "app.services.analysis.report._generate_narrative", lambda report: "Kurzer KI-Fließtext."
    )

    with app.app_context():
        app.config["ANALYSIS_NARRATIVE_ENABLED"] = True
        try:
            report = build_analysis_report(document)
        finally:
            app.config["ANALYSIS_NARRATIVE_ENABLED"] = False

    assert report["executive_summary"] == "Kurzer KI-Fließtext."


def test_narrative_failure_falls_back_to_deterministic_text(app, db, tenant, monkeypatch):
    document = make_document(db, tenant.id, doc_type=DocType.RECHNUNG)

    def raise_error(report):
        raise RuntimeError("OpenAI nicht erreichbar")

    monkeypatch.setattr("app.services.analysis.report._generate_narrative", raise_error)

    with app.app_context():
        app.config["ANALYSIS_NARRATIVE_ENABLED"] = True
        try:
            report = build_analysis_report(document)
        finally:
            app.config["ANALYSIS_NARRATIVE_ENABLED"] = False

    assert report["executive_summary"].startswith(report["kurzfassung"])


def test_narrative_disabled_by_default_in_tests(app, db, tenant):
    with app.app_context():
        assert app.config["ANALYSIS_NARRATIVE_ENABLED"] is False
