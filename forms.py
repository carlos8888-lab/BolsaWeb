from flask_wtf import FlaskForm
from wtforms import StringField, PasswordField, TextAreaField
from wtforms.validators import DataRequired, Length, Email, EqualTo, Optional

class RegisterForm(FlaskForm):
    email = StringField("Email", validators=[DataRequired(), Email(), Length(max=254)])
    username = StringField("Nombre (opcional)", validators=[Optional(), Length(max=80)])
    password = PasswordField("Contraseña", validators=[DataRequired(), Length(min=4, max=128)])
    password2 = PasswordField("Repite contraseña", validators=[DataRequired(), EqualTo("password")])

class LoginForm(FlaskForm):
    email = StringField("Email", validators=[DataRequired(), Email(), Length(max=254)])
    password = PasswordField("Contraseña", validators=[DataRequired()])

class PostForm(FlaskForm):
    title = StringField("Título", validators=[DataRequired(), Length(max=200)])
    body = TextAreaField("Contenido", validators=[DataRequired()])
