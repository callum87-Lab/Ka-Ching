"""Regression tests for the Stage 2 bug hunt fixes."""
import re
import sqlite3

from app import parser
from .conftest import add_item


def _review_form(client, text):
    page = client.post("/import", data={"order_text": text}).text
    start = page.find('/import/confirm')
    form = page[page.rfind('<form', 0, start):page.find('</form>', start)]
    data = {}
    for tag in re.findall(r'<input[^>]*>', form):
        name = re.search(r'name="([^"]+)"', tag)
        if not name or ('type="checkbox"' in tag and 'checked' not in tag):
            continue
        value = re.search(r'value="([^"]*)"', tag)
        data[name.group(1)] = value.group(1) if value else ("on" if "checkbox" in tag else "")
    return page, data


PASTE = "Order Number: 99887766\nStar Wars: Double Import Test #1\n£3.99\nStar Wars: Double Import Test #2\n£4.25\nTotal £8.24\n"


def test_importing_the_same_order_twice_skips_instead_of_crashing(client, fresh_db):
    _, form = _review_form(client, PASTE)
    assert client.post("/import/confirm", data=form, follow_redirects=False).status_code == 303
    page, form = _review_form(client, PASTE)
    assert "Already tracked in this order" in page
    r = client.post("/import/confirm", data=form, follow_redirects=False)
    assert r.status_code == 303
    count = sqlite3.connect(fresh_db).execute("SELECT COUNT(*) FROM items WHERE name LIKE '%Double Import%'").fetchone()[0]
    assert count == 2
    assert "already tracked, so skipped" in client.get("/").text


def test_review_screen_never_saves_the_word_none(client, fresh_db):
    _, form = _review_form(client, PASTE)
    assert "None" not in [v for k, v in form.items() if k.startswith(("note_", "placed_date_", "tracking_number_"))]
    client.post("/import/confirm", data=form)
    notes = sqlite3.connect(fresh_db).execute("SELECT note FROM items").fetchall()
    assert all(n[0] is None for n in notes)


def test_existing_none_notes_are_cleared(fresh_db):
    from app import db
    add_item(fresh_db, "Old Import #1", 3.0, 5)
    sqlite3.connect(fresh_db).execute("UPDATE items SET note = 'None'").connection.commit()
    db.init_db()
    assert sqlite3.connect(fresh_db).execute("SELECT note FROM items").fetchone()[0] is None


def test_names_have_no_page_furniture():
    names = [i["name"] for i in parser.parse_generic_order(PASTE)["items"]]
    assert names == ["Star Wars: Double Import Test #1", "Star Wars: Double Import Test #2"]
    t = "Order summary\nSaga #1\nQty: 1\n£3.99\nHarry Potter and the Order of the Phoenix\n£7.99\n"
    assert [i["name"] for i in parser.parse_generic_order(t)["items"]] == ["Saga #1", "Harry Potter and the Order of the Phoenix"]


def _edit(client, item_id, **changes):
    data = {"name": "Test Series #1", "price": "3.99", "release_date": "2026-12-01", "source": "Forbidden Planet", "next": "/"}
    data.update(changes)
    return client.post(f"/items/{item_id}/edit", data=data, follow_redirects=False)


def test_edit_refuses_silly_prices(client, seeded):
    for bad in ["-5", "1e308", "100000.01"]:
        _edit(client, 1, price=bad)
        assert sqlite3.connect(seeded).execute("SELECT price FROM items WHERE id = 1").fetchone()[0] == 3.99


def test_bad_date_keeps_the_old_one(client, seeded):
    before = sqlite3.connect(seeded).execute("SELECT release_date FROM items WHERE id = 1").fetchone()[0]
    _edit(client, 1, release_date="2026-13-45")
    after = sqlite3.connect(seeded).execute("SELECT release_date FROM items WHERE id = 1").fetchone()[0]
    assert after == before
    assert "t a valid date, so it was left as it was" in client.get("/").text


def test_incomplete_form_gets_a_friendly_page(client, seeded):
    r = client.post("/items/1/edit", data={"name": ""}, headers={"accept": "text/html"})
    assert r.status_code == 422 and "go through" in r.text and '{"detail"' not in r.text
    assert client.post("/settings/layout", json={"hidden": 5}).headers["content-type"].startswith("application/json")


def test_money_has_thousands_separators(client, fresh_db):
    add_item(fresh_db, "Huge Statue", 12345.67, 3)
    assert "12,345.67" in client.get("/orders").text
