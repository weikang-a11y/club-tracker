# Reset files — identical to the zip you sent

These are byte-for-byte the `app.py` and `templates/` from club-tracker_1.zip,
with no changes of mine. Copy them over your local repo to get back to a known
baseline.

Equivalent if you prefer git:

    git checkout -- app.py templates/

## What I actually changed last turn

One file only: `templates/admin_member_commitments.html`. If you did not copy
that file in, nothing on your site would look different — which matches what
you are seeing.

## Why the nav bar is not collapsible

I removed the hamburger deliberately two turns ago. It was the only structural
change I had made when the white screen first appeared, so it was the prime
suspect. The current baseline has no hamburger by design.
