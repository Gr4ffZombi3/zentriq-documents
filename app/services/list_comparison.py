"""Vergleicht eine neu verarbeitete Leipziger Liste gegen ein vorheriges Dokument und
protokolliert Aenderungen pro Kunde dauerhaft (ListComparison + ListComparisonEntry). Zwei
Vergleichsarten (siehe ComparisonKind): TEMPORAL (Default, bisheriges Verhalten - zeitbasiert
gegen das zuletzt verarbeitete Leipziger-Liste-Dokument desselben Tenants) und OWN_VS_GS (M13 -
Eigene Liste gegen Geschäftsstellen-Liste, expliziter previous_document-Override). Rein
additiv - beruehrt weder die Extraktion noch die Recommendation-/Task-Erzeugung."""

from datetime import date, datetime

from sqlalchemy.orm import load_only

from app.extensions import db
from app.models import CustomerTimelineEvent, Document, ListComparison, ListComparisonEntry
from app.models.enums import ComparisonKind, DocStatus, DocType, ListChangeType, ListScope, TimelineEventType
from app.services.customer_normalization import normalize_customer_name, normalize_postal_code
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


def _comparison_customer_key(doc_customer) -> tuple:
    customer = doc_customer.customer
    row_data = doc_customer.row_data or []
    first_row_customer = row_data[0].get("customer") if row_data and isinstance(row_data[0], dict) else {}
    name = (
        (customer.name if customer is not None else None)
        or (first_row_customer.get("name") if isinstance(first_row_customer, dict) else None)
        or ""
    )
    normalized_name = normalize_customer_name(name)

    date_of_birth = customer.date_of_birth if customer is not None else None
    if date_of_birth is None and isinstance(first_row_customer, dict):
        date_of_birth = first_row_customer.get("date_of_birth")
    if date_of_birth:
        return ("dob", normalized_name, str(date_of_birth))

    postal_code = customer.postal_code if customer is not None else None
    if not postal_code and isinstance(first_row_customer, dict):
        postal_code = first_row_customer.get("postal_code")
    normalized_postal_code = normalize_postal_code(postal_code)
    if normalized_postal_code:
        return ("postal", normalized_name, normalized_postal_code)

    return ("name", normalized_name)


def _group_document_customers(document_customers) -> dict[tuple, dict]:
    grouped: dict[tuple, dict] = {}
    for doc_customer in document_customers:
        key = _comparison_customer_key(doc_customer)
        record = grouped.get(key)
        if record is None:
            grouped[key] = {
                "customer_id": doc_customer.customer_id,
                "doc_customer": doc_customer,
                "row_data": [*(doc_customer.row_data or [])],
            }
            continue
        record["row_data"] = [*record["row_data"], *(doc_customer.row_data or [])]
    return grouped


def list_report_date(document: Document) -> date | None:
    """Berichtsdatum aus dem Listenkopf (beim Import gespeichert), sonst None."""
    value = ((document.extra_data or {}).get("leipziger_analysis") or {}).get("report_date")
    try:
        return date.fromisoformat(value) if value else None
    except (TypeError, ValueError):
        return None


def _list_order_key(document: Document) -> tuple:
    """Reihenfolge der Listen: Berichtsdatum (Berichtsjahr/Kalenderwoche), ohne erkanntes
    Berichtsdatum ersatzweise der Upload-Tag. Eine spaeter hochgeladene aeltere Woche wird so
    korrekt einsortiert, statt gegen die neuere Liste verglichen zu werden."""
    uploaded_at = (document.uploaded_at or datetime.min).replace(tzinfo=None)
    return (list_report_date(document) or uploaded_at.date(), uploaded_at, document.id or 0)


def _comparable_lists(document: Document) -> list[Document]:
    return (
        Document.query.options(
            load_only(Document.id, Document.uploaded_at, Document.extra_data, Document.original_filename)
        )
        .filter(
            Document.doc_type == DocType.LEIPZIGER_LISTE,
            Document.status == DocStatus.DONE,
            Document.id != document.id,
        )
        .all()
    )


def find_previous_list(document: Document) -> Document | None:
    """Die unmittelbar vorangehende, fertig ausgewertete Liste desselben Bueros."""
    own_key = _list_order_key(document)
    earlier = [other for other in _comparable_lists(document) if _list_order_key(other) < own_key]
    return max(earlier, key=_list_order_key, default=None)


def find_next_list(document: Document) -> Document | None:
    """Die unmittelbar folgende, fertig ausgewertete Liste desselben Bueros."""
    own_key = _list_order_key(document)
    later = [other for other in _comparable_lists(document) if _list_order_key(other) > own_key]
    return min(later, key=_list_order_key, default=None)


def find_paired_gs_or_own_document(document: Document) -> Document | None:
    """Findet das juengste Leipziger-Liste-Dokument des jeweils ENTGEGENGESETZTEN list_scope
    desselben Tenants - Grundlage fuer den M13-Eigene-Liste-vs-GS-Liste-Vergleich. Gibt None
    zurueck, wenn document.list_scope noch nicht gesetzt ist oder kein Gegenstueck existiert."""
    if document.list_scope is None:
        return None
    opposite_scope = ListScope.GESCHAEFTSSTELLE if document.list_scope == ListScope.OWN else ListScope.OWN
    return (
        Document.query.filter(
            Document.doc_type == DocType.LEIPZIGER_LISTE,
            Document.status == DocStatus.DONE,
            Document.list_scope == opposite_scope,
            Document.id != document.id,
        )
        .order_by(Document.uploaded_at.desc())
        .first()
    )


def compare_leipziger_liste(
    document: Document,
    previous_document: Document | None = None,
    comparison_kind: ComparisonKind = ComparisonKind.TEMPORAL,
) -> ListComparison | None:
    # Idempotenz bei Retry: eine vorherige Vergleichs-Auswertung fuer genau dieses Dokument UND
    # diese Vergleichsart darf nicht dupliziert werden - nach comparison_kind skopiert, damit
    # ein OWN_VS_GS-Lauf nicht versehentlich den TEMPORAL-Vergleich desselben Dokuments loescht
    # (oder umgekehrt). Bulk-delete umgeht ORM-Cascades, daher Entries zuerst explizit loeschen,
    # dann die Kopf-Zeile.
    existing_ids = [
        c.id
        for c in ListComparison.query.filter_by(document_id=document.id, comparison_kind=comparison_kind).all()
    ]
    if existing_ids:
        ListComparisonEntry.query.filter(ListComparisonEntry.list_comparison_id.in_(existing_ids)).delete(
            synchronize_session=False
        )
        ListComparison.query.filter(ListComparison.id.in_(existing_ids)).delete(synchronize_session=False)

    if previous_document is None:
        previous_document = find_previous_list(document)
    if previous_document is None:
        return None

    new_by_customer = _group_document_customers(document.document_customers)
    previous_by_customer = _group_document_customers(previous_document.document_customers)

    comparison = ListComparison(
        tenant_id=document.tenant_id,
        document_id=document.id,
        previous_document_id=previous_document.id,
        comparison_kind=comparison_kind,
    )
    db.session.add(comparison)

    counters = dict.fromkeys(_COUNTER_FIELD_BY_CHANGE_TYPE.values(), 0)

    def add_entry(record: dict, change_type: ListChangeType, details: dict) -> None:
        entry = ListComparisonEntry(
            tenant_id=document.tenant_id,
            list_comparison=comparison,
            customer_id=record["customer_id"],
            change_type=change_type,
            details=details,
        )
        db.session.add(entry)
        counters[_COUNTER_FIELD_BY_CHANGE_TYPE[change_type]] += 1

        doc_customer = record["doc_customer"]
        if doc_customer is not None:
            log_timeline_event(
                doc_customer.customer,
                TimelineEventType.LIST_COMPARISON_CHANGE,
                _ENTRY_LABELS[change_type],
                document=document,
                occurred_at=document.uploaded_at,
                extra_data=details,
            )

    # Beide Listen sind demselben Kundenstamm zugeordnet: dieselbe Kunden-ID ist derselbe Kunde,
    # auch wenn eine Liste z. B. kein Geburtsdatum oder eine andere Schreibweise enthaelt.
    # Sonst (Zeilen ohne eindeutige Zuordnung) wie bisher ueber Name + Geburtsdatum/PLZ.
    previous_key_by_customer_id = {
        record["customer_id"]: key for key, record in previous_by_customer.items() if record["customer_id"]
    }
    matched_previous_keys: set[tuple] = set()

    for customer_key, doc_customer in new_by_customer.items():
        new_signature = _row_signature(doc_customer["row_data"])
        previous_key = previous_key_by_customer_id.get(doc_customer["customer_id"])
        if previous_key is None or previous_key in matched_previous_keys:
            previous_key = customer_key if customer_key not in matched_previous_keys else None
        previous_doc_customer = previous_by_customer.get(previous_key) if previous_key else None
        if previous_doc_customer is not None:
            matched_previous_keys.add(previous_key)

        if previous_doc_customer is None:
            add_entry(doc_customer, ListChangeType.NEW_CUSTOMER, {"new": new_signature})
            continue

        old_signature = _row_signature(previous_doc_customer["row_data"])

        if new_signature["is_storno"] and not old_signature["is_storno"]:
            add_entry(doc_customer, ListChangeType.STORNO, {"old": old_signature, "new": new_signature})
        elif set(new_signature["contract_numbers"]) - set(old_signature["contract_numbers"]):
            add_entry(doc_customer, ListChangeType.NEW_CONTRACT, {"old": old_signature, "new": new_signature})
        elif new_signature["is_angebot"] and not old_signature["is_angebot"]:
            add_entry(doc_customer, ListChangeType.NEW_OFFER, {"old": old_signature, "new": new_signature})
        elif new_signature != old_signature:
            add_entry(doc_customer, ListChangeType.STATUS_CHANGE, {"old": old_signature, "new": new_signature})

        # M12: unabhaengig von der obigen Kette - eine neue Sparte kann zusaetzlich zu einem
        # der obigen Aenderungstypen auftreten, nicht nur anstelle davon.
        added_product_lines = _product_lines(new_signature) - _product_lines(old_signature)
        if added_product_lines:
            add_entry(
                doc_customer,
                ListChangeType.NEW_PRODUCT_LINE,
                {"old": old_signature, "new": new_signature, "added_products": sorted(added_product_lines)},
            )

    for customer_key, previous_doc_customer in previous_by_customer.items():
        if customer_key not in matched_previous_keys:
            add_entry(previous_doc_customer, ListChangeType.REMOVED_CUSTOMER, {"old": _row_signature(previous_doc_customer["row_data"])})

    for field, value in counters.items():
        setattr(comparison, field, value)

    return comparison


def refresh_following_list(document: Document) -> Document | None:
    """Nach dem (Neu-)Import von `document`: die in der Berichtsreihenfolge folgende Liste
    wird neu gegen `document` verglichen - z. B. wenn KW37 erst nach KW38 hochgeladen wurde
    oder eine aeltere Liste neu eingelesen wird. `document` wird ausdruecklich als Vorgaenger
    uebergeben, weil es waehrend der eigenen Auswertung noch nicht als fertig markiert ist."""
    following = find_next_list(document)
    if following is None:
        return None
    previous = find_previous_list(following)
    if previous is not None and _list_order_key(previous) > _list_order_key(document):
        return None  # zwischen beiden liegt eine weitere Liste - deren Vergleich bleibt gueltig
    recompare_list(following, previous_document=document)
    return following


def recompare_list(document: Document, previous_document: Document | None = None) -> ListComparison | None:
    """Berechnet alle Vergleiche einer bereits ausgewerteten Liste neu (zeitlich und - falls
    vorhanden - Eigene/GS-Liste), ohne Kunden oder Vorgaenge anzufassen. Die zugehoerigen
    Verlaufseintraege des alten Vergleichs werden ersetzt, nicht verdoppelt."""
    from app.services.leipziger_entries import compare_with_previous

    previous_document = previous_document or find_previous_list(document)
    CustomerTimelineEvent.query.filter(
        CustomerTimelineEvent.document_id == document.id,
        CustomerTimelineEvent.event_type == TimelineEventType.LIST_COMPARISON_CHANGE,
    ).delete(synchronize_session=False)
    # Ohne vorherige Liste entfernt compare_leipziger_liste nur den alten Vergleich.
    comparison = compare_leipziger_liste(document, previous_document=previous_document)
    meta = (document.extra_data or {}).get("leipziger_analysis")
    if meta is not None:
        document.extra_data = {
            **document.extra_data,
            "leipziger_analysis": {
                **meta,
                "import_comparison": compare_with_previous(document, previous=previous_document),
            },
        }
    paired = find_paired_gs_or_own_document(document)
    if paired is not None:
        compare_leipziger_liste(document, previous_document=paired, comparison_kind=ComparisonKind.OWN_VS_GS)
    return comparison
