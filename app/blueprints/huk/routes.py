"""Kfz-Angebot ueber den HUK-Webrechner: Angaben erfassen, Zusammenfassung bestaetigen,
Hintergrundauftrag verfolgen, Ergebnis anzeigen.

Kfz-Vergleich aus Versicherungsschein (/kfz-angebot/versicherungsschein): Foto/PDF auslesen
(app/services/huk/policy.py), erkannte Angaben pruefen, "Bei HUK berechnen", bisherigen und
HUK-Jahresbeitrag vergleichen. Blockiert der Rechner, kann der HUK-Beitrag manuell eingetragen werden.

Zugriff: Buero-Admins und Mitarbeiter (SUPER_ADMIN nie); Auftraege anderer Bueros sind 404,
Mitarbeiter sehen nur eigene Auftraege (app/services/huk/jobs.py)."""

import secrets

from flask import (
    Blueprint,
    abort,
    current_app,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    send_file,
    url_for,
)
from flask_login import current_user, login_required

from app.services import workspace
from app.services.calendar import member_name
from app.services.huk import ai, jobs, policy, schema
from app.services.timetracking.clock import local_today

huk_bp = Blueprint("huk", __name__, url_prefix="/kfz-angebot")


def _member():
    jobs.require_member(current_user)


def _form_values() -> dict:
    raw = {key: request.form.get(key, "") for key in schema.FIELD_BY_KEY}
    raw["fahrer"] = request.form.getlist("fahrer")
    return raw


def _render_form(job, values, customer, missing=(), errors=None, suggested=(), ai_error=None, ai_text="", previous=None):
    comparison = job.comparison if job is not None else None
    if comparison and not suggested:
        # Aus dem Dokument uebernommene Angaben bleiben als "bitte pruefen" markiert, bis gerechnet wird.
        suggested = [k for k in comparison.get("extracted") or [] if values.get(k)]
    return render_template(
        "huk/form.html",
        comparison=comparison,
        previous=previous if previous is not None else ((comparison or {}).get("previous") or {}),
        flags=(comparison or {}).get("flags") or {},
        intervals=policy.INTERVALS,
        label_of=schema.label_of,
        start_token=secrets.token_urlsafe(16),
        job=job,
        values=values,
        customer=customer,
        fields=schema.FIELDS,
        sections=schema.SECTIONS,
        active_keys={f.key for f in schema.active_fields(values)},
        missing=set(missing),
        errors=errors or {},
        suggested=set(suggested),
        proposals={**schema.suggestions(local_today()), **schema.corrections(values)},
        km_choices=schema.KM_CHOICES, km_label=schema.km_label, day_fields=schema.DAY_FIELDS, month_fields=schema.MONTH_FIELDS,
        ai_reason=ai.unavailable_reason(),
        ai_error=ai_error,
        ai_text=ai_text,
        ai_max_chars=current_app.config.get("HUK_AI_MAX_INPUT_CHARS"),
    )


@huk_bp.get("")
@login_required
def index():
    _member()
    return render_template(
        "huk/index.html",
        jobs=jobs.recent_jobs(current_user),
        labels=jobs.STATUS_LABELS,
        tones=jobs.STATUS_TONES,
        enabled=jobs.is_enabled(),
        member_name=member_name,
    )


@huk_bp.route("/neu", methods=["GET", "POST"])
@login_required
def new():
    _member()
    customer = jobs.customer_for(current_user, request.values.get("kunde"))
    if request.method == "GET":
        values = jobs.prefill_for(current_user, customer)
        return _render_form(None, values, customer, suggested=values.keys())
    return _save(None, customer)


@huk_bp.route("/versicherungsschein", methods=["GET", "POST"])
@login_required
def policy_upload():
    """Versicherungsschein (und optional Beitragsrechnung) hochladen und auslesen."""
    _member()
    customer = jobs.customer_for(current_user, request.values.get("kunde"))
    if request.method == "GET":
        return render_template("huk/policy.html", customer=customer, enabled=jobs.is_enabled(),
                               max_files=policy.MAX_FILES, max_pages=policy.MAX_PAGES)
    uploads = [policy.Upload(f.filename or "Datei", f.read(), kind)
               for kind, name in (("schein", "schein"), ("rechnung", "rechnung"))
               for f in request.files.getlist(name) if f and f.filename]
    try:
        result = policy.analyse(current_user, uploads)
    except policy.PolicyError as exc:
        return render_template("huk/policy.html", customer=customer, enabled=jobs.is_enabled(), error=str(exc),
                               max_files=policy.MAX_FILES, max_pages=policy.MAX_PAGES), 422
    if not result["readable"]:
        return render_template(
            "huk/policy.html", customer=customer, enabled=jobs.is_enabled(), max_files=policy.MAX_FILES, max_pages=policy.MAX_PAGES,
            error="Im Dokument wurden keine Tarifangaben erkannt. Bitte ein schärferes Foto (gerade, gut ausgeleuchtet) oder das PDF verwenden.",
        ), 422
    values = {**jobs.prefill_for(current_user, customer), **result["values"]}
    job = jobs.create_comparison(current_user, values, policy.new_comparison(result), customer)
    return redirect(url_for("huk.edit", job_id=job.id))


@huk_bp.route("/<int:job_id>/angaben", methods=["GET", "POST"])
@login_required
def edit(job_id):
    _member()
    job = jobs.get_job(current_user, job_id)
    if request.method == "GET":
        if job.is_active or job.status == "done":
            return redirect(url_for("huk.show", job_id=job.id))
        missing, errors = jobs.check_complete(job)
        return _render_form(job, job.input_data or {}, job.customer, missing if request.args.get("pruefen") else (), errors)
    # Das Formular sendet "kunde" immer mit (leer = keine Zuordnung).
    customer = jobs.customer_for(current_user, request.form.get("kunde")) if "kunde" in request.form else job.customer
    return _save(job, customer)


def _drop_form_drafts(job, was_new: bool) -> None:
    """Zwischenstaende des Formulars (app/services/workspace.py) sind nach dem Speichern erledigt."""
    if was_new:
        workspace.delete_draft(current_user, "huk:neu")
    if job is not None:
        workspace.delete_draft(current_user, f"huk:{job.id}")


def _previous_form() -> dict:
    return {
        "beitrag": request.form.get("bisher_beitrag", ""),
        "zahlweise": request.form.get("bisher_zahlweise", ""),
        "werkstatt": request.form.get("bisher_werkstatt", ""),
    }


def _save(job, customer):
    if request.form.get("aktion") == "ki":
        return _ai_suggest(job, customer)
    was_new = job is None
    previous_errors = {}
    if job is not None and job.comparison:
        previous_errors = jobs.save_previous(job, _previous_form())
    try:
        job, missing, errors = jobs.save_draft(current_user, job, _form_values(), customer)
    except jobs.HukJobError as exc:
        abort(exc.status)
    if missing or errors or previous_errors:
        return _render_form(job, job.input_data, customer, missing, {**errors, **previous_errors}), 422
    _drop_form_drafts(job, was_new)
    if request.form.get("aktion") == "berechnen" and job.comparison:
        # "Bei HUK berechnen": die gepruefte Uebersicht ist die Bestaetigung (Hinweis steht am Knopf).
        try:
            job, _new = jobs.start(current_user, job, request.form.get("client_ref"))
        except jobs.HukJobError as exc:
            flash(exc.message, "error")
    return redirect(url_for("huk.show", job_id=job.id))


def _ai_suggest(job, customer):
    """Claude-Vorschlaege aus einem Freitext - werden nur ins Formular uebernommen und markiert."""
    values = schema.normalize(_form_values())
    text = request.form.get("ki_text", "")
    if job is None:
        job, _m, _e = jobs.save_draft(current_user, None, values, customer)
        workspace.delete_draft(current_user, "huk:neu")  # weiter unter dem Entwurf des neuen Auftrags
    try:
        suggested = ai.extract(job, text)
    except ai.HukAiError as exc:
        return _render_form(job, values, customer, ai_error=exc.message, ai_text=text), 422
    # Vorhandene Eingaben des Benutzers haben Vorrang; nur leere Felder werden vorgeschlagen.
    new_keys = [k for k, v in suggested.items() if not values.get(k)]
    for key in new_keys:
        values[key] = suggested[key]
    return _render_form(job, values, customer, suggested=new_keys, ai_text=text)


@huk_bp.get("/<int:job_id>")
@login_required
def show(job_id):
    _member()
    jobs.expire_stale()
    job = jobs.get_job(current_user, job_id)
    missing, errors = jobs.check_complete(job) if job.status == "draft" else ((), {})
    return render_template(
        "huk/show.html",
        job=job,
        summary=schema.summary(job.input_data or {}),
        status=jobs.status_payload(job),
        result=jobs.result_view(job),
        comparison=policy.comparison_view(job),
        intervals=policy.INTERVALS,
        incomplete=bool(missing or errors),
        start_token=secrets.token_urlsafe(16),
        enabled=jobs.is_enabled(),
    )


@huk_bp.post("/<int:job_id>/starten")
@login_required
def start(job_id):
    _member()
    job = jobs.get_job(current_user, job_id)
    if request.form.get("bestaetigt") != "ja":
        flash("Bitte die Angaben bestätigen.", "error")
        return redirect(url_for("huk.show", job_id=job.id))
    try:
        job, _new = jobs.start(current_user, job, request.form.get("client_ref"))
    except jobs.HukJobError as exc:
        flash(exc.message, "error")
    return redirect(url_for("huk.show", job_id=job.id))


@huk_bp.post("/<int:job_id>/huk-beitrag")
@login_required
def manual_price(job_id):
    """HUK-Beitrag manuell eintragen, wenn der Rechner nicht automatisch bis zum Preis kam."""
    _member()
    job = jobs.get_job(current_user, job_id)
    try:
        jobs.save_manual_price(job, request.form)
    except jobs.HukJobError as exc:
        flash(exc.message, "error")
    return redirect(url_for("huk.show", job_id=job.id))


@huk_bp.post("/<int:job_id>/abbrechen")
@login_required
def cancel(job_id):
    _member()
    job = jobs.cancel(jobs.get_job(current_user, job_id))
    if request.accept_mimetypes.best == "application/json":
        return jsonify(jobs.status_payload(job))
    return redirect(url_for("huk.show", job_id=job.id))


@huk_bp.post("/<int:job_id>/kopie")
@login_required
def copy(job_id):
    _member()
    new = jobs.copy_as_draft(current_user, jobs.get_job(current_user, job_id))
    return redirect(url_for("huk.edit", job_id=new.id))


@huk_bp.get("/<int:job_id>/status")
@login_required
def status(job_id):
    _member()
    jobs.expire_stale()
    return jsonify(jobs.status_payload(jobs.get_job(current_user, job_id)))


@huk_bp.get("/<int:job_id>/beleg.png")
@login_required
def evidence(job_id):
    _member()
    path = jobs.evidence_file(jobs.get_job(current_user, job_id))
    if path is None:
        abort(404)
    response = send_file(path, mimetype="image/png", max_age=0)
    response.headers["Cache-Control"] = "private, no-store"
    return response
