"""
import_mdp.py — One-time historical data import from MDP Excel sheet
=====================================================================
Imports all 4 tabs from the MDP Google Sheet export into the Flask app database.

What this script does:
  1. Creates officer (mentor) accounts for all 31 mentors
  2. Creates member accounts for all 180 members
  3. Imports All-Hands (AH) attendance for each member (Tab 1)
  4. Imports Workshop (WS) attendance for each member (Tab 2)
  5. Links members to their mentor pod (Tab 4)

Usage:
  1. Place this file in the same folder as your app.py
  2. Place the MDP Excel file in the same folder
  3. Run: python import_mdp.py

Requirements:
  pip install pandas openpyxl

Notes:
  - Attendance values: 1 = present, 0.5 = excused, 0 = absent, blank = ignored
  - All accounts are created with must_change_password=True
  - Default password for all accounts: DECA2026!
  - Usernames are generated from email (part before @warriorlife.net)
  - Running this script twice is safe — it skips existing records
"""

import os
import sys
import pandas as pd
from datetime import datetime
from werkzeug.security import generate_password_hash

# ── Point to your Excel file ─────────────────────────────────────────────────
EXCEL_FILE = 'MDP_Copy_of_All-Hands_Workshop_Attendance_Sheet_2025-26.xlsx'
DEFAULT_PASSWORD = 'DECA2026!'

# ── Bootstrap Flask app context ───────────────────────────────────────────────
# Import everything from app.py — models are already defined there
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from app import app, db, User, AHAttendance, WSAttendance, MentorPod


# ── Helpers ───────────────────────────────────────────────────────────────────

def email_to_username(email):
    """Convert warriorlife email to a clean username."""
    return email.split('@')[0].strip().lower()


def name_to_username(full_name):
    """Convert a mentor full name to a username (firstname.lastname)."""
    parts = full_name.strip().lower().split()
    if len(parts) >= 2:
        return f"{parts[0]}.{parts[-1]}"
    return parts[0]


def get_or_create_officer(full_name):
    """Find existing officer or create a new one."""
    username = name_to_username(full_name)
    user = User.query.filter_by(username=username).first()
    if not user:
        user = User(
            username=username,
            password=generate_password_hash(DEFAULT_PASSWORD),
            role='officer',
            must_change_password=True,
        )
        db.session.add(user)
        db.session.flush()
        print(f"  [+] Created officer: {full_name} → username: {username}")
    return user


def get_or_create_member(email, full_name):
    """Find existing member by email or create a new one."""
    username = email_to_username(email)
    user = User.query.filter_by(username=username).first()
    if not user:
        user = User(
            username=username,
            password=generate_password_hash(DEFAULT_PASSWORD),
            role='member',
            email=email.strip(),
            must_change_password=True,
        )
        db.session.add(user)
        db.session.flush()
        print(f"  [+] Created member: {full_name} → username: {username}")
    return user


def import_ah_attendance(row, member_user, date_cols):
    """Import All-Hands attendance rows for one member."""
    count = 0
    for col in date_cols:
        val = row[col]
        if pd.isna(val) or val == '':
            continue
        try:
            float_val = float(val)
        except (ValueError, TypeError):
            continue
        session_date = col.date() if hasattr(col, 'date') else col
        existing = AHAttendance.query.filter_by(
            user_id=member_user.id,
            session_date=session_date
        ).first()
        if not existing:
            rec = AHAttendance(
                user_id=member_user.id,
                session_date=session_date,
                value=float_val,
            )
            db.session.add(rec)
            count += 1
    return count


def import_ws_attendance(row, member_user, date_cols):
    """Import Workshop attendance rows for one member."""
    count = 0
    for col in date_cols:
        val = row[col]
        if pd.isna(val) or val == '':
            continue
        try:
            float_val = float(val)
        except (ValueError, TypeError):
            continue
        session_date = col.date() if hasattr(col, 'date') else col
        existing = WSAttendance.query.filter_by(
            user_id=member_user.id,
            session_date=session_date
        ).first()
        if not existing:
            rec = WSAttendance(
                user_id=member_user.id,
                session_date=session_date,
                value=float_val,
            )
            db.session.add(rec)
            count += 1
    return count


# ── Main import logic ─────────────────────────────────────────────────────────

def run_import():
    print("=" * 60)
    print("MDP DATA IMPORT")
    print("=" * 60)
    print(f"Excel file: {EXCEL_FILE}")
    print()

    if not os.path.exists(EXCEL_FILE):
        print(f"ERROR: File not found: {EXCEL_FILE}")
        print("Make sure the Excel file is in the same folder as this script.")
        sys.exit(1)

    xl = pd.ExcelFile(EXCEL_FILE)

    # ── Step 1: Load all sheets ───────────────────────────────────────────────
    print("Loading sheets...")
    df_ah  = pd.read_excel(xl, sheet_name='All-Hands Attendance', header=0)
    df_ws  = pd.read_excel(xl, sheet_name='Workshop Attendance ', header=0)
    df_com = pd.read_excel(xl, sheet_name='Mentee Commitment TrackingMDP', header=0)
    print(f"  AH Attendance:   {len(df_ah)} rows")
    print(f"  WS Attendance:   {len(df_ws)} rows")
    print(f"  Commitment tab:  {len(df_com)} rows")
    print()

    # ── Step 2: Get date columns from each sheet ──────────────────────────────
    ah_date_cols = [c for c in df_ah.columns if isinstance(c, datetime)]
    ws_date_cols = [c for c in df_ws.columns if isinstance(c, datetime)]
    print(f"AH sessions found:  {len(ah_date_cols)} dates from "
          f"{ah_date_cols[0].strftime('%m/%d/%Y')} to "
          f"{ah_date_cols[-1].strftime('%m/%d/%Y')}")
    print(f"WS sessions found:  {len(ws_date_cols)} dates from "
          f"{ws_date_cols[0].strftime('%m/%d/%Y')} to "
          f"{ws_date_cols[-1].strftime('%m/%d/%Y')}")
    print()

    with app.app_context():
        # Create new tables if they don't exist yet
        db.create_all()

        # ── Step 3: Create officer accounts ──────────────────────────────────
        print("-" * 40)
        print("STEP 1: Creating officer (mentor) accounts")
        print("-" * 40)
        unique_mentors = df_com['Mentor Name(s)'].dropna().unique()
        mentor_map = {}  # mentor full name -> User object
        for mentor_name in sorted(unique_mentors):
            officer = get_or_create_officer(mentor_name)
            mentor_map[mentor_name.strip()] = officer
        db.session.commit()
        print(f"  Total officers: {len(mentor_map)}")
        print()

        # ── Step 4: Create member accounts + pod links ────────────────────────
        print("-" * 40)
        print("STEP 2: Creating member accounts and pod assignments")
        print("-" * 40)
        member_map = {}  # email -> User object
        pod_count = 0
        skipped = 0

        for _, row in df_com.iterrows():
            email = str(row.get('Email', '')).strip()
            first = str(row.get('First Name', '')).strip()
            last  = str(row.get('Last Name', '')).strip()
            mentor_name = str(row.get('Mentor Name(s)', '')).strip()
            pod_num = row.get('Mentor Pod #')
            level = str(row.get('Level', '')).strip()
            year  = str(row.get('Year in DECA', '')).strip() if 'Year in DECA' in df_com.columns else ''

            if not email or email == 'nan':
                skipped += 1
                continue

            full_name = f"{first} {last}".strip()
            member = get_or_create_member(email, full_name)
            member_map[email.lower()] = member

            # Create pod link if mentor exists
            mentor = mentor_map.get(mentor_name)
            if mentor and not MentorPod.query.filter_by(member_id=member.id).first():
                pod = MentorPod(
                    pod_number=int(pod_num) if pd.notna(pod_num) else 0,
                    mentor_id=mentor.id,
                    member_id=member.id,
                    experience_level=level if level in ('N', 'E') else None,
                    year_in_deca=year if year != 'nan' else None,
                )
                db.session.add(pod)
                pod_count += 1

        db.session.commit()
        print(f"  Total members created: {len(member_map)}")
        print(f"  Pod links created:     {pod_count}")
        print(f"  Skipped (no email):    {skipped}")
        print()

        # ── Step 5: Import All-Hands attendance ───────────────────────────────
        print("-" * 40)
        print("STEP 3: Importing All-Hands (AH) attendance")
        print("-" * 40)
        ah_total = 0
        ah_skipped = 0

        for _, row in df_ah.iterrows():
            email = str(row.get('Email', '')).strip().lower()
            if not email or email == 'nan':
                continue
            member = member_map.get(email)
            if not member:
                username = email_to_username(email)
                member = User.query.filter_by(username=username).first()
            if not member:
                ah_skipped += 1
                continue
            count = import_ah_attendance(row, member, ah_date_cols)
            ah_total += count

        db.session.commit()
        print(f"  AH records imported: {ah_total}")
        print(f"  Members not found:   {ah_skipped}")
        print()

        # ── Step 6: Import Workshop attendance ────────────────────────────────
        print("-" * 40)
        print("STEP 4: Importing Workshop (WS) attendance")
        print("-" * 40)
        ws_total = 0
        ws_skipped = 0

        # Tab 2 has one row per member — drop duplicate emails keeping first
        df_ws_members = df_ws[['Email'] + ws_date_cols].copy()
        df_ws_members = df_ws_members.dropna(subset=['Email'])
        df_ws_members = df_ws_members.drop_duplicates(subset=['Email'], keep='first')

        for _, row in df_ws_members.iterrows():
            email = str(row.get('Email', '')).strip().lower()
            if not email or email == 'nan':
                continue
            member = member_map.get(email)
            if not member:
                username = email_to_username(email)
                member = User.query.filter_by(username=username).first()
            if not member:
                ws_skipped += 1
                continue
            count = import_ws_attendance(row, member, ws_date_cols)
            ws_total += count

        db.session.commit()
        print(f"  WS records imported: {ws_total}")
        print(f"  Members not found:   {ws_skipped}")
        print()

        # ── Step 7: Summary ───────────────────────────────────────────────────
        print("=" * 60)
        print("IMPORT COMPLETE - SUMMARY")
        print("=" * 60)
        print(f"  Officers created:      {User.query.filter_by(role='officer').count()}")
        print(f"  Members created:       {User.query.filter_by(role='member').count()}")
        print(f"  Pod assignments:       {MentorPod.query.count()}")
        print(f"  AH attendance records: {AHAttendance.query.count()}")
        print(f"  WS attendance records: {WSAttendance.query.count()}")
        print()
        print(f"Default password for all accounts: {DEFAULT_PASSWORD}")
        print("Username = email prefix (e.g. ambery.chang for ambery.chang@warriorlife.net)")
        print("All accounts have must_change_password=True")
        print()

        # ── Step 8: Print sample accounts ────────────────────────────────────
        print("-" * 40)
        print("SAMPLE ACCOUNTS")
        print("-" * 40)
        print("Officers (first 5):")
        for u in User.query.filter_by(role='officer').limit(5).all():
            print(f"  username: {u.username}")
        print("Members (first 5):")
        for u in User.query.filter_by(role='member').limit(5).all():
            pod = MentorPod.query.filter_by(member_id=u.id).first()
            mentor = User.query.get(pod.mentor_id) if pod else None
            print(f"  username: {u.username}  |  pod: {pod.pod_number if pod else '?'}  |  mentor: {mentor.username if mentor else '?'}")


if __name__ == '__main__':
    run_import()
