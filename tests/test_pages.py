"""Every page loads - on a brand-new empty install and with data."""
import pytest

PAGES = ["/", "/orders", "/calendar", "/search", "/search?q=test", "/items/new",
         "/insights", "/insights/spend-by-shop", "/insights/price-creep",
         "/insights/top-titles", "/alerts", "/settings"]


@pytest.mark.parametrize("path", PAGES)
def test_page_loads_on_empty_install(client, path):
    r = client.get(path)
    assert r.status_code == 200
    assert "Ka-Ching" in r.text


@pytest.mark.parametrize("path", PAGES)
def test_page_loads_with_data(client, seeded, path):
    r = client.get(path)
    assert r.status_code == 200


def test_item_edit_page(client, seeded):
    assert client.get("/items/1/edit").status_code == 200


@pytest.mark.parametrize("old,new", [
    ("/classic/", "/"), ("/classic/insights", "/insights"),
    ("/v2/dashboard", "/"), ("/v2/orders?source=eBay", "/orders?source=eBay"),
])
def test_old_addresses_redirect(client, old, new):
    r = client.get(old, follow_redirects=False)
    assert r.status_code == 301
    assert r.headers["location"] == new


def test_exports(client, seeded):
    csv = client.get("/settings/export-all.csv")
    assert csv.status_code == 200
    assert "Category" in csv.text.splitlines()[0]
    ics = client.get("/calendar/export.ics")
    assert ics.status_code == 200
    assert "BEGIN:VCALENDAR" in ics.text
    assert client.get("/sw.js").headers["content-type"].startswith("application/javascript")


def test_sidebar_search_and_alert_count(client, seeded):
    page = client.get("/orders").text
    assert 'class="sb-search"' in page and 'action="/search"' in page
    assert "Advanced search" in page
    assert 'class="nav-badge"' in page          # the seeded data has an alert (an undated item)
    assert 'id="sb-collapse"' in page
