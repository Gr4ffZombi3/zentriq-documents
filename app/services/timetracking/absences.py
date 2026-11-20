"""Krankheit und Urlaub in der Zeiterfassung (nur Buero-Admin schreibt; Aufrufer prueft die Rolle).

- Gespeichert werden nur Art, Zeitraum und Bearbeitungsstatus - keine Diagnose, keine Stunden.
- Ausgefallene Sollstunden werden aus dem Arbeitszeitmodell berechnet (calc.summarize_period):
  Tagessoll minus bereits geleistete Arbeitszeit, Feiertage 0. Korrekturen an Zeitraum oder
  Buchungen wirken dadurch sofort in Stundenverlauf, Monatsbericht und Export.
- Keine doppelte Krankheit (bzw. doppelter Urlaub) fuer denselben Tag; Urlaub nicht ueber
  Krankheit. Krankheit ueber Urlaub ist erlaubt - diese Tage zaehlen dann als Krankheit.
- Jede Aenderung landet mit Bearbeiter und Zeitpunkt im Korrekturprotokoll (TimeCorrection).
- Die Uebersicht dokumentiert Ausfallzeiten als Grundlage zur Pruefung eines
  Erstattungsantrags; ein Anspruch oder Betrag wird daraus nicht abgeleitet."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from app.extensions import db
from app.models import TimeAbsence, User, VacationEntitlement
from app.models.office_settings import OfficeSettings
from app.models.timetracking import ABSENCE_KINDS, REFUND_STATUSES, utcnow_naive
from app.services.customer_lock import CustomerLockTimeout, NamedLock
from app.services.timetracking.calc import PeriodSummary, summarize_period
from app.services.timetracking.holidays import DEFAULT_STATE, STATES, holidays_between

MAX_DAYS = 366


class AbsenceError(ValueError):
    pass


def _fmt(day: date) -> str:
    return day.strftime("%d.%m.%Y")


def period_text(start: date, end: date) -> str:
    return _fmt(start) if start == end else f"{_fmt(start)} – {_fmt(end)}"


# --- Feiertage ----------------------------------------------------------------------------------


def holiday_state() -> str:
    settings = OfficeSettings.query.first()
    state = settings.holiday_state if settings is not None else None
    return state if state in STATES else DEFAULT_STATE


def holidays(start: date, end: date) -> dict[date, str]:
    return holidays_between(start, end, holiday_state())


def set_holiday_state(actor: User, state: str) -> None:
    if state not in STATES:
        raise AbsenceError("Bitte ein Bundesland auswählen.")
    settings = OfficeSettings.query.first()
    if settings is None:
        settings = OfficeSettings(tenant_id=actor.tenant_id)
        db.session.add(settings)
    settings.holiday_state = state
    settings.updated_by_user_id = actor.id
    db.session.commit()


# --- Laden / Zusammenfassen -----------------------------------------------------------------------


def active_between(user_ids, start: date, end: date, kind: str | None = None) -> list[TimeAbsence]:
    user_ids = list(user_ids)
    if not user_ids:
        return []
    query = TimeAbsence.query.filter(
        TimeAbsence.user_id.in_(user_ids), TimeAbsence.deleted_at.is_(None),
        TimeAbsence.start_date <= end, TimeAbsence.end_date >= start,
    )
    if kind:
        query = query.filter(TimeAbsence.kind == kind)
    return query.order_by(TimeAbsence.start_date, TimeAbsence.id).all()


def summarize(user: User, sessions, start: date, end: date, today: date, now: datetime) -> PeriodSummary:
    return summarize_period(sessions, start, end, user.employee_profile, today, now,
                            absences=active_between([user.id], start, end), holidays=holidays(start, end))


# --- Pruefen und Speichern ------------------------------------------------------------------------


def parse_day(value, label: str) -> date:
    try:
        return date.fromisoformat((value or "").strip())
    except ValueError:
        raise AbsenceError(f"Bitte {label} als Datum angeben.") from None


def _validate(member: User, kind: str, start: date, end: date, exclude_id: int | None = None) -> list[str]:
    """Prueft Zeitraum und Ueberschneidungen. Gibt verstaendliche Hinweise zurueck (keine Fehler)."""
    if kind not in ABSENCE_KINDS:
        raise AbsenceError("Bitte Krankheit oder Urlaub auswählen.")
    if end < start:
        raise AbsenceError("Das Ende darf nicht vor dem Beginn liegen.")
    if (end - start).days + 1 > MAX_DAYS:
        raise AbsenceError(f"Bitte höchstens {MAX_DAYS} Tage auf einmal eintragen.")
    others = [item for item in active_between([member.id], start, end) if item.id != exclude_id]
    same = [item for item in others if item.kind == kind]
    if same:
        hit = same[0]
        what = "Krankheit" if kind == "sick" else "Urlaub"
        raise AbsenceError(
            f"Für diesen Zeitraum ist bereits {what} eingetragen ({period_text(hit.start_date, hit.end_date)}). "
            "Bitte den vorhandenen Eintrag bearbeiten, statt ihn doppelt anzulegen.")
    notes = []
    if kind == "vacation":
        sick = [item for item in others if item.kind == "sick"]
        if sick:
            hit = sick[0]
            raise AbsenceError(
                f"Im Zeitraum ist bereits Krankheit eingetragen ({period_text(hit.start_date, hit.end_date)}). "
                "Urlaub kann nicht auf Krankheitstage gelegt werden – bitte den Urlaub davor bzw. danach eintragen.")
    else:
        vacation = [item for item in others if item.kind == "vacation"]
        if vacation:
            days = sum((min(end, v.end_date) - max(start, v.start_date)).days + 1 for v in vacation)
            notes.append(f"An {days} {'Tag' if days == 1 else 'Tagen'} war Urlaub eingetragen. Diese Tage zählen jetzt als "
                         "Krankheit, nicht als Urlaub.")
    return notes


def _effect_notes(member: User, start: date, end: date) -> list[str]:
    from app.services.timetracking.service import sessions_between

    notes = []
    worked = sorted({item.work_date for item in sessions_between([member.id], start, end)})
    if worked:
        notes.append(f"An {len(worked)} {'Tag' if len(worked) == 1 else 'Tagen'} ist bereits Arbeitszeit erfasst "
                     f"({', '.join(d.strftime('%d.%m.') for d in worked[:5])}{' …' if len(worked) > 5 else ''}). "
                     "Sie bleibt erhalten; angerechnet werden nur die verbleibenden Sollstunden.")
    free = holidays(start, end)
    if free:
        notes.append("Feiertag im Zeitraum (" + ", ".join(f"{name} {d.strftime('%d.%m.')}" for d, name in sorted(free.items()))
                     + ") – zählt nicht als ausgefallener Arbeitstag.")
    return notes


def _record(actor: User, absence: TimeAbsence, action: str, field_name: str | None, old, new) -> None:
    from app.services.timetracking.service import _record as record

    record(actor, absence, "absence", action, field=field_name, old=old, new=new)


def _value(absence_kind: str, start: date, end: date) -> str:
    return f"{ABSENCE_KINDS[absence_kind]} {period_text(start, end)}"


def create(actor: User, member: User, kind: str, start: date, end: date) -> tuple[TimeAbsence, list[str]]:
    try:
        with NamedLock(f"zq-absence-{member.id}", 10):
            db.session.commit()  # frischer Datenstand fuer die Doppelpruefung
            notes = _validate(member, kind, start, end)
            absence = TimeAbsence(tenant_id=member.tenant_id, user_id=member.id, kind=kind, start_date=start, end_date=end,
                                  refund_status="open" if kind == "sick" else None,
                                  created_by_user_id=actor.id, updated_by_user_id=actor.id)
            db.session.add(absence)
            db.session.flush()
            _record(actor, absence, "create", "zeitraum", None, _value(kind, start, end))
            db.session.commit()
    except CustomerLockTimeout:
        db.session.rollback()
        raise AbsenceError("Der Eintrag wird gerade gespeichert. Bitte kurz warten und die Seite neu laden.") from None
    return absence, notes + _effect_notes(member, start, end)


def update(actor: User, absence: TimeAbsence, start: date, end: date) -> list[str]:
    member = db.session.get(User, absence.user_id)
    try:
        with NamedLock(f"zq-absence-{member.id}", 10):
            db.session.commit()
            db.session.refresh(absence)
            if absence.deleted_at is not None:
                raise AbsenceError("Der Eintrag wurde inzwischen entfernt.")
            notes = _validate(member, absence.kind, start, end, exclude_id=absence.id)
            if (start, end) != (absence.start_date, absence.end_date):
                old = _value(absence.kind, absence.start_date, absence.end_date)
                absence.start_date, absence.end_date, absence.updated_by_user_id = start, end, actor.id
                _record(actor, absence, "update", "zeitraum", old, _value(absence.kind, start, end))
            db.session.commit()
    except CustomerLockTimeout:
        db.session.rollback()
        raise AbsenceError("Der Eintrag wird gerade gespeichert. Bitte kurz warten und die Seite neu laden.") from None
    return notes + _effect_notes(member, start, end)


def remove(actor: User, absence: TimeAbsence) -> None:
    if absence.deleted_at is not None:
        return
    absence.deleted_at, absence.deleted_by_user_id, absence.updated_by_user_id = utcnow_naive(), actor.id, actor.id
    _record(actor, absence, "void", "zeitraum", _value(absence.kind, absence.start_date, absence.end_date), None)
    db.session.commit()


def set_status(actor: User, absence: TimeAbsence, status: str) -> bool:
    if absence.kind != "sick":
        raise AbsenceError("Der Bearbeitungsstatus gilt nur für Krankheit.")
    if status not in REFUND_STATUSES:
        raise AbsenceError("Bitte einen gültigen Bearbeitungsstatus wählen.")
    old = absence.refund_status or "open"
    if old == status:
        return False
    absence.refund_status, absence.updated_by_user_id = status, actor.id
    _record(actor, absence, "status", "status", REFUND_STATUSES[old], REFUND_STATUSES[status])
    db.session.commit()
    return True


# --- Monatsuebersicht fuer die Erstattungspruefung -----------------------------------------------


@dataclass
class AbsenceDay:
    day: date
    planned_seconds: int
    worked_seconds: int
    missed_seconds: int
    holiday: str | None


@dataclass
class OverviewRow:
    member: User
    absence: TimeAbsence
    days: list[AbsenceDay] = field(default_factory=list)  # nur Tage im gewaehlten Monat
    total_days: int = 0  # ausgefallene geplante Arbeitstage im gesamten Zeitraum
    total_seconds: int = 0

    @property
    def month_days(self) -> int:
        return sum(1 for item in self.days if item.missed_seconds)

    @property
    def month_seconds(self) -> int:
        return sum(item.missed_seconds for item in self.days)

    @property
    def month_start(self) -> date:
        return self.days[0].day

    @property
    def month_end(self) -> date:
        return self.days[-1].day

    @property
    def crosses_month(self) -> bool:
        return (self.absence.start_date, self.absence.end_date) != (self.month_start, self.month_end)


def absence_days(member: User, absence: TimeAbsence, now: datetime | None = None) -> list[AbsenceDay]:
    """Alle Tage des Zeitraums mit geplantem Soll (auch kuenftige), geleisteter Zeit und
    ausgefallenen Stunden - unabhaengig von 'Soll bis heute'."""
    from app.services.timetracking.service import sessions_between

    now = now or utcnow_naive()
    sessions = sessions_between([member.id], absence.start_date, absence.end_date)
    summary = summarize_period(sessions, absence.start_date, absence.end_date, member.employee_profile,
                               absence.end_date, now, absences=[absence],
                               holidays=holidays(absence.start_date, absence.end_date))
    return [AbsenceDay(day=item.day, planned_seconds=item.target_seconds, worked_seconds=item.net_seconds,
                       missed_seconds=item.absence_seconds, holiday=item.holiday) for item in summary.days]


def overview(members: list[User], month_start: date, month_end: date, kind: str = "sick") -> list[OverviewRow]:
    by_id = {item.id: item for item in members}
    rows = []
    for absence in active_between(by_id, month_start, month_end, kind=kind):
        member = by_id[absence.user_id]
        all_days = absence_days(member, absence)
        row = OverviewRow(member=member, absence=absence,
                          days=[item for item in all_days if month_start <= item.day <= month_end],
                          total_days=sum(1 for item in all_days if item.missed_seconds),
                          total_seconds=sum(item.missed_seconds for item in all_days))
        rows.append(row)
    return rows



# --- Urlaubskonto --------------------------------------------------------------------------------


def format_days(value) -> str:
    """Decimal/float/int -> '30' bzw. '2,5' (deutsche Schreibweise)."""
    if value is None:
        return "–"
    number = Decimal(str(value)).quantize(Decimal("0.1"))
    text = f"{number:f}".rstrip("0").rstrip(".") if "." in f"{number:f}" else f"{number:f}"
    return text.replace(".", ",").replace("-", "−")


def parse_days(value, label: str) -> Decimal:
    raw = (value or "").strip().replace(",", ".") or "0"
    try:
        number = Decimal(raw)
    except InvalidOperation:
        raise AbsenceError(f"Bitte {label} als Zahl angeben (z. B. 30 oder 2,5).") from None
    if number < 0 or number > 365:
        raise AbsenceError(f"{label[0].upper() + label[1:]} muss zwischen 0 und 365 Tagen liegen.")
    if number * 2 != (number * 2).to_integral_value():
        raise AbsenceError(f"{label[0].upper() + label[1:]}: bitte ganze oder halbe Tage angeben.")
    return number.quantize(Decimal("0.1"))


@dataclass
class VacationAccount:
    member: User
    year: int
    entitlement: VacationEntitlement | None
    taken: int = 0  # Urlaubstage bis einschl. heute
    planned: int = 0  # kuenftige Urlaubstage

    @property
    def has_entitlement(self) -> bool:
        return self.entitlement is not None

    @property
    def days(self) -> Decimal:
        return Decimal(self.entitlement.days) if self.entitlement else Decimal(0)

    @property
    def carryover(self) -> Decimal:
        return Decimal(self.entitlement.carryover) if self.entitlement else Decimal(0)

    @property
    def total(self) -> Decimal:
        return self.days + self.carryover

    @property
    def used(self) -> int:
        return self.taken + self.planned

    @property
    def rest(self) -> Decimal | None:
        return self.total - self.used if self.entitlement else None


def _vacation_days(member: User, year: int, today: date, now: datetime) -> tuple[int, int]:
    """Gezaehlt werden geplante Arbeitstage mit Urlaub: keine Feiertage, keine freien Tage,
    keine Krankheitstage (Krankheit hat Vorrang) und keine voll gearbeiteten Tage."""
    from app.services.timetracking.service import sessions_between

    start, end = date(year, 1, 1), date(year, 12, 31)
    items = active_between([member.id], start, end)
    if not any(item.kind == "vacation" for item in items):
        return 0, 0
    first = max(start, min(item.start_date for item in items if item.kind == "vacation"))
    last = min(end, max(item.end_date for item in items if item.kind == "vacation"))
    summary = summarize_period(sessions_between([member.id], first, last), first, last, member.employee_profile,
                               last, now, absences=items, holidays=holidays(first, last))
    taken = sum(1 for d in summary.days if d.vacation_seconds and d.day <= today)
    planned = sum(1 for d in summary.days if d.vacation_seconds and d.day > today)
    return taken, planned


def vacation_account(member: User, year: int, now: datetime | None = None) -> VacationAccount:
    from app.services.timetracking.clock import local_today

    now = now or utcnow_naive()
    entitlement = VacationEntitlement.query.filter_by(user_id=member.id, year=year).first()
    taken, planned = _vacation_days(member, year, local_today(now), now)
    return VacationAccount(member=member, year=year, entitlement=entitlement, taken=taken, planned=planned)


def save_entitlement(actor: User, member: User, year: int, days_value, carryover_value) -> VacationEntitlement:
    if not 2000 <= year <= 2100:
        raise AbsenceError("Ungültiges Jahr.")
    days = parse_days(days_value, "den Jahresanspruch")
    carryover = parse_days(carryover_value, "den Übertrag")
    row = VacationEntitlement.query.filter_by(user_id=member.id, year=year).first()
    created = row is None
    if created:
        row = VacationEntitlement(tenant_id=member.tenant_id, user_id=member.id, year=year, days=0, carryover=0)
        db.session.add(row)
        db.session.flush()
    from app.services.timetracking.service import _record as record

    for field_name, label, new in (("days", "anspruch", days), ("carryover", "uebertrag", carryover)):
        old = None if created else Decimal(getattr(row, field_name))
        if created or old != new:
            record(actor, row, "entitlement", "create" if created else "update", field=label,
                   old=None if old is None else f"{year}: {format_days(old)} Tage", new=f"{year}: {format_days(new)} Tage")
            setattr(row, field_name, new)
    row.updated_by_user_id = actor.id
    db.session.commit()
    return row


def vacation_warning(member: User, start: date, end: date) -> list[str]:
    notes = []
    for year in range(start.year, end.year + 1):
        account = vacation_account(member, year)
        if account.rest is not None and account.rest < 0:
            notes.append(f"Achtung: Damit sind {format_days(-account.rest)} Urlaubstage mehr eingetragen, als im "
                         f"Urlaubskonto {year} verfügbar sind (Anspruch und Übertrag {format_days(account.total)} Tage).")
        elif account.entitlement is None:
            notes.append(f"Für {year} ist noch kein Urlaubsanspruch hinterlegt – bitte im Urlaubskonto eintragen.")
    return notes
