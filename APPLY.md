# Full package — all changes, plus the roster import script

## Why the new members are not on the site yet

This zip contains the **script**, not the data. Deploying it changes nothing
on its own. The members appear only after you RUN the script against the
Railway database. Deploying and running are two separate steps.

## Step 1 — deploy the code

```powershell
git add -A
git status
git commit -m "Roster import script, test account purge, zero-attendance guard"
git push
```

## Step 2 — back up the Railway Postgres database

The import is irreversible. Do not skip this.

## Step 3 — dry run (changes nothing)

```powershell
$env:DATABASE_URL = "postgresql://...DATABASE_PUBLIC_URL..."
python import_members_2026_27.py "All-Hands_Workshop_Attendance_2026-27_-_AH_Attendance.tsv"
```

First line must read `Database: POSTGRES (production)`. If it says SQLITE,
the variable did not take and you are pointed at a local file.

Read the output. It lists every test account it will purge and every mentor
mapping. If anything looks wrong, stop.

## Step 4 — apply

```powershell
python import_members_2026_27.py "...tsv" --apply
```

Then reload the site. Members appear immediately; no redeploy needed.

## What the import does

**Purges test/demo accounts of any role** — `test1`…`test126`, `Member1`,
`Officer1`, `demo3` and anything matching those patterns. Officer-role test
accounts survive the member wipe, so they are removed explicitly. The dry
run prints the full list first.

**Deletes all member accounts** plus attendance, commitments, checklist
items, pods, practice sessions, exam uploads, notifications, practice logs
and reminder logs. Officers, the officer roster and the MDP audit log stay.

**Mentor mappings**, all confirmed by you:

| TSV mentor | Account |
|---|---|
| Elizabeth Huang | `lizzie.huang` renamed to `elizabeth.huang` |
| Weichen Huang | `jason.huang` |
| Xueying Bai | `evelyn.bai` |
| Litian Gu | `eva.gu` |
| Chun Ka Yu | `sophie.yu` |
| Isabella Yu | converted from officer to mentee in place |

Isabella is converted rather than deleted so her user id keeps working and
audit-log references survive. Lizzie is renamed for the same reason; the
script only deletes her if `elizabeth.huang` already exists, and detaches
audit references first so the delete cannot fail on a foreign key.

**Creates 173 members** — username from the email local part, password
`DECA2026!` with forced change at first login, competing status and level
from the TSV, pod and event from the TSV, commitments seeded per level and
event, attendance empty.

## Tested end to end on a copy of your database

```
test accounts left: none
members 173 | officers 39 | pods 173 | commitments 519 | AH 0
orphan pods: 0 | lizzie.huang gone | isabella.yu role: member
no 5xx for admin, officer or member
```

519 is exactly 173 x 3. An earlier run left 6 orphan commitments behind from
purged accounts whose `user_id` was NULL; the script now sweeps rows keyed
only by name, so the count comes out exact.

## Other changes included

- Zero attendance no longer flags anyone at risk.
- 2025-26 spreadsheet view retired; `?written_view=legacy` still returns 200.
- Delete Test Accounts button removed from the admin panel.
- `session_time` widened to VARCHAR(40) with a startup migration (typed slots
  like `3:00-3:20` were 500ing on Postgres); `user.phone` capped at 20.
- AH requirement at 100% with all five hardcoded "80%" strings updated.
- Practice slot presets 3:00-3:20 / 3:20-3:40 / 3:40-4:00 plus custom entry.
- Notification bell at the top of the nav; Mentor Pods search always visible.

## Expect this after import

Every mentee shows Yellow on Mentee Status — not from attendance, but
because no commitments or written items are complete yet. Say the word if
you want Yellow held back until a deadline is near or passed.
