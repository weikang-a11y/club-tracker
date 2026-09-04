# ICPrep removal — how to apply

These files replace their counterparts on `origin/main`. There is no `.git`
folder in this archive, so nothing can rewind your repository.

## 1. Make sure you are on the remote version

```powershell
cd C:\Users\olivi\OneDrive\Documents\club-tracker
git status          # must be clean
git log --oneline -1    # must match origin/main
```

If it isn't clean, run `git reset --hard origin/main` first.

## 2. Copy these files in

Copy `app.py`, `MIGRATION_NOTES.md`, `.env.example`, and the two files in
`templates\` over the same paths in your repo. Then delete the retired
template:

```powershell
del templates\icprep_events.html
```

## 3. Verify before committing

This is the important step. Git will tell you whether the base I worked from
matched yours:

```powershell
git add -A
git diff --cached --stat
```

You should see **exactly six entries**:

| File | Change |
|---|---|
| `app.py` | modified |
| `.env.example` | modified |
| `MIGRATION_NOTES.md` | modified |
| `templates/member_commitments.html` | modified |
| `templates/exam_uploads.html` | modified |
| `templates/icprep_events.html` | deleted |

If anything else appears, or if `app.py` shows far more changes than roughly
250 deletions, **stop and run `git reset --hard origin/main`** — it means your
`app.py` had edits mine did not, and I need your copy to redo this properly.

To read the actual change before committing:

```powershell
git diff --cached app.py
```

## 4. Commit

```powershell
git commit -m "Remove ICPrep integration, tracking and webhook endpoints"
git push
```

## 5. After deploying

- **Back up the database first.** The migration drops `icprep_event`,
  `icprep_webhook_log` and `annual_icprep_tracker` on next boot. That is
  irreversible and takes the received event history with it.
- Delete `ICPREP_WEBHOOK_SECRET` from your Railway variables; nothing reads it
  now. Rotate it with ICPrep if it was ever shared with them.

## Note on a file you deleted

You removed `templates/my_commitments.html` on the remote. That is safe — the
`/my_commitments` route renders `member_commitments.html`, not the file you
deleted, so no route breaks.
