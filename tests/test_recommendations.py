from app.models.enums import Priority, RecommendationType
from app.services.llm.recommendations import build_recommendations


def test_no_recommendations_for_plain_document():
    results = build_recommendations(products=["Kfz-Haftpflicht"], vehicle="VW Golf")
    assert results == []


def test_neugeschaeft_and_fahrzeugwechsel_are_always_flagged():
    results = build_recommendations(
        products=[], vehicle=None, is_neugeschaeft=True, is_fahrzeugwechsel=True
    )
    types = {r[0] for r in results}
    assert RecommendationType.CALL_TODAY in types
    assert RecommendationType.PRIORITIZE_VEHICLE_CHANGE in types
    assert all(r[2] == Priority.HIGH for r in results)


def test_cross_sell_opportunity_suggests_missing_products():
    results = build_recommendations(
        products=["Kfz-Haftpflicht"],
        vehicle="VW Golf",
        cross_sell_opportunity=True,
        priority=Priority.MEDIUM,
    )
    types = {r[0] for r in results}
    assert RecommendationType.OFFER_LEGAL_PROTECTION in types
    assert RecommendationType.OFFER_HOUSEHOLD_INSURANCE in types
    assert RecommendationType.CHECK_ACCIDENT_INSURANCE in types
    assert RecommendationType.CHECK_SUPPLEMENTARY_HEALTH in types


def test_cross_sell_skips_products_already_present():
    results = build_recommendations(
        products=["Rechtsschutz", "Hausrat", "Unfallversicherung", "Krankenzusatzversicherung"],
        vehicle="VW Golf",
        cross_sell_opportunity=True,
    )
    assert results == []
