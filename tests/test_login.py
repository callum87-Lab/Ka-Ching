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
    client.post("/settings/security/password", data={"current": pw, "password": "new-pass-1", "confirm": "new-pass-1"})
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
