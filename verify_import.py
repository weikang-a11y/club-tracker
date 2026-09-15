"""
verify_import.py — compare the live database against the roster TSV.

READ ONLY. Changes nothing. Run it as often as you like.

    $env:DATABASE_URL = "postgresql://...DATABASE_PUBLIC_URL..."
    python verify_import.py roster.tsv

Reports, in order:

  1. Members in the TSV but missing from the database
  2. Members in the database but NOT in the TSV  (should be none)
  3. Members whose mentor, pod number, event, level or competing status
     does not match the TSV
  4. Accounts that are neither in the TSV nor on the 2026-27 officer roster
     (last year's leftovers, e.g. officers who are no longer in the chapter)
  5. Officers with no pod, split by whether the TSV names them as a mentor
  6. Pod counts per mentor, TSV vs database
"""

import csv
import os
import re
import sys

os.environ.setdefault('SKIP_STARTUP_BACKFILL', '1')
os.environ.setdefault('SKIP_DEMO_PODS', '1')

from app import (
    app,
    db,
    User,
    MentorPod,
    Commitment,
    AHAttendance,
    WSAttendance,
    OFFICER_ROSTER_2026_27,
    _canonical_officer_username,
)

try:
    from import_members_2026_27 import MENTOR_ALIASES
except Exception:
    MENTOR_ALIASES = {}


def event_code(raw):
    match = re.search(r'\(([^)]+)\)\s*$', (raw or '').strip())
    return match.group(1).strip() if match else ''


def show(title, items, empty='none'):
    print(f'\n{title} ({len(items)})')
    if not items:
        print(f'  {empty}')
        return
    for item in items[:40]:
        print(f'  {item}')
    if len(items) > 40:
        print(f'  … and {len(items) - 40} more')


def main():
    args = [a for a in sys.argv[1:] if not a.startswith('--')]
    if not args:
        print(__doc__)
        sys.exit(1)

    with open(args[0], encoding='utf-8-sig', newline='') as handle:
        rows = list(csv.DictReader(handle, delimiter='\t'))

    expected = {}
    for row in rows:
        username = (row.get('Email') or '').strip().split('@')[0].lower()
        if not username:
            continue
        level = (row.get('Level') or '').strip().lower()
        expected[username] = {
            'mentor': (row.get('Mentor Name(s)') or '').strip(),
            'pod': (row.get('Mentor Pod #') or '').strip(),
            'event': event_code(row.get('Event')),
            'level': 'E' if level.startswith('exp') else 'N',
            'competing': not (row.get('Status') or '').strip().lower().startswith('non'),
        }

    with app.app_context():
        uri = app.config.get('SQLALCHEMY_DATABASE_URI', '')
        print(f'Database: {"POSTGRES (production)" if uri.startswith("postgres") else "SQLITE (local)"}')
        print(f'Roster:   {args[0]} — {len(expected)} member(s)')

        accounts = {u.username.lower(): u for u in User.query.all()}
        members = {u.username.lower(): u for u in User.query.filter_by(role='member').all()}
        print(f'Database: {len(members)} member(s), '
              f'{len(accounts) - len(members)} non-member account(s)')

        show('1. In TSV but MISSING from the database',
             sorted(set(expected) - set(members)))

        show('2. Members in the database but NOT in the TSV',
             sorted(set(members) - set(expected)))

        # Expected mentor username, applying the same aliases as the import.
        def mentor_username(name):
            return (MENTOR_ALIASES.get(name) or _canonical_officer_username(name)).lower()

        mismatches = []
        for username, want in sorted(expected.items()):
            member = members.get(username)
            if not member:
                continue
            pod = MentorPod.query.filter_by(member_id=member.id).first()
            got_mentor = ''
            if pod and pod.mentor_id:
                mentor = db.session.get(User, pod.mentor_id)
                got_mentor = (mentor.username if mentor else '').lower()
            want_mentor = mentor_username(want['mentor'])
            problems = []
            if not pod:
                problems.append('NO POD')
            else:
                if got_mentor != want_mentor:
                    problems.append(f'mentor {got_mentor or "none"} != {want_mentor}')
                if str(pod.pod_number) != want['pod']:
                    problems.append(f'pod {pod.pod_number} != {want["pod"]}')
                if (pod.event or '') != want['event']:
                    problems.append(f'event {pod.event or "none"} != {want["event"]}')
                if (pod.experience_level or '') != want['level']:
                    problems.append(f'level {pod.experience_level} != {want["level"]}')
            if bool(member.is_competing) != want['competing']:
                problems.append(f'competing {member.is_competing} != {want["competing"]}')
            if problems:
                mismatches.append(f'{username}: ' + '; '.join(problems))
        show('3. Members whose pod details do not match the TSV', mismatches)

        roster_usernames = {
            _canonical_officer_username(name).lower()
            for name, _ in OFFICER_ROSTER_2026_27
        }
        tsv_mentor_usernames = {
            mentor_username(want['mentor']) for want in expected.values()
        }
        stale = sorted(
            f'{u.username}  (role={u.role}, admin={bool(u.is_admin)})'
            for key, u in accounts.items()
            if key not in expected
            and key not in roster_usernames
            and key not in tsv_mentor_usernames
        )
        show('4. Accounts in neither the TSV nor the 2026-27 officer roster '
             '(last year\'s leftovers)', stale)

        # Pods whose member is not a TSV member, or whose mentor the TSV does
        # not name — both mean a stale row survived the rebuild.
        stale_pods = []
        for pod in MentorPod.query.all():
            member = db.session.get(User, pod.member_id) if pod.member_id else None
            mentor = db.session.get(User, pod.mentor_id) if pod.mentor_id else None
            member_name = (member.username if member else f'id={pod.member_id}')
            mentor_name = (mentor.username if mentor else f'id={pod.mentor_id}')
            if member_name.lower() not in expected:
                stale_pods.append(
                    f'{member_name} (role={member.role if member else "?"}) '
                    f'in {mentor_name} pod {pod.pod_number} — not a TSV member')
            elif mentor_name.lower() not in tsv_mentor_usernames:
                stale_pods.append(
                    f'{member_name} assigned to {mentor_name}, '
                    f'who is not a mentor in the TSV')
        show('4b. Pods that do not come from the TSV '
             '(e.g. an officer sitting in another officer\'s pod)', stale_pods)

        officers = User.query.filter(User.role != 'member').all()
        no_pod_mentor, no_pod_other = [], []
        for officer in officers:
            if MentorPod.query.filter_by(mentor_id=officer.id).first():
                continue
            if officer.username.lower() in tsv_mentor_usernames:
                no_pod_mentor.append(officer.username)
            else:
                no_pod_other.append(officer.username)
        show('5a. Officers the TSV names as mentors but who have NO pod '
             '(a real problem)', sorted(no_pod_mentor))
        show('5b. Officers with no pod who are not TSV mentors '
             '(advisors/exec — expected)', sorted(no_pod_other))

        print('\n6. Pod size per mentor (TSV vs database)')
        want_counts = {}
        for want in expected.values():
            want_counts[mentor_username(want['mentor'])] = \
                want_counts.get(mentor_username(want['mentor']), 0) + 1
        for mentor_name in sorted(want_counts):
            user = accounts.get(mentor_name)
            got = MentorPod.query.filter_by(mentor_id=user.id).count() if user else 0
            flag = '' if got == want_counts[mentor_name] else '   <-- MISMATCH'
            print(f'  {mentor_name:26s} tsv={want_counts[mentor_name]:3d} '
                  f'db={got:3d}{flag}')

        print(f'\nTotals: members={len(members)} pods={MentorPod.query.count()} '
              f'commitments={Commitment.query.count()} '
              f'AH={AHAttendance.query.count()} WS={WSAttendance.query.count()}')


if __name__ == '__main__':
    main()
