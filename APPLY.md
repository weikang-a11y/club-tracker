# Update: written document links, mentee detail page, yellow status,
# workshop cleanup, practice slot targeting

Supersedes any earlier package. No `.git` folder here. Copy these over the
same paths, then delete two retired templates:

```powershell
del templates\add_workshop.html
del templates\attendance.html
```

Verify with `git add -A` then `git diff --cached --stat`. Expect twelve
entries: app.py, nine templates modified or added, two deleted.

## Written document links (new)

`User.written_url` stores one link per mentee, matching the written
checklist which is keyed on event rather than conference.

- **Mentees** set their own on My Commitments, in a new "My Written
  Document" card, with a reminder to check sharing permissions.
- **Mentors and admins** can set or correct it from the mentee detail page,
  scoped the same way as the rest of that page (own pod only for officers).
- An **Open Written Document** button appears in the detail page header once
  a link exists, and a small document icon appears beside the mentee's name
  on Written Progress.
- Links must start with `http://` or `https://` and cap at 500 characters,
  so `javascript:` and similar are rejected. Submitting blank clears it.

## Per-mentee detail page

Route `/mentee/<id>`, template `mentee_detail.html`. Mentee names are
clickable from Member Commitments, Written Progress, Mentee Status and both
officer report tables. Shows mentor/level/event/competing badge, AH and WS
rates against thresholds, per-conference commitment counts with deadline,
grade and status badge, the written checklist item by item, upcoming
practice sessions, recent completions, and exam uploads.

Access: admins any mentee, officers only their own pod, members not at all.

## Yellow at-risk status

`/at_risk_report` renders the finished report. `at_risk` and
`needs_attention` both display Yellow with reasons per row; Green on track,
Red non-compete. Filter with `?status=at_risk|on_track|non_compete`.
"Mentee Status" in the admin nav.

## Workshop cleanup

Only practice sessions decrement commitments. Removed `/add_workshop`,
`/edit_workshop`, `/delete_workshop`, `/signup_workshop`, `/cancel_signup`,
`/workshop/<id>/attendance`, `/increment_general_attendance` and their two
templates. The officer dashboard's "+1 Manual" table is gone.

## Reminders on practice sessions

`process_practice_session_reminders` replaces the workshop version,
honouring each member's `remind_minutes_before`. `ReminderLog` gains
`practice_session_id`; `workshop_id` becomes nullable.

## Practice slot targeting

`PracticeSession.reserved_for_id`. The post form has an "Open to" dropdown:
everyone in the pod, or one named member. Reserved slots are visible only to
that member; signup is rejected for anyone else. Officer table gains an
"Open To" column.

## Still open

- `MDP_UPLOAD_ENABLED=1` for the monthly workbook upload — reader is
  repaired and parses your real workbook; the upload route needs one
  end-to-end test.
- Phase 3: emailing officers individual logins; officer team selection at
  signup and advisor/co-pres/VP team editing, both blocked on there being no
  team field on `User`.
- `Workshop`, `GeneralAttendance`, `AttendanceSubmission` tables remain with
  their data; nothing writes to them.
- Member dashboard still computes a workshop attendance summary against a
  hardcoded 18 sessions, now meaningless.
