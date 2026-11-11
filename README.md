# Meals Match

Meals Match is a Flask food-sharing application. It uses SQLite for local
development and PostgreSQL on Railway. Individuals and organisations can
publish surplus food or request food, and the matching engine ranks eligible
donation/request pairs.

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
  with quantity (25%), food compatibility (20%), road-distance proximity
  (20%), timing (15%), urgency (10%), and recent allocation fairness (10%).
  Pickup windows must overlap. A dashboard breakdown shows each score
  component.
- Search all eligible matches by food, category, participant, or city; filter by
  category, storage, and distance; and sort by match score, nearest route,
  urgency, expiry, or quantity.
- Donors can record and extend partial allocations. Both parties can confirm collection.
- Admins can review account, allocation, and audit-log information, see
  category-level distribution totals, and download an allocation CSV report.

Exact addresses are not shown to other users on public match cards; after an
allocation, both participants can see the pickup and collection details.
Contact phone numbers and pickup instructions are available to those
participants after an allocation is recorded.

Food safety declarations and listing details are user supplied. Meals Match does
not inspect or certify food; participants must independently check condition,
allergens, transport, and applicable local requirements.

## Administrator account

Create the first administrator by setting both `ADMIN_EMAIL` and
`ADMIN_PASSWORD` before starting the application. The password is stored as a
hash. Do not use a public or shared default password. Add these as private
Railway service variables; never commit real passwords.

## Google sign-in

Google sign-in is optional and appears when both `GOOGLE_CLIENT_ID` and
`GOOGLE_CLIENT_SECRET` are configured. Create a Google OAuth client of type
**Web application** and register the exact redirect URI:

`https://YOUR-SITE-DOMAIN/auth/google/callback`

For Railway, the callback is fixed to the deployed app's canonical HTTPS
domain. Add the client ID and secret as private deployment variables. For
local testing, also register
`http://127.0.0.1:5000/auth/google/callback` with the OAuth client and set its
credentials locally. Never commit OAuth secrets.

Google must return a verified email. If that email already belongs to a
Meals Match account, the verified Google identity is linked to it; otherwise,
the user completes a donor or recipient profile. Google passwords are never
received or stored by Meals Match.

Password recovery requires SMTP settings in the deployment environment:
`SMTP_HOST`, `SMTP_PORT` (usually `587`), `SMTP_USERNAME`, `SMTP_PASSWORD`, and
`SMTP_FROM`. Set `PUBLIC_BASE_URL` to the canonical Railway site origin.
Configure mail settings as private Railway service variables; never commit
mail credentials. For Gmail, use an app password, not your Google account
password. Reset links expire after 30
minutes and can only be used once. If mail is not configured, users can sign
in with Google if their account is linked, or contact the site administrator.

The public privacy policy is available at
`https://YOUR-RAILWAY-DOMAIN/privacy-policy`. Set `PRIVACY_CONTACT_EMAIL` to a
public address for privacy questions and account deletion requests; otherwise
the app uses `ADMIN_EMAIL` as the contact address when configured.
The public terms of service are at
`https://YOUR-RAILWAY-DOMAIN/terms-of-service`.

## Distance-aware matching

Donors enter a food pickup point and recipients enter a collection point.
OpenStreetMap Nominatim geocodes each address when submitted; OSRM calculates
driving distance and estimated travel time between the resulting coordinates.
The recipient chooses a maximum driving distance from 1 to 250 km (25 km by
default). Routes beyond that hard limit are ineligible, while eligible nearby
routes receive a higher location score. Users can further filter matches by
distance or sort by nearest route.

The route-proximity component is `max(0, 100 × (1 − road_distance /
recipient_limit))`. Category, unit, storage, dietary, allergen, availability,
expiry, deadline, and pickup-window checks remain hard eligibility rules. The
other score components are quantity fit (25%), food compatibility (20%),
timing (15%), urgency (10%), and allocation fairness (10%), for a total of
100%. Match cards show the driving distance, estimated minutes, and weighted
component breakdown.

Exact addresses are stored with their listing/request and are never included
in public match cards. They are shown to both matched participants only after
an allocation is recorded. Nominatim receives the submitted address for
geocoding; OSRM receives the resulting coordinates to calculate the route using
OpenStreetMap road data. Use a public handover point or approximate pickup
address if you do not want to share a home address with those services. The
form records explicit consent. OpenStreetMap data is attributed in the matching
interface.

Geocoded coordinates are stored with each listing/request. Road-route cache
entries are keyed by hashes of rounded coordinates and refreshed after 30
days. Unroutable locations are not recommended. If the map services fail, the
app reports that the route could not be verified and does not create a
distance-qualified match.
The default public Nominatim and OSRM endpoints are intended for low-volume
prototypes and school demonstrations, not high-availability production
traffic. Set `GEOCODER_URL`, `ROUTER_URL`, and `MAPS_USER_AGENT` in Railway if
you configure a hosted provider or your own service.

Run the automated checks locally with
`.\venv\Scripts\python.exe -m unittest discover -s tests -v`. The test suite
covers provider responses and failures, route scoring and radius exclusions,
cache refresh and PostgreSQL query conversion, consent and distance validation,
and address visibility before and after allocation. External map calls are
mocked in tests.

## Deployment and data

### Railway

Deploy this repository as a Railway project with two services: the web app and
a PostgreSQL database. The web service uses the included `Dockerfile` and
Gunicorn command; Railway supplies the listening `PORT`. Add a PostgreSQL
service, then add a `DATABASE_URL` variable to the web service using Railway's
variable-reference picker to reference the PostgreSQL service's `DATABASE_URL`.
Keep the database connection private within the Railway project.

Add these private variables to the web service:

- `SESSION_SECRET`: a stable random secret of at least 32 characters. Generate
  one locally with `python -c "import secrets; print(secrets.token_hex(32))"`.
- `PUBLIC_BASE_URL`: the canonical public HTTPS URL Railway assigns to the app.
- `ADMIN_EMAIL` and `ADMIN_PASSWORD`: set these if creating the first admin.
- `GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET`, if Google sign-in is enabled.
- SMTP settings from the password recovery section, if email recovery is needed.

The app uses Railway PostgreSQL whenever `DATABASE_URL` is set and refuses to
fall back to SQLite when deployed to Railway. PostgreSQL schema setup runs
automatically on app start. Register
`https://YOUR-RAILWAY-DOMAIN/auth/google/callback` as the Google OAuth redirect
URI. Redeploy after changing variables.

To import a local SQLite database, install the requirements locally and run
`python migrate_sqlite_to_postgres.py instance/foodbridge.db` with
`DATABASE_URL` set to the Railway database connection string. The migration
script refuses to import into tables that already contain data. Railway's
private database URL is only reachable by services in the same Railway project;
run the import from the Railway service environment or temporarily use a
Railway-provided public TCP connection if importing from your computer. It
requires access to the source SQLite file.

Locally, the app continues to use SQLite in `instance/`. If the old database
file is no longer accessible, its accounts and listings cannot be imported.

Passwords are stored as salted Werkzeug password hashes, not plaintext. If
login fails after deployment, verify the web service still points to the same
Railway PostgreSQL database.
