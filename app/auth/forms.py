from flask_wtf import FlaskForm
from wtforms import (
    BooleanField,
    HiddenField,
    PasswordField,
    RadioField,
    StringField,
    SubmitField,
    TextAreaField,
)
from wtforms.validators import DataRequired, Email, EqualTo, Length, Optional


class LoginForm(FlaskForm):
    login_type = RadioField(
        "Anmelden mit",
        choices=[("email", "E-Mail"), ("vermittlernummer", "Vermittlernummer")],
        default="email",
        validators=[DataRequired()],
    )
    identifier = StringField("E-Mail oder Vermittlernummer", validators=[DataRequired()])
    password = PasswordField("Passwort", validators=[DataRequired()])
    submit = SubmitField("Anmelden")


class ForgotPasswordForm(FlaskForm):
    email = StringField(
        "E-Mail",
        validators=[DataRequired(), Email(message="Bitte eine gültige E-Mail-Adresse eingeben."), Length(max=255)],
        filters=[lambda value: value.strip() if value else value],
    )
    submit = SubmitField("Link anfordern")


class TwoFactorForm(FlaskForm):
    code = StringField(
        "Bestätigungscode",
        validators=[DataRequired(message="Bitte den Code eingeben."), Length(max=32)],
    )
    trust_browser = BooleanField("Diesem Browser 30 Tage vertrauen")
    submit = SubmitField("Bestätigen")


class ResetPasswordForm(FlaskForm):
    token = HiddenField()
    code = StringField(
        "Code aus der Authenticator-App (oder Wiederherstellungscode)",
        validators=[DataRequired(message="Bitte den Code eingeben."), Length(max=32)],
    )
    password = PasswordField(
        "Neues Passwort", validators=[DataRequired(), Length(min=8, message="Mindestens 8 Zeichen.")]
    )
    password_confirm = PasswordField(
        "Neues Passwort bestätigen",
        validators=[DataRequired(), EqualTo("password", message="Passwörter stimmen nicht überein.")],
    )
    submit = SubmitField("Passwort speichern")


def _strip(value):
    return value.strip() if isinstance(value, str) else value


class AccessRequestForm(FlaskForm):
    office_name = StringField(
        "Büro / Firma", validators=[DataRequired(message="Bitte den Namen des Büros angeben."), Length(max=200)], filters=[_strip]
    )
    contact_name = StringField(
        "Ihr Name", validators=[DataRequired(message="Bitte Ihren Namen angeben."), Length(max=200)], filters=[_strip]
    )
    email = StringField(
        "E-Mail",
        validators=[DataRequired(), Email(message="Bitte eine gültige E-Mail-Adresse eingeben."), Length(max=255)],
        filters=[_strip],
    )
    phone = StringField("Telefon (optional)", validators=[Optional(), Length(max=50)], filters=[_strip])
    message = TextAreaField("Nachricht (optional)", validators=[Optional(), Length(max=2000)], filters=[_strip])
    # Fuer Menschen unsichtbar; ausgefuellt = automatisierte Absendung (wird still verworfen).
    website = StringField("Website", validators=[Optional(), Length(max=200)])
    consent = BooleanField(
        "Ich bin einverstanden, dass meine Angaben zur Bearbeitung dieser Anfrage gespeichert werden.",
        validators=[DataRequired(message="Bitte bestätigen Sie die Einwilligung.")],
    )
    submit = SubmitField("Anfrage senden")
