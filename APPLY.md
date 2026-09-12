# Combined: AH requirement 100% + practice slot presets

This replaces BOTH earlier packages (club-tracker-ah100 and
club-tracker-slots). Use this one only — it contains everything from both.

Four files.

```powershell
git add -A
git status    # expect exactly four modified:
              #   app.py
              #   templates/dashboard.html
              #   templates/at_risk_report.html
              #   templates/practice_sessions.html
git commit -m "AH requirement to 100%, restore standard practice slots with custom entry"
git push
```

## 1. AH attendance requirement: 80% -> 100%

`AH_THRESHOLD` is now `1.00`. It feeds `get_attendance_stats()`, the
dashboard attendance panel and the at-risk report.

The percentage was also hardcoded in five template places, which is why
`dashboard.html` and `at_risk_report.html` are in this package — without
them the UI would still say 80% while the logic enforced 100%:

- `at_risk_report.html` filter label "AH Below 80%"
- `dashboard.html` status legend
- `dashboard.html` threshold summary line
- `dashboard.html` member attendance card "Required: 80%"
- `dashboard.html` member attendance summary "Required: 80%"

WS thresholds unchanged: 75% Novice, 25% Experienced.

| AH record | Rate | Flagged |
|---|---|---|
| 10/10 | 100% | no |
| 9/10 | 90% | yes |
| 8/10 | 80% | yes (previously passed) |

## 2. Practice slots: presets plus custom entry

The three standard slots are visible quick-pick buttons above the time box:
**3:00-3:20**, **3:20-3:40**, **3:40-4:00**. Clicking one fills the field;
officers can still type anything.

Buttons rather than a datalist, so both options are visible at once — a
datalist only appears after clicking into the field.

New constant `PRACTICE_SLOT_PRESETS` drives them. `TIME_SLOTS` is untouched,
since it still backs the legacy `slot` field and the `time_map` lookup.

Start times parsed for reminders:

| Slot | Start |
|---|---|
| 3:00-3:20 | 15:00 |
| 3:20-3:40 | 15:20 |
| 3:40-4:00 | 15:40 |
| 4:20-5:00 | 16:20 |
| 5:00-5:30 pm | 17:00 |

## Verification

- `AH_THRESHOLD` is 1.0; no "80%" text remains anywhere in app.py or the
  templates.
- Member dashboard shows 100%; at-risk report filter reads "AH Below 100%".
- Preset buttons render for officers, custom input present, both preset and
  typed slots save and render back exactly as entered.
- All routes as admin, officer and member: no 5xx. Reminder job runs clean.

## Heads-up on the AH change

At 100%, one missed All-Hands flags a mentee, and under the combined Yellow
rule that puts them in the at-risk bucket. Expect the Mentee Status report
to grow noticeably on first run after deploy. Worth warning the Mentorship
team so the jump is not mistaken for a fault.

An excused absence recorded as a 1 in the workbook still counts as attended,
so the officer absence-review step matters more at this threshold than it
did at 80%.
