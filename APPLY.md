# Fix: connection dropped on import — second unguarded loop

## Why it failed again

There were TWO per-member loops in app.py's startup block, not one. I guarded
the first; the second (line ~2428, "fix existing commitment rows whose
required counts don't match current matrix") was untouched and kept firing
~3 queries per member on every import.

Measured queries fired just by importing app.py:

| | Before | After |
|---|---|---|
| Normal boot | 609 | 609 |
| With SKIP_STARTUP_BACKFILL=1 | 600 | **87** |

Both loops are now behind one `SKIP_STARTUP_WORK` flag, along with the
workshop backfill, officer roster import, admin reconciliation, officer
access sync, demo pods and written deadline sync.

## STEP 1 — confirm the new app.py is actually in place

Your traceback showed an unguarded call at app.py:2349, which means the
previous package's app.py never reached your folder. Before anything else:

```powershell
cd C:\Users\olivi\OneDrive\Documents\club-tracker
Select-String -Path app.py -Pattern "SKIP_STARTUP_WORK"
```

You should see several matches. If nothing prints, app.py was not copied —
extract the zip again and make sure app.py lands at the top level of the
repo folder, not in a subfolder.

Same check for the script:

```powershell
Select-String -Path import_members_2026_27.py -Pattern "SKIP_STARTUP_BACKFILL"
```

## STEP 2 — commit and push

```powershell
git add -A
git status
git commit -m "Guard both startup backfill loops; roster import script"
git push
```

## STEP 3 — back up the database

Railway -> Postgres service -> Backups. Do not skip.

## STEP 4 — dry run

```powershell
copy "$env:USERPROFILE\Downloads\All-Hands_Workshop Attendance 2026-27 - AH Attendance.tsv" roster.tsv
python import_members_2026_27.py roster.tsv
```

Check: `Database: POSTGRES (production)`, `173 member(s)`, no UNRESOLVED
MENTORS or USERNAME COLLISIONS block, and read the test-account purge list.

If it still drops the connection, use the public URL explicitly:

```powershell
$env:DATABASE_URL = "postgresql://...proxy.rlwy.net:PORT/railway"
```

## STEP 5 — apply

```powershell
python import_members_2026_27.py roster.tsv --apply
```

Expected tail:

```
Created/updated 173 member(s).
Members now: 173
Pods now:    173
Commitments: 519
AH records:  0 (expected 0)
```

519 must equal 3 x members. Reload the site — no redeploy needed for the
members to appear.

## Tested end to end on a copy of your database

```
queries at import with flag: 87 (was 600)
members 173 | pods 173 | commitments 519 | AH 0
test accounts: none | lizzie.huang gone | isabella.yu role: member
no 5xx for admin or member
```

## Everything else in this package

Test/demo account purge for any role; the six mentor mappings; zero
attendance no longer flags anyone; 2025-26 spreadsheet view retired; Delete
Test Accounts button removed; session_time widened to VARCHAR(40) with
migration; user.phone capped; AH requirement 100%; practice slot presets plus
custom entry; notification bell at top of nav; Mentor Pods search always
visible.
