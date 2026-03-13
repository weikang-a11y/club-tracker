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
from sqlalchemy.pool import NullPool
import os

app = Flask(__name__)
app.config['SECRET_KEY'] = 'super-secret-key-change-me-98765'
<<<<<<< HEAD

# Database config with Railway/Postgres support + local SQLite fallback
DATABASE_URL = os.getenv('DATABASE_URL')
if DATABASE_URL:
    if DATABASE_URL.startswith('postgres://'):
        DATABASE_URL = DATABASE_URL.replace('postgres://', 'postgresql+psycopg://', 1)
    elif DATABASE_URL.startswith('postgresql://'):
        DATABASE_URL = DATABASE_URL.replace('postgresql://', 'postgresql+psycopg://', 1)
    print(f"[DB] Using external database: {DATABASE_URL[:60]}...")
    app.config['SQLALCHEMY_DATABASE_URI'] = DATABASE_URL
    app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {'poolclass': NullPool}
else:
    local_db = os.path.join(os.path.dirname(__file__), 'club.db')
    print('[DB] No DATABASE_URL found, using local SQLite database.')
    app.config['SQLALCHEMY_DATABASE_URI'] = f'sqlite:///{local_db}'

app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
=======
# Read database URL from environment (Railway / cloud)
DATABASE_URL = os.getenv("DATABASE_URL")

if DATABASE_URL:
    # Convert old postgres schemes to psycopg driver
    if DATABASE_URL.startswith("postgres://"):
        DATABASE_URL = DATABASE_URL.replace(
            "postgres://",
            "postgresql+psycopg://",
            1
        )
    elif DATABASE_URL.startswith("postgresql://"):
        DATABASE_URL = DATABASE_URL.replace(
            "postgresql://",
            "postgresql+psycopg://",
            1
        )

    print(f"[DB] Using external database: {DATABASE_URL[:60]}...")

    app.config["SQLALCHEMY_DATABASE_URI"] = DATABASE_URL

else:
    # Local development fallback
    local_db = os.path.join(os.path.dirname(__file__), "club.db")

    print("[DB] No DATABASE_URL found, using local SQLite database.")

    app.config["SQLALCHEMY_DATABASE_URI"] = f"sqlite:///{local_db}"

# Common SQLAlchemy settings
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
>>>>>>> f07cca205f3685850226e6dd293b63c0b2496f73

db = SQLAlchemy(app)
login_manager = LoginManager(app)
login_manager.login_view = 'login'

TIME_SLOTS = [
    ("15:00", "3:00 - 3:20 pm"),
    ("15:20", "3:20 - 3:40 pm"),
    ("15:40", "3:40 - 4:00 pm"),
]
ACTIVITY_TYPES = ['Roleplay', 'Written Presentation', 'Exam']

class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(50), unique=True, nullable=False)
    password = db.Column(db.String(200), nullable=False)
    role = db.Column(db.String(20), nullable=False)

class Commitment(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    member_name = db.Column(db.String(100))
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

@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))

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

class CommitmentForm(FlaskForm):
    member_name = StringField('Member Name', [DataRequired()])
    required_roleplay = IntegerField('Required Roleplay', [DataRequired(), NumberRange(min=0)])
    required_written = IntegerField('Required Written Presentation', [DataRequired(), NumberRange(min=0)])
    required_exam = IntegerField('Required Exam', [DataRequired(), NumberRange(min=0)])
    deadline = DateField('Deadline', [DataRequired()])
    submit = SubmitField('Save Commitment')

class WorkshopForm(FlaskForm):
    name = StringField('Workshop Name', [DataRequired()])
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
        if field.data == 0:
            raise ValidationError('Please select an officer.')

with app.app_context():
    db.create_all()

    # Lightweight schema updates for existing databases
    engine_name = db.engine.dialect.name
    cols = {c['name'] for c in db.inspect(db.engine).get_columns('workshop')}
    if 'creator_id' not in cols:
        try:
            db.session.execute(sql_text('ALTER TABLE workshop ADD COLUMN creator_id INTEGER'))
            db.session.commit()
        except Exception:
            db.session.rollback()


def friendly_slot(dt):
    if not dt:
        return 'N/A'
    date_part = dt.strftime('%Y-%m-%d')
    time_str = dt.strftime('%H:%M')
    for value, label in TIME_SLOTS:
        if value == time_str:
            return f"{date_part} {label}"
    return dt.strftime('%Y-%m-%d %I:%M %p')

app.jinja_env.filters['friendly_slot'] = friendly_slot

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

    if current_user.role == 'officer':
        commitments = Commitment.query.filter_by(user_id=current_user.id).order_by(Commitment.deadline).all()
        assigned_workshops = Workshop.query.filter_by(officer_id=current_user.id).options(joinedload(Workshop.officer)).order_by(Workshop.time).all()
        workshops = assigned_workshops
        attendance_locked_ids = {row.workshop_id for row in AttendanceSubmission.query.filter_by(officer_id=current_user.id).all()}

        member_names = {c.member_name for c in commitments}
        for member_name in member_names:
            member = User.query.filter_by(username=member_name).first()
            if member:
                actual_attended = db.session.query(workshop_signups).filter_by(user_id=member.id, attended=True).join(Workshop).filter(Workshop.officer_id == current_user.id).count()
                ga = GeneralAttendance.query.filter_by(officer_id=current_user.id, member_name=member_name).first()
                manual_count = ga.manual_count if ga else 0
                total_attended = actual_attended + manual_count
                workshop_attendance_data.append({
                    'member_name': member_name,
                    'total_attended': total_attended,
                    'manual_count': manual_count,
                    'actual_attended': actual_attended
                })
    else:
        commitments = Commitment.query.filter_by(member_name=current_user.username).all()
        workshops = Workshop.query.options(joinedload(Workshop.officer), joinedload(Workshop.creator)).order_by(Workshop.time).all()
        created_workshops = Workshop.query.filter_by(creator_id=current_user.id).options(joinedload(Workshop.officer)).order_by(Workshop.time).all()

        if commitments:
            c = commitments[0]
            progress_summary = {
                'roleplay': f"{c.required_roleplay - c.remaining_roleplay}/{c.required_roleplay}",
                'written': f"{c.required_written - c.remaining_written}/{c.required_written}",
                'exam': f"{c.required_exam - c.remaining_exam}/{c.required_exam}",
                'deadline': c.deadline.strftime('%Y-%m-%d') if c.deadline else 'N/A'
            }

        attended_count = db.session.query(workshop_signups).filter_by(
            user_id=current_user.id,
            attended=True
        ).count()

        total_signed = len(current_user.workshops.all())

        attendance_summary = {
            'signed': total_signed,
            'attended': attended_count,
            'rate': round((attended_count / 18 * 100) if 18 > 0 else 0.0, 1)
        }

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

    return render_template('dashboard.html', commitments=commitments, progress_summary=progress_summary,
                           attendance_summary=attendance_summary, assigned_workshops=assigned_workshops,
                           workshop_attendance_data=workshop_attendance_data, workshops=workshops,
                           my_signups=my_signups, mentees_workshops=mentees_workshops,
                           signed_times=signed_times, user=current_user, created_workshops=created_workshops,
                           attendance_locked_ids=attendance_locked_ids)

@app.route('/register', methods=['GET', 'POST'])
def register():
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
    form = LoginForm()
    if form.validate_on_submit():
        user = User.query.filter_by(username=form.username.data.strip()).first()
        if user and check_password_hash(user.password, form.password.data):
            login_user(user)
            return redirect(url_for('dashboard'))
        flash('Invalid username or password. Please try again.', 'danger')
    return render_template('login.html', form=form)

@app.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('login'))

@app.route('/add_commitment', methods=['GET', 'POST'])
@login_required
def add_commitment():
    if current_user.role != 'officer':
        flash('Only officers can add commitments.', 'danger')
        return redirect(url_for('dashboard'))
    form = CommitmentForm()
    if form.validate_on_submit():
        member_name = form.member_name.data.strip()
        commit = Commitment(member_name=member_name,
            required_roleplay=form.required_roleplay.data,
            required_written=form.required_written.data,
            required_exam=form.required_exam.data,
            remaining_roleplay=form.required_roleplay.data,
            remaining_written=form.required_written.data,
            remaining_exam=form.required_exam.data,
            deadline=form.deadline.data,
            user_id=current_user.id)
        db.session.add(commit)
        db.session.commit()
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
        workshop_time = datetime.strptime(f"{form.workshop_date.data} {form.slot.data}:00", '%Y-%m-%d %H:%M:%S')
        ws = Workshop(name=form.name.data.strip(), time=workshop_time, officer_id=form.officer_id.data,
                      activity_type=form.activity_type.data, creator_id=current_user.id)
        db.session.add(ws)
        db.session.commit()
        flash('Workshop added.', 'success')
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
    if len(workshop.signups) > 0:
        flash('This workshop cannot be deleted because members have already signed up.', 'warning')
        return redirect(url_for('add_workshop'))
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
                member = User.query.get(user_id)
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
        user = User.query.get(record.user_id)
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
        day = ws.time.strftime('%Y-%m-%d')
        end_dt = ws.time + timedelta(minutes=20)
        time_range = f"{ws.time.strftime('%I:%M').lstrip('0')} - {end_dt.strftime('%I:%M').lstrip('0')} {end_dt.strftime('%p').lower()}"
        calendar_groups.setdefault(day, []).append({'workshop': ws, 'time_range': time_range})
    return render_template('reports.html', reports_data=reports_data, active_tab=active_tab, calendar_groups=calendar_groups, attendance_locked_ids=attendance_locked_ids)

if __name__ == '__main__':
    app.run(debug=True)
