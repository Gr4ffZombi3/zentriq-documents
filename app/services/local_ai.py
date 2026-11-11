"""Hybridbetrieb KI: Auftraege fuer den eigenen Rechner (Windows-Laptop mit lokalem Modell).

Aufteilung im Modus "Automatisch" (feste Regeln, kein zusaetzlicher Modellaufruf zum Einordnen):
- Eindeutige Ablaeufe (Terminbestaetigung, Bestandsfragen, Beitragsdifferenz im Kfz-Vergleich ...)
  erledigt Zentriq selbst - ohne Modell (unveraendert, siehe chat.process_pending / huk.policy).
- Kurze Texte, einfache Fragen, Zusammenfassungen -> ZentriqAI-Router (aktuelles Gemini-Modell).
- Fotos, Scans, Dokumente und lange Eingaben -> eigener Rechner, sofern das Buero einen aktiven
  Rechner-Zugang hat. "Auf meinem Rechner bearbeiten" erzwingt das fuer eine Anfrage.

Rechner-Auftraege fallen NIE auf einen Cloud-Anbieter zurueck: Ist der Laptop aus, bleibt der
Auftrag in der Warteschlange und laeuft weiter, sobald der Rechner sich wieder meldet.

Ablauf mit dem Rechner (Verbindung geht immer vom Laptop aus, HTTPS):
- claim: der Rechner holt den aeltesten offenen Auftrag SEINES Bueros mit einer erlaubten Art und
  bekommt eine Lease (lease_id, Frist). Laeuft die Frist ab (Absturz, Laptop zugeklappt), wird der
  Auftrag erneut vergeben - hoechstens LOCAL_AI_MAX_ATTEMPTS Laeufe.
- complete/fail nur mit gueltiger lease_id; das Ergebnis wird per bedingtem Statuswechsel genau
  einmal uebernommen (doppelte Meldungen -> "duplicate", veraltete Lease -> "stale").
- Eingabedaten (Prompt, Bilder) werden nach Abschluss geloescht; offene Auftraege verfallen nach
  LOCAL_AI_QUEUE_DAYS.

Protokolle enthalten nur Kennungen und Status - nie Zugangsschluessel, Prompts, Bilder oder Ergebnisse."""

from __future__ import annotations

import hashlib
import hmac
import importlib
import logging
import secrets
import uuid
from datetime import timedelta

from flask import current_app

from app.extensions import db
from app.models.local_ai import LocalAiJob, LocalWorker, utcnow_naive
from app.tenancy import bypass_tenant_scope, use_tenant_id

log = logging.getLogger(__name__)

TOKEN_PREFIX = "zqw_"
LOCAL_CHOICE = "local"
# Fest definierte Auftragsarten -> Auswertung in Zentriq (Modul:Funktion fuer Ergebnis / Fehler).
KINDS = {
    "kfz_policy_extract": ("app.services.huk.policy_local", "Kfz-Versicherungsschein auslesen"),
    "assistant_chat": ("app.services.ai.chat_local", "Assistent-Anfrage"),
}
STATUS_LABELS = {"queued": "Wartet", "running": "Läuft", "done": "Fertig", "failed": "Fehlgeschlagen", "cancelled": "Abgebrochen"}
STATUS_TONES = {"queued": "warning", "running": "info", "done": "success", "failed": "danger", "cancelled": "muted"}
MAX_RESULT_CHARS = 60_000


class LocalAiError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.message = message
        self.status = status


def _cfg(key: str, default):
    value = current_app.config.get(key)
    return default if value in (None, "") else value


def lease_seconds() -> int:
    return int(_cfg("LOCAL_AI_LEASE_SECONDS", 300))


def online_seconds() -> int:
    return int(_cfg("LOCAL_AI_ONLINE_SECONDS", 90))


def max_attempts() -> int:
    return int(_cfg("LOCAL_AI_MAX_ATTEMPTS", 3))


def is_enabled() -> bool:
    return bool(current_app.config.get("LOCAL_AI_ENABLED", True))


# --- Zugaenge -------------------------------------------------------------------------------------


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def create_worker(user, name: str) -> tuple[LocalWorker, str]:
    """Neuer Zugang fuer das Buero des Benutzers. Der Schluessel wird nur hier zurueckgegeben."""
    name = " ".join((name or "").split())[:80] or "Mein Rechner"
    token = TOKEN_PREFIX + secrets.token_urlsafe(32)
    worker = LocalWorker(tenant_id=user.tenant_id, name=name, token_hash=hash_token(token), token_prefix=token[:10],
                         created_by_user_id=user.id)
    db.session.add(worker)
    db.session.commit()
    log.info("local_ai.worker_created worker_id=%s tenant_id=%s", worker.id, worker.tenant_id)
    return worker, token


def revoke_worker(worker: LocalWorker) -> None:
    if worker.revoked_at is None:
        worker.revoked_at = utcnow_naive()
        db.session.commit()
        log.info("local_ai.worker_revoked worker_id=%s tenant_id=%s", worker.id, worker.tenant_id)


def authenticate(header: str | None) -> LocalWorker | None:
    """'Bearer zqw_...' -> aktiver Zugang oder None (vergleicht nur Hashes)."""
    if not header or not header.startswith("Bearer "):
        return None
    token = header[7:].strip()
    if not token.startswith(TOKEN_PREFIX) or len(token) > 200:
        return None
    digest = hash_token(token)
    with bypass_tenant_scope():
        worker = LocalWorker.query.filter(LocalWorker.token_hash == digest).first()
        if worker is None or not hmac.compare_digest(worker.token_hash, digest) or worker.revoked_at is not None:
            return None
        _ = (worker.id, worker.tenant_id)
    return worker


def touch(worker: LocalWorker, model_name: str | None = None, version: str | None = None) -> None:
    worker.last_seen_at = utcnow_naive()
    if model_name:
        worker.model_name = str(model_name)[:120]
    if version:
        worker.worker_version = str(version)[:30]
    db.session.commit()


def is_online(worker: LocalWorker | None) -> bool:
    return bool(worker and worker.is_active and worker.last_seen_at
                and utcnow_naive() - worker.last_seen_at <= timedelta(seconds=online_seconds()))


def active_workers() -> list[LocalWorker]:
    """Aktive Zugaenge des aktuellen Bueros (Tenant-Kontext)."""
    return LocalWorker.query.filter(LocalWorker.revoked_at.is_(None)).order_by(LocalWorker.id).all()


def has_worker() -> bool:
    return is_enabled() and LocalWorker.query.filter(LocalWorker.revoked_at.is_(None)).first() is not None


def office_status() -> dict:
    workers = active_workers()
    online = [w for w in workers if is_online(w)]
    waiting = LocalAiJob.query.filter(LocalAiJob.status == "queued").count()
    running = LocalAiJob.query.filter(LocalAiJob.status == "running").count()
    last_seen = max((w.last_seen_at for w in workers if w.last_seen_at), default=None)
    return {"configured": bool(workers) and is_enabled(), "online": bool(online), "waiting": waiting, "running": running,
            "last_seen": last_seen, "model": next((w.model_name for w in online if w.model_name), None)}


# --- Ablaufregel ------------------------------------------------------------------------------------


def chat_route(choice: str | None, has_attachments: bool, text: str) -> str:
    """"local" | "cloud" fuer eine Assistent-Anfrage (nach den Direktablaeufen ohne Modell)."""
    if choice == LOCAL_CHOICE:
        return "local"
    if (choice or "auto") != "auto" or not has_worker():
        return "cloud"
    if has_attachments or len(text or "") >= int(_cfg("LOCAL_AI_AUTO_MIN_CHARS", 1500)):
        return "local"
    return "cloud"


def documents_go_local() -> bool:
    """Fotos/Scans/Dokumente: lokal, sobald das Buero einen aktiven Rechner-Zugang hat."""
    return has_worker()


# --- Auftraege (Zentriq-Seite) ---------------------------------------------------------------------


def enqueue(user, kind: str, payload: dict, origin_type: str | None = None, origin_id: int | None = None) -> LocalAiJob:
    if kind not in KINDS:
        raise LocalAiError("Unbekannte Auftragsart.")
    job = LocalAiJob(tenant_id=user.tenant_id, kind=kind, status="queued", created_by_user_id=user.id,
                     origin_type=origin_type, origin_id=origin_id, payload=payload)
    db.session.add(job)
    db.session.commit()
    log.info("local_ai.job_queued job_id=%s tenant_id=%s kind=%s", job.id, job.tenant_id, kind)
    return job


def job_for(origin_type: str, origin_id: int) -> LocalAiJob | None:
    return (LocalAiJob.query.filter(LocalAiJob.origin_type == origin_type, LocalAiJob.origin_id == origin_id)
            .order_by(LocalAiJob.id.desc()).first())


def open_origin_ids(origin_type: str, ids: list[int]) -> set[int]:
    if not ids:
        return set()
    rows = LocalAiJob.query.with_entities(LocalAiJob.origin_id).filter(
        LocalAiJob.origin_type == origin_type, LocalAiJob.origin_id.in_(ids), LocalAiJob.status.in_(("queued", "running"))).all()
    return {r[0] for r in rows}


def visible_jobs(user, limit: int = 30) -> list[LocalAiJob]:
    query = LocalAiJob.query
    if not user.is_office_admin:
        query = query.filter(LocalAiJob.created_by_user_id == user.id)
    return query.order_by(LocalAiJob.id.desc()).limit(limit).all()


def get_job(user, job_id: int) -> LocalAiJob | None:
    job = LocalAiJob.query.filter(LocalAiJob.id == job_id).first()
    if job is None or (not user.is_office_admin and job.created_by_user_id != user.id):
        return None
    return job


def cancel(job: LocalAiJob, reason: str = "Abgebrochen.") -> bool:
    updated = LocalAiJob.query.filter(LocalAiJob.id == job.id, LocalAiJob.status.in_(("queued", "running"))).update(
        {"status": "cancelled", "message": reason[:300], "payload": None, "lease_id": None, "finished_at": utcnow_naive()},
        synchronize_session=False)
    db.session.commit()
    db.session.refresh(job)
    return bool(updated)


def cancel_and_notify(job: LocalAiJob, reason: str = "Vom Benutzer abgebrochen.") -> bool:
    """Abbrechen und die zustaendige Zentriq-Funktion informieren (z. B. ohne Rechner fortfahren)."""
    if cancel(job, reason):
        _handler(job, "on_fail")
        return True
    return False


def retry(job: LocalAiJob) -> bool:
    """Fehlgeschlagenen Auftrag erneut einreihen (nur solange die Eingabe noch vorhanden ist)."""
    if job.status != "failed" or not job.payload:
        return False
    job.status, job.attempts, job.message, job.lease_id, job.lease_until, job.finished_at = "queued", 0, None, None, None, None
    db.session.commit()
    _handler(job, "on_requeue")
    return True


def expire_old() -> None:
    """Verfallene Auftraege (aller Bueros): Eingabe loeschen, offene als fehlgeschlagen markieren."""
    limit = utcnow_naive() - timedelta(days=int(_cfg("LOCAL_AI_QUEUE_DAYS", 7)))
    with bypass_tenant_scope():
        old = LocalAiJob.query.filter(LocalAiJob.created_at < limit, LocalAiJob.payload.isnot(None)).limit(50).all()
        for job in old:
            if job.status in ("queued", "running"):
                job.status, job.finished_at = "failed", utcnow_naive()
                job.message = "Abgelaufen – der Rechner hat den Auftrag nicht rechtzeitig abgeholt."
                with use_tenant_id(job.tenant_id):
                    _handler(job, "on_fail")
            job.payload = None
        if old:
            db.session.commit()


# --- Auftraege (Rechner-Seite) ---------------------------------------------------------------------


def claim(worker: LocalWorker, kinds: list[str] | None) -> LocalAiJob | None:
    """Naechsten Auftrag des eigenen Bueros vergeben (oder None)."""
    allowed = [k for k in (kinds or list(KINDS)) if k in KINDS]
    if not allowed or not is_enabled():
        return None
    now = utcnow_naive()
    with use_tenant_id(worker.tenant_id):
        candidates = (LocalAiJob.query.filter(
            LocalAiJob.kind.in_(allowed),
            db.or_(LocalAiJob.status == "queued", db.and_(LocalAiJob.status == "running", LocalAiJob.lease_until < now)),
        ).order_by(LocalAiJob.id).limit(5).all())
        for job in candidates:
            if job.attempts >= max_attempts():
                LocalAiJob.query.filter(LocalAiJob.id == job.id, LocalAiJob.status == job.status).update(
                    {"status": "failed", "message": "Der Rechner hat den Auftrag mehrfach nicht abgeschlossen.", "lease_id": None,
                     "finished_at": now}, synchronize_session=False)
                db.session.commit()
                db.session.refresh(job)
                _handler(job, "on_fail")
                continue
            lease = uuid.uuid4().hex
            updated = LocalAiJob.query.filter(
                LocalAiJob.id == job.id, LocalAiJob.status == job.status,
                LocalAiJob.lease_id.is_(None) if job.lease_id is None else LocalAiJob.lease_id == job.lease_id,
            ).update({"status": "running", "lease_id": lease, "lease_until": now + timedelta(seconds=lease_seconds()),
                      "worker_id": worker.id, "attempts": LocalAiJob.attempts + 1, "started_at": now, "message": None},
                     synchronize_session=False)
            db.session.commit()
            if updated:
                db.session.refresh(job)
                log.info("local_ai.job_claimed job_id=%s worker_id=%s attempt=%s", job.id, worker.id, job.attempts)
                return job
    return None


def extend(worker: LocalWorker, job_id: int, lease_id: str) -> bool:
    with use_tenant_id(worker.tenant_id):
        updated = LocalAiJob.query.filter(LocalAiJob.id == job_id, LocalAiJob.status == "running",
                                          LocalAiJob.lease_id == lease_id, LocalAiJob.worker_id == worker.id).update(
            {"lease_until": utcnow_naive() + timedelta(seconds=lease_seconds())}, synchronize_session=False)
        db.session.commit()
    return bool(updated)


def complete(worker: LocalWorker, job_id: int, lease_id: str, text: str, model: str | None) -> str:
    """"ok" | "duplicate" | "stale" | "unknown". Uebernimmt das Ergebnis genau einmal."""
    with use_tenant_id(worker.tenant_id):
        job = LocalAiJob.query.filter(LocalAiJob.id == job_id).first()
        if job is None:
            return "unknown"
        if job.status == "done":
            return "duplicate"
        updated = LocalAiJob.query.filter(LocalAiJob.id == job_id, LocalAiJob.status == "running", LocalAiJob.lease_id == lease_id).update(
            {"status": "done", "result": {"text": (text or "")[:MAX_RESULT_CHARS], "model": (model or "")[:120] or None},
             "payload": None, "lease_id": None, "finished_at": utcnow_naive()}, synchronize_session=False)
        db.session.commit()
        if not updated:
            return "stale"
        db.session.refresh(job)
        log.info("local_ai.job_done job_id=%s worker_id=%s", job.id, worker.id)
        _handler(job, "on_done")
    return "ok"


def fail(worker: LocalWorker, job_id: int, lease_id: str, message: str, retryable: bool) -> str:
    with use_tenant_id(worker.tenant_id):
        job = LocalAiJob.query.filter(LocalAiJob.id == job_id).first()
        if job is None:
            return "unknown"
        if job.status != "running" or job.lease_id != lease_id:
            return "stale"
        message = " ".join((message or "Unbekannter Fehler auf dem Rechner.").split())[:240]
        if retryable and job.attempts < max_attempts():
            values = {"status": "queued", "lease_id": None, "lease_until": None, "message": f"Neuer Versuch: {message}"}
        else:
            values = {"status": "failed", "lease_id": None, "message": message, "finished_at": utcnow_naive()}
        LocalAiJob.query.filter(LocalAiJob.id == job_id, LocalAiJob.lease_id == lease_id).update(values, synchronize_session=False)
        db.session.commit()
        db.session.refresh(job)
        log.info("local_ai.job_error job_id=%s worker_id=%s status=%s", job.id, worker.id, job.status)
        if job.status == "failed":
            _handler(job, "on_fail")
    return "ok"


def _handler(job: LocalAiJob, name: str) -> None:
    """Auswertung durch die zustaendige Zentriq-Funktion; Fehler dort duerfen den Auftrag nicht kippen."""
    module_name = KINDS.get(job.kind, (None,))[0]
    if not module_name:
        return
    func = getattr(importlib.import_module(module_name), name, None)
    if func is None:
        return
    try:
        func(job)
    except Exception as exc:  # noqa: BLE001
        db.session.rollback()
        log.error("local_ai.handler_failed job_id=%s handler=%s error=%s", job.id, name, type(exc).__name__)


def worker_payload(job: LocalAiJob) -> dict:
    return {"id": job.id, "art": job.kind, "lease": job.lease_id, "frist_sekunden": lease_seconds(), "eingabe": job.payload or {}}


def describe(job: LocalAiJob) -> dict:
    return {"id": job.id, "kind": job.kind, "kind_label": KINDS.get(job.kind, (None, job.kind))[1], "status": job.status,
            "label": STATUS_LABELS.get(job.status, job.status), "tone": STATUS_TONES.get(job.status, "muted"),
            "message": job.message, "attempts": job.attempts}
