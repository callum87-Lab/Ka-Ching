"""The budget and alert figures - and that every place showing them agrees."""
import re

from app import core, db
from .conftest import add_item, set_setting


def _status():
    db.request_cache  # (no request scope in tests: each call works it out fresh)
    return core._topbar_budget_status()


def test_weekly_budget_counts_items_due_this_week(seeded):
    set_setting(seeded, "budget_cycle", "weekly")
    s = _status()
    # due today..+6 and not cancelled: 4.25 + 4.50 + 12.99
    assert round(s["cycle_spend"], 2) == 21.74


def test_28_day_budget_counts_the_last_28_days(seeded):
    set_setting(seeded, "budget_cycle", "28day")
    s = _status()
    # released in the last 28 days up to today: 3.99 (the -40 day item is outside)
    assert round(s["cycle_spend"], 2) == 3.99


def test_categories_add_up_to_the_budget_figure(seeded):
    conn = db.get_db()
    cur = conn.cursor()
    from datetime import date
    _, spend = core._category_cycle_spend(cur, date.today())
    conn.close()
    s = _status()
    assert round(sum(v["total"] for v in spend.values()), 2) == round(s["cycle_spend"], 2)


def test_dashboard_ring_matches_sidebar(client, seeded):
    page = client.get("/").text
    s = _status()
    assert f"{s['cycle_spend']:.2f}" in page


def test_alert_counts(seeded):
    from datetime import date
    conn = db.get_db()
    counts = core.alert_counts(conn.cursor(), date.today())
    conn.close()
    assert counts["undated"] == 1
    assert counts["total"] == sum(v for k, v in counts.items() if k != "total")


def test_bell_shows_the_alert_total(client, seeded):
    from datetime import date
    conn = db.get_db()
    total = core.alert_counts(conn.cursor(), date.today())["total"]
    conn.close()
    assert re.search(r'class="bell-count"[^>]*>\s*%d\s*<' % total, client.get("/orders").text)


def test_undated_item_listed_in_alerts(client, seeded):
    page = client.get("/alerts").text
    assert "No release date" in page and "No Date Yet #2" in page


def test_category_limit_over_shows_in_alerts(client, seeded):
    set_setting(seeded, "category_limits", '{"1": 1.0}')
    page = client.get("/alerts").text
    assert "Over a category limit" in page
