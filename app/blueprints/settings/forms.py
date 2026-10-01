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


class UserForm(FlaskForm):
    """Anlage/Bearbeitung eines Benutzers durch einen Admin. Die Rolle hat bewusst keinen
    Standardwert - sie muss ausdruecklich gewaehlt werden."""

    email = StringField("E-Mail (Login)", validators=[DataRequired(), Email(), Length(max=255)])
    display_name = StringField("Anzeigename", validators=[Optional(), Length(max=120)])
    personnel_number = StringField("Personalnummer", validators=[Optional(), Length(max=50)])
    vermittlernummer = StringField("Vermittlernummer (Zuordnung Leipziger Liste, alternativer Login)", validators=[Optional(), Length(max=50)])
    role = RadioField(
        "Rolle",
        choices=[("admin", "Admin – alle Bereiche"), ("mitarbeiter", "Mitarbeiter – nur Zeiterfassung")],
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
