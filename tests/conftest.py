"""Shared fixtures: every test gets its own empty database file, so tests
never touch real data and never depend on each other."""
import os
import sqlite3
import tempfile
from datetime import date, timedelta

import pytest

# Point the app at a throwaway location before it's imported.
os.environ.setdefault("DB_PATH", os.path.join(tempfile.mkdtemp(), "kaching.db"))
os.environ.pop("KACHING_PASSWORD", None)
os.environ.pop("KACHING_PHONE_SYNC", None)

from fastapi.testclient import TestClient  # noqa: E402

from app import core, db  # noqa: E402
from app.main import app  # noqa: E402


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    path = str(tmp_path / "kaching.db")
    monkeypatch.setattr(db, "DB_PATH", path)
    core._login_fails.clear()
    db.init_db()
    return path


@pytest.fixture
def client(fresh_db):
    with TestClient(app) as c:
        yield c


def add_item(path, name, price, release, source="Forbidden Planet", status="preorder",
             charge_status="not_charged", order_number="12345678", category=None, placed=None):
    """Insert one item directly. release may be a date, a day offset from
    today (int), or None for an undated item."""
    if isinstance(release, int):
        release = date.today() + timedelta(days=release)
    conn = sqlite3.connect(path)
    cols = ["name", "price", "release_date", "placed_date", "source", "status", "charge_status", "order_number", "imported_at"]
    vals = [name, price, release.isoformat() if release else None,
            placed.isoformat() if placed else None, source, status, charge_status, order_number, db.utc_now()]
    if category is not None:
        cols.append("category_id")
        vals.append(category)
    conn.execute(f"INSERT INTO items ({', '.join(cols)}) VALUES ({', '.join('?' * len(vals))})", vals)
    conn.commit()
    conn.close()


def set_setting(path, key, value):
    conn = sqlite3.connect(path)
    conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, value))
    conn.commit()
    conn.close()


@pytest.fixture
def seeded(fresh_db):
    """A small, predictable set of items around today's date."""
    p = fresh_db
    add_item(p, "Test Series #1", 3.99, -2)
    add_item(p, "Test Series #2", 4.25, 1)
    add_item(p, "Test Series #3", 4.50, 3)
    add_item(p, "Other Book #1 (Variant)", 12.99, 2, source="eBay", order_number=None, charge_status="charged")
    add_item(p, "Old Thing #1", 5.00, -40)
    add_item(p, "Cancelled Thing #1", 9.99, 1, status="cancelled")
    add_item(p, "No Date Yet #2", 6.00, None)
    set_setting(p, "monthly_budget", "100")
    return p
