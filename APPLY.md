# Fix: two more officer-to-mentee conversions

## What the collision meant

`riday.appannagari` and `ruby.han` exist as officer accounts and also appear
in the mentee TSV. Officers survive the member wipe, so creating them as
mentees would have hit the unique constraint — the script stopped before
touching anything, which is what it is meant to do.

Neither is on the 2026-27 officer roster. ("Han" in the roster is Hanna Li, a
different person.) So both are last year's officers who are mentees now,
the same situation as Isabella Yu.

Both are now in `DEMOTE_TO_MEMBER`, so their accounts are converted in place:
role set to member, admin and officer access cleared, password reset to
`DECA2026!` with a forced change. Converting rather than deleting and
recreating keeps their user id, so audit-log entries and any history that
points at them stay intact.

**If either is actually a current officer who should keep officer access,
tell me instead of running this** — the fix would be to remove their row
from the TSV rather than demote them.

## Verified on a copy of your database

Seeded to match production: isabella.yu, riday.appannagari, ruby.han and
both huang accounts all present as officers.

```
[Accounts] rename check: lizzie.huang id=29; elizabeth.huang exists id=171
[Accounts] deleted lizzie.huang (elizabeth.huang already exists)
[Accounts] purged 126 test/demo account(s)
Created/updated 173 member(s)

isabella.yu        role=member admin=False officer_access=False pod=yes
riday.appannagari  role=member admin=False officer_access=False pod=yes
ruby.han           role=member admin=False officer_access=False pod=yes

members 173 | pods 173 | commitments 519 | AH 0 | lizzie.huang gone
admin 5xx = none
```

Dry run now reports no USERNAME COLLISIONS and no UNRESOLVED MENTORS.

## What to do

1. Copy these files over the repo, commit, push.
2. **Back up the Railway Postgres database.**
3. Dry run — confirm no collision or unresolved-mentor block appears:

   ```powershell
   python import_members_2026_27.py roster.tsv
   ```

4. Apply:

   ```powershell
   python import_members_2026_27.py roster.tsv --apply
   ```

5. Reload the site. Members appear immediately; no redeploy needed.

If a new collision appears for someone else, send it over — it means another
officer account overlaps the mentee roster, and the same one-line fix applies.
