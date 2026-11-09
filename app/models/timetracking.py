"""Arbeitszeiterfassung.

Grundsaetze:
- Stempelzeiten (started_at/ended_at) erzeugt ausschliesslich der Server (UTC, naiv
  gespeichert wie im Rest des Projekts). Kein Endpunkt nimmt beim Stempeln eine Zeit an.
- Buchungen werden nie geloescht, sondern hoechstens storniert (voided_at).
- Jede nachtraegliche Aenderung erzeugt append-only TimeCorrection-Eintraege mit altem und
  neuem Wert, Akteur, Zeitpunkt und Begruendung.
- `open_marker` ist 1 fuer die (einzige) offene Buchung/Pause eines Nutzers und NULL sonst.
  Der Unique-Index (user_id, open_marker) erzwingt damit auch in MariaDB (ohne partielle
  Indizes) hoechstens eine offene Buchung bzw. Pause pro Person - auch bei Doppelklicks
  oder parallelen Requests.
"""

from datetime import datetime, timezone

from app.extensions import db
from app.models.enums import CorrectionRequestStatus, TimeEntrySource
from app.tenancy import TenantScopedMixin


def utcnow_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class EmployeeProfile(TenantScopedMixin, db.Model):
    __tablename__ = "employee_profiles"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, unique=True)
    display_name = db.Column(db.String(120), nullable=True)
    personnel_number = db.Column(db.String(50), nullable=True)
    # Sollarbeitszeit pro Woche in Minuten und Arbeitstage als ISO-Wochentage ("12345" = Mo-Fr).
    weekly_target_minutes = db.Column(db.Integer, nullable=False, default=2400)
    workdays = db.Column(db.String(7), nullable=False, default="12345")
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow_naive)
    updated_at = db.Column(db.DateTime, nullable=False, default=utcnow_naive, onupdate=utcnow_naive)

    user = db.relationship("User", backref=db.backref("employee_profile", uselist=False))
    # Wochenplaene mit Gueltigkeitsdatum (aelteste zuerst). Ohne Plan: gleichmaessige Verteilung.
    plans = db.relationship(
        "EmployeeWorkPlan", primaryjoin="foreign(EmployeeWorkPlan.user_id) == EmployeeProfile.user_id",
        order_by="EmployeeWorkPlan.valid_from", viewonly=True, lazy="selectin",
    )

    def plan_for(self, day):
        """Wochenplan, der an `day` gilt (juengster mit valid_from <= day), sonst None."""
        current = None
        for plan in self.plans or []:
            if plan.valid_from <= day:
                current = plan
        return current

    @property
    def current_weekly_minutes(self) -> int:
        from app.services.timetracking.clock import local_today

        return self.weekly_minutes_on(local_today())

    def weekly_minutes_on(self, day) -> int:
        plan = self.plan_for(day)
        return plan.weekly_minutes if plan is not None else int(self.weekly_target_minutes or 0)

    @property
    def workday_numbers(self) -> set[int]:
        return {int(ch) for ch in (self.workdays or "") if ch in "1234567"}

    @property
    def day_plan_label(self) -> str:
        """Heute geltender Plan wie berechnet (calc.target_seconds_for_day)."""
        from app.services.timetracking.clock import local_today

        plan = self.plan_for(local_today())
        if plan is not None:
            return plan.label
        days = sorted(self.workday_numbers)
        if not days:
            return "keine Arbeitstage"
        minutes = round((self.weekly_target_minutes or 0) / len(days))
        names = ", ".join(("Mo", "Di", "Mi", "Do", "Fr", "Sa", "So")[d - 1] for d in days)
        return f"{names} je {minutes // 60}:{minutes % 60:02d} Std., übrige Tage 0 Std."


class WorkSession(TenantScopedMixin, db.Model):
    __tablename__ = "work_sessions"
    __table_args__ = (db.UniqueConstraint("user_id", "open_marker", name="uq_work_sessions_user_open"),)

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    # Kalendertag (Europe/Berlin) des Arbeitsbeginns - Grundlage fuer Tages-/Wochen-/Monatswerte.
    work_date = db.Column(db.Date, nullable=False, index=True)
    started_at = db.Column(db.DateTime, nullable=False)
    ended_at = db.Column(db.DateTime, nullable=True)
    open_marker = db.Column(db.SmallInteger, nullable=True)
    source = db.Column(db.Enum(TimeEntrySource), nullable=False, default=TimeEntrySource.STAMP)
    is_corrected = db.Column(db.Boolean, nullable=False, default=False)
    voided_at = db.Column(db.DateTime, nullable=True)
    voided_by_user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    created_by_user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow_naive)

    user = db.relationship("User", foreign_keys=[user_id])
    breaks = db.relationship(
        "WorkBreak",
        back_populates="work_session",
        order_by="WorkBreak.started_at",
    )

    @property
    def is_voided(self) -> bool:
        return self.voided_at is not None

    @property
    def is_open(self) -> bool:
        return self.open_marker == 1 and self.voided_at is None

    @property
    def is_incomplete(self) -> bool:
        """Ausstempeln fehlte: die Buchung wurde ohne erfundene Endzeit aus dem offenen
        Zustand geloest und wartet auf eine Korrektur."""
        return self.ended_at is None and self.open_marker is None and self.voided_at is None

    @property
    def active_breaks(self):
        return [item for item in self.breaks if item.voided_at is None]


class WorkBreak(TenantScopedMixin, db.Model):
    __tablename__ = "work_breaks"
    __table_args__ = (db.UniqueConstraint("user_id", "open_marker", name="uq_work_breaks_user_open"),)

    id = db.Column(db.Integer, primary_key=True)
    work_session_id = db.Column(db.Integer, db.ForeignKey("work_sessions.id"), nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    started_at = db.Column(db.DateTime, nullable=False)
    ended_at = db.Column(db.DateTime, nullable=True)
    open_marker = db.Column(db.SmallInteger, nullable=True)
    is_corrected = db.Column(db.Boolean, nullable=False, default=False)
    voided_at = db.Column(db.DateTime, nullable=True)
    voided_by_user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow_naive)

    work_session = db.relationship("WorkSession", back_populates="breaks")

    @property
    def is_open(self) -> bool:
        return self.open_marker == 1 and self.voided_at is None


class TimeCorrectionRequest(TenantScopedMixin, db.Model):
    """Korrekturantrag eines Mitarbeiters. Aendert selbst nichts - erst die Genehmigung durch
    einen Admin wendet die Werte an (mit TimeCorrection-Protokoll)."""

    __tablename__ = "time_correction_requests"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    # NULL = Antrag auf Nachtrag einer fehlenden Buchung.
    work_session_id = db.Column(db.Integer, db.ForeignKey("work_sessions.id"), nullable=True, index=True)
    work_date = db.Column(db.Date, nullable=False)
    original_started_at = db.Column(db.DateTime, nullable=True)
    original_ended_at = db.Column(db.DateTime, nullable=True)
    requested_started_at = db.Column(db.DateTime, nullable=False)
    requested_ended_at = db.Column(db.DateTime, nullable=False)
    reason = db.Column(db.Text, nullable=False)
    status = db.Column(
        db.Enum(CorrectionRequestStatus), nullable=False, default=CorrectionRequestStatus.PENDING, index=True
    )
    decided_by_user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    decided_at = db.Column(db.DateTime, nullable=True)
    decision_note = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow_naive, index=True)

    user = db.relationship("User", foreign_keys=[user_id])
    decided_by = db.relationship("User", foreign_keys=[decided_by_user_id])
    work_session = db.relationship("WorkSession")


class TimeCorrection(TenantScopedMixin, db.Model):
    """Revisionsprotokoll jeder nachtraeglichen Aenderung an Buchungen/Pausen. Append-only:
    im Code gibt es keinen Update- oder Delete-Pfad fuer diese Tabelle."""

    __tablename__ = "time_corrections"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    target_type = db.Column(db.String(16), nullable=False)  # "session" | "break"
    target_id = db.Column(db.Integer, nullable=False)
    action = db.Column(db.String(16), nullable=False)  # "update" | "create" | "void"
    field = db.Column(db.String(32), nullable=True)
    old_value = db.Column(db.String(64), nullable=True)
    new_value = db.Column(db.String(64), nullable=True)
    reason = db.Column(db.Text, nullable=True)
    request_id = db.Column(db.Integer, db.ForeignKey("time_correction_requests.id"), nullable=True)
    corrected_by_user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    corrected_by_email_snapshot = db.Column(db.String(255), nullable=True)
    ip_address = db.Column(db.String(64), nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow_naive, index=True)

    user = db.relationship("User", foreign_keys=[user_id])


WEEKDAY_SHORT = ("Mo", "Di", "Mi", "Do", "Fr", "Sa", "So")


class EmployeeWorkPlan(TenantScopedMixin, db.Model):
    """Wochenplan eines Mitarbeiters ab einem Stichtag. days: {"1": {"start": "10:00", "end": "14:00",
    "break": 0}, ...} - nur Arbeitstage (ISO-Wochentag); fehlende Tage = frei (0 Std.)."""

    __tablename__ = "employee_work_plans"
    __table_args__ = (db.UniqueConstraint("user_id", "valid_from", name="uq_employee_work_plans_user_from"),)

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    valid_from = db.Column(db.Date, nullable=False)
    days = db.Column(db.JSON, nullable=False)
    created_by_user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow_naive)

    @staticmethod
    def _minutes(value: str) -> int:
        h, m = value.split(":")
        return int(h) * 60 + int(m)

    def day_minutes(self, isoweekday: int) -> int:
        d = (self.days or {}).get(str(isoweekday))
        if not d:
            return 0
        return max(0, self._minutes(d["end"]) - self._minutes(d["start"]) - int(d.get("break") or 0))

    @property
    def weekly_minutes(self) -> int:
        return sum(self.day_minutes(i) for i in range(1, 8))

    @property
    def label(self) -> str:
        parts = []
        for i in range(1, 8):
            d = (self.days or {}).get(str(i))
            if d:
                m = self.day_minutes(i)
                pause = f", {int(d.get('break') or 0)} Min. Pause" if int(d.get("break") or 0) else ""
                parts.append(f"{WEEKDAY_SHORT[i - 1]} {d['start']}–{d['end']}{pause} ({m // 60}:{m % 60:02d})")
        return "; ".join(parts) + ", übrige Tage 0 Std." if parts else "keine Arbeitstage"
