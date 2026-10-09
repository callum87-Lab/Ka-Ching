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


def test_dashboard_hero_and_figures(client, seeded):
    page = client.get("/").text
    assert 'data-card="dash.hero"' in page and 'data-card="dash.figures"' in page
    assert "Still due this month" in page and "Needs attention" in page
    for gone in ("dash.rings", "dash.year", "dash.backup", "dash.stillorder"):
        assert f'data-card="{gone}"' not in page


def test_backup_line_in_alerts(client, seeded):
    page = client.get("/").text
    assert "backup-line bad" in page and "Automatic backups are off" in page   # off by default in tests
    client.post("/settings/auto-backup/on", data={"next": "/"})
    page = client.get("/").text
    assert "backup-line ok" in page and "first backup tonight" in page


def test_spend_chart_shows_the_budget_line(client, seeded):
    page = client.get("/").text
    assert "bars-svg" in page and "Budget £100" in page


def test_price_creep_chart_matches_the_headline(client, fresh_db):
    """A rise more than a year ago must still show in the running total, so
    the chart's end point equals the all-time figure in the headline."""
    import re, json, sqlite3
    con = sqlite3.connect(fresh_db)
    rows = [("Creep Test #1", 3.00, "2024-03-06"), ("Creep Test #2", 3.50, "2024-04-03"),
            ("Creep Test #3", 3.50, "2024-05-01"), ("Steady Test #1", 4.00, "2026-01-07"),
            ("Steady Test #2", 4.00, "2026-02-04")]
    for name, price, rel in rows:
        con.execute("INSERT INTO items (name, price, release_date, status, charge_status, order_number, source, imported_at) "
                    "VALUES (?, ?, ?, 'dispatched', 'charged', 'T1', 'Forbidden Planet', '2026-01-01T00:00:00')", (name, price, rel))
    con.commit()
    page = client.get("/insights/price-creep").text
    series = [json.loads(m) for m in re.findall(r"var data = (\[.*?\]);", page)]
    data = [d for d in series if d and "month_key" in d[0]][0]
    assert data[0]["month_key"] <= "2024-04"                  # starts at the first rise
    assert data[-1]["value"] == 1.0                            # two issues, 50p dearer each
    assert "£1.00" in page and "all time, vs first-tracked prices" in page
    assert 'class="creep-strip"' in page and "haven't changed price" in page


def test_spend_by_shop_uses_theme_colours(client, seeded):
    import re, json
    page = client.get("/insights/spend-by-shop").text
    data = json.loads(re.search(r"var fullData = (\{.*?\});", page, re.S).group(1))
    colours = {s["name"]: s["color"] for s in data["series"]}
    assert colours["Forbidden Planet"] == "var(--shop-1)" and colours["eBay"] == "var(--shop-2)"


def test_spending_chart_bars_and_line(client, seeded):
    page = client.get("/").text
    assert "bars-svg" in page and "line-svg" in page and 'id="chart-style"' in page
    assert "Still due" in page and 'data-due="' in page
