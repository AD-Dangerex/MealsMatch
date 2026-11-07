# Meals Match

Meals Match is a Flask and SQLite food-sharing application. Individuals and
organisations can publish surplus food or request food, and the matching
engine ranks eligible donation/request pairs.

## Run locally

```powershell
.\venv\Scripts\python.exe -m pip install -r requirements.txt
.\venv\Scripts\python.exe app.py
```

Open `http://127.0.0.1:5000`. The app creates its SQLite database in the
`instance` directory on first run. Set `DATABASE_PATH` to use a different file.

## Main workflows

- Individuals and organisations can register as donors or recipients;
  administrator accounts are not publicly self-registerable. Each account
  chooses one role. Individuals can enter their own or household name instead
  of an organisation name.
- Profiles record account type, city, and optional phone details. Recipient
  profiles also record an approximate number of people supported per week.
- Donors add food listings with quantity units, dietary tags, allergens,
  storage method, packaging, preparation time, pickup window, use-by time, and
  collection notes. Publishing requires a donor handling declaration.
- Recipients submit requests with quantity units, expected people supported,
  dietary requirements, excluded allergens, storage preference, urgency,
  preferred pickup window, deadline, and collection notes.
- The engine rejects expired or incompatible pairs, then ranks eligible pairs
  with quantity (25%), food compatibility (20%), city match (20%), timing
  (15%), urgency (10%), and recent allocation fairness (10%). Pickup windows
  must overlap. A dashboard breakdown shows each score component.
- Search all eligible matches by food, category, participant, or city; filter by
  category and storage; and sort by match score, urgency, expiry, or quantity.
- Donors can record and extend partial allocations. Both parties can confirm collection.
- Admins can review account, allocation, and audit-log information, see
  category-level distribution totals, and download an allocation CSV report.

Location matching currently compares participants' city names; it does not
calculate road distance or travel time. Contact phone numbers are only shown
to the two participants after an allocation has been recorded. Pickup
instructions are available to those participants while the allocation is
pending.

Food safety declarations and listing details are user supplied. Meals Match does
not inspect or certify food; participants must independently check condition,
allergens, transport, and applicable local requirements.

## Administrator account

Create the first administrator by setting both `ADMIN_EMAIL` and
`ADMIN_PASSWORD` before starting the application. The password is stored as a
hash. Do not use a public or shared default password. For Render, add these as
secret environment variables in the service settings and redeploy.

## Google sign-in

Google sign-in is optional and appears when both `GOOGLE_CLIENT_ID` and
`GOOGLE_CLIENT_SECRET` are configured. Create a Google OAuth client of type
**Web application** and register the exact redirect URI:

`https://YOUR-RENDER-SERVICE.onrender.com/auth/google/callback`

Add the client ID and secret privately as Render environment variables. For
local testing, also register
`http://127.0.0.1:5000/auth/google/callback` with the OAuth client and set its
credentials locally. Never commit OAuth secrets.

Google must return a verified email. If that email already belongs to a
Meals Match account, the verified Google identity is linked to it; otherwise,
the user completes a donor or recipient profile. Google passwords are never
received or stored by Meals Match.

Password recovery requires SMTP settings in the deployment environment:
`SMTP_HOST`, `SMTP_PORT` (usually `587`), `SMTP_USERNAME`, `SMTP_PASSWORD`, and
`SMTP_FROM`. Set `PUBLIC_BASE_URL` to the canonical site origin if Render does
not provide `RENDER_EXTERNAL_HOSTNAME`. Configure mail settings as private
Render environment variables; never commit mail credentials. For Gmail, use
an app password, not your Google account password. Reset links expire after
30 minutes and can only be used once. If mail is not configured, users can
sign in with Google if their account is linked, or contact the site
administrator.

## Deployment and data

`render.yaml` configures the Docker web service, a persistent Render disk at
`/var/data`, SQLite at `/var/data/foodbridge.db`, and a generated stable
`SESSION_SECRET`. A persistent disk requires a paid Render service plan and
continues to incur its applicable costs. If the existing service was created
manually, ensure its settings include the persistent disk mount and database
path, and a stable `SESSION_SECRET`; pushing this Blueprint file alone may not
change a manually configured service. If `DATABASE_PATH` or
`SESSION_SECRET_FILE` is missing, the app uses `/var/data/foodbridge.db` and
`/var/data/.session_secret` only when `/var/data` is actually mounted as a
persistent disk; otherwise it stops with an actionable startup error rather
than using temporary storage.

When the disk is mounted, the app attempts to copy an existing
`instance/foodbridge.db` into it the first time, before applying schema
migrations. If the old instance data has already been deleted, it cannot be
recovered and affected users must create accounts again. Locally, the database
and generated session key are stored under `instance/`.

Passwords are stored as salted Werkzeug password hashes, not plaintext. If
login fails after deployment, check whether the service is still using the
same persistent database file.
