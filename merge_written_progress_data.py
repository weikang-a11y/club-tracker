#!/usr/bin/env python3
"""Safely copy Written Progress metadata from the original local SQLite DB.

This script does not replace the target database. It creates a timestamped backup,
then upserts checklist requirements, per-member checklist completion, full RP/WR/EX
commitment data, conference grades, recovery emails, competing state, and mentor-pod
event labels by username.
"""
from __future__ import annotations

import argparse
import shutil
import sqlite3
from datetime import datetime
from pathlib import Path


def table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def columns(conn: sqlite3.Connection, table: str) -> set[str]:
    if not table_exists(conn, table):
        return set()
    return {row[1] for row in conn.execute(f'PRAGMA table_info("{table}")')}


def user_map(conn: sqlite3.Connection) -> dict[str, int]:
    return {
        row[1].strip().lower(): row[0]
        for row in conn.execute('SELECT id, username FROM "user"')
        if row[1]
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        '--source',
        default='/Users/aryahisharma/my_code/deca-tracker-main/club.db',
        help='Original repo SQLite database',
    )
    parser.add_argument(
        '--target',
        default='club.db',
        help='Friend2 repo SQLite database (default: ./club.db)',
    )
    args = parser.parse_args()

    source_path = Path(args.source).expanduser().resolve()
    target_path = Path(args.target).expanduser().resolve()
    if not source_path.exists():
        raise SystemExit(f'Source database not found: {source_path}')
    if not target_path.exists():
        raise SystemExit(
            f'Target database not found: {target_path}\n'
            'Run python3 app.py once in the friend2 clone so the database and new tables are created.'
        )

    stamp = datetime.now().strftime('%Y%m%d-%H%M%S')
    backup = target_path.with_name(f'{target_path.name}.backup-{stamp}')
    shutil.copy2(target_path, backup)
    print(f'Backup created: {backup}')

    source = sqlite3.connect(source_path)
    target = sqlite3.connect(target_path)
    source.row_factory = sqlite3.Row
    target.row_factory = sqlite3.Row

    required_target_tables = {'user', 'commitment', 'mentor_pod', 'checklist_item', 'checklist_requirement'}
    missing = [name for name in required_target_tables if not table_exists(target, name)]
    if missing:
        raise SystemExit(
            'Target database is missing new tables: ' + ', '.join(missing)
            + '. Run the merged app once, stop it, then rerun this script.'
        )

    source_users = user_map(source)
    target_users = user_map(target)
    target_usernames_by_id = {
        row[0]: row[1]
        for row in target.execute('SELECT id, username FROM "user"')
        if row[1]
    }
    reverse_source_users = {value: key for key, value in source_users.items()}

    counts = {
        'requirements': 0,
        'checklist_items': 0,
        'commitments': 0,
        'ah_attendance': 0,
        'ws_attendance': 0,
        'grades': 0,
        'emails': 0,
        'competing': 0,
        'pod_events': 0,
        'skipped_users': 0,
    }

    # Checklist catalog.
    if table_exists(source, 'checklist_requirement'):
        for row in source.execute(
            'SELECT event, item_name, deadline FROM checklist_requirement'
        ):
            existing = target.execute(
                'SELECT id FROM checklist_requirement WHERE event=? AND item_name=?',
                (row['event'], row['item_name']),
            ).fetchone()
            if existing:
                target.execute(
                    'UPDATE checklist_requirement SET deadline=? WHERE id=?',
                    (row['deadline'], existing['id']),
                )
            else:
                target.execute(
                    'INSERT INTO checklist_requirement(event, item_name, deadline) VALUES (?, ?, ?)',
                    (row['event'], row['item_name'], row['deadline']),
                )
            counts['requirements'] += 1
    else:
        print('Warning: source has no checklist_requirement table.')

    # Per-member checklist completion, mapped by username.
    if table_exists(source, 'checklist_item'):
        for row in source.execute(
            'SELECT user_id, event, item_name, completed FROM checklist_item'
        ):
            username = reverse_source_users.get(row['user_id'])
            target_user_id = target_users.get(username or '')
            if not target_user_id:
                counts['skipped_users'] += 1
                continue
            existing = target.execute(
                'SELECT id FROM checklist_item WHERE user_id=? AND event=? AND item_name=?',
                (target_user_id, row['event'], row['item_name']),
            ).fetchone()
            if existing:
                target.execute(
                    'UPDATE checklist_item SET completed=? WHERE id=?',
                    (row['completed'], existing['id']),
                )
            else:
                target.execute(
                    'INSERT INTO checklist_item(user_id, event, item_name, completed) VALUES (?, ?, ?, ?)',
                    (target_user_id, row['event'], row['item_name'], row['completed']),
                )
            counts['checklist_items'] += 1

    # AH/WS attendance records, mapped by username and session date. Existing
    # friend2-only dates are preserved; matching dates are updated from source.
    for table_name, count_key in (
        ('ah_attendance', 'ah_attendance'),
        ('ws_attendance', 'ws_attendance'),
    ):
        source_columns = columns(source, table_name)
        target_columns = columns(target, table_name)
        required_columns = {'user_id', 'session_date', 'value'}
        if not required_columns.issubset(source_columns):
            print(f'Warning: source has no usable {table_name} table.')
            continue
        if not required_columns.issubset(target_columns):
            print(f'Warning: target has no usable {table_name} table.')
            continue
        for row in source.execute(
            f'SELECT user_id, session_date, value FROM {table_name}'
        ):
            username = reverse_source_users.get(row['user_id'], '')
            target_user_id = target_users.get(username)
            if not target_user_id:
                counts['skipped_users'] += 1
                continue
            existing = target.execute(
                f'SELECT id FROM {table_name} WHERE user_id=? AND session_date=?',
                (target_user_id, row['session_date']),
            ).fetchone()
            if existing:
                target.execute(
                    f'UPDATE {table_name} SET value=? WHERE id=?',
                    (row['value'], existing['id']),
                )
            else:
                target.execute(
                    f'INSERT INTO {table_name}(user_id, session_date, value) VALUES (?, ?, ?)',
                    (target_user_id, row['session_date'], row['value']),
                )
            counts[count_key] += 1

    # Email and competing state by username.
    source_user_columns = columns(source, 'user')
    target_user_columns = columns(target, 'user')
    select_fields = ['id', 'username']
    if 'email' in source_user_columns:
        select_fields.append('email')
    if 'is_competing' in source_user_columns:
        select_fields.append('is_competing')
    for row in source.execute('SELECT ' + ', '.join(select_fields) + ' FROM "user"'):
        username = (row['username'] or '').strip().lower()
        target_user_id = target_users.get(username)
        if not target_user_id:
            continue
        if 'email' in row.keys() and row['email'] and 'email' in target_user_columns:
            target.execute(
                'UPDATE "user" SET email=COALESCE(NULLIF(email, \'\'), ?) WHERE id=?',
                (row['email'], target_user_id),
            )
            counts['emails'] += 1
        if 'is_competing' in row.keys() and 'is_competing' in target_user_columns:
            target.execute(
                'UPDATE "user" SET is_competing=? WHERE id=?',
                (row['is_competing'], target_user_id),
            )
            counts['competing'] += 1

    # Pod event labels by member username. Existing friend2 mentor assignments are preserved.
    source_pod_columns = columns(source, 'mentor_pod')
    target_pod_columns = columns(target, 'mentor_pod')
    if 'event' in source_pod_columns and 'event' in target_pod_columns:
        for row in source.execute('SELECT member_id, event FROM mentor_pod'):
            username = reverse_source_users.get(row['member_id'])
            target_user_id = target_users.get(username or '')
            if not target_user_id or not row['event']:
                continue
            result = target.execute(
                'UPDATE mentor_pod SET event=? WHERE member_id=?',
                (row['event'], target_user_id),
            )
            counts['pod_events'] += result.rowcount

    # Full conference commitment data by username + event. The earlier merge copied
    # only grades, which left every RP/WR/EX completion count at its default value.
    source_commitment_columns = columns(source, 'commitment')
    target_commitment_columns = columns(target, 'commitment')
    commitment_fields = [
        'required_roleplay',
        'required_written',
        'required_exam',
        'remaining_roleplay',
        'remaining_written',
        'remaining_exam',
        'deadline',
        'grade',
    ]
    required_source_fields = {'member_name', 'event', *commitment_fields}
    required_target_fields = {'member_name', 'event', 'user_id', *commitment_fields}
    if (
        required_source_fields.issubset(source_commitment_columns)
        and required_target_fields.issubset(target_commitment_columns)
    ):
        select_fields = ['user_id', 'member_name', 'event', *commitment_fields]
        for row in source.execute(
            'SELECT ' + ', '.join(select_fields) + ' FROM commitment'
        ):
            username = (row['member_name'] or '').strip().lower()
            if not username and row['user_id'] is not None:
                username = reverse_source_users.get(row['user_id'], '')
            target_user_id = target_users.get(username)
            event = (row['event'] or '').strip()
            if not target_user_id or not event:
                counts['skipped_users'] += 1
                continue

            existing = target.execute(
                'SELECT id FROM commitment WHERE user_id=? AND event=?',
                (target_user_id, event),
            ).fetchone()
            canonical_member_name = target_usernames_by_id[target_user_id]
            values = [row[field] for field in commitment_fields]
            if existing:
                assignments = ', '.join(
                    f'{field}=?' for field in commitment_fields
                )
                target.execute(
                    f'UPDATE commitment SET member_name=?, {assignments} WHERE id=?',
                    [canonical_member_name, *values, existing['id']],
                )
            else:
                column_names = [
                    'member_name', 'event', 'user_id', *commitment_fields
                ]
                columns_sql = ', '.join(column_names)
                placeholders = ', '.join('?' for _ in column_names)
                target.execute(
                    f'INSERT INTO commitment ({columns_sql}) VALUES ({placeholders})',
                    [canonical_member_name, event, target_user_id, *values],
                )
            counts['commitments'] += 1
            if row['grade'] is not None and str(row['grade']).strip():
                counts['grades'] += 1
    else:
        print('Warning: source or target commitment table is missing RP/WR/EX columns.')
    target.commit()
    source.close()
    target.close()

    print('Data merge complete:')
    for key, value in counts.items():
        print(f'  {key}: {value}')
    print('The target database was updated in place; its backup remains available above.')


if __name__ == '__main__':
    main()
