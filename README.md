# FoodBridge

FoodBridge is a Flask and SQLite food-donation matching application. Donors can
publish surplus food, recipient organisations can submit requests, and the
matching engine ranks eligible donation/request pairs.

## Run locally

```powershell
.\venv\Scripts\python.exe -m pip install -r requirements.txt
.\venv\Scripts\python.exe app.py
```

Open `http://127.0.0.1:5000`. The app creates its SQLite database in the
`instance` directory on first run. Set `DATABASE_PATH` to use a different file.

## Main workflows

- Register as a donor or recipient; administrator accounts are not publicly
  self-registerable.
- Donors add food listings with a category, quantity, expiry, dietary tags, and
  allergen information.
- Recipients submit requests with quantity, dietary requirements, excluded
  allergens, urgency, and a collection deadline.
- The engine rejects expired or incompatible pairs, then ranks eligible pairs
  with quantity (25%), compatibility (20%), city match (20%), timing (15%),
  urgency (10%), and fairness (10%).
- Donors can record and extend partial allocations. Both parties can confirm collection.
- Admins can review account, allocation, and audit-log information.

Location matching currently compares the organisations' city names; it does
not calculate road distance or travel time.

## Administrator account

Create the first administrator by setting both `ADMIN_EMAIL` and
`ADMIN_PASSWORD` before starting the application. The password is stored as a
hash. Do not use a public or shared default password. For Render, add these as
secret environment variables in the service settings and redeploy.

## Deployment and data

`render.yaml` configures a Python web service and generates a `SESSION_SECRET`.
The included `Dockerfile` can also be used for a Docker-based Render service.
Set a stable, private `SESSION_SECRET` when deploying manually.

SQLite is suitable for a school project and a single-instance demonstration.
Render's local filesystem may be replaced during redeploys or instance
restarts; configure persistent storage or use a managed database before relying
on this deployment for durable real-world records.
