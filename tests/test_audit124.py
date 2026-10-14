"""Realport 124: deep-audit fixes."""
import sqlite3
from datetime import date, timedelta

from app import core, db
from .conftest import add_item


def _postage(path, order, amounts):
    conn = sqlite3.connect(path)
    for i, a in enumerate(amounts):
        conn.execute("INSERT INTO shipment_postage (order_number, shipment_index, amount, captured_at, source) VALUES (?,?,?,?,?)",
                     (order, i, a, "2026-01-01", "Forbidden Planet"))
    conn.commit()
    conn.close()


def _parcel(path, order, d):
    conn = db.get_db()
    try:
        return core.exact_parcel_postage(conn.cursor(), {order}, d)
    finally:
        conn.close()


def test_order_postage_is_shared_across_its_parcels(fresh_db):
    t = date.today()
    for k in (10, 17, 24):
        add_item(fresh_db, f"Saga #{k}", 3.99, k, order_number="111")
    _postage(fresh_db, "111", [4.99, 3.50, 5.99])
    assert _parcel(fresh_db, "111", (t + timedelta(days=10)).isoformat()) == 4.99
    assert _parcel(fresh_db, "111", (t + timedelta(days=24)).isoformat()) == 5.99


def test_fewer_figures_than_parcels_split_evenly(fresh_db):
    t = date.today()
    for k in (10, 17, 24, 31):
        add_item(fresh_db, f"Saga #{k}", 3.99, k, order_number="222")
    _postage(fresh_db, "222", [6.00, 6.00])
    assert _parcel(fresh_db, "222", (t + timedelta(days=17)).isoformat()) == 3.00


def test_split_delivery_on_one_date_counts_in_full(fresh_db):
    t = date.today()
    add_item(fresh_db, "Saga #1", 3.99, 5, order_number="333")
    add_item(fresh_db, "Saga #2", 3.99, 5, order_number="333")
    _postage(fresh_db, "333", [2.50, 1.50])
    assert _parcel(fresh_db, "333", (t + timedelta(days=5)).isoformat()) == 4.00


def test_unknown_order_falls_back_to_estimate(fresh_db):
    add_item(fresh_db, "Saga #1", 3.99, 5, order_number="444")
    assert _parcel(fresh_db, "444", (date.today() + timedelta(days=5)).isoformat()) is None


def _cat(path, name):
    return sqlite3.connect(path).execute("SELECT id FROM categories WHERE name = ?", (name,)).fetchone()[0]


def test_all_caps_series_groups_with_normal_spelling():
    assert core.series_from_name("STAR WARS: DOCTOR APHRA #3") == "Star Wars: Doctor Aphra"
    assert core.series_from_name("Saga #2") == "Saga"


def test_price_creep_only_offers_numbered_categories(client, fresh_db):
    cards = _cat(fresh_db, "Pokémon cards")
    add_item(fresh_db, "Grogu #368", 12.0, -40, category=cards)
    add_item(fresh_db, "Grogu #369", 14.0, -20, category=cards)
    page = client.get("/insights/price-creep").text
    assert "Numbered issues" in page and "Grogu" not in page


def test_manga_uses_volumes_on_price_creep(client, fresh_db):
    manga = _cat(fresh_db, "Manga")
    client.post(f"/settings/categories/{manga}/kind", data={"kind": "manga"})
    add_item(fresh_db, "One Piece #1", 6.99, -60, category=manga)
    add_item(fresh_db, "One Piece #2", 7.99, -30, category=manga)
    page = client.get("/insights/price-creep").text
    assert "2 volumes tracked" in page and "issues tracked" not in page


def test_three_kinds_all_named_on_top_titles(client, fresh_db):
    ids = [_cat(fresh_db, n) for n in ("Comics", "Pokémon cards", "Vinyl")]
    add_item(fresh_db, "Saga #1", 3.99, -10, category=ids[0])
    add_item(fresh_db, "ETB", 44.99, -10, category=ids[1])
    add_item(fresh_db, "Romance LP", 24.99, -10, category=ids[2])
    page = client.get("/insights/top-titles?cats=" + ",".join(map(str, ids))).text
    assert "Series / Set / Artist" in page and "series, sets and artists" in page


def test_dashboard_all_time_includes_shipping(client, fresh_db):
    add_item(fresh_db, "Saga #1", 10.00, -10, charge_status="charged", order_number="555")
    _postage(fresh_db, "555", [2.50])
    page = client.get("/").text
    assert "£12.50" in page


def test_price_buckets_use_the_chosen_currency(client, fresh_db):
    from .conftest import set_setting
    set_setting(fresh_db, "currency_symbol", "usd")
    add_item(fresh_db, "Saga #1", 3.99, -10)
    page = client.get("/insights").text
    assert "$0–10" in page and "£0" not in page


def test_overview_counts_items_dated_only_by_order_date(client, fresh_db):
    from datetime import date as _d, timedelta as _t
    add_item(fresh_db, "Old order #1", 20.00, None, charge_status="charged", status="dispatched",
             placed=_d.today() - _t(days=40), order_number="777")
    _postage(fresh_db, "777", [3.00])
    page = client.get("/insights").text
    assert "£23.00 total, all time" in page
    assert "£23.00 with what" in client.get("/").text


def test_variant_only_issue_doesnt_fake_a_price_change(client, fresh_db):
    add_item(fresh_db, "Legacy Of Vader #8 (Kael Ngu Variant)", 12.65, -90)
    add_item(fresh_db, "Legacy Of Vader #11", 2.65, -60)
    add_item(fresh_db, "Legacy Of Vader #12", 2.65, -30)
    page = client.get("/insights/price-creep").text
    assert "-79%" not in page and "£12.65 &rarr;" not in page


def test_free_items_are_not_unpaid(client, fresh_db):
    add_item(fresh_db, "Freebie #1", 0.0, -20, status="released")
    page = client.get("/orders?tab=unpaid").text
    assert "Freebie" not in page


def test_failing_notifications_are_flagged(client, fresh_db):
    from .conftest import set_setting
    set_setting(fresh_db, "notify_provider", "ntfy")
    conn = sqlite3.connect(fresh_db)
    for i in range(3):
        conn.execute("INSERT INTO notification_log (sent_at, title, message, provider, success, error) VALUES (?,?,?,?,?,?)",
                     (f"2026-10-0{i+1}T08:00", "Ka-Ching!", "x", "ntfy", 0, "<urlopen error timed out>"))
    conn.commit(); conn.close()
    assert "Notifications aren&#39;t getting through" in client.get("/alerts").text or "Notifications aren't getting through" in client.get("/alerts").text
    assert "Notifications failing since" in client.get("/").text
    assert core.friendly_send_error("<urlopen error timed out>").startswith("Couldn't reach the server")


def test_upgrade_switches_numbered_on_for_unused_manga_only(tmp_path, monkeypatch):
    path = str(tmp_path / "old.db")
    monkeypatch.setattr(db, "DB_PATH", path)
    db.init_db()
    conn = sqlite3.connect(path)
    conn.execute("UPDATE categories SET has_series = 0 WHERE name IN ('Manga', 'Books')")
    conn.execute("DELETE FROM settings WHERE key = 'numbered_defaults_v34'")
    books = conn.execute("SELECT id FROM categories WHERE name = 'Books'").fetchone()[0]
    conn.commit(); conn.close()
    add_item(path, "Dune", 9.99, -10, category=books)
    db.init_db()
    rows = dict(sqlite3.connect(path).execute("SELECT name, has_series FROM categories").fetchall())
    assert rows["Manga"] == 1 and rows["Books"] == 0 and rows["Funko Pop"] == 0
