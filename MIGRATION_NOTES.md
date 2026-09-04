# Bug-fix pass — what changed and what to do before deploying

## Read this first

Four startup routines were failing silently and rolling back on every boot.
Now that they run, the **first boot** against the production database will:

- import the 39-person 2026-27 officer roster
- install 185 written checklist requirements
- create ~126 placeholder "test" member accounts (`create_new_officer_demo_pods`)

That last one is a one-time, irreversible seed into a live database.
Set `SKIP_DEMO_PODS=1` before the first deploy, confirm the roster import
looks right, then decide separately whether you want the demo pods.

## Environment variables

| Variable | Purpose |
|---|---|
| `SECRET_KEY` | Flask session signing. Was hardcoded in source. Set this. |
| `ICPREP_WEBHOOK_SECRET` | ICPrep webhook signature. Was hardcoded. Rotate and set. |
| `SKIP_DEMO_PODS` | `1` to suppress the 126 placeholder accounts. |
| `MDP_UPLOAD_ENABLED` | `1` to enable the admin .xlsx workbook upload. Off by default. |

Both secrets keep their previous literal as a fallback, so nothing breaks if
you deploy before setting them — but they are public in git history.

## Fixes

### Missing imports
`jsonify` was never imported but used in `move_pod_member`, `assign_pod_member`
and `toggle_checklist_item` — moving a mentee between pods and checking off a
commitment both raised `NameError` on every call. Also added `posixpath`,
`tempfile`, `zipfile`, `xml.etree.ElementTree as ET` (the whole pure-python
.xlsx reader was dead), and `FileField`/`FileRequired`/`FileAllowed`.
Removed a duplicate `datetime` import.

### Models that did not match their callers
| Model | Was | Now |
|---|---|---|
| `ChecklistItem` | `item` | `item_name` |
| `ChecklistRequirement` | `requirement`, `due_date` | `item_name`, `deadline` (string) |
| `MDPAuditLog` | `user_id`, `target` | `actor_id`, `target_user_id`, `category` + `actor` / `target_user` relationships |
| `DataMigration` | — | `details` |
| `MentorPodEditLog` | did not exist | created |

`/member_commitments` and `/checklist_completion` returned 200 only because
`checklist_item` was empty; they raised `AttributeError` as soon as a row
existed.

### Missing definitions
Added `MDPWorkbookUploadForm` and `SPREADSHEET_ACCOUNT_OVERRIDES` (empty, with
the key format documented — the email/name fallback matching handles the
normal cases).

### Registration hole
`register()` built a safe `role='member'` user and then immediately discarded
it for one built from `form.role.data`, which accepted `'officer'`. Meanwhile
`register.html` never rendered the role field, so legitimate signup always
failed validation. Self-registration is now closed: `/register` redirects to
login with a message, the role field is gone from `RegisterForm`, and the
"Register here" link was removed from `login.html`.

### Postgres-only migration syntax
Every `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` is Postgres-only. On SQLite it
raises and is swallowed, which is why local databases were missing
`is_competing`, `reset_token`, `grade` and `mentor_pod.event`. Replaced with
portable `_ensure_column` / `_backfill_column` helpers at module scope.

The migration block was also reordered: the workshop-creator backfill ran ORM
queries against `User` before the new columns existed, throwing
`OperationalError` on any pre-existing database.

`checklist_requirement` is dropped and recreated rather than patched — it had a
`NOT NULL member_name` the deadline sync never populates, and it is a derived
table rebuilt from constants on every boot.

### Template
Both event `<select>` blocks in `mentor_pods.html` iterated `EVENT_TABS`
(a list of tuples) as scalars, rendering option values as literal
`('BOR', 'Business Operations Research')`. Now unpacked.

## Verification

- `pyflakes`: no undefined names (was 23)
- All routes exercised as admin / officer / member: no 5xx
- Pod move returns 200, audit row written and readable via the `actor` relation
- Checklist toggle inserts correctly with `member_name` populated
- Upgrading the existing `club.db` preserved 3,335 AH records, 3,069 WS
  records and 306 pods
- The repaired .xlsx reader parses all four tabs of the real MDP workbook

## Not included

`at_risk_report.html` and `build_mentee_risk_report()` are both complete but no
route calls either one. That is a missing feature, not a bug, so it was left
out to keep this a clean bug-fix change.

`.env` is excluded from this archive (it is gitignored and holds a live
SendGrid key worth rotating).
