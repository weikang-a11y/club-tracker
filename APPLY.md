# Update: yellow status, workshop cleanup, practice slot targeting

No `.git` folder here. Copy these over the same paths, then delete two
retired templates:

```powershell
del templates\add_workshop.html
del templates\attendance.html
```

Verify with `git add -A` then `git diff --cached --stat`. Expect exactly six
entries: app.py, templates/base.html, templates/dashboard.html,
templates/practice_sessions.html modified; add_workshop.html and
attendance.html deleted.

## What changed

**Yellow at-risk status.** New `/at_risk_report` route renders the finished
report template. `at_risk` (attendance below threshold or something overdue)
and `needs_attention` (incomplete but not yet late) both display Yellow;
per-row reasons stay separate. Green = on track, Red = non-compete. Filter
via `?status=at_risk|on_track|non_compete`. "Mentee Status" added to the
admin nav.

**Workshops no longer decrement commitments.** Only practice sessions do.
Removed `/add_workshop`, `/edit_workshop`, `/delete_workshop`,
`/signup_workshop`, `/cancel_signup`, `/workshop/<id>/attendance` and
`/increment_general_attendance`, plus `add_workshop.html` and
`attendance.html`. Members cannot create or sign up for workshops.

**Manual attendance removed.** The officer dashboard's "+1 Manual" table is
gone. WS/AH attendance comes from the monthly workbook upload only.

**Reminders moved to practice sessions.** `process_workshop_reminders` is
replaced by `process_practice_session_reminders`, which emails a member
before a slot they have claimed, honouring their existing
`remind_minutes_before` setting. `ReminderLog` gains `practice_session_id`;
`workshop_id` becomes nullable so old rows survive.

**Practice slot targeting.** `PracticeSession.reserved_for_id` is new. The
post form has an "Open to" dropdown: "Everyone in my pod" or a single
member. A reserved slot is visible only to that member, and signup is
rejected if someone else tries. The officer's table gains an "Open To"
column. Server-side check confirms the chosen member is actually in the
officer's pod.

## Note

The `Workshop`, `GeneralAttendance` and `AttendanceSubmission` tables are
left in place with their data; nothing writes to them now. Say the word if
you want them dropped.
