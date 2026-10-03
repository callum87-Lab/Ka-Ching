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
    client.post("/settings/security/password", data={"password": "pass-word-1", "confirm": "pass-word-1"})
    anon = TestClient(app)
    plain = anon.post("/login", data={"password": "pass-word-1", "next": "/"}, follow_redirects=False)
    assert "secure" not in plain.headers["set-cookie"].lower()
    proxied = anon.post("/login", data={"password": "pass-word-1", "next": "/"},
                        headers={"x-forwarded-proto": "https"}, follow_redirects=False)
    assert "secure" in proxied.headers["set-cookie"].lower()
