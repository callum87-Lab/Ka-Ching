import os
import sqlite3
import uuid
from datetime import datetime, timezone

DB_PATH = os.environ.get("DB_PATH", "/data/kaching.db")


def new_uuid() -> str:
    return str(uuid.uuid4())


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    order_number TEXT,
    placed_date TEXT,
    status TEXT NOT NULL,
    release_date TEXT,
    charge_status TEXT,
    price REAL NOT NULL,
    note TEXT,
    imported_at TEXT NOT NULL,
    manual_override INTEGER NOT NULL DEFAULT 0,
    prev_status TEXT,
    prev_charge_status TEXT,
    source TEXT NOT NULL DEFAULT 'Forbidden Planet',
    UNIQUE(order_number, name, price)
);

CREATE TABLE IF NOT EXISTS orders (
    order_number TEXT PRIMARY KEY,
    declared_total REAL,
    last_seen_at TEXT
);

CREATE TABLE IF NOT EXISTS dismissed_duplicates (
    name TEXT NOT NULL,
    release_date TEXT NOT NULL,
    dismissed_at TEXT NOT NULL,
    PRIMARY KEY (name, release_date)
);

CREATE TABLE IF NOT EXISTS shipment_postage (
    order_number TEXT NOT NULL,
    shipment_index INTEGER NOT NULL,
    amount REAL NOT NULL,
    captured_at TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'Forbidden Planet',
    PRIMARY KEY (order_number, shipment_index)
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT
);

-- One row per app/device that has ever synced with this server. Lets the
-- server hand back "everything changed since your last checkpoint" instead
-- of the app having to re-fetch the whole item table on every sync.
CREATE TABLE IF NOT EXISTS sync_state (
    client_id TEXT PRIMARY KEY,
    client_label TEXT,
    last_synced_at TEXT,
    created_at TEXT NOT NULL
);

-- A lightweight audit trail for manual edits only (not routine
-- parser/import refreshes, which are expected to update things and
-- would just add noise) - so an overwritten value isn't just gone.
CREATE TABLE IF NOT EXISTS item_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id INTEGER NOT NULL,
    changed_at TEXT NOT NULL,
    field_name TEXT NOT NULL,
    old_value TEXT,
    new_value TEXT
);

-- A record of every notification actually sent (or attempted), so the
-- person can confirm the system is really firing rather than just
-- trusting it silently in the background.
CREATE TABLE IF NOT EXISTS notification_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sent_at TEXT NOT NULL,
    title TEXT NOT NULL,
    message TEXT NOT NULL,
    provider TEXT,
    success INTEGER NOT NULL,
    error TEXT
);
"""

# Columns added after each table's initial release. Listed here so an
# existing database gets upgraded in place instead of needing to be deleted
# and re-imported from scratch.
MIGRATIONS = {
    "items": [
        ("manual_override", "INTEGER NOT NULL DEFAULT 0"),
        ("prev_status", "TEXT"),
        ("prev_charge_status", "TEXT"),
        ("source", "TEXT NOT NULL DEFAULT 'Forbidden Planet'"),
        ("tracking_number", "TEXT"),
        ("prev_release_date", "TEXT"),
        ("uuid", "TEXT"),
        ("updated_at", "TEXT"),
        ("deleted_at", "TEXT"),
    ],
    "shipment_postage": [
        ("source", "TEXT NOT NULL DEFAULT 'Forbidden Planet'"),
    ],
}


def get_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _migrate(conn):
    for table, columns in MIGRATIONS.items():
        cur = conn.execute(f"PRAGMA table_info({table})")
        existing = {row["name"] for row in cur.fetchall()}
        for col_name, col_def in columns:
            if col_name not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col_name} {col_def}")


def _backfill_sync_columns(conn):
    """Existing installs will have items with uuid/updated_at still NULL
    right after the ALTER TABLE above adds the columns. Give every such
    row a real uuid and a best-guess updated_at (falling back to
    imported_at, which is NOT NULL and always present) so the sync
    endpoint has something correct to compare against from day one,
    rather than treating pre-existing data as "never changed"."""
    rows = conn.execute("SELECT id, imported_at FROM items WHERE uuid IS NULL").fetchall()
    for row in rows:
        conn.execute(
            "UPDATE items SET uuid = ? WHERE id = ?",
            (new_uuid(), row["id"]),
        )
    conn.execute(
        "UPDATE items SET updated_at = imported_at WHERE updated_at IS NULL"
    )
    # Unique index rather than a UNIQUE column constraint - SQLite can't add
    # a column-level UNIQUE via ALTER TABLE, and this only needs to be safe
    # to (re)create once every row above has a real uuid.
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_items_uuid ON items(uuid)")


def init_db():
    conn = get_db()
    conn.executescript(SCHEMA)
    _migrate(conn)
    _backfill_sync_columns(conn)
    conn.commit()
    conn.close()
