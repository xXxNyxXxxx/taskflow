from flask_wtf import FlaskForm
from wtforms import BooleanField, PasswordField, SelectField, StringField, SubmitField
from wtforms.validators import DataRequired, Email, EqualTo, Length, Optional


class LoginForm(FlaskForm):
    email = StringField("Email", validators=[DataRequired(), Email(), Length(max=255)])
    password = PasswordField("Password", validators=[DataRequired(), Length(min=8, max=128)])
    remember_me = BooleanField("Keep me signed in")
    submit = SubmitField("Sign In")


class SignupForm(FlaskForm):
    signup_mode = SelectField(
        "Registration Type",
        choices=[("invite", "Join with Invite Code"), ("new_org", "Create New Workspace")],
        default="invite",
    )
    org_name = StringField("Workspace Name", validators=[Optional(), Length(min=2, max=120)])
    invite_code = StringField("Invite Code", validators=[Optional(), Length(min=4, max=128)])
    full_name = StringField("Full Name", validators=[DataRequired(), Length(min=2, max=120)])
    email = StringField("Work Email", validators=[DataRequired(), Email(), Length(max=255)])
    password = PasswordField(
        "Password",
        validators=[
            DataRequired(),
            Length(min=8, max=128, message="Password must be at least 8 characters."),
        ],
    )
    confirm_password = PasswordField(
        "Confirm Password",
        validators=[DataRequired(), EqualTo("password", message="Passwords must match.")],
    )
    submit = SubmitField("Create Account")