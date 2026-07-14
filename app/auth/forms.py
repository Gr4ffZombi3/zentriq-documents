from flask_wtf import FlaskForm
from wtforms import PasswordField, RadioField, StringField, SubmitField
from wtforms.validators import DataRequired, Email, EqualTo, Length


class RegisterForm(FlaskForm):
    company_name = StringField("Firmenname", validators=[DataRequired(), Length(max=255)])
    email = StringField("E-Mail", validators=[DataRequired(), Email()])
    vermittlernummer = StringField("Vermittlernummer", validators=[DataRequired(), Length(max=50)])
    password = PasswordField("Passwort", validators=[DataRequired(), Length(min=8, message="Mindestens 8 Zeichen.")])
    password_confirm = PasswordField(
        "Passwort bestätigen",
        validators=[DataRequired(), EqualTo("password", message="Passwörter stimmen nicht überein.")],
    )
    submit = SubmitField("Registrieren")


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
