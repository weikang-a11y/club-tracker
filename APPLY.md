# Fix: pod add/delete UI, and view toggle applies to the whole account

Three files.

```powershell
git add -A
git status    # expect exactly: app.py, templates/base.html,
              # templates/mentor_pods.html modified
git commit -m "Expose pod add/delete, make view toggle switch the whole account"
git push
```

## 1. Pod add and delete were never in the UI

The routes existed but nothing linked to them, and there was a comment in
`mentor_pods()` saying the add form was "hidden from the page".

There was also a real bug behind that: the create route reads
`form.event.data` and `form.is_competing.data`, but `MentorPodForm` declared
neither field, so submitting it would have raised AttributeError.
`pod_number` was also a StringField writing into an integer column — fine on
SQLite, rejected by Postgres.

Fixed: added `event` and `is_competing` to the form, changed `pod_number` to
an IntegerField with a min of 1, and populated the event choices from
EVENT_TABS.

Added to the page:
- **Add Mentor Pod** form at the top of Edit Assignments (mentee, mentor,
  pod number, experience, event, competing status).
- **Remove from Pod** button on each mentee card in Edit Assignments,
  with a confirm prompt. Commitments are kept.
- **Delete Pod** button on each mentor card in Finalized Pods, removing the
  whole pod at once, with a confirm naming the mentee count.

## 2. View toggle now switches the entire account

Previously the toggle only narrowed which mentees you could see — the nav,
the role chip, and `/admin` all still behaved as admin, because they tested
`current_user.is_admin` directly rather than the selected view.

Now `admin_required` and the nav both follow `active_view()`. In officer
view the account is an officer account throughout: officer nav, Officer role
chip, officer dashboard, and `/admin` and every `/admin/...` route refuse
access. Switching back restores everything.

## Verification

Before the change, on the current build:
- pod page: 0 delete buttons, no add form
- officer view nav: still the admin menu
- `MentorPodForm` had no `event` field

After:
- pod page: 127 remove buttons, 22 delete-pod buttons, add form present
- officer view nav: Dashboard, Practice Sessions, Commitment Reports,
  Written Progress, Calendar, Exam Uploads; role chip reads Officer
- admin view nav: MDP Changes, Mentor Pods, Member Commitments, Written
  Progress, Mentee Status; role chip reads Admin
- `/admin` returns 200 in admin view, 302 in officer view, no redirect loop
- Add pod: creates the row, applies non-compete, seeds 3 commitments,
  rejects a duplicate assignment
- Remove from pod: deletes the assignment, keeps commitments
- Delete pod group: removes all members of that pod
- Officers without admin still cannot delete pods
- All routes as admin, officer, member and admin-in-officer-view: no 5xx
- Previously shipped features re-checked and intact: admin password filter,
  commitment rollover, mentee detail page

## Still open

- `MDP_UPLOAD_ENABLED=1` for the monthly workbook upload.
- Phase 3 team features.
- `/settings` is member-only, so officers and admins cannot set a
  notification email.
- Audit items: practice-type badges compare against 'Roleplay' instead of
  'In-Person Roleplay'; redundant `/mentee-progress`; orphan templates
  `add_commitment.html` and `register.html`; unlinked
  `/admin/make_first_admin`; `/change-password` has no link.
