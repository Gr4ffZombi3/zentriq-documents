"""Eigener Rechner (Hybridbetrieb KI).

- rechner_api (/api/rechner/...): Schnittstelle fuer den Windows-Worker. Anmeldung ausschliesslich
  mit dem widerrufbaren Zugangsschluessel (Authorization: Bearer zqw_...), ohne Sitzung/CSRF.
  Der Worker sieht nur Auftraege seines Bueros und nur fest definierte Auftragsarten.
- rechner (/mein-rechner): Status des Rechners und der Auftraege; Zugaenge anlegen/widerrufen
  (nur Buero-Admins). Mitarbeiter sehen ihre eigenen Auftraege."""

import logging

from flask import Blueprint, abort, flash, jsonify, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from app.extensions import csrf, db
from app.models.local_ai import LocalWorker
from app.services import local_ai
from app.tenancy import set_current_tenant_id

log = logging.getLogger(__name__)

rechner_api_bp = Blueprint("rechner_api", __name__, url_prefix="/api/rechner")
rechner_bp = Blueprint("rechner", __name__, url_prefix="/mein-rechner")

MAX_BODY_BYTES = 256 * 1024


# --- Worker-Schnittstelle ---------------------------------------------------------------------------


def _worker():
    if not local_ai.is_enabled():
        return None, (jsonify({"fehler": "abgeschaltet"}), 503)
    if (request.content_length or 0) > MAX_BODY_BYTES:
        return None, (jsonify({"fehler": "zu_gross"}), 413)
    worker = local_ai.authenticate(request.headers.get("Authorization"))
    if worker is None:
        log.warning("local_ai.auth_rejected")
        return None, (jsonify({"fehler": "zugang"}), 401)
    # Ab hier gilt fuer die Anfrage ausschliesslich das Buero des Rechners (Tenant-Filter).
    set_current_tenant_id(worker.tenant_id)
    data = request.get_json(silent=True) or {}
    local_ai.touch(worker, data.get("modell"), data.get("version"))
    return worker, None


def _body() -> dict:
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else {}


@rechner_api_bp.post("/hallo")
@csrf.exempt
def api_hello():
    worker, error = _worker()
    if error:
        return error
    return jsonify({"ok": True, "rechner": worker.name, "arten": sorted(local_ai.KINDS),
                    "abfrage_sekunden": local_ai._cfg("LOCAL_AI_POLL_SECONDS", 5)})


@rechner_api_bp.post("/auftrag")
@csrf.exempt
def api_claim():
    worker, error = _worker()
    if error:
        return error
    local_ai.expire_old()
    kinds = _body().get("arten")
    job = local_ai.claim(worker, kinds if isinstance(kinds, list) else None)
    if job is None:
        return "", 204
    return jsonify(local_ai.worker_payload(job))


@rechner_api_bp.post("/auftrag/<int:job_id>/verlaengern")
@csrf.exempt
def api_extend(job_id):
    worker, error = _worker()
    if error:
        return error
    ok = local_ai.extend(worker, job_id, str(_body().get("lease") or ""))
    return jsonify({"ok": ok}), 200 if ok else 409


@rechner_api_bp.post("/auftrag/<int:job_id>/ergebnis")
@csrf.exempt
def api_result(job_id):
    worker, error = _worker()
    if error:
        return error
    data = _body()
    text = data.get("text")
    if not isinstance(text, str):
        return jsonify({"fehler": "text_fehlt"}), 400
    outcome = local_ai.complete(worker, job_id, str(data.get("lease") or ""), text, data.get("modell"))
    status = {"ok": 200, "duplicate": 200, "stale": 409, "unknown": 404}[outcome]
    return jsonify({"ergebnis": outcome}), status


@rechner_api_bp.post("/auftrag/<int:job_id>/fehler")
@csrf.exempt
def api_error(job_id):
    worker, error = _worker()
    if error:
        return error
    data = _body()
    outcome = local_ai.fail(worker, job_id, str(data.get("lease") or ""), str(data.get("meldung") or ""), bool(data.get("wiederholen")))
    status = {"ok": 200, "stale": 409, "unknown": 404}[outcome]
    return jsonify({"ergebnis": outcome}), status


# --- Oberflaeche ------------------------------------------------------------------------------------


def _member():
    if not (current_user.is_office_admin or current_user.is_employee) or current_user.tenant_id is None:
        abort(403)


@rechner_bp.get("")
@login_required
def index():
    _member()
    local_ai.expire_old()
    workers = LocalWorker.query.order_by(LocalWorker.revoked_at.isnot(None), LocalWorker.id.desc()).all() if current_user.is_office_admin else []
    return render_template(
        "rechner/index.html", status=local_ai.office_status(), workers=workers, is_online=local_ai.is_online,
        jobs=[(job, local_ai.describe(job)) for job in local_ai.visible_jobs(current_user)], enabled=local_ai.is_enabled(),
        new_token=None, base_url=request.url_root.rstrip("/"),
    )


@rechner_bp.get("/status")
@login_required
def status():
    _member()
    data = local_ai.office_status()
    data["last_seen"] = data["last_seen"].isoformat() + "Z" if data["last_seen"] else None
    job_id = request.args.get("auftrag", type=int)
    if job_id:
        job = local_ai.get_job(current_user, job_id)
        data["auftrag"] = local_ai.describe(job) if job else None
    return jsonify(data)


@rechner_bp.post("/zugang")
@login_required
def create_access():
    _member()
    if not current_user.is_office_admin:
        abort(403)
    worker, token = local_ai.create_worker(current_user, request.form.get("name", ""))
    workers = LocalWorker.query.order_by(LocalWorker.revoked_at.isnot(None), LocalWorker.id.desc()).all()
    response = render_template(
        "rechner/index.html", status=local_ai.office_status(), workers=workers, is_online=local_ai.is_online,
        jobs=[(job, local_ai.describe(job)) for job in local_ai.visible_jobs(current_user)], enabled=local_ai.is_enabled(),
        new_token=token, new_worker=worker, base_url=request.url_root.rstrip("/"),
    )
    # Der Schluessel steht nur in dieser Antwort - nicht zwischenspeichern.
    return response, 200, {"Cache-Control": "no-store"}


@rechner_bp.post("/zugang/<int:worker_id>/widerrufen")
@login_required
def revoke_access(worker_id):
    _member()
    if not current_user.is_office_admin:
        abort(403)
    worker = LocalWorker.query.filter(LocalWorker.id == worker_id).first_or_404()
    local_ai.revoke_worker(worker)
    flash(f"Zugang „{worker.name}“ widerrufen. Der Rechner kann keine Aufträge mehr abholen.", "success")
    return redirect(url_for("rechner.index"))


@rechner_bp.post("/auftrag/<int:job_id>/<aktion>")
@login_required
def job_action(job_id, aktion):
    _member()
    job = local_ai.get_job(current_user, job_id)
    if job is None:
        abort(404)
    if aktion == "abbrechen":
        local_ai.cancel_and_notify(job)
    elif aktion == "wiederholen":
        if not local_ai.retry(job):
            flash("Der Auftrag kann nicht wiederholt werden (Eingabe nicht mehr vorhanden).", "error")
    else:
        abort(404)
    db.session.commit()
    back = request.form.get("zurueck") or ""
    # Nur Ruecksprung innerhalb von Zentriq (kein offener Redirect).
    return redirect(back if back.startswith("/") and not back.startswith("//") else url_for("rechner.index"))
