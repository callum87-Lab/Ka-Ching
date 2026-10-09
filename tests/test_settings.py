"""Layout, card order and other saved settings."""


def test_layout_ignores_unknown_cards(client):
    r = client.post("/settings/layout", json={"hidden": ["dash.week", "not.a.card", 5]})
    assert r.json()["hidden"] == ["dash.week"]


def test_card_order_rejects_unknown_page(client):
    assert client.post("/settings/card-order", json={"path": "/orders", "order": ["dash.week"]}).status_code == 400


def test_card_order_saves_and_resets(client):
    assert client.post("/settings/card-order", json={"path": "/", "order": ["dash.trend", "dash.hero"]}).json()["order"] == ["dash.trend", "dash.hero"]
    assert client.post("/settings/card-order", json={"path": "/", "order": []}).json()["order"] == []


def test_wide_layout_choice(client):
    client.post("/settings/wide-layout", data={"layout": "compact"})
    assert 'class="wide-compact"' in client.get("/").text
    client.post("/settings/wide-layout", data={"layout": "nonsense"})
    assert 'class="wide-standard"' in client.get("/").text


# --- Appearance: themes and accent colours ---------------------------------

def test_sand_is_the_default_theme(client):
    page = client.get("/").text
    assert '<html lang="en" data-theme="sand"' in page
    assert "--neon-blue: #004643" in page


def test_choosing_a_theme_and_accent(client):
    client.post("/settings/appearance", data={"theme": "grey", "accent": "mint"})
    page = client.get("/orders").text
    assert 'data-theme="grey"' in page and "--neon-blue: #5eead4" in page
    # switching theme drops an accent that doesn't belong to it
    client.post("/settings/appearance", data={"theme": "blue", "accent": "mint"})
    assert "--neon-blue: #6ee7b7" in client.get("/").text   # blue has its own mint
    client.post("/settings/appearance", data={"theme": "sand", "accent": "lime"})
    assert "--neon-blue: #004643" in client.get("/").text   # not a Sand accent -> default
    client.post("/settings/appearance", data={"theme": "noir", "accent": "kiwi"})
    page = client.get("/").text
    assert 'data-theme="noir"' in page and "--neon-blue: #9fd83a" in page


def test_unknown_theme_is_ignored(client):
    client.post("/settings/appearance", data={"theme": "neon-rainbow", "accent": "x"})
    assert 'data-theme="sand"' in client.get("/").text


def test_login_page_follows_the_theme(client):
    client.post("/settings/appearance", data={"theme": "blue", "accent": "aqua"})
    client.post("/settings/security/password", data={"password": "a-long-pass-phrase", "confirm": "a-long-pass-phrase"})
    from fastapi.testclient import TestClient
    from app.main import app
    assert 'data-theme="blue"' in TestClient(app).get("/login").text


def test_old_dashboard_cards_dropped_from_saved_order(client):
    # an order saved before v3.3 that mentions the retired cards still loads
    r = client.post("/settings/card-order", json={"path": "/", "order": ["dash.rings", "dash.trend", "dash.backup"]})
    assert r.json()["order"] == ["dash.trend"]


def test_upgrade_resets_an_old_dashboard_order(fresh_db):
    import json, sqlite3
    from app import db
    con = sqlite3.connect(fresh_db)
    con.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('card_order', ?)",
                (json.dumps({"/": ["dash.rings", "dash.trend"], "/calendar": ["cal.big", "cal.mini"]}),))
    con.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('hidden_cards', ?)",
                (json.dumps(["dash.backup", "cal.heatmap"]),))
    con.commit()
    db.init_db()
    orders = json.loads(con.execute("SELECT value FROM settings WHERE key='card_order'").fetchone()[0])
    hidden = json.loads(con.execute("SELECT value FROM settings WHERE key='hidden_cards'").fetchone()[0])
    assert "/" not in orders and orders["/calendar"] == ["cal.big", "cal.mini"]
    assert hidden == ["cal.heatmap"]


def test_old_three_columns_choice_becomes_compact(client, fresh_db):
    import sqlite3
    con = sqlite3.connect(fresh_db)
    con.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('wide_layout', 'columns')")
    con.commit()
    assert 'class="wide-compact"' in client.get("/").text


def test_white_theme_moves_to_sand(client, fresh_db):
    import sqlite3
    con = sqlite3.connect(fresh_db)
    con.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('ui_theme', 'white')")
    con.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('ui_accent', 'teal')")
    con.commit()
    page = client.get("/").text
    assert 'data-theme="sand"' in page and "--neon-blue: #004643" in page


def test_theme_change_returns_to_page(client):
    # the sidebar switcher is parked for now; the next= return path stays supported
    # picking another theme from the sidebar returns to the same page
    r = client.post("/settings/appearance", data={"theme": "noir", "next": "/orders"}, follow_redirects=False)
    assert r.headers["location"] == "/orders" and 'data-theme="noir"' in client.get("/").text
    # re-picking the current theme keeps a chosen accent
    client.post("/settings/appearance", data={"theme": "noir", "accent": "kiwi"})
    client.post("/settings/appearance", data={"theme": "noir", "next": "/"})
    assert "--neon-blue: #9fd83a" in client.get("/").text


def test_settings_sections_are_page_tabs(client):
    page = client.get("/settings").text
    assert 'class="settings-tabs"' in page and 'data-settings-tab="layout"' in page
    assert 'id="settings-sub-nav"' not in page
