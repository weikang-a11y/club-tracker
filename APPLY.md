# Performance update — no behaviour changes

Four files. `mentee_detail.html` is included because it never reached git
(that was the TemplateNotFound on Railway); make sure `git add -A` picks it
up, not `git add app.py`.

```powershell
git add -A
git status          # expect: app.py, Procfile, requirements.txt modified;
                    # templates/mentee_detail.html new
git commit -m "Performance: connection pooling, gunicorn workers, bulk-load risk report"
git push
```

## 1. Gunicorn concurrency (the big one)

`Procfile` was `web: gunicorn app:app` — a single sync worker, so one
request was served at a time and everyone else queued. That is why the site
felt fine alone and slow with several people on it. Now:

```
web: gunicorn app:app --workers 2 --threads 4 --timeout 60 --access-logfile -
```

Roughly 8 concurrent requests instead of 1. `--access-logfile -` sends
request logs to Railway so slow pages are visible in future.

## 2. Database connection pooling

`poolclass: NullPool` meant a fresh TCP + TLS + auth handshake to Postgres
on every single request. Replaced with a small pool:

```python
{'pool_size': 5, 'max_overflow': 5, 'pool_recycle': 280, 'pool_pre_ping': True}
```

`pool_pre_ping` detects connections the server has dropped, which is the
problem NullPool was working around — so the original concern is still
handled, without the per-request handshake. With 2 workers this is at most
20 connections, well inside Postgres limits.

## 3. at_risk_report N+1 query

`build_mentee_risk_report` queried per member inside its loop: pod,
AH records, WS records, pod again, commitments, checklist items, pod a third
time. It now bulk-loads all of that keyed by member id before the loop.

**1,017 queries -> 5** for 127 members.

The per-member logic below the loop is untouched. `get_attendance_stats` and
`get_mentor_name` gained optional prefetch parameters that default to the
original queries, so every other caller behaves exactly as before. A
sentinel is used for `pod` so an explicit `pod=None` (member genuinely has
no pod) is honoured rather than triggering a fallback query.

## Verification

- Report output compared row by row before and after against a 127-member
  fixture with varied attendance, grades, deadlines and checklist progress,
  covering at_risk / needs_attention / non_compete: **0 differences** across
  every field and nested dict.
- `get_attendance_stats` and `get_mentor_name` produce identical results via
  the default and prefetch paths across 40 members, including a member with
  no pod.
- All routes exercised as admin, officer and member: no 5xx.

## Note on the reminder job

With 2 workers the scheduler runs in both. Duplicate emails are prevented by
the unique constraint on `ReminderLog(practice_session_id, user_id)`: the
second worker's insert fails, it rolls back and skips the send. Safe, just
slightly redundant. Worth revisiting if you scale past 2 workers.

## Still open

- `MDP_UPLOAD_ENABLED=1` for the monthly workbook upload; needs one
  end-to-end test.
- Phase 3 team features.
- `/settings` is member-only, so officers and admins cannot set a
  notification email.
- Audit items: practice-type badges compare against 'Roleplay' instead of
  'In-Person Roleplay'; redundant `/mentee-progress`; orphan templates
  `add_commitment.html` and `register.html`; unlinked
  `/admin/make_first_admin`; `/change-password` has no link.
