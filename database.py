import os
import re
import sqlite3
from pathlib import Path

from flask import current_app, g
import psycopg
from psycopg.rows import dict_row
from werkzeug.security import generate_password_hash


INTEGRITY_ERRORS = (sqlite3.IntegrityError, psycopg.IntegrityError)
DATABASE_ERRORS = (sqlite3.Error, psycopg.Error)


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
    pickup_address TEXT,
    pickup_latitude REAL,
    pickup_longitude REAL,
    location_consent INTEGER NOT NULL DEFAULT 0,
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
    collection_address TEXT,
    collection_latitude REAL,
    collection_longitude REAL,
    max_distance_km REAL NOT NULL DEFAULT 25,
    location_consent INTEGER NOT NULL DEFAULT 0,
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

CREATE TABLE IF NOT EXISTS password_reset_tokens (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    token_hash TEXT NOT NULL UNIQUE,
    expires_at TEXT NOT NULL,
    used_at TEXT,
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
CREATE INDEX IF NOT EXISTS idx_password_reset_user_created
    ON password_reset_tokens(user_id, created_at);
CREATE INDEX IF NOT EXISTS idx_users_role
    ON users(role);
CREATE TABLE IF NOT EXISTS route_distance_cache (
    origin_key TEXT NOT NULL,
    destination_key TEXT NOT NULL,
    distance_meters REAL NOT NULL CHECK(distance_meters >= 0),
    duration_seconds REAL NOT NULL CHECK(duration_seconds >= 0),
    calculated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (origin_key, destination_key)
);
"""


class DatabaseConnection:
    def __init__(self, connection):
        self.connection = connection
        self.is_postgres = True

    def execute(self, query, parameters=()):
        query = query.replace("?", "%s")
        query = query.replace("BEGIN IMMEDIATE", "BEGIN")
        query = query.replace(
            "datetime(expires_at) > CURRENT_TIMESTAMP",
            "expires_at::timestamp > CURRENT_TIMESTAMP",
        )
        query = re.sub(
            r"(created_at|calculated_at) >= datetime\('now', '(-?\d+ minutes|-?\d+ days)'\)",
            lambda match: (
                match.group(1)
                + "::timestamp >= CURRENT_TIMESTAMP - INTERVAL '"
                + match.group(2).lstrip("-")
                + "'"
            ),
            query,
        )
        query = re.sub(
            r"\b(used_at|collected_at|calculated_at)\s*=\s*CURRENT_TIMESTAMP\b",
            r"\1 = CURRENT_TIMESTAMP::text",
            query,
        )
        return self.connection.execute(query, parameters)

    def executescript(self, script):
        for statement in script.split(";"):
            if statement.strip():
                self.execute(statement)

    def __getattr__(self, name):
        return getattr(self.connection, name)


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


def connect_database():
    database_url = os.environ.get("DATABASE_URL", "").strip()
    if database_url:
        connection = psycopg.connect(
            database_url,
            connect_timeout=10,
            row_factory=dict_row,
        )
        return DatabaseConnection(connection)
    if os.environ.get("RAILWAY_ENVIRONMENT"):
        raise RuntimeError(
            "Set DATABASE_URL to a persistent PostgreSQL database in the deployment environment."
        )
    return sqlite3.connect(database_path(), timeout=10)


def postgres_schema():
    return (
        SCHEMA.replace(
            "INTEGER PRIMARY KEY AUTOINCREMENT", "BIGSERIAL PRIMARY KEY"
        )
        .replace("INTEGER", "BIGINT")
        .replace(" COLLATE NOCASE", "")
        .replace(
            "TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP",
            "TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP::text)",
        )
    )


def connect_db():
    if "db" not in g:
        g.db = connect_database()
        if not isinstance(g.db, DatabaseConnection):
            g.db.row_factory = sqlite3.Row
            g.db.execute("PRAGMA foreign_keys = ON")
            g.db.execute("PRAGMA busy_timeout = 10000")
    return g.db


def initialize_database():
    with current_app.app_context():
        db = connect_database()
        try:
            if isinstance(db, DatabaseConnection):
                db.executescript(postgres_schema())
            else:
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
                            "Meals Match Administrator",
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
            "pickup_address": "TEXT",
            "pickup_latitude": "REAL",
            "pickup_longitude": "REAL",
            "location_consent": "INTEGER NOT NULL DEFAULT 0",
            "safety_confirmed": "INTEGER NOT NULL DEFAULT 0",
        },
        "requests": {
            "quantity_unit": "TEXT NOT NULL DEFAULT 'kg'",
            "storage_preference": "TEXT NOT NULL DEFAULT 'any'",
            "people_served": "INTEGER NOT NULL DEFAULT 0",
            "pickup_start": "TEXT",
            "pickup_end": "TEXT",
            "collection_notes": "TEXT NOT NULL DEFAULT ''",
            "collection_address": "TEXT",
            "collection_latitude": "REAL",
            "collection_longitude": "REAL",
            "max_distance_km": "REAL NOT NULL DEFAULT 25",
            "location_consent": "INTEGER NOT NULL DEFAULT 0",
        },
    }
    for table, columns in migrations.items():
        if isinstance(db, DatabaseConnection):
            existing_columns = {
                row["column_name"]
                for row in db.execute(
                    """SELECT column_name FROM information_schema.columns
                       WHERE table_schema = current_schema() AND table_name = %s""",
                    (table,),
                ).fetchall()
            }
        else:
            existing_columns = {
                row[1] for row in db.execute(f"PRAGMA table_info({table})")
            }
        for column, definition in columns.items():
            if column not in existing_columns:
                if isinstance(db, DatabaseConnection):
                    definition = definition.replace("INTEGER", "BIGINT")
                    db.execute(
                        f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS "
                        f"{column} {definition}"
                    )
                else:
                    db.execute(
                        f"ALTER TABLE {table} ADD COLUMN {column} {definition}"
                    )
