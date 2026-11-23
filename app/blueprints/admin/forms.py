from flask_wtf import FlaskForm
from flask_wtf.file import FileAllowed, FileField
from wtforms import BooleanField, IntegerField, SelectField, StringField, SubmitField
from wtforms.validators import DataRequired, Length, NumberRange, Optional


class WorkspaceSettingsForm(FlaskForm):
    # White-Label Branding
    brand_name = StringField("Custom Workspace Brand Name", validators=[Optional(), Length(min=2, max=80)])
    logo_file = FileField(
        "Workspace Logo (PNG, JPG, WEBP — max 1 MB)",
        validators=[Optional(), FileAllowed(["png", "jpg", "jpeg", "webp"], "Raster images only.")],
    )
    remove_logo = BooleanField("Remove current logo and use monogram")
    theme_preset = SelectField(
        "Low-Arousal Palette Preset",
        choices=[
            ("calm_sage", "Calm Sage & Slate (Default)"),
            ("soft_terracotta", "Soft Terracotta & Sand"),
            ("dusty_lavender", "Dusty Lavender & Stone"),
            ("forest_moss", "Forest Moss & Linen"),
        ],
    )
    custom_accent_hex = StringField(
        "Custom Muted Accent Hex (Optional)",
        validators=[Optional(), Length(min=7, max=7)],
    )

    # Workflow & Governance
    acceptance_mode = SelectField(
        "Task Assignment Mode",
        choices=[
            ("strict", "Strict Handshake (Requires Confirm / Decline)"),
            ("direct", "Direct Assignment (Auto-Confirmed)"),
        ],
    )
    pending_sla_hours = IntegerField(
        "Pending Acceptance SLA (Hours)",
        validators=[DataRequired(), NumberRange(min=1, max=720)],
    )
    require_review_gate = BooleanField("Require Project Lead QA Review before Done")
    allow_client_share_links = BooleanField("Allow Read-Only Client Share Links")
    invite_expiry_hours = IntegerField(
        "Default Invite Code Expiry (Hours)",
        validators=[DataRequired(), NumberRange(min=1, max=720)],
    )
    daily_digest_mode = SelectField(
        "Daily SLA & Deadline Digest",
        choices=[
            ("banner", "In-App Morning Banner Only"),
            ("banner_and_email", "In-App Banner + Email Digest"),
            ("off", "Disabled"),
        ],
    )

    # Custom Kanban Column Labels
    col_todo = StringField("To Do Column Label", validators=[DataRequired(), Length(min=2, max=30)])
    col_in_progress = StringField("In Progress Column Label", validators=[DataRequired(), Length(min=2, max=30)])
    col_review = StringField("In Review Column Label", validators=[DataRequired(), Length(min=2, max=30)])
    col_done = StringField("Done Column Label", validators=[DataRequired(), Length(min=2, max=30)])

    # Recognition & Performance Tiers
    recognition_enabled = BooleanField("Enable Recognition Points & Performance Tiers")
    on_time_bonus_pct = IntegerField(
        "On-Time Completion Bonus (%)",
        validators=[DataRequired(), NumberRange(min=0, max=100)],
    )
    internal_ext_penalty_pct = IntegerField(
        "Internal Extension Bonus Reduction (%)",
        validators=[DataRequired(), NumberRange(min=0, max=100)],
    )
    tier_reset_cadence = SelectField(
        "Performance Tier Cadence",
        choices=[
            ("lifetime", "Lifetime Cumulative"),
            ("monthly", "Monthly Reset (Archives Past Totals)"),
            ("quarterly", "Quarterly Reset (Archives Past Totals)"),
        ],
    )

    weight_low = IntegerField("Low Priority Points", validators=[DataRequired(), NumberRange(min=0, max=1000)])
    weight_medium = IntegerField("Medium Priority Points", validators=[DataRequired(), NumberRange(min=0, max=1000)])
    weight_high = IntegerField("High Priority Points", validators=[DataRequired(), NumberRange(min=0, max=1000)])
    weight_critical = IntegerField("Critical Priority Points", validators=[DataRequired(), NumberRange(min=0, max=1000)])

    tier_emerald = IntegerField("Emerald Tier Threshold", validators=[DataRequired(), NumberRange(min=1, max=1000000)])
    tier_indigo = IntegerField("Indigo Tier Threshold", validators=[DataRequired(), NumberRange(min=2, max=1000000)])
    tier_gold = IntegerField("Gold Tier Threshold", validators=[DataRequired(), NumberRange(min=3, max=1000000)])

    submit = SubmitField("Save Workspace Settings")


class GenerateInviteForm(FlaskForm):
    assigned_role = SelectField(
        "Assigned Role",
        choices=[("member", "Team Member"), ("project_lead", "Project Lead")],
        default="member",
    )
    max_uses = IntegerField("Max Uses", default=1, validators=[DataRequired(), NumberRange(min=1, max=100)])
    submit_invite = SubmitField("Generate Invite Code")