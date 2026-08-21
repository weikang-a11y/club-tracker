from flask import Flask, render_template, request, redirect, url_for, flash
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
from werkzeug.security import generate_password_hash, check_password_hash
from flask_wtf import FlaskForm
from wtforms import StringField, PasswordField, DateField, SelectField, IntegerField, SubmitField
from wtforms.validators import DataRequired, Length, NumberRange, ValidationError
from datetime import datetime, timedelta
from sqlalchemy.orm import joinedload
from sqlalchemy import text as sql_text
from apscheduler.schedulers.background import BackgroundScheduler
from sendgrid import SendGridAPIClient
from sendgrid.helpers.mail import Mail
from dotenv import load_dotenv
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from sqlalchemy.exc import IntegrityError
import os

load_dotenv()

app = Flask(__name__)
app.config['SECRET_KEY'] = 'super-secret-key-change-me-98765'

# Database config with Railway/Postgres support + local SQLite fallback
DATABASE_URL = os.getenv('DATABASE_URL')
if DATABASE_URL:
    if DATABASE_URL.startswith('postgres://'):
        DATABASE_URL = DATABASE_URL.replace('postgres://', 'postgresql+psycopg://', 1)
    elif DATABASE_URL.startswith('postgresql://'):
        DATABASE_URL = DATABASE_URL.replace('postgresql://', 'postgresql+psycopg://', 1)
    print("[DB] Using external PostgreSQL database.")
    app.config['SQLALCHEMY_DATABASE_URI'] = DATABASE_URL
    app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {
        'pool_pre_ping': True,
        'pool_recycle': 300,
    }
else:
    local_db = os.path.join(os.path.dirname(__file__), 'club.db')
    print('[DB] No DATABASE_URL found, using local SQLite database.')
    app.config['SQLALCHEMY_DATABASE_URI'] = f'sqlite:///{local_db}'

app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

FROM_EMAIL = os.getenv("FROM_EMAIL")
SENDGRID_API_KEY = os.getenv("SENDGRID_API_KEY")

db = SQLAlchemy(app)
login_manager = LoginManager(app)
login_manager.login_view = 'login'

TIME_SLOTS = [
    ("15:00", "3:00 - 3:20 pm"),
    ("15:20", "3:20 - 3:40 pm"),
    ("15:40", "3:40 - 4:00 pm"),
]
ACTIVITY_TYPES = [
    'In-Person Roleplay',
    'ICPrep Roleplay',
    'Written Presentation',
    'Paper Exam',
    'ICPrep Exam',
]
# Map practice types to commitment buckets
ROLEPLAY_TYPES = {'In-Person Roleplay', 'ICPrep Roleplay'}
EXAM_TYPES     = {'Paper Exam', 'ICPrep Exam'}
ICPREP_TYPES   = {'ICPrep Roleplay', 'ICPrep Exam'}
WRITTEN_TYPES  = {'Written Presentation'}

# Annual ICPrep minimums per level
ICPREP_TARGETS = {
    'N': {'roleplay': 2, 'exam': 2},
    'E': {'roleplay': 1, 'exam': 1},
}

# Per-conference requirements split by experience level (non-cumulative)
# Novice:     VCMC(RP:2,W:1,E:2), SVCDC(RP:1,W:1,E:2), SCDC(RP:2,W:1,E:2)
# Experienced:VCMC(RP:0,W:1,E:0), SVCDC(RP:2,W:1,E:2), SCDC(RP:1,W:1,E:1)
# RP/Exam counts include both in-person and ICPrep (members choose which to use for ICPrep quota)
EVENT_REQUIREMENTS = {
    "N": {  # Novice
        "VCMC":  {"roleplay": 2, "written": 1, "exam": 2, "deadline": "2026-11-15"},
        "SVCDC": {"roleplay": 1, "written": 1, "exam": 2, "deadline": "2027-01-08"},
        "SCDC":  {"roleplay": 2, "written": 1, "exam": 2, "deadline": "2027-02-23"},
    },
    "E": {  # Experienced
        "VCMC":  {"roleplay": 0, "written": 1, "exam": 0, "deadline": "2026-11-15"},
        "SVCDC": {"roleplay": 2, "written": 1, "exam": 2, "deadline": "2027-01-08"},
        "SCDC":  {"roleplay": 1, "written": 1, "exam": 1, "deadline": "2027-02-23"},
    },
}

CONFERENCE_ORDER = ["VCMC", "SVCDC", "SCDC"]
CONFERENCE_DEADLINES = {
    "VCMC":  "2026-11-15",
    "SVCDC": "2027-01-08",
    "SCDC":  "2027-02-23",
}

# Email that receives practice log completion notifications
PRACTICE_LOG_EMAIL = os.getenv("PRACTICE_LOG_EMAIL", "mentorship@vchsdeca.org")

# Attendance thresholds by experience level
AH_THRESHOLD = 0.80   # 80% for all members
WS_THRESHOLD = {
    'N': 0.75,  # Novice: 75% workshop attendance
    'E': 0.25,  # Experienced: 25% workshop attendance
}

LOCAL_TZ = ZoneInfo("America/Los_Angeles")

class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(50), unique=True, nullable=False)
    password = db.Column(db.String(200), nullable=False)
    role = db.Column(db.String(20), nullable=False)
    email = db.Column(db.String(120))
    phone = db.Column(db.String(20))
    notify_enabled = db.Column(db.Boolean, default=False)
    remind_minutes_before = db.Column(db.Integer, default=60)
    must_change_password = db.Column(db.Boolean, default=False)
    is_admin = db.Column(db.Boolean, default=False)

class Commitment(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    member_name = db.Column(db.String(100))
    event = db.Column(db.String(20))
    required_roleplay = db.Column(db.Integer, default=0)
    required_written = db.Column(db.Integer, default=0)
    required_exam = db.Column(db.Integer, default=0)
    remaining_roleplay = db.Column(db.Integer, default=0)
    remaining_written = db.Column(db.Integer, default=0)
    remaining_exam = db.Column(db.Integer, default=0)
    deadline = db.Column(db.Date)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'))
    user = db.relationship('User', backref='added_commitments')

class Workshop(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    time = db.Column(db.DateTime, nullable=False)
    officer_id = db.Column(db.Integer, db.ForeignKey('user.id'))
    activity_type = db.Column(db.String(50), nullable=False)
    creator_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True)

    __table_args__ = (
        db.UniqueConstraint('time', 'officer_id', name='unique_officer_timeslot'),
    )

    officer = db.relationship('User', foreign_keys=[officer_id], backref='hosted_workshops')
    creator = db.relationship('User', foreign_keys=[creator_id], backref='created_workshops')
    signups = db.relationship('User', secondary='workshop_signups', backref=db.backref('workshops', lazy='dynamic'))

workshop_signups = db.Table(
    'workshop_signups',
    db.Column('workshop_id', db.Integer, db.ForeignKey('workshop.id'), primary_key=True),
    db.Column('user_id', db.Integer, db.ForeignKey('user.id'), primary_key=True),
    db.Column('attended', db.Boolean, default=False, nullable=False)
)

class GeneralAttendance(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    officer_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    member_name = db.Column(db.String(100), nullable=False)
    manual_count = db.Column(db.Integer, default=0)
    officer = db.relationship('User', backref='general_attendances')

class AttendanceSubmission(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    workshop_id = db.Column(db.Integer, db.ForeignKey('workshop.id'), unique=True, nullable=False)
    officer_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    submitted_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

class ReminderLog(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    workshop_id = db.Column(db.Integer, db.ForeignKey('workshop.id'), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    sent_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    __table_args__ = (
        db.UniqueConstraint('workshop_id', 'user_id', name='unique_workshop_reminder'),
    )

class AHAttendance(db.Model):
    """All-Hands attendance — one record per member per Wednesday lunch session."""
    __tablename__ = 'ah_attendance'
    __table_args__ = (
        db.UniqueConstraint('user_id', 'session_date', name='uq_ah_user_date'),
    )
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    session_date = db.Column(db.Date, nullable=False)
    value = db.Column(db.Float, nullable=False)  # 1.0=present, 0.5=excused, 0.0=absent
    user = db.relationship('User', backref='ah_records')

class WSAttendance(db.Model):
    """Workshop attendance — one record per member per Wednesday after-school session."""
    __tablename__ = 'ws_attendance'
    __table_args__ = (
        db.UniqueConstraint('user_id', 'session_date', name='uq_ws_user_date'),
    )
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    session_date = db.Column(db.Date, nullable=False)
    value = db.Column(db.Float, nullable=False)  # 1.0=present, 0.5=excused, 0.0=absent
    user = db.relationship('User', backref='ws_records')

class MentorPod(db.Model):
    """Links each member to their mentor pod and officer."""
    __tablename__ = 'mentor_pod'
    id = db.Column(db.Integer, primary_key=True)
    pod_number = db.Column(db.Integer, nullable=False)
    mentor_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    member_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    experience_level = db.Column(db.String(1))   # 'N' = Novice, 'E' = Experienced
    year_in_deca = db.Column(db.String(20))
    mentor = db.relationship('User', foreign_keys=[mentor_id], backref='pod_members')
    member = db.relationship('User', foreign_keys=[member_id], backref='pod')


class PracticeSession(db.Model):
    """A practice slot posted by an officer for their pod members to sign up for."""
    __tablename__ = 'practice_session'
    id = db.Column(db.Integer, primary_key=True)
    officer_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    session_date = db.Column(db.Date, nullable=False)
    session_time = db.Column(db.String(5), nullable=False)
    practice_type = db.Column(db.String(30), nullable=False)
    conference = db.Column(db.String(10), nullable=False)
    member_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True)
    notes = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    log_submitted = db.Column(db.Boolean, default=False)  # True after officer logs completion
    officer = db.relationship('User', foreign_keys=[officer_id], backref='posted_sessions')
    member = db.relationship('User', foreign_keys=[member_id], backref='signed_up_sessions')


class AnnualICPrepTracker(db.Model):
    """Tracks annual ICPrep completion totals per member (across all conferences)."""
    __tablename__ = 'annual_icprep_tracker'
    id = db.Column(db.Integer, primary_key=True)
    member_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False, unique=True)
    icprep_rp_completed = db.Column(db.Integer, default=0, nullable=False)
    icprep_exam_completed = db.Column(db.Integer, default=0, nullable=False)
    member = db.relationship('User', backref=db.backref('icprep_tracker', uselist=False))


class ExamUpload(db.Model):
    """Paper exam upload submitted by a member for officer review."""
    __tablename__ = 'exam_upload'
    id = db.Column(db.Integer, primary_key=True)
    member_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    conference = db.Column(db.String(10), nullable=False)
    cloudinary_url = db.Column(db.String(500), nullable=False)
    cloudinary_public_id = db.Column(db.String(200), nullable=False)
    notes = db.Column(db.Text, nullable=True)       # member notes (e.g. which exam)
    uploaded_at = db.Column(db.DateTime, default=datetime.utcnow)
    reviewed = db.Column(db.Boolean, default=False)
    reviewed_at = db.Column(db.DateTime, nullable=True)
    reviewer_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True)
    commitment_id = db.Column(db.Integer, db.ForeignKey('commitment.id'), nullable=True)
    credited = db.Column(db.Boolean, default=False)  # officer marks credit after review
    member = db.relationship('User', foreign_keys=[member_id], backref='exam_uploads')
    reviewer = db.relationship('User', foreign_keys=[reviewer_id])


class ICPrepWebhookLog(db.Model):
    """Raw log of every inbound ICPrep webhook event."""
    __tablename__ = 'icprep_webhook_log'
    id = db.Column(db.Integer, primary_key=True)
    received_at = db.Column(db.DateTime, default=datetime.utcnow)
    payload = db.Column(db.Text)          # raw JSON string
    member_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True)
    activity_type = db.Column(db.String(30), nullable=True)  # 'ICPrep Roleplay' or 'ICPrep Exam'
    processed = db.Column(db.Boolean, default=False)
    error = db.Column(db.Text, nullable=True)
    member = db.relationship('User', backref='icprep_webhook_logs')


class PracticeLog(db.Model):
    """Completion record submitted by officer after a practice session."""
    __tablename__ = 'practice_log'
    id = db.Column(db.Integer, primary_key=True)
    practice_session_id = db.Column(db.Integer, db.ForeignKey('practice_session.id'), nullable=True)
    officer_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    member_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    commitment_id = db.Column(db.Integer, db.ForeignKey('commitment.id'), nullable=True)
    practice_type = db.Column(db.String(30), nullable=False)
    conference = db.Column(db.String(10), nullable=False)
    session_date = db.Column(db.Date, nullable=False)
    score = db.Column(db.Float, nullable=True)
    officer_notes = db.Column(db.Text, nullable=True)
    feedback = db.Column(db.Text, nullable=True)
    submitted_at = db.Column(db.DateTime, default=datetime.utcnow)
    officer = db.relationship('User', foreign_keys=[officer_id], backref='submitted_logs')
    member = db.relationship('User', foreign_keys=[member_id], backref='practice_logs')
    session = db.relationship('PracticeSession', backref='log')


@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, int(user_id))

# ── Commitment helpers ───────────────────────────────────────────────────────

def get_active_conference():
    """Return the current active conference based on today's date."""
    today = datetime.now(LOCAL_TZ).date()
    for conf in CONFERENCE_ORDER:
        deadline = datetime.strptime(CONFERENCE_DEADLINES[conf], "%Y-%m-%d").date()
        if today <= deadline:
            return conf
    return "SCDC"  # after all deadlines, default to last


def ensure_commitments(member):
    """Create or repair commitment rows only when a database change is needed."""
    pod = MentorPod.query.filter_by(member_id=member.id).first()
    level = pod.experience_level if pod else 'N'
    reqs = EVENT_REQUIREMENTS.get(level, EVENT_REQUIREMENTS['N'])
    changed = False

    for conf, rule in reqs.items():
        existing = Commitment.query.filter_by(
            member_name=member.username,
            event=conf,
        ).first()

        if not existing:
            db.session.add(Commitment(
                member_name=member.username,
                event=conf,
                required_roleplay=rule["roleplay"],
                required_written=rule["written"],
                required_exam=rule["exam"],
                remaining_roleplay=rule["roleplay"],
                remaining_written=rule["written"],
                remaining_exam=rule["exam"],
                deadline=datetime.strptime(rule["deadline"], "%Y-%m-%d").date(),
                user_id=member.id,
            ))
            changed = True
            continue

        if existing.user_id != member.id:
            existing.user_id = member.id
            changed = True
        if existing.member_name != member.username:
            existing.member_name = member.username
            changed = True
        if existing.deadline is None:
            existing.deadline = datetime.strptime(
                rule["deadline"], "%Y-%m-%d"
            ).date()
            changed = True

        # Repair only legacy rows with no requirement data so imported progress
        # is not overwritten during ordinary page loads.
        if (
            existing.required_roleplay == 0
            and existing.required_written == 0
            and existing.required_exam == 0
        ):
            existing.required_roleplay = rule["roleplay"]
            existing.required_written = rule["written"]
            existing.required_exam = rule["exam"]
            existing.remaining_roleplay = rule["roleplay"]
            existing.remaining_written = rule["written"]
            existing.remaining_exam = rule["exam"]
            changed = True

    if not AnnualICPrepTracker.query.filter_by(member_id=member.id).first():
        db.session.add(AnnualICPrepTracker(member_id=member.id))
        changed = True

    if changed:
        try:
            db.session.commit()
        except Exception:
            db.session.rollback()


def get_icprep_status(member):
    """Return ICPrep completion counts and targets for a member."""
    pod = MentorPod.query.filter_by(member_id=member.id).first()
    level = pod.experience_level if pod else 'N'
    targets = ICPREP_TARGETS.get(level, ICPREP_TARGETS['N'])
    tracker = AnnualICPrepTracker.query.filter_by(member_id=member.id).first()
    rp_done = tracker.icprep_rp_completed if tracker else 0
    ex_done = tracker.icprep_exam_completed if tracker else 0
    return {
        'rp_done': rp_done,
        'rp_target': targets['roleplay'],
        'rp_met': rp_done >= targets['roleplay'],
        'exam_done': ex_done,
        'exam_target': targets['exam'],
        'exam_met': ex_done >= targets['exam'],
        'all_met': rp_done >= targets['roleplay'] and ex_done >= targets['exam'],
    }


# ── Attendance helper ─────────────────────────────────────────────────────────

def get_attendance_stats(
    user,
    ah_records=None,
    ws_records=None,
    pod=None,
    pod_loaded=False,
):
    """Return detailed AH/WS attendance counts, rates, and risk status."""
    if ah_records is None:
        ah_records = AHAttendance.query.filter_by(user_id=user.id).all()
    if ws_records is None:
        ws_records = WSAttendance.query.filter_by(user_id=user.id).all()

    total_ah = len(ah_records)
    total_ws = len(ws_records)

    ah_sum = sum(r.value for r in ah_records)
    ws_sum = sum(r.value for r in ws_records)

    if not pod_loaded:
        pod = MentorPod.query.filter_by(member_id=user.id).first()
    level = pod.experience_level if pod and pod.experience_level else 'N'
    ws_threshold_pct = WS_THRESHOLD.get(level, WS_THRESHOLD['N']) * 100

    ah_ok = ah_rate >= (AH_THRESHOLD * 100)
    ws_ok = ws_rate >= ws_threshold_pct

    at_risk = not ah_ok or not ws_ok
    risk_reasons = []
    if not ah_ok:
        risk_reasons.append(f"AH attendance {ah_rate}% < {AH_THRESHOLD*100:.0f}% required")
    if not ws_ok:
        risk_reasons.append(f"WS attendance {ws_rate}% < {ws_threshold_pct:.0f}% required")

    return {
        'ah_total': total_ah,
        'ah_sum': ah_sum,
        'ah_rate': ah_rate,
        'ws_total': total_ws,
        'ws_sum': ws_sum,
        'ws_rate': ws_rate,
        'level': level,
        'ws_threshold_pct': ws_threshold_pct,
        'at_risk': at_risk,
        'risk_reasons': risk_reasons,
    }

# ── Forms ─────────────────────────────────────────────────────────────────────

class RegisterForm(FlaskForm):
    username = StringField('Username', [DataRequired(), Length(min=3)])
    password = PasswordField('Password', [DataRequired(), Length(min=6)])
    role = SelectField('Role', choices=[('', 'Select your role'), ('officer', 'Officer'), ('member', 'Member')], default='')
    submit = SubmitField('Register')

    def validate_role(self, field):
        if not field.data:
            raise ValidationError('Please select a role.')

class LoginForm(FlaskForm):
    username = StringField('Username', [DataRequired()])
    password = PasswordField('Password', [DataRequired()])
    submit = SubmitField('Login')

class ChangePasswordForm(FlaskForm):
    new_password = PasswordField('New Password', [DataRequired(), Length(min=6)])
    confirm_password = PasswordField('Confirm Password', [DataRequired()])
    submit = SubmitField('Set Password')

    def validate_confirm_password(self, field):
        if field.data != self.new_password.data:
            raise ValidationError('Passwords do not match.')

class CommitmentForm(FlaskForm):
    member_name = StringField('Member Name', [DataRequired()])
    event = SelectField(
        "Event",
        choices=[
            ("VCMC","VCMC"),
            ("SVCDC","SVCDC"),
            ("SCDC","SCDC")
        ],
        validators=[DataRequired()]
    )
    submit = SubmitField('Save Commitment')

class WorkshopForm(FlaskForm):
    workshop_date = DateField('Date', [DataRequired()])
    slot = SelectField('Time Slot (20 min)', [DataRequired()], choices=[('', 'Select a time slot')] + TIME_SLOTS, default='')
    activity_type = SelectField('Activity Type', [DataRequired()], choices=[('', 'Select an activity type')] + [(t, t) for t in ACTIVITY_TYPES], default='')
    officer_id = SelectField('Officer', [DataRequired()], coerce=int, choices=[(0, 'Select an officer')], default=0)
    submit = SubmitField('Create Workshop')

    def validate_slot(self, field):
        if not field.data:
            raise ValidationError('Please select a time slot.')

    def validate_activity_type(self, field):
        if not field.data:
            raise ValidationError('Please select an activity type.')

    def validate_officer_id(self, field):
        if not field.data:
            raise ValidationError('Please select an officer.')

# ── Schema migration ──────────────────────────────────────────────────────────

with app.app_context():
    db.create_all()

    engine_name = db.engine.dialect.name
    cols = {c['name'] for c in db.inspect(db.engine).get_columns('workshop')}
    if 'creator_id' not in cols:
        try:
            db.session.execute(sql_text('ALTER TABLE workshop ADD COLUMN creator_id INTEGER'))
            db.session.commit()
        except Exception:
            db.session.rollback()

    user_cols = {c['name'] for c in db.inspect(db.engine).get_columns('user')}

    if 'email' not in user_cols:
        try:
            db.session.execute(sql_text('ALTER TABLE "user" ADD COLUMN email VARCHAR(120)'))
            db.session.commit()
        except Exception:
            db.session.rollback()

    if 'phone' not in user_cols:
        try:
            db.session.execute(sql_text('ALTER TABLE "user" ADD COLUMN phone VARCHAR(20)'))
            db.session.commit()
        except Exception:
            db.session.rollback()

    if 'notify_enabled' not in user_cols:
        try:
            db.session.execute(sql_text('ALTER TABLE "user" ADD COLUMN notify_enabled BOOLEAN DEFAULT FALSE'))
            db.session.commit()
        except Exception:
            db.session.rollback()

    if 'remind_minutes_before' not in user_cols:
        try:
            db.session.execute(sql_text('ALTER TABLE "user" ADD COLUMN remind_minutes_before INTEGER DEFAULT 60'))
            db.session.commit()
        except Exception:
            db.session.rollback()

    if 'must_change_password' not in user_cols:
        try:
            db.session.execute(sql_text('ALTER TABLE "user" ADD COLUMN must_change_password BOOLEAN DEFAULT FALSE'))
            db.session.commit()
        except Exception:
            db.session.rollback()

    if 'is_admin' not in user_cols:
        try:
            db.session.execute(sql_text('ALTER TABLE "user" ADD COLUMN is_admin BOOLEAN DEFAULT FALSE'))
            db.session.commit()
        except Exception:
            db.session.rollback()

    commitment_cols = {c['name'] for c in db.inspect(db.engine).get_columns('commitment')}

    if 'event' not in commitment_cols:
        try:
            db.session.execute(sql_text('ALTER TABLE commitment ADD COLUMN event VARCHAR(20)'))
            db.session.commit()
        except Exception:
            db.session.rollback()

    # Backfill missing creator signups for existing workshops
    for ws in Workshop.query.all():
        if ws.creator_id:
            creator = db.session.get(User, ws.creator_id)
            if creator and creator not in ws.signups:
                ws.signups.append(creator)
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()

    # Migrate: ensure commitment rows exist for all members
    # (safe to run multiple times — ensure_commitments is idempotent)
    for member in User.query.filter_by(role='member').all():
        ensure_commitments(member)

    # Migrate: add log_submitted column to practice_session if missing
    ps_cols = [col[1] for col in db.session.execute(sql_text("PRAGMA table_info(practice_session)")).fetchall()] if db.engine.dialect.name == 'sqlite' else []
    if db.engine.dialect.name == 'sqlite' and 'log_submitted' not in ps_cols:
        try:
            db.session.execute(sql_text('ALTER TABLE practice_session ADD COLUMN log_submitted BOOLEAN DEFAULT FALSE'))
            db.session.commit()
        except Exception:
            db.session.rollback()
    elif db.engine.dialect.name != 'sqlite':
        try:
            db.session.execute(sql_text('ALTER TABLE practice_session ADD COLUMN IF NOT EXISTS log_submitted BOOLEAN DEFAULT FALSE'))
            db.session.commit()
        except Exception:
            db.session.rollback()

    # Migrate: fix existing commitment rows whose required counts don't match current matrix
    for member in User.query.filter_by(role='member').all():
        pod = MentorPod.query.filter_by(member_id=member.id).first()
        level = pod.experience_level if pod else 'N'
        reqs = EVENT_REQUIREMENTS.get(level, EVENT_REQUIREMENTS['N'])
        for conf, rule in reqs.items():
            com = Commitment.query.filter_by(member_name=member.username, event=conf).first()
            if com:
                # Only update required counts; adjust remaining proportionally
                old_req_rp = com.required_roleplay
                old_req_ex = com.required_exam
                old_req_wr = com.required_written
                completed_rp = old_req_rp - com.remaining_roleplay
                completed_ex = old_req_ex - com.remaining_exam
                completed_wr = old_req_wr - com.remaining_written
                com.required_roleplay = rule["roleplay"]
                com.required_written  = rule["written"]
                com.required_exam     = rule["exam"]
                com.remaining_roleplay = max(0, rule["roleplay"] - completed_rp)
                com.remaining_written  = max(0, rule["written"]  - completed_wr)
                com.remaining_exam     = max(0, rule["exam"]     - completed_ex)
                db.session.add(com)
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()

# ── Helpers ───────────────────────────────────────────────────────────────────

def friendly_slot(dt):
    if not dt:
        return 'N/A'
    local_dt = utc_to_local(dt)
    date_part = local_dt.strftime('%Y-%m-%d')
    time_str = local_dt.strftime('%H:%M')
    for value, label in TIME_SLOTS:
        if value == time_str:
            return f"{date_part} {label}"
    return local_dt.strftime('%Y-%m-%d %I:%M %p')

def utc_to_local(dt):
    if not dt:
        return dt
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(LOCAL_TZ)

def local_time(dt, fmt="%Y-%m-%d %I:%M %p"):
    local_dt = utc_to_local(dt)
    return local_dt.strftime(fmt) if local_dt else "N/A"

def local_to_utc(dt):
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=LOCAL_TZ)
    return dt.astimezone(timezone.utc)

app.jinja_env.filters['friendly_slot'] = friendly_slot
app.jinja_env.filters['local_time'] = local_time

def validate_workshop_slot(workshop_time, officer_id, exclude_workshop_id=None):
    query = Workshop.query.filter_by(time=workshop_time, officer_id=officer_id)
    if exclude_workshop_id is not None:
        query = query.filter(Workshop.id != exclude_workshop_id)
    existing = query.first()
    if existing:
        return "This officer already has a workshop booked for that date and time slot. Please choose a different time or officer."
    return None

def send_email(to_email, subject, html_content):
    if not SENDGRID_API_KEY or not FROM_EMAIL or not to_email:
        return False
    try:
        message = Mail(from_email=FROM_EMAIL, to_emails=to_email, subject=subject, html_content=html_content)
        sg = SendGridAPIClient(SENDGRID_API_KEY)
        response = sg.send(message)
        return True
    except Exception as e:
        print("SEND_EMAIL error:", e)
        return False

def send_email_reminder(user, workshop):
    local_time_str = utc_to_local(workshop.time).strftime('%Y-%m-%d %I:%M %p')
    return send_email(
        user.email,
        "Workshop Reminder",
        f"Reminder: Your <b>{workshop.activity_type}</b> workshop is scheduled at {local_time_str}."
    )

def process_workshop_reminders():
    with app.app_context():
        now = datetime.now(timezone.utc)
        soonest_cutoff = now + timedelta(days=1)
        if not Workshop.query.filter(Workshop.time.between(now, soonest_cutoff)).first():
            return
        if not User.query.filter(User.notify_enabled == True).first():
            return
        active_users_exist = User.query.filter(
            User.notify_enabled == True,
            User.email.isnot(None),
            User.email != ''
        ).first()
        if not active_users_exist:
            return
        reminder_window = now + timedelta(hours=24)
        upcoming = Workshop.query.filter(
            Workshop.time.between(now, reminder_window)
        ).options(joinedload(Workshop.signups)).all()
        for ws in upcoming:
            for user in ws.signups:
                if not user.notify_enabled or not user.email:
                    continue
                remind_at = utc_to_local(ws.time) - timedelta(minutes=user.remind_minutes_before)
                already_sent = ReminderLog.query.filter_by(workshop_id=ws.id, user_id=user.id).first()
                if remind_at <= now and not already_sent:
                    db.session.add(ReminderLog(workshop_id=ws.id, user_id=user.id))
                    db.session.commit()
                    send_email_reminder(user, ws)
        try:
            db.session.commit()
        except Exception:
            db.session.rollback()
            raise



def get_mentor_name(user):
    pod = MentorPod.query.filter_by(member_id=user.id).first()
    return pod.mentor.username if pod and pod.mentor else 'Unassigned'


def log_pod_edit(actor_id, member_id, action, details=""):
    return MentorPodEditLog(
        actor_id=actor_id,
        member_id=member_id,
        action=action,
        details=details,
    )


def log_mdp_action(actor_id, action, category, target_user_id=None, details=""):
    db.session.add(MDPAuditLog(
        actor_id=actor_id,
        target_user_id=target_user_id,
        action=action,
        category=category,
        details=details,
    ))


def get_commitment_status(user):
    commitments = Commitment.query.filter_by(user_id=user.id).all()
    if not commitments:
        return 'on_track'
    today = datetime.now(LOCAL_TZ).date()
    for commitment in commitments:
        remaining = (
            commitment.remaining_roleplay
            + commitment.remaining_written
            + commitment.remaining_exam
        )
        if remaining > 0 and commitment.deadline and commitment.deadline < today:
            return 'at_risk'
    return 'on_track'


def get_commitments_incomplete(user, commitments=None):
    if commitments is None:
        commitments = Commitment.query.filter_by(user_id=user.id).all()
    return any(
        (row.remaining_roleplay + row.remaining_written + row.remaining_exam) > 0
        for row in commitments
    ) if commitments else False


def _members_visible_to_current_user():
    if current_user.is_admin:
        return User.query.filter_by(role='member').order_by(User.username).all()
    pod_member_ids = [
        row.member_id
        for row in MentorPod.query.filter_by(mentor_id=current_user.id).all()
    ]
    if not pod_member_ids:
        return []
    return User.query.filter(User.id.in_(pod_member_ids)).order_by(User.username).all()


def _numeric_grade_below_100(value):
    if value is None:
        return False
    normalized = str(value).replace('%', '').strip()
    if not normalized:
        return False
    try:
        return float(normalized) < 100
    except ValueError:
        return False


def _written_academic_start_year(today=None):
    today = today or datetime.now(LOCAL_TZ).date()
    configured = os.getenv('WRITTEN_ACADEMIC_START_YEAR', '').strip()
    if configured:
        try:
            year = int(configured)
        except ValueError as exc:
            raise RuntimeError(
                'WRITTEN_ACADEMIC_START_YEAR must be a four-digit year, for example 2025.'
            ) from exc
        if year < 2000 or year > 2100:
            raise RuntimeError('WRITTEN_ACADEMIC_START_YEAR must be between 2000 and 2100.')
        return year
    return today.year if today.month >= 7 else today.year - 1


def _parse_written_deadline(value, today=None):
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if hasattr(value, 'year') and hasattr(value, 'month') and hasattr(value, 'day'):
        return value
    raw = str(value).strip()
    if not raw:
        return None
    for fmt in ('%Y-%m-%d', '%m/%d/%Y', '%m/%d/%y', '%b %d, %Y', '%B %d, %Y'):
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    match = re.search(r'(?<!\d)(\d{1,2})/(\d{1,2})(?!/\d)', raw)
    if not match:
        return None
    month, day = int(match.group(1)), int(match.group(2))
    today = today or datetime.now(LOCAL_TZ).date()
    start_year = _written_academic_start_year(today=today)
    year = start_year if month >= 7 else start_year + 1
    try:
        return datetime(year, month, day).date()
    except ValueError:
        return None


def _written_status(item_names, completed_by_item, deadlines_by_item, today=None):
    today = today or datetime.now(LOCAL_TZ).date()
    missing_items = []
    overdue_items = []
    for item_name in item_names:
        if completed_by_item.get(item_name, False):
            continue
        deadline_text = deadlines_by_item.get(item_name)
        deadline_date = _parse_written_deadline(deadline_text, today=today)
        item = {
            'name': item_name,
            'deadline_text': deadline_text,
            'deadline_date': deadline_date,
        }
        missing_items.append(item)
        if deadline_date and deadline_date < today:
            overdue_items.append(item)
    if overdue_items:
        status, label = 'overdue', 'Overdue'
    elif missing_items:
        status, label = 'needs_attention', 'Needs Attention'
    else:
        status, label = 'complete', 'Complete'
    return {
        'status': status,
        'status_label': label,
        'missing_items': missing_items,
        'overdue_items': overdue_items,
        'complete': not missing_items,
        'deadline_safe': not overdue_items,
    }


def get_written_checklist_catalog():
    requirements = ChecklistRequirement.query.order_by(ChecklistRequirement.id).all()
    event_items = defaultdict(list)
    event_deadlines = defaultdict(dict)
    for requirement in requirements:
        event = (requirement.event or '').strip()
        item_name = (requirement.item_name or '').strip()
        deadline = (requirement.deadline or '').strip() if requirement.deadline else None
        if not event or not item_name:
            continue
        if item_name not in event_items[event]:
            event_items[event].append(item_name)
        if deadline:
            event_deadlines[event][item_name] = deadline
    return dict(event_items), dict(event_deadlines)


def _conference_summary_for_user(user, today=None, commitments=None):
    today = today or datetime.now(LOCAL_TZ).date()
    if commitments is None:
        commitments = Commitment.query.filter_by(user_id=user.id).all()
        if not commitments:
            commitments = Commitment.query.filter_by(member_name=user.username).all()
    commitments_by_event = {row.event: row for row in commitments}
    grades = {}
    incomplete_reasons = []
    overdue_reasons = []
    low_grade_reasons = []
    conferences = {}
    for conference in CONFERENCE_ORDER:
        commitment = commitments_by_event.get(conference)
        if not commitment:
            grades[conference] = None
            conferences[conference] = None
            continue
        grades[conference] = commitment.grade
        missing_parts = []
        if commitment.remaining_roleplay:
            missing_parts.append(f'{commitment.remaining_roleplay} roleplay')
        if commitment.remaining_written:
            missing_parts.append(f'{commitment.remaining_written} written')
        if commitment.remaining_exam:
            missing_parts.append(f'{commitment.remaining_exam} exam')
        description = f"{conference}: " + ', '.join(missing_parts) if missing_parts else None
        if description:
            incomplete_reasons.append(description)
            if commitment.deadline and commitment.deadline < today:
                overdue_reasons.append(description)
        if _numeric_grade_below_100(commitment.grade):
            low_grade_reasons.append(f'{conference} grade is {commitment.grade} (below 100%)')
        conferences[conference] = {
            'grade': commitment.grade,
            'deadline': commitment.deadline,
            'complete': not missing_parts,
            'remaining_roleplay': commitment.remaining_roleplay,
            'remaining_written': commitment.remaining_written,
            'remaining_exam': commitment.remaining_exam,
        }
    return {
        'grades': grades,
        'conferences': conferences,
        'incomplete_reasons': incomplete_reasons,
        'overdue_reasons': overdue_reasons,
        'low_grade_reasons': low_grade_reasons,
    }


def build_mentee_risk_report(members, event_items, event_deadlines, today=None):
    today = today or datetime.now(LOCAL_TZ).date()
    rows = []
    for member in members:
        pod = MentorPod.query.filter_by(member_id=member.id).first()
        event = (pod.event or '').strip() if pod else ''
        level = pod.experience_level if pod else 'N'
        attendance = get_attendance_stats(member)
        attendance_reasons = []
        if attendance['ah_total'] > 0 and attendance['ah_rate'] < AH_THRESHOLD * 100:
            attendance_reasons.append(
                f"AH attendance {attendance['ah_rate']}% (requires {AH_THRESHOLD * 100:.0f}%)"
            )
        if attendance['ws_total'] > 0 and attendance['ws_rate'] < attendance['ws_threshold_pct']:
            attendance_reasons.append(
                f"WS attendance {attendance['ws_rate']}% (requires {attendance['ws_threshold_pct']:.0f}%)"
            )
        conference = _conference_summary_for_user(member, today=today)
        item_names = event_items.get(event, [])
        completed = {
            item.item_name: bool(item.completed)
            for item in ChecklistItem.query.filter_by(user_id=member.id, event=event).all()
        }
        written = _written_status(
            item_names,
            completed,
            event_deadlines.get(event, {}),
            today=today,
        ) if item_names else {
            'status': 'not_tracked',
            'status_label': 'Not Tracked',
            'missing_items': [],
            'overdue_items': [],
            'complete': True,
            'deadline_safe': True,
        }
        overdue_written_names = {item['name'] for item in written['overdue_items']}
        hard_risk_reasons = list(attendance_reasons) + list(conference['overdue_reasons'])
        hard_risk_reasons.extend(
            'Written overdue: ' + item['name']
            + (f" ({item['deadline_text']})" if item['deadline_text'] else '')
            for item in written['overdue_items']
        )
        attention_reasons = list(conference['incomplete_reasons'])
        attention_reasons.extend(conference['low_grade_reasons'])
        attention_reasons.extend(
            'Written incomplete: ' + item['name']
            + (f" ({item['deadline_text']})" if item['deadline_text'] else '')
            for item in written['missing_items']
            if item['name'] not in overdue_written_names
        )
        if hard_risk_reasons:
            status, status_label = 'at_risk', 'At Risk'
        elif attention_reasons:
            status, status_label = 'needs_attention', 'Needs Attention'
        else:
            status, status_label = 'on_track', 'On Track'
        event_label = event or 'Unassigned'
        rows.append({
            'member': member,
            'mentor_name': get_mentor_name(member),
            'event': event_label,
            'event_keys': [event_label],
            'level': level,
            'status': status,
            'status_label': status_label,
            'hard_risk_reasons': hard_risk_reasons,
            'attention_reasons': attention_reasons,
            'attendance': attendance,
            'grades': conference['grades'],
            'conference': conference,
            'written': written,
        })
    return rows


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route('/')
@login_required
def dashboard():
    commitments = []
    progress_summary = None
    attendance_summary = None
    assigned_workshops = []
    workshop_attendance_data = []
    workshops = []
    created_workshops = []
    attendance_locked_ids = set()
    ah_ws_data = []      # NEW: AH/WS attendance per member for officer view
    member_stats = None  # NEW: AH/WS stats for member's own view
    all_commitments = []
    dashboard_scope_label = "Members in Pod"
    visible_members = []

    if current_user.role == 'officer':
        commitments = Commitment.query.filter_by(user_id=current_user.id).order_by(Commitment.deadline).all()
        assigned_workshops = Workshop.query.filter_by(officer_id=current_user.id).options(joinedload(Workshop.officer)).order_by(Workshop.time).all()
        workshops = assigned_workshops
        attendance_locked_ids = {row.workshop_id for row in AttendanceSubmission.query.filter_by(officer_id=current_user.id).all()}

        # Build member list from pod assignments
        pod_members = MentorPod.query.filter_by(mentor_id=current_user.id).all()
        pod_member_users = [db.session.get(User, pm.member_id) for pm in pod_members]
        pod_member_users = [u for u in pod_member_users if u]

        # Fall back to commitment-based member names if no pod assignments
        if not pod_member_users:
            member_names = {c.member_name for c in commitments}
            for ws in assigned_workshops:
                for member in ws.signups:
                    member_names.add(member.username)
            pod_member_users = [User.query.filter_by(username=n).first() for n in sorted(member_names)]
            pod_member_users = [u for u in pod_member_users if u]

        # Workshop attendance (existing logic)
        for member in sorted(pod_member_users, key=lambda u: u.username.lower()):
            actual_attended = db.session.query(workshop_signups).filter_by(user_id=member.id, attended=True).join(Workshop).filter(Workshop.officer_id == current_user.id).count()
            ga = GeneralAttendance.query.filter_by(officer_id=current_user.id, member_name=member.username).first()
            manual_count = ga.manual_count if ga else 0
            total_attended = actual_attended + manual_count
            workshop_attendance_data.append({
                'member_name': member.username,
                'total_attended': total_attended,
                'manual_count': manual_count,
                'actual_attended': actual_attended
            })

        # NEW: AH/WS attendance stats per pod member
        for member in sorted(pod_member_users, key=lambda u: u.username.lower()):
            stats = get_attendance_stats(member)
            pod = MentorPod.query.filter_by(member_id=member.id).first()
            ah_ws_data.append({
                'member': member,
                'pod_number': pod.pod_number if pod else '?',
                'level': stats['level'],
                'ah_rate': stats['ah_rate'],
                'ah_sum': stats['ah_sum'],
                'ah_total': stats['ah_total'],
                'ws_rate': stats['ws_rate'],
                'ws_sum': stats['ws_sum'],
                'ws_total': stats['ws_total'],
                'ws_threshold_pct': stats['ws_threshold_pct'],
                'at_risk': stats['at_risk'],
                'risk_reasons': stats['risk_reasons'],
            })

    else:
        # Use the active conference commitment only
        active_conf = get_active_conference()
        ensure_commitments(current_user)
        commitments = Commitment.query.filter_by(
            member_name=current_user.username, event=active_conf).all()
        all_commitments = Commitment.query.filter_by(member_name=current_user.username).order_by(Commitment.deadline).all()
        workshops = current_user.workshops.options(joinedload(Workshop.officer), joinedload(Workshop.creator)).order_by(Workshop.time).all()
        created_workshops = []

        if commitments:
            c = commitments[0]
            progress_summary = {
                'roleplay': f"{c.required_roleplay - c.remaining_roleplay}/{c.required_roleplay}",
                'written': f"{c.required_written - c.remaining_written}/{c.required_written}",
                'exam': f"{c.required_exam - c.remaining_exam}/{c.required_exam}",
                'deadline': c.deadline.strftime('%Y-%m-%d') if c.deadline else 'N/A',
                'event': c.event
            }

        # Workshop attendance summary (kept for AH/WS section)
        pod = MentorPod.query.filter_by(member_id=current_user.id).first()
        officer_id = pod.mentor_id if pod else None
        actual_attended = 0
        manual_count = 0
        if officer_id:
            actual_attended = db.session.query(workshop_signups).filter_by(user_id=current_user.id, attended=True).join(Workshop).filter(Workshop.officer_id == officer_id).count()
            ga = GeneralAttendance.query.filter_by(officer_id=officer_id, member_name=current_user.username).first()
            manual_count = ga.manual_count if ga else 0
        total_attended = actual_attended + manual_count
        total_signed = len(current_user.workshops.all())
        attendance_summary = {
            'signed': total_signed,
            'attended': total_attended,
            'rate': round((total_attended / 18 * 100) if 18 > 0 else 0.0, 1)
        }

        # AH/WS stats for member's own dashboard
        member_stats = get_attendance_stats(current_user)

    # Admins see every member; officers see their pod/fallback members.
    if current_user.is_admin or current_user.role == 'officer':
        member_ids = [member.id for member in visible_members]
        pods_by_member = {}
        ah_by_user = defaultdict(list)
        ws_by_user = defaultdict(list)
        commitments_by_user = defaultdict(list)

        if member_ids:
            for pod in MentorPod.query.options(joinedload(MentorPod.mentor)).filter(
                MentorPod.member_id.in_(member_ids)
            ).all():
                pods_by_member.setdefault(pod.member_id, pod)

            for record in AHAttendance.query.filter(
                AHAttendance.user_id.in_(member_ids)
            ).all():
                ah_by_user[record.user_id].append(record)

            for record in WSAttendance.query.filter(
                WSAttendance.user_id.in_(member_ids)
            ).all():
                ws_by_user[record.user_id].append(record)

            for commitment in Commitment.query.filter(
                Commitment.user_id.in_(member_ids)
            ).all():
                commitments_by_user[commitment.user_id].append(commitment)

        for member in sorted(visible_members, key=lambda row: row.username.lower()):
            pod = pods_by_member.get(member.id)
            stats = get_attendance_stats(
                member,
                ah_records=ah_by_user.get(member.id, []),
                ws_records=ws_by_user.get(member.id, []),
                pod=pod,
                pod_loaded=True,
            )
            ah_ws_data.append({
                'member': member,
                'pod_number': pod.pod_number if pod else None,
                'mentor_name': (
                    pod.mentor.username if pod and pod.mentor else 'Unassigned'
                ),
                'event': pod.event if pod and pod.event else 'Unassigned',
                'level': stats['level'],
                'ah_rate': stats['ah_rate'],
                'ah_present': stats['ah_present'],
                'ah_excused': stats['ah_excused'],
                'ah_absent': stats['ah_absent'],
                'ah_sum': stats['ah_sum'],
                'ah_total': stats['ah_total'],
                'ws_rate': stats['ws_rate'],
                'ws_present': stats['ws_present'],
                'ws_excused': stats['ws_excused'],
                'ws_absent': stats['ws_absent'],
                'ws_sum': stats['ws_sum'],
                'ws_total': stats['ws_total'],
                'ws_threshold_pct': stats['ws_threshold_pct'],
                'at_risk': stats['at_risk'],
                'risk_reasons': stats['risk_reasons'],
                'below_commitments': get_commitments_incomplete(
                    member,
                    commitments=commitments_by_user.get(member.id, []),
                ),
            })
    for ws in workshops:
        ws.end_time = ws.time + timedelta(minutes=20)
    for ws in created_workshops:
        ws.end_time = ws.time + timedelta(minutes=20)

    my_signups = current_user.workshops.all() if current_user.role == 'member' else []
    mentees_workshops = {}
    if current_user.role == 'officer':
        mentee_names = {c.member_name for c in commitments}
        for name in mentee_names:
            member = User.query.filter_by(username=name).first()
            mentees_workshops[name] = member.workshops.order_by(Workshop.time).all() if member else []

    signed_times = [(w.time, w.time + timedelta(minutes=20)) for w in my_signups] if current_user.role == 'member' else []

    active_conf = get_active_conference() if current_user.role == 'member' else None
    return render_template('dashboard.html',
        commitments=commitments,
        progress_summary=progress_summary,
        attendance_summary=attendance_summary,
        assigned_workshops=assigned_workshops,
        workshop_attendance_data=workshop_attendance_data,
        workshops=workshops,
        my_signups=my_signups,
        mentees_workshops=mentees_workshops,
        signed_times=signed_times,
        user=current_user,
        created_workshops=created_workshops,
        attendance_locked_ids=attendance_locked_ids,
        ah_ws_data=ah_ws_data,
        member_stats=member_stats,
        active_conf=active_conf,
        all_commitments=all_commitments if current_user.role == 'member' else [],
        conference_order=CONFERENCE_ORDER,
    )

@app.route('/register', methods=['GET', 'POST'])
def register():
    if current_user.is_authenticated:
        return redirect(url_for('dashboard'))
    form = RegisterForm()
    if form.validate_on_submit():
        existing = User.query.filter_by(username=form.username.data.strip()).first()
        if existing:
            flash('This username is already in use. Please choose a different username.', 'warning')
            return render_template('register.html', form=form)
        hashed_pw = generate_password_hash(form.password.data)
        user = User(username=form.username.data.strip(), password=hashed_pw, role=form.role.data)
        db.session.add(user)
        db.session.commit()
        flash('Registration successful. Please sign in.', 'success')
        return redirect(url_for('login'))
    return render_template('register.html', form=form)

@app.route('/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated:
        return redirect(url_for('dashboard'))
    form = LoginForm()
    if form.validate_on_submit():
        user = User.query.filter_by(username=form.username.data.strip()).first()
        if user and check_password_hash(user.password, form.password.data):
            login_user(user)
            if user.must_change_password:
                return redirect(url_for('change_password'))
            # Auto-create commitment rows for members on login
            if user.role == 'member':
                ensure_commitments(user)
            return redirect(url_for('dashboard'))
        flash('Invalid username or password. Please try again.', 'danger')
    return render_template('login.html', form=form)

@app.route('/change-password', methods=['GET', 'POST'])
@login_required
def change_password():
    form = ChangePasswordForm()
    if form.validate_on_submit():
        current_user.password = generate_password_hash(form.new_password.data)
        current_user.must_change_password = False
        db.session.commit()
        flash('Password updated successfully. Welcome!', 'success')
        return redirect(url_for('dashboard'))
    return render_template('change_password.html', form=form)

@app.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('login'))

@app.route('/settings', methods=['GET', 'POST'])
@login_required
def settings():
    if current_user.role != 'member':
        flash('Only members can access settings.', 'danger')
        return redirect(url_for('dashboard'))
    if request.method == 'POST':
        current_user.email = request.form.get('email', '').strip() or None
        current_user.phone = request.form.get('phone', '').strip() or None
        current_user.notify_enabled = bool(request.form.get('notify_enabled'))
        remind_val = request.form.get('remind_minutes_before', '60').strip()
        try:
            current_user.remind_minutes_before = int(remind_val)
        except ValueError:
            current_user.remind_minutes_before = 60
        db.session.commit()
        flash('Settings updated.', 'success')
        return redirect(url_for('settings'))
    return render_template('settings.html')

@app.route('/add_workshop', methods=['GET', 'POST'])
@login_required
def add_workshop():
    if current_user.role != 'member':
        flash('Only members can add workshops.', 'danger')
        return redirect(url_for('dashboard'))
    form = WorkshopForm()
    officers = User.query.filter_by(role='officer').order_by(User.username).all()
    form.officer_id.choices = [(0, 'Select an officer')] + [(o.id, o.username) for o in officers]
    if form.validate_on_submit():
        local_workshop_time = datetime.strptime(
            f"{form.workshop_date.data} {form.slot.data}:00", "%Y-%m-%d %H:%M:%S"
        )
        workshop_time = local_to_utc(local_workshop_time)
        error = validate_workshop_slot(workshop_time, form.officer_id.data)
        if error:
            flash(error, 'warning')
            created_workshops = Workshop.query.filter_by(creator_id=current_user.id).options(joinedload(Workshop.officer)).order_by(Workshop.time).all()
            return render_template('add_workshop.html', form=form, created_workshops=created_workshops)
        ws = Workshop(name=form.activity_type.data, time=workshop_time, officer_id=form.officer_id.data,
                      activity_type=form.activity_type.data, creator_id=current_user.id)
        db.session.add(ws)
        db.session.flush()
        if current_user not in ws.signups:
            ws.signups.append(current_user)
        try:
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            flash('This officer already has a workshop booked for that date and time slot. Please choose a different time or officer.', 'warning')
            created_workshops = Workshop.query.filter_by(creator_id=current_user.id).options(joinedload(Workshop.officer)).order_by(Workshop.time).all()
            return render_template('add_workshop.html', form=form, created_workshops=created_workshops)
        flash('Workshop added.', 'success')
        return redirect(url_for('add_workshop'))
    created_workshops = Workshop.query.filter_by(creator_id=current_user.id).options(joinedload(Workshop.officer)).order_by(Workshop.time).all()
    return render_template('add_workshop.html', form=form, created_workshops=created_workshops)

@app.route('/edit_workshop/<int:workshop_id>', methods=['GET', 'POST'])
@login_required
def edit_workshop(workshop_id):
    workshop = Workshop.query.get_or_404(workshop_id)
    if current_user.role != 'member' or workshop.creator_id != current_user.id:
        flash('You are not allowed to edit this workshop.', 'danger')
        return redirect(url_for('add_workshop'))
    original_date = workshop.time.date()
    original_slot = workshop.time.strftime('%H:%M')
    form = WorkshopForm()
    officers = User.query.filter_by(role='officer').order_by(User.username).all()
    form.officer_id.choices = [(0, 'Select an officer')] + [(o.id, o.username) for o in officers]
    if request.method == 'GET':
        form.workshop_date.data = original_date
        form.slot.data = original_slot
        form.activity_type.data = workshop.activity_type
        form.officer_id.data = workshop.officer_id
    if form.validate_on_submit():
        local_workshop_time = datetime.strptime(
            f"{form.workshop_date.data} {form.slot.data}:00", "%Y-%m-%d %H:%M:%S"
        )
        workshop_time = local_to_utc(local_workshop_time)
        error = validate_workshop_slot(workshop_time, form.officer_id.data, exclude_workshop_id=workshop.id)
        if error:
            flash(error, 'warning')
            created_workshops = Workshop.query.filter_by(creator_id=current_user.id).options(joinedload(Workshop.officer)).order_by(Workshop.time).all()
            form.workshop_date.data = original_date
            form.slot.data = original_slot
            form.officer_id.data = workshop.officer_id
            form.activity_type.data = workshop.activity_type
            return render_template('add_workshop.html', form=form, created_workshops=created_workshops, editing_workshop=workshop)
        workshop.name = form.activity_type.data
        workshop.time = workshop_time
        workshop.officer_id = form.officer_id.data
        workshop.activity_type = form.activity_type.data
        try:
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            flash('This officer already has a workshop booked for that date and time slot. Please choose a different time or officer.', 'warning')
            created_workshops = Workshop.query.filter_by(creator_id=current_user.id).options(joinedload(Workshop.officer)).order_by(Workshop.time).all()
            form.workshop_date.data = original_date
            form.slot.data = original_slot
            form.officer_id.data = workshop.officer_id
            form.activity_type.data = workshop.activity_type
            return render_template('add_workshop.html', form=form, created_workshops=created_workshops, editing_workshop=workshop)
        flash('Workshop updated.', 'success')
        return redirect(url_for('add_workshop'))
    created_workshops = Workshop.query.filter_by(creator_id=current_user.id).options(joinedload(Workshop.officer)).order_by(Workshop.time).all()
    return render_template('add_workshop.html', form=form, created_workshops=created_workshops)

@app.route('/delete_workshop/<int:workshop_id>', methods=['POST'])
@login_required
def delete_workshop(workshop_id):
    workshop = Workshop.query.get_or_404(workshop_id)
    if current_user.role != 'member' or workshop.creator_id != current_user.id:
        flash('You are not allowed to delete this workshop.', 'danger')
        return redirect(url_for('dashboard'))
    db.session.delete(workshop)
    db.session.commit()
    flash('Workshop deleted.', 'success')
    return redirect(url_for('add_workshop'))

@app.route('/signup_workshop/<int:workshop_id>', methods=['POST'])
@login_required
def signup_workshop(workshop_id):
    if current_user.role != 'member':
        flash('Only members can sign up.', 'danger')
        return redirect(url_for('dashboard'))
    workshop = Workshop.query.get_or_404(workshop_id)
    if current_user in workshop.signups:
        flash('Already signed up.', 'info')
        return redirect(url_for('dashboard'))
    workshop.signups.append(current_user)
    db.session.commit()
    flash('Signed up.', 'success')
    return redirect(url_for('dashboard'))

@app.route('/cancel_signup/<int:workshop_id>', methods=['POST'])
@login_required
def cancel_signup(workshop_id):
    if current_user.role != 'member':
        flash('Only members can cancel sign-ups.', 'danger')
        return redirect(url_for('dashboard'))
    workshop = Workshop.query.get_or_404(workshop_id)
    if current_user in workshop.signups:
        workshop.signups.remove(current_user)
        db.session.commit()
        flash('Sign-up cancelled.', 'success')
    else:
        flash('Not signed up.', 'info')
    return redirect(url_for('dashboard'))

@app.route('/workshop/<int:workshop_id>/attendance', methods=['GET', 'POST'])
@login_required
def workshop_attendance(workshop_id):
    if current_user.role != 'officer':
        flash('Only officers can take attendance.', 'danger')
        return redirect(url_for('dashboard'))
    workshop = Workshop.query.options(joinedload(Workshop.officer)).get_or_404(workshop_id)
    if workshop.officer_id != current_user.id:
        flash('You are not the assigned officer for this workshop.', 'danger')
        return redirect(url_for('dashboard'))
    already_submitted = AttendanceSubmission.query.filter_by(workshop_id=workshop_id, officer_id=current_user.id).first() is not None
    if request.method == 'POST':
        if already_submitted:
            flash('Attendance has already been submitted for this workshop.', 'warning')
            return redirect(url_for('reports', tab='calendar'))
        db.session.execute(workshop_signups.update().where(workshop_signups.c.workshop_id == workshop_id).values(attended=False))
        for key in request.form:
            if key.startswith('attended_user_') and request.form.get(key) == 'on':
                user_id = int(key.split('_')[-1])
                db.session.execute(workshop_signups.update().where(workshop_signups.c.workshop_id == workshop_id).where(workshop_signups.c.user_id == user_id).values(attended=True))
                member = db.session.get(User, user_id)
                if member:
                    commitment = Commitment.query.filter_by(member_name=member.username, user_id=current_user.id).first()
                    if commitment:
                        if workshop.activity_type == 'Roleplay':
                            commitment.remaining_roleplay = max(0, commitment.remaining_roleplay - 1)
                        elif workshop.activity_type == 'Written Presentation':
                            commitment.remaining_written = max(0, commitment.remaining_written - 1)
                        elif workshop.activity_type == 'Exam':
                            commitment.remaining_exam = max(0, commitment.remaining_exam - 1)
                        db.session.add(commitment)
        db.session.add(AttendanceSubmission(workshop_id=workshop_id, officer_id=current_user.id))
        db.session.commit()
        flash('Attendance updated successfully.', 'success')
        return redirect(url_for('reports', tab='calendar'))
    attendance_records = db.session.query(workshop_signups).filter_by(workshop_id=workshop_id).all()
    members_with_attendance = []
    for record in attendance_records:
        user = db.session.get(User, record.user_id)
        if user:
            members_with_attendance.append({'user': user, 'attended': record.attended})
    return render_template('attendance.html', workshop=workshop, members_with_attendance=members_with_attendance, already_submitted=already_submitted)

@app.route('/increment_general_attendance', methods=['POST'])
@login_required
def increment_general_attendance():
    if current_user.role != 'officer':
        flash('Only officers can update attendance.', 'danger')
        return redirect(url_for('dashboard'))
    member_name_raw = request.form.get('member_name', '').strip()
    if not member_name_raw:
        flash('Member name is missing.', 'warning')
        return redirect(url_for('dashboard'))
    member_name = member_name_raw.lower()
    ga = GeneralAttendance.query.filter(GeneralAttendance.officer_id == current_user.id, db.func.lower(GeneralAttendance.member_name) == member_name).first()
    if not ga:
        ga = GeneralAttendance(officer_id=current_user.id, member_name=member_name_raw, manual_count=0)
        db.session.add(ga)
        db.session.commit()
    ga.manual_count += 1
    db.session.commit()
    flash('Attendance was added successfully.', 'success')
    return redirect(request.referrer or url_for('dashboard'))

# ── Member Commitments route ─────────────────────────────────────────────────

@app.route('/my_commitments')
@login_required
def member_commitments():
    if current_user.role != 'member':
        return redirect(url_for('dashboard'))
    ensure_commitments(current_user)
    active_conf = get_active_conference()
    all_commitments = Commitment.query.filter_by(member_name=current_user.username).all()
    all_commitments_map = {com.event: com for com in all_commitments}
    icprep_status = get_icprep_status(current_user)
    return render_template('member_commitments.html',
        all_commitments_map=all_commitments_map,
        conference_order=CONFERENCE_ORDER,
        active_conf=active_conf,
        icprep_status=icprep_status,
    )


# ── Practice Session routes ──────────────────────────────────────────────────

@app.route('/practice_sessions', methods=['GET', 'POST'])
@login_required
def practice_sessions():
    """Officer: post and manage practice slots. Member: view and sign up."""
    if current_user.role == 'officer':
        # POST: create a new practice session slot
        if request.method == 'POST':
            action = request.form.get('action')
            if action == 'create':
                ps = PracticeSession(
                    officer_id=current_user.id,
                    session_date=datetime.strptime(request.form['session_date'], '%Y-%m-%d').date(),
                    session_time=request.form['session_time'],
                    practice_type=request.form['practice_type'],
                    conference=request.form['conference'],
                    notes=request.form.get('notes', '').strip() or None,
                )
                db.session.add(ps)
                db.session.commit()
                flash('Practice slot posted.', 'success')
            elif action == 'delete':
                ps_id = int(request.form['session_id'])
                ps = PracticeSession.query.get_or_404(ps_id)
                if ps.officer_id != current_user.id:
                    flash('Not authorized.', 'danger')
                else:
                    db.session.delete(ps)
                    db.session.commit()
                    flash('Practice slot removed.', 'success')
            return redirect(url_for('practice_sessions'))

        # GET: show officer's posted slots
        my_sessions = PracticeSession.query.filter_by(officer_id=current_user.id).order_by(
            PracticeSession.session_date, PracticeSession.session_time).all()
        # Pod members for the log form
        pod_members = MentorPod.query.filter_by(mentor_id=current_user.id).all()
        pod_member_users = [db.session.get(User, pm.member_id) for pm in pod_members]
        pod_member_users = [u for u in pod_member_users if u]
        return render_template('practice_sessions.html',
            my_sessions=my_sessions,
            pod_member_users=pod_member_users,
            conference_order=CONFERENCE_ORDER,
            activity_types=ACTIVITY_TYPES,
            time_slots=TIME_SLOTS,
            active_conf=get_active_conference(),
        )
    else:
        # Member view: show open slots from their officer
        pod = MentorPod.query.filter_by(member_id=current_user.id).first()
        open_sessions = []
        signed_up = []
        officer = None
        active_conf = get_active_conference()
        active_commitment = Commitment.query.filter_by(
            member_name=current_user.username, event=active_conf).first()
        if pod:
            officer = db.session.get(User, pod.mentor_id)
            open_sessions = PracticeSession.query.filter_by(
                officer_id=pod.mentor_id, member_id=None).order_by(
                PracticeSession.session_date, PracticeSession.session_time).all()
            signed_up = PracticeSession.query.filter_by(
                officer_id=pod.mentor_id, member_id=current_user.id).order_by(
                PracticeSession.session_date).all()
        icprep_status = get_icprep_status(current_user)
        return render_template('practice_sessions.html',
            open_sessions=open_sessions,
            signed_up=signed_up,
            officer=officer,
            active_conf=active_conf,
            active_commitment=active_commitment,
            conference_order=CONFERENCE_ORDER,
            icprep_status=icprep_status,
        )


@app.route('/practice_sessions/signup/<int:session_id>', methods=['POST'])
@login_required
def practice_session_signup(session_id):
    if current_user.role != 'member':
        flash('Only members can sign up for practice sessions.', 'danger')
        return redirect(url_for('practice_sessions'))
    ps = PracticeSession.query.get_or_404(session_id)
    if ps.member_id is not None:
        flash('This slot is already taken.', 'warning')
        return redirect(url_for('practice_sessions'))
    ps.member_id = current_user.id
    db.session.commit()
    flash('Signed up for practice session.', 'success')
    return redirect(url_for('practice_sessions'))


@app.route('/practice_sessions/cancel/<int:session_id>', methods=['POST'])
@login_required
def practice_session_cancel(session_id):
    if current_user.role != 'member':
        flash('Only members can cancel.', 'danger')
        return redirect(url_for('practice_sessions'))
    ps = PracticeSession.query.get_or_404(session_id)
    if ps.member_id != current_user.id:
        flash('You are not signed up for this slot.', 'warning')
        return redirect(url_for('practice_sessions'))
    ps.member_id = None
    db.session.commit()
    flash('Cancelled sign-up.', 'success')
    return redirect(url_for('practice_sessions'))



@app.route('/log_commitment/<int:session_id>', methods=['POST'])
@login_required
def log_commitment(session_id):
    """Officer checks off a commitment directly from the session slot — no form."""
    if current_user.role != 'officer':
        flash('Only officers can log commitments.', 'danger')
        return redirect(url_for('practice_sessions'))

    ps = db.session.get(PracticeSession, session_id)
    if not ps or ps.officer_id != current_user.id:
        flash('Session not found or not yours.', 'danger')
        return redirect(url_for('practice_sessions'))
    if ps.log_submitted:
        flash('Completion already logged for this session.', 'warning')
        return redirect(request.referrer or url_for('practice_sessions'))
    if not ps.member:
        flash('No member signed up for this slot yet.', 'warning')
        return redirect(request.referrer or url_for('practice_sessions'))

    member = ps.member
    conference = ps.conference
    practice_type = ps.practice_type

    # Decrement conference commitment
    commitment = Commitment.query.filter_by(
        member_name=member.username, event=conference).first()
    if commitment:
        if practice_type in ROLEPLAY_TYPES and commitment.remaining_roleplay > 0:
            commitment.remaining_roleplay -= 1
        elif practice_type in WRITTEN_TYPES and commitment.remaining_written > 0:
            commitment.remaining_written -= 1
        elif practice_type in EXAM_TYPES and commitment.remaining_exam > 0:
            commitment.remaining_exam -= 1
        db.session.add(commitment)

    # If ICPrep type, increment annual tracker
    if practice_type in ICPREP_TYPES:
        ensure_commitments(member)
        tracker = AnnualICPrepTracker.query.filter_by(member_id=member.id).first()
        if tracker:
            if practice_type == 'ICPrep Roleplay':
                tracker.icprep_rp_completed += 1
            elif practice_type == 'ICPrep Exam':
                tracker.icprep_exam_completed += 1
            db.session.add(tracker)

    # Create a minimal log record for audit trail
    log = PracticeLog(
        practice_session_id=ps.id,
        officer_id=current_user.id,
        member_id=member.id,
        commitment_id=commitment.id if commitment else None,
        practice_type=practice_type,
        conference=conference,
        session_date=ps.session_date,
    )
    db.session.add(log)

    ps.log_submitted = True
    db.session.add(ps)
    db.session.commit()

    flash(f'Marked {practice_type} complete for {member.username} ({conference}).', 'success')
    return redirect(request.referrer or url_for('practice_sessions'))


@app.route('/reports')
@login_required
def reports():
    if current_user.role != 'officer':
        flash('Only officers can view reports.', 'danger')
        return redirect(url_for('dashboard'))
    active_tab = request.args.get('tab', 'commitment')

    # ── Commitment Reports tab: pod member progress per conference ──
    pod_members = MentorPod.query.filter_by(mentor_id=current_user.id).all()
    pod_member_users = [db.session.get(User, pm.member_id) for pm in pod_members]
    pod_member_users = [u for u in pod_member_users if u]
    active_conf = get_active_conference()

    commitment_data = []
    for member in sorted(pod_member_users, key=lambda u: u.username.lower()):
        pod = MentorPod.query.filter_by(member_id=member.id).first()
        level = pod.experience_level if pod else 'N'
        member_commitments = {}
        for conf in CONFERENCE_ORDER:
            com = Commitment.query.filter_by(member_name=member.username, event=conf).first()
            member_commitments[conf] = com
        commitment_data.append({
            'member': member,
            'level': level,
            'pod_number': pod.pod_number if pod else '?',
            'commitments': member_commitments,
            'active_conf': active_conf,
        })

    # ── Calendar tab: practice sessions with signups ──
    practice_sessions_list = PracticeSession.query.filter_by(officer_id=current_user.id).order_by(
        PracticeSession.session_date, PracticeSession.session_time).all()
    calendar_groups = {}
    time_map = {'15:00': '3:00 pm', '15:20': '3:20 pm', '15:40': '3:40 pm'}
    for ps in practice_sessions_list:
        day = ps.session_date.strftime('%Y-%m-%d')
        calendar_groups.setdefault(day, []).append({
            'session': ps,
            'time_label': time_map.get(ps.session_time, ps.session_time),
        })

    # Keep AH/WS data for reports page (used in existing split tables)
    ah_ws_data = []
    for member in sorted(pod_member_users, key=lambda u: u.username.lower()):
        stats = get_attendance_stats(member)
        pod = MentorPod.query.filter_by(member_id=member.id).first()
        ah_ws_data.append({
            'member': member,
            'pod_number': pod.pod_number if pod else '?',
            'level': stats['level'],
            'ah_rate': stats['ah_rate'],
            'ah_sum': stats['ah_sum'],
            'ah_total': stats['ah_total'],
            'ws_rate': stats['ws_rate'],
            'ws_sum': stats['ws_sum'],
            'ws_total': stats['ws_total'],
            'ws_threshold_pct': stats['ws_threshold_pct'],
            'at_risk': stats['at_risk'],
            'risk_reasons': stats['risk_reasons'],
        })

    return render_template('reports.html',
        active_tab=active_tab,
        commitment_data=commitment_data,
        conference_order=CONFERENCE_ORDER,
        active_conf=active_conf,
        calendar_groups=calendar_groups,
        ah_ws_data=ah_ws_data,
    )


# ── ICPrep Webhook ───────────────────────────────────────────────────────────

@app.route('/webhook/icprep', methods=['POST'])
def icprep_webhook():
    """Placeholder webhook endpoint for ICPrep completions.
    Expected payload (TBD with ICPrep):
      { "member_username": "...", "activity_type": "ICPrep Roleplay"|"ICPrep Exam",
        "conference": "VCMC"|"SVCDC"|"SCDC", "score": 0.0, "secret": "..." }
    """
    import json as _json
    ICPREP_WEBHOOK_SECRET = os.getenv('ICPREP_WEBHOOK_SECRET', 'changeme')
    raw = request.get_data(as_text=True)
    log = ICPrepWebhookLog(payload=raw)

    try:
        data = request.get_json(force=True) or {}
        # Verify shared secret
        if data.get('secret') != ICPREP_WEBHOOK_SECRET:
            log.error = 'Invalid secret'
            db.session.add(log)
            db.session.commit()
            return {'error': 'Unauthorized'}, 401

        member_username = data.get('member_username', '').strip()
        activity_type = data.get('activity_type', '')
        conference = data.get('conference', '')

        member = User.query.filter_by(username=member_username, role='member').first()
        if not member:
            log.error = f'Member not found: {member_username}'
            db.session.add(log)
            db.session.commit()
            return {'error': 'Member not found'}, 404

        log.member_id = member.id
        log.activity_type = activity_type

        if activity_type not in ICPREP_TYPES:
            log.error = f'Unknown activity_type: {activity_type}'
            db.session.add(log)
            db.session.commit()
            return {'error': 'Unknown activity_type'}, 400

        # Decrement conference commitment
        ensure_commitments(member)
        commitment = Commitment.query.filter_by(
            member_name=member.username, event=conference).first()
        if commitment:
            if activity_type == 'ICPrep Roleplay' and commitment.remaining_roleplay > 0:
                commitment.remaining_roleplay -= 1
            elif activity_type == 'ICPrep Exam' and commitment.remaining_exam > 0:
                commitment.remaining_exam -= 1
            db.session.add(commitment)

        # Increment annual ICPrep tracker
        tracker = AnnualICPrepTracker.query.filter_by(member_id=member.id).first()
        if tracker:
            if activity_type == 'ICPrep Roleplay':
                tracker.icprep_rp_completed += 1
            elif activity_type == 'ICPrep Exam':
                tracker.icprep_exam_completed += 1
            db.session.add(tracker)

        log.processed = True
        db.session.add(log)
        db.session.commit()
        return {'status': 'ok', 'member': member_username, 'type': activity_type}, 200

    except Exception as e:
        log.error = str(e)
        db.session.add(log)
        try:
            db.session.commit()
        except Exception:
            db.session.rollback()
        return {'error': 'Internal error'}, 500


# ── Exam Upload routes ────────────────────────────────────────────────────────

def get_cloudinary():
    """Lazy-import and configure cloudinary."""
    import cloudinary
    import cloudinary.uploader
    cloudinary.config(
        cloud_name=os.getenv('CLOUDINARY_CLOUD_NAME'),
        api_key=os.getenv('CLOUDINARY_API_KEY'),
        api_secret=os.getenv('CLOUDINARY_API_SECRET'),
        secure=True,
    )
    return cloudinary


@app.route('/exam_uploads', methods=['GET', 'POST'])
@login_required
def exam_uploads():
    """Member: upload paper exam. Officer: view and review pod uploads."""
    if current_user.role == 'member':
        if request.method == 'POST':
            file = request.files.get('exam_file')
            conference = request.form.get('conference', '').strip()
            notes = request.form.get('notes', '').strip()

            if not file or file.filename == '':
                flash('Please select a file to upload.', 'danger')
                return redirect(url_for('exam_uploads'))
            if not conference:
                flash('Please select a conference.', 'danger')
                return redirect(url_for('exam_uploads'))

            allowed = {'png', 'jpg', 'jpeg', 'gif', 'pdf', 'heic', 'webp'}
            ext = file.filename.rsplit('.', 1)[-1].lower() if '.' in file.filename else ''
            if ext not in allowed:
                flash('File type not allowed. Please upload an image or PDF.', 'danger')
                return redirect(url_for('exam_uploads'))

            try:
                cld = get_cloudinary()
                result = cld.uploader.upload(
                    file,
                    folder=f'deca_tracker/exams/{current_user.username}',
                    resource_type='auto',
                )
                upload = ExamUpload(
                    member_id=current_user.id,
                    conference=conference,
                    cloudinary_url=result['secure_url'],
                    cloudinary_public_id=result['public_id'],
                    notes=notes or None,
                )
                db.session.add(upload)

                # In-app notification: find officer and notify via flash on their next load
                # (stored as a simple DB notification row via send_email to officer)
                pod = MentorPod.query.filter_by(member_id=current_user.id).first()
                if pod:
                    officer = db.session.get(User, pod.mentor_id)
                    if officer and officer.email:
                        send_email(
                            officer.email,
                            f'[DECA] Exam Upload: {current_user.username} ({conference})',
                            f'<p>{current_user.username} uploaded a paper exam for <strong>{conference}</strong>.</p>'
                            f'<p>Notes: {notes or "None"}</p>'
                            f'<p><a href="{result["secure_url"]}">View exam</a></p>'
                            f'<p>Please review and mark as credited on the DECA Tracker.</p>'
                        )

                db.session.commit()
                flash('Exam uploaded successfully. Your officer has been notified.', 'success')
            except Exception as e:
                db.session.rollback()
                flash(f'Upload failed: {str(e)}', 'danger')

            return redirect(url_for('exam_uploads'))

        # GET: show member's own uploads
        active_conf = get_active_conference()
        my_uploads = ExamUpload.query.filter_by(member_id=current_user.id).order_by(
            ExamUpload.uploaded_at.desc()).all()
        return render_template('exam_uploads.html',
            my_uploads=my_uploads,
            conference_order=CONFERENCE_ORDER,
            active_conf=active_conf,
        )

    else:  # officer
        # Show all pod member exam uploads
        pod_members = MentorPod.query.filter_by(mentor_id=current_user.id).all()
        pod_member_ids = [pm.member_id for pm in pod_members]
        uploads = ExamUpload.query.filter(
            ExamUpload.member_id.in_(pod_member_ids)
        ).order_by(ExamUpload.uploaded_at.desc()).all()
        return render_template('exam_uploads.html',
            uploads=uploads,
            conference_order=CONFERENCE_ORDER,
        )


@app.route('/exam_uploads/review/<int:upload_id>', methods=['POST'])
@login_required
def review_exam_upload(upload_id):
    """Officer marks an exam upload as reviewed and optionally credits it."""
    if current_user.role != 'officer':
        flash('Only officers can review exam uploads.', 'danger')
        return redirect(url_for('exam_uploads'))
    upload = ExamUpload.query.get_or_404(upload_id)
    action = request.form.get('action', 'review')  # 'review' or 'credit'

    upload.reviewed = True
    upload.reviewed_at = datetime.utcnow()
    upload.reviewer_id = current_user.id

    if action == 'credit':
        upload.credited = True
        # Decrement the member's exam commitment for the conference
        commitment = Commitment.query.filter_by(
            member_name=upload.member.username, event=upload.conference).first()
        if commitment and commitment.remaining_exam > 0:
            commitment.remaining_exam -= 1
            db.session.add(commitment)
        upload.commitment_id = commitment.id if commitment else None
        flash(f'Exam credited for {upload.member.username} ({upload.conference}).', 'success')
    else:
        flash(f'Exam marked as reviewed for {upload.member.username}.', 'success')

    db.session.add(upload)
    db.session.commit()
    return redirect(url_for('exam_uploads'))


# ── Admin routes ─────────────────────────────────────────────────────────────

def admin_required(f):
    """Decorator that restricts a route to admin users only."""
    from functools import wraps
    @wraps(f)
    def decorated(*args, **kwargs):
        if not current_user.is_authenticated or not current_user.is_admin:
            flash('Admin access required.', 'danger')
            return redirect(url_for('dashboard'))
        return f(*args, **kwargs)
    return decorated


@app.route('/admin')
@login_required
@admin_required
def admin_panel():
    users = User.query.order_by(User.role, User.username).all()
    stats = {
        'total_users': User.query.count(),
        'officers': User.query.filter_by(role='officer').count(),
        'members': User.query.filter_by(role='member').count(),
        'admins': User.query.filter_by(is_admin=True).count(),
        'pods': MentorPod.query.count(),
        'ah_records': AHAttendance.query.count(),
        'ws_records': WSAttendance.query.count(),
        'workshops': Workshop.query.count(),
        'commitments': Commitment.query.count(),
    }
    return render_template('admin.html', users=users, stats=stats)


@app.route('/admin/delete_user/<int:user_id>', methods=['POST'])
@login_required
@admin_required
def admin_delete_user(user_id):
    user = User.query.get_or_404(user_id)
    if user.id == current_user.id:
        flash('You cannot delete your own account.', 'danger')
        return redirect(url_for('admin_panel'))
    username = user.username
    AHAttendance.query.filter_by(user_id=user.id).delete()
    WSAttendance.query.filter_by(user_id=user.id).delete()
    MentorPod.query.filter(
        (MentorPod.member_id == user.id) | (MentorPod.mentor_id == user.id)
    ).delete()
    Commitment.query.filter_by(user_id=user.id).delete()
    ReminderLog.query.filter_by(user_id=user.id).delete()
    db.session.execute(
        workshop_signups.delete().where(workshop_signups.c.user_id == user.id)
    )
    db.session.delete(user)
    db.session.commit()
    flash(f'User "{username}" deleted successfully.', 'success')
    return redirect(url_for('admin_panel'))


@app.route('/admin/delete_test_users', methods=['POST'])
@login_required
@admin_required
def admin_delete_test_users():
    """Delete accounts that look like test accounts (Officer1, Member1, etc.)"""
    test_prefixes = ['officer', 'member', 'test']
    deleted = []
    for user in User.query.all():
        if user.id == current_user.id:
            continue
        lower = user.username.lower()
        if any(lower.startswith(p) and lower[len(p):].isdigit() for p in test_prefixes):
            AHAttendance.query.filter_by(user_id=user.id).delete()
            WSAttendance.query.filter_by(user_id=user.id).delete()
            MentorPod.query.filter(
                (MentorPod.member_id == user.id) | (MentorPod.mentor_id == user.id)
            ).delete()
            Commitment.query.filter_by(user_id=user.id).delete()
            ReminderLog.query.filter_by(user_id=user.id).delete()
            db.session.execute(
                workshop_signups.delete().where(workshop_signups.c.user_id == user.id)
            )
            deleted.append(user.username)
            db.session.delete(user)
    db.session.commit()
    if deleted:
        flash(f'Deleted {len(deleted)} test account(s): {", ".join(deleted)}', 'success')
    else:
        flash('No test accounts found.', 'info')
    return redirect(url_for('admin_panel'))


@app.route('/admin/toggle_admin/<int:user_id>', methods=['POST'])
@login_required
@admin_required
def admin_toggle_admin(user_id):
    user = User.query.get_or_404(user_id)
    if user.id == current_user.id:
        flash('You cannot change your own admin status.', 'danger')
        return redirect(url_for('admin_panel'))
    user.is_admin = not user.is_admin
    db.session.commit()
    status = 'granted' if user.is_admin else 'removed'
    flash(f'Admin access {status} for "{user.username}".', 'success')
    return redirect(url_for('admin_panel'))


@app.route('/admin/reset_password/<int:user_id>', methods=['POST'])
@login_required
@admin_required
def admin_reset_password(user_id):
    user = User.query.get_or_404(user_id)
    user.password = generate_password_hash('DECA2026!')
    user.must_change_password = True
    db.session.commit()
    flash(f'Password reset to DECA2026! for "{user.username}".', 'success')
    return redirect(url_for('admin_panel'))


@app.route('/admin/make_first_admin', methods=['GET', 'POST'])
def make_first_admin():
    """One-time bootstrap page to grant admin to first account.
    Only works if zero admins exist in the database."""
    if User.query.filter_by(is_admin=True).first():
        flash('An admin already exists. Use /admin to manage users.', 'info')
        return redirect(url_for('login'))
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '').strip()
        user = User.query.filter_by(username=username).first()
        if not user or not check_password_hash(user.password, password):
            flash('Incorrect username or password.', 'danger')
            return render_template('make_first_admin.html')
        user.is_admin = True
        db.session.commit()
        flash(f'"{username}" is now an admin. Please log in and go to /admin.', 'success')
        return redirect(url_for('login'))
    return render_template('make_first_admin.html')


@app.route('/admin/logs')
@login_required
@admin_required
def view_logs():
    logs = MDPAuditLog.query.order_by(MDPAuditLog.timestamp.desc()).all()
    return render_template('logs.html', logs=logs)


@app.route('/admin/toggle_competing/<int:user_id>', methods=['POST'])
@login_required
@admin_required
def admin_toggle_competing(user_id):
    user = User.query.get_or_404(user_id)
    user.is_competing = not user.is_competing
    log_mdp_action(
        current_user.id,
        'toggle_competing',
        'user',
        target_user_id=user.id,
        details=f'Marked {user.username} as ' + ('competing' if user.is_competing else 'non-competing'),
    )
    db.session.commit()
    flash(f'"{user.username}" marked as ' + ('competing.' if user.is_competing else 'non-competing.'), 'success')
    return redirect(url_for('admin_panel'))


@app.route('/admin/mentor_pods', methods=['GET', 'POST'])
@login_required
@admin_required
def mentor_pods():
    form = MentorPodForm()
    form.member_id.choices = [
        (user.id, user.username)
        for user in User.query.filter_by(role='member').order_by(User.username).all()
    ]
    form.mentor_id.choices = [
        (user.id, user.username)
        for user in User.query.filter(
            User.role == 'officer', User.is_admin.is_(False)
        ).order_by(User.username).all()
    ]
    if form.validate_on_submit():
        existing = MentorPod.query.filter_by(member_id=form.member_id.data).first()
        if existing:
            flash('That member already has a mentor-pod assignment.', 'warning')
            return redirect(url_for('mentor_pods'))
        pod = MentorPod(
            pod_number=form.pod_number.data,
            member_id=form.member_id.data,
            mentor_id=form.mentor_id.data,
            experience_level=form.experience_level.data,
            event=form.event.data.strip(),
            year_in_deca='',
        )
        db.session.add(pod)
        member = db.session.get(User, form.member_id.data)
        if member:
            member.is_competing = form.is_competing.data == 'yes'
            ensure_commitments(member)
        log_mdp_action(
            current_user.id, 'pod_add', 'pod', target_user_id=pod.member_id,
            details=f'Added to Pod {pod.pod_number}',
        )
        db.session.commit()
        flash('Mentor pod saved.', 'success')
        return redirect(url_for('mentor_pods'))
    pods = MentorPod.query.options(
        joinedload(MentorPod.mentor), joinedload(MentorPod.member)
    ).order_by(MentorPod.mentor_id, MentorPod.pod_number).all()
    grouped_pods = defaultdict(list)
    for pod in pods:
        grouped_pods[pod.mentor].append(pod)
    return render_template(
        'mentor_pods.html',
        form=form,
        pods=pods,
        grouped_pods=dict(grouped_pods),
        event_choices=EVENT_TABS,
    )


@app.route('/admin/mentor_pods/edit/<int:pod_id>', methods=['POST'])
@login_required
@admin_required
def edit_pod(pod_id):
    pod = MentorPod.query.get_or_404(pod_id)
    pod.pod_number = request.form.get('pod_number', pod.pod_number, type=int)
    pod.member_id = request.form.get('member_id', pod.member_id, type=int)
    pod.mentor_id = request.form.get('mentor_id', pod.mentor_id, type=int)
    pod.experience_level = request.form.get('experience_level', pod.experience_level)
    pod.year_in_deca = request.form.get('year_in_deca', pod.year_in_deca or '')
    pod.event = request.form.get('event', pod.event or '').strip()
    if 'is_competing' in request.form and pod.member:
        pod.member.is_competing = request.form.get('is_competing') == 'yes'
    log_mdp_action(
        current_user.id, 'pod_edit', 'pod', target_user_id=pod.member_id,
        details=f'Updated Pod {pod.pod_number}',
    )
    db.session.commit()
    flash('Pod updated.', 'success')
    return redirect(url_for('mentor_pods'))


@app.route('/admin/mentor_pods/delete/<int:pod_id>', methods=['POST'])
@login_required
@admin_required
def delete_pod(pod_id):
    pod = MentorPod.query.get_or_404(pod_id)
    member_id, pod_number = pod.member_id, pod.pod_number
    log_mdp_action(
        current_user.id, 'pod_delete', 'pod', target_user_id=member_id,
        details=f'Removed from Pod {pod_number}',
    )
    db.session.delete(pod)
    db.session.commit()
    flash('Pod deleted.', 'success')
    return redirect(url_for('mentor_pods'))


@app.route('/admin/mentor_pods/delete_group/<int:mentor_id>/<int:pod_number>', methods=['POST'])
@login_required
@admin_required
def delete_pod_group(mentor_id, pod_number):
    pods = MentorPod.query.filter_by(mentor_id=mentor_id, pod_number=pod_number).all()
    if not pods:
        flash('Pod not found.', 'warning')
        return redirect(url_for('mentor_pods'))
    for pod in pods:
        log_mdp_action(
            current_user.id, 'pod_delete', 'pod', target_user_id=pod.member_id,
            details=f'Removed Pod {pod.pod_number} (bulk pod delete)',
        )
        db.session.delete(pod)
    db.session.commit()
    flash(f'Pod #{pod_number} deleted.', 'success')
    return redirect(url_for('mentor_pods'))


@app.route('/mentee-progress')
@login_required
def mentee_progress_page():
    return redirect(url_for('admin_member_commitments'))


def _bool_from_form(value):
    if value is None:
        return None
    return str(value).strip().lower() in {'1', 'true', 'yes', 'y', 'on', 'checked'}


@app.route('/toggle_checklist_item', methods=['POST'])
@login_required
def toggle_checklist_item():
    if current_user.role != 'officer' and not current_user.is_admin:
        return jsonify({'success': False, 'message': 'Only officers/admins can update checklist items.'}), 403
    user_id = request.form.get('user_id', type=int)
    event = request.form.get('event', '').strip()
    item_name = request.form.get('item_name', '').strip()
    requested_completed = _bool_from_form(request.form.get('completed'))
    if not user_id or not event or not item_name:
        return jsonify({'success': False, 'message': 'Missing checklist item details.'}), 400
    event_items, _ = get_written_checklist_catalog()
    if item_name not in event_items.get(event, []):
        return jsonify({'success': False, 'message': 'That item is not part of this event checklist.'}), 400
    item = ChecklistItem.query.filter_by(
        user_id=user_id, event=event, item_name=item_name
    ).first()
    if item is None:
        item = ChecklistItem(
            user_id=user_id,
            event=event,
            item_name=item_name,
            completed=requested_completed if requested_completed is not None else True,
        )
        db.session.add(item)
    else:
        item.completed = (not item.completed) if requested_completed is None else requested_completed
    log_mdp_action(
        current_user.id,
        'checklist_update',
        'written_progress',
        target_user_id=user_id,
        details=f'{event} - {item_name}: ' + ('complete' if item.completed else 'incomplete'),
    )
    db.session.commit()
    return jsonify({'success': True, 'completed': bool(item.completed)})


@app.route('/member_commitments')
@login_required
def admin_member_commitments():
    if current_user.role != 'officer' and not current_user.is_admin:
        flash('Only officers/admins can view this.', 'danger')
        return redirect(url_for('dashboard'))
    members = _members_visible_to_current_user()
    rows = []
    for member in members:
        pod = MentorPod.query.filter_by(member_id=member.id).first()
        level = pod.experience_level if pod else 'N'
        event = pod.event if pod else None
        commitments = Commitment.query.filter_by(user_id=member.id).all()
        if not commitments:
            commitments = Commitment.query.filter_by(member_name=member.username).all()
        commitments_by_conf = {row.event: row for row in commitments}
        checklist_by_event = defaultdict(dict)
        for item in ChecklistItem.query.filter_by(user_id=member.id).all():
            checklist_by_event[item.event][item.item_name] = item.completed
        conferences = {}
        total_done = total_required = 0
        for conference in CONFERENCE_ORDER:
            commitment = commitments_by_conf.get(conference)
            if not commitment:
                conferences[conference] = None
                continue
            done = (
                commitment.required_roleplay - commitment.remaining_roleplay
                + commitment.required_written - commitment.remaining_written
                + commitment.required_exam - commitment.remaining_exam
            )
            required = (
                commitment.required_roleplay
                + commitment.required_written
                + commitment.required_exam
            )
            total_done += done
            total_required += required
            conferences[conference] = {
                'roleplay_done': commitment.required_roleplay - commitment.remaining_roleplay,
                'roleplay_req': commitment.required_roleplay,
                'written_done': commitment.required_written - commitment.remaining_written,
                'written_req': commitment.required_written,
                'exam_done': commitment.required_exam - commitment.remaining_exam,
                'exam_req': commitment.required_exam,
                'deadline': commitment.deadline,
                'grade': commitment.grade,
                'complete': (
                    commitment.remaining_roleplay
                    + commitment.remaining_written
                    + commitment.remaining_exam
                ) == 0,
                'checklist': {
                    name: checklist_by_event.get(conference, {}).get(name, False)
                    for name in CHECKLIST_ITEMS.get(conference, [])
                },
            }
        overall_pct = round(total_done / total_required * 100, 1) if total_required else 0
        incomplete = any(value is not None and not value['complete'] for value in conferences.values())
        rows.append({
            'member': member,
            'mentor_name': get_mentor_name(member),
            'level': level,
            'event': event,
            'status': get_commitment_status(member),
            'conferences': conferences,
            'overall_pct': overall_pct,
            'commitments_incomplete': incomplete,
        })
    total_members = len(rows)
    stats = {
        'total_members': total_members,
        'at_risk_count': sum(row['status'] == 'at_risk' for row in rows),
        'incomplete_count': sum(row['commitments_incomplete'] for row in rows),
        'avg_progress': round(sum(row['overall_pct'] for row in rows) / total_members, 1) if total_members else 0,
    }
    return render_template(
        'admin_member_commitments.html',
        rows=rows,
        stats=stats,
        checklist_items_by_conf=CHECKLIST_ITEMS,
    )


@app.route('/checklist_completion')
@login_required
def checklist_completion():
    if current_user.role != 'officer' and not current_user.is_admin:
        flash('Only officers/admins can view this.', 'danger')
        return redirect(url_for('dashboard'))
    members = _members_visible_to_current_user()
    event_items, event_deadlines = get_written_checklist_catalog()
    today = datetime.now(LOCAL_TZ).date()
    rows = []
    grouped_rows = defaultdict(list)
    member_ids = [member.id for member in members]
    pods_by_member = {}
    checklist_by_user_event = defaultdict(dict)
    commitments_by_user = defaultdict(list)

    if member_ids:
        for pod in MentorPod.query.options(joinedload(MentorPod.mentor)).filter(
            MentorPod.member_id.in_(member_ids)
        ).all():
            pods_by_member.setdefault(pod.member_id, pod)

        for item in ChecklistItem.query.filter(
            ChecklistItem.user_id.in_(member_ids)
        ).all():
            checklist_by_user_event[(item.user_id, item.event)][item.item_name] = bool(
                item.completed
            )

        for commitment in Commitment.query.filter(
            Commitment.user_id.in_(member_ids)
        ).all():
            commitments_by_user[commitment.user_id].append(commitment)

    for member in members:
        pod = pods_by_member.get(member.id)
        level = pod.experience_level if pod else 'N'
        event = (pod.event or '').strip() if pod else ''
        if not event or event not in event_items:
            continue

        item_names = event_items.get(event, [])
        imported_completion = checklist_by_user_event.get((member.id, event), {})
        completed = {
            name: imported_completion.get(name, False)
            for name in item_names
        }
        written = _written_status(
            item_names,
            completed,
            event_deadlines.get(event, {}),
            today=today,
        )
        conference = _conference_summary_for_user(
            member,
            today=today,
            commitments=commitments_by_user.get(member.id, []),
        )
        row = {
            'member': member,
            'mentor_name': (
                pod.mentor.username if pod and pod.mentor else 'Unassigned'
            ),
            'level': level,
            'event': event,
            'status': written['status'],
            'status_label': written['status_label'],
            'checklists': {event: completed},
            'missing_items': written['missing_items'],
            'overdue_items': written['overdue_items'],
            'deadline_safe': written['deadline_safe'],
            'grades': conference['grades'],
        }
        rows.append(row)
        grouped_rows[event].append(row)
    written_stats = {
        'total_members': len(rows),
        'complete_count': sum(row['status'] == 'complete' for row in rows),
        'needs_attention_count': sum(row['status'] == 'needs_attention' for row in rows),
        'overdue_count': sum(row['status'] == 'overdue' for row in rows),
        'deadline_safe_count': sum(row['deadline_safe'] for row in rows),
    }
    start_year = _written_academic_start_year(today=today)
    return render_template(
        'checklist.html',
        rows=rows,
        grouped_rows=dict(grouped_rows),
        event_items=event_items,
        event_deadlines=event_deadlines,
        written_stats=written_stats,
        report_date=today,
        written_academic_year_label=f"{start_year}-{str(start_year + 1)[-2:]}",
    )


scheduler = BackgroundScheduler()

if not scheduler.running:
    scheduler.add_job(process_workshop_reminders, 'interval', minutes=1)
    scheduler.start()
    print("REMINDER SCHEDULER STARTED")

if __name__ == '__main__':
    app.run(debug=True)
