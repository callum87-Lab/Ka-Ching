import os
import sqlite3

DB_PATH = os.environ.get("DB_PATH", "/data/pullcost.db")

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


def init_db():
    conn = get_db()
    conn.executescript(SCHEMA)
    _migrate(conn)
    conn.commit()
    conn.close()
