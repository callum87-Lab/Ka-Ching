"""v3.4: the first-run setup guide (/welcome)."""
import json
import sqlite3

import pytest

from app import core
from .conftest import add_item


def _settings(path):
    return dict(sqlite3.connect(path).execute("SELECT key, value FROM settings").fetchall())


@pytest.fixture
def new_install(fresh_db):
    conn = sqlite3.connect(fresh_db)
    conn.execute("DELETE FROM settings WHERE key = 'setup_done'")
    conn.commit()
    conn.close()
    return fresh_db


def test_new_install_is_sent_to_the_guide(client, new_install):
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/welcome"
    page = client.get("/welcome").text
    assert "What do you collect?" in page and 'name="kinds" value="cards"' in page
    assert "Skip setup" in page and "Log my first order" in page


def test_existing_install_with_items_is_never_sent(client, new_install):
    add_item(new_install, "Saga #1", 3.99, 5)
    assert client.get("/", follow_redirects=False).status_code == 200


def test_finished_guide_is_not_shown_again(client, fresh_db):
    assert client.get("/", follow_redirects=False).status_code == 200
    # but it can still be opened from Settings
    assert client.get("/welcome").status_code == 200
    assert 'href="/welcome"' in client.get("/settings").text


def test_skip_setup_saves_nothing_else(client, new_install):
    r = client.post("/welcome", data={"skip_all": "1", "kinds": ["cards"], "currency": "usd"}, follow_redirects=False)
    assert r.headers["location"] == "/"
    s = _settings(new_install)
    assert s["setup_done"] == "yes"
    assert "tracking_types" not in s and s.get("currency_symbol", "gbp") == "gbp"
    assert client.get("/", follow_redirects=False).status_code == 200


def test_full_guide_saves_every_step(client, new_install):
    r = client.post("/welcome", data={
        "kinds": ["cards", "figures"], "preorder": "no",
        "card_page.titles": "off", "card_titles.series": "on",
        "currency": "usd", "budget": "$120", "cycle": "weekly",
        "theme": "noir", "layout": "compact", "then": "log",
    }, follow_redirects=False)
    assert r.headers["location"] == "/items/new"
    s = _settings(new_install)
    assert json.loads(s["tracking_types"]) == ["cards", "figures"] and s["tracking_preorder"] == "no"
    hidden = set(json.loads(s["hidden_cards"]))
    assert "page.creep" in hidden and "page.titles" in hidden and "dash.week" in hidden
    assert "titles.series" not in hidden
    assert s["currency_symbol"] == "usd" and s["monthly_budget"] == "120" and s["budget_cycle"] == "weekly"
    assert s["ui_theme"] == "noir" and s["wide_layout"] == "compact"
    assert s["setup_done"] == "yes" and "login_password_hash" not in s


def test_skipped_steps_are_left_alone(client, new_install):
    client.post("/welcome", data={"skip": ["budget", "look"], "kinds": ["comics"], "preorder": "yes",
                                  "currency": "eur", "budget": "50", "theme": "noir"})
    s = _settings(new_install)
    assert json.loads(s["tracking_types"]) == ["comics"]
    assert s.get("currency_symbol", "gbp") == "gbp" and not s.get("monthly_budget") and s.get("ui_theme") != "noir"


def test_rerun_keeps_other_hidden_cards(client, fresh_db):
    from .conftest import set_setting
    set_setting(fresh_db, "hidden_cards", json.dumps(["cal.heatmap"]))
    client.post("/welcome", data={"kinds": ["comics"], "preorder": "yes", "skip": ["pages"]})
    assert "cal.heatmap" in json.loads(_settings(fresh_db)["hidden_cards"])


@pytest.mark.parametrize("pw,confirm,err", [
    ("correct horse battery", "correct horse batteyr", "mismatch"),
    ("short", "short", "too-short"),
    ("password1234", "password1234", "too-common"),
])
def test_bad_password_goes_back_to_that_step(client, new_install, pw, confirm, err):
    r = client.post("/welcome", data={"kinds": ["cards"], "password": pw, "confirm": confirm}, follow_redirects=False)
    assert r.headers["location"] == f"/welcome?step=password&error={err}"
    s = _settings(new_install)
    assert "login_password_hash" not in s and s.get("setup_done") != "yes"
    assert json.loads(s["tracking_types"]) == ["cards"]  # the rest was saved
    page = client.get(r.headers["location"]).text
    assert 'data-start="password"' in page and 'class="err"' in page


def test_password_turns_the_login_on(client, new_install):
    r = client.post("/welcome", data={"password": "tidy marmot lantern", "confirm": "tidy marmot lantern"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/"
    assert core.login_enabled()
    assert client.get("/", follow_redirects=False).status_code == 200  # signed in by the guide
    client.cookies.clear()
    assert client.get("/", follow_redirects=False).status_code in (303, 401)
