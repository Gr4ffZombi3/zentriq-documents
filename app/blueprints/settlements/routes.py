"""Verwaltung > Abrechnungen: automatische Mitarbeiteruebersicht aus der HUK-Abrechnung.

Rechte (serverseitig je Endpunkt):
- Einreichen + eigene Eingangsbestaetigungen: Buero-Admins und Mitarbeiter des Bueros.
- Uebersicht, Originaldatei, Neuauswertung, Export: NUR der Buero-Admin des eigenen Bueros
  (sonst 404). Fremde Bueros sind ueber den Tenant-Filter ausgeschlossen.
Keine manuelle Zeilenpruefung und keine Zuordnung des Belegs zu einem einzelnen Mitarbeiter:
zugeordnet wird automatisch ueber die VM-Nummer der Tabellenbloecke (app/services/settlements.py)."""

import csv
import io
from datetime import date

from flask import Blueprint, abort, flash, redirect, render_template, request, send_file, url_for
from flask_login import current_user, login_required

from app.models import Settlement
from app.services import settlements as svc
from app.tenancy import get_or_404_scoped

settlements_bp = Blueprint("settlements", __name__, url_prefix="/abrechnungen")
STATUS_LABELS = {
    "processing": "Wird gelesen", "done": "Ausgewertet", "incomplete": "Auswertung unvollständig",
    "needs_review": "Alte Auswertung – bitte neu auswerten", "duplicate": "Doppelt – nicht gezählt", "failed": "Nicht auswertbar",
}


def _member():
    if not (current_user.is_office_admin or current_user.is_employee):
        abort(403)


def _admin():
    if not current_user.is_office_admin:
        abort(404)


def _period():
    today = date.today()
    available = svc.periods()
    raw = request.args.get("monat") or ""
    try:
        year, month = (int(x) for x in raw.split("-"))
        date(year, month, 1)
    except ValueError:
        year, month = available[0] if available else (today.year, today.month)
    return year, month, available


@settlements_bp.get("")
@login_required
def index():
    _member()
    if not current_user.is_office_admin:
        own = Settlement.query.filter(Settlement.uploaded_by_user_id == current_user.id).order_by(Settlement.created_at.desc()).limit(20).all()
        return render_template("settlements/submit.html", own=own)
    year, month, available = _period()
    documents = Settlement.query.order_by(Settlement.created_at.desc()).limit(30).all()
    return render_template(
        "settlements/index.html",
        view=svc.overview(year, month), year=year, month=month, available=available, month_label=svc.month_label,
        documents=documents, status_labels=STATUS_LABELS, is_current=svc.is_current,
        processing=any(d.status == "processing" for d in documents),
    )


@settlements_bp.post("/einreichen")
@login_required
def submit():
    _member()
    files = [f for f in request.files.getlist("dateien") if f and f.filename]
    if not files:
        flash("Bitte mindestens eine PDF-Datei auswählen.", "error")
        return redirect(url_for("settlements.index"))
    for f in files[:10]:
        try:
            record, created = svc.submit(current_user, f.filename, f.read())
        except svc.SettlementError as exc:
            flash(f"{f.filename}: {exc}", "error")
            continue
        if created:
            flash(f"Eingang bestätigt: „{f.filename}“ wurde eingereicht.", "success")
        else:
            flash(f"„{f.filename}“ wurde bereits eingereicht und wird nicht doppelt gezählt.", "info")
    if current_user.is_office_admin:
        latest = Settlement.query.order_by(Settlement.id.desc()).first()
        if latest is not None and latest.period_start is not None:
            return redirect(url_for("settlements.index", monat=f"{latest.period_start:%Y-%m}"))
    return redirect(url_for("settlements.index"))


@settlements_bp.get("/beleg/<int:settlement_id>")
@login_required
def detail(settlement_id):
    _admin()
    record = get_or_404_scoped(Settlement, settlement_id)
    owners = svc.owner_map(record.tenant_id)
    from app.services.calendar import member_name
    from app.utils.vermittlernummer import vermittlernummer_key

    blocks = []
    for block in (record.details or {}).get("blocks") or []:
        user = owners.get(vermittlernummer_key(block["vm"]))
        lines = [line for line in record.lines if line.broker_number == block["vm"] and line.amount is not None]
        blocks.append({"vm": block["vm"], "name": block.get("name"), "member": member_name(user) if user else None, "lines": len(lines),
                       "total": sum((line.amount for line in lines), svc.ZERO)})
    return render_template("settlements/detail.html", record=record, status_labels=STATUS_LABELS, blocks=blocks,
                           current=svc.is_current(record), details=record.details or {})


@settlements_bp.post("/beleg/<int:settlement_id>/neu-auswerten")
@login_required
def reevaluate(settlement_id):
    """Dieselbe Auswertung wie beim Upload. Das bisherige Ergebnis wird erst nach erfolgreichem
    Lesen ersetzt - im selben Beleg, ohne Dubletten."""
    _admin()
    record = get_or_404_scoped(Settlement, settlement_id)
    if record.status == "processing":
        flash("Der Beleg wird gerade gelesen.", "info")
        return redirect(url_for("settlements.detail", settlement_id=record.id))
    try:
        svc.reevaluate(record)
    except svc.SettlementError as exc:
        flash(f"Neu auswerten nicht möglich – das bisherige Ergebnis bleibt unverändert. {exc}", "error")
        return redirect(url_for("settlements.detail", settlement_id=record.id))
    except Exception:  # noqa: BLE001
        flash("Neu auswerten nicht möglich – das bisherige Ergebnis bleibt unverändert.", "error")
        return redirect(url_for("settlements.detail", settlement_id=record.id))
    from app.models.audit_log import AuditEventType
    from app.services.audit import log_audit_event

    log_audit_event(AuditEventType.USER_UPDATED, user=current_user, details={"settlement_reevaluated": record.id, "lines": len(record.lines)})
    flash(f"Neu ausgewertet: {STATUS_LABELS.get(record.status, record.status)}.", "success" if record.status == "done" else "info")
    if record.period_start is not None and record.status in svc.COUNTED_STATUSES:
        return redirect(url_for("settlements.index", monat=f"{record.period_start:%Y-%m}"))
    return redirect(url_for("settlements.detail", settlement_id=record.id))


@settlements_bp.get("/beleg/<int:settlement_id>/original")
@login_required
def original(settlement_id):
    _admin()
    record = get_or_404_scoped(Settlement, settlement_id)
    path = svc.file_path(record)
    if path is None:
        abort(404)
    response = send_file(path, mimetype="application/pdf", download_name=record.original_filename, as_attachment=request.args.get("download") == "1", max_age=0)
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


@settlements_bp.get("/export.csv")
@login_required
def export():
    """Alle zugeordneten Abschluesse und 10-€-Positionen des Monats (wie in der Uebersicht)."""
    _admin()
    year, month, _available = _period()
    view = svc.overview(year, month)
    out = io.StringIO()
    writer = csv.writer(out, delimiter=";")
    writer.writerow(["Mitarbeiter", "VM-Nummer", "Art", "Vertragsnummer", "Sparte", "Antragsdatum", "Vergütung laut Beleg"])

    def money(value):
        return str(value).replace(".", ",")

    for row in view.rows:
        for item in row.closings:
            writer.writerow([row.name, row.vm or "", "Neuabschluss", item.ref, item.sparte, item.applied_on or "", money(item.amount)])
        for item in row.cancellations:
            writer.writerow([row.name, row.vm or "", "Storno/Korrektur", item.ref, item.sparte, item.applied_on or "", money(item.amount)])
        for line in row.ten_lines:
            writer.writerow([row.name, row.vm or "", "10-€-Position", line.contract_ref or "", line.sparte or "", line.booking_date or "", money(line.amount)])
    data = io.BytesIO(("﻿" + out.getvalue()).encode("utf-8"))
    response = send_file(data, mimetype="text/csv", as_attachment=True, download_name=f"abrechnungen-{year}-{month:02d}.csv", max_age=0)
    response.headers["Cache-Control"] = "private, no-store"
    return response
