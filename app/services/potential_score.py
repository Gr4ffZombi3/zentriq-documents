"""Approximiert Umsatzpotenzial ohne echten Geldwert (es wird an keiner Stelle ein
Preis/Beitrag aus Dokumenten extrahiert). Deterministischer Score aus vorhandenen
Signalen - in der UI stets als 'Potenzial-Score' beschriftet, nie als Euro-Betrag."""

from app.models.enums import PRIORITY_ORDER, Priority


def compute_potential_score(
    *,
    priority: Priority,
    products: list[str],
    cross_sell_opportunity: bool = False,
    has_multiple_products: bool = False,
) -> int:
    score = PRIORITY_ORDER[priority] * 10
    score += len(products) * 5
    if cross_sell_opportunity:
        score += 15
    if has_multiple_products:
        score += 10
    return score
