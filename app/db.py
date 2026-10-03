import contextvars
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

CREATE TABLE IF NOT EXISTS categories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE COLLATE NOCASE,
    color TEXT NOT NULL,
    has_series INTEGER NOT NULL DEFAULT 0,
    sort_order INTEGER NOT NULL DEFAULT 0,
    created_at TEXT
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
        ("category_id", "INTEGER"),
    ],
    "shipment_postage": [
        ("source", "TEXT NOT NULL DEFAULT 'Forbidden Planet'"),
    ],
}


# One database connection per page load. Every helper that renders part
# of a page (budget box, bell count, login check, layout settings...) used
# to open its own connection - 14 to 21 per page. Inside a request, get_db()
# now hands out the same connection; its close() is a no-op and the real
# connection is closed when the request finishes. Outside a request (the
# daily scheduler, startup) it behaves exactly as before.
_request_scope = contextvars.ContextVar("kaching_request_scope", default=None)


class _SharedConnection:
    def __init__(self, conn):
        self._conn = conn

    def close(self):
        pass  # closed once, at the end of the request

    def __getattr__(self, name):
        return getattr(self._conn, name)

    def __enter__(self):
        return self._conn.__enter__()

    def __exit__(self, *exc):
        return self._conn.__exit__(*exc)


def begin_request():
    """Start a request scope (called by the request middleware)."""
    return _request_scope.set({"conn": None, "cache": {}})


def end_request(token):
    scope = _request_scope.get()
    if scope and scope["conn"] is not None:
        try:
            scope["conn"].close()
        except Exception:
            pass
    _request_scope.reset(token)


def reset_request_connection():
    """Drop this request's shared connection (e.g. after the database file
    has been replaced by a restore), so the next get_db() opens a fresh one."""
    scope = _request_scope.get()
    if scope and scope["conn"] is not None:
        try:
            scope["conn"].close()
        except Exception:
            pass
        scope["conn"] = None
        scope["cache"].clear()


def request_cache():
    """A dict that lives for the current request only (None outside one),
    for values read many times per page - settings, alert counts, the budget."""
    scope = _request_scope.get()
    return scope["cache"] if scope else None


def get_db():
    scope = _request_scope.get()
    if scope is not None:
        if scope["conn"] is None:
            scope["conn"] = _connect(check_same_thread=False)
        return _SharedConnection(scope["conn"])
    return _connect()


def _connect(check_same_thread=True):
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=15.0, check_same_thread=check_same_thread)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    # WAL lets readers (e.g. the dashboard loading) and a writer (e.g. a
    # large first-time sync inserting hundreds of items) proceed without
    # blocking each other - the default rollback journal locks the whole
    # file for the duration of a write, which is what caused "database
    # is locked" errors during a big sync. busy_timeout is a second,
    # SQLite-level backstop alongside the connection's own Python timeout.
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 15000")
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


# Starter categories, seeded once on first run. Comics comes first and is
# the default, since every item that existed before categories did was a
# comic. After seeding, the list is the user's to edit in Settings.
STARTER_CATEGORIES = [
    ("Comics", "#2fd8ff", 1),
    ("Manga", "#9b7bff", 1),
    ("Omnibus / collected editions", "#6aa8ff", 1),
    ("Pokémon cards", "#ffcb3c", 0),
    ("Pokémon sealed products", "#ff9f43", 0),
    ("Magic: The Gathering", "#ff4d8d", 0),
    ("Funko Pop", "#3cf2a6", 0),
    ("DVD / Blu-ray", "#c77dff", 0),
    ("Books", "#8fd3c9", 0),
    ("Vinyl", "#f07167", 0),
    ("Coins", "#d4a373", 0),
    ("Gold / silver", "#e9c46a", 0),
    ("Other", "#7c89ad", 0),
]

# Colours handed out, in order, to categories the user adds themselves.
EXTRA_CATEGORY_COLORS = [
    "#5ec8ff", "#b388ff", "#ff8fab", "#80ed99", "#ffd166", "#90e0ef",
    "#f4a261", "#e5989b", "#a0c4ff", "#caffbf",
]


def _seed_categories(conn):
    """First run only: fill the categories table with the starter list.
    Never touches an existing list, so user edits survive restarts."""
    count = conn.execute("SELECT COUNT(*) FROM categories").fetchone()[0]
    if count:
        return
    now = utc_now()
    for i, (name, color, has_series) in enumerate(STARTER_CATEGORIES):
        conn.execute(
            "INSERT INTO categories (name, color, has_series, sort_order, created_at) VALUES (?, ?, ?, ?, ?)",
            (name, color, has_series, i, now),
        )


def default_category_id(conn):
    """The category new items get when none is chosen (sync from an app
    that doesn't know about categories yet, older code paths). Stored as a
    setting so it survives renames; falls back to the first category."""
    row = conn.execute("SELECT value FROM settings WHERE key = 'default_category_id'").fetchone()
    if row:
        try:
            cat_id = int(row[0])
            if conn.execute("SELECT 1 FROM categories WHERE id = ?", (cat_id,)).fetchone():
                return cat_id
        except (TypeError, ValueError):
            pass
    first = conn.execute("SELECT id FROM categories ORDER BY sort_order, id LIMIT 1").fetchone()
    return first[0] if first else None


def _backfill_categories(conn):
    """Every item without a category gets the default one. On the first
    run after upgrading, that's all existing items - which were all comics.
    Afterwards it only catches items added by code paths that don't set a
    category themselves (e.g. sync from an older app version)."""
    default_id = default_category_id(conn)
    if default_id is None:
        return
    row = conn.execute("SELECT value FROM settings WHERE key = 'default_category_id'").fetchone()
    if not row:
        conn.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES ('default_category_id', ?)",
            (str(default_id),),
        )
    conn.execute("UPDATE items SET category_id = ? WHERE category_id IS NULL", (default_id,))
    conn.execute("CREATE INDEX IF NOT EXISTS idx_items_category ON items(category_id)")


def init_db():
    conn = get_db()
    conn.executescript(SCHEMA)
    _migrate(conn)
    _backfill_sync_columns(conn)
    _seed_categories(conn)
    _backfill_categories(conn)
    conn.commit()
    conn.close()
