import os
import sqlite3
import sys

import psycopg
from flask import Flask
from psycopg.rows import dict_row


TABLES = (
    "users",
    "donors",
    "recipients",
    "locations",
    "donations",
    "requests",
    "allocations",
    "audit_logs",
    "password_reset_tokens",
)
SEQUENCE_TABLES = (
    "users",
    "locations",
    "donations",
    "requests",
    "allocations",
    "audit_logs",
    "password_reset_tokens",
)


def main():
    if len(sys.argv) != 2:
        raise SystemExit(
            "Usage: python migrate_sqlite_to_postgres.py PATH_TO_SQLITE_DATABASE"
        )

    database_url = os.environ.get("DATABASE_URL", "").strip()
    if not database_url:
        raise SystemExit("Set DATABASE_URL to the destination PostgreSQL database.")

    from database import initialize_database

    migration_app = Flask(__name__)
    admin_environment = {
        key: os.environ.pop(key, None) for key in ("ADMIN_EMAIL", "ADMIN_PASSWORD")
    }
    try:
        with migration_app.app_context():
            initialize_database()
    finally:
        for key, value in admin_environment.items():
            if value is not None:
                os.environ[key] = value

    source_path = sys.argv[1]
    with sqlite3.connect(source_path) as source, psycopg.connect(
        database_url, connect_timeout=10, row_factory=dict_row
    ) as destination:
        source.row_factory = sqlite3.Row
        for table in TABLES:
            if destination.execute(
                f"SELECT EXISTS (SELECT 1 FROM {table}) AS has_rows"
            ).fetchone()["has_rows"]:
                raise SystemExit(
                    f"The PostgreSQL {table} table is not empty. "
                    "Import only into a new, empty database."
                )

        for table in TABLES:
            rows = source.execute(f"SELECT * FROM {table}").fetchall()
            if not rows:
                continue
            columns = rows[0].keys()
            column_sql = ", ".join(columns)
            placeholders = ", ".join("%s" for _ in columns)
            insert_sql = (
                f"INSERT INTO {table} ({column_sql}) VALUES ({placeholders})"
            )
            destination.executemany(
                insert_sql,
                [tuple(row[column] for column in columns) for row in rows],
            )

        for table in SEQUENCE_TABLES:
            destination.execute(
                """SELECT setval(
                       pg_get_serial_sequence(%s, 'id'),
                       COALESCE(MAX(id), 1),
                       MAX(id) IS NOT NULL
                   )
                   FROM """ + table,
                (table,),
            )

        destination.commit()
        print("SQLite data imported successfully.")


if __name__ == "__main__":
    main()
