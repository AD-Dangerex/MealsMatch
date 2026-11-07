import os
import sqlite3
from pathlib import Path

from flask import current_app, g
from werkzeug.security import generate_password_hash


SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL CHECK(length(name) <= 100),
    email TEXT NOT NULL UNIQUE COLLATE NOCASE,
    password_hash TEXT NOT NULL,
    google_sub TEXT UNIQUE,
    role TEXT NOT NULL CHECK(role IN ('donor', 'recipient', 'admin')),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS donors (
    user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    organization_name TEXT NOT NULL,
    organization_type TEXT NOT NULL DEFAULT 'Other',
    phone TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS recipients (
    user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    organization_name TEXT NOT NULL,
    organization_type TEXT NOT NULL DEFAULT 'Other',
    people_served INTEGER NOT NULL DEFAULT 0,
    phone TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS locations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL UNIQUE REFERENCES users(id) ON DELETE CASCADE,
    city TEXT NOT NULL,
    address TEXT,
    latitude REAL,
    longitude REAL
);

CREATE TABLE IF NOT EXISTS donations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    donor_id INTEGER NOT NULL REFERENCES donors(user_id),
    food_name TEXT NOT NULL,
    category TEXT NOT NULL,
    quantity REAL NOT NULL CHECK(quantity > 0),
    remaining_quantity REAL NOT NULL CHECK(remaining_quantity >= 0),
    dietary_tags TEXT NOT NULL DEFAULT '',
    allergens TEXT NOT NULL DEFAULT '',
    quantity_unit TEXT NOT NULL DEFAULT 'kg',
    storage_method TEXT NOT NULL DEFAULT 'ambient',
    packaging TEXT NOT NULL DEFAULT '',
    prepared_at TEXT,
    pickup_start TEXT,
    pickup_end TEXT,
    pickup_notes TEXT NOT NULL DEFAULT '',
    safety_confirmed INTEGER NOT NULL DEFAULT 0,
    expires_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'available'
        CHECK(status IN ('available', 'partially_allocated', 'fully_allocated', 'cancelled')),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    recipient_id INTEGER NOT NULL REFERENCES recipients(user_id),
    category TEXT NOT NULL,
    quantity REAL NOT NULL CHECK(quantity > 0),
    remaining_quantity REAL NOT NULL CHECK(remaining_quantity >= 0),
    dietary_requirements TEXT NOT NULL DEFAULT '',
    excluded_allergens TEXT NOT NULL DEFAULT '',
    quantity_unit TEXT NOT NULL DEFAULT 'kg',
    storage_preference TEXT NOT NULL DEFAULT 'any',
    people_served INTEGER NOT NULL DEFAULT 0,
    urgency INTEGER NOT NULL CHECK(urgency BETWEEN 1 AND 5),
    pickup_start TEXT,
    pickup_end TEXT,
    collection_deadline TEXT NOT NULL,
    collection_notes TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'open'
        CHECK(status IN ('open', 'partially_fulfilled', 'fulfilled', 'cancelled')),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS allocations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    donation_id INTEGER NOT NULL REFERENCES donations(id),
    request_id INTEGER NOT NULL REFERENCES requests(id),
    allocated_quantity REAL NOT NULL CHECK(allocated_quantity > 0),
    match_score REAL NOT NULL CHECK(match_score BETWEEN 0 AND 100),
    explanation TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK(status IN ('pending', 'collected')),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    collected_at TEXT,
    UNIQUE(donation_id, request_id)
);

CREATE TABLE IF NOT EXISTS audit_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    action TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    entity_id INTEGER,
    details TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_donations_status_category
    ON donations(status, category);
CREATE INDEX IF NOT EXISTS idx_requests_status_category
    ON requests(status, category);
CREATE INDEX IF NOT EXISTS idx_allocations_created
    ON allocations(created_at);
CREATE INDEX IF NOT EXISTS idx_audit_logs_created
    ON audit_logs(created_at);
CREATE INDEX IF NOT EXISTS idx_users_role
    ON users(role);
"""


def database_path():
    configured = os.environ.get("DATABASE_PATH")
    if configured:
        path = Path(configured)
    else:
        path = Path(current_app.instance_path) / "foodbridge.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    legacy_path = Path(current_app.instance_path) / "foodbridge.db"
    if (
        configured
        and not path.exists()
        and legacy_path.exists()
        and path.resolve() != legacy_path.resolve()
    ):
        legacy_db = sqlite3.connect(legacy_path, timeout=10)
        persistent_db = sqlite3.connect(path, timeout=10)
        try:
            legacy_db.backup(persistent_db)
        finally:
            legacy_db.close()
            persistent_db.close()
    return path


def connect_db():
    if "db" not in g:
        g.db = sqlite3.connect(database_path(), timeout=10)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
        g.db.execute("PRAGMA busy_timeout = 10000")
    return g.db


def initialize_database():
    with current_app.app_context():
        db = sqlite3.connect(database_path(), timeout=10)
        try:
            db.execute("PRAGMA foreign_keys = ON")
            db.executescript(SCHEMA)
            migrate_database(db)
            db.execute(
                """CREATE UNIQUE INDEX IF NOT EXISTS idx_users_google_sub
                   ON users(google_sub) WHERE google_sub IS NOT NULL"""
            )
            admin_email = os.environ.get("ADMIN_EMAIL", "").strip().lower()
            admin_password = os.environ.get("ADMIN_PASSWORD", "")
            if admin_email and admin_password:
                existing = db.execute(
                    "SELECT id FROM users WHERE email = ?", (admin_email,)
                ).fetchone()
                if existing is None:
                    db.execute(
                        """INSERT INTO users (name, email, password_hash, role)
                           VALUES (?, ?, ?, 'admin')""",
                        (
                            "FoodBridge Administrator",
                            admin_email,
                            generate_password_hash(admin_password),
                        ),
                    )
            db.commit()
        finally:
            db.close()


def migrate_database(db):
    migrations = {
        "users": {
            "google_sub": "TEXT",
        },
        "donors": {
            "organization_type": "TEXT NOT NULL DEFAULT 'Other'",
            "phone": "TEXT NOT NULL DEFAULT ''",
        },
        "recipients": {
            "organization_type": "TEXT NOT NULL DEFAULT 'Other'",
            "people_served": "INTEGER NOT NULL DEFAULT 0",
            "phone": "TEXT NOT NULL DEFAULT ''",
        },
        "donations": {
            "quantity_unit": "TEXT NOT NULL DEFAULT 'kg'",
            "storage_method": "TEXT NOT NULL DEFAULT 'ambient'",
            "packaging": "TEXT NOT NULL DEFAULT ''",
            "prepared_at": "TEXT",
            "pickup_start": "TEXT",
            "pickup_end": "TEXT",
            "pickup_notes": "TEXT NOT NULL DEFAULT ''",
            "safety_confirmed": "INTEGER NOT NULL DEFAULT 0",
        },
        "requests": {
            "quantity_unit": "TEXT NOT NULL DEFAULT 'kg'",
            "storage_preference": "TEXT NOT NULL DEFAULT 'any'",
            "people_served": "INTEGER NOT NULL DEFAULT 0",
            "pickup_start": "TEXT",
            "pickup_end": "TEXT",
            "collection_notes": "TEXT NOT NULL DEFAULT ''",
        },
    }
    for table, columns in migrations.items():
        existing_columns = {
            row[1] for row in db.execute(f"PRAGMA table_info({table})")
        }
        for column, definition in columns.items():
            if column not in existing_columns:
                db.execute(
                    f"ALTER TABLE {table} ADD COLUMN {column} {definition}"
                )
