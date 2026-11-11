"""Kfz-Versicherungsschein auf dem eigenen Rechner auslesen (Auftragsart "kfz_policy_extract").

Die Zentriq-eigene Erkennung (policy.local_extract) liegt sofort vor; das lokale Modell liest die
Seitenbilder zusaetzlich aus. Sein Ergebnis wird mit der Zentriq-Erkennung zusammengefuehrt -
Widersprueche bleiben leer und werden markiert. Kein Rueckfall auf Cloud-Modelle."""

from __future__ import annotations

import logging

from app.extensions import db
from app.models.huk_quote import HukQuoteJob
from app.services.huk import policy, schema

log = logging.getLogger(__name__)


def _quote(job) -> HukQuoteJob | None:
    if job.origin_type != "huk_quote_job" or not job.origin_id:
        return None
    quote = HukQuoteJob.query.filter(HukQuoteJob.id == job.origin_id).first()
    if quote is None or not quote.comparison or (quote.comparison.get("local") or {}).get("job_id") != job.id:
        return None
    return quote


def _set_local(quote: HukQuoteJob, **changes) -> dict:
    comparison = dict(quote.comparison)
    local = dict(comparison.get("local") or {})
    local.update(changes)
    comparison["local"] = local
    quote.comparison = comparison
    return comparison


def on_done(job) -> None:
    quote = _quote(job)
    if quote is None or quote.status != "draft" or quote.comparison["local"].get("state") != "pending":
        return
    result = policy.apply_local_result(quote.comparison["local"].get("found") or {}, (job.result or {}).get("text") or "")
    if result is None:
        _set_local(quote, state="failed", message="Die Antwort des lokalen Modells war unvollständig.", found=None)
        db.session.commit()
        return
    # Vorhandene Werte (z. B. Geburtsdatum/PLZ aus der Kundenakte) bleiben; Dokumentwerte werden neu gesetzt.
    values = {k: v for k, v in (quote.input_data or {}).items() if k not in policy.DOCUMENT_FIELDS}
    values.update(result["values"])
    quote.input_data = schema.normalize(values)
    comparison = _set_local(quote, state="done", model=(job.result or {}).get("model"), found=None)
    comparison.update(previous=result["previous"], flags=result["flags"], extracted=sorted(result["values"]), model=result["model"])
    quote.comparison = dict(comparison)
    db.session.commit()
    log.info("huk.policy.local_applied quote_id=%s fields=%s flags=%s", quote.id, len(result["values"]), len(result["flags"]))


def on_fail(job) -> None:
    quote = _quote(job)
    if quote is None or quote.comparison["local"].get("state") != "pending":
        return
    reason = job.message or ("abgebrochen" if job.status == "cancelled" else "unbekannter Fehler")
    if job.status == "cancelled":
        _set_local(quote, state="skipped", message=reason[:240], found=None)
    else:
        # Zentriq-Erkennung bleibt fuer "Erneut versuchen" erhalten.
        _set_local(quote, state="failed", message=reason[:240])
    db.session.commit()


def on_requeue(job) -> None:
    quote = _quote(job)
    if quote is not None and quote.comparison["local"].get("state") == "failed" and quote.status == "draft":
        _set_local(quote, state="pending", message=None)
        db.session.commit()
