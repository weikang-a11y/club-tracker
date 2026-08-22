"""Import DECA tracker data from the MDP workbook.

Examples:
    python import_from_spreadsheet.py "FINAL MDP Deadline Tracking.xlsx"
    python import_from_spreadsheet.py "FINAL MDP Deadline Tracking.xlsx" --commitments-only

Spreadsheet parsing and member matching live in ``app.py`` so the website and
this command always use the same positional conference-column mapping and
requirement rules. The commitments-only mode is recommended when repairing
progress counts because it leaves pods, competing status, and checklist data
unchanged.
"""

import argparse
import os

from app import app, db, import_mdp_tracking_workbook


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('workbook', help='Path to the MDP tracking .xlsx file')
    parser.add_argument(
        '--commitments-only',
        action='store_true',
        help='Update commitment progress only; preserve pods and checklist data',
    )
    return parser.parse_args()


def main():
    args = parse_args()
    workbook_path = os.path.abspath(args.workbook)
    if not os.path.isfile(workbook_path):
        raise SystemExit(f'Workbook not found: {workbook_path}')

    with app.app_context():
        try:
            stats = import_mdp_tracking_workbook(
                workbook_path,
                commitments_only=args.commitments_only,
            )
            db.session.commit()
        except Exception:
            db.session.rollback()
            raise

    mode = 'commitment repair' if args.commitments_only else 'full import'
    print(f'MDP {mode} complete.')
    print(f"Matched members: {stats['matched_members']}")
    print(f"Commitments updated: {stats['commitments_updated']}")
    if not args.commitments_only:
        print(f"Pods updated: {stats['pods_updated']}")
        print(f"Checklist requirements imported: {stats['checklist_requirements']}")
        print(f"Checklist items updated: {stats['checklist_items_updated']}")
    if stats['unmatched_members']:
        print('Unmatched members: ' + ', '.join(stats['unmatched_members']))
    if stats['unmatched_mentors']:
        print('Unmatched mentors: ' + ', '.join(stats['unmatched_mentors']))


if __name__ == '__main__':
    main()

