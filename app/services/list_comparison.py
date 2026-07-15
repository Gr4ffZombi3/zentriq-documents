"""Vergleicht eine neu verarbeitete Leipziger Liste gegen die zuletzt verarbeitete
Leipziger Liste desselben Tenants und protokolliert Aenderungen pro Kunde dauerhaft
(ListComparison + ListComparisonEntry). Rein additiv - beruehrt weder die Extraktion
noch die Recommendation-/Task-Erzeugung."""

from app.extensions import db
from app.models import Document, ListComparison, ListComparisonEntry
from app.models.enums import DocStatus, DocType, ListChangeType, TimelineEventType
from app.services.timeline import log_timeline_event

_COUNTER_FIELD_BY_CHANGE_TYPE: dict[ListChangeType, str] = {
    ListChangeType.NEW_CUSTOMER: "new_customer_count",
    ListChangeType.NEW_CONTRACT: "new_contract_count",
    ListChangeType.NEW_OFFER: "new_offer_count",
    ListChangeType.STATUS_CHANGE: "status_change_count",
    ListChangeType.STORNO: "storno_count",
    ListChangeType.REMOVED_CUSTOMER: "removed_customer_count",
    ListChangeType.NEW_PRODUCT_LINE: "new_product_line_count",
}

_ENTRY_LABELS: dict[ListChangeType, str] = {
    ListChangeType.NEW_CUSTOMER: "Neuer Kunde in der Liste",
    ListChangeType.NEW_CONTRACT: "Neuer Vertrag erkannt",
    ListChangeType.NEW_OFFER: "Neues Angebot erkannt",
    ListChangeType.STATUS_CHANGE: "Statusänderung erkannt",
    ListChangeType.STORNO: "Storno erkannt",
    ListChangeType.REMOVED_CUSTOMER: "Kunde nicht mehr in der Liste",
    ListChangeType.NEW_PRODUCT_LINE: "Neue Sparte erkannt",
}


def _row_signature(row_data: list[dict] | None) -> dict:
    rows = row_data or []
    return {
        "is_angebot": any(r.get("is_angebot") for r in rows),
        "is_storno": any(r.get("is_storno") for r in rows),
        "contract_numbers": sorted({r.get("contract_number") for r in rows if r.get("contract_number")}),
        "products": sorted({p for r in rows for p in (r.get("products") or [])}),
        "vehicle": sorted({r.get("vehicle") for r in rows if r.get("vehicle")}),
        # M12: "Sparte" pro Zeile, zusaetzlich zur freien Produktliste - beide fliessen in
        # die Neue-Sparte-Erkennung ein.
        "product_lines": sorted({r.get("product_line") for r in rows if r.get("product_line")}),
    }


def _product_lines(signature: dict) -> set:
    return set(signature["products"]) | set(signature["product_lines"])


def _find_previous_leipziger_liste(document: Document) -> Document | None:
    return (
        Document.query.filter(
            Document.doc_type == DocType.LEIPZIGER_LISTE,
            Document.status == DocStatus.DONE,
            Document.id != document.id,
            Document.uploaded_at < document.uploaded_at,
        )
        .order_by(Document.uploaded_at.desc())
        .first()
    )


def compare_leipziger_liste(document: Document) -> ListComparison | None:
    # Idempotenz bei Retry: eine vorherige Vergleichs-Auswertung fuer genau dieses Dokument
    # darf nicht dupliziert werden. Bulk-delete umgeht ORM-Cascades, daher Entries zuerst
    # explizit loeschen, dann die Kopf-Zeile.
    existing_ids = [c.id for c in ListComparison.query.filter_by(document_id=document.id).all()]
    if existing_ids:
        ListComparisonEntry.query.filter(ListComparisonEntry.list_comparison_id.in_(existing_ids)).delete(
            synchronize_session=False
        )
        ListComparison.query.filter(ListComparison.id.in_(existing_ids)).delete(synchronize_session=False)

    previous_document = _find_previous_leipziger_liste(document)
    if previous_document is None:
        return None

    new_by_customer = {dc.customer_id: dc for dc in document.document_customers}
    previous_by_customer = {dc.customer_id: dc for dc in previous_document.document_customers}

    comparison = ListComparison(
        tenant_id=document.tenant_id, document_id=document.id, previous_document_id=previous_document.id
    )
    db.session.add(comparison)

    counters = dict.fromkeys(_COUNTER_FIELD_BY_CHANGE_TYPE.values(), 0)

    def add_entry(customer_id: int, change_type: ListChangeType, details: dict) -> None:
        entry = ListComparisonEntry(
            tenant_id=document.tenant_id,
            list_comparison=comparison,
            customer_id=customer_id,
            change_type=change_type,
            details=details,
        )
        db.session.add(entry)
        counters[_COUNTER_FIELD_BY_CHANGE_TYPE[change_type]] += 1

        doc_customer = new_by_customer.get(customer_id) or previous_by_customer.get(customer_id)
        if doc_customer is not None:
            log_timeline_event(
                doc_customer.customer,
                TimelineEventType.LIST_COMPARISON_CHANGE,
                _ENTRY_LABELS[change_type],
                document=document,
                occurred_at=document.uploaded_at,
                extra_data=details,
            )

    for customer_id, doc_customer in new_by_customer.items():
        new_signature = _row_signature(doc_customer.row_data)
        previous_doc_customer = previous_by_customer.get(customer_id)

        if previous_doc_customer is None:
            add_entry(customer_id, ListChangeType.NEW_CUSTOMER, {"new": new_signature})
            continue

        old_signature = _row_signature(previous_doc_customer.row_data)

        if new_signature["is_storno"] and not old_signature["is_storno"]:
            add_entry(customer_id, ListChangeType.STORNO, {"old": old_signature, "new": new_signature})
        elif set(new_signature["contract_numbers"]) - set(old_signature["contract_numbers"]):
            add_entry(customer_id, ListChangeType.NEW_CONTRACT, {"old": old_signature, "new": new_signature})
        elif new_signature["is_angebot"] and not old_signature["is_angebot"]:
            add_entry(customer_id, ListChangeType.NEW_OFFER, {"old": old_signature, "new": new_signature})
        elif new_signature != old_signature:
            add_entry(customer_id, ListChangeType.STATUS_CHANGE, {"old": old_signature, "new": new_signature})

        # M12: unabhaengig von der obigen Kette - eine neue Sparte kann zusaetzlich zu einem
        # der obigen Aenderungstypen auftreten, nicht nur anstelle davon.
        added_product_lines = _product_lines(new_signature) - _product_lines(old_signature)
        if added_product_lines:
            add_entry(
                customer_id,
                ListChangeType.NEW_PRODUCT_LINE,
                {"old": old_signature, "new": new_signature, "added_products": sorted(added_product_lines)},
            )

    for customer_id in previous_by_customer:
        if customer_id not in new_by_customer:
            add_entry(customer_id, ListChangeType.REMOVED_CUSTOMER, {"old": _row_signature(previous_by_customer[customer_id].row_data)})

    for field, value in counters.items():
        setattr(comparison, field, value)

    return comparison
