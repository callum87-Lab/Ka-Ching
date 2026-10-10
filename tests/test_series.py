"""v3.4: the Series / Set field and a kind per category."""
import sqlite3

from app import core, db, parser
from .conftest import add_item
from .test_bughunt import _review_form, PASTE


def _q(path, sql, *a):
    return sqlite3.connect(path).execute(sql, a).fetchall()


def test_new_install_categories_have_kinds_and_no_gold(fresh_db):
    kinds = dict(_q(fresh_db, "SELECT name, kind FROM categories"))
    assert kinds["Comics"] == "comics" and kinds["Pokémon cards"] == "cards" and kinds["Funko Pop"] == "figures"
    assert "Gold / silver" not in kinds
    assert all(k in db.KINDS for k in kinds.values())


def test_old_categories_get_a_guessed_kind(fresh_db):
    conn = sqlite3.connect(fresh_db)
    conn.execute("INSERT INTO categories (name, color, has_series, sort_order) VALUES ('Yu-Gi-Oh singles', '#fff', 0, 50)")
    conn.execute("INSERT INTO categories (name, color, has_series, sort_order) VALUES ('Mystery stuff', '#fff', 1, 51)")
    conn.execute("UPDATE categories SET kind = NULL")
    conn.commit(); conn.close()
    db.init_db()
    kinds = dict(_q(fresh_db, "SELECT name, kind FROM categories"))
    assert kinds["Yu-Gi-Oh singles"] == "cards" and kinds["Mystery stuff"] == "comics" and kinds["Vinyl"] == "vinyl"


def test_series_helpers():
    assert core.series_from_name("Star Wars: Jedi Knights #3 (Variant)") == "Star Wars: Jedi Knights"
    assert core.series_from_name("Fusion Strike Elite Trainer Box") is None
    assert core.item_series({"name": "X #2", "series": None}, "comics") == "X"
    assert core.item_series({"name": "X #2", "series": None}, "cards") is None
    assert core.item_series({"name": "ETB", "series": "  Fusion   Strike "}, "cards") == "Fusion Strike"


def test_manual_entry_saves_series(client, fresh_db):
    client.post("/items/new", data={"release_date": "2026-11-01", "source": "Magic Madhouse",
                                    "name": ["Fusion Strike ETB", "Comic #1"], "category_id": ["Pokémon sealed products", "Comics"],
                                    "series": ["Fusion Strike", ""], "price": ["44.99", "3.99"]})
    assert dict(_q(fresh_db, "SELECT name, series FROM items")) == {"Fusion Strike ETB": "Fusion Strike", "Comic #1": None}


def test_edit_saves_series_with_history(client, fresh_db):
    add_item(fresh_db, "Silver Tempest booster", 4.5, 10)
    item_id = _q(fresh_db, "SELECT id FROM items")[0][0]
    page = client.get(f"/items/{item_id}/edit").text
    assert 'name="series"' in page and 'class="series-label"' in page
    client.post(f"/items/{item_id}/edit", data={"name": "Silver Tempest booster", "price": "4.5", "release_date": "2026-11-01",
                                                "source": "Forbidden Planet", "series": "Silver Tempest", "category_id": "Pokémon cards"})
    assert _q(fresh_db, "SELECT series FROM items")[0][0] == "Silver Tempest"
    assert _q(fresh_db, "SELECT new_value FROM item_history WHERE field_name = 'series'")[0][0] == "Silver Tempest"


def test_import_review_has_series_boxes_and_saves_them(client, fresh_db):
    page, form = _review_form(client, PASTE)
    assert 'name="series_0"' in page
    form["series_0"] = "Double Import Test"
    client.post("/import/confirm", data=form)
    rows = dict(_q(fresh_db, "SELECT name, series FROM items"))
    assert rows["Star Wars: Double Import Test #1"] == "Double Import Test"
    assert rows["Star Wars: Double Import Test #2"] is None


def test_forbidden_planet_store_keeps_series_and_never_overwrites_it(fresh_db):
    from datetime import date
    item = {"name": "Thing #1", "order_number": "123", "placed_date": None, "status": "preorder",
            "release_date": date(2026, 12, 1), "charge_status": "not_charged", "price": 3.0, "note": None, "series": "Typed"}
    parser.store_parsed_items([item], {})
    parser.store_parsed_items([dict(item, series="Other")], {})
    assert _q(fresh_db, "SELECT series FROM items") == [("Typed",)]


def test_changing_a_category_kind_sets_numbered_issues(client, fresh_db):
    cat_id = _q(fresh_db, "SELECT id FROM categories WHERE name = 'Funko Pop'")[0][0]
    client.post(f"/settings/categories/{cat_id}/kind", data={"kind": "comics"})
    assert _q(fresh_db, "SELECT kind, has_series FROM categories WHERE id = ?", cat_id) == [("comics", 1)]
    client.post(f"/settings/categories/{cat_id}/kind", data={"kind": "nonsense"})
    assert _q(fresh_db, "SELECT kind FROM categories WHERE id = ?", cat_id) == [("comics",)]
    assert 'name="kind"' in client.get("/settings").text


def test_new_typed_category_gets_a_kind(client, fresh_db):
    client.post("/settings/categories/add", data={"name": "Lorcana cards"})
    assert _q(fresh_db, "SELECT kind, has_series FROM categories WHERE name = 'Lorcana cards'") == [("cards", 0)]


def test_csv_export_has_series_column(client, fresh_db):
    add_item(fresh_db, "Star Wars: Jedi Knights #3", 3.99, 5)
    csv_text = client.get("/settings/export-all.csv").text
    assert "Series / Set" in csv_text.splitlines()[0] and "Star Wars: Jedi Knights" in csv_text.splitlines()[1]
