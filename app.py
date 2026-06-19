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
from collections import defaultdict

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
    print(f"[DB] Using external database: {DATABASE_URL[:60]}...")
    app.config['SQLALCHEMY_DATABASE_URI'] = DATABASE_URL
    app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {
       'pool_pre_ping': True,
       'pool_recycle': 300,
       'pool_size': 5,
       'max_overflow': 10,
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
ACTIVITY_TYPES = ['Roleplay', 'Written Presentation', 'Exam']

EVENT_REQUIREMENTS = {
    "VCMC": {"roleplay":1, "written":1, "exam":1, "deadline":"2026-11-15"},
    "SVCDC": {"roleplay":2, "written":2, "exam":1, "deadline":"2027-01-08"},
    "SCDC": {"roleplay":2, "written":2, "exam":2, "deadline":"2027-02-23"}
}

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

class MentorPodEditLog(db.Model):
    id = db.Column(db.Integer, primary_key=True)

    actor_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    member_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)

    action = db.Column(db.String(50), nullable=False)  # "add", "edit", "delete"
    details = db.Column(db.String(255))

    timestamp = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    actor = db.relationship('User', foreign_keys=[actor_id])
    member = db.relationship('User', foreign_keys=[member_id])


class MDPAuditLog(db.Model):
    id = db.Column(db.Integer, primary_key=True)

    actor_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    target_user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True)

    action = db.Column(db.String(50), nullable=False)
    category = db.Column(db.String(50), nullable=False)  # commitment/workshop/pod/attendance

    details = db.Column(db.String(255))
    timestamp = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    actor = db.relationship('User', foreign_keys=[actor_id])
    target_user = db.relationship('User', foreign_keys=[target_user_id])


@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, int(user_id))

# ── Attendance helper ─────────────────────────────────────────────────────────

def get_attendance_stats(user):
    """Return AH rate, WS rate, and at-risk flag for a member."""
    ah_records = AHAttendance.query.filter_by(user_id=user.id).all()
    ws_records = WSAttendance.query.filter_by(user_id=user.id).all()

    total_ah = len(ah_records)
    total_ws = len(ws_records)

    ah_sum = sum(r.value for r in ah_records)
    ws_sum = sum(r.value for r in ws_records)

    ah_rate = round((ah_sum / total_ah) * 100, 1) if total_ah > 0 else 0.0
    ws_rate = round((ws_sum / total_ws) * 100, 1) if total_ws > 0 else 0.0

    # Get experience level from pod
    pod = MentorPod.query.filter_by(member_id=user.id).first()
    level = pod.experience_level if pod else 'N'
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

def commitment_progress(commitment):
    total = (
        commitment.required_roleplay +
        commitment.required_written +
        commitment.required_exam
    )

    remaining = (
        commitment.remaining_roleplay +
        commitment.remaining_written +
        commitment.remaining_exam
    )

    done = total - remaining
    return round((done / total) * 100, 1) if total else 0

# ── Forms ─────────────────────────────────────────────────────────────────────

class RegisterForm(FlaskForm):
    username = StringField('Username', [DataRequired(), Length(min=3)])
    password = PasswordField('Password', [DataRequired(), Length(min=6)])
    role = SelectField('Role', choices=[('', 'Select your role'), ('officer', 'Officer'), ('member', 'Member'),('admin','Admin')], default='')
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

class MentorPodForm(FlaskForm):
    pod_number = IntegerField('Pod Number', validators=[DataRequired()])

    member_id = SelectField(
        'Member',
        coerce=int,
        validators=[DataRequired()]
    )

    mentor_id = SelectField(
        'Mentor',
        coerce=int,
        validators=[DataRequired()]
    )

    experience_level = SelectField(
        'Level',
        choices=[
            ('N', 'Novice'),
            ('E', 'Experienced')
        ]
    )

    submit = SubmitField('Save')

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

def log_pod_edit(actor_id, member_id, action, details=""):
    return MentorPodEditLog(
        actor_id=actor_id,
        member_id=member_id,
        action=action,
        details=details
    )

def log_mdp_action(actor_id, action, category, target_user_id=None, details=""):
    log = MDPAuditLog(
        actor_id=actor_id,
        target_user_id=target_user_id,
        action=action,
        category=category,
        details=details
    )
    db.session.add(log)

# ── Routes ────────────────────────────────────────────────────────────────────

# REMOVE THIS LINE (IMPORTANT - causes crash)
# from app import workshop_signups


# --- keep your existing imports ---
# (everything else stays the same)

# workshop_signups must be defined BEFORE any usage (keep where it already is in your file)
# DO NOT IMPORT IT


# =========================
# FIXED DASHBOARD ROUTE
# =========================
@app.route('/')
@login_required
def dashboard():

    overall_mentee_progress = 0
    mentee_progress = []
    progress_summary = None
    attendance_summary = None
    assigned_workshops = []
    workshop_attendance_data = []
    workshops = []
    created_workshops = []
    attendance_locked_ids = set()
    ah_ws_data = []
    member_stats = None
    mentees_workshops = {}

    # ALWAYS define this so template never breaks
    mentee_names = set()

    if current_user.role == 'officer' or current_user.is_admin:

        assigned_workshops = Workshop.query.filter_by(
            officer_id=current_user.id
        ).options(joinedload(Workshop.officer)).order_by(Workshop.time).all()

        workshops = assigned_workshops

        attendance_locked_ids = {
            row.workshop_id for row in AttendanceSubmission.query.filter_by(
                officer_id=current_user.id
            ).all()
        }

        pod_members = MentorPod.query.filter_by(
            mentor_id=current_user.id
        ).all()

        pod_member_users = [
            db.session.get(User, pm.member_id)
            for pm in pod_members
        ]
        pod_member_users = [u for u in pod_member_users if u]

        # FIX: rename variable so we don't overwrite anything
        user_commitments = []

        for member in pod_member_users:
            user_commitments = Commitment.query.filter_by(user_id=member.id).all()

            avg = (
                sum(commitment_progress(c) for c in user_commitments) / len(user_commitments)
                if user_commitments else 0
            )

            mentee_progress.append({
                'member': member.username,
                'progress': round(avg, 1)
            })

        overall_mentee_progress = (
            sum(m['progress'] for m in mentee_progress) / len(mentee_progress)
            if mentee_progress else 0
        )

        # fallback safety
        if not pod_member_users:
            member_ids = set()

            for ws in assigned_workshops:
                for m in ws.signups:
                    member_ids.add(m.id)

            pod_member_users = [
                db.session.get(User, uid)
                for uid in member_ids
            ]
            pod_member_users = [u for u in pod_member_users if u]

        # attendance data
        for member in sorted(pod_member_users, key=lambda u: u.username.lower()):

            actual_attended = db.session.query(workshop_signups).filter_by(
                user_id=member.id,
                attended=True
            ).join(Workshop).filter(
                Workshop.officer_id == current_user.id
            ).count()

            ga = GeneralAttendance.query.filter_by(
                officer_id=current_user.id,
                member_name=member.username
            ).first()

            manual_count = ga.manual_count if ga else 0

            workshop_attendance_data.append({
                'member_name': member.username,
                'total_attended': actual_attended + manual_count,
                'manual_count': manual_count,
                'actual_attended': actual_attended
            })

        # AH/WS stats
        for member in sorted(pod_member_users, key=lambda u: u.username.lower()):
            stats = get_attendance_stats(member)
            pod = MentorPod.query.filter_by(member_id=member.id).first()

            ah_ws_data.append({
                'member': member,
                'pod_number': pod.pod_number if pod else '?',
                'level': stats['level'],
                'ah_rate': stats['ah_rate'],
                'ws_rate': stats['ws_rate'],
                'at_risk': stats['at_risk'],
                'ah_sum': stats['ah_sum'],
                'ah_total': stats['ah_total'],
                'ws_sum': stats['ws_sum'],
                'ws_total': stats['ws_total'],
                'ws_threshold_pct': stats['ws_threshold_pct'],
            })

        # FIX: define mentee_names safely
        mentee_names = {c.member_name for c in Commitment.query.filter_by(user_id=current_user.id).all()}

    else:
        # MEMBER VIEW

        commitments = Commitment.query.filter_by(
            member_name=current_user.username
        ).all()

        workshops = current_user.workshops.options(
            joinedload(Workshop.officer),
            joinedload(Workshop.creator)
        ).order_by(Workshop.time).all()

        created_workshops = Workshop.query.filter_by(
            creator_id=current_user.id
        ).options(joinedload(Workshop.officer)).order_by(Workshop.time).all()

        if commitments:
            c = commitments[0]

            progress_summary = {
                'roleplay': f"{c.required_roleplay - c.remaining_roleplay}/{c.required_roleplay}",
                'written': f"{c.required_written - c.remaining_written}/{c.required_written}",
                'exam': f"{c.required_exam - c.remaining_exam}/{c.required_exam}",
                'deadline': c.deadline.strftime('%Y-%m-%d') if c.deadline else 'N/A',
                'event': c.event
            }

        member_stats = get_attendance_stats(current_user)

    # SAFE: never crash here
    signed_times = []

    if current_user.role == 'member':
        my_signups = current_user.workshops.all()
        signed_times = [
            (w.time, w.time + timedelta(minutes=20))
            for w in my_signups
        ]

    return render_template(
        'dashboard.html',
        commitments=commitments if 'commitments' in locals() else [],
        progress_summary=progress_summary,
        attendance_summary=attendance_summary,
        assigned_workshops=assigned_workshops,
        workshop_attendance_data=workshop_attendance_data,
        workshops=workshops,
        created_workshops=created_workshops,
        attendance_locked_ids=attendance_locked_ids,
        ah_ws_data=ah_ws_data,
        member_stats=member_stats,
        mentee_progress=mentee_progress,
        overall_mentee_progress=overall_mentee_progress,
        signed_times=signed_times,
        user=current_user,
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
        user = User(username=form.username.data.strip(), password=hashed_pw, role=form.role.data, is_admin=(form.role.data == 'admin'))
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
            
            print("===== LOGIN DEBUG =====")
            print("Username:", user.username)
            print("Role:", user.role)
            print("is_admin:", user.is_admin)
            print("=======================")
            
            login_user(user)

            print("current_user role after login:", user.role)
            
            # NEW: redirect to change password if flagged
            if user.must_change_password:
                return redirect(url_for('change_password'))
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

@app.route('/add_commitment', methods=['GET', 'POST'])
@login_required
def add_commitment():
    if current_user.role != 'officer':
        flash('Only officers can add commitments.', 'danger')
        return redirect(url_for('dashboard'))
    form = CommitmentForm()
    if form.validate_on_submit():
        member_name = form.member_name.data.strip()

        member = User.query.filter_by(
            username=member_name
        ).first()

        event = form.event.data
        rule = EVENT_REQUIREMENTS[event]
        commit = Commitment(
            member_name=member_name,
            event=event,
            required_roleplay=rule["roleplay"],
            required_written=rule["written"],
            required_exam=rule["exam"],
            remaining_roleplay=rule["roleplay"],
            remaining_written=rule["written"],
            remaining_exam=rule["exam"],
            deadline=datetime.strptime(rule["deadline"], "%Y-%m-%d").date(),
            user_id=member.id if member else None)
        db.session.add(commit)
        db.session.commit()

        log_mdp_action(
            actor_id=current_user.id,
            action="add",
            category="commitment",
            target_user_id=current_user.id,
            details=f"Added commitment for {member_name}"
        )
        
        flash('Commitment added.', 'success')
        return redirect(url_for('add_commitment'))
    commitments = Commitment.query.filter_by(user_id=current_user.id).order_by(Commitment.deadline).all()
    return render_template('add_commitment.html', form=form, commitments=commitments)

@app.route('/delete_commitment/<int:commitment_id>', methods=['POST'])
@login_required
def delete_commitment(commitment_id):
    commitment = Commitment.query.get_or_404(commitment_id)
    if current_user.role != 'officer' or commitment.user_id != current_user.id:
        flash('You are not allowed to delete this commitment.', 'danger')
        return redirect(url_for('dashboard'))
    db.session.delete(commitment)

    log_mdp_action(
        actor_id=current_user.id,
        action="delete",
        category="commitment",
        target_user_id=commitment.user_id,
        details=f"Deleted commitment for {commitment.user_id}"
    )
    
    db.session.commit()
    flash('Commitment deleted.', 'success')
    return redirect(url_for('add_commitment'))

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

            log_mdp_action(
                actor_id=current_user.id,
                action="add",
                category="workshop",
                target_user_id=current_user.id,
                details=f"Created workshop {ws.activity_type} on {ws.time}"
            )
            
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

        log_mdp_action(
            actor_id=current_user.id,
            action="edit",
            category="workshop",
            target_user_id=workshop.creator_id,
            details=f"Edited workshop {workshop.id} ({workshop.activity_type})"
        )
        
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

    log_mdp_action(
        actor_id=current_user.id,
        action="signup",
        category="workshop",
        target_user_id=workshop.officer_id,
        details=f"Signed up for workshop {workshop.id}"
    )
    
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

        log_mdp_action(
            actor_id=current_user.id,
            action="cancel",
            category="workshop",
            target_user_id=workshop.officer_id,
            details=f"Cancelled workshop {workshop.id}"
        )

        
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

        log_mdp_action(
            actor_id=current_user.id,
            action="attendance_submit",
            category="attendance",
            target_user_id=current_user.id,
            details=f"Submitted attendance for workshop {workshop_id}"
        )
        
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

@app.route('/reports')
@login_required
def reports():
    if current_user.role != 'officer':
        flash('Only officers can view reports.', 'danger')
        return redirect(url_for('dashboard'))
    active_tab = request.args.get('tab', 'attendance')
    officer_workshops = Workshop.query.filter_by(officer_id=current_user.id).options(joinedload(Workshop.officer), joinedload(Workshop.creator)).order_by(Workshop.time).all()

    member_summary = {}
    for ws in officer_workshops:
        attended_ids = {row[0] for row in db.session.query(workshop_signups.c.user_id).filter_by(workshop_id=ws.id, attended=True).all()}
        for member in ws.signups:
            username = member.username
            if username not in member_summary:
                member_summary[username] = {'signups': 0, 'attended': 0, 'manual': 0}
            member_summary[username]['signups'] += 1
            if member.id in attended_ids:
                member_summary[username]['attended'] += 1
    gas = GeneralAttendance.query.filter_by(officer_id=current_user.id).all()
    for ga in gas:
        username = ga.member_name
        if username in member_summary:
            member_summary[username]['manual'] += ga.manual_count
        else:
            member_summary[username] = {'signups': 0, 'attended': 0, 'manual': ga.manual_count}

    reports_data = []
    for username, stats in member_summary.items():
        total_attended = stats['attended'] + stats['manual']
        rate = (total_attended / 18 * 100) if 18 > 0 else 0
        reports_data.append({'member': username, 'signups_count': stats['signups'], 'attended_count': total_attended, 'attendance_rate': round(rate, 1)})
    reports_data.sort(key=lambda x: x['member'].lower())

    attendance_locked_ids = {row.workshop_id for row in AttendanceSubmission.query.filter_by(officer_id=current_user.id).all()}
    calendar_groups = {}
    for ws in officer_workshops:
        local_start = utc_to_local(ws.time)
        local_end = local_start + timedelta(minutes=20)
        day = local_start.strftime('%Y-%m-%d')
        time_range = f"{local_start.strftime('%I:%M').lstrip('0')} - {local_end.strftime('%I:%M').lstrip('0')} {local_end.strftime('%p').lower()}"
        signup_names = ', '.join(sorted([u.username for u in ws.signups], key=str.lower)) or 'None'
        calendar_groups.setdefault(day, []).append({
            'workshop': ws,
            'time_range': time_range,
            'signup_names': signup_names
        })
    for day in calendar_groups:
        calendar_groups[day].sort(key=lambda item: item['workshop'].time)

    return render_template('reports.html', reports_data=reports_data, active_tab=active_tab,
                           calendar_groups=calendar_groups, attendance_locked_ids=attendance_locked_ids)


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


@app.route('/admin/logs')
@login_required
@admin_required
def view_logs():
    logs = MDPAuditLog.query.order_by(MDPAuditLog.timestamp.desc()).all()
    return render_template('logs.html', logs=logs)


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

@app.route('/admin/mentor_pods', methods=['GET', 'POST'])
@login_required
@admin_required
def mentor_pods():
    form = MentorPodForm()

    form.member_id.choices = [
        (u.id, u.username)
        for u in User.query.filter_by(role='member').all()
    ]
    form.mentor_id.choices = [
        (u.id, u.username)
        for u in User.query.filter_by(role='officer').all()
    ]

    if form.validate_on_submit():
        pod = MentorPod(
            pod_number=form.pod_number.data,
            member_id=form.member_id.data,
            mentor_id=form.mentor_id.data,
            experience_level=form.experience_level.data,
            year_in_deca=""
        )
        db.session.add(pod)

        log_mdp_action(
            actor_id=current_user.id,
            action="pod_add",
            category="pod",
            target_user_id=pod.member_id,
            details=f"Added to Pod {pod.pod_number}"
        )
        
        db.session.commit()

        
        
        flash("Mentor pod saved", "success")
        return redirect(url_for('mentor_pods'))

    pods = MentorPod.query.all()

    grouped_pods = defaultdict(list)

    for pod in pods:
        grouped_pods[pod.mentor].append(pod)

    grouped_pods = dict(grouped_pods)

    return render_template(
        'mentor_pods.html',
        form=form,
        pods=pods,
        grouped_pods=grouped_pods
    )

@app.route('/admin/mentor_pods/edit/<int:pod_id>', methods=['POST'])
@login_required
@admin_required
def edit_pod(pod_id):
    pod = MentorPod.query.get_or_404(pod_id)

    pod.pod_number = request.form['pod_number']
    pod.member_id = request.form['member_id']
    pod.experience_level = request.form['experience_level']
    pod.year_in_deca = request.form['year_in_deca']

    log_mdp_action(
        actor_id=current_user.id,
        action="pod_edit",
        category="pod",
        target_user_id=pod.member_id,
        details=f"Updated Pod {pod.pod_number}"
    )

    db.session.commit()
    flash("Pod updated", "success")
    return redirect(url_for('mentor_pods'))


@app.route('/admin/mentor_pods/delete/<int:pod_id>', methods=['POST'])
@login_required
@admin_required
def delete_pod(pod_id):
    pod = MentorPod.query.get_or_404(pod_id)

    log_mdp_action(
        actor_id=current_user.id,
        action="pod_delete",
        category="pod",
        target_user_id=pod.member_id,
        details=f"Removed from Pod {pod.pod_number}"
    )
    
    db.session.delete(pod)
    db.session.commit()
    flash("Pod deleted", "success")
    return redirect(url_for('mentor_pods'))

@app.route('/mentee-progress')
@login_required
def mentee_progress_page():
    if current_user.role != 'officer' and not current_user.is_admin:
        flash('Only officers can view mentee progress.', 'danger')
        return redirect(url_for('dashboard'))

    # build mentee progress here
    mentee_progress = []
    if current_user.is_admin:
        pod_members = MentorPod.query.all()
    else:
        pod_members = MentorPod.query.filter_by(
            mentor_id=current_user.id
        ).all()

    for p in pod_members[:10]:
            print("POD:", p.id, "mentor_id:", p.mentor_id, "member_id:", p.member_id)

    pod_member_users = [db.session.get(User, pm.member_id) for pm in pod_members]
    pod_member_users = [u for u in pod_member_users if u]

    for member in pod_member_users:

        print("LOOKING FOR:",member.username)
            
        # LOOK UP COMMITMENTS BY USER ID
        commitments = Commitment.query.filter_by(
            user_id=member.id
        ).all()

        print("FOUND:",len(commitments))

        if commitments:
            avg = (
                sum(commitment_progress(c) for c in commitments)
                / len(commitments)
            )
        else:
            avg = 0

        mentee_progress.append({
            'member': member.username,
            'progress': round(avg, 1)
        })


    overall_mentee_progress = (
        sum(m['progress'] for m in mentee_progress) / len(mentee_progress)
        if mentee_progress else 0
    )

    for c in Commitment.query.limit(20).all():
        print(
            c.member_name,
            "required:",
            c.required_roleplay,
            c.required_written,
            c.required_exam,
            "remaining:",
            c.remaining_roleplay,
            c.remaining_written,
            c.remaining_exam,
            "progress:",
            commitment_progress(c)
        )

    print("\nCOMMITMENT USER IDS:")
    for c in Commitment.query.limit(20).all():
        print(
            "member_name =", c.member_name,
            "| user_id =", c.user_id
        )
    return render_template(
        'mentee_progress.html',
        mentee_progress=mentee_progress,
        overall_mentee_progress=overall_mentee_progress
    )

scheduler = BackgroundScheduler()

if not scheduler.running:
    scheduler.add_job(process_workshop_reminders, 'interval', minutes=1)
    scheduler.start()
    print("REMINDER SCHEDULER STARTED")

if __name__ == '__main__':
    app.run(debug=True)
