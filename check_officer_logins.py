"""
check_officer_logins.py — diagnose (and optionally repair) officer logins.

Run it against the SAME database the website uses. On Railway:

    railway run python check_officer_logins.py

Locally against club.db, just:

    python check_officer_logins.py

Report only, changes nothing:

    python check_officer_logins.py
    python check_officer_logins.py crystal.chen philina.chen olivia.kang

Reset the listed accounts (or all officers if none listed) to the standard
temporary passwords and force a change at next login:

    python check_officer_logins.py --reset crystal.chen
    python check_officer_logins.py --reset --all-officers
"""

import sys

from app import (
    app,
    db,
    User,
    DataMigration,
    OFFICER_IMPORT_KEY,
    OFFICER_IMPORT_ADMIN_PASSWORD,
    OFFICER_IMPORT_OFFICER_PASSWORD,
)
from werkzeug.security import check_password_hash, generate_password_hash

CANDIDATES = {
    'Admin2627!': 'admin temp',
    'Officer2627!': 'officer temp',
    'DECA2026!': 'member/reset temp',
}


def which_password_works(user):
    """Return the label of whichever known password matches, if any."""
    for value, label in CANDIDATES.items():
        try:
            if check_password_hash(user.password, value):
                return f'{value}  ({label})'
        except Exception:
            pass
    return 'none of the known defaults'


def main():
    args = [a for a in sys.argv[1:] if not a.startswith('--')]
    do_reset = '--reset' in sys.argv
    all_officers = '--all-officers' in sys.argv

    with app.app_context():
        uri = app.config.get('SQLALCHEMY_DATABASE_URI', '')
        engine = 'POSTGRES (production)' if uri.startswith('postgres') else 'SQLITE (local)'
        print(f'\nDatabase: {engine}')

        marker = db.session.get(DataMigration, OFFICER_IMPORT_KEY)
        if marker:
            print(f'Roster import: HAS RUN (recorded {marker.applied_at})')
            print('  -> It will never run again on this database.')
        else:
            print('Roster import: HAS NOT RUN on this database.')
            print('  -> Officer passwords were never set to the temp values.')

        print(f'\nExpected admin password:   {OFFICER_IMPORT_ADMIN_PASSWORD}')
        print(f'Expected officer password: {OFFICER_IMPORT_OFFICER_PASSWORD}')
        if OFFICER_IMPORT_ADMIN_PASSWORD != 'Admin2627!':
            print('  !! Overridden by OFFICER_IMPORT_ADMIN_PASSWORD in the environment.')
        if OFFICER_IMPORT_OFFICER_PASSWORD != 'Officer2627!':
            print('  !! Overridden by OFFICER_IMPORT_OFFICER_PASSWORD in the environment.')

        if all_officers:
            users = User.query.filter_by(role='officer').order_by(User.username).all()
        elif args:
            users = []
            for name in args:
                found = User.query.filter_by(username=name).first()
                if found:
                    users.append(found)
                else:
                    print(f'\n  NOT FOUND: {name}')
                    near = User.query.filter(
                        User.username.ilike(f'%{name.split(".")[-1]}%')
                    ).limit(5).all()
                    if near:
                        print('    similar usernames:',
                              ', '.join(u.username for u in near))
        else:
            users = User.query.filter_by(role='officer').order_by(User.username).all()

        print(f'\n{"username":24s} {"role":9s} {"admin":6s} {"must_chg":9s} working password')
        print('-' * 92)
        for u in users:
            print(f'{u.username:24s} {u.role or "":9s} '
                  f'{str(bool(u.is_admin)):6s} '
                  f'{str(bool(getattr(u, "must_change_password", False))):9s} '
                  f'{which_password_works(u)}')

        if not do_reset:
            print('\nReport only — nothing was changed.')
            print('Add --reset to set these accounts back to the temp passwords.')
            return

        changed = 0
        for u in users:
            temp = (OFFICER_IMPORT_ADMIN_PASSWORD if u.is_admin
                    else OFFICER_IMPORT_OFFICER_PASSWORD)
            u.password = generate_password_hash(temp)
            if hasattr(u, 'must_change_password'):
                u.must_change_password = True
            changed += 1
        db.session.commit()
        print(f'\nReset {changed} account(s). Each must set a new password at next login.')


if __name__ == '__main__':
    main()
