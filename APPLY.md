# Update: bell at top of nav, always-visible pod search
# (plus the session_time fix and AH 100% from the last package)

Supersedes every earlier package. Seven files.

```powershell
git add -A
git status    # expect: app.py, templates/base.html, templates/mentor_pods.html,
              # templates/dashboard.html, templates/at_risk_report.html,
              # templates/practice_sessions.html, templates/settings.html
git commit -m "Bell at top of nav, always-visible pod search, session_time fix, AH 100%"
git push
```

## 1. Notification bell

Now sits directly under the DECA Tracker heading, above the role chip and
all nav links, for every account. Verified above the nav for admin, officer
and member.

## 2. Search on admin pages

Checked every admin page that has a filter. Four already had a search beside
the filter and were left alone:

| Page | Search |
|---|---|
| Admin panel | already present |
| Member Commitments | already present |
| Written Progress | already present |
| Mentee Status | already present |
| **Mentor Pods** | **was hidden** |

Mentor Pods was the odd one out: the search box existed but was
`display:none` until you picked a filter type from the dropdown, which is
why it was missing from your screenshot.

Changed so it is always visible next to "Filter by", keeps its text when you
change filter type, and searches mentee *and* mentor names at once. It now
applies on top of whatever category filter is selected rather than only
working for the Mentee and Mentor filter types, so you can, for example,
filter to Novice and then type a name within that.

## 3. Carried over from the previous package

**session_time fix** — the column was `VARCHAR(5)` and typed slots like
`3:00-3:20` overflowed on Postgres, causing a 500 when creating a practice
session. Column widened to 40, startup migration widens the live column, and
the route truncates defensively. `user.phone` had the same shape (VARCHAR(20),
free text, no maxlength) and was capped the same way.

**AH requirement 100%** — `AH_THRESHOLD = 1.00` with the five hardcoded
"80%" strings updated in `dashboard.html` and `at_risk_report.html`.

## Verification

- Bell above the nav for all three roles.
- Search present and visible on all five admin pages.
- All routes as admin, officer and member: no 5xx.

## Reminder

When this deploys, watch the logs for
`[Migration] widened practice_session.session_time to VARCHAR(40).`
If that line does not appear and practice session creation still 500s, tell
me and I will check the migration guard.
