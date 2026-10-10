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

CREATE TABLE IF NOT EXISTS dismissed_alerts (
    item_id INTEGER NOT NULL,
    kind TEXT NOT NULL,
    dismissed_at TEXT NOT NULL,
    PRIMARY KEY (item_id, kind)
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
        ("series", "TEXT"),
    ],
    "categories": [
        ("kind", "TEXT"),
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
    ("Comics", "#2fd8ff", 1, "comics"),
    ("Manga", "#9b7bff", 1, "manga"),
    ("Omnibus / collected editions", "#6aa8ff", 1, "comics"),
    ("Pokémon cards", "#ffcb3c", 0, "cards"),
    ("Pokémon sealed products", "#ff9f43", 0, "cards"),
    ("Magic: The Gathering", "#ff4d8d", 0, "cards"),
    ("Funko Pop", "#3cf2a6", 0, "figures"),
    ("DVD / Blu-ray", "#c77dff", 0, "film"),
    ("Books", "#8fd3c9", 0, "manga"),
    ("Vinyl", "#f07167", 0, "vinyl"),
    ("Coins", "#d4a373", 0, "general"),
    ("Other", "#7c89ad", 0, "general"),
]


# What kind of thing a category holds. Decides the words used for it (the
# Series / Set box, Top titles) and which pages its preset turns on.
#   group: what a run of related items is called; item: one of them.
#   numbered: items carry issue numbers, so Price creep works for them.
KINDS = {
    "comics":  {"name": "Comics",           "examples": "Comics, omnibuses",       "group": "Series",    "item": "issue",   "numbered": True},
    "manga":   {"name": "Manga and books",  "examples": "Manga, books, novels",    "group": "Series",    "item": "volume",  "numbered": True},
    "cards":   {"name": "Trading cards",    "examples": "Pokémon, Magic, sealed",  "group": "Set",       "item": "card",    "numbered": False},
    "figures": {"name": "Figures and toys", "examples": "Funko, statues, Lego",    "group": "Line",      "item": "figure",  "numbered": False},
    "vinyl":   {"name": "Vinyl and music",  "examples": "Records, CDs, cassettes", "group": "Artist",    "item": "release", "numbered": False},
    "film":    {"name": "Films and TV",     "examples": "DVD, Blu-ray, 4K",        "group": "Franchise", "item": "release", "numbered": False},
    "games":   {"name": "Video games",      "examples": "Games, consoles",         "group": "Platform",  "item": "game",    "numbered": False},
    "general": {"name": "Something else",   "examples": "Coins, anything",         "group": "Group",     "item": "item",    "numbered": False},
}
DEFAULT_KIND = "general"

# First match wins; checked against the category's name in lower case.
_KIND_HINTS = [
    ("cards",   ("pokémon", "pokemon", "magic", "mtg", "yu-gi-oh", "yugioh", "lorcana", "one piece card",
                 "trading card", "tcg", "booster", "sealed")),
    ("manga",   ("manga", "book", "novel", "light novel")),
    ("comics",  ("comic", "omnibus", "graphic novel", "trade paperback", "tpb", "collected edition")),
    ("figures", ("funko", "figure", "statue", "toy", "lego", "action figure", "pop!")),
    ("vinyl",   ("vinyl", "record", "music", "cd", "cassette", "album")),
    ("film",    ("dvd", "blu-ray", "bluray", "4k", "film", "movie", "steelbook")),
    ("games",   ("game", "console", "nintendo", "playstation", "xbox", "switch")),
]


def guess_kind(name, has_series=False):
    """A sensible kind for a category from its name. Unknown names with
    Numbered issues switched on count as comics, everything else General."""
    low = (name or "").lower()
    for kind, words in _KIND_HINTS:
        if any(w in low for w in words):
            return kind
    return "comics" if has_series else DEFAULT_KIND


def _backfill_category_kinds(conn):
    """Categories from before v3.4 have no kind yet: give each one a
    guess from its name. Changeable in Settings afterwards."""
    rows = conn.execute("SELECT id, name, has_series FROM categories WHERE kind IS NULL OR kind = ''").fetchall()
    for r in rows:
        conn.execute("UPDATE categories SET kind = ? WHERE id = ?", (guess_kind(r["name"], r["has_series"]), r["id"]))

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
    for i, (name, color, has_series, kind) in enumerate(STARTER_CATEGORIES):
        conn.execute(
            "INSERT INTO categories (name, color, has_series, sort_order, created_at, kind) VALUES (?, ?, ?, ?, ?, ?)",
            (name, color, has_series, i, now, kind),
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


RETIRED_DASHBOARD_CARDS = ("dash.rings", "dash.year", "dash.backup", "dash.stillorder")


def _retire_old_dashboard_cards(conn):
    """v3.3 replaced four Dashboard cards with the hero and key figures. A
    Dashboard card order saved before then would put the new cards last, so
    it's reset (the Dashboard goes back to its default order); the other
    pages' orders are untouched. Hidden-card entries for the old cards are
    dropped too."""
    import json
    row = conn.execute("SELECT value FROM settings WHERE key = 'card_order'").fetchone()
    if row:
        try:
            orders = json.loads(row[0] or "{}")
        except ValueError:
            orders = {}
        if isinstance(orders, dict) and any(c in RETIRED_DASHBOARD_CARDS for c in orders.get("/", [])):
            orders.pop("/", None)
            conn.execute("UPDATE settings SET value = ? WHERE key = 'card_order'", (json.dumps(orders),))
    row = conn.execute("SELECT value FROM settings WHERE key = 'hidden_cards'").fetchone()
    if row:
        try:
            hidden = json.loads(row[0] or "[]")
        except ValueError:
            hidden = []
        if isinstance(hidden, list) and any(c in RETIRED_DASHBOARD_CARDS for c in hidden):
            hidden = [c for c in hidden if c not in RETIRED_DASHBOARD_CARDS]
            conn.execute("UPDATE settings SET value = ? WHERE key = 'hidden_cards'", (json.dumps(hidden),))


def init_db():
    conn = get_db()
    conn.executescript(SCHEMA)
    _migrate(conn)
    # Older import screens saved the word "None" as a note; clear it.
    conn.execute("UPDATE items SET note = NULL WHERE note = 'None'")
    _retire_old_dashboard_cards(conn)
    _backfill_sync_columns(conn)
    _seed_categories(conn)
    _backfill_category_kinds(conn)
    _backfill_categories(conn)
    conn.commit()
    conn.close()
