# Update: admin home = admin panel, nav cleanup, written links,
# mentee detail page, yellow status, workshop cleanup, slot targeting

Supersedes any earlier package. No `.git` folder. Copy over the same paths,
then delete two retired templates:

```powershell
del templates\add_workshop.html
del templates\attendance.html
```

Verify with `git add -A` then `git diff --cached --stat`. Expect twelve
entries: app.py, nine templates modified or added, two deleted.

## Admin home screen (this round)

The `/` dashboard route now redirects admins to `/admin`. Doing it at the
route rather than the link means every existing `url_for('dashboard')` in
the codebase — login, the settings guard, various flash redirects — lands
admins on the admin panel. The member dashboard is unreachable for them,
by URL or otherwise.

No redirect loop: `is_admin_view()` and `admin_required` both test
`current_user.is_admin`, so they can never disagree.

Admin nav reads: MDP Changes, Mentor Pods, Member Commitments,
Written Progress, Mentee Status. Officer and member navs unchanged.

## Member dashboard

- "Workshop Attendance Summary" card removed. It counted retired workshop
  sign-up tables and showed 0/0/0% beside the real AH/WS figures.
- Panel relabelled from "<EVENT> Workshops" to "<EVENT> Practice Sessions";
  now full width.

## Written document links

`User.written_url`, one per mentee. Mentees set their own on My Commitments;
mentors and admins set or correct it from the mentee detail page (own pod
only for officers). "Open Written Document" button in the detail header, and
a document icon beside the name on Written Progress. Must start with
http:// or https://, 500 character cap; blank clears.

## Per-mentee detail page

`/mentee/<id>`. Names clickable from Member Commitments, Written Progress,
Mentee Status and both officer report tables. Shows
mentor/level/event/competing badge, AH and WS rates, per-conference
commitments with deadline, grade and status, the written checklist, upcoming
practice sessions, recent completions, and exam uploads.

## Yellow at-risk status

`/at_risk_report`. `at_risk` and `needs_attention` both display Yellow with
reasons per row; Green on track, Red non-compete. Filter with
`?status=at_risk|on_track|non_compete`.

## Workshop cleanup and reminders

Only practice sessions decrement commitments. Removed the workshop booking,
signup, attendance and manual-increment routes and their two templates.
`process_practice_session_reminders` replaces the workshop reminder job.
`ReminderLog` gains `practice_session_id`; `workshop_id` becomes nullable.

## Practice slot targeting

`PracticeSession.reserved_for_id`. "Open to" dropdown: everyone in the pod,
or one named member. Reserved slots visible only to that member.

## Still open

- `MDP_UPLOAD_ENABLED=1` for the monthly workbook upload; needs one
  end-to-end test with a real workbook.
- Phase 3 team features, pending your discussion.
- `/settings` is member-only by an existing guard, so officers and admins
  cannot set a notification email. Flag if that is not intended.
- Audit items not yet actioned: practice-type badges compare against
  'Roleplay' instead of 'In-Person Roleplay' (four places);
  `/mentee-progress` redirect is redundant; orphan templates
  `add_commitment.html` (broken url_for inside) and `register.html`;
  unlinked `/admin/make_first_admin` bootstrap page; `/change-password` has
  no link.
