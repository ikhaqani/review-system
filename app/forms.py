from flask_wtf import FlaskForm
from wtforms import StringField, PasswordField, SubmitField, TextAreaField
from wtforms.validators import DataRequired, Email, EqualTo, ValidationError
from app.models import User

class RegistrationForm(FlaskForm):
    email = StringField('E-mailadres', validators=[DataRequired(), Email()])
    submit = SubmitField('Vraag account aan')

    def validate_email(self, email):
        user = User.query.filter_by(email=email.data).first()
        if user is not None:
            raise ValidationError('Dit e-mailadres is al in gebruik.')

class LoginForm(FlaskForm):
    username = StringField('Gebruikersnaam', validators=[DataRequired()])
    password = PasswordField('Wachtwoord', validators=[DataRequired()])
    submit = SubmitField('Inloggen')