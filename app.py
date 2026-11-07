import csv
import hmac
import io
import os
import re
import secrets
import sqlite3
from datetime import datetime
from functools import wraps
from pathlib import Path

from authlib.integrations.base_client.errors import OAuthError
from authlib.integrations.flask_client import OAuth
from flask import (
    Flask,
    Response,
    flash,
    g,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash, generate_password_hash

from allocation import get_recommendations
from database import connect_db, initialize_database


app = Flask(__name__)
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=bool(os.environ.get("RENDER")),
    GOOGLE_CLIENT_ID=os.environ.get("GOOGLE_CLIENT_ID"),
    GOOGLE_CLIENT_SECRET=os.environ.get("GOOGLE_CLIENT_SECRET"),
)
def load_session_secret():
    configured_secret = os.environ.get("SESSION_SECRET")
    if configured_secret:
        if len(configured_secret) < 32:
            raise RuntimeError("SESSION_SECRET must contain at least 32 characters.")
        return configured_secret

    configured_secret_file = os.environ.get("SESSION_SECRET_FILE")
    if configured_secret_file:
        secret_file = Path(configured_secret_file)
    elif os.environ.get("DATABASE_PATH"):
        secret_file = Path(os.environ["DATABASE_PATH"]).parent / ".session_secret"
    else:
        secret_file = Path(app.instance_path) / ".session_secret"
    secret_file.parent.mkdir(parents=True, exist_ok=True)
    try:
        with secret_file.open("x", encoding="utf-8") as secret_stream:
            secret_stream.write(secrets.token_urlsafe(48))
    except FileExistsError:
        pass

    persistent_secret = secret_file.read_text(encoding="utf-8").strip()
    if len(persistent_secret) < 32:
        raise RuntimeError(f"Session secret file {secret_file} is invalid.")
    return persistent_secret


app.config["SECRET_KEY"] = load_session_secret()
if os.environ.get("RENDER"):
    app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)

oauth = OAuth(app)
google_client = None
if app.config["GOOGLE_CLIENT_ID"] and app.config["GOOGLE_CLIENT_SECRET"]:
    google_client = oauth.register(
        name="google",
        client_id=app.config["GOOGLE_CLIENT_ID"],
        client_secret=app.config["GOOGLE_CLIENT_SECRET"],
        server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
        client_kwargs={"scope": "openid email profile"},
    )

with app.app_context():
    initialize_database()


@app.before_request
def load_user_and_check_csrf():
    g.user = None
    user_id = session.get("user_id")
    if user_id is not None:
        db = connect_db()
        g.user = db.execute(
            "SELECT id, name, email, role FROM users WHERE id = ?", (user_id,)
        ).fetchone()
        if g.user is None:
            session.clear()

    if request.method == "POST":
        submitted_token = request.form.get("csrf_token", "")
        session_token = session.get("csrf_token", "")
        if not session_token or not hmac.compare_digest(submitted_token, session_token):
            return "Your session expired. Refresh the page and try again.", 400


@app.teardown_appcontext
def close_db(error=None):
    db = g.pop("db", None)
    if db is not None:
        if error is not None:
            db.rollback()
        db.close()


@app.context_processor
def inject_template_helpers():
    def csrf_token():
        if "csrf_token" not in session:
            session["csrf_token"] = secrets.token_urlsafe(32)
        return session["csrf_token"]

    return {
        "csrf_token": csrf_token,
        "current_user": g.get("user"),
        "google_login_enabled": google_client is not None,
    }


def role_required(*roles):
    def decorator(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            if g.user is None:
                flash("Please log in to continue.", "error")
                return redirect(url_for("login"))
            if g.user["role"] not in roles:
                flash("You do not have permission to access that page.", "error")
                return redirect(url_for("dashboard"))
            return view(*args, **kwargs)

        return wrapped

    return decorator


def audit(db, user_id, action, entity_type, entity_id, details=""):
    db.execute(
        """INSERT INTO audit_logs
           (user_id, action, entity_type, entity_id, details)
           VALUES (?, ?, ?, ?, ?)""",
        (user_id, action, entity_type, entity_id, details[:500]),
    )


def csv_safe(value):
    if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@")):
        return "'" + value
    return value


def parse_datetime(value, label):
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        raise ValueError(f"Enter a valid {label}.") from None
    if parsed.tzinfo is not None:
        raise ValueError(f"Enter {label} in your local time.")
    if parsed <= datetime.now():
        raise ValueError(f"{label.capitalize()} must be in the future.")
    return parsed.isoformat(timespec="minutes")


@app.route("/")
def home():
    db = connect_db()
    impact_stats = {
        "completed": db.execute(
            "SELECT COUNT(*) AS n FROM allocations WHERE status = 'collected'"
        ).fetchone()["n"],
        "partners": db.execute(
            "SELECT COUNT(*) AS n FROM users WHERE role IN ('donor', 'recipient')"
        ).fetchone()["n"],
        "active_listings": db.execute(
            """SELECT COUNT(*) AS n FROM donations
               WHERE status IN ('available', 'partially_allocated')"""
        ).fetchone()["n"],
    }
    return render_template("index.html", impact_stats=impact_stats)


@app.route("/register", methods=["GET", "POST"])
def register():
    if g.user is not None:
        return redirect(url_for("dashboard"))

    selected_role = request.args.get("role", "recipient")
    if selected_role not in ("donor", "recipient"):
        selected_role = "recipient"

    if request.method == "POST":
        name = request.form.get("name", "").strip()
        organization = request.form.get("organization", "").strip()
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        role = request.form.get("role", "")
        city = request.form.get("city", "").strip()
        phone = request.form.get("phone", "").strip()
        organization_type = request.form.get("organization_type", "").strip()
        allowed_types = {
            "donor": {
                "Restaurant",
                "Grocery store",
                "Farm",
                "Caterer",
                "Individual",
                "Other",
            },
            "recipient": {
                "Food bank",
                "Community kitchen",
                "Shelter",
                "School",
                "Individual",
                "Other",
            },
        }

        if not name or not organization or not city:
            flash("Complete every required field.", "error")
        elif len(name) > 100 or len(organization) > 120 or len(city) > 100:
            flash("One or more fields are too long.", "error")
        elif len(email) > 254 or not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
            flash("Enter a valid email address.", "error")
        elif len(password) < 10:
            flash("Use a password with at least 10 characters.", "error")
        elif role not in ("donor", "recipient"):
            flash("Choose a valid account type.", "error")
        elif organization_type not in allowed_types[role]:
            flash("Choose a valid organisation type.", "error")
        elif len(phone) > 40 or (phone and not re.fullmatch(r"[+0-9() .-]{7,40}", phone)):
            flash("Enter a valid contact phone number.", "error")
        else:
            db = connect_db()
            try:
                cursor = db.execute(
                    """INSERT INTO users (name, email, password_hash, role)
                       VALUES (?, ?, ?, ?)""",
                    (name, email, generate_password_hash(password), role),
                )
                user_id = cursor.lastrowid
                if role == "donor":
                    db.execute(
                        """INSERT INTO donors
                           (user_id, organization_name, organization_type, phone)
                           VALUES (?, ?, ?, ?)""",
                        (user_id, organization, organization_type, phone),
                    )
                else:
                    people_served = int(request.form.get("people_served", "0") or 0)
                    if not 0 <= people_served <= 1000000:
                        raise ValueError("Expected people served must be between 0 and 1,000,000.")
                    db.execute(
                        """INSERT INTO recipients
                           (user_id, organization_name, organization_type, people_served, phone)
                           VALUES (?, ?, ?, ?, ?)""",
                        (user_id, organization, organization_type, people_served, phone),
                    )
                db.execute(
                    "INSERT INTO locations (user_id, city) VALUES (?, ?)",
                    (user_id, city),
                )
                audit(db, user_id, "registered", "user", user_id)
                db.commit()
            except sqlite3.IntegrityError:
                db.rollback()
                flash("An account with that email already exists.", "error")
            except ValueError as error:
                db.rollback()
                flash(str(error), "error")
            else:
                session.clear()
                session["user_id"] = user_id
                flash("Your account is ready. Welcome to FoodBridge!", "success")
                return redirect(url_for("dashboard"))

    return render_template("auth.html", mode="register", selected_role=selected_role)


@app.route("/login", methods=["GET", "POST"])
def login():
    if g.user is not None:
        return redirect(url_for("dashboard"))

    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        db = connect_db()
        user = db.execute(
            "SELECT id, password_hash FROM users WHERE email = ?", (email,)
        ).fetchone()
        if user is None or not check_password_hash(user["password_hash"], password):
            flash("Email or password is incorrect.", "error")
        else:
            session.clear()
            session["user_id"] = user["id"]
            flash("You are now logged in.", "success")
            return redirect(url_for("dashboard"))

    return render_template("auth.html", mode="login", selected_role="recipient")


@app.get("/auth/google")
def google_login():
    if google_client is None:
        flash("Google sign-in is not configured yet.", "error")
        return redirect(url_for("login"))
    role = request.args.get("role", "")
    session["google_signup_role"] = role if role in ("donor", "recipient") else ""
    callback_url = url_for("google_callback", _external=True)
    return google_client.authorize_redirect(callback_url)


@app.get("/auth/google/callback")
def google_callback():
    if google_client is None:
        flash("Google sign-in is not configured yet.", "error")
        return redirect(url_for("login"))

    try:
        token = google_client.authorize_access_token()
    except OAuthError:
        app.logger.warning("Google sign-in was cancelled or could not be completed")
        flash("Google sign-in could not be completed. Please try again.", "error")
        return redirect(url_for("login"))

    profile = token.get("userinfo")
    if not profile:
        app.logger.warning("Google sign-in did not return verified profile information")
        flash("Google did not provide account information. Please try again.", "error")
        return redirect(url_for("login"))

    google_sub = profile.get("sub")
    email = str(profile.get("email", "")).strip().lower()
    name = str(profile.get("name", "")).strip()
    if (
        not google_sub
        or not email
        or len(email) > 254
        or not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email)
        or profile.get("email_verified") is not True
    ):
        flash("Google must provide a verified email address to sign in.", "error")
        return redirect(url_for("login"))

    db = connect_db()
    user = db.execute(
        "SELECT id, email FROM users WHERE google_sub = ?", (google_sub,)
    ).fetchone()
    if user is None:
        user = db.execute(
            "SELECT id, google_sub FROM users WHERE email = ?", (email,)
        ).fetchone()
        if user is not None:
            if user["google_sub"] not in (None, google_sub):
                flash("That email is linked to a different Google account.", "error")
                return redirect(url_for("login"))
            db.execute(
                "UPDATE users SET google_sub = ? WHERE id = ?",
                (google_sub, user["id"]),
            )
            audit(db, user["id"], "linked", "google_account", user["id"])
            db.commit()

    if user is not None:
        session.clear()
        session["user_id"] = user["id"]
        flash("You are now signed in with Google.", "success")
        return redirect(url_for("dashboard"))

    session["pending_google_profile"] = {
        "sub": google_sub,
        "email": email,
        "name": name[:100] or email.split("@", 1)[0][:100],
    }
    return redirect(url_for("complete_google_profile"))


@app.route("/auth/google/complete", methods=["GET", "POST"])
def complete_google_profile():
    profile = session.get("pending_google_profile")
    if not profile:
        flash("Start Google sign-in to create your account.", "error")
        return redirect(url_for("register"))

    if request.method == "POST":
        role = request.form.get("role", "")
        organization = request.form.get("organization", "").strip()
        organization_type = request.form.get("organization_type", "").strip()
        city = request.form.get("city", "").strip()
        phone = request.form.get("phone", "").strip()
        allowed_types = {
            "donor": {
                "Restaurant",
                "Grocery store",
                "Farm",
                "Caterer",
                "Individual",
                "Other",
            },
            "recipient": {
                "Food bank",
                "Community kitchen",
                "Shelter",
                "School",
                "Individual",
                "Other",
            },
        }

        try:
            people_served = int(request.form.get("people_served", "0") or 0)
        except ValueError:
            people_served = -1

        if role not in allowed_types:
            flash("Choose whether you want to donate or receive food.", "error")
        elif not organization or len(organization) > 120 or not city or len(city) > 100:
            flash("Enter your name or organisation name and city.", "error")
        elif organization_type not in allowed_types[role]:
            flash("Choose a valid organisation type.", "error")
        elif phone and not re.fullmatch(r"[+0-9() .-]{7,40}", phone):
            flash("Enter a valid contact phone number.", "error")
        elif role == "recipient" and not 0 <= people_served <= 1000000:
            flash("People supported must be between 0 and 1,000,000.", "error")
        else:
            db = connect_db()
            try:
                cursor = db.execute(
                    """INSERT INTO users (name, email, password_hash, google_sub, role)
                       VALUES (?, ?, ?, ?, ?)""",
                    (
                        profile["name"],
                        profile["email"],
                        generate_password_hash(secrets.token_urlsafe(48)),
                        profile["sub"],
                        role,
                    ),
                )
                user_id = cursor.lastrowid
                if role == "donor":
                    db.execute(
                        """INSERT INTO donors
                           (user_id, organization_name, organization_type, phone)
                           VALUES (?, ?, ?, ?)""",
                        (user_id, organization, organization_type, phone),
                    )
                else:
                    db.execute(
                        """INSERT INTO recipients
                           (user_id, organization_name, organization_type, people_served, phone)
                           VALUES (?, ?, ?, ?, ?)""",
                        (user_id, organization, organization_type, people_served, phone),
                    )
                db.execute(
                    "INSERT INTO locations (user_id, city) VALUES (?, ?)",
                    (user_id, city),
                )
                audit(db, user_id, "registered_with_google", "user", user_id)
                db.commit()
            except sqlite3.IntegrityError:
                db.rollback()
                flash("An account with that email already exists. Try signing in again.", "error")
                return redirect(url_for("login"))

            session.clear()
            session["user_id"] = user_id
            flash("Your FoodBridge account is ready.", "success")
            return redirect(url_for("dashboard"))

    return render_template(
        "google_profile.html",
        profile=profile,
        selected_role=session.get("google_signup_role", ""),
    )


@app.post("/logout")
@role_required("donor", "recipient", "admin")
def logout():
    session.clear()
    flash("You have logged out.", "success")
    return redirect(url_for("home"))


@app.route("/dashboard")
@role_required("donor", "recipient", "admin")
def dashboard():
    db = connect_db()
    user = g.user
    if user["role"] == "admin":
        counts = {
            "users": db.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"],
            "donations": db.execute(
                "SELECT COUNT(*) AS n FROM donations WHERE status IN ('available', 'partially_allocated')"
            ).fetchone()["n"],
            "requests": db.execute(
                "SELECT COUNT(*) AS n FROM requests WHERE status IN ('open', 'partially_fulfilled')"
            ).fetchone()["n"],
            "allocations": db.execute(
                "SELECT COUNT(*) AS n FROM allocations"
            ).fetchone()["n"],
        }
        users = db.execute(
            """SELECT id, name, email, role, created_at
               FROM users ORDER BY created_at DESC LIMIT 100"""
        ).fetchall()
        allocations = db.execute(
            """SELECT a.*, d.food_name, d.quantity_unit, donor.organization_name AS donor_name,
                      recipient.organization_name AS recipient_name
               FROM allocations a
               JOIN donations d ON d.id = a.donation_id
               JOIN donors donor ON donor.user_id = d.donor_id
               JOIN requests r ON r.id = a.request_id
               JOIN recipients recipient ON recipient.user_id = r.recipient_id
               ORDER BY a.created_at DESC LIMIT 100"""
        ).fetchall()
        logs = db.execute(
            """SELECT audit_logs.*, users.name AS user_name
               FROM audit_logs LEFT JOIN users ON users.id = audit_logs.user_id
               ORDER BY audit_logs.created_at DESC LIMIT 50"""
        ).fetchall()
        category_report = db.execute(
            """SELECT d.category, d.quantity_unit,
                      COUNT(a.id) AS allocations,
                      ROUND(COALESCE(SUM(a.allocated_quantity), 0), 1) AS quantity,
                      SUM(CASE WHEN a.status = 'collected' THEN 1 ELSE 0 END) AS collected
               FROM donations d
               LEFT JOIN allocations a ON a.donation_id = d.id
               GROUP BY d.category, d.quantity_unit
               ORDER BY quantity DESC, d.category LIMIT 20"""
        ).fetchall()
        return render_template(
            "dashboard.html",
            counts=counts,
            users=users,
            allocations=allocations,
            logs=logs,
            category_report=category_report,
            role="admin",
        )

    if user["role"] == "donor":
        profile = db.execute(
            """SELECT donors.organization_name, donors.organization_type, donors.phone,
                      locations.city
               FROM donors JOIN locations ON locations.user_id = donors.user_id
               WHERE donors.user_id = ?""",
            (user["id"],),
        ).fetchone()
        donations = db.execute(
            """SELECT donations.*,
                      (SELECT COUNT(*) FROM allocations WHERE donation_id = donations.id) AS allocation_count
               FROM donations WHERE donor_id = ?
               ORDER BY created_at DESC""",
            (user["id"],),
        ).fetchall()
        recommendations = []
        for donation in donations:
            if donation["status"] in ("available", "partially_allocated"):
                recommendations.extend(
                    get_recommendations(db, donation=donation, limit=3)
                )
        allocations = db.execute(
            """SELECT a.*, d.food_name, d.quantity_unit, d.storage_method, d.pickup_start,
                      d.pickup_end, d.pickup_notes, d.packaging,
                      r.category, recipient.organization_name AS partner_name,
                      recipient.phone AS partner_phone
               FROM allocations a
               JOIN donations d ON d.id = a.donation_id
               JOIN requests r ON r.id = a.request_id
               JOIN recipients recipient ON recipient.user_id = r.recipient_id
               WHERE d.donor_id = ? ORDER BY a.created_at DESC""",
            (user["id"],),
        ).fetchall()
        donor_metrics = {
            "active": sum(
                item["status"] in ("available", "partially_allocated")
                for item in donations
            ),
            "allocations": len(allocations),
            "collected": sum(item["status"] == "collected" for item in allocations),
        }
        return render_template(
            "dashboard.html",
            role="donor",
            profile=profile,
            metrics=donor_metrics,
            donations=donations,
            recommendations=recommendations,
            allocations=allocations,
        )

    profile = db.execute(
        """SELECT recipients.organization_name, recipients.organization_type,
                  recipients.people_served, recipients.phone, locations.city
           FROM recipients JOIN locations ON locations.user_id = recipients.user_id
           WHERE recipients.user_id = ?""",
        (user["id"],),
    ).fetchone()
    requests = db.execute(
        """SELECT * FROM requests WHERE recipient_id = ?
           ORDER BY created_at DESC""",
        (user["id"],),
    ).fetchall()
    recommendations = []
    for food_request in requests:
        if food_request["status"] in ("open", "partially_fulfilled"):
            recommendations.extend(
                get_recommendations(db, food_request=food_request, limit=3)
            )
    allocations = db.execute(
        """SELECT a.*, d.food_name, d.category, d.quantity_unit, d.storage_method,
                  d.pickup_start, d.pickup_end, d.pickup_notes, d.packaging,
                  donor.organization_name AS partner_name, donor.phone AS partner_phone
           FROM allocations a
           JOIN donations d ON d.id = a.donation_id
           JOIN requests r ON r.id = a.request_id
           JOIN donors donor ON donor.user_id = d.donor_id
           WHERE r.recipient_id = ? ORDER BY a.created_at DESC""",
        (user["id"],),
    ).fetchall()
    recipient_metrics = {
        "open": sum(
            item["status"] in ("open", "partially_fulfilled")
            for item in requests
        ),
        "allocations": len(allocations),
        "collected": sum(item["status"] == "collected" for item in allocations),
    }
    return render_template(
        "dashboard.html",
        role="recipient",
        profile=profile,
        metrics=recipient_metrics,
        requests=requests,
        recommendations=recommendations,
        allocations=allocations,
    )


@app.route("/reports/allocations.csv")
@role_required("admin")
def export_allocations():
    db = connect_db()
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(
        [
            "Allocation ID",
            "Food",
            "Category",
            "Donor organisation",
            "Recipient organisation",
            "Quantity",
            "Unit",
            "Match score",
            "Status",
            "Created at",
            "Collected at",
            "Match explanation",
        ]
    )
    rows = db.execute(
        """SELECT a.id, d.food_name, d.category, donor.organization_name,
                  recipient.organization_name, a.allocated_quantity, d.quantity_unit,
                  a.match_score, a.status, a.created_at, a.collected_at, a.explanation
           FROM allocations a
           JOIN donations d ON d.id = a.donation_id
           JOIN donors donor ON donor.user_id = d.donor_id
           JOIN requests r ON r.id = a.request_id
           JOIN recipients recipient ON recipient.user_id = r.recipient_id
           ORDER BY a.created_at DESC"""
    )
    writer.writerows(
        [csv_safe(value) for value in row]
        for row in rows
    )
    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=foodbridge-allocations.csv"},
    )


@app.route("/donations/new", methods=["GET", "POST"])
@role_required("donor")
def new_donation():
    db = connect_db()
    if request.method == "POST":
        food_name = request.form.get("food_name", "").strip()
        category = request.form.get("category", "").strip().lower()
        dietary_tags = request.form.get("dietary_tags", "").strip().lower()
        allergens = request.form.get("allergens", "").strip().lower()
        quantity_unit = request.form.get("quantity_unit", "")
        storage_method = request.form.get("storage_method", "")
        packaging = request.form.get("packaging", "").strip()
        pickup_notes = request.form.get("pickup_notes", "").strip()
        prepared_at_value = request.form.get("prepared_at", "").strip()
        try:
            quantity = float(request.form.get("quantity", ""))
            if not 0 < quantity <= 100000 or round(quantity, 1) != quantity:
                raise ValueError("Quantity must be between 0.1 and 100,000 units, in 0.1 increments.")
            expires_at = parse_datetime(
                request.form.get("expires_at", ""), "expiry time"
            )
            pickup_start = parse_datetime(
                request.form.get("pickup_start", ""), "pickup window start"
            )
            pickup_end = parse_datetime(
                request.form.get("pickup_end", ""), "pickup window end"
            )
            prepared_at = None
            if prepared_at_value:
                prepared_at = datetime.fromisoformat(prepared_at_value)
                if prepared_at.tzinfo is not None or prepared_at > datetime.now():
                    raise ValueError("Preparation time must be in the past and use local time.")
        except ValueError as error:
            flash(str(error), "error")
        else:
            if quantity_unit not in {"kg", "portions", "boxes"}:
                flash("Choose a valid quantity unit.", "error")
            elif storage_method not in {"ambient", "refrigerated", "frozen"}:
                flash("Choose a valid storage method.", "error")
            elif pickup_start >= pickup_end:
                flash("Pickup window must end after it starts.", "error")
            elif pickup_end > expires_at:
                flash("Pickup window must end before the food expires.", "error")
            elif request.form.get("safety_confirmed") != "yes":
                flash("Confirm the food handling declaration before publishing.", "error")
            elif not food_name or not category:
                flash("Enter a food name and category.", "error")
            elif len(food_name) > 120 or len(category) > 60:
                flash("Food name or category is too long.", "error")
            elif len(packaging) > 200 or len(pickup_notes) > 500:
                flash("Packaging or pickup details are too long.", "error")
            else:
                cursor = db.execute(
                    """INSERT INTO donations
                       (donor_id, food_name, category, quantity, remaining_quantity,
                        dietary_tags, allergens, quantity_unit, storage_method,
                        packaging, prepared_at, pickup_start, pickup_end,
                        pickup_notes, safety_confirmed, expires_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        g.user["id"],
                        food_name,
                        category,
                        quantity,
                        quantity,
                        dietary_tags,
                        allergens,
                        quantity_unit,
                        storage_method,
                        packaging,
                        prepared_at.isoformat(timespec="minutes") if prepared_at else None,
                        pickup_start,
                        pickup_end,
                        pickup_notes,
                        1,
                        expires_at,
                    ),
                )
                donation_id = cursor.lastrowid
                audit(db, g.user["id"], "created", "donation", donation_id, food_name)
                db.commit()
                flash("Your food listing has been added.", "success")
                return redirect(url_for("dashboard"))
    return render_template("form.html", mode="donation")


@app.route("/requests/new", methods=["GET", "POST"])
@role_required("recipient")
def new_request():
    db = connect_db()
    if request.method == "POST":
        category = request.form.get("category", "").strip().lower()
        dietary_requirements = request.form.get(
            "dietary_requirements", ""
        ).strip().lower()
        excluded_allergens = request.form.get(
            "excluded_allergens", ""
        ).strip().lower()
        quantity_unit = request.form.get("quantity_unit", "")
        storage_preference = request.form.get("storage_preference", "")
        collection_notes = request.form.get("collection_notes", "").strip()
        try:
            quantity = float(request.form.get("quantity", ""))
            if not 0 < quantity <= 100000 or round(quantity, 1) != quantity:
                raise ValueError("Quantity must be between 0.1 and 100,000 units, in 0.1 increments.")
            deadline = parse_datetime(
                request.form.get("collection_deadline", ""), "collection deadline"
            )
            pickup_start = parse_datetime(
                request.form.get("pickup_start", ""), "preferred pickup start"
            )
            pickup_end = parse_datetime(
                request.form.get("pickup_end", ""), "preferred pickup end"
            )
            urgency = int(request.form.get("urgency", ""))
            people_served = int(request.form.get("people_served", ""))
            if urgency not in range(1, 6):
                raise ValueError("Choose an urgency from 1 (normal) to 5 (critical).")
            if not 1 <= people_served <= 1000000:
                raise ValueError("People to support must be between 1 and 1,000,000.")
        except ValueError as error:
            flash(str(error), "error")
        else:
            if quantity_unit not in {"kg", "portions", "boxes"}:
                flash("Choose a valid quantity unit.", "error")
            elif storage_preference not in {
                "any",
                "ambient",
                "refrigerated",
                "frozen",
            }:
                flash("Choose a valid storage preference.", "error")
            elif pickup_start >= pickup_end:
                flash("Preferred pickup window must end after it starts.", "error")
            elif pickup_end > deadline:
                flash("Preferred pickup window must end before your collection deadline.", "error")
            elif not category:
                flash("Enter a food category.", "error")
            elif len(category) > 60:
                flash("Food category is too long.", "error")
            elif len(collection_notes) > 500:
                flash("Collection notes are too long.", "error")
            else:
                cursor = db.execute(
                    """INSERT INTO requests
                       (recipient_id, category, quantity, remaining_quantity,
                        dietary_requirements, excluded_allergens, quantity_unit,
                        storage_preference, people_served, urgency, pickup_start,
                        pickup_end, collection_deadline, collection_notes)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        g.user["id"],
                        category,
                        quantity,
                        quantity,
                        dietary_requirements,
                        excluded_allergens,
                        quantity_unit,
                        storage_preference,
                        people_served,
                        urgency,
                        pickup_start,
                        pickup_end,
                        deadline,
                        collection_notes,
                    ),
                )
                request_id = cursor.lastrowid
                audit(db, g.user["id"], "created", "request", request_id, category)
                db.commit()
                flash("Your food request has been added.", "success")
                return redirect(url_for("dashboard"))
    return render_template("form.html", mode="request")


@app.post("/allocations")
@role_required("donor")
def create_allocation():
    try:
        donation_id = int(request.form.get("donation_id", ""))
        request_id = int(request.form.get("request_id", ""))
        quantity = float(request.form.get("quantity", ""))
        if quantity <= 0 or round(quantity, 1) != quantity:
            raise ValueError
    except ValueError:
        flash("Enter a valid allocation quantity.", "error")
        return redirect(url_for("dashboard"))

    db = connect_db()
    try:
        db.execute("BEGIN IMMEDIATE")
        donation = db.execute(
            """SELECT * FROM donations
               WHERE id = ? AND donor_id = ?""",
            (donation_id, g.user["id"]),
        ).fetchone()
        food_request = db.execute(
            "SELECT * FROM requests WHERE id = ?", (request_id,)
        ).fetchone()
        if donation is None or food_request is None:
            raise ValueError("The selected food or request is no longer available.")
        if donation["status"] not in ("available", "partially_allocated") or food_request[
            "status"
        ] not in ("open", "partially_fulfilled"):
            raise ValueError("That food or request has already been closed.")
        if quantity > donation["remaining_quantity"] or quantity > food_request[
            "remaining_quantity"
        ]:
            raise ValueError("Quantity exceeds the available food or outstanding request.")

        recommendations = get_recommendations(
            db, donation=donation, food_request=food_request
        )
        if not recommendations:
            raise ValueError("This food is not eligible for the selected request.")
        recommendation = recommendations[0]
        existing = db.execute(
            """SELECT id, status FROM allocations
               WHERE donation_id = ? AND request_id = ?""",
            (donation_id, request_id),
        ).fetchone()
        if existing and existing["status"] == "collected":
            raise ValueError("This food and request are already marked as collected.")
        if existing:
            db.execute(
                """UPDATE allocations
                   SET allocated_quantity = allocated_quantity + ?,
                       match_score = ?, explanation = ?
                   WHERE id = ?""",
                (
                    quantity,
                    recommendation["score"],
                    recommendation["explanation"],
                    existing["id"],
                ),
            )
        else:
            db.execute(
                """INSERT INTO allocations
                   (donation_id, request_id, allocated_quantity, match_score, explanation)
                   VALUES (?, ?, ?, ?, ?)""",
                (
                    donation_id,
                    request_id,
                    quantity,
                    recommendation["score"],
                    recommendation["explanation"],
                ),
            )
        donation_remaining = round(donation["remaining_quantity"] - quantity, 1)
        request_remaining = round(food_request["remaining_quantity"] - quantity, 1)
        donation_remaining = max(0, donation_remaining)
        request_remaining = max(0, request_remaining)
        donation_status = "fully_allocated" if donation_remaining <= 0 else "partially_allocated"
        request_status = "fulfilled" if request_remaining <= 0 else "partially_fulfilled"
        db.execute(
            "UPDATE donations SET remaining_quantity = ?, status = ? WHERE id = ?",
            (donation_remaining, donation_status, donation_id),
        )
        db.execute(
            "UPDATE requests SET remaining_quantity = ?, status = ? WHERE id = ?",
            (request_remaining, request_status, request_id),
        )
        audit(
            db,
            g.user["id"],
            "allocated",
            "allocation",
            donation_id,
            f"{quantity:g} units allocated to request {request_id}",
        )
        db.commit()
    except ValueError as error:
        db.rollback()
        flash(str(error), "error")
        return redirect(url_for("dashboard"))
    except sqlite3.Error:
        db.rollback()
        app.logger.exception("Could not save food allocation")
        flash("The allocation could not be saved. Please try again.", "error")
        return redirect(url_for("dashboard"))

    flash("The allocation was recorded.", "success")
    return redirect(url_for("dashboard"))


@app.post("/allocations/<int:allocation_id>/collected")
@role_required("donor", "recipient")
def mark_collected(allocation_id):
    db = connect_db()
    allocation = db.execute(
        """SELECT a.id, a.status, d.donor_id, r.recipient_id
           FROM allocations a
           JOIN donations d ON d.id = a.donation_id
           JOIN requests r ON r.id = a.request_id
           WHERE a.id = ?""",
        (allocation_id,),
    ).fetchone()
    if allocation is None or g.user["id"] not in (
        allocation["donor_id"],
        allocation["recipient_id"],
    ):
        flash("That allocation was not found.", "error")
    elif allocation["status"] == "collected":
        flash("This allocation is already marked as collected.", "error")
    else:
        db.execute(
            """UPDATE allocations SET status = 'collected', collected_at = CURRENT_TIMESTAMP
               WHERE id = ?""",
            (allocation_id,),
        )
        audit(db, g.user["id"], "collected", "allocation", allocation_id)
        db.commit()
        flash("The food collection has been recorded.", "success")
    return redirect(url_for("dashboard"))


@app.post("/donations/<int:donation_id>/cancel")
@role_required("donor")
def cancel_donation(donation_id):
    db = connect_db()
    cursor = db.execute(
        """UPDATE donations SET status = 'cancelled'
           WHERE id = ? AND donor_id = ?
             AND status IN ('available', 'partially_allocated')""",
        (donation_id, g.user["id"]),
    )
    if cursor.rowcount == 0:
        flash("That listing cannot be cancelled.", "error")
    else:
        audit(db, g.user["id"], "cancelled", "donation", donation_id)
        db.commit()
        flash("The remaining food listing has been cancelled.", "success")
    return redirect(url_for("dashboard"))


@app.post("/requests/<int:request_id>/cancel")
@role_required("recipient")
def cancel_request(request_id):
    db = connect_db()
    cursor = db.execute(
        """UPDATE requests SET status = 'cancelled'
           WHERE id = ? AND recipient_id = ?
             AND status IN ('open', 'partially_fulfilled')""",
        (request_id, g.user["id"]),
    )
    if cursor.rowcount == 0:
        flash("That request cannot be cancelled.", "error")
    else:
        audit(db, g.user["id"], "cancelled", "request", request_id)
        db.commit()
        flash("The outstanding request has been cancelled.", "success")
    return redirect(url_for("dashboard"))


if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "5000")),
        debug=False,
    )
