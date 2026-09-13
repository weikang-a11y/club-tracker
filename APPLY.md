# Fix: duplicate rename block caused the unique-constraint failure

## Your database was not changed

The failure happened during a flush before the member wipe. No commit ran,
so the transaction rolled back. Nothing was deleted or created.

Two things did work on that run: the connection held (the startup guard is
now in place) and it reached the import logic.

## The bug

There were TWO rename blocks in the script. `tidy_stale_accounts()` checks
whether the target username is already taken and deletes the stale account
instead of renaming. But an older block left behind in `main()` ran first and
renamed unconditionally:

```python
account.username = new_name      # no check that the name is free
db.session.flush()               # -> UniqueViolation
```

In production `elizabeth.huang` already exists, so the rename collided.
That duplicate block is removed; renaming now happens only in
`tidy_stale_accounts()`.

The lookup is also case- and whitespace-insensitive now, excludes the source
row itself, prints what it found, and falls back to deleting the stale
account if a rename somehow still collides.

## Verified both paths on a copy of your database

Target already exists (matches production):

```
[Accounts] rename check: lizzie.huang id=29; elizabeth.huang exists id=169
[Accounts] deleted lizzie.huang (elizabeth.huang already exists)
Members now: 173 | Commitments: 519
```

Target does not exist:

```
[Accounts] rename check: lizzie.huang id=29; elizabeth.huang not found
[Accounts] renamed lizzie.huang -> elizabeth.huang
Members now: 173 | Commitments: 519
```

Both end with 173 members, 173 pods, 519 commitments, 0 attendance records,
no test accounts, and no 5xx for admin or member.

## What to do

1. Copy these files over the repo. Confirm app.py landed:

   ```powershell
   Select-String -Path app.py -Pattern "SKIP_STARTUP_WORK"
   ```

2. Commit and push.

3. **Back up the Railway Postgres database.**

4. Dry run:

   ```powershell
   python import_members_2026_27.py roster.tsv
   ```

   Confirm `Database: POSTGRES (production)` and `173 member(s)`.

5. Apply:

   ```powershell
   python import_members_2026_27.py roster.tsv --apply
   ```

   Watch for the `[Accounts] rename check:` line — it will now say whether
   elizabeth.huang was found, and act accordingly.

   Expected tail: 173 members, 173 pods, 519 commitments, 0 AH records.

6. Reload the site. No redeploy needed for the members to appear.
