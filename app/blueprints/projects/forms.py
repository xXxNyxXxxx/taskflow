from flask_wtf import FlaskForm
from wtforms import DateField, SelectField, StringField, SubmitField, TextAreaField
from wtforms.validators import DataRequired, Length, Optional


class ProjectCreateForm(FlaskForm):
    title = StringField("Project Title", validators=[DataRequired(), Length(min=2, max=160)])
    description = TextAreaField("Scope / Overview", validators=[Optional(), Length(max=2000)])
    est_completion_date = DateField("Target Completion Date", validators=[DataRequired()])
    lead_id = SelectField("Project Lead", coerce=int, validators=[Optional()])
    template_id = SelectField("Start From Template (Optional)", coerce=int, validators=[Optional()])
    submit = SubmitField("Create Project")


class SaveTemplateForm(FlaskForm):
    name = StringField("Template Name", validators=[DataRequired(), Length(min=2, max=140)])
    description = TextAreaField("Template Notes", validators=[Optional(), Length(max=1000)])
    submit = SubmitField("Save as Reusable Template")