import sys
import re
import difflib
from collections import defaultdict
from datetime import datetime

import pandas as pd

from app import (
    app,
    db,
    User,
    MentorPod,
    Commitment,
    ChecklistRequirement,
    ChecklistItem,
    EVENT_REQUIREMENTS,
)

CONFERENCES = ['VCMC', 'SVCDC', 'SCDC']

CONFERENCE_COLUMNS = {
    'VCMC': {
        'roleplay': 'Roleplays: 1 or N/A',
        'exam': 'Exams: 2 or N/A',
        'written': 'Written Presentation: 1 or N/A',
    },
    'SVCDC': {
        'roleplay': 'Roleplays: 1',
        'exam': 'Exams: 2',
        'written': 'Written Presentation: 1',
    },
    'SCDC': {
        'roleplay': 'Roleplays: 2 or 1',
        'exam': 'Exams: 2 or 1',
        'written': 'Written Presentation: 1',
    },
}

EVENT_TABS = ['BOR', 'EIP', 'EFB', 'EIB', 'ESB', 'IBP', 'IMC', 'PM', 'PSE', 'NA']

MANUAL_OVERRIDES = {
    "Amber Chang": "ambery.chang",
    "Chun Ka Yu": "chunka.yu",
    "Lincoln Tran": "lincolnjacob.tran",
    "Lucas Wang": "lucasj.wang",
    "Ruhaan Parandekar": "r.parandekar",
    "Sidharth Swaminathan": "s.swaminathan",
    "Srinivasan Satagopan": "sk.satagopan",
    "Varshini Subramanian": "v.subramanian",
    "Yen-Nhi Tran": "yennhi.tran",
}

DEADLINE_RE = re.compile(r'(?<!\d)(\d{1,2}/\d{1,2})(?!\d)')


def clean_text(value):
    if value is None or pd.isna(value):
        return ''
    return re.sub(r'\s+', ' ', str(value).replace('\n', ' ')).strip()


def norm(name):
    return clean_text(name).lower()


def event_key(value):
    return clean_text(value).upper()


def normalize_columns(df):
    df = df.copy()
    df.columns = [clean_text(col) for col in df.columns]
    return df


def name_candidates(name):
    base = norm(name)
    if not base:
        return []

    parts = base.split(' ')
    candidates = {base}

    if len(parts) >= 2:
        candidates.add('.'.join(parts))
        candidates.add(''.join(parts))
        candidates.add('_'.join(parts))
        candidates.add(f"{parts[0]}.{parts[-1]}")

    return list(candidates)


_NORMALIZED_OVERRIDES = {norm(k): v for k, v in MANUAL_OVERRIDES.items()}


def find_user(name, users_by_username):
    override = _NORMALIZED_OVERRIDES.get(norm(name))
    if override and override.lower() in users_by_username:
        return users_by_username[override.lower()]

    for candidate in name_candidates(name):
        if candidate in users_by_username:
            return users_by_username[candidate]

    return None


def find_user_by_email_or_name(row, users_by_username, users_by_email, name_columns):
    email = norm(row.get('Email'))
    if email and email in users_by_email:
        return users_by_email[email]

    for col in name_columns:
        if col in row.index:
            user = find_user(row.get(col), users_by_username)
            if user:
                return user

    return None


def find_col(columns, *wanted_names):
    wanted = {norm(name) for name in wanted_names}
    for col in columns:
        if norm(col) in wanted:
            return col
    return None


def safe_num(value):
    if value is None or pd.isna(value):
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def is_checked(value):
    if value is None or pd.isna(value):
        return False

    if isinstance(value, bool):
        return value

    try:
        return float(value) == 1.0
    except (TypeError, ValueError):
        return norm(value) in {'1', '1.0', 'true', 'yes', 'y', 'checked', 'x'}


def find_written_range(columns):
    start_idx = None
    end_idx = None

    for idx, col in enumerate(columns):
        col_key = norm(col)
        if col_key == 'scdc total progress':
            start_idx = idx + 1
        elif 'conference:' in col_key and 'vcmc' in col_key:
            end_idx = idx
            break

    if start_idx is None or end_idx is None or end_idx <= start_idx:
        return None, None

    return start_idx, end_idx


def split_written_header(header):
    text = clean_text(header)
    match = DEADLINE_RE.search(text)

    if not text or not match:
        return None, None

    deadline = match.group(1)
    item_name = DEADLINE_RE.sub('', text).strip(' -–—()')
    item_name = re.sub(r'\s+', ' ', item_name).strip()

    # Clean common trailing punctuation left after removing the date, e.g.
    # "Exec Sum. 10/30" -> "Exec Sum".
    item_name = item_name.rstrip(' .:-')

    if not item_name:
        return None, None

    return item_name, deadline


def get_written_columns(df):
    columns = list(df.columns)
    start_idx, end_idx = find_written_range(columns)

    if start_idx is None:
        return []

    written_cols = []
    for col in columns[start_idx:end_idx]:
        item_name, deadline = split_written_header(col)
        if item_name and deadline:
            written_cols.append((col, item_name, deadline))

    return written_cols


def build_event_category_map(main_df):
    event_to_category = {}

    if 'Event' not in main_df.columns or 'Event Category' not in main_df.columns:
        return event_to_category

    for _, row in main_df.iterrows():
        event = event_key(row.get('Event'))
        category = event_key(row.get('Event Category'))
        if event and category:
            event_to_category[event] = category

    return event_to_category


def row_belongs_to_tab(row_event, tab, event_to_category):
    if not row_event:
        return False

    category = event_to_category.get(row_event, row_event)
    return category == event_key(tab)


def import_checklist_requirements_and_items(xl, event_to_category, users_by_username, users_by_email):
    """Import dynamic written-page columns and per-member 0/1 completion."""
    # Requirements are generated from the spreadsheet each import. Clearing this
    # table prevents old bad columns from showing on the website.
    ChecklistRequirement.query.delete()

    requirements_seen = set()
    items_created = 0
    items_updated = 0
    requirements_created = 0
    unmatched_checklist_rows = []

    # Read each category tab once, store its item definitions, then process only
    # the rows whose actual event maps to that category.
    for tab in EVENT_TABS:
        if tab not in xl.sheet_names:
            continue

        try:
            df = normalize_columns(xl.parse(tab, header=0))
        except Exception as exc:
            print(f"[WARN] Could not read tab '{tab}' for checklist data: {exc}")
            continue

        written_cols = get_written_columns(df)
        if not written_cols:
            continue

        columns = list(df.columns)
        event_col = find_col(columns, 'Event')
        name_col = find_col(columns, 'Legal Name', 'Legal name', 'Mentee Name', 'Mentee', 'Name') or columns[0]

        if not event_col:
            print(f"[WARN] Tab '{tab}' has written columns but no Event column; skipping checklist rows.")
            continue

        for _, row in df.iterrows():
            row_event = event_key(row.get(event_col))
            if not row_belongs_to_tab(row_event, tab, event_to_category):
                continue

            user = find_user_by_email_or_name(
                row,
                users_by_username,
                users_by_email,
                [name_col, 'Legal Name', 'Legal name', 'Mentee Name', 'Mentee', 'Name'],
            )

            if not user:
                display_name = clean_text(row.get(name_col)) or clean_text(row.get('Email')) or '(blank row)'
                unmatched_checklist_rows.append(f"{display_name} [{row_event}]")
                continue

            # Create each requirement once for the actual event code, e.g. BMOR,
            # IMC, ESB. Events that share a written page/category inherit the
            # same item list, but still display under their real event name.
            for _, item_name, deadline in written_cols:
                req_key = (row_event, item_name)
                if req_key not in requirements_seen:
                    db.session.add(ChecklistRequirement(
                        event=row_event,
                        item_name=item_name,
                        deadline=deadline,
                    ))
                    requirements_seen.add(req_key)
                    requirements_created += 1

            for col, item_name, _ in written_cols:
                completed = is_checked(row.get(col))
                item = ChecklistItem.query.filter_by(
                    user_id=user.id,
                    event=row_event,
                    item_name=item_name,
                ).first()

                if item is None:
                    db.session.add(ChecklistItem(
                        user_id=user.id,
                        event=row_event,
                        item_name=item_name,
                        completed=completed,
                    ))
                    items_created += 1
                else:
                    item.completed = completed
                    items_updated += 1

    return requirements_created, items_created, items_updated, unmatched_checklist_rows


def main(xlsx_path):
    xl = pd.ExcelFile(xlsx_path)

    with app.app_context():
        all_users = User.query.all()
        users_by_username = {u.username.lower(): u for u in all_users}
        users_by_email = {u.email.lower(): u for u in all_users if u.email}
        officers_by_username = {u.username.lower(): u for u in all_users if u.role == 'officer'}

        unmatched_mentees = []
        unmatched_mentors = []
        pods_updated = 0
        commitments_updated = 0

        grades_by_name = {}
        event_to_category = {}

        try:
            main_df = normalize_columns(xl.parse('Mentee Commitment TrackingMDP', header=0))
            event_to_category = build_event_category_map(main_df)

            for _, row in main_df.iterrows():
                name = norm(row.get('Mentee Name'))
                if not name:
                    continue

                grades_by_name[name] = {
                    'MC': row.get('MC Grade'),
                    'SV': row.get('SV Grade'),
                    'SC': row.get('SC Grade'),
                }
        except Exception as exc:
            print(f"[WARN] Could not read main summary tab for grades/event categories: {exc}")

        # Dynamic written checklist import. This runs before pod import so the
        # /checklist_completion page has clean event_items immediately.
        reqs_created, checklist_created, checklist_updated, unmatched_checklist_rows = (
            import_checklist_requirements_and_items(
                xl,
                event_to_category,
                users_by_username,
                users_by_email,
            )
        )

        # Per-conference completion counts from each event/category tab.
        completion_by_name = {}

        for tab in EVENT_TABS:
            if tab not in xl.sheet_names:
                continue

            try:
                df = normalize_columns(xl.parse(tab, header=0))
            except Exception as exc:
                print(f"[WARN] Could not read tab '{tab}' for commitment data: {exc}")
                continue

            columns = list(df.columns)
            name_col = find_col(columns, 'Legal Name', 'Legal name', 'Mentee Name', 'Mentee', 'Name') or columns[0]
            event_col = find_col(columns, 'Event')

            for _, row in df.iterrows():
                row_event = event_key(row.get(event_col)) if event_col else event_key(tab)
                if event_col and not row_belongs_to_tab(row_event, tab, event_to_category):
                    continue

                name = norm(row.get(name_col))
                if not name:
                    continue

                entry = completion_by_name.setdefault(name, {})

                for conf, cols in CONFERENCE_COLUMNS.items():
                    rp_col = cols['roleplay']
                    ex_col = cols['exam']
                    wr_col = cols['written']

                    if rp_col not in df.columns:
                        continue

                    rp_done = int(round(safe_num(row.get(rp_col))))
                    ex_done = int(round(safe_num(row.get(ex_col))))
                    wr_done = int(round(safe_num(row.get(wr_col))))

                    if conf not in entry:
                        entry[conf] = {
                            'roleplay_done': rp_done,
                            'exam_done': ex_done,
                            'written_done': wr_done,
                        }
                    else:
                        entry[conf]['roleplay_done'] += rp_done
                        entry[conf]['exam_done'] += ex_done
                        entry[conf]['written_done'] += wr_done

        try:
            pods_df = normalize_columns(xl.parse('Pods (VIEW ONLY)', header=0))
        except Exception as exc:
            print(f"[ERROR] Could not read 'Pods (VIEW ONLY)': {exc}")
            sys.exit(1)

        for _, row in pods_df.iterrows():
            mentee_name = row.get('Mentee')
            legal_name = row.get('Legal name')
            mentor_name = row.get('Mentor')
            status = clean_text(row.get('Status'))
            event = event_key(row.get('Event')) or None
            pod_number = row.get('Unnamed: 0')

            if pd.isna(mentee_name) and pd.isna(legal_name):
                continue

            mentee_user = (
                find_user_by_email_or_name(row, users_by_username, users_by_email, ['Mentee', 'Legal name'])
                or find_user(mentee_name, users_by_username)
                or find_user(legal_name, users_by_username)
            )

            if not mentee_user:
                unmatched_mentees.append(clean_text(legal_name or mentee_name))
                continue

            mentor_user = find_user(mentor_name, officers_by_username)
            if not mentor_user:
                unmatched_mentors.append(f"{mentor_name} (mentor for {mentee_user.username})")
                continue

            if status == 'Non-Compete':
                mentee_user.is_competing = False
                level = 'N'
            elif status == 'Experienced':
                mentee_user.is_competing = True
                level = 'E'
            elif status == 'Novice':
                mentee_user.is_competing = True
                level = 'N'
            else:
                level = 'N'

            pod = MentorPod.query.filter_by(member_id=mentee_user.id).first()
            if not pod:
                pod = MentorPod(member_id=mentee_user.id)
                db.session.add(pod)

            pod.mentor_id = mentor_user.id
            pod.experience_level = level
            pod.event = event

            try:
                pod.pod_number = int(pod_number) if not pd.isna(pod_number) else (pod.pod_number or 0)
            except (TypeError, ValueError):
                pass

            pods_updated += 1

            lookup_key = norm(legal_name) if norm(legal_name) in completion_by_name else norm(mentee_name)
            completion = completion_by_name.get(lookup_key, {})
            grades = grades_by_name.get(lookup_key, {}) or grades_by_name.get(norm(mentee_name), {})
            grade_map = {'VCMC': grades.get('MC'), 'SVCDC': grades.get('SV'), 'SCDC': grades.get('SC')}

            for conf in CONFERENCES:
                if conf not in completion:
                    continue

                req = EVENT_REQUIREMENTS[conf].get(level, EVENT_REQUIREMENTS[conf]['N'])
                done = completion[conf]

                commitment = Commitment.query.filter_by(user_id=mentee_user.id, event=conf).first()
                if not commitment:
                    commitment = Commitment(
                        member_name=mentee_user.username,
                        event=conf,
                        user_id=mentee_user.id,
                    )
                    db.session.add(commitment)

                commitment.required_roleplay = req['roleplay']
                commitment.required_written = req['written']
                commitment.required_exam = req['exam']
                commitment.remaining_roleplay = max(0, req['roleplay'] - done['roleplay_done'])
                commitment.remaining_written = max(0, req['written'] - done['written_done'])
                commitment.remaining_exam = max(0, req['exam'] - done['exam_done'])
                commitment.deadline = datetime.strptime(
                    EVENT_REQUIREMENTS[conf]['deadline'], "%Y-%m-%d"
                ).date()

                grade = grade_map.get(conf)
                if grade is not None and not pd.isna(grade):
                    try:
                        grade_num = float(grade)
                        commitment.grade = f"{grade_num * 100:.1f}%" if grade_num <= 1 else f"{grade_num:.1f}"
                    except (TypeError, ValueError):
                        pass

                commitments_updated += 1

        db.session.commit()

        print("\n===== IMPORT COMPLETE =====")
        print(f"Pods created/updated:              {pods_updated}")
        print(f"Commitments created/updated:       {commitments_updated}")
        print(f"Checklist requirements created:    {reqs_created}")
        print(f"Checklist items created:           {checklist_created}")
        print(f"Checklist items updated:           {checklist_updated}")

        all_usernames = list(users_by_username.keys())

        print(f"\nUnmatched mentees ({len(unmatched_mentees)}) - no matching username found:")
        for name in sorted(set(unmatched_mentees)):
            if not name or name.lower() == 'nan':
                print(f"  - {name}  (blank row - ignore)")
                continue

            suggestions = set()
            for guess in name_candidates(name):
                suggestions.update(difflib.get_close_matches(guess, all_usernames, n=2, cutoff=0.6))

            if suggestions:
                print(f"  - {name}  --> did you mean: {', '.join(sorted(suggestions))}?")
            else:
                print(f"  - {name}  (no close match - probably not registered yet)")

        print(f"\nUnmatched mentors ({len(unmatched_mentors)}) - no matching officer username found:")
        for name in sorted(set(unmatched_mentors)):
            print(f"  - {name}")

        print(f"\nUnmatched checklist rows ({len(unmatched_checklist_rows)}) - no matching member user found:")
        for row_name in sorted(set(unmatched_checklist_rows))[:50]:
            print(f"  - {row_name}")
        if len(set(unmatched_checklist_rows)) > 50:
            print("  ... additional unmatched checklist rows omitted")

        print("\nReload /checklist_completion after the import finishes.")


if __name__ == '__main__':
    if len(sys.argv) < 2:
        print('Usage: python3 import_from_spreadsheet.py <path_to_xlsx>')
        sys.exit(1)

    main(sys.argv[1])

