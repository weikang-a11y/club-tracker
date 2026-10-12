#!/usr/bin/env python3
"""
Import All-Hands attendance from the 2026-27 TSV spreadsheet.

Usage:
    python import_ah_attendance_2026_27.py --dry-run   # preview only
    python import_ah_attendance_2026_27.py --apply     # write to database

The TSV columns are:
  0: Mentor Pod #
  1: Mentor Name(s)
  2: Email (e.g. shua.cheon@warriorlife.net)
  3: First Name
  4: Last Name
  5: Event
  6: Status
  7: Level
  8: Roleplay
  9: Exam
  10+: Session dates (9/16, 9/23, 9/30, 10/7, ...)

Values: 1 = present, 0.5 = excused/late, 0 = absent
"""

import sys
import os
import csv
from datetime import date, datetime

# ── Locate the TSV ──────────────────────────────────────────────────────────
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
TSV_PATH = os.path.join(SCRIPT_DIR, 'All-Hands_Workshop_Attendance_2026-27_-_AH_Attendance.tsv')

# Allow overriding via env var for Railway one-off runs
TSV_PATH = os.environ.get('AH_TSV_PATH', TSV_PATH)

# ── Date parsing ─────────────────────────────────────────────────────────────
def parse_session_date(date_str: str) -> date | None:
    """
    Convert a header like '9/16' or '1/6' to a date object.
    Academic year logic: month >= 7 → 2026, month < 7 → 2027.
    """
    date_str = date_str.strip()
    if not date_str:
        return None
    try:
        parts = date_str.split('/')
        if len(parts) != 2:
            return None
        month, day = int(parts[0]), int(parts[1])
        year = 2026 if month >= 7 else 2027
        return date(year, month, day)
    except (ValueError, IndexError):
        return None


def run(apply: bool):
    # ── Bootstrap Flask app ──────────────────────────────────────────────────
    sys.path.insert(0, SCRIPT_DIR)
    from app import app, db, User, AHAttendance

    with app.app_context():
        # ── Read header to find date columns ────────────────────────────────
        with open(TSV_PATH, newline='', encoding='utf-8') as f:
            reader = csv.reader(f, delimiter='\t')
            header = next(reader)

        FIXED_COLS = 10  # columns before the first date column
        date_columns: list[tuple[int, date]] = []
        for col_idx, col_header in enumerate(header):
            if col_idx < FIXED_COLS:
                continue
            parsed = parse_session_date(col_header)
            if parsed:
                date_columns.append((col_idx, parsed))
            elif col_header.strip():
                print(f'[WARN] Unrecognised column header at index {col_idx}: {col_header!r}')

        if not date_columns:
            print('[ERROR] No date columns found in TSV header. Aborting.')
            sys.exit(1)

        print(f'Found {len(date_columns)} session date columns:')
        for _, d in date_columns:
            print(f'  {d}')

        # ── Build email → user_id lookup ─────────────────────────────────────
        users = User.query.all()
        email_to_id: dict[str, int] = {}
        for u in users:
            if u.email:
                email_to_id[u.email.strip().lower()] = u.id

        # ── Process rows ─────────────────────────────────────────────────────
        inserted = 0
        updated = 0
        skipped_no_user = 0
        skipped_no_value = 0
        absent_skipped = 0

        with open(TSV_PATH, newline='', encoding='utf-8') as f:
            reader = csv.reader(f, delimiter='\t')
            next(reader)  # skip header
            for row_num, row in enumerate(reader, start=2):
                if len(row) < FIXED_COLS:
                    continue

                raw_email = row[2].strip().lower()
                first = row[3].strip()
                last = row[4].strip()

                user_id = email_to_id.get(raw_email)
                if not user_id:
                    print(f'[SKIP] Row {row_num}: no user found for email {raw_email!r} ({first} {last})')
                    skipped_no_user += 1
                    continue

                for col_idx, session_date in date_columns:
                    if col_idx >= len(row):
                        continue
                    raw_val = row[col_idx].strip()
                    if raw_val == '' or raw_val == '0':
                        # Don't store absent records — saves space; get_attendance_stats counts missing as 0
                        absent_skipped += 1
                        continue

                    try:
                        value = float(raw_val)
                    except ValueError:
                        print(f'[WARN] Row {row_num} col {col_idx}: unrecognised value {raw_val!r}')
                        skipped_no_value += 1
                        continue

                    if value <= 0:
                        absent_skipped += 1
                        continue

                    # Upsert
                    existing = AHAttendance.query.filter_by(
                        user_id=user_id, session_date=session_date
                    ).first()

                    if existing:
                        if existing.value != value:
                            if apply:
                                existing.value = value
                                db.session.add(existing)
                            print(f'  [UPDATE] {raw_email} {session_date} → {value}  (was {existing.value})')
                            updated += 1
                        # else already correct — silent
                    else:
                        if apply:
                            rec = AHAttendance(
                                user_id=user_id,
                                session_date=session_date,
                                value=value,
                            )
                            db.session.add(rec)
                        print(f'  [INSERT] {raw_email} {session_date} = {value}')
                        inserted += 1

        if apply:
            db.session.commit()
            print(f'\n✅ Committed: {inserted} inserted, {updated} updated.')
        else:
            print(f'\n🔍 Dry-run: would insert {inserted}, update {updated}.')

        print(f'   Absent/blank rows skipped (not stored): {absent_skipped}')
        print(f'   Members not found in DB: {skipped_no_user}')
        print(f'   Bad values skipped: {skipped_no_value}')


if __name__ == '__main__':
    if '--apply' in sys.argv:
        print('=== APPLY mode — writing to database ===\n')
        run(apply=True)
    elif '--dry-run' in sys.argv:
        print('=== DRY-RUN mode — no changes will be written ===\n')
        run(apply=False)
    else:
        print('Usage: python import_ah_attendance_2026_27.py [--dry-run | --apply]')
        sys.exit(1)
