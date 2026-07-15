"""Automatischer Analysebericht: deterministisch aus bereits berechneten Zahlen komponiert
(Recommendation-/Task-Zaehlungen, potential_score.py) - das ist der garantierte Fallback und
laeuft ohne jeden OpenAI-Aufruf. Optional wird EIN zusaetzlicher GPT-Aufruf gemacht, der
ausschliesslich diese bereits bekannten Fakten in einen kurzen Fliesstext-Absatz umformuliert
(kein neuer Extraktions-Call, strukturell keine neuen Fakten moeglich) - steuerbar ueber
ANALYSIS_NARRATIVE_ENABLED, mit sicherem Fallback auf den deterministischen Text bei jedem
Fehler (gleiches Muster wie app/services/llm/search_parser.py)."""

import json

from flask import current_app

from app.models import Document, Recommendation, Task
from app.models.enums import DocType, Priority, RecommendationType, TaskStatus
from app.services.llm.client import get_openai_client
from app.services.potential_score import compute_potential_score

TOP_N = 5

_RISK_TYPES = (RecommendationType.SALES_RISK_MULTIPLE_OFFERS_NO_CLOSURE, RecommendationType.HIGH_PRIORITY_STORNO)

_PRIORITY_RANK = {"low": 0, "medium": 1, "high": 2}

NARRATIVE_SYSTEM_PROMPT = (
    "Formuliere ausschließlich einen kurzen Fließtext-Absatz (2-4 Sätze) auf Deutsch aus den "
    "folgenden bereits berechneten Fakten. Erfinde KEINE neuen Fakten, nenne nur, was in den "
    "Daten steht. Antworte nur mit dem Fließtext, ohne Anführungszeichen oder zusätzliche "
    "Formatierung."
)


def _customer_row_flags(document: Document) -> dict[int, dict]:
    """customer_id -> zusammengefasste Flags/Signale (any-row-true) fuer dieses Dokument."""
    flags_by_customer: dict[int, dict] = {}
    for doc_customer in document.document_customers:
        rows = doc_customer.row_data or []
        priorities = [r.get("priority", "medium") for r in rows] or ["medium"]
        flags_by_customer[doc_customer.customer_id] = {
            "customer_name": doc_customer.customer.name if doc_customer.customer else None,
            "is_neugeschaeft": any(r.get("is_neugeschaeft") for r in rows),
            "is_angebot": any(r.get("is_angebot") for r in rows),
            "is_storno": any(r.get("is_storno") for r in rows),
            "products": sorted({p for r in rows for p in (r.get("products") or [])}),
            "cross_sell_opportunity": any(r.get("cross_sell_opportunity") for r in rows),
            "has_multiple_products": any(r.get("has_multiple_products") for r in rows),
            "priority": max(priorities, key=lambda p: _PRIORITY_RANK.get(p, 1)),
        }
    return flags_by_customer


def _top_chancen(flags_by_customer: dict[int, dict]) -> list[dict]:
    scored = []
    for customer_id, flags in flags_by_customer.items():
        score = compute_potential_score(
            priority=Priority(flags["priority"]),
            products=flags["products"],
            cross_sell_opportunity=flags["cross_sell_opportunity"],
            has_multiple_products=flags["has_multiple_products"],
        )
        if score > 0:
            scored.append({"customer_id": customer_id, "customer_name": flags["customer_name"], "score": score})
    scored.sort(key=lambda entry: entry["score"], reverse=True)
    return scored[:TOP_N]


def _top_risiken(document: Document) -> list[dict]:
    recommendations = Recommendation.query.filter(
        Recommendation.document_id == document.id, Recommendation.type.in_(_RISK_TYPES)
    ).all()
    return [
        {
            "customer_id": rec.customer_id,
            "customer_name": rec.customer.name if rec.customer else None,
            "reason": rec.explanation or rec.label,
        }
        for rec in recommendations[:TOP_N]
    ]


def _count_untouched_customers(document: Document, flags_by_customer: dict[int, dict]) -> int:
    """Kunden dieses Dokuments, fuer die weder eine Empfehlung noch eine Aufgabe entstanden ist."""
    recommended_ids = {
        r.customer_id for r in Recommendation.query.filter_by(document_id=document.id).all() if r.customer_id
    }
    tasked_ids = {t.customer_id for t in Task.query.filter_by(document_id=document.id).all() if t.customer_id}
    touched_ids = recommended_ids | tasked_ids
    return sum(1 for customer_id in flags_by_customer if customer_id not in touched_ids)


def _gesamtbewertung(stornos: int, abschlussquote: float) -> str:
    if stornos > 0:
        return "kritisch"
    if abschlussquote >= 0.5:
        return "positiv"
    return "neutral"


def _deterministic_summary_text(stats: dict) -> tuple[str, str]:
    kurzfassung = (
        f"{stats['neue_abschluesse']} neue Abschlüsse, {stats['neue_angebote']} offene Angebote, "
        f"{stats['stornos']} Stornos bei {stats['total_customers']} Kunden."
    )
    if stats["stornos"] > 0:
        zweiter_satz = "Es besteht Handlungsbedarf bei den erkannten Stornos."
    elif stats["neue_abschluesse"] > 0:
        zweiter_satz = "Die Bearbeitung zeigt positive Ergebnisse."
    else:
        zweiter_satz = "Es gibt aktuell keine neuen Abschlüsse in diesem Dokument."
    return kurzfassung, f"{kurzfassung} {zweiter_satz}"


def build_analysis_report(document: Document) -> dict:
    is_leipziger_liste = document.doc_type == DocType.LEIPZIGER_LISTE
    flags_by_customer = _customer_row_flags(document) if is_leipziger_liste else {}

    total_customers = len(flags_by_customer)
    neue_abschluesse = sum(1 for f in flags_by_customer.values() if f["is_neugeschaeft"])
    neue_angebote = sum(1 for f in flags_by_customer.values() if f["is_angebot"])
    stornos = sum(1 for f in flags_by_customer.values() if f["is_storno"])
    abschlussquote = round(neue_abschluesse / total_customers, 2) if total_customers else 0.0

    stats = {
        "total_customers": total_customers,
        "abschlussquote": abschlussquote,
        "neue_abschluesse": neue_abschluesse,
        "neue_angebote": neue_angebote,
        "stornos": stornos,
        "offene_vorgaenge": Task.query.filter_by(document_id=document.id, status=TaskStatus.OPEN).count(),
        "nicht_bearbeitet": _count_untouched_customers(document, flags_by_customer),
        "empfehlungen_count": Recommendation.query.filter_by(document_id=document.id).count(),
    }
    gesamtbewertung = _gesamtbewertung(stornos, abschlussquote)
    kurzfassung, executive_summary = _deterministic_summary_text(stats)

    report = {
        **stats,
        "gesamtbewertung": gesamtbewertung,
        "kurzfassung": kurzfassung,
        "executive_summary": executive_summary,
        "top_chancen": _top_chancen(flags_by_customer),
        "top_risiken": _top_risiken(document),
    }

    if current_app.config.get("ANALYSIS_NARRATIVE_ENABLED"):
        try:
            narrative = _generate_narrative(report)
        except Exception:
            narrative = None  # sicherer Fallback: deterministischer Text bleibt bestehen
        if narrative:
            report["executive_summary"] = narrative

    return report


def _generate_narrative(report: dict) -> str | None:
    client = get_openai_client()
    facts = {key: value for key, value in report.items() if key != "executive_summary"}
    response = client.chat.completions.create(
        model=current_app.config["ANALYSIS_NARRATIVE_MODEL"],
        messages=[
            {"role": "system", "content": NARRATIVE_SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps(facts, ensure_ascii=False)},
        ],
    )
    text = response.choices[0].message.content
    return text.strip() if text else None


__all__ = ["build_analysis_report"]
