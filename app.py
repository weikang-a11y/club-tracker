try:
    import truststore
except ImportError:
    truststore = None
else:
    truststore.inject_into_ssl()

from flask import Flask, render_template, request, redirect, url_for, flash, jsonify, Response, abort, session
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
from werkzeug.security import generate_password_hash, check_password_hash
from flask_wtf import FlaskForm
from flask_wtf.file import FileAllowed, FileField, FileRequired
from wtforms import StringField, PasswordField, DateField, SelectField, IntegerField, SubmitField
from wtforms.validators import DataRequired, Length, NumberRange, ValidationError
from datetime import datetime, timedelta
from sqlalchemy.orm import joinedload
from sqlalchemy import text as sql_text
from apscheduler.schedulers.background import BackgroundScheduler
from dotenv import load_dotenv
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from sqlalchemy.exc import IntegrityError
import hashlib
import hmac
import csv
import io
import os
import posixpath
import re
import requests
import tempfile
import zipfile
import xml.etree.ElementTree as ET
from collections import defaultdict

load_dotenv()

app = Flask(__name__)

# Set SECRET_KEY in .env in production. The fallback keeps existing local
# friend2 installations bootable while they migrate their environment.
app.config['SECRET_KEY'] = os.getenv(
    'SECRET_KEY',
    'super-secret-key-change-me-98765',
)

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
FROM_NAME = os.getenv("FROM_NAME", "DECA Tracker")
BREVO_API_KEY = os.getenv("BREVO_API_KEY")
BREVO_EMAIL_ENDPOINT = "https://api.brevo.com/v3/smtp/email"

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
# Novice:     VCMC(RP:1,W:1,E:2), SVCDC(RP:1,W:1,E:2), SCDC(RP:2,W:1,E:2)
# Experienced:VCMC(N/A),          SVCDC(RP:1,W:1,E:2), SCDC(RP:1,W:1,E:1)
# RP/Exam counts include both in-person and ICPrep (members choose which to use for ICPrep quota)
EVENT_REQUIREMENTS = {
    "N": {  # Novice
        "VCMC":  {"roleplay": 1, "written": 1, "exam": 2, "deadline": "2025-11-15"},
        "SVCDC": {"roleplay": 1, "written": 1, "exam": 2, "deadline": "2026-01-08"},
        "SCDC":  {"roleplay": 2, "written": 1, "exam": 2, "deadline": "2026-02-23"},
    },
    "E": {  # Experienced
        "VCMC":  {"roleplay": 0, "written": 0, "exam": 0, "deadline": "2025-11-15"},
        "SVCDC": {"roleplay": 1, "written": 1, "exam": 2, "deadline": "2026-01-08"},
        "SCDC":  {"roleplay": 1, "written": 1, "exam": 1, "deadline": "2026-02-23"},
    },
}

CONFERENCE_ORDER = ["VCMC", "SVCDC", "SCDC"]
CONFERENCE_DEADLINES = {
    "VCMC":  "2025-11-15",
    "SVCDC": "2026-01-08",
    "SCDC":  "2026-02-23",
}

# Competitive-event labels used by mentor-pod administration.
EVENT_TABS = ['BOR', 'EIP', 'EFB', 'EIB', 'ESB', 'IBP', 'IMC', 'PM', 'PSE', 'NA']

MDP_TRACKING_FILE = os.getenv(
    'MDP_TRACKING_FILE',
    os.path.join(os.path.dirname(__file__), 'FINAL MDP Deadline Tracking.xlsx'),
)
MDP_IMPORT_KEY_PREFIX = 'mdp-tracking-xlsx-positional-v3-canonical-members-'


def is_mdp_upload_enabled():
    """Return True only while the temporary admin import page is enabled."""
    return os.getenv('ENABLE_MDP_UPLOAD', '').strip().lower() in {
        '1', 'true', 'yes', 'on',
    }

# Spreadsheet names that intentionally differ from account usernames. Joey's
# source row is labelled "Joey Zhu" but the existing account is joey.shu.
SPREADSHEET_ACCOUNT_OVERRIDES = {
    'amber chang': ('ambery.chang',),
    # The live account uses the workbook/email spelling. Do not fall back to
    # the old ``avaneesh.nagare`` typo: if both accounts exist, that would
    # update the wrong user's commitments and leave avaneesh.nangare stale.
    'avaneesh nangare': ('avaneesh.nangare',),
    'chun ka yu': ('chunka.yu',),
    'elizabeth huang': ('lizzie.huang', 'elizabeth.huang'),
    'joey zhu': ('joey.shu', 'joey.zhu'),
    'lincoln tran': ('lincolnjacob.tran',),
    'lucas wang': ('lucasj.wang',),
    'ruhaan parandekar': ('r.parandekar',),
    'sidharth swaminathan': ('s.swaminathan',),
    'srinivasan satagopan': ('sk.satagopan',),
    'varshini subramanian': ('v.subramanian',),
    'yen-nhi tran': ('yennhi.tran',),
}

# Fallback catalog. Imported ChecklistRequirement rows drive Written Progress.
CHECKLIST_ITEMS = {
    "VCMC": ["Exec Summary", "Registration Form"],
    "SVCDC": ["Exec Summary", "Written Statement", "Registration Form"],
    "SCDC": ["Exec Summary", "Written Statement", "Registration Form", "Judge Sign-up"],
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

# Validated from "2026-27 Officer List.xlsx". Position is intentionally omitted
# because application permissions are driven only by Account Access.
OFFICER_ROSTER_2026_27 = [
    ("Mr. Shimada", "Admin"),
    ("Dr. Stuart", "Admin"),
    ("Mrs. Parayno", "Admin"),
    ("Ms. Chicas", "Admin"),
    ("Ms. Wang", "Admin"),
    ("Hanna Li", "Admin, Officer"),
    ("Chloe Ding", "Admin, Officer"),
    ("Arissa Cao", "Admin, Officer"),
    ("Saron Amdeberhan", "Admin, Officer"),
    ("Revathi Mekkoth", "Officer"),
    ("Crystal Chen", "Officer"),
    ("Philina Chen", "Officer, Admin"),
    ("Natalie Zhang", "Officer"),
    ("Melody Leong", "Officer"),
    ("Aryahi Sharma", "Officer"),
    ("Olivia Kang", "Officer"),
    ("Zihan Liu", "Officer"),
    ("Aaron Vu", "Admin, Officer"),
    ("Thatcher Kim", "Admin, Officer"),
    ("Mia Tran", "Admin, Officer"),
    ("Yen-Nhi Tran", "Admin, Officer"),
    ("Eva Gu", "Officer"),
    ("Evelyn Bai", "Officer"),
    ("Allen Tu", "Officer"),
    ("Jessica Ma", "Officer"),
    ("Neela Koneru", "Officer"),
    ("Sophie Yu", "Officer"),
    ("Anay Kalchuri", "Officer"),
    ("Lizzie Huang", "Officer"),
    ("Ayden Wang", "Officer"),
    ("Sophie Ji", "Officer"),
    ("Sarah Xu", "Officer"),
    ("Jason Huang", "Officer"),
    ("Purab Shah", "Officer"),
    ("Armaan Arya", "Officer"),
    ("Audrey Sansone", "Officer"),
    ("Isabella Yu", "Officer"),
    ("Riya Khattri", "Officer"),
    ("Zubin Lakhia", "Officer"),
]

# Shared temporary passwords requested for the roster import. Railway variables
# can override these values without changing source code. Every imported user is
# required to choose an individual password on first login.
OFFICER_IMPORT_ADMIN_PASSWORD = os.getenv(
    "OFFICER_IMPORT_ADMIN_PASSWORD", "Admin2627!"
)
OFFICER_IMPORT_OFFICER_PASSWORD = os.getenv(
    "OFFICER_IMPORT_OFFICER_PASSWORD", "Officer2627!"
)
OFFICER_IMPORT_KEY = "2026-27-officer-roster-v3-advisors-and-additions"

# This migration updates already-imported databases without rerunning the full
# roster import (which would reset every officer's temporary password).
REMOVED_ADMIN_ACCESS = ("Aryahi Sharma", "Olivia Kang", "Zihan Liu")
REMOVED_ADMIN_ACCESS_KEY = "2026-27-remove-admin-access-aryahi-olivia-zihan-v1"

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
    # True only when this account is entitled to use officer functionality.
    # This is separate from role because legacy admin-only accounts are stored
    # with role='officer' plus is_admin=True.
    has_officer_access = db.Column(db.Boolean, default=False, nullable=False)
    is_competing = db.Column(db.Boolean, default=True)

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
    grade = db.Column(db.String(10))
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
    event = db.Column(db.String(20))


class MentorPodEditLog(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    actor_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    member_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    action = db.Column(db.String(50), nullable=False)
    details = db.Column(db.String(255))
    timestamp = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    actor = db.relationship('User', foreign_keys=[actor_id])
    member = db.relationship('User', foreign_keys=[member_id])


class MDPAuditLog(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    actor_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    target_user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True)
    action = db.Column(db.String(50), nullable=False)
    category = db.Column(db.String(50), nullable=False)
    details = db.Column(db.String(255))
    timestamp = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    actor = db.relationship('User', foreign_keys=[actor_id])
    target_user = db.relationship('User', foreign_keys=[target_user_id])


class ChecklistItem(db.Model):
    """Per-member written-progress item."""
    __tablename__ = 'checklist_item'
    __table_args__ = (
        db.UniqueConstraint('user_id', 'event', 'item_name', name='uq_checklist_item'),
    )
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    event = db.Column(db.String(20), nullable=False)
    item_name = db.Column(db.String(50), nullable=False)
    completed = db.Column(db.Boolean, default=False)
    user = db.relationship('User', backref='checklist_items')


class ChecklistRequirement(db.Model):
    __tablename__ = 'checklist_requirement'
    id = db.Column(db.Integer, primary_key=True)
    event = db.Column(db.String(50), nullable=False)
    item_name = db.Column(db.String(100), nullable=False)
    deadline = db.Column(db.String(20))


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


class DataMigration(db.Model):
    """Records one-time data imports so redeploys do not reset accounts again."""
    __tablename__ = 'data_migration'
    key = db.Column(db.String(100), primary_key=True)
    applied_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    details = db.Column(db.String(255))


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


VIEW_MODE_SESSION_KEY = 'deca_account_view'


def can_switch_account_view(user=None):
    """Return whether the account genuinely has both admin and officer access."""
    user = user or current_user
    return bool(
        user.is_authenticated
        and user.is_admin
        and user.role == 'officer'
        and user.has_officer_access
    )


def get_account_view():
    """Resolve the current UI/permission scope for this signed-in account."""
    if not current_user.is_authenticated:
        return None
    if can_switch_account_view():
        selected = session.get(VIEW_MODE_SESSION_KEY, 'admin')
        return selected if selected in {'admin', 'officer'} else 'admin'
    if current_user.is_admin:
        return 'admin'
    return current_user.role


def is_admin_view():
    return get_account_view() == 'admin'


def is_officer_view():
    return get_account_view() == 'officer'


@app.context_processor
def inject_notification_defaults():
    """Inject shared navigation state into every template."""
    return {
        'notifications': [],
        'unread_count': 0,
        'active_view': get_account_view(),
        'can_switch_view': can_switch_account_view(),
    }


def _account_name_key(value):
    """Match names case-, whitespace-, and punctuation-insensitively."""
    return re.sub(r'[^a-z0-9]', '', (value or '').lower())


def _canonical_officer_username(full_name):
    """Convert a roster name to first.last, or an advisor's lowercase surname."""
    parts = re.findall(r'[a-z0-9]+(?:-[a-z0-9]+)*', (full_name or '').lower())
    if len(parts) >= 2 and parts[0] in {'mr', 'ms', 'mrs', 'dr'}:
        return parts[-1]
    if len(parts) < 2:
        raise ValueError(f'Officer name needs a first and last name: {full_name!r}')
    return f'{parts[0]}.{parts[-1]}'


def _delete_duplicate_member_account(member):
    """Delete a roster officer's duplicate member account and member data."""
    if member.role != 'member':
        raise ValueError(f'Refusing to delete non-member account: {member.username}')

    if Workshop.query.filter(
        (Workshop.officer_id == member.id) | (Workshop.creator_id == member.id)
    ).first():
        raise RuntimeError(
            f'Duplicate member {member.username} owns a workshop; '
            'manual review is required before deletion.'
        )

    AHAttendance.query.filter_by(user_id=member.id).delete()
    WSAttendance.query.filter_by(user_id=member.id).delete()
    MentorPod.query.filter(
        (MentorPod.member_id == member.id) | (MentorPod.mentor_id == member.id)
    ).delete(synchronize_session=False)
    Commitment.query.filter_by(user_id=member.id).delete()
    ReminderLog.query.filter_by(user_id=member.id).delete()
    ChecklistItem.query.filter_by(user_id=member.id).delete()
    AnnualICPrepTracker.query.filter_by(member_id=member.id).delete()
    ExamUpload.query.filter_by(member_id=member.id).delete()
    ExamUpload.query.filter_by(reviewer_id=member.id).update(
        {'reviewer_id': None}, synchronize_session=False
    )
    ICPrepWebhookLog.query.filter_by(member_id=member.id).delete()
    PracticeLog.query.filter(
        (PracticeLog.member_id == member.id) | (PracticeLog.officer_id == member.id)
    ).delete(synchronize_session=False)
    PracticeSession.query.filter_by(officer_id=member.id).delete()
    PracticeSession.query.filter_by(member_id=member.id).update(
        {'member_id': None}, synchronize_session=False
    )
    MentorPodEditLog.query.filter(
        (MentorPodEditLog.member_id == member.id)
        | (MentorPodEditLog.actor_id == member.id)
    ).delete(synchronize_session=False)
    MDPAuditLog.query.filter(
        (MDPAuditLog.target_user_id == member.id)
        | (MDPAuditLog.actor_id == member.id)
    ).delete(synchronize_session=False)
    AttendanceSubmission.query.filter_by(officer_id=member.id).delete()
    GeneralAttendance.query.filter(
        (GeneralAttendance.officer_id == member.id)
        | (
            db.func.lower(GeneralAttendance.member_name)
            == member.username.strip().lower()
        )
    ).delete(synchronize_session=False)
    db.session.execute(
        workshop_signups.delete().where(workshop_signups.c.user_id == member.id)
    )
    db.session.delete(member)


def import_2026_27_officer_roster():
    """Canonicalize the 2026-27 roster exactly once, in one transaction."""
    if db.session.get(DataMigration, OFFICER_IMPORT_KEY):
        return

    existing_by_key = defaultdict(list)
    for user in User.query.all():
        existing_by_key[_account_name_key(user.username)].append(user)

    created = 0
    updated = 0
    deleted_member_duplicates = 0
    for roster_name, access in OFFICER_ROSTER_2026_27:
        username = _canonical_officer_username(roster_name)
        roster_key = _account_name_key(roster_name)
        username_key = _account_name_key(username)
        candidate_keys = {roster_key, username_key}
        matches = []
        seen_user_ids = set()
        for candidate_key in candidate_keys:
            for candidate in existing_by_key.get(candidate_key, []):
                identity = candidate.id if candidate.id is not None else id(candidate)
                if identity not in seen_user_ids:
                    seen_user_ids.add(identity)
                    matches.append(candidate)
        officer_matches = [user for user in matches if user.role != 'member']
        member_matches = [user for user in matches if user.role == 'member']
        if len(officer_matches) > 1:
            names = ', '.join(user.username for user in officer_matches)
            raise RuntimeError(
                f'Multiple officer accounts match {roster_name}: {names}'
            )

        # Prefer an existing officer account. If only a member account exists,
        # convert one of those rows so the person's database identity is reused.
        user = officer_matches[0] if officer_matches else (
            member_matches.pop(0) if member_matches else None
        )
        is_admin = 'admin' in access.lower()
        temporary_password = (
            OFFICER_IMPORT_ADMIN_PASSWORD
            if is_admin else OFFICER_IMPORT_OFFICER_PASSWORD
        )

        if user is None:
            user = User(username=username)
            db.session.add(user)
            existing_by_key[username_key] = [user]
            created += 1
        else:
            updated += 1

        # If both officer and member accounts existed for this roster name,
        # remove only the duplicate member rows and their member-only records.
        for duplicate_member in member_matches:
            _delete_duplicate_member_account(duplicate_member)
            deleted_member_duplicates += 1
        if member_matches:
            db.session.flush()

        # The app represents admins as officer accounts with an admin flag.
        user.username = username
        user.role = 'officer'
        user.is_admin = is_admin
        user.has_officer_access = 'officer' in access.lower()
        user.password = generate_password_hash(temporary_password)
        user.must_change_password = True
        if user.is_competing is None:
            user.is_competing = True

    db.session.add(DataMigration(
        key=OFFICER_IMPORT_KEY,
        details=(
            f'Created {created}; updated {updated}; '
            f'deleted member duplicates {deleted_member_duplicates}; '
            f'total {len(OFFICER_ROSTER_2026_27)}'
        ),
    ))
    db.session.commit()
    print(
        f'[Officer Import] 2026-27 complete: created={created}, '
        f'updated={updated}, member_duplicates_deleted={deleted_member_duplicates}, '
        f'total={len(OFFICER_ROSTER_2026_27)}'
    )


def reconcile_removed_admin_access():
    """Remove admin access from the three officers in existing databases."""
    if db.session.get(DataMigration, REMOVED_ADMIN_ACCESS_KEY):
        return

    target_keys = set()
    for roster_name in REMOVED_ADMIN_ACCESS:
        target_keys.add(_account_name_key(roster_name))
        target_keys.add(_account_name_key(_canonical_officer_username(roster_name)))

    updated = []
    for user in User.query.all():
        if _account_name_key(user.username) in target_keys and user.is_admin:
            user.is_admin = False
            user.role = 'officer'
            user.has_officer_access = True
            updated.append(user.username)

    db.session.add(DataMigration(
        key=REMOVED_ADMIN_ACCESS_KEY,
        details=f'Removed admin access from: {", ".join(updated) or "no matching admins"}',
    ))
    db.session.commit()
    print(f'[Admin Access] removed from: {", ".join(updated) or "no matching admins"}')


def sync_officer_access_flags():
    """Backfill admin/officer access for old accounts without changing passwords."""
    roster_access = {}
    for roster_name, access in OFFICER_ROSTER_2026_27:
        permissions = (
            'admin' in access.lower(),
            'officer' in access.lower(),
        )
        roster_access[_account_name_key(roster_name)] = permissions
        roster_access[
            _account_name_key(_canonical_officer_username(roster_name))
        ] = permissions
    changed = False
    for user in User.query.all():
        key = _account_name_key(user.username)
        if key in roster_access:
            desired_admin, desired_officer = roster_access[key]
            if bool(user.is_admin) != desired_admin:
                user.is_admin = desired_admin
                changed = True
            if user.role != 'officer':
                user.role = 'officer'
                changed = True
        elif user.role == 'officer' and not user.is_admin:
            # Preserve officer-only accounts created outside the roster import.
            desired_officer = True
        else:
            # Unknown legacy admins remain admin-only until explicitly granted
            # officer access through the normal admin toggle workflow.
            continue
        if bool(user.has_officer_access) != desired_officer:
            user.has_officer_access = desired_officer
            changed = True

    if changed:
        db.session.commit()
def reconcile_saron_access():
    """Correct the old Saron username typo without changing the password."""
    correct = User.query.filter(
        db.func.lower(User.username) == 'saron.amdeberhan'
    ).first()

    typo = User.query.filter(
        db.func.lower(User.username) == 'saron.amberhan'
    ).first()

    # If only the typo account exists, rename it so its password and data remain.
    if correct is None and typo is not None:
        typo.username = 'saron.amdeberhan'
        correct = typo
        typo = None

    if correct is not None:
        correct.role = 'officer'
        correct.is_admin = True
        correct.has_officer_access = True

    # If both accounts exist, remove access from the unintended typo account.
    if typo is not None and typo.id != correct.id:
        typo.is_admin = False
        typo.has_officer_access = False
        typo.role = 'member'

    db.session.commit()

# ── Dependency-free MDP workbook import ─────────────────────────────────────

_XLSX_NS = 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'
_XLSX_REL_NS = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'
_XLSX_PACKAGE_REL_NS = 'http://schemas.openxmlformats.org/package/2006/relationships'
_WRITTEN_DEADLINE_RE = re.compile(r'(?<!\d)(\d{1,2}/\d{1,2})(?!\d)')


def _spreadsheet_text(value):
    if value is None:
        return ''
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return re.sub(r'\s+', ' ', str(value).replace('\n', ' ')).strip()


def _spreadsheet_key(value):
    return _spreadsheet_text(value).lower()


def _spreadsheet_event_key(value):
    key = _spreadsheet_text(value).upper()
    return 'NA' if key in {'N/A', 'N-A'} else key


def _xlsx_formula_fallback(formula):
    """Recover the cached Google-Sheets value stored in an IFERROR formula."""
    text = (formula or '').strip()
    if not text.upper().startswith('IFERROR('):
        return None
    match = re.search(
        r',\s*("(?:[^"]|"")*"|[-+]?\d+(?:\.\d+)?)\s*\)\s*$',
        text,
    )
    if not match:
        return None
    raw = match.group(1)
    if raw.startswith('"'):
        return raw[1:-1].replace('""', '"')
    try:
        number = float(raw)
        return int(number) if number.is_integer() else number
    except ValueError:
        return None


def _xlsx_column_index(cell_reference):
    letters = re.match(r'[A-Z]+', (cell_reference or '').upper())
    if not letters:
        return 0
    result = 0
    for char in letters.group(0):
        result = result * 26 + ord(char) - ord('A') + 1
    return result - 1


def _xlsx_cell_value(cell, shared_strings):
    cell_type = cell.attrib.get('t')
    formula_node = cell.find(f'{{{_XLSX_NS}}}f')
    formula = formula_node.text if formula_node is not None else ''
    value_node = cell.find(f'{{{_XLSX_NS}}}v')
    raw = value_node.text if value_node is not None else None

    if cell_type == 'inlineStr':
        parts = [node.text or '' for node in cell.findall(f'.//{{{_XLSX_NS}}}t')]
        value = ''.join(parts)
    elif cell_type == 's' and raw is not None:
        try:
            value = shared_strings[int(raw)]
        except (ValueError, IndexError):
            value = ''
    elif cell_type == 'b':
        value = raw == '1'
    elif cell_type in {'str', 'e'}:
        value = raw or ''
    elif raw in (None, ''):
        value = ''
    else:
        try:
            number = float(raw)
            value = int(number) if number.is_integer() else number
        except ValueError:
            value = raw

    # The Pods sheet was exported from Google Sheets. Excel stores #NAME? in
    # its value cache and the real imported value as IFERROR's final argument.
    if value in ('', '#NAME?') and formula:
        fallback = _xlsx_formula_fallback(formula)
        if fallback is not None:
            value = fallback
    return value


def _read_xlsx_sheets(path, wanted_names):
    """Return {sheet_name: rows} using only Python's standard library."""
    with zipfile.ZipFile(path) as archive:
        shared_strings = []
        if 'xl/sharedStrings.xml' in archive.namelist():
            root = ET.fromstring(archive.read('xl/sharedStrings.xml'))
            for item in root.findall(f'{{{_XLSX_NS}}}si'):
                shared_strings.append(''.join(
                    node.text or '' for node in item.findall(f'.//{{{_XLSX_NS}}}t')
                ))

        workbook_root = ET.fromstring(archive.read('xl/workbook.xml'))
        rels_root = ET.fromstring(archive.read('xl/_rels/workbook.xml.rels'))
        relationships = {
            rel.attrib['Id']: rel.attrib['Target']
            for rel in rels_root.findall(f'{{{_XLSX_PACKAGE_REL_NS}}}Relationship')
        }
        sheet_paths = {}
        for sheet in workbook_root.findall(f'.//{{{_XLSX_NS}}}sheet'):
            name = sheet.attrib.get('name')
            if name not in wanted_names:
                continue
            rel_id = sheet.attrib.get(f'{{{_XLSX_REL_NS}}}id')
            target = relationships.get(rel_id, '')
            sheet_paths[name] = (
                target.lstrip('/') if target.startswith('/')
                else posixpath.normpath(posixpath.join('xl', target))
            )

        missing = sorted(set(wanted_names) - set(sheet_paths))
        if missing:
            raise RuntimeError(f'MDP workbook is missing sheet(s): {", ".join(missing)}')

        result = {}
        for name, sheet_path in sheet_paths.items():
            root = ET.fromstring(archive.read(sheet_path))
            rows = []
            for row_node in root.findall(f'.//{{{_XLSX_NS}}}sheetData/{{{_XLSX_NS}}}row'):
                values_by_index = {}
                for cell in row_node.findall(f'{{{_XLSX_NS}}}c'):
                    index = _xlsx_column_index(cell.attrib.get('r'))
                    values_by_index[index] = _xlsx_cell_value(cell, shared_strings)
                if values_by_index:
                    width = max(values_by_index) + 1
                    row = [''] * width
                    for index, value in values_by_index.items():
                        row[index] = value
                else:
                    row = []
                rows.append(row)
            result[name] = rows
        return result


def _row_value(row, index):
    return row[index] if index is not None and index < len(row) else ''


def _header_index(headers, *wanted):
    wanted_keys = {_spreadsheet_key(value) for value in wanted}
    for index, header in enumerate(headers):
        if _spreadsheet_key(header) in wanted_keys:
            return index
    return None


def _spreadsheet_number(value):
    try:
        return int(round(float(value or 0)))
    except (TypeError, ValueError):
        return 0


def _spreadsheet_checked(value):
    if isinstance(value, bool):
        return value
    try:
        return float(value) == 1.0
    except (TypeError, ValueError):
        return _spreadsheet_key(value) in {'true', 'yes', 'y', 'checked', 'x'}


def _spreadsheet_username_candidates(name):
    normalized = _spreadsheet_key(name)
    if not normalized:
        return []
    parts = normalized.split()
    candidates = {
        normalized,
        normalized.replace(' ', ''),
        normalized.replace(' ', '.'),
        normalized.replace(' ', '_'),
    }
    if len(parts) >= 2:
        candidates.add(f'{parts[0]}.{parts[-1]}')
    return list(candidates)


def _find_spreadsheet_user(name, email, users_by_username, users_by_email):
    normalized_name = _spreadsheet_key(name)
    for username in SPREADSHEET_ACCOUNT_OVERRIDES.get(normalized_name, ()):
        user = users_by_username.get(username.lower())
        if user:
            return user
    normalized_email = _spreadsheet_key(email)
    if normalized_email and normalized_email in users_by_email:
        return users_by_email[normalized_email]
    # Most app usernames are the local part of the Warriorlife email. This
    # keeps imports reliable even when the User.email field was never filled.
    if '@' in normalized_email:
        email_username = normalized_email.split('@', 1)[0]
        user = users_by_username.get(email_username)
        if user:
            return user
    for candidate in _spreadsheet_username_candidates(name):
        user = users_by_username.get(candidate)
        if user:
            return user
    return None


def _conference_columns(headers):
    """Map duplicate RP/EX/WR headers inside their conference block."""
    result = defaultdict(dict)
    active_conference = None
    for index, header in enumerate(headers):
        key = _spreadsheet_key(header)
        conference_match = re.search(r'\b(vcmc|svcdc|scdc)\b', key)
        if conference_match and ('conference:' in key or 'commitments' in key):
            active_conference = conference_match.group(1).upper()
            continue
        if not active_conference:
            continue
        if key.startswith('roleplays:'):
            result[active_conference]['roleplay'] = index
        elif key.startswith('exams:'):
            result[active_conference]['exam'] = index
        elif key.startswith('written presentation:'):
            result[active_conference]['written'] = index
    return dict(result)


def _written_columns(headers):
    start_index = _header_index(headers, 'SCDC Total Progress')
    if start_index is None:
        return []
    end_index = None
    for index in range(start_index + 1, len(headers)):
        key = _spreadsheet_key(headers[index])
        if 'conference:' in key and 'vcmc' in key:
            end_index = index
            break
    if end_index is None:
        return []
    columns = []
    for index in range(start_index + 1, end_index):
        header = _spreadsheet_text(headers[index])
        match = _WRITTEN_DEADLINE_RE.search(header)
        if not match:
            continue
        deadline = match.group(1)
        item_name = _WRITTEN_DEADLINE_RE.sub('', header).strip(' -–—().:')
        if item_name:
            columns.append((index, item_name, deadline))
    return columns


def _format_spreadsheet_grade(value):
    if value in (None, ''):
        return None

    try:
        number = float(value)
    except (TypeError, ValueError):
        return _spreadsheet_text(value) or None

    # Spreadsheet percentage cells are stored as decimal ratios.
    # Examples: 0.85 = 85%, 1.10 = 110%, 1.20 = 120%.
    if -2 <= number <= 2:
        number *= 100

    return f'{number:.1f}%'

def normalize_stored_conference_grades():
    """Repair older imported grades such as 1.1 that mean 110%."""
    changed = 0

    for commitment in Commitment.query.filter(
        Commitment.grade.is_not(None)
    ).all():
        raw = str(commitment.grade).strip()
        numeric_text = raw.replace('%', '').strip()

        try:
            number = float(numeric_text)
        except ValueError:
            continue

        # Values without a percent sign between -2 and 2 came from
        # spreadsheet percentage ratios.
        if '%' not in raw and -2 <= number <= 2:
            number *= 100

        normalized = f'{number:.1f}%'

        if commitment.grade != normalized:
            commitment.grade = normalized
            changed += 1

    if changed:
        db.session.commit()
        print(f'[Grade Repair] normalized {changed} conference grade(s).')


def import_mdp_tracking_workbook(path, commitments_only=False):
    """Synchronize matched members from the MDP workbook transactionally."""
    wanted_sheets = set(EVENT_TABS) | {
        'Pods (VIEW ONLY)',
        'Mentee Commitment TrackingMDP',
    }
    sheets = _read_xlsx_sheets(path, wanted_sheets)

    main_rows = sheets['Mentee Commitment TrackingMDP']
    if not main_rows:
        raise RuntimeError('MDP summary sheet is empty.')
    main_headers = main_rows[0]
    main_name_col = _header_index(main_headers, 'Mentee Name')
    main_email_col = _header_index(main_headers, 'Email')
    main_event_col = _header_index(main_headers, 'Event')
    main_category_col = _header_index(main_headers, 'Event Category')
    grade_columns = {
        'VCMC': _header_index(main_headers, 'MC Grade'),
        'SVCDC': _header_index(main_headers, 'SV Grade'),
        'SCDC': _header_index(main_headers, 'SC Grade'),
    }
    event_to_category = {}
    grades_by_identity = {}
    for row in main_rows[1:]:
        name = _spreadsheet_text(_row_value(row, main_name_col))
        email = _spreadsheet_key(_row_value(row, main_email_col))
        event = _spreadsheet_event_key(_row_value(row, main_event_col))
        category = _spreadsheet_event_key(_row_value(row, main_category_col))
        if event and category:
            event_to_category[event] = category
        grades = {
            conference: _format_spreadsheet_grade(_row_value(row, column))
            for conference, column in grade_columns.items()
        }
        for identity in (email, _spreadsheet_key(name)):
            if identity:
                grades_by_identity[identity] = grades

    completion_by_identity = {}
    written_requirements = {}
    written_completion = {}
    for tab in EVENT_TABS:
        rows = sheets[tab]
        if not rows:
            continue
        headers = rows[0]
        name_col = _header_index(headers, 'Legal Name', 'Legal name', 'Mentee Name', 'Mentee', 'Name')
        email_col = _header_index(headers, 'Email')
        event_col = _header_index(headers, 'Event')
        conference_columns = _conference_columns(headers)
        written_columns = _written_columns(headers)
        for row in rows[1:]:
            name = _spreadsheet_text(_row_value(row, name_col))
            email = _spreadsheet_key(_row_value(row, email_col))
            event = _spreadsheet_event_key(_row_value(row, event_col))
            if not name or not event:
                continue
            category = event_to_category.get(event, event)
            if category != tab:
                continue
            completion = {
                conference: {
                    bucket: _spreadsheet_number(_row_value(row, column))
                    for bucket, column in bucket_columns.items()
                }
                for conference, bucket_columns in conference_columns.items()
            }
            checks = {
                item_name: _spreadsheet_checked(_row_value(row, column))
                for column, item_name, _ in written_columns
            }
            for _, item_name, deadline in written_columns:
                written_requirements[(event, item_name)] = deadline
            for identity in (email, _spreadsheet_key(name)):
                if identity:
                    completion_by_identity[identity] = completion
                    written_completion[(identity, event)] = checks

    pods_rows = sheets['Pods (VIEW ONLY)']
    if not pods_rows:
        raise RuntimeError('Pods (VIEW ONLY) sheet is empty.')
    pod_headers = pods_rows[0]
    pod_number_col = 0
    mentor_col = _header_index(pod_headers, 'Mentor')
    mentee_col = _header_index(pod_headers, 'Mentee')
    pod_email_col = _header_index(pod_headers, 'Email')
    status_col = _header_index(pod_headers, 'Status')
    years_col = _header_index(pod_headers, 'Years in DECA')
    pod_event_col = _header_index(pod_headers, 'Event')
    legal_name_col = _header_index(pod_headers, 'Legal name', 'Legal Name')

    all_users = User.query.all()
    users_by_username = {user.username.lower(): user for user in all_users}
    users_by_email = {
        user.email.lower(): user for user in all_users if user.email
    }
    officer_users = [user for user in all_users if user.role == 'officer']
    officers_by_username = {user.username.lower(): user for user in officer_users}
    officers_by_email = {
        user.email.lower(): user for user in officer_users if user.email
    }
    pods_by_user = {
        pod.member_id: pod for pod in MentorPod.query.all()
    }
    commitments_by_key = {
        (commitment.user_id, commitment.event): commitment
        for commitment in Commitment.query.filter(Commitment.user_id.is_not(None)).all()
    }
    checklist_items_by_key = {
        (item.user_id, item.event, item.item_name): item
        for item in ChecklistItem.query.all()
    }

    matched_user_ids = set()
    unmatched_members = []
    members_without_completion = []
    unmatched_mentors = []
    pods_updated = commitments_updated = checklist_items_updated = 0

    # Replace the written catalog only for a full import. A corrective
    # commitment-only run must not overwrite checkbox changes made in the app.
    if not commitments_only:
        ChecklistRequirement.query.delete()
        for (event, item_name), deadline in sorted(written_requirements.items()):
            db.session.add(ChecklistRequirement(
                event=event,
                item_name=item_name,
                deadline=deadline,
            ))

    for row in pods_rows[1:]:
        mentee_name = _spreadsheet_text(_row_value(row, mentee_col))
        legal_name = _spreadsheet_text(_row_value(row, legal_name_col))
        display_name = legal_name or mentee_name
        email = _spreadsheet_text(_row_value(row, pod_email_col))
        if not display_name and not email:
            continue
        # Google Sheets leaves formula artifacts below the real pod roster.
        # Every actual roster row has a Warriorlife email; ignoring the
        # artifacts prevents team IDs from being reported as fake members.
        if '@' not in email:
            continue
        member = _find_spreadsheet_user(
            display_name or mentee_name,
            email,
            users_by_username,
            users_by_email,
        ) or _find_spreadsheet_user(
            mentee_name,
            email,
            users_by_username,
            users_by_email,
        )
        if not member:
            unmatched_members.append(display_name or email)
            continue

        mentor_name = _spreadsheet_text(_row_value(row, mentor_col))
        mentor = None
        if not commitments_only:
            mentor = _find_spreadsheet_user(
                mentor_name,
                '',
                officers_by_username,
                officers_by_email,
            )
            if not mentor:
                unmatched_mentors.append(f'{mentor_name} ({member.username})')

        matched_user_ids.add(member.id)
        status = _spreadsheet_text(_row_value(row, status_col))
        level = 'E' if status.lower() == 'experienced' else 'N'
        event = _spreadsheet_event_key(_row_value(row, pod_event_col))
        year_in_deca = _spreadsheet_text(_row_value(row, years_col)) or None
        if not commitments_only:
            if email and not member.email:
                member.email = email
                users_by_email[email.lower()] = member
            member.is_competing = status.lower() != 'non-compete'
            pod = pods_by_user.get(member.id)
            if not pod and mentor:
                pod = MentorPod(member_id=member.id, mentor_id=mentor.id, pod_number=0)
                db.session.add(pod)
                pods_by_user[member.id] = pod
            if pod:
                if mentor:
                    pod.mentor_id = mentor.id
                pod.experience_level = level
                pod.year_in_deca = year_in_deca
                pod.event = event or None
                pod_number = _spreadsheet_number(_row_value(row, pod_number_col))
                if pod_number:
                    pod.pod_number = pod_number
                pods_updated += 1

        identities = [_spreadsheet_key(email), _spreadsheet_key(display_name), _spreadsheet_key(mentee_name)]
        completion = next(
            (completion_by_identity[key] for key in identities if key in completion_by_identity),
            None,
        )
        # A missing completion row is a mapping error, not proof that the
        # member completed zero requirements. Preserve the existing database
        # rows and surface the name in import diagnostics instead of silently
        # resetting all three conferences to 0 completed.
        if completion is None:
            members_without_completion.append(display_name or email)
            continue
        grades = next(
            (grades_by_identity[key] for key in identities if key in grades_by_identity),
            {},
        )
        requirements = EVENT_REQUIREMENTS.get(level, EVENT_REQUIREMENTS['N'])
        for conference in CONFERENCE_ORDER:
            rule = requirements[conference]
            done = completion.get(conference)
            required = {
                bucket: (
                    rule[bucket]
                    if done is None or bucket in done
                    else 0
                )
                for bucket in ('roleplay', 'written', 'exam')
            }
            done = done or {}
            commitment_key = (member.id, conference)
            commitment = commitments_by_key.get(commitment_key)
            if not commitment:
                commitment = Commitment(
                    user_id=member.id,
                    member_name=member.username,
                    event=conference,
                )
                db.session.add(commitment)
                commitments_by_key[commitment_key] = commitment
            commitment.member_name = member.username
            commitment.required_roleplay = required['roleplay']
            commitment.required_written = required['written']
            commitment.required_exam = required['exam']
            commitment.remaining_roleplay = max(0, required['roleplay'] - done.get('roleplay', 0))
            commitment.remaining_written = max(0, required['written'] - done.get('written', 0))
            commitment.remaining_exam = max(0, required['exam'] - done.get('exam', 0))
            commitment.deadline = datetime.strptime(rule['deadline'], '%Y-%m-%d').date()
            if grades.get(conference) is not None:
                commitment.grade = grades[conference]
            commitments_updated += 1

        if not commitments_only:
            checks = next(
                (
                    written_completion[(key, event)]
                    for key in identities
                    if (key, event) in written_completion
                ),
                {},
            )
            for item_name, completed in checks.items():
                item_key = (member.id, event, item_name)
                item = checklist_items_by_key.get(item_key)
                if not item:
                    item = ChecklistItem(
                        user_id=member.id,
                        event=event,
                        item_name=item_name,
                    )
                    db.session.add(item)
                    checklist_items_by_key[item_key] = item
                item.completed = completed
                checklist_items_updated += 1

    return {
        'matched_members': len(matched_user_ids),
        'unmatched_members': sorted(set(filter(None, unmatched_members))),
        'members_without_completion': sorted(set(filter(None, members_without_completion))),
        'unmatched_mentors': sorted(set(filter(None, unmatched_mentors))),
        'pods_updated': pods_updated,
        'commitments_updated': commitments_updated,
        'checklist_requirements': len(written_requirements),
        'checklist_items_updated': checklist_items_updated,
    }


def sync_mdp_tracking_workbook():
    """Import each workbook version once without adding pandas to production."""
    candidates = [MDP_TRACKING_FILE]
    bundled = os.path.join(
        os.path.dirname(__file__),
        'source_workbook',
        'FINAL MDP Deadline Tracking.xlsx',
    )
    if bundled not in candidates:
        candidates.append(bundled)
    path = next((candidate for candidate in candidates if candidate and os.path.isfile(candidate)), None)
    if not path:
        print('[MDP Import] workbook not found; set MDP_TRACKING_FILE to enable sync.')
        return None

    digest = hashlib.sha256()
    with open(path, 'rb') as workbook_file:
        for chunk in iter(lambda: workbook_file.read(1024 * 1024), b''):
            digest.update(chunk)
    migration_key = MDP_IMPORT_KEY_PREFIX + digest.hexdigest()[:32]
    if db.session.get(DataMigration, migration_key):
        return None

    stats = import_mdp_tracking_workbook(path, commitments_only=True)
    db.session.add(DataMigration(
        key=migration_key,
        details=(
            f"Matched {stats['matched_members']}; pods {stats['pods_updated']}; "
            f"commitments {stats['commitments_updated']}; "
            f"unmatched members {len(stats['unmatched_members'])}; "
            f"missing completion rows {len(stats['members_without_completion'])}"
        ),
    ))
    db.session.commit()
    print(
        '[MDP Import] complete: '
        f"matched={stats['matched_members']}, pods={stats['pods_updated']}, "
        f"commitments={stats['commitments_updated']}, "
        f"unmatched_members={len(stats['unmatched_members'])}, "
        f"missing_completion={len(stats['members_without_completion'])}, "
        f"unmatched_mentors={len(stats['unmatched_mentors'])}"
    )
    if stats['unmatched_members']:
        print('[MDP Import] unmatched members: ' + ', '.join(stats['unmatched_members']))
    if stats['members_without_completion']:
        print(
            '[MDP Import] members without completion rows: '
            + ', '.join(stats['members_without_completion'])
        )
    if stats['unmatched_mentors']:
        print('[MDP Import] unmatched mentors: ' + ', '.join(stats['unmatched_mentors']))
    return stats

WRITTEN_DEADLINES_BY_FAMILY = {
    'IMC': [
        ('Product/Service Description & Campaign Objectives', '10/4'),
        ('Target Market', '10/11'),
        ('Campaign Activities & Schedule', '10/18'),
        ('Budget', '10/25'),
        ('Executive Summary', '10/29'),
        ('Key Metrics & Final Review (100% Complete)', '10/29'),
        ('VCMC Written Submission Due', '10/31'),
    ],

    'BOR': [
        ('Research Methods', '10/4'),
        ('Findings & Conclusions (Target Market)', '10/11'),
        ('Strategic Plan', '10/18'),
        ('Budget', '10/25'),
        ('Executive Summary', '10/29'),
        ('Performance Metrics & Final Review', '10/29'),
        ('VCMC Written Submission Due', '10/31'),
    ],

    'ENT': [
        ('Customer Segments', '10/4'),
        ('SWOT Analysis', '10/11'),
        ('Unique Value Proposition & Competition', '10/18'),
        ('Revenue & Cost Structure (Financials)', '10/25'),
        ('Executive Summary & Business Concept', '10/29'),
        ('Key Metrics, Marketing Channels & Final Review', '10/29'),
        ('VCMC Written Submission Due', '10/31'),
    ],

    'PM': [
        ('Planning & Organization', '10/4'),
        ('Execution Timeline', '10/11'),
        ('Monitoring & Controlling', '10/18'),
        ('Closing the Project', '10/25'),
        ('Executive Summary & Initiating', '10/29'),
        ('Final Review & Presentation Readiness', '10/29'),
        ('VCMC Written Submission Due', '10/31'),
    ],

    'PS': [
        ('Customer Needs / Problem Statement', '10/4'),
        ('Solution Development', '10/11'),
        ('Timeline & Implementation', '10/18'),
        ('Financials & Budget', '10/25'),
        ('Final Review & Presentation Readiness', '10/29'),
        ('VCMC Written Submission Due', '10/31'),
    ],
}


WRITTEN_EVENT_FAMILY = {
    # Business Operations Research
    'BOR': 'BOR',
    'BMOR': 'BOR',
    'FOR': 'BOR',
    'HTOR': 'BOR',
    'SEOR': 'BOR',

    # Integrated Marketing Campaign
    'IMC': 'IMC',
    'IMCE': 'IMC',
    'IMCP': 'IMC',
    'IMCS': 'IMC',

    # Entrepreneurship
    'ENT': 'ENT',
    'EBG': 'ENT',
    'EFB': 'ENT',
    'EIB': 'ENT',
    'EIP': 'ENT',
    'ESB': 'ENT',
    'IBP': 'ENT',

    # Project Management
    'PM': 'PM',
    'PMBS': 'PM',
    'PMCD': 'PM',
    'PMCA': 'PM',
    'PMCG': 'PM',
    'PMFL': 'PM',
    'PMSP': 'PM',

    # Professional Selling and Consulting
    'PS': 'PS',
    'FCE': 'PS',
    'HTPS': 'PS',
    'PSE': 'PS',
}


def sync_written_deadline_catalog():
    """Install the written deadlines from the chapter deadline document."""
    desired_rows = []

    for event, family in WRITTEN_EVENT_FAMILY.items():
        for item_name, deadline in WRITTEN_DEADLINES_BY_FAMILY[family]:
            desired_rows.append((event, item_name, deadline))

    tracked_events = list(WRITTEN_EVENT_FAMILY)

    existing_requirements = ChecklistRequirement.query.filter(
        ChecklistRequirement.event.in_(tracked_events)
    ).order_by(ChecklistRequirement.id).all()

    existing_rows = [
        (
            requirement.event,
            requirement.item_name,
            requirement.deadline,
        )
        for requirement in existing_requirements
    ]

    # Avoid rewriting the table on every deployment if it is already correct.
    if existing_rows == desired_rows:
        return

    ChecklistRequirement.query.filter(
        ChecklistRequirement.event.in_(tracked_events)
    ).delete(synchronize_session=False)

    for event, item_name, deadline in desired_rows:
        db.session.add(ChecklistRequirement(
            event=event,
            item_name=item_name,
            deadline=deadline,
        ))

    db.session.commit()

    print(
        f'[Written Deadlines] installed '
        f'{len(desired_rows)} event checklist requirement(s).'
    )



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
    """Create or reconcile commitment rows while preserving completed work."""
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
        configured_deadline = datetime.strptime(
            rule["deadline"], "%Y-%m-%d"
        ).date()
        if existing.deadline != configured_deadline:
            existing.deadline = configured_deadline
            changed = True

        # Apply the current matrix to existing rows. Preserve the number already
        # completed in each bucket, and clear both values when a bucket is N/A.
        for bucket in ("roleplay", "written", "exam"):
            required_attr = f"required_{bucket}"
            remaining_attr = f"remaining_{bucket}"
            old_required = getattr(existing, required_attr) or 0
            old_remaining = getattr(existing, remaining_attr) or 0
            completed = max(old_required - old_remaining, 0)
            new_required = rule[bucket]
            new_remaining = max(new_required - completed, 0)
            if old_required != new_required or old_remaining != new_remaining:
                setattr(existing, required_attr, new_required)
                setattr(existing, remaining_attr, new_remaining)
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

    ah_present = sum(1 for row in ah_records if row.value == 1.0)
    ah_excused = sum(1 for row in ah_records if row.value == 0.5)
    ah_absent = sum(1 for row in ah_records if row.value == 0.0)
    ws_present = sum(1 for row in ws_records if row.value == 1.0)
    ws_excused = sum(1 for row in ws_records if row.value == 0.5)
    ws_absent = sum(1 for row in ws_records if row.value == 0.0)

    # Excused absences remain visible and count as scheduled sessions, but they
    # are not counted as present in the attendance percentage.
    ah_total = ah_present + ah_excused + ah_absent
    ws_total = ws_present + ws_excused + ws_absent
    ah_rate = round((ah_present / ah_total) * 100, 1) if ah_total else 0.0
    ws_rate = round((ws_present / ws_total) * 100, 1) if ws_total else 0.0

    if not pod_loaded:
        pod = MentorPod.query.filter_by(member_id=user.id).first()
    level = pod.experience_level if pod and pod.experience_level else 'N'
    ws_threshold_pct = WS_THRESHOLD.get(level, WS_THRESHOLD['N']) * 100

    is_competing = user.is_competing is not False
    ah_ok = ah_rate >= (AH_THRESHOLD * 100)
    ws_ok = ws_rate >= ws_threshold_pct
    at_risk = is_competing and (not ah_ok or not ws_ok)
    risk_reasons = []
    if is_competing and not ah_ok:
        risk_reasons.append(
            f"AH attendance {ah_rate}% < {AH_THRESHOLD * 100:.0f}% required"
        )
    if is_competing and not ws_ok:
        risk_reasons.append(
            f"WS attendance {ws_rate}% < {ws_threshold_pct:.0f}% required"
        )

    return {
        'ah_total': ah_total,
        'ah_present': ah_present,
        'ah_excused': ah_excused,
        'ah_absent': ah_absent,
        'ah_sum': ah_present,
        'ah_rate': ah_rate,
        'ws_total': ws_total,
        'ws_present': ws_present,
        'ws_excused': ws_excused,
        'ws_absent': ws_absent,
        'ws_sum': ws_present,
        'ws_rate': ws_rate,
        'level': level,
        'ws_threshold_pct': ws_threshold_pct,
        'is_competing': is_competing,
        'status': 'non_compete' if not is_competing else ('at_risk' if at_risk else 'on_track'),
        'status_label': 'Non-Compete' if not is_competing else ('At Risk' if at_risk else 'On Track'),
        'at_risk': at_risk,
        'risk_reasons': risk_reasons,
    }

# ── Forms ─────────────────────────────────────────────────────────────────────

class RegisterForm(FlaskForm):
    username = StringField('Username', [DataRequired(), Length(min=3)])
    password = PasswordField('Password', [DataRequired(), Length(min=6)])
    # Admin access is granted only by the canonical roster or an existing admin.
    # Public registration must never allow a visitor to self-select admin.
    submit = SubmitField('Register')

    def validate_role(self, field):
        if not field.data:
            raise ValidationError('Please select a role.')

class LoginForm(FlaskForm):
    username = StringField('Username', [DataRequired()])
    password = PasswordField('Password', [DataRequired()])
    submit = SubmitField('Login')

class SetPasswordForm(FlaskForm):
    new_password = PasswordField(
        'New Password',
        [DataRequired(), Length(min=8, max=128)],
    )
    confirm_password = PasswordField('Confirm Password', [DataRequired()])
    submit = SubmitField('Save Password')

    def validate_new_password(self, field):
        password = field.data or ''
        if not re.search(r'[A-Z]', password):
            raise ValidationError('Include at least one uppercase letter.')
        if not re.search(r'[a-z]', password):
            raise ValidationError('Include at least one lowercase letter.')
        if not re.search(r'\d', password):
            raise ValidationError('Include at least one number.')

    def validate_confirm_password(self, field):
        if field.data != self.new_password.data:
            raise ValidationError('Passwords do not match.')


class ForgotPasswordForm(FlaskForm):
    email = StringField('Email', [DataRequired(), Length(max=120)])
    submit = SubmitField('Send Reset Link')

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
    member_id = SelectField('Member', coerce=int, validators=[DataRequired()])
    mentor_id = SelectField('Mentor', coerce=int, validators=[DataRequired()])
    experience_level = SelectField(
        'Level', choices=[('N', 'Novice'), ('E', 'Experienced')]
    )
    event = StringField('Event', validators=[DataRequired(), Length(max=20)])
    is_competing = SelectField(
        'Competing',
        choices=[('yes', 'Competing'), ('no', 'Non-Compete')],
        default='yes',
    )
    submit = SubmitField('Save')


class MDPWorkbookUploadForm(FlaskForm):
    """Temporary, admin-only upload form for commitment repairs."""
    workbook = FileField(
        'MDP tracking workbook',
        validators=[
            FileRequired(message='Please select the MDP tracking workbook.'),
            FileAllowed(['xlsx'], message='Please upload an .xlsx workbook.'),
        ],
    )
    submit = SubmitField('Import Commitments')


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

    if 'has_officer_access' not in user_cols:
        try:
            db.session.execute(sql_text(
                'ALTER TABLE "user" ADD COLUMN has_officer_access BOOLEAN DEFAULT FALSE'
            ))
            db.session.commit()
        except Exception:
            db.session.rollback()


    if 'is_competing' not in user_cols:
        try:
            db.session.execute(sql_text('ALTER TABLE "user" ADD COLUMN is_competing BOOLEAN DEFAULT TRUE'))
            db.session.commit()
        except Exception:
            db.session.rollback()

    mentor_pod_cols = {c['name'] for c in db.inspect(db.engine).get_columns('mentor_pod')}
    if 'event' not in mentor_pod_cols:
        try:
            db.session.execute(sql_text('ALTER TABLE mentor_pod ADD COLUMN event VARCHAR(20)'))
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

    if 'grade' not in commitment_cols:
        try:
            db.session.execute(sql_text('ALTER TABLE commitment ADD COLUMN grade VARCHAR(10)'))
            db.session.commit()
        except Exception:
            db.session.rollback()

    # Older friend2 rows were sometimes created without the member user_id.
    # Normalize ownership from the stable member_name field.
    for member in User.query.filter_by(role='member').all():
        for commitment in Commitment.query.filter_by(member_name=member.username).all():
            commitment.user_id = member.id
    try:
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

    # One-time 2026-27 officer import. The operation is transactional and an
    # import problem is logged without preventing the web service from booting.
    try:
        import_2026_27_officer_roster()
    except Exception as exc:
        db.session.rollback()
        print(f'[Officer Import] skipped: {exc}')

    # Apply targeted access changes to databases where the roster import has
    # already run. This does not change anyone's password.
    try:
        reconcile_removed_admin_access()
    except Exception as exc:
        db.session.rollback()
        print(f'[Admin Access] reconciliation skipped: {exc}')

    try:
        sync_officer_access_flags()
    except Exception as exc:
        db.session.rollback()
        print(f'[Officer Access] synchronization skipped: {exc}')
    try:
        reconcile_saron_access()
    except Exception as exc:
        db.session.rollback()
        print(f'[Saron Access] reconciliation skipped: {exc}')



    # Import the exact workbook values after officer reconciliation so members
    # who are also officers (for example Anay Kalchuri) retain their tracking
    # rows. Each workbook byte-version is imported only once.
    try:
        sync_mdp_tracking_workbook()
    except Exception as exc:
        db.session.rollback()
        print(f'[MDP Import] skipped: {exc}')

    try:
        normalize_stored_conference_grades()
    except Exception as exc:
        db.session.rollback()
        print(f'[Grade Repair] skipped: {exc}')

    try:
        sync_written_deadline_catalog()
    except Exception as exc:
        db.session.rollback()
        print(f'[Written Deadlines] synchronization skipped: {exc}')





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

PASSWORD_RESET_SALT = 'password-reset-v2'
PASSWORD_RESET_MAX_AGE_SECONDS = 3600


def _password_fingerprint(user):
    return hashlib.sha256(user.password.encode('utf-8')).hexdigest()


def generate_reset_token(user):
    serializer = URLSafeTimedSerializer(app.config['SECRET_KEY'])
    payload = {
        'user_id': user.id,
        'password_fingerprint': _password_fingerprint(user),
    }
    return serializer.dumps(payload, salt=PASSWORD_RESET_SALT)


def verify_reset_token(token, expiration=PASSWORD_RESET_MAX_AGE_SECONDS):
    serializer = URLSafeTimedSerializer(app.config['SECRET_KEY'])
    try:
        payload = serializer.loads(
            token,
            salt=PASSWORD_RESET_SALT,
            max_age=expiration,
        )
    except (SignatureExpired, BadSignature, TypeError, ValueError):
        return None

    if not isinstance(payload, dict):
        return None

    user_id = payload.get('user_id')
    expected_fingerprint = payload.get('password_fingerprint')
    if not user_id or not expected_fingerprint:
        return None

    try:
        user_id = int(user_id)
    except (TypeError, ValueError):
        return None

    user = db.session.get(User, user_id)
    if not user:
        return None

    if not hmac.compare_digest(
        expected_fingerprint,
        _password_fingerprint(user),
    ):
        return None

    return user


def send_email(to_email, subject, html_content):
    if not BREVO_API_KEY:
        app.logger.error('Email not sent: BREVO_API_KEY is missing.')
        return False
    if not FROM_EMAIL:
        app.logger.error('Email not sent: FROM_EMAIL is missing.')
        return False
    if not to_email:
        app.logger.error('Email not sent: recipient email is missing.')
        return False

    payload = {
        'sender': {'name': FROM_NAME, 'email': FROM_EMAIL},
        'to': [{'email': to_email}],
        'subject': subject,
        'htmlContent': html_content,
    }
    headers = {
        'accept': 'application/json',
        'api-key': BREVO_API_KEY,
        'content-type': 'application/json',
    }

    try:
        response = requests.post(
            BREVO_EMAIL_ENDPOINT,
            headers=headers,
            json=payload,
            timeout=20,
        )
    except requests.RequestException:
        app.logger.exception(
            'Brevo request failed while sending email to %s.',
            to_email,
        )
        return False

    if response.status_code == 201:
        try:
            message_id = response.json().get('messageId', 'unknown')
        except ValueError:
            message_id = 'unknown'
        app.logger.info(
            'Brevo accepted email for %s; message id=%s.',
            to_email,
            message_id,
        )
        return True

    app.logger.error(
        'Brevo rejected email for %s: HTTP %s: %s',
        to_email,
        response.status_code,
        response.text[:1000],
    )
    return False


def send_password_reset_email(user):
    if not user.email:
        return False

    token = generate_reset_token(user)
    reset_url = url_for('reset_password', token=token, _external=True)
    html_content = f'''
        <p>A password reset was requested for your account.</p>
        <p><a href="{reset_url}">Reset your password</a></p>
        <p>This link expires in 1 hour and stops working after the password changes.</p>
        <p>If you did not request this, you can ignore this email.</p>
    '''
    return send_email(user.email, 'Reset your password', html_content)

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


def get_commitment_status(user, commitments=None, today=None):
    if user.is_competing is False:
        return 'non_compete'
    if commitments is None:
        commitments = Commitment.query.filter_by(user_id=user.id).all()
    if not commitments:
        return 'on_track'
    today = today or datetime.now(LOCAL_TZ).date()
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
    if user.is_competing is False:
        return False
    if commitments is None:
        commitments = Commitment.query.filter_by(user_id=user.id).all()
    return any(
        (row.remaining_roleplay + row.remaining_written + row.remaining_exam) > 0
        for row in commitments
    ) if commitments else False


def _members_visible_to_current_user():
    if is_admin_view():
        tracked_ids = [row[0] for row in db.session.query(MentorPod.member_id).all()]
        query = User.query.filter(User.role == 'member')
        if tracked_ids:
            query = User.query.filter(
                db.or_(User.role == 'member', User.id.in_(tracked_ids))
            )
        return query.order_by(User.username).all()
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
        is_competing = member.is_competing is not False
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
        if not is_competing:
            status, status_label = 'non_compete', 'Non-Compete'
            hard_risk_reasons = []
            attention_reasons = []
        elif hard_risk_reasons:
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
            'is_competing': is_competing,
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
    ah_ws_data = []
    member_stats = None
    all_commitments = []
    dashboard_scope_label = 'Members in Pod'
    visible_members = []

    if is_admin_view():
        # Admin is a distinct dashboard scope. Show every member, not only
        # members assigned to the admin's underlying officer account. Tracked
        # student officers remain included through their MentorPod member row.
        dashboard_scope_label = 'All Members'
        visible_members = _members_visible_to_current_user()

    elif is_officer_view():
        assigned_workshops = Workshop.query.filter_by(
            officer_id=current_user.id
        ).options(joinedload(Workshop.officer)).order_by(Workshop.time).all()
        workshops = assigned_workshops
        attendance_locked_ids = {
            row.workshop_id
            for row in AttendanceSubmission.query.filter_by(officer_id=current_user.id).all()
        }

        pod_rows = MentorPod.query.filter_by(mentor_id=current_user.id).all()
        visible_members = [db.session.get(User, row.member_id) for row in pod_rows]
        visible_members = [member for member in visible_members if member]

        # Preserve friend2's fallback for officers whose pod rows have not yet
        # been imported but who already have workshop signups.
        if not visible_members:
            member_ids = set()
            for workshop in assigned_workshops:
                member_ids.update(member.id for member in workshop.signups)
            visible_members = [db.session.get(User, user_id) for user_id in member_ids]
            visible_members = [member for member in visible_members if member]

        for member in sorted(visible_members, key=lambda row: row.username.lower()):
            actual_attended = db.session.query(workshop_signups).filter_by(
                user_id=member.id,
                attended=True,
            ).join(Workshop).filter(Workshop.officer_id == current_user.id).count()
            general = GeneralAttendance.query.filter_by(
                officer_id=current_user.id,
                member_name=member.username,
            ).first()
            manual_count = general.manual_count if general else 0
            workshop_attendance_data.append({
                'member_name': member.username,
                'total_attended': actual_attended + manual_count,
                'manual_count': manual_count,
                'actual_attended': actual_attended,
            })

    else:
        # Member dashboard.
        active_conf = get_active_conference()
        ensure_commitments(current_user)
        commitments = Commitment.query.filter_by(
            member_name=current_user.username,
            event=active_conf,
        ).all()
        all_commitments = Commitment.query.filter_by(
            member_name=current_user.username
        ).order_by(Commitment.deadline).all()
        workshops = current_user.workshops.options(
            joinedload(Workshop.officer),
            joinedload(Workshop.creator),
        ).order_by(Workshop.time).all()

        if commitments:
            commitment = commitments[0]
            progress_summary = {
                'roleplay': (
                    'N/A' if commitment.required_roleplay == 0 else
                    f"{commitment.required_roleplay - commitment.remaining_roleplay}/"
                    f"{commitment.required_roleplay}"
                ),
                'written': (
                    'N/A' if commitment.required_written == 0 else
                    f"{commitment.required_written - commitment.remaining_written}/"
                    f"{commitment.required_written}"
                ),
                'exam': (
                    'N/A' if commitment.required_exam == 0 else
                    f"{commitment.required_exam - commitment.remaining_exam}/"
                    f"{commitment.required_exam}"
                ),
                'deadline': (
                    commitment.deadline.strftime('%Y-%m-%d')
                    if commitment.deadline else 'N/A'
                ),
                'event': commitment.event,
            }

        pod = MentorPod.query.filter_by(member_id=current_user.id).first()
        officer_id = pod.mentor_id if pod else None
        actual_attended = 0
        manual_count = 0
        if officer_id:
            actual_attended = db.session.query(workshop_signups).filter_by(
                user_id=current_user.id,
                attended=True,
            ).join(Workshop).filter(Workshop.officer_id == officer_id).count()
            general = GeneralAttendance.query.filter_by(
                officer_id=officer_id,
                member_name=current_user.username,
            ).first()
            manual_count = general.manual_count if general else 0
        total_attended = actual_attended + manual_count
        attendance_summary = {
            'signed': len(current_user.workshops.all()),
            'attended': total_attended,
            'rate': round((total_attended / 18) * 100, 1),
        }
        member_stats = get_attendance_stats(current_user)

    # Admins see every member; officers see their pod/fallback members.
    if is_admin_view() or is_officer_view():
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
                'is_competing': stats['is_competing'],
                'status': stats['status'],
                'status_label': stats['status_label'],
                'at_risk': stats['at_risk'],
                'risk_reasons': stats['risk_reasons'],
                'below_commitments': get_commitments_incomplete(
                    member,
                    commitments=commitments_by_user.get(member.id, []),
                ),
            })

    for workshop in workshops:
        workshop.end_time = workshop.time + timedelta(minutes=20)
    for workshop in created_workshops:
        workshop.end_time = workshop.time + timedelta(minutes=20)

    my_signups = (
        current_user.workshops.all()
        if current_user.role == 'member' and not current_user.is_admin
        else []
    )
    signed_times = [
        (workshop.time, workshop.time + timedelta(minutes=20))
        for workshop in my_signups
    ]
    active_conf = (
        get_active_conference()
        if current_user.role == 'member' and not current_user.is_admin
        else None
    )

    return render_template(
        'dashboard.html',
        commitments=commitments,
        progress_summary=progress_summary,
        attendance_summary=attendance_summary,
        assigned_workshops=assigned_workshops,
        workshop_attendance_data=workshop_attendance_data,
        workshops=workshops,
        my_signups=my_signups,
        mentees_workshops={},
        signed_times=signed_times,
        user=current_user,
        created_workshops=created_workshops,
        attendance_locked_ids=attendance_locked_ids,
        ah_ws_data=ah_ws_data,
        member_stats=member_stats,
        active_conf=active_conf,
        all_commitments=all_commitments,
        conference_order=CONFERENCE_ORDER,
        dashboard_scope_label=dashboard_scope_label,
        # Officers already see only their pod, so hide member/pod filters for
        # them. Admins retain filtering across all members.
        show_member_filters=is_admin_view(),
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
        user = User(
            username=form.username.data.strip(),
            password=hashed_pw,
            role='member',
            is_admin=False,
            has_officer_access=False,
            is_competing=True
        )
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
            session.pop(VIEW_MODE_SESSION_KEY, None)
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
    form = SetPasswordForm()
    if form.validate_on_submit():
        current_user.password = generate_password_hash(form.new_password.data)
        current_user.must_change_password = False
        db.session.commit()
        flash('Your password has been updated.', 'success')
        return redirect(url_for('dashboard'))
    return render_template(
        'set_password.html',
        form=form,
        page_title='Change Password',
        page_description='Choose a new password for your account.',
        submit_label='Change Password',
        show_login_link=False,
    )


@app.route('/forgot-password', methods=['GET', 'POST'])
def forgot_password():
    if current_user.is_authenticated:
        return redirect(url_for('dashboard'))

    form = ForgotPasswordForm()
    if form.validate_on_submit():
        email = form.email.data.strip().lower()
        user = User.query.filter(db.func.lower(User.email) == email).first()

        # Deliberately use the same response whether or not an account exists.
        if user and not send_password_reset_email(user):
            app.logger.error('Reset email failed for user id %s.', user.id)

        flash(
            'If an account matches that email, a reset link has been sent.',
            'info',
        )
        return redirect(url_for('forgot_password'))

    return render_template('forgot_password.html', form=form)


@app.route('/reset-password/<token>', methods=['GET', 'POST'])
def reset_password(token):
    user = verify_reset_token(token)
    if not user:
        flash('This password reset link is invalid or has expired.', 'danger')
        return redirect(url_for('forgot_password'))

    form = SetPasswordForm()
    if form.validate_on_submit():
        user.password = generate_password_hash(form.new_password.data)
        user.must_change_password = False
        db.session.commit()

        if current_user.is_authenticated:
            logout_user()

        flash('Your password has been reset. You can now sign in.', 'success')
        return redirect(url_for('login'))

    return render_template(
        'set_password.html',
        form=form,
        page_title='Reset Password',
        page_description=f'Choose a new password for {user.username}.',
        submit_label='Reset Password',
        show_login_link=True,
    )


@app.route('/logout')
@login_required
def logout():
    session.pop(VIEW_MODE_SESSION_KEY, None)
    logout_user()
    return redirect(url_for('login'))


@app.route('/switch-view/<mode>', methods=['POST'])
@login_required
def switch_account_view(mode):
    """Switch a dual-access account between isolated admin/officer scopes."""
    if not can_switch_account_view():
        flash('This account does not have both admin and officer access.', 'danger')
        return redirect(url_for('dashboard'))
    if mode not in {'admin', 'officer'}:
        abort(400)
    session[VIEW_MODE_SESSION_KEY] = mode
    flash(f'Switched to {mode.title()} View.', 'success')
    return redirect(url_for('dashboard'))

@app.route('/settings', methods=['GET', 'POST'])
@login_required
def settings():
    if current_user.role != 'member' and not current_user.is_admin:
        flash('Only members and admins can access settings.', 'danger')
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
    if not is_officer_view():
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
    if not is_officer_view():
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
def my_commitments():
    if current_user.is_admin or current_user.role != 'member':
        return redirect(url_for('dashboard'))
    ensure_commitments(current_user)
    active_conf = get_active_conference()
    all_commitments = Commitment.query.filter_by(member_name=current_user.username).all()
    all_commitments_map = {com.event: com for com in all_commitments}
    icprep_status = get_icprep_status(current_user)
    return render_template('my_commitments.html',
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
    if is_admin_view():
        flash('Practice Sessions are available to officers and members, not admins.', 'info')
        return redirect(url_for('dashboard'))
    if is_officer_view():
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
    """Compatibility action: mark a signed-up practice slot complete in one click."""
    if not is_officer_view():
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
    ensure_commitments(member)
    commitment = Commitment.query.filter_by(
        member_name=member.username,
        event=ps.conference,
    ).first()
    if commitment:
        if ps.practice_type in ROLEPLAY_TYPES and commitment.remaining_roleplay > 0:
            commitment.remaining_roleplay -= 1
        elif ps.practice_type in WRITTEN_TYPES and commitment.remaining_written > 0:
            commitment.remaining_written -= 1
        elif ps.practice_type in EXAM_TYPES and commitment.remaining_exam > 0:
            commitment.remaining_exam -= 1

    if ps.practice_type in ICPREP_TYPES:
        tracker = AnnualICPrepTracker.query.filter_by(member_id=member.id).first()
        if tracker:
            if ps.practice_type == 'ICPrep Roleplay':
                tracker.icprep_rp_completed += 1
            elif ps.practice_type == 'ICPrep Exam':
                tracker.icprep_exam_completed += 1

    db.session.add(PracticeLog(
        practice_session_id=ps.id,
        officer_id=current_user.id,
        member_id=member.id,
        commitment_id=commitment.id if commitment else None,
        practice_type=ps.practice_type,
        conference=ps.conference,
        session_date=ps.session_date,
    ))
    ps.log_submitted = True

    timestamp = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')
    db.session.commit()

    email_body = (
        f'<p><strong>{ps.practice_type}</strong> marked complete for '
        f'<strong>{ps.conference}</strong> on {timestamp}.</p>'
        f'<p>Member: {member.username}</p>'
        f'<p>Logged by: {current_user.username}</p>'
    )
    send_email(
        PRACTICE_LOG_EMAIL,
        f'[DECA] Commitment logged: {member.username} – '
        f'{ps.practice_type} ({ps.conference})',
        email_body,
    )
    if member.email:
        send_email(
            member.email,
            f'[DECA] Your {ps.practice_type} for {ps.conference} was completed',
            email_body,
        )

    flash(
        f'Marked {ps.practice_type} complete for {member.username} '
        f'({ps.conference}).',
        'success',
    )
    return redirect(request.referrer or url_for('practice_sessions'))


@app.route('/practice_log/submit', methods=['GET', 'POST'])
@login_required
def submit_practice_log():
    """Officer submits a completion form after a practice session."""
    if not is_officer_view():
        flash('Only officers can submit practice logs.', 'danger')
        return redirect(url_for('dashboard'))

    pod_members = MentorPod.query.filter_by(mentor_id=current_user.id).all()
    pod_member_users = sorted(
        [db.session.get(User, pm.member_id) for pm in pod_members if db.session.get(User, pm.member_id)],
        key=lambda u: u.username.lower()
    )

    if request.method == 'POST':
        member_username = request.form.get('member_username', '').strip()
        practice_type = request.form.get('practice_type', '')
        conference = request.form.get('conference', '')
        session_date_str = request.form.get('session_date', '')
        score_str = request.form.get('score', '').strip()
        officer_notes = request.form.get('officer_notes', '').strip()
        feedback = request.form.get('feedback', '').strip()

        member = User.query.filter_by(username=member_username).first()
        if not member:
            flash('Member not found.', 'danger')
            return redirect(url_for('submit_practice_log'))

        try:
            session_date = datetime.strptime(session_date_str, '%Y-%m-%d').date()
        except ValueError:
            flash('Invalid date.', 'danger')
            return redirect(url_for('submit_practice_log'))

        score = float(score_str) if score_str else None

        # Find matching commitment and decrement based on type bucket
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

        # If ICPrep type, also increment annual ICPrep tracker
        if practice_type in ICPREP_TYPES:
            ensure_commitments(member)  # guarantees tracker exists
            tracker = AnnualICPrepTracker.query.filter_by(member_id=member.id).first()
            if tracker:
                if practice_type == 'ICPrep Roleplay':
                    tracker.icprep_rp_completed += 1
                elif practice_type == 'ICPrep Exam':
                    tracker.icprep_exam_completed += 1
                db.session.add(tracker)

        # Create log record
        log = PracticeLog(
            officer_id=current_user.id,
            member_id=member.id,
            commitment_id=commitment.id if commitment else None,
            practice_type=practice_type,
            conference=conference,
            session_date=session_date,
            score=score,
            officer_notes=officer_notes or None,
            feedback=feedback or None,
        )
        db.session.add(log)

        # Mark the linked practice session as logged if submitted from calendar
        session_id = request.form.get('practice_session_id')
        if session_id:
            ps = db.session.get(PracticeSession, int(session_id))
            if ps and ps.officer_id == current_user.id:
                ps.log_submitted = True
                db.session.add(ps)

        db.session.commit()

        # Send email notification
        type_label = practice_type
        score_line = f"Score: {score}" if score is not None else "Score: N/A"
        email_body = f"""
<h3>Practice Log Submitted</h3>
<p><strong>Officer:</strong> {current_user.username}</p>
<p><strong>Member:</strong> {member.username}</p>
<p><strong>Type:</strong> {type_label}</p>
<p><strong>Conference:</strong> {conference}</p>
<p><strong>Date:</strong> {session_date.strftime('%Y-%m-%d')}</p>
<p><strong>{score_line}</strong></p>
<p><strong>Officer Notes:</strong> {officer_notes or '—'}</p>
<p><strong>Feedback:</strong> {feedback or '—'}</p>
"""
        send_email(PRACTICE_LOG_EMAIL, f"[DECA] Practice Log: {member.username} – {type_label} ({conference})", email_body)

        flash(f'Practice log submitted for {member.username}.', 'success')
        return redirect(url_for('practice_sessions'))

    # GET: show the form
    prefill_member = request.args.get('member', '')
    prefill_type = request.args.get('type', '')
    prefill_conf = request.args.get('conf', get_active_conference())
    prefill_session_id = request.args.get('session_id', '')
    return render_template('practice_log_form.html',
        pod_member_users=pod_member_users,
        activity_types=ACTIVITY_TYPES,
        conference_order=CONFERENCE_ORDER,
        prefill_member=prefill_member,
        prefill_type=prefill_type,
        prefill_conf=prefill_conf,
        prefill_session_id=prefill_session_id,
    )


@app.route('/mark_complete', methods=['POST'])
@login_required
def mark_complete():
    """Compatibility action for the Reports page's open member selector."""
    if not is_officer_view():
        flash('Only officers can mark commitments complete.', 'danger')
        return redirect(url_for('dashboard'))

    member_id = request.form.get('member_id', '').strip()
    practice_type = request.form.get('practice_type', '').strip()
    conference = request.form.get('conference', get_active_conference()).strip()

    member = db.session.get(User, int(member_id)) if member_id.isdigit() else None
    if not member or member.role != 'member':
        flash('Member not found.', 'danger')
        return redirect(url_for('reports', tab='commitment'))
    if practice_type not in set(ACTIVITY_TYPES):
        flash('Invalid practice type.', 'danger')
        return redirect(url_for('reports', tab='commitment'))
    if conference not in CONFERENCE_ORDER:
        flash('Invalid conference.', 'danger')
        return redirect(url_for('reports', tab='commitment'))

    ensure_commitments(member)
    commitment = Commitment.query.filter_by(
        member_name=member.username,
        event=conference,
    ).first()
    if commitment:
        if practice_type in ROLEPLAY_TYPES and commitment.remaining_roleplay > 0:
            commitment.remaining_roleplay -= 1
        elif practice_type in WRITTEN_TYPES and commitment.remaining_written > 0:
            commitment.remaining_written -= 1
        elif practice_type in EXAM_TYPES and commitment.remaining_exam > 0:
            commitment.remaining_exam -= 1

    if practice_type in ICPREP_TYPES:
        tracker = AnnualICPrepTracker.query.filter_by(member_id=member.id).first()
        if tracker:
            if practice_type == 'ICPrep Roleplay':
                tracker.icprep_rp_completed += 1
            elif practice_type == 'ICPrep Exam':
                tracker.icprep_exam_completed += 1

    db.session.add(PracticeLog(
        officer_id=current_user.id,
        member_id=member.id,
        commitment_id=commitment.id if commitment else None,
        practice_type=practice_type,
        conference=conference,
        session_date=datetime.now(LOCAL_TZ).date(),
    ))

    timestamp = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')
    db.session.commit()

    email_body = (
        f'<p><strong>{practice_type}</strong> marked complete for '
        f'<strong>{conference}</strong> on {timestamp}.</p>'
        f'<p>Member: {member.username}</p>'
        f'<p>Logged by: {current_user.username}</p>'
    )
    send_email(
        PRACTICE_LOG_EMAIL,
        f'[DECA] Commitment logged: {member.username} – '
        f'{practice_type} ({conference})',
        email_body,
    )
    if member.email:
        send_email(
            member.email,
            f'[DECA] Your {practice_type} for {conference} was completed',
            email_body,
        )

    flash(
        f'Marked {practice_type} complete for {member.username} ({conference}).',
        'success',
    )
    return redirect(url_for('reports', tab='commitment'))


@app.route('/reports')
@login_required
def reports():
    if not is_officer_view():
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
        all_members=User.query.filter_by(role='member').order_by(User.username).all(),
        activity_types=ACTIVITY_TYPES,
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
    if is_admin_view():
        flash('Switch to Officer View to access exam uploads.', 'info')
        return redirect(url_for('dashboard'))
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
    if not is_officer_view():
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




@app.route('/at-risk-report')
@login_required
def at_risk_report():
    if not is_admin_view():
        flash('Only admins can view the Overall Risk Report.', 'danger')
        return redirect(url_for('dashboard'))
    # The dashboard's existing At-Risk Report link now exports a spreadsheet.
    # Add ?view=1 only when the legacy on-screen HTML report is needed.
    if request.args.get('view') != '1':
        return redirect(url_for('export_at_risk_report'))
    members = _members_visible_to_current_user()
    event_items, event_deadlines = get_written_checklist_catalog()
    today = datetime.now(LOCAL_TZ).date()
    all_rows = build_mentee_risk_report(
        members, event_items, event_deadlines, today=today
    )
    report_rows = [
        row for row in all_rows
        if row['status'] in {'at_risk', 'needs_attention'}
    ]
    report_rows.sort(key=lambda row: (
        0 if row['status'] == 'at_risk' else 1,
        row['mentor_name'].lower(),
        row['member'].username.lower(),
    ))
    summary = {
        'total_members': len(all_rows),
        'at_risk_count': sum(row['status'] == 'at_risk' for row in all_rows),
        'needs_attention_count': sum(row['status'] == 'needs_attention' for row in all_rows),
        'on_track_count': sum(row['status'] == 'on_track' for row in all_rows),
        'non_compete_count': sum(row['status'] == 'non_compete' for row in all_rows),
        'actionable_count': len(report_rows),
    }
    return render_template(
        'at_risk_report.html', rows=report_rows, summary=summary, report_date=today
    )


@app.route('/at-risk-report/export')
@login_required
def export_at_risk_report():
    """Export the admin at-risk report as a Google Sheets-compatible CSV."""
    if not is_admin_view():
        flash('Only admins can export the Overall Risk Report.', 'danger')
        return redirect(url_for('dashboard'))

    event_items, event_deadlines = get_written_checklist_catalog()
    today = datetime.now(LOCAL_TZ).date()
    rows = build_mentee_risk_report(
        _members_visible_to_current_user(), event_items, event_deadlines, today=today
    )
    rows = [
        row for row in rows
        if row['status'] in {'at_risk', 'needs_attention'}
    ]
    rows.sort(key=lambda row: (
        0 if row['status'] == 'at_risk' else 1,
        row['mentor_name'].lower(),
        row['member'].username.lower(),
    ))

    output = io.StringIO(newline='')
    writer = csv.writer(output)
    writer.writerow([
        'Member', 'Officer', 'Event', 'Experience', 'Status',
        'AH Attendance', 'WS Attendance', 'VCMC Grade', 'SVCDC Grade',
        'SCDC Grade', 'At-Risk Reasons', 'Needs-Attention Reasons',
    ])
    for row in rows:
        writer.writerow([
            row['member'].username,
            row['mentor_name'],
            row['event'],
            'Novice' if row['level'] == 'N' else 'Experienced',
            row['status_label'],
            f"{row['attendance']['ah_rate']}%",
            f"{row['attendance']['ws_rate']}%",
            row['grades'].get('VCMC') or '',
            row['grades'].get('SVCDC') or '',
            row['grades'].get('SCDC') or '',
            '; '.join(row['hard_risk_reasons']),
            '; '.join(row['attention_reasons']),
        ])

    # The UTF-8 BOM keeps names clean when imported into Google Sheets.
    csv_text = '\ufeff' + output.getvalue()
    filename = f"deca-at-risk-report-{today.isoformat()}.csv"
    return Response(
        csv_text,
        mimetype='text/csv; charset=utf-8',
        headers={'Content-Disposition': f'attachment; filename="{filename}"'},
    )



# ── Admin routes ─────────────────────────────────────────────────────────────

def admin_required(f):
    """Decorator that restricts a route to admin users only."""
    from functools import wraps
    @wraps(f)
    def decorated(*args, **kwargs):
        if not current_user.is_authenticated or not is_admin_view():
            flash('Admin access required.', 'danger')
            return redirect(url_for('dashboard'))
        return f(*args, **kwargs)
    return decorated


def _delete_user_account_and_dependencies(user):
    """Delete one account after removing every database reference to it."""
    user_id = user.id
    username = (user.username or '').strip()

    # Remove sessions owned by this user; preserve another officer's session by
    # clearing only the deleted member's signup.
    owned_session_ids = [
        row[0]
        for row in db.session.query(PracticeSession.id).filter_by(
            officer_id=user_id
        ).all()
    ]
    practice_log_filter = (
        (PracticeLog.member_id == user_id)
        | (PracticeLog.officer_id == user_id)
    )
    if owned_session_ids:
        practice_log_filter = practice_log_filter | (
            PracticeLog.practice_session_id.in_(owned_session_ids)
        )
    PracticeLog.query.filter(practice_log_filter).delete(
        synchronize_session=False
    )
    if owned_session_ids:
        PracticeSession.query.filter(
            PracticeSession.id.in_(owned_session_ids)
        ).delete(synchronize_session=False)
    PracticeSession.query.filter_by(member_id=user_id).update(
        {'member_id': None}, synchronize_session=False
    )

    # Remove uploads owned by this member and retain other uploads by clearing
    # the deleted reviewer's optional reference.
    ExamUpload.query.filter_by(member_id=user_id).delete(
        synchronize_session=False
    )
    ExamUpload.query.filter_by(reviewer_id=user_id).update(
        {'reviewer_id': None}, synchronize_session=False
    )
    ICPrepWebhookLog.query.filter_by(member_id=user_id).delete(
        synchronize_session=False
    )

    # Delete hosted workshops and their dependent rows. If this user only
    # created a workshop hosted by somebody else, retain it with no creator.
    hosted_workshop_ids = [
        row[0]
        for row in db.session.query(Workshop.id).filter_by(
            officer_id=user_id
        ).all()
    ]
    attendance_submission_filter = AttendanceSubmission.officer_id == user_id
    reminder_filter = ReminderLog.user_id == user_id
    if hosted_workshop_ids:
        attendance_submission_filter = attendance_submission_filter | (
            AttendanceSubmission.workshop_id.in_(hosted_workshop_ids)
        )
        reminder_filter = reminder_filter | (
            ReminderLog.workshop_id.in_(hosted_workshop_ids)
        )
    AttendanceSubmission.query.filter(attendance_submission_filter).delete(
        synchronize_session=False
    )
    ReminderLog.query.filter(reminder_filter).delete(
        synchronize_session=False
    )
    db.session.execute(
        workshop_signups.delete().where(workshop_signups.c.user_id == user_id)
    )
    if hosted_workshop_ids:
        db.session.execute(
            workshop_signups.delete().where(
                workshop_signups.c.workshop_id.in_(hosted_workshop_ids)
            )
        )
        Workshop.query.filter(
            Workshop.id.in_(hosted_workshop_ids)
        ).delete(synchronize_session=False)
    Workshop.query.filter_by(creator_id=user_id).update(
        {'creator_id': None}, synchronize_session=False
    )

    # Delete member progress only after rows that can reference commitments.
    AHAttendance.query.filter_by(user_id=user_id).delete(
        synchronize_session=False
    )
    WSAttendance.query.filter_by(user_id=user_id).delete(
        synchronize_session=False
    )
    ChecklistItem.query.filter_by(user_id=user_id).delete(
        synchronize_session=False
    )
    AnnualICPrepTracker.query.filter_by(member_id=user_id).delete(
        synchronize_session=False
    )
    Commitment.query.filter_by(user_id=user_id).delete(
        synchronize_session=False
    )
    MentorPod.query.filter(
        (MentorPod.member_id == user_id) | (MentorPod.mentor_id == user_id)
    ).delete(synchronize_session=False)
    GeneralAttendance.query.filter(
        (GeneralAttendance.officer_id == user_id)
        | (
            db.func.lower(GeneralAttendance.member_name)
            == username.lower()
        )
    ).delete(synchronize_session=False)

    # Preserve target-side audit history, but actor rows cannot survive because
    # actor_id is required. Pod edit logs require both user references.
    MDPAuditLog.query.filter_by(target_user_id=user_id).update(
        {'target_user_id': None}, synchronize_session=False
    )
    MDPAuditLog.query.filter_by(actor_id=user_id).delete(
        synchronize_session=False
    )
    MentorPodEditLog.query.filter(
        (MentorPodEditLog.member_id == user_id)
        | (MentorPodEditLog.actor_id == user_id)
    ).delete(synchronize_session=False)

    db.session.delete(user)
    # Surface any missed constraint for this specific account before continuing
    # to the next test account; the surrounding transaction still stays atomic.
    db.session.flush()


@app.route('/admin')
@login_required
@admin_required
def admin_panel():
    users = User.query.order_by(User.role, User.username).all()
    stats = {
        'total_users': User.query.count(),
        'officers': User.query.filter_by(has_officer_access=True).count(),
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
    try:
        _delete_user_account_and_dependencies(user)
        db.session.commit()
    except Exception as exc:
        db.session.rollback()
        print(f'[Admin Delete User] failed for {username}: {type(exc).__name__}: {exc}')
        flash(f'Could not delete user "{username}". Check the Railway logs.', 'danger')
        return redirect(url_for('admin_panel'))
    flash(f'User "{username}" deleted successfully.', 'success')
    return redirect(url_for('admin_panel'))


@app.route('/admin/delete_test_users', methods=['POST'])
@login_required
@admin_required
def admin_delete_test_users():
    """Delete accounts that look like test accounts (Officer1, Presentation1, etc.)"""
    test_prefixes = ['officer', 'member', 'test', 'presentation']
    deleted = []
    try:
        for user in User.query.all():
            if user.id == current_user.id:
                continue
            lower = user.username.lower()
            if any(
                lower.startswith(prefix)
                and lower[len(prefix):].isdigit()
                for prefix in test_prefixes
            ):
                deleted.append(user.username)
                _delete_user_account_and_dependencies(user)
        db.session.commit()
    except Exception as exc:
        db.session.rollback()
        print(f'[Delete Test Users] failed: {type(exc).__name__}: {exc}')
        flash('Test accounts could not be deleted. Check the Railway logs.', 'danger')
        return redirect(url_for('admin_panel'))
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
    if user.is_admin:
        user.is_admin = False
        user.role = 'officer' if user.has_officer_access else 'member'
        status = 'removed'
    else:
        # Preserve whether the account was an officer before the legacy role
        # field is normalized to 'officer' for admin compatibility.
        user.has_officer_access = user.role == 'officer'
        user.is_admin = True
        user.role = 'officer'
        status = 'granted'
    db.session.commit()
    flash(f'Admin access {status} for "{user.username}".', 'success')
    return redirect(url_for('admin_panel'))


@app.route('/admin/reset_password/<int:user_id>', methods=['POST'])
@login_required
@admin_required
def admin_reset_password(user_id):
    user = User.query.get_or_404(user_id)
    if not user.email:
        flash(
            f'No email is stored for "{user.username}". Add an email before sending a reset link.',
            'warning',
        )
    elif send_password_reset_email(user):
        flash(f'Password reset link sent to {user.email}.', 'success')
    else:
        flash('The reset email could not be sent. Check the server logs.', 'danger')
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
        user.has_officer_access = user.role == 'officer'
        user.is_admin = True
        if user.role != 'officer':
            user.role = 'officer'
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
        for user in User.query.filter_by(role='member')
        .order_by(User.username)
        .all()
    ]

    form.mentor_id.choices = [
        (user.id, user.username)
        for user in User.query.filter(
            User.has_officer_access.is_(True)
        )
        .order_by(User.username)
        .all()
    ]

    # Keep the existing add-assignment backend available,
    # even though the Add Mentor Pod form is hidden from the page.
    if form.validate_on_submit():
        existing = MentorPod.query.filter_by(
            member_id=form.member_id.data
        ).first()

        if existing:
            flash(
                'That member already has a mentor-pod assignment.',
                'warning',
            )
            return redirect(url_for('mentor_pods', view='edit'))

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
            current_user.id,
            'pod_add',
            'pod',
            target_user_id=pod.member_id,
            details=f'Added to Pod {pod.pod_number}',
        )

        db.session.commit()
        flash('Mentor assignment saved.', 'success')
        return redirect(url_for('mentor_pods', view='edit'))

    # This section must be outside the form.validate_on_submit() block.
    pods = MentorPod.query.options(
        joinedload(MentorPod.mentor),
        joinedload(MentorPod.member),
    ).order_by(
        MentorPod.mentor_id,
        MentorPod.member_id,
    ).all()

    grouped_pods = defaultdict(list)
    for pod in pods:
        grouped_pods[pod.mentor].append(pod)

    assigned_member_ids = {pod.member_id for pod in pods}

    unassigned_query = User.query.filter(
        User.role == 'member'
    )

    if assigned_member_ids:
        unassigned_query = unassigned_query.filter(
            ~User.id.in_(assigned_member_ids)
        )

    unassigned_members = unassigned_query.order_by(
        User.username
    ).all()



    active_pod_view = request.args.get('view', 'finalized')
    if active_pod_view not in {'finalized', 'edit'}:
        active_pod_view = 'finalized'

    return render_template(
        'mentor_pods.html',
        form=form,
        pods=pods,
        grouped_pods=dict(grouped_pods),
        event_choices=EVENT_TABS,
        active_pod_view=active_pod_view,
        unassigned_members=unassigned_members,
    )





@app.route('/admin/mentor_pods/move/<int:pod_id>', methods=['POST'])
@login_required
@admin_required
def move_pod_member(pod_id):
    pod = MentorPod.query.get_or_404(pod_id)
    data = request.get_json(silent=True) or {}

    try:
        destination_mentor_id = int(data.get('mentor_id'))
        destination_pod_number = int(data.get('pod_number'))
    except (TypeError, ValueError):
        return jsonify({'error': 'Invalid destination pod.'}), 400

    destination = MentorPod.query.filter_by(
        mentor_id=destination_mentor_id,
        pod_number=destination_pod_number,
    ).first()

    if not destination:
        return jsonify({'error': 'Destination pod was not found.'}), 404

    old_mentor = pod.mentor.username if pod.mentor else 'Unknown'
    old_pod_number = pod.pod_number

    pod.mentor_id = destination_mentor_id
    pod.pod_number = destination_pod_number

    log_mdp_action(
        current_user.id,
        'pod_move',
        'pod',
        target_user_id=pod.member_id,
        details=(
            f'Moved from {old_mentor} Pod {old_pod_number} '
            f'to {destination.mentor.username} Pod {destination_pod_number}'
        ),
    )

    db.session.commit()

    return jsonify({
        'success': True,
        'message': f'{pod.member.username} was moved successfully.',
    })

@app.route('/admin/mentor_pods/assign/<int:member_id>', methods=['POST'])
@login_required
@admin_required
def assign_pod_member(member_id):
    member = User.query.get_or_404(member_id)

    if MentorPod.query.filter_by(member_id=member.id).first():
        flash('That member already has a mentor assignment.', 'warning')
        return redirect(url_for('mentor_pods', view='edit'))

    mentor_id = request.form.get('mentor_id', type=int)

    mentor = User.query.filter(
        User.id == mentor_id,
        User.has_officer_access.is_(True),
    ).first()

    if not mentor:
        flash('Please select a valid mentor.', 'danger')
        return redirect(url_for('mentor_pods', view='edit'))

    experience_level = request.form.get('experience_level', 'N')
    if experience_level not in {'N', 'E'}:
        experience_level = 'N'

    event = request.form.get('event', 'NA').strip().upper()
    if event not in EVENT_TABS:
        event = 'NA'

    existing_mentor_assignment = MentorPod.query.filter_by(
        mentor_id=mentor.id
    ).first()

    internal_pod_number = (
        existing_mentor_assignment.pod_number
        if existing_mentor_assignment
        else 1
    )

    member.is_competing = (
        request.form.get('is_competing', 'yes') == 'yes'
    )

    pod = MentorPod(
        pod_number=internal_pod_number,
        mentor_id=mentor.id,
        member_id=member.id,
        experience_level=experience_level,
        event=event,
        year_in_deca='',
    )

    db.session.add(pod)

    log_mdp_action(
        current_user.id,
        'pod_add',
        'pod',
        target_user_id=member.id,
        details=f'Assigned to mentor {mentor.username}',
    )

    db.session.commit()

    flash(
        f'{member.username} was assigned to {mentor.username}.',
        'success',
    )
    return redirect(url_for('mentor_pods', view='edit'))


@app.route('/admin/mentor_pods/edit/<int:pod_id>', methods=['POST'])
@login_required
@admin_required
def edit_pod(pod_id):
    pod = MentorPod.query.get_or_404(pod_id)

    new_mentor_id = request.form.get(
        'mentor_id',
        pod.mentor_id,
        type=int,
    )

    new_mentor = User.query.filter_by(
        id=new_mentor_id,
        has_officer_access=True,
    ).first()

    if not new_mentor:
        flash('The selected mentor does not have officer access.', 'danger')
        return redirect(url_for('mentor_pods', view='edit'))

    old_mentor_name = pod.mentor.username if pod.mentor else 'Unassigned'

    pod.mentor_id = new_mentor.id

    # Pod numbers are handled internally and are no longer shown or edited.
    destination_assignment = MentorPod.query.filter(
        MentorPod.mentor_id == new_mentor.id,
        MentorPod.id != pod.id,
    ).first()

    pod.pod_number = (
        destination_assignment.pod_number
        if destination_assignment
        else 1
    )

    experience_level = request.form.get(
        'experience_level',
        pod.experience_level,
    )
    if experience_level in {'N', 'E'}:
        pod.experience_level = experience_level

    event = request.form.get('event', pod.event or '').strip().upper()
    if event in EVENT_TABS:
        pod.event = event

    if pod.member and 'is_competing' in request.form:
        pod.member.is_competing = (
            request.form.get('is_competing') == 'yes'
        )

    log_mdp_action(
        current_user.id,
        'pod_edit',
        'pod',
        target_user_id=pod.member_id,
        details=(
            f'Updated mentor from {old_mentor_name} to '
            f'{new_mentor.username}; event={pod.event}; '
            f'experience={pod.experience_level}; '
            f'competing={pod.member.is_competing if pod.member else "unknown"}'
        ),
    )

    db.session.commit()
    flash(f'Changes saved for {pod.member.username}.', 'success')
    return redirect(url_for('mentor_pods', view='edit'))




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
    if not (is_officer_view() or is_admin_view()):
        return jsonify({'success': False, 'message': 'Only officers/admins can update checklist items.'}), 403
    user_id = request.form.get('user_id', type=int)
    event = request.form.get('event', '').strip()
    item_name = request.form.get('item_name', '').strip()
    requested_completed = _bool_from_form(request.form.get('completed'))
    if not user_id or not event or not item_name:
        return jsonify({'success': False, 'message': 'Missing checklist item details.'}), 400
        # Officers may update only members assigned to their own pod.
    if is_officer_view():
        assigned_to_officer = MentorPod.query.filter_by(
            mentor_id=current_user.id,
            member_id=user_id,
        ).first()

        if not assigned_to_officer:
            return jsonify({
                'success': False,
                'message': 'That member is not assigned to your pod.',
            }), 403

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

@app.route('/admin/mentor_pods/create_demo_pods', methods=['POST'])
@login_required
@admin_required
def create_demo_pods():
    officers = User.query.filter(
        User.has_officer_access.is_(True)
    ).order_by(User.username).all()

    officers_without_pods = [
        officer
        for officer in officers
        if not MentorPod.query.filter_by(
            mentor_id=officer.id
        ).first()
    ]

    if not officers_without_pods:
        flash('Every officer already has at least one mentee.', 'info')
        return redirect(url_for('mentor_pods'))

    next_test_number = 1

    def next_test_username():
        nonlocal next_test_number

        while User.query.filter_by(
            username=f'test{next_test_number}'
        ).first():
            next_test_number += 1

        username = f'test{next_test_number}'
        next_test_number += 1
        return username

    created_members = []

    for officer in officers_without_pods:
        demo_profiles = [
            ('N', 'EIP'),
            ('E', 'PSE'),
        ]

        for experience_level, event in demo_profiles:
            demo_member = User(
                username=next_test_username(),
                password=generate_password_hash(
                    os.urandom(32).hex()
                ),
                role='member',
                is_admin=False,
                has_officer_access=False,
                is_competing=False,
                must_change_password=True,
            )

            db.session.add(demo_member)
            db.session.flush()

            db.session.add(MentorPod(
                pod_number=1,
                mentor_id=officer.id,
                member_id=demo_member.id,
                experience_level=experience_level,
                year_in_deca='Demo',
                event=event,
            ))

            created_members.append(demo_member)

    db.session.commit()

    # Give the demo accounts normal commitment rows for dashboard visuals.
    for demo_member in created_members:
        ensure_commitments(demo_member)

    flash(
        f'Created {len(created_members)} demo mentees for '
        f'{len(officers_without_pods)} officer(s).',
        'success',
    )
    return redirect(url_for('mentor_pods'))




@app.route('/admin/import_commitments', methods=['POST'])
@login_required
@admin_required
def admin_import_commitments():
    """Temporarily accept an MDP workbook and repair commitment progress."""
    if not is_mdp_upload_enabled():
        abort(404)

    # Keep accidental uploads bounded without changing limits for other routes.
    if request.content_length and request.content_length > 25 * 1024 * 1024:
        flash('The workbook is too large. The maximum upload size is 25 MB.', 'danger')
        return redirect(url_for('admin_member_commitments'))

    form = MDPWorkbookUploadForm()
    if not form.validate_on_submit():
        for messages in form.errors.values():
            for message in messages:
                flash(message, 'danger')
        return redirect(url_for('admin_member_commitments'))

    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix='.xlsx', delete=False) as temporary_file:
            temporary_path = temporary_file.name
        form.workbook.data.save(temporary_path)

        stats = import_mdp_tracking_workbook(
            temporary_path,
            commitments_only=True,
        )
        log_mdp_action(
            current_user.id,
            'workbook_import',
            'member_commitments',
            details=(
                f"Matched {stats['matched_members']}; "
                f"commitments {stats['commitments_updated']}; "
                f"unmatched {len(stats['unmatched_members'])}; "
                f"missing completion {len(stats['members_without_completion'])}"
            ),
        )
        db.session.commit()
    except Exception as exc:
        db.session.rollback()
        print(f'[MDP Admin Import] failed: {type(exc).__name__}: {exc}')
        flash('The import failed. Check the Railway deployment logs for details.', 'danger')
        return redirect(url_for('admin_member_commitments'))
    finally:
        if temporary_path:
            try:
                os.remove(temporary_path)
            except FileNotFoundError:
                pass

    flash(
        f"Import complete: matched {stats['matched_members']} members and updated "
        f"{stats['commitments_updated']} commitment rows.",
        'success',
    )
    if stats['unmatched_members']:
        flash(
            'Unmatched workbook members: ' + ', '.join(stats['unmatched_members']),
            'warning',
        )
    if stats['members_without_completion']:
        flash(
            'Members without completion rows: '
            + ', '.join(stats['members_without_completion']),
            'warning',
        )
    return redirect(url_for('admin_member_commitments'))


@app.route('/member_commitments')
@login_required
def admin_member_commitments():
    if not (is_officer_view() or is_admin_view()):
        flash('Only officers/admins can view this.', 'danger')
        return redirect(url_for('dashboard'))
    members = _members_visible_to_current_user()
    member_ids = [member.id for member in members]
    members_by_username = {member.username: member for member in members}
    pods_by_member = {}
    commitments_by_user = defaultdict(list)
    checklist_by_user_event = defaultdict(dict)
    today = datetime.now(LOCAL_TZ).date()

    if member_ids:
        for pod in MentorPod.query.options(joinedload(MentorPod.mentor)).filter(
            MentorPod.member_id.in_(member_ids)
        ).all():
            pods_by_member.setdefault(pod.member_id, pod)
        for commitment in Commitment.query.filter(
            Commitment.user_id.in_(member_ids)
        ).all():
            commitments_by_user[commitment.user_id].append(commitment)
        for commitment in Commitment.query.filter(
            Commitment.user_id.is_(None),
            Commitment.member_name.in_(list(members_by_username)),
        ).all():
            member = members_by_username.get(commitment.member_name)
            if member:
                commitments_by_user[member.id].append(commitment)
        for item in ChecklistItem.query.filter(
            ChecklistItem.user_id.in_(member_ids)
        ).all():
            checklist_by_user_event[(item.user_id, item.event)][item.item_name] = bool(
                item.completed
            )

    rows = []
    for member in members:
        is_competing = member.is_competing is not False
        pod = pods_by_member.get(member.id)
        level = pod.experience_level if pod else 'N'
        event = pod.event if pod else None
        commitments = commitments_by_user.get(member.id, [])
        commitments_by_conf = {row.event: row for row in commitments}
        conferences = {}
        total_done = total_required = 0
        for conference in CONFERENCE_ORDER:
            commitment = commitments_by_conf.get(conference)
            if not commitment:
                conferences[conference] = None
                continue
            done = (
                commitment.required_roleplay - commitment.remaining_roleplay
                + commitment.required_exam - commitment.remaining_exam
                + commitment.required_written - commitment.remaining_written
            )
            required = (
                commitment.required_roleplay
                + commitment.required_exam
                + commitment.required_written
            )
            total_done += done
            total_required += required
            conferences[conference] = {
                'roleplay_done': commitment.required_roleplay - commitment.remaining_roleplay,
                'roleplay_req': commitment.required_roleplay,
                'roleplay_display': (
                    'N/A' if commitment.required_roleplay == 0 else
                    f'{commitment.required_roleplay - commitment.remaining_roleplay}/{commitment.required_roleplay}'
                ),
                'exam_done': commitment.required_exam - commitment.remaining_exam,
                'exam_req': commitment.required_exam,
                'exam_display': (
                    'N/A' if commitment.required_exam == 0 else
                    f'{commitment.required_exam - commitment.remaining_exam}/{commitment.required_exam}'
                ),
                'written_done': commitment.required_written - commitment.remaining_written,
                'written_req': commitment.required_written,
                'written_display': (
                    'N/A' if commitment.required_written == 0 else
                    f'{commitment.required_written - commitment.remaining_written}/{commitment.required_written}'
                ),
                'deadline': commitment.deadline,
                'grade': commitment.grade,
                'complete': (
                    commitment.remaining_roleplay
                    + commitment.remaining_exam
                    + commitment.remaining_written
                ) == 0,
                'checklist': {
                    name: checklist_by_user_event.get(
                        (member.id, conference), {}
                    ).get(name, False)
                    for name in CHECKLIST_ITEMS.get(conference, [])
                },
            }
        overall_pct = round(total_done / total_required * 100, 1) if total_required else 0
        incomplete = (
            is_competing
            and any(
                value is not None and not value['complete']
                for value in conferences.values()
            )
        )
        status = get_commitment_status(
            member,
            commitments=commitments,
            today=today,
        )
        rows.append({
            'member': member,
            'mentor_name': (
                pod.mentor.username if pod and pod.mentor else 'Unassigned'
            ),
            'level': level,
            'event': event,
            'is_competing': is_competing,
            'status': status,
            'status_label': (
                'Non-Compete' if status == 'non_compete'
                else 'At Risk' if status == 'at_risk'
                else 'On Track'
            ),
            'conferences': conferences,
            'overall_pct': overall_pct,
            'commitments_incomplete': incomplete,
        })
    total_members = len(rows)
    stats = {
        'total_members': total_members,
        'at_risk_count': sum(row['status'] == 'at_risk' for row in rows),
        'non_compete_count': sum(row['status'] == 'non_compete' for row in rows),
        'incomplete_count': sum(row['commitments_incomplete'] for row in rows),
        'avg_progress': round(sum(row['overall_pct'] for row in rows) / total_members, 1) if total_members else 0,
    }
    return render_template(
        'admin_member_commitments.html',
        rows=rows,
        stats=stats,
        checklist_items_by_conf=CHECKLIST_ITEMS,
        mdp_upload_enabled=is_mdp_upload_enabled(),
        mdp_upload_form=(
            MDPWorkbookUploadForm()
            if is_admin_view() and is_mdp_upload_enabled()
            else None
        ),
    )


@app.route('/checklist_completion')
@login_required
def checklist_completion():
    if not (is_officer_view() or is_admin_view()):
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
        is_competing = member.is_competing is not False
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
        if not is_competing:
            display_status = 'non_compete'
            display_status_label = 'Non-Compete'
            overdue_items = []
            deadline_safe = True
        else:
            display_status = written['status']
            display_status_label = written['status_label']
            overdue_items = written['overdue_items']
            deadline_safe = written['deadline_safe']
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
            'is_competing': is_competing,
            'status': display_status,
            'status_label': display_status_label,
            'checklists': {event: completed},
            'missing_items': written['missing_items'],
            'overdue_items': overdue_items,
            'deadline_safe': deadline_safe,
            'grades': conference['grades'],
        }
        rows.append(row)
        grouped_rows[event].append(row)
    written_stats = {
        'total_members': len(rows),
        'complete_count': sum(row['status'] == 'complete' for row in rows),
        'needs_attention_count': sum(row['status'] == 'needs_attention' for row in rows),
        'overdue_count': sum(row['status'] == 'overdue' for row in rows),
        'non_compete_count': sum(row['status'] == 'non_compete' for row in rows),
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

