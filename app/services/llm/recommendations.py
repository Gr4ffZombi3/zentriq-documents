"""Regelbasierte Empfehlungs-Engine: laeuft fuer alle Dokumenttypen, erzeugt aber nur dann
Ergebnisse, wenn die zugrundeliegenden Flags gesetzt sind. Bei generischen Dokumenten (ohne
Leipziger-Liste-Flags) bleibt die Liste daher meist leer; bei Leipziger Listen liefert sie den
vollen Empfehlungssatz aus dem Briefing."""

from app.extensions import db
from app.models import Customer, Document, Recommendation
from app.models.enums import Priority, RecommendationType

LABELS = {
    RecommendationType.CALL_TODAY: "Heute anrufen",
    RecommendationType.PRIORITIZE_VEHICLE_CHANGE: "Fahrzeugwechsel priorisieren",
    RecommendationType.OFFER_LEGAL_PROTECTION: "Rechtsschutz anbieten",
    RecommendationType.OFFER_HOUSEHOLD_INSURANCE: "Hausrat anbieten",
    RecommendationType.CHECK_ACCIDENT_INSURANCE: "Unfallversicherung prüfen",
    RecommendationType.CHECK_SUPPLEMENTARY_HEALTH: "Krankenzusatz prüfen",
}


def build_recommendations(
    *,
    products: list[str],
    vehicle: str | None,
    is_neugeschaeft: bool = False,
    is_fahrzeugwechsel: bool = False,
    cross_sell_opportunity: bool = False,
    priority: Priority = Priority.MEDIUM,
) -> list[tuple[RecommendationType, str, Priority]]:
    products_lower = {p.lower() for p in products}
    results: list[tuple[RecommendationType, str, Priority]] = []

    if is_neugeschaeft:
        results.append((RecommendationType.CALL_TODAY, LABELS[RecommendationType.CALL_TODAY], Priority.HIGH))
    if is_fahrzeugwechsel:
        results.append(
            (
                RecommendationType.PRIORITIZE_VEHICLE_CHANGE,
                LABELS[RecommendationType.PRIORITIZE_VEHICLE_CHANGE],
                Priority.HIGH,
            )
        )
    if cross_sell_opportunity:
        if "rechtsschutz" not in products_lower:
            results.append(
                (RecommendationType.OFFER_LEGAL_PROTECTION, LABELS[RecommendationType.OFFER_LEGAL_PROTECTION], priority)
            )
        if "hausrat" not in products_lower:
            results.append(
                (
                    RecommendationType.OFFER_HOUSEHOLD_INSURANCE,
                    LABELS[RecommendationType.OFFER_HOUSEHOLD_INSURANCE],
                    priority,
                )
            )
        if vehicle and "unfallversicherung" not in products_lower:
            results.append(
                (
                    RecommendationType.CHECK_ACCIDENT_INSURANCE,
                    LABELS[RecommendationType.CHECK_ACCIDENT_INSURANCE],
                    priority,
                )
            )
        if not any("kranken" in p for p in products_lower):
            results.append(
                (
                    RecommendationType.CHECK_SUPPLEMENTARY_HEALTH,
                    LABELS[RecommendationType.CHECK_SUPPLEMENTARY_HEALTH],
                    priority,
                )
            )

    return results


def create_recommendations(document: Document, customer: Customer | None, **flags) -> list[Recommendation]:
    created = []
    for rec_type, label, rec_priority in build_recommendations(**flags):
        recommendation = Recommendation(
            document=document,
            customer=customer,
            type=rec_type,
            label=label,
            priority=rec_priority,
        )
        db.session.add(recommendation)
        created.append(recommendation)
    return created
