"""Erweiterte, additive Business-Regeln (Cross-Selling nach Sparten-Luecke, Vertriebsrisiko
bei wiederholten Angeboten ohne Abschluss, hohe Prioritaet bei Storno) mit generierter
Begruendung aus echten extrahierten Werten (Template, kein GPT-Call). Beruehrt NICHT
app/services/llm/recommendations.py - build_recommendations()/create_recommendations()
bleiben unveraendert, das ist eine parallele, eigenstaendige Regelschicht."""

from app.extensions import db
from app.models import Customer, Document, DocumentCustomer, Recommendation
from app.models.enums import Priority, RecommendationType

LABELS: dict[RecommendationType, str] = {
    RecommendationType.CROSS_SELL_HOUSEHOLD_FROM_BUILDING: "Hausrat anbieten (Gebäude vorhanden)",
    RecommendationType.CROSS_SELL_LIABILITY_FROM_VEHICLE: "Privathaftpflicht anbieten (KFZ vorhanden)",
    RecommendationType.SALES_RISK_MULTIPLE_OFFERS_NO_CLOSURE: "Vertriebsrisiko: mehrere Angebote ohne Abschluss",
    RecommendationType.HIGH_PRIORITY_STORNO: "Storno - dringend Rückgewinnungsgespräch",
}


def count_offer_occurrences(customer_id: int) -> int:
    """Anzahl aller row_data-Eintraege mit is_angebot=True fuer diesen Kunden, ueber alle
    jemals hochgeladenen Leipziger-Liste-Dokumente hinweg."""
    doc_customers = DocumentCustomer.query.filter_by(customer_id=customer_id).all()
    return sum(1 for dc in doc_customers for row in (dc.row_data or []) if row.get("is_angebot"))


def customer_has_ever_closed(customer_id: int) -> bool:
    doc_customers = DocumentCustomer.query.filter_by(customer_id=customer_id).all()
    return any(row.get("is_neugeschaeft") for dc in doc_customers for row in (dc.row_data or []))


def build_advanced_recommendations(
    *,
    products: list[str],
    vehicle: str | None = None,
    is_storno: bool = False,
    sibling_offer_count: int = 0,
    has_closed: bool = False,
) -> list[tuple[RecommendationType, str, Priority, str]]:
    products_lower = {p.lower() for p in products}
    results: list[tuple[RecommendationType, str, Priority, str]] = []

    if "gebäude" in products_lower and "hausrat" not in products_lower:
        results.append((
            RecommendationType.CROSS_SELL_HOUSEHOLD_FROM_BUILDING,
            LABELS[RecommendationType.CROSS_SELL_HOUSEHOLD_FROM_BUILDING],
            Priority.MEDIUM,
            "Kunde hat Gebäudeversicherung, aber keine Hausratversicherung. Cross-Selling-Potenzial erkannt.",
        ))

    has_kfz = vehicle is not None or "kfz" in products_lower
    if has_kfz and "privathaftpflicht" not in products_lower:
        results.append((
            RecommendationType.CROSS_SELL_LIABILITY_FROM_VEHICLE,
            LABELS[RecommendationType.CROSS_SELL_LIABILITY_FROM_VEHICLE],
            Priority.MEDIUM,
            "Kunde hat Kfz-Versicherung, aber keine Privathaftpflicht. Cross-Selling-Potenzial erkannt.",
        ))

    if sibling_offer_count >= 2 and not has_closed:
        results.append((
            RecommendationType.SALES_RISK_MULTIPLE_OFFERS_NO_CLOSURE,
            LABELS[RecommendationType.SALES_RISK_MULTIPLE_OFFERS_NO_CLOSURE],
            Priority.HIGH,
            f"{sibling_offer_count} Angebote ohne Abschluss erkannt. Vertriebsrisiko.",
        ))

    if is_storno:
        results.append((
            RecommendationType.HIGH_PRIORITY_STORNO,
            LABELS[RecommendationType.HIGH_PRIORITY_STORNO],
            Priority.HIGH,
            "Storno erkannt. Hohe Priorität für Rückgewinnungsgespräch.",
        ))

    return results


def create_advanced_recommendations(document: Document, customer: Customer | None, **flags) -> list[Recommendation]:
    created = []
    for rec_type, label, rec_priority, explanation in build_advanced_recommendations(**flags):
        recommendation = Recommendation(
            document=document,
            customer=customer,
            type=rec_type,
            label=label,
            priority=rec_priority,
            explanation=explanation,
            tenant_id=document.tenant_id,
        )
        db.session.add(recommendation)
        created.append(recommendation)
    return created


def offer_followup_explanation(offer_age_days: int, status_changed: bool = False) -> str:
    """Begruendungstext fuer die bestehende Nachfassen-Empfehlung, gebaut aus echten Werten
    (z.B. aus wiedervorlagen.py), kein GPT-Call. Beispiel aus dem Briefing: "Angebot wurde vor
    18 Tagen erstellt. Keine Rückmeldung erkannt. Status unverändert."."""
    parts = [f"Angebot wurde vor {offer_age_days} Tagen erstellt."]
    parts.append("Keine Rückmeldung erkannt." if offer_age_days >= 7 else "Noch keine Frist überschritten.")
    parts.append("Status hat sich seither geändert." if status_changed else "Status unverändert.")
    return " ".join(parts)
