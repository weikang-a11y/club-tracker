"""
import_members_2026_27.py — replace last year's mentees with this year's roster.

DESTRUCTIVE. Deletes every member account and all of their data, then
recreates members from the TSV. Officers, the officer roster and the MDP
audit log are left alone.

BACK UP THE DATABASE FIRST. There is no undo.

Usage
-----
Dry run (default — reports what it would do, changes nothing):

    python import_members_2026_27.py roster.tsv

Apply for real:

    python import_members_2026_27.py roster.tsv --apply

Against Railway, set DATABASE_URL to the Postgres DATABASE_PUBLIC_URL first:

    $env:DATABASE_URL = "postgresql://..."
    python import_members_2026_27.py roster.tsv --apply

Expected TSV columns
--------------------
Mentor Pod #, Mentor Name(s), Email, First Name, Last Name, Event, Status,
Level, then one column per meeting date. Date columns are ignored: attendance
starts empty and accumulates as meetings happen.
"""

import csv
import os
import re
import sys

# Importing app.py runs its startup migrations. The per-member commitment
# backfill is hundreds of round trips, which is slow and fragile over a
# remote Postgres connection — skip it; this script seeds commitments itself.
os.environ.setdefault('SKIP_STARTUP_BACKFILL', '1')
os.environ.setdefault('SKIP_DEMO_PODS', '1')

from app import (
    app,
    db,
    User,
    MDPAuditLog,
    MentorPod,
    Commitment,
    ChecklistItem,
    AHAttendance,
    WSAttendance,
    PracticeSession,
    PracticeLog,
    ExamUpload,
    Notification,
    ReminderLog,
    ensure_commitments,
    _canonical_officer_username,
    EVENT_TABS,
)
from werkzeug.security import generate_password_hash

DEFAULT_PASSWORD = 'DECA2026!'

# The TSV lists mentors by legal name; accounts often use a preferred name
# (e.g. "Elizabeth Huang" -> lizzie.huang). Map each one here. The script
# refuses to run with --apply until every mentor resolves, so no pod is
# silently orphaned and no duplicate officer account is created.
MENTOR_ALIASES = {
    # Confirmed by the chapter:
    'Elizabeth Huang': 'elizabeth.huang',   # account renamed from lizzie.huang
    # Resolved by elimination — only one account with that surname remains:
    'Xueying Bai': 'evelyn.bai',
    'Litian Gu': 'eva.gu',
    'Weichen Huang': 'jason.huang',         # the other Huang is Elizabeth
    'Chun Ka Yu': 'sophie.yu',              # Isabella Yu is now a mentee
}

# Officer accounts to rename before anything else, so their pods and history
# follow the new username.
RENAME_ACCOUNTS = {
    'lizzie.huang': 'elizabeth.huang',
}

# People who were officers last year and are mentees this year. Their account
# is converted in place rather than deleted, so audit-log references survive.
DEMOTE_TO_MEMBER = {
    'isabella.yu',
}

# Seeded/demo accounts to purge regardless of role. Member-role ones are
# already removed by the member wipe; this also catches officer-role ones such
# as Officer1, which would otherwise survive. The dry run prints every account
# these patterns match, so check that list before applying.
TEST_ACCOUNT_PATTERNS = [
    r'^test[._-]?\d*$',
    r'^member[._-]?\d+$',
    r'^officer[._-]?\d+$',
    r'^demo[._-]?\d*$',
]
VALID_EVENT_CODES = {code for code, _ in EVENT_TABS}


def event_code(raw):
    """'Finance Operations (FOR)' -> 'FOR'."""
    match = re.search(r'\(([^)]+)\)\s*$', (raw or '').strip())
    return match.group(1).strip() if match else ''


def username_from_email(email):
    """shua.cheon@warriorlife.net -> shua.cheon"""
    return (email or '').strip().split('@')[0].lower()


def read_roster(path):
    with open(path, encoding='utf-8-sig', newline='') as handle:
        rows = list(csv.DictReader(handle, delimiter='\t'))
    parsed, problems = [], []
    seen = set()
    for index, row in enumerate(rows, start=2):
        email = (row.get('Email') or '').strip()
        username = username_from_email(email)
        code = event_code(row.get('Event'))
        level = (row.get('Level') or '').strip().lower()
        if not email or not username:
            problems.append(f'line {index}: missing email')
            continue
        if username in seen:
            problems.append(f'line {index}: duplicate email {email}')
            continue
        if code and code not in VALID_EVENT_CODES:
            problems.append(f'line {index}: unknown event code {code!r}')
        seen.add(username)
        parsed.append({
            'line': index,
            'username': username,
            'email': email,
            'first': (row.get('First Name') or '').strip(),
            'last': (row.get('Last Name') or '').strip(),
            'mentor': (row.get('Mentor Name(s)') or '').strip(),
            'pod_number': (row.get('Mentor Pod #') or '').strip(),
            'event': code,
            'level': 'E' if level.startswith('exp') else 'N',
            'competing': not (row.get('Status') or '').strip().lower().startswith('non'),
        })
    return parsed, problems


def find_test_accounts():
    """Accounts matching a seeded/demo naming pattern, any role."""
    patterns = [re.compile(p, re.IGNORECASE) for p in TEST_ACCOUNT_PATTERNS]
    return sorted(
        (u for u in User.query.all()
         if any(p.match(u.username or '') for p in patterns)),
        key=lambda u: u.username.lower(),
    )


def detach_user_references(user_ids):
    """Null or remove anything pointing at these accounts so a delete works."""
    MDPAuditLog.query.filter(MDPAuditLog.actor_id.in_(user_ids)).update(
        {'actor_id': None}, synchronize_session=False)
    MDPAuditLog.query.filter(MDPAuditLog.target_user_id.in_(user_ids)).update(
        {'target_user_id': None}, synchronize_session=False)
    PracticeLog.query.filter(PracticeLog.officer_id.in_(user_ids)).update(
        {'officer_id': None}, synchronize_session=False)
    for model, column in [
        (Notification, 'user_id'),
        (ReminderLog, 'user_id'),
        (AHAttendance, 'user_id'),
        (WSAttendance, 'user_id'),
        (Commitment, 'user_id'),
        (ChecklistItem, 'user_id'),
        (ExamUpload, 'member_id'),
        (PracticeLog, 'member_id'),
        (PracticeSession, 'member_id'),
        (PracticeSession, 'reserved_for_id'),
        (PracticeSession, 'officer_id'),
        (MentorPod, 'member_id'),
        (MentorPod, 'mentor_id'),
    ]:
        model.query.filter(
            getattr(model, column).in_(user_ids)
        ).delete(synchronize_session=False)


def purge_test_accounts():
    """Remove seeded/demo accounts of any role."""
    accounts = find_test_accounts()
    if not accounts:
        return []
    ids = [u.id for u in accounts]
    names = [u.username for u in accounts]
    detach_user_references(ids)
    # Rows keyed only by name, from before user_id was populated, are missed
    # by the id-based deletes above.
    Commitment.query.filter(Commitment.member_name.in_(names)).delete(
        synchronize_session=False)
    ChecklistItem.query.filter(ChecklistItem.member_name.in_(names)).delete(
        synchronize_session=False)
    db.session.flush()
    User.query.filter(User.id.in_(ids)).delete(synchronize_session=False)
    db.session.flush()
    return names


def tidy_stale_accounts():
    """Rename or remove leftover accounts before matching mentors."""
    notes = []
    for old_name, new_name in RENAME_ACCOUNTS.items():
        source = User.query.filter_by(username=old_name).first()
        if not source:
            continue
        target = User.query.filter_by(username=new_name).first()
        if target:
            # Detach anything that points at the stale account, then remove it.
            MDPAuditLog.query.filter_by(actor_id=source.id).update(
                {'actor_id': None}, synchronize_session=False)
            MDPAuditLog.query.filter_by(target_user_id=source.id).update(
                {'target_user_id': None}, synchronize_session=False)
            Notification.query.filter_by(user_id=source.id).delete(
                synchronize_session=False)
            ReminderLog.query.filter_by(user_id=source.id).delete(
                synchronize_session=False)
            PracticeLog.query.filter_by(officer_id=source.id).update(
                {'officer_id': None}, synchronize_session=False)
            db.session.delete(source)
            notes.append(f'deleted {old_name} ({new_name} already exists)')
        else:
            source.username = new_name
            notes.append(f'renamed {old_name} -> {new_name}')
    db.session.flush()
    return notes


def wipe_members():
    """Delete every member account and everything hanging off it."""
    members = User.query.filter_by(role='member').all()
    member_ids = [m.id for m in members]
    counts = {'members': len(member_ids)}
    if not member_ids:
        return counts

    # Children first, then the pods, then the accounts themselves.
    for label, model, columns in [
        ('ah_attendance', AHAttendance, ['user_id']),
        ('ws_attendance', WSAttendance, ['user_id']),
        ('commitments', Commitment, ['user_id']),
        ('checklist_items', ChecklistItem, ['user_id']),
        ('exam_uploads', ExamUpload, ['member_id']),
        ('notifications', Notification, ['user_id']),
        ('practice_logs', PracticeLog, ['member_id']),
        ('reminder_logs', ReminderLog, ['user_id']),
        ('practice_sessions', PracticeSession, ['member_id', 'reserved_for_id']),
        ('pods', MentorPod, ['member_id']),
    ]:
        total = 0
        for column in columns:
            total += model.query.filter(
                getattr(model, column).in_(member_ids)
            ).delete(synchronize_session=False)
        counts[label] = total

    # Commitments keyed only by name, from before user_id was populated.
    names = [m.username for m in members]
    counts['commitments'] += Commitment.query.filter(
        Commitment.member_name.in_(names)
    ).delete(synchronize_session=False)

    # Any practice session left over from last year, whoever created it.
    counts['practice_sessions'] += PracticeSession.query.delete(
        synchronize_session=False
    )

    User.query.filter(User.id.in_(member_ids)).delete(synchronize_session=False)
    return counts


def main():
    args = [a for a in sys.argv[1:] if not a.startswith('--')]
    apply_changes = '--apply' in sys.argv
    if not args:
        print(__doc__)
        sys.exit(1)

    roster, problems = read_roster(args[0])

    with app.app_context():
        uri = app.config.get('SQLALCHEMY_DATABASE_URI', '')
        engine = 'POSTGRES (production)' if uri.startswith('postgres') else 'SQLITE (local)'
        print(f'\nDatabase: {engine}')
        print(f'Mode:     {"APPLY — changes will be written" if apply_changes else "DRY RUN — nothing will change"}')
        print(f'Roster:   {args[0]} — {len(roster)} member(s)\n')

        if problems:
            print('Problems in the file:')
            for problem in problems:
                print(f'  {problem}')
            print()

        # Rename first, so mentor lookups and collision checks see the new
        # usernames.
        for old_name, new_name in RENAME_ACCOUNTS.items():
            account = User.query.filter(
                db.func.lower(User.username) == old_name
            ).first()
            if account and apply_changes:
                account.username = new_name
                db.session.flush()
                print(f'[Rename] {old_name} -> {new_name}')
            elif account:
                print(f'[Rename] would rename {old_name} -> {new_name}')

        if apply_changes:
            for note in tidy_stale_accounts():
                print(f'[Accounts] {note}')

        # Resolve mentors against existing officer accounts.
        accounts = {u.username.lower(): u for u in User.query.all()}
        # On a dry run the rename has not happened yet, so show the result it
        # will produce rather than reporting a false mismatch.
        for old_name, new_name in RENAME_ACCOUNTS.items():
            if old_name in accounts and new_name not in accounts:
                accounts[new_name] = accounts[old_name]
        mentor_ids, missing_mentors = {}, {}
        for name in sorted({r['mentor'] for r in roster if r['mentor']}):
            alias = MENTOR_ALIASES.get(name)
            candidate = (alias or _canonical_officer_username(name)).lower()
            user = accounts.get(candidate)
            if user:
                mentor_ids[name] = user.id
            else:
                surname = name.split()[-1].lower()
                suggestions = sorted(
                    u for u in accounts
                    if u.endswith('.' + surname) or surname in u.split('.')[-1]
                )
                missing_mentors[name] = (candidate, suggestions)

        if missing_mentors:
            print('UNRESOLVED MENTORS — add each to MENTOR_ALIASES at the top')
            print('of this script, then re-run:\n')
            for name, (candidate, suggestions) in missing_mentors.items():
                count = sum(1 for r in roster if r['mentor'] == name)
                print(f"  '{name}': '?',   # {count} mentee(s); "
                      f"tried {candidate}")
                if suggestions:
                    print(f'        existing accounts with that surname: '
                          f'{", ".join(suggestions)}')
            print()

        # A roster username that already belongs to an officer would collide:
        # officers survive the wipe, so the insert would fail partway through.
        survivors = {
            u.username.lower(): u.role
            for u in User.query.filter(User.role != 'member').all()
        }
        collisions = sorted(
            ({r['username'] for r in roster} & set(survivors)) - DEMOTE_TO_MEMBER
        )
        demoting = sorted({r['username'] for r in roster} & DEMOTE_TO_MEMBER
                          & set(survivors))
        if demoting:
            print('Officer accounts being converted to mentee accounts:')
            for name in demoting:
                print(f'  {name}')
            print()
        if collisions:
            print('USERNAME COLLISIONS with accounts that survive the wipe:')
            for name in collisions:
                print(f'  {name}  (existing {survivors[name]} account)')
            print('  Decide whether this is the same person or a name clash,')
            print('  then either remove the row from the TSV or rename the')
            print('  existing account before applying.\n')

        test_accounts = find_test_accounts()
        if test_accounts:
            by_role = {}
            for account in test_accounts:
                by_role.setdefault(account.role or '?', []).append(account.username)
            print(f'Test/demo accounts to purge ({len(test_accounts)}):')
            for role, names in sorted(by_role.items()):
                shown = ', '.join(names[:8])
                more = f' … +{len(names) - 8} more' if len(names) > 8 else ''
                print(f'  {role}: {shown}{more}')
            print()

        existing_members = User.query.filter_by(role='member').count()
        print(f'Will DELETE {existing_members} existing member account(s) and all their data.')
        print(f'Will CREATE {len(roster)} member account(s), password {DEFAULT_PASSWORD}, '
              f'forced change at first login.')
        print('Attendance starts empty; commitments are seeded per level and event.\n')

        if not apply_changes:
            print('Dry run complete. Re-run with --apply to make these changes.')
            print('BACK UP THE DATABASE FIRST — this cannot be undone.')
            return

        if missing_mentors or collisions:
            print('REFUSING TO APPLY — resolve the issues listed above first.')
            sys.exit(1)

        # 1. Purge seeded/demo accounts of any role, then remove last year.
        purged = purge_test_accounts()
        if purged:
            print(f'\n[Accounts] purged {len(purged)} test/demo account(s)')

        counts = wipe_members()
        db.session.flush()
        # Bulk deletes leave stale objects in the identity map; clearing it
        # keeps the inserts below clean.
        db.session.expunge_all()
        print('\nDeleted:')
        for key, value in counts.items():
            print(f'  {key:20s} {value}')

        # 2. Create this year.
        created = 0
        converted = []
        for entry in roster:
            existing = User.query.filter(
                db.func.lower(User.username) == entry['username']
            ).first()
            if existing is not None:
                # A former officer returning as a mentee: reuse the row so
                # audit-log references to them stay valid.
                existing.role = 'member'
                existing.is_admin = False
                existing.has_officer_access = False
                existing.email = entry['email']
                existing.is_competing = entry['competing']
                existing.password = generate_password_hash(DEFAULT_PASSWORD)
                existing.must_change_password = True
                member = existing
                db.session.flush()
            else:
                member = User(
                    username=entry['username'],
                    password=generate_password_hash(DEFAULT_PASSWORD),
                    role='member',
                    email=entry['email'],
                    is_admin=False,
                    has_officer_access=False,
                    is_competing=entry['competing'],
                    must_change_password=True,
                )
                db.session.add(member)
                db.session.flush()
            mentor_id = mentor_ids.get(entry['mentor'])
            if mentor_id:
                db.session.add(MentorPod(
                    mentor_id=mentor_id,
                    member_id=member.id,
                    pod_number=int(entry['pod_number']) if entry['pod_number'].isdigit() else 0,
                    experience_level=entry['level'],
                    event=entry['event'],
                ))
            ensure_commitments(member)
            created += 1

        # Final sweep: anything still keyed to a name that is no longer a
        # member is left over from a previous year.
        current_names = {entry['username'] for entry in roster}
        orphans = [row for row in Commitment.query.all()
                   if row.member_name not in current_names]
        stale_items = [row for row in ChecklistItem.query.all()
                       if row.member_name and row.member_name not in current_names]
        for row in orphans + stale_items:
            db.session.delete(row)
        if orphans or stale_items:
            print(f'Swept {len(orphans)} orphan commitment(s), '
                  f'{len(stale_items)} orphan checklist item(s)')

        db.session.commit()
        if converted:
            print(f'\nConverted to member in place: {", ".join(converted)}')
        print(f'\nCreated/updated {created} member(s).')
        print(f'Members now: {User.query.filter_by(role="member").count()}')
        print(f'Pods now:    {MentorPod.query.count()}')
        print(f'Commitments: {Commitment.query.count()}')
        print(f'AH records:  {AHAttendance.query.count()} (expected 0)')


if __name__ == '__main__':
    main()
