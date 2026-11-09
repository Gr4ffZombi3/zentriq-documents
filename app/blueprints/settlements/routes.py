"""Verwaltung > Abrechnungen.

Rechte (serverseitig je Endpunkt):
- Einreichen + eigene Eingangsbestaetigungen: Buero-Admins und Mitarbeiter des Bueros.
- Originaldatei, Buchungen, Auswertung, Pruefung, Export: NUR der Buero-Admin des eigenen
  Bueros (sonst 404). Fremde Bueros sind ueber den Tenant-Filter ausgeschlossen."""

import csv
import io
from datetime import date

from flask import Blueprint, abort, flash, redirect, render_template, request, send_file, url_for
from flask_login import current_user, login_required

from app.extensions import db
from app.models import Settlement, SettlementLine, User
from app.services import settlements as svc
from app.tenancy import get_or_404_scoped

settlements_bp = Blueprint("settlements", __name__, url_prefix="/abrechnungen")
STATUS_LABELS = {"processing": "Wird gelesen", "done": "Ausgewertet", "needs_review": "Prüfung nötig", "duplicate": "Doppelt – nicht gezählt", "failed": "Nicht lesbar"}


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


def _filters():
    exclude_ten = request.args.get("ohne_zehn") == "1"
    excluded = tuple(s for s in request.args.getlist("ohne") if s in svc.sparten())
    return exclude_ten, excluded


@settlements_bp.get("")
@login_required
def index():
    _member()
    if not current_user.is_office_admin:
        own = Settlement.query.filter(Settlement.uploaded_by_user_id == current_user.id).order_by(Settlement.created_at.desc()).limit(20).all()
        return render_template("settlements/submit.html", own=own)
    year, month, available = _period()
    exclude_ten, excluded = _filters()
    return render_template(
        "settlements/index.html",
        rows=svc.summary(year, month, exclude_ten=exclude_ten, exclude_sparten=excluded),
        year=year, month=month, available=available, month_label=svc.month_label, exclude_ten=exclude_ten, excluded=excluded,
        sparten=svc.sparten(), documents=Settlement.query.order_by(Settlement.created_at.desc()).limit(30).all(),
        status_labels=STATUS_LABELS, type_labels=svc.TYPE_LABELS,
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
    return redirect(url_for("settlements.index"))


@settlements_bp.get("/beleg/<int:settlement_id>")
@login_required
def detail(settlement_id):
    _admin()
    record = get_or_404_scoped(Settlement, settlement_id)
    from app.models import User
    from app.services.user_admin import office_members_query

    members = office_members_query().filter(User.is_active.is_(True)).order_by(User.email).all()
    base = SettlementLine.query.filter(SettlementLine.settlement_id == record.id)
    counts = {"all": base.count(), "open": base.filter(SettlementLine.needs_review.is_(True), SettlementLine.review_state.is_(None)).count()}
    blocks = svc.broker_blocks(SettlementLine.query.filter(SettlementLine.settlement_id == record.id, SettlementLine.broker_number.isnot(None)).all())
    return render_template("settlements/detail.html", record=record, status_labels=STATUS_LABELS, type_labels=svc.TYPE_LABELS,
                           sparten=svc.sparten(), members=members, counts=counts, blocks=blocks, member_label=_member_label)


def _member_label(user) -> str:
    from app.services.calendar import member_name

    return member_name(user) if user else "nicht zugeordnet"


@settlements_bp.post("/beleg/<int:settlement_id>/vermittler")
@login_required
def assign_broker(settlement_id):
    """VM-Block einem aktiven Mitglied des eigenen Bueros zuordnen (nie SUPER_ADMIN/geloescht)."""
    _admin()
    from app.services.user_admin import office_members_query

    record = get_or_404_scoped(Settlement, settlement_id)
    broker = (request.form.get("vm") or "").strip()
    user_id = request.form.get("mitarbeiter", type=int)
    user = office_members_query().filter(User.id == user_id, User.is_active.is_(True)).first() if user_id else None
    if user_id and user is None:
        abort(404)
    count = svc.assign_broker(record, broker, user)
    if not count:
        abort(404)
    _refresh_status(record)
    db.session.commit()
    flash(f"VM {broker}: {count} Buchungen {_member_label(user) if user else 'ohne Zuordnung'}.", "success")
    return redirect(url_for("settlements.detail", settlement_id=record.id))


@settlements_bp.get("/beleg/<int:settlement_id>/zeilen")
@login_required
def detail_lines(settlement_id):
    """Buchungen eines Belegs - erst beim Aufklappen geladen (nur=unklar: offene Zeilen)."""
    _admin()
    record = get_or_404_scoped(Settlement, settlement_id)
    lines = record.lines
    if request.args.get("nur") == "unklar":
        lines = [line for line in lines if line.needs_review and line.review_state is None]
    return render_template("settlements/_detail_lines.html", record=record, lines=lines, type_labels=svc.TYPE_LABELS, sparten=svc.sparten())


@settlements_bp.route("/beleg/<int:settlement_id>/neu-auswerten", methods=["GET", "POST"])
@login_required
def reevaluate(settlement_id):
    """Gepruefte Neuauswertung: erst Vorschau (nichts gespeichert), dann Uebernahme nur mit
    passendem Vorschau-Token. Gleicher Beleg, keine Dubletten, Pruefungen bleiben erhalten."""
    _admin()
    record = get_or_404_scoped(Settlement, settlement_id)
    if request.method == "POST":
        try:
            result = svc.reevaluate_apply(record, request.form.get("token", ""), drop_lost_reviews=request.form.get("verwerfen") == "1",
                                          user=current_user)
        except svc.SettlementError as exc:
            db.session.rollback()
            flash(str(exc), "error")
            return redirect(url_for("settlements.reevaluate", settlement_id=record.id))
        flash(f"Neu ausgewertet: {result.new_count} Buchungen (vorher {result.old_count}), {len(result.kept_reviews)} Prüfung(en) übernommen.", "success")
        return redirect(url_for("settlements.detail", settlement_id=record.id))
    try:
        preview = svc.reevaluate_preview(record)
        names = {u.id: _member_label(u) for u in User.query.filter(User.id.in_([e["user_id"] for e in preview.by_broker.values() if e["user_id"]])).all()}
    except svc.SettlementError as exc:
        flash(str(exc), "error")
        return redirect(url_for("settlements.detail", settlement_id=record.id))
    db.session.rollback()  # Vorschau-Zeilen nie speichern
    return render_template("settlements/reevaluate.html", record=record, preview=preview, type_labels=svc.TYPE_LABELS, names=names)


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


@settlements_bp.post("/beleg/<int:settlement_id>/mitarbeiter")
@login_required
def assign(settlement_id):
    _admin()
    from app.models import User

    record = get_or_404_scoped(Settlement, settlement_id)
    user_id = request.form.get("mitarbeiter", type=int)
    user = User.query.filter(User.id == user_id, User.tenant_id == current_user.tenant_id).first() if user_id else None
    record.employee_user_id = user.id if user else None
    _refresh_status(record)
    db.session.commit()
    flash("Zuordnung gespeichert.", "success")
    return redirect(url_for("settlements.detail", settlement_id=record.id))


@settlements_bp.post("/zeile/<int:line_id>")
@login_required
def review(line_id):
    _admin()
    from app.models.settlement import _now

    line = get_or_404_scoped(SettlementLine, line_id)
    if request.form.get("aktion") == "ignorieren":
        line.review_state = "ignored"
    else:
        sparte = request.form.get("sparte")
        booking_type = request.form.get("buchungsart")
        ref = " ".join((request.form.get("referenz") or "").split())[:60] or None
        amount = svc.parse_amount(request.form.get("betrag") or "") if request.form.get("betrag") else line.amount
        if sparte not in svc.sparten() or booking_type not in svc.TYPE_LABELS or amount is None:
            flash("Bitte Sparte, Buchungsart und Betrag angeben.", "error")
            return redirect(url_for("settlements.detail", settlement_id=line.settlement_id) + f"#zeile-{line.id}")
        line.sparte, line.booking_type, line.contract_ref, line.amount = sparte, booking_type, ref, amount
        line.is_ten = abs(amount) == svc.TEN
        line.review_state = "ok"
    line.reviewed_by_user_id, line.reviewed_at = current_user.id, _now()
    _refresh_status(line.settlement)
    db.session.commit()
    flash("Zeile geprüft.", "success")
    return redirect(url_for("settlements.detail", settlement_id=line.settlement_id) + f"#zeile-{line.id}")


def _refresh_status(record) -> None:
    if record.status not in ("done", "needs_review"):
        return
    open_lines = [line for line in record.lines if line.needs_review and line.review_state is None]
    problems = []
    if record.period_start is None:
        problems.append("Abrechnungszeitraum nicht erkannt")
    if record.employee_user_id is None and any(line.broker_number is None for line in record.lines):
        problems.append("Mitarbeiter/Vermittlernummer nicht zugeordnet")
    if open_lines:
        problems.append(f"{len(open_lines)} unklare Zeile(n)")
    record.status = "needs_review" if problems else "done"
    record.message = "; ".join(problems) or None


def _drill_lines():
    year, month, _available = _period()
    exclude_ten, excluded = _filters()
    user_id = request.args.get("mitarbeiter", type=int)
    only = request.args.get("art") if request.args.get("art") in ("ten", "storno", "korrektur", "regular", "unclear") else None
    lines = svc.counted_lines(year, month, user_id=user_id, exclude_ten=exclude_ten, exclude_sparten=excluded, only=only)
    sparte = request.args.get("sparte")
    if sparte:
        lines = [line for line in lines if (line.sparte or "Sparte unklar") == sparte]
    return year, month, lines


@settlements_bp.get("/buchungen")
@login_required
def bookings():
    _admin()
    year, month, lines = _drill_lines()
    total = sum((line.amount or 0 for line in lines), start=0)
    if request.args.get("teil") == "1":  # beim Aufklappen in der Uebersicht nachgeladen
        return render_template("settlements/_lines.html", lines=lines, total=total, type_labels=svc.TYPE_LABELS)
    return render_template("settlements/lines.html", lines=lines, total=total, label=svc.month_label(year, month), type_labels=svc.TYPE_LABELS,
                           args=request.args)


@settlements_bp.get("/export.csv")
@login_required
def export():
    _admin()
    year, month, lines = _drill_lines()
    out = io.StringIO()
    writer = csv.writer(out, delimiter=";")
    writer.writerow(["Beleg", "Seite", "Mitarbeiter-Vermittlernr.", "Datum", "Referenz", "Sparte", "Buchungsart", "Betrag", "10-EUR", "Geprueft"])
    for line in lines:
        writer.writerow([line.settlement.original_filename, line.page, line.settlement.broker_number or "", line.booking_date or "", line.contract_ref or "",
                         line.sparte or "", svc.TYPE_LABELS.get(line.booking_type or "", ""), str(line.amount).replace(".", ","), "ja" if line.is_ten else "",
                         line.review_state or ""])
    data = io.BytesIO(("﻿" + out.getvalue()).encode("utf-8"))
    response = send_file(data, mimetype="text/csv", as_attachment=True, download_name=f"abrechnungen-{year}-{month:02d}.csv", max_age=0)
    response.headers["Cache-Control"] = "private, no-store"
    return response
