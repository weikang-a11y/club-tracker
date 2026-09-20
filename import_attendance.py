"""
import_attendance.py — load AH or WS attendance from the tracking sheet.

Safe to run as often as you like. It UPSERTS: a member/date pair already in
the database is updated, never duplicated, so re-running the same file changes
nothing. Blank cells are skipped, so meetings that have not happened yet are
left alone.

Usage
-----
Dry run (default — reports what it would change, writes nothing):

    python import_attendance.py ah_attendance.tsv --type ah

Apply:

    python import_attendance.py ah_attendance.tsv --type ah --apply
    python import_attendance.py ws_attendance.tsv --type ws --apply

Against Railway:

    $env:DATABASE_URL = "postgresql://...DATABASE_PUBLIC_URL..."
    python import_attendance.py ah_attendance.tsv --type ah --apply

Expected file
-------------
Tab-separated, exported from the attendance sheet. Must contain an Email
column and one column per meeting date headed like 9/16, 10/7, 1/13.

Cell values accepted (case-insensitive):
    1, p, present, y, yes, x        -> attended (1.0)
    0.5, .5, e, ex, excused         -> excused, counts as half a meeting
    0, a, absent, n, no             -> absent (0.0)
    any decimal between 0 and 1     -> stored exactly as written
    blank                           -> skipped, meeting not recorded yet

Anything outside 0-1, or text that is not recognised, is reported and
skipped rather than guessed at.

School year
-----------
Months August–December map to ACADEMIC_START_YEAR, January–July to the year
after, so 9/16 and 1/13 land in the right calendar years. Override with
--year 2026 if needed.
"""

import csv
import os
import re
import sys
from datetime import date

os.environ.setdefault('SKIP_STARTUP_BACKFILL', '1')
os.environ.setdefault('SKIP_DEMO_PODS', '1')

from app import app, db, User, AHAttendance, WSAttendance

ACADEMIC_START_YEAR = int(os.getenv('WRITTEN_ACADEMIC_START_YEAR', '2026'))

PRESENT = {'1', '1.0', 'p', 'present', 'y', 'yes', 'x'}
ABSENT = {'0', '0.0', 'a', 'absent', 'n', 'no'}
# The sheet records an excused absence as 0.5, and the attendance column is a
# float, so partial credit is stored and averaged exactly as written.
EXCUSED_VALUE = 0.5
EXCUSED = {'e', 'ex', 'excused', '0.5', '.5'}

DATE_HEADER = re.compile(r'^\s*(\d{1,2})\s*/\s*(\d{1,2})\s*$')


def parse_header_date(header, start_year):
    """'9/16' -> date(2026, 9, 16); '1/13' -> date(2027, 1, 13)."""
    match = DATE_HEADER.match(header or '')
    if not match:
        return None
    month, day = int(match.group(1)), int(match.group(2))
    year = start_year if month >= 8 else start_year + 1
    try:
        return date(year, month, day)
    except ValueError:
        return None


def parse_value(raw):
    """Return the attendance value, None to skip, or 'BAD' if unreadable.

    Fractions are preserved: 0.5 is an excused absence and counts as half a
    meeting, matching how the sheet totals it. Rounding these to 0 or 1 would
    silently change everyone's attendance percentage.
    """
    text = (raw or '').strip().lower()
    if not text:
        return None
    if text in EXCUSED:
        return EXCUSED_VALUE
    if text in PRESENT:
        return 1.0
    if text in ABSENT:
        return 0.0
    try:
        number = float(text)
    except ValueError:
        return 'BAD'
    if 0.0 <= number <= 1.0:
        return number
    return 'BAD'


def main():
    args = [a for a in sys.argv[1:] if not a.startswith('--')]
    apply_changes = '--apply' in sys.argv
    last_wins = '--last-wins' in sys.argv
    kind = 'ah'
    if '--type' in sys.argv:
        kind = sys.argv[sys.argv.index('--type') + 1].strip().lower()
    start_year = ACADEMIC_START_YEAR
    if '--year' in sys.argv:
        start_year = int(sys.argv[sys.argv.index('--year') + 1])

    if not args or kind not in {'ah', 'ws'}:
        print(__doc__)
        sys.exit(1)

    model = AHAttendance if kind == 'ah' else WSAttendance
    label = 'All-Hands' if kind == 'ah' else 'Workshop'

    with open(args[0], encoding='utf-8-sig', newline='') as handle:
        rows = list(csv.DictReader(handle, delimiter='\t'))
    if not rows:
        print('The file has no rows.')
        sys.exit(1)

    date_columns = [
        (header, parse_header_date(header, start_year))
        for header in rows[0].keys()
        if parse_header_date(header, start_year)
    ]

    with app.app_context():
        uri = app.config.get('SQLALCHEMY_DATABASE_URI', '')
        print(f'\nDatabase: {"POSTGRES (production)" if uri.startswith("postgres") else "SQLITE (local)"}')
        print(f'Mode:     {"APPLY" if apply_changes else "DRY RUN — nothing will be written"}')
        print(f'Type:     {label} attendance')
        print(f'File:     {args[0]} — {len(rows)} row(s), '
              f'{len(date_columns)} meeting date(s)')
        if date_columns:
            print(f'Dates:    {date_columns[0][1]} … {date_columns[-1][1]}')

        members = {u.username.lower(): u for u in User.query.filter_by(role='member').all()}
        existing = {}
        for record in model.query.all():
            existing[(record.user_id, record.session_date)] = record

        created = updated = unchanged = 0
        unknown_people, bad_values, dates_seen = [], [], set()
        conflicts = []

        # Collapse the file to one value per (member, date) BEFORE touching the
        # database. A member listed on two rows would otherwise be inserted
        # twice in the same batch and trip the uq_..._user_date constraint.
        wanted = {}
        row_count = {}
        for row in rows:
            email = (row.get('Email') or '').strip()
            username = email.split('@')[0].lower()
            member = members.get(username)
            if not member:
                if email:
                    unknown_people.append(email)
                continue
            row_count[username] = row_count.get(username, 0) + 1
            for header, session_date in date_columns:
                value = parse_value(row.get(header))
                if value is None:
                    continue
                if value == 'BAD':
                    bad_values.append(f'{username} {header}={row.get(header)!r}')
                    continue
                key = (member.id, session_date)
                if key in wanted and abs(wanted[key] - value) > 1e-9:
                    conflicts.append(
                        f'{username} {header}: {wanted[key]} on one row, '
                        f'{value} on another'
                    )
                wanted[key] = value

        duplicated = sorted(name for name, n in row_count.items() if n > 1)
        if duplicated:
            print(f'\nMembers listed on more than one row ({len(duplicated)}):')
            for name in duplicated[:15]:
                print(f'  {name} — {row_count[name]} rows')
            if len(duplicated) > 15:
                print(f'  … and {len(duplicated) - 15} more')
            print('  Duplicate rows are merged; fix the sheet when you can.')

        if conflicts:
            print(f'\nCONFLICTING values for the same member and date '
                  f'({len(conflicts)}):')
            for item in conflicts[:15]:
                print(f'  {item}')
            if len(conflicts) > 15:
                print(f'  … and {len(conflicts) - 15} more')

        for (user_id, session_date), value in wanted.items():
            dates_seen.add(session_date)
            record = existing.get((user_id, session_date))
            if record is None:
                created += 1
                if apply_changes:
                    db.session.add(model(
                        user_id=user_id,
                        session_date=session_date,
                        value=value,
                    ))
            elif abs(float(record.value or 0) - value) > 1e-9:
                updated += 1
                if apply_changes:
                    record.value = value
            else:
                unchanged += 1

        if unknown_people:
            print(f'\nEmails with no matching member account ({len(unknown_people)}):')
            for email in unknown_people[:15]:
                print(f'  {email}')
            if len(unknown_people) > 15:
                print(f'  … and {len(unknown_people) - 15} more')
            print('  Add them with the Add Member form, then re-run.')

        if bad_values:
            print(f'\nCells that could not be read ({len(bad_values)}) — skipped:')
            for item in bad_values[:15]:
                print(f'  {item}')

        print(f'\nMeetings with data: {len(dates_seen)}')
        print(f'  new records:    {created}')
        print(f'  changed:        {updated}')
        print(f'  already correct:{unchanged}')

        if conflicts and not last_wins:
            print('\nREFUSING TO APPLY: the same member and date have two '
                  'different values.\nFix the sheet, or re-run with '
                  '--last-wins to accept the later row.')
            return

        if not apply_changes:
            print('\nDry run complete. Re-run with --apply to write these changes.')
            return

        try:
            db.session.commit()
        except Exception as exc:
            db.session.rollback()
            print(f'\nNOTHING WAS WRITTEN — the database rejected the batch:'
                  f'\n  {exc}')
            return
        print(f'\nWrote {created} new and {updated} updated {label} record(s).')
        print(f'{label} rows now: {model.query.count()}')


if __name__ == '__main__':
    main()
