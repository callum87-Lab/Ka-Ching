"""Fixes for the issues CodeQL found: open redirects, slow regexes on
crafted pastes, and the login cookie over HTTPS."""
import time

import pytest
from fastapi.testclient import TestClient

from app import parser
from app.core import safe_redirect
from app.main import app

EVIL = ["https://evil.example", "//evil.example", "/\\evil.example", "\\\\evil.example",
        "http:/evil.example", "javascript:alert(1)", "/\r\nSet-Cookie:x", " //evil.example"]


@pytest.mark.parametrize("target", EVIL)
def test_safe_redirect_never_leaves_the_site(target):
    from urllib.parse import urlparse
    result = safe_redirect(target, "/fallback")
    # either refused, or reduced to a harmless path on this site
    assert result.startswith("/") and not result.startswith("//")
    assert "\\" not in result and not urlparse(result).netloc and not urlparse(result).scheme
    assert "evil.example" not in result or result == "/evil.example"


@pytest.mark.parametrize("target", ["/", "/orders", "/settings#layout", "/search?q=x&source=eBay"])
def test_safe_redirect_keeps_own_pages(target):
    assert safe_redirect(target, "/fallback") == target


@pytest.mark.parametrize("target", EVIL[:3])
def test_forms_never_redirect_off_site(client, seeded, target):
    checks = [
        client.post("/items/1/mark", data={"action": "paid", "next": target}, follow_redirects=False),
        client.post("/settings/categories/add", data={"name": "Zines", "next": target}, follow_redirects=False),
        client.post("/settings/auto-backup/on", data={"next": target}, follow_redirects=False),
        client.post("/login", data={"password": "", "next": target}, follow_redirects=False),
    ]
    for r in checks:
        loc = r.headers.get("location", "")
        assert loc.startswith("/") and not loc.startswith("//") and "\\" not in loc, (target, loc)


CRAFTED = [
    "Order number" + "\t" * 100_000,
    "Sold by" + "\t" * 100_000,
    "Number" + "\n\t" * 50_000,
    "Subtotal" + "\n" * 100_000,
    "Ships" + " " * 100_000,
    "(expected" * 25_000,
    "package with 9 item" + " " * 100_000,
    "9x " + "  " * 50_000,
    "*" + " " * 100_000,
    "x" + " \t" * 50_000 + "y",
    "Total" + " " * 100_000,
    "Postage" + " \t" * 50_000,
    "Order #" + " " * 100_000,
]


@pytest.mark.parametrize("text", CRAFTED, ids=range(len(CRAFTED)))
def test_crafted_paste_parses_quickly(text):
    start = time.time()
    parser.detect_import(text)
    parser.detect_import(text, shop_hint="ebay")
    assert time.time() - start < 2.0


def test_huge_paste_is_cut_short():
    start = time.time()
    parser.detect_import("A" * (parser.MAX_PASTE_CHARS * 3))
    assert time.time() - start < 5.0


def test_login_cookie_secure_behind_https_proxy(client):
    client.post("/settings/security/password", data={"password": "pass-word-phrase-1", "confirm": "pass-word-phrase-1"})
    anon = TestClient(app)
    plain = anon.post("/login", data={"password": "pass-word-phrase-1", "next": "/"}, follow_redirects=False)
    assert "secure" not in plain.headers["set-cookie"].lower()
    proxied = anon.post("/login", data={"password": "pass-word-phrase-1", "next": "/"},
                        headers={"x-forwarded-proto": "https"}, follow_redirects=False)
    assert "secure" in proxied.headers["set-cookie"].lower()


@pytest.mark.parametrize("path", ["/v2//evil.example", "/classic//evil.example", "/v2/%2Fevil.example",
                                  "/classic/%5Cevil.example", "/v2/%5C%5Cevil.example"])
def test_old_address_redirects_stay_on_site(client, path):
    loc = client.get(path, follow_redirects=False).headers["location"]
    assert loc.startswith("/") and not loc.startswith("//") and "\\" not in loc


@pytest.mark.parametrize("field_route", ["/items/new", "/import/confirm"])
def test_raw_form_next_cannot_leave_site(client, seeded, field_route):
    from urllib.parse import urlparse
    r = client.post(field_route, data={"next": "//evil.example"}, follow_redirects=False)
    loc = r.headers.get("location", "/")
    assert not urlparse(loc).netloc and not loc.startswith("//"), loc


PAYLOAD = '</script><script>alert(1)</script><img src=x onerror=alert(2)>'


def test_names_cannot_inject_script(client, fresh_db):
    """Item, shop and category names (which can come from pasted pages) are
    always escaped - in the HTML and inside the pages' scripts."""
    import sqlite3
    from .conftest import add_item
    con = sqlite3.connect(fresh_db)
    con.execute("INSERT INTO categories (name, color, has_series, sort_order) VALUES (?, '#ff00ff', 1, 99)", (f"Cat {PAYLOAD}",))
    cat = con.execute("SELECT id FROM categories WHERE name LIKE 'Cat %'").fetchone()[0]
    con.commit()
    for days in (2, -3, -20, 40):
        add_item(fresh_db, f"Evil {PAYLOAD} #{days}", 3.0, days, source=f"Shop {PAYLOAD}",
                 order_number=f"X{days}", category=cat)
    for path in ["/", "/orders", "/calendar", "/search?q=Evil", "/insights", "/insights/spend-by-shop",
                 "/insights/price-creep", "/insights/top-titles", "/alerts", "/settings"]:
        page = client.get(path).text
        assert "<script>alert(1)" not in page, path
        assert "<img src=x onerror" not in page, path


# --- cross-site request protection, headers, size limits ---------------------

def test_security_headers_on_every_page(client):
    for path in ["/", "/settings", "/login", "/static/manifest.json"]:
        h = client.get(path).headers
        assert "frame-ancestors 'none'" in h["content-security-policy"]
        assert h["x-content-type-options"] == "nosniff"
        assert h["x-frame-options"] == "DENY"
        assert h["referrer-policy"] == "same-origin"


@pytest.mark.parametrize("headers", [
    {"origin": "https://evil.example"},
    {"referer": "https://evil.example/page"},
    {"sec-fetch-site": "cross-site"},
    {"sec-fetch-site": "same-site", "origin": "https://other.example"},
    {"origin": "null"},
])
def test_forms_from_other_websites_are_blocked(client, seeded, headers):
    r = client.post("/settings/factory-reset", data={"confirm": "RESET"}, headers=headers, follow_redirects=False)
    assert r.status_code == 403
    assert "Test Series #1" in client.get("/search?q=Test").text   # nothing was reset


def test_same_site_forms_still_work(client, seeded):
    r = client.post("/settings/layout", json={"hidden": ["dash.week"]},
                    headers={"origin": "http://testserver", "sec-fetch-site": "same-origin"})
    assert r.status_code == 200
    r = client.post("/items/1/mark", data={"action": "paid", "next": "/orders"},
                    headers={"referer": "http://testserver/orders"}, follow_redirects=False)
    assert r.status_code == 303


def test_restore_refuses_oversized_files(client, seeded, monkeypatch):
    from app import core
    monkeypatch.setattr(core, "MAX_BACKUP_BYTES", 1000)
    from app import settings_pages
    monkeypatch.setattr(settings_pages, "MAX_BACKUP_BYTES", 1000)
    big = b"SQLite format 3\x00" + b"\x00" * 5000
    r = client.post("/settings/restore", files={"backup_file": ("big.db", big)}, data={"next": "/settings"}, follow_redirects=False)
    assert "too large" in r.headers["location"].replace("%20", " ")
    assert "Test Series #1" in client.get("/search?q=Test").text


def test_huge_requests_refused_before_reading(client):
    r = client.post("/import", content=b"x", headers={"content-length": str(200 * 1024 * 1024),
                                                      "content-type": "application/x-www-form-urlencoded"})
    assert r.status_code == 413
