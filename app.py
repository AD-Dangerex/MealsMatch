import hmac
import os
import re
import secrets
import sqlite3
from datetime import datetime
from functools import wraps

from flask import Flask, flash, g, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

from allocation import get_recommendations
from database import connect_db, initialize_database


app = Flask(__name__)
app.config.update(
    SECRET_KEY=os.environ.get("SESSION_SECRET") or secrets.token_hex(32),
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=bool(os.environ.get("RENDER")),
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

    return {"csrf_token": csrf_token, "current_user": g.get("user")}


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
    return render_template("index.html")


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
        else:
            db = connect_db()
            try:
                cursor = db.execute(
                    """INSERT INTO users (name, email, password_hash, role)
                       VALUES (?, ?, ?, ?)""",
                    (name, email, generate_password_hash(password), role),
                )
                user_id = cursor.lastrowid
                profile_table = "donors" if role == "donor" else "recipients"
                db.execute(
                    f"INSERT INTO {profile_table} (user_id, organization_name) VALUES (?, ?)",
                    (user_id, organization),
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
            """SELECT a.*, d.food_name, donor.organization_name AS donor_name,
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
        return render_template(
            "dashboard.html",
            counts=counts,
            users=users,
            allocations=allocations,
            logs=logs,
            role="admin",
        )

    if user["role"] == "donor":
        donations = db.execute(
            """SELECT * FROM donations WHERE donor_id = ?
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
            """SELECT a.*, d.food_name, r.category, recipient.organization_name AS partner_name
               FROM allocations a
               JOIN donations d ON d.id = a.donation_id
               JOIN requests r ON r.id = a.request_id
               JOIN recipients recipient ON recipient.user_id = r.recipient_id
               WHERE d.donor_id = ? ORDER BY a.created_at DESC""",
            (user["id"],),
        ).fetchall()
        return render_template(
            "dashboard.html",
            role="donor",
            donations=donations,
            recommendations=recommendations,
            allocations=allocations,
        )

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
        """SELECT a.*, d.food_name, d.category, donor.organization_name AS partner_name
           FROM allocations a
           JOIN donations d ON d.id = a.donation_id
           JOIN requests r ON r.id = a.request_id
           JOIN donors donor ON donor.user_id = d.donor_id
           WHERE r.recipient_id = ? ORDER BY a.created_at DESC""",
        (user["id"],),
    ).fetchall()
    return render_template(
        "dashboard.html",
        role="recipient",
        requests=requests,
        recommendations=recommendations,
        allocations=allocations,
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
        try:
            quantity = float(request.form.get("quantity", ""))
            if not 0 < quantity <= 100000 or round(quantity, 1) != quantity:
                raise ValueError("Quantity must be between 0.1 and 100,000 kg, in 0.1 kg increments.")
            expires_at = parse_datetime(
                request.form.get("expires_at", ""), "expiry time"
            )
        except ValueError as error:
            flash(str(error), "error")
        else:
            if not food_name or not category:
                flash("Enter a food name and category.", "error")
            elif len(food_name) > 120 or len(category) > 60:
                flash("Food name or category is too long.", "error")
            else:
                cursor = db.execute(
                    """INSERT INTO donations
                       (donor_id, food_name, category, quantity, remaining_quantity,
                        dietary_tags, allergens, expires_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        g.user["id"],
                        food_name,
                        category,
                        quantity,
                        quantity,
                        dietary_tags,
                        allergens,
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
        try:
            quantity = float(request.form.get("quantity", ""))
            if not 0 < quantity <= 100000 or round(quantity, 1) != quantity:
                raise ValueError("Quantity must be between 0.1 and 100,000 kg, in 0.1 kg increments.")
            deadline = parse_datetime(
                request.form.get("collection_deadline", ""), "collection deadline"
            )
            urgency = int(request.form.get("urgency", ""))
            if urgency not in range(1, 6):
                raise ValueError("Choose an urgency from 1 (normal) to 5 (critical).")
        except ValueError as error:
            flash(str(error), "error")
        else:
            if not category:
                flash("Enter a food category.", "error")
            elif len(category) > 60:
                flash("Food category is too long.", "error")
            else:
                cursor = db.execute(
                    """INSERT INTO requests
                       (recipient_id, category, quantity, remaining_quantity,
                        dietary_requirements, excluded_allergens, urgency, collection_deadline)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        g.user["id"],
                        category,
                        quantity,
                        quantity,
                        dietary_requirements,
                        excluded_allergens,
                        urgency,
                        deadline,
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
