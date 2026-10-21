"""v3.3.x tweaks: bulk Delay, 'Fine as it is' alerts, and the bulk bar's messages."""
import sqlite3
from datetime import date, timedelta

from tests.conftest import add_item


def _ids(path):
    return [r[0] for r in sqlite3.connect(path).execute("SELECT id FROM items ORDER BY id")]


def test_bulk_delay_moves_dated_items_and_logs_history(client, fresh_db):
    add_item(fresh_db, "A #1", 3.0, 10)
    add_item(fresh_db, "B #1", 4.0, None)          # undated: left alone
    ids = _ids(fresh_db)
    r = client.post("/items/bulk-action", data={"item_ids": [str(i) for i in ids], "bulk_action": "delay",
                                                "days": "30", "next": "/orders"}, follow_redirects=False)
    assert r.status_code == 303
    conn = sqlite3.connect(fresh_db)
    dates = dict(conn.execute("SELECT name, release_date FROM items"))
    assert dates["A #1"] == (date.today() + timedelta(days=40)).isoformat()
    assert dates["B #1"] is None
    assert conn.execute("SELECT COUNT(*) FROM item_history WHERE field_name = 'release_date'").fetchone()[0] == 1
    assert "Moved 2 items back 30 days." in client.get("/orders").text


def test_bulk_delay_rejects_other_amounts(client, fresh_db):
    add_item(fresh_db, "A #1", 3.0, 10)
    client.post("/items/bulk-action", data={"item_ids": [str(i) for i in _ids(fresh_db)], "bulk_action": "delay", "days": "7"})
    assert sqlite3.connect(fresh_db).execute("SELECT release_date FROM items").fetchone()[0] == (date.today() + timedelta(days=10)).isoformat()


def test_fine_as_it_is_stops_counting_and_can_be_put_back(client, fresh_db):
    add_item(fresh_db, "Undated thing", 5.0, None)
    add_item(fresh_db, "Ghost #1", 2.0, -5, order_number=None)
    undated, ghost = _ids(fresh_db)
    page = client.get("/alerts").text
    assert page.count('class="fine-btn"') == 2
    client.post("/alerts/fine", data={"item_id": undated, "kind": "undated"})
    client.post("/alerts/fine", data={"item_id": ghost, "kind": "ghost"})
    page = client.get("/alerts").text
    assert 'class="fine-btn"' not in page and "Marked as fine (2)" in page
    # Remove all ghost items leaves one you've marked fine
    client.post("/ghost-items/remove-all")
    assert len(_ids(fresh_db)) == 2
    client.post("/alerts/unfine", data={"item_id": undated, "kind": "undated"})
    page = client.get("/alerts").text
    assert page.count('class="fine-btn"') == 1 and "Marked as fine (1)" in page


def test_fine_ignores_unknown_kinds(client, fresh_db):
    add_item(fresh_db, "Undated thing", 5.0, None)
    client.post("/alerts/fine", data={"item_id": _ids(fresh_db)[0], "kind": "awaiting"})
    assert sqlite3.connect(fresh_db).execute("SELECT COUNT(*) FROM dismissed_alerts").fetchone()[0] == 0


def test_dismissed_duplicate_can_be_put_back(client, fresh_db):
    add_item(fresh_db, "Same #1", 3.0, 10, order_number="111")
    add_item(fresh_db, "Same #1", 3.0, 10, order_number="222")
    rd = (date.today() + timedelta(days=10)).isoformat()
    client.post("/duplicates/dismiss", data={"name": "Same #1", "release_date": rd})
    assert "Not a duplicate</span>" in client.get("/alerts").text
    client.post("/duplicates/undismiss", data={"name": "Same #1", "release_date": rd})
    assert "Possible duplicate" in client.get("/alerts").text or "Not a duplicate</button>" in client.get("/alerts").text


def test_security_tab_no_longer_mentions_classic(client, fresh_db):
    assert "/classic/" not in client.get("/settings").text
