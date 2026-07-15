from app.models import Customer, Document, DocumentCustomer, Recommendation, Task
from app.models.enums import DocType, Priority, RecommendationType
from app.services.analysis.business_rules import (
    build_advanced_recommendations,
    count_offer_occurrences,
    customer_has_ever_closed,
    offer_followup_explanation,
)
from app.services.documents import apply_leipziger_liste_extraction
from app.services.llm.schemas import ExtractedCustomer, LeipzigerListeExtraction, LeipzigerListeRow


def test_building_without_household_triggers_cross_sell():
    results = build_advanced_recommendations(products=["Gebäude"])
    types = {r[0] for r in results}
    assert RecommendationType.CROSS_SELL_HOUSEHOLD_FROM_BUILDING in types
    entry = next(r for r in results if r[0] == RecommendationType.CROSS_SELL_HOUSEHOLD_FROM_BUILDING)
    assert entry[2] == Priority.MEDIUM
    assert "Gebäudeversicherung" in entry[3]
    assert "Hausratversicherung" in entry[3]


def test_building_with_household_does_not_trigger_cross_sell():
    results = build_advanced_recommendations(products=["Gebäude", "Hausrat"])
    types = {r[0] for r in results}
    assert RecommendationType.CROSS_SELL_HOUSEHOLD_FROM_BUILDING not in types


def test_vehicle_without_liability_triggers_cross_sell():
    results = build_advanced_recommendations(products=[], vehicle="VW Golf")
    types = {r[0] for r in results}
    assert RecommendationType.CROSS_SELL_LIABILITY_FROM_VEHICLE in types


def test_kfz_product_without_liability_triggers_cross_sell():
    results = build_advanced_recommendations(products=["KFZ"])
    types = {r[0] for r in results}
    assert RecommendationType.CROSS_SELL_LIABILITY_FROM_VEHICLE in types


def test_vehicle_with_liability_does_not_trigger_cross_sell():
    results = build_advanced_recommendations(products=["Privathaftpflicht"], vehicle="VW Golf")
    types = {r[0] for r in results}
    assert RecommendationType.CROSS_SELL_LIABILITY_FROM_VEHICLE not in types


def test_multiple_offers_without_closure_triggers_sales_risk():
    results = build_advanced_recommendations(products=[], sibling_offer_count=3, has_closed=False)
    types = {r[0] for r in results}
    assert RecommendationType.SALES_RISK_MULTIPLE_OFFERS_NO_CLOSURE in types
    entry = next(r for r in results if r[0] == RecommendationType.SALES_RISK_MULTIPLE_OFFERS_NO_CLOSURE)
    assert entry[2] == Priority.HIGH
    assert "3 Angebote" in entry[3]


def test_single_offer_does_not_trigger_sales_risk():
    results = build_advanced_recommendations(products=[], sibling_offer_count=1, has_closed=False)
    types = {r[0] for r in results}
    assert RecommendationType.SALES_RISK_MULTIPLE_OFFERS_NO_CLOSURE not in types


def test_multiple_offers_with_closure_does_not_trigger_sales_risk():
    results = build_advanced_recommendations(products=[], sibling_offer_count=3, has_closed=True)
    types = {r[0] for r in results}
    assert RecommendationType.SALES_RISK_MULTIPLE_OFFERS_NO_CLOSURE not in types


def test_storno_triggers_high_priority_recommendation():
    results = build_advanced_recommendations(products=[], is_storno=True)
    types = {r[0] for r in results}
    assert RecommendationType.HIGH_PRIORITY_STORNO in types
    entry = next(r for r in results if r[0] == RecommendationType.HIGH_PRIORITY_STORNO)
    assert entry[2] == Priority.HIGH


def test_no_storno_does_not_trigger_recommendation():
    results = build_advanced_recommendations(products=[], is_storno=False)
    types = {r[0] for r in results}
    assert RecommendationType.HIGH_PRIORITY_STORNO not in types


def test_offer_followup_explanation_matches_briefing_example():
    text = offer_followup_explanation(18, status_changed=False)
    assert text == "Angebot wurde vor 18 Tagen erstellt. Keine Rückmeldung erkannt. Status unverändert."


def test_offer_followup_explanation_status_changed():
    text = offer_followup_explanation(10, status_changed=True)
    assert text == "Angebot wurde vor 10 Tagen erstellt. Keine Rückmeldung erkannt. Status hat sich seither geändert."


def test_offer_followup_explanation_before_response_deadline():
    text = offer_followup_explanation(3)
    assert "Noch keine Frist überschritten." in text


def make_document(db, tenant_id, filename="liste.pdf"):
    document = Document(
        filename=filename, original_filename=filename, file_path=f"/tmp/{filename}", tenant_id=tenant_id
    )
    db.session.add(document)
    db.session.commit()
    return document


def test_count_offer_occurrences_and_customer_has_ever_closed(app, db, tenant):
    document1 = make_document(db, tenant.id, "liste1.pdf")
    customer = Customer(tenant_id=tenant.id, name="Vielfach Angebot")
    db.session.add(customer)
    db.session.commit()

    dc1 = DocumentCustomer(
        document=document1, customer=customer, tenant_id=tenant.id,
        row_data=[{"is_angebot": True}, {"is_angebot": True}],
    )
    db.session.add(dc1)
    db.session.commit()

    assert count_offer_occurrences(customer.id) == 2
    assert customer_has_ever_closed(customer.id) is False

    document2 = make_document(db, tenant.id, "liste2.pdf")
    dc2 = DocumentCustomer(
        document=document2, customer=customer, tenant_id=tenant.id,
        row_data=[{"is_angebot": False, "is_neugeschaeft": True}],
    )
    db.session.add(dc2)
    db.session.commit()

    assert customer_has_ever_closed(customer.id) is True


def test_apply_leipziger_liste_extraction_wires_advanced_rules(app, db, tenant):
    document = Document(
        filename="liste.pdf", original_filename="liste.pdf", file_path="/tmp/liste.pdf", tenant_id=tenant.id
    )
    db.session.add(document)
    db.session.commit()

    extraction = LeipzigerListeExtraction(
        rows=[
            LeipzigerListeRow(
                customer=ExtractedCustomer(name="Storno Kunde"),
                products=["Gebäude"],
                is_storno=True,
            )
        ]
    )
    apply_leipziger_liste_extraction(document, extraction)
    db.session.commit()

    assert document.doc_type == DocType.LEIPZIGER_LISTE

    recommendations = Recommendation.query.join(Customer).filter(Customer.name == "Storno Kunde").all()
    types = {r.type for r in recommendations}
    assert RecommendationType.HIGH_PRIORITY_STORNO in types
    assert RecommendationType.CROSS_SELL_HOUSEHOLD_FROM_BUILDING in types

    storno_rec = next(r for r in recommendations if r.type == RecommendationType.HIGH_PRIORITY_STORNO)
    assert storno_rec.explanation == "Storno erkannt. Hohe Priorität für Rückgewinnungsgespräch."

    storno_task = Task.query.filter_by(recommendation_id=storno_rec.id).one()
    assert storno_task.explanation == storno_rec.explanation
