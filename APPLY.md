# Update: view toggle, admin password filter, commitment rollover

Three files. Built on the assumption that your repo matches what I last sent
(you said all changes up to here are applied) — the verification step below
will catch it if not.

```powershell
git add -A
git status    # expect exactly: app.py, templates/base.html,
              # templates/admin.html modified
git commit -m "Add view toggle, admin password filter, commitment rollover"
git push
```

If `git status` shows more than those three, stop — the base diverged and I
need your current app.py.

## Already done, no change needed

**Written deadlines** already match your list exactly. I compared all five
families item by item and date by date: IMC, BOR, ENT, PM, PS all identical,
including the two items sharing 10/29 in each and PS having no Executive
Summary.

**Pod add and delete** already exist: creation via `/admin/mentor_pods`,
plus `edit`, `delete/<pod_id>`, `delete_group/<mentor_id>/<pod_number>`,
`move` and `assign`. If the buttons are not showing for you, that is a
template or permission issue rather than missing code — send a screenshot
and I will trace it.

## 1. Admin / officer view toggle

Users who hold both admin and officer roles get a "Viewing as" control at
the top of the nav with Admin and My Pod buttons. The choice lives in the
session, so it persists across pages and clears on logout.

`is_admin_view()` and `is_officer_view()` were stubs; they now respect the
selected mode, which means every permission check and
`_members_visible_to_current_user()` follows automatically — no route
needed changing. In officer view an admin also lands on the officer
dashboard rather than the admin panel.

Verified: member_commitments shows 128 mentees in admin view and 1 in
officer view; same for the at-risk report. Users without both roles get the
toggle hidden and the route refuses them.

## 2. Admin panel password filter

A "Filter by" dropdown beside the user search: All users / Password not set
yet / Password already set. Accounts awaiting setup sort to the top in every
view, then by role and username. A badge in the header shows how many are
still pending.

## 3. Commitment rollover

Previously a completion was discarded entirely if that conference's
requirement was already met — the decrement was guarded by `> 0` with no
else branch.

Now `_apply_completion()` starts at the named conference and walks forward
through CONFERENCE_ORDER to the first conference that still needs that
category, crediting there. Rollover never crosses categories: a roleplay
only ever reduces a roleplay requirement. It only moves forward, never back
to an earlier conference. If every conference is satisfied, nothing is
decremented.

Both completion paths use it — logging a practice session and marking a
commitment complete manually.

Verified with a novice needing RP 2/1/2 across VCMC/SVCDC/SCDC: four
roleplays logged against VCMC left 0/0/1, two writtens (1 each) left 0/0/1,
and exam requirements were untouched throughout.

## Still open

- `MDP_UPLOAD_ENABLED=1` for the monthly workbook upload.
- Phase 3 team features.
- `/settings` is member-only, so officers and admins cannot set a
  notification email.
- Audit items: practice-type badges compare against 'Roleplay' instead of
  'In-Person Roleplay'; redundant `/mentee-progress`; orphan templates
  `add_commitment.html` and `register.html`; unlinked
  `/admin/make_first_admin`; `/change-password` has no link.
