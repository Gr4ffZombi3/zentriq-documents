from flask_wtf import FlaskForm
from wtforms import (
    BooleanField,
    PasswordField,
    RadioField,
    SelectMultipleField,
    StringField,
    SubmitField,
    ValidationError,
    widgets,
)
from wtforms.validators import DataRequired, Email, EqualTo, Length, Optional


class ChangePasswordForm(FlaskForm):
    current_password = PasswordField("Aktuelles Passwort", validators=[DataRequired()])
    new_password = PasswordField(
        "Neues Passwort", validators=[DataRequired(), Length(min=8, message="Mindestens 8 Zeichen.")]
    )
    new_password_confirm = PasswordField(
        "Neues Passwort bestätigen",
        validators=[DataRequired(), EqualTo("new_password", message="Passwörter stimmen nicht überein.")],
    )
    submit = SubmitField("Passwort ändern")


WEEKDAY_CHOICES = [("1", "Mo"), ("2", "Di"), ("3", "Mi"), ("4", "Do"), ("5", "Fr"), ("6", "Sa"), ("7", "So")]


class MultiCheckboxField(SelectMultipleField):
    widget = widgets.ListWidget(prefix_label=False)
    option_widget = widgets.CheckboxInput()


def _parse_hours(value: str) -> float:
    return float((value or "").strip().replace(",", "."))


OFFICE_ROLE_CHOICES = [
    ("office_admin", "Büro-Admin – verwaltet das Büro, alle Bereiche und alle Mitarbeiter"),
    ("employee", "Mitarbeiter – eigene Leipziger-Liste-Vorgänge, Memo und eigene Zeiterfassung"),
]
PLATFORM_ROLE_CHOICES = OFFICE_ROLE_CHOICES + [
    ("super_admin", "Super-Admin – Plattformverwaltung, kein Zugriff auf Bürodaten"),
]


class UserForm(FlaskForm):
    """Anlage/Bearbeitung eines Benutzers durch einen Admin. Die Rolle hat bewusst keinen
    Standardwert - sie muss ausdruecklich gewaehlt werden. Die Auswahl ist auf die Rollen
    begrenzt, die der Akteur vergeben darf (RadioField lehnt andere Werte serverseitig ab)."""

    email = StringField("E-Mail (Login)", validators=[DataRequired(), Email(), Length(max=255)])
    display_name = StringField("Anzeigename", validators=[Optional(), Length(max=120)])
    personnel_number = StringField("Personalnummer", validators=[Optional(), Length(max=50)])
    vermittlernummer = StringField("Vermittlernummer (Zuordnung Leipziger Liste, alternativer Login)", validators=[Optional(), Length(max=50)])
    role = RadioField(
        "Rolle",
        choices=OFFICE_ROLE_CHOICES,
        validators=[DataRequired(message="Bitte eine Rolle auswählen.")],
    )
    weekly_hours = StringField("Sollstunden pro Woche", validators=[DataRequired()], default="40")
    workdays = MultiCheckboxField("Arbeitstage", choices=WEEKDAY_CHOICES, default=["1", "2", "3", "4", "5"])
    is_active = BooleanField("Konto aktiv", default=True)
    password = PasswordField("Passwort", validators=[Optional(), Length(min=8, message="Mindestens 8 Zeichen.")])
    password_confirm = PasswordField(
        "Passwort bestätigen", validators=[EqualTo("password", message="Passwörter stimmen nicht überein.")]
    )
    submit = SubmitField("Speichern")

    def validate_weekly_hours(self, field):
        try:
            hours = _parse_hours(field.data)
        except ValueError as exc:
            raise ValidationError("Bitte eine Zahl angeben, z. B. 40 oder 38,5.") from exc
        if not 0 <= hours <= 80:
            raise ValidationError("Die Sollstunden müssen zwischen 0 und 80 liegen.")

    def validate_workdays(self, field):
        if not field.data:
            raise ValidationError("Bitte mindestens einen Arbeitstag auswählen.")

    @property
    def weekly_target_minutes(self) -> int:
        return round(_parse_hours(self.weekly_hours.data) * 60)


class PlatformUserForm(UserForm):
    """Benutzerverwaltung durch den SUPER_ADMIN: zusaetzlich die Rolle Super-Admin."""

    role = RadioField(
        "Rolle",
        choices=PLATFORM_ROLE_CHOICES,
        validators=[DataRequired(message="Bitte eine Rolle auswählen.")],
    )


class TenantForm(FlaskForm):
    name = StringField("Name des Büros", validators=[DataRequired(), Length(max=255)])
    is_active = BooleanField("Büro aktiv", default=True)
    submit = SubmitField("Speichern")


class TenantCreateForm(UserForm):
    """Neues Buero inklusive erstem Buero-Admin."""

    tenant_name = StringField("Name des Büros", validators=[DataRequired(), Length(max=255)])
    role = RadioField("Rolle", choices=[("office_admin", "Büro-Admin")], default="office_admin")


class TwoFactorCodeForm(FlaskForm):
    code = StringField("Code aus der Authenticator-App", validators=[DataRequired(message="Bitte den Code eingeben."), Length(max=32)])
    submit = SubmitField("Bestätigen")
