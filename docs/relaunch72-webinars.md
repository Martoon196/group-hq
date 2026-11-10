# Webinars from Group HQ (sign in once)

Webinars for Property Predator and Martin Howard Property run inside Relaunch72 on
each brand's host (`hq.propertypredator.com`, `app.martinhowardproperty.co.uk`):
video, registration pages, reminders, replays, the browser studio and the CRM
handoff all live there. Group HQ is the front door: `/webinars` lists both brands'
webinars and every button opens the right page on the brand host already signed in.

## How the sign-in works

1. The founder signs in to Group HQ (password + authenticator), as today.
2. A click on `/webinars/open?brand=mhp&path=/portal/webinars/<id>/host` makes
   Group HQ sign a token with `R72_SSO_SECRET`: the login email, the brand, the
   exact brand host, the exact portal path, issued/expiry (90 seconds) and a
   random nonce. The browser is redirected to
   `https://<brand host>/portal/auth/group-hq?t=<token>`.
3. Relaunch72 verifies the signature, host, path and lifetime, refuses a nonce it
   has seen, maps the email to a portal user through its own server-side
   allowlist (`R72_GROUP_HQ_SSO_USERS`) and asks its database for a session.
   The database refuses unless that user is an active owner or admin of the
   brand's workspace (for MHP, the MHP marketing owner). The result is the same
   session cookie a password login sets, bounded to twelve hours.

Nothing in the token is authority on its own, Relaunch72 never trusts a browser
value for the workspace, and the token exists only in that one redirect.

## Settings (names only)

Group HQ web service: `R72_SSO_SECRET` (32-256 random characters, shared with
nothing else), optionally `R72_PP_HOST` and `R72_MHP_HOST` (default to the live
hosts). The webinar list reuses `R72_CRM_TOKEN`.

Relaunch72 web service: `R72_GROUP_HQ_SSO_SECRET` (the same value),
`R72_GROUP_HQ_SSO_USERS` (JSON object, Group HQ login email to portal user id),
and for MHP the existing `R72_GROUP_HQ_CRM_MHP_WORKSPACE_ID` and
`_ORGANIZATION_ID` so the listing bridge can answer `brand=mhp`.

## Release sequence

Deploy Relaunch72 first (migration 0147 adds the session function; its `/ready`
stays green before and after). Set the two secrets, open `/webinars` here, press
Host on one webinar and confirm you land signed in on the brand host. Check that
a replayed link fails with a neutral sign-in page, and that removing the email
from `R72_GROUP_HQ_SSO_USERS` fails the next click. To stop the handoff, remove
`R72_SSO_SECRET` here: the Webinars page keeps listing and links fall back to
the brand host's own login.

Smoke checks without a database: `python -m unittest discover -s unit_checks -p '*.py' -v`.
