"""Kfz-Berechnungsauftraege (HUK-Webrechner): Entwurf -> Bestaetigung -> Hintergrundlauf.

Zugriff: Buero-Admins sehen alle Auftraege ihres Bueros, Mitarbeiter nur ihre eigenen; andere
Bueros sind ueber den Tenant-Filter (get_or_404_scoped) ausgeschlossen. Personenbezogene Angaben
gehen erst nach der ausdruecklichen Startaktion an huk.de. Doppelte Auftraege verhindern zwei
Sperren: dieselbe Startaktion (client_ref, eindeutig je Benutzer) und hoechstens ein aktiver
Auftrag je Benutzer."""

from __future__ import annotations

import logging
import shutil
import time
from datetime import timedelta
from pathlib import Path

from flask import abort, current_app
from sqlalchemy.exc import IntegrityError

from app.extensions import db
from app.models import Customer
from app.models.huk_quote import ACTIVE_STATUSES, HukQuoteJob, utcnow_naive
from app.services.huk import schema
from app.services.timetracking.clock import local_today
from app.tenancy import get_or_404_scoped

log = logging.getLogger(__name__)

STATUS_LABELS = {
    "draft": "Entwurf",
    "queued": "In Bearbeitung",
    "running": "In Bearbeitung",
    "needs_input": "Angaben erforderlich",
    "done": "Fertig",
    "failed": "Berechnung fehlgeschlagen",
    "cancelled": "Abgebrochen",
}
STATUS_TONES = {
    "draft": "muted", "queued": "info", "running": "info", "needs_input": "warning",
    "done": "success", "failed": "danger", "cancelled": "muted",
}
# Laeuft ein Auftrag laenger als das, ist der Worker abgestuerzt (harte Grenze des Tasks: 300 s).
STALE_AFTER = timedelta(minutes=12)
CLIENT_REF_MAX = 64


class HukJobError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.message = message
        self.status = status


def is_enabled() -> bool:
    return bool(current_app.config.get("HUK_ENABLED", True))


def require_member(user) -> None:
    if not (user.is_office_admin or user.is_employee) or user.tenant_id is None:
        abort(403)


def visible_query(user):
    query = HukQuoteJob.query
    if not user.is_office_admin:
        query = query.filter(HukQuoteJob.created_by_user_id == user.id)
    return query


def get_job(user, job_id: int) -> HukQuoteJob:
    job = get_or_404_scoped(HukQuoteJob, job_id)
    if not user.is_office_admin and job.created_by_user_id != user.id:
        abort(404)
    return job


def recent_jobs(user, limit: int = 30) -> list[HukQuoteJob]:
    expire_stale()
    return visible_query(user).order_by(HukQuoteJob.created_at.desc()).limit(limit).all()


def jobs_for_customer(user, customer_id: int, limit: int = 10) -> list[HukQuoteJob]:
    return (
        visible_query(user)
        .filter(HukQuoteJob.customer_id == customer_id)
        .order_by(HukQuoteJob.created_at.desc())
        .limit(limit)
        .all()
    )


def customer_for(user, customer_id) -> Customer | None:
    """Kunde des eigenen Bueros (Tenant-Filter) oder None."""
    try:
        customer_id = int(customer_id)
    except (TypeError, ValueError):
        return None
    if customer_id <= 0:
        return None
    return Customer.query.filter(Customer.id == customer_id).first()


def prefill_for(user, customer: Customer | None) -> dict:
    """Vorhandene Kundendaten (Geburtsdatum, PLZ) nur fuer Benutzer mit Zugriff auf die
    Kundenakte (Buero-Admins). Mitarbeiter sehen keine Stammdaten und geben selbst ein."""
    if customer is None or not user.is_office_admin:
        return {}
    return schema.from_customer(customer)


def save_draft(user, job: HukQuoteJob | None, raw: dict, customer: Customer | None) -> tuple[HukQuoteJob, list, dict]:
    values = schema.normalize(raw)
    if job is None:
        job = HukQuoteJob(tenant_id=user.tenant_id, status="draft", created_by_user_id=user.id)
        db.session.add(job)
    elif job.status not in ("draft", "needs_input", "failed", "cancelled"):
        raise HukJobError("Dieser Auftrag läuft bereits und kann nicht mehr geändert werden.", 409)
    job.status = "draft"
    job.input_data = values
    job.customer_id = customer.id if customer is not None else None
    if job.comparison and job.comparison.get("manual"):
        # Neue Berechnung: ein frueher manuell eingetragener HUK-Beitrag gilt nicht mehr.
        job.comparison = {k: v for k, v in job.comparison.items() if k != "manual"}
    job.result = None
    job.message = None
    job.error_code = None
    job.confirmed_at = None
    job.cancel_requested = False
    db.session.commit()
    missing, errors = schema.validate(values, local_today())
    return job, missing, errors


def create_comparison(user, values: dict, comparison: dict, customer: Customer | None) -> HukQuoteJob:
    """Entwurf aus einem ausgelesenen Versicherungsschein (nur gueltige Rechnerfelder)."""
    job = HukQuoteJob(tenant_id=user.tenant_id, status="draft", created_by_user_id=user.id,
                      customer_id=customer.id if customer is not None else None,
                      input_data=schema.normalize(values), comparison=comparison)
    db.session.add(job)
    db.session.commit()
    return job


def save_previous(job: HukQuoteJob, form: dict) -> dict[str, str]:
    """Bisheriger Vertrag (Beitrag, Zahlweise, Werkstattbindung) aus dem Formular. Gibt Fehler je Feld zurueck."""
    from app.services.huk import policy

    if job.status not in ("draft", "needs_input", "failed", "cancelled"):
        raise HukJobError("Dieser Auftrag läuft bereits und kann nicht mehr geändert werden.", 409)
    comparison = dict(job.comparison or {})
    previous = dict(comparison.get("previous") or {})
    errors = {}
    raw = (form.get("beitrag") or "").strip()
    amount = policy.parse_amount(raw)
    if raw and amount is None:
        errors["bisher_beitrag"] = "Bitte einen Betrag wie 44,22 eingeben."
    interval = form.get("zahlweise") or None
    if interval not in policy.INTERVALS:
        interval = None
    if amount and not interval:
        errors["bisher_zahlweise"] = "Bitte angeben, für welchen Zeitraum der Beitrag gilt."
    previous["beitrag"] = amount if raw else None
    previous["zahlweise"] = interval
    previous["werkstatt"] = form.get("werkstatt") if form.get("werkstatt") in policy.WORKSHOP else None
    comparison["previous"] = previous
    job.comparison = comparison
    db.session.commit()
    return errors


def save_manual_price(job: HukQuoteJob, form) -> None:
    """HUK-Beitrag von Hand (nach manueller Berechnung auf huk.de), nur wenn kein automatisches Ergebnis vorliegt."""
    from app.services.huk import policy

    if not job.comparison:
        raise HukJobError("Für diese Berechnung gibt es keinen Vergleich.", 400)
    if job.status not in ("failed", "needs_input", "cancelled"):
        raise HukJobError("Ein HUK-Beitrag kann nur eingetragen werden, wenn die automatische Berechnung nicht fertig wurde.", 409)
    amount = policy.parse_amount(form.get("betrag"))
    interval = form.get("zahlweise")
    if amount is None:
        raise HukJobError("Bitte den HUK-Beitrag als Betrag eingeben, z. B. 438,93.", 400)
    if interval not in policy.INTERVALS:
        raise HukJobError("Bitte die Zahlweise des HUK-Beitrags angeben.", 400)
    comparison = dict(job.comparison)
    comparison["manual"] = {
        "betrag": amount, "zahlweise": interval, "tarif": (form.get("tarif") or "").strip()[:60] or None,
        "werkstatt": form.get("werkstatt") if form.get("werkstatt") in policy.WORKSHOP else None,
        "at": utcnow_naive().isoformat(timespec="seconds") + "Z",
    }
    job.comparison = comparison
    db.session.commit()


def check_complete(job: HukQuoteJob) -> tuple[list, dict]:
    return schema.validate(job.input_data or {}, local_today())


def start(user, job: HukQuoteJob, client_ref: str | None) -> tuple[HukQuoteJob, bool]:
    """Bestaetigte Startaktion. Gibt (Auftrag, neu_gestartet) zurueck; ein zweites Absenden
    derselben Aktion liefert den bereits gestarteten Auftrag."""
    if not is_enabled():
        raise HukJobError("Die Kfz-Berechnung ist derzeit abgeschaltet.", 503)
    client_ref = (client_ref or "").strip()[:CLIENT_REF_MAX] or None
    if client_ref:
        existing = HukQuoteJob.query.filter(
            HukQuoteJob.created_by_user_id == user.id, HukQuoteJob.client_ref == client_ref
        ).first()
        if existing is not None:
            return existing, False
    if job.status != "draft":
        if job.is_active:
            return job, False
        raise HukJobError("Bitte die Angaben zuerst prüfen und speichern.", 409)
    missing, errors = check_complete(job)
    if missing or errors:
        raise HukJobError("Es fehlen noch Angaben. Bitte das Formular vervollständigen.", 400)
    expire_stale()
    active = visible_query(user).filter(
        HukQuoteJob.created_by_user_id == user.id, HukQuoteJob.status.in_(ACTIVE_STATUSES)
    ).first()
    if active is not None and active.id != job.id:
        raise HukJobError("Es läuft bereits eine Berechnung. Bitte warten, bis sie fertig ist, oder sie abbrechen.", 409)

    # Bedingtes Update: nur ein gleichzeitiger Request kann den Entwurf in "queued" ueberfuehren.
    now = utcnow_naive()
    updated = HukQuoteJob.query.filter(HukQuoteJob.id == job.id, HukQuoteJob.status == "draft").update(
        {
            "status": "queued", "confirmed_at": now, "confirmed_by_user_id": user.id, "client_ref": client_ref,
            "message": None, "error_code": None, "attempts": 0, "cancel_requested": False, "updated_at": now,
        },
        synchronize_session=False,
    )
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        existing = HukQuoteJob.query.filter(
            HukQuoteJob.created_by_user_id == user.id, HukQuoteJob.client_ref == client_ref
        ).first()
        if existing is not None:
            return existing, False
        raise
    db.session.refresh(job)
    if not updated:
        return job, False

    from app.tasks.huk_tasks import run_huk_quote

    try:
        async_result = run_huk_quote.delay(job.id, job.tenant_id)
    except Exception:  # Broker nicht erreichbar
        log.exception("huk.job.enqueue_failed job_id=%s", job.id)
        db.session.rollback()
        _finish(job, "failed", "worker_unavailable", "Die Hintergrundverarbeitung ist nicht erreichbar. Bitte später erneut versuchen.")
        return job, True
    task_id = getattr(async_result, "id", None)
    if task_id:
        HukQuoteJob.query.filter(HukQuoteJob.id == job.id, HukQuoteJob.celery_task_id.is_(None)).update(
            {"celery_task_id": task_id[:64]}, synchronize_session=False
        )
        db.session.commit()
    db.session.refresh(job)
    return job, True


def cancel(job: HukQuoteJob) -> HukQuoteJob:
    if job.status == "queued":
        _finish(job, "cancelled", "cancelled", "Die Berechnung wurde abgebrochen.")
    elif job.status == "running":
        job.cancel_requested = True
        job.message = "Wird abgebrochen …"
        db.session.commit()
    return job


def copy_as_draft(user, job: HukQuoteJob) -> HukQuoteJob:
    new = HukQuoteJob(
        tenant_id=user.tenant_id, status="draft", created_by_user_id=user.id,
        customer_id=job.customer_id, input_data=dict(job.input_data or {}),
        comparison={k: v for k, v in (job.comparison or {}).items() if k != "manual"} or None,
    )
    db.session.add(new)
    db.session.commit()
    return new


def _finish(job: HukQuoteJob, status: str, code: str | None, message: str | None) -> None:
    job.status = status
    job.error_code = code
    job.message = message
    job.finished_at = utcnow_naive()
    db.session.commit()


def expire_stale() -> None:
    """Auftraege, deren Worker abgestuerzt ist, als fehlgeschlagen markieren (je Mandant)."""
    limit = utcnow_naive() - STALE_AFTER
    stale = HukQuoteJob.query.filter(
        HukQuoteJob.status.in_(ACTIVE_STATUSES), HukQuoteJob.updated_at < limit
    ).all()
    for job in stale:
        log.warning("huk.job.stale job_id=%s status=%s", job.id, job.status)
        job.status = "failed"
        job.error_code = "stale"
        job.message = "Die Berechnung wurde nicht abgeschlossen. Bitte erneut starten."
        job.finished_at = utcnow_naive()
    if stale:
        db.session.commit()


# --- Darstellung --------------------------------------------------------------------------------


def status_payload(job: HukQuoteJob) -> dict:
    return {
        "id": job.id,
        "status": job.status,
        "label": STATUS_LABELS.get(job.status, job.status),
        "tone": STATUS_TONES.get(job.status, "muted"),
        "active": job.is_active,
        "message": job.message,
        "finished": job.status in ("done", "failed", "needs_input", "cancelled"),
    }


def result_view(job: HukQuoteJob) -> dict | None:
    result = job.result or {}
    if job.status != "done" or result.get("source") != "huk.de":
        return None
    tariffs = result.get("tariffs") or []
    selected = next((t for t in tariffs if t.get("selected")), None)
    complete = bool(selected and result.get("coverage") and result.get("payment_interval"))
    return {**result, "selected": selected, "complete": complete}


# --- Dateien -----------------------------------------------------------------------------------


def evidence_dir() -> Path:
    return Path(current_app.config["HUK_STORAGE_DIR"])


def evidence_file(job: HukQuoteJob) -> Path | None:
    if not job.evidence_path:
        return None
    base = evidence_dir().resolve()
    path = (base / job.evidence_path).resolve()
    if base not in path.parents or not path.is_file():
        return None
    return path


def cleanup_files() -> None:
    """Alte Belege und liegengebliebene Browserprofile entfernen (laeuft vor jedem Auftrag)."""
    retention = int(current_app.config.get("HUK_EVIDENCE_RETENTION_DAYS") or 90)
    cutoff = time.time() - retention * 86400
    base = evidence_dir()
    if base.is_dir():
        for path in base.glob("*/*.png"):
            try:
                if path.stat().st_mtime < cutoff:
                    path.unlink()
            except OSError:
                pass
    tmp = Path(current_app.config["HUK_BROWSER_TMP_DIR"])
    if tmp.is_dir():
        for path in tmp.iterdir():
            try:
                if path.stat().st_mtime < time.time() - 3600:
                    shutil.rmtree(path, ignore_errors=True)
            except OSError:
                pass
