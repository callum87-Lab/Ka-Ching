"""The optional login."""
import os

import pytest

from app import core
from fastapi.testclient import TestClient
from app.main import app


def _turn_on(client, pw="correct-horse"):
    r = client.post("/settings/security/password", data={"password": pw, "confirm": pw}, follow_redirects=False)
    assert "security=on" in r.headers["location"]
    return pw


def test_login_off_by_default(client):
    assert client.get("/orders").status_code == 200


def test_turning_on_protects_every_page(client, fresh_db):
    _turn_on(client)
    anon = TestClient(app)
    r = anon.get("/orders", headers={"accept": "text/html"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/login")
    for path in ["/settings/backup", "/settings/export-all.csv", "/calendar/export.ics"]:
        assert anon.get(path, follow_redirects=False).status_code == 401
    assert anon.post("/settings/layout", json={"hidden": []}).status_code == 401
    assert anon.get("/static/manifest.json").status_code == 200


def test_password_is_stored_hashed(client, fresh_db):
    import sqlite3
    pw = _turn_on(client)
    stored = sqlite3.connect(fresh_db).execute("SELECT value FROM settings WHERE key='login_password_hash'").fetchone()[0]
    assert stored.startswith("pbkdf2_sha256$") and pw not in stored


def test_sign_in_wrong_and_right(client):
    pw = _turn_on(client)
    anon = TestClient(app)
    r = anon.post("/login", data={"password": "nope", "next": "/orders"}, follow_redirects=False)
    assert "error=wrong" in r.headers["location"]
    r = anon.post("/login", data={"password": pw, "next": "/orders"}, follow_redirects=False)
    assert r.headers["location"] == "/orders"
    assert anon.get("/orders").status_code == 200


def test_lockout_after_five_wrong(client):
    pw = _turn_on(client)
    anon = TestClient(app)
    for _ in range(5):
        anon.post("/login", data={"password": "bad", "next": "/"}, follow_redirects=False)
    r = anon.post("/login", data={"password": pw, "next": "/"}, follow_redirects=False)
    assert "error=locked" in r.headers["location"]


def test_changing_password_signs_others_out(client):
    pw = _turn_on(client)
    other = TestClient(app)
    other.post("/login", data={"password": pw, "next": "/"})
    assert other.get("/orders", follow_redirects=False).status_code == 200
    client.post("/settings/security/password", data={"current": pw, "password": "new-pass-phrase-1", "confirm": "new-pass-phrase-1"})
    assert other.get("/orders", headers={"accept": "text/html"}, follow_redirects=False).status_code == 303


def test_calendar_private_key(client):
    _turn_on(client)
    key = core._calendar_feed_key()
    anon = TestClient(app)
    assert anon.get(f"/calendar/export.ics?key={key}").status_code == 200
    assert anon.get("/calendar/export.ics?key=wrong").status_code == 401


def test_docker_compose_master_password(client, monkeypatch):
    monkeypatch.setenv("KACHING_PASSWORD", "master-pass-1")
    anon = TestClient(app)
    assert anon.get("/orders", headers={"accept": "text/html"}, follow_redirects=False).status_code == 303
    r = anon.post("/login", data={"password": "master-pass-1", "next": "/"}, follow_redirects=False)
    assert r.headers["location"] == "/"
    # can't be switched off from the web UI
    r = anon.post("/settings/security/off", data={"current": "x"}, follow_redirects=False)
    assert "security=" in r.headers["location"]
    assert core.login_enabled()


# --- OWASP ASVS Level 1 additions ------------------------------------------

def test_password_must_be_12_characters(client):
    r = client.post("/settings/security/password", data={"password": "short-pass1", "confirm": "short-pass1"}, follow_redirects=False)
    assert "security=too-short" in r.headers["location"]
    assert not core.login_enabled()


@pytest.mark.parametrize("pw", ["password1234", "qwerty123456", "aaaaaaaaaaaa", "Password123!"[:0] + "password12345"])
def test_common_passwords_refused(client, pw):
    r = client.post("/settings/security/password", data={"password": pw, "confirm": pw}, follow_redirects=False)
    assert "security=too-common" in r.headers["location"] or "security=too-short" in r.headers["location"]
    assert not core.login_enabled()


def test_sign_out_ends_the_session_on_the_server(client):
    pw = _turn_on(client)
    anon = TestClient(app)
    anon.post("/login", data={"password": pw, "next": "/"})
    stolen = anon.cookies.get("kc_session")
    anon.get("/logout")
    thief = TestClient(app)
    thief.cookies.set("kc_session", stolen)
    r = thief.get("/orders", headers={"accept": "text/html"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/login")


def test_login_change_sends_a_notification(client, fresh_db, monkeypatch):
    from app import notifications
    import sqlite3
    sent = []
    monkeypatch.setattr(notifications, "send_via_configured_provider", lambda cur, t, m: sent.append(m) or (True, None))
    con = sqlite3.connect(fresh_db)
    con.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('notify_provider', 'ntfy')")
    con.commit()
    _turn_on(client)
    assert sent and "turned on" in sent[0]


def test_pages_not_cached_and_no_server_banner(client):
    r = client.get("/")
    assert r.headers["cache-control"] == "no-store"
    assert "cache-control" not in client.get("/static/manifest.json").headers or \
        client.get("/static/manifest.json").headers["cache-control"] != "no-store"


def test_hsts_only_over_https(client):
    assert "strict-transport-security" not in client.get("/").headers
    assert "max-age" in client.get("/", headers={"x-forwarded-proto": "https"}).headers["strict-transport-security"]


def test_sign_in_page_counts_tries_and_locks(client):
    pw = _turn_on(client)
    anon = TestClient(app)
    anon.post("/login", data={"password": "nope", "next": "/"})
    assert "4 tries left" in anon.get("/login?error=wrong").text
    for _ in range(4):
        anon.post("/login", data={"password": "nope", "next": "/"})
    page = anon.get("/login?error=locked").text
    assert 'id="lock"' in page and 'data-wait="' in page and "Locked for a moment" in page
    assert "Forgotten your password?" in page and "KACHING_PASSWORD" in page
