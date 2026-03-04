from flask import Flask, render_template, request, redirect, url_for, flash
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
from werkzeug.security import generate_password_hash, check_password_hash
from flask_wtf import FlaskForm
from wtforms import StringField, PasswordField, DateField, SelectField, IntegerField, TextAreaField, SubmitField
from wtforms.validators import DataRequired, Length, NumberRange
from datetime import datetime, timedelta, date
from sqlalchemy.orm import joinedload
import os
from sqlalchemy import create_engine
from sqlalchemy.pool import NullPool

app = Flask(__name__)
app.config['SECRET_KEY'] = 'super-secret-key-change-me-98765'
# Force psycopg dialect (v3) for PostgreSQL - critical for Render + Supabase
database_url = os.environ.get('DATABASE_URL')
if database_url:
    # Replace any default psycopg2 dialect with psycopg (v3)
    database_url = database_url.replace('postgresql+psycopg2', 'postgresql+psycopg')
    database_url = database_url.replace('postgres://', 'postgresql+psycopg://')  # Supabase sometimes uses postgres://
    # Ensure SSL mode (Supabase requires it)
    if 'sslmode' not in database_url:
        database_url += '?sslmode=require'
    app.config['SQLALCHEMY_DATABASE_URI'] = database_url
    app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {'poolclass': NullPool}  # prevent pooling crashes
else:
    app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///club.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

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
    role = db.Column(db.String(20))

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

    officer = db.relationship('User', foreign_keys=[officer_id], backref='hosted_workshops')

    signups = db.relationship('User', secondary='workshop_signups',
                              backref=db.backref('workshops', lazy='dynamic'))

workshop_signups = db.Table('workshop_signups',
    db.Column('workshop_id', db.Integer, db.ForeignKey('workshop.id'), primary_key=True),
    db.Column('user_id', db.Integer, db.ForeignKey('user.id'), primary_key=True),
    db.Column('attended', db.Boolean, default=False, nullable=False)
)

class GeneralAttendance(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    officer_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    member_name = db.Column(db.String(100), nullable=False)
    manual_count = db.Column(db.Integer, default=0)  # manual +1 increments

    officer = db.relationship('User', backref='general_attendances')

@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))

class RegisterForm(FlaskForm):
    username = StringField('Username', [DataRequired(), Length(min=3)])
    password = PasswordField('Password', [DataRequired(), Length(min=6)])
    role = SelectField('Role', choices=[('officer', 'Officer'), ('member', 'Member')])
    submit = SubmitField('Register')

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
    slot = SelectField('Time Slot (20 min)', [DataRequired()], choices=TIME_SLOTS)
    activity_type = SelectField('Activity Type', [DataRequired()], choices=[(t, t) for t in ACTIVITY_TYPES])
    officer_id = SelectField('Officer / Host', [DataRequired()], coerce=int)
    submit = SubmitField('Create Workshop')

with app.app_context():
    db.create_all()

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

    if current_user.role == 'officer':
        commitments = Commitment.query.filter_by(user_id=current_user.id).all()
        assigned_workshops = Workshop.query.filter_by(officer_id=current_user.id)\
                                            .options(joinedload(Workshop.officer))\
                                            .order_by(Workshop.time).all()
        workshops = assigned_workshops

        # Officer: per-member workshop attendance summary
        member_names = {c.member_name for c in commitments}
        for member_name in member_names:
            member = User.query.filter_by(username=member_name).first()
            if member:
                actual_attended = db.session.query(workshop_signups).filter_by(
                    user_id=member.id,
                    attended=True
                ).join(Workshop).filter(Workshop.officer_id == current_user.id).count()

                ga = GeneralAttendance.query.filter_by(officer_id=current_user.id, member_name=member_name).first()
                manual_count = ga.manual_count if ga else 0

                total_attended = actual_attended + manual_count

                workshop_attendance_data.append({
                    'member_name': member_name,
                    'total_attended': total_attended,
                    'manual_count': manual_count,
                    'actual_attended': actual_attended
                })

    else:  # member
        commitments = Commitment.query.filter_by(member_name=current_user.username).all()
        workshops = Workshop.query.options(joinedload(Workshop.officer)).order_by(Workshop.time).all()

        if commitments:
            c = commitments[0]  # assume one commitment per member
            progress_summary = {
                'roleplay': f"{c.required_roleplay - c.remaining_roleplay}/{c.required_roleplay}",
                'written': f"{c.required_written - c.remaining_written}/{c.required_written}",
                'exam': f"{c.required_exam - c.remaining_exam}/{c.required_exam}",
                'deadline': c.deadline.strftime('%Y-%m-%d') if c.deadline else 'N/A'
            }

            # Find the officer who assigned this commitment
            officer = c.user
            officer_id = officer.id if officer else None

            # Attended only from workshops assigned to that officer
            actual_attended = 0
            manual_count = 0
            if officer_id:
                actual_attended = db.session.query(workshop_signups).filter_by(
                    user_id=current_user.id,
                    attended=True
                ).join(Workshop).filter(Workshop.officer_id == officer_id).count()

                ga = GeneralAttendance.query.filter_by(officer_id=officer_id, member_name=current_user.username).first()
                manual_count = ga.manual_count if ga else 0

            total_attended = actual_attended + manual_count

            total_signed = len(current_user.workshops.all())
            attendance_rate = (total_attended / 18 * 100) if 18 > 0 else 0.0  # fixed 18 to match officer table

            attendance_summary = {
                'signed': total_signed,
                'attended': total_attended,
                'rate': round(attendance_rate, 1)
            }

    for ws in workshops:
        ws.end_time = ws.time + timedelta(minutes=20)

    my_signups = current_user.workshops.all() if current_user.role == 'member' else []

    mentees_workshops = {}
    if current_user.role == 'officer':
        mentee_names = {c.member_name for c in commitments}
        for mentee_name in mentee_names:
            mentee_user = User.query.filter_by(username=mentee_name).first()
            if mentee_user:
                mentees_workshops[mentee_name] = mentee_user.workshops.all()

    signed_times = [(ws.time, ws.end_time) for ws in my_signups]

    return render_template(
        'dashboard.html',
        commitments=commitments,
        progress_summary=progress_summary,
        attendance_summary=attendance_summary,
        assigned_workshops=assigned_workshops,
        workshop_attendance_data=workshop_attendance_data,
        workshops=workshops,
        my_signups=my_signups,
        mentees_workshops=mentees_workshops,
        signed_times=signed_times,
        user=current_user
    )

@app.route('/register', methods=['GET', 'POST'])
def register():
    form = RegisterForm()
    if form.validate_on_submit():
        hashed_pw = generate_password_hash(form.password.data)
        user = User(username=form.username.data, password=hashed_pw, role=form.role.data)
        db.session.add(user)
        db.session.commit()
        flash('Registered!', 'success')
        return redirect(url_for('login'))
    return render_template('register.html', form=form)

@app.route('/login', methods=['GET', 'POST'])
def login():
    form = LoginForm()
    if form.validate_on_submit():
        user = User.query.filter_by(username=form.username.data).first()
        if user and check_password_hash(user.password, form.password.data):
            login_user(user)
            return redirect(url_for('dashboard'))
        flash('Invalid credentials', 'danger')
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
        commit = Commitment(
            member_name=form.member_name.data,
            required_roleplay=form.required_roleplay.data,
            required_written=form.required_written.data,
            required_exam=form.required_exam.data,
            remaining_roleplay=form.required_roleplay.data,
            remaining_written=form.required_written.data,
            remaining_exam=form.required_exam.data,
            deadline=form.deadline.data,
            user_id=current_user.id
        )
        db.session.add(commit)
        db.session.commit()
        flash('Commitment added!', 'success')
        return redirect(url_for('dashboard'))
    return render_template('add_commitment.html', form=form)

@app.route('/add_workshop', methods=['GET', 'POST'])
@login_required
def add_workshop():
    if current_user.role != 'member':
        flash('Only members can add workshops.', 'danger')
        return redirect(url_for('dashboard'))
    form = WorkshopForm()
    officers = User.query.filter_by(role='officer').order_by(User.username).all()
    form.officer_id.choices = [(o.id, o.username) for o in officers]
    if form.validate_on_submit():
        time_str = form.slot.data
        dt_str = f"{form.workshop_date.data} {time_str}:00"
        workshop_time = datetime.strptime(dt_str, '%Y-%m-%d %H:%M:%S')
        ws = Workshop(name=form.name.data, time=workshop_time, officer_id=form.officer_id.data, activity_type=form.activity_type.data)
        db.session.add(ws)
        db.session.commit()
        flash('Workshop added!', 'success')
        return redirect(url_for('dashboard'))
    return render_template('add_workshop.html', form=form)

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
    flash('Signed up!', 'success')
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
    
    if request.method == 'POST':
        # Reset all to False first
        db.session.execute(
            workshop_signups.update()
            .where(workshop_signups.c.workshop_id == workshop_id)
            .values(attended=False)
        )
        # Set True for checked ones + update remaining counts
        for key in request.form:
            if key.startswith('attended_user_'):
                user_id = int(key.split('_')[-1])
                attended = request.form.get(key) == 'on'
                
                db.session.execute(
                    workshop_signups.update()
                    .where(workshop_signups.c.workshop_id == workshop_id)
                    .where(workshop_signups.c.user_id == user_id)
                    .values(attended=attended)
                )
                
                if attended:
                    member = User.query.get(user_id)
                    if member:
                        commitment = Commitment.query.filter_by(member_name=member.username).first()
                        if commitment:
                            if workshop.activity_type == 'Roleplay':
                                commitment.remaining_roleplay = max(0, commitment.remaining_roleplay - 1)
                            elif workshop.activity_type == 'Written Presentation':
                                commitment.remaining_written = max(0, commitment.remaining_written - 1)
                            elif workshop.activity_type == 'Exam':
                                commitment.remaining_exam = max(0, commitment.remaining_exam - 1)
                            db.session.add(commitment)
        
        db.session.commit()
        db.session.expire_all()
        db.session.close()
        flash('Attendance updated successfully!', 'success')
        return redirect(url_for('workshop_attendance', workshop_id=workshop_id))
    
    attendance_records = db.session.query(workshop_signups).filter_by(workshop_id=workshop_id).all()
    
    members_with_attendance = []
    for record in attendance_records:
        user = User.query.get(record.user_id)
        if user:
            members_with_attendance.append({
                'user': user,
                'attended': record.attended
            })
    
    return render_template(
        'attendance.html',
        workshop=workshop,
        members_with_attendance=members_with_attendance
    )

@app.route('/increment_general_attendance', methods=['POST'])
@login_required
def increment_general_attendance():
    if current_user.role != 'officer':
        flash('Only officers can update attendance.', 'danger')
        return redirect(url_for('dashboard'))

    member_name_raw = request.form.get('member_name', '')
    member_name = member_name_raw.strip().lower()  # normalize input

    print(f"[INCREMENT] Officer {current_user.username} clicked +1 for raw: '{member_name_raw}' → normalized: '{member_name}'")

    # Find or create the record (case-insensitive, trimmed match)
    ga = GeneralAttendance.query.filter(
        GeneralAttendance.officer_id == current_user.id,
        db.func.lower(GeneralAttendance.member_name) == member_name
    ).first()

    if not ga:
        # Create new record if no match found
        ga = GeneralAttendance(
            officer_id=current_user.id,
            member_name=member_name_raw.strip(),  # save original casing
            manual_count=0
        )
        db.session.add(ga)
        db.session.commit()
        print(f"[INCREMENT] Created new GeneralAttendance for '{member_name_raw.strip()}'")

    ga.manual_count += 1
    db.session.commit()

    flash('General attendance incremented!', 'success')
    print(f"[INCREMENT] Success: '{ga.member_name}' now {ga.manual_count}")

    return redirect(url_for('dashboard'))

@app.route('/reports', methods=['GET', 'POST'])
@login_required
def reports():
    if current_user.role != 'officer':
        flash('Only officers can view reports.', 'danger')
        return redirect(url_for('dashboard'))
    
    officer_workshops = Workshop.query.filter_by(officer_id=current_user.id).all()

    member_summary = {}
    for ws in officer_workshops:
        signups_count = len(ws.signups)
        attended_count = db.session.query(workshop_signups).filter_by(
            workshop_id=ws.id, attended=True
        ).count()
        for member in ws.signups:
            username = member.username
            if username not in member_summary:
                member_summary[username] = {'signups': 0, 'attended': 0, 'manual': 0}
            member_summary[username]['signups'] += 1
            if member.id in {row[0] for row in db.session.query(workshop_signups.c.user_id).filter_by(
                workshop_id=ws.id, attended=True
            ).all()}:
                member_summary[username]['attended'] += 1

    # Add manual +1 from GeneralAttendance to match dashboard "Workshop Attendance"
    gas = GeneralAttendance.query.filter_by(officer_id=current_user.id).all()
    for ga in gas:
        username = ga.member_name
        if username in member_summary:
            member_summary[username]['manual'] += ga.manual_count
        else:
            member_summary[username] = {'signups': 0, 'attended': 0, 'manual': ga.manual_count}

    reports_data = []
    for username, stats in member_summary.items():
        actual_attended = stats['attended']
        manual = stats['manual']
        total_attended = actual_attended + manual  # now matches "Workshop Attendance"
        signups = stats['signups']
        rate = (total_attended / 18 * 100) if 18 > 0 else 0  # fixed 18
        reports_data.append({
            'member': username,
            'signups_count': signups,
            'attended_count': total_attended,  # matches dashboard
            'attendance_rate': round(rate, 1)
        })

    return render_template(
        'reports.html',
        reports_data=reports_data
    )

@app.route('/force-reset-db', methods=['GET'])
def force_reset_db():
    import os
    db_path = 'club.db'
    if os.path.exists(db_path):
        try:
            os.remove(db_path)
            return "OLD club.db DELETED - DATABASE RESET SUCCESSFULLY!<br><br>Refresh the homepage now.<br><br>You can remove this /force-reset-db route from app.py now."
        except Exception as e:
            return f"Failed to delete club.db: {str(e)}"
    else:
        return "No club.db found - already fresh."
    
if __name__ == '__main__':
    app.run(debug=True)
