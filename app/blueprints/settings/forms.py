from flask_wtf import FlaskForm
from wtforms import PasswordField, SubmitField
from wtforms.validators import DataRequired, EqualTo, Length


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
