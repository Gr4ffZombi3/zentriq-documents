from datetime import datetime, timezone

from flask_login import UserMixin
from werkzeug.security import check_password_hash, generate_password_hash

from app.extensions import db
from app.models.enums import UserRole
from app.tenancy import TenantScopedMixin


class User(TenantScopedMixin, UserMixin, db.Model):
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(255), nullable=False, unique=True, index=True)
    vermittlernummer = db.Column(db.String(50), nullable=True, unique=True, index=True)
    password_hash = db.Column(db.String(255), nullable=False)
    is_active = db.Column(db.Boolean, nullable=False, default=True)
    is_email_verified = db.Column(db.Boolean, nullable=False, default=False)
    # Standard ist die Rolle mit den geringsten Rechten; Admins werden explizit vergeben.
    role = db.Column(
        db.Enum(UserRole), nullable=False, default=UserRole.EMPLOYEE, server_default=UserRole.EMPLOYEE.name
    )

    # Soft-Delete: geloeschte Konten bleiben fuer Zeiterfassungs-/Audit-Historie erhalten,
    # sind aber dauerhaft deaktiviert und erscheinen in keiner Verwaltungsliste mehr.
    deleted_at = db.Column(db.DateTime, nullable=True)
    deleted_by_user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="SET NULL"), nullable=True)

    # Wird bei Passwortaenderung, Deaktivierung, Loeschung und 2FA-Reset erhoeht. Ist Teil der
    # Session-ID (get_id), dadurch werden alle bestehenden Sessions sofort ungueltig.
    auth_version = db.Column(db.Integer, nullable=False, default=0, server_default="0")

    # TOTP-Secret, verschluesselt (app/services/two_factor.py). Gesetzt + totp_enabled_at NULL
    # bedeutet: Einrichtung begonnen, aber noch nicht mit einem gueltigen Code bestaetigt.
    totp_secret_encrypted = db.Column(db.String(255), nullable=True)
    totp_enabled_at = db.Column(db.DateTime, nullable=True)
    # Zuletzt akzeptierter TOTP-Zeitschritt - verhindert die Wiederverwendung eines Codes.
    totp_last_counter = db.Column(db.BigInteger, nullable=True)

    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = db.Column(
        db.DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )
    last_login_at = db.Column(db.DateTime, nullable=True)

    @property
    def is_super_admin(self) -> bool:
        return self.role == UserRole.SUPER_ADMIN

    @property
    def is_office_admin(self) -> bool:
        return self.role == UserRole.OFFICE_ADMIN

    @property
    def is_employee(self) -> bool:
        return self.role == UserRole.EMPLOYEE

    @property
    def is_admin(self) -> bool:
        """Verwaltungsrechte innerhalb eines Bueros (fachliche Daten). Bewusst NICHT fuer
        SUPER_ADMIN - der Plattformbetreiber hat keinen Zugriff auf Buerodaten."""
        return self.is_office_admin

    @property
    def is_deleted(self) -> bool:
        return self.deleted_at is not None

    @property
    def two_factor_enabled(self) -> bool:
        return self.totp_enabled_at is not None and self.totp_secret_encrypted is not None

    def get_id(self) -> str:
        return f"{self.id}:{self.auth_version or 0}"

    def invalidate_sessions(self) -> None:
        self.auth_version = (self.auth_version or 0) + 1
        if self.id is not None:
            # Auch die Sitzungsliste ("Mein Konto") bereinigen - abgemeldet sind die Sitzungen
            # bereits ueber auth_version.
            from app.models.user_session import UserSession

            UserSession.query.filter(UserSession.user_id == self.id, UserSession.revoked_at.is_(None)).update(
                {UserSession.revoked_at: datetime.now(timezone.utc).replace(tzinfo=None)},
                synchronize_session=False,
            )

    def set_password(self, password: str) -> None:
        self.password_hash = generate_password_hash(password, method="scrypt")

    def check_password(self, password: str) -> bool:
        return check_password_hash(self.password_hash, password)

    def __repr__(self):
        return f"<User {self.id} {self.email!r}>"


class RecoveryCode(db.Model):
    """Einmal-Wiederherstellungscode fuer die Zwei-Faktor-Anmeldung. Gespeichert wird nur ein
    HMAC des Codes. Absichtlich NICHT TenantScopedMixin: wird waehrend der Anmeldung (vor dem
    Setzen des Tenant-Kontexts) ausschliesslich ueber user_id abgefragt."""

    __tablename__ = "user_recovery_codes"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    code_hash = db.Column(db.String(128), nullable=False)
    used_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=lambda: datetime.now(timezone.utc))
