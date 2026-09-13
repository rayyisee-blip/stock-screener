"""
Shared database connection helper.

Both the ingest jobs (ingest/) and the Streamlit app (app/) import this
module to talk to the same Supabase Postgres database. Keeping the
connection logic in exactly one place means we only have to get it right
once, and both sides always agree on how to connect.

We use psycopg2 directly (no ORM) because db/schema.sql is plain SQL and
this project intentionally avoids ORMs/migration tools.
"""

import os

import psycopg2
from psycopg2.extensions import connection as PGConnection


def get_connection() -> PGConnection:
    """
    Open a new connection to the Supabase Postgres database.

    The connection string is read from the DATABASE_URL environment
    variable only - never hard-coded and never committed to git. Locally,
    set it in your shell before running a script (see README.md). In
    GitHub Actions and Streamlit Community Cloud, it comes from a secret.

    Returns a plain psycopg2 connection. Callers are responsible for
    closing it (a "with get_connection() as conn:" block does this
    automatically) and for calling conn.commit() after writes.
    """
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        raise RuntimeError(
            "DATABASE_URL environment variable is not set. "
            "Copy .env.example to .env, fill in your Supabase connection "
            "string (Supabase dashboard -> Project Settings -> Database -> "
            "Connection string -> URI), and load it into your shell before "
            "running this code. See README.md for step-by-step instructions."
        )
    return psycopg2.connect(database_url)
